"""Hostile-ZIP preflight for immutable Kirby2 pack transport bytes.

This module never extracts archive members.  It snapshots local files into exact
``bytes``, validates the complete ZIP/manifest/inventory relationship, and exposes a
narrow member reader for :mod:`kirby2.packs.staging`.  Activation and installation are
owned by later work orders.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import stat
import struct
import zlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .formats import (
    K2PACK_MANIFEST_PATH,
    load_manifest_bytes,
    require_relative_pack_path,
    require_sha256,
)
from .identity import inventory_sha256, transport_sha256
from .models import PackFileV1, PackManifestV1
from .validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackRefusalCodeV1,
    PackValidationLimitsV1,
    PackValidationPhaseV1,
    PackValidationRefused,
    enforce_pack_limit,
    refuse,
    validate_integer_compression_ratio,
    validate_manifest_complexity,
    validate_pack_member_path,
    validate_pack_member_paths,
    validate_structural_payload,
    validation_policy_id,
)


_COPY_CHUNK_BYTES = 64 * 1024
_EOCD = struct.Struct("<4s4H2LH")
_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_EOCD_SIGNATURE = b"PK\x05\x06"
_LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"
_CENTRAL_HEADER_SIGNATURE = b"PK\x01\x02"
_ZIP64_EXTRA_ID = 0x0001
_UNIX_EXTRA_ID = 0x000D
_ASI_UNIX_EXTRA_ID = 0x756E
_UTF8_FILENAME_FLAG = 0x0800
_DATA_DESCRIPTOR_FLAG = 0x0008
_ENCRYPTED_FLAGS = 0x0041
_UNSUPPORTED_GENERAL_FLAGS = 0x2020
_ALLOWED_GENERAL_FLAGS = _UTF8_FILENAME_FLAG | 0x0006
_ALLOWED_COMPRESSION_METHODS = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)


@dataclass(frozen=True, slots=True)
class PackArchiveMemberV1:
    """One payload member bound to its exact central-directory ordinal."""

    ordinal: int
    path: str
    compressed_byte_count: int
    expanded_byte_count: int
    compression_method: int
    crc32: int

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("pack archive member ordinal must be nonnegative")
        require_relative_pack_path(self.path, "pack archive member path")
        if (
            type(self.compressed_byte_count) is not int
            or self.compressed_byte_count < 0
        ):
            raise ValueError("pack archive compressed byte count must be nonnegative")
        if type(self.expanded_byte_count) is not int or self.expanded_byte_count <= 0:
            raise ValueError("pack archive expanded byte count must be positive")
        if (
            type(self.compression_method) is not int
            or self.compression_method not in _ALLOWED_COMPRESSION_METHODS
        ):
            raise ValueError("pack archive member compression method is unsupported")
        if type(self.crc32) is not int or not 0 <= self.crc32 <= 0xFFFFFFFF:
            raise ValueError("pack archive member CRC-32 is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "compressed_byte_count": self.compressed_byte_count,
            "compression_method": self.compression_method,
            "crc32": self.crc32,
            "expanded_byte_count": self.expanded_byte_count,
            "ordinal": self.ordinal,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class PackArchivePreflightV1:
    """Complete no-write preflight, not installation or activation authority."""

    manifest: PackManifestV1
    pack_id: str
    transport_sha256: str
    archive_byte_count: int
    manifest_sha256: str
    inventory_sha256: str
    payload_members: tuple[PackArchiveMemberV1, ...]
    total_expanded_byte_count: int
    validation_policy_id: str

    def __post_init__(self) -> None:
        if type(self.manifest) is not PackManifestV1:
            raise TypeError("pack archive preflight manifest is invalid")
        require_sha256(self.pack_id, "pack archive preflight pack ID")
        require_sha256(
            self.transport_sha256,
            "pack archive preflight transport digest",
        )
        require_sha256(self.manifest_sha256, "pack archive preflight manifest digest")
        require_sha256(
            self.inventory_sha256,
            "pack archive preflight inventory digest",
        )
        require_sha256(
            self.validation_policy_id,
            "pack archive validation policy ID",
        )
        if type(self.archive_byte_count) is not int or self.archive_byte_count <= 0:
            raise ValueError("pack archive preflight byte count must be positive")
        if (
            type(self.total_expanded_byte_count) is not int
            or self.total_expanded_byte_count <= 0
        ):
            raise ValueError("pack archive expanded byte count must be positive")
        if type(self.payload_members) is not tuple or any(
            type(item) is not PackArchiveMemberV1 for item in self.payload_members
        ):
            raise TypeError("pack archive payload members must be an immutable tuple")
        expected_paths = tuple(item.path for item in self.manifest.inventory)
        if tuple(item.path for item in self.payload_members) != expected_paths:
            raise ValueError("pack archive members do not follow manifest inventory order")
        if not hmac.compare_digest(self.pack_id, self.manifest.pack_id):
            raise ValueError("pack archive preflight pack ID differs from its manifest")

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_byte_count": self.archive_byte_count,
            "inventory_sha256": self.inventory_sha256,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "payload_members": [item.as_dict() for item in self.payload_members],
            "total_expanded_byte_count": self.total_expanded_byte_count,
            "transport_sha256": self.transport_sha256,
            "validation_policy_id": self.validation_policy_id,
        }


@dataclass(frozen=True, slots=True)
class _EndOfCentralDirectoryV1:
    entry_count: int
    central_offset: int
    central_size: int
    offset: int


@dataclass(frozen=True, slots=True)
class _LocalMemberBoundsV1:
    data_start: int
    data_end: int


@dataclass(frozen=True, slots=True)
class _PackArchiveReadContextV1:
    """One exact, fully rebound central-directory view used during staging."""

    archive_bytes: bytes
    preflight: PackArchivePreflightV1
    infos: tuple[zipfile.ZipInfo, ...]
    local_bounds: tuple[_LocalMemberBoundsV1, ...]
    payload_members_by_ordinal: tuple[PackArchiveMemberV1 | None, ...]
    payload_declarations_by_ordinal: tuple[PackFileV1 | None, ...]


def read_pack_archive_bytes(
    source: Path,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> bytes:
    """Read one no-follow regular file into a bounded immutable byte snapshot."""

    _require_limits(limits)
    if not isinstance(source, Path):
        raise TypeError("pack archive source must be a pathlib.Path")
    if not hasattr(os, "O_NOFOLLOW"):
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.TRANSPORT,
            "pack archive capture requires a no-follow file descriptor",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(source, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            refuse(
                PackRefusalCodeV1.ARCHIVE_MALFORMED,
                PackValidationPhaseV1.TRANSPORT,
                "pack archive source must be one non-linked regular file",
            )
        enforce_pack_limit(
            before.st_size,
            limits.maximum_archive_bytes,
            code=PackRefusalCodeV1.ARCHIVE_TOO_LARGE,
            phase=PackValidationPhaseV1.TRANSPORT,
            message="pack archive exceeds the transport byte limit",
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            enforce_pack_limit(
                total,
                limits.maximum_archive_bytes,
                code=PackRefusalCodeV1.ARCHIVE_TOO_LARGE,
                phase=PackValidationPhaseV1.TRANSPORT,
                message="pack archive exceeds the transport byte limit",
            )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or total != after.st_size:
            refuse(
                PackRefusalCodeV1.ARCHIVE_MALFORMED,
                PackValidationPhaseV1.TRANSPORT,
                "pack archive source changed while its bytes were captured",
            )
        raw = b"".join(chunks)
    except PackValidationRefused:
        raise
    except OSError:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.TRANSPORT,
            "pack archive source could not be opened as a confined regular file",
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not raw:
        refuse(
            PackRefusalCodeV1.ARCHIVE_EMPTY,
            PackValidationPhaseV1.TRANSPORT,
            "pack archive transport bytes are empty",
        )
    return raw


def preflight_pack_archive_bytes(
    archive_bytes: bytes,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    expected_pack_id: str | None = None,
    expected_transport_sha256: str | None = None,
) -> PackArchivePreflightV1:
    """Validate one entire hostile ZIP without creating filesystem entries."""

    _require_limits(limits)
    _require_archive_bytes(archive_bytes, limits)
    actual_transport_sha256 = transport_sha256(archive_bytes)
    if expected_transport_sha256 is not None:
        declared_transport = require_sha256(
            expected_transport_sha256,
            "expected pack transport digest",
        )
        if not hmac.compare_digest(actual_transport_sha256, declared_transport):
            refuse(
                PackRefusalCodeV1.EXPECTED_TRANSPORT_DIGEST_MISMATCH,
                PackValidationPhaseV1.TRANSPORT,
                "pack transport digest differs from the expected identity",
            )

    end_record = _parse_end_of_central_directory(archive_bytes, limits)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            infos = tuple(archive.infolist())
    except PackValidationRefused:
        raise
    except (
        EOFError,
        NotImplementedError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack archive central directory is malformed",
        )

    if len(infos) != end_record.entry_count:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack archive entry count differs from its end record",
        )
    enforce_pack_limit(
        len(infos),
        limits.maximum_entries,
        code=PackRefusalCodeV1.ENTRY_COUNT_LIMIT,
        phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
        message="pack archive exceeds the entry-count limit",
    )
    if not infos:
        refuse(
            PackRefusalCodeV1.MANIFEST_MISSING,
            PackValidationPhaseV1.MANIFEST,
            "pack archive does not contain manifest.toml",
        )

    paths: list[str] = []
    total_expanded = 0
    for info in infos:
        original_name = _original_name(info)
        path = validate_pack_member_path(
            original_name,
            limits=limits,
            phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
            allow_manifest=True,
        )
        _validate_central_member(info, path, limits)
        paths.append(path)
        total_expanded += info.file_size
        enforce_pack_limit(
            total_expanded,
            limits.maximum_total_expanded_bytes,
            code=PackRefusalCodeV1.TOTAL_EXPANDED_SIZE_LIMIT,
            phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
            message="pack archive exceeds the total expanded-byte limit",
        )
    manifest_ordinals = tuple(
        ordinal for ordinal, path in enumerate(paths) if path == K2PACK_MANIFEST_PATH
    )
    if not manifest_ordinals:
        refuse(
            PackRefusalCodeV1.MANIFEST_MISSING,
            PackValidationPhaseV1.MANIFEST,
            "pack archive does not contain manifest.toml",
        )
    if len(manifest_ordinals) != 1:
        refuse(
            PackRefusalCodeV1.MANIFEST_DUPLICATE,
            PackValidationPhaseV1.MANIFEST,
            "pack archive contains multiple manifest.toml entries",
        )
    canonical_paths = validate_pack_member_paths(
        tuple(paths),
        limits=limits,
        phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
        allow_manifest=True,
    )
    if tuple(paths) != canonical_paths:
        refuse(
            PackRefusalCodeV1.PATH_NONCANONICAL,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack archive path validation changed an original member name",
        )

    local_bounds = _validate_local_layout(
        archive_bytes,
        infos,
        paths,
        end_record.central_offset,
        end_record.central_size,
    )
    manifest_ordinal = manifest_ordinals[0]
    manifest_info = infos[manifest_ordinal]
    enforce_pack_limit(
        manifest_info.file_size,
        limits.maximum_manifest_bytes,
        code=PackRefusalCodeV1.MANIFEST_SIZE_LIMIT,
        phase=PackValidationPhaseV1.MANIFEST,
        member_path=K2PACK_MANIFEST_PATH,
        message="pack manifest exceeds its expanded-byte limit",
    )
    manifest_raw = _decode_member_bytes(
        archive_bytes,
        manifest_info,
        local_bounds[manifest_ordinal],
        maximum_expanded_bytes=limits.maximum_manifest_bytes,
        phase=PackValidationPhaseV1.MANIFEST,
        member_path=K2PACK_MANIFEST_PATH,
    )
    try:
        manifest = load_manifest_bytes(manifest_raw)
    except PackValidationRefused:
        raise
    except (RecursionError, TypeError, ValueError):
        refuse(
            PackRefusalCodeV1.MANIFEST_INVALID,
            PackValidationPhaseV1.MANIFEST,
            "pack manifest is not exact canonical WO39-A data",
            member_path=K2PACK_MANIFEST_PATH,
        )
    if type(manifest) is not PackManifestV1:
        refuse(
            PackRefusalCodeV1.MANIFEST_INVALID,
            PackValidationPhaseV1.MANIFEST,
            "pack manifest decoded to the wrong contract type",
            member_path=K2PACK_MANIFEST_PATH,
        )
    validate_manifest_complexity(manifest, limits=limits)

    actual_pack_id = manifest.pack_id
    if expected_pack_id is not None:
        declared_pack_id = require_sha256(expected_pack_id, "expected logical pack ID")
        if not hmac.compare_digest(actual_pack_id, declared_pack_id):
            refuse(
                PackRefusalCodeV1.EXPECTED_PACK_ID_MISMATCH,
                PackValidationPhaseV1.MANIFEST,
                "logical pack ID differs from the expected identity",
            )

    info_by_path = {path: (ordinal, infos[ordinal]) for ordinal, path in enumerate(paths)}
    declared_paths = tuple(item.path for item in manifest.inventory)
    actual_payload_paths = tuple(
        sorted(
            (path for path in paths if path != K2PACK_MANIFEST_PATH),
            key=lambda item: item.encode("utf-8"),
        )
    )
    undeclared = sorted(set(actual_payload_paths) - set(declared_paths))
    if undeclared:
        refuse(
            PackRefusalCodeV1.UNDECLARED_FILE,
            PackValidationPhaseV1.MANIFEST,
            "pack archive contains a file absent from its manifest inventory",
            member_path=undeclared[0],
        )
    missing = sorted(set(declared_paths) - set(actual_payload_paths))
    if missing:
        refuse(
            PackRefusalCodeV1.DECLARED_FILE_MISSING,
            PackValidationPhaseV1.MANIFEST,
            "pack manifest inventory declares a missing archive file",
            member_path=missing[0],
        )
    if actual_payload_paths != declared_paths:
        refuse(
            PackRefusalCodeV1.MANIFEST_MISMATCH,
            PackValidationPhaseV1.MANIFEST,
            "pack archive payload order differs from canonical inventory order",
        )

    payload_members: list[PackArchiveMemberV1] = []
    for declared_file in manifest.inventory:
        ordinal, info = info_by_path[declared_file.path]
        if info.file_size != declared_file.byte_count:
            refuse(
                PackRefusalCodeV1.DECLARED_SIZE_MISMATCH,
                PackValidationPhaseV1.MANIFEST,
                "central-directory size differs from the manifest inventory",
                member_path=declared_file.path,
                observed=info.file_size,
                limit=declared_file.byte_count,
            )
        member = PackArchiveMemberV1(
            ordinal=ordinal,
            path=declared_file.path,
            compressed_byte_count=info.compress_size,
            expanded_byte_count=info.file_size,
            compression_method=info.compress_type,
            crc32=info.CRC,
        )
        _read_verified_member_with_info(
            archive_bytes,
            member,
            declared_file,
            info,
            local_bounds[ordinal],
            limits=limits,
        )
        payload_members.append(member)

    return PackArchivePreflightV1(
        manifest=manifest,
        pack_id=actual_pack_id,
        transport_sha256=actual_transport_sha256,
        archive_byte_count=len(archive_bytes),
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        inventory_sha256=inventory_sha256(manifest),
        payload_members=tuple(payload_members),
        total_expanded_byte_count=total_expanded,
        validation_policy_id=validation_policy_id(limits),
    )


def _open_verified_archive_context(
    archive_bytes: bytes,
    preflight: PackArchivePreflightV1,
    *,
    limits: PackValidationLimitsV1,
) -> _PackArchiveReadContextV1:
    """Bind one immutable archive view in one bounded metadata/layout pass."""

    _require_limits(limits)
    _require_archive_bytes(archive_bytes, limits)
    if type(preflight) is not PackArchivePreflightV1:
        raise TypeError("pack archive context requires PackArchivePreflightV1")
    if (
        len(archive_bytes) != preflight.archive_byte_count
        or not hmac.compare_digest(
            transport_sha256(archive_bytes),
            preflight.transport_sha256,
        )
        or not hmac.compare_digest(
            validation_policy_id(limits),
            preflight.validation_policy_id,
        )
    ):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive context differs from its preflight binding",
        )

    end_record = _parse_end_of_central_directory(archive_bytes, limits)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
            infos = tuple(archive.infolist())
    except (
        EOFError,
        NotImplementedError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive changed into malformed transport bytes",
        )
    if len(infos) != end_record.entry_count:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive entry count differs from its preflighted end record",
        )
    expected_entry_count = len(preflight.payload_members) + 1
    if len(infos) != expected_entry_count:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive entry count differs from its preflight inventory",
        )

    paths: list[str] = []
    total_expanded = 0
    for info in infos:
        path = validate_pack_member_path(
            _original_name(info),
            limits=limits,
            phase=PackValidationPhaseV1.CONTENT_STREAM,
            allow_manifest=True,
        )
        _validate_central_member(
            info,
            path,
            limits,
            phase=PackValidationPhaseV1.CONTENT_STREAM,
        )
        paths.append(path)
        total_expanded += info.file_size
        enforce_pack_limit(
            total_expanded,
            limits.maximum_total_expanded_bytes,
            code=PackRefusalCodeV1.TOTAL_EXPANDED_SIZE_LIMIT,
            phase=PackValidationPhaseV1.CONTENT_STREAM,
            message="pack archive exceeds the rebound expanded-byte limit",
        )
    canonical_paths = validate_pack_member_paths(
        tuple(paths),
        limits=limits,
        phase=PackValidationPhaseV1.CONTENT_STREAM,
        allow_manifest=True,
    )
    if tuple(paths) != canonical_paths:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive paths differ from their preflight canonical form",
        )
    manifest_ordinals = tuple(
        ordinal for ordinal, path in enumerate(paths) if path == K2PACK_MANIFEST_PATH
    )
    if len(manifest_ordinals) != 1:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive manifest ordinal differs from its preflight inventory",
        )

    bounds_by_ordinal = _validate_local_layout(
        archive_bytes,
        infos,
        paths,
        end_record.central_offset,
        end_record.central_size,
    )
    manifest_ordinal = manifest_ordinals[0]
    manifest_info = infos[manifest_ordinal]
    enforce_pack_limit(
        manifest_info.file_size,
        limits.maximum_manifest_bytes,
        code=PackRefusalCodeV1.MANIFEST_SIZE_LIMIT,
        phase=PackValidationPhaseV1.CONTENT_STREAM,
        member_path=K2PACK_MANIFEST_PATH,
        message="rebound pack manifest exceeds its expanded-byte limit",
    )
    manifest_raw = _decode_member_bytes(
        archive_bytes,
        manifest_info,
        bounds_by_ordinal[manifest_ordinal],
        maximum_expanded_bytes=limits.maximum_manifest_bytes,
        phase=PackValidationPhaseV1.CONTENT_STREAM,
        member_path=K2PACK_MANIFEST_PATH,
    )
    if not hmac.compare_digest(
        hashlib.sha256(manifest_raw).hexdigest(),
        preflight.manifest_sha256,
    ):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive manifest bytes differ from their preflight digest",
            member_path=K2PACK_MANIFEST_PATH,
        )

    payload_ordinals: set[int] = set()
    payload_members_by_ordinal: list[PackArchiveMemberV1 | None] = [None] * len(infos)
    payload_declarations_by_ordinal: list[PackFileV1 | None] = [None] * len(infos)
    for member, declared_file in zip(
        preflight.payload_members,
        preflight.manifest.inventory,
        strict=True,
    ):
        if member.ordinal >= len(infos) or member.ordinal == manifest_ordinal:
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.CONTENT_STREAM,
                "preflight payload ordinal is absent or names the manifest",
                member_path=member.path,
            )
        info = infos[member.ordinal]
        if (
            paths[member.ordinal] != member.path
            or info.compress_size != member.compressed_byte_count
            or info.file_size != member.expanded_byte_count
            or info.compress_type != member.compression_method
            or info.CRC != member.crc32
        ):
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.CONTENT_STREAM,
                "pack archive central metadata differs from its preflight member",
                member_path=member.path,
            )
        if member.ordinal in payload_ordinals:
            refuse(
                PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
                PackValidationPhaseV1.CONTENT_STREAM,
                "preflight payload members reuse one central-directory ordinal",
                member_path=member.path,
            )
        payload_ordinals.add(member.ordinal)
        payload_members_by_ordinal[member.ordinal] = member
        payload_declarations_by_ordinal[member.ordinal] = declared_file
    if payload_ordinals != set(range(len(infos))) - {manifest_ordinal}:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "preflight payload ordinals do not cover the exact archive inventory",
        )
    if total_expanded != preflight.total_expanded_byte_count:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive expanded-byte total differs from its preflight",
            observed=total_expanded,
            limit=preflight.total_expanded_byte_count,
        )
    return _PackArchiveReadContextV1(
        archive_bytes=archive_bytes,
        preflight=preflight,
        infos=infos,
        local_bounds=tuple(
            bounds_by_ordinal[ordinal] for ordinal in range(len(infos))
        ),
        payload_members_by_ordinal=tuple(payload_members_by_ordinal),
        payload_declarations_by_ordinal=tuple(payload_declarations_by_ordinal),
    )


def _read_verified_context_member_bytes(
    context: _PackArchiveReadContextV1,
    member: PackArchiveMemberV1,
    declared_file: PackFileV1,
    *,
    limits: PackValidationLimitsV1,
) -> bytes:
    """Decode one member without another archive-wide metadata scan."""

    _require_limits(limits)
    if type(context) is not _PackArchiveReadContextV1:
        raise TypeError("pack staging requires a verified archive read context")
    if type(member) is not PackArchiveMemberV1:
        raise TypeError("pack staging member uses the wrong archive contract")
    if type(declared_file) is not PackFileV1:
        raise TypeError("pack staging declaration uses the wrong file contract")
    if not hmac.compare_digest(
        validation_policy_id(limits),
        context.preflight.validation_policy_id,
    ):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive reader limits differ from its verified context policy",
            member_path=declared_file.path,
        )
    if (
        member.path != declared_file.path
        or member.ordinal < 0
        or member.ordinal >= len(context.infos)
    ):
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive member was rebound to another manifest declaration",
            member_path=declared_file.path,
        )
    if context.payload_members_by_ordinal[member.ordinal] != member:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive member differs from the verified read context",
            member_path=declared_file.path,
        )
    if context.payload_declarations_by_ordinal[member.ordinal] != declared_file:
        refuse(
            PackRefusalCodeV1.STAGING_ENTRY_REBOUND,
            PackValidationPhaseV1.CONTENT_STREAM,
            "pack archive declaration differs from the verified read context",
            member_path=declared_file.path,
        )
    return _read_verified_member_with_info(
        context.archive_bytes,
        member,
        declared_file,
        context.infos[member.ordinal],
        context.local_bounds[member.ordinal],
        limits=limits,
    )


def _read_verified_member_with_info(
    archive_bytes: bytes,
    member: PackArchiveMemberV1,
    declared_file: PackFileV1,
    info: zipfile.ZipInfo,
    bounds: _LocalMemberBoundsV1,
    *,
    limits: PackValidationLimitsV1,
) -> bytes:
    if member.expanded_byte_count != declared_file.byte_count:
        refuse(
            PackRefusalCodeV1.DECLARED_SIZE_MISMATCH,
            PackValidationPhaseV1.CONTENT_STREAM,
            "preflight member size differs from the manifest inventory",
            member_path=member.path,
            observed=member.expanded_byte_count,
            limit=declared_file.byte_count,
        )
    raw = _decode_member_bytes(
        archive_bytes,
        info,
        bounds,
        maximum_expanded_bytes=min(
            declared_file.byte_count,
            limits.maximum_file_expanded_bytes,
        ),
        phase=PackValidationPhaseV1.CONTENT_STREAM,
        member_path=member.path,
    )
    if len(raw) != declared_file.byte_count:
        refuse(
            PackRefusalCodeV1.PAYLOAD_BYTE_COUNT_MISMATCH,
            PackValidationPhaseV1.CONTENT_STREAM,
            "expanded payload byte count differs from the manifest inventory",
            member_path=member.path,
            observed=len(raw),
            limit=declared_file.byte_count,
        )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_sha256, declared_file.sha256):
        refuse(
            PackRefusalCodeV1.PAYLOAD_DIGEST_MISMATCH,
            PackValidationPhaseV1.CONTENT_STREAM,
            "expanded payload digest differs from the manifest inventory",
            member_path=member.path,
        )
    validate_structural_payload(
        declared_file,
        raw,
        limits=limits,
        phase=PackValidationPhaseV1.CONTENT_STREAM,
    )
    return raw


def _require_limits(limits: PackValidationLimitsV1) -> None:
    if type(limits) is not PackValidationLimitsV1:
        raise TypeError("pack archive validation requires PackValidationLimitsV1")


def _require_archive_bytes(
    archive_bytes: bytes,
    limits: PackValidationLimitsV1,
) -> None:
    if type(archive_bytes) is not bytes:
        raise TypeError("pack archive transport must be exact immutable bytes")
    if not archive_bytes:
        refuse(
            PackRefusalCodeV1.ARCHIVE_EMPTY,
            PackValidationPhaseV1.TRANSPORT,
            "pack archive transport bytes are empty",
        )
    enforce_pack_limit(
        len(archive_bytes),
        limits.maximum_archive_bytes,
        code=PackRefusalCodeV1.ARCHIVE_TOO_LARGE,
        phase=PackValidationPhaseV1.TRANSPORT,
        message="pack archive exceeds the transport byte limit",
    )


def _parse_end_of_central_directory(
    archive_bytes: bytes,
    limits: PackValidationLimitsV1,
) -> _EndOfCentralDirectoryV1:
    minimum_offset = max(0, len(archive_bytes) - (_EOCD.size + 0xFFFF))
    candidates: list[tuple[int, tuple[object, ...]]] = []
    offset = len(archive_bytes) - _EOCD.size
    while offset >= minimum_offset:
        if archive_bytes[offset : offset + 4] == _EOCD_SIGNATURE:
            fields = _EOCD.unpack_from(archive_bytes, offset)
            comment_length = int(fields[7])
            if offset + _EOCD.size + comment_length == len(archive_bytes):
                candidates.append((offset, fields))
        offset -= 1
    if len(candidates) != 1:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack archive must contain one unambiguous terminal ZIP end record",
        )
    eocd_offset, fields = candidates[0]
    disk_number = int(fields[1])
    central_disk = int(fields[2])
    entries_on_disk = int(fields[3])
    entry_count = int(fields[4])
    central_size = int(fields[5])
    central_offset = int(fields[6])
    if disk_number != 0 or central_disk != 0 or entries_on_disk != entry_count:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "multi-disk and spanned ZIP archives are unsupported",
        )
    if entry_count == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "ZIP64 layout is outside the bounded K2PACK V1 profile",
        )
    enforce_pack_limit(
        entry_count,
        limits.maximum_entries,
        code=PackRefusalCodeV1.ENTRY_COUNT_LIMIT,
        phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
        message="pack archive exceeds the entry-count limit",
    )
    enforce_pack_limit(
        central_size,
        limits.maximum_central_directory_bytes,
        code=PackRefusalCodeV1.CENTRAL_DIRECTORY_LIMIT,
        phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
        message="pack central directory exceeds its byte limit",
    )
    if central_offset + central_size != eocd_offset:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack ZIP contains a prefix, gap, signature block, or overlapping directory",
        )
    if entry_count and archive_bytes[:4] != _LOCAL_HEADER_SIGNATURE:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack ZIP may not contain a prepended executable or container",
        )
    if entry_count and archive_bytes[central_offset : central_offset + 4] != _CENTRAL_HEADER_SIGNATURE:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack ZIP central directory has an invalid start signature",
        )
    return _EndOfCentralDirectoryV1(
        entry_count=entry_count,
        central_offset=central_offset,
        central_size=central_size,
        offset=eocd_offset,
    )


def _original_name(info: zipfile.ZipInfo) -> str:
    name = info.orig_filename
    if type(name) is not str:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack ZIP member has a non-text original filename",
        )
    return name


def _validate_central_member(
    info: zipfile.ZipInfo,
    path: str,
    limits: PackValidationLimitsV1,
    *,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CENTRAL_DIRECTORY,
) -> None:
    if info.flag_bits & _ENCRYPTED_FLAGS:
        refuse(
            PackRefusalCodeV1.ARCHIVE_ENCRYPTED,
            phase,
            "encrypted ZIP members are unsupported",
            member_path=path,
        )
    if info.flag_bits & (_DATA_DESCRIPTOR_FLAG | _UNSUPPORTED_GENERAL_FLAGS):
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            phase,
            "streaming descriptors, patching, and masked ZIP headers are unsupported",
            member_path=path,
        )
    if info.flag_bits & ~_ALLOWED_GENERAL_FLAGS:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            phase,
            "ZIP member uses unsupported general-purpose flags",
            member_path=path,
        )
    if info.compress_type not in _ALLOWED_COMPRESSION_METHODS:
        refuse(
            PackRefusalCodeV1.COMPRESSION_METHOD_UNSUPPORTED,
            phase,
            "ZIP member compression must be stored or deflated",
            member_path=path,
            observed=info.compress_type,
        )
    if info.compress_type == zipfile.ZIP_STORED and info.flag_bits & 0x0006:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            phase,
            "stored ZIP members cannot carry deflate option flags",
            member_path=path,
        )
    if info.volume != 0:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            phase,
            "ZIP member starts on an unsupported disk",
            member_path=path,
        )
    if type(info.file_size) is not int or info.file_size <= 0:
        refuse(
            PackRefusalCodeV1.DECLARED_SIZE_MISMATCH,
            phase,
            "ZIP members must declare a positive expanded size",
            member_path=path,
        )
    if type(info.compress_size) is not int or info.compress_size < 0:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            phase,
            "ZIP member has an invalid compressed size",
            member_path=path,
        )
    enforce_pack_limit(
        info.file_size,
        limits.maximum_file_expanded_bytes,
        code=PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
        phase=phase,
        member_path=path,
        message="ZIP member exceeds the per-file expanded-byte limit",
    )
    validate_integer_compression_ratio(
        info.compress_size,
        info.file_size,
        limits=limits,
        phase=phase,
        member_path=path,
    )
    _validate_extra_fields(info.extra, path, phase)
    _validate_member_file_type(info, path, phase)


def _validate_member_file_type(
    info: zipfile.ZipInfo,
    path: str,
    phase: PackValidationPhaseV1,
) -> None:
    if info.is_dir() or path.endswith("/"):
        refuse(
            PackRefusalCodeV1.ENTRY_SPECIAL_FILE,
            phase,
            "directory entries are not part of the complete file inventory",
            member_path=path,
        )
    if info.create_system == 3:
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if kind == stat.S_IFLNK:
            refuse(
                PackRefusalCodeV1.ENTRY_SYMLINK,
                phase,
                "symbolic links are forbidden in pack archives",
                member_path=path,
            )
        if kind in {stat.S_IFCHR, stat.S_IFBLK}:
            refuse(
                PackRefusalCodeV1.ENTRY_DEVICE,
                phase,
                "device entries are forbidden in pack archives",
                member_path=path,
            )
        if kind == stat.S_IFIFO:
            refuse(
                PackRefusalCodeV1.ENTRY_FIFO,
                phase,
                "FIFO entries are forbidden in pack archives",
                member_path=path,
            )
        if kind != stat.S_IFREG:
            refuse(
                PackRefusalCodeV1.ENTRY_SPECIAL_FILE,
                phase,
                "ZIP member lacks explicit regular-file metadata",
                member_path=path,
            )
        if _extra_declares_link(info.extra, kind):
            refuse(
                PackRefusalCodeV1.ENTRY_HARDLINK,
                phase,
                "hard-link metadata is forbidden in pack archives",
                member_path=path,
            )
        return
    if info.create_system == 0:
        dos_attributes = info.external_attr & 0xFFFF
        if dos_attributes & 0x58:
            refuse(
                PackRefusalCodeV1.ENTRY_SPECIAL_FILE,
                phase,
                "DOS directory, volume, and device entries are forbidden",
                member_path=path,
            )
        if any(
            field_id in {_UNIX_EXTRA_ID, _ASI_UNIX_EXTRA_ID}
            for field_id, _ in _iter_extra_fields(info.extra, path, phase)
        ):
            refuse(
                PackRefusalCodeV1.ENTRY_SPECIAL_FILE,
                phase,
                "Unix file-type metadata is forbidden on DOS-host ZIP members",
                member_path=path,
            )
        return
    refuse(
        PackRefusalCodeV1.ENTRY_SPECIAL_FILE,
        phase,
        "ZIP member uses an unsupported host file-type encoding",
        member_path=path,
    )


def _validate_extra_fields(
    extra: bytes,
    path: str,
    phase: PackValidationPhaseV1,
) -> None:
    for field_id, _ in _iter_extra_fields(extra, path, phase):
        if field_id == _ZIP64_EXTRA_ID:
            refuse(
                PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
                phase,
                "ZIP64 extra fields are outside the bounded K2PACK V1 profile",
                member_path=path,
            )


def _extra_declares_link(extra: bytes, kind: int) -> bool:
    for field_id, payload in _iter_extra_fields(
        extra,
        None,
        PackValidationPhaseV1.CENTRAL_DIRECTORY,
    ):
        if field_id == _UNIX_EXTRA_ID and len(payload) > 12:
            return True
        if field_id == _ASI_UNIX_EXTRA_ID and len(payload) > 14:
            embedded_mode = int.from_bytes(payload[4:6], "little")
            embedded_kind = stat.S_IFMT(embedded_mode)
            if embedded_kind in {stat.S_IFLNK, stat.S_IFREG} or kind == stat.S_IFREG:
                return True
    return False


def _iter_extra_fields(
    extra: bytes,
    path: str | None,
    phase: PackValidationPhaseV1,
) -> tuple[tuple[int, bytes], ...]:
    if type(extra) is not bytes:
        refuse(
            PackRefusalCodeV1.ARCHIVE_MALFORMED,
            phase,
            "ZIP extra-field storage is malformed",
            member_path=path,
        )
    fields: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            refuse(
                PackRefusalCodeV1.ARCHIVE_MALFORMED,
                phase,
                "ZIP extra field has a truncated header",
                member_path=path,
            )
        field_id, size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        end = offset + size
        if end > len(extra):
            refuse(
                PackRefusalCodeV1.ARCHIVE_MALFORMED,
                phase,
                "ZIP extra field has a truncated payload",
                member_path=path,
            )
        fields.append((field_id, extra[offset:end]))
        offset = end
    return tuple(fields)


def _validate_local_layout(
    archive_bytes: bytes,
    infos: tuple[zipfile.ZipInfo, ...],
    paths: list[str],
    central_offset: int,
    central_size: int,
) -> dict[int, _LocalMemberBoundsV1]:
    order = sorted(range(len(infos)), key=lambda ordinal: infos[ordinal].header_offset)
    central_accounted_bytes = sum(
        zipfile.sizeCentralDir
        + len(_encoded_filename(infos[ordinal], paths[ordinal], PackValidationPhaseV1.CENTRAL_DIRECTORY))
        + len(infos[ordinal].extra)
        + len(infos[ordinal].comment)
        for ordinal in range(len(infos))
    )
    if central_accounted_bytes != central_size:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack central directory contains a gap or unsupported signature record",
        )
    if not order or infos[order[0]].header_offset != 0:
        refuse(
            PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
            PackValidationPhaseV1.CENTRAL_DIRECTORY,
            "pack archive local members must start at byte zero",
        )
    result: dict[int, _LocalMemberBoundsV1] = {}
    for position, ordinal in enumerate(order):
        info = infos[ordinal]
        bounds = _local_member_bounds(
            archive_bytes,
            info,
            paths[ordinal],
            phase=PackValidationPhaseV1.CENTRAL_DIRECTORY,
        )
        expected_end = (
            infos[order[position + 1]].header_offset
            if position + 1 < len(order)
            else central_offset
        )
        if bounds.data_end != expected_end:
            refuse(
                PackRefusalCodeV1.ARCHIVE_UNSUPPORTED_LAYOUT,
                PackValidationPhaseV1.CENTRAL_DIRECTORY,
                "pack ZIP contains overlapping members or unreferenced local bytes",
                member_path=paths[ordinal],
            )
        result[ordinal] = bounds
    return result


def _local_member_bounds(
    archive_bytes: bytes,
    info: zipfile.ZipInfo,
    path: str,
    *,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CONTENT_STREAM,
) -> _LocalMemberBoundsV1:
    offset = info.header_offset
    if type(offset) is not int or offset < 0 or offset + _LOCAL_HEADER.size > len(archive_bytes):
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP member local-header offset is invalid",
            member_path=path,
        )
    fields = _LOCAL_HEADER.unpack_from(archive_bytes, offset)
    if fields[0] != _LOCAL_HEADER_SIGNATURE:
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP member local-header signature is invalid",
            member_path=path,
        )
    local_flags = int(fields[2])
    local_method = int(fields[3])
    local_crc32 = int(fields[6])
    local_compressed = int(fields[7])
    local_expanded = int(fields[8])
    filename_length = int(fields[9])
    extra_length = int(fields[10])
    name_start = offset + _LOCAL_HEADER.size
    name_end = name_start + filename_length
    extra_end = name_end + extra_length
    data_end = extra_end + info.compress_size
    if data_end > len(archive_bytes):
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP member local bytes extend beyond the transport",
            member_path=path,
        )
    expected_name = _encoded_filename(info, path, phase)
    if archive_bytes[name_start:name_end] != expected_name:
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP local and central filenames differ",
            member_path=path,
        )
    if (
        local_flags != info.flag_bits
        or local_method != info.compress_type
        or local_crc32 != info.CRC
        or local_compressed != info.compress_size
        or local_expanded != info.file_size
    ):
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP local and central metadata differ",
            member_path=path,
        )
    local_extra = archive_bytes[name_end:extra_end]
    _validate_extra_fields(
        local_extra,
        path,
        phase,
    )
    if local_extra != info.extra:
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP local and central extra fields differ",
            member_path=path,
        )
    return _LocalMemberBoundsV1(data_start=extra_end, data_end=data_end)


def _encoded_filename(
    info: zipfile.ZipInfo,
    path: str,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CONTENT_STREAM,
) -> bytes:
    encoding = "utf-8" if info.flag_bits & _UTF8_FILENAME_FLAG else "cp437"
    try:
        return info.orig_filename.encode(encoding)
    except UnicodeEncodeError:
        refuse(
            PackRefusalCodeV1.LOCAL_HEADER_MISMATCH,
            phase,
            "ZIP filename cannot round-trip through its declared encoding",
            member_path=path,
        )


def _decode_member_bytes(
    archive_bytes: bytes,
    info: zipfile.ZipInfo,
    bounds: _LocalMemberBoundsV1,
    *,
    maximum_expanded_bytes: int,
    phase: PackValidationPhaseV1,
    member_path: str,
) -> bytes:
    compressed = memoryview(archive_bytes)[bounds.data_start : bounds.data_end]
    try:
        if info.compress_type == zipfile.ZIP_STORED:
            if len(compressed) > maximum_expanded_bytes:
                refuse(
                    PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
                    phase,
                    "stored ZIP member exceeds its hard expanded-byte ceiling",
                    member_path=member_path,
                    observed=len(compressed),
                    limit=maximum_expanded_bytes,
                )
            raw = bytes(compressed)
        elif info.compress_type == zipfile.ZIP_DEFLATED:
            raw = _bounded_deflate_decode(
                compressed,
                maximum_expanded_bytes,
                phase=phase,
                member_path=member_path,
            )
        else:  # pragma: no cover - guarded by central validation
            refuse(
                PackRefusalCodeV1.COMPRESSION_METHOD_UNSUPPORTED,
                phase,
                "ZIP member compression is unsupported",
                member_path=member_path,
                observed=info.compress_type,
            )
    except PackValidationRefused:
        raise
    except (OverflowError, ValueError, zlib.error):
        refuse(
            PackRefusalCodeV1.DECOMPRESSION_FAILED,
            phase,
            "ZIP member decompression failed",
            member_path=member_path,
        )
    if len(raw) != info.file_size:
        refuse(
            PackRefusalCodeV1.PAYLOAD_BYTE_COUNT_MISMATCH,
            phase,
            "expanded ZIP member size differs from its central-directory size",
            member_path=member_path,
            observed=len(raw),
            limit=info.file_size,
        )
    if zlib.crc32(raw) & 0xFFFFFFFF != info.CRC:
        refuse(
            PackRefusalCodeV1.DECOMPRESSION_FAILED,
            phase,
            "expanded ZIP member CRC-32 differs from its central directory",
            member_path=member_path,
        )
    return raw


def _bounded_deflate_decode(
    compressed: memoryview,
    maximum_expanded_bytes: int,
    *,
    phase: PackValidationPhaseV1,
    member_path: str,
) -> bytes:
    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
    output = bytearray()
    for offset in range(0, len(compressed), _COPY_CHUNK_BYTES):
        chunk = compressed[offset : offset + _COPY_CHUNK_BYTES]
        remaining = maximum_expanded_bytes - len(output)
        decoded = decoder.decompress(chunk, remaining + 1)
        output.extend(decoded)
        if len(output) > maximum_expanded_bytes or decoder.unconsumed_tail:
            refuse(
                PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
                phase,
                "deflated ZIP member exceeds its hard expanded-byte ceiling",
                member_path=member_path,
                observed=len(output),
                limit=maximum_expanded_bytes,
            )
    remaining = maximum_expanded_bytes - len(output)
    output.extend(decoder.flush(remaining + 1))
    if len(output) > maximum_expanded_bytes:
        refuse(
            PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
            phase,
            "deflated ZIP member exceeds its hard expanded-byte ceiling",
            member_path=member_path,
            observed=len(output),
            limit=maximum_expanded_bytes,
        )
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        refuse(
            PackRefusalCodeV1.DECOMPRESSION_FAILED,
            phase,
            "deflated ZIP member has a truncated or concatenated stream",
            member_path=member_path,
        )
    return bytes(output)


__all__ = [
    "PackArchiveMemberV1",
    "PackArchivePreflightV1",
    "preflight_pack_archive_bytes",
    "read_pack_archive_bytes",
]
