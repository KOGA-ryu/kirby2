"""Byte-normative ustar/gzip packaging for the Kirby2 offline release.

This module does not discover files or fetch dependencies.  It accepts a closed,
already verified member plan and emits the single archive representation frozen by
WO40-D.  Reproducibility is therefore a property of explicit bytes, not ambient
filesystem behavior.
"""

from __future__ import annotations

import binascii
import hashlib
import re
import struct
import unicodedata
import zlib
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from kirby2.packs.formats import canonical_json_bytes, require_sha256


CANONICAL_RELEASE_ARCHIVE_ID_V1 = "CANONICAL_RELEASE_ARCHIVE_V1"
ARCHIVE_MEMBER_PLAN_SCHEMA_ID_V1 = "KIRBY2_ARCHIVE_MEMBER_PLAN_V1"
USTAR_BLOCK_SIZE_V1 = 512
GZIP_FIXED_HEADER_V1 = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"


class ReleaseSourceClassV1(str, Enum):
    CANDIDATE_PROJECT_WHEEL = "CANDIDATE_PROJECT_WHEEL"
    CANDIDATE_SOURCE = "CANDIDATE_SOURCE"
    CANDIDATE_LAUNCHER = "CANDIDATE_LAUNCHER"
    CANDIDATE_DOCUMENTATION = "CANDIDATE_DOCUMENTATION"
    CANDIDATE_ASSET = "CANDIDATE_ASSET"
    LOCKED_DEPENDENCY_WHEEL = "LOCKED_DEPENDENCY_WHEEL"
    GENERATED_MANIFEST = "GENERATED_MANIFEST"
    GENERATED_LICENSE = "GENERATED_LICENSE"
    GENERATED_NOTICE = "GENERATED_NOTICE"
    GENERATED_LAYOUT = "GENERATED_LAYOUT"
    CANDIDATE_STARTER_PACK = "CANDIDATE_STARTER_PACK"


RELEASE_SOURCE_CLASS_ORDER_V1 = tuple(item.value for item in ReleaseSourceClassV1)

_DRIVE = re.compile(r"[A-Za-z]:")
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LAUNCHER_PATHS = frozenset(
    {
        "release/launchers/macos/kirby2",
        "release/launchers/linux/kirby2",
        "release/launchers/headless/kirby2",
    }
)


def normalize_release_path(path: object, *, label: str = "release path") -> str:
    """Require one canonical root-relative POSIX path without rewriting it."""

    if type(path) is not str or not path:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", path) != path:
        raise ValueError(f"{label} must already be NFC-normalized")
    if path.startswith("/") or path.endswith("/") or "\\" in path:
        raise ValueError(f"{label} must be a relative POSIX file path")
    if _DRIVE.match(path) is not None:
        raise ValueError(f"{label} cannot contain a drive prefix")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise ValueError(f"{label} contains a control character")
    segments = path.split("/")
    if any(not item or item in {".", ".."} for item in segments):
        raise ValueError(f"{label} contains an empty or traversal segment")
    if str(PurePosixPath(path)) != path:
        raise ValueError(f"{label} is not in canonical POSIX form")
    encoded = path.encode("utf-8")
    if len(encoded) > 255:
        raise ValueError(f"{label} exceeds the POSIX ustar path limit")
    _split_ustar_path(path)
    return path


def _split_ustar_path(path: str) -> tuple[bytes, bytes]:
    encoded = path.encode("utf-8")
    if len(encoded) <= 100:
        return encoded, b""
    slash_positions = [index for index, byte in enumerate(encoded) if byte == 0x2F]
    for index in reversed(slash_positions):
        prefix = encoded[:index]
        name = encoded[index + 1 :]
        if prefix and name and len(prefix) <= 155 and len(name) <= 100:
            return name, prefix
    raise ValueError("release path is not representable by POSIX ustar")


def _octal(value: int, digits: int, label: str) -> bytes:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    encoded = format(value, f"0{digits}o").encode("ascii")
    if len(encoded) != digits:
        raise ValueError(f"{label} does not fit the canonical octal field")
    return encoded + b"\0"


