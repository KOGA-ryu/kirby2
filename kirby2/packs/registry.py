"""Immutable canonical registry records for installed Kirby2 packs.

This module is deliberately a pure data boundary.  It defines the exact
``registry.json`` representation, content-addressed object names, and read-only
lookups.  Dependency selection, filesystem installation, locking, activation,
removal, staging, and network access belong to later WO39-C boundaries.
"""

from __future__ import annotations

import hashlib
import hmac
import heapq
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar

from .formats import (
    canonical_json_bytes,
    canonical_manifest_bytes,
    compare_semver_precedence,
    load_canonical_json_bytes,
    require_semver,
    require_semver_range,
    require_sha256,
)
from .models import PackManifestV1, PackRegistryKeyV1


PACK_REGISTRY_FILENAME = "registry.json"
PACK_REGISTRY_SCHEMA_ID = "KIRBY2_PACK_REGISTRY_V1"
PACK_REGISTRY_SCHEMA_VERSION = 1
PACK_REGISTRY_SHA256_ALGORITHM = "SHA256_CANONICAL_PACK_REGISTRY_V1"
PACK_OBJECT_STORE_DIRECTORY = "objects"
PACK_OBJECT_DIGEST_ALGORITHM = "sha256"
PACK_REGISTRY_MAX_BYTE_COUNT = 64 * 1024 * 1024
PACK_REGISTRY_MAX_ENTRY_COUNT = 16_384


def pack_object_relative_path(pack_id: object) -> str:
    """Return the sole V1 object-directory name for one logical pack digest."""

    digest = require_sha256(pack_id, "pack object digest")
    return (
        f"{PACK_OBJECT_STORE_DIRECTORY}/{PACK_OBJECT_DIGEST_ALGORITHM}/"
        f"{digest[:2]}/{digest}"
    )


