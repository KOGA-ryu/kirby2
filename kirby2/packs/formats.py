"""Canonical, data-only format rules for portable Kirby2 packs.

WO39-A deliberately keeps logical identity independent of the archive container.
This module owns the byte encodings and declarations that are safe to place in a
``.k2pack``.  Archive bounds, hostile extraction, and activation arrive in WO39-B
and WO39-C; callers here operate only on already supplied bytes and logical paths.
"""

from __future__ import annotations

import json
import re
import stat
import tomllib
import unicodedata
import zipfile
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

from kirby2.research.toml_codec import canonical_toml


K2PACK_MANIFEST_PATH = "manifest.toml"
K2PACK_CANONICALIZATION_ID = "KIRBY2_K2PACK_CANONICALIZATION_V1"
K2PACK_CANONICALIZATION_VERSION = 1
K2PACK_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
K2PACK_ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
K2PACK_ZIP_COMPRESSLEVEL = 9

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DATA_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_NAMESPACE_SEGMENT = re.compile(r"[a-z][a-z0-9-]*\Z")
_SEMVER = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)
_RANGE_TERM = re.compile(r"(?P<operator>>=|>|<=|<)?(?P<version>.+)\Z")

_FORMAT_MEDIA_TYPES = {
    "TOML": frozenset({"application/toml"}),
    "PARQUET": frozenset({"application/vnd.apache.parquet"}),
    "CANONICAL_JSON": frozenset({"application/json"}),
    "CANONICAL_EVENT_STREAM": frozenset({"application/x-ndjson"}),
    "REPORT_DATA": frozenset({"application/vnd.kirby2.report+json"}),
    "BINARY_EVIDENCE": frozenset(
        {
            "application/vnd.apache.arrow.file",
            "image/jpeg",
            "image/png",
            "image/tiff",
            "image/webp",
        }
    ),
}
_FORMAT_SUFFIXES = {
    "TOML": (".toml",),
    "PARQUET": (".parquet",),
    "CANONICAL_JSON": (".json",),
    "CANONICAL_EVENT_STREAM": (".jsonl",),
    "REPORT_DATA": (".report.json",),
}
_BINARY_MEDIA_SUFFIXES = {
    "application/vnd.apache.arrow.file": (".arrow", ".feather"),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/tiff": (".tif", ".tiff"),
    "image/webp": (".webp",),
}
_PROHIBITED_SUFFIXES = frozenset(
    {
        ".7z",
        ".app",
        ".bat",
        ".bash",
        ".bz2",
        ".cjs",
        ".class",
        ".cmd",
        ".com",
        ".deb",
        ".dll",
        ".dylib",
        ".exe",
        ".fish",
        ".gz",
        ".htm",
        ".html",
        ".jar",
        ".js",
        ".k2pack",
        ".mjs",
        ".msi",
        ".pdf",
        ".ps1",
        ".py",
        ".pyc",
        ".pyd",
        ".rar",
        ".rpm",
        ".sh",
        ".so",
        ".svg",
        ".tar",
        ".wasm",
        ".whl",
        ".xlsm",
        ".xz",
        ".zsh",
        ".zip",
    }
)
_EXECUTABLE_MAGICS = (
    b"#!",
    b"MZ",
    b"PK\x03\x04",
    b"\x00asm",
    b"\x7fELF",
    b"\xca\xfe\xba\xbe",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)


def require_nfc_text(
    value: object,
    label: str,
    *,
    maximum_bytes: int = 4096,
) -> str:
    """Return bounded, nonempty NFC text without transport control bytes."""

    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC-normalized")
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{label} contains a forbidden control/code point")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its canonical byte limit")
    return value


def require_data_identifier(value: object, label: str) -> str:
    result = require_nfc_text(value, label, maximum_bytes=256)
    if _DATA_IDENTIFIER.fullmatch(result) is None:
        raise ValueError(f"{label} must be one canonical data identifier")
    return result


def require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def require_namespace(value: object, label: str = "pack namespace") -> str:
    result = require_nfc_text(value, label, maximum_bytes=255)
    segments = result.split(".")
    if not segments or any(_NAMESPACE_SEGMENT.fullmatch(item) is None for item in segments):
        raise ValueError(
            f"{label} must be lowercase dot-separated [a-z][a-z0-9-]* segments"
        )
    return result