@dataclass(frozen=True, slots=True)
class ArchiveMemberPlanV1:
    path: str
    payload: bytes
    source_class: ReleaseSourceClassV1
    encoder_id: str | None = None
    input_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalize_release_path(self.path, label="archive member path")
        if type(self.payload) is not bytes:
            raise TypeError("archive member payload must be exact bytes")
        if type(self.source_class) is not ReleaseSourceClassV1:
            raise TypeError("archive member source class is invalid")
        generated = self.source_class in {
            ReleaseSourceClassV1.GENERATED_MANIFEST,
            ReleaseSourceClassV1.GENERATED_LICENSE,
            ReleaseSourceClassV1.GENERATED_NOTICE,
            ReleaseSourceClassV1.GENERATED_LAYOUT,
        }
        if generated != (self.encoder_id is not None):
            raise ValueError("only generated members require an encoder ID")
        if self.encoder_id is not None:
            if type(self.encoder_id) is not str or not self.encoder_id:
                raise ValueError("generated encoder ID must be nonempty text")
            if (
                unicodedata.normalize("NFC", self.encoder_id) != self.encoder_id
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in self.encoder_id)
            ):
                raise ValueError("generated encoder ID must be canonical NFC text")
            if not self.input_digests:
                raise ValueError("generated members require complete input digests")
        elif self.input_digests:
            raise ValueError("candidate and dependency members cannot claim generated inputs")
        for digest in self.input_digests:
            require_sha256(digest, "generated-member input digest")

    @property
    def size(self) -> int:
        return len(self.payload)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def projection_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "source_class": self.source_class.value,
        }

    def plan_dict(self) -> dict[str, object]:
        return {
            **self.projection_dict(),
            "encoder_id": self.encoder_id,
            "input_digests": list(self.input_digests),
        }


@dataclass(frozen=True, slots=True)
class CanonicalArchiveV1:
    archive_root: str
    source_date_epoch: int
    members: tuple[ArchiveMemberPlanV1, ...]
    tar_bytes: bytes
    gzip_bytes: bytes

    def __post_init__(self) -> None:
        normalize_release_path(self.archive_root, label="archive root")
        if "/" in self.archive_root:
            raise ValueError("archive root must be exactly one path segment")
        if type(self.source_date_epoch) is not int or self.source_date_epoch < 0:
            raise ValueError("SOURCE_DATE_EPOCH must be a nonnegative integer")
        if type(self.members) is not tuple or not self.members:
            raise ValueError("canonical archive requires at least one member")
        if any(type(item) is not ArchiveMemberPlanV1 for item in self.members):
            raise TypeError("canonical archive members are invalid")
        if type(self.tar_bytes) is not bytes or type(self.gzip_bytes) is not bytes:
            raise TypeError("canonical archive transports must be exact bytes")
        expected_tar = canonical_tar_bytes(
            self.archive_root,
            self.members,
            source_date_epoch=self.source_date_epoch,
        )
        if self.tar_bytes != expected_tar:
            raise ValueError("canonical archive tar bytes differ from its member plan")
        if self.gzip_bytes != canonical_gzip_bytes(expected_tar):
            raise ValueError("canonical archive gzip bytes differ from its tar stream")

    @property
    def member_plan_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes([item.plan_dict() for item in self.members])
        ).hexdigest()

    @property
    def transport_sha256(self) -> str:
        return hashlib.sha256(self.gzip_bytes).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "archive_root": self.archive_root,
            "member_count": len(self.members),
            "member_plan_sha256": self.member_plan_sha256,
            "schema_id": "KIRBY2_CANONICAL_RELEASE_ARCHIVE_V1",
            "schema_version": 1,
            "source_date_epoch": self.source_date_epoch,
            "tar_sha256": hashlib.sha256(self.tar_bytes).hexdigest(),
            "transport_sha256": self.transport_sha256,
            "transport_size": len(self.gzip_bytes),
        }