@dataclass(frozen=True, slots=True)
class PackRegistryDependencyEdgeV1:
    """One exact, locally resolved dependency key and logical pack identity."""

    key: PackRegistryKeyV1
    pack_id: str

    def __post_init__(self) -> None:
        if type(self.key) is not PackRegistryKeyV1:
            raise TypeError("registry dependency key must be PackRegistryKeyV1")
        require_sha256(self.pack_id, "registry dependency pack ID")

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return self.key.sort_key

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key.as_dict(),
            "pack_id": self.pack_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> PackRegistryDependencyEdgeV1:
        payload = _exact_object(
            value,
            {"key", "pack_id"},
            "pack registry dependency edge",
        )
        restored = cls(
            key=PackRegistryKeyV1.from_dict(payload["key"]),
            pack_id=_exact_text(payload, "pack_id"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack registry dependency edge did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackRegistryEntryV1:
    """One immutable registry-key to content-addressed pack binding.

    The complete canonical manifest is retained so registry parsing can verify the
    key, logical identity, manifest digest, and dependency edges without trusting a
    second metadata representation.  Payload bytes remain only in the object store.
    """

    key: PackRegistryKeyV1
    pack_id: str
    manifest_sha256: str
    object_path: str
    manifest: PackManifestV1
    resolved_dependencies: tuple[PackRegistryDependencyEdgeV1, ...]
    active: bool

    def __post_init__(self) -> None:
        if type(self.key) is not PackRegistryKeyV1:
            raise TypeError("pack registry entry key must be PackRegistryKeyV1")
        require_sha256(self.pack_id, "registry pack ID")
        require_sha256(self.manifest_sha256, "registry manifest digest")
        if type(self.object_path) is not str:
            raise TypeError("registry object path must be exact text")
        if type(self.manifest) is not PackManifestV1:
            raise TypeError("pack registry entry manifest must be PackManifestV1")
        if type(self.resolved_dependencies) is not tuple or any(
            type(item) is not PackRegistryDependencyEdgeV1
            for item in self.resolved_dependencies
        ):
            raise TypeError(
                "resolved registry dependencies must be an immutable typed tuple"
            )
        if type(self.active) is not bool:
            raise TypeError("pack registry active state must be an exact boolean")

        if self.key != self.manifest.registry_key:
            raise ValueError("registry key differs from its canonical manifest")
        if not hmac.compare_digest(self.pack_id, self.manifest.pack_id):
            raise ValueError("registry pack ID differs from its canonical manifest")
        expected_manifest_sha256 = hashlib.sha256(
            canonical_manifest_bytes(self.manifest)
        ).hexdigest()
        if not hmac.compare_digest(self.manifest_sha256, expected_manifest_sha256):
            raise ValueError("registry manifest digest differs from canonical bytes")
        if self.object_path != pack_object_relative_path(self.pack_id):
            raise ValueError("registry object path is not the canonical pack address")

        ordered = tuple(
            sorted(self.resolved_dependencies, key=lambda item: item.sort_key)
        )
        if ordered != self.resolved_dependencies:
            raise ValueError("resolved registry dependencies must use canonical order")
        edge_keys = tuple(item.key for item in self.resolved_dependencies)
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("resolved registry dependency keys must be unique")
        if any(edge.key == self.key for edge in self.resolved_dependencies):
            raise ValueError("a pack registry entry cannot depend on itself")
        self._validate_dependency_edges()

    def _validate_dependency_edges(self) -> None:
        requirements = {
            dependency.target_key: dependency
            for dependency in self.manifest.dependencies
        }
        if len(self.resolved_dependencies) != len(requirements):
            raise ValueError(
                "resolved registry dependencies differ from manifest requirements"
            )
        seen_targets: set[tuple[str, str, str]] = set()
        for edge in self.resolved_dependencies:
            target = (
                edge.key.creator_id,
                edge.key.namespace,
                edge.key.name,
            )
            requirement = requirements.get(target)
            if requirement is None or target in seen_targets:
                raise ValueError(
                    "resolved registry dependency target is absent or duplicated"
                )
            seen_targets.add(target)
            if not hmac.compare_digest(edge.pack_id, requirement.expected_pack_id):
                raise ValueError(
                    "resolved registry dependency digest differs from its manifest"
                )
            if not _semver_satisfies(
                edge.key.version,
                requirement.version_constraint,
            ):
                raise ValueError(
                    "resolved registry dependency version violates its manifest"
                )

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return self.key.sort_key

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "key": self.key.as_dict(),
            "manifest": self.manifest.as_dict(),
            "manifest_sha256": self.manifest_sha256,
            "object_path": self.object_path,
            "pack_id": self.pack_id,
            "resolved_dependencies": [
                item.as_dict() for item in self.resolved_dependencies
            ],
        }

    @classmethod
    def from_manifest(
        cls,
        manifest: PackManifestV1,
        resolved_dependencies: tuple[PackRegistryDependencyEdgeV1, ...],
        *,
        active: bool,
    ) -> PackRegistryEntryV1:
        """Create the only derived entry shape accepted for a verified manifest."""

        if type(manifest) is not PackManifestV1:
            raise TypeError("registry entry construction requires PackManifestV1")
        return cls(
            key=manifest.registry_key,
            pack_id=manifest.pack_id,
            manifest_sha256=hashlib.sha256(
                canonical_manifest_bytes(manifest)
            ).hexdigest(),
            object_path=pack_object_relative_path(manifest.pack_id),
            manifest=manifest,
            resolved_dependencies=resolved_dependencies,
            active=active,
        )

    @classmethod
    def from_dict(cls, value: object) -> PackRegistryEntryV1:
        payload = _exact_object(
            value,
            {
                "active",
                "key",
                "manifest",
                "manifest_sha256",
                "object_path",
                "pack_id",
                "resolved_dependencies",
            },
            "pack registry entry",
        )
        raw_edges = _exact_array(
            payload["resolved_dependencies"],
            "resolved registry dependencies",
        )
        restored = cls(
            key=PackRegistryKeyV1.from_dict(payload["key"]),
            pack_id=_exact_text(payload, "pack_id"),
            manifest_sha256=_exact_text(payload, "manifest_sha256"),
            object_path=_exact_text(payload, "object_path"),
            manifest=PackManifestV1.from_dict(payload["manifest"]),
            resolved_dependencies=tuple(
                PackRegistryDependencyEdgeV1.from_dict(item) for item in raw_edges
            ),
            active=_exact_bool(payload, "active"),
        )
        if restored.as_dict() != payload:
            raise ValueError("pack registry entry did not round-trip exactly")
        return restored


@dataclass(frozen=True, slots=True)
class PackRegistryV1:
    """One complete immutable ``registry.json`` snapshot."""

    entries: tuple[PackRegistryEntryV1, ...]
    _by_key: Mapping[PackRegistryKeyV1, PackRegistryEntryV1] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _active_entries: tuple[PackRegistryEntryV1, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    schema_id: ClassVar[str] = PACK_REGISTRY_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or any(
            type(item) is not PackRegistryEntryV1 for item in self.entries
        ):
            raise TypeError("pack registry entries must be an immutable typed tuple")
        if len(self.entries) > PACK_REGISTRY_MAX_ENTRY_COUNT:
            raise ValueError("pack registry entry count exceeds the V1 bound")
        ordered = tuple(sorted(self.entries, key=lambda item: item.sort_key))
        if ordered != self.entries:
            raise ValueError("pack registry entries must use canonical full-key order")

        keys = tuple(item.key for item in self.entries)
        if len(keys) != len(set(keys)):
            raise ValueError("pack registry keys must be unique")
        pack_ids = tuple(item.pack_id for item in self.entries)
        if len(pack_ids) != len(set(pack_ids)):
            raise ValueError("one pack identity cannot occupy multiple registry keys")

        by_key = {item.key: item for item in self.entries}
        self._validate_cross_entry_edges(by_key)
        _reject_dependency_cycles(self.entries, by_key)
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))
        object.__setattr__(
            self,
            "_active_entries",
            tuple(item for item in self.entries if item.active),
        )

    @staticmethod
    def _validate_cross_entry_edges(
        by_key: Mapping[PackRegistryKeyV1, PackRegistryEntryV1],
    ) -> None:
        for entry in by_key.values():
            for edge in entry.resolved_dependencies:
                target = by_key.get(edge.key)
                if target is None:
                    if entry.active:
                        raise ValueError(
                            "active registry entry has an unavailable dependency"
                        )
                    continue
                if not hmac.compare_digest(target.pack_id, edge.pack_id):
                    raise ValueError(
                        "registry dependency edge differs from target pack identity"
                    )
                if entry.active and not target.active:
                    raise ValueError(
                        "active registry entry has an inactive dependency"
                    )

    @classmethod
    def empty(cls) -> PackRegistryV1:
        return cls(entries=())

    @property
    def by_key(self) -> Mapping[PackRegistryKeyV1, PackRegistryEntryV1]:
        """Return a read-only exact-key view of the snapshot."""

        return self._by_key

    @property
    def keys(self) -> tuple[PackRegistryKeyV1, ...]:
        return tuple(item.key for item in self.entries)

    @property
    def active_entries(self) -> tuple[PackRegistryEntryV1, ...]:
        return self._active_entries

    def get(self, key: PackRegistryKeyV1) -> PackRegistryEntryV1 | None:
        if type(key) is not PackRegistryKeyV1:
            raise TypeError("pack registry lookup requires PackRegistryKeyV1")
        return self._by_key.get(key)

    def require(self, key: PackRegistryKeyV1) -> PackRegistryEntryV1:
        entry = self.get(key)
        if entry is None:
            raise KeyError(
                "UNKNOWN_PACK_REGISTRY_KEY: "
                f"{key.creator_id}/{key.namespace}/{key.name}@{key.version}"
            )
        return entry

    def lookup(
        self,
        *,
        creator_id: str,
        namespace: str,
        name: str,
        version: str,
    ) -> PackRegistryEntryV1 | None:
        return self.get(
            PackRegistryKeyV1(
                creator_id=creator_id,
                namespace=namespace,
                name=name,
                version=version,
            )
        )

    def dependents_of(
        self,
        key: PackRegistryKeyV1,
        *,
        active_only: bool = False,
    ) -> tuple[PackRegistryEntryV1, ...]:
        if type(key) is not PackRegistryKeyV1:
            raise TypeError("dependent lookup requires PackRegistryKeyV1")
        if type(active_only) is not bool:
            raise TypeError("active-only lookup state must be an exact boolean")
        return tuple(
            entry
            for entry in self.entries
            if (not active_only or entry.active)
            and any(edge.key == key for edge in entry.resolved_dependencies)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [item.as_dict() for item in self.entries],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_pack_registry_bytes(self)

    @property
    def sha256(self) -> str:
        return pack_registry_sha256(self)

    @classmethod
    def from_dict(cls, value: object) -> PackRegistryV1:
        payload = _exact_object(
            value,
            {"entries", "schema_id", "schema_version"},
            "pack registry",
        )
        if (
            type(payload["schema_id"]) is not str
            or payload["schema_id"] != cls.schema_id
        ):
            raise ValueError("pack registry schema ID is unsupported")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != cls.schema_version
        ):
            raise ValueError("pack registry schema version is unsupported")
        raw_entries = _exact_array(payload["entries"], "pack registry entries")
        if len(raw_entries) > PACK_REGISTRY_MAX_ENTRY_COUNT:
            raise ValueError("pack registry entry count exceeds the V1 bound")
        restored = cls(
            entries=tuple(PackRegistryEntryV1.from_dict(item) for item in raw_entries)
        )
        if restored.as_dict() != payload:
            raise ValueError("pack registry did not round-trip exactly")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> PackRegistryV1:
        return load_pack_registry_bytes(raw)


