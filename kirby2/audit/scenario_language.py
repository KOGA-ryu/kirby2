"""Non-persisting executable evidence for the WO32-A source contracts."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, replace
from typing import Callable

from kirby2.research.toml_codec import canonical_toml
from kirby2.scenario_lang.identity import (
    SourceBundleEntryV1,
    canonical_semantic_plan_bytes,
    compiled_artifact_digest,
    semantic_plan_digest,
    source_bundle_digest,
)
from kirby2.scenario_lang.models import (
    SCENARIO_SOURCE_SECTION_NAMES,
    SCENARIO_TARGET_CONTRACTS_V1,
    ExactFixedPointV1,
    ScenarioFieldV1,
    ScenarioMetadataV1,
    ScenarioPlanEnvelopeV1,
    ScenarioRecordV1,
    ScenarioSectionV1,
    ScenarioSourceV1,
    ScenarioTargetKindV1,
    ScenarioValueKindV1,
    VolumeMultiplierV1,
)
from kirby2.scenario_lang.schema import (
    canonical_scenario_source_bytes,
    parse_canonical_scenario_source,
    parse_scenario_source,
    scenario_source_round_trip,
)


WO32A_STRICT_REFUSAL_COUNT = 13


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
    return audit_wo32a_scenario_language()


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


def _expect_refusal(operation: Callable[[], object], label: str) -> str | None:
    try:
        operation()
    except (TypeError, ValueError):
        return None
    return f"{label} was accepted"


__all__ = [
    "ScenarioLanguageAuditCase",
    "WO32A_STRICT_REFUSAL_COUNT",
    "audit_scenario_language",
    "audit_wo32a_scenario_language",
]