def _archive_relative_path(rooted_path: str, archive_root: str) -> str:
    prefix = archive_root + "/"
    if not rooted_path.startswith(prefix):
        raise ValueError("archive member lies outside the one declared root")
    relative = rooted_path[len(prefix) :]
    normalize_release_path(relative, label="archive-root-relative path")
    return relative


def _member_mode(rooted_path: str, archive_root: str) -> int:
    relative = _archive_relative_path(rooted_path, archive_root)
    return 0o755 if relative in _LAUNCHER_PATHS else 0o644


def _ustar_header(path: str, *, size: int, mtime: int, mode: int) -> bytes:
    name, prefix = _split_ustar_path(path)
    header = bytearray(USTAR_BLOCK_SIZE_V1)
    header[0 : len(name)] = name
    header[100:108] = _octal(mode, 7, "member mode")
    header[108:116] = _octal(0, 7, "member uid")
    header[116:124] = _octal(0, 7, "member gid")
    header[124:136] = _octal(size, 11, "member size")
    header[136:148] = _octal(mtime, 11, "member mtime")
    header[148:156] = b"        "
    header[156] = ord("0")
    header[257:263] = b"ustar\0"
    header[263:265] = b"00"
    header[345 : 345 + len(prefix)] = prefix
    checksum = sum(header)
    encoded_checksum = format(checksum, "06o").encode("ascii")
    if len(encoded_checksum) != 6:
        raise ValueError("ustar checksum does not fit its canonical field")
    header[148:156] = encoded_checksum + b"\0 "
    return bytes(header)


def canonical_tar_bytes(
    archive_root: str,
    members: tuple[ArchiveMemberPlanV1, ...],
    *,
    source_date_epoch: int,
) -> bytes:
    """Encode a complete byte-normative POSIX ustar stream."""

    normalize_release_path(archive_root, label="archive root")
    if "/" in archive_root:
        raise ValueError("archive root must be one segment")
    if type(source_date_epoch) is not int or source_date_epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a nonnegative integer")
    if type(members) is not tuple or not members:
        raise ValueError("archive plan must be a nonempty tuple")
    paths = tuple(item.path for item in members)
    expected = tuple(sorted(paths, key=lambda value: value.encode("utf-8")))
    if paths != expected or len(paths) != len(set(paths)):
        raise ValueError("archive member plan must be unique and path-sorted")
    folded = tuple(path.casefold() for path in paths)
    if len(folded) != len(set(folded)):
        raise ValueError("archive member plan contains a case-fold collision")

    blocks: list[bytes] = []
    for member in members:
        if type(member) is not ArchiveMemberPlanV1:
            raise TypeError("archive plan contains an invalid member")
        _archive_relative_path(member.path, archive_root)
        blocks.append(
            _ustar_header(
                member.path,
                size=member.size,
                mtime=source_date_epoch,
                mode=_member_mode(member.path, archive_root),
            )
        )
        blocks.append(member.payload)
        padding = (-member.size) % USTAR_BLOCK_SIZE_V1
        if padding:
            blocks.append(bytes(padding))
    blocks.append(bytes(USTAR_BLOCK_SIZE_V1 * 2))
    return b"".join(blocks)


def canonical_gzip_bytes(tar_bytes: bytes) -> bytes:
    """Wrap one complete tar byte string in the exact WO40-D gzip member."""

    if type(tar_bytes) is not bytes or not tar_bytes:
        raise ValueError("gzip input must be nonempty exact tar bytes")
    compressor = zlib.compressobj(
        level=9,
        method=zlib.DEFLATED,
        wbits=-15,
        memLevel=8,
        strategy=zlib.Z_DEFAULT_STRATEGY,
    )
    deflated = compressor.compress(tar_bytes) + compressor.flush(zlib.Z_FINISH)
    trailer = struct.pack(
        "<II",
        binascii.crc32(tar_bytes) & 0xFFFFFFFF,
        len(tar_bytes) & 0xFFFFFFFF,
    )
    return GZIP_FIXED_HEADER_V1 + deflated + trailer