def canonical_pack_registry_bytes(registry: object) -> bytes:
    if type(registry) is not PackRegistryV1:
        raise TypeError("canonical pack registry requires PackRegistryV1")
    raw = canonical_json_bytes(registry.as_dict())
    if len(raw) > PACK_REGISTRY_MAX_BYTE_COUNT:
        raise ValueError("canonical pack registry exceeds the V1 byte bound")
    return raw


def load_pack_registry_bytes(raw: bytes) -> PackRegistryV1:
    if type(raw) is not bytes or not raw:
        raise ValueError("pack registry must contain nonempty exact bytes")
    if len(raw) > PACK_REGISTRY_MAX_BYTE_COUNT:
        raise ValueError("pack registry exceeds the V1 byte bound")
    try:
        value = load_canonical_json_bytes(raw, "pack registry")
        restored = PackRegistryV1.from_dict(value)
        if canonical_pack_registry_bytes(restored) != raw:
            raise ValueError("pack registry did not survive canonical reconstruction")
        return restored
    except (OverflowError, RecursionError) as error:
        raise ValueError("pack registry exceeds canonical parse complexity") from error


def pack_registry_sha256(registry: object) -> str:
    if type(registry) is not PackRegistryV1:
        raise TypeError("pack registry digest requires PackRegistryV1")
    return hashlib.sha256(canonical_pack_registry_bytes(registry)).hexdigest()


