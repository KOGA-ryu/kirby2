"""Closed validation policy and stable refusals for untrusted Kirby2 packs.

The records in this module are deliberately data-oriented.  Archive inspection and
filesystem staging share one immutable limit vector and communicate failures through
closed refusal codes; neither layer needs to infer policy from exception text.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import struct
import unicodedata
import zlib
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import NoReturn

from .formats import (
    K2PACK_MANIFEST_PATH,
    canonical_json_bytes,
    inspect_payload_format_claim,
    load_canonical_json_bytes,
    load_canonical_toml_bytes,
    require_nfc_text,
)
from .models import PackContentFormatV1, PackFileV1, PackManifestV1


PACK_VALIDATION_POLICY_SCHEMA_ID = "KIRBY2_PACK_VALIDATION_POLICY_V1"
PACK_VALIDATION_POLICY_SCHEMA_VERSION = 1
PACK_VALIDATION_POLICY_ALGORITHM = "SHA256_CANONICAL_PACK_VALIDATION_POLICY_V1"
ALLOWED_PACK_ZIP_COMPRESSION_METHODS_V1 = (0, 8)


class PackValidationPhaseV1(str, Enum):
    TRANSPORT = "TRANSPORT"
    CENTRAL_DIRECTORY = "CENTRAL_DIRECTORY"
    MANIFEST = "MANIFEST"
    CONTENT_STREAM = "CONTENT_STREAM"
    STAGE_WRITE = "STAGE_WRITE"
    STAGE_REVALIDATION = "STAGE_REVALIDATION"


class PackRefusalCodeV1(str, Enum):
    ARCHIVE_EMPTY = "ARCHIVE_EMPTY"
    ARCHIVE_TOO_LARGE = "ARCHIVE_TOO_LARGE"
    ARCHIVE_MALFORMED = "ARCHIVE_MALFORMED"
    ARCHIVE_UNSUPPORTED_LAYOUT = "ARCHIVE_UNSUPPORTED_LAYOUT"
    ARCHIVE_ENCRYPTED = "ARCHIVE_ENCRYPTED"
    ENTRY_COUNT_LIMIT = "ENTRY_COUNT_LIMIT"
    CENTRAL_DIRECTORY_LIMIT = "CENTRAL_DIRECTORY_LIMIT"

    PATH_ABSOLUTE = "PATH_ABSOLUTE"
    PATH_PARENT_TRAVERSAL = "PATH_PARENT_TRAVERSAL"
    PATH_BACKSLASH = "PATH_BACKSLASH"
    PATH_WINDOWS_DRIVE = "PATH_WINDOWS_DRIVE"
    PATH_UNC = "PATH_UNC"
    PATH_NUL = "PATH_NUL"
    PATH_NONCANONICAL = "PATH_NONCANONICAL"
    PATH_LENGTH_LIMIT = "PATH_LENGTH_LIMIT"
    PATH_DEPTH_LIMIT = "PATH_DEPTH_LIMIT"
    PATH_DUPLICATE = "PATH_DUPLICATE"
    PATH_CASEFOLD_COLLISION = "PATH_CASEFOLD_COLLISION"
    PATH_UNICODE_COLLISION = "PATH_UNICODE_COLLISION"
    PATH_FILE_DIRECTORY_COLLISION = "PATH_FILE_DIRECTORY_COLLISION"

    ENTRY_SYMLINK = "ENTRY_SYMLINK"
    ENTRY_HARDLINK = "ENTRY_HARDLINK"
    ENTRY_DEVICE = "ENTRY_DEVICE"
    ENTRY_FIFO = "ENTRY_FIFO"
    ENTRY_SPECIAL_FILE = "ENTRY_SPECIAL_FILE"
    COMPRESSION_METHOD_UNSUPPORTED = "COMPRESSION_METHOD_UNSUPPORTED"
    COMPRESSION_RATIO_LIMIT = "COMPRESSION_RATIO_LIMIT"

    MANIFEST_MISSING = "MANIFEST_MISSING"
    MANIFEST_DUPLICATE = "MANIFEST_DUPLICATE"
    MANIFEST_SIZE_LIMIT = "MANIFEST_SIZE_LIMIT"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
    EXPECTED_PACK_ID_MISMATCH = "EXPECTED_PACK_ID_MISMATCH"
    EXPECTED_TRANSPORT_DIGEST_MISMATCH = "EXPECTED_TRANSPORT_DIGEST_MISMATCH"
    DEPENDENCY_COUNT_LIMIT = "DEPENDENCY_COUNT_LIMIT"

    UNDECLARED_FILE = "UNDECLARED_FILE"
    DECLARED_FILE_MISSING = "DECLARED_FILE_MISSING"
    DECLARED_SIZE_MISMATCH = "DECLARED_SIZE_MISMATCH"
    FILE_EXPANDED_SIZE_LIMIT = "FILE_EXPANDED_SIZE_LIMIT"
    TOTAL_EXPANDED_SIZE_LIMIT = "TOTAL_EXPANDED_SIZE_LIMIT"
    LOCAL_HEADER_MISMATCH = "LOCAL_HEADER_MISMATCH"
    DECOMPRESSION_FAILED = "DECOMPRESSION_FAILED"
    PAYLOAD_BYTE_COUNT_MISMATCH = "PAYLOAD_BYTE_COUNT_MISMATCH"
    PAYLOAD_DIGEST_MISMATCH = "PAYLOAD_DIGEST_MISMATCH"
    NESTED_ARCHIVE = "NESTED_ARCHIVE"
    TYPE_SPOOFING = "TYPE_SPOOFING"
    PARSE_COMPLEXITY_LIMIT = "PARSE_COMPLEXITY_LIMIT"
    PAYLOAD_PARSER_REJECTED = "PAYLOAD_PARSER_REJECTED"

    STAGING_ROOT_UNSAFE = "STAGING_ROOT_UNSAFE"
    STAGING_TARGET_EXISTS = "STAGING_TARGET_EXISTS"
    STAGING_WRITE_FAILED = "STAGING_WRITE_FAILED"
    STAGING_TREE_MISMATCH = "STAGING_TREE_MISMATCH"
    STAGING_ENTRY_REBOUND = "STAGING_ENTRY_REBOUND"
    STAGING_REVALIDATION_FAILED = "STAGING_REVALIDATION_FAILED"
    STAGING_CLEANUP_FAILED = "STAGING_CLEANUP_FAILED"


@dataclass(frozen=True, slots=True)
class PackValidationLimitsV1:
    """One immutable resource budget shared by preflight and staging."""

    maximum_archive_bytes: int = 256 * 1024 * 1024
    maximum_manifest_bytes: int = 4 * 1024 * 1024
    maximum_entries: int = 4096
    maximum_central_directory_bytes: int = 16 * 1024 * 1024
    maximum_file_expanded_bytes: int = 128 * 1024 * 1024
    maximum_total_expanded_bytes: int = 512 * 1024 * 1024
    maximum_path_bytes: int = 1024
    maximum_path_depth: int = 32
    maximum_compression_ratio: int = 100
    maximum_dependencies: int = 256
    maximum_parse_depth: int = 64
    maximum_parse_nodes: int = 1_000_000
    maximum_event_rows: int = 1_000_000
    maximum_tabular_rows: int = 1_000_000
    maximum_parquet_row_groups: int = 4096
    maximum_parquet_columns: int = 4096
    maximum_image_pixels: int = 100_000_000

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"pack validation limit {item.name} must be positive")
        if self.maximum_manifest_bytes > self.maximum_file_expanded_bytes:
            raise ValueError("manifest limit cannot exceed the per-file expanded limit")
        if self.maximum_file_expanded_bytes > self.maximum_total_expanded_bytes:
            raise ValueError("per-file expanded limit cannot exceed the total limit")

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed_compression_methods": list(
                ALLOWED_PACK_ZIP_COMPRESSION_METHODS_V1
            ),
            **{item.name: getattr(self, item.name) for item in fields(self)},
            "schema_id": PACK_VALIDATION_POLICY_SCHEMA_ID,
            "schema_version": PACK_VALIDATION_POLICY_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackValidationLimitsV1:
        if type(value) is not dict:
            raise TypeError("pack validation policy must be one exact object")
        expected = {
            "allowed_compression_methods",
            *(item.name for item in fields(cls)),
            "schema_id",
            "schema_version",
        }
        if set(value) != expected:
            raise ValueError("pack validation policy fields differ from the V1 contract")
        if value["schema_id"] != PACK_VALIDATION_POLICY_SCHEMA_ID:
            raise ValueError("pack validation policy schema ID differs")
        if value["schema_version"] != PACK_VALIDATION_POLICY_SCHEMA_VERSION:
            raise ValueError("pack validation policy schema version differs")
        if value["allowed_compression_methods"] != list(
            ALLOWED_PACK_ZIP_COMPRESSION_METHODS_V1
        ):
            raise ValueError("pack validation compression allowlist differs")
        restored = cls(
            **{item.name: value[item.name] for item in fields(cls)}  # type: ignore[arg-type]
        )
        if restored.as_dict() != value:
            raise ValueError("pack validation policy did not round-trip exactly")
        return restored

    @property
    def validation_policy_id(self) -> str:
        return validation_policy_id(self)


DEFAULT_PACK_VALIDATION_LIMITS_V1 = PackValidationLimitsV1()


def validation_policy_id(
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> str:
    if type(limits) is not PackValidationLimitsV1:
        raise TypeError("validation policy identity requires PackValidationLimitsV1")
    projection = {
        "algorithm": PACK_VALIDATION_POLICY_ALGORITHM,
        "policy": limits.as_dict(),
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


@dataclass(frozen=True, slots=True)
class PackRefusalV1:
    """Machine-stable failure data; callers must not branch on ``message``."""

    code: PackRefusalCodeV1
    phase: PackValidationPhaseV1
    member_path: str | None
    observed: int | None
    limit: int | None
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not PackRefusalCodeV1:
            raise TypeError("pack refusal code is invalid")
        if type(self.phase) is not PackValidationPhaseV1:
            raise TypeError("pack refusal phase is invalid")
        if self.member_path is not None:
            require_nfc_text(self.member_path, "pack refusal member path", maximum_bytes=4096)
        for label, value in (("observed", self.observed), ("limit", self.limit)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"pack refusal {label} must be a nonnegative integer")
        require_nfc_text(self.message, "pack refusal message", maximum_bytes=4096)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "limit": self.limit,
            "member_path": self.member_path,
            "message": self.message,
            "observed": self.observed,
            "phase": self.phase.value,
            "schema_id": "KIRBY2_PACK_REFUSAL_V1",
            "schema_version": 1,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackRefusalV1:
        if type(value) is not dict or set(value) != {
            "code",
            "limit",
            "member_path",
            "message",
            "observed",
            "phase",
            "schema_id",
            "schema_version",
        }:
            raise ValueError("pack refusal must contain the exact V1 fields")
        if value["schema_id"] != "KIRBY2_PACK_REFUSAL_V1" or value["schema_version"] != 1:
            raise ValueError("pack refusal schema differs")
        try:
            code = PackRefusalCodeV1(value["code"])
            phase = PackValidationPhaseV1(value["phase"])
        except (TypeError, ValueError) as error:
            raise ValueError("pack refusal code or phase is unknown") from error
        restored = cls(
            code=code,
            phase=phase,
            member_path=value["member_path"],  # type: ignore[arg-type]
            observed=value["observed"],  # type: ignore[arg-type]
            limit=value["limit"],  # type: ignore[arg-type]
            message=value["message"],  # type: ignore[arg-type]
        )
        if restored.as_dict() != value:
            raise ValueError("pack refusal did not round-trip exactly")
        return restored


class PackValidationRefused(ValueError):
    """Raised for every archive-controlled validation failure."""

    def __init__(self, refusal: PackRefusalV1) -> None:
        if type(refusal) is not PackRefusalV1:
            raise TypeError("PackValidationRefused requires PackRefusalV1")
        self.refusal = refusal
        super().__init__(f"{refusal.code.value}: {refusal.message}")


def refuse(
    code: PackRefusalCodeV1,
    phase: PackValidationPhaseV1,
    message: str,
    *,
    member_path: str | None = None,
    observed: int | None = None,
    limit: int | None = None,
) -> NoReturn:
    safe_member_path = member_path
    if safe_member_path is not None:
        try:
            require_nfc_text(
                safe_member_path,
                "pack refusal member path",
                maximum_bytes=4096,
            )
        except (TypeError, ValueError):
            safe_member_path = None
    raise PackValidationRefused(
        PackRefusalV1(
            code=code,
            phase=phase,
            member_path=safe_member_path,
            observed=observed,
            limit=limit,
            message=message,
        )
    )


def require_validation_limits(value: object) -> PackValidationLimitsV1:
    if type(value) is not PackValidationLimitsV1:
        raise TypeError("pack validation requires PackValidationLimitsV1")
    return value


def validate_archive_member_path(
    value: object,
    *,
    limits: PackValidationLimitsV1,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CENTRAL_DIRECTORY,
) -> str:
    """Return one exact portable path or raise its most specific refusal code."""

    require_validation_limits(limits)
    if type(value) is not str or not value:
        refuse(PackRefusalCodeV1.PATH_NONCANONICAL, phase, "archive path is empty or non-text")
    path = value
    if "\x00" in path:
        refuse(PackRefusalCodeV1.PATH_NUL, phase, "archive path contains NUL")
    if path.startswith(("//", "\\\\")):
        refuse(PackRefusalCodeV1.PATH_UNC, phase, "archive path is a UNC path", member_path=path)
    if "\\" in path:
        refuse(PackRefusalCodeV1.PATH_BACKSLASH, phase, "archive path contains a backslash", member_path=path)
    if PurePosixPath(path).is_absolute():
        refuse(PackRefusalCodeV1.PATH_ABSOLUTE, phase, "archive path is absolute", member_path=path)
    windows = PureWindowsPath(path)
    if windows.drive:
        refuse(PackRefusalCodeV1.PATH_WINDOWS_DRIVE, phase, "archive path contains a Windows drive", member_path=path)
    raw_parts = path.split("/")
    if ".." in raw_parts:
        refuse(PackRefusalCodeV1.PATH_PARENT_TRAVERSAL, phase, "archive path traverses a parent", member_path=path)
    encoded = path.encode("utf-8", errors="surrogatepass")
    if len(encoded) > limits.maximum_path_bytes:
        refuse(
            PackRefusalCodeV1.PATH_LENGTH_LIMIT,
            phase,
            "archive path exceeds its byte limit",
            member_path=path,
            observed=len(encoded),
            limit=limits.maximum_path_bytes,
        )
    if len(raw_parts) > limits.maximum_path_depth:
        refuse(
            PackRefusalCodeV1.PATH_DEPTH_LIMIT,
            phase,
            "archive path exceeds its depth limit",
            member_path=path,
            observed=len(raw_parts),
            limit=limits.maximum_path_depth,
        )
    if (
        any(part in {"", "."} for part in raw_parts)
        or PurePosixPath(path).as_posix() != path
        or unicodedata.normalize("NFC", path) != path
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in path
        )
    ):
        refuse(PackRefusalCodeV1.PATH_NONCANONICAL, phase, "archive path is not canonical NFC POSIX", member_path=path)
    return path


def enforce_pack_limit(
    observed: int,
    limit: int,
    *,
    code: PackRefusalCodeV1,
    phase: PackValidationPhaseV1,
    message: str,
    member_path: str | None = None,
) -> int:
    """Enforce one nonnegative observation against one positive integer limit."""

    if type(observed) is not int or observed < 0:
        raise TypeError("pack validation observation must be a nonnegative integer")
    if type(limit) is not int or limit <= 0:
        raise TypeError("pack validation limit must be a positive integer")
    if type(code) is not PackRefusalCodeV1:
        raise TypeError("pack validation refusal code is invalid")
    if type(phase) is not PackValidationPhaseV1:
        raise TypeError("pack validation phase is invalid")
    if observed > limit:
        refuse(
            code,
            phase,
            message,
            member_path=member_path,
            observed=observed,
            limit=limit,
        )
    return observed


def validate_integer_compression_ratio(
    compressed_byte_count: int,
    expanded_byte_count: int,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CENTRAL_DIRECTORY,
    member_path: str | None = None,
) -> None:
    """Apply the compression-ratio policy using integer arithmetic only."""

    require_validation_limits(limits)
    if type(phase) is not PackValidationPhaseV1:
        raise TypeError("pack compression validation phase is invalid")
    if (
        type(compressed_byte_count) is not int
        or compressed_byte_count < 0
        or type(expanded_byte_count) is not int
        or expanded_byte_count < 0
    ):
        raise TypeError("pack compression byte counts must be nonnegative integers")
    permitted = compressed_byte_count * limits.maximum_compression_ratio
    if expanded_byte_count and (
        compressed_byte_count == 0 or expanded_byte_count > permitted
    ):
        refuse(
            PackRefusalCodeV1.COMPRESSION_RATIO_LIMIT,
            phase,
            "member compression ratio exceeds its integer limit",
            member_path=member_path,
            observed=expanded_byte_count,
            limit=permitted,
        )


def validate_pack_member_path(
    value: object,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CENTRAL_DIRECTORY,
    allow_manifest: bool = False,
) -> str:
    """Validate one original ZIP member name without rewriting it."""

    require_validation_limits(limits)
    if type(phase) is not PackValidationPhaseV1:
        raise TypeError("pack path validation phase is invalid")
    if type(allow_manifest) is not bool:
        raise TypeError("pack manifest path allowance must be an exact boolean")
    path = validate_archive_member_path(value, limits=limits, phase=phase)
    if path == K2PACK_MANIFEST_PATH:
        if allow_manifest:
            return path
        refuse(
            PackRefusalCodeV1.PATH_NONCANONICAL,
            phase,
            "payload path uses the reserved pack manifest name",
            member_path=path,
        )
    if path.split("/", 1)[0] == K2PACK_MANIFEST_PATH:
        refuse(
            PackRefusalCodeV1.PATH_NONCANONICAL,
            phase,
            "payload path uses the reserved pack manifest prefix",
            member_path=path,
        )
    return path


def validate_pack_member_paths(
    paths: object,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CENTRAL_DIRECTORY,
    allow_manifest: bool = False,
) -> tuple[str, ...]:
    """Validate count, spelling, and portable collisions for all member names."""

    require_validation_limits(limits)
    if type(phase) is not PackValidationPhaseV1:
        raise TypeError("pack path validation phase is invalid")
    if type(allow_manifest) is not bool:
        raise TypeError("pack manifest path allowance must be an exact boolean")
    if type(paths) not in {tuple, list} or any(type(item) is not str for item in paths):
        raise TypeError("pack member paths must be one exact text sequence")
    snapshot = tuple(paths)
    enforce_pack_limit(
        len(snapshot),
        limits.maximum_entries,
        code=PackRefusalCodeV1.ENTRY_COUNT_LIMIT,
        phase=phase,
        message="pack archive exceeds the entry-count limit",
    )
    seen: set[str] = set()
    unicode_seen: dict[str, str] = {}
    casefold_seen: dict[str, str] = {}
    for raw in snapshot:
        if raw in seen:
            refuse(
                PackRefusalCodeV1.PATH_DUPLICATE,
                phase,
                "pack archive contains a duplicate path",
                member_path=raw,
            )
        seen.add(raw)
        normalized = unicodedata.normalize("NFC", raw)
        prior_unicode = unicode_seen.get(normalized)
        if prior_unicode is not None and prior_unicode != raw:
            refuse(
                PackRefusalCodeV1.PATH_UNICODE_COLLISION,
                phase,
                "pack paths collide under Unicode normalization",
                member_path=raw,
            )
        unicode_seen[normalized] = raw
        portable = normalized.casefold()
        prior_casefold = casefold_seen.get(portable)
        if prior_casefold is not None and prior_casefold != raw:
            refuse(
                PackRefusalCodeV1.PATH_CASEFOLD_COLLISION,
                phase,
                "pack paths collide under case folding",
                member_path=raw,
            )
        casefold_seen[portable] = raw

    canonical = tuple(
        validate_pack_member_path(
            item,
            limits=limits,
            phase=phase,
            allow_manifest=allow_manifest,
        )
        for item in snapshot
    )

    exact_set = set(canonical)
    portable_set = {
        unicodedata.normalize("NFC", item).casefold() for item in canonical
    }
    for path in canonical:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            portable_prefix = unicodedata.normalize("NFC", prefix).casefold()
            if prefix in exact_set or portable_prefix in portable_set:
                refuse(
                    PackRefusalCodeV1.PATH_FILE_DIRECTORY_COLLISION,
                    phase,
                    "pack archive contains a file/directory prefix collision",
                    member_path=path,
                )
    return canonical


def validate_parse_complexity(
    value: object,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CONTENT_STREAM,
    member_path: str | None = None,
    event_rows: int | None = None,
) -> None:
    """Bound an already decoded data graph without recursive Python traversal."""

    require_validation_limits(limits)
    if type(phase) is not PackValidationPhaseV1:
        raise TypeError("pack parse validation phase is invalid")
    if event_rows is not None:
        if type(event_rows) is not int or event_rows < 0:
            raise TypeError("pack event row count must be a nonnegative integer")
        enforce_pack_limit(
            event_rows,
            limits.maximum_event_rows,
            code=PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
            phase=phase,
            member_path=member_path,
            message="event stream row count exceeds its limit",
        )
    _measure_parse_structure(
        value,
        limits=limits,
        phase=phase,
        member_path=member_path,
        maximum_nodes=limits.maximum_parse_nodes,
    )


def validate_manifest_complexity(
    manifest: PackManifestV1,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
) -> None:
    """Apply repeated-record and structural budgets after canonical decoding."""

    require_validation_limits(limits)
    if type(manifest) is not PackManifestV1:
        raise TypeError("pack manifest complexity requires PackManifestV1")
    enforce_pack_limit(
        len(manifest.dependencies),
        limits.maximum_dependencies,
        code=PackRefusalCodeV1.DEPENDENCY_COUNT_LIMIT,
        phase=PackValidationPhaseV1.MANIFEST,
        member_path=K2PACK_MANIFEST_PATH,
        message="pack dependency count exceeds its limit",
    )
    enforce_pack_limit(
        len(manifest.inventory) + 1,
        limits.maximum_entries,
        code=PackRefusalCodeV1.ENTRY_COUNT_LIMIT,
        phase=PackValidationPhaseV1.MANIFEST,
        member_path=K2PACK_MANIFEST_PATH,
        message="manifest inventory exceeds the archive entry-count limit",
    )
    validate_parse_complexity(
        manifest.as_dict(),
        limits=limits,
        phase=PackValidationPhaseV1.MANIFEST,
        member_path=K2PACK_MANIFEST_PATH,
    )


_NESTED_ARCHIVE_PREFIXES_V1 = (
    ("ZIP", b"PK\x03\x04"),
    ("ZIP_EMPTY", b"PK\x05\x06"),
    ("ZIP_SPANNED", b"PK\x07\x08"),
    ("GZIP", b"\x1f\x8b"),
    ("BZIP2", b"BZh"),
    ("XZ", b"\xfd7zXZ\x00"),
    ("SEVEN_Z", b"7z\xbc\xaf'\x1c"),
    ("RAR4", b"Rar!\x1a\x07\x00"),
    ("RAR5", b"Rar!\x1a\x07\x01\x00"),
    ("ZSTD", b"\x28\xb5\x2f\xfd"),
    ("LZIP", b"LZIP"),
    ("CAB", b"MSCF"),
)


def detect_nested_archive(raw: bytes) -> str | None:
    """Return a stable container label when payload bytes begin as an archive."""

    if type(raw) is not bytes:
        raise TypeError("nested archive inspection requires exact bytes")
    for label, signature in _NESTED_ARCHIVE_PREFIXES_V1:
        if raw.startswith(signature):
            return label
    if len(raw) >= 262 and raw[257:262] == b"ustar":
        return "TAR"
    return None


def validate_structural_payload(
    item: PackFileV1,
    raw: bytes,
    *,
    limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    phase: PackValidationPhaseV1 = PackValidationPhaseV1.CONTENT_STREAM,
) -> None:
    """Validate exact bytes, declarations, bounded parsers, and container structure."""

    require_validation_limits(limits)
    if type(item) is not PackFileV1:
        raise TypeError("structural payload validation requires PackFileV1")
    if type(raw) is not bytes:
        raise TypeError("structural payload validation requires exact bytes")
    if type(phase) is not PackValidationPhaseV1:
        raise TypeError("structural payload validation phase is invalid")
    enforce_pack_limit(
        len(raw),
        limits.maximum_file_expanded_bytes,
        code=PackRefusalCodeV1.FILE_EXPANDED_SIZE_LIMIT,
        phase=phase,
        member_path=item.path,
        message="payload exceeds the expanded-byte limit",
    )
    if len(raw) != item.byte_count:
        refuse(
            PackRefusalCodeV1.PAYLOAD_BYTE_COUNT_MISMATCH,
            phase,
            "payload byte count differs from the manifest inventory",
            member_path=item.path,
            observed=len(raw),
            limit=item.byte_count,
        )
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), item.sha256):
        refuse(
            PackRefusalCodeV1.PAYLOAD_DIGEST_MISMATCH,
            phase,
            "payload digest differs from the manifest inventory",
            member_path=item.path,
        )
    nested_kind = detect_nested_archive(raw)
    if nested_kind is not None:
        refuse(
            PackRefusalCodeV1.NESTED_ARCHIVE,
            phase,
            f"payload begins with forbidden nested archive magic: {nested_kind}",
            member_path=item.path,
        )
    try:
        _preflight_payload_lexical(item, raw, limits)
    except _PayloadComplexityError as error:
        refuse(
            PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
            phase,
            error.message,
            member_path=item.path,
            observed=error.observed,
            limit=error.limit,
        )
    except (OverflowError, RecursionError, UnicodeError, ValueError):
        refuse(
            PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
            phase,
            "payload failed bounded lexical preflight",
            member_path=item.path,
        )
    try:
        inspect_payload_format_claim(
            raw,
            path=item.path,
            content_format=item.content_format.value,
            media_type=item.media_type,
            schema_id=item.schema_id,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        code = (
            PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED
            if item.content_format
            in {
                PackContentFormatV1.TOML,
                PackContentFormatV1.CANONICAL_JSON,
                PackContentFormatV1.CANONICAL_EVENT_STREAM,
                PackContentFormatV1.REPORT_DATA,
            }
            else PackRefusalCodeV1.TYPE_SPOOFING
        )
        refuse(
            code,
            phase,
            "payload bytes do not match the declared canonical format",
            member_path=item.path,
        )

    try:
        _validate_payload_structure(item, raw, limits)
    except _PayloadComplexityError as error:
        refuse(
            PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
            phase,
            error.message,
            member_path=item.path,
            observed=error.observed,
            limit=error.limit,
        )
    except (
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        struct.error,
    ):
        refuse(
            PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
            phase,
            "payload failed bounded structural parsing",
            member_path=item.path,
        )


class _PayloadComplexityError(ValueError):
    def __init__(self, message: str, observed: int, limit: int) -> None:
        self.message = message
        self.observed = observed
        self.limit = limit
        super().__init__(message)


def _raise_lexical_node(
    nodes: int,
    depth: int,
    limits: PackValidationLimitsV1,
    *,
    label: str,
) -> int:
    nodes += 1
    if nodes > limits.maximum_parse_nodes:
        raise _PayloadComplexityError(
            f"{label} lexical node count exceeds its limit",
            nodes,
            limits.maximum_parse_nodes,
        )
    if depth > limits.maximum_parse_depth:
        raise _PayloadComplexityError(
            f"{label} lexical depth exceeds its limit",
            depth,
            limits.maximum_parse_depth,
        )
    return nodes


def _scan_json_lexical(
    raw: bytes,
    start: int,
    end: int,
    limits: PackValidationLimitsV1,
    *,
    initial_nodes: int = 0,
    require_object: bool = False,
) -> int:
    """Bound a JSON token stream before the JSON decoder can allocate a tree."""

    stack: list[int] = []
    nodes = initial_nodes
    saw_token = False
    first_token: int | None = None
    index = start
    while index < end:
        byte = raw[index]
        if byte in b" \t\r\n":
            index += 1
            continue
        if byte > 0x7F:
            raise ValueError("canonical JSON lexical input must be ASCII")
        if first_token is None:
            first_token = byte
        if byte == 0x22:
            nodes = _raise_lexical_node(
                nodes,
                len(stack) + 1,
                limits,
                label="JSON",
            )
            saw_token = True
            index += 1
            while index < end:
                byte = raw[index]
                if byte < 0x20 or byte > 0x7F:
                    raise ValueError("canonical JSON string byte is invalid")
                if byte == 0x22:
                    index += 1
                    break
                if byte == 0x5C:
                    index += 1
                    if index >= end or raw[index] not in b'"\\/bfnrtu':
                        raise ValueError("canonical JSON escape is invalid")
                    if raw[index] == 0x75:
                        if index + 4 >= end or any(
                            raw[position] not in b"0123456789abcdefABCDEF"
                            for position in range(index + 1, index + 5)
                        ):
                            raise ValueError("canonical JSON Unicode escape is truncated")
                        index += 4
                index += 1
            else:
                raise ValueError("canonical JSON string is unterminated")
            continue
        if byte in {0x7B, 0x5B}:
            nodes = _raise_lexical_node(
                nodes,
                len(stack) + 1,
                limits,
                label="JSON",
            )
            saw_token = True
            stack.append(0x7D if byte == 0x7B else 0x5D)
            index += 1
            continue
        if byte in {0x7D, 0x5D}:
            if not stack or stack.pop() != byte:
                raise ValueError("canonical JSON delimiters are unbalanced")
            index += 1
            continue
        if byte in {0x2C, 0x3A}:
            index += 1
            continue
        atom_start = index
        while index < end and raw[index] not in b" \t\r\n,:{}[]\"":
            if raw[index] > 0x7F:
                raise ValueError("canonical JSON lexical input must be ASCII")
            index += 1
        if index == atom_start:
            raise ValueError("canonical JSON contains an invalid token")
        nodes = _raise_lexical_node(
            nodes,
            len(stack) + 1,
            limits,
            label="JSON",
        )
        saw_token = True
    if stack or not saw_token:
        raise ValueError("canonical JSON lexical structure is incomplete")
    if require_object and first_token != 0x7B:
        raise ValueError("canonical event row must begin with an object")
    return nodes


def _scan_event_stream_lexical(
    raw: bytes,
    limits: PackValidationLimitsV1,
) -> None:
    if not raw.endswith(b"\n"):
        raise ValueError("canonical event stream must end with LF")
    row_count = raw.count(b"\n")
    if row_count > limits.maximum_event_rows:
        raise _PayloadComplexityError(
            "event stream row count exceeds its limit",
            row_count,
            limits.maximum_event_rows,
        )
    nodes = 0
    start = 0
    for _ in range(row_count):
        end = raw.find(b"\n", start)
        if end <= start:
            raise ValueError("canonical event stream contains an empty row")
        nodes = _scan_json_lexical(
            raw,
            start,
            end,
            limits,
            initial_nodes=nodes,
            require_object=True,
        )
        start = end + 1
    if start != len(raw):
        raise ValueError("canonical event stream row framing differs")


def _scan_toml_quoted(text: str, index: int, end: int) -> int:
    if index >= end or text[index] != '"':
        raise ValueError("canonical TOML key or string is not quoted")
    index += 1
    while index < end:
        character = text[index]
        if character == '"':
            return index + 1
        if character in "\r\n" or ord(character) < 0x20:
            raise ValueError("canonical TOML string contains a control character")
        if character == "\\":
            index += 1
            if index >= end or text[index] not in '"\\bfnrtu':
                raise ValueError("canonical TOML string escape is invalid")
            if text[index] == "u":
                if index + 4 >= end or any(
                    text[position] not in "0123456789abcdefABCDEF"
                    for position in range(index + 1, index + 5)
                ):
                    raise ValueError("canonical TOML Unicode escape is truncated")
                index += 4
        index += 1
    raise ValueError("canonical TOML string is unterminated")


def _scan_toml_value_lexical(
    text: str,
    start: int,
    end: int,
    limits: PackValidationLimitsV1,
    *,
    nodes: int,
    table_depth: int,
) -> int:
    stack: list[str] = []
    saw_value = False
    index = start
    while index < end:
        character = text[index]
        if character in " \t":
            index += 1
            continue
        if character == '"':
            nodes = _raise_lexical_node(
                nodes,
                table_depth + len(stack) + 2,
                limits,
                label="TOML",
            )
            saw_value = True
            index = _scan_toml_quoted(text, index, end)
            continue
        if character in "[{":
            nodes = _raise_lexical_node(
                nodes,
                table_depth + len(stack) + 2,
                limits,
                label="TOML",
            )
            saw_value = True
            stack.append("]" if character == "[" else "}")
            index += 1
            continue
        if character in "]}":
            if not stack or stack.pop() != character:
                raise ValueError("canonical TOML value delimiters are unbalanced")
            index += 1
            continue
        if character in ",=":
            index += 1
            continue
        atom_start = index
        while index < end and text[index] not in " \t,=[]{}\"":
            index += 1
        if index == atom_start:
            raise ValueError("canonical TOML contains an invalid value token")
        nodes = _raise_lexical_node(
            nodes,
            table_depth + len(stack) + 2,
            limits,
            label="TOML",
        )
        saw_value = True
    if stack or not saw_value:
        raise ValueError("canonical TOML value is incomplete")
    return nodes


def _scan_toml_lexical(raw: bytes, limits: PackValidationLimitsV1) -> None:
    """Bound canonical TOML tables and values before tomllib materialization."""

    text = raw.decode("utf-8")
    if not text.endswith("\n"):
        raise ValueError("canonical TOML must end with LF")
    nodes = _raise_lexical_node(0, 1, limits, label="TOML")
    table_depth = 0
    line_start = 0
    while line_start < len(text):
        line_end = text.find("\n", line_start)
        if line_end < 0:
            raise ValueError("canonical TOML line framing differs")
        start = line_start
        while start < line_end and text[start] in " \t":
            start += 1
        end = line_end
        while end > start and text[end - 1] in " \t":
            end -= 1
        if start != end:
            if text[start] == "[":
                if text[end - 1] != "]" or start + 2 > end:
                    raise ValueError("canonical TOML table header is malformed")
                cursor = start + 1
                components = 0
                while cursor < end - 1:
                    while cursor < end - 1 and text[cursor] == " ":
                        cursor += 1
                    cursor = _scan_toml_quoted(text, cursor, end - 1)
                    components += 1
                    while cursor < end - 1 and text[cursor] == " ":
                        cursor += 1
                    if cursor < end - 1:
                        if text[cursor] != ".":
                            raise ValueError("canonical TOML table path is malformed")
                        cursor += 1
                if components == 0:
                    raise ValueError("canonical TOML table path is empty")
                for component_depth in range(1, components + 1):
                    nodes = _raise_lexical_node(
                        nodes,
                        component_depth + 1,
                        limits,
                        label="TOML",
                    )
                    nodes = _raise_lexical_node(
                        nodes,
                        component_depth + 1,
                        limits,
                        label="TOML",
                    )
                table_depth = components
            else:
                cursor = _scan_toml_quoted(text, start, end)
                while cursor < end and text[cursor] == " ":
                    cursor += 1
                if cursor >= end or text[cursor] != "=":
                    raise ValueError("canonical TOML assignment is malformed")
                nodes = _raise_lexical_node(
                    nodes,
                    table_depth + 2,
                    limits,
                    label="TOML",
                )
                nodes = _scan_toml_value_lexical(
                    text,
                    cursor + 1,
                    end,
                    limits,
                    nodes=nodes,
                    table_depth=table_depth,
                )
        line_start = line_end + 1


def _preflight_payload_lexical(
    item: PackFileV1,
    raw: bytes,
    limits: PackValidationLimitsV1,
) -> None:
    if item.content_format is PackContentFormatV1.TOML:
        _scan_toml_lexical(raw, limits)
    elif item.content_format in {
        PackContentFormatV1.CANONICAL_JSON,
        PackContentFormatV1.REPORT_DATA,
    }:
        _scan_json_lexical(raw, 0, len(raw), limits)
    elif item.content_format is PackContentFormatV1.CANONICAL_EVENT_STREAM:
        _scan_event_stream_lexical(raw, limits)


def _measure_parse_structure(
    value: object,
    *,
    limits: PackValidationLimitsV1,
    phase: PackValidationPhaseV1,
    member_path: str | None,
    maximum_nodes: int,
) -> int:
    stack: list[tuple[object, int]] = [(value, 1)]
    seen_containers: set[int] = set()
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > maximum_nodes:
            refuse(
                PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
                phase,
                "payload parse node count exceeds its limit",
                member_path=member_path,
                observed=nodes,
                limit=maximum_nodes,
            )
        if depth > limits.maximum_parse_depth:
            refuse(
                PackRefusalCodeV1.PARSE_COMPLEXITY_LIMIT,
                phase,
                "payload parse depth exceeds its limit",
                member_path=member_path,
                observed=depth,
                limit=limits.maximum_parse_depth,
            )
        if type(current) is dict:
            identity = id(current)
            if identity in seen_containers:
                refuse(
                    PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
                    phase,
                    "payload parse graph contains an alias or cycle",
                    member_path=member_path,
                )
            seen_containers.add(identity)
            if any(type(key) is not str for key in current):
                refuse(
                    PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
                    phase,
                    "payload parse graph contains a non-text object key",
                    member_path=member_path,
                )
            stack.extend((item, depth + 1) for pair in current.items() for item in pair)
        elif type(current) in {list, tuple}:
            identity = id(current)
            if identity in seen_containers:
                refuse(
                    PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
                    phase,
                    "payload parse graph contains an alias or cycle",
                    member_path=member_path,
                )
            seen_containers.add(identity)
            stack.extend((item, depth + 1) for item in current)
        elif current is None or type(current) in {str, bool, int}:
            continue
        else:
            refuse(
                PackRefusalCodeV1.PAYLOAD_PARSER_REJECTED,
                phase,
                "payload parse graph contains an unsupported value type",
                member_path=member_path,
            )
    return nodes


def _validate_payload_structure(
    item: PackFileV1,
    raw: bytes,
    limits: PackValidationLimitsV1,
) -> None:
    if item.content_format is PackContentFormatV1.TOML:
        value = load_canonical_toml_bytes(raw, item.path)
        _raise_on_parse_metrics(value, limits)
        return
    if item.content_format in {
        PackContentFormatV1.CANONICAL_JSON,
        PackContentFormatV1.REPORT_DATA,
    }:
        value = load_canonical_json_bytes(raw, item.path)
        _raise_on_parse_metrics(value, limits)
        return
    if item.content_format is PackContentFormatV1.CANONICAL_EVENT_STREAM:
        _validate_event_stream_structure(raw, limits)
        return
    if item.content_format is PackContentFormatV1.PARQUET:
        _validate_parquet_structure(raw, limits)
        return
    if item.content_format is PackContentFormatV1.BINARY_EVIDENCE:
        _validate_binary_structure(raw, item.media_type, limits)


def _raise_on_parse_metrics(
    value: object,
    limits: PackValidationLimitsV1,
    maximum_nodes: int | None = None,
) -> int:
    node_limit = limits.maximum_parse_nodes if maximum_nodes is None else maximum_nodes
    if type(node_limit) is not int or node_limit <= 0:
        raise _PayloadComplexityError(
            "payload parse node count exceeds its limit",
            limits.maximum_parse_nodes + 1,
            limits.maximum_parse_nodes,
        )
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > node_limit:
            raise _PayloadComplexityError(
                "payload parse node count exceeds its limit",
                limits.maximum_parse_nodes + 1,
                limits.maximum_parse_nodes,
            )
        if depth > limits.maximum_parse_depth:
            raise _PayloadComplexityError(
                "payload parse depth exceeds its limit",
                depth,
                limits.maximum_parse_depth,
            )
        if type(current) is dict:
            stack.extend((item, depth + 1) for pair in current.items() for item in pair)
        elif type(current) in {list, tuple}:
            stack.extend((item, depth + 1) for item in current)
    return nodes


def _validate_event_stream_structure(
    raw: bytes,
    limits: PackValidationLimitsV1,
) -> None:
    row_count = raw.count(b"\n")
    if row_count > limits.maximum_event_rows:
        raise _PayloadComplexityError(
            "event stream row count exceeds its limit",
            row_count,
            limits.maximum_event_rows,
        )
    nodes = 0
    stream = io.BytesIO(raw)
    for ordinal, row in enumerate(stream, start=1):
        value = load_canonical_json_bytes(row[:-1], f"event row {ordinal}")
        nodes += _raise_on_parse_metrics(
            value,
            limits,
            limits.maximum_parse_nodes - nodes,
        )
        if nodes > limits.maximum_parse_nodes:
            raise _PayloadComplexityError(
                "event stream parse node count exceeds its limit",
                nodes,
                limits.maximum_parse_nodes,
            )


class _CompactProtocolReader:
    __slots__ = (
        "_data",
        "_end",
        "_limits",
        "_node_limit",
        "_nodes",
        "_offset",
        "_start",
    )

    def __init__(
        self,
        data: bytes,
        limits: PackValidationLimitsV1,
        *,
        start: int = 0,
        end: int | None = None,
        node_limit: int | None = None,
    ) -> None:
        terminal = len(data) if end is None else end
        if (
            type(start) is not int
            or type(terminal) is not int
            or start < 0
            or terminal < start
            or terminal > len(data)
        ):
            raise ValueError("compact-protocol reader bounds are invalid")
        maximum_nodes = limits.maximum_parse_nodes if node_limit is None else node_limit
        if type(maximum_nodes) is not int or maximum_nodes <= 0:
            raise _PayloadComplexityError(
                "Parquet metadata node count exceeds its limit",
                limits.maximum_parse_nodes + 1,
                limits.maximum_parse_nodes,
            )
        self._data = data
        self._end = terminal
        self._limits = limits
        self._node_limit = maximum_nodes
        self._nodes = 0
        self._offset = start
        self._start = start

    @property
    def absolute_offset(self) -> int:
        return self._offset

    @property
    def nodes(self) -> int:
        return self._nodes

    @property
    def offset(self) -> int:
        return self._offset - self._start

    def _node(self) -> None:
        self._nodes += 1
        if self._nodes > self._node_limit:
            raise _PayloadComplexityError(
                "Parquet metadata node count exceeds its limit",
                self._nodes,
                self._node_limit,
            )

    def _take(self, count: int) -> bytes:
        if type(count) is not int or count < 0 or self._offset + count > self._end:
            raise ValueError("truncated compact-protocol value")
        result = self._data[self._offset : self._offset + count]
        self._offset += count
        return result

    def _varint(self) -> int:
        value = 0
        shift = 0
        for _ in range(10):
            byte = self._take(1)[0]
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
        raise ValueError("compact-protocol varint is too long")

    def _signed_varint(self) -> int:
        raw = self._varint()
        return (raw >> 1) ^ -(raw & 1)

    def _list_header(self) -> tuple[int, int]:
        header = self._take(1)[0]
        size = header >> 4
        element_type = header & 0x0F
        if size == 15:
            size = self._varint()
        if size > self._node_limit - self._nodes:
            raise _PayloadComplexityError(
                "Parquet collection size exceeds its limit",
                self._nodes + size,
                self._node_limit,
            )
        return size, element_type

    def read_value(self, compact_type: int, depth: int) -> object:
        self._node()
        if depth > self._limits.maximum_parse_depth:
            raise _PayloadComplexityError(
                "Parquet metadata depth exceeds its limit",
                depth,
                self._limits.maximum_parse_depth,
            )
        if compact_type == 1:
            return True
        if compact_type == 2:
            return False
        if compact_type == 3:
            return int.from_bytes(self._take(1), "little", signed=True)
        if compact_type in {4, 5, 6}:
            return self._signed_varint()
        if compact_type == 7:
            return struct.unpack("<d", self._take(8))[0]
        if compact_type == 8:
            size = self._varint()
            if size > self._limits.maximum_central_directory_bytes:
                raise _PayloadComplexityError(
                    "Parquet metadata binary value exceeds its byte limit",
                    size,
                    self._limits.maximum_central_directory_bytes,
                )
            return self._take(size)
        if compact_type in {9, 10}:
            size, element_type = self._list_header()
            return tuple(
                self.read_value(element_type, depth + 1) for _ in range(size)
            )
        if compact_type == 11:
            size = self._varint()
            if size > (self._node_limit - self._nodes) // 2:
                raise _PayloadComplexityError(
                    "Parquet map size exceeds its limit",
                    self._nodes + size * 2,
                    self._node_limit,
                )
            if not size:
                return ()
            types = self._take(1)[0]
            key_type, value_type = types >> 4, types & 0x0F
            return tuple(
                (
                    self.read_value(key_type, depth + 1),
                    self.read_value(value_type, depth + 1),
                )
                for _ in range(size)
            )
        if compact_type == 12:
            return self.read_struct(depth + 1)
        raise ValueError("unsupported compact-protocol type")

    def read_struct(self, depth: int) -> dict[int, object]:
        if depth > self._limits.maximum_parse_depth:
            raise _PayloadComplexityError(
                "Parquet metadata depth exceeds its limit",
                depth,
                self._limits.maximum_parse_depth,
            )
        result: dict[int, object] = {}
        last_field_id = 0
        while True:
            header = self._take(1)[0]
            compact_type = header & 0x0F
            if compact_type == 0:
                return result
            delta = header >> 4
            if delta:
                field_id = last_field_id + delta
            else:
                field_id = self._signed_varint()
            if field_id <= 0 or field_id in result:
                raise ValueError("compact-protocol field ID is invalid or duplicate")
            last_field_id = field_id
            result[field_id] = self.read_value(compact_type, depth + 1)


def _parquet_int(
    fields: dict[int, object],
    field_id: int,
    label: str,
    *,
    minimum: int = 0,
) -> int:
    value = fields.get(field_id)
    if type(value) is not int or value < minimum:
        raise ValueError(f"Parquet {label} is absent or invalid")
    return value


def _parquet_optional_int(
    fields: dict[int, object],
    field_id: int,
    label: str,
    *,
    minimum: int = 0,
) -> int | None:
    value = fields.get(field_id)
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise ValueError(f"Parquet {label} is invalid")
    return value


def _parquet_text(value: object, label: str) -> str:
    if type(value) is not bytes or not value:
        raise ValueError(f"Parquet {label} is absent or invalid")
    result = value.decode("utf-8")
    if not result or unicodedata.normalize("NFC", result) != result:
        raise ValueError(f"Parquet {label} is not canonical UTF-8 text")
    return result


def _validate_parquet_schema(
    schema: object,
    limits: PackValidationLimitsV1,
) -> tuple[tuple[tuple[str, ...], int], ...]:
    if type(schema) is not tuple or not schema:
        raise ValueError("Parquet footer has no schema elements")
    if len(schema) > limits.maximum_parquet_columns:
        raise _PayloadComplexityError(
            "Parquet schema element count exceeds its limit",
            len(schema),
            limits.maximum_parquet_columns,
        )
    if any(type(element) is not dict for element in schema):
        raise ValueError("Parquet schema element has the wrong type")
    root = schema[0]
    _parquet_text(root.get(4), "root schema name")
    root_children = _parquet_int(root, 5, "root child count", minimum=1)
    if 1 in root or 3 in root:
        raise ValueError("Parquet root schema element is not a group")

    frames: list[list[object]] = [[root_children, ()]]
    leaves: list[tuple[tuple[str, ...], int]] = []
    for element in schema[1:]:
        while frames and frames[-1][0] == 0:
            frames.pop()
        if not frames:
            raise ValueError("Parquet schema contains an orphan element")
        frames[-1][0] = int(frames[-1][0]) - 1
        parent_path = frames[-1][1]
        if type(parent_path) is not tuple:
            raise ValueError("Parquet schema traversal state is invalid")
        name = _parquet_text(element.get(4), "schema element name")
        if _parquet_int(element, 3, "schema repetition type") not in {0, 1, 2}:
            raise ValueError("Parquet schema repetition type is invalid")
        path = (*parent_path, name)
        children = _parquet_optional_int(element, 5, "schema child count")
        if children is not None and children > 0:
            if 1 in element:
                raise ValueError("Parquet group schema element declares a physical type")
            if len(path) + 1 > limits.maximum_parse_depth:
                raise _PayloadComplexityError(
                    "Parquet schema depth exceeds its limit",
                    len(path) + 1,
                    limits.maximum_parse_depth,
                )
            frames.append([children, path])
        else:
            physical_type = _parquet_int(element, 1, "physical column type")
            if physical_type not in set(range(9)):
                raise ValueError("Parquet physical column type is invalid")
            leaves.append((path, physical_type))
    while frames and frames[-1][0] == 0:
        frames.pop()
    if frames or not leaves:
        raise ValueError("Parquet flattened schema tree is incomplete")
    if len(leaves) > limits.maximum_parquet_columns:
        raise _PayloadComplexityError(
            "Parquet leaf column count exceeds its limit",
            len(leaves),
            limits.maximum_parquet_columns,
        )
    return tuple(leaves)


def _validate_parquet_pages(
    raw: bytes,
    start: int,
    end: int,
    data_offset: int,
    index_offset: int | None,
    dictionary_offset: int | None,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[int, int]:
    cursor = start
    pages = 0
    data_values = 0
    decoded_bytes = 0
    saw_dictionary = False
    page_types: dict[int, int] = {}
    while cursor < end:
        reader = _CompactProtocolReader(
            raw,
            limits,
            start=cursor,
            end=end,
            node_limit=limits.maximum_parse_nodes - budget[0],
        )
        header = reader.read_struct(1)
        budget[0] += reader.nodes
        header_end = reader.absolute_offset
        if header_end <= cursor:
            raise ValueError("Parquet page header is empty")
        page_type = _parquet_int(header, 1, "page type")
        if page_type not in {0, 1, 2, 3}:
            raise ValueError("Parquet page type is unsupported")
        uncompressed_size = _parquet_int(
            header,
            2,
            "uncompressed page size",
            minimum=1,
        )
        if uncompressed_size > limits.maximum_file_expanded_bytes:
            raise _PayloadComplexityError(
                "Parquet page uncompressed size exceeds its limit",
                uncompressed_size,
                limits.maximum_file_expanded_bytes,
            )
        compressed_size = _parquet_int(
            header,
            3,
            "compressed page size",
            minimum=1,
        )
        expected_detail = {0: 5, 1: 6, 2: 7, 3: 8}[page_type]
        detail = header.get(expected_detail)
        if type(detail) is not dict:
            raise ValueError("Parquet page lacks its type-specific header")
        if page_type == 2:
            if saw_dictionary or cursor != start or cursor != dictionary_offset:
                raise ValueError("Parquet dictionary page is duplicate or not first")
            saw_dictionary = True
        if page_type == 0:
            page_values = _parquet_int(
                detail,
                1,
                "data-page value count",
                minimum=1,
            )
            encodings = (
                _parquet_int(detail, 2, "data-page value encoding"),
                _parquet_int(detail, 3, "data-page definition encoding"),
                _parquet_int(detail, 4, "data-page repetition encoding"),
            )
            if page_values > limits.maximum_tabular_rows or any(
                encoding not in set(range(10)) for encoding in encodings
            ):
                raise ValueError("Parquet data-page counts or encodings are invalid")
        elif page_type == 2:
            dictionary_values = _parquet_int(
                detail,
                1,
                "dictionary-page value count",
                minimum=1,
            )
            dictionary_encoding = _parquet_int(
                detail,
                2,
                "dictionary-page encoding",
            )
            if (
                dictionary_values > limits.maximum_tabular_rows
                or dictionary_encoding not in set(range(10))
            ):
                raise ValueError("Parquet dictionary-page metadata is invalid")
        elif page_type == 3:
            page_values = _parquet_int(
                detail,
                1,
                "data-page-v2 value count",
                minimum=1,
            )
            nulls = _parquet_int(detail, 2, "data-page-v2 null count")
            rows = _parquet_int(
                detail,
                3,
                "data-page-v2 row count",
                minimum=1,
            )
            encoding = _parquet_int(detail, 4, "data-page-v2 encoding")
            definition_bytes = _parquet_int(
                detail,
                5,
                "data-page-v2 definition-level bytes",
            )
            repetition_bytes = _parquet_int(
                detail,
                6,
                "data-page-v2 repetition-level bytes",
            )
            if (
                page_values > limits.maximum_tabular_rows
                or rows > limits.maximum_tabular_rows
                or nulls > page_values
                or encoding not in set(range(10))
                or definition_bytes + repetition_bytes > compressed_size
            ):
                raise ValueError("Parquet data-page-v2 metadata is invalid")
        if page_type in {0, 3}:
            data_values += page_values
            if data_values > limits.maximum_tabular_rows:
                raise _PayloadComplexityError(
                    "Parquet column page values exceed their limit",
                    data_values,
                    limits.maximum_tabular_rows,
                )
        page_end = header_end + compressed_size
        if page_end > end:
            raise ValueError("Parquet page data exceeds its column chunk")
        decoded_bytes += (header_end - cursor) + uncompressed_size
        if decoded_bytes > limits.maximum_file_expanded_bytes:
            raise _PayloadComplexityError(
                "Parquet column page bytes exceed their decoded limit",
                decoded_bytes,
                limits.maximum_file_expanded_bytes,
            )
        checksum = header.get(4)
        if checksum is not None:
            if type(checksum) is not int or (
                checksum & 0xFFFFFFFF
            ) != zlib.crc32(raw[header_end:page_end]) & 0xFFFFFFFF:
                raise ValueError("Parquet page checksum differs from its body")
        pages += 1
        if pages > limits.maximum_parse_nodes:
            raise _PayloadComplexityError(
                "Parquet page count exceeds its limit",
                pages,
                limits.maximum_parse_nodes,
            )
        page_types[cursor] = page_type
        cursor = page_end
    if cursor != end or pages == 0:
        raise ValueError("Parquet column chunk page framing differs")
    if page_types.get(data_offset) not in {0, 3}:
        raise ValueError("Parquet data-page offset does not reference a data page")
    if index_offset is not None and page_types.get(index_offset) != 1:
        raise ValueError("Parquet index-page offset does not reference an index page")
    if dictionary_offset is not None and page_types.get(dictionary_offset) != 2:
        raise ValueError("Parquet dictionary-page offset does not reference a dictionary page")
    return data_values, decoded_bytes


def _validate_parquet_column_chunk(
    raw: bytes,
    chunk: object,
    expected_path: tuple[str, ...],
    expected_physical_type: int,
    footer_start: int,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[int, int, int, int, tuple[tuple[int, int], ...]]:
    if type(chunk) is not dict:
        raise ValueError("Parquet row-group column chunk has the wrong type")
    if 1 in chunk:
        raise ValueError("Parquet external column-chunk files are forbidden")
    file_offset = _parquet_int(chunk, 2, "column file offset", minimum=4)
    metadata = chunk.get(3)
    if type(metadata) is not dict:
        raise ValueError("Parquet column chunk lacks metadata")
    physical_type = _parquet_int(metadata, 1, "column physical type")
    if physical_type != expected_physical_type:
        raise ValueError("Parquet column physical type differs from its schema leaf")
    encodings = metadata.get(2)
    path = metadata.get(3)
    if (
        type(encodings) is not tuple
        or not encodings
        or any(type(value) is not int or value not in set(range(10)) for value in encodings)
        or type(path) is not tuple
        or not path
    ):
        raise ValueError("Parquet column encodings or schema path are invalid")
    decoded_path = tuple(_parquet_text(value, "column path") for value in path)
    if decoded_path != expected_path:
        raise ValueError("Parquet column path differs from the footer schema")
    codec = _parquet_int(metadata, 4, "column compression codec")
    if codec not in set(range(8)):
        raise ValueError("Parquet column compression codec is unsupported")
    values = _parquet_int(metadata, 5, "column value count")
    uncompressed_size = _parquet_int(metadata, 6, "column uncompressed size")
    if values > limits.maximum_tabular_rows:
        raise _PayloadComplexityError(
            "Parquet column value count exceeds its limit",
            values,
            limits.maximum_tabular_rows,
        )
    if uncompressed_size > limits.maximum_file_expanded_bytes:
        raise _PayloadComplexityError(
            "Parquet column uncompressed size exceeds its limit",
            uncompressed_size,
            limits.maximum_file_expanded_bytes,
        )
    compressed_size = _parquet_int(
        metadata,
        7,
        "column compressed size",
        minimum=1,
    )
    data_offset = _parquet_int(metadata, 9, "data page offset", minimum=4)
    index_offset = _parquet_optional_int(metadata, 10, "index page offset", minimum=4)
    dictionary_offset = _parquet_optional_int(
        metadata,
        11,
        "dictionary page offset",
        minimum=4,
    )
    page_offsets = [
        offset
        for offset in (data_offset, index_offset, dictionary_offset)
        if offset is not None
    ]
    chunk_start = min(page_offsets)
    chunk_end = chunk_start + compressed_size
    if file_offset != chunk_start or chunk_end > footer_start:
        raise ValueError("Parquet column chunk range exceeds the file body")
    if any(offset < chunk_start or offset >= chunk_end for offset in page_offsets):
        raise ValueError("Parquet page offset lies outside its column chunk")
    page_values, page_uncompressed_size = _validate_parquet_pages(
        raw,
        chunk_start,
        chunk_end,
        data_offset,
        index_offset,
        dictionary_offset,
        limits,
        budget,
    )
    if page_values != values:
        raise ValueError("Parquet page value counts differ from column metadata")
    if page_uncompressed_size != uncompressed_size:
        raise ValueError("Parquet page sizes differ from column uncompressed size")

    auxiliary: list[tuple[int, int]] = []
    for offset_field, length_field, label in (
        (4, 5, "offset index"),
        (6, 7, "column index"),
    ):
        offset = _parquet_optional_int(chunk, offset_field, f"{label} offset")
        length = _parquet_optional_int(chunk, length_field, f"{label} length")
        if (offset is None) != (length is None):
            raise ValueError(f"Parquet {label} range is incomplete")
        if offset is not None and length is not None:
            if length <= 0 or offset < 4 or offset + length > footer_start:
                raise ValueError(f"Parquet {label} range exceeds the file body")
            auxiliary.append((offset, offset + length))
    bloom_offset = _parquet_optional_int(metadata, 14, "bloom filter offset")
    bloom_length = _parquet_optional_int(metadata, 15, "bloom filter length")
    if (bloom_offset is None) != (bloom_length is None):
        raise ValueError("Parquet bloom-filter range is incomplete")
    if bloom_offset is not None and bloom_length is not None:
        if bloom_length <= 0 or bloom_offset < 4 or bloom_offset + bloom_length > footer_start:
            raise ValueError("Parquet bloom-filter range exceeds the file body")
        auxiliary.append((bloom_offset, bloom_offset + bloom_length))
    if values == 0:
        raise ValueError("Parquet materialized row group contains an empty column chunk")
    return chunk_start, chunk_end, uncompressed_size, compressed_size, tuple(auxiliary)


def _validate_parquet_structure(
    raw: bytes,
    limits: PackValidationLimitsV1,
) -> None:
    if len(raw) < 12 or not raw.startswith(b"PAR1") or not raw.endswith(b"PAR1"):
        raise ValueError("invalid Parquet framing")
    footer_size = int.from_bytes(raw[-8:-4], "little")
    footer_start = len(raw) - 8 - footer_size
    if footer_size <= 0 or footer_start < 4:
        raise ValueError("invalid Parquet footer bounds")
    if footer_size > limits.maximum_central_directory_bytes:
        raise _PayloadComplexityError(
            "Parquet footer exceeds its structural byte limit",
            footer_size,
            limits.maximum_central_directory_bytes,
        )
    reader = _CompactProtocolReader(
        raw,
        limits,
        start=footer_start,
        end=len(raw) - 8,
    )
    metadata = reader.read_struct(1)
    if reader.offset != footer_size:
        raise ValueError("Parquet footer contains trailing metadata bytes")
    if _parquet_int(metadata, 1, "format version", minimum=1) not in {1, 2}:
        raise ValueError("Parquet format version is unsupported")
    leaf_paths = _validate_parquet_schema(metadata.get(2), limits)
    declared_rows = _parquet_int(metadata, 3, "file row count")
    if declared_rows > limits.maximum_tabular_rows:
        raise _PayloadComplexityError(
            "Parquet file row count exceeds its limit",
            declared_rows,
            limits.maximum_tabular_rows,
        )
    row_groups = metadata.get(4)
    if type(row_groups) is not tuple:
        raise ValueError("Parquet footer lacks its row-group vector")
    if len(row_groups) > limits.maximum_parquet_row_groups:
        raise _PayloadComplexityError(
            "Parquet row-group count exceeds its limit",
            len(row_groups),
            limits.maximum_parquet_row_groups,
        )
    if bool(row_groups) != bool(declared_rows):
        raise ValueError("Parquet row-group presence differs from its row count")
    budget = [reader.nodes]
    observed_rows = 0
    decoded_bytes = 0
    referenced: list[tuple[int, int]] = []
    for row_group in row_groups:
        if type(row_group) is not dict:
            raise ValueError("Parquet row group has the wrong type")
        columns = row_group.get(1)
        if type(columns) is not tuple or len(columns) != len(leaf_paths):
            raise ValueError("Parquet row-group columns differ from its schema")
        row_uncompressed = _parquet_int(row_group, 2, "row-group byte size")
        row_count = _parquet_int(row_group, 3, "row-group row count", minimum=1)
        if row_count > limits.maximum_tabular_rows:
            raise _PayloadComplexityError(
                "Parquet row-group row count exceeds its limit",
                row_count,
                limits.maximum_tabular_rows,
            )
        if row_uncompressed > limits.maximum_total_expanded_bytes:
            raise _PayloadComplexityError(
                "Parquet row-group uncompressed size exceeds its limit",
                row_uncompressed,
                limits.maximum_total_expanded_bytes,
            )
        observed_rows += row_count
        decoded_bytes += row_uncompressed
        if decoded_bytes > limits.maximum_total_expanded_bytes:
            raise _PayloadComplexityError(
                "Parquet aggregate decoded bytes exceed their limit",
                decoded_bytes,
                limits.maximum_total_expanded_bytes,
            )
        observed_uncompressed = 0
        observed_compressed = 0
        for column, (expected_path, expected_physical_type) in zip(
            columns,
            leaf_paths,
        ):
            start, end, uncompressed, compressed, auxiliary = (
                _validate_parquet_column_chunk(
                    raw,
                    column,
                    expected_path,
                    expected_physical_type,
                    footer_start,
                    limits,
                    budget,
                )
            )
            referenced.append((start, end))
            referenced.extend(auxiliary)
            observed_uncompressed += uncompressed
            observed_compressed += compressed
        if observed_uncompressed != row_uncompressed:
            raise ValueError("Parquet row-group byte size differs from its columns")
        declared_compressed = _parquet_optional_int(
            row_group,
            6,
            "row-group compressed size",
        )
        if declared_compressed is not None and declared_compressed != observed_compressed:
            raise ValueError("Parquet row-group compressed size differs from its columns")
    if observed_rows != declared_rows:
        raise ValueError("Parquet row counts do not reconcile")
    previous_end = 4
    for start, end in sorted(referenced):
        if start != previous_end or end <= start or end > footer_start:
            raise ValueError(
                "Parquet referenced body ranges leave a gap, overlap, or escape"
            )
        previous_end = end
    if previous_end != footer_start:
        raise ValueError("Parquet file body contains unreferenced trailing bytes")


def _validate_binary_structure(
    raw: bytes,
    media_type: str,
    limits: PackValidationLimitsV1,
) -> None:
    if media_type == "image/png":
        _validate_png_structure(raw, limits)
    elif media_type == "image/jpeg":
        _validate_jpeg_structure(raw, limits)
    elif media_type == "image/webp":
        _validate_webp_structure(raw, limits)
    elif media_type == "image/tiff":
        _validate_tiff_structure(raw, limits)
    elif media_type == "application/vnd.apache.arrow.file":
        _validate_arrow_structure(raw, limits)
    else:
        raise ValueError("unsupported binary evidence media type")


def _validate_pixels(width: int, height: int, limits: PackValidationLimitsV1) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    pixels = width * height
    if pixels > limits.maximum_image_pixels:
        raise _PayloadComplexityError(
            "image pixel count exceeds its limit",
            pixels,
            limits.maximum_image_pixels,
        )


def _validate_png_structure(raw: bytes, limits: PackValidationLimitsV1) -> None:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    offset = 8
    chunks = 0
    saw_header = False
    saw_palette = False
    saw_data = False
    data_ended = False
    color_type = -1
    bit_depth = -1
    decompressor: object | None = None
    expected_decoded = 0
    decoded = 0
    scanline_segments: tuple[tuple[int, int], ...] = ()
    segment_index = 0
    rows_remaining = 0
    row_offset = 0

    def consume_scanlines(block: bytes) -> None:
        nonlocal segment_index, rows_remaining, row_offset
        cursor = 0
        while cursor < len(block):
            while rows_remaining == 0:
                if segment_index >= len(scanline_segments):
                    raise ValueError("PNG zlib stream exceeds the IHDR scanline shape")
                rows_remaining, _ = scanline_segments[segment_index]
                segment_index += 1
                if rows_remaining:
                    break
            row_size = scanline_segments[segment_index - 1][1]
            if row_offset == 0:
                if block[cursor] > 4:
                    raise ValueError("PNG scanline uses an invalid filter method")
                cursor += 1
                row_offset = 1
            amount = min(len(block) - cursor, row_size + 1 - row_offset)
            cursor += amount
            row_offset += amount
            if row_offset == row_size + 1:
                row_offset = 0
                rows_remaining -= 1

    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("truncated PNG chunk")
        size = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_type = raw[offset + 4 : offset + 8]
        end = offset + 12 + size
        if (
            end > len(raw)
            or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in chunk_type)
            or not (65 <= chunk_type[2] <= 90)
        ):
            raise ValueError("invalid PNG chunk bounds or type")
        data = memoryview(raw)[offset + 8 : offset + 8 + size]
        expected_crc = int.from_bytes(raw[offset + 8 + size : end], "big")
        observed_crc = zlib.crc32(data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if observed_crc != expected_crc:
            raise ValueError("PNG chunk CRC differs")
        chunks += 1
        if chunks > limits.maximum_parse_nodes:
            raise _PayloadComplexityError(
                "PNG chunk count exceeds its limit",
                chunks,
                limits.maximum_parse_nodes,
            )
        if not saw_header:
            if chunk_type != b"IHDR" or size != 13:
                raise ValueError("PNG IHDR must be first")
            width = int.from_bytes(data[0:4], "big")
            height = int.from_bytes(data[4:8], "big")
            _validate_pixels(width, height, limits)
            bit_depth = data[8]
            color_type = data[9]
            permitted_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                color_type not in permitted_depths
                or bit_depth not in permitted_depths[color_type]
                or data[10] != 0
                or data[11] != 0
                or data[12] not in {0, 1}
            ):
                raise ValueError("PNG IHDR declares an unsupported pixel layout")
            samples = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
            bits_per_pixel = samples * bit_depth
            if data[12] == 0:
                scanline_segments = (
                    (height, (width * bits_per_pixel + 7) // 8),
                )
            else:
                adam7 = (
                    (0, 0, 8, 8),
                    (4, 0, 8, 8),
                    (0, 4, 4, 8),
                    (2, 0, 4, 4),
                    (0, 2, 2, 4),
                    (1, 0, 2, 2),
                    (0, 1, 1, 2),
                )
                segments: list[tuple[int, int]] = []
                for x_start, y_start, x_step, y_step in adam7:
                    pass_width = (
                        0
                        if width <= x_start
                        else (width - x_start + x_step - 1) // x_step
                    )
                    pass_height = (
                        0
                        if height <= y_start
                        else (height - y_start + y_step - 1) // y_step
                    )
                    if pass_width and pass_height:
                        segments.append(
                            (
                                pass_height,
                                (pass_width * bits_per_pixel + 7) // 8,
                            )
                        )
                scanline_segments = tuple(segments)
            scanline_count = sum(rows for rows, _ in scanline_segments)
            if scanline_count > limits.maximum_parse_nodes:
                raise _PayloadComplexityError(
                    "PNG scanline count exceeds its limit",
                    scanline_count,
                    limits.maximum_parse_nodes,
                )
            expected_decoded = sum(
                rows * (row_size + 1) for rows, row_size in scanline_segments
            )
            if expected_decoded > limits.maximum_file_expanded_bytes:
                raise _PayloadComplexityError(
                    "PNG decoded scanlines exceed the structural byte limit",
                    expected_decoded,
                    limits.maximum_file_expanded_bytes,
                )
            decompressor = zlib.decompressobj()
            saw_header = True
        elif chunk_type == b"IHDR":
            raise ValueError("PNG contains a duplicate IHDR")
        elif chunk_type == b"PLTE":
            if saw_palette or saw_data or color_type in {0, 4}:
                raise ValueError("PNG PLTE placement is invalid")
            if size == 0 or size % 3 or size > 256 * 3:
                raise ValueError("PNG palette size is invalid")
            if color_type == 3 and size // 3 > 1 << bit_depth:
                raise ValueError("PNG palette exceeds its indexed bit depth")
            saw_palette = True
        elif chunk_type == b"IDAT":
            if data_ended or size == 0 or (color_type == 3 and not saw_palette):
                raise ValueError("PNG IDAT placement or size is invalid")
            if decompressor is None:
                raise ValueError("PNG zlib decoder was not initialized")
            saw_data = True
            try:
                output = decompressor.decompress(  # type: ignore[attr-defined]
                    data,
                    expected_decoded - decoded + 1,
                )
            except zlib.error as error:
                raise ValueError("PNG IDAT zlib stream is invalid") from error
            decoded += len(output)
            if decoded > expected_decoded or decompressor.unconsumed_tail:  # type: ignore[attr-defined]
                raise ValueError("PNG zlib stream exceeds the IHDR scanline shape")
            if decompressor.unused_data:  # type: ignore[attr-defined]
                raise ValueError("PNG IDAT contains bytes after its zlib stream")
            consume_scanlines(output)
        elif chunk_type == b"IEND":
            if size != 0 or not saw_data or end != len(raw):
                raise ValueError("PNG IEND is invalid or nonterminal")
            if decompressor is None:
                raise ValueError("PNG zlib decoder was not initialized")
            try:
                output = decompressor.flush(expected_decoded - decoded + 1)  # type: ignore[attr-defined]
            except zlib.error as error:
                raise ValueError("PNG IDAT zlib stream is invalid") from error
            decoded += len(output)
            consume_scanlines(output)
            if (
                decoded != expected_decoded
                or not decompressor.eof  # type: ignore[attr-defined]
                or decompressor.unused_data  # type: ignore[attr-defined]
                or decompressor.unconsumed_tail  # type: ignore[attr-defined]
                or row_offset
                or rows_remaining
                or segment_index != len(scanline_segments)
            ):
                raise ValueError("PNG zlib stream does not match its scanline shape")
            return
        elif 65 <= chunk_type[0] <= 90:
            raise ValueError("PNG contains an unknown critical chunk")
        elif saw_data:
            data_ended = True
        offset = end
    raise ValueError("PNG lacks a terminal IEND chunk")


_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _validate_jpeg_structure(raw: bytes, limits: PackValidationLimitsV1) -> None:
    if not raw.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG SOI marker")
    offset = 2
    markers = 0
    dimensions: tuple[int, int] | None = None
    frame_components: frozenset[int] = frozenset()
    saw_scan = False
    entropy_bytes = 0
    while offset < len(raw):
        if raw[offset] != 0xFF:
            raise ValueError("JPEG marker stream is malformed")
        marker_start = offset
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            raise ValueError("truncated JPEG marker")
        marker = raw[offset]
        offset += 1
        markers += 1
        if markers > limits.maximum_parse_nodes:
            raise _PayloadComplexityError(
                "JPEG marker count exceeds its limit",
                markers,
                limits.maximum_parse_nodes,
            )
        if marker == 0xD9:
            if not saw_scan or entropy_bytes == 0 or offset != len(raw):
                raise ValueError("JPEG EOI is premature or nonterminal")
            if dimensions is None:
                raise ValueError("JPEG lacks a frame header")
            _validate_pixels(*dimensions, limits)
            return
        if marker in {0x00, 0xD8, *range(0xD0, 0xD8)}:
            raise ValueError("JPEG contains a misplaced standalone marker")
        if marker == 0x01:
            continue
        if offset + 2 > len(raw):
            raise ValueError("truncated JPEG segment")
        segment_size = int.from_bytes(raw[offset : offset + 2], "big")
        if segment_size < 2 or offset + segment_size > len(raw):
            raise ValueError("invalid JPEG segment size")
        if marker in _JPEG_SOF_MARKERS:
            if dimensions is not None or segment_size < 8:
                raise ValueError("JPEG frame header is duplicate or truncated")
            component_count = raw[offset + 7]
            if component_count == 0 or component_count > 4 or segment_size != 8 + 3 * component_count:
                raise ValueError("JPEG frame component table is invalid")
            height = int.from_bytes(raw[offset + 3 : offset + 5], "big")
            width = int.from_bytes(raw[offset + 5 : offset + 7], "big")
            dimensions = (width, height)
            components: set[int] = set()
            for component_index in range(component_count):
                component = offset + 8 + component_index * 3
                identifier = raw[component]
                sampling = raw[component + 1]
                if identifier in components or not (sampling >> 4) or not (sampling & 0x0F):
                    raise ValueError("JPEG frame component declaration is invalid")
                components.add(identifier)
            frame_components = frozenset(components)
        if marker == 0xDC:
            raise ValueError("JPEG DNL dimensions are unsupported by the V1 validator")
        segment_end = offset + segment_size
        if marker == 0xDA:
            if dimensions is None or segment_size < 6:
                raise ValueError("JPEG scan precedes its frame header")
            component_count = raw[offset + 2]
            if component_count == 0 or component_count > 4 or segment_size != 6 + 2 * component_count:
                raise ValueError("JPEG scan component table is invalid")
            selectors = {
                raw[offset + 3 + component_index * 2]
                for component_index in range(component_count)
            }
            if len(selectors) != component_count or not selectors.issubset(frame_components):
                raise ValueError("JPEG scan selectors differ from its frame")
            cursor = segment_end
            scan_entropy = 0
            while cursor < len(raw):
                if raw[cursor] != 0xFF:
                    scan_entropy += 1
                    cursor += 1
                    continue
                fill_start = cursor
                while cursor < len(raw) and raw[cursor] == 0xFF:
                    cursor += 1
                if cursor >= len(raw):
                    raise ValueError("JPEG entropy marker is truncated")
                entropy_marker = raw[cursor]
                if entropy_marker == 0x00:
                    if cursor != fill_start + 1:
                        raise ValueError("JPEG stuffed entropy byte has marker fill")
                    scan_entropy += 1
                    cursor += 1
                    continue
                if 0xD0 <= entropy_marker <= 0xD7:
                    cursor += 1
                    continue
                offset = fill_start
                break
            else:
                raise ValueError("JPEG entropy scan lacks an EOI marker")
            if scan_entropy == 0:
                raise ValueError("JPEG entropy scan is empty")
            entropy_bytes += scan_entropy
            saw_scan = True
            continue
        offset += segment_size
        if offset <= marker_start:
            raise ValueError("JPEG marker parser did not advance")
    raise ValueError("JPEG lacks a terminal EOI marker")


def _webp_u24(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset : offset + 3], "little")


def _walk_webp_chunks(
    raw: bytes,
    start: int,
    end: int,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[tuple[bytes, int, int], ...]:
    chunks: list[tuple[bytes, int, int]] = []
    offset = start
    while offset < end:
        if offset + 8 > end:
            raise ValueError("truncated WebP chunk")
        chunk_type = raw[offset : offset + 4]
        if any(not (32 <= byte <= 126) for byte in chunk_type):
            raise ValueError("WebP chunk type is invalid")
        size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + size
        padded_end = data_end + (size & 1)
        if padded_end > end or (size & 1 and raw[data_end] != 0):
            raise ValueError("WebP chunk bounds or padding are invalid")
        budget[0] += 1
        if budget[0] > limits.maximum_parse_nodes:
            raise _PayloadComplexityError(
                "WebP chunk count exceeds its limit",
                budget[0],
                limits.maximum_parse_nodes,
            )
        chunks.append((chunk_type, data_start, data_end))
        offset = padded_end
    if offset != end:
        raise ValueError("WebP chunk framing differs")
    return tuple(chunks)


def _validate_vp8_payload(raw: bytes, start: int, end: int) -> tuple[int, int]:
    if end - start < 11:
        raise ValueError("WebP VP8 frame payload is empty or truncated")
    frame_tag = int.from_bytes(raw[start : start + 3], "little")
    if (
        frame_tag & 1
        or ((frame_tag >> 1) & 0x07) > 3
        or not ((frame_tag >> 4) & 1)
        or raw[start + 3 : start + 6] != b"\x9d\x01\x2a"
    ):
        raise ValueError("WebP VP8 key-frame header is invalid")
    first_partition = frame_tag >> 5
    if first_partition <= 0 or start + 10 + first_partition > end:
        raise ValueError("WebP VP8 first partition exceeds its payload")
    dimensions = (
        int.from_bytes(raw[start + 6 : start + 8], "little") & 0x3FFF,
        int.from_bytes(raw[start + 8 : start + 10], "little") & 0x3FFF,
    )
    if not dimensions[0] or not dimensions[1]:
        raise ValueError("WebP VP8 frame dimensions are invalid")
    return dimensions


def _validate_vp8l_payload(raw: bytes, start: int, end: int) -> tuple[int, int]:
    if end - start < 6 or raw[start] != 0x2F:
        raise ValueError("WebP VP8L frame payload is empty or truncated")
    bits = int.from_bytes(raw[start + 1 : start + 5], "little")
    if bits >> 29:
        raise ValueError("WebP VP8L version bits are unsupported")
    return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)


def _validate_webp_image_chunk(
    raw: bytes,
    chunk: tuple[bytes, int, int],
) -> tuple[int, int]:
    chunk_type, start, end = chunk
    if chunk_type == b"VP8 ":
        return _validate_vp8_payload(raw, start, end)
    if chunk_type == b"VP8L":
        return _validate_vp8l_payload(raw, start, end)
    raise ValueError("WebP image chunk has an unsupported type")


def _validate_webp_structure(raw: bytes, limits: PackValidationLimitsV1) -> None:
    if len(raw) < 20 or not raw.startswith(b"RIFF") or raw[8:12] != b"WEBP":
        raise ValueError("invalid WebP framing")
    if int.from_bytes(raw[4:8], "little") + 8 != len(raw):
        raise ValueError("WebP RIFF length differs")
    budget = [0]
    chunks = _walk_webp_chunks(raw, 12, len(raw), limits, budget)
    if not chunks:
        raise ValueError("WebP has no image chunks")
    if chunks[0][0] != b"VP8X":
        if len(chunks) != 1 or chunks[0][0] not in {b"VP8 ", b"VP8L"}:
            raise ValueError("simple WebP must contain exactly one image chunk")
        dimensions = _validate_webp_image_chunk(raw, chunks[0])
        _validate_pixels(*dimensions, limits)
        return

    _, header_start, header_end = chunks[0]
    if header_end - header_start != 10:
        raise ValueError("WebP VP8X chunk size is invalid")
    flags = raw[header_start]
    if flags & 0xC1 or raw[header_start + 1 : header_start + 4] != b"\x00\x00\x00":
        raise ValueError("WebP VP8X reserved bits are nonzero")
    canvas = (
        _webp_u24(raw, header_start + 4) + 1,
        _webp_u24(raw, header_start + 7) + 1,
    )
    _validate_pixels(*canvas, limits)
    animation = bool(flags & 0x02)
    image_chunks = [chunk for chunk in chunks[1:] if chunk[0] in {b"VP8 ", b"VP8L"}]
    animation_headers = [chunk for chunk in chunks[1:] if chunk[0] == b"ANIM"]
    frames = [chunk for chunk in chunks[1:] if chunk[0] == b"ANMF"]
    permitted = {b"ICCP", b"ALPH", b"VP8 ", b"VP8L", b"ANIM", b"ANMF", b"EXIF", b"XMP "}
    if any(chunk[0] not in permitted for chunk in chunks[1:]):
        raise ValueError("WebP contains an unsupported extended chunk")
    if not animation:
        if animation_headers or frames or len(image_chunks) != 1:
            raise ValueError("extended still WebP does not contain one image")
        dimensions = _validate_webp_image_chunk(raw, image_chunks[0])
        if dimensions != canvas:
            raise ValueError("WebP image dimensions differ from its VP8X canvas")
        return
    if image_chunks or len(animation_headers) != 1 or not frames:
        raise ValueError("animated WebP framing is incomplete")
    anim = animation_headers[0]
    if anim[2] - anim[1] != 6:
        raise ValueError("WebP ANIM chunk size is invalid")
    cumulative_pixels = 0
    for _, frame_start, frame_end in frames:
        if frame_end - frame_start < 16:
            raise ValueError("WebP ANMF frame header is truncated")
        x = 2 * _webp_u24(raw, frame_start)
        y = 2 * _webp_u24(raw, frame_start + 3)
        width = _webp_u24(raw, frame_start + 6) + 1
        height = _webp_u24(raw, frame_start + 9) + 1
        if raw[frame_start + 15] & 0xFC or x + width > canvas[0] or y + height > canvas[1]:
            raise ValueError("WebP ANMF rectangle or flags are invalid")
        nested = _walk_webp_chunks(
            raw,
            frame_start + 16,
            frame_end,
            limits,
            budget,
        )
        embedded = [chunk for chunk in nested if chunk[0] in {b"VP8 ", b"VP8L"}]
        if (
            len(embedded) != 1
            or any(chunk[0] not in {b"ALPH", b"VP8 ", b"VP8L"} for chunk in nested)
            or (any(chunk[0] == b"ALPH" for chunk in nested) and embedded[0][0] == b"VP8L")
            or (nested and nested[-1] != embedded[0])
        ):
            raise ValueError("WebP ANMF image subchunks are invalid")
        dimensions = _validate_webp_image_chunk(raw, embedded[0])
        if dimensions != (width, height):
            raise ValueError("WebP ANMF image dimensions differ from its frame")
        cumulative_pixels += width * height
        if cumulative_pixels > limits.maximum_image_pixels:
            raise _PayloadComplexityError(
                "WebP cumulative frame pixels exceed their limit",
                cumulative_pixels,
                limits.maximum_image_pixels,
            )


_TIFF_TYPE_WIDTHS = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
}


def _tiff_entry_value_range(
    raw: bytes,
    entry: int,
    order: str,
) -> tuple[int, int, int, int]:
    kind = int.from_bytes(raw[entry + 2 : entry + 4], order)
    count = int.from_bytes(raw[entry + 4 : entry + 8], order)
    width = _TIFF_TYPE_WIDTHS.get(kind)
    if width is None or count <= 0:
        raise ValueError("TIFF IFD entry type or count is invalid")
    byte_count = count * width
    if byte_count <= 4:
        start = entry + 8
    else:
        start = int.from_bytes(raw[entry + 8 : entry + 12], order)
        if start < 8 or start + byte_count > len(raw):
            raise ValueError("TIFF IFD value pointer exceeds the file")
    return kind, count, start, byte_count


def _tiff_unsigned_values(
    raw: bytes,
    descriptor: tuple[int, int, int, int],
    order: str,
    label: str,
) -> tuple[int, ...]:
    kind, count, start, _ = descriptor
    if kind not in {1, 3, 4}:
        raise ValueError(f"TIFF {label} has an unsupported integer type")
    width = _TIFF_TYPE_WIDTHS[kind]
    return tuple(
        int.from_bytes(
            raw[start + index * width : start + (index + 1) * width],
            order,
        )
        for index in range(count)
    )


def _validate_tiff_structure(raw: bytes, limits: PackValidationLimitsV1) -> None:
    if len(raw) < 8 or raw[:2] not in {b"II", b"MM"}:
        raise ValueError("invalid TIFF byte-order marker")
    order = "little" if raw[:2] == b"II" else "big"
    if int.from_bytes(raw[2:4], order) != 42:
        raise ValueError("invalid TIFF version")
    offset = int.from_bytes(raw[4:8], order)
    visited: set[int] = set()
    metadata_ranges: list[tuple[int, int]] = [(0, 8)]
    pixel_ranges: list[tuple[int, int]] = []
    nodes = 0
    total_pixels = 0
    while offset:
        if offset in visited or offset < 8 or offset + 2 > len(raw):
            raise ValueError("TIFF IFD cycle or invalid offset")
        visited.add(offset)
        count = int.from_bytes(raw[offset : offset + 2], order)
        nodes += count
        if nodes > limits.maximum_parse_nodes:
            raise _PayloadComplexityError(
                "TIFF directory entry count exceeds its limit",
                nodes,
                limits.maximum_parse_nodes,
            )
        table_end = offset + 2 + count * 12
        if table_end + 4 > len(raw):
            raise ValueError("truncated TIFF IFD")
        metadata_ranges.append((offset, table_end + 4))
        entries: dict[int, tuple[int, int, int, int]] = {}
        for index in range(count):
            entry = offset + 2 + index * 12
            tag = int.from_bytes(raw[entry : entry + 2], order)
            if tag in entries:
                raise ValueError("TIFF IFD contains a duplicate tag")
            descriptor = _tiff_entry_value_range(raw, entry, order)
            entries[tag] = descriptor
            if descriptor[3] > 4:
                metadata_ranges.append(
                    (descriptor[2], descriptor[2] + descriptor[3])
                )
        if 330 in entries:
            raise ValueError("TIFF SubIFDs are unsupported by the V1 validator")

        def values(tag: int, label: str, default: tuple[int, ...] | None = None) -> tuple[int, ...]:
            descriptor = entries.get(tag)
            if descriptor is None:
                if default is None:
                    raise ValueError(f"TIFF lacks {label}")
                return default
            nonlocal nodes
            value_count = descriptor[1]
            if nodes + value_count > limits.maximum_parse_nodes:
                raise _PayloadComplexityError(
                    "TIFF metadata element count exceeds its limit",
                    nodes + value_count,
                    limits.maximum_parse_nodes,
                )
            result = _tiff_unsigned_values(raw, descriptor, order, label)
            nodes += value_count
            return result

        width_values = values(256, "image width")
        height_values = values(257, "image height")
        if len(width_values) != 1 or len(height_values) != 1:
            raise ValueError("TIFF image dimensions are not scalar")
        width, height = width_values[0], height_values[0]
        _validate_pixels(width, height, limits)
        total_pixels += width * height
        if total_pixels > limits.maximum_image_pixels:
            raise _PayloadComplexityError(
                "TIFF cumulative image pixels exceed their limit",
                total_pixels,
                limits.maximum_image_pixels,
            )
        compression = values(259, "compression", (1,))
        samples = values(277, "samples per pixel", (1,))
        planar = values(284, "planar configuration", (1,))
        if compression != (1,) or len(samples) != 1 or samples[0] <= 0 or planar not in {(1,), (2,)}:
            raise ValueError("TIFF V1 accepts only bounded uncompressed planar layouts")
        bits = values(258, "bits per sample", (1,))
        if len(bits) != samples[0] or any(value not in {1, 2, 4, 8, 16, 32} for value in bits):
            raise ValueError("TIFF bits-per-sample vector differs from its samples")

        strip_mode = 273 in entries or 279 in entries
        tile_mode = 324 in entries or 325 in entries
        if strip_mode == tile_mode:
            raise ValueError("TIFF must use exactly one strip or tile storage scheme")
        if strip_mode:
            offsets = values(273, "strip offsets")
            byte_counts = values(279, "strip byte counts")
            rows_values = values(278, "rows per strip", (height,))
            if len(rows_values) != 1 or rows_values[0] <= 0:
                raise ValueError("TIFF rows-per-strip is invalid")
            rows_per_strip = rows_values[0]
            strips_per_plane = (height + rows_per_strip - 1) // rows_per_strip
            expected_count = strips_per_plane * (samples[0] if planar == (2,) else 1)
            if nodes + expected_count > limits.maximum_parse_nodes:
                raise _PayloadComplexityError(
                    "TIFF pixel-block count exceeds its limit",
                    nodes + expected_count,
                    limits.maximum_parse_nodes,
                )
            nodes += expected_count
            expected_sizes: list[int] = []
            if planar == (1,):
                row_bytes = (width * sum(bits) + 7) // 8
                expected_sizes.extend(
                    min(rows_per_strip, height - strip * rows_per_strip) * row_bytes
                    for strip in range(strips_per_plane)
                )
            else:
                for sample_bits in bits:
                    row_bytes = (width * sample_bits + 7) // 8
                    expected_sizes.extend(
                        min(rows_per_strip, height - strip * rows_per_strip) * row_bytes
                        for strip in range(strips_per_plane)
                    )
        else:
            offsets = values(324, "tile offsets")
            byte_counts = values(325, "tile byte counts")
            tile_width = values(322, "tile width")
            tile_height = values(323, "tile height")
            if (
                len(tile_width) != 1
                or len(tile_height) != 1
                or tile_width[0] <= 0
                or tile_height[0] <= 0
            ):
                raise ValueError("TIFF tile dimensions are invalid")
            tiles_per_plane = (
                (width + tile_width[0] - 1) // tile_width[0]
            ) * ((height + tile_height[0] - 1) // tile_height[0])
            expected_count = tiles_per_plane * (samples[0] if planar == (2,) else 1)
            if nodes + expected_count > limits.maximum_parse_nodes:
                raise _PayloadComplexityError(
                    "TIFF pixel-block count exceeds its limit",
                    nodes + expected_count,
                    limits.maximum_parse_nodes,
                )
            nodes += expected_count
            if planar == (1,):
                tile_bytes = tile_height[0] * (
                    (tile_width[0] * sum(bits) + 7) // 8
                )
                expected_sizes = [tile_bytes] * tiles_per_plane
            else:
                expected_sizes = [
                    tile_height[0] * ((tile_width[0] * sample_bits + 7) // 8)
                    for sample_bits in bits
                    for _ in range(tiles_per_plane)
                ]
        if (
            len(offsets) != expected_count
            or len(byte_counts) != expected_count
            or tuple(expected_sizes) != byte_counts
        ):
            raise ValueError("TIFF pixel block vectors differ from the image layout")
        for block_offset, byte_count in zip(offsets, byte_counts):
            if block_offset < 8 or byte_count <= 0 or block_offset + byte_count > len(raw):
                raise ValueError("TIFF pixel block exceeds the file")
            pixel_ranges.append((block_offset, block_offset + byte_count))
        offset = int.from_bytes(raw[table_end : table_end + 4], order)
    if not visited:
        raise ValueError("TIFF has no image file directory")
    for start, end in pixel_ranges:
        if any(start < metadata_end and metadata_start < end for metadata_start, metadata_end in metadata_ranges):
            raise ValueError("TIFF pixel data overlaps its metadata")
    previous_end = 0
    for start, end in sorted(pixel_ranges):
        if start < previous_end:
            raise ValueError("TIFF pixel blocks overlap")
        previous_end = end


class _FlatBufferTable:
    __slots__ = ("_data", "_object_end", "_region_end", "_region_start", "_table", "_vtable", "_vtable_size")

    def __init__(
        self,
        data: bytes,
        region_start: int,
        region_end: int,
        table: int,
    ) -> None:
        if table < region_start + 4 or table + 4 > region_end:
            raise ValueError("Arrow FlatBuffer table offset is invalid")
        distance = int.from_bytes(data[table : table + 4], "little", signed=True)
        vtable = table - distance
        if distance <= 0 or vtable < region_start or vtable + 4 > region_end:
            raise ValueError("Arrow FlatBuffer vtable offset is invalid")
        vtable_size = int.from_bytes(data[vtable : vtable + 2], "little")
        object_size = int.from_bytes(data[vtable + 2 : vtable + 4], "little")
        if (
            vtable_size < 4
            or vtable_size % 2
            or vtable + vtable_size > region_end
            or object_size < 4
            or table + object_size > region_end
        ):
            raise ValueError("Arrow FlatBuffer table or vtable bounds are invalid")
        self._data = data
        self._object_end = table + object_size
        self._region_end = region_end
        self._region_start = region_start
        self._table = table
        self._vtable = vtable
        self._vtable_size = vtable_size

    @classmethod
    def root(
        cls,
        data: bytes,
        region_start: int,
        region_end: int,
    ) -> _FlatBufferTable:
        if region_start < 0 or region_start + 4 > region_end or region_end > len(data):
            raise ValueError("Arrow FlatBuffer root region is invalid")
        relative = int.from_bytes(data[region_start : region_start + 4], "little")
        if relative < 4:
            raise ValueError("Arrow FlatBuffer root offset is invalid")
        return cls(data, region_start, region_end, region_start + relative)

    @property
    def table_offset(self) -> int:
        return self._table

    def present_ordinals(self) -> tuple[int, ...]:
        result: list[int] = []
        for ordinal in range((self._vtable_size - 4) // 2):
            if self._field(ordinal, 1) is not None:
                result.append(ordinal)
        return tuple(result)

    def _field(self, ordinal: int, width: int) -> int | None:
        if type(ordinal) is not int or ordinal < 0 or width <= 0:
            raise ValueError("Arrow FlatBuffer field request is invalid")
        entry = self._vtable + 4 + ordinal * 2
        if entry + 2 > self._vtable + self._vtable_size:
            return None
        relative = int.from_bytes(self._data[entry : entry + 2], "little")
        if relative == 0:
            return None
        position = self._table + relative
        if relative < 4 or position + width > self._object_end:
            raise ValueError("Arrow FlatBuffer field exceeds its table object")
        return position

    def scalar(
        self,
        ordinal: int,
        width: int,
        *,
        signed: bool = False,
        default: int | None = None,
    ) -> int | None:
        position = self._field(ordinal, width)
        if position is None:
            return default
        return int.from_bytes(
            self._data[position : position + width],
            "little",
            signed=signed,
        )

    def table(self, ordinal: int) -> _FlatBufferTable | None:
        position = self._field(ordinal, 4)
        if position is None:
            return None
        relative = int.from_bytes(self._data[position : position + 4], "little")
        if relative < 4 or position + relative + 4 > self._region_end:
            raise ValueError("Arrow nested FlatBuffer table offset is invalid")
        return _FlatBufferTable(
            self._data,
            self._region_start,
            self._region_end,
            position + relative,
        )

    def vector(self, ordinal: int, element_size: int) -> tuple[int, int] | None:
        position = self._field(ordinal, 4)
        if position is None:
            return None
        relative = int.from_bytes(self._data[position : position + 4], "little")
        vector = position + relative
        if relative < 4 or vector + 4 > self._region_end:
            raise ValueError("Arrow FlatBuffer vector offset is invalid")
        count = int.from_bytes(self._data[vector : vector + 4], "little")
        start = vector + 4
        if element_size <= 0 or start + count * element_size > self._region_end:
            raise ValueError("Arrow FlatBuffer vector exceeds its region")
        return start, count

    def vector_table(self, vector_start: int, index: int) -> _FlatBufferTable:
        element = vector_start + index * 4
        relative = int.from_bytes(self._data[element : element + 4], "little")
        if relative < 4 or element + relative + 4 > self._region_end:
            raise ValueError("Arrow FlatBuffer table-vector element is invalid")
        return _FlatBufferTable(
            self._data,
            self._region_start,
            self._region_end,
            element + relative,
        )

    def text(self, ordinal: int) -> str | None:
        position = self._field(ordinal, 4)
        if position is None:
            return None
        relative = int.from_bytes(self._data[position : position + 4], "little")
        string = position + relative
        if relative < 4 or string + 4 > self._region_end:
            raise ValueError("Arrow FlatBuffer string offset is invalid")
        size = int.from_bytes(self._data[string : string + 4], "little")
        start = string + 4
        if start + size + 1 > self._region_end or self._data[start + size] != 0:
            raise ValueError("Arrow FlatBuffer string bounds are invalid")
        result = self._data[start : start + size].decode("utf-8")
        if unicodedata.normalize("NFC", result) != result:
            raise ValueError("Arrow FlatBuffer string is not NFC text")
        return result


def _arrow_node(budget: list[int], limits: PackValidationLimitsV1, label: str) -> None:
    budget[0] += 1
    if budget[0] > limits.maximum_parse_nodes:
        raise _PayloadComplexityError(
            f"Arrow {label} node count exceeds its limit",
            budget[0],
            limits.maximum_parse_nodes,
        )


def _arrow_require_fields(
    table: _FlatBufferTable,
    allowed: set[int],
    label: str,
) -> None:
    if any(ordinal not in allowed for ordinal in table.present_ordinals()):
        raise ValueError(f"Arrow {label} contains unsupported fields")


def _arrow_required_scalar(
    table: _FlatBufferTable,
    ordinal: int,
    width: int,
    label: str,
    *,
    signed: bool = True,
) -> int:
    value = table.scalar(ordinal, width, signed=signed)
    if value is None:
        raise ValueError(f"Arrow {label} is absent")
    return value


def _arrow_int_vector(
    table: _FlatBufferTable,
    ordinal: int,
    width: int,
    limits: PackValidationLimitsV1,
    budget: list[int],
    label: str,
) -> tuple[int, ...]:
    vector = table.vector(ordinal, width)
    if vector is None:
        raise ValueError(f"Arrow {label} vector is absent")
    start, count = vector
    if budget[0] + count > limits.maximum_parse_nodes:
        raise _PayloadComplexityError(
            f"Arrow {label} vector exceeds its element limit",
            budget[0] + count,
            limits.maximum_parse_nodes,
        )
    result = tuple(
        int.from_bytes(
            table._data[start + index * width : start + (index + 1) * width],
            "little",
            signed=True,
        )
        for index in range(count)
    )
    budget[0] += count
    return result


def _project_arrow_type(
    type_id: int,
    type_table: _FlatBufferTable,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[object, ...]:
    _arrow_node(budget, limits, "type")
    # View-backed binary/string arrays require variadic buffer reconciliation,
    # which the deliberately bounded V1 subset does not claim to implement.
    empty_types = {1, 4, 5, 6, 12, 13, 19, 20, 21, 22, 25, 26}
    if type_id in empty_types:
        _arrow_require_fields(type_table, set(), "empty type table")
        return (type_id,)
    if type_id == 2:
        _arrow_require_fields(type_table, {0, 1}, "integer type")
        bit_width = _arrow_required_scalar(type_table, 0, 4, "integer bit width")
        signed = type_table.scalar(1, 1, default=0)
        if bit_width not in {8, 16, 32, 64} or signed not in {0, 1}:
            raise ValueError("Arrow integer type parameters are invalid")
        return (type_id, bit_width, signed)
    if type_id == 3:
        _arrow_require_fields(type_table, {0}, "floating-point type")
        precision = type_table.scalar(0, 2, signed=True, default=0)
        if precision not in {0, 1, 2}:
            raise ValueError("Arrow floating-point precision is invalid")
        return (type_id, precision)
    if type_id == 7:
        _arrow_require_fields(type_table, {0, 1, 2}, "decimal type")
        precision = _arrow_required_scalar(type_table, 0, 4, "decimal precision")
        scale = _arrow_required_scalar(type_table, 1, 4, "decimal scale")
        bit_width = type_table.scalar(2, 4, signed=True, default=128)
        if precision <= 0 or bit_width not in {128, 256}:
            raise ValueError("Arrow decimal type parameters are invalid")
        return (type_id, precision, scale, bit_width)
    if type_id in {8, 11, 18}:
        _arrow_require_fields(type_table, {0}, "temporal type")
        unit = type_table.scalar(0, 2, signed=True, default=0)
        maximum = 1 if type_id == 8 else (2 if type_id == 11 else 3)
        if unit is None or not (0 <= unit <= maximum):
            raise ValueError("Arrow temporal unit is invalid")
        return (type_id, unit)
    if type_id == 9:
        _arrow_require_fields(type_table, {0, 1}, "time type")
        unit = type_table.scalar(0, 2, signed=True, default=1)
        bit_width = type_table.scalar(1, 4, signed=True, default=32)
        if unit not in {0, 1, 2, 3} or bit_width not in {32, 64}:
            raise ValueError("Arrow time type parameters are invalid")
        return (type_id, unit, bit_width)
    if type_id == 10:
        _arrow_require_fields(type_table, {0, 1}, "timestamp type")
        unit = type_table.scalar(0, 2, signed=True, default=0)
        timezone = type_table.text(1)
        if unit not in {0, 1, 2, 3}:
            raise ValueError("Arrow timestamp unit is invalid")
        return (type_id, unit, timezone)
    if type_id == 14:
        _arrow_require_fields(type_table, {0, 1}, "union type")
        mode = type_table.scalar(0, 2, signed=True, default=0)
        type_ids = _arrow_int_vector(
            type_table,
            1,
            4,
            limits,
            budget,
            "union type IDs",
        )
        if mode not in {0, 1} or len(type_ids) != len(set(type_ids)):
            raise ValueError("Arrow union type parameters are invalid")
        return (type_id, mode, type_ids)
    if type_id in {15, 16}:
        _arrow_require_fields(type_table, {0}, "fixed-size type")
        size = _arrow_required_scalar(type_table, 0, 4, "fixed-size width")
        if size <= 0:
            raise ValueError("Arrow fixed-size type width is invalid")
        return (type_id, size)
    if type_id == 17:
        _arrow_require_fields(type_table, {0}, "map type")
        keys_sorted = type_table.scalar(0, 1, default=0)
        if keys_sorted not in {0, 1}:
            raise ValueError("Arrow map keys-sorted flag is invalid")
        return (type_id, keys_sorted)
    raise ValueError("Arrow field type is unsupported by the V1 schema projector")


def _project_arrow_dictionary(
    dictionary: _FlatBufferTable | None,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[object, ...] | None:
    if dictionary is None:
        return None
    _arrow_node(budget, limits, "dictionary")
    _arrow_require_fields(dictionary, {0, 1, 2, 3}, "dictionary encoding")
    identifier = _arrow_required_scalar(dictionary, 0, 8, "dictionary ID")
    index_type = dictionary.table(1)
    ordered = dictionary.scalar(2, 1, default=0)
    kind = dictionary.scalar(3, 2, signed=True, default=0)
    if identifier < 0 or index_type is None or ordered not in {0, 1} or kind != 0:
        raise ValueError("Arrow dictionary encoding is invalid")
    index_projection = _project_arrow_type(2, index_type, limits, budget)
    return (identifier, index_projection, ordered, kind)


def _validate_arrow_schema(
    schema: _FlatBufferTable,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[tuple[object, ...], ...]:
    _arrow_require_fields(schema, {0, 1}, "schema")
    endianness = schema.scalar(0, 2, signed=True, default=0)
    if endianness not in {0, 1}:
        raise ValueError("Arrow schema endianness is invalid")
    fields = schema.vector(1, 4)
    if fields is None:
        raise ValueError("Arrow schema lacks its field vector")
    start, count = fields
    if count > limits.maximum_parquet_columns:
        raise _PayloadComplexityError(
            "Arrow schema field count exceeds its limit",
            count,
            limits.maximum_parquet_columns,
        )
    stack: list[tuple[_FlatBufferTable, int]] = [
        (schema.vector_table(start, index), 1) for index in range(count - 1, -1, -1)
    ]
    seen: set[int] = set()
    summary: list[tuple[object, ...]] = [("SCHEMA", endianness, count)]
    while stack:
        field, depth = stack.pop()
        _arrow_node(budget, limits, "schema")
        if depth > limits.maximum_parse_depth:
            raise _PayloadComplexityError(
                "Arrow schema depth exceeds its limit",
                depth,
                limits.maximum_parse_depth,
            )
        if field.table_offset in seen:
            raise ValueError("Arrow schema field graph contains an alias or cycle")
        seen.add(field.table_offset)
        name = field.text(0)
        type_id = field.scalar(2, 1, default=0)
        nullable = field.scalar(1, 1, default=0)
        if not name or type_id is None or not (1 <= type_id <= 26) or nullable not in {0, 1}:
            raise ValueError("Arrow schema field declaration is invalid")
        _arrow_require_fields(field, set(range(6)), "schema field")
        type_table = field.table(3)
        if type_table is None:
            raise ValueError("Arrow schema field lacks its type table")
        type_projection = _project_arrow_type(
            type_id,
            type_table,
            limits,
            budget,
        )
        dictionary_projection = _project_arrow_dictionary(
            field.table(4),
            limits,
            budget,
        )
        children = field.vector(5, 4)
        child_count = 0 if children is None else children[1]
        if type_id in {12, 16, 17, 21, 25, 26} and child_count != 1:
            raise ValueError("Arrow list/map field does not have one child")
        if type_id == 22 and child_count != 2:
            raise ValueError("Arrow run-end-encoded field does not have two children")
        if type_id == 14 and child_count != len(type_projection[2]):
            raise ValueError("Arrow union child count differs from its type IDs")
        if type_id not in {12, 13, 14, 16, 17, 21, 22, 25, 26} and child_count:
            raise ValueError("Arrow scalar field unexpectedly has child fields")
        summary.append(
            (
                depth,
                name,
                type_id,
                nullable,
                type_projection,
                child_count,
                dictionary_projection,
            )
        )
        if children is not None:
            child_start, child_count = children
            if budget[0] + child_count > limits.maximum_parse_nodes:
                raise _PayloadComplexityError(
                    "Arrow schema field count exceeds its limit",
                    budget[0] + child_count,
                    limits.maximum_parse_nodes,
                )
            stack.extend(
                (field.vector_table(child_start, index), depth + 1)
                for index in range(child_count - 1, -1, -1)
            )
    return tuple(summary)


def _validate_arrow_record_batch(
    batch: _FlatBufferTable,
    body_length: int,
    limits: PackValidationLimitsV1,
    budget: list[int],
    expected_schema: tuple[tuple[object, ...], ...] | None = None,
) -> None:
    _arrow_require_fields(batch, {0, 1, 2, 3}, "record batch")
    row_count = batch.scalar(0, 8, signed=True)
    if row_count is None or row_count < 0:
        raise ValueError("Arrow record batch row count is invalid")
    if row_count > limits.maximum_tabular_rows:
        raise _PayloadComplexityError(
            "Arrow record batch row count exceeds its limit",
            row_count,
            limits.maximum_tabular_rows,
        )
    if batch.table(3) is not None:
        raise ValueError("compressed Arrow record batches are unsupported by V1")
    nodes = batch.vector(1, 16)
    if nodes is None:
        raise ValueError("Arrow record batch lacks field nodes")
    node_start, node_count = nodes
    if expected_schema is not None and node_count != len(expected_schema) - 1:
        raise ValueError("Arrow field nodes differ from the flattened schema")
    if budget[0] + node_count > limits.maximum_parse_nodes:
        raise _PayloadComplexityError(
            "Arrow field-node count exceeds its limit",
            budget[0] + node_count,
            limits.maximum_parse_nodes,
        )
    for index in range(node_count):
        position = node_start + index * 16
        length = int.from_bytes(batch._data[position : position + 8], "little", signed=True)
        nulls = int.from_bytes(batch._data[position + 8 : position + 16], "little", signed=True)
        if (
            length < 0
            or length > limits.maximum_tabular_rows
            or nulls < 0
            or nulls > length
        ):
            raise ValueError("Arrow field node is invalid")
        if expected_schema is not None:
            schema_row = expected_schema[index + 1]
            if schema_row[0] == 1 and length != row_count:
                raise ValueError("Arrow top-level field length differs from its batch")
        _arrow_node(budget, limits, "field")
    buffers = batch.vector(2, 16)
    if buffers is None:
        raise ValueError("Arrow record batch lacks buffer ranges")
    buffer_start, buffer_count = buffers
    if row_count > 0 and (node_count == 0 or buffer_count == 0):
        raise ValueError("positive Arrow record batch has empty nodes or buffers")
    ranges: list[tuple[int, int]] = []
    for index in range(buffer_count):
        position = buffer_start + index * 16
        offset = int.from_bytes(batch._data[position : position + 8], "little", signed=True)
        length = int.from_bytes(batch._data[position + 8 : position + 16], "little", signed=True)
        if offset < 0 or length < 0 or offset % 8 or offset + length > body_length:
            raise ValueError("Arrow record-batch buffer range is invalid")
        _arrow_node(budget, limits, "buffer")
        if length:
            ranges.append((offset, offset + length))
    previous_end = 0
    for start, end in sorted(ranges):
        if start < previous_end:
            raise ValueError("Arrow record-batch buffers overlap")
        previous_end = end


def _validate_arrow_message(
    raw: bytes,
    offset: int,
    region_end: int,
    expected_header: int,
    limits: PackValidationLimitsV1,
    budget: list[int],
    *,
    expected_metadata_length: int | None = None,
    expected_body_length: int | None = None,
    expected_schema: tuple[tuple[object, ...], ...] | None = None,
) -> tuple[int, tuple[tuple[object, ...], ...] | None]:
    if offset < 8 or offset + 4 > region_end:
        raise ValueError("Arrow IPC message offset is invalid")
    first = int.from_bytes(raw[offset : offset + 4], "little")
    if first == 0xFFFFFFFF:
        if offset + 8 > region_end:
            raise ValueError("Arrow IPC continuation marker is truncated")
        prefix_size = 8
        metadata_size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
    else:
        prefix_size = 4
        metadata_size = first
    if metadata_size < 4:
        raise ValueError("Arrow IPC message metadata is empty")
    metadata_start = offset + prefix_size
    metadata_end = metadata_start + metadata_size
    padded_metadata_end = offset + ((prefix_size + metadata_size + 7) // 8) * 8
    if padded_metadata_end > region_end or any(raw[metadata_end:padded_metadata_end]):
        raise ValueError("Arrow IPC message metadata or padding is invalid")
    metadata_length = padded_metadata_end - offset
    if expected_metadata_length is not None and metadata_length != expected_metadata_length:
        raise ValueError("Arrow block metadata length differs from its message")
    message = _FlatBufferTable.root(raw, metadata_start, metadata_end)
    _arrow_require_fields(message, {0, 1, 2, 3}, "message")
    version = message.scalar(0, 2, signed=True)
    header_type = message.scalar(1, 1, default=0)
    body_length = message.scalar(3, 8, signed=True, default=0)
    header = message.table(2)
    if version is None or version not in set(range(6)) or header_type != expected_header or header is None:
        raise ValueError("Arrow IPC message header is invalid")
    if body_length is None or body_length < 0 or body_length % 8:
        raise ValueError("Arrow IPC message body length is invalid")
    if expected_body_length is not None and body_length != expected_body_length:
        raise ValueError("Arrow block body length differs from its message")
    body_end = padded_metadata_end + body_length
    if body_end > region_end:
        raise ValueError("Arrow IPC message body exceeds its region")
    _arrow_node(budget, limits, "message")
    schema_summary: tuple[tuple[object, ...], ...] | None = None
    if expected_header == 1:
        if body_length:
            raise ValueError("Arrow schema message unexpectedly has a body")
        schema_summary = _validate_arrow_schema(header, limits, budget)
    elif expected_header == 2:
        identifier = header.scalar(0, 8, signed=True)
        data = header.table(1)
        if identifier is None or data is None:
            raise ValueError("Arrow dictionary batch header is invalid")
        _validate_arrow_record_batch(data, body_length, limits, budget)
    elif expected_header == 3:
        _validate_arrow_record_batch(
            header,
            body_length,
            limits,
            budget,
            expected_schema,
        )
    return body_end, schema_summary


def _arrow_blocks(
    footer: _FlatBufferTable,
    ordinal: int,
    header_type: int,
    footer_start: int,
    limits: PackValidationLimitsV1,
    budget: list[int],
) -> tuple[tuple[int, int, int, int], ...]:
    vector = footer.vector(ordinal, 24)
    if vector is None:
        return ()
    start, count = vector
    if budget[0] + count > limits.maximum_parse_nodes:
        raise _PayloadComplexityError(
            "Arrow block count exceeds its limit",
            budget[0] + count,
            limits.maximum_parse_nodes,
        )
    result: list[tuple[int, int, int, int]] = []
    previous = -1
    for index in range(count):
        position = start + index * 24
        offset = int.from_bytes(footer._data[position : position + 8], "little", signed=True)
        metadata_length = int.from_bytes(
            footer._data[position + 8 : position + 12],
            "little",
            signed=True,
        )
        body_length = int.from_bytes(
            footer._data[position + 16 : position + 24],
            "little",
            signed=True,
        )
        if (
            offset < 8
            or offset % 8
            or metadata_length <= 0
            or metadata_length % 8
            or body_length < 0
            or body_length % 8
            or offset + metadata_length + body_length > footer_start
            or offset <= previous
        ):
            raise ValueError("Arrow footer block range is invalid")
        previous = offset
        _arrow_node(budget, limits, "block")
        result.append((offset, metadata_length, body_length, header_type))
    return tuple(result)


def _validate_arrow_structure(raw: bytes, limits: PackValidationLimitsV1) -> None:
    if (
        len(raw) < 26
        or not raw.startswith(b"ARROW1")
        or raw[6:8] != b"\x00\x00"
        or not raw.endswith(b"ARROW1")
    ):
        raise ValueError("invalid Arrow IPC file framing")
    footer_size = int.from_bytes(raw[-10:-6], "little")
    footer_start = len(raw) - 10 - footer_size
    if footer_size < 8 or footer_start < 16:
        raise ValueError("invalid Arrow footer bounds")
    maximum_footer_bytes = min(
        limits.maximum_central_directory_bytes,
        limits.maximum_parse_nodes * 64,
    )
    if footer_size > maximum_footer_bytes:
        raise _PayloadComplexityError(
            "Arrow footer exceeds its structural byte limit",
            footer_size,
            maximum_footer_bytes,
        )
    budget = [0]
    footer = _FlatBufferTable.root(raw, footer_start, len(raw) - 10)
    _arrow_require_fields(footer, {0, 1, 2, 3}, "footer")
    version = footer.scalar(0, 2, signed=True)
    schema = footer.table(1)
    if version is None or version not in set(range(6)) or schema is None:
        raise ValueError("Arrow footer version or schema is invalid")
    footer_schema = _validate_arrow_schema(schema, limits, budget)
    dictionaries = _arrow_blocks(
        footer,
        2,
        2,
        footer_start,
        limits,
        budget,
    )
    batches = _arrow_blocks(
        footer,
        3,
        3,
        footer_start,
        limits,
        budget,
    )
    blocks = sorted((*dictionaries, *batches))
    schema_end, message_schema = _validate_arrow_message(
        raw,
        8,
        footer_start,
        1,
        limits,
        budget,
    )
    if message_schema != footer_schema:
        raise ValueError("Arrow schema message differs from its footer schema")
    expected_offset = schema_end
    for offset, metadata_length, body_length, header_type in blocks:
        if offset != expected_offset:
            raise ValueError("Arrow IPC blocks are not contiguous or ordered")
        observed_end, _ = _validate_arrow_message(
            raw,
            offset,
            footer_start,
            header_type,
            limits,
            budget,
            expected_metadata_length=metadata_length,
            expected_body_length=body_length,
            expected_schema=footer_schema if header_type == 3 else None,
        )
        expected_offset = observed_end
    if expected_offset + 8 != footer_start or raw[expected_offset:footer_start] != b"\xff\xff\xff\xff\x00\x00\x00\x00":
        raise ValueError("Arrow IPC stream lacks one terminal EOS marker")


__all__ = [
    "ALLOWED_PACK_ZIP_COMPRESSION_METHODS_V1",
    "DEFAULT_PACK_VALIDATION_LIMITS_V1",
    "PACK_VALIDATION_POLICY_ALGORITHM",
    "PACK_VALIDATION_POLICY_SCHEMA_ID",
    "PACK_VALIDATION_POLICY_SCHEMA_VERSION",
    "PackRefusalCodeV1",
    "PackRefusalV1",
    "PackValidationLimitsV1",
    "PackValidationPhaseV1",
    "PackValidationRefused",
    "enforce_pack_limit",
    "refuse",
    "require_validation_limits",
    "detect_nested_archive",
    "validate_integer_compression_ratio",
    "validate_manifest_complexity",
    "validate_pack_member_path",
    "validate_pack_member_paths",
    "validate_parse_complexity",
    "validate_structural_payload",
    "validation_policy_id",
]
