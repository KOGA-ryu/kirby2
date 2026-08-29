"""Non-persisting executable evidence for the WO32 scenario language."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from kirby2.research.toml_codec import canonical_toml
from kirby2.scenario_lang.identity import (
    SourceBundleEntryV1,
    canonical_semantic_plan_bytes,
    compiled_artifact_digest,
    semantic_plan_digest,
    source_bundle_digest,
)
from kirby2.scenario_lang.imports import (
    ScenarioImportLimitsV1,
    ScenarioImportResolver,
    parse_scenario_source_document,
    validate_scenario_import_path,
)
from kirby2.scenario_lang.models import (
    DEFINITION_SECTION_BY_TYPE_V1,
    DEFINITION_MERGE_POLICIES_V1,
    SCENARIO_BEHAVIOR_SECTION_NAMES,
    SCENARIO_SOURCE_SECTION_NAMES,
    SCENARIO_TARGET_CONTRACTS_V1,
    ExactFixedPointV1,
    ScenarioDefinitionTypeV1,
    ScenarioFieldV1,
    ScenarioImportV1,
    ScenarioListMergeModeV1,
    ScenarioMetadataV1,
    ScenarioPlanEnvelopeV1,
    ScenarioRecordV1,
    ScenarioSectionV1,
    ScenarioSourceV1,
    ScenarioTargetKindV1,
    ScenarioValueKindV1,
    VolumeMultiplierV1,
)
from kirby2.scenario_lang.resolution import resolve_scenario_bundle
from kirby2.scenario_lang.schema import (
    canonical_scenario_source_bytes,
    parse_canonical_scenario_source,
    parse_scenario_source,
    scenario_source_round_trip,
)


WO32A_STRICT_REFUSAL_COUNT = 13
WO32B_IMPORT_REFUSAL_COUNT = 18
WO32B_DEFINITION_REFUSAL_COUNT = 9


@dataclass(frozen=True, slots=True)
class ScenarioLanguageAuditCase:
    name: str
    detail: str
    failures: tuple[str, ...]
    required: bool = True

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("scenario-language audit case requires a name")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("scenario-language audit case requires detail")
        if type(self.failures) is not tuple or any(
            type(item) is not str or not item for item in self.failures
        ):
            raise TypeError("scenario-language audit failures must be a string tuple")
        if type(self.required) is not bool:
            raise TypeError("scenario-language audit required flag must be a bool")

    @property
    def status(self) -> str:
        return "FAIL" if self.failures else "PASS"

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "failures": list(self.failures),
            "name": self.name,
            "required": self.required,
            "status": self.status,
        }


def audit_wo32a_scenario_language() -> tuple[ScenarioLanguageAuditCase, ...]:
    """Exercise only the source/schema/identity/envelope contracts in WO32-A."""

    source = _sample_source()
    return (
        _section_inventory_case(source),
        _canonical_round_trip_case(source),
        _identity_domains_case(source),
        _strict_refusal_case(source),
        _native_envelope_case(),
        _immutable_ownership_case(source),
    )


def audit_scenario_language() -> tuple[ScenarioLanguageAuditCase, ...]:
    return (
        *audit_wo32a_scenario_language(),
        *audit_wo32b_scenario_language(),
    )


def audit_wo32b_scenario_language() -> tuple[ScenarioLanguageAuditCase, ...]:
    """Exercise confined imports and deterministic definition inheritance."""

    return (
        _wo32a_contract_regression_case(),
        _nested_import_relocation_case(),
        _definition_inheritance_merge_case(),
        _hostile_import_graph_case(),
        _hostile_definition_resolution_case(),
    )


def _sample_source() -> ScenarioSourceV1:
    contract = SCENARIO_TARGET_CONTRACTS_V1[
        ScenarioTargetKindV1.MARKET_SCENARIO_V1
    ]
    fields = (
        ScenarioFieldV1(
            "behavioral_bound",
            ScenarioValueKindV1.FIXED_POINT,
            ExactFixedPointV1(125, 100, "RATIO"),
        ),
        ScenarioFieldV1(
            "decision_interval",
            ScenarioValueKindV1.DURATION_MS,
            250,
        ),
        ScenarioFieldV1(
            "initial_mid",
            ScenarioValueKindV1.PRICE_TICKS,
            10_000,
        ),
        ScenarioFieldV1(
            "initial_quantity",
            ScenarioValueKindV1.QUANTITY_SHARES,
            500,
        ),
        ScenarioFieldV1(
            "message_rate",
            ScenarioValueKindV1.RATE_PER_SECOND,
            40,
        ),
        ScenarioFieldV1(
            "routing_latency",
            ScenarioValueKindV1.LATENCY_US,
            125,
        ),
        ScenarioFieldV1(
            "relative_volume",
            ScenarioValueKindV1.VOLUME_MULTIPLIER,
            VolumeMultiplierV1(5, 4),
        ),
        ScenarioFieldV1(
            "transition",
            ScenarioValueKindV1.PROBABILITY_WEIGHT,
            7,
        ),
    )
    market = ScenarioSectionV1(
        (
            ScenarioRecordV1(
                logical_name="primary_market",
                record_type="SYNTHETIC_MARKET",
                version=1,
                fields=fields,
            ),
        )
    )
    capabilities = ScenarioSectionV1(
        (
            ScenarioRecordV1(
                logical_name="required_observation",
                record_type="CAPABILITY_REQUIREMENT",
                version=1,
                fields=(
                    ScenarioFieldV1(
                        "capability",
                        ScenarioValueKindV1.IDENTIFIER,
                        "TOP_OF_BOOK_V1",
                    ),
                ),
            ),
        )
    )
    empty = ScenarioSectionV1(())
    return ScenarioSourceV1(
        schema_version=1,
        metadata=ScenarioMetadataV1(
            scenario_id="audit_market_source_v1",
            scenario_version=1,
            title="WO32-A audit market",
            description="Contract-only source fixture",
            target_kind=contract.target_kind,
            target_version=contract.target_version,
            adapter_id=contract.adapter_id,
            adapter_version=contract.adapter_version,
            capability_digest=hashlib.sha256(
                b"WO32-A_AUDIT_CAPABILITIES_V1"
            ).hexdigest(),
        ),
        market_profile=market,
        instrument=empty,
        venues=empty,
        session_schedule=empty,
        flow_model=empty,
        regimes=empty,
        day_local_states=empty,
        volume=empty,
        liquidity=empty,
        latency=empty,
        agent_populations=empty,
        scheduled_events=empty,
        unscheduled_events=empty,
        transition_rules=empty,
        historical_constraints=empty,
        player_objective=empty,
        strategy=empty,
        curriculum_metadata=empty,
        reveal_policy=empty,
        checkpoint_policy=empty,
        seed_policy=empty,
        accepted_behavioral_envelopes=empty,
        required_source_capabilities=capabilities,
    )


def _section_inventory_case(source: ScenarioSourceV1) -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    payload = source.as_dict()
    actual_sections = tuple(
        name for name in SCENARIO_SOURCE_SECTION_NAMES if name in payload
    )
    if actual_sections != SCENARIO_SOURCE_SECTION_NAMES:
        failures.append("source does not expose the complete ordered section inventory")
    if set(payload) != {"schema_version", *SCENARIO_SOURCE_SECTION_NAMES}:
        failures.append("source root differs from the closed V1 field inventory")
    record = source.market_profile.records[0]
    value_kinds = {field.value_kind for field in record.fields}
    expected_kinds = {
        ScenarioValueKindV1.DURATION_MS,
        ScenarioValueKindV1.PRICE_TICKS,
        ScenarioValueKindV1.QUANTITY_SHARES,
        ScenarioValueKindV1.RATE_PER_SECOND,
        ScenarioValueKindV1.LATENCY_US,
        ScenarioValueKindV1.VOLUME_MULTIPLIER,
        ScenarioValueKindV1.PROBABILITY_WEIGHT,
    }
    if not expected_kinds.issubset(value_kinds):
        failures.append("sample source does not exercise the fixed V1 unit vocabulary")
    semantic_fields = {
        item["name"]: item
        for item in source.semantic_projection()["market_profile"]["records"][0][
            "fields"
        ]
    }
    duration = semantic_fields["decision_interval"]
    if duration != {"duration_us": 250_000, "name": "decision_interval"}:
        failures.append("duration_ms did not normalize exactly to integer microseconds")
    multiplier = semantic_fields["relative_volume"].get("volume_multiplier")
    if multiplier != {"denominator": 4, "numerator": 5}:
        failures.append("volume multiplier did not retain its reduced rational")
    return ScenarioLanguageAuditCase(
        "scenario_source_section_inventory",
        (
            f"sections={len(SCENARIO_SOURCE_SECTION_NAMES)} "
            "unit_tags=7 duration_ms=250 duration_us=250000 multiplier=5/4"
        ),
        tuple(failures),
    )


def _canonical_round_trip_case(
    source: ScenarioSourceV1,
) -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    canonical = canonical_scenario_source_bytes(source)
    restored = parse_canonical_scenario_source(canonical)
    if restored != source:
        failures.append("canonical TOML did not restore the immutable source")
    if scenario_source_round_trip(source) != source:
        failures.append("scenario source round-trip helper changed the source")
    commented = b"# provenance-only author comment\n\n" + canonical
    parsed_commented = parse_scenario_source(commented)
    if parsed_commented != source:
        failures.append("comments or formatting changed parsed source behavior")
    if canonical_scenario_source_bytes(parsed_commented) != canonical:
        failures.append("formatted source did not converge to one canonical TOML form")
    fixed_point_payload = source.as_dict()
    multiplier = next(
        item
        for item in fixed_point_payload["market_profile"]["records"][0]["fields"]
        if item["name"] == "relative_volume"
    )
    multiplier["volume_multiplier"] = {"coefficient": 125, "scale": 100}
    fixed_point_source = parse_scenario_source(
        canonical_toml(fixed_point_payload).encode("utf-8")
    )
    if fixed_point_source != source:
        failures.append("exact fixed-point multiplier did not normalize to 5/4")
    return ScenarioLanguageAuditCase(
        "scenario_source_canonical_roundtrip",
        (
            f"canonical_bytes={len(canonical)} comment_bytes={len(commented)} "
            "strict_parse=PASS"
        ),
        tuple(failures),
    )


def _identity_domains_case(source: ScenarioSourceV1) -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    canonical = canonical_scenario_source_bytes(source)
    formatted = b"# source formatting variant\n" + canonical
    parsed_formatted = parse_scenario_source(formatted)
    source_digest = source_bundle_digest(
        (SourceBundleEntryV1("main.toml", canonical),)
    )
    formatted_source_digest = source_bundle_digest(
        (SourceBundleEntryV1("main.toml", formatted),)
    )
    semantic_digest = semantic_plan_digest(source)
    formatted_semantic_digest = semantic_plan_digest(parsed_formatted)
    artifact = canonical_semantic_plan_bytes(source)
    artifact_digest = compiled_artifact_digest(
        artifact,
        {"source_bundle_digest": source_digest},
    )
    formatted_artifact_digest = compiled_artifact_digest(
        artifact,
        {"source_bundle_digest": formatted_source_digest},
    )
    changed_source = _semantic_edit(source)
    changed_raw = canonical_scenario_source_bytes(changed_source)
    changed_source_digest = source_bundle_digest(
        (SourceBundleEntryV1("main.toml", changed_raw),)
    )
    changed_semantic_digest = semantic_plan_digest(changed_source)
    changed_artifact_digest = compiled_artifact_digest(
        canonical_semantic_plan_bytes(changed_source),
        {"source_bundle_digest": changed_source_digest},
    )
    if source_digest == formatted_source_digest:
        failures.append("format/comment change did not alter source provenance")
    if semantic_digest != formatted_semantic_digest:
        failures.append("format/comment change altered semantic identity")
    if artifact_digest == formatted_artifact_digest:
        failures.append("provenance change did not alter compiled artifact identity")
    if semantic_digest == changed_semantic_digest:
        failures.append("semantic field edit did not alter semantic identity")
    if artifact_digest == changed_artifact_digest:
        failures.append("semantic field edit did not alter artifact identity")
    if len({source_digest, semantic_digest, artifact_digest}) != 3:
        failures.append("source, semantic, and artifact digest domains collided")
    ordered_a = source_bundle_digest((b"alpha", b"beta"))
    ordered_b = source_bundle_digest((b"beta", b"alpha"))
    if ordered_a == ordered_b:
        failures.append("source bundle identity ignored member order")
    return ScenarioLanguageAuditCase(
        "scenario_identity_domain_separation",
        (
            "format_source_changed=true format_semantic_changed=false "
            "format_artifact_changed=true semantic_edit_changed=true ordered=true"
        ),
        tuple(failures),
    )


def _semantic_edit(source: ScenarioSourceV1) -> ScenarioSourceV1:
    record = source.market_profile.records[0]
    fields = tuple(
        replace(field, value=10_001)
        if field.name == "initial_mid"
        else field
        for field in record.fields
    )
    changed_record = replace(record, fields=fields)
    return replace(
        source,
        market_profile=ScenarioSectionV1((changed_record,)),
    )


def _strict_refusal_case(source: ScenarioSourceV1) -> ScenarioLanguageAuditCase:
    base = source.as_dict()
    probes: list[tuple[str, Callable[[], object]]] = []

    def encoded(payload: dict[str, object]) -> bytes:
        return canonical_toml(payload).encode("utf-8")

    changed = copy.deepcopy(base)
    changed["schema_version"] = 2
    probes.append(
        (
            "unsupported schema version",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    changed["unknown_section"] = {"records": []}
    probes.append(
        (
            "unknown root field",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    changed["metadata"]["python_import"] = "os.system"
    probes.append(
        (
            "unknown metadata field",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    changed["liquidity"]["enabled"] = True
    probes.append(
        (
            "unknown section field",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    changed["market_profile"]["records"].append(
        copy.deepcopy(changed["market_profile"]["records"][0])
    )
    probes.append(
        (
            "duplicate logical name",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    changed["market_profile"]["records"][0]["fields"].append(
        copy.deepcopy(changed["market_profile"]["records"][0]["fields"][0])
    )
    probes.append(
        (
            "duplicate field name",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    changed["metadata"]["scenario_version"] = 1.0
    probes.append(("float identity", lambda value=changed: parse_scenario_source(encoded(value))))

    changed = copy.deepcopy(base)
    field = next(
        item
        for item in changed["market_profile"]["records"][0]["fields"]
        if item["name"] == "decision_interval"
    )
    value = next(item for key, item in field.items() if key != "name")
    for key in tuple(field):
        if key != "name":
            del field[key]
    field["value"] = value
    probes.append(
        (
            "bare ambiguous numeric",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    field = changed["market_profile"]["records"][0]["fields"][0]
    field["count"] = 1
    probes.append(
        (
            "multiple value tags",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    multiplier = next(
        item
        for item in changed["market_profile"]["records"][0]["fields"]
        if item["name"] == "relative_volume"
    )
    multiplier["volume_multiplier"] = {"numerator": 10, "denominator": 8}
    probes.append(
        (
            "unreduced volume multiplier",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    fixed = next(
        item
        for item in changed["market_profile"]["records"][0]["fields"]
        if item["name"] == "behavioral_bound"
    )
    fixed["fixed_point"] = {"coefficient": 120, "scale": 100, "unit": "RATIO"}
    probes.append(
        (
            "unreduced fixed point",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    changed = copy.deepcopy(base)
    del changed["historical_constraints"]
    probes.append(
        (
            "missing required section",
            lambda value=changed: parse_scenario_source(encoded(value)),
        )
    )

    duplicate_key = b"schema_version = 1\n" + canonical_scenario_source_bytes(source)
    probes.append(("duplicate TOML key", lambda: parse_scenario_source(duplicate_key)))

    failures: list[str] = []
    refusals = 0
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure is None:
            refusals += 1
        else:
            failures.append(failure)
    if len(probes) != WO32A_STRICT_REFUSAL_COUNT:
        failures.append("strict refusal inventory count changed")
    return ScenarioLanguageAuditCase(
        "scenario_source_strict_refusals",
        (
            f"refused={refusals}/{len(probes)} unknown=float=duplicate="
            "ambiguous_numeric=version=PASS"
        ),
        tuple(failures),
    )


def _native_envelope_case() -> ScenarioLanguageAuditCase:
    from kirby2.audit.full_day import _sample_plan
    from kirby2.historical.lesson_catalog import load_historical_lessons
    from kirby2.multivenue.replay import MultiVenueRecording
    from kirby2.observability.replay import ObservabilityRecording
    from kirby2.scenarios.market import load_scenario_definitions

    failures: list[str] = []
    empty_digest = hashlib.sha256(b"{}").hexdigest()
    payloads: dict[ScenarioTargetKindV1, object] = {
        ScenarioTargetKindV1.FULL_DAY_PLAN_V1: _sample_plan(),
        ScenarioTargetKindV1.MARKET_SCENARIO_V1: load_scenario_definitions()[
            "balanced"
        ],
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1: ObservabilityRecording(
            rules={},
            commands=(),
            completed_time_us=0,
            expected_observable_feed={},
            expected_ground_truth={},
            expected_observable_sha256=empty_digest,
            expected_truth_sha256=empty_digest,
            expected_state_sha256="0" * 64,
        ),
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1: MultiVenueRecording(
            seed=1,
            venue_configs=(),
            depth_subscriptions=(),
            commands=(),
            completed_time_us=0,
            route_ids=(),
            expected_events=(),
            expected_feed={},
            expected_ground_truth={},
            expected_scores={},
            expected_state_sha256="0" * 64,
        ),
        ScenarioTargetKindV1.HISTORICAL_LESSON_V1: next(
            iter(load_historical_lessons().values())
        ),
    }
    capability_digest = hashlib.sha256(b"WO32-A_ENVELOPE_CAPABILITY_V1").hexdigest()
    for kind in ScenarioTargetKindV1:
        contract = SCENARIO_TARGET_CONTRACTS_V1[kind]
        envelope = ScenarioPlanEnvelopeV1(
            target_kind=kind,
            payload=payloads[kind],
            capability_digest=capability_digest,
            target_version=contract.target_version,
            adapter_id=contract.adapter_id,
            adapter_version=contract.adapter_version,
        )
        restored = ScenarioPlanEnvelopeV1.from_dict(envelope.as_dict())
        if restored != envelope or restored.canonical_bytes() != envelope.canonical_bytes():
            failures.append(f"{kind.value} envelope did not round trip canonically")
        if type(restored.payload) is not type(payloads[kind]):
            failures.append(f"{kind.value} envelope changed its native payload type")
        semantic = envelope.semantic_projection()
        expected_identity_fields = {
            "adapter_id",
            "adapter_version",
            "capability_digest",
            "target_kind",
            "target_version",
        }
        if not expected_identity_fields.issubset(semantic):
            failures.append(f"{kind.value} envelope omitted dispatch identity fields")

    market_payload = payloads[ScenarioTargetKindV1.MARKET_SCENARIO_V1]
    market_envelope = ScenarioPlanEnvelopeV1(
        target_kind=ScenarioTargetKindV1.MARKET_SCENARIO_V1,
        payload=market_payload,
        capability_digest=capability_digest,
    )
    detached_payload = market_envelope.payload
    detached_payload.parameter_overrides["mutated"] = 1.0
    if "mutated" in market_envelope.payload.parameter_overrides:
        failures.append("native payload mutation escaped into the immutable envelope")
    alternate_capability = hashlib.sha256(b"WO32-A_ALTERNATE_CAPABILITY_V1").hexdigest()
    alternate_envelope = ScenarioPlanEnvelopeV1(
        target_kind=ScenarioTargetKindV1.MARKET_SCENARIO_V1,
        payload=market_payload,
        capability_digest=alternate_capability,
    )
    if semantic_plan_digest(market_envelope) == semantic_plan_digest(alternate_envelope):
        failures.append("capability digest did not enter envelope semantic identity")

    probes = (
        (
            "wrong native payload type",
            lambda: ScenarioPlanEnvelopeV1(
                target_kind=ScenarioTargetKindV1.FULL_DAY_PLAN_V1,
                payload=market_payload,
                capability_digest=capability_digest,
            ),
        ),
        (
            "arbitrary target kind",
            lambda: ScenarioPlanEnvelopeV1(
                target_kind="builtins.eval",
                payload=market_payload,
                capability_digest=capability_digest,
            ),
        ),
        (
            "arbitrary adapter import",
            lambda: ScenarioPlanEnvelopeV1(
                target_kind=ScenarioTargetKindV1.MARKET_SCENARIO_V1,
                payload=market_payload,
                capability_digest=capability_digest,
                adapter_id="os.system",
            ),
        ),
        (
            "wrong target version",
            lambda: ScenarioPlanEnvelopeV1(
                target_kind=ScenarioTargetKindV1.MARKET_SCENARIO_V1,
                payload=market_payload,
                capability_digest=capability_digest,
                target_version=2,
            ),
        ),
        (
            "untagged payload",
            lambda: ScenarioPlanEnvelopeV1.from_dict(
                {"payload": market_payload.as_dict()}
            ),
        ),
        (
            "unknown envelope field",
            lambda: ScenarioPlanEnvelopeV1.from_dict(
                {**market_envelope.as_dict(), "python_class": "os.system"}
            ),
        ),
        (
            "unknown native payload field",
            lambda: ScenarioPlanEnvelopeV1.from_dict(
                {
                    **market_envelope.as_dict(),
                    "payload": {
                        **market_envelope.as_dict()["payload"],
                        "python_class": "os.system",
                    },
                }
            ),
        ),
    )
    refusals = 0
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure is None:
            refusals += 1
        else:
            failures.append(failure)
    return ScenarioLanguageAuditCase(
        "scenario_plan_native_envelopes",
        (
            f"target_kinds={len(payloads)} native_roundtrips={len(payloads)} "
            f"hostile_refusals={refusals}/{len(probes)} second_runtime_ir=false"
        ),
        tuple(failures),
    )


def _immutable_ownership_case(source: ScenarioSourceV1) -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    before = canonical_scenario_source_bytes(source)
    detached = source.as_dict()
    detached["metadata"]["title"] = "mutated detached copy"
    detached["market_profile"]["records"][0]["fields"][0]["name"] = "mutated"
    if canonical_scenario_source_bytes(source) != before:
        failures.append("detached source serialization mutated the immutable source")
    field_names = tuple(
        field.name for field in source.market_profile.records[0].fields
    )
    if field_names != tuple(sorted(field_names)):
        failures.append("source fields are not stored in canonical name order")
    try:
        source.schema_version = 2
    except (AttributeError, TypeError):
        pass
    else:
        failures.append("frozen source accepted attribute mutation")
    return ScenarioLanguageAuditCase(
        "scenario_source_immutable_ownership",
        "frozen_source=true detached_serialization=true canonical_order=true",
        tuple(failures),
    )


def _wo32a_contract_regression_case() -> ScenarioLanguageAuditCase:
    cases = audit_wo32a_scenario_language()
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    if len(cases) != 6:
        failures = (*failures, "WO32-A evidence inventory no longer has six cases")
    return ScenarioLanguageAuditCase(
        "scenario_import_wo32a_contract_regression",
        f"wo32a_cases={len(cases)} passing={sum(not case.failures for case in cases)}",
        failures,
    )


def _nested_import_relocation_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    with TemporaryDirectory(prefix="kirby2-wo32b-relocation-a-") as first_temp, (
        TemporaryDirectory(prefix="kirby2-wo32b-relocation-b-")
    ) as second_temp:
        first_source, first_pack = _write_valid_import_fixture(Path(first_temp))
        second_source, second_pack = _write_valid_import_fixture(Path(second_temp))
        first = resolve_scenario_bundle(
            first_source,
            "main.toml",
            activated_pack_namespaces={"audit_pack": first_pack},
        )
        second = resolve_scenario_bundle(
            second_source,
            "main.toml",
            activated_pack_namespaces={"audit_pack": second_pack},
        )
        expected_documents = (
            "source-root:main.toml",
            "source-root:defs/base.toml",
            "pack:audit_pack:common/venue.toml",
            "pack:audit_pack:common/latency.toml",
        )
        actual_documents = tuple(
            document.logical_path for document in first.import_bundle.documents
        )
        if actual_documents != expected_documents:
            failures.append("nested import graph did not retain declared DFS order")
        expected_edges = (
            (
                "source-root:main.toml",
                "source-root:defs/base.toml",
                0,
            ),
            (
                "source-root:defs/base.toml",
                "pack:audit_pack:common/venue.toml",
                0,
            ),
            (
                "pack:audit_pack:common/venue.toml",
                "pack:audit_pack:common/latency.toml",
                0,
            ),
        )
        actual_edges = tuple(
            (
                edge.importer_logical_path,
                edge.imported_logical_path,
                edge.import_ordinal,
            )
            for edge in first.import_bundle.edges
        )
        if actual_edges != expected_edges:
            failures.append("nested import provenance omitted or reordered graph edges")
        if first.semantic_projection() != second.semantic_projection():
            failures.append("relocating source and pack roots changed resolved behavior")
        if first.provenance_projection() != second.provenance_projection():
            failures.append("relocating source and pack roots changed source provenance")
        if (
            first.import_bundle.source_bundle_digest
            != second.import_bundle.source_bundle_digest
        ):
            failures.append("relocating roots changed the ordered source bundle digest")
        encoded_provenance = json.dumps(
            first.provenance_projection(),
            sort_keys=True,
        )
        if str(first_source) in encoded_provenance or str(first_pack) in encoded_provenance:
            failures.append("physical root path leaked into stable provenance")
        if len(first.import_bundle.documents) != len(
            {document.raw_sha256 for document in first.import_bundle.documents}
        ):
            failures.append("audit fixture unexpectedly reused a document byte digest")
    return ScenarioLanguageAuditCase(
        "scenario_nested_import_relocation",
        "documents=4 edges=3 authorities=2 ordered_graph=true relocation_stable=true",
        tuple(failures),
    )


def _definition_inheritance_merge_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    with TemporaryDirectory(prefix="kirby2-wo32b-inheritance-") as inherited_temp, (
        TemporaryDirectory(prefix="kirby2-wo32b-flattened-")
    ) as flattened_temp:
        inherited_source, inherited_pack = _write_valid_import_fixture(
            Path(inherited_temp)
        )
        flattened_source, flattened_pack = _write_valid_import_fixture(
            Path(flattened_temp),
            flattened=True,
        )
        inherited = resolve_scenario_bundle(
            inherited_source,
            "main.toml",
            activated_pack_namespaces={"audit_pack": inherited_pack},
        )
        flattened = resolve_scenario_bundle(
            flattened_source,
            "main.toml",
            activated_pack_namespaces={"audit_pack": flattened_pack},
        )
        market = inherited.definition("market:derived_market")
        market_fields = {field.name: field for field in market.record.fields}
        if tuple(market_fields["symbols"].value) != ("CCC",):
            failures.append("market identifier list was not explicitly replaced")
        if market_fields["initial_mid"].value != 10_050:
            failures.append("child market scalar did not override its parent")
        if market_fields["decision_interval"].value != 250:
            failures.append("child market omitted an inherited scalar")
        if market.record.reference != "base_market_reference":
            failures.append("child market omitted its inherited reference")
        if market.inheritance_chain != ("market:base_market",):
            failures.append("market inheritance provenance is incomplete")

        venue = inherited.definition("venue:derived_venue")
        venue_fields = {field.name: field for field in venue.record.fields}
        if tuple(venue_fields["supported_orders"].value) != (
            "LIMIT",
            "MARKET",
            "POST_ONLY",
        ):
            failures.append("venue identifier list did not use keyed merge")
        if venue_fields["queue_model"].value != "FIFO":
            failures.append("child venue omitted an inherited scalar")
        if venue.record.reference != "derived_venue_reference":
            failures.append("child venue reference did not explicitly replace its parent")
        if inherited.semantic_projection() != flattened.semantic_projection():
            failures.append("inheritance and its explicitly flattened form differ semantically")
        if semantic_plan_digest(inherited.semantic_projection()) != semantic_plan_digest(
            flattened.semantic_projection()
        ):
            failures.append("flattened and inherited semantic identities differ")
        if (
            inherited.import_bundle.source_bundle_digest
            == flattened.import_bundle.source_bundle_digest
        ):
            failures.append("inheritance source change disappeared from provenance identity")
        if inherited.provenance_projection() == flattened.provenance_projection():
            failures.append("inheritance chain disappeared from provenance")
    return ScenarioLanguageAuditCase(
        "scenario_definition_inheritance_merge",
        "single_inheritance=true scalar_override=true list_replace=true keyed_merge=true",
        tuple(failures),
    )


def _hostile_import_graph_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    refusals = 0

    lexical_probes = (
        ("HTTPS URL", "https://example.invalid/scenario.toml"),
        ("file URI", "file:///tmp/scenario.toml"),
        ("absolute POSIX path", "/tmp/scenario.toml"),
        ("parent traversal", "../scenario.toml"),
        ("backslash path", "nested\\scenario.toml"),
        ("Windows drive path", "C:/scenario.toml"),
        ("Windows UNC path", "//server/share/scenario.toml"),
        ("NUL path", "scenario\x00.toml"),
    )
    for label, path in lexical_probes:
        failure = _expect_refusal(
            lambda value=path: validate_scenario_import_path(value),
            label,
        )
        if failure is None:
            refusals += 1
        else:
            failures.append(failure)

    with TemporaryDirectory(prefix="kirby2-wo32b-hostile-imports-") as temp:
        fixture_root = Path(temp)

        case_root = fixture_root / "symlink_escape"
        source_root = case_root / "source"
        outside_root = case_root / "outside"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_symlink_escape_root_v1",
                imports=(ScenarioImportV1("escape.toml"),),
            ),
        )
        _write_document(
            outside_root / "escape.toml",
            _source_document_bytes("audit_symlink_escape_target_v1"),
        )
        os.symlink(outside_root / "escape.toml", source_root / "escape.toml")
        refusals += _record_read_only_refusal(
            failures,
            "escaping symlink",
            case_root,
            lambda: ScenarioImportResolver(source_root).resolve("main.toml"),
        )

        case_root = fixture_root / "duplicate_canonical"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_duplicate_canonical_root_v1",
                imports=(
                    ScenarioImportV1("target.toml"),
                    ScenarioImportV1("alias.toml"),
                ),
            ),
        )
        _write_document(
            source_root / "target.toml",
            _source_document_bytes("audit_duplicate_canonical_target_v1"),
        )
        os.symlink(source_root / "target.toml", source_root / "alias.toml")
        refusals += _record_read_only_refusal(
            failures,
            "duplicate canonical path",
            case_root,
            lambda: ScenarioImportResolver(source_root).resolve("main.toml"),
        )

        case_root = fixture_root / "cycle"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_import_cycle_root_v1",
                imports=(ScenarioImportV1("child.toml"),),
            ),
        )
        _write_document(
            source_root / "child.toml",
            _source_document_bytes(
                "audit_import_cycle_child_v1",
                imports=(ScenarioImportV1("main.toml"),),
            ),
        )
        refusals += _record_read_only_refusal(
            failures,
            "import cycle",
            case_root,
            lambda: ScenarioImportResolver(source_root).resolve("main.toml"),
        )

        case_root = fixture_root / "depth_limit"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_depth_root_v1",
                imports=(ScenarioImportV1("one.toml"),),
            ),
        )
        _write_document(
            source_root / "one.toml",
            _source_document_bytes(
                "audit_depth_one_v1",
                imports=(ScenarioImportV1("two.toml"),),
            ),
        )
        _write_document(
            source_root / "two.toml",
            _source_document_bytes("audit_depth_two_v1"),
        )
        refusals += _record_read_only_refusal(
            failures,
            "excessive import depth",
            case_root,
            lambda: ScenarioImportResolver(
                source_root,
                limits=ScenarioImportLimitsV1(maximum_depth=1),
            ).resolve("main.toml"),
        )

        case_root = fixture_root / "count_limit"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_count_root_v1",
                imports=(
                    ScenarioImportV1("one.toml"),
                    ScenarioImportV1("two.toml"),
                ),
            ),
        )
        _write_document(
            source_root / "one.toml",
            _source_document_bytes("audit_count_one_v1"),
        )
        _write_document(
            source_root / "two.toml",
            _source_document_bytes("audit_count_two_v1"),
        )
        refusals += _record_read_only_refusal(
            failures,
            "excessive import count",
            case_root,
            lambda: ScenarioImportResolver(
                source_root,
                limits=ScenarioImportLimitsV1(maximum_documents=2),
            ).resolve("main.toml"),
        )

        case_root = fixture_root / "byte_limit"
        source_root = case_root / "source"
        byte_limited_source = _source_document_bytes("audit_byte_limit_root_v1")
        _write_document(source_root / "main.toml", byte_limited_source)
        refusals += _record_read_only_refusal(
            failures,
            "excessive expanded bytes",
            case_root,
            lambda: ScenarioImportResolver(
                source_root,
                limits=ScenarioImportLimitsV1(
                    maximum_expanded_bytes=len(byte_limited_source) - 1
                ),
            ).resolve("main.toml"),
        )

        case_root = fixture_root / "unactivated_pack"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_unactivated_pack_root_v1",
                imports=(ScenarioImportV1("definition.toml", "missing_pack"),),
            ),
        )
        refusals += _record_read_only_refusal(
            failures,
            "unactivated pack namespace",
            case_root,
            lambda: ScenarioImportResolver(source_root).resolve("main.toml"),
        )

        case_root = fixture_root / "pack_collision"
        source_root = case_root / "source"
        first_pack = case_root / "pack_one"
        second_pack = case_root / "pack_two"
        source_root.mkdir(parents=True)
        first_pack.mkdir(parents=True)
        second_pack.mkdir(parents=True)
        refusals += _record_read_only_refusal(
            failures,
            "activated pack case collision",
            case_root,
            lambda: ScenarioImportResolver(
                source_root,
                activated_pack_namespaces={
                    "AuditPack": first_pack,
                    "auditpack": second_pack,
                },
            ),
        )

        case_root = fixture_root / "unicode_collision"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_unicode_collision_root_v1",
                imports=(
                    ScenarioImportV1("A.toml"),
                    ScenarioImportV1("\uff21.toml"),
                ),
            ),
        )
        _write_document(
            source_root / "A.toml",
            _source_document_bytes("audit_unicode_collision_ascii_v1"),
        )
        _write_document(
            source_root / "\uff21.toml",
            _source_document_bytes("audit_unicode_collision_nfkc_v1"),
        )
        refusals += _record_read_only_refusal(
            failures,
            "Unicode logical path collision",
            case_root,
            lambda: ScenarioImportResolver(source_root).resolve("main.toml"),
        )

        case_root = fixture_root / "missing_target"
        source_root = case_root / "source"
        _write_document(
            source_root / "main.toml",
            _source_document_bytes(
                "audit_missing_target_root_v1",
                imports=(ScenarioImportV1("missing.toml"),),
            ),
        )
        refusals += _record_read_only_refusal(
            failures,
            "missing import target",
            case_root,
            lambda: ScenarioImportResolver(source_root).resolve("main.toml"),
        )

    if len(lexical_probes) + 10 != WO32B_IMPORT_REFUSAL_COUNT:
        failures.append("hostile import refusal inventory count changed")
    return ScenarioLanguageAuditCase(
        "scenario_hostile_import_graph_refusals",
        (
            f"refused={refusals}/{WO32B_IMPORT_REFUSAL_COUNT} "
            "network=false escape=false resolver_writes=false"
        ),
        tuple(failures),
    )


def _hostile_definition_resolution_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    refusals = 0
    with TemporaryDirectory(prefix="kirby2-wo32b-hostile-definitions-") as temp:
        fixture_root = Path(temp)

        def run_case(
            case_name: str,
            root_sections: dict[str, tuple[ScenarioRecordV1, ...]],
            *,
            imported_sections: dict[str, tuple[ScenarioRecordV1, ...]] | None = None,
            raw_root_mutator: Callable[[dict[str, object]], None] | None = None,
        ) -> None:
            nonlocal refusals
            case_root = fixture_root / case_name
            source_root = case_root / "source"
            imports = (
                (ScenarioImportV1("definitions.toml"),)
                if imported_sections is not None
                else ()
            )
            root_raw = _source_document_bytes(
                f"audit_{case_name}_root_v1",
                sections=root_sections,
                imports=imports,
            )
            if raw_root_mutator is not None:
                payload = _source_document_payload(
                    f"audit_{case_name}_root_v1",
                    sections=root_sections,
                    imports=imports,
                )
                raw_root_mutator(payload)
                root_raw = canonical_toml(payload).encode("utf-8")
            _write_document(source_root / "main.toml", root_raw)
            if imported_sections is not None:
                _write_document(
                    source_root / "definitions.toml",
                    _source_document_bytes(
                        f"audit_{case_name}_import_v1",
                        sections=imported_sections,
                    ),
                )
            refusals += _record_read_only_refusal(
                failures,
                case_name.replace("_", " "),
                case_root,
                lambda: resolve_scenario_bundle(source_root, "main.toml"),
            )

        base_market = _record(
            "base_market",
            fields=(_field("price", ScenarioValueKindV1.PRICE_TICKS, 10_000),),
        )
        run_case(
            "duplicate_definition",
            {"market_profile": (base_market,)},
            imported_sections={"market_profile": (base_market,)},
        )
        run_case(
            "definition_case_collision",
            {"market_profile": (_record("Base"),)},
            imported_sections={"market_profile": (_record("base"),)},
        )
        run_case(
            "unknown_parent",
            {
                "market_profile": (
                    _record("derived", extends="market:missing"),
                )
            },
        )
        run_case(
            "cross_type_inheritance",
            {
                "market_profile": (
                    _record("derived", extends="venue:base_venue"),
                ),
                "venues": (_record("base_venue"),),
            },
        )
        run_case(
            "inheritance_cycle",
            {
                "market_profile": (
                    _record("first", extends="market:second"),
                    _record("second", extends="market:first"),
                )
            },
        )
        run_case(
            "inherited_value_tag_change",
            {
                "market_profile": (
                    base_market,
                    _record(
                        "derived",
                        fields=(
                            _field("price", ScenarioValueKindV1.COUNT, 10_000),
                        ),
                        extends="market:base_market",
                    ),
                )
            },
        )
        run_case(
            "inheritance_in_nondefinition",
            {
                "flow_model": (
                    _record("flow", extends="market:base_market"),
                )
            },
        )
        run_case(
            "imported_runtime_behavior",
            {},
            imported_sections={"flow_model": (_record("imported_flow"),)},
        )

        def make_multiple_inheritance(payload: dict[str, object]) -> None:
            payload["market_profile"]["records"][0]["extends"] = [
                "market:first",
                "market:second",
            ]

        run_case(
            "multiple_inheritance",
            {
                "market_profile": (
                    _record("derived", extends="market:first"),
                )
            },
            raw_root_mutator=make_multiple_inheritance,
        )

    policies = tuple(DEFINITION_MERGE_POLICIES_V1.values())
    if set(DEFINITION_MERGE_POLICIES_V1) != set(ScenarioDefinitionTypeV1):
        failures.append("definition merge policy does not cover exactly seven types")
    if set(DEFINITION_SECTION_BY_TYPE_V1) != set(ScenarioDefinitionTypeV1):
        failures.append("definition section mapping does not cover exactly seven types")
    if len(ScenarioDefinitionTypeV1) != 7:
        failures.append("reusable definition type inventory is not exactly seven")
    if any(policy.scalar_mode != "KEYED_OVERRIDE" for policy in policies):
        failures.append("definition scalar merge policy is not explicit")
    list_modes = {
        mode: sum(policy.identifier_list_mode is mode for policy in policies)
        for mode in ScenarioListMergeModeV1
    }
    if list_modes != {
        ScenarioListMergeModeV1.REPLACE: 4,
        ScenarioListMergeModeV1.KEYED_MERGE: 3,
    }:
        failures.append("definition list replacement/keyed-merge policy changed")
    if refusals != WO32B_DEFINITION_REFUSAL_COUNT:
        failures.append("hostile definition refusal inventory count changed")
    return ScenarioLanguageAuditCase(
        "scenario_hostile_definition_refusals",
        (
            f"refused={refusals}/{WO32B_DEFINITION_REFUSAL_COUNT} "
            "definition_types=7 single_inheritance=true resolver_writes=false"
        ),
        tuple(failures),
    )


def _write_valid_import_fixture(
    root: Path,
    *,
    flattened: bool = False,
) -> tuple[Path, Path]:
    source_root = root / "source"
    pack_root = root / "pack"
    market_fields = (
        _field("decision_interval", ScenarioValueKindV1.DURATION_MS, 250),
        _field("initial_mid", ScenarioValueKindV1.PRICE_TICKS, 10_050),
        _field("symbols", ScenarioValueKindV1.IDENTIFIERS, ("CCC",)),
    )
    venue_fields = (
        _field("queue_model", ScenarioValueKindV1.IDENTIFIER, "FIFO"),
        _field(
            "supported_orders",
            ScenarioValueKindV1.IDENTIFIERS,
            ("LIMIT", "MARKET", "POST_ONLY"),
        ),
    )
    root_market = _record(
        "derived_market",
        fields=(
            market_fields
            if flattened
            else (
                _field("initial_mid", ScenarioValueKindV1.PRICE_TICKS, 10_050),
                _field("symbols", ScenarioValueKindV1.IDENTIFIERS, ("CCC",)),
            )
        ),
        reference=("base_market_reference" if flattened else None),
        extends=(None if flattened else "market:base_market"),
    )
    root_venue = _record(
        "derived_venue",
        fields=(
            venue_fields
            if flattened
            else (
                _field(
                    "supported_orders",
                    ScenarioValueKindV1.IDENTIFIERS,
                    ("POST_ONLY",),
                ),
            )
        ),
        reference="derived_venue_reference",
        extends=(None if flattened else "venue:base_venue"),
    )
    root_flow = _record(
        "root_flow",
        fields=(_field("message_rate", ScenarioValueKindV1.RATE_PER_SECOND, 40),),
    )
    _write_document(
        source_root / "main.toml",
        _source_document_bytes(
            "audit_nested_root_v1",
            sections={
                "market_profile": (root_market,),
                "venues": (root_venue,),
                "flow_model": (root_flow,),
            },
            imports=(ScenarioImportV1("defs/base.toml"),),
        ),
    )
    _write_document(
        source_root / "defs" / "base.toml",
        _source_document_bytes(
            "audit_nested_base_v1",
            sections={
                "market_profile": (
                    _record(
                        "base_market",
                        fields=(
                            _field(
                                "decision_interval",
                                ScenarioValueKindV1.DURATION_MS,
                                250,
                            ),
                            _field(
                                "initial_mid",
                                ScenarioValueKindV1.PRICE_TICKS,
                                10_000,
                            ),
                            _field(
                                "symbols",
                                ScenarioValueKindV1.IDENTIFIERS,
                                ("AAA", "BBB"),
                            ),
                        ),
                        reference="base_market_reference",
                    ),
                )
            },
            imports=(
                ScenarioImportV1("common/venue.toml", "audit_pack"),
            ),
        ),
    )
    _write_document(
        pack_root / "common" / "venue.toml",
        _source_document_bytes(
            "audit_nested_venue_v1",
            sections={
                "venues": (
                    _record(
                        "base_venue",
                        fields=(
                            _field(
                                "queue_model",
                                ScenarioValueKindV1.IDENTIFIER,
                                "FIFO",
                            ),
                            _field(
                                "supported_orders",
                                ScenarioValueKindV1.IDENTIFIERS,
                                ("LIMIT", "MARKET"),
                            ),
                        ),
                        reference="base_venue_reference",
                    ),
                )
            },
            imports=(ScenarioImportV1("latency.toml"),),
        ),
    )
    _write_document(
        pack_root / "common" / "latency.toml",
        _source_document_bytes(
            "audit_nested_latency_v1",
            sections={
                "latency": (
                    _record(
                        "base_latency",
                        fields=(
                            _field(
                                "routing_latency",
                                ScenarioValueKindV1.LATENCY_US,
                                125,
                            ),
                        ),
                    ),
                )
            },
        ),
    )
    return source_root, pack_root


def _source_document_bytes(
    scenario_id: str,
    *,
    sections: dict[str, tuple[ScenarioRecordV1, ...]] | None = None,
    imports: tuple[ScenarioImportV1, ...] = (),
) -> bytes:
    return canonical_toml(
        _source_document_payload(
            scenario_id,
            sections=sections,
            imports=imports,
        )
    ).encode("utf-8")


def _source_document_payload(
    scenario_id: str,
    *,
    sections: dict[str, tuple[ScenarioRecordV1, ...]] | None = None,
    imports: tuple[ScenarioImportV1, ...] = (),
) -> dict[str, object]:
    selected_sections = sections or {}
    unknown_sections = set(selected_sections).difference(
        SCENARIO_BEHAVIOR_SECTION_NAMES
    )
    if unknown_sections:
        raise ValueError(f"unknown audit fixture sections: {sorted(unknown_sections)}")
    empty = ScenarioSectionV1(())
    section_values = {
        name: ScenarioSectionV1(tuple(selected_sections.get(name, ())))
        if name in selected_sections
        else empty
        for name in SCENARIO_BEHAVIOR_SECTION_NAMES
    }
    source = ScenarioSourceV1(
        schema_version=1,
        metadata=replace(
            _sample_source().metadata,
            scenario_id=scenario_id,
            title="WO32-B audit fixture",
            description="Confined import and definition fixture",
        ),
        **section_values,
    )
    payload = source.as_dict()
    if imports:
        payload["imports"] = [item.as_dict() for item in imports]
    return payload


def _record(
    logical_name: str,
    *,
    fields: tuple[ScenarioFieldV1, ...] = (),
    reference: str | None = None,
    extends: str | None = None,
) -> ScenarioRecordV1:
    return ScenarioRecordV1(
        logical_name=logical_name,
        record_type="AUDIT_DEFINITION_V1",
        version=1,
        fields=fields,
        reference=reference,
        extends=extends,
    )


def _field(
    name: str,
    value_kind: ScenarioValueKindV1,
    value: object,
) -> ScenarioFieldV1:
    return ScenarioFieldV1(name, value_kind, value)


def _write_document(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _record_read_only_refusal(
    failures: list[str],
    label: str,
    fixture_root: Path,
    operation: Callable[[], object],
) -> int:
    before = _filesystem_snapshot(fixture_root)
    failure = _expect_refusal(operation, label)
    after = _filesystem_snapshot(fixture_root)
    if after != before:
        failures.append(f"{label} changed its fixture filesystem")
    if failure is not None:
        failures.append(failure)
        return 0
    return 1


def _filesystem_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        for name in sorted((*directory_names, *file_names)):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, "symlink", os.readlink(path)))
            elif path.is_dir():
                entries.append((relative, "directory", ""))
            elif path.is_file():
                entries.append(
                    (relative, "file", hashlib.sha256(path.read_bytes()).hexdigest())
                )
            else:
                entries.append((relative, "other", ""))
    return tuple(sorted(entries))


def _expect_refusal(operation: Callable[[], object], label: str) -> str | None:
    try:
        operation()
    except (TypeError, ValueError):
        return None
    return f"{label} was accepted"


__all__ = [
    "ScenarioLanguageAuditCase",
    "WO32A_STRICT_REFUSAL_COUNT",
    "WO32B_DEFINITION_REFUSAL_COUNT",
    "WO32B_IMPORT_REFUSAL_COUNT",
    "audit_scenario_language",
    "audit_wo32a_scenario_language",
    "audit_wo32b_scenario_language",
]
