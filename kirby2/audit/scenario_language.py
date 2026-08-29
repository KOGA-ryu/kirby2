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
from kirby2.scenario_lang.capabilities import (
    ScenarioCapabilityRequirementV1,
    scenario_capability_contract_digest_v1,
)
from kirby2.scenario_lang.compiler import (
    DEFAULT_SCENARIO_TARGET_REGISTRY,
    ScenarioExecutionRefused,
    ScenarioTargetRegistry,
    compile_resolved_scenario,
    compile_scenario,
    compile_validated_scenario,
    replay_compiled_scenario,
    run_compiled_scenario,
)
from kirby2.scenario_lang.defaults import (
    SCENARIO_SEED_POLICY_LOGICAL_NAME_V1,
    SCENARIO_SEED_POLICY_RECORD_TYPE_V1,
)
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
    SCENARIO_COMPILATION_PHASES_V1,
    SCENARIO_EXECUTION_ELIGIBLE_REASON_V1,
    SCENARIO_EXECUTION_INELIGIBLE_REASON_V1,
    SCENARIO_FINALIZED_COMPILATION_PHASES_V1,
    SCENARIO_PENDING_COMPILATION_PHASES_V1,
    SCENARIO_SOURCE_SECTION_NAMES,
    SCENARIO_TARGET_CONTRACTS_V1,
    SCENARIO_VALIDATION_FAMILIES_V1,
    ExactFixedPointV1,
    CompiledScenarioArtifactV1,
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
    ScenarioValidationReportV1,
    ScenarioValidationSeverityV1,
    ScenarioValueKindV1,
    VolumeMultiplierV1,
)
from kirby2.scenario_lang.resolution import resolve_scenario_bundle
from kirby2.scenario_lang.seeds import (
    derive_scenario_substream_seed,
    scenario_run_identity_digest,
)
from kirby2.scenario_lang.schema import (
    canonical_scenario_source_bytes,
    parse_canonical_scenario_source,
    parse_scenario_source,
    scenario_source_round_trip,
)
from kirby2.scenario_lang.validation import (
    ScenarioValidationRefused,
    finalize_compiled_scenario,
    validate_compiled_scenario,
)


WO32A_STRICT_REFUSAL_COUNT = 13
WO32B_IMPORT_REFUSAL_COUNT = 18
WO32B_DEFINITION_REFUSAL_COUNT = 9
WO32C_COMPILER_REFUSAL_COUNT = 15
WO32D_VALIDATION_FAMILY_COUNT = 12
WO32D_FINALIZATION_REFUSAL_COUNT = 8


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
        *audit_wo32c_scenario_language(),
        *audit_wo32d_scenario_language(),
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


def audit_wo32c_scenario_language() -> tuple[ScenarioLanguageAuditCase, ...]:
    """Exercise immutable compilation, target dispatch, and seed ownership."""

    return (
        _compiler_phase_and_artifact_case(),
        _compiler_determinism_case(),
        _compiler_seed_policy_case(),
        _target_registry_and_runtime_case(),
        _hostile_compiler_refusal_case(),
    )


