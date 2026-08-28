"""Non-persisting contract audit for the WO31-A full-day execution IR."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Callable

from kirby2.exchange.mechanics_engine import MarketMechanicsEngine
from kirby2.exchange.mechanics_models import (
    AdvancedOrderRequest,
    InstrumentRules,
    MechanicsEventType,
    OrderInstruction,
    SessionSchedule,
    SessionState,
)
from kirby2.exchange.models import OrderOwner, Side


@dataclass(frozen=True, slots=True)
class FullDayAuditCase:
    name: str
    detail: str
    failures: tuple[str, ...]
    status_override: str | None = None
    reason_code: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("full-day audit case requires a nonempty name")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("full-day audit case requires nonempty detail")
        if type(self.failures) is not tuple or any(
            type(item) is not str or not item for item in self.failures
        ):
            raise TypeError("full-day audit failures must be an immutable string tuple")
        if self.status_override not in {None, "NOT_EXERCISED"}:
            raise ValueError("full-day audit status override is unsupported")
        if self.reason_code is not None and (
            type(self.reason_code) is not str
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", self.reason_code) is None
        ):
            raise ValueError("full-day audit reason code must be an uppercase identifier")
        if (self.status_override == "NOT_EXERCISED") != (self.reason_code is not None):
            raise ValueError("NOT_EXERCISED audit cases require exactly one reason code")
        if type(self.required) is not bool:
            raise TypeError("full-day audit required flag must be a bool")

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def status(self) -> str:
        if self.status_override is not None:
            return self.status_override
        return "PASS" if self.passed else "FAIL"

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "failures": list(self.failures),
            "name": self.name,
            "reason_code": self.reason_code,
            "required": self.required,
            "status": self.status,
        }


def audit_wo31a_contracts() -> tuple[FullDayAuditCase, ...]:
    """Exercise the frozen contracts without creating runtime or evidence files."""

    return (
        _canonical_plan_case(),
        _scheduled_event_semantics_case(),
        _strict_wire_refusal_case(),
        _calendar_case(),
        _seed_case(),
        _scheduled_work_case(),
        _composition_case(),
        _checkpoint_case(),
        _pilot_limits_case(),
        _mechanics_boundary_case(),
        FullDayAuditCase(
            "full_day_runtime_restore_capability",
            "runtime restore is outside this contract-only slice",
            (),
            status_override="NOT_EXERCISED",
            reason_code="RESTORE_NOT_IMPLEMENTED",
            required=False,
        ),
    )


def _expect_refusal(operation: Callable[[], object], label: str) -> str | None:
    try:
        operation()
    except (TypeError, ValueError, RuntimeError):
        return None
    return f"{label} was accepted"


def _canonical_plan_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        MACRO_REGIME_SCHEDULE_SEMANTICS_V1,
        FullDayPlanV1,
        canonical_sha256,
        derive_substream_seed,
    )

    plan = _sample_plan()
    wire = plan.to_json_bytes()
    restored = FullDayPlanV1.from_json_bytes(wire)
    failures: list[str] = []
    if restored.to_json_bytes() != wire or restored.sha256 != plan.sha256:
        failures.append("full-day plan canonical round trip changed bytes or identity")
    payload = plan.as_dict()
    reordered = {key: payload[key] for key in reversed(tuple(payload))}
    reordered_plan = FullDayPlanV1.from_dict(reordered)
    if reordered_plan.to_json_bytes() != wire:
        failures.append("plan map insertion order changed canonical bytes")
    root_variable = "KIRBY2_DATA_ROOT"
    original_root = os.environ.get(root_variable)
    root_was_present = root_variable in os.environ
    original_cwd = os.getcwd()
    identities_at_relocated_roots: dict[str, str] = {}
    try:
        for root, cwd in (
            ("/audit/root-a", "/"),
            ("/audit/relocated/root-b", original_cwd),
        ):
            os.environ[root_variable] = root
            os.chdir(cwd)
            identities_at_relocated_roots[root] = canonical_sha256(
                FullDayPlanV1.from_json_bytes(wire).as_dict()
            )
    finally:
        os.chdir(original_cwd)
        if root_was_present:
            assert original_root is not None
            os.environ[root_variable] = original_root
        else:
            os.environ.pop(root_variable, None)
    if len(set(identities_at_relocated_roots.values())) != 1:
        failures.append("ambient data-root relocation changed semantic plan identity")
    if any(root.encode("utf-8") in wire for root in identities_at_relocated_roots):
        failures.append("semantic plan bytes captured an ambient filesystem root")
    if (
        plan.macro_regime_schedule_semantics
        != MACRO_REGIME_SCHEDULE_SEMANTICS_V1
    ):
        failures.append("macro-regime anchor/state-transition precedence is ambiguous")
    expected_checkpoint_times = tuple(
        operation.boundary.simulation_time_us
        for operation in plan.calendar.boundary_operations
    )
    if plan.resolved_checkpoint_times_us != expected_checkpoint_times:
        failures.append("phase-boundary checkpoint schedule did not resolve exactly")

    probes: list[tuple[str, Callable[[], object]]] = []
    unknown = copy.deepcopy(payload)
    unknown["implicit_runtime_default"] = 1
    probes.append(("unknown plan field", lambda: FullDayPlanV1.from_dict(unknown)))
    missing = copy.deepcopy(payload)
    del missing["scheduled_events"]
    probes.append(("missing plan field", lambda: FullDayPlanV1.from_dict(missing)))
    floating = copy.deepcopy(payload)
    floating["pressure_profiles"][0]["segments"][0]["modifier_ppm"] = 1.0  # type: ignore[index]
    probes.append(("floating pressure modifier", lambda: FullDayPlanV1.from_dict(floating)))
    unknown_nested = copy.deepcopy(payload)
    unknown_nested["scheduled_events"][0]["unknown_payload"] = 1  # type: ignore[index]
    probes.append(
        ("unknown scheduled-event field", lambda: FullDayPlanV1.from_dict(unknown_nested))
    )
    double_calendar = copy.deepcopy(payload)
    double_calendar["instrument_profile"]["mechanics_rules"]["session_schedule"] = [  # type: ignore[index]
        {"simulation_time_us": 1, "state": "PREOPEN"}
    ]
    probes.append(
        ("second mechanics calendar", lambda: FullDayPlanV1.from_dict(double_calendar))
    )
    missing_digest = copy.deepcopy(payload)
    missing_digest["component_configurations"] = missing_digest[  # type: ignore[index]
        "component_configurations"
    ][1:]
    probes.append(
        (
            "missing referenced component digest",
            lambda: FullDayPlanV1.from_dict(missing_digest),
        )
    )
    wrong_owner = copy.deepcopy(payload)
    maker_binding = next(
        binding
        for binding in wrong_owner["component_configurations"]  # type: ignore[union-attr]
        if binding["configuration"]["reference_id"] == "AUDIT_MAKER_SPEC_V1"
    )
    quantity_binding = next(
        binding
        for binding in wrong_owner["component_configurations"]  # type: ignore[union-attr]
        if binding["configuration"]["reference_id"]
        == "AUDIT_QUANTITY_DISTRIBUTION_V1"
    )
    maker_binding["component_id"], quantity_binding["component_id"] = (
        quantity_binding["component_id"],
        maker_binding["component_id"],
    )
    wrong_owner["component_configurations"] = sorted(  # type: ignore[index]
        wrong_owner["component_configurations"],  # type: ignore[arg-type]
        key=lambda binding: (
            binding["component_id"],
            binding["configuration"]["reference_id"],
            binding["configuration"]["version"],
            binding["configuration"]["sha256"],
        ),
    )
    probes.append(
        (
            "component configuration bound to the wrong role owner",
            lambda: FullDayPlanV1.from_dict(wrong_owner),
        )
    )
    excessive_checkpoints = copy.deepcopy(payload)
    excessive_checkpoints["checkpoint_policy"]["interval_us"] = 1  # type: ignore[index]
    probes.append(
        (
            "resolved checkpoint schedule exceeding its declared maximum",
            lambda: FullDayPlanV1.from_dict(excessive_checkpoints),
        )
    )
    unowned_rng = copy.deepcopy(payload)
    seed_policy = unowned_rng["seed_policy"]  # type: ignore[index]
    unowned_path = "full_day/unowned/audit"
    seed_policy["substreams"].append(  # type: ignore[index]
        {
            "derived_seed": derive_substream_seed(
                seed_policy["root_seed"],  # type: ignore[index]
                seed_policy["policy_version"],  # type: ignore[index]
                unowned_path,
            ),
            "semantic_path": unowned_path,
        }
    )
    seed_policy["substreams"].sort(  # type: ignore[index]
        key=lambda item: item["semantic_path"]
    )
    probes.append(
        (
            "declared RNG path without exactly one selected component owner",
            lambda: FullDayPlanV1.from_dict(unowned_rng),
        )
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "full_day_plan_strict_canonical_roundtrip_and_schema_closure",
        (
            f"schema=1 bytes={len(wire)} semantic_sha256={plan.sha256} "
            "root_paths_excluded=true"
        ),
        tuple(failures),
    )


def _scheduled_event_semantics_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        SCHEDULED_EVENT_SEMANTICS_V1,
        FlowSideV1,
        FullDayPlanV1,
        IntegerParameterUnitV1,
        NamedIntegerParameterV1,
        ParticipantKindV1,
        ScheduledEventTypeV1,
        ScheduledEventV1,
        canonical_sha256,
        derive_substream_seed,
    )

    plan = _sample_plan()
    phases = {phase.phase_id: phase for phase in plan.calendar.phases}
    metaorder_participant = next(
        participant
        for participant in plan.participant_definitions
        if participant.participant_kind is ParticipantKindV1.METAORDER
    )
    maker_participant = next(
        participant
        for participant in plan.participant_definitions
        if participant.participant_kind is ParticipantKindV1.MARKET_MAKER
    )
    population_reference = metaorder_participant.specification
    halt_reference = plan.halt_reopen_rules.halt_trigger_reference
    resume_reference = plan.halt_reopen_rules.resume_trigger_reference
    continuous_start = phases["CONTINUOUS"].start.simulation_time_us

    def parameter(
        name: str,
        unit: IntegerParameterUnitV1,
        value: int,
    ) -> NamedIntegerParameterV1:
        return NamedIntegerParameterV1(name, unit, value)

    fixtures = (
        ScheduledEventV1(
            "AUDIT_ECONOMIC",
            100,
            ScheduledEventTypeV1.ECONOMIC_ANNOUNCEMENT,
            1,
            FlowSideV1.NONE,
            (parameter("impact_ppm", IntegerParameterUnitV1.PPM, 1_000),),
            None,
            None,
        ),
        ScheduledEventV1(
            "AUDIT_EARNINGS",
            200,
            ScheduledEventTypeV1.EARNINGS_LIKE_RELEASE,
            1,
            FlowSideV1.BUY,
            (parameter("impact_ppm", IntegerParameterUnitV1.PPM, 2_000),),
            None,
            None,
        ),
        ScheduledEventV1(
            "AUDIT_NEWS",
            300,
            ScheduledEventTypeV1.NEWS_SHOCK,
            1,
            FlowSideV1.SELL,
            (
                parameter("duration_us", IntegerParameterUnitV1.MICROSECONDS, 50),
                parameter("impact_ppm", IntegerParameterUnitV1.PPM, 3_000),
            ),
            None,
            None,
        ),
        ScheduledEventV1(
            "AUDIT_AUCTION_IMBALANCE",
            phases["OPENING_AUCTION"].start.simulation_time_us + 1,
            ScheduledEventTypeV1.AUCTION_IMBALANCE_PUBLICATION,
            1,
            FlowSideV1.BUY,
            (
                parameter(
                    "imbalance_shares", IntegerParameterUnitV1.SHARES, 1_000
                ),
            ),
            None,
            halt_reference,
        ),
        ScheduledEventV1(
            "AUDIT_METAORDER",
            continuous_start + 100,
            ScheduledEventTypeV1.LARGE_SCHEDULED_METAORDER,
            1,
            FlowSideV1.SELL,
            (
                parameter("duration_us", IntegerParameterUnitV1.MICROSECONDS, 100),
                parameter("participation_ppm", IntegerParameterUnitV1.PPM, 250_000),
                parameter("quantity_shares", IntegerParameterUnitV1.SHARES, 5_000),
            ),
            population_reference,
            None,
        ),
        ScheduledEventV1(
            "AUDIT_VOLATILITY_INTERRUPTION",
            continuous_start + 1_000_000,
            ScheduledEventTypeV1.VOLATILITY_INTERRUPTION,
            1,
            FlowSideV1.NONE,
            (
                parameter(
                    "halt_duration_us", IntegerParameterUnitV1.MICROSECONDS, 10
                ),
            ),
            None,
            halt_reference,
        ),
        ScheduledEventV1(
            "AUDIT_REOPEN_VOLATILITY",
            continuous_start + 1_000_010,
            ScheduledEventTypeV1.REOPENING,
            1,
            FlowSideV1.NONE,
            (
                parameter(
                    "reopening_auction_duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    10,
                ),
            ),
            None,
            resume_reference,
        ),
        ScheduledEventV1(
            "AUDIT_HALT",
            continuous_start + 2_000_000,
            ScheduledEventTypeV1.HALT,
            1,
            FlowSideV1.NONE,
            (
                parameter(
                    "halt_duration_us", IntegerParameterUnitV1.MICROSECONDS, 20
                ),
            ),
            None,
            halt_reference,
        ),
        ScheduledEventV1(
            "AUDIT_REOPEN_HALT",
            continuous_start + 2_000_020,
            ScheduledEventTypeV1.REOPENING,
            1,
            FlowSideV1.NONE,
            (
                parameter(
                    "reopening_auction_duration_us",
                    IntegerParameterUnitV1.MICROSECONDS,
                    10,
                ),
            ),
            None,
            resume_reference,
        ),
    )
    ordered_fixtures = tuple(
        sorted(fixtures, key=lambda item: (item.simulation_time_us, item.event_id))
    )
    valid_plan = replace(
        plan,
        scheduled_events=ordered_fixtures,
        halt_reopen_rules=replace(plan.halt_reopen_rules, maximum_halts=2),
    )
    failures: list[str] = []
    if tuple(SCHEDULED_EVENT_SEMANTICS_V1) != tuple(ScheduledEventTypeV1):
        failures.append("scheduled-event semantic registry is not exhaustive and ordered")
    if FullDayPlanV1.from_json_bytes(valid_plan.to_json_bytes()).as_dict() != valid_plan.as_dict():
        failures.append("eight-family scheduled-event plan did not round trip exactly")

    first_by_type: dict[ScheduledEventTypeV1, ScheduledEventV1] = {}
    for event in fixtures:
        first_by_type.setdefault(event.event_type, event)
    if set(first_by_type) != set(ScheduledEventTypeV1):
        failures.append("scheduled-event audit fixtures are not exhaustive")

    probes: list[tuple[str, Callable[[], object]]] = []
    for event_type in ScheduledEventTypeV1:
        event = first_by_type[event_type]
        restored = ScheduledEventV1.from_dict(event.as_dict())
        if restored.as_dict() != event.as_dict():
            failures.append(f"{event_type.value} did not round trip exactly")
        expected_parameter_digest = canonical_sha256(
            [parameter.as_dict() for parameter in event.parameters]
        )
        if event.parameter_set_sha256 != expected_parameter_digest:
            failures.append(
                f"{event_type.value} parameter-set identity is not canonical"
            )
        specification = SCHEDULED_EVENT_SEMANTICS_V1[event_type]
        payload = event.as_dict()
        missing_parameter = copy.deepcopy(payload)
        missing_parameter["parameters"] = missing_parameter["parameters"][1:]
        extra_parameter = copy.deepcopy(payload)
        extra_parameter["parameters"].append(
            {"name": "zz_extra", "unit": "COUNT", "value": 1}
        )
        wrong_unit = copy.deepcopy(payload)
        wrong_unit["parameters"][0]["unit"] = "COUNT"
        below_minimum = copy.deepcopy(payload)
        below_minimum["parameters"][0]["value"] = (
            specification.parameters[0].minimum_value - 1
        )
        wrong_side = copy.deepcopy(payload)
        wrong_side["side"] = (
            "BUY" if FlowSideV1.NONE in specification.allowed_sides else "NONE"
        )
        wrong_population = copy.deepcopy(payload)
        wrong_population["population_reference"] = (
            None
            if specification.population_reference_required
            else population_reference.as_dict()
        )
        wrong_mechanics = copy.deepcopy(payload)
        wrong_mechanics["mechanics_reference"] = (
            None
            if specification.mechanics_reference_required
            else halt_reference.as_dict()
        )
        for suffix, invalid in (
            ("missing parameter", missing_parameter),
            ("extra parameter", extra_parameter),
            ("wrong parameter unit", wrong_unit),
            ("parameter below minimum", below_minimum),
            ("wrong side", wrong_side),
            ("wrong population-reference presence", wrong_population),
            ("wrong mechanics-reference presence", wrong_mechanics),
        ):
            probes.append(
                (
                    f"{event_type.value} {suffix}",
                    lambda invalid=invalid: ScheduledEventV1.from_dict(invalid),
                )
            )
        for parameter_index, parameter_specification in enumerate(
            specification.parameters
        ):
            at_maximum = copy.deepcopy(payload)
            at_maximum["parameters"][parameter_index]["value"] = (
                parameter_specification.maximum_value
            )
            try:
                maximum_event = ScheduledEventV1.from_dict(at_maximum)
            except (TypeError, ValueError) as error:
                failures.append(
                    f"{event_type.value} {parameter_specification.name} finite "
                    f"maximum was refused: {error}"
                )
            else:
                if (
                    parameter_specification.maximum_value
                    != event.parameters[parameter_index].value
                    and maximum_event.parameter_set_sha256
                    == event.parameter_set_sha256
                ):
                    failures.append(
                        f"{event_type.value} parameter digest ignored "
                        f"{parameter_specification.name}"
                    )
            above_maximum = copy.deepcopy(payload)
            above_maximum["parameters"][parameter_index]["value"] = (
                parameter_specification.maximum_value + 1
            )
            probes.append(
                (
                    f"{event_type.value} {parameter_specification.name} above finite maximum",
                    lambda invalid=above_maximum: ScheduledEventV1.from_dict(invalid),
                )
            )

    def plan_payload_with_events(
        events: list[dict[str, object]],
    ) -> dict[str, object]:
        payload = copy.deepcopy(valid_plan.as_dict())
        payload["scheduled_events"] = sorted(
            events,
            key=lambda item: (item["simulation_time_us"], item["event_id"]),
        )
        return payload

    valid_event_payloads = [item.as_dict() for item in ordered_fixtures]
    maximum_zero = plan_payload_with_events(copy.deepcopy(valid_event_payloads))
    maximum_zero["halt_reopen_rules"]["maximum_halts"] = 0
    orphan_events = [
        copy.deepcopy(
            next(
                item
                for item in valid_event_payloads
                if item["event_id"] == "AUDIT_REOPEN_VOLATILITY"
            )
        )
    ]
    unclosed_events = [
        copy.deepcopy(item)
        for item in valid_event_payloads
        if item["event_id"] != "AUDIT_REOPEN_HALT"
    ]
    wrong_trigger_events = copy.deepcopy(valid_event_payloads)
    next(
        item for item in wrong_trigger_events if item["event_id"] == "AUDIT_HALT"
    )["mechanics_reference"] = resume_reference.as_dict()
    wrong_resume_events = copy.deepcopy(valid_event_payloads)
    next(
        item
        for item in wrong_resume_events
        if item["event_id"] == "AUDIT_REOPEN_HALT"
    )["mechanics_reference"] = halt_reference.as_dict()
    wrong_reopening_duration_events = copy.deepcopy(valid_event_payloads)
    next(
        item
        for item in wrong_reopening_duration_events
        if item["event_id"] == "AUDIT_REOPEN_HALT"
    )["parameters"][0]["value"] = 11
    mismatched_reopening_time_events = copy.deepcopy(valid_event_payloads)
    next(
        item
        for item in mismatched_reopening_time_events
        if item["event_id"] == "AUDIT_REOPEN_HALT"
    )["simulation_time_us"] += 1
    boundary_metaorder_events = copy.deepcopy(valid_event_payloads)
    next(
        item
        for item in boundary_metaorder_events
        if item["event_id"] == "AUDIT_METAORDER"
    )["simulation_time_us"] = phases["CONTINUOUS"].end.simulation_time_us
    overlap_events = copy.deepcopy(valid_event_payloads)
    overlap_halt = next(
        item for item in overlap_events if item["event_id"] == "AUDIT_HALT"
    )
    overlap_reopening = next(
        item
        for item in overlap_events
        if item["event_id"] == "AUDIT_REOPEN_HALT"
    )
    overlap_halt["simulation_time_us"] = continuous_start + 1_000_015
    overlap_reopening["simulation_time_us"] = continuous_start + 1_000_035
    overflow_events = copy.deepcopy(valid_event_payloads)
    overflow_halt = next(
        item for item in overflow_events if item["event_id"] == "AUDIT_HALT"
    )
    overflow_reopening = next(
        item
        for item in overflow_events
        if item["event_id"] == "AUDIT_REOPEN_HALT"
    )
    overflow_halt["simulation_time_us"] = phases["CONTINUOUS"].end.simulation_time_us - 15
    overflow_halt["parameters"][0]["value"] = 10
    overflow_reopening["simulation_time_us"] = phases["CONTINUOUS"].end.simulation_time_us - 5
    spill_news_events = copy.deepcopy(valid_event_payloads)
    spill_news = next(
        item for item in spill_news_events if item["event_id"] == "AUDIT_NEWS"
    )
    spill_news["simulation_time_us"] = (
        phases["PREOPEN"].end.simulation_time_us
        - spill_news["parameters"][0]["value"]
        + 1
    )
    spill_metaorder_events = copy.deepcopy(valid_event_payloads)
    spill_metaorder = next(
        item
        for item in spill_metaorder_events
        if item["event_id"] == "AUDIT_METAORDER"
    )
    spill_metaorder["simulation_time_us"] = (
        phases["CONTINUOUS"].end.simulation_time_us
        - spill_metaorder["parameters"][0]["value"]
        + 1
    )
    exact_phase_end_events = copy.deepcopy(valid_event_payloads)
    exact_news = next(
        item
        for item in exact_phase_end_events
        if item["event_id"] == "AUDIT_NEWS"
    )
    exact_news["simulation_time_us"] = (
        phases["PREOPEN"].end.simulation_time_us
        - exact_news["parameters"][0]["value"]
    )
    exact_metaorder = next(
        item
        for item in exact_phase_end_events
        if item["event_id"] == "AUDIT_METAORDER"
    )
    exact_metaorder["simulation_time_us"] = (
        phases["CONTINUOUS"].end.simulation_time_us
        - exact_metaorder["parameters"][0]["value"]
    )
    try:
        FullDayPlanV1.from_dict(plan_payload_with_events(exact_phase_end_events))
    except (TypeError, ValueError) as error:
        failures.append(f"exact phase-end scheduled duration was refused: {error}")
    wrong_kind_events = copy.deepcopy(valid_event_payloads)
    next(
        item
        for item in wrong_kind_events
        if item["event_id"] == "AUDIT_METAORDER"
    )["population_reference"] = maker_participant.specification.as_dict()

    missing_population = plan_payload_with_events(copy.deepcopy(valid_event_payloads))
    ghost_reference = {
        "reference_id": "AUDIT_GHOST_METAORDER_SPEC_V1",
        "sha256": "8" * 64,
        "version": 1,
    }
    next(
        item
        for item in missing_population["scheduled_events"]
        if item["event_id"] == "AUDIT_METAORDER"
    )["population_reference"] = ghost_reference
    missing_population["component_configurations"].append(
        {
            "component_id": "AGENT_SCHEDULER_V1",
            "configuration": ghost_reference,
        }
    )
    missing_population["component_configurations"].sort(
        key=lambda binding: (
            binding["component_id"],
            binding["configuration"]["reference_id"],
            binding["configuration"]["version"],
            binding["configuration"]["sha256"],
        )
    )

    ambiguous_population = plan_payload_with_events(copy.deepcopy(valid_event_payloads))
    clone_path = "full_day/participant/audit_metaorder_clone/decision"
    clone = copy.deepcopy(
        next(
            participant
            for participant in ambiguous_population["participant_definitions"]
            if participant["participant_id"] == "AUDIT_METAORDER"
        )
    )
    clone["participant_id"] = "AUDIT_METAORDER_CLONE"
    clone["rng_substream_label"] = clone_path
    ambiguous_population["participant_definitions"].append(clone)
    ambiguous_population["participant_definitions"].sort(
        key=lambda participant: participant["participant_id"]
    )
    seed_policy = ambiguous_population["seed_policy"]
    seed_policy["substreams"].append(
        {
            "derived_seed": derive_substream_seed(
                seed_policy["root_seed"],
                seed_policy["policy_version"],
                clone_path,
            ),
            "semantic_path": clone_path,
        }
    )
    seed_policy["substreams"].sort(key=lambda item: item["semantic_path"])

    plan_level_payloads = (
        ("scheduled halt count above zero budget", maximum_zero),
        ("orphan scheduled reopening", plan_payload_with_events(orphan_events)),
        ("scheduled halt without reopening", plan_payload_with_events(unclosed_events)),
        ("halt using resume-trigger reference", plan_payload_with_events(wrong_trigger_events)),
        ("reopening using halt-trigger reference", plan_payload_with_events(wrong_resume_events)),
        ("wrong reopening-auction duration", plan_payload_with_events(wrong_reopening_duration_events)),
        ("reopening at the wrong time", plan_payload_with_events(mismatched_reopening_time_events)),
        ("half-open phase-boundary event", plan_payload_with_events(boundary_metaorder_events)),
        ("overlapping halt/reopening lifecycles", plan_payload_with_events(overlap_events)),
        ("reopening auction crossing continuous close", plan_payload_with_events(overflow_events)),
        ("news-shock duration spilling beyond its phase", plan_payload_with_events(spill_news_events)),
        ("metaorder duration spilling beyond its phase", plan_payload_with_events(spill_metaorder_events)),
        ("metaorder population resolving to MARKET_MAKER", plan_payload_with_events(wrong_kind_events)),
        ("metaorder population absent from participant definitions", missing_population),
        ("metaorder population ambiguously resolving twice", ambiguous_population),
    )
    probes.extend(
        (
            label,
            lambda payload=payload: FullDayPlanV1.from_dict(payload),
        )
        for label, payload in plan_level_payloads
    )
    phase_midpoints = {
        phase_id: phase.start.simulation_time_us + 1
        for phase_id, phase in phases.items()
    }
    for event_type, event in first_by_type.items():
        specification = SCHEDULED_EVENT_SEMANTICS_V1[event_type]
        for forbidden_phase_id in sorted(
            set(phases) - set(specification.allowed_phase_ids)
        ):
            wrong_phase_events = copy.deepcopy(valid_event_payloads)
            next(
                item
                for item in wrong_phase_events
                if item["event_id"] == event.event_id
            )["simulation_time_us"] = phase_midpoints[forbidden_phase_id]
            probes.append(
                (
                    f"{event_type.value} in forbidden phase {forbidden_phase_id}",
                    lambda payload=plan_payload_with_events(
                        wrong_phase_events
                    ): FullDayPlanV1.from_dict(payload),
                )
            )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "scheduled_event_type_semantics_phase_and_halt_lifecycle",
        (
            "eight exact scheduled-event families; typed parameters/sides/references; "
            f"hostile_refusals={len(probes)}"
        ),
        tuple(failures),
    )


def _strict_wire_refusal_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        VersionedReferenceV1,
        canonical_json_bytes,
        canonical_sha256,
        parse_canonical_json_object,
        validate_strict_json,
    )

    failures: list[str] = []
    left = {"z": [3, 2, 1], "a": {"y": 2, "x": 1}}
    right = {"a": {"x": 1, "y": 2}, "z": [3, 2, 1]}
    if canonical_json_bytes(left) != canonical_json_bytes(right):
        failures.append("map insertion order changed canonical wire bytes")
    if canonical_sha256(left) != canonical_sha256(right):
        failures.append("map insertion order changed semantic identity")

    reference = VersionedReferenceV1("AUDIT_PROFILE_V1", 1, "1" * 64)
    if (
        VersionedReferenceV1.from_dict(reference.as_dict()).as_dict()
        != reference.as_dict()
    ):
        failures.append("strict reference did not round trip")
    probes: tuple[tuple[str, Callable[[], object]], ...] = (
        (
            "unknown field",
            lambda: VersionedReferenceV1.from_dict(
                {**reference.as_dict(), "unexpected": 1}
            ),
        ),
        (
            "missing field",
            lambda: VersionedReferenceV1.from_dict(
                {
                    "reference_id": reference.reference_id,
                    "version": reference.version,
                }
            ),
        ),
        ("nested float", lambda: validate_strict_json({"nested": [1, 2.0]})),
        ("non-NFC string", lambda: validate_strict_json({"x": "cafe\u0301"})),
        (
            "noncanonical key order",
            lambda: parse_canonical_json_object(b'{"z":1,"a":2}'),
        ),
        (
            "noncanonical whitespace",
            lambda: parse_canonical_json_object(b'{"a": 1}'),
        ),
        (
            "duplicate JSON key",
            lambda: parse_canonical_json_object(b'{"a":1,"a":2}'),
        ),
        (
            "floating JSON number",
            lambda: parse_canonical_json_object(b'{"a":1.0}'),
        ),
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "strict_float_free_canonical_json_and_exact_fields",
        (
            "compact sorted-key JSON rejects floats, duplicate keys, unknown/missing "
            "fields, non-NFC text, and noncanonical bytes"
        ),
        tuple(failures),
    )


def _calendar_case() -> FullDayAuditCase:
    from kirby2.full_day.calendar import (
        LocalBoundaryV1,
        TradingDayCalendarV1,
    )

    calendar = _sample_calendar()
    failures: list[str] = []
    restored = TradingDayCalendarV1.from_json_bytes(calendar.canonical_bytes())
    if restored.as_dict() != calendar.as_dict() or restored.sha256 != calendar.sha256:
        failures.append("calendar canonical round trip changed bytes or identity")

    invalid_payloads: list[tuple[str, Callable[[], object]]] = []

    class LocalBoundarySubclass(LocalBoundaryV1):
        pass

    forged_boundary = LocalBoundarySubclass.from_dict(
        calendar.phases[0].start.as_dict()
    )
    gap = copy.deepcopy(calendar.as_dict())
    gap["phases"][1]["start"]["local_time"] = "09:11:00.000000"  # type: ignore[index]
    gap["phases"][1]["start"]["simulation_time_us"] = 660_000_000  # type: ignore[index]
    invalid_payloads.append(("calendar gap", lambda: TradingDayCalendarV1.from_dict(gap)))
    overlap = copy.deepcopy(calendar.as_dict())
    overlap["phases"][1]["start"]["local_time"] = "09:09:00.000000"  # type: ignore[index]
    overlap["phases"][1]["start"]["simulation_time_us"] = 540_000_000  # type: ignore[index]
    invalid_payloads.append(
        ("calendar overlap", lambda: TradingDayCalendarV1.from_dict(overlap))
    )
    wrong_order = copy.deepcopy(calendar.as_dict())
    wrong_order["phases"][0]["phase_id"] = "CONTINUOUS"  # type: ignore[index]
    invalid_payloads.append(
        ("invalid phase order", lambda: TradingDayCalendarV1.from_dict(wrong_order))
    )
    backward = copy.deepcopy(calendar.as_dict())
    backward["phases"][0]["end"]["simulation_time_us"] = 0  # type: ignore[index]
    invalid_payloads.append(
        ("backward calendar time", lambda: TradingDayCalendarV1.from_dict(backward))
    )
    invalid_payloads.extend(
        (
            (
                "boolean local-boundary schema version",
                lambda: replace(calendar.phases[0].start, schema_version=True),
            ),
            (
                "boolean calendar-phase schema version",
                lambda: replace(calendar.phases[0], schema_version=True),
            ),
            (
                "boolean boundary-operation schema version",
                lambda: replace(calendar.boundary_operations[0], schema_version=True),
            ),
            (
                "boolean trading-calendar schema version",
                lambda: replace(calendar, schema_version=True),
            ),
            (
                "dataclass subclass smuggled into a calendar phase",
                lambda: replace(calendar.phases[0], start=forged_boundary),
            ),
        )
    )

    for label, operation in invalid_payloads:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)

    dst_probes = (
        (
            "nonexistent DST local time",
            lambda: LocalBoundaryV1(
                1,
                "2024-03-10",
                "02:30:00.000000",
                "America/New_York",
                0,
                -18_000,
                0,
            ),
        ),
        (
            "unresolved DST fold",
            lambda: LocalBoundaryV1(
                1,
                "2024-01-02",
                "09:00:00.000000",
                "America/New_York",
                1,
                -18_000,
                0,
            ),
        ),
        (
            "mismatched UTC offset",
            lambda: LocalBoundaryV1(
                1,
                "2024-11-03",
                "01:30:00.000000",
                "America/New_York",
                0,
                -18_000,
                0,
            ),
        ),
    )
    for label, operation in dst_probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)

    first_fold = LocalBoundaryV1(
        1,
        "2024-11-03",
        "01:30:00.000000",
        "America/New_York",
        0,
        -14_400,
        0,
    )
    second_fold = LocalBoundaryV1(
        1,
        "2024-11-03",
        "01:30:00.000000",
        "America/New_York",
        1,
        -18_000,
        3_600_000_000,
    )
    if second_fold.utc_datetime <= first_fold.utc_datetime:
        failures.append("explicit DST folds did not resolve distinct ordered instants")
    return FullDayAuditCase(
        "synthetic_calendar_roundtrip_dst_and_topology_refusals",
        (
            f"five contiguous half-open phases and six operations; "
            f"calendar_sha256={calendar.sha256}"
        ),
        tuple(failures),
    )


def _seed_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        SeedPolicyV1,
        SubstreamDeclarationV1,
        derive_substream_seed,
    )

    root_seed = 42
    policy_version = "FULL_DAY_SUBSTREAM_V1"
    maker_path = "full_day/participant/maker_1/decision"
    shock_path = "full_day/shock/candidate"
    maker_seed = derive_substream_seed(root_seed, policy_version, maker_path)
    independent_expected = int.from_bytes(
        hashlib.sha256(
            root_seed.to_bytes(8, "big")
            + b"\x00"
            + policy_version.encode("utf-8")
            + b"\x00"
            + maker_path.encode("utf-8")
        ).digest()[:8],
        "big",
    ) & ((1 << 63) - 1)
    failures: list[str] = []
    if maker_seed != independent_expected:
        failures.append("seed derivation differs from the frozen SHA-256 construction")
    before = derive_substream_seed(root_seed, policy_version, maker_path)
    shock_seed = derive_substream_seed(root_seed, policy_version, shock_path)
    after = derive_substream_seed(root_seed, policy_version, maker_path)
    if before != after:
        failures.append("unrelated substream derivation perturbed an existing seed")
    if shock_seed == maker_seed:
        failures.append("distinct semantic paths produced the same audit seed")

    declarations = tuple(
        sorted(
            (
                SubstreamDeclarationV1(maker_path, maker_seed),
                SubstreamDeclarationV1(shock_path, shock_seed),
            ),
            key=lambda item: item.semantic_path,
        )
    )
    policy = SeedPolicyV1(1, policy_version, root_seed, declarations)
    if SeedPolicyV1.from_dict(policy.as_dict()).as_dict() != policy.as_dict():
        failures.append("seed policy did not round trip exactly")

    probes: list[tuple[str, Callable[[], object]]] = []
    for label in (
        "",
        "participant/maker/decision",
        "/full_day/participant/maker",
        "full_day//decision",
        "full_day/../decision",
        "full_day/cafe\u0301/decision",
        "full_day/bad label/decision",
    ):
        probes.append(
            (
                f"bad seed label {label!r}",
                lambda label=label: derive_substream_seed(
                    root_seed, policy_version, label
                ),
            )
        )
    for bad_root in (-1, 2**63, 1.0):
        probes.append(
            (
                f"bad root seed {bad_root!r}",
                lambda bad_root=bad_root: derive_substream_seed(  # type: ignore[arg-type]
                    bad_root, policy_version, maker_path
                ),
            )
        )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "versioned_semantic_seed_substreams",
        (
            f"policy={policy_version} root={root_seed} maker_seed={maker_seed} "
            f"shock_seed={shock_seed}"
        ),
        tuple(failures),
    )


def _scheduled_work_case() -> FullDayAuditCase:
    from kirby2.full_day.events import (
        CALENDAR_BOUNDARY_INDEX_V1,
        FULL_DAY_ALLOWED_NATIVE_EVENT_TYPES_V1,
        FULL_DAY_PAYLOAD_FIELD_RULES_V1,
        FullDayEventPayloadV1,
        FullDayEventTypeV1,
        FullDayEventV1,
        NativeLedgerEntryV1,
        NativeEventReferenceV1,
        ScheduledWorkKeyV1,
        WorkStageV1,
        canonical_event_prefix_sha256,
        validate_deferred_work_key,
        validate_full_day_event_suffix,
        validate_full_day_event_stream,
    )
    from kirby2.full_day.checkpoint_contract import QuiescentCutV1
    from kirby2.full_day.models import (
        FlowSideV1,
        IntegerParameterUnitV1,
        MacroRegimeSegmentV1,
        NamedIntegerParameterV1,
        ParticipantScheduleActionV1,
        ParticipantScheduleEntryV1,
        ScheduledEventTypeV1,
        ScheduledEventV1,
        canonical_sha256,
    )
    from kirby2.full_day.states import (
        DayStateV1,
        DurationExhaustionBehaviorV1,
        DurationLawV1,
        DurationMassV1,
    )

    failures: list[str] = []
    probes: list[tuple[str, Callable[[], object]]] = []
    base_plan = _sample_plan()
    contract_plan = replace(
        base_plan,
        participant_schedule=(),
        scheduled_events=(),
    )
    maximum_microsteps = 128
    expected_stages = (
        ("ATOMIC_CALENDAR_BOUNDARY", 0),
        ("SCHEDULED_INFORMATION", 1),
        ("DAY_STATE_TRANSITION", 2),
        ("LOCAL_STATE_TRANSITION", 3),
        ("PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE", 4),
        ("PENDING_VENUE_ARRIVAL", 5),
        ("ENDOGENOUS_PARTICIPANT_DECISION", 6),
        ("BACKGROUND_FLOW_PROPOSAL", 7),
        ("OBSERVABLE_CLIENT_DELIVERY", 8),
        ("FEATURE_UPDATE", 9),
        ("STRATEGY_ALGORITHM_DEADLINE", 10),
        ("CHECKPOINT_CAPTURE", 11),
    )
    actual_stages = tuple((stage.name, int(stage)) for stage in WorkStageV1)
    if actual_stages != expected_stages:
        failures.append("microstep-zero stage inventory or ordinals changed")
    parent = ScheduledWorkKeyV1(
        simulation_time_us=1_000,
        microstep=0,
        stage_ordinal=WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        source_component_id="PARTICIPANT_MAKER_1",
        component_local_sequence=4,
    )
    ordered_keys = (
        ScheduledWorkKeyV1(
            1_000,
            1,
            WorkStageV1.DAY_STATE_TRANSITION,
            "STATE_RUNTIME",
            0,
        ),
        ScheduledWorkKeyV1(
            1_000,
            0,
            WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            "PARTICIPANT_Z",
            0,
        ),
        ScheduledWorkKeyV1(
            999,
            0,
            WorkStageV1.CHECKPOINT_CAPTURE,
            "FULL_DAY_RUNTIME",
            0,
        ),
        ScheduledWorkKeyV1(
            1_000,
            0,
            WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
            "FULL_DAY_RUNTIME",
            0,
        ),
        ScheduledWorkKeyV1(
            1_000,
            0,
            WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            "PARTICIPANT_A",
            2,
        ),
        ScheduledWorkKeyV1(
            1_000,
            0,
            WorkStageV1.SCHEDULED_INFORMATION,
            "INFORMATION_RUNTIME",
            0,
        ),
        ScheduledWorkKeyV1(
            1_000,
            0,
            WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            "PARTICIPANT_A",
            1,
        ),
    )
    expected_order = (
        (999, 0, 11, "FULL_DAY_RUNTIME", 0),
        (1_000, 0, 0, "FULL_DAY_RUNTIME", 0),
        (1_000, 0, 1, "INFORMATION_RUNTIME", 0),
        (1_000, 0, 6, "PARTICIPANT_A", 1),
        (1_000, 0, 6, "PARTICIPANT_A", 2),
        (1_000, 0, 6, "PARTICIPANT_Z", 0),
        (1_000, 1, 2, "STATE_RUNTIME", 0),
    )
    actual_order = tuple(key.ordering_key for key in sorted(ordered_keys))
    if actual_order != expected_order:
        failures.append("scheduled-work keys do not use the frozen five-field order")

    parent_wire = parent.to_json_bytes()
    restored_parent = ScheduledWorkKeyV1.from_json_bytes(parent_wire)
    if restored_parent != parent or restored_parent.to_json_bytes() != parent_wire:
        failures.append("scheduled-work key did not round trip byte-identically")
    reordered_parent_payload = {
        key: parent.as_dict()[key] for key in reversed(tuple(parent.as_dict()))
    }
    if ScheduledWorkKeyV1.from_dict(reordered_parent_payload).to_json_bytes() != parent_wire:
        failures.append("map insertion order changed scheduled-work identity")

    later_earlier_stage = ScheduledWorkKeyV1(
        1_000,
        1,
        WorkStageV1.DAY_STATE_TRANSITION,
        "STATE_RUNTIME",
        1,
    )
    try:
        validate_deferred_work_key(parent, later_earlier_stage, maximum_microsteps)
    except (TypeError, ValueError) as error:
        failures.append(f"valid later-microstep consequence was refused: {error}")
    if later_earlier_stage.ordering_key <= parent.ordering_key:
        failures.append("later microstep did not sort after its parent work item")

    def validate_stream(
        events: tuple[FullDayEventV1, ...],
        work_items: tuple[ScheduledWorkKeyV1, ...],
        native_entries: tuple[NativeLedgerEntryV1, ...] = (),
        scheduled_entries: tuple[ScheduledEventV1, ...] = (),
        full_day_plan=None,
    ) -> None:
        plan = contract_plan if full_day_plan is None else full_day_plan
        rebased_events: list[FullDayEventV1] = []
        for event in events:
            payload = event.as_dict()
            payload["global_event_sequence"] = event.global_event_sequence + 3
            payload["causal_parent_ids"] = [
                (
                    f"event:{int(parent_id.removeprefix('event:')) + 3}"
                    if parent_id.startswith("event:")
                    else parent_id
                )
                for parent_id in event.causal_parent_ids
            ]
            if event.source_component_id == "FULL_DAY_RUNTIME_V1":
                payload["component_local_sequence"] = (
                    event.component_local_sequence + 3
                )
            rebased_events.append(FullDayEventV1.from_dict(payload))
        validate_full_day_event_suffix(
            tuple(rebased_events),
            executed_work_items={item.work_id: item for item in work_items},
            native_event_ledger={item.ledger_key: item for item in native_entries},
            scheduled_event_ledger={item.event_id: item for item in scheduled_entries},
            full_day_plan=plan,
            verified_prefix_cut=genesis_cut,
            verified_prefix_events=genesis_prefix_events,
        )

    def make_work(
        simulation_time_us: int,
        stage: WorkStageV1,
        source_component_id: str,
        component_local_sequence: int = 1,
        microstep: int = 0,
    ) -> ScheduledWorkKeyV1:
        return ScheduledWorkKeyV1(
            simulation_time_us,
            microstep,
            stage,
            source_component_id,
            component_local_sequence,
        )

    def make_event(
        *,
        event_type: FullDayEventTypeV1,
        work: ScheduledWorkKeyV1,
        data: dict[str, object],
        source_component_id: str,
        global_sequence: int = 1,
        component_local_sequence: int = 1,
        causal_parent_id: str | None = None,
        native_event: NativeEventReferenceV1 | None = None,
    ) -> FullDayEventV1:
        return FullDayEventV1(
            schema_version=1,
            global_event_sequence=global_sequence,
            simulation_time_us=work.simulation_time_us,
            microstep=work.microstep,
            stage=work.stage_ordinal,
            source_component_id=source_component_id,
            component_local_sequence=component_local_sequence,
            event_type=event_type,
            causal_parent_ids=(causal_parent_id or work.work_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=event_type.value,
                payload_version=1,
                native_event=native_event,
                data=data,
            ),
        )

    genesis_operation = contract_plan.calendar.boundary_operations[0]
    genesis_boundary_work = make_work(
        genesis_operation.boundary.simulation_time_us,
        WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
        "FULL_DAY_RUNTIME_V1",
        1,
    )
    genesis_boundary_event = make_event(
        event_type=FullDayEventTypeV1.CALENDAR_BOUNDARY,
        work=genesis_boundary_work,
        data={
            "boundary_operation_index": 0,
            "destination_session_state": (
                genesis_operation.destination_session_state.value
            ),
            "uncross_before": genesis_operation.uncross_before,
        },
        source_component_id="FULL_DAY_RUNTIME_V1",
        global_sequence=1,
        component_local_sequence=1,
    )
    genesis_segment = contract_plan.macro_regime_schedule[0]
    genesis_anchor_work = make_work(
        genesis_segment.start_us,
        WorkStageV1.DAY_STATE_TRANSITION,
        "FULL_DAY_RUNTIME_V1",
        2,
    )
    genesis_anchor_event = make_event(
        event_type=FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
        work=genesis_anchor_work,
        data={
            "anchored_state": genesis_segment.day_state.value,
            "entered_time_us": genesis_segment.start_us,
            "macro_segment_index": 0,
            "macro_segment_sha256": canonical_sha256(genesis_segment.as_dict()),
            "previous_state": contract_plan.state_model.initial_day_state.value,
            "sampled_duration_us": 10,
        },
        source_component_id="FULL_DAY_RUNTIME_V1",
        global_sequence=2,
        component_local_sequence=2,
    )
    genesis_marker_work = make_work(
        0, WorkStageV1.CHECKPOINT_CAPTURE, "FULL_DAY_RUNTIME_V1", 3
    )
    genesis_marker_event = make_event(
        event_type=FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER,
        work=genesis_marker_work,
        data={"checkpoint_request_id": "GENESIS_CHECKPOINT_REQUEST"},
        source_component_id="FULL_DAY_RUNTIME_V1",
        global_sequence=3,
        component_local_sequence=3,
    )
    genesis_prefix_events = (
        genesis_boundary_event,
        genesis_anchor_event,
        genesis_marker_event,
    )
    genesis_prefix_works = (
        genesis_boundary_work,
        genesis_anchor_work,
        genesis_marker_work,
    )
    genesis_cut = QuiescentCutV1(
        schema_version=1,
        simulation_time_us=0,
        microstep=0,
        checkpoint_stage_ordinal=int(WorkStageV1.CHECKPOINT_CAPTURE),
        last_global_event_sequence=3,
        event_prefix_last_global_sequence=3,
        event_prefix_sha256=canonical_event_prefix_sha256(genesis_prefix_events),
        pending_work_count=1,
        next_pending_time_us=10,
        next_pending_microstep=0,
        due_work_at_or_before_cut=0,
        generated_microsteps_complete=True,
        checkpoint_stage_complete=True,
        boundary_complete_at_cut=True,
    )
    try:
        validate_full_day_event_stream(
            genesis_prefix_events,
            executed_work_items={item.work_id: item for item in genesis_prefix_works},
            native_event_ledger={},
            scheduled_event_ledger={},
            full_day_plan=contract_plan,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"authoritative genesis checkpoint prefix was refused: {error}")

    def resource_abort_event(
        global_sequence: int,
        component_local_sequence: int,
        work: ScheduledWorkKeyV1,
        source_component_id: str,
        causal_parent_id: str,
    ) -> FullDayEventV1:
        return FullDayEventV1(
            schema_version=1,
            global_event_sequence=global_sequence,
            simulation_time_us=work.simulation_time_us,
            microstep=work.microstep,
            stage=work.stage_ordinal,
            source_component_id=source_component_id,
            component_local_sequence=component_local_sequence,
            event_type=FullDayEventTypeV1.RESOURCE_LIMIT_ABORT,
            causal_parent_ids=(causal_parent_id,),
            payload=FullDayEventPayloadV1(
                1,
                FullDayEventTypeV1.RESOURCE_LIMIT_ABORT.value,
                1,
                None,
                {
                    "limit_id": "AUDIT_LIMIT",
                    "maximum_value": 10,
                    "observed_value": 11,
                },
            ),
        )

    digest_fixture = "b" * 64
    valid_payload_data_by_type: dict[FullDayEventTypeV1, dict[str, object]] = {
        FullDayEventTypeV1.CALENDAR_BOUNDARY: {
            "boundary_operation_index": 0,
            "destination_session_state": "PREOPEN",
            "uncross_before": False,
        },
        FullDayEventTypeV1.SCHEDULED_INFORMATION: {
            "parameter_set_sha256": digest_fixture,
            "scheduled_event_id": "SCHEDULED_EVENT_1",
            "scheduled_event_type": "ECONOMIC_ANNOUNCEMENT",
            "side": "NONE",
        },
        FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET: {
            "anchored_state": "QUIET",
            "entered_time_us": 0,
            "macro_segment_index": 0,
            "macro_segment_sha256": digest_fixture,
            "previous_state": "QUIET",
            "sampled_duration_us": 0,
        },
        FullDayEventTypeV1.DAY_STATE_TRANSITION: {
            "entered_time_us": 1_000,
            "new_state": "QUIET",
            "previous_state": "NORMAL",
            "sampled_duration_us": 0,
            "transition_id": "DAY_NORMAL_TO_QUIET",
            "trigger_id": "DAY_TRIGGER_1",
            "trigger_version": 1,
        },
        FullDayEventTypeV1.LOCAL_STATE_TRANSITION: {
            "entered_time_us": 1_000,
            "new_state": "BUY_PRESSURE",
            "previous_state": "BALANCED",
            "sampled_duration_us": 0,
            "transition_id": "LOCAL_BALANCED_TO_BUY",
            "trigger_id": "LOCAL_TRIGGER_1",
            "trigger_version": 1,
        },
        FullDayEventTypeV1.PARTICIPANT_ACTIVATED: {
            "native_payload_sha256": digest_fixture,
            "participant_id": "PARTICIPANT_1",
            "schedule_id": "SCHEDULE_1",
        },
        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED: {
            "native_payload_sha256": digest_fixture,
            "participant_id": "PARTICIPANT_1",
            "schedule_id": "SCHEDULE_2",
        },
        FullDayEventTypeV1.PARTICIPANT_RETUNED: {
            "native_payload_sha256": digest_fixture,
            "participant_id": "PARTICIPANT_1",
            "replacement_specification_sha256": digest_fixture,
            "schedule_id": "SCHEDULE_3",
        },
        FullDayEventTypeV1.PENDING_VENUE_ARRIVAL: {
            "arrival_time_us": 1_000,
            "native_payload_sha256": digest_fixture,
            "order_id": "ORDER_1",
        },
        FullDayEventTypeV1.PARTICIPANT_DECISION: {
            "decision_id": "DECISION_1",
            "information_cutoff_us": 1_000,
            "native_payload_sha256": digest_fixture,
            "participant_id": "PARTICIPANT_1",
        },
        FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL: {
            "native_payload_sha256": digest_fixture,
            "observation_cutoff_us": 1_000,
            "proposal_id": "PROPOSAL_1",
        },
        FullDayEventTypeV1.OBSERVABLE_DELIVERY: {
            "information_cutoff_us": 1_000,
            "message_id": "MESSAGE_1",
            "native_payload_sha256": digest_fixture,
        },
        FullDayEventTypeV1.FEATURE_UPDATED: {
            "feature_batch_id": "FEATURE_BATCH_1",
            "information_cutoff_us": 1_000,
            "native_payload_sha256": digest_fixture,
        },
        FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE: {
            "deadline_id": "DEADLINE_1",
            "information_cutoff_us": 1_000,
            "native_payload_sha256": digest_fixture,
        },
        FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER: {
            "checkpoint_request_id": "CHECKPOINT_REQUEST_1",
        },
        FullDayEventTypeV1.SHOCK_CANDIDATE: {
            "candidate_id": "CANDIDATE_1",
            "information_cutoff_us": 1_000,
            "quantity_shares": 1,
            "side": "BUY",
        },
        FullDayEventTypeV1.SHOCK_ACCEPTED: {
            "candidate_id": "CANDIDATE_1",
            "information_cutoff_us": 1_000,
            "quantity_shares": 1,
            "side": "SELL",
        },
        FullDayEventTypeV1.SHOCK_REJECTED: {
            "candidate_id": "CANDIDATE_2",
            "information_cutoff_us": 1_000,
            "reason_code": "POLICY_REJECTION",
        },
        FullDayEventTypeV1.SUBSYSTEM_EVENT: {
            "native_payload_sha256": digest_fixture,
        },
        FullDayEventTypeV1.RESOURCE_LIMIT_ABORT: {
            "limit_id": "OUTER_EVENTS",
            "maximum_value": 10,
            "observed_value": 11,
        },
        FullDayEventTypeV1.CAPABILITY_REFUSED: {
            "capability_id": "HISTORICAL_REPLAY",
            "reason_code": "COMPOSITION_PROFILE_REFUSES_COMPONENT",
        },
    }
    if (
        len(FullDayEventTypeV1) != 21
        or len(FULL_DAY_PAYLOAD_FIELD_RULES_V1) != 21
        or set(FULL_DAY_PAYLOAD_FIELD_RULES_V1) != set(FullDayEventTypeV1)
    ):
        failures.append("outer-payload schema registry is not exactly all 21 types")
    if (
        len(valid_payload_data_by_type) != 21
        or set(valid_payload_data_by_type) != set(FullDayEventTypeV1)
    ):
        failures.append("outer-payload audit fixtures are not exactly all 21 types")
    payload_field_probe_count = 0
    payload_enum_probe_count = 0
    for event_type in FullDayEventTypeV1:
        data = valid_payload_data_by_type[event_type]
        try:
            payload = FullDayEventPayloadV1(
                1,
                event_type.value,
                1,
                None,
                data,
            )
            restored_payload = FullDayEventPayloadV1.from_dict(payload.as_dict())
        except (TypeError, ValueError) as error:
            failures.append(f"valid {event_type.value} payload was refused: {error}")
            continue
        if restored_payload.as_dict() != payload.as_dict():
            failures.append(
                f"{event_type.value} payload did not round trip canonically"
            )
        rules = FULL_DAY_PAYLOAD_FIELD_RULES_V1[event_type]
        for rule in rules:
            missing = copy.deepcopy(data)
            del missing[rule.field_name]
            wrong_scalar = copy.deepcopy(data)
            wrong_scalar[rule.field_name] = {
                "BOOLEAN": 0,
                "IDENTIFIER": 0,
                "NONNEGATIVE_INTEGER": "0",
                "POSITIVE_INTEGER": "1",
                "SHA256": "not-a-sha256",
            }[rule.value_kind]
            probes.extend(
                (
                    (
                        f"{event_type.value} missing field {rule.field_name}",
                        lambda event_type=event_type, missing=missing: FullDayEventPayloadV1(
                            1, event_type.value, 1, None, missing
                        ),
                    ),
                    (
                        f"{event_type.value} wrong scalar {rule.field_name}",
                        lambda event_type=event_type, wrong_scalar=wrong_scalar: FullDayEventPayloadV1(
                            1, event_type.value, 1, None, wrong_scalar
                        ),
                    ),
                )
            )
            payload_field_probe_count += 2
            if rule.allowed_values:
                invalid_enum = copy.deepcopy(data)
                invalid_enum[rule.field_name] = "OUTSIDE_FROZEN_ENUM"
                probes.append(
                    (
                        f"{event_type.value} invalid enum {rule.field_name}",
                        lambda event_type=event_type, invalid_enum=invalid_enum: FullDayEventPayloadV1(
                            1, event_type.value, 1, None, invalid_enum
                        ),
                    )
                )
                payload_enum_probe_count += 1
        extra = {**copy.deepcopy(data), "unexpected_field": 1}
        probes.extend(
            (
                (
                    f"{event_type.value} extra payload field",
                    lambda event_type=event_type, extra=extra: FullDayEventPayloadV1(
                        1, event_type.value, 1, None, extra
                    ),
                ),
                (
                    f"{event_type.value} payload version 2",
                    lambda event_type=event_type, data=data: FullDayEventPayloadV1(
                        1, event_type.value, 2, None, data
                    ),
                ),
                (
                    f"{event_type.value} payload schema version 2",
                    lambda event_type=event_type, data=data: FullDayEventPayloadV1(
                        2, event_type.value, 1, None, data
                    ),
                ),
            )
        )

    cross_field_information = copy.deepcopy(
        valid_payload_data_by_type[FullDayEventTypeV1.SCHEDULED_INFORMATION]
    )
    cross_field_information["side"] = FlowSideV1.BUY.value
    probes.append(
        (
            "scheduled-information forbidden type/side pair",
            lambda: FullDayEventPayloadV1(
                1,
                FullDayEventTypeV1.SCHEDULED_INFORMATION.value,
                1,
                None,
                cross_field_information,
            ),
        )
    )

    # The six boundary operation rows are frozen as exact index/state/uncross tuples.
    calendar_rows = tuple(
        (index, state.value, uncross)
        for index, (state, uncross) in CALENDAR_BOUNDARY_INDEX_V1.items()
    )
    expected_calendar_rows = (
        (0, "PREOPEN", False),
        (1, "OPENING_AUCTION", False),
        (2, "CONTINUOUS", True),
        (3, "CLOSING_AUCTION", False),
        (4, "POSTCLOSE", True),
        (5, "CLOSED", False),
    )
    if calendar_rows != expected_calendar_rows:
        failures.append("calendar boundary index/state/uncross table changed")
    sample_calendar_rows = tuple(
        (
            index,
            operation.destination_session_state.value,
            operation.uncross_before,
        )
        for index, operation in enumerate(_sample_plan().calendar.boundary_operations)
    )
    if sample_calendar_rows != expected_calendar_rows:
        failures.append(
            "sample calendar boundary operations differ from the frozen boundary table"
        )
    for index, destination, uncross in expected_calendar_rows:
        calendar_data = {
            "boundary_operation_index": index,
            "destination_session_state": destination,
            "uncross_before": uncross,
        }
        bad_destination = dict(calendar_data)
        bad_destination["destination_session_state"] = expected_calendar_rows[
            (index + 1) % len(expected_calendar_rows)
        ][1]
        bad_uncross = dict(calendar_data)
        bad_uncross["uncross_before"] = not uncross
        probes.extend(
            (
                (
                    f"calendar boundary {index} destination mismatch",
                    lambda bad_destination=bad_destination: FullDayEventPayloadV1(
                        1,
                        FullDayEventTypeV1.CALENDAR_BOUNDARY.value,
                        1,
                        None,
                        bad_destination,
                    ),
                ),
                (
                    f"calendar boundary {index} uncross mismatch",
                    lambda bad_uncross=bad_uncross: FullDayEventPayloadV1(
                        1,
                        FullDayEventTypeV1.CALENDAR_BOUNDARY.value,
                        1,
                        None,
                        bad_uncross,
                    ),
                ),
            )
        )

    def plan_boundary_event(
        plan,
        boundary_index: int,
        global_sequence: int,
        component_local_sequence: int,
    ) -> tuple[FullDayEventV1, ScheduledWorkKeyV1]:
        operation = plan.calendar.boundary_operations[boundary_index]
        work = make_work(
            operation.boundary.simulation_time_us,
            WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
            "FULL_DAY_RUNTIME_V1",
            component_local_sequence,
        )
        event = make_event(
            event_type=FullDayEventTypeV1.CALENDAR_BOUNDARY,
            work=work,
            data={
                "boundary_operation_index": boundary_index,
                "destination_session_state": (
                    operation.destination_session_state.value
                ),
                "uncross_before": operation.uncross_before,
            },
            source_component_id="FULL_DAY_RUNTIME_V1",
            global_sequence=global_sequence,
            component_local_sequence=component_local_sequence,
        )
        return event, work

    def plan_anchor_event(
        plan,
        macro_index: int,
        previous_state: str,
        sampled_duration_us: int,
        global_sequence: int,
        component_local_sequence: int,
    ) -> tuple[FullDayEventV1, ScheduledWorkKeyV1]:
        segment = plan.macro_regime_schedule[macro_index]
        work = make_work(
            segment.start_us,
            WorkStageV1.DAY_STATE_TRANSITION,
            "FULL_DAY_RUNTIME_V1",
            component_local_sequence,
        )
        event = make_event(
            event_type=FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
            work=work,
            data={
                "anchored_state": segment.day_state.value,
                "entered_time_us": segment.start_us,
                "macro_segment_index": macro_index,
                "macro_segment_sha256": canonical_sha256(segment.as_dict()),
                "previous_state": previous_state,
                "sampled_duration_us": sampled_duration_us,
            },
            source_component_id="FULL_DAY_RUNTIME_V1",
            global_sequence=global_sequence,
            component_local_sequence=component_local_sequence,
        )
        return event, work

    # One authoritative full trace proves the sample calendar's six rows and order.
    calendar_events: list[FullDayEventV1] = []
    calendar_works: list[ScheduledWorkKeyV1] = []
    boundary_zero, boundary_zero_work = plan_boundary_event(
        contract_plan, 0, 1, 1
    )
    anchor_zero, anchor_zero_work = plan_anchor_event(
        contract_plan,
        0,
        contract_plan.state_model.initial_day_state.value,
        10,
        2,
        2,
    )
    calendar_events.extend((boundary_zero, anchor_zero))
    calendar_works.extend((boundary_zero_work, anchor_zero_work))
    for boundary_index in range(1, 6):
        event, work = plan_boundary_event(
            contract_plan,
            boundary_index,
            boundary_index + 2,
            boundary_index + 2,
        )
        calendar_events.append(event)
        calendar_works.append(work)
    try:
        validate_full_day_event_stream(
            tuple(calendar_events),
            executed_work_items={item.work_id: item for item in calendar_works},
            native_event_ledger={},
            scheduled_event_ledger={},
            full_day_plan=contract_plan,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"authoritative six-boundary trace was refused: {error}")

    invented_calendar_time = copy.deepcopy(calendar_events[0].as_dict())
    invented_calendar_time["simulation_time_us"] = 1
    invented_calendar_owner = copy.deepcopy(calendar_events[0].as_dict())
    invented_calendar_owner["source_component_id"] = "INVENTED_RUNTIME_V1"
    for label, forged in (
        ("calendar boundary at an invented time", invented_calendar_time),
        ("calendar boundary from an invented owner", invented_calendar_owner),
    ):
        forged_trace = (
            FullDayEventV1.from_dict(forged),
            *calendar_events[1:],
        )
        probes.append(
            (
                label,
                lambda forged_trace=forged_trace: validate_full_day_event_stream(
                    forged_trace,
                    executed_work_items={
                        item.work_id: item for item in calendar_works
                    },
                    native_event_ledger={},
                    scheduled_event_ledger={},
                    full_day_plan=contract_plan,
                ),
            )
        )

    # Zero durations are legal on acyclic paths, but a forced zero-time cycle is not.
    base_state_model = _sample_state_model()
    zero_duration_law = DurationLawV1(0, 0, (DurationMassV1(0, 1),))
    first_day_definition = base_state_model.day_definitions[0]
    zero_acyclic_transition = replace(
        first_day_definition.transitions[0],
        minimum_age_us=0,
        duration_exhaustion_behavior=(
            DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        ),
    )
    try:
        replace(
            base_state_model,
            day_definitions=(
                replace(
                    first_day_definition,
                    duration_law=zero_duration_law,
                    transitions=(zero_acyclic_transition,),
                ),
                *base_state_model.day_definitions[1:],
            ),
        )
    except (TypeError, ValueError) as error:
        failures.append(f"acyclic zero-duration state path was refused: {error}")
    forced_cycle_definitions = tuple(
        replace(
            definition,
            duration_law=zero_duration_law,
            transitions=tuple(
                replace(
                    transition,
                    minimum_age_us=0,
                    duration_exhaustion_behavior=(
                        DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
                    ),
                )
                for transition in definition.transitions
            ),
        )
        for definition in base_state_model.day_definitions
    )
    probes.append(
        (
            "forced zero-time state cycle",
            lambda: replace(
                base_state_model, day_definitions=forced_cycle_definitions
            ),
        )
    )

    zero_support_definitions = tuple(
        replace(definition, duration_law=zero_duration_law)
        if definition.state in {DayStateV1.QUIET, DayStateV1.NORMAL}
        else definition
        for definition in base_state_model.day_definitions
    )
    authority_state_model = replace(
        base_state_model, day_definitions=zero_support_definitions
    )
    anchor_plan = replace(
        contract_plan,
        state_model=authority_state_model,
        macro_regime_schedule=(
            MacroRegimeSegmentV1(0, 5_000, DayStateV1.QUIET),
            MacroRegimeSegmentV1(
                5_000, base_plan.calendar.end_time_us, DayStateV1.NORMAL
            ),
        ),
    )
    actual_transition = base_state_model.day_definitions[0].transitions[0]
    parallel_transition = replace(
        actual_transition,
        transition_id=f"{actual_transition.transition_id}_PARALLEL",
    )
    parallel_first_definition = replace(
        authority_state_model.day_definitions[0],
        transitions=(actual_transition, parallel_transition),
    )
    parallel_state_model = replace(
        authority_state_model,
        day_definitions=(
            parallel_first_definition,
            *authority_state_model.day_definitions[1:],
        ),
    )
    transition_plan = replace(contract_plan, state_model=parallel_state_model)

    authority_boundary, authority_boundary_work = plan_boundary_event(
        anchor_plan, 0, 1, 1
    )
    authority_anchor_zero, authority_anchor_zero_work = plan_anchor_event(
        anchor_plan,
        0,
        DayStateV1.QUIET.value,
        0,
        2,
        2,
    )
    authority_anchor_later, authority_anchor_later_work = plan_anchor_event(
        anchor_plan,
        1,
        DayStateV1.QUIET.value,
        0,
        3,
        3,
    )
    anchor_trace = (
        authority_boundary,
        authority_anchor_zero,
        authority_anchor_later,
    )
    anchor_trace_works = (
        authority_boundary_work,
        authority_anchor_zero_work,
        authority_anchor_later_work,
    )
    try:
        validate_full_day_event_stream(
            anchor_trace,
            executed_work_items={item.work_id: item for item in anchor_trace_works},
            native_event_ledger={},
            scheduled_event_ledger={},
            full_day_plan=anchor_plan,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"authoritative zero-duration macro anchors were refused: {error}")
    invented_anchor = copy.deepcopy(authority_anchor_later.as_dict())
    invented_anchor["payload"]["data"]["macro_segment_sha256"] = "f" * 64  # type: ignore[index]
    invented_anchor_trace = (
        authority_boundary,
        authority_anchor_zero,
        FullDayEventV1.from_dict(invented_anchor),
    )
    probes.append(
        (
            "macro anchor with an invented segment identity",
            lambda: validate_full_day_event_stream(
                invented_anchor_trace,
                executed_work_items={
                    item.work_id: item for item in anchor_trace_works
                },
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=anchor_plan,
            ),
        )
    )

    transition_work = make_work(
        6_000, WorkStageV1.DAY_STATE_TRANSITION, "FULL_DAY_RUNTIME_V1", 3
    )
    transition_data = {
        "entered_time_us": 6_000,
        "new_state": actual_transition.successor_state,
        "previous_state": actual_transition.source_state,
        "sampled_duration_us": 0,
        "transition_id": actual_transition.transition_id,
        "trigger_id": actual_transition.trigger_id,
        "trigger_version": actual_transition.trigger_version,
    }
    transition_a = make_event(
        event_type=FullDayEventTypeV1.DAY_STATE_TRANSITION,
        work=transition_work,
        data=transition_data,
        source_component_id="FULL_DAY_RUNTIME_V1",
        global_sequence=3,
        component_local_sequence=3,
    )
    transition_b_data = {
        **transition_data,
        "transition_id": parallel_transition.transition_id,
        "trigger_id": parallel_transition.trigger_id,
        "trigger_version": parallel_transition.trigger_version,
    }
    transition_b = make_event(
        event_type=FullDayEventTypeV1.DAY_STATE_TRANSITION,
        work=transition_work,
        data=transition_b_data,
        source_component_id="FULL_DAY_RUNTIME_V1",
        global_sequence=3,
        component_local_sequence=3,
    )
    transition_boundary, transition_boundary_work = plan_boundary_event(
        transition_plan, 0, 1, 1
    )
    transition_anchor, transition_anchor_work = plan_anchor_event(
        transition_plan,
        0,
        DayStateV1.QUIET.value,
        0,
        2,
        2,
    )
    try:
        for transition_event in (transition_a, transition_b):
            validate_full_day_event_stream(
                (transition_boundary, transition_anchor, transition_event),
                executed_work_items={
                    item.work_id: item
                    for item in (
                        transition_boundary_work,
                        transition_anchor_work,
                        transition_work,
                    )
                },
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=transition_plan,
            )
    except (TypeError, ValueError) as error:
        failures.append(f"valid zero-duration state transition was refused: {error}")
    if transition_a.to_json_bytes() == transition_b.to_json_bytes():
        failures.append("parallel transition identities produced identical event bytes")
    invented_transition = copy.deepcopy(transition_a.as_dict())
    invented_transition["payload"]["data"][  # type: ignore[index]
        "transition_id"
    ] = "INVENTED_DAY_TRANSITION"
    probes.append(
        (
            "day-state transition with an invented graph edge",
            lambda: validate_full_day_event_stream(
                (
                    transition_boundary,
                    transition_anchor,
                    FullDayEventV1.from_dict(invented_transition),
                ),
                executed_work_items={
                    item.work_id: item
                    for item in (
                        transition_boundary_work,
                        transition_anchor_work,
                        transition_work,
                    )
                },
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=transition_plan,
            ),
        )
    )

    # Every native-required outer family gets a real owner/ledger/event context.
    activation_schedule = base_plan.participant_schedule[0]
    maker_specification = base_plan.participant_definitions[0].specification
    deactivation_schedule = ParticipantScheduleEntryV1(
        "AUDIT_MAKER_DEACTIVATE",
        101,
        "AUDIT_MAKER",
        ParticipantScheduleActionV1.DEACTIVATE,
        None,
    )
    retune_schedule = ParticipantScheduleEntryV1(
        "AUDIT_MAKER_RETUNE",
        102,
        "AUDIT_MAKER",
        ParticipantScheduleActionV1.RETUNE,
        maker_specification,
    )
    activation_plan = replace(
        contract_plan, participant_schedule=(activation_schedule,)
    )
    deactivation_plan = replace(
        contract_plan, participant_schedule=(deactivation_schedule,)
    )
    retune_plan = replace(contract_plan, participant_schedule=(retune_schedule,))
    native_contexts: dict[
        FullDayEventTypeV1,
        tuple[WorkStageV1, str, str, int, dict[str, object], object],
    ] = {
        FullDayEventTypeV1.PARTICIPANT_ACTIVATED: (
            WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE,
            "PARTICIPANT_ACTIVATED",
            "AGENT_SCHEDULER_V1",
            activation_schedule.simulation_time_us,
            {
                "participant_id": activation_schedule.participant_id,
                "schedule_id": activation_schedule.schedule_id,
            },
            activation_plan,
        ),
        FullDayEventTypeV1.PARTICIPANT_DEACTIVATED: (
            WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE,
            "PARTICIPANT_DEACTIVATED",
            "AGENT_SCHEDULER_V1",
            deactivation_schedule.simulation_time_us,
            {
                "participant_id": deactivation_schedule.participant_id,
                "schedule_id": deactivation_schedule.schedule_id,
            },
            deactivation_plan,
        ),
        FullDayEventTypeV1.PARTICIPANT_RETUNED: (
            WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE,
            "PARTICIPANT_RETUNED",
            "AGENT_SCHEDULER_V1",
            retune_schedule.simulation_time_us,
            {
                "participant_id": retune_schedule.participant_id,
                "replacement_specification_sha256": maker_specification.sha256,
                "schedule_id": retune_schedule.schedule_id,
            },
            retune_plan,
        ),
        FullDayEventTypeV1.PENDING_VENUE_ARRIVAL: (
            WorkStageV1.PENDING_VENUE_ARRIVAL,
            "PENDING_VENUE_ARRIVAL",
            "ENGINE_MARKET_MECHANICS_V1",
            10_003,
            {"arrival_time_us": 10_003, "order_id": "ORDER_1"},
            contract_plan,
        ),
        FullDayEventTypeV1.PARTICIPANT_DECISION: (
            WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            "PARTICIPANT_DECISION",
            "AUDIT_MAKER",
            10_004,
            {
                "decision_id": "DECISION_1",
                "information_cutoff_us": 10_004,
                "participant_id": "PARTICIPANT_1",
            },
            contract_plan,
        ),
        FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL: (
            WorkStageV1.BACKGROUND_FLOW_PROPOSAL,
            "BACKGROUND_FLOW_PROPOSAL",
            "FLOW_SIMPLE_V1",
            10_005,
            {"observation_cutoff_us": 10_005, "proposal_id": "PROPOSAL_1"},
            contract_plan,
        ),
        FullDayEventTypeV1.OBSERVABLE_DELIVERY: (
            WorkStageV1.OBSERVABLE_CLIENT_DELIVERY,
            "CLIENT_MESSAGE_DELIVERED",
            "DELIVERY_ASYNC_V1",
            10_006,
            {"information_cutoff_us": 10_006, "message_id": "MESSAGE_1"},
            contract_plan,
        ),
        FullDayEventTypeV1.FEATURE_UPDATED: (
            WorkStageV1.FEATURE_UPDATE,
            "FEATURE_UPDATED",
            "FEATURES_V1",
            10_007,
            {"feature_batch_id": "FEATURE_BATCH_1", "information_cutoff_us": 10_007},
            contract_plan,
        ),
        FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE: (
            WorkStageV1.STRATEGY_ALGORITHM_DEADLINE,
            "STRATEGY_ALGORITHM_DEADLINE",
            "ALGORITHMS_V1",
            10_008,
            {"deadline_id": "DEADLINE_1", "information_cutoff_us": 10_008},
            contract_plan,
        ),
        FullDayEventTypeV1.SUBSYSTEM_EVENT: (
            WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            "TRADE",
            "ENGINE_MARKET_MECHANICS_V1",
            10_009,
            {"price_ticks": 10_000, "quantity_shares": 25},
            contract_plan,
        ),
    }
    if set(native_contexts) != set(FULL_DAY_ALLOWED_NATIVE_EVENT_TYPES_V1):
        failures.append("native outer-event audit contexts are not exhaustive")
    native_fixtures: dict[
        FullDayEventTypeV1,
        tuple[FullDayEventV1, ScheduledWorkKeyV1, NativeLedgerEntryV1],
    ] = {}
    for index, (event_type, context) in enumerate(native_contexts.items()):
        stage, native_type, owner, time_us, native_payload, context_plan = context
        work = make_work(time_us, stage, owner, index + 1)
        if event_type is FullDayEventTypeV1.PENDING_VENUE_ARRIVAL:
            native_payload = {**native_payload, "arrival_time_us": time_us}
        for cutoff_name in ("information_cutoff_us", "observation_cutoff_us"):
            if cutoff_name in native_payload:
                native_payload = {**native_payload, cutoff_name: time_us}
        reference = NativeEventReferenceV1(
            1,
            owner,
            f"{event_type.value}_NATIVE_LEDGER_V1",
            native_type,
            1,
            f"native:{index + 1}",
        )
        entry = NativeLedgerEntryV1(reference, native_payload)
        outer_data = (
            {"native_payload_sha256": entry.payload_sha256}
            if event_type is FullDayEventTypeV1.SUBSYSTEM_EVENT
            else {**native_payload, "native_payload_sha256": entry.payload_sha256}
        )
        event = make_event(
            event_type=event_type,
            work=work,
            data=outer_data,
            source_component_id=owner,
            native_event=reference,
        )
        native_fixtures[event_type] = (event, work, entry)
        try:
            validate_stream(
                (event,),
                (work,),
                (entry,),
                full_day_plan=context_plan,
            )
        except (TypeError, ValueError) as error:
            failures.append(f"valid native context for {event_type.value} was refused: {error}")
        native_wire = entry.canonical_bytes()
        if NativeLedgerEntryV1.from_json_bytes(native_wire).canonical_bytes() != native_wire:
            failures.append(f"native ledger row for {event_type.value} changed bytes")
        event_wire = event.to_json_bytes()
        if FullDayEventV1.from_json_bytes(event_wire).to_json_bytes() != event_wire:
            failures.append(f"outer event for {event_type.value} changed bytes")

    # Outer owner allocation and native owner/ledger allocation are independent.
    dual_work = make_work(
        20_000,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "ENGINE_MARKET_MECHANICS_V1",
    )
    dual_ref_a = NativeEventReferenceV1(
        1,
        "ENGINE_MARKET_MECHANICS_V1",
        "MECHANICS_EVENT_LEDGER_V1",
        "TRADE",
        1,
        "mechanics:event:1",
    )
    dual_ref_b = NativeEventReferenceV1(
        1,
        "ENGINE_MARKET_MECHANICS_V1",
        "CONTINUOUS_BOOK_EVENT_JOURNAL_V1",
        "FULL_FILL",
        1,
        "book:event:1",
    )
    dual_entry_a = NativeLedgerEntryV1(
        dual_ref_a, {"price_ticks": 10_000, "quantity_shares": 25}
    )
    dual_entry_b = NativeLedgerEntryV1(
        dual_ref_b, {"order_id": "ORDER_1", "remaining_quantity_shares": 0}
    )
    event_one = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=dual_work,
        data={"native_payload_sha256": dual_entry_a.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        component_local_sequence=1,
        native_event=dual_ref_a,
    )
    event_two = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=dual_work,
        data={"native_payload_sha256": dual_entry_b.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        global_sequence=2,
        component_local_sequence=2,
        causal_parent_id=event_one.event_id,
        native_event=dual_ref_b,
    )
    try:
        validate_stream(
            (event_one, event_two),
            (dual_work,),
            (dual_entry_a, dual_entry_b),
        )
    except (TypeError, ValueError) as error:
        failures.append(f"valid overlapping native-ledger sequences were refused: {error}")
    if (
        dual_ref_a.local_sequence != 1
        or dual_ref_b.local_sequence != 1
        or (event_one.component_local_sequence, event_two.component_local_sequence)
        != (1, 2)
    ):
        failures.append("outer and per-native-ledger allocators were conflated")
    probes.append(
        (
            "zero native local sequence",
            lambda: NativeEventReferenceV1(
                1,
                "ENGINE_MARKET_MECHANICS_V1",
                "MECHANICS_EVENT_LEDGER_V1",
                "TRADE",
                0,
                "mechanics:event:0",
            ),
        )
    )

    # Scheduled information is a projection of one authoritative plan ledger row.
    scheduled_plan = replace(base_plan, participant_schedule=())
    scheduled_row = scheduled_plan.scheduled_events[0]
    invented_scheduled_row = ScheduledEventV1(
        "AUDIT_LEDGER_EVENT",
        30_000,
        ScheduledEventTypeV1.EARNINGS_LIKE_RELEASE,
        1,
        FlowSideV1.BUY,
        (
            NamedIntegerParameterV1(
                "impact_ppm", IntegerParameterUnitV1.PPM, 1_000
            ),
        ),
        None,
        None,
    )
    scheduled_data = {
        "parameter_set_sha256": scheduled_row.parameter_set_sha256,
        "scheduled_event_id": scheduled_row.event_id,
        "scheduled_event_type": scheduled_row.event_type.value,
        "side": scheduled_row.side.value,
    }

    def validate_scheduled_projection(
        data: dict[str, object],
        simulation_time_us: int,
        ledger_rows: tuple[ScheduledEventV1, ...],
    ) -> None:
        work = make_work(
            simulation_time_us,
            WorkStageV1.SCHEDULED_INFORMATION,
            "INFORMATION_RUNTIME_V1",
        )
        event = make_event(
            event_type=FullDayEventTypeV1.SCHEDULED_INFORMATION,
            work=work,
            data=data,
            source_component_id="FULL_DAY_RUNTIME_V1",
        )
        validate_stream(
            (event,),
            (work,),
            scheduled_entries=ledger_rows,
            full_day_plan=scheduled_plan,
        )

    def validate_mismatched_scheduled_ledger_key() -> None:
        work = make_work(
            scheduled_row.simulation_time_us,
            WorkStageV1.SCHEDULED_INFORMATION,
            "FULL_DAY_RUNTIME_V1",
        )
        raw_event = make_event(
            event_type=FullDayEventTypeV1.SCHEDULED_INFORMATION,
            work=work,
            data=scheduled_data,
            source_component_id="FULL_DAY_RUNTIME_V1",
        )
        event_payload = raw_event.as_dict()
        event_payload["global_event_sequence"] = 4
        event_payload["component_local_sequence"] = 4
        validate_full_day_event_suffix(
            (FullDayEventV1.from_dict(event_payload),),
            executed_work_items={work.work_id: work},
            native_event_ledger={},
            scheduled_event_ledger={"WRONG_LEDGER_KEY": scheduled_row},
            full_day_plan=scheduled_plan,
            verified_prefix_cut=genesis_cut,
            verified_prefix_events=genesis_prefix_events,
        )

    try:
        validate_scheduled_projection(
            scheduled_data, scheduled_row.simulation_time_us, (scheduled_row,)
        )
    except (TypeError, ValueError) as error:
        failures.append(f"valid scheduled-information ledger projection was refused: {error}")
    for label, field, value in (
        ("scheduled event ID", "scheduled_event_id", "AUDIT_OTHER_EVENT"),
        ("scheduled event type", "scheduled_event_type", "HALT"),
        ("scheduled event side", "side", "SELL"),
        ("scheduled parameter digest", "parameter_set_sha256", "0" * 64),
    ):
        mutated = dict(scheduled_data)
        mutated[field] = value
        probes.append(
            (
                f"{label} differs from authoritative ledger",
                lambda mutated=mutated: validate_scheduled_projection(
                    mutated, scheduled_row.simulation_time_us, (scheduled_row,)
                ),
            )
        )
    probes.extend(
        (
            (
                "scheduled event time differs from authoritative ledger",
                lambda: validate_scheduled_projection(
                    scheduled_data,
                    scheduled_row.simulation_time_us + 1,
                    (scheduled_row,),
                ),
            ),
            (
                "missing scheduled event ledger row",
                lambda: validate_scheduled_projection(
                    scheduled_data, scheduled_row.simulation_time_us, ()
                ),
            ),
            (
                "extra scheduled event ledger row",
                lambda: validate_scheduled_projection(
                    scheduled_data,
                    scheduled_row.simulation_time_us,
                    (scheduled_row, invented_scheduled_row),
                ),
            ),
            (
                "scheduled ledger key differs from embedded plan row",
                validate_mismatched_scheduled_ledger_key,
            ),
            (
                "caller-invented self-consistent scheduled row absent from plan",
                lambda: validate_scheduled_projection(
                    {
                        "parameter_set_sha256": (
                            invented_scheduled_row.parameter_set_sha256
                        ),
                        "scheduled_event_id": invented_scheduled_row.event_id,
                        "scheduled_event_type": (
                            invented_scheduled_row.event_type.value
                        ),
                        "side": invented_scheduled_row.side.value,
                    },
                    invented_scheduled_row.simulation_time_us,
                    (invented_scheduled_row,),
                ),
            ),
            (
                "plan scheduled event due by horizon but omitted",
                lambda: validate_stream(
                    (
                        resource_abort_event(
                            1,
                            1,
                            make_work(
                                scheduled_row.simulation_time_us + 1,
                                WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
                                "FULL_DAY_RUNTIME_V1",
                            ),
                            "FULL_DAY_RUNTIME_V1",
                            make_work(
                                scheduled_row.simulation_time_us + 1,
                                WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
                                "FULL_DAY_RUNTIME_V1",
                            ).work_id,
                        ),
                    ),
                    (
                        make_work(
                            scheduled_row.simulation_time_us + 1,
                            WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
                            "FULL_DAY_RUNTIME_V1",
                        ),
                    ),
                    full_day_plan=scheduled_plan,
                ),
            ),
        )
    )

    # Shock decisions are one candidate followed synchronously by exactly one outcome.
    shock_work = make_work(
        40_000, WorkStageV1.SCHEDULED_INFORMATION, "FULL_DAY_RUNTIME_V1"
    )
    shock_candidate_data = {
        "candidate_id": "CANDIDATE_1",
        "information_cutoff_us": 40_000,
        "quantity_shares": 100,
        "side": "BUY",
    }

    def shock_event(
        event_type: FullDayEventTypeV1,
        data: dict[str, object],
        sequence: int,
        parent_id: str,
    ) -> FullDayEventV1:
        return make_event(
            event_type=event_type,
            work=shock_work,
            data=data,
            source_component_id="FULL_DAY_RUNTIME_V1",
            global_sequence=sequence,
            component_local_sequence=sequence,
            causal_parent_id=parent_id,
        )

    candidate_event = shock_event(
        FullDayEventTypeV1.SHOCK_CANDIDATE,
        shock_candidate_data,
        1,
        shock_work.work_id,
    )
    accepted_data = dict(shock_candidate_data)
    accepted_event = shock_event(
        FullDayEventTypeV1.SHOCK_ACCEPTED,
        accepted_data,
        2,
        candidate_event.event_id,
    )
    rejected_data = {
        "candidate_id": "CANDIDATE_1",
        "information_cutoff_us": 40_000,
        "reason_code": "POLICY_REJECTION",
    }
    rejected_event = shock_event(
        FullDayEventTypeV1.SHOCK_REJECTED,
        rejected_data,
        2,
        candidate_event.event_id,
    )
    try:
        validate_stream((candidate_event, accepted_event), (shock_work,))
        validate_stream((candidate_event, rejected_event), (shock_work,))
    except (TypeError, ValueError) as error:
        failures.append(f"valid shock candidate lifecycle was refused: {error}")

    orphan_terminal = shock_event(
        FullDayEventTypeV1.SHOCK_ACCEPTED,
        accepted_data,
        1,
        shock_work.work_id,
    )
    duplicate_terminal = shock_event(
        FullDayEventTypeV1.SHOCK_REJECTED,
        rejected_data,
        3,
        accepted_event.event_id,
    )
    mismatched_cutoff_data = dict(accepted_data)
    mismatched_cutoff_data["information_cutoff_us"] = 39_999
    mismatched_quantity_data = dict(accepted_data)
    mismatched_quantity_data["quantity_shares"] = 101
    mismatched_side_data = dict(accepted_data)
    mismatched_side_data["side"] = "SELL"
    mismatched_rejection_cutoff = dict(rejected_data)
    mismatched_rejection_cutoff["information_cutoff_us"] = 39_999
    duplicate_candidate = shock_event(
        FullDayEventTypeV1.SHOCK_CANDIDATE,
        shock_candidate_data,
        2,
        candidate_event.event_id,
    )
    shock_lifecycle_probes = (
        ("orphan shock terminal", (orphan_terminal,)),
        ("unresolved shock candidate", (candidate_event,)),
        (
            "duplicate shock terminal",
            (candidate_event, accepted_event, duplicate_terminal),
        ),
        (
            "shock terminal cutoff mismatch",
            (
                candidate_event,
                shock_event(
                    FullDayEventTypeV1.SHOCK_ACCEPTED,
                    mismatched_cutoff_data,
                    2,
                    candidate_event.event_id,
                ),
            ),
        ),
        (
            "accepted shock quantity mismatch",
            (
                candidate_event,
                shock_event(
                    FullDayEventTypeV1.SHOCK_ACCEPTED,
                    mismatched_quantity_data,
                    2,
                    candidate_event.event_id,
                ),
            ),
        ),
        (
            "accepted shock side mismatch",
            (
                candidate_event,
                shock_event(
                    FullDayEventTypeV1.SHOCK_ACCEPTED,
                    mismatched_side_data,
                    2,
                    candidate_event.event_id,
                ),
            ),
        ),
        (
            "rejected shock cutoff mismatch",
            (
                candidate_event,
                shock_event(
                    FullDayEventTypeV1.SHOCK_REJECTED,
                    mismatched_rejection_cutoff,
                    2,
                    candidate_event.event_id,
                ),
            ),
        ),
        ("duplicate shock candidate ID", (candidate_event, duplicate_candidate)),
    )
    for label, events in shock_lifecycle_probes:
        probes.append(
            (label, lambda events=events: validate_stream(events, (shock_work,)))
        )
    spoofed_candidate = copy.deepcopy(candidate_event.as_dict())
    spoofed_candidate["source_component_id"] = "SPOOFED_SHOCK_RUNTIME"
    probes.append(
        (
            "shock candidate source spoof",
            lambda: validate_stream(
                (FullDayEventV1.from_dict(spoofed_candidate), accepted_event),
                (shock_work,),
            ),
        )
    )

    # Capture emits a pre-capture marker. The cut binds the actual marker-inclusive
    # canonical prefix, so the marker never needs to contain its own digest.
    suffix_work = make_work(
        50,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "FULL_DAY_RUNTIME_V1",
        4,
    )
    suffix_event = resource_abort_event(
        4,
        4,
        suffix_work,
        "FULL_DAY_RUNTIME_V1",
        suffix_work.work_id,
    )
    try:
        validate_full_day_event_suffix(
            (suffix_event,),
            executed_work_items={suffix_work.work_id: suffix_work},
            native_event_ledger={},
            scheduled_event_ledger={},
            full_day_plan=contract_plan,
            verified_prefix_cut=genesis_cut,
            verified_prefix_events=genesis_prefix_events,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"valid post-marker checkpoint suffix was refused: {error}")
    if (
        genesis_cut.event_prefix_sha256
        != canonical_event_prefix_sha256(genesis_prefix_events)
        or set(genesis_marker_event.payload.data) != {"checkpoint_request_id"}
    ):
        failures.append("checkpoint marker/cut retained a self-referential prefix field")
    forged_cut = replace(genesis_cut, event_prefix_sha256="0" * 64)
    empty_prefix_cut = replace(
        genesis_cut,
        last_global_event_sequence=0,
        event_prefix_last_global_sequence=0,
        event_prefix_sha256=canonical_event_prefix_sha256(()),
    )
    empty_prefix_suffix_event = resource_abort_event(
        1,
        1,
        suffix_work,
        "FULL_DAY_RUNTIME_V1",
        suffix_work.work_id,
    )
    stale_component_sequence = copy.deepcopy(suffix_event.as_dict())
    stale_component_sequence["component_local_sequence"] = 3
    probes.append(
        (
            "forged checkpoint cut digest against actual prefix",
            lambda: validate_full_day_event_suffix(
                (suffix_event,),
                executed_work_items={suffix_work.work_id: suffix_work},
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=contract_plan,
                verified_prefix_cut=forged_cut,
                verified_prefix_events=genesis_prefix_events,
            ),
        ),
    )
    probes.append(
        (
            "suffix component allocator reuses checkpoint-prefix sequence",
            lambda: validate_full_day_event_suffix(
                (FullDayEventV1.from_dict(stale_component_sequence),),
                executed_work_items={suffix_work.work_id: suffix_work},
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=contract_plan,
                verified_prefix_cut=genesis_cut,
                verified_prefix_events=genesis_prefix_events,
            ),
        )
    )
    probes.append(
        (
            "synthetic empty checkpoint prefix",
            lambda: validate_full_day_event_suffix(
                (empty_prefix_suffix_event,),
                executed_work_items={suffix_work.work_id: suffix_work},
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=contract_plan,
                verified_prefix_cut=empty_prefix_cut,
                verified_prefix_events=(),
            ),
        )
    )

    # A second real cut proves native owner/ledger allocators continue across restore.
    prefix_native_reference = NativeEventReferenceV1(
        1,
        "ENGINE_MARKET_MECHANICS_V1",
        "RESTORE_NATIVE_LEDGER_V1",
        "TRADE",
        2,
        "restore-native:event:2",
    )
    prefix_native_entry = NativeLedgerEntryV1(
        prefix_native_reference, {"price_ticks": 10_000, "quantity_shares": 1}
    )
    prefix_native_work = make_work(
        0,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "ENGINE_MARKET_MECHANICS_V1",
        1,
    )
    prefix_native_event = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=prefix_native_work,
        data={"native_payload_sha256": prefix_native_entry.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        global_sequence=3,
        component_local_sequence=1,
        native_event=prefix_native_reference,
    )
    native_cut_marker = make_event(
        event_type=FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER,
        work=genesis_marker_work,
        data={"checkpoint_request_id": "NATIVE_ALLOCATOR_CHECKPOINT"},
        source_component_id="FULL_DAY_RUNTIME_V1",
        global_sequence=4,
        component_local_sequence=3,
    )
    native_prefix_events = (
        genesis_boundary_event,
        genesis_anchor_event,
        prefix_native_event,
        native_cut_marker,
    )
    native_prefix_works = (
        genesis_boundary_work,
        genesis_anchor_work,
        prefix_native_work,
        genesis_marker_work,
    )
    native_prefix_cut = replace(
        genesis_cut,
        last_global_event_sequence=4,
        event_prefix_last_global_sequence=4,
        event_prefix_sha256=canonical_event_prefix_sha256(native_prefix_events),
    )
    try:
        validate_full_day_event_stream(
            native_prefix_events,
            executed_work_items={item.work_id: item for item in native_prefix_works},
            native_event_ledger={prefix_native_entry.ledger_key: prefix_native_entry},
            scheduled_event_ledger={},
            full_day_plan=contract_plan,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"valid native allocator checkpoint prefix was refused: {error}")

    suffix_native_work = make_work(
        50,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "ENGINE_MARKET_MECHANICS_V1",
        2,
    )
    reused_native_suffix_event = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=suffix_native_work,
        data={"native_payload_sha256": prefix_native_entry.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        global_sequence=5,
        component_local_sequence=2,
        native_event=prefix_native_reference,
    )
    backstep_reference = NativeEventReferenceV1(
        1,
        "ENGINE_MARKET_MECHANICS_V1",
        "RESTORE_NATIVE_LEDGER_V1",
        "TRADE",
        1,
        "restore-native:event:1",
    )
    backstep_entry = NativeLedgerEntryV1(
        backstep_reference, {"price_ticks": 9_999, "quantity_shares": 1}
    )
    backstep_native_suffix_event = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=suffix_native_work,
        data={"native_payload_sha256": backstep_entry.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        global_sequence=5,
        component_local_sequence=2,
        native_event=backstep_reference,
    )
    for label, event, entry in (
        (
            "suffix reuses checkpoint-prefix native identity",
            reused_native_suffix_event,
            prefix_native_entry,
        ),
        (
            "suffix backsteps checkpoint-prefix native ledger sequence",
            backstep_native_suffix_event,
            backstep_entry,
        ),
    ):
        probes.append(
            (
                label,
                lambda event=event, entry=entry: validate_full_day_event_suffix(
                    (event,),
                    executed_work_items={
                        suffix_native_work.work_id: suffix_native_work
                    },
                    native_event_ledger={entry.ledger_key: entry},
                    scheduled_event_ledger={},
                    full_day_plan=contract_plan,
                    verified_prefix_cut=native_prefix_cut,
                    verified_prefix_events=native_prefix_events,
                ),
            )
        )

    # Native ledger authority and typed projection hostility.
    delivery_event, delivery_work, delivery_entry = native_fixtures[
        FullDayEventTypeV1.OBSERVABLE_DELIVERY
    ]
    delivery_no_native = copy.deepcopy(delivery_event.as_dict())
    delivery_no_native["payload"]["native_event"] = None  # type: ignore[index]
    delivery_wrong_owner = copy.deepcopy(delivery_event.as_dict())
    delivery_wrong_owner["payload"]["native_event"][  # type: ignore[index]
        "owner_component_id"
    ] = "WRONG_DELIVERY_OWNER"
    delivery_wrong_native_type = copy.deepcopy(delivery_event.as_dict())
    delivery_wrong_native_type["payload"]["native_event"][  # type: ignore[index]
        "event_type"
    ] = "FEATURE_UPDATED"
    forged_delivery_payload = dict(delivery_entry.payload)
    forged_delivery_payload["message_id"] = "FORGED_MESSAGE"
    forged_delivery_entry = NativeLedgerEntryV1(
        delivery_entry.reference, forged_delivery_payload
    )
    projection_mismatch = copy.deepcopy(delivery_event.as_dict())
    projection_mismatch["payload"]["data"][  # type: ignore[index]
        "native_payload_sha256"
    ] = forged_delivery_entry.payload_sha256
    forged_both = copy.deepcopy(delivery_event.as_dict())
    forged_both["payload"]["data"]["message_id"] = "FORGED_MESSAGE"  # type: ignore[index]
    forged_both["payload"]["data"][  # type: ignore[index]
        "native_payload_sha256"
    ] = forged_delivery_entry.payload_sha256
    extra_native_reference = NativeEventReferenceV1(
        1, "UNUSED_NATIVE_V1", "UNUSED_LEDGER_V1", "UNUSED", 1, "unused:event:1"
    )
    extra_native_entry = NativeLedgerEntryV1(extra_native_reference, {"unused": True})
    native_reference_mismatch = copy.deepcopy(delivery_event.as_dict())
    native_reference_mismatch["payload"]["native_event"][  # type: ignore[index]
        "event_id"
    ] = "delivery:event:forged"
    probes.extend(
        (
            (
                "typed native outer event missing native identity",
                lambda: FullDayEventV1.from_dict(delivery_no_native),
            ),
            (
                "native owner differs from outer source",
                lambda: FullDayEventV1.from_dict(delivery_wrong_owner),
            ),
            (
                "typed outer event uses wrong native event family",
                lambda: FullDayEventV1.from_dict(delivery_wrong_native_type),
            ),
            (
                "typed native projection differs from matching digest row",
                lambda: validate_stream(
                    (FullDayEventV1.from_dict(projection_mismatch),),
                    (delivery_work,),
                    (forged_delivery_entry,),
                ),
            ),
            (
                "forged outer projection and digest differ from authoritative row",
                lambda: validate_stream(
                    (FullDayEventV1.from_dict(forged_both),),
                    (delivery_work,),
                    (delivery_entry,),
                ),
            ),
            (
                "missing native ledger row",
                lambda: validate_stream((delivery_event,), (delivery_work,)),
            ),
            (
                "extra native ledger row",
                lambda: validate_stream(
                    (delivery_event,),
                    (delivery_work,),
                    (delivery_entry, extra_native_entry),
                ),
            ),
            (
                "native ledger key differs from embedded reference",
                lambda: validate_full_day_event_stream(
                    (delivery_event,),
                    executed_work_items={delivery_work.work_id: delivery_work},
                    native_event_ledger={delivery_entry.ledger_key: extra_native_entry},
                    scheduled_event_ledger={},
                    full_day_plan=contract_plan,
                ),
            ),
            (
                "outer native reference is absent from authoritative ledger",
                lambda: validate_stream(
                    (FullDayEventV1.from_dict(native_reference_mismatch),),
                    (delivery_work,),
                    (delivery_entry,),
                ),
            ),
        )
    )

    descending_work = make_work(
        60_000,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "ENGINE_MARKET_MECHANICS_V1",
    )
    descending_ref_two = NativeEventReferenceV1(
        1,
        "ENGINE_MARKET_MECHANICS_V1",
        "MECHANICS_EVENT_LEDGER_V1",
        "TRADE",
        2,
        "mechanics:event:2",
    )
    descending_ref_one = NativeEventReferenceV1(
        1,
        "ENGINE_MARKET_MECHANICS_V1",
        "MECHANICS_EVENT_LEDGER_V1",
        "FULL_FILL",
        1,
        "mechanics:event:1",
    )
    descending_entry_two = NativeLedgerEntryV1(descending_ref_two, {"row": 2})
    descending_entry_one = NativeLedgerEntryV1(descending_ref_one, {"row": 1})
    descending_event_two = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=descending_work,
        data={"native_payload_sha256": descending_entry_two.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        component_local_sequence=1,
        native_event=descending_ref_two,
    )
    descending_event_one = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=descending_work,
        data={"native_payload_sha256": descending_entry_one.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        global_sequence=2,
        component_local_sequence=2,
        causal_parent_id=descending_event_two.event_id,
        native_event=descending_ref_one,
    )
    duplicate_native_event = make_event(
        event_type=FullDayEventTypeV1.SUBSYSTEM_EVENT,
        work=dual_work,
        data={"native_payload_sha256": dual_entry_a.payload_sha256},
        source_component_id="ENGINE_MARKET_MECHANICS_V1",
        global_sequence=2,
        component_local_sequence=2,
        causal_parent_id=event_one.event_id,
        native_event=dual_ref_a,
    )
    probes.extend(
        (
            (
                "backward native local sequence within one owner/ledger",
                lambda: validate_stream(
                    (descending_event_two, descending_event_one),
                    (descending_work,),
                    (descending_entry_two, descending_entry_one),
                ),
            ),
            (
                "duplicate native event identity in outer stream",
                lambda: validate_stream(
                    (event_one, duplicate_native_event),
                    (dual_work,),
                    (dual_entry_a,),
                ),
            ),
        )
    )

    probes.extend(
        (
            (
                "same-microstep deferred work",
                lambda: validate_deferred_work_key(
                    parent,
                    ScheduledWorkKeyV1(
                        1_000,
                        0,
                        WorkStageV1.OBSERVABLE_CLIENT_DELIVERY,
                        "CLIENT_RUNTIME",
                        0,
                    ),
                    maximum_microsteps,
                ),
            ),
            (
                "backward deferred work",
                lambda: validate_deferred_work_key(
                    parent,
                    ScheduledWorkKeyV1(
                        999,
                        0,
                        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
                        "PARTICIPANT_MAKER_1",
                        5,
                    ),
                    maximum_microsteps,
                ),
            ),
            (
                "microstep overflow",
                lambda: validate_deferred_work_key(
                    parent,
                    ScheduledWorkKeyV1(
                        1_000,
                        maximum_microsteps,
                        WorkStageV1.DAY_STATE_TRANSITION,
                        "STATE_RUNTIME",
                        2,
                    ),
                    maximum_microsteps,
                ),
            ),
            (
                "future-time nonzero microstep",
                lambda: validate_deferred_work_key(
                    parent,
                    ScheduledWorkKeyV1(
                        1_001,
                        1,
                        WorkStageV1.OBSERVABLE_CLIENT_DELIVERY,
                        "CLIENT_RUNTIME",
                        1,
                    ),
                    maximum_microsteps,
                ),
            ),
            (
                "regenerated calendar stage",
                lambda: ScheduledWorkKeyV1(
                    1_000,
                    1,
                    WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
                    "FULL_DAY_RUNTIME",
                    1,
                ),
            ),
            (
                "regenerated information stage",
                lambda: ScheduledWorkKeyV1(
                    1_000,
                    1,
                    WorkStageV1.SCHEDULED_INFORMATION,
                    "INFORMATION_RUNTIME",
                    1,
                ),
            ),
        )
    )

    runtime_event_one = resource_abort_event(
        1, 1, parent, "FULL_DAY_RUNTIME_V1", parent.work_id
    )
    runtime_event_duplicate_sequence = resource_abort_event(
        2, 1, parent, "FULL_DAY_RUNTIME_V1", runtime_event_one.event_id
    )
    runtime_event_backward_sequence = resource_abort_event(
        2, 0, parent, "FULL_DAY_RUNTIME_V1", runtime_event_one.event_id
    )
    same_stage_work_a = make_work(
        70_000,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "PARTICIPANT_A",
    )
    same_stage_work_z = make_work(
        70_000,
        WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
        "PARTICIPANT_Z",
    )
    reversed_work_events = (
        resource_abort_event(
            1, 1, same_stage_work_z, "RUNTIME_Z", same_stage_work_z.work_id
        ),
        resource_abort_event(
            2, 1, same_stage_work_a, "RUNTIME_A", same_stage_work_a.work_id
        ),
    )
    wrong_payload_type = copy.deepcopy(runtime_event_one.as_dict())
    wrong_payload_type["payload"]["payload_type"] = "CAPABILITY_REFUSED"  # type: ignore[index]
    orphan_work = copy.deepcopy(runtime_event_one.as_dict())
    orphan_work["causal_parent_ids"] = [f"work:{'f' * 64}"]
    non_exceeding_resource_abort = copy.deepcopy(runtime_event_one.as_dict())
    non_exceeding_resource_abort["payload"]["data"][  # type: ignore[index]
        "observed_value"
    ] = 10
    future_parent = copy.deepcopy(runtime_event_duplicate_sequence.as_dict())
    future_parent["causal_parent_ids"] = ["event:2"]
    multiple_causal_parents = copy.deepcopy(runtime_event_duplicate_sequence.as_dict())
    multiple_causal_parents["causal_parent_ids"] = [
        parent.work_id,
        runtime_event_one.event_id,
    ]
    later_work = make_work(
        parent.simulation_time_us + 1,
        WorkStageV1.OBSERVABLE_CLIENT_DELIVERY,
        "FULL_DAY_RUNTIME_V1",
    )
    later_event = resource_abort_event(
        2, 2, later_work, "FULL_DAY_RUNTIME_V1", runtime_event_one.event_id
    )
    noncontiguous = copy.deepcopy(runtime_event_duplicate_sequence.as_dict())
    noncontiguous["global_event_sequence"] = 3
    reemitted_prefix_sequence = copy.deepcopy(suffix_event.as_dict())
    reemitted_prefix_sequence["global_event_sequence"] = 1
    prefix_parent_in_suffix = copy.deepcopy(suffix_event.as_dict())
    prefix_parent_in_suffix["causal_parent_ids"] = [genesis_marker_event.event_id]
    lower_bound_work = make_work(
        0,
        WorkStageV1.STRATEGY_ALGORITHM_DEADLINE,
        "FULL_DAY_RUNTIME_V1",
        4,
    )
    lower_bound_suffix_event = resource_abort_event(
        4,
        4,
        lower_bound_work,
        "FULL_DAY_RUNTIME_V1",
        lower_bound_work.work_id,
    )
    chronological_first_work = make_work(
        80_000,
        WorkStageV1.OBSERVABLE_CLIENT_DELIVERY,
        "FULL_DAY_RUNTIME_V1",
    )
    chronological_first = resource_abort_event(
        1,
        1,
        chronological_first_work,
        "FULL_DAY_RUNTIME_V1",
        chronological_first_work.work_id,
    )
    chronological_second = FullDayEventV1(
        1,
        2,
        chronological_first_work.simulation_time_us,
        0,
        WorkStageV1.PENDING_VENUE_ARRIVAL,
        "FULL_DAY_RUNTIME_V1",
        2,
        FullDayEventTypeV1.RESOURCE_LIMIT_ABORT,
        (chronological_first.event_id,),
        FullDayEventPayloadV1(
            1,
            FullDayEventTypeV1.RESOURCE_LIMIT_ABORT.value,
            1,
            None,
            {"limit_id": "TRACE", "maximum_value": 10, "observed_value": 11},
        ),
    )
    probes.extend(
        (
            (
                "event payload type mismatch",
                lambda: FullDayEventV1.from_dict(wrong_payload_type),
            ),
            (
                "orphan causal work identity",
                lambda: validate_stream(
                    (FullDayEventV1.from_dict(orphan_work),), (parent,)
                ),
            ),
            (
                "deferred event bypassing child work identity",
                lambda: validate_stream(
                    (runtime_event_one, later_event), (parent, later_work)
                ),
            ),
            (
                "resource abort without a strict limit exceedance",
                lambda: FullDayEventV1.from_dict(non_exceeding_resource_abort),
            ),
            ("future causal parent", lambda: FullDayEventV1.from_dict(future_parent)),
            (
                "multiple causal parents with one effective work lineage",
                lambda: FullDayEventV1.from_dict(multiple_causal_parents),
            ),
            (
                "duplicate runtime component-local sequence",
                lambda: validate_stream(
                    (runtime_event_one, runtime_event_duplicate_sequence), (parent,)
                ),
            ),
            (
                "backward runtime component-local sequence",
                lambda: validate_stream(
                    (runtime_event_one, runtime_event_backward_sequence), (parent,)
                ),
            ),
            (
                "reversed same-stage dequeued work order",
                lambda: validate_stream(
                    reversed_work_events,
                    (same_stage_work_a, same_stage_work_z),
                ),
            ),
            (
                "checkpoint prefix re-emitted in suffix sequence",
                lambda: validate_full_day_event_suffix(
                    (FullDayEventV1.from_dict(reemitted_prefix_sequence),),
                    executed_work_items={suffix_work.work_id: suffix_work},
                    native_event_ledger={},
                    scheduled_event_ledger={},
                    full_day_plan=contract_plan,
                    verified_prefix_cut=genesis_cut,
                    verified_prefix_events=genesis_prefix_events,
                ),
            ),
            (
                "suffix event citing a quiescent checkpoint-prefix event",
                lambda: validate_full_day_event_suffix(
                    (FullDayEventV1.from_dict(prefix_parent_in_suffix),),
                    executed_work_items={suffix_work.work_id: suffix_work},
                    native_event_ledger={},
                    scheduled_event_ledger={},
                    full_day_plan=contract_plan,
                    verified_prefix_cut=genesis_cut,
                    verified_prefix_events=genesis_prefix_events,
                ),
            ),
            (
                "suffix event moves before checkpoint chronological lower bound",
                lambda: validate_full_day_event_suffix(
                    (lower_bound_suffix_event,),
                    executed_work_items={lower_bound_work.work_id: lower_bound_work},
                    native_event_ledger={},
                    scheduled_event_ledger={},
                    full_day_plan=contract_plan,
                    verified_prefix_cut=genesis_cut,
                    verified_prefix_events=genesis_prefix_events,
                ),
            ),
            (
                "noncontiguous global event sequence",
                lambda: validate_stream(
                    (
                        runtime_event_one,
                        FullDayEventV1.from_dict(noncontiguous),
                    ),
                    (parent,),
                ),
            ),
            (
                "chronologically misordered outer events",
                lambda: validate_stream(
                    (chronological_first, chronological_second),
                    (chronological_first_work,),
                ),
            ),
        )
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)

    event_digest = canonical_event_prefix_sha256((event_one, event_two))
    return FullDayAuditCase(
        "scheduled_work_microsteps_and_outer_event_envelope",
        (
            "five-field FIFO order, bounded deferral, exactly 21 payload families, "
            f"{payload_field_probe_count} every-field missing/type refusals, "
            f"{payload_enum_probe_count} enum refusals, six boundary tuples, zero-duration "
            "acyclic state/transition/anchor evidence, exhaustive typed native contexts, "
            "independent outer/native-ledger allocators, scheduled-ledger projection, "
            "exact shock outcomes, and a marker-inclusive checkpoint cut; "
            f"hostile_probes={len(probes)} stream_sha256={event_digest}"
        ),
        tuple(failures),
    )


def _composition_case() -> FullDayAuditCase:
    from kirby2.full_day.composition import (
        AGENT_SCHEDULER_COMPONENT,
        FULL_DAY_RUNTIME_COMPONENT,
        MECHANICS_COMPONENT,
        ComponentSpecV1,
        CompositionMatrixV1,
        CompositionProfileV1,
        component_configured_predicate,
        initial_composition_matrix,
    )

    matrix = initial_composition_matrix()
    profile = matrix.profiles[0]
    plan = _sample_plan()
    failures: list[str] = []
    restored = CompositionMatrixV1.from_json_bytes(matrix.canonical_bytes())
    if restored.as_dict() != matrix.as_dict() or restored.sha256 != matrix.sha256:
        failures.append("composition matrix canonical round trip changed identity")
    expected_refused_families = {
        "AGENT_ECOLOGY_COMPATIBILITY_WRAPPER",
        "ALGORITHMS",
        "ASYNCHRONOUS_EXECUTION_SESSION",
        "FEATURES",
        "FLOW_HAWKES",
        "FLOW_QUEUE_REACTIVE",
        "FLOW_SIMPLE",
        "HIDDEN_LIQUIDITY",
        "HISTORICAL_REPLAY",
        "MULTIVENUE_ROUTING",
        "PLAYER_OVERLAY",
        "REGIME_ORDER_FLOW",
        "STRATEGIES",
    }
    if set(profile.refused_component_ids) != expected_refused_families:
        failures.append("initial profile does not refuse every named unsupported family")

    predicate_values = profile.predicate_values_for_plan_bindings(
        plan.selected_component_ids,
        participant_schedule_nonempty=bool(plan.participant_schedule),
        any_participant_initially_active=any(
            item.initially_active for item in plan.participant_definitions
        ),
    )
    active = profile.resolve_active_components(predicate_values)
    try:
        profile.validate_activation(active, predicate_values)
    except (TypeError, ValueError) as error:
        failures.append(f"declared initial activation was refused: {error}")
    initially_active_only = profile.predicate_values_for_plan_bindings(
        plan.selected_component_ids,
        participant_schedule_nonempty=False,
        any_participant_initially_active=True,
    )
    if AGENT_SCHEDULER_COMPONENT not in profile.resolve_active_components(
        initially_active_only
    ):
        failures.append("initially-active participant did not retain AgentScheduler")

    promoted_component_ids = {AGENT_SCHEDULER_COMPONENT, MECHANICS_COMPONENT}
    promoted_components = tuple(
        replace(
            component,
            component_version=component.component_version + 1,
            implementation_status="EXECUTABLE",
        )
        if component.component_id in promoted_component_ids
        else component
        for component in profile.components
    )
    promoted_profile = replace(
        profile,
        profile_version=2,
        implementation_status="EXECUTABLE",
        components=promoted_components,
    )
    successor = CompositionMatrixV1(
        schema_version=1,
        matrix_id=matrix.matrix_id,
        matrix_version=2,
        previous_matrix_sha256=matrix.sha256,
        profiles=(profile, promoted_profile),
    )
    try:
        matrix.validate_append_only_successor(successor)
    except (TypeError, ValueError) as error:
        failures.append(f"append-only executable promotion was refused: {error}")
    if (
        successor.profile(profile.profile_id, 1).canonical_bytes()
        != profile.canonical_bytes()
        or successor.profile(profile.profile_id).profile_version != 2
    ):
        failures.append("append-only profile revision changed old meaning or latest lookup")

    flow_component_ids = (
        "FLOW_HAWKES_V1",
        "FLOW_QUEUE_REACTIVE_V1",
        "FLOW_SIMPLE_V1",
    )
    flow_specs = tuple(
        ComponentSpecV1(
            schema_version=1,
            component_id=component_id,
            component_version=1,
            implementation_status="CONTRACT_ONLY",
            active_predicate=component_configured_predicate(component_id),
            dependencies=tuple(
                sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT})
            ),
            owned_resources=(f"{component_id}_STATE",),
            borrowed_resources=("ORDER_GATEWAY", "SIMULATION_CLOCK"),
            rng_label_prefixes={
                "FLOW_HAWKES_V1": ("full_day/flow/hawkes",),
                "FLOW_QUEUE_REACTIVE_V1": (
                    "full_day/flow/queue_reactive",
                ),
                "FLOW_SIMPLE_V1": ("full_day/flow/simple",),
            }[component_id],
            checkpoint_state_ids=(component_id,),
        )
        for component_id in flow_component_ids
    )
    flow_selection_profile = CompositionProfileV1(
        schema_version=1,
        profile_id="AUDIT_FLOW_SELECTION_V1",
        profile_version=1,
        implementation_status="CONTRACT_ONLY",
        runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
        components=tuple(
            sorted((*profile.components, *flow_specs), key=lambda item: item.component_id)
        ),
        refused_component_ids=(),
        exactly_one_component_groups=(flow_component_ids,),
    )
    try:
        one_flow_predicates = flow_selection_profile.predicate_values_for_plan_bindings(
            (*plan.selected_component_ids, "FLOW_SIMPLE_V1"),
            participant_schedule_nonempty=True,
            any_participant_initially_active=False,
        )
        if "FLOW_SIMPLE_V1" not in flow_selection_profile.resolve_active_components(
            one_flow_predicates
        ):
            failures.append("one selected flow adapter did not activate")
    except (TypeError, ValueError) as error:
        failures.append(f"valid exactly-one flow selection was refused: {error}")

    delivery_spec = ComponentSpecV1(
        schema_version=1,
        component_id="DELIVERY_ASYNC_V1",
        component_version=1,
        implementation_status="CONTRACT_ONLY",
        active_predicate=component_configured_predicate("DELIVERY_ASYNC_V1"),
        dependencies=tuple(
            sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT})
        ),
        owned_resources=("DELIVERY_ASYNC_STATE",),
        borrowed_resources=("ORDER_GATEWAY", "SIMULATION_CLOCK"),
        rng_label_prefixes=("full_day/delivery",),
        checkpoint_state_ids=("PENDING_LATENCY_CLIENT_DELIVERY_V1",),
    )
    required_delivery_profile = CompositionProfileV1(
        schema_version=1,
        profile_id="AUDIT_REQUIRED_DELIVERY_V1",
        profile_version=1,
        implementation_status="CONTRACT_ONLY",
        runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
        components=tuple(
            sorted((*profile.components, delivery_spec), key=lambda item: item.component_id)
        ),
        refused_component_ids=(),
        exactly_one_component_groups=(("DELIVERY_ASYNC_V1",),),
    )
    try:
        required_delivery_profile.predicate_values_for_plan_bindings(
            (*plan.selected_component_ids, "DELIVERY_ASYNC_V1"),
            participant_schedule_nonempty=True,
            any_participant_initially_active=False,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"configured required delivery adapter was refused: {error}")

    duplicate_owner = copy.deepcopy(matrix.as_dict())
    components = duplicate_owner["profiles"][0]["components"]  # type: ignore[index]
    mechanics = next(
        item for item in components if item["component_id"] == "ENGINE_MARKET_MECHANICS_V1"
    )
    mechanics["owned_resources"] = ["ORDER_BOOK"]
    duplicate_calendar = copy.deepcopy(matrix.as_dict())
    components_with_calendar = duplicate_calendar["profiles"][0]["components"]  # type: ignore[index]
    mechanics_with_calendar = next(
        item
        for item in components_with_calendar
        if item["component_id"] == "ENGINE_MARKET_MECHANICS_V1"
    )
    mechanics_with_calendar["owned_resources"] = ["SESSION_CALENDAR"]

    class ComponentSpecSubclass(ComponentSpecV1):
        pass

    forged_component = ComponentSpecSubclass.from_dict(
        profile.components[0].as_dict()
    )
    subclass_components = tuple(
        forged_component if index == 0 else component
        for index, component in enumerate(profile.components)
    )
    probes = (
        (
            "duplicate book owner",
            lambda: CompositionMatrixV1.from_dict(duplicate_owner),
        ),
        (
            "double calendar owner",
            lambda: CompositionMatrixV1.from_dict(duplicate_calendar),
        ),
        (
            "active component omission",
            lambda: profile.validate_activation(active[:-1], predicate_values),
        ),
        (
            "unsupported component activation",
            lambda: profile.validate_activation(
                (*active, profile.refused_component_ids[0]), predicate_values
            ),
        ),
        (
            "unknown composition profile",
            lambda: matrix.profile("ENABLE_EVERYTHING"),
        ),
        (
            "zero selected flow adapters in an exactly-one group",
            lambda: flow_selection_profile.predicate_values_for_plan_bindings(
                plan.selected_component_ids,
                participant_schedule_nonempty=True,
                any_participant_initially_active=False,
            ),
        ),
        (
            "two selected flow adapters in an exactly-one group",
            lambda: flow_selection_profile.predicate_values_for_plan_bindings(
                (*plan.selected_component_ids, "FLOW_HAWKES_V1", "FLOW_SIMPLE_V1"),
                participant_schedule_nonempty=True,
                any_participant_initially_active=False,
            ),
        ),
        (
            "required singleton delivery adapter omitted from plan bindings",
            lambda: required_delivery_profile.predicate_values_for_plan_bindings(
                plan.selected_component_ids,
                participant_schedule_nonempty=True,
                any_participant_initially_active=False,
            ),
        ),
        (
            "boolean component schema version",
            lambda: replace(profile.components[0], schema_version=True),
        ),
        (
            "boolean profile schema version",
            lambda: replace(profile, schema_version=True),
        ),
        (
            "boolean matrix schema version",
            lambda: replace(matrix, schema_version=True),
        ),
        (
            "dataclass subclass smuggled into a composition profile",
            lambda: replace(profile, components=subclass_components),
        ),
        (
            "status change without component version advance",
            lambda: CompositionMatrixV1(
                schema_version=1,
                matrix_id=matrix.matrix_id,
                matrix_version=2,
                previous_matrix_sha256=matrix.sha256,
                profiles=(
                    profile,
                    replace(
                        profile,
                        profile_version=2,
                        implementation_status="EXECUTABLE",
                        components=tuple(
                            replace(component, implementation_status="EXECUTABLE")
                            if component.component_id in promoted_component_ids
                            else component
                            for component in profile.components
                        ),
                    ),
                ),
            ),
        ),
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    owners = {
        resource: component.component_id
        for component in profile.components
        for resource in component.owned_resources
    }
    return FullDayAuditCase(
        "composition_matrix_singletons_dependencies_and_refusals",
        (
            f"profile={profile.profile_id} refused={len(profile.refused_component_ids)} "
            f"singleton_owners={len(owners)} matrix_sha256={matrix.sha256} "
            f"promotion_sha256={successor.sha256}"
        ),
        tuple(failures),
    )


def _checkpoint_case() -> FullDayAuditCase:
    from kirby2.full_day.checkpoint_contract import (
        CheckpointCaptureScopeV1,
        CheckpointInventoryV1,
        QuiescentCutV1,
        checkpoint_inventory_v1,
        validate_checkpoint_capture,
        validate_checkpoint_component_state_keys,
        validate_checkpoint_owned_state_semantics,
    )

    inventory = checkpoint_inventory_v1()
    restored = CheckpointInventoryV1.from_json_bytes(inventory.canonical_bytes())
    failures: list[str] = []
    if restored.as_dict() != inventory.as_dict() or restored.sha256 != inventory.sha256:
        failures.append("checkpoint inventory canonical round trip changed identity")
    by_state_id = {item.component_id: item for item in inventory.items}
    required_owned_fields = {
        "AGENT_SCHEDULER_METAORDERS_V1": {
            "agent.activation_state",
            "agent.inventories",
            "agent.order_bindings",
            "agent.pending_decisions",
            "agent.policy_states",
            "agent.rng_states",
            "agent.scheduler_allocator_state",
            "metaorder.states",
        },
        "PENDING_LATENCY_CLIENT_DELIVERY_V1": {
            "delivery.client_delivery_work_ids",
            "delivery.client_known_working_orders",
            "delivery.fill_report_messages",
            "delivery.latency.rng_state",
            "delivery.message_allocator_state",
            "delivery.pending_acknowledgements",
            "delivery.protocol_order_states",
            "delivery.venue_receipt_work_ids",
            "delivery.venue_timestamps",
        },
        "PLAYER_OVERLAY_WORKING_ORDERS_V1": {
            "client.decision_information_cutoffs",
            "client.fill_report_consumption_cursor",
            "client.player_action_bindings",
            "client.player_fill_history",
            "client.player_pending_decisions",
            "client.player_position",
            "client.player_working_order_ids",
        },
        "ALGORITHMS_DEADLINES_CHILD_ORDERS_V1": {
            "algorithm.benchmark_tracker_state",
            "algorithm.child_fill_records",
            "algorithm.child_order_bindings",
            "algorithm.child_order_records",
            "algorithm.current_time_us",
            "algorithm.objective_parameters",
            "algorithm.pending_deadline",
            "algorithm.policy_id_version",
            "algorithm.schedule_progress",
        },
        "MULTIVENUE_V1": {
            "multivenue.consolidated_feed_state",
            "multivenue.coordinator_event_ledger",
            "multivenue.coordinator_event_sequence",
            "multivenue.current_time_us",
            "multivenue.global_player_position",
            "multivenue.pending_route_legs",
            "multivenue.route_sequence_allocator_state",
            "multivenue.seed",
            "multivenue.venue_configs",
            "multivenue.venue_latency_rng_states",
            "multivenue.venue_routing_states",
            "multivenue.venue_session_states",
            "multivenue.venue_working_order_records",
        },
        "HIDDEN_LIQUIDITY_V1": {
            "hidden.venue_arrival_sequence_allocators",
            "hidden.venue_clock_states",
            "hidden.venue_completion_states",
            "hidden.venue_observable_event_prefixes",
            "hidden.venue_order_records",
            "hidden.venue_pending_observable_ordinals",
            "hidden.venue_pending_observable_queues",
            "hidden.venue_player_ledgers",
            "hidden.venue_priority_sequence_allocators",
            "hidden.venue_public_tapes",
            "hidden.venue_published_feed_states",
            "hidden.venue_rules",
            "hidden.venue_seen_order_ids",
            "hidden.venue_strategy_event_prefixes",
            "hidden.venue_trade_sequence_allocators",
            "hidden.venue_truth_event_prefixes",
        },
        "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1": {
            "state.component_local_sequence",
            "state.current_day",
            "state.current_local",
            "state.day_elapsed_age_us",
            "state.day_next_eligible_transition_id",
            "state.day_sampled_duration_us",
            "state.local_elapsed_age_us",
            "state.local_next_eligible_transition_id",
            "state.local_sampled_duration_us",
        },
        "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1": {
            "halt_reopen.controller_local_sequence",
            "halt_reopen.halt_count",
            "halt_reopen.halt_entered_time_us",
            "halt_reopen.maximum_resume_deadline_us",
            "halt_reopen.minimum_resume_eligible_time_us",
            "halt_reopen.pending_transition_work_id",
            "halt_reopen.reopening_auction_state",
            "shock.proposal_sequence",
        },
        "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1": {
            "mechanics.arrival_allocator_state",
            "mechanics.command_allocator_state",
            "mechanics.event_allocator_state",
            "mechanics.event_prefix",
            "mechanics.session_state",
        },
        "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1": {
            "runtime.derived_seed_registry",
            "runtime.rng_algorithm_codec_version",
            "runtime.rng_states",
            "runtime.root_seed",
        },
    }
    for state_id, required_fields in required_owned_fields.items():
        missing_fields = required_fields - set(by_state_id[state_id].owned_state_fields)
        if missing_fields:
            failures.append(
                f"checkpoint state {state_id} omits owned fields: "
                + ",".join(sorted(missing_fields))
            )
    all_owned_fields = [
        field
        for item in inventory.items
        for field in item.owned_state_fields
    ]
    if len(all_owned_fields) != len(set(all_owned_fields)):
        failures.append("checkpoint inventory contains duplicate exact state ownership")
    algorithm_item = by_state_id["ALGORITHMS_DEADLINES_CHILD_ORDERS_V1"]
    algorithm_state = {field: None for field in algorithm_item.owned_state_fields}
    try:
        validate_checkpoint_component_state_keys(
            inventory,
            component_id=algorithm_item.component_id,
            state=algorithm_state,
        )
    except (TypeError, ValueError) as error:
        failures.append(f"exact preserved component state keys were refused: {error}")

    cut = QuiescentCutV1(
        schema_version=1,
        simulation_time_us=0,
        microstep=1,
        checkpoint_stage_ordinal=11,
        last_global_event_sequence=3,
        event_prefix_last_global_sequence=3,
        event_prefix_sha256="0" * 64,
        pending_work_count=1,
        next_pending_time_us=1,
        next_pending_microstep=0,
        due_work_at_or_before_cut=0,
        generated_microsteps_complete=True,
        checkpoint_stage_complete=True,
        boundary_complete_at_cut=True,
    )
    if QuiescentCutV1.from_json_bytes(cut.canonical_bytes()).as_dict() != cut.as_dict():
        failures.append("quiescent t=0 cut did not round trip exactly")

    all_ids = {item.component_id for item in inventory.items}
    always_ids = {
        item.component_id for item in inventory.items if item.presence == "ALWAYS"
    }
    absent_ids = all_ids - always_ids
    try:
        validate_checkpoint_capture(
            inventory,
            cut=cut,
            active_component_ids=sorted(always_ids),
            preserved_component_ids=sorted(always_ids),
            absent_component_ids=sorted(absent_ids),
        )
    except (TypeError, ValueError) as error:
        failures.append(f"complete checkpoint inventory was refused: {error}")

    component_only_sets = (
        {
            "MULTIVENUE_V1",
            "HIDDEN_LIQUIDITY_V1",
        },
        {
            "ALGORITHMS_DEADLINES_CHILD_ORDERS_V1",
        },
    )
    for label, component_ids in zip(
        ("multivenue-hidden", "execution-algorithm"),
        component_only_sets,
        strict=True,
    ):
        try:
            validate_checkpoint_capture(
                inventory,
                cut=cut,
                active_component_ids=sorted(component_ids),
                preserved_component_ids=sorted(component_ids),
                absent_component_ids=sorted(all_ids - component_ids),
                capture_scope=CheckpointCaptureScopeV1.RESTORABLE_COMPONENT_ONLY,
            )
        except (TypeError, ValueError) as error:
            failures.append(f"{label} component-only inventory was refused: {error}")
        mechanics_ids = {
            item.component_id
            for item in inventory.items
            if item.state_owner_id == "ENGINE_MARKET_MECHANICS_V1"
        }
        if mechanics_ids & component_ids:
            failures.append(f"{label} component-only inventory retained single-venue mechanics")

    conditional = next(
        item for item in inventory.items if item.presence == "CONDITIONAL"
    )
    duplicate_field_item = replace(
        inventory.items[1],
        owned_state_fields=tuple(
            sorted(
                {
                    *inventory.items[1].owned_state_fields,
                    inventory.items[0].owned_state_fields[0],
                }
            )
        ),
    )
    duplicate_field_items = tuple(
        duplicate_field_item if index == 1 else item
        for index, item in enumerate(inventory.items)
    )
    queue_index = next(
        index
        for index, item in enumerate(inventory.items)
        if item.component_id == "SCHEDULED_WORK_QUEUE_V1"
    )
    ambiguous_queue_item = replace(
        inventory.items[queue_index],
        owned_state_fields=tuple(
            sorted(
                {
                    *inventory.items[queue_index].owned_state_fields,
                    "next_component_local_sequences",
                }
            )
        ),
    )
    ambiguous_queue_items = tuple(
        ambiguous_queue_item if index == queue_index else item
        for index, item in enumerate(inventory.items)
    )
    allocator_index = next(
        index
        for index, item in enumerate(inventory.items)
        if item.component_id == "COMPONENT_LOCAL_ALLOCATORS_V1"
    )
    legacy_runtime_allocator_item = replace(
        inventory.items[allocator_index],
        owned_state_fields=tuple(
            sorted(
                {
                    *inventory.items[allocator_index].owned_state_fields,
                    "runtime.component_local_event_sequence_allocators",
                }
            )
        ),
    )
    legacy_runtime_allocator_items = tuple(
        legacy_runtime_allocator_item if index == allocator_index else item
        for index, item in enumerate(inventory.items)
    )
    state_runtime_index = next(
        index
        for index, item in enumerate(inventory.items)
        if item.component_id
        == "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1"
    )
    legacy_transition_counter_item = replace(
        inventory.items[state_runtime_index],
        owned_state_fields=tuple(
            sorted(
                {
                    *inventory.items[state_runtime_index].owned_state_fields,
                    "state.completed_transition_count",
                }
            )
        ),
    )
    legacy_transition_counter_items = tuple(
        legacy_transition_counter_item if index == state_runtime_index else item
        for index, item in enumerate(inventory.items)
    )

    class QuiescentCutSubclass(QuiescentCutV1):
        pass

    forged_cut = QuiescentCutSubclass.from_dict(cut.as_dict())
    probes: list[tuple[str, Callable[[], object]]] = [
        (
            "active checkpoint component omission",
            lambda: validate_checkpoint_capture(
                inventory,
                cut=cut,
                active_component_ids=sorted((*always_ids, conditional.component_id)),
                preserved_component_ids=sorted(always_ids),
                absent_component_ids=sorted(absent_ids),
            ),
        ),
        (
            "preserved inactive checkpoint component",
            lambda: validate_checkpoint_capture(
                inventory,
                cut=cut,
                active_component_ids=sorted(always_ids),
                preserved_component_ids=sorted((*always_ids, conditional.component_id)),
                absent_component_ids=sorted(absent_ids - {conditional.component_id}),
            ),
        ),
        (
            "boolean checkpoint-item schema version",
            lambda: replace(inventory.items[0], schema_version=True),
        ),
        (
            "boolean checkpoint-inventory schema version",
            lambda: replace(inventory, schema_version=True),
        ),
        (
            "boolean quiescent-cut schema version",
            lambda: replace(cut, schema_version=True),
        ),
        (
            "dataclass subclass smuggled into checkpoint validation",
            lambda: validate_checkpoint_capture(
                inventory,
                cut=forged_cut,
                active_component_ids=sorted(always_ids),
                preserved_component_ids=sorted(always_ids),
                absent_component_ids=sorted(absent_ids),
            ),
        ),
        (
            "duplicate authoritative field ownership",
            lambda: validate_checkpoint_owned_state_semantics(
                duplicate_field_items
            ),
        ),
        (
            "renamed component-local allocator under scheduled-work queue",
            lambda: validate_checkpoint_owned_state_semantics(
                ambiguous_queue_items
            ),
        ),
        (
            "legacy generic component-local allocator ownership",
            lambda: validate_checkpoint_owned_state_semantics(
                legacy_runtime_allocator_items
            ),
        ),
        (
            "legacy completed-transition counter in state runtime",
            lambda: validate_checkpoint_owned_state_semantics(
                legacy_transition_counter_items
            ),
        ),
        (
            "preserved component state omitting one frozen field",
            lambda: validate_checkpoint_component_state_keys(
                inventory,
                component_id=algorithm_item.component_id,
                state={
                    key: value
                    for key, value in algorithm_state.items()
                    if key != algorithm_item.owned_state_fields[0]
                },
            ),
        ),
        (
            "preserved component state adding an undeclared field",
            lambda: validate_checkpoint_component_state_keys(
                inventory,
                component_id=algorithm_item.component_id,
                state={**algorithm_state, "undeclared.state": None},
            ),
        ),
        (
            "component-only capture missing a selected dependency",
            lambda: validate_checkpoint_capture(
                inventory,
                cut=cut,
                active_component_ids=["HIDDEN_LIQUIDITY_V1"],
                preserved_component_ids=["HIDDEN_LIQUIDITY_V1"],
                absent_component_ids=sorted(
                    all_ids - {"HIDDEN_LIQUIDITY_V1"}
                ),
                capture_scope=CheckpointCaptureScopeV1.RESTORABLE_COMPONENT_ONLY,
            ),
        ),
    ]
    for field, value in (
        ("due_work_at_or_before_cut", 1),
        ("generated_microsteps_complete", False),
        ("checkpoint_stage_complete", False),
        ("boundary_complete_at_cut", False),
        ("event_prefix_last_global_sequence", 2),
        ("next_pending_time_us", 0),
    ):
        payload = copy.deepcopy(cut.as_dict())
        payload[field] = value
        probes.append(
            (
                f"nonquiescent cut {field}",
                lambda payload=payload: QuiescentCutV1.from_dict(payload),
            )
        )
    same_time_future = copy.deepcopy(cut.as_dict())
    same_time_future["next_pending_time_us"] = 0
    same_time_future["next_pending_microstep"] = cut.microstep + 1
    probes.append(
        (
            "same-time future microstep after checkpoint cut",
            lambda: QuiescentCutV1.from_dict(same_time_future),
        )
    )
    nonzero_incomplete_boundary = copy.deepcopy(cut.as_dict())
    nonzero_incomplete_boundary.update(
        {
            "boundary_complete_at_cut": False,
            "next_pending_microstep": None,
            "next_pending_time_us": None,
            "pending_work_count": 0,
            "simulation_time_us": 1,
        }
    )
    probes.append(
        (
            "nonzero checkpoint with incomplete due boundary work",
            lambda: QuiescentCutV1.from_dict(nonzero_incomplete_boundary),
        )
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "checkpoint_inventory_active_omission_and_quiescent_cut",
        (
            f"always={len(always_ids)} conditional={len(absent_ids)} "
            f"owned_fields={sum(len(item.owned_state_fields) for item in inventory.items)} "
            "component_only_profiles=2 "
            f"inventory_sha256={inventory.sha256}"
        ),
        tuple(failures),
    )


def _pilot_limits_case() -> FullDayAuditCase:
    from kirby2.full_day.checkpoint_contract import PilotLimitsV1, load_pilot_limits
    from kirby2.full_day.models import canonical_sha256

    failures: list[str] = []
    limits = load_pilot_limits()
    restored = PilotLimitsV1.from_json_bytes(limits.canonical_bytes())
    if restored.as_dict() != limits.as_dict():
        failures.append("packaged pilot limits canonical round trip changed bytes")
    if limits.manifest_version != 1 or limits.semantic_version != 1:
        failures.append("packaged WO31-F pilot must remain manifest/semantic version 1")
    resource_bytes = files("kirby2.full_day").joinpath("pilot_limits.toml").read_bytes()
    resource_sha256 = hashlib.sha256(resource_bytes).hexdigest()
    forged = copy.deepcopy(limits.as_dict())
    forged["manifest_sha256"] = "0" * 64
    forged_semantic = copy.deepcopy(limits.as_dict())
    forged_semantic["semantic_sha256"] = "0" * 64
    floating = copy.deepcopy(limits.as_dict())
    floating["max_outer_events"] = 1.5
    boolean_version_identity = limits.manifest_identity_dict()
    boolean_version_identity["manifest_version"] = True
    for label, operation in (
        ("forged pilot digest", lambda: PilotLimitsV1.from_dict(forged)),
        (
            "forged pilot semantic digest",
            lambda: PilotLimitsV1.from_dict(forged_semantic),
        ),
        ("floating pilot limit", lambda: PilotLimitsV1.from_dict(floating)),
        (
            "boolean pilot schema version",
            lambda: replace(limits, schema_version=True),
        ),
        (
            "boolean pilot manifest version",
            lambda: replace(
                limits,
                manifest_version=True,
                manifest_sha256=canonical_sha256(boolean_version_identity),
            ),
        ),
        (
            "boolean pilot semantic version",
            lambda: replace(limits, semantic_version=True),
        ),
    ):
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)

    def recompute(payload: dict[str, object]) -> PilotLimitsV1:
        semantic_keys = set(limits.semantic_identity_dict())
        payload["semantic_sha256"] = canonical_sha256(
            {key: payload[key] for key in semantic_keys}
        )
        payload["manifest_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "manifest_sha256"}
        )
        return PilotLimitsV1.from_dict(payload)

    operational_payload = copy.deepcopy(limits.as_dict())
    operational_payload["manifest_version"] = limits.manifest_version + 1
    operational_payload["max_generation_wall_time_ns"] = (
        limits.max_generation_wall_time_ns + 1
    )
    operational = recompute(operational_payload)
    if (
        operational.semantic_identity_dict() != limits.semantic_identity_dict()
        or operational.semantic_sha256 != limits.semantic_sha256
    ):
        failures.append("operational wall-time revision perturbed semantic identity")
    if (
        operational.manifest_identity_dict() == limits.manifest_identity_dict()
        or operational.manifest_sha256 == limits.manifest_sha256
    ):
        failures.append("operational wall-time revision did not perturb manifest identity")

    deterministic_payload = copy.deepcopy(limits.as_dict())
    deterministic_payload["manifest_version"] = limits.manifest_version + 1
    deterministic_payload["semantic_version"] = limits.semantic_version + 1
    deterministic_payload["max_outer_events"] = limits.max_outer_events + 1
    deterministic = recompute(deterministic_payload)
    if (
        deterministic.semantic_identity_dict() == limits.semantic_identity_dict()
        or deterministic.semantic_sha256 == limits.semantic_sha256
        or deterministic.manifest_sha256 == limits.manifest_sha256
    ):
        failures.append("deterministic-limit revision failed to change semantic identity")
    reversed_versions = copy.deepcopy(limits.as_dict())
    reversed_versions["semantic_version"] = limits.semantic_version + 1
    failure = _expect_refusal(
        lambda: recompute(reversed_versions),
        "pilot semantic version newer than its manifest version",
    )
    if failure:
        failures.append(failure)
    return FullDayAuditCase(
        "packaged_preregistered_pilot_limit_schema_and_digest",
        (
            f"manifest={limits.manifest_id} version={limits.manifest_version} "
            f"semantic_version={limits.semantic_version} "
            f"semantic_sha256={limits.semantic_sha256} "
            f"manifest_sha256={limits.manifest_sha256} "
            f"resource_sha256={resource_sha256}"
        ),
        tuple(failures),
    )


def _mechanics_boundary_case() -> FullDayAuditCase:
    calendar_failures, calendar_trace = _calendar_boundary_trace_probe()
    engine = _continuous_engine()
    engine.submit(_limit("BOUNDARY-DAY", Side.BUY, 10, 97))
    engine.submit(
        _limit(
            "BOUNDARY-GTT",
            Side.BUY,
            10,
            96,
            time_in_force=OrderInstruction.GOOD_UNTIL_TIME,
            good_until_time_us=200,
        )
    )
    _apply_native_boundary(
        engine,
        100,
        SessionState.CLOSING_AUCTION,
        uncross_before=False,
        reason="WO31_A_CLOSING_CALL",
    )
    engine.submit(
        _limit(
            "BOUNDARY-AUCTION-BUY",
            Side.BUY,
            100,
            101,
            auction_only=True,
        )
    )
    engine.submit(
        _limit(
            "BOUNDARY-AUCTION-SELL",
            Side.SELL,
            40,
            99,
            auction_only=True,
        )
    )
    start = len(engine.events)
    _apply_native_boundary(
        engine,
        200,
        SessionState.POSTCLOSE,
        uncross_before=True,
        reason="WO31_A_POSTCLOSE",
    )
    suffix = engine.events[start:]
    observed = tuple(event.event_type for event in suffix)
    expected = (
        MechanicsEventType.AUCTION_FILL,
        MechanicsEventType.ORDER_EXPIRED,
        MechanicsEventType.AUCTION_UNCROSS,
        MechanicsEventType.ORDER_EXPIRED,
        MechanicsEventType.SESSION_STATE_CHANGED,
        MechanicsEventType.ORDER_EXPIRED,
    )
    reasons = tuple(
        event.data.get("reason")
        for event in suffix
        if event.event_type is MechanicsEventType.ORDER_EXPIRED
    )
    failures: list[str] = list(calendar_failures)
    if observed != expected:
        failures.append("closing boundary mechanics event types are misordered")
    if reasons != (
        "AUCTION_REMAINDER",
        "DAY_END_POSTCLOSE",
        "GOOD_UNTIL_TIME",
    ):
        failures.append("boundary expiration classes are misordered")
    sequences = tuple(event.sequence for event in suffix)
    if sequences != tuple(range(sequences[0], sequences[0] + len(sequences))):
        failures.append("native mechanics sequences were not preserved")
    if any(event.simulation_time_us != 200 for event in suffix):
        failures.append("atomic boundary consequences do not share boundary time")
    try:
        engine.assert_invariants()
    except RuntimeError as error:
        failures.append(f"boundary invariant failure: {error}")

    halt_failures, resume_trace = _halt_resume_boundary_probe()
    failures.extend(halt_failures)
    return FullDayAuditCase(
        "atomic_market_mechanics_boundary_trace",
        (
            "uncross, transition-owned expirations, session/HALT/RESUME, and "
            "same-time GTT expiry retain native order; "
            f"calendar_trace={calendar_trace}; resume_trace={resume_trace}"
        ),
        tuple(failures),
    )


def _sample_plan():
    from kirby2.full_day.checkpoint_contract import load_pilot_limits
    from kirby2.full_day.composition import initial_composition_matrix
    from kirby2.full_day.models import (
        FULL_DAY_SUBSTREAM_POLICY_VERSION,
        CheckpointPolicyV1,
        ComponentConfigurationBindingV1,
        DeterministicLimitsV1,
        FlowSideV1,
        FullDayPlanV1,
        HaltReopenRulesV1,
        IntegerParameterUnitV1,
        MacroRegimeSegmentV1,
        MechanicsRulesV1,
        NamedIntegerParameterV1,
        ParticipantDefinitionV1,
        ParticipantKindV1,
        ParticipantScheduleActionV1,
        ParticipantScheduleEntryV1,
        PressureKindV1,
        PressureProfileV1,
        PressureSegmentV1,
        ResolvedInstrumentProfileV1,
        ScheduledEventTypeV1,
        ScheduledEventV1,
        SeedPolicyV1,
        SubstreamDeclarationV1,
        UnscheduledShockPolicyV1,
        VersionedReferenceV1,
        derive_substream_seed,
    )
    from kirby2.full_day.states import (
        DAY_STATE_RNG_SUBSTREAM_PATH_V1,
        LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
        DayStateV1,
    )

    digest = lambda character: character * 64
    maker_spec = VersionedReferenceV1("AUDIT_MAKER_SPEC_V1", 1, digest("2"))
    metaorder_spec = VersionedReferenceV1(
        "AUDIT_METAORDER_SPEC_V1", 1, digest("7")
    )
    quantity_distribution = VersionedReferenceV1(
        "AUDIT_QUANTITY_DISTRIBUTION_V1", 1, digest("3")
    )
    halt_trigger = VersionedReferenceV1("AUDIT_HALT_TRIGGER_V1", 1, digest("4"))
    resume_trigger = VersionedReferenceV1(
        "AUDIT_RESUME_TRIGGER_V1", 1, digest("5")
    )
    component_configurations = tuple(
        sorted(
            (
                ComponentConfigurationBindingV1("AGENT_SCHEDULER_V1", maker_spec),
                ComponentConfigurationBindingV1(
                    "AGENT_SCHEDULER_V1", metaorder_spec
                ),
                ComponentConfigurationBindingV1(
                    "ENGINE_MARKET_MECHANICS_V1", halt_trigger
                ),
                ComponentConfigurationBindingV1(
                    "ENGINE_MARKET_MECHANICS_V1", resume_trigger
                ),
                ComponentConfigurationBindingV1(
                    "FULL_DAY_RUNTIME_V1", quantity_distribution
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    calendar = _sample_calendar()
    end_us = calendar.end_time_us
    pressure_profiles = tuple(
        PressureProfileV1(
            profile_id=f"AUDIT_{kind.value}_PRESSURE_V1",
            profile_version=1,
            pressure_kind=kind,
            minimum_ppm=500_000,
            maximum_ppm=1_500_000,
            segments=(PressureSegmentV1(0, end_us, 1_000_000),),
        )
        for kind in PressureKindV1
    )
    participant_path = "full_day/participant/audit_maker/decision"
    metaorder_path = "full_day/participant/audit_metaorder/decision"
    shock_path = "full_day/runtime/shock/audit/candidate"
    root_seed = 42
    substreams = tuple(
        SubstreamDeclarationV1(
            path,
            derive_substream_seed(
                root_seed, FULL_DAY_SUBSTREAM_POLICY_VERSION, path
            ),
        )
        for path in sorted(
            (
                DAY_STATE_RNG_SUBSTREAM_PATH_V1,
                LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
                participant_path,
                metaorder_path,
                shock_path,
            )
        )
    )
    matrix = initial_composition_matrix()
    pilot = load_pilot_limits()
    mechanics = MechanicsRulesV1(
        schema_version=1,
        tick_size_numerator=1,
        tick_size_denominator=100,
        lot_size=1,
        minimum_quantity=1,
        maximum_quantity=1_000_000,
        lower_price_band_ticks=1,
        upper_price_band_ticks=1_000_000,
        supported_order_instructions=tuple(sorted({"DAY", "LIMIT", "MARKET"})),
        session_schedule=(),
        preserve_priority_on_quantity_reduction=True,
        reference_price_ticks=10_000,
        price_collar_ticks=None,
        volatility_interruption_ticks=None,
        fat_finger_ticks=None,
        account_stp_modes=(),
    )
    return FullDayPlanV1(
        schema_version=1,
        plan_id="AUDIT_FULL_DAY_PLAN_V1",
        plan_version=1,
        market_profile=VersionedReferenceV1(
            "AUDIT_MARKET_PROFILE_V1", 1, digest("1")
        ),
        instrument_profile=ResolvedInstrumentProfileV1(
            VersionedReferenceV1("AUDIT_INSTRUMENT_PROFILE_V1", 1, digest("6")),
            mechanics,
        ),
        calendar=calendar,
        pressure_profiles=pressure_profiles,
        state_model=_sample_state_model(),
        macro_regime_schedule=(MacroRegimeSegmentV1(0, end_us, DayStateV1.QUIET),),
        participant_definitions=(
            ParticipantDefinitionV1(
                "AUDIT_MAKER",
                ParticipantKindV1.MARKET_MAKER,
                maker_spec,
                participant_path,
                False,
            ),
            ParticipantDefinitionV1(
                "AUDIT_METAORDER",
                ParticipantKindV1.METAORDER,
                metaorder_spec,
                metaorder_path,
                False,
            ),
        ),
        participant_schedule=(
            ParticipantScheduleEntryV1(
                "AUDIT_MAKER_ACTIVATE",
                100,
                "AUDIT_MAKER",
                ParticipantScheduleActionV1.ACTIVATE,
                None,
            ),
        ),
        scheduled_events=(
            ScheduledEventV1(
                "AUDIT_ANNOUNCEMENT",
                200,
                ScheduledEventTypeV1.ECONOMIC_ANNOUNCEMENT,
                1,
                FlowSideV1.NONE,
                (
                    NamedIntegerParameterV1(
                        "impact_ppm", IntegerParameterUnitV1.PPM, 1_000
                    ),
                ),
                None,
                None,
            ),
        ),
        unscheduled_shock_policy=UnscheduledShockPolicyV1(
            "AUDIT_SHOCK_POLICY_V1",
            1,
            True,
            300,
            400,
            1,
            1,
            0,
            1,
            2,
            shock_path,
            (FlowSideV1.BUY, FlowSideV1.SELL),
            quantity_distribution,
            (),
        ),
        halt_reopen_rules=HaltReopenRulesV1(
            "AUDIT_HALT_REOPEN_V1",
            1,
            halt_trigger,
            resume_trigger,
            10,
            100,
            10,
            1,
            True,
            False,
            True,
        ),
        seed_policy=SeedPolicyV1(
            1, FULL_DAY_SUBSTREAM_POLICY_VERSION, root_seed, substreams
        ),
        checkpoint_policy=CheckpointPolicyV1(
            1,
            None,
            (0,),
            True,
            True,
            True,
            True,
            10,
        ),
        deterministic_limits=DeterministicLimitsV1(
            1,
            end_us,
            250_000,
            25_000,
            128,
            10_000,
            67_108_864,
            10_000,
            1_000,
        ),
        pilot_limits_reference=VersionedReferenceV1(
            pilot.manifest_id, pilot.semantic_version, pilot.semantic_sha256
        ),
        composition_profile=VersionedReferenceV1(
            "SINGLE_VENUE_AGENT_MECHANICS_V1", 1, matrix.sha256
        ),
        component_configurations=component_configurations,
    )


def _sample_state_model():
    from kirby2.full_day.states import (
        DAY_STATE_RNG_SUBSTREAM_PATH_V1,
        LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
        DayStateDefinitionV1,
        DayStateV1,
        DurationExhaustionBehaviorV1,
        DurationLawV1,
        DurationMassV1,
        LocalStateDefinitionV1,
        LocalStateV1,
        StateModelV1,
        StateTransitionV1,
        TriggerInformationClassV1,
    )

    law = DurationLawV1(10, 10, (DurationMassV1(10, 1),))

    def definitions(states, definition_type, prefix):
        rows = []
        for index, state in enumerate(states):
            successor = states[(index + 1) % len(states)]
            transition = StateTransitionV1(
                f"{prefix}_{state.value}_NEXT",
                state.value,
                successor.value,
                10,
                DurationExhaustionBehaviorV1.WAIT_FOR_TRIGGER,
                1,
                "AGE_ELIGIBLE_V1",
                1,
                (),
                TriggerInformationClassV1.OBSERVABLE_AT_TIME,
                (),
            )
            rows.append(definition_type(state, law, (), (transition,)))
        return tuple(rows)

    day_states = tuple(DayStateV1)
    local_states = tuple(LocalStateV1)
    return StateModelV1(
        1,
        DayStateV1.QUIET,
        LocalStateV1.BALANCED,
        DAY_STATE_RNG_SUBSTREAM_PATH_V1,
        LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
        definitions(day_states, DayStateDefinitionV1, "DAY"),
        definitions(local_states, LocalStateDefinitionV1, "LOCAL"),
    )


def _sample_calendar():
    from kirby2.full_day.calendar import (
        BoundaryOperationV1,
        CalendarPhaseV1,
        LocalBoundaryV1,
        PHASE_IDS,
        TradingDayCalendarV1,
    )

    local_times = (
        "09:00:00.000000",
        "09:10:00.000000",
        "09:20:00.000000",
        "10:20:00.000000",
        "10:30:00.000000",
        "10:40:00.000000",
    )
    simulation_times = (
        0,
        600_000_000,
        1_200_000_000,
        4_800_000_000,
        5_400_000_000,
        6_000_000_000,
    )
    boundaries = tuple(
        LocalBoundaryV1(
            1,
            "2024-01-02",
            local_time,
            "UTC",
            0,
            0,
            simulation_time_us,
        )
        for local_time, simulation_time_us in zip(
            local_times, simulation_times, strict=True
        )
    )
    phases = tuple(
        CalendarPhaseV1(1, phase_id, boundaries[index], boundaries[index + 1])
        for index, phase_id in enumerate(PHASE_IDS)
    )
    destinations = (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.CONTINUOUS,
        SessionState.CLOSING_AUCTION,
        SessionState.POSTCLOSE,
        SessionState.CLOSED,
    )
    uncrosses = (False, False, True, False, True, False)
    operations = tuple(
        BoundaryOperationV1(1, boundary, destination, uncross)
        for boundary, destination, uncross in zip(
            boundaries, destinations, uncrosses, strict=True
        )
    )
    return TradingDayCalendarV1(
        1,
        "AUDIT_SYNTHETIC_CALENDAR_V1",
        "2024-01-02",
        "UTC",
        phases,
        operations,
    )


def _calendar_boundary_trace_probe() -> tuple[list[str], tuple[tuple[str, ...], ...]]:
    """Drive all six canonical records through the empty-schedule mechanics engine."""

    calendar = _sample_calendar()
    engine = MarketMechanicsEngine(
        InstrumentRules(session_schedule=SessionSchedule(()))
    )
    expected = (
        (MechanicsEventType.SESSION_STATE_CHANGED,),
        (MechanicsEventType.SESSION_STATE_CHANGED,),
        (
            MechanicsEventType.AUCTION_FILL,
            MechanicsEventType.AUCTION_UNCROSS,
            MechanicsEventType.SESSION_STATE_CHANGED,
        ),
        (MechanicsEventType.SESSION_STATE_CHANGED,),
        (
            MechanicsEventType.AUCTION_FILL,
            MechanicsEventType.AUCTION_UNCROSS,
            MechanicsEventType.SESSION_STATE_CHANGED,
        ),
        (MechanicsEventType.SESSION_STATE_CHANGED,),
    )
    failures: list[str] = []
    traces: list[tuple[str, ...]] = []
    for index, (operation, expected_types) in enumerate(
        zip(calendar.boundary_operations, expected, strict=True)
    ):
        if index in (2, 4):
            label = "OPEN" if index == 2 else "CLOSE"
            engine.submit(
                _limit(f"{label}-TRACE-BUY", Side.BUY, 10, 101, auction_only=True)
            )
            engine.submit(
                _limit(f"{label}-TRACE-SELL", Side.SELL, 10, 99, auction_only=True)
            )
        start = len(engine.events)
        _apply_boundary_operation(engine, operation, reason=f"WO31_A_BOUNDARY_{index}")
        suffix = engine.events[start:]
        observed = tuple(event.event_type for event in suffix)
        traces.append(tuple(item.value for item in observed))
        if observed != expected_types:
            failures.append(f"calendar boundary operation {index} emitted the wrong trace")
        if engine.session_state is not operation.destination_session_state:
            failures.append(f"calendar boundary operation {index} reached the wrong state")
        if engine.clock.current_time_us != operation.boundary.simulation_time_us:
            failures.append(f"calendar boundary operation {index} reached the wrong time")
        if any(
            event.simulation_time_us != operation.boundary.simulation_time_us
            for event in suffix
        ):
            failures.append(f"calendar boundary operation {index} was not atomic in time")
    if engine.session_state is not SessionState.CLOSED:
        failures.append("terminal calendar operation did not leave mechanics CLOSED")
    try:
        engine.assert_invariants()
    except RuntimeError as error:
        failures.append(f"six-boundary trace invariant failure: {error}")
    return failures, tuple(traces)


def _halt_resume_boundary_probe() -> tuple[list[str], tuple[str, ...]]:
    failures: list[str] = []
    engine = _continuous_engine()
    engine.submit(
        _limit(
            "HALT-SESSION",
            Side.BUY,
            10,
            97,
            time_in_force=OrderInstruction.SESSION,
        )
    )
    engine.submit(
        _limit(
            "RESUME-GTT",
            Side.BUY,
            10,
            96,
            time_in_force=OrderInstruction.GOOD_UNTIL_TIME,
            good_until_time_us=20,
        )
    )
    start = len(engine.events)
    _apply_native_boundary(
        engine,
        10,
        SessionState.HALTED,
        uncross_before=False,
        reason="WO31_A_HALT",
    )
    halt = engine.events[start:]
    halt_types = tuple(event.event_type for event in halt)
    if halt_types != (
        MechanicsEventType.ORDER_EXPIRED,
        MechanicsEventType.SESSION_STATE_CHANGED,
        MechanicsEventType.HALT,
    ):
        failures.append("halt boundary did not order expiration/session/HALT")
    _apply_native_boundary(
        engine,
        15,
        SessionState.REOPENING_AUCTION,
        uncross_before=False,
        reason="WO31_A_REOPENING_CALL",
    )
    engine.submit(
        _limit("REOPEN-BUY", Side.BUY, 10, 101, auction_only=True)
    )
    engine.submit(
        _limit("REOPEN-SELL", Side.SELL, 10, 99, auction_only=True)
    )
    start = len(engine.events)
    _apply_native_boundary(
        engine,
        20,
        SessionState.CONTINUOUS,
        uncross_before=True,
        reason="WO31_A_RESUME",
    )
    resume = engine.events[start:]
    resume_types = tuple(event.event_type for event in resume)
    expected = (
        MechanicsEventType.AUCTION_FILL,
        MechanicsEventType.AUCTION_UNCROSS,
        MechanicsEventType.SESSION_STATE_CHANGED,
        MechanicsEventType.RESUME,
        MechanicsEventType.ORDER_EXPIRED,
    )
    if resume_types != expected:
        failures.append("resume boundary did not order uncross/session/RESUME/GTT")
    return failures, tuple(item.value for item in resume_types)


def _apply_native_boundary(
    engine: MarketMechanicsEngine,
    simulation_time_us: int,
    destination: SessionState,
    *,
    uncross_before: bool,
    reason: str,
) -> None:
    if engine.rules.session_schedule.transitions:
        raise RuntimeError("full-day mechanics engine must have an empty session schedule")
    engine.clock.advance_to(simulation_time_us)
    if uncross_before:
        engine.uncross_auction()
    engine.transition_session(destination, reason=reason)
    engine.advance_to(simulation_time_us)


def _apply_boundary_operation(engine, operation, *, reason: str) -> None:
    from kirby2.full_day.calendar import BoundaryOperationV1

    if type(operation) is not BoundaryOperationV1:
        raise TypeError("boundary operation must use BoundaryOperationV1")
    _apply_native_boundary(
        engine,
        operation.boundary.simulation_time_us,
        operation.destination_session_state,
        uncross_before=operation.uncross_before,
        reason=reason,
    )


def _continuous_engine() -> MarketMechanicsEngine:
    engine = MarketMechanicsEngine(
        InstrumentRules(session_schedule=SessionSchedule(()))
    )
    engine.transition_session(SessionState.PREOPEN, reason="WO31_A_OPEN")
    engine.transition_session(SessionState.OPENING_AUCTION, reason="WO31_A_OPEN")
    engine.uncross_auction()
    engine.transition_session(SessionState.CONTINUOUS, reason="WO31_A_OPEN")
    return engine


def _limit(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    *,
    time_in_force: OrderInstruction = OrderInstruction.DAY,
    good_until_time_us: int | None = None,
    auction_only: bool = False,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id,
        side,
        quantity,
        OrderInstruction.LIMIT,
        OrderOwner.SIMULATED,
        f"ACCOUNT-{order_id}",
        price_ticks,
        time_in_force,
        good_until_time_us=good_until_time_us,
        auction_only=auction_only,
    )


__all__ = ["FullDayAuditCase", "audit_wo31a_contracts"]