def build_canonical_release_archive(
    archive_root: str,
    relative_members: tuple[ArchiveMemberPlanV1, ...],
    *,
    source_date_epoch: int,
) -> CanonicalArchiveV1:
    """Prefix one closed relative plan, sort it, and encode both transports."""

    normalize_release_path(archive_root, label="archive root")
    rooted: list[ArchiveMemberPlanV1] = []
    for member in relative_members:
        if type(member) is not ArchiveMemberPlanV1:
            raise TypeError("relative member plan is invalid")
        rooted.append(
            ArchiveMemberPlanV1(
                path=f"{archive_root}/{member.path}",
                payload=member.payload,
                source_class=member.source_class,
                encoder_id=member.encoder_id,
                input_digests=member.input_digests,
            )
        )
    members = tuple(sorted(rooted, key=lambda item: item.path.encode("utf-8")))
    tar_bytes = canonical_tar_bytes(
        archive_root,
        members,
        source_date_epoch=source_date_epoch,
    )
    return CanonicalArchiveV1(
        archive_root=archive_root,
        source_date_epoch=source_date_epoch,
        members=members,
        tar_bytes=tar_bytes,
        gzip_bytes=canonical_gzip_bytes(tar_bytes),
    )


def verify_canonical_release_archive(
    archive: bytes,
    archive_root: str,
    relative_members: tuple[ArchiveMemberPlanV1, ...],
    *,
    source_date_epoch: int,
) -> CanonicalArchiveV1:
    """Re-encode a declared plan and require exact transport equality."""

    if type(archive) is not bytes:
        raise TypeError("release archive verification requires exact bytes")
    expected = build_canonical_release_archive(
        archive_root,
        relative_members,
        source_date_epoch=source_date_epoch,
    )
    if archive != expected.gzip_bytes:
        raise ValueError("release archive differs from its canonical member plan")
    if archive[:10] != GZIP_FIXED_HEADER_V1:
        raise ValueError("release archive has a noncanonical gzip header")
    if len(archive) < 18:
        raise ValueError("release archive is truncated")
    decoder = zlib.decompressobj(-15)
    restored = decoder.decompress(archive[10:-8]) + decoder.flush()
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("release archive does not contain exactly one deflate stream")
    crc32, isize = struct.unpack("<II", archive[-8:])
    if crc32 != (binascii.crc32(restored) & 0xFFFFFFFF):
        raise ValueError("release archive gzip CRC differs")
    if isize != (len(restored) & 0xFFFFFFFF):
        raise ValueError("release archive gzip size differs")
    if restored != expected.tar_bytes:
        raise ValueError("release archive restored tar differs")
    return expected


def source_archive_root(release_version: str) -> str:
    return normalize_release_path(f"kirby2-{release_version}", label="source archive root")


def desktop_archive_root(release_version: str, target: str) -> str:
    return normalize_release_path(
        f"kirby2-{release_version}-{target}", label="desktop archive root"
    )


def wheelhouse_archive_root(release_version: str, target: str) -> str:
    return normalize_release_path(
        f"kirby2-{release_version}-{target}-wheelhouse/".removesuffix("/"),
        label="wheelhouse archive root",
    )


__all__ = [
    "ARCHIVE_MEMBER_PLAN_SCHEMA_ID_V1",
    "CANONICAL_RELEASE_ARCHIVE_ID_V1",
    "GZIP_FIXED_HEADER_V1",
    "RELEASE_SOURCE_CLASS_ORDER_V1",
    "USTAR_BLOCK_SIZE_V1",
    "ArchiveMemberPlanV1",
    "CanonicalArchiveV1",
    "ReleaseSourceClassV1",
    "build_canonical_release_archive",
    "canonical_gzip_bytes",
    "canonical_tar_bytes",
    "desktop_archive_root",
    "normalize_release_path",
    "source_archive_root",
    "verify_canonical_release_archive",
    "wheelhouse_archive_root",
]