def audit_wo32d_scenario_language() -> tuple[ScenarioLanguageAuditCase, ...]:
    """Exercise complete static validation and capability-bound finalization."""

    return (
        _validation_wo32abc_regression_case(),
        _validation_report_and_finalization_case(),
        _validation_family_diagnostics_case(),
        _validation_target_capability_matrix_case(),
        _validation_required_unknown_and_refusal_case(),
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


def _compiler_phase_and_artifact_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    with TemporaryDirectory(prefix="kirby2-wo32c-artifact-") as temp:
        source_root, pack_root, unused_pack = _write_compiler_fixture(Path(temp))
        artifact = compile_scenario(
            source_root,
            "main.toml",
            _balanced_native_scenario(),
            activated_pack_namespaces={
                "audit_pack": pack_root,
                "unused_pack": unused_pack,
            },
        )
        payload = artifact.as_dict()
        if tuple(payload["completed_phases"]) != SCENARIO_COMPILATION_PHASES_V1:
            failures.append("compiled artifact omitted or reordered a compiler phase")
        if tuple(payload["pending_phases"]) != (
            SCENARIO_PENDING_COMPILATION_PHASES_V1
        ):
            failures.append("compiled artifact did not retain capability validation")
        if artifact.execution_eligible or (
            artifact.execution_reason_code
            != SCENARIO_EXECUTION_INELIGIBLE_REASON_V1
        ):
            failures.append("WO32-C artifact did not fail closed")
        if payload["source_bundle_digest"] != artifact.source_bundle_digest:
            failures.append("artifact source digest property is inconsistent")
        if payload["semantic_plan_digest"] != artifact.semantic_plan_digest:
            failures.append("artifact semantic digest property is inconsistent")
        if payload["native_plan_digest"] != artifact.native_plan_digest:
            failures.append("artifact native digest property is inconsistent")
        if payload["compiled_artifact_digest"] != artifact.compiled_artifact_digest:
            failures.append("artifact self digest property is inconsistent")
        provenance = artifact.provenance
        import_graph = provenance["import_bundle"]
        if len(import_graph["documents"]) != 2 or len(import_graph["edges"]) != 1:
            failures.append("compiled provenance omitted the ordered import graph")
        if any(not row["raw_sha256"] for row in import_graph["documents"]):
            failures.append("compiled provenance omitted a source byte digest")

        materialized = artifact.materialized_plan
        applied_defaults = materialized["applied_defaults"]
        if len(applied_defaults) != 4:
            failures.append("empty source seed policy did not materialize four defaults")
        definitions = materialized["resolved_definitions"]
        if len(definitions) != 1:
            failures.append("compiler did not select exactly the root definition")
        else:
            record = definitions[0]["record"]
            if "extends" in record or "reference" in record:
                failures.append("compiled definition retained an unresolved source link")
            fields = {field["name"]: field for field in record["fields"]}
            if fields.get("decision_interval") != {
                "duration_us": 250_000,
                "name": "decision_interval",
            }:
                failures.append("compiler did not normalize duration_ms to duration_us")
        root = materialized["root_source"]
        flow_record = root["flow_model"]["records"][0]
        if "reference" in flow_record or "bound_reference" not in flow_record:
            failures.append("compiler did not fully bind the root record reference")
        declarations = payload["required_capability_declarations"]
        decisions = payload["capability_decisions"]
        if len(declarations) != 1 or len(decisions) != 1:
            failures.append("compiled artifact omitted required capability evidence")
        elif (
            decisions[0]["decision"] != "PENDING_VALIDATOR"
            or decisions[0]["declaration_id"]
            != declarations[0]["declaration_id"]
        ):
            failures.append("compiled capability decision is not pending and paired")
        if set(payload["adapter_operations"]) != {
            "parse",
            "persist",
            "replay",
            "run",
            "validate",
        }:
            failures.append("compiled target adapter inventory is incomplete")
        if artifact.plan_envelope.native_plan_digest != artifact.native_plan_digest:
            failures.append("embedded native plan digest differs from the artifact")
    return ScenarioLanguageAuditCase(
        "scenario_compiler_phase_and_artifact_inventory",
        (
            "phases=8 pending=CAPABILITY_VALIDATION defaults=4 "
            "references=bound units=normalized capabilities=pending"
        ),
        tuple(failures),
    )


def _compiler_determinism_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    with TemporaryDirectory(prefix="kirby2-wo32c-determinism-a-") as first_temp, (
        TemporaryDirectory(prefix="kirby2-wo32c-determinism-b-")
    ) as second_temp:
        first_root = Path(first_temp)
        second_root = Path(second_temp)
        first_source, first_pack, first_unused = _write_compiler_fixture(
            first_root,
            unrelated_id="audit_unrelated_first_v1",
        )
        second_source, second_pack, second_unused = _write_compiler_fixture(
            second_root,
            unrelated_id="audit_unrelated_second_v1",
        )
        (first_root / "ambient").mkdir()
        (second_root / "ambient").mkdir()
        _write_document(first_root / "ambient" / "definition.toml", b"first\n")
        _write_document(second_root / "ambient" / "definition.toml", b"second\n")
        before_first = _filesystem_snapshot(first_root)
        before_second = _filesystem_snapshot(second_root)
        prior_cwd = Path.cwd()
        try:
            os.chdir(first_root / "ambient")
            first = compile_scenario(
                first_source,
                "main.toml",
                _balanced_native_scenario(),
                activated_pack_namespaces={
                    "unused_pack": first_unused,
                    "audit_pack": first_pack,
                },
                warnings=("ZETA_AUDIT_WARNING", "ALPHA_AUDIT_WARNING"),
            )
            os.chdir(second_root / "ambient")
            second = compile_scenario(
                second_source,
                "main.toml",
                _balanced_native_scenario(),
                activated_pack_namespaces={
                    "audit_pack": second_pack,
                    "unused_pack": second_unused,
                },
                warnings=("ALPHA_AUDIT_WARNING", "ZETA_AUDIT_WARNING"),
            )
        finally:
            os.chdir(prior_cwd)
        repeated = compile_scenario(
            first_source,
            "main.toml",
            _balanced_native_scenario(),
            activated_pack_namespaces={
                "audit_pack": first_pack,
                "unused_pack": first_unused,
            },
            warnings=("ALPHA_AUDIT_WARNING", "ZETA_AUDIT_WARNING"),
        )
        if first.canonical_bytes() != second.canonical_bytes():
            failures.append("relocation, mapping order, or ambient files changed artifact bytes")
        if first.canonical_bytes() != repeated.canonical_bytes():
            failures.append("recompiling identical inputs changed artifact bytes")
        if first.compiled_artifact_digest != second.compiled_artifact_digest:
            failures.append("relocation changed compiled artifact identity")
        if _filesystem_snapshot(first_root) != before_first:
            failures.append("compiler wrote into the first source or ambient tree")
        if _filesystem_snapshot(second_root) != before_second:
            failures.append("compiler wrote into the second source or ambient tree")
    return ScenarioLanguageAuditCase(
        "scenario_compiler_determinism_and_ambient_independence",
        (
            "recompilation=byte_identical relocation=true mapping_order=true "
            "unrelated_definitions=true ambient_defaults=false writes=false"
        ),
        tuple(failures),
    )


def _compiler_seed_policy_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    with TemporaryDirectory(prefix="kirby2-wo32c-seeds-") as temp:
        source_root, pack_root, unused_pack = _write_compiler_fixture(
            Path(temp),
            allow_override=True,
        )
        packs = {"audit_pack": pack_root, "unused_pack": unused_pack}
        native = _balanced_native_scenario()
        source_selected = compile_scenario(
            source_root,
            "main.toml",
            native,
            activated_pack_namespaces=packs,
        )
        overridden = compile_scenario(
            source_root,
            "main.toml",
            _balanced_native_scenario(),
            activated_pack_namespaces=packs,
            cli_seed_override=99,
        )
        repeated_override = compile_scenario(
            source_root,
            "main.toml",
            _balanced_native_scenario(),
            activated_pack_namespaces=packs,
            cli_seed_override=99,
        )
        if source_selected.seed_policy.selected_root_seed != 17:
            failures.append("source-selected seed was not retained")
        if (
            overridden.seed_policy.selected_root_seed != 99
            or not overridden.seed_policy.cli_override_applied
            or not overridden.seed_policy.cli_override_allowed
        ):
            failures.append("permitted CLI seed override was not materialized")
        if overridden.run_identity_digest == source_selected.run_identity_digest:
            failures.append("selected seed did not enter run identity")
        if overridden.compiled_artifact_digest == source_selected.compiled_artifact_digest:
            failures.append("selected seed did not enter compiled artifact identity")
        if overridden.source_bundle_digest != source_selected.source_bundle_digest:
            failures.append("seed override changed source provenance identity")
        if overridden.semantic_plan_digest != source_selected.semantic_plan_digest:
            failures.append("seed override changed the source semantic plan")
        if overridden.native_plan_digest != source_selected.native_plan_digest:
            failures.append("seed override changed the native plan")
        if overridden.canonical_bytes() != repeated_override.canonical_bytes():
            failures.append("same CLI seed override did not reproduce artifact bytes")
        if overridden.run_identity_digest != scenario_run_identity_digest(
            overridden.native_plan_digest,
            overridden.seed_policy,
        ):
            failures.append("artifact run identity differs from the V1 derivation")
        for substream in overridden.seed_policy.substreams:
            expected = derive_scenario_substream_seed(
                99,
                overridden.seed_policy.policy_version,
                substream.semantic_path,
            )
            if substream.derived_seed != expected:
                failures.append(
                    f"substream {substream.semantic_path} has the wrong derived seed"
                )
    return ScenarioLanguageAuditCase(
        "scenario_compiler_seed_override_and_substreams",
        (
            "source_seed=17 override_seed=99 policy_version=1 "
            "substreams=2 run_identity=seed_bound"
        ),
        tuple(failures),
    )


def _target_registry_and_runtime_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    payloads = _compiler_native_payloads()
    direct_run_refusals = 0
    for target_kind in ScenarioTargetKindV1:
        adapter = DEFAULT_SCENARIO_TARGET_REGISTRY.adapter(target_kind)
        native = payloads[target_kind]
        persisted = adapter.persist(native)
        restored = adapter.replay(persisted)
        parsed = adapter.parse(native.as_dict())
        if adapter.validate(restored) != persisted or adapter.persist(parsed) != persisted:
            failures.append(f"{target_kind.value} adapter did not round trip")
        try:
            adapter.run(
                native,
                _compiler_seed_policy_fixture(),
            )
        except ScenarioExecutionRefused as error:
            if error.reason_code == SCENARIO_EXECUTION_INELIGIBLE_REASON_V1:
                direct_run_refusals += 1
            else:
                failures.append(f"{target_kind.value} refused with the wrong reason")
        else:
            failures.append(f"{target_kind.value} native run adapter executed early")

    duplicate_registry = ScenarioTargetRegistry()
    first_adapter = DEFAULT_SCENARIO_TARGET_REGISTRY.adapter(
        ScenarioTargetKindV1.FULL_DAY_PLAN_V1
    )
    duplicate_registry.register(first_adapter)
    refusal = _expect_refusal(
        lambda: duplicate_registry.register(first_adapter),
        "duplicate target adapter",
    )
    if refusal is not None:
        failures.append(refusal)
    refusal = _expect_refusal(
        lambda: DEFAULT_SCENARIO_TARGET_REGISTRY.register(first_adapter),
        "sealed target registry mutation",
    )
    if refusal is not None:
        failures.append(refusal)

    with TemporaryDirectory(prefix="kirby2-wo32c-runtime-") as temp:
        source_root, pack_root, unused_pack = _write_compiler_fixture(Path(temp))
        native = _balanced_native_scenario()
        artifact = compile_scenario(
            source_root,
            "main.toml",
            native,
            activated_pack_namespaces={
                "audit_pack": pack_root,
                "unused_pack": unused_pack,
            },
        )
        original_bytes = artifact.canonical_bytes()
        native.parameter_overrides["caller_mutation"] = 1.0
        detached_native = artifact.plan_envelope.payload
        detached_native.parameter_overrides["detached_mutation"] = 2.0
        detached_artifact = artifact.as_dict()
        detached_artifact["warnings"].append("DETACHED_MUTATION")
        if artifact.canonical_bytes() != original_bytes:
            failures.append("runtime/native caller mutation changed immutable artifact")
        try:
            artifact.execution_eligible = True
        except (AttributeError, TypeError):
            pass
        else:
            failures.append("compiled artifact accepted direct mutation")
        restored = replay_compiled_scenario(original_bytes)
        if restored != artifact or restored.canonical_bytes() != original_bytes:
            failures.append("compiled artifact replay was not byte-identical")
        before = _filesystem_snapshot(Path(temp))
        try:
            run_compiled_scenario(artifact)
        except ScenarioExecutionRefused as error:
            if error.reason_code != SCENARIO_EXECUTION_INELIGIBLE_REASON_V1:
                failures.append("compiled runtime refusal used the wrong reason code")
        else:
            failures.append("unvalidated compiled artifact reached a runtime adapter")
        if _filesystem_snapshot(Path(temp)) != before:
            failures.append("refused runtime attempt wrote to the fixture tree")
    if direct_run_refusals != len(ScenarioTargetKindV1):
        failures.append("one or more direct target run adapters did not fail closed")
    return ScenarioLanguageAuditCase(
        "scenario_target_registry_and_fail_closed_runtime",
        (
            f"targets={len(payloads)} operations=5 direct_run_refusals="
            f"{direct_run_refusals}/5 immutable=true runtime_writes=false"
        ),
        tuple(failures),
    )


def _hostile_compiler_refusal_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    refusals = 0
    native = _balanced_native_scenario()
    with TemporaryDirectory(prefix="kirby2-wo32c-hostile-") as temp:
        fixture_root = Path(temp)

        def source_refusal(
            case_name: str,
            sections: dict[str, tuple[ScenarioRecordV1, ...]],
            *,
            native_payload: object | None = None,
            cli_seed_override: int | None = None,
            target_registry: ScenarioTargetRegistry = DEFAULT_SCENARIO_TARGET_REGISTRY,
        ) -> None:
            nonlocal refusals
            case_root = fixture_root / case_name
            source_root = case_root / "source"
            _write_document(
                source_root / "main.toml",
                _source_document_bytes(
                    f"audit_compiler_{case_name}_v1",
                    sections=sections,
                ),
            )
            refusals += _record_read_only_refusal(
                failures,
                case_name.replace("_", " "),
                case_root,
                lambda: compile_scenario(
                    source_root,
                    "main.toml",
                    native if native_payload is None else native_payload,
                    cli_seed_override=cli_seed_override,
                    target_registry=target_registry,
                ),
            )

        source_refusal(
            "unsafe_expression_record",
            {
                "flow_model": (
                    _record("unsafe", record_type="PYTHON-EXPRESSION-V1"),
                )
            },
        )
        source_refusal(
            "unsafe_python_field",
            {
                "flow_model": (
                    _record(
                        "unsafe",
                        fields=(
                            _field(
                                "python_symbol",
                                ScenarioValueKindV1.TEXT,
                                "os.system",
                            ),
                        ),
                    ),
                )
            },
        )
        source_refusal(
            "unknown_reference",
            {"flow_model": (_record("flow", reference="market:missing"),)},
        )
        source_refusal(
            "reference_cycle",
            {
                "market_profile": (
                    _record("first", reference="market:second"),
                    _record("second", reference="market:first"),
                )
            },
        )
        source_refusal(
            "multiple_seed_policies",
            {
                "seed_policy": (
                    _seed_policy_record(),
                    _seed_policy_record(logical_name="alternate_seed_policy"),
                )
            },
        )
        source_refusal(
            "wrong_seed_value_tag",
            {
                "seed_policy": (
                    _seed_policy_record(
                        extra_fields=(
                            _field("root_seed", ScenarioValueKindV1.COUNT, 7),
                        ),
                    ),
                )
            },
        )
        source_refusal(
            "unsupported_seed_policy_version",
            {
                "seed_policy": (
                    _seed_policy_record(
                        extra_fields=(
                            _field(
                                "policy_version",
                                ScenarioValueKindV1.VERSION,
                                2,
                            ),
                        ),
                    ),
                )
            },
        )
        source_refusal(
            "denied_cli_seed_override",
            {},
            cli_seed_override=9,
        )
        source_refusal(
            "boolean_cli_seed_override",
            {
                "seed_policy": (
                    _seed_policy_record(
                        allow_override=True,
                    ),
                )
            },
            cli_seed_override=True,
        )
        from kirby2.audit.full_day import _sample_plan

        source_refusal(
            "wrong_native_payload_type",
            {},
            native_payload=_sample_plan(),
        )
        source_refusal(
            "incomplete_target_registry",
            {},
            target_registry=ScenarioTargetRegistry(),
        )

        valid_root, valid_pack, valid_unused = _write_compiler_fixture(
            fixture_root / "artifact_tampering"
        )
        artifact = compile_scenario(
            valid_root,
            "main.toml",
            _balanced_native_scenario(),
            activated_pack_namespaces={
                "audit_pack": valid_pack,
                "unused_pack": valid_unused,
            },
        )

        def tampered_artifact(
            label: str,
            mutator: Callable[[dict[str, object]], None],
        ) -> None:
            nonlocal refusals
            payload = artifact.as_dict()
            mutator(payload)
            failure = _expect_refusal(
                lambda: CompiledScenarioArtifactV1(
                    canonical_semantic_plan_bytes(payload)
                ),
                label,
            )
            if failure is None:
                refusals += 1
            else:
                failures.append(failure)

        tampered_artifact(
            "forged execution eligibility",
            lambda payload: payload.__setitem__("execution_eligible", True),
        )
        tampered_artifact(
            "forged compiled artifact digest",
            lambda payload: payload.__setitem__(
                "compiled_artifact_digest",
                "0" * 64,
            ),
        )
        tampered_artifact(
            "noncanonical native envelope",
            lambda payload: payload.__setitem__(
                "native_plan_envelope_json",
                str(payload["native_plan_envelope_json"]) + " ",
            ),
        )
        duplicate_key_raw = b'{"schema_version":1,' + artifact.canonical_bytes()[1:]
        failure = _expect_refusal(
            lambda: CompiledScenarioArtifactV1(duplicate_key_raw),
            "duplicate artifact JSON key",
        )
        if failure is None:
            refusals += 1
        else:
            failures.append(failure)

    if refusals != WO32C_COMPILER_REFUSAL_COUNT:
        failures.append("hostile compiler refusal inventory count changed")
    return ScenarioLanguageAuditCase(
        "scenario_compiler_hostile_refusals",
        (
            f"refused={refusals}/{WO32C_COMPILER_REFUSAL_COUNT} "
            "expressions=false dynamic_python=false mutation=false early_run=false"
        ),
        tuple(failures),
    )


def _validation_wo32abc_regression_case() -> ScenarioLanguageAuditCase:
    cases = (
        *audit_wo32a_scenario_language(),
        *audit_wo32b_scenario_language(),
        *audit_wo32c_scenario_language(),
    )
    failures = tuple(
        f"{case.name}: {failure}"
        for case in cases
        for failure in case.failures
    )
    return ScenarioLanguageAuditCase(
        "scenario_validation_wo32abc_regression",
        (
            f"prior_cases={len(cases)} passing="
            f"{sum(not case.failures for case in cases)}"
        ),
        failures,
    )


def _validation_report_and_finalization_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    requirement = ScenarioCapabilityRequirementV1(
        declaration_id="top_of_book_required",
        capability_id="TOP_OF_BOOK_V1",
        required=True,
        source_location=(
            "root_source.required_source_capabilities.records"
            "[top_of_book_required]"
        ),
    )
    with TemporaryDirectory(prefix="kirby2-wo32d-finalization-") as temp:
        source_root = _write_validation_fixture(
            Path(temp),
            sections={
                "required_source_capabilities": (
                    _record(
                        "top_of_book_required",
                        record_type="CAPABILITY_REQUIREMENT",
                        fields=(
                            _field(
                                "capability",
                                ScenarioValueKindV1.IDENTIFIER,
                                "TOP_OF_BOOK_V1",
                            ),
                        ),
                    ),
                )
            },
            requirements=(requirement,),
        )
        native = replace(_balanced_native_scenario(), duration_seconds=1)
        unvalidated = compile_scenario(source_root, "main.toml", native)
        report = validate_compiled_scenario(unvalidated)
        repeated_report = validate_compiled_scenario(unvalidated)
        if not report.passed or report.error_count != 0:
            failures.append("valid compiled scenario did not produce a passing report")
        if report.canonical_bytes() != repeated_report.canonical_bytes():
            failures.append("repeated static validation changed report bytes")
        if report.completed_families != SCENARIO_VALIDATION_FAMILIES_V1:
            failures.append("validation report omitted or reordered a family")
        if len(report.capability_decisions) != 1 or (
            report.capability_decisions[0].decision.value != "SUPPORTED"
        ):
            failures.append("required top-of-book capability was not supported")
        finalized = finalize_compiled_scenario(unvalidated, report)
        direct = compile_validated_scenario(
            source_root,
            "main.toml",
            replace(_balanced_native_scenario(), duration_seconds=1),
        )
        if finalized.canonical_bytes() != direct.canonical_bytes():
            failures.append("direct validated compilation changed finalized bytes")
        if (
            not finalized.execution_eligible
            or finalized.execution_reason_code
            != SCENARIO_EXECUTION_ELIGIBLE_REASON_V1
            or tuple(finalized.as_dict()["completed_phases"])
            != SCENARIO_FINALIZED_COMPILATION_PHASES_V1
            or finalized.as_dict()["pending_phases"]
        ):
            failures.append("passing report did not finalize exact execution state")
        if finalized.validation_report != report or (
            finalized.validation_report_digest
            != report.validation_report_digest
        ):
            failures.append("finalized artifact did not bind the exact report")
        for name in (
            "source_bundle_digest",
            "semantic_plan_digest",
            "native_plan_digest",
            "run_identity_digest",
        ):
            if getattr(finalized, name) != getattr(unvalidated, name):
                failures.append(f"validation finalization changed {name}")
        if finalized.compiled_artifact_digest == unvalidated.compiled_artifact_digest:
            failures.append("validation report did not enter compiled artifact identity")
        restored = replay_compiled_scenario(finalized.canonical_bytes())
        if restored != finalized or restored.validation_report != report:
            failures.append("validation-finalized artifact did not replay exactly")
        before_run = _filesystem_snapshot(Path(temp))
        run = run_compiled_scenario(finalized)
        if (
            getattr(run, "seed", None)
            != finalized.seed_policy.selected_root_seed
            or getattr(run, "duration_seconds", None) != 1
        ):
            failures.append("validated artifact did not reach its native run adapter")
        if _filesystem_snapshot(Path(temp)) != before_run:
            failures.append("validated in-memory runtime wrote to the fixture tree")
        detached = finalized.validation_report.as_dict()
        detached["findings"].append({"detached": True})
        if finalized.canonical_bytes() != direct.canonical_bytes():
            failures.append("detached report mutation changed finalized artifact")
    return ScenarioLanguageAuditCase(
        "scenario_validation_report_and_finalization",
        (
            f"families={len(SCENARIO_VALIDATION_FAMILIES_V1)} "
            "report=canonical subject_digest=bound execution_eligible=true "
            "runtime_handoff=true"
        ),
        tuple(failures),
    )


def _validation_family_diagnostics_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    family_fixtures = _validation_family_fixtures()
    observed: dict[str, str] = {}
    with TemporaryDirectory(prefix="kirby2-wo32d-families-") as temp:
        fixture_root = Path(temp)
        for ordinal, (family, expected_code, sections, valid_digest) in enumerate(
            family_fixtures,
            start=1,
        ):
            case_root = fixture_root / f"family-{ordinal:02d}"
            source_root = _write_validation_fixture(
                case_root,
                sections=sections,
                valid_capability_digest=valid_digest,
            )
            artifact = compile_scenario(
                source_root,
                "main.toml",
                _balanced_native_scenario(),
            )
            before = _filesystem_snapshot(case_root)
            first = validate_compiled_scenario(artifact)
            second = validate_compiled_scenario(artifact)
            after = _filesystem_snapshot(case_root)
            if before != after:
                failures.append(f"{family} validation wrote to its fixture tree")
            if first.canonical_bytes() != second.canonical_bytes():
                failures.append(f"{family} diagnostics were not byte-stable")
            matching = tuple(
                item
                for item in first.findings
                if item.family == family and item.code == expected_code
            )
            if len(matching) != 1:
                failures.append(
                    f"{family} did not emit exactly one {expected_code} diagnostic"
                )
                continue
            finding = matching[0]
            if (
                not finding.source_location
                or finding.suggested_correction is None
                or not finding.blocks_execution
                or first.passed
            ):
                failures.append(f"{family} diagnostic was not useful and blocking")
            observed[family] = finding.code
    expected_families = set(SCENARIO_VALIDATION_FAMILIES_V1)
    if set(observed) != expected_families:
        failures.append(
            "validation fixture matrix did not cover every fixed family: "
            f"missing={sorted(expected_families.difference(observed))}"
        )
    if len(family_fixtures) != WO32D_VALIDATION_FAMILY_COUNT:
        failures.append("WO32-D validation family fixture count changed")
    return ScenarioLanguageAuditCase(
        "scenario_validation_family_diagnostics",
        (
            f"families={len(family_fixtures)} stable_paths={len(observed)} "
            "corrections=present writes=false"
        ),
        tuple(failures),
    )


def _validation_target_capability_matrix_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    payloads = _compiler_native_payloads()
    validated_kinds: list[str] = []
    with TemporaryDirectory(prefix="kirby2-wo32d-targets-") as temp:
        root = Path(temp)
        for target_kind in ScenarioTargetKindV1:
            sections: dict[str, tuple[ScenarioRecordV1, ...]] = {}
            if target_kind is ScenarioTargetKindV1.FULL_DAY_PLAN_V1:
                sections["seed_policy"] = (
                    _seed_policy_record(
                        extra_fields=(
                            _field(
                                "root_seed",
                                ScenarioValueKindV1.SEED,
                                payloads[target_kind].seed_policy.root_seed,
                            ),
                        ),
                    ),
                )
            source_root = _write_validation_fixture(
                root / target_kind.value.lower(),
                sections=sections,
                target_kind=target_kind,
            )
            artifact = compile_scenario(
                source_root,
                "main.toml",
                payloads[target_kind],
            )
            report = validate_compiled_scenario(artifact)
            if not report.passed:
                failures.append(
                    f"{target_kind.value} separately tagged target failed: "
                    f"{[item.code for item in report.findings]}"
                )
                continue
            finalized = finalize_compiled_scenario(artifact, report)
            if (
                finalized.target_kind is not target_kind
                or finalized.plan_envelope.target_kind is not target_kind
                or type(finalized.plan_envelope.payload)
                is not type(payloads[target_kind])
            ):
                failures.append(f"{target_kind.value} was silently coerced")
                continue
            validated_kinds.append(target_kind.value)
    if tuple(validated_kinds) != tuple(kind.value for kind in ScenarioTargetKindV1):
        failures.append("not every closed target validated independently")
    return ScenarioLanguageAuditCase(
        "scenario_validation_target_capability_matrix",
        (
            f"targets={len(validated_kinds)}/5 persist_replay=true "
            "coercions=false separately_tagged=true"
        ),
        tuple(failures),
    )


def _validation_required_unknown_and_refusal_case() -> ScenarioLanguageAuditCase:
    failures: list[str] = []
    refusals = 0
    with TemporaryDirectory(prefix="kirby2-wo32d-refusals-") as temp:
        root = Path(temp)
        unknown_root = _write_validation_fixture(
            root / "required-unknown",
            sections={
                "strategy": (
                    _record(
                        "general_strategy",
                        record_type="STRATEGY_V1",
                        fields=(
                            _field(
                                "requires_general_proof",
                                ScenarioValueKindV1.FLAG,
                                True,
                            ),
                        ),
                    ),
                )
            },
        )
        unknown_artifact = compile_scenario(
            unknown_root,
            "main.toml",
            _balanced_native_scenario(),
        )
        unknown_report = validate_compiled_scenario(unknown_artifact)
        blocking_unknown = tuple(
            item
            for item in unknown_report.findings
            if item.severity
            is ScenarioValidationSeverityV1.NOT_PROVABLE_STATICALLY
            and item.required
        )
        if (
            len(blocking_unknown) != 1
            or unknown_report.passed
            or unknown_report.blocking_not_provable_count != 1
        ):
            failures.append("required unknown proof was translated into a pass")

        optional_requirement = ScenarioCapabilityRequirementV1(
            declaration_id="optional_unknown",
            capability_id="FUTURE_UNDECLARED_CAPABILITY_V1",
            required=False,
            source_location=(
                "root_source.required_source_capabilities.records"
                "[optional_unknown]"
            ),
        )
        optional_root = _write_validation_fixture(
            root / "optional-unknown",
            sections={
                "required_source_capabilities": (
                    _record(
                        "optional_unknown",
                        record_type="CAPABILITY_REQUIREMENT",
                        fields=(
                            _field(
                                "capability",
                                ScenarioValueKindV1.IDENTIFIER,
                                "FUTURE_UNDECLARED_CAPABILITY_V1",
                            ),
                            _field(
                                "required",
                                ScenarioValueKindV1.FLAG,
                                False,
                            ),
                        ),
                    ),
                )
            },
            requirements=(optional_requirement,),
        )
        optional_artifact = compile_scenario(
            optional_root,
            "main.toml",
            _balanced_native_scenario(),
        )
        optional_report = validate_compiled_scenario(optional_artifact)
        if (
            not optional_report.passed
            or len(optional_report.capability_decisions) != 1
            or optional_report.capability_decisions[0].decision.value
            != "UNSUPPORTED"
        ):
            failures.append("optional unknown capability was hidden or made blocking")
        finalized_optional = finalize_compiled_scenario(
            optional_artifact,
            optional_report,
        )

        valid_root = _write_validation_fixture(root / "valid", sections={})
        valid_artifact = compile_scenario(
            valid_root,
            "main.toml",
            _balanced_native_scenario(),
        )
        valid_report = validate_compiled_scenario(valid_artifact)
        finalized = finalize_compiled_scenario(valid_artifact, valid_report)
        alternate_root = _write_validation_fixture(
            root / "alternate",
            sections={"flow_model": (_record("alternate_flow"),)},
        )
        alternate_artifact = compile_scenario(
            alternate_root,
            "main.toml",
            _balanced_native_scenario(),
        )

        probes: list[tuple[str, Callable[[], object]]] = [
            (
                "failing report finalization",
                lambda: finalize_compiled_scenario(
                    unknown_artifact,
                    unknown_report,
                ),
            ),
            (
                "cross-artifact validation report",
                lambda: finalize_compiled_scenario(
                    alternate_artifact,
                    valid_report,
                ),
            ),
            (
                "eligibility without report",
                lambda: CompiledScenarioArtifactV1(
                    _tampered_final_artifact_bytes(
                        valid_artifact,
                        lambda payload: payload.update(
                            {
                                "completed_phases": list(
                                    SCENARIO_FINALIZED_COMPILATION_PHASES_V1
                                ),
                                "execution_eligible": True,
                                "execution_reason_code": (
                                    SCENARIO_EXECUTION_ELIGIBLE_REASON_V1
                                ),
                                "pending_phases": [],
                            }
                        ),
                    )
                ),
            ),
            (
                "forged validation report digest",
                lambda: CompiledScenarioArtifactV1(
                    _tampered_final_artifact_bytes(
                        finalized,
                        lambda payload: payload.__setitem__(
                            "validation_report_digest",
                            "0" * 64,
                        ),
                    )
                ),
            ),
            (
                "noncanonical validation report JSON",
                lambda: CompiledScenarioArtifactV1(
                    _tampered_final_artifact_bytes(
                        finalized,
                        lambda payload: payload.__setitem__(
                            "validation_report_json",
                            str(payload["validation_report_json"]) + " ",
                        ),
                    )
                ),
            ),
            (
                "pending decision in finalized artifact",
                lambda: CompiledScenarioArtifactV1(
                    _tampered_final_artifact_bytes(
                        finalized_optional,
                        lambda payload: payload.__setitem__(
                            "capability_decisions",
                            optional_artifact.as_dict()["capability_decisions"],
                        ),
                    )
                ),
            ),
            (
                "validator-bypassing passing report",
                lambda: finalize_compiled_scenario(
                    unknown_artifact,
                    _forged_passing_validation_report(unknown_report),
                ),
            ),
        ]
        for label, operation in probes:
            failure = _expect_refusal(operation, label)
            if failure is None:
                refusals += 1
            else:
                failures.append(failure)
        try:
            run_compiled_scenario(valid_artifact)
        except ScenarioExecutionRefused as error:
            if error.reason_code == SCENARIO_EXECUTION_INELIGIBLE_REASON_V1:
                refusals += 1
            else:
                failures.append("unvalidated runtime refused with wrong reason")
        else:
            failures.append("unvalidated artifact reached a target runtime")
        if not finalized_optional.execution_eligible:
            failures.append("optional unsupported capability could not finalize")
    if refusals != WO32D_FINALIZATION_REFUSAL_COUNT:
        failures.append("WO32-D finalization refusal inventory count changed")
    return ScenarioLanguageAuditCase(
        "scenario_validation_required_unknown_and_refusals",
        (
            f"required_unknown=blocked optional_unknown=explicit "
            f"refused={refusals}/{WO32D_FINALIZATION_REFUSAL_COUNT}"
        ),
        tuple(failures),
    )


def _validation_family_fixtures() -> tuple[
    tuple[
        str,
        str,
        dict[str, tuple[ScenarioRecordV1, ...]],
        bool,
    ],
    ...,
]:
    return (
        (
            "SESSION_AUCTION_HALT",
            "INVALID_TIME_RANGE",
            {
                "session_schedule": (
                    _record(
                        "bad_window",
                        record_type="SESSION_WINDOW_V1",
                        fields=(
                            _field("start_us", ScenarioValueKindV1.LATENCY_US, 20),
                            _field("end_us", ScenarioValueKindV1.LATENCY_US, 10),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "STATE_GRAPH_REACHABILITY",
            "UNREACHABLE_STRATEGY_STATE",
            {
                "day_local_states": (
                    _record(
                        "initial_state",
                        record_type="STATE_V1",
                        fields=(
                            _field("initial", ScenarioValueKindV1.FLAG, True),
                        ),
                    ),
                    _record(
                        "orphan_state",
                        record_type="STATE_V1",
                        fields=(
                            _field("initial", ScenarioValueKindV1.FLAG, False),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "TRANSITION_NUMERICS",
            "INVALID_TRANSITION_WEIGHT",
            {
                "transition_rules": (
                    _record(
                        "zero_weight",
                        record_type="STATE_TRANSITION_V1",
                        fields=(
                            _field(
                                "from_state",
                                ScenarioValueKindV1.IDENTIFIER,
                                "first",
                            ),
                            _field(
                                "to_state",
                                ScenarioValueKindV1.IDENTIFIER,
                                "second",
                            ),
                            _field(
                                "probability_weight",
                                ScenarioValueKindV1.COUNT,
                                0,
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "HAWKES_STABILITY",
            "HAWKES_PROFILE_UNKNOWN",
            {
                "flow_model": (
                    _record(
                        "unstable_hawkes",
                        record_type="HAWKES_FLOW_V1",
                        fields=(
                            _field(
                                "accepted_profile",
                                ScenarioValueKindV1.IDENTIFIER,
                                "not_accepted",
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "VENUE_INSTRUMENT_COMPATIBILITY",
            "UNSUPPORTED_ORDER_INSTRUCTION",
            {
                "venues": (
                    _record(
                        "venue_a",
                        record_type="VENUE_V1",
                        fields=(
                            _field(
                                "supported_order_instructions",
                                ScenarioValueKindV1.IDENTIFIERS,
                                ("LIMIT", "PEGGED_TO_FUTURE"),
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "LATENCY_REPLAY_COMPATIBILITY",
            "EXACT_REPLAY_REQUIRES_STRONGER_DATA",
            {
                "latency": (
                    _record(
                        "weak_replay",
                        record_type="RECORDED_LATENCY_V1",
                        fields=(
                            _field(
                                "replay_mode",
                                ScenarioValueKindV1.IDENTIFIER,
                                "EXACT_REPLAY",
                            ),
                            _field(
                                "source_capability",
                                ScenarioValueKindV1.IDENTIFIER,
                                "BARS_ONLY",
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "FEATURE_OBSERVABILITY",
            "HIDDEN_TRUTH_EXPOSED",
            {
                "strategy": (
                    _record(
                        "truth_strategy",
                        record_type="STRATEGY_V1",
                        fields=(
                            _field(
                                "required_features",
                                ScenarioValueKindV1.IDENTIFIERS,
                                ("GROUND_TRUTH",),
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "STRATEGY_NO_LOOKAHEAD",
            "FUTURE_INFORMATION_EXPOSED",
            {
                "strategy": (
                    _record(
                        "future_strategy",
                        record_type="STRATEGY_V1",
                        fields=(
                            _field(
                                "future_offset_us",
                                ScenarioValueKindV1.LATENCY_US,
                                1,
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "RESOURCE_LIMITS",
            "AGENT_BUDGET_UNBOUNDED",
            {
                "agent_populations": (
                    _record(
                        "unbounded_agents",
                        record_type="AGENT_POPULATION_V1",
                        fields=(
                            _field(
                                "agent_count",
                                ScenarioValueKindV1.COUNT,
                                10,
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "CHECKPOINT_ADAPTERS",
            "CHECKPOINT_ADAPTER_UNSUPPORTED",
            {
                "checkpoint_policy": (
                    _record(
                        "unsupported_checkpoint",
                        record_type="CHECKPOINT_POLICY_V1",
                        fields=(
                            _field(
                                "required_adapters",
                                ScenarioValueKindV1.IDENTIFIERS,
                                ("PYTHON_PICKLE_RUNTIME",),
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "HISTORICAL_CAPABILITY",
            "HISTORICAL_MBO_REQUIRES_MARKET_BY_ORDER",
            {
                "historical_constraints": (
                    _record(
                        "weak_historical_source",
                        record_type="HISTORICAL_SOURCE_V1",
                        fields=(
                            _field(
                                "replay_mode",
                                ScenarioValueKindV1.IDENTIFIER,
                                "EXACT_REPLAY",
                            ),
                            _field(
                                "requires_market_by_order",
                                ScenarioValueKindV1.FLAG,
                                True,
                            ),
                            _field(
                                "source_capability",
                                ScenarioValueKindV1.IDENTIFIER,
                                "TRADES_AND_QUOTES",
                            ),
                        ),
                    ),
                )
            },
            True,
        ),
        (
            "TARGET_CAPABILITY_CONTRACT",
            "CAPABILITY_DIGEST_MISMATCH",
            {},
            False,
        ),
    )


def _write_validation_fixture(
    root: Path,
    *,
    sections: dict[str, tuple[ScenarioRecordV1, ...]],
    target_kind: ScenarioTargetKindV1 = ScenarioTargetKindV1.MARKET_SCENARIO_V1,
    requirements: tuple[ScenarioCapabilityRequirementV1, ...] = (),
    valid_capability_digest: bool = True,
) -> Path:
    source_root = root / "source"
    capability_digest = (
        scenario_capability_contract_digest_v1(target_kind, requirements)
        if valid_capability_digest
        else "f" * 64
    )
    _write_document(
        source_root / "main.toml",
        _source_document_bytes(
            f"audit_validation_{root.name}_v1",
            sections=sections,
            target_kind=target_kind,
            capability_digest=capability_digest,
        ),
    )
    return source_root


def _tampered_final_artifact_bytes(
    artifact: CompiledScenarioArtifactV1,
    mutator: Callable[[dict[str, object]], None],
) -> bytes:
    payload = artifact.as_dict()
    mutator(payload)
    provenance = payload["provenance"]
    if not isinstance(provenance, dict):
        raise TypeError("audit compiled provenance must be an object")
    body = dict(payload)
    del body["compiled_artifact_digest"]
    del body["provenance"]
    payload["compiled_artifact_digest"] = compiled_artifact_digest(
        canonical_semantic_plan_bytes(body),
        provenance,
    )
    return canonical_semantic_plan_bytes(payload)


def _forged_passing_validation_report(
    report: ScenarioValidationReportV1,
) -> ScenarioValidationReportV1:
    payload = report.as_dict()
    payload.update(
        {
            "blocking_not_provable_count": 0,
            "error_count": 0,
            "findings": [],
            "not_provable_count": 0,
            "passed": True,
            "warning_count": 0,
        }
    )
    return ScenarioValidationReportV1.from_dict(payload)


def _write_compiler_fixture(
    root: Path,
    *,
    allow_override: bool = False,
    unrelated_id: str = "audit_unrelated_definition_v1",
) -> tuple[Path, Path, Path]:
    source_root = root / "source"
    pack_root = root / "pack"
    unused_pack = root / "unused_pack"
    seed_records: tuple[ScenarioRecordV1, ...] = ()
    if allow_override:
        seed_records = (
            _seed_policy_record(
                allow_override=True,
                extra_fields=(
                    _field("root_seed", ScenarioValueKindV1.SEED, 17),
                    _field(
                        "substreams",
                        ScenarioValueKindV1.IDENTIFIERS,
                        (
                            "scenario/market/analysis",
                            "scenario/market/runtime",
                        ),
                    ),
                ),
            ),
        )
    _write_document(
        source_root / "main.toml",
        _source_document_bytes(
            "audit_compiler_root_v1",
            sections={
                "market_profile": (
                    _record(
                        "derived_market",
                        fields=(
                            _field(
                                "initial_mid",
                                ScenarioValueKindV1.PRICE_TICKS,
                                10_050,
                            ),
                        ),
                        extends="market:base_market",
                    ),
                ),
                "flow_model": (
                    _record(
                        "root_flow",
                        fields=(
                            _field(
                                "message_rate",
                                ScenarioValueKindV1.RATE_PER_SECOND,
                                40,
                            ),
                        ),
                        reference="market:derived_market",
                    ),
                ),
                "seed_policy": seed_records,
                "required_source_capabilities": (
                    _record(
                        "top_of_book_required",
                        record_type="CAPABILITY_REQUIREMENT",
                        fields=(
                            _field(
                                "capability",
                                ScenarioValueKindV1.IDENTIFIER,
                                "TOP_OF_BOOK_V1",
                            ),
                        ),
                    ),
                ),
            },
            imports=(ScenarioImportV1("base.toml", "audit_pack"),),
        ),
    )
    _write_document(
        pack_root / "base.toml",
        _source_document_bytes(
            "audit_compiler_base_v1",
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
                        ),
                    ),
                )
            },
        ),
    )
    _write_document(
        source_root / "unrelated.toml",
        _source_document_bytes(
            unrelated_id,
            sections={
                "market_profile": (_record("unrelated_market"),),
            },
        ),
    )
    unused_pack.mkdir(parents=True, exist_ok=True)
    return source_root, pack_root, unused_pack


def _seed_policy_record(
    *,
    logical_name: str = SCENARIO_SEED_POLICY_LOGICAL_NAME_V1,
    allow_override: bool = False,
    extra_fields: tuple[ScenarioFieldV1, ...] = (),
) -> ScenarioRecordV1:
    fields = (
        _field(
            "allow_cli_override",
            ScenarioValueKindV1.FLAG,
            allow_override,
        ),
        *extra_fields,
    )
    return _record(
        logical_name,
        record_type=SCENARIO_SEED_POLICY_RECORD_TYPE_V1,
        fields=fields,
    )


def _balanced_native_scenario() -> object:
    from kirby2.scenarios.market import load_scenario_definitions

    return load_scenario_definitions()["balanced"]


def _runnable_full_day_plan() -> object:
    from kirby2.audit.full_day import _sample_plan
    from kirby2.full_day.composition import (
        INITIAL_PROFILE_ID,
        executable_agent_mechanics_composition_matrix,
    )
    from kirby2.full_day.models import VersionedReferenceV1

    base = _sample_plan()
    matrix = executable_agent_mechanics_composition_matrix()
    shock_policy = replace(
        base.unscheduled_shock_policy,
        enabled=False,
        maximum_accepted_shocks=0,
        acceptance_numerator=0,
        acceptance_denominator=1,
    )
    return replace(
        base,
        composition_profile=VersionedReferenceV1(
            INITIAL_PROFILE_ID,
            2,
            matrix.sha256,
        ),
        participant_schedule=(),
        scheduled_events=(),
        unscheduled_shock_policy=shock_policy,
    )


def _compiler_native_payloads() -> dict[ScenarioTargetKindV1, object]:
    from kirby2.historical.lesson_catalog import load_historical_lessons
    from kirby2.multivenue.replay import MultiVenueRecording
    from kirby2.observability.replay import ObservabilityRecording

    empty_digest = hashlib.sha256(b"{}").hexdigest()
    return {
        ScenarioTargetKindV1.FULL_DAY_PLAN_V1: _runnable_full_day_plan(),
        ScenarioTargetKindV1.MARKET_SCENARIO_V1: _balanced_native_scenario(),
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


def _compiler_seed_policy_fixture():
    from kirby2.scenario_lang.defaults import materialize_scenario_defaults
    from kirby2.scenario_lang.seeds import build_compiled_seed_policy

    return build_compiled_seed_policy(
        materialize_scenario_defaults(_sample_source()).seed_policy_record
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
    target_kind: ScenarioTargetKindV1 = ScenarioTargetKindV1.MARKET_SCENARIO_V1,
    capability_digest: str | None = None,
) -> bytes:
    return canonical_toml(
        _source_document_payload(
            scenario_id,
            sections=sections,
            imports=imports,
            target_kind=target_kind,
            capability_digest=capability_digest,
        )
    ).encode("utf-8")


def _source_document_payload(
    scenario_id: str,
    *,
    sections: dict[str, tuple[ScenarioRecordV1, ...]] | None = None,
    imports: tuple[ScenarioImportV1, ...] = (),
    target_kind: ScenarioTargetKindV1 = ScenarioTargetKindV1.MARKET_SCENARIO_V1,
    capability_digest: str | None = None,
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
    target_contract = SCENARIO_TARGET_CONTRACTS_V1[target_kind]
    source = ScenarioSourceV1(
        schema_version=1,
        metadata=replace(
            _sample_source().metadata,
            scenario_id=scenario_id,
            title="WO32-B audit fixture",
            description="Confined import and definition fixture",
            target_kind=target_kind,
            target_version=target_contract.target_version,
            adapter_id=target_contract.adapter_id,
            adapter_version=target_contract.adapter_version,
            capability_digest=(
                _sample_source().metadata.capability_digest
                if capability_digest is None
                else capability_digest
            ),
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
    record_type: str = "AUDIT_DEFINITION_V1",
    fields: tuple[ScenarioFieldV1, ...] = (),
    reference: str | None = None,
    extends: str | None = None,
) -> ScenarioRecordV1:
    return ScenarioRecordV1(
        logical_name=logical_name,
        record_type=record_type,
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
    "WO32C_COMPILER_REFUSAL_COUNT",
    "WO32D_FINALIZATION_REFUSAL_COUNT",
    "WO32D_VALIDATION_FAMILY_COUNT",
    "audit_scenario_language",
    "audit_wo32a_scenario_language",
    "audit_wo32b_scenario_language",
    "audit_wo32c_scenario_language",
    "audit_wo32d_scenario_language",
]