def require_pack_name(value: object, label: str = "pack name") -> str:
    result = require_nfc_text(value, label, maximum_bytes=128)
    if _NAMESPACE_SEGMENT.fullmatch(result) is None:
        raise ValueError(f"{label} must match [a-z][a-z0-9-]*")
    return result


def require_semver(value: object, label: str = "semantic version") -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be text")
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"{label} must be canonical SemVer 2.0.0")
    prerelease = match.group("prerelease")
    if prerelease is not None:
        for identifier in prerelease.split("."):
            if identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0":
                raise ValueError(
                    f"{label} numeric prerelease identifiers cannot have leading zeroes"
                )
    return value


def require_semver_range(value: object, label: str = "semantic-version range") -> str:
    """Validate Kirby2's deliberately small canonical SemVer range grammar.

    A range is ``*``, one exact version, one lower/upper bound, or a lower bound
    followed by an upper bound (for example ``>=1.2.0,<2.0.0``).  Shorthand such
    as ``^``, ``~``, whitespace, unions, and a redundant ``=`` are rejected so one
    semantic constraint has one byte spelling.
    """

    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty canonical text")
    if value == "*":
        return value
    if any(character.isspace() for character in value) or "||" in value:
        raise ValueError(f"{label} contains noncanonical whitespace or union syntax")
    raw_terms = value.split(",")
    if len(raw_terms) > 2 or any(not term for term in raw_terms):
        raise ValueError(f"{label} must contain at most one lower and one upper bound")

    terms: list[tuple[str, str]] = []
    for raw_term in raw_terms:
        match = _RANGE_TERM.fullmatch(raw_term)
        if match is None:
            raise ValueError(f"{label} contains an invalid comparator")
        operator = match.group("operator") or ""
        version = require_semver(match.group("version"), f"{label} version")
        if operator and "+" in version:
            raise ValueError(
                f"{label} comparator bounds cannot contain ignored build metadata"
            )
        terms.append((operator, version))

    if len(terms) == 1:
        return value
    lower, upper = terms
    if lower[0] not in {">", ">="} or upper[0] not in {"<", "<="}:
        raise ValueError(f"{label} bounds must be written lower then upper")
    comparison = compare_semver_precedence(lower[1], upper[1])
    if comparison >= 0:
        raise ValueError(f"{label} bounds are empty or should be one exact version")
    return value


def compare_semver_precedence(left: str, right: str) -> int:
    """Compare canonical SemVer precedence, ignoring build metadata."""

    left_parts = _semver_parts(require_semver(left, "left semantic version"))
    right_parts = _semver_parts(require_semver(right, "right semantic version"))
    for left_number, right_number in zip(left_parts[:3], right_parts[:3], strict=True):
        if left_number != right_number:
            return -1 if left_number < right_number else 1
    return _compare_prerelease(left_parts[3], right_parts[3])


