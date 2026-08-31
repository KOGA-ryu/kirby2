"""Typed release identity and schema-compatibility inventory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from kirby2 import __version__
from kirby2.research.paths import DATA_PATHS_SCHEMA_VERSION

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_data_identifier,
    require_nfc_text,
    require_semver,
    require_sha256,
)


RELEASE_SCHEMA_INVENTORY_SCHEMA_ID_V1 = "KIRBY2_RELEASE_SCHEMA_INVENTORY_V1"
RELEASE_SCHEMA_INVENTORY_SCHEMA_VERSION_V1 = 1


class ReleaseSchemaKindV1(str, Enum):
    ENGINE = "ENGINE"
    SOURCE = "SOURCE"
    RUN = "RUN"
    CHECKPOINT = "CHECKPOINT"
    PACK = "PACK"
    COMPILER = "COMPILER"
    LEARNER = "LEARNER"
    SCORING = "SCORING"
    STUDY = "STUDY"
    REPORT = "REPORT"


class ReleaseSchemaUseV1(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    REPLAY_EQUIVALENT = "REPLAY_EQUIVALENT"


@dataclass(frozen=True, slots=True)
class ReleaseVersionRangeV1:
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if (
            type(self.minimum) is not int
            or type(self.maximum) is not int
            or self.minimum <= 0
            or self.maximum < self.minimum
        ):
            raise ValueError("release schema range must be one positive closed interval")

    def contains(self, version: object) -> bool:
        return type(version) is int and self.minimum <= version <= self.maximum

    def as_dict(self) -> dict[str, int]:
        return {"maximum": self.maximum, "minimum": self.minimum}

    @classmethod
    def from_dict(cls, value: object) -> ReleaseVersionRangeV1:
        if type(value) is not dict or set(value) != {"maximum", "minimum"}:
            raise ValueError("release schema range fields differ")
        minimum = value["minimum"]
        maximum = value["maximum"]
        if type(minimum) is not int or type(maximum) is not int:
            raise TypeError("release schema range bounds must be integers")
        return cls(minimum=minimum, maximum=maximum)


@dataclass(frozen=True, slots=True)
class ReleaseSchemaCompatibilityV1:
    kind: ReleaseSchemaKindV1
    schema_id: str
    current_version: int
    readable: ReleaseVersionRangeV1
    writable: ReleaseVersionRangeV1 | None
    replay_equivalent: ReleaseVersionRangeV1 | None

    def __post_init__(self) -> None:
        if type(self.kind) is not ReleaseSchemaKindV1:
            raise TypeError("release schema kind is invalid")
        require_data_identifier(self.schema_id, "release schema ID")
        if type(self.current_version) is not int or self.current_version <= 0:
            raise ValueError("release current schema version must be positive")
        if type(self.readable) is not ReleaseVersionRangeV1:
            raise TypeError("release readable schema range is invalid")
        for value, label in (
            (self.writable, "writable"),
            (self.replay_equivalent, "replay-equivalent"),
        ):
            if value is not None and type(value) is not ReleaseVersionRangeV1:
                raise TypeError(f"release {label} schema range is invalid")
        if not self.readable.contains(self.current_version):
            raise ValueError("current schema version must be readable")
        if self.writable is not None and not self.writable.contains(
            self.current_version
        ):
            raise ValueError("current schema version must be writable when writing exists")

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.kind.value, self.schema_id)

    def range_for(self, use: ReleaseSchemaUseV1) -> ReleaseVersionRangeV1 | None:
        if type(use) is not ReleaseSchemaUseV1:
            raise TypeError("release schema use is invalid")
        return {
            ReleaseSchemaUseV1.READ: self.readable,
            ReleaseSchemaUseV1.WRITE: self.writable,
            ReleaseSchemaUseV1.REPLAY_EQUIVALENT: self.replay_equivalent,
        }[use]

    def supports(self, use: ReleaseSchemaUseV1, version: object) -> bool:
        supported = self.range_for(use)
        return supported is not None and supported.contains(version)

    def as_dict(self) -> dict[str, object]:
        return {
            "current_version": self.current_version,
            "kind": self.kind.value,
            "readable": self.readable.as_dict(),
            "replay_equivalent": (
                None
                if self.replay_equivalent is None
                else self.replay_equivalent.as_dict()
            ),
            "schema_id": self.schema_id,
            "writable": None if self.writable is None else self.writable.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> ReleaseSchemaCompatibilityV1:
        expected = {
            "current_version",
            "kind",
            "readable",
            "replay_equivalent",
            "schema_id",
            "writable",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("release schema compatibility fields differ")
        current = value["current_version"]
        kind = value["kind"]
        schema_id = value["schema_id"]
        if type(current) is not int or type(kind) is not str or type(schema_id) is not str:
            raise TypeError("release schema compatibility scalar fields differ")
        return cls(
            kind=ReleaseSchemaKindV1(kind),
            schema_id=schema_id,
            current_version=current,
            readable=ReleaseVersionRangeV1.from_dict(value["readable"]),
            writable=(
                None
                if value["writable"] is None
                else ReleaseVersionRangeV1.from_dict(value["writable"])
            ),
            replay_equivalent=(
                None
                if value["replay_equivalent"] is None
                else ReleaseVersionRangeV1.from_dict(value["replay_equivalent"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ReleaseSchemaInventoryV1:
    engine_version: str
    source_revision: str
    source_sha256: str
    data_paths_schema_version: int
    schemas: tuple[ReleaseSchemaCompatibilityV1, ...]

    schema_id: ClassVar[str] = RELEASE_SCHEMA_INVENTORY_SCHEMA_ID_V1
    schema_version: ClassVar[int] = RELEASE_SCHEMA_INVENTORY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        require_semver(self.engine_version, "release engine version")
        require_nfc_text(
            self.source_revision,
            "release source revision",
            maximum_bytes=256,
        )
        if not self.source_revision:
            raise ValueError("release source revision must not be empty")
        require_sha256(self.source_sha256, "release source digest")
        if (
            type(self.data_paths_schema_version) is not int
            or self.data_paths_schema_version <= 0
        ):
            raise ValueError("release data-paths schema version must be positive")
        if type(self.schemas) is not tuple or any(
            type(item) is not ReleaseSchemaCompatibilityV1 for item in self.schemas
        ):
            raise TypeError("release schema inventory must be a typed tuple")
        if self.schemas != tuple(sorted(self.schemas, key=lambda item: item.sort_key)):
            raise ValueError("release schema inventory must use canonical order")
        if len({item.schema_id for item in self.schemas}) != len(self.schemas):
            raise ValueError("release schema inventory IDs must be unique")
        if {item.kind for item in self.schemas} != set(ReleaseSchemaKindV1):
            raise ValueError("release schema inventory must cover every required kind")

    def schema(self, kind: ReleaseSchemaKindV1) -> ReleaseSchemaCompatibilityV1:
        if type(kind) is not ReleaseSchemaKindV1:
            raise TypeError("release schema lookup kind is invalid")
        selected = tuple(item for item in self.schemas if item.kind is kind)
        if len(selected) != 1:
            raise ValueError(f"release schema inventory has ambiguous {kind.value} entries")
        return selected[0]

    def require_supported(
        self,
        kind: ReleaseSchemaKindV1,
        use: ReleaseSchemaUseV1,
        version: int,
    ) -> None:
        schema = self.schema(kind)
        if not schema.supports(use, version):
            supported = schema.range_for(use)
            detail = (
                "unsupported"
                if supported is None
                else f"{supported.minimum}..{supported.maximum}"
            )
            direction = (
                "future schema or unsafe downgrade"
                if version > schema.current_version
                else "unsupported legacy schema"
            )
            raise ValueError(
                f"{direction}: {schema.schema_id} version {version}; "
                f"{use.value} range is {detail}. Restore the pre-migration backup "
                "or use a compatible Kirby2 release."
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "data_paths_schema_version": self.data_paths_schema_version,
            "engine_version": self.engine_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "schemas": [item.as_dict() for item in self.schemas],
            "source_revision": self.source_revision,
            "source_sha256": self.source_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ReleaseSchemaInventoryV1:
        value = load_canonical_json_bytes(raw, "release schema inventory")
        expected = {
            "data_paths_schema_version",
            "engine_version",
            "schema_id",
            "schema_version",
            "schemas",
            "source_revision",
            "source_sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("release schema inventory fields differ")
        if value["schema_id"] != cls.schema_id or value["schema_version"] != 1:
            raise ValueError("release schema inventory contract differs")
        raw_schemas = value["schemas"]
        if type(raw_schemas) is not list:
            raise TypeError("release schemas must be an array")
        scalar_fields = (
            value["engine_version"],
            value["source_revision"],
            value["source_sha256"],
        )
        if any(type(item) is not str for item in scalar_fields):
            raise TypeError("release schema inventory identity fields must be text")
        paths_version = value["data_paths_schema_version"]
        if type(paths_version) is not int:
            raise TypeError("release data-paths schema version must be an integer")
        restored = cls(
            engine_version=scalar_fields[0],
            source_revision=scalar_fields[1],
            source_sha256=scalar_fields[2],
            data_paths_schema_version=paths_version,
            schemas=tuple(
                sorted(
                    (ReleaseSchemaCompatibilityV1.from_dict(item) for item in raw_schemas),
                    key=lambda item: item.sort_key,
                )
            ),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("release schema inventory changed during restoration")
        return restored


def builtin_release_schema_inventory(
    *,
    source_revision: str,
    source_sha256: str,
) -> ReleaseSchemaInventoryV1:
    """Return the closed compatibility inventory shipped by this release."""

    rows = (
        _schema(ReleaseSchemaKindV1.ENGINE, "KIRBY2_ENGINE_STATE_V1", 1),
        _schema(ReleaseSchemaKindV1.SOURCE, "KIRBY2_SOURCE_CONFIGURATION_V1", 1),
        ReleaseSchemaCompatibilityV1(
            kind=ReleaseSchemaKindV1.RUN,
            schema_id="KIRBY2_RUN_MANIFEST",
            current_version=2,
            readable=ReleaseVersionRangeV1(1, 2),
            writable=ReleaseVersionRangeV1(2, 2),
            replay_equivalent=ReleaseVersionRangeV1(1, 2),
        ),
        _schema(ReleaseSchemaKindV1.CHECKPOINT, "KIRBY2_RUNTIME_CHECKPOINT", 1),
        _schema(ReleaseSchemaKindV1.PACK, "KIRBY2_PACK_MANIFEST", 1),
        _schema(ReleaseSchemaKindV1.COMPILER, "KIRBY2_SCENARIO_COMPILER", 1),
        _schema(ReleaseSchemaKindV1.LEARNER, "KIRBY2_LEARNER_EVIDENCE", 1),
        _schema(ReleaseSchemaKindV1.SCORING, "KIRBY2_RUBRIC_SCORE_SIDECAR", 1),
        _schema(ReleaseSchemaKindV1.STUDY, "KIRBY2_STUDY_MANIFEST", 1),
        ReleaseSchemaCompatibilityV1(
            kind=ReleaseSchemaKindV1.REPORT,
            schema_id="KIRBY2_PORTABLE_REPLAY_REPORT",
            current_version=1,
            readable=ReleaseVersionRangeV1(1, 1),
            writable=ReleaseVersionRangeV1(1, 1),
            replay_equivalent=None,
        ),
    )
    return ReleaseSchemaInventoryV1(
        engine_version=__version__,
        source_revision=source_revision,
        source_sha256=source_sha256,
        data_paths_schema_version=DATA_PATHS_SCHEMA_VERSION,
        schemas=tuple(sorted(rows, key=lambda item: item.sort_key)),
    )


def _schema(
    kind: ReleaseSchemaKindV1,
    schema_id: str,
    version: int,
) -> ReleaseSchemaCompatibilityV1:
    exact = ReleaseVersionRangeV1(version, version)
    return ReleaseSchemaCompatibilityV1(
        kind=kind,
        schema_id=schema_id,
        current_version=version,
        readable=exact,
        writable=exact,
        replay_equivalent=exact,
    )


__all__ = [
    "RELEASE_SCHEMA_INVENTORY_SCHEMA_ID_V1",
    "RELEASE_SCHEMA_INVENTORY_SCHEMA_VERSION_V1",
    "ReleaseSchemaCompatibilityV1",
    "ReleaseSchemaInventoryV1",
    "ReleaseSchemaKindV1",
    "ReleaseSchemaUseV1",
    "ReleaseVersionRangeV1",
    "builtin_release_schema_inventory",
]
