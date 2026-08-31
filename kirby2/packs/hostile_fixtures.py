"""Governed hostile-archive fixtures for the WO39-B validation boundary.

The committed TOML file governs fixture identity and expected stable refusal data.
Archive bytes are rebuilt deterministically from one caller-supplied valid manifest
and payload inventory so hostile cases never become a second pack format.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import struct
import tomllib
import warnings
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .formats import (
    K2PACK_MANIFEST_PATH,
    K2PACK_ZIP_COMPRESSION,
    K2PACK_ZIP_TIMESTAMP,
    canonical_json_bytes,
    canonical_manifest_bytes,
    normalized_archive_paths,
)
from .identity import verify_pack_payload_identity
from .models import (
    PackContentFormatV1,
    PackFileV1,
    PackManifestV1,
)
from .validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackRefusalCodeV1,
    PackValidationLimitsV1,
    PackValidationPhaseV1,
)


HOSTILE_ARCHIVE_FIXTURE_SCHEMA_ID = "KIRBY2_HOSTILE_ARCHIVE_FIXTURE_SET_V1"
HOSTILE_ARCHIVE_FIXTURE_SCHEMA_VERSION = 1
HOSTILE_ARCHIVE_FIXTURE_MANIFEST = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "hostile_archive"
    / "manifest.toml"
)

_FIXTURE_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_UNIX_EXTRA_ID = 0x000D


@dataclass(frozen=True, slots=True)
class HostileArchiveFixtureSpecV1:
    fixture_id: str
    attack_kind: str
    expected_code: PackRefusalCodeV1
    expected_phase: PackValidationPhaseV1

    def __post_init__(self) -> None:
        if type(self.fixture_id) is not str or _FIXTURE_ID.fullmatch(self.fixture_id) is None:
            raise ValueError("hostile archive fixture ID is invalid")
        if type(self.attack_kind) is not str or self.attack_kind not in _ATTACK_BUILDERS:
            raise ValueError("hostile archive attack kind is unsupported")
        if type(self.expected_code) is not PackRefusalCodeV1:
            raise TypeError("hostile archive expected refusal code is invalid")
        if type(self.expected_phase) is not PackValidationPhaseV1:
            raise TypeError("hostile archive expected refusal phase is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "attack_kind": self.attack_kind,
            "expected_code": self.expected_code.value,
            "expected_phase": self.expected_phase.value,
            "fixture_id": self.fixture_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> HostileArchiveFixtureSpecV1:
        if type(value) is not dict or set(value) != {
            "attack_kind",
            "expected_code",
            "expected_phase",
            "fixture_id",
        }:
            raise ValueError("hostile archive fixture spec fields differ")
        try:
            return cls(
                fixture_id=_text(value["fixture_id"], "fixture ID"),
                attack_kind=_text(value["attack_kind"], "attack kind"),
                expected_code=PackRefusalCodeV1(value["expected_code"]),
                expected_phase=PackValidationPhaseV1(value["expected_phase"]),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("hostile archive fixture spec is invalid") from error


@dataclass(frozen=True, slots=True)
class HostileArchiveFixtureV1:
    spec: HostileArchiveFixtureSpecV1
    archive_bytes: bytes
    limits: PackValidationLimitsV1

    def __post_init__(self) -> None:
        if type(self.spec) is not HostileArchiveFixtureSpecV1:
            raise TypeError("hostile archive fixture spec is invalid")
        if type(self.archive_bytes) is not bytes or not self.archive_bytes:
            raise ValueError("hostile archive fixture bytes are empty")
        if type(self.limits) is not PackValidationLimitsV1:
            raise TypeError("hostile archive fixture limits are invalid")

    @property
    def fixture_id(self) -> str:
        return self.spec.fixture_id

    @property
    def transport_sha256(self) -> str:
        return hashlib.sha256(self.archive_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class _MemberSourceV1:
    path: str
    raw: bytes
    mode: int = stat.S_IFREG | 0o644
    compression: int = K2PACK_ZIP_COMPRESSION
    extra: bytes = b""


_FixtureBuilder = Callable[
    [PackManifestV1, dict[str, bytes]],
    tuple[bytes, PackValidationLimitsV1],
]


def load_hostile_archive_fixture_specs(
    path: Path = HOSTILE_ARCHIVE_FIXTURE_MANIFEST,
) -> tuple[HostileArchiveFixtureSpecV1, ...]:
    """Load the exact committed fixture inventory without following a symlink."""

    if not isinstance(path, Path):
        raise TypeError("hostile archive fixture manifest path must be pathlib.Path")
    if path.is_symlink() or not path.is_file():
        raise ValueError("hostile archive fixture manifest must be a regular file")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("hostile archive fixture manifest is invalid TOML") from error
    if set(payload) != {"fixtures", "schema_id", "schema_version"}:
        raise ValueError("hostile archive fixture manifest fields differ")
    if payload["schema_id"] != HOSTILE_ARCHIVE_FIXTURE_SCHEMA_ID:
        raise ValueError("hostile archive fixture schema ID differs")
    if payload["schema_version"] != HOSTILE_ARCHIVE_FIXTURE_SCHEMA_VERSION:
        raise ValueError("hostile archive fixture schema version differs")
    raw_specs = payload["fixtures"]
    if type(raw_specs) is not list or not raw_specs:
        raise ValueError("hostile archive fixture manifest requires fixtures")
    specs = tuple(HostileArchiveFixtureSpecV1.from_dict(item) for item in raw_specs)
    fixture_ids = tuple(item.fixture_id for item in specs)
    attack_kinds = tuple(item.attack_kind for item in specs)
    if len(fixture_ids) != len(set(fixture_ids)):
        raise ValueError("hostile archive fixture IDs repeat")
    if len(attack_kinds) != len(set(attack_kinds)):
        raise ValueError("hostile archive attack kinds repeat")
    if set(attack_kinds) != set(_ATTACK_BUILDERS):
        raise ValueError("hostile archive fixture manifest and builders differ")
    return specs


def build_hostile_archive_fixtures(
    manifest: PackManifestV1,
    payloads: Mapping[str, bytes],
    *,
    spec_path: Path = HOSTILE_ARCHIVE_FIXTURE_MANIFEST,
) -> tuple[HostileArchiveFixtureV1, ...]:
    """Derive every governed hostile transport from one valid data-only pack."""

    if type(manifest) is not PackManifestV1:
        raise TypeError("hostile fixtures require PackManifestV1")
    if not isinstance(payloads, Mapping):
        raise TypeError("hostile fixtures require a payload mapping")
    snapshot = dict(payloads)
    if any(type(path) is not str or type(raw) is not bytes for path, raw in snapshot.items()):
        raise TypeError("hostile fixture payloads require exact paths and bytes")
    verify_pack_payload_identity(manifest, snapshot)
    specs = load_hostile_archive_fixture_specs(spec_path)
    fixtures: list[HostileArchiveFixtureV1] = []
    for spec in specs:
        archive_bytes, limits = _ATTACK_BUILDERS[spec.attack_kind](
            manifest,
            snapshot,
        )
        fixtures.append(
            HostileArchiveFixtureV1(
                spec=spec,
                archive_bytes=archive_bytes,
                limits=limits,
            )
        )
    return tuple(fixtures)


def _path_replacement(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    replacement_path: str,
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    entries = tuple(
        replace(item, path=replacement_path) if item.path == selected else item
        for item in _base_members(manifest, payloads)
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _file_mode_attack(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    mode: int,
    *,
    extra: bytes = b"",
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    entries = tuple(
        replace(item, mode=mode, extra=extra) if item.path == selected else item
        for item in _base_members(manifest, payloads)
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _duplicate_path(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    entries = list(_base_members(manifest, payloads))
    selected = next(item for item in entries if item.path == _selected_json_path(manifest))
    entries.append(selected)
    return _archive(tuple(entries)), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _casefold_collision(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    entries = (
        *_base_members(manifest, payloads),
        _MemberSourceV1("data/Case.json", b"{}"),
        _MemberSourceV1("data/case.json", b"{}"),
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _file_directory_collision(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    entries = (
        *_base_members(manifest, payloads),
        _MemberSourceV1("data/collision", b"{}"),
        _MemberSourceV1("data/collision/value.json", b"{}"),
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _undeclared_file(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    entries = (
        *_base_members(manifest, payloads),
        _MemberSourceV1(
            "data/undeclared.json",
            canonical_json_bytes({"undeclared": True}),
        ),
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _digest_mismatch(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    changed = _same_length_mutation(payloads[selected])
    entries = tuple(
        replace(item, raw=changed) if item.path == selected else item
        for item in _base_members(manifest, payloads)
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _nested_archive(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    nested = b"PK\x03\x04nested archive payload"
    changed_manifest, changed_payloads = _replace_payload(
        manifest,
        payloads,
        selected,
        nested,
    )
    return _archive(_base_members(changed_manifest, changed_payloads)), (
        DEFAULT_PACK_VALIDATION_LIMITS_V1
    )


def _type_spoofing(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    changed_manifest, changed_payloads = _replace_payload(
        manifest,
        payloads,
        selected,
        b"\x7fELFhostile executable bytes",
        new_path="data/figure.png",
        content_format=PackContentFormatV1.BINARY_EVIDENCE,
        media_type="image/png",
    )
    return _archive(_base_members(changed_manifest, changed_payloads)), (
        DEFAULT_PACK_VALIDATION_LIMITS_V1
    )


def _compression_ratio(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    raw = canonical_json_bytes(
        {
            "padding": "A" * 32_768,
            "schema_id": next(
                item.schema_id for item in manifest.inventory if item.path == selected
            ),
            "schema_version": 1,
        }
    )
    changed_manifest, changed_payloads = _replace_payload(
        manifest,
        payloads,
        selected,
        raw,
    )
    limits = replace(
        DEFAULT_PACK_VALIDATION_LIMITS_V1,
        maximum_compression_ratio=2,
    )
    return _archive(_base_members(changed_manifest, changed_payloads)), limits


def _unsupported_compression(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[bytes, PackValidationLimitsV1]:
    selected = _selected_json_path(manifest)
    entries = tuple(
        replace(item, compression=zipfile.ZIP_BZIP2)
        if item.path == selected
        else item
        for item in _base_members(manifest, payloads)
    )
    return _archive(entries), DEFAULT_PACK_VALIDATION_LIMITS_V1


def _replace_payload(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
    path: str,
    raw: bytes,
    *,
    new_path: str | None = None,
    content_format: PackContentFormatV1 | None = None,
    media_type: str | None = None,
) -> tuple[PackManifestV1, dict[str, bytes]]:
    original = next(item for item in manifest.inventory if item.path == path)
    destination = path if new_path is None else new_path
    replacement = replace(
        original,
        path=destination,
        byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        content_format=(
            original.content_format if content_format is None else content_format
        ),
        media_type=original.media_type if media_type is None else media_type,
    )
    inventory = tuple(
        sorted(
            (replacement if item.path == path else item for item in manifest.inventory),
            key=lambda item: item.sort_key,
        )
    )
    entrypoints = tuple(
        sorted(
            (
                replace(item, path=destination) if item.path == path else item
                for item in manifest.entrypoints
            ),
            key=lambda item: item.sort_key,
        )
    )
    changed_manifest = replace(
        manifest,
        inventory=inventory,
        entrypoints=entrypoints,
    )
    changed_payloads = dict(payloads)
    del changed_payloads[path]
    changed_payloads[destination] = raw
    return changed_manifest, changed_payloads


def _base_members(
    manifest: PackManifestV1,
    payloads: dict[str, bytes],
) -> tuple[_MemberSourceV1, ...]:
    values = {
        K2PACK_MANIFEST_PATH: canonical_manifest_bytes(manifest),
        **payloads,
    }
    return tuple(
        _MemberSourceV1(path=path, raw=values[path])
        for path in normalized_archive_paths(tuple(values))
    )


def _archive(entries: tuple[_MemberSourceV1, ...]) -> bytes:
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, mode="w", allowZip64=False) as archive:
            for item in entries:
                info = zipfile.ZipInfo(item.path, date_time=K2PACK_ZIP_TIMESTAMP)
                info.compress_type = item.compression
                info.create_system = 3
                info.external_attr = item.mode << 16
                info.extra = item.extra
                archive.writestr(info, item.raw)
    return output.getvalue()


def _selected_json_path(manifest: PackManifestV1) -> str:
    candidates = tuple(
        item.path
        for item in manifest.inventory
        if item.content_format is PackContentFormatV1.CANONICAL_JSON
    )
    if not candidates:
        raise ValueError("hostile fixture base pack requires canonical JSON")
    return candidates[0]


def _same_length_mutation(raw: bytes) -> bytes:
    changed = bytearray(raw)
    for index, value in enumerate(changed):
        if value in b"0123456789":
            changed[index] = ord("0") + ((value - ord("0") + 1) % 10)
            return bytes(changed)
    changed[-1] ^= 1
    return bytes(changed)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"hostile archive {label} must be nonempty text")
    return value


_PATH_REPLACEMENTS = {
    "ABSOLUTE_PATH": "/absolute/escape.json",
    "BACKSLASH_PATH": "data\\escape.json",
    "PARENT_TRAVERSAL": "../escape.json",
    "UNC_PATH": "//server/share/escape.json",
    "WINDOWS_DRIVE_PATH": "C:/escape.json",
}
_FILE_MODES = {
    "DEVICE_ENTRY": stat.S_IFCHR | 0o600,
    "FIFO_ENTRY": stat.S_IFIFO | 0o600,
    "SPECIAL_ENTRY": stat.S_IFSOCK | 0o600,
    "SYMLINK_ENTRY": stat.S_IFLNK | 0o777,
}
_HARDLINK_EXTRA = struct.pack("<HH", _UNIX_EXTRA_ID, 13) + (b"\x00" * 13)


def _path_builder(attack_kind: str) -> _FixtureBuilder:
    return lambda manifest, payloads: _path_replacement(
        manifest,
        payloads,
        _PATH_REPLACEMENTS[attack_kind],
    )


def _mode_builder(attack_kind: str) -> _FixtureBuilder:
    return lambda manifest, payloads: _file_mode_attack(
        manifest,
        payloads,
        _FILE_MODES[attack_kind],
    )


_ATTACK_BUILDERS: dict[str, _FixtureBuilder] = {
    **{name: _path_builder(name) for name in _PATH_REPLACEMENTS},
    **{name: _mode_builder(name) for name in _FILE_MODES},
    "CASEFOLD_COLLISION": _casefold_collision,
    "COMPRESSION_RATIO": _compression_ratio,
    "DIGEST_MISMATCH": _digest_mismatch,
    "DUPLICATE_PATH": _duplicate_path,
    "FILE_DIRECTORY_COLLISION": _file_directory_collision,
    "HARDLINK_ENTRY": lambda manifest, payloads: _file_mode_attack(
        manifest,
        payloads,
        stat.S_IFREG | 0o644,
        extra=_HARDLINK_EXTRA,
    ),
    "NESTED_ARCHIVE": _nested_archive,
    "TYPE_SPOOFING": _type_spoofing,
    "UNDECLARED_FILE": _undeclared_file,
    "UNSUPPORTED_COMPRESSION": _unsupported_compression,
}


__all__ = [
    "HOSTILE_ARCHIVE_FIXTURE_MANIFEST",
    "HOSTILE_ARCHIVE_FIXTURE_SCHEMA_ID",
    "HOSTILE_ARCHIVE_FIXTURE_SCHEMA_VERSION",
    "HostileArchiveFixtureSpecV1",
    "HostileArchiveFixtureV1",
    "build_hostile_archive_fixtures",
    "load_hostile_archive_fixture_specs",
]
