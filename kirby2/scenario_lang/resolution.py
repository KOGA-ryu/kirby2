"""Deterministic reusable-definition and single-inheritance resolution."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .imports import ScenarioImportLimitsV1, resolve_scenario_import_bundle
from .models import (
    DEFINITION_MERGE_POLICIES_V1,
    DEFINITION_SECTION_BY_TYPE_V1,
    DEFINITION_TYPE_BY_SECTION_V1,
    SCENARIO_BEHAVIOR_SECTION_NAMES,
    ResolvedScenarioBundleV1,
    ResolvedScenarioDefinitionV1,
    ScenarioDefinitionTypeV1,
    ScenarioFieldV1,
    ScenarioImportBundleV1,
    ScenarioListMergeModeV1,
    ScenarioRecordV1,
    ScenarioValueKindV1,
)


@dataclass(frozen=True, slots=True)
class _UnresolvedDefinition:
    definition_type: ScenarioDefinitionTypeV1
    qualified_name: str
    source_logical_path: str
    record: ScenarioRecordV1


def qualified_definition_name(
    definition_type: ScenarioDefinitionTypeV1,
    logical_name: str,
) -> str:
    if type(definition_type) is not ScenarioDefinitionTypeV1:
        raise TypeError("definition qualified name requires a V1 definition type")
    if type(logical_name) is not str or not logical_name or ":" in logical_name:
        raise ValueError("definition logical name must be nonempty and contain no colon")
    return f"{definition_type.value}:{logical_name}"


def resolve_scenario_bundle(
    source_root: Path,
    entry_path: str,
    *,
    activated_pack_namespaces: Mapping[str, Path] | None = None,
    limits: ScenarioImportLimitsV1 = ScenarioImportLimitsV1(),
) -> ResolvedScenarioBundleV1:
    import_bundle = resolve_scenario_import_bundle(
        source_root,
        entry_path,
        activated_pack_namespaces=activated_pack_namespaces,
        limits=limits,
    )
    return resolve_scenario_definitions(import_bundle)


def resolve_scenario_definitions(
    import_bundle: ScenarioImportBundleV1,
) -> ResolvedScenarioBundleV1:
    if type(import_bundle) is not ScenarioImportBundleV1:
        raise TypeError("definition resolution requires ScenarioImportBundleV1")
    if set(DEFINITION_MERGE_POLICIES_V1) != set(ScenarioDefinitionTypeV1):
        raise RuntimeError("definition merge policies do not cover exactly V1 types")
    _reject_imported_behavior(import_bundle)
    registry: dict[str, _UnresolvedDefinition] = {}
    collision_names: dict[str, str] = {}
    definition_sections = set(DEFINITION_TYPE_BY_SECTION_V1)
    for document in import_bundle.documents:
        source = document.source
        for section_name in SCENARIO_BEHAVIOR_SECTION_NAMES:
            section = getattr(source, section_name)
            if section_name not in definition_sections:
                if any(record.extends is not None for record in section.records):
                    raise ValueError(
                        "inheritance is allowed only for reusable definition types"
                    )
                continue
            definition_type = DEFINITION_TYPE_BY_SECTION_V1[section_name]
            for record in section.records:
                qualified_name = qualified_definition_name(
                    definition_type,
                    record.logical_name,
                )
                collision_key = _logical_collision_key(qualified_name)
                previous = collision_names.get(collision_key)
                if previous is not None:
                    if previous == qualified_name:
                        raise ValueError(
                            f"duplicate scenario definition: {qualified_name}"
                        )
                    raise ValueError(
                        "scenario definition names have a case/Unicode collision"
                    )
                collision_names[collision_key] = qualified_name
                registry[qualified_name] = _UnresolvedDefinition(
                    definition_type,
                    qualified_name,
                    document.logical_path,
                    record,
                )

    resolved: dict[str, ResolvedScenarioDefinitionV1] = {}
    visiting: set[str] = set()

    def resolve_one(qualified_name: str) -> ResolvedScenarioDefinitionV1:
        cached = resolved.get(qualified_name)
        if cached is not None:
            return cached
        if qualified_name in visiting:
            raise ValueError("scenario definition inheritance contains a cycle")
        try:
            unresolved = registry[qualified_name]
        except KeyError as error:
            raise ValueError(
                f"scenario definition references an unknown parent: {qualified_name}"
            ) from error
        visiting.add(qualified_name)
        try:
            if unresolved.record.extends is None:
                record = replace(unresolved.record, extends=None)
                chain: tuple[str, ...] = ()
            else:
                parent_type, parent_name = _parse_qualified_name(
                    unresolved.record.extends
                )
                if parent_type is not unresolved.definition_type:
                    raise ValueError(
                        "scenario definition inheritance cannot cross definition types"
                    )
                parent = resolve_one(parent_name)
                record = merge_definition_records(
                    unresolved.definition_type,
                    parent.record,
                    unresolved.record,
                )
                chain = (*parent.inheritance_chain, parent.qualified_name)
            result = ResolvedScenarioDefinitionV1(
                definition_type=unresolved.definition_type,
                qualified_name=unresolved.qualified_name,
                source_logical_path=unresolved.source_logical_path,
                inheritance_chain=chain,
                record=record,
            )
            resolved[qualified_name] = result
            return result
        finally:
            visiting.remove(qualified_name)

    for qualified_name in sorted(registry):
        resolve_one(qualified_name)
    return ResolvedScenarioBundleV1(
        import_bundle,
        tuple(resolved[name] for name in sorted(resolved)),
    )


def merge_definition_records(
    definition_type: ScenarioDefinitionTypeV1,
    parent: ScenarioRecordV1,
    child: ScenarioRecordV1,
) -> ScenarioRecordV1:
    """Apply the explicit V1 merge policy; no list concatenation is implicit."""

    if type(definition_type) is not ScenarioDefinitionTypeV1:
        raise TypeError("definition merge requires a V1 definition type")
    if type(parent) is not ScenarioRecordV1 or type(child) is not ScenarioRecordV1:
        raise TypeError("definition merge requires typed scenario records")
    policy = DEFINITION_MERGE_POLICIES_V1[definition_type]
    fields: dict[str, ScenarioFieldV1] = {
        field.name: field for field in parent.fields
    }
    for child_field in child.fields:
        parent_field = fields.get(child_field.name)
        if parent_field is None:
            fields[child_field.name] = child_field
            continue
        if parent_field.value_kind is not child_field.value_kind:
            raise ValueError(
                "scenario inheritance cannot change an inherited field value tag"
            )
        if child_field.value_kind is ScenarioValueKindV1.IDENTIFIERS:
            if policy.identifier_list_mode is ScenarioListMergeModeV1.KEYED_MERGE:
                merged = tuple(
                    sorted({*tuple(parent_field.value), *tuple(child_field.value)})
                )
                fields[child_field.name] = replace(child_field, value=merged)
            elif policy.identifier_list_mode is ScenarioListMergeModeV1.REPLACE:
                fields[child_field.name] = child_field
            else:  # pragma: no cover - policy contract is closed
                raise RuntimeError("unsupported definition identifier-list merge mode")
        else:
            fields[child_field.name] = child_field
    return ScenarioRecordV1(
        logical_name=child.logical_name,
        record_type=child.record_type,
        version=child.version,
        fields=tuple(fields.values()),
        reference=(child.reference if child.reference is not None else parent.reference),
        extends=None,
    )


def _reject_imported_behavior(import_bundle: ScenarioImportBundleV1) -> None:
    definition_sections = set(DEFINITION_TYPE_BY_SECTION_V1)
    for document in import_bundle.documents[1:]:
        for section_name in SCENARIO_BEHAVIOR_SECTION_NAMES:
            if section_name in definition_sections:
                continue
            section = getattr(document.source, section_name)
            if section.records:
                raise ValueError(
                    "imported scenario documents may contain reusable definitions only"
                )


def _parse_qualified_name(
    value: str,
) -> tuple[ScenarioDefinitionTypeV1, str]:
    if type(value) is not str or value.count(":") != 1:
        raise ValueError("inherited definitions require one stable qualified name")
    prefix, logical_name = value.split(":", 1)
    try:
        definition_type = ScenarioDefinitionTypeV1(prefix)
    except ValueError as error:
        raise ValueError("inherited definition uses an unknown definition type") from error
    expected = qualified_definition_name(definition_type, logical_name)
    return definition_type, expected


def _logical_collision_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


__all__ = [
    "merge_definition_records",
    "qualified_definition_name",
    "resolve_scenario_bundle",
    "resolve_scenario_definitions",
]
