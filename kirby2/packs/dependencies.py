"""Pure local compatibility and dependency resolution for Kirby2 packs.

This module makes no filesystem or network decisions.  It evaluates one canonical
manifest against an explicit runtime environment and resolves dependencies only from
the active entries already present in an immutable :class:`PackRegistryV1` snapshot.
There is deliberately no fallback provider, remote lookup, or ``latest`` selection.
"""

from __future__ import annotations

import heapq
import hmac
from dataclasses import dataclass
from typing import cast

from .formats import (
    compare_semver_precedence,
    require_data_identifier,
    require_semver,
    require_semver_range,
)
from .models import (
    PackCompatibilityLevelV1,
    PackCompatibilityV1,
    PackDependencyV1,
    PackManifestV1,
    PackRegistryKeyV1,
    PackVersionRequirementV1,
)
from .registry import (
    PackRegistryDependencyEdgeV1,
    PackRegistryEntryV1,
    PackRegistryV1,
)


@dataclass(frozen=True, slots=True)
class PackRuntimeEnvironmentV1:
    """Exact local versions against which installability is decided.

    Compiler and schema tuples are ordered by their canonical identifier and contain
    one exact local version per identifier.  They describe already available local
    capabilities; they are not dependency requests and cannot trigger discovery.
    """

    engine_component_id: str
    engine_version: str
    compiler_versions: tuple[tuple[str, str], ...]
    schema_versions: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        require_data_identifier(
            self.engine_component_id,
            "pack runtime engine component ID",
        )
        require_semver(self.engine_version, "pack runtime engine version")
        _validate_runtime_compilers(self.compiler_versions)
        _validate_runtime_schemas(self.schema_versions)

    def compiler_version(self, component_id: str) -> str | None:
        canonical_id = require_data_identifier(
            component_id,
            "pack runtime compiler component ID",
        )
        return next(
            (
                version
                for candidate_id, version in self.compiler_versions
                if candidate_id == canonical_id
            ),
            None,
        )

    def schema_version(self, schema_id: str) -> int | None:
        canonical_id = require_data_identifier(
            schema_id,
            "pack runtime schema ID",
        )
        return next(
            (
                version
                for candidate_id, version in self.schema_versions
                if candidate_id == canonical_id
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "compilers": [
                {"component_id": component_id, "version": version}
                for component_id, version in self.compiler_versions
            ],
            "engine": {
                "component_id": self.engine_component_id,
                "version": self.engine_version,
            },
            "schemas": [
                {"schema_id": schema_id, "version": version}
                for schema_id, version in self.schema_versions
            ],
        }


@dataclass(frozen=True, slots=True)
class ResolvedPackDependencyV1:
    """One exact active local registry entry selected as a dependency."""

    entry: PackRegistryEntryV1

    def __post_init__(self) -> None:
        if type(self.entry) is not PackRegistryEntryV1:
            raise TypeError("resolved pack dependency must contain PackRegistryEntryV1")
        if not self.entry.active:
            raise ValueError(
                "INACTIVE_PACK_DEPENDENCY: resolved dependency is not active"
            )

    @property
    def key(self) -> PackRegistryKeyV1:
        return self.entry.key

    @property
    def pack_id(self) -> str:
        return self.entry.pack_id

    @property
    def object_path(self) -> str:
        return self.entry.object_path

    @property
    def manifest(self) -> PackManifestV1:
        return self.entry.manifest

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return self.entry.sort_key

    def as_registry_edge(self) -> PackRegistryDependencyEdgeV1:
        return PackRegistryDependencyEdgeV1(key=self.key, pack_id=self.pack_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key.as_dict(),
            "object_path": self.object_path,
            "pack_id": self.pack_id,
        }


@dataclass(frozen=True, slots=True)
class PackDependencyResolutionV1:
    """Canonical direct bindings and dependency-first transitive activation order."""

    manifest: PackManifestV1
    direct_dependencies: tuple[ResolvedPackDependencyV1, ...]
    dependency_first_order: tuple[ResolvedPackDependencyV1, ...]

    def __post_init__(self) -> None:
        if type(self.manifest) is not PackManifestV1:
            raise TypeError("pack dependency resolution manifest must be PackManifestV1")
        _require_resolved_tuple(self.direct_dependencies, "direct dependencies")
        _require_resolved_tuple(
            self.dependency_first_order,
            "dependency-first order",
        )

        ordered_direct = tuple(
            sorted(self.direct_dependencies, key=lambda item: item.sort_key)
        )
        if ordered_direct != self.direct_dependencies:
            raise ValueError(
                "resolved direct dependencies must use canonical full-key order"
            )

        ordered_keys = tuple(item.key for item in self.dependency_first_order)
        if len(ordered_keys) != len(set(ordered_keys)):
            raise ValueError("dependency-first order contains duplicate registry keys")
        if self.root_key in set(ordered_keys):
            raise ValueError("PACK_DEPENDENCY_CYCLE: root pack depends on its own key")

        by_key = {item.key: item for item in self.dependency_first_order}
        if any(item.key not in by_key for item in self.direct_dependencies):
            raise ValueError(
                "resolved direct dependencies are absent from dependency-first order"
            )
        if any(
            by_key[item.key].entry != item.entry
            for item in self.direct_dependencies
        ):
            raise ValueError(
                "resolved direct dependencies differ from dependency-first entries"
            )
        _validate_direct_bindings(self.manifest, self.direct_dependencies)

        dependencies_by_key: dict[
            PackRegistryKeyV1,
            tuple[PackRegistryKeyV1, ...],
        ] = {
            self.root_key: tuple(item.key for item in self.direct_dependencies)
        }
        for resolved in self.dependency_first_order:
            dependency_keys: list[PackRegistryKeyV1] = []
            for edge in resolved.entry.resolved_dependencies:
                target = by_key.get(edge.key)
                if target is None:
                    raise ValueError(
                        "resolved transitive dependency is absent from resolution"
                    )
                if not hmac.compare_digest(target.pack_id, edge.pack_id):
                    raise ValueError(
                        "resolved transitive dependency digest differs from registry edge"
                    )
                dependency_keys.append(edge.key)
            dependencies_by_key[resolved.key] = tuple(dependency_keys)

        canonical_order = _canonical_dependency_first_keys(
            self.root_key,
            dependencies_by_key,
        )
        expected_order = tuple(key for key in canonical_order if key != self.root_key)
        if ordered_keys != expected_order:
            raise ValueError(
                "resolved dependencies are not in canonical dependency-first order"
            )

    @property
    def root_key(self) -> PackRegistryKeyV1:
        return self.manifest.registry_key

    @property
    def root_pack_id(self) -> str:
        return self.manifest.pack_id

    @property
    def registry_edges(self) -> tuple[PackRegistryDependencyEdgeV1, ...]:
        """Return the exact direct edges used by ``PackRegistryEntryV1``."""

        return tuple(item.as_registry_edge() for item in self.direct_dependencies)

    def as_dict(self) -> dict[str, object]:
        return {
            "dependency_first_order": [
                item.as_dict() for item in self.dependency_first_order
            ],
            "direct_dependencies": [
                item.as_dict() for item in self.direct_dependencies
            ],
            "root_key": self.root_key.as_dict(),
            "root_pack_id": self.root_pack_id,
        }


def semver_satisfies(version: str, constraint: str) -> bool:
    """Evaluate Kirby2's closed canonical SemVer range grammar."""

    candidate = require_semver(version, "candidate semantic version")
    requirement = require_semver_range(
        constraint,
        "semantic-version constraint",
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
        else:  # require_semver_range closes this branch
            return False
    return True


def validate_installability(
    manifest: PackManifestV1,
    environment: PackRuntimeEnvironmentV1,
) -> PackCompatibilityV1:
    """Require the manifest's INSTALLABLE row to match one exact local runtime."""

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack installability requires PackManifestV1")
    if type(environment) is not PackRuntimeEnvironmentV1:
        raise TypeError("pack installability requires PackRuntimeEnvironmentV1")

    installable = next(
        item
        for item in manifest.compatibility
        if item.level is PackCompatibilityLevelV1.INSTALLABLE
    )
    if not installable.supported:
        raise ValueError(
            "PACK_NOT_INSTALLABLE: manifest does not claim INSTALLABLE compatibility"
        )

    engine = cast(PackVersionRequirementV1, installable.engine)
    if engine.component_id != environment.engine_component_id:
        raise ValueError(
            "INCOMPATIBLE_PACK_ENGINE: required engine component is unavailable"
        )
    if not semver_satisfies(environment.engine_version, engine.version_constraint):
        raise ValueError(
            "INCOMPATIBLE_PACK_ENGINE: local engine version violates requirement"
        )

    for requirement in installable.compilers:
        available = environment.compiler_version(requirement.component_id)
        if available is None:
            raise ValueError(
                "MISSING_PACK_COMPILER: required local compiler is unavailable"
            )
        if not semver_satisfies(available, requirement.version_constraint):
            raise ValueError(
                "INCOMPATIBLE_PACK_COMPILER: local compiler version violates requirement"
            )

    for requirement in installable.schemas:
        available = environment.schema_version(requirement.schema_id)
        if available is None:
            raise ValueError(
                "MISSING_PACK_SCHEMA: required local schema is unavailable"
            )
        if available not in requirement.supported_versions:
            raise ValueError(
                "INCOMPATIBLE_PACK_SCHEMA: local schema version is not supported"
            )
    return installable


def resolve_pack_dependencies(
    manifest: PackManifestV1,
    registry: PackRegistryV1,
    environment: PackRuntimeEnvironmentV1,
) -> PackDependencyResolutionV1:
    """Resolve one complete active local dependency closure without discovery.

    Every requirement is creator-qualified, range-checked, and digest-bound.  Zero
    and multiple matches fail instead of choosing a fallback version.  The returned
    transitive order uses dependency edges first and full registry keys as the sole
    deterministic tie-breaker.
    """

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack dependency resolution requires PackManifestV1")
    if type(registry) is not PackRegistryV1:
        raise TypeError("pack dependency resolution requires PackRegistryV1")
    if type(environment) is not PackRuntimeEnvironmentV1:
        raise TypeError("pack dependency resolution requires PackRuntimeEnvironmentV1")

    validate_installability(manifest, environment)
    active_candidates = _index_active_candidates(registry.active_entries)
    direct_entries = tuple(
        _select_active_dependency(requirement, active_candidates)
        for requirement in manifest.dependencies
    )
    direct = tuple(
        sorted(
            (ResolvedPackDependencyV1(entry=item) for item in direct_entries),
            key=lambda item: item.sort_key,
        )
    )

    root_key = manifest.registry_key
    selected: dict[PackRegistryKeyV1, PackRegistryEntryV1] = {}
    dependencies_by_key: dict[
        PackRegistryKeyV1,
        tuple[PackRegistryKeyV1, ...],
    ] = {root_key: tuple(item.key for item in direct)}
    pending = list(reversed(tuple(item.entry for item in direct)))
    while pending:
        entry = pending.pop()
        if entry.key == root_key:
            raise ValueError("PACK_DEPENDENCY_CYCLE: root pack depends on its own key")
        previous = selected.get(entry.key)
        if previous is not None:
            if previous != entry:
                raise ValueError(
                    "PACK_DEPENDENCY_CONFLICT: one key resolved to differing entries"
                )
            continue

        validate_installability(entry.manifest, environment)
        selected[entry.key] = entry
        child_entries = tuple(
            _select_active_dependency(requirement, active_candidates)
            for requirement in entry.manifest.dependencies
        )
        child_edges = tuple(
            PackRegistryDependencyEdgeV1(key=item.key, pack_id=item.pack_id)
            for item in child_entries
        )
        if child_edges != entry.resolved_dependencies:
            raise ValueError(
                "PACK_DEPENDENCY_CONFLICT: local selection differs from registry edge"
            )
        dependencies_by_key[entry.key] = tuple(item.key for item in child_entries)
        for child in reversed(child_entries):
            pending.append(child)

    canonical_keys = _canonical_dependency_first_keys(root_key, dependencies_by_key)
    dependency_keys = tuple(key for key in canonical_keys if key != root_key)
    dependency_first = tuple(
        ResolvedPackDependencyV1(entry=selected[key]) for key in dependency_keys
    )
    return PackDependencyResolutionV1(
        manifest=manifest,
        direct_dependencies=direct,
        dependency_first_order=dependency_first,
    )


def _select_active_dependency(
    requirement: PackDependencyV1,
    active_candidates: dict[
        tuple[str, str, str],
        tuple[PackRegistryEntryV1, ...],
    ],
) -> PackRegistryEntryV1:
    if type(requirement) is not PackDependencyV1:
        raise TypeError("dependency selection requires PackDependencyV1")
    qualified = active_candidates.get(requirement.target_key, ())
    version_matches = tuple(
        item
        for item in qualified
        if semver_satisfies(item.key.version, requirement.version_constraint)
    )
    matches = tuple(
        item
        for item in version_matches
        if hmac.compare_digest(item.pack_id, requirement.expected_pack_id)
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            "AMBIGUOUS_PACK_DEPENDENCY: multiple active local entries match exactly"
        )
    if not qualified:
        raise ValueError(
            "MISSING_PACK_DEPENDENCY: no active local creator-qualified entry"
        )
    if not version_matches:
        raise ValueError(
            "PACK_DEPENDENCY_VERSION_CONFLICT: no active local version satisfies range"
        )
    raise ValueError(
        "PACK_DEPENDENCY_DIGEST_CONFLICT: active compatible version has wrong digest"
    )


def _index_active_candidates(
    entries: tuple[PackRegistryEntryV1, ...],
) -> dict[tuple[str, str, str], tuple[PackRegistryEntryV1, ...]]:
    if type(entries) is not tuple or any(
        type(item) is not PackRegistryEntryV1 or not item.active for item in entries
    ):
        raise TypeError("dependency candidates must be active registry entries")
    grouped: dict[tuple[str, str, str], list[PackRegistryEntryV1]] = {}
    for entry in entries:
        target = (entry.key.creator_id, entry.key.namespace, entry.key.name)
        grouped.setdefault(target, []).append(entry)
    return {target: tuple(candidates) for target, candidates in grouped.items()}


def _canonical_dependency_first_keys(
    root_key: PackRegistryKeyV1,
    dependencies_by_key: dict[
        PackRegistryKeyV1,
        tuple[PackRegistryKeyV1, ...],
    ],
) -> tuple[PackRegistryKeyV1, ...]:
    if type(root_key) is not PackRegistryKeyV1:
        raise TypeError("dependency ordering root must be PackRegistryKeyV1")
    nodes = set(dependencies_by_key)
    if any(
        type(key) is not PackRegistryKeyV1
        or type(dependencies) is not tuple
        or any(type(item) is not PackRegistryKeyV1 for item in dependencies)
        for key, dependencies in dependencies_by_key.items()
    ):
        raise TypeError("dependency graph must contain exact registry keys")
    referenced = {
        dependency
        for dependencies in dependencies_by_key.values()
        for dependency in dependencies
    }
    if not referenced.issubset(nodes):
        raise ValueError("dependency graph references a node outside its closure")

    incoming = {
        key: len(set(dependencies_by_key[key]))
        for key in nodes
    }
    if any(
        len(set(dependencies_by_key[key])) != len(dependencies_by_key[key])
        for key in nodes
    ):
        raise ValueError("dependency graph contains duplicate edges")
    dependents: dict[PackRegistryKeyV1, list[PackRegistryKeyV1]] = {
        key: [] for key in nodes
    }
    for dependent, dependencies in dependencies_by_key.items():
        for dependency in dependencies:
            dependents[dependency].append(dependent)
    for values in dependents.values():
        values.sort(key=lambda item: item.sort_key)

    ready = [
        (key.sort_key, key)
        for key, count in incoming.items()
        if count == 0
    ]
    heapq.heapify(ready)
    ordered: list[PackRegistryKeyV1] = []
    while ready:
        _, key = heapq.heappop(ready)
        ordered.append(key)
        for dependent in dependents[key]:
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                heapq.heappush(ready, (dependent.sort_key, dependent))
    if len(ordered) != len(nodes):
        raise ValueError("PACK_DEPENDENCY_CYCLE: local dependency graph contains a cycle")
    if root_key not in ordered:
        raise ValueError("dependency graph does not contain its root")
    return tuple(ordered)


def _validate_direct_bindings(
    manifest: PackManifestV1,
    direct: tuple[ResolvedPackDependencyV1, ...],
) -> None:
    if len(direct) != len(manifest.dependencies):
        raise ValueError("resolved direct dependencies differ from manifest requirements")
    by_target: dict[tuple[str, str, str], ResolvedPackDependencyV1] = {}
    for item in direct:
        target = (item.key.creator_id, item.key.namespace, item.key.name)
        if target in by_target:
            raise ValueError("resolved direct dependency targets must be unique")
        by_target[target] = item
    for requirement in manifest.dependencies:
        selected = by_target.get(requirement.target_key)
        if selected is None:
            raise ValueError("manifest dependency has no resolved direct binding")
        if not semver_satisfies(
            selected.key.version,
            requirement.version_constraint,
        ):
            raise ValueError("resolved direct dependency violates its version range")
        if not hmac.compare_digest(selected.pack_id, requirement.expected_pack_id):
            raise ValueError("resolved direct dependency violates its expected digest")


def _require_resolved_tuple(value: object, label: str) -> None:
    if type(value) is not tuple or any(
        type(item) is not ResolvedPackDependencyV1 for item in value
    ):
        raise TypeError(f"resolved {label} must be an immutable typed tuple")


def _validate_runtime_compilers(value: object) -> None:
    if type(value) is not tuple:
        raise TypeError("pack runtime compiler versions must be an immutable tuple")
    normalized: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("pack runtime compiler rows must be exact pairs")
        component_id, version = item
        normalized.append(
            (
                require_data_identifier(
                    component_id,
                    "pack runtime compiler component ID",
                ),
                require_semver(version, "pack runtime compiler version"),
            )
        )
    if tuple(normalized) != value:
        raise ValueError("pack runtime compiler rows changed during validation")
    if tuple(sorted(normalized)) != value:
        raise ValueError("pack runtime compiler versions must use canonical order")
    identifiers = tuple(item[0] for item in normalized)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("pack runtime compiler component IDs must be unique")


def _validate_runtime_schemas(value: object) -> None:
    if type(value) is not tuple:
        raise TypeError("pack runtime schema versions must be an immutable tuple")
    normalized: list[tuple[str, int]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("pack runtime schema rows must be exact pairs")
        schema_id, version = item
        canonical_id = require_data_identifier(
            schema_id,
            "pack runtime schema ID",
        )
        if type(version) is not int or version <= 0:
            raise ValueError("pack runtime schema versions must be positive integers")
        normalized.append((canonical_id, version))
    if tuple(normalized) != value:
        raise ValueError("pack runtime schema rows changed during validation")
    if tuple(normalized) != tuple(sorted(normalized)):
        raise ValueError("pack runtime schema versions must use canonical order")
    identifiers = tuple(item[0] for item in normalized)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("pack runtime schema IDs must be unique")


__all__ = [
    "PackDependencyResolutionV1",
    "PackRuntimeEnvironmentV1",
    "ResolvedPackDependencyV1",
    "resolve_pack_dependencies",
    "semver_satisfies",
    "validate_installability",
]