def require_relative_pack_path(value: object, label: str = "pack payload path") -> str:
    result = require_nfc_text(value, label, maximum_bytes=1024)
    posix = PurePosixPath(result)
    windows = PureWindowsPath(result)
    if (
        "\\" in result
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or result != posix.as_posix()
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError(f"{label} must be canonical relative POSIX")
    if (
        posix.parts
        and posix.parts[0].casefold() == K2PACK_MANIFEST_PATH.casefold()
    ):
        raise ValueError(f"{label} uses the reserved pack manifest path")
    return result


def require_content_declaration(
    *,
    path: object,
    content_format: object,
    media_type: object,
    schema_id: object,
) -> tuple[str, str, str, str]:
    """Validate one allowlisted, explicitly typed, non-executable payload."""

    canonical_path = require_relative_pack_path(path)
    format_name = require_data_identifier(content_format, "pack content format")
    if format_name not in _FORMAT_MEDIA_TYPES:
        raise ValueError("pack content format is not allowlisted")
    canonical_media_type = require_nfc_text(media_type, "pack media type", maximum_bytes=128)
    if canonical_media_type not in _FORMAT_MEDIA_TYPES[format_name]:
        raise ValueError("pack media type does not match its declared content format")
    canonical_schema_id = require_data_identifier(schema_id, "pack payload schema ID")

    lower_path = canonical_path.lower()
    if any(lower_path.endswith(suffix) for suffix in _PROHIBITED_SUFFIXES):
        raise ValueError("pack payload path has an executable or container suffix")
    if format_name == "BINARY_EVIDENCE":
        suffixes = _BINARY_MEDIA_SUFFIXES[canonical_media_type]
    else:
        suffixes = _FORMAT_SUFFIXES[format_name]
    if not any(lower_path.endswith(suffix) for suffix in suffixes):
        raise ValueError("pack payload suffix does not match its declared content format")
    return canonical_path, format_name, canonical_media_type, canonical_schema_id


def inspect_payload_format_claim(
    raw: bytes,
    *,
    path: object,
    content_format: object,
    media_type: object,
    schema_id: object,
) -> None:
    """Inspect a format claim without asserting WO39-B archive/parser safety.

    Canonical textual formats receive complete byte-form checks.  Parquet, Arrow,
    and image declarations receive only their closed suffix/media/magic screening;
    bounded complete parsers and hostile/polyglot rejection belong to WO39-B.
    """

    canonical_path, format_name, canonical_media_type, _ = require_content_declaration(
        path=path,
        content_format=content_format,
        media_type=media_type,
        schema_id=schema_id,
    )
    if type(raw) is not bytes or not raw:
        raise ValueError(f"pack payload {canonical_path!r} must contain exact bytes")
    if format_name == "TOML":
        load_canonical_toml_bytes(raw, canonical_path)
    elif format_name in {"CANONICAL_JSON", "REPORT_DATA"}:
        load_canonical_json_bytes(raw, canonical_path)
    elif format_name == "CANONICAL_EVENT_STREAM":
        _validate_canonical_event_stream(raw, canonical_path)
    elif format_name == "PARQUET":
        if len(raw) < 12 or not raw.startswith(b"PAR1") or not raw.endswith(b"PAR1"):
            raise ValueError(f"pack payload {canonical_path!r} is not a Parquet file")
    else:
        _validate_binary_evidence(raw, canonical_path, canonical_media_type)


def canonical_json_bytes(value: object) -> bytes:
    _validate_json_value(value, set())
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def load_canonical_json_bytes(raw: bytes, label: str = "canonical JSON") -> object:
    if type(raw) is not bytes or not raw:
        raise ValueError(f"{label} must contain nonempty exact bytes")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} bytes are not canonical JSON")
    return value


def canonical_toml_bytes(value: Mapping[str, object]) -> bytes:
    if type(value) is not dict:
        raise TypeError("canonical TOML root must be an exact object")
    _validate_toml_identity_value(value, set())
    return canonical_toml(value).encode("utf-8")


def load_canonical_toml_bytes(raw: bytes, label: str = "canonical TOML") -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise ValueError(f"{label} must contain nonempty exact bytes")
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{label} must be valid UTF-8 TOML") from error
    if canonical_toml_bytes(parsed) != raw:
        raise ValueError(f"{label} bytes are not canonical TOML")
    return parsed


def canonical_manifest_bytes(manifest: object) -> bytes:
    from .models import PackManifestV1

    if type(manifest) is not PackManifestV1:
        raise TypeError("canonical pack manifest requires PackManifestV1")
    return canonical_toml_bytes(manifest.as_dict())


def load_manifest_bytes(raw: bytes) -> object:
    from .models import PackManifestV1

    manifest = PackManifestV1.from_dict(load_canonical_toml_bytes(raw, "pack manifest"))
    if canonical_manifest_bytes(manifest) != raw:
        raise ValueError("pack manifest did not survive exact canonical reconstruction")
    return manifest


def normalized_zip_info(path: object) -> zipfile.ZipInfo:
    """Return the sole normalized ZIP metadata shape used by future builders."""

    if path == K2PACK_MANIFEST_PATH:
        canonical_path = K2PACK_MANIFEST_PATH
    else:
        canonical_path = require_relative_pack_path(path, "pack archive path")
    info = zipfile.ZipInfo(canonical_path, date_time=K2PACK_ZIP_TIMESTAMP)
    info.compress_type = K2PACK_ZIP_COMPRESSION
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits = 0x800
    info.extra = b""
    info.comment = b""
    return info