def lookup_pack_registry_entry(
    registry: PackRegistryV1,
    key: PackRegistryKeyV1,
) -> PackRegistryEntryV1 | None:
    if type(registry) is not PackRegistryV1:
        raise TypeError("registry lookup requires PackRegistryV1")
    return registry.get(key)


def require_pack_registry_entry(
    registry: PackRegistryV1,
    key: PackRegistryKeyV1,
) -> PackRegistryEntryV1:
    if type(registry) is not PackRegistryV1:
        raise TypeError("registry lookup requires PackRegistryV1")
    return registry.require(key)


def _semver_satisfies(version: str, constraint: str) -> bool:
    candidate = require_semver(version, "resolved registry dependency version")
    requirement = require_semver_range(
        constraint,
        "registry dependency version constraint",
    )
    if requirement == "*":
        return True
    terms = requirement.split(",")
    if len(terms) == 1 and not terms[0].startswith((">", "<")):
        return candidate == terms[0]
    for term in terms:
        if term.startswith(">="):
            if compare_semver_precedence(candidate, term[2:]) < 0:
                return False
        elif term.startswith(">"):
            if compare_semver_precedence(candidate, term[1:]) <= 0:
                return False
        elif term.startswith("<="):
            if compare_semver_precedence(candidate, term[2:]) > 0:
                return False
        elif term.startswith("<"):
            if compare_semver_precedence(candidate, term[1:]) >= 0:
                return False
        else:  # pragma: no cover - require_semver_range closes this branch
            return False
    return True


def _reject_dependency_cycles(
    entries: tuple[PackRegistryEntryV1, ...],
    by_key: Mapping[PackRegistryKeyV1, PackRegistryEntryV1],
) -> None:
    """Reject cycles among installed nodes while permitting inactive missing edges."""

    incoming = {entry.key: 0 for entry in entries}
    outgoing: dict[PackRegistryKeyV1, list[PackRegistryKeyV1]] = {
        entry.key: [] for entry in entries
    }
    for entry in entries:
        for edge in entry.resolved_dependencies:
            if edge.key not in by_key:
                continue
            outgoing[entry.key].append(edge.key)
            incoming[edge.key] += 1
    ready = [
        (key.sort_key, key)
        for key, count in incoming.items()
        if count == 0
    ]
    heapq.heapify(ready)
    visited = 0
    while ready:
        _, key = heapq.heappop(ready)
        visited += 1
        for target in outgoing[key]:
            incoming[target] -= 1
            if incoming[target] == 0:
                heapq.heappush(ready, (target.sort_key, target))
    if visited != len(entries):
        raise ValueError("pack registry dependency graph contains a cycle")


def _exact_object(
    value: object,
    expected: set[str] | frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} field names must be exact text")
    actual = set(value)
    missing = sorted(set(expected) - actual)
    unknown = sorted(actual - set(expected))
    if missing or unknown:
        raise ValueError(
            f"serialized {label} fields are not exact: "
            f"missing={missing} unknown={unknown}"
        )
    return value


def _exact_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"serialized {label} must be an exact array")
    return value


def _exact_text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _exact_bool(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise TypeError(f"serialized {key} must be an exact boolean")
    return value


__all__ = [
    "PACK_OBJECT_DIGEST_ALGORITHM",
    "PACK_OBJECT_STORE_DIRECTORY",
    "PACK_REGISTRY_FILENAME",
    "PACK_REGISTRY_MAX_BYTE_COUNT",
    "PACK_REGISTRY_MAX_ENTRY_COUNT",
    "PACK_REGISTRY_SCHEMA_ID",
    "PACK_REGISTRY_SCHEMA_VERSION",
    "PACK_REGISTRY_SHA256_ALGORITHM",
    "PackRegistryDependencyEdgeV1",
    "PackRegistryEntryV1",
    "PackRegistryV1",
    "canonical_pack_registry_bytes",
    "load_pack_registry_bytes",
    "lookup_pack_registry_entry",
    "pack_object_relative_path",
    "pack_registry_sha256",
    "require_pack_registry_entry",
]
