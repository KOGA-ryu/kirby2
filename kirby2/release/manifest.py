"""Canonical release manifests, logical identity, and external artifact indexes.

The embedded manifest intentionally cannot name its own bytes or the transport
digest of its containing archive.  Those values belong to the external artifact
index, which keeps both encodings finite and independently verifiable.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_nfc_text,
    require_semver,
    require_sha256,
)

from .packaging import normalize_release_path


RELEASE_VERSION_V1 = "0.1.0"
RELEASE_MANIFEST_SCHEMA_ID_V1 = "KIRBY2_RELEASE_MANIFEST_V1"
RELEASE_ARTIFACT_INDEX_SCHEMA_ID_V1 = "KIRBY2_RELEASE_ARTIFACT_INDEX_V1"
RELEASE_LOGICAL_BUILD_PROJECTION_SCHEMA_ID_V1 = (
    "KIRBY2_RELEASE_LOGICAL_BUILD_PROJECTION_V1"
)
RELEASE_MANIFEST_SCHEMA_VERSION_V1 = 1
RELEASE_ARTIFACT_INDEX_SCHEMA_VERSION_V1 = 1
RELEASE_LOGICAL_BUILD_PROJECTION_SCHEMA_VERSION_V1 = 1

RELEASE_PROTOCOL_PATHS_V1 = (
    "release/artifact_layout.toml",
    "release/performance_thresholds.toml",
    "release/platforms.toml",
    "release/qualification.toml",
    "release/requirements.lock",
)

RELEASE_ARTIFACT_ROWS_V1 = (
    ("linux-x86_64-desktop-bundle", "DESKTOP_TAR_GZ", "linux-x86_64", True),
    (
        "linux-x86_64-wheelhouse",
        "HEADLESS_WHEELHOUSE_TAR_GZ",
        "linux-x86_64",
        True,
    ),
    ("macos-arm64-desktop-bundle", "DESKTOP_TAR_GZ", "macos-arm64", True),
    (
        "macos-arm64-wheelhouse",
        "HEADLESS_WHEELHOUSE_TAR_GZ",
        "macos-arm64",
        True,
    ),
    ("project-wheel", "PY3_NONE_ANY_WHEEL", "any", False),
    ("source-archive", "SOURCE_TAR_GZ", "source", False),
)

RELEASE_ARTIFACT_SELECTORS_V1 = {
    "linux-x86_64/desktop": ("linux-x86_64-desktop-bundle",),
    "linux-x86_64/headless": (
        "project-wheel",
        "source-archive",
        "linux-x86_64-wheelhouse",
    ),
    "macos-arm64/desktop": ("macos-arm64-desktop-bundle",),
    "macos-arm64/headless": (
        "project-wheel",
        "source-archive",
        "macos-arm64-wheelhouse",
    ),
}

RELEASE_PAYLOAD_SOURCE_CLASSES_V1 = frozenset(
    {
        "CANDIDATE_PROJECT_WHEEL",
        "CANDIDATE_SOURCE",
        "CANDIDATE_LAUNCHER",
        "CANDIDATE_DOCUMENTATION",
        "CANDIDATE_ASSET",
        "LOCKED_DEPENDENCY_WHEEL",
        "GENERATED_MANIFEST",
        "GENERATED_LICENSE",
        "GENERATED_NOTICE",
        "GENERATED_LAYOUT",
        "CANDIDATE_STARTER_PACK",
    }
)

RELEASE_REQUIRED_KNOWN_LIMITATIONS_V1 = (
    (
        "NO_INVESTMENT_OR_PROFITABILITY_CLAIM",
        "Kirby2 does not provide investment advice or claim profitable strategies, "
        "validated mastery, or suitability for real-market decisions.",
    ),
    (
        "NO_LIVE_TRADING_CONNECTIVITY",
        "Kirby2 has no broker, exchange, order-routing, live-account, credential, "
        "or live-execution integration.",
    ),
    (
        "SYNTHETIC_SIMULATION_ONLY",
        "Outputs are deterministic mathematical simulation artifacts, not observed "
        "market data, real-market predictions, or evidence of empirical resemblance.",
    ),
)

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_NORMALIZED_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_ARTIFACT_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _sha256(raw: bytes) -> str:
    if type(raw) is not bytes:
        raise TypeError("release hashing requires exact bytes")
    return hashlib.sha256(raw).hexdigest()


def _exact_object(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} must contain exactly {sorted(fields)!r}")
    return value


def _exact_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _text(value: object, label: str, *, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _commit(value: object) -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise ValueError("candidate commit must be forty lowercase hexadecimal bytes")
    return value


def _sorted_unique(
    values: Sequence[object], key, label: str
) -> tuple[object, ...]:
    result = tuple(values)
    keys = tuple(key(item) for item in result)
    if keys != tuple(sorted(keys, key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be in ascending NFC UTF-8 order")
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must be unique")
    return result


@dataclass(frozen=True, slots=True)
class ReleaseTargetV1:
    system: str
    machine: str
    artifact_form: str

    def __post_init__(self) -> None:
        _text(self.system, "release target system", maximum_bytes=64)
        _text(self.machine, "release target machine", maximum_bytes=64)
        _text(self.artifact_form, "release target artifact form", maximum_bytes=128)

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_form": self.artifact_form,
            "machine": self.machine,
            "system": self.system,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseTargetV1":
        row = _exact_object(value, {"system", "machine", "artifact_form"}, "target")
        return cls(
            system=_text(row["system"], "target system"),
            machine=_text(row["machine"], "target machine"),
            artifact_form=_text(row["artifact_form"], "target artifact form"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseRuntimeV1:
    python_implementation: str
    python_version: str
    cache_tag: str
    compiler: str
    zlib_version: str

    def __post_init__(self) -> None:
        for label, value in self.as_dict().items():
            _text(value, f"runtime {label}", maximum_bytes=512)

    def as_dict(self) -> dict[str, object]:
        return {
            "cache_tag": self.cache_tag,
            "compiler": self.compiler,
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
            "zlib_version": self.zlib_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseRuntimeV1":
        fields = {
            "python_implementation",
            "python_version",
            "cache_tag",
            "compiler",
            "zlib_version",
        }
        row = _exact_object(value, fields, "runtime")
        return cls(**{key: _text(row[key], f"runtime {key}") for key in fields})


@dataclass(frozen=True, slots=True)
class ReleaseDependencyV1:
    name: str
    version: str
    wheel_filename: str
    wheel_sha256: str
    license_id: str

    def __post_init__(self) -> None:
        if _NORMALIZED_NAME.fullmatch(self.name) is None:
            raise ValueError("dependency name must be normalized lowercase text")
        _text(self.version, "dependency version", maximum_bytes=128)
        _text(self.wheel_filename, "dependency wheel filename", maximum_bytes=255)
        if "/" in self.wheel_filename or "\\" in self.wheel_filename:
            raise ValueError("dependency wheel filename cannot contain a path")
        require_sha256(self.wheel_sha256, "dependency wheel digest")
        _text(self.license_id, "dependency license ID", maximum_bytes=128)

    def as_dict(self) -> dict[str, object]:
        return {
            "license_id": self.license_id,
            "name": self.name,
            "version": self.version,
            "wheel_filename": self.wheel_filename,
            "wheel_sha256": self.wheel_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseDependencyV1":
        fields = {"name", "version", "wheel_filename", "wheel_sha256", "license_id"}
        row = _exact_object(value, fields, "dependency")
        return cls(
            name=_text(row["name"], "dependency name"),
            version=_text(row["version"], "dependency version"),
            wheel_filename=_text(row["wheel_filename"], "dependency wheel filename"),
            wheel_sha256=_text(row["wheel_sha256"], "dependency wheel digest"),
            license_id=_text(row["license_id"], "dependency license ID"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseSchemaVersionV1:
    schema_id: str
    version: int

    def __post_init__(self) -> None:
        _text(self.schema_id, "schema ID", maximum_bytes=256)
        if type(self.version) is not int or self.version <= 0:
            raise ValueError("schema version must be positive")

    def as_dict(self) -> dict[str, object]:
        return {"schema_id": self.schema_id, "version": self.version}

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseSchemaVersionV1":
        row = _exact_object(value, {"schema_id", "version"}, "schema version")
        return cls(
            schema_id=_text(row["schema_id"], "schema ID"),
            version=row["version"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReleaseStarterEntryManifestV1:
    role: str
    manifest_path: str
    manifest_sha256: str
    pack_id: str

    def __post_init__(self) -> None:
        if self.role not in {"SCENARIO", "CURRICULUM"}:
            raise ValueError("starter entry role is invalid")
        normalize_release_path(self.manifest_path, label="starter manifest path")
        require_sha256(self.manifest_sha256, "starter manifest digest")
        require_sha256(self.pack_id, "starter pack ID")

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseStarterEntryManifestV1":
        fields = {"role", "manifest_path", "manifest_sha256", "pack_id"}
        row = _exact_object(value, fields, "starter entry")
        return cls(
            role=_text(row["role"], "starter role"),
            manifest_path=_text(row["manifest_path"], "starter manifest path"),
            manifest_sha256=_text(row["manifest_sha256"], "starter manifest digest"),
            pack_id=_text(row["pack_id"], "starter pack ID"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseAssetV1:
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        normalize_release_path(self.path, label="asset path")
        _nonnegative(self.size, "asset size")
        require_sha256(self.sha256, "asset digest")

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseAssetV1":
        row = _exact_object(value, {"path", "size", "sha256"}, "asset")
        return cls(
            path=_text(row["path"], "asset path"),
            size=row["size"],  # type: ignore[arg-type]
            sha256=_text(row["sha256"], "asset digest"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseKnownLimitationV1:
    code: str
    detail: str

    def __post_init__(self) -> None:
        _text(self.code, "limitation code", maximum_bytes=128)
        _text(self.detail, "limitation detail", maximum_bytes=4096)

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "detail": self.detail}

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseKnownLimitationV1":
        row = _exact_object(value, {"code", "detail"}, "known limitation")
        return cls(
            code=_text(row["code"], "limitation code"),
            detail=_text(row["detail"], "limitation detail"),
        )


@dataclass(frozen=True, slots=True)
class ReleasePayloadMemberV1:
    path: str
    size: int
    sha256: str
    source_class: str

    def __post_init__(self) -> None:
        normalize_release_path(self.path, label="payload member path")
        _nonnegative(self.size, "payload member size")
        require_sha256(self.sha256, "payload member digest")
        _text(self.source_class, "payload member source class", maximum_bytes=128)
        if self.source_class not in RELEASE_PAYLOAD_SOURCE_CLASSES_V1:
            raise ValueError("payload member source class is outside the closed V1 enum")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "source_class": self.source_class,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleasePayloadMemberV1":
        fields = {"path", "size", "sha256", "source_class"}
        row = _exact_object(value, fields, "payload member")
        return cls(
            path=_text(row["path"], "payload member path"),
            size=row["size"],  # type: ignore[arg-type]
            sha256=_text(row["sha256"], "payload member digest"),
            source_class=_text(row["source_class"], "payload member source class"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseSubordinateArtifactV1:
    artifact_id: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _text(self.artifact_id, "subordinate artifact ID", maximum_bytes=256)
        _nonnegative(self.size, "subordinate artifact size")
        require_sha256(self.sha256, "subordinate artifact digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseSubordinateArtifactV1":
        row = _exact_object(value, {"artifact_id", "size", "sha256"}, "subordinate artifact")
        return cls(
            artifact_id=_text(row["artifact_id"], "subordinate artifact ID"),
            size=row["size"],  # type: ignore[arg-type]
            sha256=_text(row["sha256"], "subordinate artifact digest"),
        )


@dataclass(frozen=True, slots=True)
class ReleaseManifestV1:
    release_version: str
    candidate_commit: str
    build_timestamp: str
    target: ReleaseTargetV1
    runtime: ReleaseRuntimeV1
    dependencies: tuple[ReleaseDependencyV1, ...]
    schema_versions: tuple[ReleaseSchemaVersionV1, ...]
    starter_set_id: str
    starter_entries_sha256: str
    starter_entries: tuple[ReleaseStarterEntryManifestV1, ...]
    assets: tuple[ReleaseAssetV1, ...]
    known_limitations: tuple[ReleaseKnownLimitationV1, ...]
    license_inventory_sha256: str
    notices_sha256: str
    artifact_layout_sha256: str
    archive_root: str
    logical_build_id: str
    payload_members: tuple[ReleasePayloadMemberV1, ...]
    subordinate_artifacts: tuple[ReleaseSubordinateArtifactV1, ...]

    schema_version: ClassVar[int] = RELEASE_MANIFEST_SCHEMA_VERSION_V1
    supported_targets: ClassVar[tuple[dict[str, str], ...]] = (
        {"system": "Darwin", "machine": "arm64"},
        {"system": "Linux", "machine": "x86_64"},
    )

    def __post_init__(self) -> None:
        if require_semver(self.release_version, "release version") != RELEASE_VERSION_V1:
            raise ValueError(f"V1 release version must be {RELEASE_VERSION_V1}")
        _commit(self.candidate_commit)
        if type(self.build_timestamp) is not str or _UTC_SECOND.fullmatch(self.build_timestamp) is None:
            raise ValueError("build timestamp must be UTC to whole seconds")
        if type(self.target) is not ReleaseTargetV1 or type(self.runtime) is not ReleaseRuntimeV1:
            raise TypeError("release target and runtime must use exact V1 records")
        _sorted_unique(self.dependencies, lambda item: item.name, "dependencies")
        _sorted_unique(self.schema_versions, lambda item: item.schema_id, "schema versions")
        _text(self.starter_set_id, "starter set ID", maximum_bytes=128)
        require_sha256(self.starter_entries_sha256, "starter entries digest")
        if tuple(item.role for item in self.starter_entries) != ("SCENARIO", "CURRICULUM"):
            raise ValueError("starter entries must be scenario then curriculum")
        expected_starter_digest = _sha256(
            canonical_json_bytes([item.as_dict() for item in self.starter_entries])
        )
        if expected_starter_digest != self.starter_entries_sha256:
            raise ValueError("starter entries digest differs")
        _sorted_unique(self.assets, lambda item: item.path, "assets")
        _sorted_unique(self.known_limitations, lambda item: item.code, "known limitations")
        by_code = {item.code: item.detail for item in self.known_limitations}
        if any(by_code.get(code) != detail for code, detail in RELEASE_REQUIRED_KNOWN_LIMITATIONS_V1):
            raise ValueError("release manifest omits or changes a required scope limitation")
        require_sha256(self.license_inventory_sha256, "license inventory digest")
        require_sha256(self.notices_sha256, "notices digest")
        require_sha256(self.artifact_layout_sha256, "artifact layout digest")
        normalize_release_path(self.archive_root, label="archive root")
        if "/" in self.archive_root:
            raise ValueError("release archive root must be one path segment")
        _text(self.logical_build_id, "logical build ID", maximum_bytes=128)
        if not self.logical_build_id.startswith("kirby2-release-"):
            raise ValueError("logical build ID has the wrong namespace")
        require_sha256(self.logical_build_id.removeprefix("kirby2-release-"), "logical build suffix")
        _sorted_unique(self.payload_members, lambda item: item.path, "payload members")
        _sorted_unique(
            self.subordinate_artifacts,
            lambda item: item.artifact_id,
            "subordinate artifacts",
        )

    @property
    def payload_projection_sha256(self) -> str:
        return _sha256(canonical_json_bytes([item.as_dict() for item in self.payload_members]))

    def as_dict(self) -> dict[str, object]:
        return {
            "assets": [item.as_dict() for item in self.assets],
            "build_timestamp": self.build_timestamp,
            "candidate_commit": self.candidate_commit,
            "dependencies": [item.as_dict() for item in self.dependencies],
            "known_limitations": [item.as_dict() for item in self.known_limitations],
            "layout": {
                "archive_root": self.archive_root,
                "artifact_layout_sha256": self.artifact_layout_sha256,
            },
            "licenses": {
                "inventory_sha256": self.license_inventory_sha256,
                "notices_sha256": self.notices_sha256,
            },
            "logical_build_id": self.logical_build_id,
            "payload_members": [item.as_dict() for item in self.payload_members],
            "payload_projection_sha256": self.payload_projection_sha256,
            "release_version": self.release_version,
            "runtime": self.runtime.as_dict(),
            "schema_version": self.schema_version,
            "schema_versions": [item.as_dict() for item in self.schema_versions],
            "starter_set": {
                "entries": [item.as_dict() for item in self.starter_entries],
                "entries_sha256": self.starter_entries_sha256,
                "set_id": self.starter_set_id,
            },
            "subordinate_artifacts": [
                item.as_dict() for item in self.subordinate_artifacts
            ],
            "supported_targets": list(self.supported_targets),
            "target": self.target.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseManifestV1":
        fields = {
            "schema_version",
            "release_version",
            "candidate_commit",
            "build_timestamp",
            "target",
            "runtime",
            "dependencies",
            "schema_versions",
            "starter_set",
            "assets",
            "supported_targets",
            "known_limitations",
            "licenses",
            "layout",
            "logical_build_id",
            "payload_members",
            "subordinate_artifacts",
            "payload_projection_sha256",
        }
        row = _exact_object(value, fields, "release manifest")
        if row["schema_version"] != cls.schema_version:
            raise ValueError("release manifest schema version differs")
        targets = _exact_array(row["supported_targets"], "supported targets")
        if targets != list(cls.supported_targets):
            raise ValueError("supported target matrix differs")
        starter = _exact_object(row["starter_set"], {"set_id", "entries_sha256", "entries"}, "starter set")
        licenses = _exact_object(row["licenses"], {"inventory_sha256", "notices_sha256"}, "licenses")
        layout = _exact_object(row["layout"], {"artifact_layout_sha256", "archive_root"}, "layout")
        instance = cls(
            release_version=_text(row["release_version"], "release version"),
            candidate_commit=_text(row["candidate_commit"], "candidate commit"),
            build_timestamp=_text(row["build_timestamp"], "build timestamp"),
            target=ReleaseTargetV1.from_dict(row["target"]),
            runtime=ReleaseRuntimeV1.from_dict(row["runtime"]),
            dependencies=tuple(
                ReleaseDependencyV1.from_dict(item)
                for item in _exact_array(row["dependencies"], "dependencies")
            ),
            schema_versions=tuple(
                ReleaseSchemaVersionV1.from_dict(item)
                for item in _exact_array(row["schema_versions"], "schema versions")
            ),
            starter_set_id=_text(starter["set_id"], "starter set ID"),
            starter_entries_sha256=_text(
                starter["entries_sha256"], "starter entries digest"
            ),
            starter_entries=tuple(
                ReleaseStarterEntryManifestV1.from_dict(item)
                for item in _exact_array(starter["entries"], "starter entries")
            ),
            assets=tuple(
                ReleaseAssetV1.from_dict(item)
                for item in _exact_array(row["assets"], "assets")
            ),
            known_limitations=tuple(
                ReleaseKnownLimitationV1.from_dict(item)
                for item in _exact_array(row["known_limitations"], "known limitations")
            ),
            license_inventory_sha256=_text(
                licenses["inventory_sha256"], "license inventory digest"
            ),
            notices_sha256=_text(licenses["notices_sha256"], "notices digest"),
            artifact_layout_sha256=_text(
                layout["artifact_layout_sha256"], "artifact layout digest"
            ),
            archive_root=_text(layout["archive_root"], "archive root"),
            logical_build_id=_text(row["logical_build_id"], "logical build ID"),
            payload_members=tuple(
                ReleasePayloadMemberV1.from_dict(item)
                for item in _exact_array(row["payload_members"], "payload members")
            ),
            subordinate_artifacts=tuple(
                ReleaseSubordinateArtifactV1.from_dict(item)
                for item in _exact_array(row["subordinate_artifacts"], "subordinate artifacts")
            ),
        )
        if instance.payload_projection_sha256 != row["payload_projection_sha256"]:
            raise ValueError("payload projection digest differs")
        return instance

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseManifestV1":
        value = load_canonical_json_bytes(raw, "ReleaseManifestV1")
        restored = cls.from_dict(value)
        if restored.canonical_bytes() != raw:
            raise ValueError("release manifest bytes are not canonical")
        return restored


@dataclass(frozen=True, slots=True)
class ReleaseProtocolFileV1:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        normalize_release_path(self.path, label="protocol path")
        require_sha256(self.sha256, "protocol digest")

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReleaseLogicalBuildProjectionV1:
    release_version: str
    candidate_commit: str
    source_manifest_sha256: str
    protocol_files: tuple[ReleaseProtocolFileV1, ...]
    starter_set_entries_sha256: str

    schema_version: ClassVar[int] = RELEASE_LOGICAL_BUILD_PROJECTION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if require_semver(self.release_version) != RELEASE_VERSION_V1:
            raise ValueError("logical build release version differs")
        _commit(self.candidate_commit)
        require_sha256(self.source_manifest_sha256, "source manifest digest")
        require_sha256(self.starter_set_entries_sha256, "starter entries digest")
        if tuple(item.path for item in self.protocol_files) != RELEASE_PROTOCOL_PATHS_V1:
            raise ValueError("logical build protocol inventory differs")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_commit": self.candidate_commit,
            "protocol_files": [item.as_dict() for item in self.protocol_files],
            "release_version": self.release_version,
            "schema_version": self.schema_version,
            "source_manifest_sha256": self.source_manifest_sha256,
            "starter_set_entries_sha256": self.starter_set_entries_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def logical_build_id(self) -> str:
        return "kirby2-release-" + _sha256(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class ReleaseArtifactRecordV1:
    artifact_id: str
    artifact_form: str
    target: str
    size: int
    transport_sha256: str
    embedded_manifest_sha256: str | None

    def __post_init__(self) -> None:
        if _ARTIFACT_ID.fullmatch(self.artifact_id) is None:
            raise ValueError("release artifact ID is invalid")
        _text(self.artifact_form, "artifact form", maximum_bytes=128)
        _text(self.target, "artifact target", maximum_bytes=128)
        _nonnegative(self.size, "artifact size")
        require_sha256(self.transport_sha256, "artifact transport digest")
        if self.embedded_manifest_sha256 is not None:
            require_sha256(self.embedded_manifest_sha256, "embedded manifest digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_form": self.artifact_form,
            "artifact_id": self.artifact_id,
            "embedded_manifest_sha256": self.embedded_manifest_sha256,
            "size": self.size,
            "target": self.target,
            "transport_sha256": self.transport_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseArtifactRecordV1":
        fields = {
            "artifact_id",
            "artifact_form",
            "target",
            "size",
            "transport_sha256",
            "embedded_manifest_sha256",
        }
        row = _exact_object(value, fields, "release artifact")
        return cls(
            artifact_id=_text(row["artifact_id"], "artifact ID"),
            artifact_form=_text(row["artifact_form"], "artifact form"),
            target=_text(row["target"], "artifact target"),
            size=row["size"],  # type: ignore[arg-type]
            transport_sha256=_text(row["transport_sha256"], "transport digest"),
            embedded_manifest_sha256=(
                None
                if row["embedded_manifest_sha256"] is None
                else _text(
                    row["embedded_manifest_sha256"], "embedded manifest digest"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseArtifactIndexV1:
    candidate_commit: str
    logical_build_id: str
    artifacts: tuple[ReleaseArtifactRecordV1, ...]

    schema_version: ClassVar[int] = RELEASE_ARTIFACT_INDEX_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _commit(self.candidate_commit)
        if not self.logical_build_id.startswith("kirby2-release-"):
            raise ValueError("artifact index logical build ID is invalid")
        require_sha256(self.logical_build_id.removeprefix("kirby2-release-"), "logical build suffix")
        actual = tuple(
            (
                item.artifact_id,
                item.artifact_form,
                item.target,
                item.embedded_manifest_sha256 is not None,
            )
            for item in self.artifacts
        )
        expected = tuple(RELEASE_ARTIFACT_ROWS_V1)
        if actual != expected:
            raise ValueError("release artifact inventory differs from the six-row protocol")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "candidate_commit": self.candidate_commit,
            "logical_build_id": self.logical_build_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes())

    def select(self, selector: str) -> tuple[ReleaseArtifactRecordV1, ...]:
        artifact_ids = RELEASE_ARTIFACT_SELECTORS_V1.get(selector)
        if artifact_ids is None:
            raise ValueError(f"unknown release artifact selector: {selector!r}")
        by_id = {item.artifact_id: item for item in self.artifacts}
        return tuple(by_id[artifact_id] for artifact_id in artifact_ids)

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseArtifactIndexV1":
        row = _exact_object(
            value,
            {"schema_version", "candidate_commit", "logical_build_id", "artifacts"},
            "release artifact index",
        )
        if row["schema_version"] != cls.schema_version:
            raise ValueError("release artifact index schema version differs")
        return cls(
            candidate_commit=_text(row["candidate_commit"], "candidate commit"),
            logical_build_id=_text(row["logical_build_id"], "logical build ID"),
            artifacts=tuple(
                ReleaseArtifactRecordV1.from_dict(item)
                for item in _exact_array(row["artifacts"], "release artifacts")
            ),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseArtifactIndexV1":
        value = load_canonical_json_bytes(raw, "ReleaseArtifactIndexV1")
        restored = cls.from_dict(value)
        if restored.canonical_bytes() != raw:
            raise ValueError("artifact index bytes are not canonical")
        return restored


__all__ = [
    "RELEASE_ARTIFACT_INDEX_SCHEMA_ID_V1",
    "RELEASE_ARTIFACT_INDEX_SCHEMA_VERSION_V1",
    "RELEASE_ARTIFACT_ROWS_V1",
    "RELEASE_ARTIFACT_SELECTORS_V1",
    "RELEASE_LOGICAL_BUILD_PROJECTION_SCHEMA_ID_V1",
    "RELEASE_LOGICAL_BUILD_PROJECTION_SCHEMA_VERSION_V1",
    "RELEASE_MANIFEST_SCHEMA_ID_V1",
    "RELEASE_MANIFEST_SCHEMA_VERSION_V1",
    "RELEASE_PAYLOAD_SOURCE_CLASSES_V1",
    "RELEASE_PROTOCOL_PATHS_V1",
    "RELEASE_REQUIRED_KNOWN_LIMITATIONS_V1",
    "RELEASE_VERSION_V1",
    "ReleaseArtifactIndexV1",
    "ReleaseArtifactRecordV1",
    "ReleaseAssetV1",
    "ReleaseDependencyV1",
    "ReleaseKnownLimitationV1",
    "ReleaseLogicalBuildProjectionV1",
    "ReleaseManifestV1",
    "ReleasePayloadMemberV1",
    "ReleaseProtocolFileV1",
    "ReleaseRuntimeV1",
    "ReleaseSchemaVersionV1",
    "ReleaseStarterEntryManifestV1",
    "ReleaseSubordinateArtifactV1",
    "ReleaseTargetV1",
]