def normalized_archive_paths(paths: object) -> tuple[str, ...]:
    """Return canonical ZIP ordering, not a WO39-B archive-safety verdict."""

    if type(paths) not in {tuple, list}:
        raise TypeError("pack archive paths must be one ordered sequence")
    normalized = tuple(
        K2PACK_MANIFEST_PATH
        if item == K2PACK_MANIFEST_PATH
        else require_relative_pack_path(item, "pack archive path")
        for item in paths
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError("pack archive paths contain duplicates")
    collision_keys = tuple(item.casefold() for item in normalized)
    if len(collision_keys) != len(set(collision_keys)):
        raise ValueError("pack archive paths contain case-fold collisions")
    ordered = tuple(sorted(normalized, key=lambda item: item.encode("utf-8")))
    path_set = set(ordered)
    for path in ordered:
        parts = PurePosixPath(path).parts
        if any("/".join(parts[:depth]) in path_set for depth in range(1, len(parts))):
            raise ValueError("pack archive paths contain file/directory prefix collisions")
    return ordered


def _semver_parts(value: str) -> tuple[int, int, int, tuple[str, ...] | None]:
    match = _SEMVER.fullmatch(value)
    if match is None:  # pragma: no cover - guarded by require_semver
        raise AssertionError("validated SemVer disappeared")
    prerelease = match.group("prerelease")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        None if prerelease is None else tuple(prerelease.split(".")),
    )


def _compare_prerelease(
    left: tuple[str, ...] | None,
    right: tuple[str, ...] | None,
) -> int:
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    for left_item, right_item in zip(left, right):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("canonical JSON contains a duplicate object key")
        result[key] = value
    return result


def _validate_json_value(value: object, active: set[int]) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("canonical JSON text must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("canonical JSON text contains a surrogate code point")
        return
    if type(value) is float:
        raise TypeError("canonical identity JSON forbids binary floats")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("canonical JSON values must not contain cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_json_value(key, active)
                _validate_json_value(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("canonical JSON values must not contain cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_json_value(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _validate_toml_identity_value(value: object, active: set[int]) -> None:
    if type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("canonical TOML text must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("canonical TOML text contains a surrogate code point")
        return
    if value is None or type(value) is float:
        raise TypeError("canonical pack TOML forbids nulls and binary floats")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("canonical TOML object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("canonical TOML values must not contain cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_toml_identity_value(key, active)
                _validate_toml_identity_value(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("canonical TOML values must not contain cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_toml_identity_value(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported canonical TOML value: {type(value).__name__}")


def _validate_canonical_event_stream(raw: bytes, label: str) -> None:
    if not raw.endswith(b"\n"):
        raise ValueError(f"{label} must end with one canonical LF")
    rows = raw[:-1].split(b"\n")
    if not rows or any(not row for row in rows):
        raise ValueError(f"{label} contains an empty event row")
    for ordinal, row in enumerate(rows, start=1):
        value = load_canonical_json_bytes(row, f"{label} row {ordinal}")
        if type(value) is not dict:
            raise TypeError(f"{label} row {ordinal} must be one canonical object")


def _validate_binary_evidence(raw: bytes, label: str, media_type: str) -> None:
    if any(raw.startswith(magic) for magic in _EXECUTABLE_MAGICS):
        raise ValueError(f"{label} has executable or nested-container magic bytes")
    if media_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{label} does not have PNG magic bytes")
    if media_type == "image/jpeg" and not (raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")):
        raise ValueError(f"{label} does not have complete JPEG markers")
    if media_type == "image/webp" and not (
        len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
    ):
        raise ValueError(f"{label} does not have WebP magic bytes")
    if media_type == "image/tiff" and not raw.startswith((b"II*\x00", b"MM\x00*")):
        raise ValueError(f"{label} does not have TIFF magic bytes")
    if media_type == "application/vnd.apache.arrow.file" and not (
        raw.startswith(b"ARROW1") and raw.endswith(b"ARROW1")
    ):
        raise ValueError(f"{label} does not have Arrow file magic bytes")


__all__ = [
    "K2PACK_CANONICALIZATION_ID",
    "K2PACK_CANONICALIZATION_VERSION",
    "K2PACK_MANIFEST_PATH",
    "K2PACK_ZIP_COMPRESSLEVEL",
    "K2PACK_ZIP_COMPRESSION",
    "K2PACK_ZIP_TIMESTAMP",
    "canonical_json_bytes",
    "canonical_manifest_bytes",
    "canonical_toml_bytes",
    "compare_semver_precedence",
    "load_canonical_json_bytes",
    "load_canonical_toml_bytes",
    "load_manifest_bytes",
    "normalized_archive_paths",
    "normalized_zip_info",
    "require_content_declaration",
    "require_data_identifier",
    "require_namespace",
    "require_nfc_text",
    "require_pack_name",
    "require_relative_pack_path",
    "require_semver",
    "require_semver_range",
    "require_sha256",
    "inspect_payload_format_claim",
]
