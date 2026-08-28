"""Non-persisting contract audit for the WO31-A full-day execution IR."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Callable

from kirby2.exchange.mechanics_engine import MarketMechanicsEngine
from kirby2.exchange.mechanics_models import (
    AdvancedOrderRequest,
    InstrumentRules,
    MechanicsEventType,
    OrderInstruction,
    ScheduledSessionState,
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


def audit_dev0004_atomic_boundary_replay() -> tuple[FullDayAuditCase, ...]:
    """Exercise the repaired shared-clock boundary replay and strict cut rule."""

    return (_mechanics_boundary_case(),)


def _expect_refusal(operation: Callable[[], object], label: str) -> str | None:
    try:
        operation()
    except (TypeError, ValueError, RuntimeError):
        return None
    return f"{label} was accepted"


def _dev0003_state_checkpoint_fields() -> frozenset[str]:
    """Return the complete semantic state-runtime inventory sealed by DEV-0003."""

    return frozenset(
        {
            "state.component_local_sequence",
            "state.component_sequence_offset",
            "state.current_day",
            "state.current_local",
            "state.day_elapsed_age_us",
            "state.day_entered_time_us",
            "state.day_next_eligible_transition_id",
            "state.day_next_eligible_transition_time_us",
            "state.day_sampled_deadline_us",
            "state.day_sampled_duration_us",
            "state.day_transition_count",
            "state.day_transitions_since_macro_anchor",
            "state.day_trigger_memory",
            "state.input_closed_through_time_us",
            "state.local_elapsed_age_us",
            "state.local_entered_time_us",
            "state.local_next_eligible_transition_id",
            "state.local_next_eligible_transition_time_us",
            "state.local_sampled_deadline_us",
            "state.local_sampled_duration_us",
            "state.local_transition_count",
            "state.local_trigger_memory",
            "state.next_macro_segment_index",
            "state.observation_ids_seen",
            "state.plan_sha256",
            "state.runtime_emission_count",
            "state.state_model_sha256",
        }
    )


def audit_dev0003_state_checkpoint_inventory() -> tuple[FullDayAuditCase, ...]:
    """Prove the state runtime's complete restorable authority is inventoried."""

    from kirby2.full_day import checkpoint_contract as contract
    from kirby2.full_day.checkpoint_contract import (
        CheckpointInventoryV1,
        checkpoint_inventory_v1,
    )

    inventory = checkpoint_inventory_v1()
    item_index = next(
        index
        for index, item in enumerate(inventory.items)
        if item.component_id
        == "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1"
    )
    item = inventory.items[item_index]
    expected = _dev0003_state_checkpoint_fields()
    actual = frozenset(item.owned_state_fields)
    failures: list[str] = []
    if actual != expected:
        failures.append(
            "state runtime checkpoint fields differ: "
            f"missing={tuple(sorted(expected - actual))} "
            f"extra={tuple(sorted(actual - expected))}"
        )
    restored = CheckpointInventoryV1.from_json_bytes(inventory.canonical_bytes())
    if restored.as_dict() != inventory.as_dict() or restored.sha256 != inventory.sha256:
        failures.append("amended checkpoint inventory did not round trip canonically")
    aliases = contract._SEMANTIC_FIELD_FAMILY_ALIASES
    if "state.completed_transition_count" in aliases:
        failures.append("obsolete aggregate transition-counter alias remains registered")

    omission_refusals = 0
    for field_id in sorted(expected):
        incomplete_item = replace(
            item,
            owned_state_fields=tuple(
                field for field in item.owned_state_fields if field != field_id
            ),
        )
        incomplete_items = tuple(
            incomplete_item if index == item_index else row
            for index, row in enumerate(inventory.items)
        )
        failure = _expect_refusal(
            lambda rows=incomplete_items: replace(inventory, items=rows),
            f"state checkpoint inventory omitting {field_id}",
        )
        if failure:
            failures.append(failure)
        else:
            omission_refusals += 1

    aggregate_item = replace(
        item,
        owned_state_fields=tuple(
            sorted(
                {
                    *(
                        field
                        for field in item.owned_state_fields
                        if field
                        not in {
                            "state.day_transition_count",
                            "state.local_transition_count",
                        }
                    ),
                    "state.completed_transition_count",
                }
            )
        ),
    )
    aggregate_rows = tuple(
        aggregate_item if index == item_index else row
        for index, row in enumerate(inventory.items)
    )
    failure = _expect_refusal(
        lambda: replace(inventory, items=aggregate_rows),
        "aggregate completed-transition counter replacing separate level counters",
    )
    if failure:
        failures.append(failure)

    return (
        FullDayAuditCase(
            "complete_state_runtime_checkpoint_authority",
            (
                f"owned_fields={len(expected)} omission_refusals={omission_refusals} "
                "plan_model_bindings=preserved separate_transition_counts=preserved "
                "obsolete_alias=absent"
            ),
            tuple(failures),
        ),
    )


def audit_dev0002_anchor_transition_ordering() -> tuple[FullDayAuditCase, ...]:
    """Exercise the repaired causal ordering at a macro-state anchor."""

    from kirby2.full_day.events import (
        FullDayEventPayloadV1,
        FullDayEventTypeV1,
        FullDayEventV1,
        ScheduledWorkKeyV1,
        WorkStageV1,
        canonical_event_prefix_sha256,
        validate_full_day_event_suffix,
        validate_full_day_event_stream,
    )
    from kirby2.full_day.checkpoint_contract import QuiescentCutV1
    from kirby2.full_day.models import canonical_sha256
    from kirby2.full_day.states import (
        DurationExhaustionBehaviorV1,
        DurationLawV1,
        DurationMassV1,
    )

    failures: list[str] = []
    base = replace(_sample_plan(), participant_schedule=(), scheduled_events=())

    def plan_with_first_two_states(
        *,
        quiet_duration_us: int,
        quiet_minimum_age_us: int,
        quiet_exhaustion: DurationExhaustionBehaviorV1,
        normal_duration_us: int = 10,
        normal_minimum_age_us: int = 10,
        normal_exhaustion: DurationExhaustionBehaviorV1 = (
            DurationExhaustionBehaviorV1.WAIT_FOR_TRIGGER
        ),
    ):
        day_rows = base.state_model.day_definitions
        quiet = day_rows[0]
        normal = day_rows[1]

        def fixed_law(duration_us: int) -> DurationLawV1:
            return DurationLawV1(
                duration_us,
                duration_us,
                (DurationMassV1(duration_us, 1),),
            )

        quiet_edge = replace(
            quiet.transitions[0],
            minimum_age_us=quiet_minimum_age_us,
            duration_exhaustion_behavior=quiet_exhaustion,
        )
        normal_edge = replace(
            normal.transitions[0],
            minimum_age_us=normal_minimum_age_us,
            duration_exhaustion_behavior=normal_exhaustion,
        )
        model = replace(
            base.state_model,
            day_definitions=(
                replace(
                    quiet,
                    duration_law=fixed_law(quiet_duration_us),
                    transitions=(quiet_edge,),
                ),
                replace(
                    normal,
                    duration_law=fixed_law(normal_duration_us),
                    transitions=(normal_edge,),
                ),
                *day_rows[2:],
            ),
        )
        return replace(base, state_model=model)

    def work(
        microstep: int,
        stage: WorkStageV1,
        local_sequence: int,
    ) -> ScheduledWorkKeyV1:
        return ScheduledWorkKeyV1(
            0,
            microstep,
            stage,
            "FULL_DAY_RUNTIME_V1",
            local_sequence,
        )

    def event(
        event_type: FullDayEventTypeV1,
        item: ScheduledWorkKeyV1,
        sequence: int,
        data: dict[str, object],
    ) -> FullDayEventV1:
        return FullDayEventV1(
            1,
            sequence,
            item.simulation_time_us,
            item.microstep,
            item.stage_ordinal,
            "FULL_DAY_RUNTIME_V1",
            sequence,
            event_type,
            (item.work_id,),
            FullDayEventPayloadV1(1, event_type.value, 1, None, data),
        )

    def trace(
        plan,
        *,
        anchor_microstep: int = 0,
        first_transition_microstep: int = 1,
        include_second_transition: bool = False,
    ) -> tuple[tuple[FullDayEventV1, ...], dict[str, ScheduledWorkKeyV1]]:
        boundary_work = work(0, WorkStageV1.ATOMIC_CALENDAR_BOUNDARY, 1)
        anchor_work = work(
            anchor_microstep,
            WorkStageV1.DAY_STATE_TRANSITION,
            2,
        )
        transition_work = work(
            first_transition_microstep,
            WorkStageV1.DAY_STATE_TRANSITION,
            3,
        )
        operation = plan.calendar.boundary_operations[0]
        segment = plan.macro_regime_schedule[0]
        quiet = plan.state_model.day_definitions[0]
        quiet_edge = quiet.transitions[0]
        normal = plan.state_model.day_definitions[1]
        rows = [
            event(
                FullDayEventTypeV1.CALENDAR_BOUNDARY,
                boundary_work,
                1,
                {
                    "boundary_operation_index": 0,
                    "destination_session_state": (
                        operation.destination_session_state.value
                    ),
                    "uncross_before": operation.uncross_before,
                },
            ),
            event(
                FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
                anchor_work,
                2,
                {
                    "anchored_state": segment.day_state.value,
                    "entered_time_us": 0,
                    "macro_segment_index": 0,
                    "macro_segment_sha256": canonical_sha256(segment.as_dict()),
                    "previous_state": plan.state_model.initial_day_state.value,
                    "sampled_duration_us": quiet.duration_law.masses[0].duration_us,
                },
            ),
            event(
                FullDayEventTypeV1.DAY_STATE_TRANSITION,
                transition_work,
                3,
                {
                    "entered_time_us": 0,
                    "new_state": quiet_edge.successor_state,
                    "previous_state": quiet_edge.source_state,
                    "sampled_duration_us": normal.duration_law.masses[0].duration_us,
                    "transition_id": quiet_edge.transition_id,
                    "trigger_id": quiet_edge.trigger_id,
                    "trigger_version": quiet_edge.trigger_version,
                },
            ),
        ]
        works = [boundary_work, anchor_work, transition_work]
        if include_second_transition:
            second_work = work(
                first_transition_microstep + 1,
                WorkStageV1.DAY_STATE_TRANSITION,
                4,
            )
            second_edge = normal.transitions[0]
            successor = next(
                definition
                for definition in plan.state_model.day_definitions
                if definition.state.value == second_edge.successor_state
            )
            rows.append(
                event(
                    FullDayEventTypeV1.DAY_STATE_TRANSITION,
                    second_work,
                    4,
                    {
                        "entered_time_us": 0,
                        "new_state": second_edge.successor_state,
                        "previous_state": second_edge.source_state,
                        "sampled_duration_us": (
                            successor.duration_law.masses[0].duration_us
                        ),
                        "transition_id": second_edge.transition_id,
                        "trigger_id": second_edge.trigger_id,
                        "trigger_version": second_edge.trigger_version,
                    },
                )
            )
            works.append(second_work)
        return tuple(rows), {item.work_id: item for item in works}

    forced_plan = plan_with_first_two_states(
        quiet_duration_us=0,
        quiet_minimum_age_us=0,
        quiet_exhaustion=(
            DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        ),
    )
    trigger_plan = plan_with_first_two_states(
        quiet_duration_us=10,
        quiet_minimum_age_us=0,
        quiet_exhaustion=DurationExhaustionBehaviorV1.WAIT_FOR_TRIGGER,
    )
    chain_plan = plan_with_first_two_states(
        quiet_duration_us=0,
        quiet_minimum_age_us=0,
        quiet_exhaustion=(
            DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        ),
        normal_duration_us=0,
        normal_minimum_age_us=0,
        normal_exhaustion=(
            DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        ),
    )

    accepted_traces = (
        ("forced post-anchor successor", forced_plan, trace(forced_plan)),
        ("triggered post-anchor successor", trigger_plan, trace(trigger_plan)),
        (
            "two-hop acyclic post-anchor chain",
            chain_plan,
            trace(chain_plan, include_second_transition=True),
        ),
    )
    accepted_keys: list[tuple[tuple[int, int, int], ...]] = []
    for label, plan, (events, executed) in accepted_traces:
        try:
            validate_full_day_event_stream(
                events,
                executed_work_items=executed,
                native_event_ledger={},
                scheduled_event_ledger={},
                full_day_plan=plan,
            )
        except (TypeError, ValueError) as error:
            failures.append(f"valid {label} was refused: {error}")
        accepted_keys.append(tuple(item.chronological_key for item in events))

    late_age_plan = plan_with_first_two_states(
        quiet_duration_us=0,
        quiet_minimum_age_us=1,
        quiet_exhaustion=(
            DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        ),
    )
    hostile = (
        (
            "same-microstep post-anchor successor",
            forced_plan,
            trace(forced_plan, first_transition_microstep=0),
        ),
        (
            "nonzero-microstep macro anchor",
            forced_plan,
            trace(
                forced_plan,
                anchor_microstep=1,
                first_transition_microstep=2,
            ),
        ),
        (
            "same-time successor before minimum-age eligibility",
            late_age_plan,
            trace(late_age_plan),
        ),
    )
    for label, plan, (events, executed) in hostile:
        refusal = _expect_refusal(
            lambda events=events, executed=executed, plan=plan: (
                validate_full_day_event_stream(
                    events,
                    executed_work_items=executed,
                    native_event_ledger={},
                    scheduled_event_ledger={},
                    full_day_plan=plan,
                )
            ),
            label,
        )
        if refusal:
            failures.append(refusal)

    suffix_plan = base
    boundary_work = work(0, WorkStageV1.ATOMIC_CALENDAR_BOUNDARY, 1)
    anchor_work = work(0, WorkStageV1.DAY_STATE_TRANSITION, 2)
    marker_work = work(0, WorkStageV1.CHECKPOINT_CAPTURE, 3)
    operation = suffix_plan.calendar.boundary_operations[0]
    segment = suffix_plan.macro_regime_schedule[0]
    quiet = suffix_plan.state_model.day_definitions[0]
    prefix_events = (
        event(
            FullDayEventTypeV1.CALENDAR_BOUNDARY,
            boundary_work,
            1,
            {
                "boundary_operation_index": 0,
                "destination_session_state": operation.destination_session_state.value,
                "uncross_before": operation.uncross_before,
            },
        ),
        event(
            FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
            anchor_work,
            2,
            {
                "anchored_state": segment.day_state.value,
                "entered_time_us": 0,
                "macro_segment_index": 0,
                "macro_segment_sha256": canonical_sha256(segment.as_dict()),
                "previous_state": suffix_plan.state_model.initial_day_state.value,
                "sampled_duration_us": quiet.duration_law.masses[0].duration_us,
            },
        ),
        event(
            FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER,
            marker_work,
            3,
            {"checkpoint_request_id": "DEV_0002_PREFIX_CUT"},
        ),
    )
    prefix_cut = QuiescentCutV1(
        schema_version=1,
        simulation_time_us=0,
        microstep=0,
        checkpoint_stage_ordinal=int(WorkStageV1.CHECKPOINT_CAPTURE),
        last_global_event_sequence=3,
        event_prefix_last_global_sequence=3,
        event_prefix_sha256=canonical_event_prefix_sha256(prefix_events),
        pending_work_count=1,
        next_pending_time_us=10,
        next_pending_microstep=0,
        due_work_at_or_before_cut=0,
        generated_microsteps_complete=True,
        checkpoint_stage_complete=True,
        boundary_complete_at_cut=True,
    )

    def suffix_transition(
        *,
        simulation_time_us: int,
        transition,
    ) -> tuple[FullDayEventV1, ScheduledWorkKeyV1]:
        item = ScheduledWorkKeyV1(
            simulation_time_us,
            0,
            WorkStageV1.DAY_STATE_TRANSITION,
            "FULL_DAY_RUNTIME_V1",
            4,
        )
        successor = next(
            definition
            for definition in suffix_plan.state_model.day_definitions
            if definition.state.value == transition.successor_state
        )
        return (
            event(
                FullDayEventTypeV1.DAY_STATE_TRANSITION,
                item,
                4,
                {
                    "entered_time_us": simulation_time_us,
                    "new_state": transition.successor_state,
                    "previous_state": transition.source_state,
                    "sampled_duration_us": (
                        successor.duration_law.masses[0].duration_us
                    ),
                    "transition_id": transition.transition_id,
                    "trigger_id": transition.trigger_id,
                    "trigger_version": transition.trigger_version,
                },
            ),
            item,
        )

    def validate_suffix(
        suffix_event: FullDayEventV1,
        suffix_work: ScheduledWorkKeyV1,
    ) -> None:
        validate_full_day_event_suffix(
            (suffix_event,),
            executed_work_items={suffix_work.work_id: suffix_work},
            native_event_ledger={},
            scheduled_event_ledger={},
            full_day_plan=suffix_plan,
            verified_prefix_cut=prefix_cut,
            verified_prefix_events=prefix_events,
        )

    quiet_edge = quiet.transitions[0]
    valid_suffix_event, valid_suffix_work = suffix_transition(
        simulation_time_us=10,
        transition=quiet_edge,
    )
    try:
        validate_suffix(valid_suffix_event, valid_suffix_work)
    except (TypeError, ValueError) as error:
        failures.append(f"valid prefix-bound day-state suffix was refused: {error}")

    normal_edge = suffix_plan.state_model.day_definitions[1].transitions[0]
    wrong_state_event, wrong_state_work = suffix_transition(
        simulation_time_us=1,
        transition=normal_edge,
    )
    early_event, early_work = suffix_transition(
        simulation_time_us=1,
        transition=quiet_edge,
    )
    suffix_hostile = (
        (
            "suffix transition whose previous state differs from its verified prefix",
            wrong_state_event,
            wrong_state_work,
        ),
        (
            "suffix transition before verified-prefix minimum-age eligibility",
            early_event,
            early_work,
        ),
    )
    for label, suffix_event, suffix_work in suffix_hostile:
        refusal = _expect_refusal(
            lambda suffix_event=suffix_event, suffix_work=suffix_work: validate_suffix(
                suffix_event,
                suffix_work,
            ),
            label,
        )
        if refusal:
            failures.append(refusal)

    return (
        FullDayAuditCase(
            "macro_anchor_later_microstep_transition_reconciliation",
            (
                f"accepted_traces={len(accepted_traces) + 1} "
                f"hostile_refusals={len(hostile) + len(suffix_hostile)} "
                f"prefix_bound_suffix=true keys={tuple(accepted_keys)}"
            ),
            tuple(failures),
        ),
    )


def audit_wo31b_transitions() -> tuple[FullDayAuditCase, ...]:
    """Exercise duration-aware state runtime truth without composing Hawkes."""

    return (
        _wo31b_frozen_state_contract_case(),
        _wo31b_partition_wire_seed_case(),
        _wo31b_trigger_causality_stage_age_case(),
        _wo31b_macro_anchor_authority_case(),
        _wo31b_runtime_limits_restore_and_allocator_case(),
        _wo31b_modifier_and_price_boundary_case(),
        _wo31b_hawkes_regression_case(),
        FullDayAuditCase(
            "hawkes_flow_composition_capability",
            "Hawkes remains outside the initial composition until WO31-E2",
            (),
            status_override="NOT_EXERCISED",
            reason_code="HAWKES_COMPOSITION_DEFERRED_TO_WO31_E2",
            required=False,
        ),
    )


def _wo31b_frozen_state_contract_case() -> FullDayAuditCase:
    """Prove the runtime's immutable duration/graph/effect inputs fail closed."""

    from kirby2.full_day.states import (
        DayStateV1,
        DurationExhaustionBehaviorV1,
        DurationLawV1,
        DurationMassV1,
        ParameterEffectV1,
        ParameterTargetV1,
    )

    failures: list[str] = []
    law = DurationLawV1(
        3,
        11,
        (
            DurationMassV1(3, 1),
            DurationMassV1(7, 2),
            DurationMassV1(11, 3),
        ),
    )
    restored_law = DurationLawV1.from_dict(law.as_dict())
    if restored_law != law:
        failures.append("finite duration-law strict wire round trip changed values")
    if (
        law.expected_duration_numerator,
        law.expected_duration_denominator,
    ) != (25, 3):
        failures.append("finite integer-weight duration expectation is not reduced")
    if tuple(item.duration_us for item in law.masses) != (3, 7, 11):
        failures.append("finite duration support changed declared integer bounds")
    if any(
        type(item.duration_us) is not int
        or type(item.weight) is not int
        or item.weight <= 0
        for item in law.masses
    ):
        failures.append("duration law contains a noninteger or nonpositive mass")

    model = _sample_state_model()
    zero_law = DurationLawV1(0, 0, (DurationMassV1(0, 1),))
    first = model.day_definitions[0]
    try:
        replace(
            model,
            day_definitions=(
                replace(
                    first,
                    duration_law=zero_law,
                    transitions=(
                        replace(
                            first.transitions[0],
                            minimum_age_us=0,
                            duration_exhaustion_behavior=(
                                DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
                            ),
                        ),
                    ),
                ),
                *model.day_definitions[1:],
            ),
        )
    except (TypeError, ValueError) as error:
        failures.append(f"acyclic zero-duration state path was refused: {error}")

    forced_cycle = tuple(
        replace(
            definition,
            duration_law=zero_law,
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
        for definition in model.day_definitions
    )
    graph_breaker = replace(
        model.day_definitions[-1],
        transitions=(
            replace(
                model.day_definitions[-1].transitions[0],
                successor_state=DayStateV1.NORMAL.value,
            ),
        ),
    )
    probes: tuple[tuple[str, Callable[[], object]], ...] = (
        (
            "duration mass with floating microseconds",
            lambda: DurationMassV1(3.0, 1),  # type: ignore[arg-type]
        ),
        ("duration mass with zero weight", lambda: DurationMassV1(3, 0)),
        (
            "duration law whose maximum differs from its support",
            lambda: DurationLawV1(3, 12, law.masses),
        ),
        (
            "state graph with no predecessor for its initial state",
            lambda: replace(
                model,
                day_definitions=(*model.day_definitions[:-1], graph_breaker),
            ),
        ),
        (
            "forced zero-time state cycle",
            lambda: replace(model, day_definitions=forced_cycle),
        ),
        (
            "parameter modifier above its declared maximum",
            lambda: ParameterEffectV1(
                ParameterTargetV1.ORDER_SIZE_SCALE,
                3,
                1,
                1,
                1,
                2,
                1,
            ),
        ),
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "duration_graph_and_modifier_contracts",
        (
            "finite_support=(3,7,11) integer_weights=(1,2,3) "
            "expected_duration=25/3; zero_time_acyclic=accepted; "
            f"hostile_refusals={len(probes)}"
        ),
        tuple(failures),
    )


def _wo31b_hawkes_regression_case() -> FullDayAuditCase:
    """Keep Hawkes scientifically green without claiming it in composition."""

    from kirby2.audit.hawkes import audit_hawkes_stability
    from kirby2.full_day.composition import initial_composition_matrix

    cases = audit_hawkes_stability()
    profile = initial_composition_matrix().profiles[0]
    failures = [
        f"Hawkes regression failed: {case.name}"
        for case in cases
        if not case.passed
    ]
    if "FLOW_HAWKES" not in profile.refused_component_ids:
        failures.append("initial composition no longer refuses FLOW_HAWKES")
    if any(component.component_id == "FLOW_HAWKES" for component in profile.components):
        failures.append("FLOW_HAWKES appeared in the executable component set")
    status, reason = profile.component_status_and_reason("FLOW_HAWKES")
    if (status, reason) != (
        "REFUSED",
        "COMPOSITION_PROFILE_REFUSES_COMPONENT",
    ):
        failures.append("Hawkes composition refusal lost its stable capability reason")
    classifications = tuple(
        sorted({case.certification.classification for case in cases})
    )
    return FullDayAuditCase(
        "hawkes_stability_regression_boundary",
        (
            f"regression_cases={len(cases)} classifications={classifications}; "
            "composition=NOT_EXERCISED"
        ),
        tuple(failures),
    )


def _wo31b_runtime_plan(
    *,
    root_seed: int = 42,
    transition_on_exhaustion: bool = True,
    parallel_edges: bool = True,
    ground_truth_triggers: bool = False,
    include_effects: bool = True,
):
    """Return a compact authoritative plan that exercises the WO31-B runtime."""

    from kirby2.full_day.models import (
        FULL_DAY_SUBSTREAM_POLICY_VERSION,
        SeedPolicyV1,
        SubstreamDeclarationV1,
        derive_substream_seed,
    )
    from kirby2.full_day.states import (
        DurationExhaustionBehaviorV1,
        DurationLawV1,
        DurationMassV1,
        ParameterEffectV1,
        ParameterTargetV1,
        TriggerInformationClassV1,
    )

    plan = _sample_plan()
    law = DurationLawV1(
        5,
        11,
        (
            DurationMassV1(5, 1),
            DurationMassV1(7, 2),
            DurationMassV1(11, 3),
        ),
    )
    state_effects = (
        ParameterEffectV1(
            ParameterTargetV1.ORDER_SIZE_SCALE,
            3,
            2,
            1,
            1,
            2,
            1,
        ),
        ParameterEffectV1(
            ParameterTargetV1.PARTICIPANT_ACTIVITY_SCALE,
            3,
            4,
            1,
            2,
            3,
            2,
        ),
    ) if include_effects else ()
    transition_effects = (
        ParameterEffectV1(
            ParameterTargetV1.MARKET_BUY_INTENSITY,
            2,
            1,
            1,
            2,
            3,
            1,
        ),
    ) if include_effects else ()
    information_class = (
        TriggerInformationClassV1.SYNTHETIC_GROUND_TRUTH
        if ground_truth_triggers
        else TriggerInformationClassV1.OBSERVABLE_AT_TIME
    )
    trigger_id = (
        "AUDIT_GROUND_TRUTH_TRIGGER_V1"
        if ground_truth_triggers
        else "AGE_ELIGIBLE_V1"
    )
    exhaustion_behavior = (
        DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
        if transition_on_exhaustion
        else DurationExhaustionBehaviorV1.WAIT_FOR_TRIGGER
    )

    def definitions(rows):
        states = tuple(item.state for item in rows)
        result = []
        for index, definition in enumerate(rows):
            primary = replace(
                definition.transitions[0],
                minimum_age_us=5,
                duration_exhaustion_behavior=exhaustion_behavior,
                weight=1,
                trigger_id=trigger_id,
                trigger_information_class=information_class,
                parameter_effects=transition_effects,
            )
            transitions = [primary]
            if parallel_edges:
                transitions.append(
                    replace(
                        primary,
                        transition_id=f"{primary.transition_id}_ALT",
                        successor_state=states[(index + 2) % len(states)].value,
                        weight=3,
                    )
                )
            result.append(
                replace(
                    definition,
                    duration_law=law,
                    parameter_effects=state_effects,
                    transitions=tuple(
                        sorted(transitions, key=lambda item: item.transition_id)
                    ),
                )
            )
        return tuple(result)

    state_model = replace(
        plan.state_model,
        day_definitions=definitions(plan.state_model.day_definitions),
        local_definitions=definitions(plan.state_model.local_definitions),
    )
    substreams = tuple(
        SubstreamDeclarationV1(
            declaration.semantic_path,
            derive_substream_seed(
                root_seed,
                FULL_DAY_SUBSTREAM_POLICY_VERSION,
                declaration.semantic_path,
            ),
        )
        for declaration in plan.seed_policy.substreams
    )
    seed_policy = SeedPolicyV1(
        1,
        FULL_DAY_SUBSTREAM_POLICY_VERSION,
        root_seed,
        substreams,
    )
    return replace(plan, state_model=state_model, seed_policy=seed_policy)


def _wo31b_check_runtime_wire(
    runtime,
    plan,
    label: str,
    failures: list[str],
) -> None:
    from kirby2.full_day.transitions import (
        HierarchicalStateRuntimeStateV1,
        HierarchicalStateRuntimeV1,
    )

    state = runtime.state()
    wire = state.canonical_bytes()
    try:
        restored_state = HierarchicalStateRuntimeStateV1.from_json_bytes(wire)
        restored_runtime = HierarchicalStateRuntimeV1.from_state(
            plan,
            restored_state,
            verified_component_local_sequence_floor=(
                state.component_local_sequence
            ),
        )
    except (TypeError, ValueError) as error:
        failures.append(f"{label} runtime wire was refused: {error}")
        return
    if restored_state.canonical_bytes() != wire:
        failures.append(f"{label} runtime state changed canonical bytes")
    if restored_runtime.state().canonical_bytes() != wire:
        failures.append(f"{label} restored runtime changed canonical state")


def _wo31b_trigger_observation(
    plan,
    runtime,
    level,
    *,
    observation_id: str,
    observation_time_us: int,
    available_time_us: int | None = None,
    information_cutoff_us: int | None = None,
    phase=None,
    triggered: bool = True,
    information_class=None,
):
    from kirby2.full_day.models import canonical_sha256
    from kirby2.full_day.transitions import (
        StateLevelV1,
        TriggerObservationPhaseV1,
        TriggerObservationV1,
        trigger_parameter_set_sha256_v1,
    )

    snapshot = runtime.state()
    if level is StateLevelV1.DAY:
        selected_id = snapshot.day.next_eligible_transition_id
        definitions = plan.state_model.day_definitions
    elif level is StateLevelV1.LOCAL:
        selected_id = snapshot.local.next_eligible_transition_id
        definitions = plan.state_model.local_definitions
    else:
        raise TypeError("audit trigger observation requires StateLevelV1")
    transition = next(
        item
        for definition in definitions
        for item in definition.transitions
        if item.transition_id == selected_id
    )
    return TriggerObservationV1(
        1,
        observation_id,
        transition.transition_id,
        transition.trigger_id,
        transition.trigger_version,
        trigger_parameter_set_sha256_v1(transition),
        transition.trigger_information_class
        if information_class is None
        else information_class,
        observation_time_us,
        observation_time_us
        if information_cutoff_us is None
        else information_cutoff_us,
        observation_time_us if available_time_us is None else available_time_us,
        TriggerObservationPhaseV1.PRE_TRANSITION if phase is None else phase,
        triggered,
        canonical_sha256(
            {
                "audit_observation_id": observation_id,
                "selected_transition_id": transition.transition_id,
            }
        ),
    )


def _wo31b_partition_wire_seed_case() -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes
    from kirby2.full_day.transitions import (
        HierarchicalStateRuntimeStateV1,
        HierarchicalStateRuntimeV1,
        StateLevelV1,
        StateTransitionEmissionV1,
    )

    failures: list[str] = []
    plan = _wo31b_runtime_plan()
    large = HierarchicalStateRuntimeV1.create(plan)
    large_emissions = large.advance_to(40)
    runtime_state = large.state()
    expected_top_level_keys = {
        "component_local_sequence",
        "component_sequence_offset",
        "current_time_us",
        "day",
        "day_rng",
        "day_transition_count",
        "day_transitions_since_macro_anchor",
        "input_closed_through_time_us",
        "local",
        "local_rng",
        "local_transition_count",
        "next_macro_segment_index",
        "observation_ids_seen",
        "plan_sha256",
        "runtime_emission_count",
        "schema_version",
        "state_model_sha256",
    }
    expected_level_keys = {
        "as_of_time_us",
        "current_state",
        "deadline_time_us",
        "elapsed_age_us",
        "entered_time_us",
        "level",
        "next_eligible_transition_id",
        "next_eligible_transition_time_us",
        "sampled_duration_us",
        "trigger_memory",
    }
    runtime_payload = runtime_state.as_dict()
    if set(runtime_payload) != expected_top_level_keys:
        failures.append("runtime state top-level authority/delegation shape changed")
    for level_name in ("day", "local"):
        level_payload = runtime_payload[level_name]
        if type(level_payload) is not dict or set(level_payload) != expected_level_keys:
            failures.append(f"runtime {level_name} state wire shape changed")
    checkpoint_semantic_state = {
        "state.component_local_sequence": runtime_state.component_local_sequence,
        "state.component_sequence_offset": runtime_state.component_sequence_offset,
        "state.current_day": runtime_state.day.current_state,
        "state.current_local": runtime_state.local.current_state,
        "state.day_elapsed_age_us": runtime_state.day.elapsed_age_us,
        "state.day_entered_time_us": runtime_state.day.entered_time_us,
        "state.day_next_eligible_transition_id": (
            runtime_state.day.next_eligible_transition_id
        ),
        "state.day_next_eligible_transition_time_us": (
            runtime_state.day.next_eligible_transition_time_us
        ),
        "state.day_sampled_deadline_us": runtime_state.day.deadline_time_us,
        "state.day_sampled_duration_us": runtime_state.day.sampled_duration_us,
        "state.day_transition_count": runtime_state.day_transition_count,
        "state.day_transitions_since_macro_anchor": (
            runtime_state.day_transitions_since_macro_anchor
        ),
        "state.day_trigger_memory": runtime_state.day.trigger_memory,
        "state.input_closed_through_time_us": (
            runtime_state.input_closed_through_time_us
        ),
        "state.local_elapsed_age_us": runtime_state.local.elapsed_age_us,
        "state.local_entered_time_us": runtime_state.local.entered_time_us,
        "state.local_next_eligible_transition_id": (
            runtime_state.local.next_eligible_transition_id
        ),
        "state.local_next_eligible_transition_time_us": (
            runtime_state.local.next_eligible_transition_time_us
        ),
        "state.local_sampled_deadline_us": runtime_state.local.deadline_time_us,
        "state.local_sampled_duration_us": runtime_state.local.sampled_duration_us,
        "state.local_transition_count": runtime_state.local_transition_count,
        "state.local_trigger_memory": runtime_state.local.trigger_memory,
        "state.next_macro_segment_index": runtime_state.next_macro_segment_index,
        "state.observation_ids_seen": runtime_state.observation_ids_seen,
        "state.plan_sha256": runtime_state.plan_sha256,
        "state.runtime_emission_count": runtime_state.runtime_emission_count,
        "state.state_model_sha256": runtime_state.state_model_sha256,
    }
    if set(checkpoint_semantic_state) != _dev0003_state_checkpoint_fields():
        failures.append("runtime state does not project the amended checkpoint inventory")
    boundary_times = tuple(
        sorted({item.simulation_time_us for item in large_emissions} | {40})
    )
    subdivided = HierarchicalStateRuntimeV1.create(plan)
    subdivided_emissions = tuple(
        emission
        for boundary_time in boundary_times
        for emission in subdivided.advance_to(boundary_time)
    )
    large_bytes = tuple(
        canonical_json_bytes(item.as_dict()) for item in large_emissions
    )
    subdivided_bytes = tuple(
        canonical_json_bytes(item.as_dict()) for item in subdivided_emissions
    )
    if large_bytes != subdivided_bytes:
        failures.append("large and exact-boundary subdivided advances changed emissions")
    if tuple(item.event_key for item in large_emissions) != tuple(
        item.event_key for item in subdivided_emissions
    ):
        failures.append("large and subdivided advances changed event keys")
    if large.state().canonical_bytes() != subdivided.state().canonical_bytes():
        failures.append("large and subdivided advances changed terminal runtime state")
    support = {5, 7, 11}
    sampled = {
        large.state().day.sampled_duration_us,
        large.state().local.sampled_duration_us,
        *(item.sampled_duration_us for item in large_emissions),
    }
    if not sampled.issubset(support):
        failures.append("runtime duration sampling escaped finite integer support")

    same_seed = HierarchicalStateRuntimeV1.create(plan)
    same_seed_emissions = same_seed.advance_to(40)
    if tuple(canonical_json_bytes(item.as_dict()) for item in same_seed_emissions) != large_bytes:
        failures.append("same seed did not reproduce transition event identity")
    if same_seed.state().canonical_bytes() != large.state().canonical_bytes():
        failures.append("same seed did not reproduce terminal state identity")
    different_plan = _wo31b_runtime_plan(root_seed=43)
    different_seed = HierarchicalStateRuntimeV1.create(different_plan)
    same_start = (
        different_seed.state().day.current_state,
        different_seed.state().local.current_state,
    ) == (
        HierarchicalStateRuntimeV1.create(plan).state().day.current_state,
        HierarchicalStateRuntimeV1.create(plan).state().local.current_state,
    )
    different_emissions = different_seed.advance_to(40)
    if not same_start:
        failures.append("different-seed comparison did not begin from the same states")
    different_transitions = tuple(
        item
        for item in different_emissions
        if type(item) is StateTransitionEmissionV1
    )
    large_transitions = tuple(
        item for item in large_emissions if type(item) is StateTransitionEmissionV1
    )
    if tuple(
        (
            item.level.value,
            item.simulation_time_us,
            item.transition_id,
            item.new_state,
            item.sampled_duration_us,
        )
        for item in different_transitions
    ) == tuple(
        (
            item.level.value,
            item.simulation_time_us,
            item.transition_id,
            item.new_state,
            item.sampled_duration_us,
        )
        for item in large_transitions
    ):
        failures.append("different seeds did not produce a divergent state path")

    trigger_plan = _wo31b_runtime_plan(
        transition_on_exhaustion=False,
        parallel_edges=False,
        include_effects=False,
    )
    pre_eligibility = HierarchicalStateRuntimeV1.create(trigger_plan)
    if any(
        type(item) is StateTransitionEmissionV1
        for item in pre_eligibility.advance_to(4)
    ):
        failures.append("state transitioned before minimum-age eligibility")
    _wo31b_check_runtime_wire(
        pre_eligibility, trigger_plan, "pre-eligibility", failures
    )
    trigger_runtime = HierarchicalStateRuntimeV1.create(trigger_plan)
    trigger_observations = tuple(
        _wo31b_trigger_observation(
            trigger_plan,
            trigger_runtime,
            level,
            observation_id=f"AUDIT_EXACT_TRIGGER_{level.value}",
            observation_time_us=5,
        )
        for level in (StateLevelV1.DAY, StateLevelV1.LOCAL)
    )
    exact_trigger_emissions = trigger_runtime.advance_to(5, trigger_observations)
    if sum(
        type(item) is StateTransitionEmissionV1
        for item in exact_trigger_emissions
    ) != 2:
        failures.append("exact trigger boundary did not transition both state levels")
    _wo31b_check_runtime_wire(trigger_runtime, trigger_plan, "exact-trigger", failures)
    deadline_runtime = HierarchicalStateRuntimeV1.create(plan)
    exact_deadline = min(
        deadline_runtime.state().day.deadline_time_us,
        deadline_runtime.state().local.deadline_time_us,
    )
    if not any(
        type(item) is StateTransitionEmissionV1
        for item in deadline_runtime.advance_to(exact_deadline)
    ):
        failures.append("exact sampled deadline did not emit a transition")
    _wo31b_check_runtime_wire(deadline_runtime, plan, "exact-deadline", failures)

    hostile_state = copy.deepcopy(pre_eligibility.state().as_dict())
    extra = copy.deepcopy(hostile_state)
    extra["ambient_runtime_default"] = 1
    missing = copy.deepcopy(hostile_state)
    del missing["day_rng"]
    wrong_scalar = copy.deepcopy(hostile_state)
    wrong_scalar["day"]["elapsed_age_us"] = True  # type: ignore[index]
    inconsistent_age = copy.deepcopy(hostile_state)
    inconsistent_age["local"]["elapsed_age_us"] = 3  # type: ignore[index]
    for label, payload in (
        ("runtime state with an extra field", extra),
        ("runtime state missing day RNG", missing),
        ("runtime state with Boolean elapsed age", wrong_scalar),
        ("runtime state with unreconciled elapsed age", inconsistent_age),
    ):
        failure = _expect_refusal(
            lambda payload=payload: HierarchicalStateRuntimeStateV1.from_dict(payload),
            label,
        )
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "state_partition_seed_duration_and_wire",
        (
            f"emissions={len(large_emissions)} exact_boundaries={boundary_times}; "
            f"event_keys={tuple(item.event_key for item in large_emissions)}; "
            f"sampled_support={tuple(sorted(sampled))}; strict_wire_boundaries=3; "
            "checkpoint_owned_fields=27; different_seed=43"
        ),
        tuple(failures),
    )


def _wo31b_trigger_causality_stage_age_case() -> FullDayAuditCase:
    from kirby2.full_day.events import WorkStageV1
    from kirby2.full_day.states import TriggerInformationClassV1
    from kirby2.full_day.transitions import (
        HierarchicalStateRuntimeV1,
        StateLevelV1,
        StateTransitionEmissionV1,
        TriggerObservationPhaseV1,
    )

    failures: list[str] = []
    plan = _wo31b_runtime_plan(
        transition_on_exhaustion=False,
        parallel_edges=False,
        include_effects=False,
    )
    runtime = HierarchicalStateRuntimeV1.create(plan)
    initial_states = (
        runtime.state().day.current_state,
        runtime.state().local.current_state,
    )
    observation_count = 0
    for simulation_time_us in range(1, 5):
        observations = tuple(
            _wo31b_trigger_observation(
                plan,
                runtime,
                level,
                observation_id=(
                    f"AUDIT_AGE_{simulation_time_us}_{level.value}"
                ),
                observation_time_us=simulation_time_us,
                triggered=False,
            )
            for level in (StateLevelV1.DAY, StateLevelV1.LOCAL)
        )
        observation_count += len(observations)
        if any(
            type(item) is StateTransitionEmissionV1
            for item in runtime.advance_to(simulation_time_us, observations)
        ):
            failures.append("untriggered event changed hierarchical state")
        state = runtime.state()
        if (
            state.day.elapsed_age_us,
            state.local.elapsed_age_us,
        ) != (simulation_time_us, simulation_time_us):
            failures.append(
                f"state age did not survive event time {simulation_time_us}"
            )
        if (state.day.current_state, state.local.current_state) != initial_states:
            failures.append("untriggered observations changed current state")

    triggered_observations = tuple(
        _wo31b_trigger_observation(
            plan,
            runtime,
            level,
            observation_id=f"AUDIT_OBSERVABLE_TRIGGER_{level.value}",
            observation_time_us=5,
        )
        for level in (StateLevelV1.DAY, StateLevelV1.LOCAL)
    )
    emissions = tuple(
        item
        for item in runtime.advance_to(5, triggered_observations)
        if type(item) is StateTransitionEmissionV1
    )
    if tuple(item.level for item in emissions) != (
        StateLevelV1.DAY,
        StateLevelV1.LOCAL,
    ):
        failures.append("equal-time state emissions did not order day before local")
    if tuple(item.stage for item in emissions) != (
        WorkStageV1.DAY_STATE_TRANSITION,
        WorkStageV1.LOCAL_STATE_TRANSITION,
    ):
        failures.append("equal-time state emissions used the wrong frozen stages")
    if tuple(item.event_key for item in emissions) != tuple(
        sorted(item.event_key for item in emissions)
    ):
        failures.append("equal-time state event keys are not chronologically ordered")
    if emissions and not all(
        int(item.stage)
        < int(WorkStageV1.PARTICIPANT_ACTIVATION_DEACTIVATION_RETUNE)
        for item in emissions
    ):
        failures.append("state emissions do not precede newly parameterized actions")
    if (
        runtime.state().day.elapsed_age_us,
        runtime.state().local.elapsed_age_us,
    ) != (0, 0):
        failures.append("state age did not reset exactly on transition")
    runtime.advance_to(7)
    if (
        runtime.state().day.elapsed_age_us,
        runtime.state().local.elapsed_age_us,
    ) != (2, 2):
        failures.append("state age did not continue across a post-transition event")

    ground_plan = _wo31b_runtime_plan(
        transition_on_exhaustion=False,
        parallel_edges=False,
        ground_truth_triggers=True,
        include_effects=False,
    )
    ground_runtime = HierarchicalStateRuntimeV1.create(ground_plan)
    ground_observations = tuple(
        _wo31b_trigger_observation(
            ground_plan,
            ground_runtime,
            level,
            observation_id=f"AUDIT_GROUND_TRIGGER_{level.value}",
            observation_time_us=5,
        )
        for level in (StateLevelV1.DAY, StateLevelV1.LOCAL)
    )
    if sum(
        type(item) is StateTransitionEmissionV1
        for item in ground_runtime.advance_to(5, ground_observations)
    ) != 2:
        failures.append("synthetic-ground-truth trigger boundary did not transition")

    probes: list[tuple[str, Callable[[], object]]] = []
    future_runtime = HierarchicalStateRuntimeV1.create(plan)
    probes.append(
        (
            "future observation whose availability precedes its observation time",
            lambda: _wo31b_trigger_observation(
                plan,
                future_runtime,
                StateLevelV1.DAY,
                observation_id="AUDIT_FUTURE_READ",
                observation_time_us=6,
                available_time_us=5,
            ),
        )
    )

    beyond_runtime = HierarchicalStateRuntimeV1.create(plan)
    beyond_observation = _wo31b_trigger_observation(
        plan,
        beyond_runtime,
        StateLevelV1.DAY,
        observation_id="AUDIT_BEYOND_TARGET",
        observation_time_us=5,
        available_time_us=6,
    )
    probes.append(
        (
            "observation beyond the advance target",
            lambda: beyond_runtime.advance_to(5, (beyond_observation,)),
        )
    )
    for phase in (
        TriggerObservationPhaseV1.REVEAL_ONLY,
        TriggerObservationPhaseV1.POST_TRANSITION,
    ):
        phase_runtime = HierarchicalStateRuntimeV1.create(plan)
        phase_observation = _wo31b_trigger_observation(
            plan,
            phase_runtime,
            StateLevelV1.DAY,
            observation_id=f"AUDIT_FORBIDDEN_{phase.value}",
            observation_time_us=5,
            phase=phase,
        )
        probes.append(
            (
                f"{phase.value} trigger observation",
                lambda phase_runtime=phase_runtime,
                phase_observation=phase_observation: phase_runtime.advance_to(
                    5, (phase_observation,)
                ),
            )
        )

    unmatched_runtime = HierarchicalStateRuntimeV1.create(plan)
    unmatched = replace(
        _wo31b_trigger_observation(
            plan,
            unmatched_runtime,
            StateLevelV1.DAY,
            observation_id="AUDIT_UNMATCHED_TRANSITION",
            observation_time_us=5,
        ),
        transition_id="INVENTED_TRANSITION_V1",
    )
    probes.append(
        (
            "observation for an unmatched transition",
            lambda: unmatched_runtime.advance_to(5, (unmatched,)),
        )
    )
    wrong_class_runtime = HierarchicalStateRuntimeV1.create(plan)
    wrong_class = _wo31b_trigger_observation(
        plan,
        wrong_class_runtime,
        StateLevelV1.DAY,
        observation_id="AUDIT_WRONG_INFORMATION_CLASS",
        observation_time_us=5,
        information_class=TriggerInformationClassV1.SYNTHETIC_GROUND_TRUTH,
    )
    probes.append(
        (
            "observable transition evaluated with ground-truth information",
            lambda: wrong_class_runtime.advance_to(5, (wrong_class,)),
        )
    )
    wrong_digest_runtime = HierarchicalStateRuntimeV1.create(plan)
    wrong_digest = replace(
        _wo31b_trigger_observation(
            plan,
            wrong_digest_runtime,
            StateLevelV1.DAY,
            observation_id="AUDIT_WRONG_TRIGGER_PARAMETERS",
            observation_time_us=5,
        ),
        trigger_parameter_set_sha256="f" * 64,
    )
    probes.append(
        (
            "observation with a forged trigger-parameter digest",
            lambda: wrong_digest_runtime.advance_to(5, (wrong_digest,)),
        )
    )
    for label, operation in probes:
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "trigger_causality_stage_order_and_age",
        (
            f"age_event_observations={observation_count}; equal_time_keys="
            f"{tuple(item.event_key for item in emissions)}; "
            "observable=accepted ground_truth=accepted; "
            f"causal_refusals={len(probes)}"
        ),
        tuple(failures),
    )


def _wo31b_macro_anchor_authority_case() -> FullDayAuditCase:
    from kirby2.full_day.events import WorkStageV1
    from kirby2.full_day.models import MacroRegimeSegmentV1, canonical_sha256
    from kirby2.full_day.states import DayStateV1
    from kirby2.full_day.transitions import (
        DayStateAnchorEmissionV1,
        HierarchicalStateRuntimeStateV1,
        HierarchicalStateRuntimeV1,
        StateLevelV1,
        project_anchor_payload_v1,
    )

    failures: list[str] = []
    base = _wo31b_runtime_plan(
        transition_on_exhaustion=False,
        parallel_edges=True,
        include_effects=True,
    )
    plan = replace(
        base,
        macro_regime_schedule=(
            MacroRegimeSegmentV1(0, 15, DayStateV1.QUIET),
            MacroRegimeSegmentV1(
                15,
                base.calendar.end_time_us,
                DayStateV1.RISK_OFF,
            ),
        ),
    )
    runtime = HierarchicalStateRuntimeV1.create(plan)
    prepared = runtime.state()
    first_rows = runtime.advance_to(0)
    first_anchors = tuple(
        item for item in first_rows if type(item) is DayStateAnchorEmissionV1
    )
    if len(first_anchors) != 1 or first_anchors[0].macro_segment_index != 0:
        failures.append("t=0 macro segment did not emit exactly one anchor")
    after_first = runtime.state()
    if after_first.next_macro_segment_index != 1:
        failures.append("t=0 anchor did not advance the macro cursor exactly once")
    if (
        after_first.day_rng != prepared.day_rng
        or after_first.local_rng != prepared.local_rng
    ):
        failures.append("prepared t=0 anchor consumed duplicate RNG draws")
    if runtime.advance_to(0):
        failures.append("repeated t=0 advance re-emitted the initial macro anchor")
    runtime.advance_to(14)
    before_later = runtime.state()
    later_rows = runtime.advance_to(15)
    later_anchors = tuple(
        item for item in later_rows if type(item) is DayStateAnchorEmissionV1
    )
    if len(later_anchors) != 1 or later_anchors[0].macro_segment_index != 1:
        failures.append("later plan macro segment did not emit exactly one anchor")
    after_later = runtime.state()
    if (
        after_later.day.current_state != DayStateV1.RISK_OFF.value
        or after_later.day.entered_time_us != 15
        or after_later.day.elapsed_age_us != 0
        or after_later.next_macro_segment_index != 2
    ):
        failures.append("later macro anchor did not hard-reset day state/cursor")
    if after_later.day_rng.draw_count != before_later.day_rng.draw_count + 2:
        failures.append("later macro anchor did not resample exactly duration and edge")
    local_before_projection = (
        before_later.local.current_state,
        before_later.local.entered_time_us,
        before_later.local.sampled_duration_us,
        before_later.local.deadline_time_us,
        before_later.local.next_eligible_transition_id,
        before_later.local.trigger_memory,
        before_later.local_rng,
    )
    local_after_projection = (
        after_later.local.current_state,
        after_later.local.entered_time_us,
        after_later.local.sampled_duration_us,
        after_later.local.deadline_time_us,
        after_later.local.next_eligible_transition_id,
        after_later.local.trigger_memory,
        after_later.local_rng,
    )
    if local_before_projection != local_after_projection:
        failures.append("day macro reset perturbed local-state identity or RNG")
    if after_later.local.elapsed_age_us != before_later.local.elapsed_age_us + 1:
        failures.append("local state age did not independently survive the day reset")

    if later_anchors:
        anchor = later_anchors[0]
        payload = project_anchor_payload_v1(anchor, plan=plan)
        segment = plan.macro_regime_schedule[1]
        if (
            anchor.stage is not WorkStageV1.DAY_STATE_TRANSITION
            or anchor.plan_sha256 != canonical_sha256(plan.as_dict())
            or anchor.macro_segment_sha256 != canonical_sha256(segment.as_dict())
            or payload.data["macro_segment_index"] != 1
            or payload.data["anchored_state"] != DayStateV1.RISK_OFF.value
        ):
            failures.append("later anchor lost plan/stage/payload authority")
        anchor_hostile = (
            (
                "anchor with a forged state-model digest",
                replace(anchor, state_model_sha256="f" * 64),
            ),
            (
                "anchor with modifiers removed from its plan definition",
                replace(anchor, state_modifiers=()),
            ),
        )
        for label, forged_anchor in anchor_hostile:
            refusal = _expect_refusal(
                lambda forged_anchor=forged_anchor: project_anchor_payload_v1(
                    forged_anchor, plan=plan
                ),
                label,
            )
            if refusal:
                failures.append(refusal)
    _wo31b_check_runtime_wire(runtime, plan, "post-macro-anchor", failures)

    stale_runtime = HierarchicalStateRuntimeV1.create(plan)
    stale_runtime.advance_to(14)
    stale_observation = _wo31b_trigger_observation(
        plan,
        stale_runtime,
        StateLevelV1.DAY,
        observation_id="AUDIT_STALE_PRE_ANCHOR_DAY_EDGE",
        observation_time_us=15,
    )
    stale_transition_id = stale_observation.transition_id
    try:
        stale_rows = stale_runtime.advance_to(15, (stale_observation,))
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"obsolete pre-anchor day observation aborted anchor: {error}")
        stale_rows = ()
    if not any(type(item) is DayStateAnchorEmissionV1 for item in stale_rows):
        failures.append("obsolete pre-anchor input suppressed the authoritative anchor")
    if any(
        getattr(item, "transition_id", None) == stale_transition_id
        for item in stale_rows
    ):
        failures.append("obsolete pre-anchor day transition was emitted")
    if (
        stale_observation.observation_id
        not in stale_runtime.state().observation_ids_seen
    ):
        failures.append("suppressed obsolete observation was not consumed exactly once")

    cursor_payload = copy.deepcopy(after_later.as_dict())
    cursor_payload["next_macro_segment_index"] = 1
    plan_payload = copy.deepcopy(after_later.as_dict())
    plan_payload["plan_sha256"] = "f" * 64
    for label, payload in (
        ("restored runtime with a backstepped macro cursor", cursor_payload),
        ("restored runtime bound to a forged plan digest", plan_payload),
    ):
        failure = _expect_refusal(
            lambda payload=payload: HierarchicalStateRuntimeV1.from_state(
                plan,
                HierarchicalStateRuntimeStateV1.from_dict(payload),
                verified_component_local_sequence_floor=(
                    after_later.component_local_sequence
                ),
            ),
            label,
        )
        if failure:
            failures.append(failure)
    return FullDayAuditCase(
        "macro_anchor_cursor_and_day_only_reset",
        (
            "anchors=((0,QUIET),(15,RISK_OFF)); t0_duplicate_rng_draws=0; "
            "later_day_rng_draws="
            f"{after_later.day_rng.draw_count - before_later.day_rng.draw_count}; "
            "local_identity_preserved=true; stale_pre_anchor=suppressed; cursor=2"
        ),
        tuple(failures),
    )


def _wo31b_runtime_limits_restore_and_allocator_case() -> FullDayAuditCase:
    """Prove shared allocation, quiescent restore, and hard runtime bounds."""

    from kirby2.full_day.models import MacroRegimeSegmentV1
    from kirby2.full_day.states import (
        DayStateV1,
        DurationExhaustionBehaviorV1,
        DurationLawV1,
        DurationMassV1,
    )
    from kirby2.full_day.transitions import (
        HierarchicalStateRuntimeStateV1,
        HierarchicalStateRuntimeV1,
        StateLevelV1,
        StateTransitionEmissionV1,
        StateTriggerMemoryV1,
    )

    failures: list[str] = []
    refusal_count = 0
    plan = _wo31b_runtime_plan(
        transition_on_exhaustion=True,
        parallel_edges=False,
        include_effects=False,
    )

    shared = HierarchicalStateRuntimeV1.create(
        plan, component_local_sequence=0
    )
    t0_rows = shared.advance_to(0)
    t0_sequences = tuple(item.component_local_sequence for item in t0_rows)
    scheduled_information_sequence = shared.reserve_component_local_sequence()
    next_deadline = min(
        shared.state().day.deadline_time_us,
        shared.state().local.deadline_time_us,
    )
    later_rows = shared.advance_to(next_deadline)
    later_sequences = tuple(item.component_local_sequence for item in later_rows)
    shared_trace = (0, *t0_sequences, scheduled_information_sequence, *later_sequences)
    if (
        t0_sequences != (1,)
        or scheduled_information_sequence != 2
        or not later_sequences
        or later_sequences[0] != 3
        or shared_trace != tuple(sorted(set(shared_trace)))
    ):
        failures.append(
            "shared FULL_DAY_RUNTIME component allocation produced a collision"
        )
    _wo31b_check_runtime_wire(shared, plan, "shared-allocator", failures)

    high_water_runtime = HierarchicalStateRuntimeV1.create(
        plan, component_local_sequence=5
    )
    high_water_anchor = high_water_runtime.advance_to(0)
    reserved_first_runtime = HierarchicalStateRuntimeV1.create(plan)
    reserved_first = reserved_first_runtime.reserve_component_local_sequence()
    reserved_first_anchor = reserved_first_runtime.advance_to(0)
    if (
        tuple(item.component_local_sequence for item in high_water_anchor) != (6,)
        or reserved_first != 1
        or tuple(
            item.component_local_sequence for item in reserved_first_anchor
        )
        != (2,)
    ):
        failures.append(
            "t=0 cloning lost a legitimate shared-owner high-water reservation"
        )

    anchored = HierarchicalStateRuntimeV1.create(plan)
    anchored.advance_to(0)
    allocator_backstep = copy.deepcopy(anchored.state().as_dict())
    allocator_backstep["component_local_sequence"] = 0
    coordinated_allocator = HierarchicalStateRuntimeV1.create(plan)
    coordinated_allocator.advance_to(0)
    coordinated_floor = coordinated_allocator.reserve_component_local_sequence()
    coordinated_backstep = copy.deepcopy(
        coordinated_allocator.state().as_dict()
    )
    coordinated_backstep["component_local_sequence"] -= 1
    coordinated_backstep["component_sequence_offset"] -= 1

    trigger_plan = _wo31b_runtime_plan(
        transition_on_exhaustion=False,
        parallel_edges=False,
        include_effects=False,
    )
    unclosed = HierarchicalStateRuntimeV1.create(trigger_plan)
    unclosed.advance_to(4)
    unclosed_payload = copy.deepcopy(unclosed.state().as_dict())
    unclosed_payload["input_closed_through_time_us"] = None
    unclosed_payload["next_macro_segment_index"] = 0

    due = HierarchicalStateRuntimeV1.create(plan)
    due_time = min(
        due.state().day.deadline_time_us,
        due.state().local.deadline_time_us,
    )
    due.advance_to(due_time - 1)
    due_payload = copy.deepcopy(due.state().as_dict())
    due_payload["current_time_us"] = due_time
    due_payload["input_closed_through_time_us"] = due_time
    for level_name in ("day", "local"):
        row = due_payload[level_name]
        row["as_of_time_us"] = due_time
        row["elapsed_age_us"] = due_time - row["entered_time_us"]

    trigger_due = HierarchicalStateRuntimeV1.create(trigger_plan)
    trigger_due.advance_to(4)
    trigger_observation = _wo31b_trigger_observation(
        trigger_plan,
        trigger_due,
        StateLevelV1.DAY,
        observation_id="AUDIT_RESTORED_DUE_TRIGGER",
        observation_time_us=5,
    )
    trigger_due_payload = copy.deepcopy(trigger_due.state().as_dict())
    trigger_due_payload["current_time_us"] = 5
    trigger_due_payload["input_closed_through_time_us"] = 5
    for level_name in ("day", "local"):
        row = trigger_due_payload[level_name]
        row["as_of_time_us"] = 5
        row["elapsed_age_us"] = 5 - row["entered_time_us"]
    trigger_due_payload["observation_ids_seen"] = [
        trigger_observation.observation_id
    ]
    trigger_due_payload["day"]["trigger_memory"] = [
        StateTriggerMemoryV1(
            trigger_due.state().day.current_state,
            trigger_due.state().day.entered_time_us,
            trigger_observation,
        ).as_dict()
    ]

    horizon_payload = copy.deepcopy(unclosed.state().as_dict())
    beyond_horizon = trigger_plan.calendar.end_time_us + 1
    horizon_payload["current_time_us"] = beyond_horizon
    horizon_payload["input_closed_through_time_us"] = beyond_horizon
    for level_name in ("day", "local"):
        row = horizon_payload[level_name]
        row["as_of_time_us"] = beyond_horizon
        row["elapsed_age_us"] = beyond_horizon - row["entered_time_us"]

    macro_base = _wo31b_runtime_plan(
        transition_on_exhaustion=False,
        parallel_edges=False,
        include_effects=False,
    )
    macro_plan = replace(
        macro_base,
        macro_regime_schedule=(
            MacroRegimeSegmentV1(0, 15, DayStateV1.QUIET),
            MacroRegimeSegmentV1(
                15,
                macro_base.calendar.end_time_us,
                DayStateV1.RISK_OFF,
            ),
        ),
    )
    macro_runtime = HierarchicalStateRuntimeV1.create(macro_plan)
    macro_runtime.advance_to(15)
    forged_macro = copy.deepcopy(macro_runtime.state().as_dict())
    quiet_definition = next(
        item
        for item in macro_plan.state_model.day_definitions
        if item.state is DayStateV1.QUIET
    )
    quiet_duration = quiet_definition.duration_law.masses[0].duration_us
    quiet_transition = quiet_definition.transitions[0]
    forged_macro["day"].update(
        {
            "current_state": DayStateV1.QUIET.value,
            "deadline_time_us": 15 + quiet_duration,
            "elapsed_age_us": 0,
            "entered_time_us": 15,
            "next_eligible_transition_id": quiet_transition.transition_id,
            "next_eligible_transition_time_us": (
                15 + quiet_transition.minimum_age_us
            ),
            "sampled_duration_us": quiet_duration,
            "trigger_memory": [],
        }
    )
    forged_macro_counters = copy.deepcopy(forged_macro)
    forged_macro_counters["day_transitions_since_macro_anchor"] = 1
    forged_macro_counters["day_transition_count"] += 1
    forged_macro_counters["runtime_emission_count"] += 1
    forged_macro_counters["component_local_sequence"] += 1

    restore_probes: tuple[
        tuple[str, dict[str, object], object, int], ...
    ] = (
        (
            "backstepped shared component allocator",
            allocator_backstep,
            plan,
            anchored.state().component_local_sequence,
        ),
        (
            "coordinated shared component allocator rollback",
            coordinated_backstep,
            plan,
            coordinated_floor,
        ),
        (
            "non-pristine unclosed lifecycle state",
            unclosed_payload,
            trigger_plan,
            unclosed.state().component_local_sequence,
        ),
        (
            "closed exhaustion transition left due",
            due_payload,
            plan,
            due.state().component_local_sequence,
        ),
        (
            "closed triggered transition left due",
            trigger_due_payload,
            trigger_plan,
            trigger_due.state().component_local_sequence,
        ),
        (
            "runtime state beyond the plan horizon",
            horizon_payload,
            trigger_plan,
            unclosed.state().component_local_sequence,
        ),
        (
            "day state inconsistent with latest macro authority",
            forged_macro,
            macro_plan,
            macro_runtime.state().component_local_sequence,
        ),
        (
            "invented macro transition with coordinated counters but no RNG samples",
            forged_macro_counters,
            macro_plan,
            macro_runtime.state().component_local_sequence + 1,
        ),
    )
    for label, payload, restore_plan, verified_floor in restore_probes:
        refusal = _expect_refusal(
            lambda payload=payload, restore_plan=restore_plan, verified_floor=verified_floor: (
                HierarchicalStateRuntimeV1.from_state(
                    restore_plan,
                    HierarchicalStateRuntimeStateV1.from_dict(payload),
                    verified_component_local_sequence_floor=verified_floor,
                )
            ),
            label,
        )
        refusal_count += 1
        if refusal:
            failures.append(refusal)

    horizon_runtime = HierarchicalStateRuntimeV1.create(trigger_plan)
    horizon_before = horizon_runtime.state().canonical_bytes()
    refusal = _expect_refusal(
        lambda: horizon_runtime.advance_to(beyond_horizon),
        "advance beyond the plan horizon",
    )
    refusal_count += 1
    if refusal:
        failures.append(refusal)
    if horizon_runtime.state().canonical_bytes() != horizon_before:
        failures.append("horizon refusal was not atomic")

    zero_law = DurationLawV1(0, 0, (DurationMassV1(0, 1),))
    day_definitions = list(plan.state_model.day_definitions)
    for index in (0, 1):
        definition = day_definitions[index]
        forced_edge = replace(
            definition.transitions[0],
            minimum_age_us=0,
            duration_exhaustion_behavior=(
                DurationExhaustionBehaviorV1.TRANSITION_ON_EXHAUSTION
            ),
        )
        day_definitions[index] = replace(
            definition,
            duration_law=zero_law,
            transitions=(forced_edge,),
        )
    microstep_plan = replace(
        plan,
        state_model=replace(
            plan.state_model,
            day_definitions=tuple(day_definitions),
        ),
        deterministic_limits=replace(
            plan.deterministic_limits,
            maximum_microsteps_per_timestamp=2,
        ),
    )
    microstep_runtime = HierarchicalStateRuntimeV1.create(microstep_plan)
    microstep_before = microstep_runtime.state().canonical_bytes()
    refusal = _expect_refusal(
        lambda: microstep_runtime.advance_to(0),
        "zero-duration chain at the forbidden microstep index",
    )
    refusal_count += 1
    if refusal:
        failures.append(refusal)
    if microstep_runtime.state().canonical_bytes() != microstep_before:
        failures.append("microstep-limit refusal was not atomic")

    return FullDayAuditCase(
        "runtime_allocator_limits_and_restore_refusals",
        (
            f"shared_component_sequences={shared_trace}; "
            "initial_high_water=(5->6) reserved_first=(1->2); "
            f"hostile_refusals={refusal_count}; horizon={beyond_horizon - 1}; "
            "closed_cut_due_work=0; atomic_limit_refusals=true"
        ),
        tuple(failures),
    )


def _wo31b_modifier_and_price_boundary_case() -> FullDayAuditCase:
    from kirby2.full_day.transitions import (
        FixedPointValueV1,
        HierarchicalStateRuntimeV1,
        StateTransitionEmissionV1,
        apply_bounded_modifiers_v1,
        project_transition_payload_v1,
    )
    from kirby2.full_day.states import ParameterTargetV1

    failures: list[str] = []
    plan = _wo31b_runtime_plan(include_effects=True)
    runtime = HierarchicalStateRuntimeV1.create(plan)
    transitions = tuple(
        item
        for item in runtime.advance_to(12)
        if type(item) is StateTransitionEmissionV1
    )
    if not transitions:
        return FullDayAuditCase(
            "bounded_modifier_consumers_and_exchange_price_boundary",
            "no transition emission was available for modifier projection",
            ("duration-aware runtime emitted no transition by t=12",),
        )
    emission = transitions[0]
    modifiers = (*emission.state_modifiers, *emission.transition_modifiers)
    base_values = {
        ParameterTargetV1.MARKET_BUY_INTENSITY: FixedPointValueV1(10, 1),
        ParameterTargetV1.ORDER_SIZE_SCALE: FixedPointValueV1(100, 1),
        ParameterTargetV1.PARTICIPANT_ACTIVITY_SCALE: FixedPointValueV1(80, 1),
    }
    consumed = apply_bounded_modifiers_v1(base_values, modifiers)
    expected = {
        ParameterTargetV1.MARKET_BUY_INTENSITY: FixedPointValueV1(20, 1),
        ParameterTargetV1.ORDER_SIZE_SCALE: FixedPointValueV1(150, 1),
        ParameterTargetV1.PARTICIPANT_ACTIVITY_SCALE: FixedPointValueV1(60, 1),
    }
    if dict(consumed) != expected:
        failures.append("bounded mock consumers did not receive exact fixed-point values")
    if runtime.active_modifiers(emission.level) != emission.state_modifiers:
        failures.append("emitted state modifiers differ from the active state projection")
    for modifier in modifiers:
        if (
            modifier.minimum.numerator * modifier.modifier.denominator
            > modifier.modifier.numerator * modifier.minimum.denominator
            or modifier.modifier.numerator * modifier.maximum.denominator
            > modifier.maximum.numerator * modifier.modifier.denominator
        ):
            failures.append(f"modifier {modifier.target.value} escaped exact bounds")
    payload = project_transition_payload_v1(emission, plan=plan)
    projection_probes = (
        (
            "transition emission projected through a different full-day plan",
            emission,
            _wo31b_runtime_plan(root_seed=43, include_effects=True),
        ),
        (
            "transition emission with forged successor-state modifiers",
            replace(emission, state_modifiers=()),
            plan,
        ),
        (
            "transition emission with forged edge modifiers",
            replace(emission, transition_modifiers=()),
            plan,
        ),
        (
            "transition emission beyond the full-day plan horizon",
            replace(
                emission,
                simulation_time_us=plan.calendar.end_time_us + 1,
            ),
            plan,
        ),
    )
    for label, forged_emission, projection_plan in projection_probes:
        refusal = _expect_refusal(
            lambda forged_emission=forged_emission, projection_plan=projection_plan: (
                project_transition_payload_v1(
                    forged_emission,
                    plan=projection_plan,
                )
            ),
            label,
        )
        if refusal:
            failures.append(refusal)

    forbidden_keys = {
        "book_mutation",
        "desired_return",
        "forced_close",
        "forced_order",
        "forced_trade",
        "order_command",
        "price_ticks",
        "target_price",
    }
    observed_keys: set[str] = set()
    observed_values: set[str] = set()

    def inspect_output(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                observed_keys.add(str(key).lower())
                inspect_output(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                inspect_output(item)
        elif isinstance(value, str):
            observed_values.add(value.lower())

    inspect_output(emission.as_dict())
    inspect_output(payload.as_dict())
    inspect_output(
        {
            target.value: value.as_dict()
            for target, value in consumed.items()
        }
    )
    forbidden_present = forbidden_keys.intersection(observed_keys | observed_values)
    if forbidden_present:
        failures.append(
            "state output contains imperative/price keys: "
            + ",".join(sorted(forbidden_present))
        )

    engine = _continuous_engine()
    engine.submit(_limit("WO31B-BID", Side.BUY, 10, 99))
    engine.submit(_limit("WO31B-ASK-1", Side.SELL, 10, 101))
    engine.submit(_limit("WO31B-ASK-2", Side.SELL, 10, 102))
    before_runtime_digest = engine.book.state_sha256()
    before_best_ask = engine.book.best_ask
    # Consuming state outputs is intentionally disconnected from venue truth.
    apply_bounded_modifiers_v1(base_values, modifiers)
    if engine.book.state_sha256() != before_runtime_digest:
        failures.append("state transition/modifier consumption mutated the order book")
    engine.submit(
        AdvancedOrderRequest(
            "WO31B-MARKET-BUY",
            Side.BUY,
            10,
            OrderInstruction.MARKET,
            OrderOwner.SIMULATED,
            "ACCOUNT-WO31B-MARKET-BUY",
        )
    )
    if (
        before_best_ask != 101
        or engine.book.best_ask != 102
        or engine.last_trade_price_ticks != 101
    ):
        failures.append("ordinary exchange activity did not produce the price boundary")
    try:
        engine.assert_invariants()
    except RuntimeError as error:
        failures.append(f"exchange-only price boundary broke invariants: {error}")
    return FullDayAuditCase(
        "bounded_modifier_consumers_and_exchange_price_boundary",
        (
            "mock_outputs={MARKET_BUY_INTENSITY:20/1,ORDER_SIZE_SCALE:150/1,"
            "PARTICIPANT_ACTIVITY_SCALE:60/1}; forbidden_output_keys=0; "
            "projection_refusals=4; state_book_mutations=0; "
            "exchange_best_ask=101->102; last_trade=101"
        ),
        tuple(failures),
    )


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
        "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1": set(
            _dev0003_state_checkpoint_fields()
        ),
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
            "legacy aggregate completed-transition counter in state runtime",
            lambda: replace(inventory, items=legacy_transition_counter_items),
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
    try:
        checkpoint = engine.checkpoint_state()
        restored = MarketMechanicsEngine.from_checkpoint_state(checkpoint)
        if restored.canonical_state_bytes() != engine.canonical_state_bytes():
            failures.append("completed boundary checkpoint did not round-trip exactly")
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"completed boundary checkpoint failure: {error}")

    transient = _continuous_engine()
    transient.submit(
        _limit(
            "BOUNDARY-TRANSIENT-GTT",
            Side.BUY,
            10,
            96,
            time_in_force=OrderInstruction.GOOD_UNTIL_TIME,
            good_until_time_us=200,
        )
    )
    _apply_native_boundary(
        transient,
        100,
        SessionState.CLOSING_AUCTION,
        uncross_before=False,
        reason="WO31_A_TRANSIENT_CLOSING_CALL",
    )
    transient.clock.advance_to(200)
    transient.uncross_auction()
    transient.transition_session(
        SessionState.POSTCLOSE,
        reason="WO31_A_TRANSIENT_POSTCLOSE",
    )
    refusal = _expect_refusal(
        transient.checkpoint_state,
        "boundary checkpoint before exact-time GTT completion",
    )
    if refusal:
        failures.append(refusal)
    transient.advance_to(200)
    try:
        transient.checkpoint_state()
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"completed transient boundary checkpoint failure: {error}")

    scheduled = MarketMechanicsEngine(
        InstrumentRules(
            session_schedule=SessionSchedule(
                (ScheduledSessionState(200, SessionState.POSTCLOSE),)
            )
        )
    )
    for state in (
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
    ):
        scheduled.transition_session(state, reason="WO31_A_SCHEDULE_DEFERRAL_PROBE")
    scheduled.uncross_auction()
    scheduled.transition_session(
        SessionState.CONTINUOUS,
        reason="WO31_A_SCHEDULE_DEFERRAL_PROBE",
    )
    scheduled.transition_session(
        SessionState.CLOSING_AUCTION,
        reason="WO31_A_SCHEDULE_DEFERRAL_PROBE",
    )
    scheduled.clock.advance_to(200)
    refusal = _expect_refusal(
        scheduled.uncross_auction,
        "configured engine-owned transition deferral",
    )
    if refusal:
        failures.append(refusal)

    overdue = _continuous_engine()
    overdue.submit(
        _limit(
            "BOUNDARY-OVERDUE-GTT",
            Side.BUY,
            10,
            96,
            time_in_force=OrderInstruction.GOOD_UNTIL_TIME,
            good_until_time_us=150,
        )
    )
    _apply_native_boundary(
        overdue,
        100,
        SessionState.CLOSING_AUCTION,
        uncross_before=False,
        reason="WO31_A_OVERDUE_CLOSING_CALL",
    )
    overdue.clock.advance_to(200)
    refusal = _expect_refusal(
        overdue.uncross_auction,
        "overdue GTT deferral",
    )
    if refusal:
        failures.append(refusal)

    halt_failures, resume_trace = _halt_resume_boundary_probe()
    failures.extend(halt_failures)
    return FullDayAuditCase(
        "atomic_market_mechanics_boundary_trace",
        (
            "uncross, transition-owned expirations, session/HALT/RESUME, and "
            "same-time GTT expiry retain native order; incomplete exact-time GTT "
            "work is refused by strict checkpoints; configured schedules and overdue "
            "GTT work cannot defer; "
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


def _wo31c_data_paths_case() -> FullDayAuditCase:
    """Prove data-root discovery is non-writing and every area stays contained."""

    from pathlib import Path
    from tempfile import TemporaryDirectory

    from kirby2.research.paths import (
        DataAreaId,
        DataPaths,
        _open_or_create_directory_at,
        _safe_directory_open_flags,
    )

    failures: list[str] = []
    hostile_refusals = 0
    with TemporaryDirectory(prefix="kirby2-wo31c-paths-") as temporary:
        sandbox = Path(temporary).resolve()

        untouched_root = (sandbox / "untouched").resolve()
        untouched = DataPaths(untouched_root)
        untouched.validate()
        if untouched_root.exists():
            failures.append("DataPaths construction or validation created its root")
        if untouched.ensure(()) != () or untouched_root.exists():
            failures.append("empty DataPaths.ensure request wrote filesystem state")

        requested = untouched.ensure((DataAreaId.CACHE, DataAreaId.CHECKPOINTS))
        expected_requested = (untouched.checkpoints, untouched.cache)
        if requested != expected_requested:
            failures.append("selective DataPaths.ensure order is not declaration-stable")
        actual_children = tuple(sorted(path.name for path in untouched_root.iterdir()))
        if actual_children != ("cache", "checkpoints"):
            failures.append(
                "selective DataPaths.ensure created an unrequested governed area"
            )
        if any(
            untouched.area(area_id).exists()
            for area_id in DataAreaId
            if area_id not in {DataAreaId.CACHE, DataAreaId.CHECKPOINTS}
        ):
            failures.append("an unrequested DataPaths area exists after selective ensure")

        hostile_constructors: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "relative data root",
                lambda: DataPaths("relative/kirby2"),
            ),
            (
                "unresolved data root",
                lambda: DataPaths(sandbox / "missing" / ".." / "unresolved"),
            ),
            (
                "absolute area child",
                lambda: DataPaths(
                    (sandbox / "absolute-child-root").resolve(),
                    area_children={
                        DataAreaId.CHECKPOINTS: str(
                            (sandbox / "absolute-child").resolve()
                        )
                    },
                ),
            ),
            (
                "parent traversal area child",
                lambda: DataPaths(
                    (sandbox / "traversal-root").resolve(),
                    area_children={DataAreaId.CHECKPOINTS: "../outside"},
                ),
            ),
            (
                "exact area alias",
                lambda: DataPaths(
                    (sandbox / "exact-alias-root").resolve(),
                    area_children={DataAreaId.EVIDENCE: "runs"},
                ),
            ),
            (
                "area containment alias",
                lambda: DataPaths(
                    (sandbox / "containment-root").resolve(),
                    area_children={DataAreaId.EVIDENCE: "runs/child"},
                ),
            ),
            (
                "case-folded area alias",
                lambda: DataPaths(
                    (sandbox / "case-alias-root").resolve(),
                    area_children={
                        DataAreaId.RUNS: "CaseSensitive",
                        DataAreaId.EVIDENCE: "casesensitive",
                    },
                ),
            ),
            (
                "Unicode-normalized area alias",
                lambda: DataPaths(
                    (sandbox / "unicode-alias-root").resolve(),
                    area_children={
                        DataAreaId.RUNS: "\u00e9vidence",
                        DataAreaId.EVIDENCE: "e\u0301vidence",
                    },
                ),
            ),
            (
                "duplicate selective ensure ID",
                lambda: untouched.ensure(
                    (DataAreaId.CACHE, DataAreaId.CACHE)
                ),
            ),
        )
        for label, operation in hostile_constructors:
            refusal = _expect_refusal(operation, label)
            if refusal:
                failures.append(refusal)
            else:
                hostile_refusals += 1

        file_root = (sandbox / "file-root").resolve()
        file_root.mkdir()
        (file_root / "checkpoints").write_bytes(b"not-a-directory")
        refusal = _expect_refusal(
            lambda: DataPaths(file_root),
            "file occupying a governed directory",
        )
        if refusal:
            failures.append(refusal)
        else:
            hostile_refusals += 1

        rebound_root = (sandbox / "rebound-root").resolve()
        rebound = DataPaths(rebound_root)
        rebound_root.mkdir()
        rebound_target = (sandbox / "rebound-target").resolve()
        rebound_target.mkdir()
        os.symlink(rebound_target, rebound_root / "checkpoints")
        refusal = _expect_refusal(
            lambda: rebound.validate((DataAreaId.CHECKPOINTS,)),
            "post-construction symlink rebind",
        )
        if refusal:
            failures.append(refusal)
        else:
            hostile_refusals += 1

        # Reproduce the write-boundary interleaving directly: pin the governed
        # root, move its pathname aside, and replace that pathname with a symlink
        # before the selected child is created.  The fd-relative primitive must
        # create beneath the pinned original directory, never beneath the external
        # target, and the subsequent pathname validation must refuse the rebind.
        pinned_root = (sandbox / "pinned-root").resolve()
        pinned_parked = (sandbox / "pinned-root-parked").resolve()
        pinned_external = (sandbox / "pinned-root-external").resolve()
        pinned_paths = DataPaths(pinned_root)
        pinned_root.mkdir()
        pinned_external.mkdir()
        open_flags = _safe_directory_open_flags()
        pinned_fd = os.open(pinned_root, open_flags)
        child_fd: int | None = None
        try:
            pinned_root.rename(pinned_parked)
            os.symlink(pinned_external, pinned_root)
            child_fd = _open_or_create_directory_at(
                pinned_fd,
                DataAreaId.RUNS.value,
                open_flags=open_flags,
                display_path=pinned_root / DataAreaId.RUNS.value,
            )
        finally:
            if child_fd is not None:
                os.close(child_fd)
            os.close(pinned_fd)
        if (pinned_external / DataAreaId.RUNS.value).exists():
            failures.append("fd-relative ensure primitive wrote through rebound root")
        if not (pinned_parked / DataAreaId.RUNS.value).is_dir():
            failures.append("fd-relative ensure primitive lost its pinned root")
        refusal = _expect_refusal(
            lambda: pinned_paths.validate((DataAreaId.RUNS,)),
            "descriptor-pinned root pathname rebind",
        )
        if refusal:
            failures.append(refusal)
        else:
            hostile_refusals += 1

        portable_root = (sandbox / "portable-sibling-root").resolve()
        portable_root.mkdir()
        (portable_root / "CHECKPOINTS").mkdir()
        refusal = _expect_refusal(
            lambda: DataPaths(portable_root),
            "existing case-colliding governed sibling",
        )
        if refusal:
            failures.append(refusal)
        else:
            hostile_refusals += 1

    return FullDayAuditCase(
        "portable_data_paths_governance",
        (
            f"area_count={len(DataAreaId)} construction_writes=0 "
            "selective_ensure=('checkpoints','cache') "
            f"hostile_refusals={hostile_refusals}"
        ),
        tuple(failures),
    )


def _wo31c_genesis_checkpoint_prefix(plan=None):
    """Return a validated t=0 boundary/anchor/checkpoint outer-event prefix."""

    from kirby2.full_day.checkpoint_contract import QuiescentCutV1
    from kirby2.full_day.events import (
        FullDayEventPayloadV1,
        FullDayEventTypeV1,
        FullDayEventV1,
        ScheduledWorkKeyV1,
        WorkStageV1,
        canonical_event_prefix_sha256,
        validate_full_day_event_stream,
    )
    from kirby2.full_day.transitions import HierarchicalStateRuntimeV1

    if plan is None:
        plan = _sample_plan()
    runtime_id = "FULL_DAY_RUNTIME_V1"

    def work(stage: WorkStageV1, local_sequence: int) -> ScheduledWorkKeyV1:
        return ScheduledWorkKeyV1(
            simulation_time_us=0,
            microstep=0,
            stage_ordinal=stage,
            source_component_id=runtime_id,
            component_local_sequence=local_sequence,
        )

    def event(
        event_type: FullDayEventTypeV1,
        item: ScheduledWorkKeyV1,
        sequence: int,
        data: dict[str, object],
    ) -> FullDayEventV1:
        return FullDayEventV1(
            schema_version=1,
            global_event_sequence=sequence,
            simulation_time_us=0,
            microstep=0,
            stage=item.stage_ordinal,
            source_component_id=runtime_id,
            component_local_sequence=item.component_local_sequence,
            event_type=event_type,
            causal_parent_ids=(item.work_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=event_type.value,
                payload_version=1,
                native_event=None,
                data=data,
            ),
        )

    runtime = HierarchicalStateRuntimeV1.create(plan)
    boundary_work = work(
        WorkStageV1.ATOMIC_CALENDAR_BOUNDARY,
        runtime.reserve_component_local_sequence(),
    )
    anchor_emissions = runtime.advance_to(0)
    if len(anchor_emissions) != 1:
        raise RuntimeError("genesis fixture requires exactly one runtime anchor")
    anchor_emission = anchor_emissions[0]
    if anchor_emission.event_type is not FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET:
        raise RuntimeError("genesis runtime emitted the wrong state event")
    anchor_work = anchor_emission.scheduled_work_key
    checkpoint_work = work(
        WorkStageV1.CHECKPOINT_CAPTURE,
        runtime.reserve_component_local_sequence(),
    )
    pending_local_sequence = runtime.reserve_component_local_sequence()
    runtime_snapshot = runtime.state()
    operation = plan.calendar.boundary_operations[0]
    pending_state_times = tuple(
        max(
            level.deadline_time_us,
            (
                level.deadline_time_us
                if level.next_eligible_transition_time_us is None
                else level.next_eligible_transition_time_us
            ),
        )
        for level in (runtime_snapshot.day, runtime_snapshot.local)
    )
    next_pending_time_us = min(pending_state_times)
    events = (
        event(
            FullDayEventTypeV1.CALENDAR_BOUNDARY,
            boundary_work,
            1,
            {
                "boundary_operation_index": 0,
                "destination_session_state": (
                    operation.destination_session_state.value
                ),
                "uncross_before": operation.uncross_before,
            },
        ),
        event(
            FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET,
            anchor_work,
            2,
            {
                "anchored_state": anchor_emission.anchored_state.value,
                "entered_time_us": anchor_emission.simulation_time_us,
                "macro_segment_index": anchor_emission.macro_segment_index,
                "macro_segment_sha256": anchor_emission.macro_segment_sha256,
                "previous_state": anchor_emission.previous_state.value,
                "sampled_duration_us": anchor_emission.sampled_duration_us,
            },
        ),
        event(
            FullDayEventTypeV1.CHECKPOINT_CAPTURE_MARKER,
            checkpoint_work,
            3,
            {"checkpoint_request_id": "WO31_C_GENESIS_CHECKPOINT_V1"},
        ),
    )
    executed = {
        item.work_id: item
        for item in (boundary_work, anchor_work, checkpoint_work)
    }
    validate_full_day_event_stream(
        events,
        executed_work_items=executed,
        native_event_ledger={},
        scheduled_event_ledger={},
        full_day_plan=plan,
    )
    cut = QuiescentCutV1(
        schema_version=1,
        simulation_time_us=0,
        microstep=0,
        checkpoint_stage_ordinal=int(WorkStageV1.CHECKPOINT_CAPTURE),
        last_global_event_sequence=3,
        event_prefix_last_global_sequence=3,
        event_prefix_sha256=canonical_event_prefix_sha256(events),
        pending_work_count=1,
        next_pending_time_us=next_pending_time_us,
        next_pending_microstep=0,
        due_work_at_or_before_cut=0,
        generated_microsteps_complete=True,
        checkpoint_stage_complete=True,
        boundary_complete_at_cut=True,
    )
    pending_work = ScheduledWorkKeyV1(
        simulation_time_us=next_pending_time_us,
        microstep=0,
        stage_ordinal=WorkStageV1.DAY_STATE_TRANSITION,
        source_component_id=runtime_id,
        component_local_sequence=pending_local_sequence,
    )
    return plan, events, cut, runtime_snapshot, pending_work


def _wo31c_runtime_flat_state(runtime_snapshot) -> dict[str, object]:
    """Project the authoritative state-runtime snapshot into frozen owned fields."""

    day = runtime_snapshot.day
    local = runtime_snapshot.local
    return {
        "state.component_local_sequence": runtime_snapshot.component_local_sequence,
        "state.component_sequence_offset": runtime_snapshot.component_sequence_offset,
        "state.current_day": day.current_state,
        "state.current_local": local.current_state,
        "state.day_elapsed_age_us": day.elapsed_age_us,
        "state.day_entered_time_us": day.entered_time_us,
        "state.day_next_eligible_transition_id": (
            day.next_eligible_transition_id
        ),
        "state.day_next_eligible_transition_time_us": (
            day.next_eligible_transition_time_us
        ),
        "state.day_sampled_deadline_us": day.deadline_time_us,
        "state.day_sampled_duration_us": day.sampled_duration_us,
        "state.day_transition_count": runtime_snapshot.day_transition_count,
        "state.day_transitions_since_macro_anchor": (
            runtime_snapshot.day_transitions_since_macro_anchor
        ),
        "state.day_trigger_memory": [
            memory.as_dict() for memory in day.trigger_memory
        ],
        "state.input_closed_through_time_us": (
            runtime_snapshot.input_closed_through_time_us
        ),
        "state.local_elapsed_age_us": local.elapsed_age_us,
        "state.local_entered_time_us": local.entered_time_us,
        "state.local_next_eligible_transition_id": (
            local.next_eligible_transition_id
        ),
        "state.local_next_eligible_transition_time_us": (
            local.next_eligible_transition_time_us
        ),
        "state.local_sampled_deadline_us": local.deadline_time_us,
        "state.local_sampled_duration_us": local.sampled_duration_us,
        "state.local_transition_count": runtime_snapshot.local_transition_count,
        "state.local_trigger_memory": [
            memory.as_dict() for memory in local.trigger_memory
        ],
        "state.next_macro_segment_index": runtime_snapshot.next_macro_segment_index,
        "state.observation_ids_seen": list(runtime_snapshot.observation_ids_seen),
        "state.plan_sha256": runtime_snapshot.plan_sha256,
        "state.runtime_emission_count": runtime_snapshot.runtime_emission_count,
        "state.state_model_sha256": runtime_snapshot.state_model_sha256,
    }


def _wo31c_preserved_state(
    item,
    *,
    plan,
    expectation,
    cut,
    runtime_snapshot,
    pending_work,
    event_prefix,
) -> dict[str, object]:
    """Build strict, complete audit state for one frozen inventory row."""

    from random import Random

    from kirby2.full_day.checkpoints import (
        OWNED_PRNG_CODEC_REGISTRY_ID,
        OwnedPrngStateV1,
    )
    from kirby2.full_day.models import RNG_LABEL_PREFIXES_BY_COMPONENT_V1
    from kirby2.full_day.states import (
        DAY_STATE_RNG_SUBSTREAM_PATH_V1,
        LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
    )

    state: dict[str, object] = {
        field: {} for field in item.owned_state_fields
    }
    transition_rng_by_substream = {
        runtime_snapshot.day_rng.substream_label: runtime_snapshot.day_rng,
        runtime_snapshot.local_rng.substream_label: runtime_snapshot.local_rng,
    }
    active_owner_ids = {
        expectation.state_owner_ids[component_id]
        for component_id in expectation.active_component_ids
    }
    rng_records: list[dict[str, object]] = []
    rng_owner_by_substream: dict[str, str] = {}
    for declaration in plan.seed_policy.substreams:
        owners = tuple(
            component_id
            for component_id, prefixes in RNG_LABEL_PREFIXES_BY_COMPONENT_V1.items()
            if any(
                declaration.semantic_path == prefix
                or declaration.semantic_path.startswith(prefix + "/")
                for prefix in prefixes
            )
        )
        if len(owners) != 1:
            raise RuntimeError("audit PRNG substream has ambiguous frozen ownership")
        if owners[0] not in active_owner_ids:
            continue
        rng_owner_by_substream[declaration.semantic_path] = owners[0]
        if declaration.semantic_path in {
            DAY_STATE_RNG_SUBSTREAM_PATH_V1,
            LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
        }:
            transition_rng = transition_rng_by_substream[declaration.semantic_path]
            owned_state = OwnedPrngStateV1.splitmix64(
                substream_id=transition_rng.substream_label,
                initial_seed=transition_rng.initial_seed,
                state_u64=transition_rng.state_u64,
                draw_count=transition_rng.draw_count,
                sample_count=transition_rng.sample_count,
            )
        else:
            generator = Random(declaration.derived_seed)
            random_state = generator.getstate()[1]
            owned_state = OwnedPrngStateV1.cpython_mt19937(
                substream_id=declaration.semantic_path,
                initial_seed=declaration.derived_seed,
                state_words=tuple(random_state[:-1]),
                state_index=random_state[-1],
            )
        rng_records.append(owned_state.as_dict())
    rng_records.sort(key=lambda record: record["substream_id"])

    marker_events = tuple(
        event
        for event in event_prefix
        if event.event_type.value == "CHECKPOINT_CAPTURE_MARKER"
    )
    request_ids = [
        event.payload.data["checkpoint_request_id"] for event in marker_events
    ]
    coincident_request_ids = [
        event.payload.data["checkpoint_request_id"]
        for event in marker_events
        if event.chronological_key
        == (
            cut.simulation_time_us,
            cut.microstep,
            cut.checkpoint_stage_ordinal,
        )
    ]
    next_checkpoint_time_us = next(
        (
            time_us
            for time_us in plan.resolved_checkpoint_times_us
            if time_us > cut.simulation_time_us
        ),
        None,
    )
    participant_specifications = {
        participant.participant_id: participant.specification.as_dict()
        for participant in sorted(
            plan.participant_definitions,
            key=lambda definition: definition.participant_id,
        )
    }
    participant_generations = {
        participant.participant_id: 0
        for participant in sorted(
            plan.participant_definitions,
            key=lambda definition: definition.participant_id,
        )
    }

    scalar_overrides: dict[str, object] = {
        "calendar.boundary_operation_index": 1,
        "calendar.current_phase_id": (
            plan.calendar.boundary_operations[0].destination_session_state.value
        ),
        "calendar.next_boundary_time_us": (
            plan.calendar.boundary_operations[1].boundary.simulation_time_us
        ),
        "checkpoint.capture_policy_state": {
            "checkpoint_policy": plan.checkpoint_policy.as_dict(),
            "resolved_checkpoint_times_us": list(
                plan.resolved_checkpoint_times_us
            ),
        },
        "checkpoint.coincident_request_state": {
            "capture_time_us": cut.simulation_time_us,
            "request_ids": coincident_request_ids,
        },
        "checkpoint.completed_count": 1,
        "checkpoint.next_time_us": next_checkpoint_time_us,
        "checkpoint.pending_request_state": {
            "pending_work_ids": sorted(
                item.work_id
                for item in (pending_work,)
                if int(item.stage_ordinal) == cut.checkpoint_stage_ordinal
            ),
        },
        "checkpoint.sequence_allocator_state": {
            "allocated_request_ids": request_ids,
            "next_sequence": len(request_ids) + 1,
        },
        "full_day.current_time_us": cut.simulation_time_us,
        "global_event_allocator.next_sequence": (
            cut.last_global_event_sequence + 1
        ),
        "ledger.event_prefix_last_global_sequence": (
            cut.event_prefix_last_global_sequence
        ),
        "ledger.event_prefix_sha256": cut.event_prefix_sha256,
        "observable.last_published_global_sequence": 0,
        "observable.client_publication_cursor": 0,
        "observable.publication_time_us": cut.simulation_time_us,
        "participant_schedule.next_index": 0,
        "participant_schedule.replacement_generation": participant_generations,
        "participant_schedule.spec_version_bindings": participant_specifications,
        "plan.composition_matrix_sha256": expectation.composition_matrix_sha256,
        "plan.composition_profile_id": expectation.composition_profile_id,
        "plan.composition_profile_version": expectation.composition_profile_version,
        "plan.semantic_sha256": plan.semantic_sha256,
        "runtime.derived_seed_registry": [
            declaration.as_dict()
            for declaration in plan.seed_policy.substreams
        ],
        "runtime.non_state_component_local_event_sequence_allocators": {},
        "runtime.rng_algorithm_codec_version": (
            OWNED_PRNG_CODEC_REGISTRY_ID
        ),
        "runtime.rng_states": rng_records,
        "runtime.root_seed": plan.seed_policy.root_seed,
        "runtime.substream_policy_version": plan.seed_policy.policy_version,
        "scheduled_event.next_index": 0,
        "scheduled_event.state": {},
        "scheduled_work.dequeued_count": 0,
        "scheduled_work.pending_heap": [pending_work.as_dict()],
    }
    scalar_overrides.update(_wo31c_runtime_flat_state(runtime_snapshot))
    for field in tuple(state):
        if field in scalar_overrides:
            state[field] = scalar_overrides[field]
        elif field.endswith(".rng_state") or field.endswith(".rng_states"):
            state[field] = [
                record
                for record in rng_records
                if rng_owner_by_substream[record["substream_id"]]
                == item.state_owner_id
            ]
    return state


def _wo31c_checkpoint_fixture(plan=None):
    """Build one complete composition-derived checkpoint and its real prefix."""

    from kirby2.full_day.checkpoint_contract import checkpoint_inventory_v1
    from kirby2.full_day.checkpoints import (
        ABSENT_NATIVE_PLAN,
        RUNTIME_CHECKPOINT_FORMAT_ID,
        EngineRuntimeCompatibilityV1,
        RuntimeCheckpointV1,
        derive_checkpoint_composition_expectation,
    )
    from kirby2.full_day.composition import initial_composition_matrix
    from kirby2.runtime_state import RuntimeComponentStateV1

    (
        selected_plan,
        events,
        cut,
        runtime_snapshot,
        pending_work,
    ) = _wo31c_genesis_checkpoint_prefix(plan)
    matrix = initial_composition_matrix()
    inventory = checkpoint_inventory_v1()
    expectation = derive_checkpoint_composition_expectation(
        selected_plan,
        matrix,
        inventory,
    )
    active = set(expectation.active_component_ids)
    records: list[RuntimeComponentStateV1] = []
    for item in inventory.items:
        if item.component_id in active:
            records.append(
                RuntimeComponentStateV1.preserved(
                    component_id=item.component_id,
                    component_schema_version=(
                        expectation.component_schema_versions[item.component_id]
                    ),
                    implementation_version=(
                        expectation.implementation_versions[item.component_id]
                    ),
                    state=_wo31c_preserved_state(
                        item,
                        plan=selected_plan,
                        expectation=expectation,
                        cut=cut,
                        runtime_snapshot=runtime_snapshot,
                        pending_work=pending_work,
                        event_prefix=events,
                    ),
                    dependencies=(
                        expectation.dependencies_by_component[item.component_id]
                    ),
                )
            )
        else:
            records.append(
                RuntimeComponentStateV1.absent(
                    component_id=item.component_id,
                    absent_reason=(
                        expectation.absent_reasons_by_component[item.component_id]
                    ),
                )
            )
    checkpoint = RuntimeCheckpointV1(
        schema_version=1,
        format_id=RUNTIME_CHECKPOINT_FORMAT_ID,
        engine_runtime=EngineRuntimeCompatibilityV1.current(),
        native_plan_compiler_identity=ABSENT_NATIVE_PLAN,
        semantic_plan_sha256=expectation.semantic_plan_sha256,
        composition_matrix_sha256=expectation.composition_matrix_sha256,
        composition_profile_id=expectation.composition_profile_id,
        composition_profile_version=expectation.composition_profile_version,
        composition_profile_sha256=expectation.composition_profile_sha256,
        checkpoint_inventory_id=expectation.checkpoint_inventory_id,
        checkpoint_inventory_sha256=expectation.checkpoint_inventory_sha256,
        component_inventory=expectation.component_inventory,
        quiescent_cut=cut,
        components=tuple(records),
    )
    return (
        selected_plan,
        matrix,
        inventory,
        expectation,
        events,
        checkpoint,
        runtime_snapshot,
        pending_work,
    )


def audit_wo31c_checkpoints() -> tuple[FullDayAuditCase, ...]:
    """Exercise portable checkpoint truth and governed data paths end to end."""

    return (
        _wo31c_data_paths_case(),
        _wo31c_checkpoint_inventory_truth_case(),
        _wo31c_checkpoint_wire_prefix_case(),
        _wo31c_checkpoint_hostile_case(),
        _wo31c_checkpoint_relocation_case(),
    )


def _wo31c_checkpoint_inventory_truth_case() -> FullDayAuditCase:
    """Prove all 29 rows follow exact composition PRESERVED/ABSENT truth."""

    from kirby2.full_day.checkpoints import validate_runtime_checkpoint
    from kirby2.runtime_state import RuntimeComponentStatusV1

    failures: list[str] = []
    full = _wo31c_checkpoint_fixture()
    (
        full_plan,
        matrix,
        inventory,
        full_expectation,
        full_events,
        full_checkpoint,
        _,
        _,
    ) = full
    try:
        validated_full = validate_runtime_checkpoint(
            full_checkpoint,
            plan=full_plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=full_events,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"complete checkpoint fixture was refused: {error}")
        validated_full = None

    inactive_plan = replace(full_plan, participant_schedule=())
    inactive = _wo31c_checkpoint_fixture(inactive_plan)
    (
        inactive_plan,
        inactive_matrix,
        inactive_inventory,
        inactive_expectation,
        inactive_events,
        inactive_checkpoint,
        _,
        _,
    ) = inactive
    try:
        validated_inactive = validate_runtime_checkpoint(
            inactive_checkpoint,
            plan=inactive_plan,
            composition_matrix=inactive_matrix,
            inventory=inactive_inventory,
            event_prefix=inactive_events,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"inactive checkpoint fixture was refused: {error}")
        validated_inactive = None

    def preserved_ids(checkpoint) -> tuple[str, ...]:
        return tuple(
            record.component_id
            for record in checkpoint.components
            if record.status is RuntimeComponentStatusV1.PRESERVED
        )

    def absent_reasons(checkpoint) -> dict[str, str]:
        return {
            record.component_id: record.absent_reason
            for record in checkpoint.components
            if record.status is RuntimeComponentStatusV1.ABSENT
        }

    if len(full_checkpoint.components) != 29 or len(inactive_checkpoint.components) != 29:
        failures.append("checkpoint fixtures do not contain the frozen 29-row inventory")
    if full_checkpoint.component_inventory != inactive_checkpoint.component_inventory:
        failures.append("active/inactive fixtures changed the frozen component inventory")
    if preserved_ids(full_checkpoint) != full_expectation.active_component_ids:
        failures.append("complete fixture PRESERVED rows differ from composition truth")
    if absent_reasons(full_checkpoint) != dict(
        full_expectation.absent_reasons_by_component
    ):
        failures.append("complete fixture ABSENT reasons differ from composition truth")
    if preserved_ids(inactive_checkpoint) != inactive_expectation.active_component_ids:
        failures.append("inactive fixture PRESERVED rows differ from composition truth")
    if absent_reasons(inactive_checkpoint) != dict(
        inactive_expectation.absent_reasons_by_component
    ):
        failures.append("inactive fixture ABSENT reasons differ from composition truth")
    if "AGENT_SCHEDULER_METAORDERS_V1" not in preserved_ids(full_checkpoint):
        failures.append("scheduled-agent checkpoint state was not preserved")
    inactive_agent = next(
        record
        for record in inactive_checkpoint.components
        if record.component_id == "AGENT_SCHEDULER_METAORDERS_V1"
    )
    if (
        inactive_agent.status is not RuntimeComponentStatusV1.ABSENT
        or inactive_agent.absent_reason != "COMPOSITION_ACTIVE_PREDICATE_FALSE"
    ):
        failures.append("inactive AgentScheduler lacks its composition proof")
    if validated_full is not None and validated_full.as_dict() != full_expectation.as_dict():
        failures.append("complete checkpoint validation changed derived expectation")
    if (
        validated_inactive is not None
        and validated_inactive.as_dict() != inactive_expectation.as_dict()
    ):
        failures.append("inactive checkpoint validation changed derived expectation")

    return FullDayAuditCase(
        "checkpoint_composition_inventory_truth",
        (
            "inventory_rows=29 "
            f"full_preserved={len(full_expectation.active_component_ids)} "
            f"full_absent={len(full_expectation.absent_reasons_by_component)} "
            f"inactive_preserved={len(inactive_expectation.active_component_ids)} "
            f"inactive_absent={len(inactive_expectation.absent_reasons_by_component)}"
        ),
        tuple(failures),
    )


def _wo31c_checkpoint_wire_prefix_case() -> FullDayAuditCase:
    """Prove canonical bytes and a real marker-inclusive genesis prefix."""

    from kirby2.full_day.checkpoints import (
        RuntimeCheckpointV1,
        validate_checkpoint_event_prefix,
        validate_runtime_checkpoint,
    )
    from kirby2.full_day.models import canonical_json_bytes

    failures: list[str] = []
    (
        plan,
        matrix,
        inventory,
        expectation,
        events,
        checkpoint,
        runtime_snapshot,
        _,
    ) = (
        _wo31c_checkpoint_fixture()
    )
    wire = checkpoint.canonical_bytes()
    try:
        restored = RuntimeCheckpointV1.from_json_bytes(wire)
        validate_checkpoint_event_prefix(restored, events)
        validate_runtime_checkpoint(
            restored,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=events,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"canonical checkpoint round trip was refused: {error}")
        restored = None
    if restored is not None:
        if restored.canonical_bytes() != wire:
            failures.append("checkpoint canonical bytes changed after round trip")
        if (
            restored.semantic_sha256 != checkpoint.semantic_sha256
            or restored.checkpoint_id != checkpoint.checkpoint_id
        ):
            failures.append("checkpoint semantic identity changed after round trip")
    event_types = tuple(event.event_type.value for event in events)
    expected_types = (
        "CALENDAR_BOUNDARY",
        "DAY_STATE_ANCHOR_RESET",
        "CHECKPOINT_CAPTURE_MARKER",
    )
    if event_types != expected_types:
        failures.append("genesis checkpoint prefix lacks its exact causal events")
    if (
        checkpoint.quiescent_cut.simulation_time_us != 0
        or checkpoint.quiescent_cut.last_global_event_sequence != 3
        or events[-1].chronological_key != (0, 0, 11)
    ):
        failures.append("genesis checkpoint is not aligned after all t=0 microsteps")
    if expectation.component_inventory != checkpoint.component_inventory:
        failures.append("wire checkpoint inventory differs from derived expectation")
    state_record = next(
        record
        for record in checkpoint.components
        if record.component_id
        == "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1"
    )
    if state_record.state is None or canonical_json_bytes(
        state_record.state
    ) != canonical_json_bytes(_wo31c_runtime_flat_state(runtime_snapshot)):
        failures.append("checkpoint state fields differ from the real runtime snapshot")
    root_record = next(
        record
        for record in checkpoint.components
        if record.component_id
        == "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
    )
    root_rng_rows = (
        ()
        if root_record.state is None
        else root_record.state["runtime.rng_states"]
    )
    root_rng_by_id = {
        row["substream_id"]: row for row in root_rng_rows
    }
    for label, transition_rng in (
        (runtime_snapshot.day_rng.substream_label, runtime_snapshot.day_rng),
        (runtime_snapshot.local_rng.substream_label, runtime_snapshot.local_rng),
    ):
        row = root_rng_by_id.get(label)
        expected = {
            "draw_count": transition_rng.draw_count,
            "initial_seed": transition_rng.initial_seed,
            "sample_count": transition_rng.sample_count,
            "state_u64": transition_rng.state_u64,
            "substream_id": transition_rng.substream_label,
        }
        if row is None or any(row.get(key) != value for key, value in expected.items()):
            failures.append(f"checkpoint {label} RNG differs from runtime snapshot")
        elif row["draw_count"] != 2 or row["sample_count"] != 2:
            failures.append(f"genesis {label} RNG did not record its two samples")

    return FullDayAuditCase(
        "canonical_checkpoint_and_genesis_prefix",
        (
            "canonical_roundtrip=byte_identical t0_cut=(0,0,11) "
            "prefix=('CALENDAR_BOUNDARY','DAY_STATE_ANCHOR_RESET',"
            "'CHECKPOINT_CAPTURE_MARKER') runtime_snapshot=exact "
            "owned_prng_codecs=validated"
        ),
        tuple(failures),
    )


def _wo31c_checkpoint_hostile_case() -> FullDayAuditCase:
    """Exercise the required fail-closed checkpoint corruption classes."""

    from kirby2.full_day.events import (
        FullDayEventPayloadV1,
        FullDayEventTypeV1,
        FullDayEventV1,
        NativeEventReferenceV1,
        ScheduledWorkKeyV1,
        WorkStageV1,
        canonical_event_prefix_sha256,
    )
    from kirby2.full_day.checkpoints import (
        OwnedPrngStateV1,
        RuntimeCheckpointV1,
        checkpoint_artifact_reference,
        validate_checkpoint_event_prefix,
        validate_runtime_checkpoint,
    )
    from kirby2.full_day.models import canonical_sha256
    from kirby2.runtime_state import (
        RuntimeComponentStateV1,
        RuntimeComponentStatusV1,
        validate_runtime_component_inventory,
    )

    (
        plan,
        matrix,
        inventory,
        expectation,
        events,
        checkpoint,
        runtime_snapshot,
        pending_work,
    ) = (
        _wo31c_checkpoint_fixture()
    )
    row_by_id = {item.component_id: item for item in inventory.items}
    failures: list[str] = []
    refusal_count = 0

    def validate(candidate) -> object:
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=events,
        )

    def replace_record(component_id: str, replacement_record, *, candidate=checkpoint):
        return replace(
            candidate,
            components=tuple(
                replacement_record if record.component_id == component_id else record
                for record in candidate.components
            ),
        )

    def mutate_component_state(
        component_id: str,
        mutator: Callable[[dict[str, object]], None],
        *,
        candidate=checkpoint,
    ):
        record = next(
            row for row in candidate.components if row.component_id == component_id
        )
        payload = record.as_dict()
        state = payload["state"]
        assert isinstance(state, dict)
        mutator(state)
        replacement_record = RuntimeComponentStateV1.preserved(
            component_id=record.component_id,
            component_schema_version=record.component_schema_version,
            implementation_version=record.implementation_version,
            state=state,
            dependencies=record.dependencies or (),
        )
        return replace_record(
            component_id,
            replacement_record,
            candidate=candidate,
        )

    def rebind_checkpoint_prefix(candidate, event_prefix):
        prefix_digest = canonical_event_prefix_sha256(event_prefix)
        prefix_length = len(event_prefix)
        candidate = replace(
            candidate,
            quiescent_cut=replace(
                candidate.quiescent_cut,
                last_global_event_sequence=prefix_length,
                event_prefix_last_global_sequence=prefix_length,
                event_prefix_sha256=prefix_digest,
            ),
        )
        candidate = mutate_component_state(
            "GLOBAL_EVENT_ALLOCATOR_V1",
            lambda state: state.__setitem__(
                "global_event_allocator.next_sequence", prefix_length + 1
            ),
            candidate=candidate,
        )

        def mutate_ledger(state: dict[str, object]) -> None:
            state["ledger.event_prefix_last_global_sequence"] = prefix_length
            state["ledger.event_prefix_sha256"] = prefix_digest

        return mutate_component_state(
            "LEDGER_PREFIX_V1",
            mutate_ledger,
            candidate=candidate,
        )

    active_record = next(
        record
        for record in checkpoint.components
        if record.status is RuntimeComponentStatusV1.PRESERVED
    )
    inactive_record = next(
        record
        for record in checkpoint.components
        if record.status is RuntimeComponentStatusV1.ABSENT
    )

    def missing_active_state() -> object:
        replacement_record = RuntimeComponentStateV1.absent(
            component_id=active_record.component_id,
            absent_reason="COMPOSITION_ACTIVE_PREDICATE_FALSE",
        )
        return validate(replace_record(active_record.component_id, replacement_record))

    def preserved_inactive_state() -> object:
        item = row_by_id[inactive_record.component_id]
        replacement_record = RuntimeComponentStateV1.preserved(
            component_id=item.component_id,
            component_schema_version=item.state_schema_version,
            implementation_version=1,
            state=_wo31c_preserved_state(
                item,
                plan=plan,
                expectation=expectation,
                cut=checkpoint.quiescent_cut,
                runtime_snapshot=runtime_snapshot,
                pending_work=pending_work,
                event_prefix=events,
            ),
            dependencies=item.dependencies,
        )
        return validate(replace_record(item.component_id, replacement_record))

    def corrupt_component_digest() -> object:
        payload = active_record.as_dict()
        state = payload["state"]
        assert isinstance(state, dict)
        first_field = sorted(state)[0]
        state[first_field] = {"corrupt": True}
        return RuntimeComponentStateV1.from_dict(payload)

    def duplicate_component_record() -> object:
        payload = checkpoint.as_dict()
        components = payload["components"]
        assert isinstance(components, list)
        components[1] = copy.deepcopy(components[0])
        return RuntimeCheckpointV1.from_dict(payload)

    def cyclic_dependencies() -> object:
        left_id = "COMPONENT_LOCAL_ALLOCATORS_V1"
        right_id = "GLOBAL_EVENT_ALLOCATOR_V1"
        cycle_dependencies = dict(expectation.dependencies_by_component)
        cycle_dependencies[left_id] = (right_id,)
        cycle_dependencies[right_id] = (left_id,)
        cycle_records = []
        for record in checkpoint.components:
            if record.component_id not in {left_id, right_id}:
                cycle_records.append(record)
                continue
            state = record.as_dict()["state"]
            assert isinstance(state, dict)
            cycle_records.append(
                RuntimeComponentStateV1.preserved(
                    component_id=record.component_id,
                    component_schema_version=record.component_schema_version,
                    implementation_version=record.implementation_version,
                    state=state,
                    dependencies=cycle_dependencies[record.component_id],
                )
            )
        return validate_runtime_component_inventory(
            tuple(cycle_records),
            expected_component_ids=expectation.component_inventory,
            active_component_ids=expectation.active_component_ids,
            dependencies_by_component=cycle_dependencies,
            absent_reasons_by_component=expectation.absent_reasons_by_component,
        )

    def wrong_engine_runtime() -> object:
        payload = checkpoint.as_dict()
        engine = payload["engine_runtime"]
        assert isinstance(engine, dict)
        engine["python_minor"] = engine["python_minor"] + 1
        return RuntimeCheckpointV1.from_dict(payload)

    def component_with_mutated_rng(mutator: Callable[[dict[str, object]], None]):
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        root_record = next(
            record for record in checkpoint.components if record.component_id == root_id
        )
        root_state = root_record.as_dict()["state"]
        assert isinstance(root_state, dict)
        rng_states = root_state["runtime.rng_states"]
        assert isinstance(rng_states, list) and rng_states
        selected = next(
            row
            for row in rng_states
            if row["algorithm_id"] == "CPYTHON_MT19937_V1"
        )
        mutator(selected)
        replacement_record = RuntimeComponentStateV1.preserved(
            component_id=root_id,
            component_schema_version=root_record.component_schema_version,
            implementation_version=root_record.implementation_version,
            state=root_state,
            dependencies=root_record.dependencies or (),
        )
        return validate(replace_record(root_id, replacement_record))

    def wrong_rng_codec() -> object:
        return component_with_mutated_rng(
            lambda row: row.__setitem__("codec_id", "UNSUPPORTED_CODEC_V1")
        )

    def wrong_rng_runtime() -> object:
        return component_with_mutated_rng(
            lambda row: row.__setitem__("python_minor", row["python_minor"] + 1)
        )

    def unknown_checkpoint_schema() -> object:
        payload = checkpoint.as_dict()
        payload["schema_version"] = 2
        return RuntimeCheckpointV1.from_dict(payload)

    def nonquiescent_cut() -> object:
        return replace(checkpoint.quiescent_cut, due_work_at_or_before_cut=1)

    def incomplete_t0_prefix() -> object:
        marker = replace(
            events[-1],
            global_event_sequence=2,
            component_local_sequence=2,
        )
        incomplete = (events[0], marker)
        cut = replace(
            checkpoint.quiescent_cut,
            last_global_event_sequence=2,
            event_prefix_last_global_sequence=2,
            event_prefix_sha256=canonical_event_prefix_sha256(incomplete),
        )
        candidate = replace(checkpoint, quiescent_cut=cut)
        return validate_checkpoint_event_prefix(candidate, incomplete)

    def embedded_self_digest() -> object:
        payload = checkpoint.as_dict()
        payload["checkpoint_sha256"] = checkpoint.semantic_sha256
        return RuntimeCheckpointV1.from_dict(payload)

    def unknown_component_implementation() -> object:
        assert active_record.state is not None
        replacement_record = RuntimeComponentStateV1.preserved(
            component_id=active_record.component_id,
            component_schema_version=active_record.component_schema_version,
            implementation_version=(active_record.implementation_version or 0) + 1,
            state=active_record.state,
            dependencies=active_record.dependencies or (),
        )
        return validate(replace_record(active_record.component_id, replacement_record))

    def floating_state() -> object:
        payload = active_record.as_dict()
        state = payload["state"]
        assert isinstance(state, dict)
        state[sorted(state)[0]] = 1.5
        return RuntimeComponentStateV1.from_dict(payload)

    def noncanonical_checkpoint_bytes() -> object:
        return RuntimeCheckpointV1.from_json_bytes(b" " + checkpoint.canonical_bytes())

    def duplicate_global_event_sequence() -> object:
        malformed_events = (
            events[0],
            events[1],
            replace(events[2], global_event_sequence=2),
        )
        malformed_digest = canonical_sha256(
            [event.as_dict() for event in malformed_events]
        )
        candidate = replace(
            checkpoint,
            quiescent_cut=replace(
                checkpoint.quiescent_cut,
                event_prefix_sha256=malformed_digest,
            ),
        )
        candidate = mutate_component_state(
            "LEDGER_PREFIX_V1",
            lambda state: state.__setitem__(
                "ledger.event_prefix_sha256", malformed_digest
            ),
            candidate=candidate,
        )
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=malformed_events,
        )

    def negative_zero_gaussian_cache() -> object:
        return component_with_mutated_rng(
            lambda row: row.__setitem__(
                "gaussian_cache_u64", 0x8000000000000000
            )
        )

    state_runtime_id = (
        "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1"
    )

    def forged_day_duration() -> object:
        def mutate(state: dict[str, object]) -> None:
            entered = state["state.day_entered_time_us"]
            duration = state["state.day_sampled_duration_us"]
            assert type(entered) is int and type(duration) is int
            state["state.day_sampled_duration_us"] = duration + 1
            state["state.day_sampled_deadline_us"] = entered + duration + 1

        return validate(mutate_component_state(state_runtime_id, mutate))

    def forged_day_deadline() -> object:
        def mutate(state: dict[str, object]) -> None:
            deadline = state["state.day_sampled_deadline_us"]
            assert type(deadline) is int
            state["state.day_sampled_deadline_us"] = deadline + 1

        return validate(mutate_component_state(state_runtime_id, mutate))

    def forged_day_eligible_time() -> object:
        def mutate(state: dict[str, object]) -> None:
            eligible = state["state.day_next_eligible_transition_time_us"]
            assert type(eligible) is int
            state["state.day_next_eligible_transition_time_us"] = eligible + 1

        return validate(mutate_component_state(state_runtime_id, mutate))

    def participant_cursor_advance() -> object:
        return validate(
            mutate_component_state(
                "PARTICIPANT_SCHEDULE_RUNTIME_V1",
                lambda state: state.__setitem__(
                    "participant_schedule.next_index", 1
                ),
            )
        )

    def scheduled_event_cursor_advance() -> object:
        return validate(
            mutate_component_state(
                "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1",
                lambda state: state.__setitem__("scheduled_event.next_index", 1),
            )
        )

    def wrong_algorithm_root_owner_swap() -> object:
        agent_id = "AGENT_SCHEDULER_METAORDERS_V1"
        agent_record = next(
            record
            for record in checkpoint.components
            if record.component_id == agent_id
        )
        agent_payload = agent_record.as_dict()
        agent_state = agent_payload["state"]
        assert isinstance(agent_state, dict)
        agent_rng = agent_state["agent.rng_states"]
        assert isinstance(agent_rng, list) and agent_rng
        selected_label = agent_rng[0]["substream_id"]
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        root_record = next(
            record
            for record in checkpoint.components
            if record.component_id == root_id
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        selected = next(
            row for row in root_rng if row["substream_id"] == selected_label
        )
        fake = OwnedPrngStateV1.splitmix64(
            substream_id=selected_label,
            initial_seed=selected["initial_seed"],
            state_u64=selected["initial_seed"],
            draw_count=0,
            sample_count=0,
        ).as_dict()

        def swap(rows: object) -> list[object]:
            assert isinstance(rows, list)
            return [
                copy.deepcopy(fake)
                if row["substream_id"] == selected_label
                else row
                for row in rows
            ]

        candidate = mutate_component_state(
            root_id,
            lambda state: state.__setitem__(
                "runtime.rng_states", swap(state["runtime.rng_states"])
            ),
        )
        candidate = mutate_component_state(
            agent_id,
            lambda state: state.__setitem__(
                "agent.rng_states", swap(state["agent.rng_states"])
            ),
            candidate=candidate,
        )
        return validate(candidate)

    def extra_day_splitmix_raw_draw() -> object:
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        day_label = runtime_snapshot.day_rng.substream_label
        root_record = next(
            record
            for record in checkpoint.components
            if record.component_id == root_id
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        selected = next(
            row for row in root_rng if row["substream_id"] == day_label
        )
        advanced = OwnedPrngStateV1.splitmix64(
            substream_id=day_label,
            initial_seed=selected["initial_seed"],
            state_u64=(
                selected["state_u64"] + 0x9E3779B97F4A7C15
            )
            & ((1 << 64) - 1),
            draw_count=selected["draw_count"] + 1,
            sample_count=selected["sample_count"],
        ).as_dict()

        def mutate(state: dict[str, object]) -> None:
            rows = state["runtime.rng_states"]
            assert isinstance(rows, list)
            state["runtime.rng_states"] = [
                copy.deepcopy(advanced)
                if row["substream_id"] == day_label
                else row
                for row in rows
            ]

        return validate(mutate_component_state(root_id, mutate))

    def future_participant_mt_raw_draw() -> object:
        from random import Random

        participant = next(
            participant
            for participant in plan.participant_definitions
            if not participant.initially_active
            and all(
                entry.simulation_time_us > checkpoint.quiescent_cut.simulation_time_us
                for entry in plan.participant_schedule
                if entry.participant_id == participant.participant_id
            )
        )
        selected_label = participant.rng_substream_label
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        root_record = next(
            record
            for record in checkpoint.components
            if record.component_id == root_id
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        selected = next(
            row for row in root_rng if row["substream_id"] == selected_label
        )
        assert selected["gaussian_cache_u64"] is None
        generator = Random()
        generator.setstate(
            (
                selected["random_state_version"],
                tuple(selected["state_words"]) + (selected["state_index"],),
                None,
            )
        )
        generator.getrandbits(32)
        random_state_version, inner_state, gaussian_cache = generator.getstate()
        assert gaussian_cache is None
        advanced = OwnedPrngStateV1.cpython_mt19937(
            substream_id=selected_label,
            initial_seed=selected["initial_seed"],
            state_words=tuple(inner_state[:-1]),
            state_index=inner_state[-1],
        ).as_dict()

        def replace_row(rows: object) -> list[object]:
            assert isinstance(rows, list)
            return [
                copy.deepcopy(advanced)
                if row["substream_id"] == selected_label
                else row
                for row in rows
            ]

        candidate = mutate_component_state(
            root_id,
            lambda state: state.__setitem__(
                "runtime.rng_states", replace_row(state["runtime.rng_states"])
            ),
        )
        candidate = mutate_component_state(
            "AGENT_SCHEDULER_METAORDERS_V1",
            lambda state: state.__setitem__(
                "agent.rng_states", replace_row(state["agent.rng_states"])
            ),
            candidate=candidate,
        )
        return validate(candidate)

    def initially_active_participant_mt_raw_draw() -> object:
        from random import Random

        selected_participant_id = "AUDIT_MAKER"
        alternate_plan = replace(
            plan,
            participant_definitions=tuple(
                replace(participant, initially_active=True)
                if participant.participant_id == selected_participant_id
                else participant
                for participant in plan.participant_definitions
            ),
        )
        (
            alternate_plan,
            alternate_matrix,
            alternate_inventory,
            _,
            alternate_events,
            alternate_checkpoint,
            _,
            _,
        ) = _wo31c_checkpoint_fixture(alternate_plan)
        participant = next(
            participant
            for participant in alternate_plan.participant_definitions
            if participant.participant_id == selected_participant_id
        )
        selected_label = participant.rng_substream_label
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        root_record = next(
            record
            for record in alternate_checkpoint.components
            if record.component_id == root_id
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        selected = next(
            row for row in root_rng if row["substream_id"] == selected_label
        )
        assert selected["gaussian_cache_u64"] is None
        generator = Random()
        generator.setstate(
            (
                selected["random_state_version"],
                tuple(selected["state_words"]) + (selected["state_index"],),
                None,
            )
        )
        generator.getrandbits(32)
        _, inner_state, gaussian_cache = generator.getstate()
        assert gaussian_cache is None
        advanced = OwnedPrngStateV1.cpython_mt19937(
            substream_id=selected_label,
            initial_seed=selected["initial_seed"],
            state_words=tuple(inner_state[:-1]),
            state_index=inner_state[-1],
        ).as_dict()

        def replace_row(rows: object) -> list[object]:
            assert isinstance(rows, list)
            return [
                copy.deepcopy(advanced)
                if row["substream_id"] == selected_label
                else row
                for row in rows
            ]

        candidate = mutate_component_state(
            root_id,
            lambda state: state.__setitem__(
                "runtime.rng_states", replace_row(state["runtime.rng_states"])
            ),
            candidate=alternate_checkpoint,
        )
        candidate = mutate_component_state(
            "AGENT_SCHEDULER_METAORDERS_V1",
            lambda state: state.__setitem__(
                "agent.rng_states", replace_row(state["agent.rng_states"])
            ),
            candidate=candidate,
        )
        return validate_runtime_checkpoint(
            candidate,
            plan=alternate_plan,
            composition_matrix=alternate_matrix,
            inventory=alternate_inventory,
            event_prefix=alternate_events,
        )

    def forged_participant_decision_rng_exemption() -> object:
        from random import Random

        participant = next(
            participant
            for participant in plan.participant_definitions
            if participant.participant_id == "AUDIT_MAKER"
        )
        selected_label = participant.rng_substream_label
        decision_work = ScheduledWorkKeyV1(
            simulation_time_us=0,
            microstep=0,
            stage_ordinal=WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            source_component_id="AGENT_SCHEDULER_V1",
            component_local_sequence=1,
        )
        native = NativeEventReferenceV1(
            schema_version=1,
            owner_component_id="AGENT_SCHEDULER_V1",
            native_ledger_id="AGENT_EVENT_LEDGER_V1",
            event_type="PARTICIPANT_DECISION",
            local_sequence=1,
            event_id="FORGED_NATIVE_DECISION_V1",
        )
        decision = FullDayEventV1(
            schema_version=1,
            global_event_sequence=3,
            simulation_time_us=0,
            microstep=0,
            stage=WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            source_component_id="AGENT_SCHEDULER_V1",
            component_local_sequence=1,
            event_type=FullDayEventTypeV1.PARTICIPANT_DECISION,
            causal_parent_ids=(decision_work.work_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=FullDayEventTypeV1.PARTICIPANT_DECISION.value,
                payload_version=1,
                native_event=native,
                data={
                    "decision_id": "FORGED_DECISION_V1",
                    "information_cutoff_us": 0,
                    "native_payload_sha256": "d" * 64,
                    "participant_id": participant.participant_id,
                },
            ),
        )
        forged_events = (
            events[0],
            events[1],
            decision,
            replace(events[2], global_event_sequence=4),
        )
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        root_record = next(
            record
            for record in checkpoint.components
            if record.component_id == root_id
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        selected = next(
            row for row in root_rng if row["substream_id"] == selected_label
        )
        generator = Random()
        generator.setstate(
            (
                selected["random_state_version"],
                tuple(selected["state_words"]) + (selected["state_index"],),
                None,
            )
        )
        generator.getrandbits(32)
        _, inner_state, gaussian_cache = generator.getstate()
        assert gaussian_cache is None
        advanced = OwnedPrngStateV1.cpython_mt19937(
            substream_id=selected_label,
            initial_seed=selected["initial_seed"],
            state_words=tuple(inner_state[:-1]),
            state_index=inner_state[-1],
        ).as_dict()

        def replace_row(rows: object) -> list[object]:
            assert isinstance(rows, list)
            return [
                copy.deepcopy(advanced)
                if row["substream_id"] == selected_label
                else row
                for row in rows
            ]

        candidate = mutate_component_state(
            root_id,
            lambda state: state.__setitem__(
                "runtime.rng_states", replace_row(state["runtime.rng_states"])
            ),
        )
        candidate = mutate_component_state(
            "AGENT_SCHEDULER_METAORDERS_V1",
            lambda state: state.__setitem__(
                "agent.rng_states", replace_row(state["agent.rng_states"])
            ),
            candidate=candidate,
        )
        candidate = mutate_component_state(
            "COMPONENT_LOCAL_ALLOCATORS_V1",
            lambda state: state.__setitem__(
                "runtime.non_state_component_local_event_sequence_allocators",
                {"AGENT_SCHEDULER_V1": 1},
            ),
            candidate=candidate,
        )
        candidate = rebind_checkpoint_prefix(candidate, forged_events)
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=forged_events,
        )

    def omitted_non_state_allocator() -> object:
        refused_work = ScheduledWorkKeyV1(
            simulation_time_us=0,
            microstep=0,
            stage_ordinal=WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            source_component_id="AGENT_SCHEDULER_V1",
            component_local_sequence=1,
        )
        refused = FullDayEventV1(
            schema_version=1,
            global_event_sequence=3,
            simulation_time_us=0,
            microstep=0,
            stage=WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            source_component_id="AGENT_SCHEDULER_V1",
            component_local_sequence=1,
            event_type=FullDayEventTypeV1.CAPABILITY_REFUSED,
            causal_parent_ids=(refused_work.work_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=FullDayEventTypeV1.CAPABILITY_REFUSED.value,
                payload_version=1,
                native_event=None,
                data={
                    "capability_id": "FORGED_CAPABILITY_V1",
                    "reason_code": "FORGED_REFUSAL_V1",
                },
            ),
        )
        forged_events = (
            events[0],
            events[1],
            refused,
            replace(events[2], global_event_sequence=4),
        )
        candidate = rebind_checkpoint_prefix(checkpoint, forged_events)
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=forged_events,
        )

    def forged_shock_rng_exemption() -> object:
        from random import Random

        runtime_id = "FULL_DAY_RUNTIME_V1"
        candidate_work = ScheduledWorkKeyV1(
            simulation_time_us=0,
            microstep=0,
            stage_ordinal=WorkStageV1.SCHEDULED_INFORMATION,
            source_component_id=runtime_id,
            component_local_sequence=2,
        )
        shock_candidate = FullDayEventV1(
            schema_version=1,
            global_event_sequence=2,
            simulation_time_us=0,
            microstep=0,
            stage=WorkStageV1.SCHEDULED_INFORMATION,
            source_component_id=runtime_id,
            component_local_sequence=2,
            event_type=FullDayEventTypeV1.SHOCK_CANDIDATE,
            causal_parent_ids=(candidate_work.work_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=FullDayEventTypeV1.SHOCK_CANDIDATE.value,
                payload_version=1,
                native_event=None,
                data={
                    "candidate_id": "FORGED_SHOCK_CANDIDATE_V1",
                    "information_cutoff_us": 0,
                    "quantity_shares": 1,
                    "side": "BUY",
                },
            ),
        )
        shock_rejected = FullDayEventV1(
            schema_version=1,
            global_event_sequence=3,
            simulation_time_us=0,
            microstep=0,
            stage=WorkStageV1.SCHEDULED_INFORMATION,
            source_component_id=runtime_id,
            component_local_sequence=3,
            event_type=FullDayEventTypeV1.SHOCK_REJECTED,
            causal_parent_ids=(shock_candidate.event_id,),
            payload=FullDayEventPayloadV1(
                schema_version=1,
                payload_type=FullDayEventTypeV1.SHOCK_REJECTED.value,
                payload_version=1,
                native_event=None,
                data={
                    "candidate_id": "FORGED_SHOCK_CANDIDATE_V1",
                    "information_cutoff_us": 0,
                    "reason_code": "FORGED_SHOCK_REJECTION_V1",
                },
            ),
        )
        anchor_work = ScheduledWorkKeyV1(
            simulation_time_us=0,
            microstep=0,
            stage_ordinal=WorkStageV1.DAY_STATE_TRANSITION,
            source_component_id=runtime_id,
            component_local_sequence=4,
        )
        marker_work = ScheduledWorkKeyV1(
            simulation_time_us=0,
            microstep=0,
            stage_ordinal=WorkStageV1.CHECKPOINT_CAPTURE,
            source_component_id=runtime_id,
            component_local_sequence=5,
        )
        anchor = replace(
            events[1],
            global_event_sequence=4,
            component_local_sequence=4,
            causal_parent_ids=(anchor_work.work_id,),
        )
        marker = replace(
            events[2],
            global_event_sequence=5,
            component_local_sequence=5,
            causal_parent_ids=(marker_work.work_id,),
        )
        forged_events = (
            events[0],
            shock_candidate,
            shock_rejected,
            anchor,
            marker,
        )
        root_id = "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        root_record = next(
            record
            for record in checkpoint.components
            if record.component_id == root_id
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        selected_label = plan.unscheduled_shock_policy.substream_label
        selected = next(
            row for row in root_rng if row["substream_id"] == selected_label
        )
        generator = Random()
        generator.setstate(
            (
                selected["random_state_version"],
                tuple(selected["state_words"]) + (selected["state_index"],),
                None,
            )
        )
        generator.getrandbits(32)
        _, inner_state, gaussian_cache = generator.getstate()
        assert gaussian_cache is None
        advanced = OwnedPrngStateV1.cpython_mt19937(
            substream_id=selected_label,
            initial_seed=selected["initial_seed"],
            state_words=tuple(inner_state[:-1]),
            state_index=inner_state[-1],
        ).as_dict()

        def mutate_root_rng(state: dict[str, object]) -> None:
            rows = state["runtime.rng_states"]
            assert isinstance(rows, list)
            state["runtime.rng_states"] = [
                copy.deepcopy(advanced)
                if row["substream_id"] == selected_label
                else row
                for row in rows
            ]

        candidate = mutate_component_state(root_id, mutate_root_rng)

        def mutate_runtime(state: dict[str, object]) -> None:
            state["state.component_local_sequence"] = 6
            state["state.component_sequence_offset"] = 5

        candidate = mutate_component_state(
            state_runtime_id,
            mutate_runtime,
            candidate=candidate,
        )
        future_state_work = ScheduledWorkKeyV1(
            simulation_time_us=pending_work.simulation_time_us,
            microstep=pending_work.microstep,
            stage_ordinal=pending_work.stage_ordinal,
            source_component_id=runtime_id,
            component_local_sequence=6,
        )
        candidate = mutate_component_state(
            "SCHEDULED_WORK_QUEUE_V1",
            lambda state: state.__setitem__(
                "scheduled_work.pending_heap", [future_state_work.as_dict()]
            ),
            candidate=candidate,
        )

        def mutate_shock_state(state: dict[str, object]) -> None:
            state["shock.accepted_count"] = 0
            state["shock.candidate_draw_count"] = 1
            state["shock.last_accepted_time_us"] = None
            state["shock.proposal_sequence"] = 1
            state["shock.rejected_count"] = 1

        candidate = mutate_component_state(
            "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1",
            mutate_shock_state,
            candidate=candidate,
        )
        candidate = rebind_checkpoint_prefix(candidate, forged_events)
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=forged_events,
        )

    def pending_event_base_fixture():
        future_work = ScheduledWorkKeyV1(
            simulation_time_us=pending_work.simulation_time_us + 1,
            microstep=0,
            stage_ordinal=WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION,
            source_component_id="AGENT_SCHEDULER_V1",
            component_local_sequence=1,
        )
        native = NativeEventReferenceV1(
            schema_version=1,
            owner_component_id="AGENT_SCHEDULER_V1",
            native_ledger_id="AGENT_EVENT_LEDGER_V1",
            event_type="PARTICIPANT_DECISION",
            local_sequence=1,
            event_id="PENDING_NATIVE_DECISION_V1",
        )
        payload = FullDayEventPayloadV1(
            schema_version=1,
            payload_type=FullDayEventTypeV1.PARTICIPANT_DECISION.value,
            payload_version=1,
            native_event=native,
            data={
                "decision_id": "PENDING_DECISION_V1",
                "information_cutoff_us": 0,
                "native_payload_sha256": "e" * 64,
                "participant_id": "AUDIT_MAKER",
            },
        )
        candidate = replace(
            checkpoint,
            quiescent_cut=replace(
                checkpoint.quiescent_cut,
                pending_work_count=2,
            ),
        )
        ordered_work = tuple(
            sorted((pending_work, future_work), key=lambda item: item.ordering_key)
        )
        candidate = mutate_component_state(
            "SCHEDULED_WORK_QUEUE_V1",
            lambda state: state.__setitem__(
                "scheduled_work.pending_heap",
                [item.as_dict() for item in ordered_work],
            ),
            candidate=candidate,
        )
        candidate = mutate_component_state(
            "COMPONENT_LOCAL_ALLOCATORS_V1",
            lambda state: state.__setitem__(
                "runtime.non_state_component_local_event_sequence_allocators",
                {"AGENT_SCHEDULER_V1": 1},
            ),
            candidate=candidate,
        )

        def mutate_pending(state: dict[str, object]) -> None:
            state["pending_event.causal_parent_by_work_id"] = {
                future_work.work_id: future_work.work_id
            }
            state["pending_event.payloads_by_work_id"] = {
                future_work.work_id: payload.as_dict()
            }

        candidate = mutate_component_state(
            "PENDING_EVENT_QUEUES_V1",
            mutate_pending,
            candidate=candidate,
        )
        return candidate, future_work

    pending_event_base, pending_event_work = pending_event_base_fixture()
    try:
        validate(pending_event_base)
    except (TypeError, ValueError, RuntimeError) as error:
        failures.append(f"valid pending-event hostile base was refused: {error}")

    def mismatched_pending_work_parent() -> object:
        def mutate(state: dict[str, object]) -> None:
            causal = state["pending_event.causal_parent_by_work_id"]
            assert isinstance(causal, dict)
            causal[pending_event_work.work_id] = "work:" + "0" * 64

        return validate(
            mutate_component_state(
                "PENDING_EVENT_QUEUES_V1",
                mutate,
                candidate=pending_event_base,
            )
        )

    def wrong_stage_pending_payload() -> object:
        native = NativeEventReferenceV1(
            schema_version=1,
            owner_component_id="AGENT_SCHEDULER_V1",
            native_ledger_id="AGENT_EVENT_LEDGER_V1",
            event_type="BACKGROUND_FLOW_PROPOSAL",
            local_sequence=1,
            event_id="PENDING_WRONG_STAGE_NATIVE_V1",
        )
        wrong_payload = FullDayEventPayloadV1(
            schema_version=1,
            payload_type=FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL.value,
            payload_version=1,
            native_event=native,
            data={
                "native_payload_sha256": "f" * 64,
                "observation_cutoff_us": 0,
                "proposal_id": "PENDING_WRONG_STAGE_PROPOSAL_V1",
            },
        )

        def mutate(state: dict[str, object]) -> None:
            payloads = state["pending_event.payloads_by_work_id"]
            assert isinstance(payloads, dict)
            payloads[pending_event_work.work_id] = wrong_payload.as_dict()

        return validate(
            mutate_component_state(
                "PENDING_EVENT_QUEUES_V1",
                mutate,
                candidate=pending_event_base,
            )
        )

    def omitted_nonplan_pending_event_state() -> object:
        def mutate(state: dict[str, object]) -> None:
            causal = state["pending_event.causal_parent_by_work_id"]
            payloads = state["pending_event.payloads_by_work_id"]
            assert isinstance(causal, dict)
            assert isinstance(payloads, dict)
            del causal[pending_event_work.work_id]
            del payloads[pending_event_work.work_id]

        return validate(
            mutate_component_state(
                "PENDING_EVENT_QUEUES_V1",
                mutate,
                candidate=pending_event_base,
            )
        )

    def runtime_allocator_rollback() -> object:
        def mutate(state: dict[str, object]) -> None:
            state["state.component_local_sequence"] = 3
            state["state.component_sequence_offset"] = 2

        return validate(mutate_component_state(state_runtime_id, mutate))

    def calendar_cursor_advance() -> object:
        second_operation = plan.calendar.boundary_operations[1]
        third_operation = plan.calendar.boundary_operations[2]

        def mutate(state: dict[str, object]) -> None:
            state["calendar.boundary_operation_index"] = 2
            state["calendar.current_phase_id"] = (
                second_operation.destination_session_state.value
            )
            state["calendar.next_boundary_time_us"] = (
                third_operation.boundary.simulation_time_us
            )

        return validate(mutate_component_state("CALENDAR_CURSOR_V1", mutate))

    def forged_calendar_phase() -> object:
        return validate(
            mutate_component_state(
                "CALENDAR_CURSOR_V1",
                lambda state: state.__setitem__(
                    "calendar.current_phase_id", "CLOSED"
                ),
            )
        )

    def observable_cursor_ahead() -> object:
        return validate(
            mutate_component_state(
                "OBSERVABLE_PUBLICATION_CURSOR_V1",
                lambda state: state.__setitem__(
                    "observable.client_publication_cursor",
                    checkpoint.quiescent_cut.last_global_event_sequence + 1,
                ),
            )
        )

    def forged_t0_event_source() -> object:
        forged_events = (
            replace(
                events[0],
                source_component_id="ENGINE_MARKET_MECHANICS_V1",
            ),
            *events[1:],
        )
        forged_digest = canonical_event_prefix_sha256(forged_events)
        forged_cut = replace(
            checkpoint.quiescent_cut,
            event_prefix_sha256=forged_digest,
        )
        candidate = replace(checkpoint, quiescent_cut=forged_cut)
        candidate = mutate_component_state(
            "LEDGER_PREFIX_V1",
            lambda state: state.__setitem__(
                "ledger.event_prefix_sha256", forged_digest
            ),
            candidate=candidate,
        )
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=forged_events,
        )

    def forged_t0_work_parent() -> object:
        forged_events = (
            replace(
                events[0],
                causal_parent_ids=("work:" + "0" * 64,),
            ),
            *events[1:],
        )
        forged_digest = canonical_event_prefix_sha256(forged_events)
        forged_cut = replace(
            checkpoint.quiescent_cut,
            event_prefix_sha256=forged_digest,
        )
        candidate = replace(checkpoint, quiescent_cut=forged_cut)
        candidate = mutate_component_state(
            "LEDGER_PREFIX_V1",
            lambda state: state.__setitem__(
                "ledger.event_prefix_sha256", forged_digest
            ),
            candidate=candidate,
        )
        return validate_runtime_checkpoint(
            candidate,
            plan=plan,
            composition_matrix=matrix,
            inventory=inventory,
            event_prefix=forged_events,
        )

    def forged_seen_observation() -> object:
        return validate(
            mutate_component_state(
                state_runtime_id,
                lambda state: state.__setitem__(
                    "state.observation_ids_seen",
                    ["FORGED_OBSERVATION_V1"],
                ),
            )
        )

    def dot_artifact_path() -> object:
        return checkpoint_artifact_reference(checkpoint, "./checkpoint.json")

    def nul_artifact_path() -> object:
        return checkpoint_artifact_reference(
            checkpoint,
            "checkpoints/\x00checkpoint.json",
        )

    def empty_owner_rng_copy() -> object:
        return validate(
            mutate_component_state(
                "AGENT_SCHEDULER_METAORDERS_V1",
                lambda state: state.__setitem__("agent.rng_states", []),
            )
        )

    def cross_owner_rng_copy() -> object:
        root_record = next(
            record
            for record in checkpoint.components
            if record.component_id
            == "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1"
        )
        root_payload = root_record.as_dict()
        root_state = root_payload["state"]
        assert isinstance(root_state, dict)
        root_rng = root_state["runtime.rng_states"]
        assert isinstance(root_rng, list)
        foreign = next(
            row
            for row in root_rng
            if row["substream_id"] == runtime_snapshot.day_rng.substream_label
        )

        def mutate(state: dict[str, object]) -> None:
            rows = state["agent.rng_states"]
            assert isinstance(rows, list)
            state["agent.rng_states"] = sorted(
                [*rows, copy.deepcopy(foreign)],
                key=lambda row: row["substream_id"],
            )

        return validate(
            mutate_component_state(
                "AGENT_SCHEDULER_METAORDERS_V1",
                mutate,
            )
        )

    probes: tuple[tuple[str, Callable[[], object]], ...] = (
        ("missing active component state", missing_active_state),
        ("preserved inactive component state", preserved_inactive_state),
        ("corrupt component state digest", corrupt_component_digest),
        ("duplicate component record", duplicate_component_record),
        ("component dependency cycle", cyclic_dependencies),
        ("wrong engine runtime", wrong_engine_runtime),
        ("wrong RNG codec", wrong_rng_codec),
        ("wrong RNG runtime", wrong_rng_runtime),
        ("unknown checkpoint schema", unknown_checkpoint_schema),
        ("nonquiescent checkpoint cut", nonquiescent_cut),
        ("t=0 checkpoint before macro anchor", incomplete_t0_prefix),
        ("embedded checkpoint self digest", embedded_self_digest),
        ("unknown component implementation", unknown_component_implementation),
        ("floating component state", floating_state),
        ("noncanonical checkpoint bytes", noncanonical_checkpoint_bytes),
        ("duplicate global event sequence", duplicate_global_event_sequence),
        ("negative-zero Gaussian cache", negative_zero_gaussian_cache),
        ("forged day sampled duration", forged_day_duration),
        ("forged day deadline", forged_day_deadline),
        ("forged day eligible time", forged_day_eligible_time),
        ("participant schedule cursor advance", participant_cursor_advance),
        ("scheduled event cursor advance", scheduled_event_cursor_advance),
        ("wrong algorithm root and owner swap", wrong_algorithm_root_owner_swap),
        ("extra day SplitMix raw draw", extra_day_splitmix_raw_draw),
        ("future participant MT raw draw", future_participant_mt_raw_draw),
        (
            "initially active participant MT raw draw",
            initially_active_participant_mt_raw_draw,
        ),
        (
            "forged participant decision RNG exemption",
            forged_participant_decision_rng_exemption,
        ),
        ("forged shock RNG exemption", forged_shock_rng_exemption),
        ("omitted non-state allocator", omitted_non_state_allocator),
        ("mismatched pending work parent", mismatched_pending_work_parent),
        ("wrong-stage pending payload", wrong_stage_pending_payload),
        (
            "omitted non-plan pending event state",
            omitted_nonplan_pending_event_state,
        ),
        ("full-day runtime allocator rollback", runtime_allocator_rollback),
        ("calendar cursor advance", calendar_cursor_advance),
        ("forged calendar phase", forged_calendar_phase),
        ("observable cursor ahead", observable_cursor_ahead),
        ("forged t=0 event source", forged_t0_event_source),
        ("forged t=0 work parent", forged_t0_work_parent),
        ("forged seen observation", forged_seen_observation),
        ("dot artifact path", dot_artifact_path),
        ("NUL artifact path", nul_artifact_path),
        ("empty owner RNG copy", empty_owner_rng_copy),
        ("cross-owner RNG copy", cross_owner_rng_copy),
    )
    for label, operation in probes:
        refusal = _expect_refusal(operation, label)
        if refusal:
            failures.append(refusal)
        else:
            refusal_count += 1

    return FullDayAuditCase(
        "checkpoint_hostile_refusals",
        (
            f"hostile_refusals={refusal_count} "
            "missing_active=refused preserved_inactive=refused digest=refused "
            "duplicate=refused cycle=refused runtime_rng=refused "
            "rng_extra_draws=refused initial_active_rng=refused t0=refused "
            "rng_event_exemptions=refused negative_zero=refused "
            "semantic_forgery=refused work_parent=refused event_sequence=refused "
            "observation_forgery=refused cursor_rollback=refused paths=refused "
            "allocator_coverage=refused pending_events=refused "
            "pending_event_coverage=refused self_digest=refused"
        ),
        tuple(failures),
    )


def _wo31c_checkpoint_relocation_case() -> FullDayAuditCase:
    """Prove paths are metadata while artifact hashes still bind exact bytes."""

    from pathlib import Path
    from tempfile import TemporaryDirectory

    from kirby2.full_day.checkpoints import (
        RuntimeCheckpointV1,
        checkpoint_artifact_reference,
    )
    from kirby2.research.paths import DataAreaId, DataPaths

    failures: list[str] = []
    _, _, _, _, _, checkpoint, _, _ = _wo31c_checkpoint_fixture()
    wire = checkpoint.canonical_bytes()
    filename = checkpoint.checkpoint_id + ".json"
    with TemporaryDirectory(prefix="kirby2-wo31c-relocation-") as temporary:
        sandbox = Path(temporary).resolve()
        left = DataPaths((sandbox / "left-root").resolve())
        right = DataPaths((sandbox / "right-root").resolve())
        left.ensure((DataAreaId.CHECKPOINTS,))
        right.ensure((DataAreaId.CHECKPOINTS,))
        left_path = left.checkpoints / filename
        right_directory = right.checkpoints / "relocated"
        right_directory.mkdir()
        right_path = right_directory / filename
        left_path.write_bytes(wire)
        right_path.write_bytes(left_path.read_bytes())
        relocated = RuntimeCheckpointV1.from_json_bytes(right_path.read_bytes())
        if (
            relocated.semantic_sha256 != checkpoint.semantic_sha256
            or relocated.checkpoint_id != checkpoint.checkpoint_id
        ):
            failures.append("checkpoint relocation changed semantic identity")
        left_reference = checkpoint_artifact_reference(
            checkpoint,
            f"checkpoints/{filename}",
        )
        right_reference = checkpoint_artifact_reference(
            checkpoint,
            f"checkpoints/relocated/{filename}",
        )
        artifact_sha256 = hashlib.sha256(wire).hexdigest()
        if (
            left_reference.sha256 != artifact_sha256
            or right_reference.sha256 != artifact_sha256
        ):
            failures.append("artifact references do not bind exact checkpoint bytes")
        if left_reference.relative_path == right_reference.relative_path:
            failures.append("relocation fixture did not exercise distinct paths")
        if str(left.root) in wire.decode("utf-8") or str(right.root) in wire.decode("utf-8"):
            failures.append("checkpoint semantic bytes contain a local data root")
        if hashlib.sha256(wire + b"\n").hexdigest() == artifact_sha256:
            failures.append("transport substitution did not change artifact identity")

    traversal_refusal = _expect_refusal(
        lambda: checkpoint_artifact_reference(checkpoint, "../checkpoint.json"),
        "checkpoint artifact traversal path",
    )
    if traversal_refusal:
        failures.append(traversal_refusal)
    identity_keys = set(checkpoint.semantic_identity_dict())
    forbidden_identity_keys = {
        "artifact_sha256",
        "checkpoint_id",
        "checkpoint_sha256",
        "display_metadata",
        "path",
        "relative_path",
        "semantic_sha256",
    }
    if identity_keys & forbidden_identity_keys:
        failures.append("checkpoint semantic projection includes path or self identity")

    return FullDayAuditCase(
        "checkpoint_identity_and_path_relocation",
        (
            "semantic_identity=relocation_stable artifact_sha256=byte_bound "
            "self_digest=excluded traversal_path=refused relocations=2"
        ),
        tuple(failures),
    )


def _wo31d_order(
    order_id: str,
    side: Side,
    quantity: int,
    *,
    price_ticks: int | None = None,
    owner: OrderOwner = OrderOwner.SIMULATED,
    auction_only: bool = False,
) -> AdvancedOrderRequest:
    """Build one deterministic mechanics request for the restoration audit."""

    is_market = price_ticks is None
    return AdvancedOrderRequest(
        order_id=order_id,
        side=side,
        quantity=quantity,
        instruction=(
            OrderInstruction.MARKET if is_market else OrderInstruction.LIMIT
        ),
        owner=owner,
        account_id=f"ACCOUNT-{order_id}",
        price_ticks=price_ticks,
        time_in_force=(
            OrderInstruction.IOC
            if is_market
            else OrderInstruction.DAY
        ),
        auction_only=auction_only,
    )


def _wo31d_open_continuous(engine: MarketMechanicsEngine, reason: str) -> None:
    engine.transition_session(SessionState.PREOPEN, reason=reason)
    engine.transition_session(SessionState.OPENING_AUCTION, reason=reason)
    engine.transition_session(SessionState.CONTINUOUS, reason=reason)


def _wo31d_command(
    sequence: int,
    simulation_time_us: int,
    command_type: str,
    parameters: dict[str, object],
):
    from kirby2.full_day.restore import CoreSessionCommandV1

    return CoreSessionCommandV1(
        sequence=sequence,
        simulation_time_us=simulation_time_us,
        command_type=command_type,
        parameters=parameters,
    )


def _wo31d_submit(
    sequence: int,
    simulation_time_us: int,
    request: AdvancedOrderRequest,
):
    return _wo31d_command(
        sequence,
        simulation_time_us,
        "SUBMIT",
        {"request": request.as_dict()},
    )


def _wo31d_transition(
    sequence: int,
    simulation_time_us: int,
    state: SessionState,
    reason: str,
):
    return _wo31d_command(
        sequence,
        simulation_time_us,
        "TRANSITION",
        {"reason": reason, "state": state.value},
    )


def _wo31d_boundary_fixtures():
    """Return the eight checkpoint boundaries fixed by WO31-D."""

    fixtures: list[tuple[str, MarketMechanicsEngine, tuple[object, ...], int]] = []

    quiet = MarketMechanicsEngine()
    _wo31d_open_continuous(quiet, "WO31D_QUIET_OPEN")
    quiet.advance_to(11)
    fixtures.append(
        (
            "post_t0_quiet",
            quiet,
            (
                _wo31d_submit(
                    1,
                    12,
                    _wo31d_order("QUIET-BID", Side.BUY, 90, price_ticks=100),
                ),
                _wo31d_submit(
                    2,
                    13,
                    _wo31d_order("QUIET-ASK", Side.SELL, 80, price_ticks=102),
                ),
            ),
            14,
        )
    )

    auction = MarketMechanicsEngine()
    auction.transition_session(SessionState.PREOPEN, reason="WO31D_AUCTION_OPEN")
    auction.transition_session(
        SessionState.OPENING_AUCTION,
        reason="WO31D_AUCTION_OPEN",
    )
    auction.submit(
        _wo31d_order(
            "AUC-BUY",
            Side.BUY,
            100,
            price_ticks=101,
            auction_only=True,
        )
    )
    auction.submit(
        _wo31d_order(
            "AUC-SELL",
            Side.SELL,
            40,
            price_ticks=100,
            owner=OrderOwner.PLAYER,
            auction_only=True,
        )
    )
    fixtures.append(
        (
            "auction_order_imbalance",
            auction,
            (
                _wo31d_command(1, 1, "UNCROSS", {}),
                _wo31d_transition(
                    2,
                    2,
                    SessionState.CONTINUOUS,
                    "WO31D_AUCTION_COMPLETE",
                ),
            ),
            3,
        )
    )

    post_uncross = MarketMechanicsEngine()
    post_uncross.transition_session(
        SessionState.PREOPEN,
        reason="WO31D_POST_UNCROSS_OPEN",
    )
    post_uncross.transition_session(
        SessionState.OPENING_AUCTION,
        reason="WO31D_POST_UNCROSS_OPEN",
    )
    post_uncross.submit(
        _wo31d_order(
            "POST-AUC-BUY",
            Side.BUY,
            60,
            price_ticks=101,
            auction_only=True,
        )
    )
    post_uncross.submit(
        _wo31d_order(
            "POST-AUC-SELL",
            Side.SELL,
            60,
            price_ticks=100,
            auction_only=True,
        )
    )
    post_uncross.uncross_auction()
    fixtures.append(
        (
            "post_uncross",
            post_uncross,
            (
                _wo31d_transition(
                    1,
                    1,
                    SessionState.CONTINUOUS,
                    "WO31D_POST_UNCROSS_CONTINUOUS",
                ),
                _wo31d_submit(
                    2,
                    2,
                    _wo31d_order("POST-BID", Side.BUY, 70, price_ticks=99),
                ),
            ),
            3,
        )
    )

    partial = MarketMechanicsEngine()
    _wo31d_open_continuous(partial, "WO31D_PARTIAL_OPEN")
    partial.submit(
        _wo31d_order("PARTIAL-MAKER", Side.SELL, 100, price_ticks=101)
    )
    partial.submit(
        _wo31d_order(
            "PARTIAL-PLAYER",
            Side.BUY,
            40,
            owner=OrderOwner.PLAYER,
        )
    )
    fixtures.append(
        (
            "partial_fill",
            partial,
            (
                _wo31d_submit(
                    1,
                    1,
                    _wo31d_order("PARTIAL-FINISH", Side.BUY, 60),
                ),
                _wo31d_submit(
                    2,
                    2,
                    _wo31d_order("PARTIAL-BID", Side.BUY, 25, price_ticks=99),
                ),
            ),
            3,
        )
    )

    working_player = MarketMechanicsEngine()
    _wo31d_open_continuous(working_player, "WO31D_PLAYER_OPEN")
    working_player.submit(
        _wo31d_order(
            "WORKING-PLAYER",
            Side.BUY,
            50,
            price_ticks=100,
            owner=OrderOwner.PLAYER,
        )
    )
    fixtures.append(
        (
            "working_player_order",
            working_player,
            (
                _wo31d_submit(
                    1,
                    1,
                    _wo31d_order("PLAYER-HIT", Side.SELL, 20),
                ),
            ),
            2,
        )
    )

    fifo = MarketMechanicsEngine()
    _wo31d_open_continuous(fifo, "WO31D_FIFO_OPEN")
    fifo.submit(
        _wo31d_order("FIFO-AHEAD", Side.BUY, 1_100, price_ticks=100)
    )
    fifo.submit(
        _wo31d_order(
            "FIFO-PLAYER",
            Side.BUY,
            500,
            price_ticks=100,
            owner=OrderOwner.PLAYER,
        )
    )
    fixtures.append(
        (
            "queued_fifo_depth",
            fifo,
            (
                _wo31d_submit(
                    1,
                    1,
                    _wo31d_order("FIFO-AGGRESSOR", Side.SELL, 1_200),
                ),
            ),
            2,
        )
    )

    halted = MarketMechanicsEngine()
    _wo31d_open_continuous(halted, "WO31D_HALT_OPEN")
    halted.submit(_wo31d_order("HALT-BID", Side.BUY, 90, price_ticks=99))
    halted.transition_session(SessionState.HALTED, reason="WO31D_HALT")
    fixtures.append(
        (
            "halt",
            halted,
            (
                _wo31d_transition(
                    1,
                    1,
                    SessionState.REOPENING_AUCTION,
                    "WO31D_REOPEN_CALL",
                ),
                _wo31d_submit(
                    2,
                    2,
                    _wo31d_order(
                        "HALT-AUC-BUY",
                        Side.BUY,
                        30,
                        price_ticks=101,
                        auction_only=True,
                    ),
                ),
                _wo31d_submit(
                    3,
                    2,
                    _wo31d_order(
                        "HALT-AUC-SELL",
                        Side.SELL,
                        30,
                        price_ticks=100,
                        auction_only=True,
                    ),
                ),
                _wo31d_command(4, 3, "UNCROSS", {}),
                _wo31d_transition(
                    5,
                    4,
                    SessionState.CONTINUOUS,
                    "WO31D_REOPEN_COMPLETE",
                ),
            ),
            5,
        )
    )

    reopened = MarketMechanicsEngine()
    _wo31d_open_continuous(reopened, "WO31D_REOPEN_PREFIX")
    reopened.submit(
        _wo31d_order("REOPEN-ASK", Side.SELL, 80, price_ticks=102)
    )
    reopened.transition_session(SessionState.HALTED, reason="WO31D_REOPEN_HALT")
    reopened.transition_session(
        SessionState.REOPENING_AUCTION,
        reason="WO31D_REOPEN_CALL",
    )
    reopened.submit(
        _wo31d_order(
            "REOPEN-AUC-BUY",
            Side.BUY,
            25,
            price_ticks=101,
            auction_only=True,
        )
    )
    reopened.submit(
        _wo31d_order(
            "REOPEN-AUC-SELL",
            Side.SELL,
            25,
            price_ticks=100,
            auction_only=True,
        )
    )
    reopened.uncross_auction()
    reopened.transition_session(
        SessionState.CONTINUOUS,
        reason="WO31D_REOPEN_COMPLETE",
    )
    fixtures.append(
        (
            "reopen",
            reopened,
            (
                _wo31d_submit(
                    1,
                    1,
                    _wo31d_order(
                        "REOPEN-PLAYER",
                        Side.BUY,
                        20,
                        owner=OrderOwner.PLAYER,
                    ),
                ),
            ),
            2,
        )
    )
    return tuple(fixtures)


def _wo31d_run_worker(raw: bytes):
    """Run the documented worker from an empty directory in a new interpreter."""

    import subprocess
    import sys
    from pathlib import Path
    from tempfile import TemporaryDirectory

    repository = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    prior_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(repository)
        if not prior_path
        else str(repository) + os.pathsep + prior_path
    )
    environment.pop("PYTHONPYCACHEPREFIX", None)
    with TemporaryDirectory(prefix="kirby2-wo31d-worker-") as temporary:
        directory = Path(temporary)
        before = tuple(sorted(str(path.relative_to(directory)) for path in directory.rglob("*")))
        completed = subprocess.run(
            [sys.executable, "-m", "kirby2.full_day.restore_worker"],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=directory,
            env=environment,
            check=False,
            timeout=20,
        )
        after = tuple(sorted(str(path.relative_to(directory)) for path in directory.rglob("*")))
    return completed.returncode, completed.stdout, completed.stderr, before != after


def _wo31d_boundary_case(
    boundary: str,
    engine: MarketMechanicsEngine,
    raw_commands: tuple[object, ...],
    completed_time_us: int,
) -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes, parse_canonical_json_object
    from kirby2.full_day.restore import (
        CORE_RESTORE_REQUEST_FORMAT_ID,
        CORE_RESTORE_REQUEST_SCHEMA_VERSION,
        CoreRestoreRequestV1,
        CoreSessionCheckpointV1,
        CoreSessionCommandV1,
        execute_uninterrupted_suffix,
    )

    failures: list[str] = []
    commands = tuple(raw_commands)
    if any(type(command) is not CoreSessionCommandV1 for command in commands):
        failures.append("boundary fixture contains a noncanonical suffix command")
        return FullDayAuditCase(
            f"core_restore_{boundary}",
            f"boundary={boundary} fixture=invalid",
            tuple(failures),
        )
    checkpoint = CoreSessionCheckpointV1.capture(engine)
    request = CoreRestoreRequestV1(
        schema_version=CORE_RESTORE_REQUEST_SCHEMA_VERSION,
        format_id=CORE_RESTORE_REQUEST_FORMAT_ID,
        checkpoint=checkpoint,
        suffix_commands=commands,
        completed_time_us=completed_time_us,
    )
    uninterrupted = execute_uninterrupted_suffix(
        engine,
        checkpoint,
        commands,
        completed_time_us=completed_time_us,
    )
    expected = canonical_json_bytes(uninterrupted)
    returncode, stdout, stderr, wrote_files = _wo31d_run_worker(
        request.canonical_bytes()
    )
    if returncode != 0:
        failures.append(
            f"fresh worker exited {returncode}: "
            f"{stderr.decode('utf-8', errors='backslashreplace').strip()}"
        )
    if stderr:
        failures.append("successful fresh worker wrote diagnostics to stderr")
    if wrote_files:
        failures.append("fresh worker changed its empty working directory")
    if stdout != expected:
        failures.append("fresh restore result differs byte-for-byte from uninterrupted suffix")
    try:
        parsed = parse_canonical_json_object(stdout)
    except (TypeError, ValueError) as error:
        failures.append(f"fresh worker stdout is not one canonical JSON object: {error}")
        parsed = {}
    prefix = parsed.get("prefix")
    if not isinstance(prefix, dict) or set(prefix) != {
        "local_event_count",
        "outer_event_count",
        "sha256",
    }:
        failures.append("worker result re-emits prefix state instead of digest metadata")
    checkpoint_payload = checkpoint.as_dict()["engine_state"]
    assert isinstance(checkpoint_payload, dict)
    checkpoint_book = checkpoint_payload["book"]
    checkpoint_auction = checkpoint_payload["auction"]
    assert isinstance(checkpoint_book, dict) and isinstance(checkpoint_auction, dict)
    prefix_continuous_fills = checkpoint_book["fills"]
    prefix_auction_executions = checkpoint_auction["executions"]
    assert isinstance(prefix_continuous_fills, list)
    assert isinstance(prefix_auction_executions, list)
    final = uninterrupted["final"]
    assert isinstance(final, dict)
    explicit_fills = final["fills"]
    assert isinstance(explicit_fills, dict)
    suffix_continuous_fills = explicit_fills["continuous"]
    suffix_auction_executions = explicit_fills["auction"]
    assert isinstance(suffix_continuous_fills, list)
    assert isinstance(suffix_auction_executions, list)
    prefix_continuous_trade_ids = {
        row["trade_id"] for row in prefix_continuous_fills if isinstance(row, dict)
    }
    prefix_auction_trade_ids = {
        row["trade_id"] for row in prefix_auction_executions if isinstance(row, dict)
    }
    if any(
        isinstance(row, dict) and row.get("trade_id") in prefix_continuous_trade_ids
        for row in suffix_continuous_fills
    ):
        failures.append("explicit continuous fills re-emit checkpoint-prefix fills")
    if any(
        isinstance(row, dict) and row.get("trade_id") in prefix_auction_trade_ids
        for row in suffix_auction_executions
    ):
        failures.append("explicit auction fills re-emit checkpoint-prefix executions")
    final_book = final["book_state"]
    final_auction = final["auction_state"]
    final_position = final["player_position_state"]
    assert isinstance(final_book, dict)
    assert isinstance(final_auction, dict)
    assert isinstance(final_position, dict)
    if {"fills", "journal", "player_position", "trades"} & set(final_book):
        failures.append("final book projection re-emits prefix history rows")
    if (
        {"executions", "uncross_history"} & set(final_auction)
        or "fills" in final_position
    ):
        failures.append("final auction or position projection re-emits prefix rows")
    suffix = uninterrupted["suffix"]
    assert isinstance(suffix, dict)
    outer_events = suffix["outer_events"]
    local_events = suffix["local_events"]
    assert isinstance(outer_events, list) and isinstance(local_events, list)
    return FullDayAuditCase(
        f"core_restore_{boundary}",
        (
            f"boundary={boundary} fresh_process=executed "
            f"suffix_outer_events={len(outer_events)} "
            f"suffix_local_events={len(local_events)} "
            f"result_bytes={len(expected)} "
            f"result_sha256={hashlib.sha256(expected).hexdigest()}"
        ),
        tuple(failures),
    )


def _wo31d_hostile_fixture():
    """Return a checkpoint containing FIFO depth and a player fill history."""

    from kirby2.full_day.restore import CoreSessionCheckpointV1

    engine = MarketMechanicsEngine()
    _wo31d_open_continuous(engine, "WO31D_HOSTILE_OPEN")
    engine.submit(_wo31d_order("HOST-AHEAD", Side.BUY, 100, price_ticks=100))
    engine.submit(
        _wo31d_order(
            "HOST-PLAYER",
            Side.BUY,
            50,
            price_ticks=100,
            owner=OrderOwner.PLAYER,
        )
    )
    engine.submit(_wo31d_order("HOST-BEHIND", Side.BUY, 50, price_ticks=100))
    engine.submit(_wo31d_order("HOST-HIT", Side.SELL, 120))
    return CoreSessionCheckpointV1.capture(engine)


def _wo31d_request_payload(checkpoint_payload: dict[str, object]) -> dict[str, object]:
    from kirby2.full_day.restore import (
        CORE_RESTORE_REQUEST_FORMAT_ID,
        CORE_RESTORE_REQUEST_SCHEMA_VERSION,
    )

    clock = checkpoint_payload["engine_state"]
    assert isinstance(clock, dict)
    clock = clock["clock"]
    assert isinstance(clock, dict)
    current_time = clock["current_time_us"]
    assert type(current_time) is int
    return {
        "checkpoint": checkpoint_payload,
        "completed_time_us": current_time,
        "format_id": CORE_RESTORE_REQUEST_FORMAT_ID,
        "schema_version": CORE_RESTORE_REQUEST_SCHEMA_VERSION,
        "suffix_commands": [],
    }


def _wo31d_rebind_engine_digest(checkpoint_payload: dict[str, object]) -> None:
    from kirby2.full_day.models import canonical_sha256

    state = checkpoint_payload["engine_state"]
    assert isinstance(state, dict)
    checkpoint_payload["engine_state_sha256"] = canonical_sha256(state)


def _wo31d_rebind_prefix_digest(checkpoint_payload: dict[str, object]) -> None:
    from kirby2.full_day.models import canonical_sha256

    state = checkpoint_payload["engine_state"]
    assert isinstance(state, dict)
    book = state["book"]
    assert isinstance(book, dict)
    journal = book["journal"]
    assert isinstance(journal, dict)
    local_events = journal["events"]
    outer_events = state["events"]
    assert isinstance(local_events, list) and isinstance(outer_events, list)
    checkpoint_payload["prefix_local_event_count"] = len(local_events)
    checkpoint_payload["prefix_outer_event_count"] = len(outer_events)
    checkpoint_payload["prefix_sha256"] = canonical_sha256(
        {"local_events": local_events, "outer_events": outer_events}
    )


def _wo31d_refusal_case() -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes

    checkpoint = _wo31d_hostile_fixture()
    refusals: list[tuple[str, bytes]] = []

    duplicate = copy.deepcopy(checkpoint.as_dict())
    duplicate_state = duplicate["engine_state"]
    assert isinstance(duplicate_state, dict)
    duplicate_orders = duplicate_state["managed_orders"]
    assert isinstance(duplicate_orders, list) and duplicate_orders
    duplicate_orders.append(copy.deepcopy(duplicate_orders[0]))
    _wo31d_rebind_engine_digest(duplicate)
    refusals.append(
        ("duplicate_ids", canonical_json_bytes(_wo31d_request_payload(duplicate)))
    )

    fifo = copy.deepcopy(checkpoint.as_dict())
    fifo_state = fifo["engine_state"]
    assert isinstance(fifo_state, dict)
    fifo_book = fifo_state["book"]
    assert isinstance(fifo_book, dict)
    bid_levels = fifo_book["bid_levels"]
    assert isinstance(bid_levels, list) and bid_levels
    level = bid_levels[0]
    assert isinstance(level, dict)
    order_ids = level["order_ids"]
    assert isinstance(order_ids, list) and len(order_ids) >= 2
    order_ids[0], order_ids[1] = order_ids[1], order_ids[0]
    _wo31d_rebind_engine_digest(fifo)
    refusals.append(("corrupt_fifo", canonical_json_bytes(_wo31d_request_payload(fifo))))

    fill_history = copy.deepcopy(checkpoint.as_dict())
    fill_state = fill_history["engine_state"]
    assert isinstance(fill_state, dict)
    fill_book = fill_state["book"]
    assert isinstance(fill_book, dict)
    position = fill_book["player_position"]
    assert isinstance(position, dict)
    player_fills = position["fills"]
    assert isinstance(player_fills, list) and player_fills
    player_fill = player_fills[0]
    assert isinstance(player_fill, dict)
    player_fill["quantity"] = int(player_fill["quantity"]) + 1
    position["bought_quantity"] = int(position["bought_quantity"]) + 1
    position["position"] = int(position["position"]) + 1
    _wo31d_rebind_engine_digest(fill_history)
    refusals.append(
        (
            "mutated_fill_history",
            canonical_json_bytes(_wo31d_request_payload(fill_history)),
        )
    )

    allocator = copy.deepcopy(checkpoint.as_dict())
    allocator_state = allocator["engine_state"]
    assert isinstance(allocator_state, dict)
    allocators = allocator_state["allocators"]
    assert isinstance(allocators, dict)
    allocators["arrival_sequence"] = int(allocators["arrival_sequence"]) - 1
    _wo31d_rebind_engine_digest(allocator)
    refusals.append(
        ("bad_allocator", canonical_json_bytes(_wo31d_request_payload(allocator)))
    )

    historical_engine = MarketMechanicsEngine()
    _wo31d_open_continuous(historical_engine, "WO31D_HISTORICAL_FIFO_OPEN")
    historical_engine.submit(
        _wo31d_order("HIST-ASK-1", Side.SELL, 10, price_ticks=101)
    )
    historical_engine.submit(
        _wo31d_order("HIST-ASK-2", Side.SELL, 10, price_ticks=101)
    )
    historical_engine.submit(_wo31d_order("HIST-BUY", Side.BUY, 20))
    from kirby2.full_day.restore import CoreSessionCheckpointV1

    historical_fifo = copy.deepcopy(
        CoreSessionCheckpointV1.capture(historical_engine).as_dict()
    )
    historical_state = historical_fifo["engine_state"]
    assert isinstance(historical_state, dict)
    historical_book = historical_state["book"]
    assert isinstance(historical_book, dict)
    historical_orders = historical_book["orders"]
    assert isinstance(historical_orders, list)
    historical_by_id = {
        row["order_id"]: row for row in historical_orders if isinstance(row, dict)
    }
    first_historical = historical_by_id["HIST-ASK-1"]
    second_historical = historical_by_id["HIST-ASK-2"]
    first_historical["resting_sequence"], second_historical["resting_sequence"] = (
        second_historical["resting_sequence"],
        first_historical["resting_sequence"],
    )
    _wo31d_rebind_engine_digest(historical_fifo)
    refusals.append(
        (
            "historical_fifo_forgery",
            canonical_json_bytes(_wo31d_request_payload(historical_fifo)),
        )
    )

    cancel_engine = MarketMechanicsEngine()
    _wo31d_open_continuous(cancel_engine, "WO31D_CANCEL_TARGET_OPEN")
    cancel_engine.submit(
        _wo31d_order("TARGET-ONE", Side.BUY, 10, price_ticks=99)
    )
    cancel_engine.submit(
        _wo31d_order("TARGET-TWO", Side.BUY, 10, price_ticks=98)
    )
    cancel_engine.cancel("TARGET-ONE", reason="WO31D_TARGET_CANCEL")
    cancel_target = copy.deepcopy(
        CoreSessionCheckpointV1.capture(cancel_engine).as_dict()
    )
    cancel_state = cancel_target["engine_state"]
    assert isinstance(cancel_state, dict)
    cancel_book = cancel_state["book"]
    assert isinstance(cancel_book, dict)
    cancel_orders = cancel_book["orders"]
    assert isinstance(cancel_orders, list)
    command_rows = [
        row
        for row in cancel_orders
        if isinstance(row, dict) and row.get("order_type") == "cancel"
    ]
    assert len(command_rows) == 1
    command_rows[0]["cancel_target_id"] = "TARGET-TWO"
    _wo31d_rebind_engine_digest(cancel_target)
    refusals.append(
        (
            "cancel_target_forgery",
            canonical_json_bytes(_wo31d_request_payload(cancel_target)),
        )
    )

    allocation_engine = MarketMechanicsEngine()
    allocation_engine.transition_session(
        SessionState.PREOPEN,
        reason="WO31D_ALLOCATION_OPEN",
    )
    allocation_engine.transition_session(
        SessionState.OPENING_AUCTION,
        reason="WO31D_ALLOCATION_OPEN",
    )
    for request in (
        _wo31d_order(
            "ALLOC-BUY-1",
            Side.BUY,
            10,
            price_ticks=101,
            auction_only=True,
        ),
        _wo31d_order(
            "ALLOC-BUY-2",
            Side.BUY,
            10,
            price_ticks=101,
            auction_only=True,
        ),
        _wo31d_order(
            "ALLOC-SELL",
            Side.SELL,
            20,
            price_ticks=100,
            auction_only=True,
        ),
    ):
        allocation_engine.submit(request)
    allocation_engine.uncross_auction()
    allocation = copy.deepcopy(
        CoreSessionCheckpointV1.capture(allocation_engine).as_dict()
    )
    allocation_state = allocation["engine_state"]
    assert isinstance(allocation_state, dict)
    allocation_auction = allocation_state["auction"]
    assert isinstance(allocation_auction, dict)
    allocation_executions = allocation_auction["executions"]
    allocation_history = allocation_auction["uncross_history"]
    assert isinstance(allocation_executions, list) and len(allocation_executions) == 2
    assert isinstance(allocation_history, list) and len(allocation_history) == 1
    first_execution, second_execution = allocation_executions
    assert isinstance(first_execution, dict) and isinstance(second_execution, dict)
    first_execution["buy_order_id"], second_execution["buy_order_id"] = (
        second_execution["buy_order_id"],
        first_execution["buy_order_id"],
    )
    history_row = allocation_history[0]
    assert isinstance(history_row, dict)
    history_result = history_row["result"]
    assert isinstance(history_result, dict)
    history_executions = history_result["executions"]
    assert isinstance(history_executions, list) and len(history_executions) == 2
    first_history, second_history = history_executions
    assert isinstance(first_history, dict) and isinstance(second_history, dict)
    first_history["buy_order_id"], second_history["buy_order_id"] = (
        second_history["buy_order_id"],
        first_history["buy_order_id"],
    )
    _wo31d_rebind_engine_digest(allocation)
    refusals.append(
        (
            "auction_allocation_forgery",
            canonical_json_bytes(_wo31d_request_payload(allocation)),
        )
    )

    terminal_engine = MarketMechanicsEngine()
    _wo31d_open_continuous(terminal_engine, "WO31D_TERMINAL_OPEN")
    terminal = copy.deepcopy(
        CoreSessionCheckpointV1.capture(terminal_engine).as_dict()
    )
    terminal_state = terminal["engine_state"]
    assert isinstance(terminal_state, dict)
    terminal_state["session_state"] = SessionState.HALTED.value
    _wo31d_rebind_engine_digest(terminal)
    refusals.append(
        (
            "terminal_session_forgery",
            canonical_json_bytes(_wo31d_request_payload(terminal)),
        )
    )

    from kirby2.exchange.mechanics_models import ScheduledSessionState

    scheduled_rules = replace(
        InstrumentRules(),
        session_schedule=SessionSchedule(
            (ScheduledSessionState(0, SessionState.PREOPEN),)
        ),
    )
    scheduled_engine = MarketMechanicsEngine(scheduled_rules)
    scheduled_engine.advance_to(0)
    schedule_cursor = copy.deepcopy(
        CoreSessionCheckpointV1.capture(scheduled_engine).as_dict()
    )
    schedule_state = schedule_cursor["engine_state"]
    assert isinstance(schedule_state, dict)
    schedule_state["schedule_index"] = 0
    _wo31d_rebind_engine_digest(schedule_cursor)
    refusals.append(
        (
            "equal_time_schedule_rollback",
            canonical_json_bytes(_wo31d_request_payload(schedule_cursor)),
        )
    )

    rejected_engine = MarketMechanicsEngine()
    rejected_engine.submit(
        _wo31d_order("REJECTED-LIFECYCLE", Side.BUY, 10, price_ticks=99)
    )
    rejected = copy.deepcopy(
        CoreSessionCheckpointV1.capture(rejected_engine).as_dict()
    )
    rejected_state = rejected["engine_state"]
    assert isinstance(rejected_state, dict)
    rejected_orders = rejected_state["managed_orders"]
    assert isinstance(rejected_orders, list) and len(rejected_orders) == 1
    rejected_row = rejected_orders[0]
    assert isinstance(rejected_row, dict)
    rejected_row["filled_quantity"] = 5
    rejected_row["remaining_quantity"] = 5
    rejected_row["resting_sequence"] = 1
    _wo31d_rebind_engine_digest(rejected)
    refusals.append(
        (
            "rejected_lifecycle_forgery",
            canonical_json_bytes(_wo31d_request_payload(rejected)),
        )
    )

    duplicate_event = copy.deepcopy(
        CoreSessionCheckpointV1.capture(terminal_engine).as_dict()
    )
    duplicate_event_state = duplicate_event["engine_state"]
    assert isinstance(duplicate_event_state, dict)
    duplicate_events = duplicate_event_state["events"]
    assert isinstance(duplicate_events, list) and duplicate_events
    forged_event = copy.deepcopy(duplicate_events[-1])
    assert isinstance(forged_event, dict)
    forged_event["sequence"] = len(duplicate_events) + 1
    duplicate_events.append(forged_event)
    _wo31d_rebind_engine_digest(duplicate_event)
    _wo31d_rebind_prefix_digest(duplicate_event)
    refusals.append(
        (
            "semantic_event_duplication",
            canonical_json_bytes(_wo31d_request_payload(duplicate_event)),
        )
    )

    closure_engine = MarketMechanicsEngine()
    closure_engine.transition_session(
        SessionState.PREOPEN,
        reason="WO31D_CLOSURE_OPEN",
    )
    closure_engine.transition_session(
        SessionState.OPENING_AUCTION,
        reason="WO31D_CLOSURE_OPEN",
    )
    closure_engine.submit(
        _wo31d_order(
            "CLOSURE-AUCTION",
            Side.BUY,
            10,
            price_ticks=100,
            auction_only=True,
        )
    )
    closure_engine.cancel("CLOSURE-AUCTION", reason="WO31D_USER_CANCEL")
    closure = copy.deepcopy(
        CoreSessionCheckpointV1.capture(closure_engine).as_dict()
    )
    closure_state = closure["engine_state"]
    assert isinstance(closure_state, dict)
    closure_managed = closure_state["managed_orders"]
    closure_auction = closure_state["auction"]
    assert isinstance(closure_managed, list) and len(closure_managed) == 1
    assert isinstance(closure_auction, dict)
    closure_orders = closure_auction["orders"]
    assert isinstance(closure_orders, list) and len(closure_orders) == 1
    assert isinstance(closure_managed[0], dict) and isinstance(closure_orders[0], dict)
    closure_managed[0]["status"] = "CANCELLED_STP"
    closure_orders[0]["status"] = "CANCELLED_STP"
    _wo31d_rebind_engine_digest(closure)
    refusals.append(
        (
            "auction_closure_reason_forgery",
            canonical_json_bytes(_wo31d_request_payload(closure)),
        )
    )

    canonical_request = canonical_json_bytes(
        _wo31d_request_payload(copy.deepcopy(checkpoint.as_dict()))
    )
    refusals.append(("noncanonical_wire", b" " + canonical_request))

    regeneration = _wo31d_request_payload(copy.deepcopy(checkpoint.as_dict()))
    regeneration["seed"] = 42
    regeneration["prefix_commands"] = []
    refusals.append(("prefix_regeneration", canonical_json_bytes(regeneration)))

    failures: list[str] = []
    refusal_count = 0
    for label, raw in refusals:
        returncode, stdout, stderr, wrote_files = _wo31d_run_worker(raw)
        if returncode == 0:
            failures.append(f"{label} was accepted by the fresh worker")
        else:
            refusal_count += 1
        if stdout:
            failures.append(f"{label} refusal wrote a result to stdout")
        if not stderr:
            failures.append(f"{label} refusal omitted its stderr diagnostic")
        if wrote_files:
            failures.append(f"{label} refusal changed the worker directory")
    return FullDayAuditCase(
        "core_restore_hostile_refusals",
        (
            f"hostile_refusals={refusal_count} duplicate_ids=refused "
            "corrupt_fifo=refused mutated_fill_history=refused "
            "bad_allocator=refused noncanonical_wire=refused "
            "prefix_regeneration=refused historical_fifo=refused "
            "cancel_target=refused auction_allocation=refused "
            "terminal_session=refused schedule_cursor=refused "
            "rejected_lifecycle=refused duplicate_event=refused "
            "auction_closure_reason=refused"
        ),
        tuple(failures),
    )


def _wo31d_protocol_case() -> FullDayAuditCase:
    from pathlib import Path

    from kirby2.full_day.restore import (
        CORE_RNG_STATE_ABSENT,
        CoreSessionCheckpointV1,
    )

    failures: list[str] = []
    engine = MarketMechanicsEngine()
    _wo31d_open_continuous(engine, "WO31D_PROTOCOL_OPEN")
    checkpoint = CoreSessionCheckpointV1.capture(engine)
    if checkpoint.core_rng_state != CORE_RNG_STATE_ABSENT:
        failures.append("core checkpoint invented RNG state")
    worker_source = (
        Path(__file__).resolve().parents[1] / "full_day" / "restore_worker.py"
    ).read_text(encoding="utf-8")
    forbidden_calls = ("open(", "write_text(", "write_bytes(", "Path(")
    if any(token in worker_source for token in forbidden_calls):
        failures.append("restore worker source exposes a filesystem-write surface")
    if "seed" in {
        key.lower() for key in _wo31d_request_payload(checkpoint.as_dict())
    }:
        failures.append("restore request exposes seed-based prefix regeneration")
    return FullDayAuditCase(
        "core_restore_worker_protocol_scope",
        (
            "stdin=one_canonical_request stdout=one_canonical_result "
            "stderr=diagnostics_only filesystem_writes=absent "
            "prefix_fixture_import=absent core_rng=ABSENT "
            "scope=single_venue_market_mechanics"
        ),
        tuple(failures),
    )


def audit_wo31d_core_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise core restoration in fresh interpreters at all fixed boundaries."""

    boundary_cases = tuple(
        _wo31d_boundary_case(boundary, engine, commands, completed_time_us)
        for boundary, engine, commands, completed_time_us in _wo31d_boundary_fixtures()
    )
    return boundary_cases + (
        _wo31d_refusal_case(),
        _wo31d_protocol_case(),
    )


def _wo31e1_plan():
    """Return the small executable E1 fixture over the frozen WO31-A plan."""

    from kirby2.full_day.composition import (
        executable_agent_mechanics_composition_matrix,
    )
    from kirby2.full_day.models import (
        FlowSideV1,
        IntegerParameterUnitV1,
        NamedIntegerParameterV1,
        ParticipantScheduleActionV1,
        ParticipantScheduleEntryV1,
        ScheduledEventTypeV1,
        ScheduledEventV1,
        VersionedReferenceV1,
        canonical_sha256,
    )

    base = _sample_plan()
    matrix = executable_agent_mechanics_composition_matrix()
    population = _wo31e1_population(base)
    specs_by_id = {spec.agent_id: spec for spec in population.agents}
    spec_references = {
        participant.participant_id: VersionedReferenceV1(
            participant.specification.reference_id,
            participant.specification.version,
            canonical_sha256(
                specs_by_id[participant.participant_id].identity_dict()
            ),
        )
        for participant in base.participant_definitions
    }
    participant_definitions = tuple(
        replace(
            participant,
            specification=spec_references[participant.participant_id],
        )
        for participant in base.participant_definitions
    )
    original_agent_references = {
        participant.specification: spec_references[participant.participant_id]
        for participant in base.participant_definitions
    }
    component_configurations = tuple(
        sorted(
            (
                replace(
                    binding,
                    configuration=original_agent_references.get(
                        binding.configuration, binding.configuration
                    ),
                )
                for binding in base.component_configurations
            ),
            key=lambda binding: binding.sort_key,
        )
    )
    phases = {phase.phase_id: phase for phase in base.calendar.phases}
    opening_start = phases["OPENING_AUCTION"].start.simulation_time_us
    continuous_start = phases["CONTINUOUS"].start.simulation_time_us
    participant_by_id = {
        participant.participant_id: participant
        for participant in participant_definitions
    }
    parameter = NamedIntegerParameterV1
    scheduled_events = tuple(
        sorted(
            (
                ScheduledEventV1(
                    "E1_AUCTION_IMBALANCE",
                    opening_start + 10,
                    ScheduledEventTypeV1.AUCTION_IMBALANCE_PUBLICATION,
                    1,
                    FlowSideV1.BUY,
                    (
                        parameter(
                            "imbalance_shares",
                            IntegerParameterUnitV1.SHARES,
                            20,
                        ),
                    ),
                    None,
                    base.halt_reopen_rules.halt_trigger_reference,
                ),
                ScheduledEventV1(
                    "E1_ACTIVE_METAORDER",
                    continuous_start + 12,
                    ScheduledEventTypeV1.LARGE_SCHEDULED_METAORDER,
                    1,
                    FlowSideV1.BUY,
                    (
                        parameter(
                            "duration_us",
                            IntegerParameterUnitV1.MICROSECONDS,
                            1_000_000_000,
                        ),
                        parameter(
                            "participation_ppm",
                            IntegerParameterUnitV1.PPM,
                            250_000,
                        ),
                        parameter(
                            "quantity_shares",
                            IntegerParameterUnitV1.SHARES,
                            60,
                        ),
                    ),
                    participant_by_id["AUDIT_METAORDER"].specification,
                    None,
                ),
                ScheduledEventV1(
                    "E1_HALT",
                    continuous_start + 200,
                    ScheduledEventTypeV1.HALT,
                    1,
                    FlowSideV1.NONE,
                    (
                        parameter(
                            "halt_duration_us",
                            IntegerParameterUnitV1.MICROSECONDS,
                            20,
                        ),
                    ),
                    None,
                    base.halt_reopen_rules.halt_trigger_reference,
                ),
                ScheduledEventV1(
                    "E1_REOPEN",
                    continuous_start + 220,
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
                    base.halt_reopen_rules.resume_trigger_reference,
                ),
            ),
            key=lambda event: (event.simulation_time_us, event.event_id),
        )
    )
    participant_schedule = tuple(
        sorted(
            (
                ParticipantScheduleEntryV1(
                    "E1_MAKER_ACTIVATE",
                    100,
                    "AUDIT_MAKER",
                    ParticipantScheduleActionV1.ACTIVATE,
                    None,
                ),
                ParticipantScheduleEntryV1(
                    "E1_METAORDER_ACTIVATE",
                    continuous_start + 10,
                    "AUDIT_METAORDER",
                    ParticipantScheduleActionV1.ACTIVATE,
                    None,
                ),
                ParticipantScheduleEntryV1(
                    "E1_MAKER_WITHDRAW",
                    continuous_start + 500,
                    "AUDIT_MAKER",
                    ParticipantScheduleActionV1.DEACTIVATE,
                    None,
                ),
            ),
            key=lambda entry: (entry.simulation_time_us, entry.schedule_id),
        )
    )
    return replace(
        base,
        composition_profile=VersionedReferenceV1(
            "SINGLE_VENUE_AGENT_MECHANICS_V1",
            2,
            matrix.sha256,
        ),
        component_configurations=component_configurations,
        participant_definitions=participant_definitions,
        participant_schedule=participant_schedule,
        scheduled_events=scheduled_events,
    )


def _wo31e1_population(plan):
    from kirby2.agents.models import AgentFamily, PopulationDefinition
    from kirby2.agents.populations import _spec

    end_us = plan.calendar.end_time_us
    continuous_start = next(
        phase.start.simulation_time_us
        for phase in plan.calendar.phases
        if phase.phase_id == "CONTINUOUS"
    )
    maker = _spec(
        "AUDIT_MAKER",
        AgentFamily.PASSIVE_MARKET_MAKER,
        end_us,
        budget=100,
        working=100,
        max_order=20,
        clip=20,
        rate=1,
        latency_us=5,
        interval_us=1_000_000_000,
    )
    metaorder = _spec(
        "AUDIT_METAORDER",
        AgentFamily.SCHEDULED_METAORDER,
        end_us,
        side=Side.BUY,
        activation_us=continuous_start + 10,
        budget=60,
        working=60,
        max_order=20,
        clip=20,
        rate=1,
        latency_us=5,
        interval_us=1_000_000_000,
    )
    return PopulationDefinition(
        "WO31_E1_AUDIT_POPULATION_V1",
        "Two bounded synthetic participants for composed restore evidence.",
        (maker, metaorder),
        end_us,
        initial_mid_ticks=10_000,
        initial_depth_levels=1,
        initial_level_quantity=20,
    )


def _wo31e1_runtime():
    from kirby2.full_day.runtime import FullDayRuntime

    plan = _wo31e1_plan()
    return FullDayRuntime.create_with_agent_scheduler(
        plan,
        _wo31e1_population(plan),
    )


def _wo31e1_limit(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
    *,
    auction_only: bool = False,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id=order_id,
        side=side,
        quantity=quantity,
        instruction=OrderInstruction.LIMIT,
        owner=OrderOwner.SIMULATED,
        account_id="WO31-E1-AUDIT-LIQUIDITY",
        price_ticks=price_ticks,
        time_in_force=OrderInstruction.DAY,
        auction_only=auction_only,
    )


def _wo31e1_times(runtime) -> tuple[int, int]:
    phases = {phase.phase_id: phase for phase in runtime.plan.calendar.phases}
    return (
        phases["OPENING_AUCTION"].start.simulation_time_us,
        phases["CONTINUOUS"].start.simulation_time_us,
    )


def _wo31e1_prepare_boundary(boundary: str):
    runtime = _wo31e1_runtime()
    opening_start, continuous_start = _wo31e1_times(runtime)
    if boundary in {"auction_imbalance", "auction_uncross"}:
        runtime.advance_to(0)
        runtime.submit_request(
            _wo31e1_limit(
                "E1-AUCTION-BUY", Side.BUY, 50, 10_001, auction_only=True
            ),
            at_time_us=1,
        )
        runtime.submit_request(
            _wo31e1_limit(
                "E1-AUCTION-SELL", Side.SELL, 30, 9_999, auction_only=True
            ),
            at_time_us=1,
        )
        cut_time = (
            opening_start + 10
            if boundary == "auction_imbalance"
            else continuous_start
        )
    elif boundary == "participant_activation":
        cut_time = 100
    elif boundary in {
        "active_metaorder",
        "agent_inventories",
        "next_scheduled_decision",
        "agent_substream_state",
        "order_allocator",
    }:
        runtime.advance_to(continuous_start)
        runtime.submit_request(
            _wo31e1_limit("E1-META-LIQ-ASK", Side.SELL, 50, 10_001),
            at_time_us=continuous_start + 5,
        )
        cut_time = (
            continuous_start + 10
            if boundary
            in {
                "active_metaorder",
                "next_scheduled_decision",
                "agent_substream_state",
            }
            else continuous_start + 15
        )
    elif boundary in {"exchange_queues", "same_time_microsteps"}:
        runtime.advance_to(continuous_start)
        cut_time = continuous_start + 50
        for request in (
            _wo31e1_limit("E1-FIFO-BUY-1", Side.BUY, 10, 9_999),
            _wo31e1_limit("E1-FIFO-BUY-2", Side.BUY, 15, 9_999),
            _wo31e1_limit("E1-FIFO-ASK-1", Side.SELL, 12, 10_001),
        ):
            runtime.submit_request(request, at_time_us=cut_time)
            runtime.advance_to(cut_time)
    elif boundary == "halt":
        cut_time = continuous_start + 200
    elif boundary == "reopen":
        cut_time = continuous_start + 220
    elif boundary == "participant_withdrawal":
        cut_time = continuous_start + 500
    else:  # pragma: no cover - fixed inventory controls calls
        raise ValueError(f"unknown WO31-E1 boundary: {boundary}")
    runtime.advance_to(cut_time)
    runtime.capture_quiescent_cut(
        f"WO31-E1-{boundary.upper()}-CUT",
        at_time_us=cut_time,
    )
    return runtime, cut_time


def _wo31e1_boundary_probe(
    boundary: str,
    runtime,
    cut_time: int,
) -> tuple[str, ...]:
    failures: list[str] = []
    scheduler = runtime.agent_scheduler
    if scheduler is None:
        return ("active WO31-E1 fixture omitted AgentScheduler",)
    if boundary == "auction_imbalance":
        indication = runtime.engine.auction_indication()
        if (
            indication.matched_quantity != 30
            or indication.imbalance_quantity != 20
            or len(runtime.engine.auction.active_orders) != 2
        ):
            failures.append("auction imbalance/queue state is not preserved")
    elif boundary == "auction_uncross":
        if (
            runtime.engine.session_state is not SessionState.CONTINUOUS
            or not runtime.engine.auction.executions
            or runtime.engine.auction.active_orders
        ):
            failures.append("opening uncross did not preserve its exact result")
    elif boundary == "halt":
        if runtime.engine.session_state is not SessionState.HALTED:
            failures.append("halt cut is not HALTED")
    elif boundary == "reopen":
        if runtime.engine.session_state is not SessionState.REOPENING_AUCTION:
            failures.append("reopen cut is not REOPENING_AUCTION")
    elif boundary == "participant_activation":
        if scheduler._active.get("AUDIT_MAKER") is not True:
            failures.append("maker activation state is absent")
    elif boundary == "participant_withdrawal":
        if scheduler._active.get("AUDIT_MAKER") is not False:
            failures.append("maker withdrawal state is absent")
    elif boundary == "active_metaorder":
        if (
            scheduler._active.get("AUDIT_METAORDER") is not True
            or scheduler.next_pending_arrival_time_us != cut_time + 5
        ):
            failures.append("active metaorder/pending child is not exact")
    elif boundary == "agent_inventories":
        if scheduler.agents["AUDIT_METAORDER"].inventory != 20:
            failures.append("metaorder inventory does not reconcile to its fill")
    elif boundary == "next_scheduled_decision":
        if (
            scheduler.next_decision_time_us is None
            or scheduler.next_decision_time_us <= cut_time
        ):
            failures.append("next scheduled participant decision is absent")
    elif boundary == "agent_substream_state":
        state = scheduler.checkpoint_state()["state"]
        agent_state = state["agents"]["AUDIT_METAORDER"]
        if (
            state["rng_labels"]["AUDIT_METAORDER"]
            != "full_day/participant/audit_metaorder/decision"
            or not agent_state["rng"]["internal_state"]
        ):
            failures.append("labeled agent RNG substream state is absent")
    elif boundary == "order_allocator":
        if runtime._order_id_allocator.next_sequence != 2:
            failures.append("runtime order allocator did not preserve FD-O highwater")
    elif boundary == "exchange_queues":
        level = runtime.engine.book.bids.get(9_999)
        if level is None or tuple(
            order.order_id for order in level.orders
        ) != ("E1-FIFO-BUY-1", "E1-FIFO-BUY-2"):
            failures.append("continuous exchange FIFO queue is not exact")
    elif boundary == "same_time_microsteps":
        microsteps = tuple(
            sorted(
                {
                    key.microstep
                    for key in runtime.executed_work_items.values()
                    if key.simulation_time_us == cut_time
                    and key.stage_ordinal.value == 5
                }
            )
        )
        if microsteps[:3] != (0, 1, 2):
            failures.append("same-time venue work did not retain microsteps 0/1/2")
    return tuple(failures)


def _wo31e1_run_worker(raw: bytes):
    import subprocess
    import sys
    from pathlib import Path
    from tempfile import TemporaryDirectory

    repository = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    prior_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(repository)
        if not prior_path
        else str(repository) + os.pathsep + prior_path
    )
    environment.pop("PYTHONPYCACHEPREFIX", None)
    script = (
        "from kirby2.full_day.restore import "
        "full_day_runtime_restore_worker_main as main; "
        "raise SystemExit(main())"
    )
    with TemporaryDirectory(prefix="kirby2-wo31e1-worker-") as temporary:
        directory = Path(temporary)
        before = tuple(directory.rglob("*"))
        completed = subprocess.run(
            [sys.executable, "-c", script],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=directory,
            env=environment,
            check=False,
            timeout=30,
        )
        after = tuple(directory.rglob("*"))
    return completed.returncode, completed.stdout, completed.stderr, before != after


def _wo31e1_fresh_boundary_case(boundary: str) -> FullDayAuditCase:
    from kirby2.full_day.models import (
        canonical_json_bytes,
        parse_canonical_json_object,
    )
    from kirby2.full_day.restore import (
        FullDayRuntimeRestoreRequestV1,
        execute_uninterrupted_full_day_runtime_suffix,
    )

    runtime, cut_time = _wo31e1_prepare_boundary(boundary)
    failures = list(_wo31e1_boundary_probe(boundary, runtime, cut_time))
    target = cut_time + (10 if boundary == "reopen" else 1)
    request = FullDayRuntimeRestoreRequestV1.capture(
        runtime,
        suffix_targets_us=(target,),
        final_checkpoint_request_id=f"WO31-E1-{boundary.upper()}-FINAL",
    )
    raw = request.canonical_bytes()
    returncode, stdout, stderr, wrote_files = _wo31e1_run_worker(raw)
    expected = execute_uninterrupted_full_day_runtime_suffix(runtime, request)
    actual: dict[str, object] | None = None
    if returncode != 0:
        failures.append(
            f"fresh worker returned {returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    else:
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"fresh worker emitted noncanonical JSON: {error}")
    if stderr:
        failures.append("successful fresh worker wrote diagnostics to stderr")
    if wrote_files:
        failures.append("fresh worker wrote into its empty working directory")
    if actual is not None:
        if canonical_json_bytes(actual) != stdout:
            failures.append("fresh worker output is not its canonical byte form")
        if actual != expected:
            failures.append("fresh-process suffix differs from uninterrupted suffix")
        suffix = actual.get("suffix")
        if not isinstance(suffix, Mapping) or set(suffix) != {
            "agent_scheduler",
            "mechanics_event_bytes_sha256",
            "mechanics_events",
            "native_ledger",
            "native_ledger_bytes_sha256",
            "outer_event_bytes_sha256",
            "outer_events",
        }:
            failures.append("fresh result omits one composed replay ledger")
    digest = "ABSENT" if actual is None else str(actual["invariant_sha256"])
    return FullDayAuditCase(
        f"full_day_restore_{boundary}",
        (
            f"boundary={boundary} cut_us={cut_time} target_us={target} "
            f"fresh_process=true invariant_sha256={digest}"
        ),
        tuple(failures),
    )


def _wo31e1_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import (
        MarketMechanicsComponentAdapterV1,
    )
    from kirby2.full_day.composition import (
        AGENT_SCHEDULER_COMPONENT,
        FULL_DAY_RUNTIME_COMPONENT,
        INITIAL_PROFILE_ID,
        MECHANICS_COMPONENT,
        executable_agent_mechanics_composition_matrix,
        initial_composition_matrix,
    )

    failures: list[str] = []
    prior = initial_composition_matrix()
    matrix = executable_agent_mechanics_composition_matrix()
    if tuple(profile.as_dict() for profile in matrix.profiles[:-1]) != tuple(
        profile.as_dict() for profile in prior.profiles
    ):
        failures.append("composition matrix v2 rewrote immutable v1 rows")
    profile = matrix.profile(INITIAL_PROFILE_ID, 2)
    statuses = {
        component.component_id: (
            component.component_version,
            component.implementation_status,
        )
        for component in profile.components
    }
    if statuses != {
        AGENT_SCHEDULER_COMPONENT: (2, "EXECUTABLE"),
        FULL_DAY_RUNTIME_COMPONENT: (1, "CONTRACT_ONLY"),
        MECHANICS_COMPONENT: (2, "EXECUTABLE"),
    } or profile.implementation_status != "EXECUTABLE":
        failures.append("E1 statuses/versions differ from the append-only promotion")
    plan = _wo31e1_plan()
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
        ),
        plan=plan,
        profile=profile,
    )
    if graph.restore_order != (
        FULL_DAY_RUNTIME_COMPONENT,
        MECHANICS_COMPONENT,
        AGENT_SCHEDULER_COMPONENT,
    ):
        failures.append("component restore dependency order is not exact")
    exact_resources = {
        resource: graph.resource_owners.get(resource)
        for resource in (
            "MARKET_MECHANICS_ENGINE",
            "ORDER_ALLOCATOR",
            "ORDER_BOOK",
            "SESSION_CALENDAR",
            "SIMULATION_CLOCK",
        )
    }
    if any(owner != FULL_DAY_RUNTIME_COMPONENT for owner in exact_resources.values()):
        failures.append("clock/engine/book/calendar/allocator ownership is not singular")
    if profile.refused_component_ids != prior.profiles[0].refused_component_ids:
        failures.append("unrelated/refused component inventory changed in E1")
    return FullDayAuditCase(
        "full_day_mechanics_agent_composition",
        (
            f"matrix_v1_sha256={prior.sha256} matrix_v2_sha256={matrix.sha256} "
            f"restore_order={graph.restore_order} exact_resources={exact_resources}"
        ),
        tuple(failures),
    )


def _wo31e1_one_shot_case() -> FullDayAuditCase:
    failures: list[str] = []
    one_shot = _wo31e1_runtime()
    subdivided = _wo31e1_runtime()
    opening_start, continuous_start = _wo31e1_times(one_shot)
    target = continuous_start + 50
    one_shot.advance_to(target)
    for time_us in (
        0,
        100,
        opening_start,
        opening_start + 10,
        continuous_start,
        continuous_start + 10,
        continuous_start + 15,
        target,
    ):
        subdivided.advance_to(time_us)
    one_shot.capture_quiescent_cut("WO31-E1-ADVANCE-EQUIVALENCE")
    subdivided.capture_quiescent_cut("WO31-E1-ADVANCE-EQUIVALENCE")
    if one_shot.canonical_state_bytes() != subdivided.canonical_state_bytes():
        failures.append("one-shot and subdivided runtime checkpoints differ")
    if one_shot.event_stream_bytes() != subdivided.event_stream_bytes():
        failures.append("one-shot and subdivided outer event streams differ")
    return FullDayAuditCase(
        "full_day_one_shot_subdivided",
        (
            f"target_us={target} events={len(one_shot.events)} "
            f"state_sha256={one_shot.state_sha256()}"
        ),
        tuple(failures),
    )


def _wo31e1_inactive_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import (
        MarketMechanicsComponentAdapterV1,
    )
    from kirby2.full_day.composition import (
        ABSENT_REASON_COMPONENT_INACTIVE,
        AGENT_SCHEDULER_COMPONENT,
        executable_agent_mechanics_composition_matrix,
    )
    from kirby2.full_day.runtime import FullDayRuntime

    plan = replace(_wo31e1_plan(), participant_schedule=())
    runtime = FullDayRuntime.create(plan)
    runtime.advance_to(0)
    runtime.capture_quiescent_cut("WO31-E1-INACTIVE-CUT")
    state = runtime.checkpoint_state()
    restored = FullDayRuntime.from_checkpoint_state(state)
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
        ),
        plan=plan,
        profile=executable_agent_mechanics_composition_matrix().profile(
            "SINGLE_VENUE_AGENT_MECHANICS_V1", 2
        ),
    )
    failures: list[str] = []
    union = state["agent_scheduler"]
    if union != {
        "absent_reason": ABSENT_REASON_COMPONENT_INACTIVE,
        "status": "ABSENT",
    }:
        failures.append("inactive scheduler union does not use the stable ABSENT reason")
    if AGENT_SCHEDULER_COMPONENT in graph.active_component_ids:
        failures.append("inactive scheduler adapter remains active")
    if restored.canonical_state_bytes() != runtime.canonical_state_bytes():
        failures.append("inactive ABSENT runtime did not restore exactly")
    return FullDayAuditCase(
        "full_day_inactive_scheduler_absent",
        (
            f"reason={union.get('absent_reason')} "
            f"active_components={graph.active_component_ids}"
        ),
        tuple(failures),
    )


def _wo31e1_refresh_cut_digests(state: dict[str, object]) -> None:
    from kirby2.full_day.events import (
        FullDayEventV1,
        canonical_event_prefix_sha256,
    )

    events = tuple(FullDayEventV1.from_dict(row) for row in state["events"])
    for cut in state["checkpoint_controller"]["quiescent_cuts"]:
        prefix_length = int(cut["event_prefix_last_global_sequence"])
        cut["event_prefix_sha256"] = canonical_event_prefix_sha256(
            events[:prefix_length]
        )


def _wo31e1_hostile_case() -> FullDayAuditCase:
    from kirby2.agents.ecology import AgentEcology
    from kirby2.full_day.models import canonical_sha256
    from kirby2.full_day.restore import FullDayRuntimeRestoreRequestV1
    from kirby2.full_day.runtime import (
        FullDayRuntime,
        RuntimeOrderIdAllocatorV1,
    )

    failures: list[str] = []
    refused = 0

    def probe(label: str, operation: Callable[[], object]) -> None:
        nonlocal refused
        failure = _expect_refusal(operation, label)
        if failure:
            failures.append(failure)
        else:
            refused += 1

    def atomic_probe(
        label: str,
        runtime: object,
        operation: Callable[[], object],
    ) -> None:
        before = runtime.canonical_state_bytes()
        probe(label, operation)
        try:
            after = runtime.canonical_state_bytes()
        except (TypeError, ValueError, RuntimeError) as error:
            failures.append(f"{label} damaged canonical state after refusal: {error}")
            return
        if after != before:
            failures.append(f"{label} mutated canonical state before refusing")

    class Holder:
        pass

    for label, value in (
        ("nested second mechanics engine", MarketMechanicsEngine()),
        ("nested second session calendar", _wo31e1_plan().calendar),
        ("nested second order allocator", RuntimeOrderIdAllocatorV1()),
    ):
        runtime = _wo31e1_runtime()
        holder = Holder()
        holder.value = value
        runtime.agent_scheduler._hostile_holder = holder
        probe(label, runtime.assert_invariants)

    plan = _wo31e1_plan()
    compatibility = AgentEcology(_wo31e1_population(plan), 42)
    probe(
        "compatibility-wrapper scheduler smuggling",
        lambda: FullDayRuntime.create(plan, agent_scheduler=compatibility),
    )

    runtime, _cut = _wo31e1_prepare_boundary("participant_activation")
    active_state = runtime.checkpoint_state()
    omitted = copy.deepcopy(active_state)
    omitted["agent_scheduler"] = {
        "absent_reason": "COMPOSITION_ACTIVE_PREDICATE_FALSE",
        "status": "ABSENT",
    }
    probe(
        "active omitted scheduler component",
        lambda: FullDayRuntime.from_checkpoint_state(omitted),
    )

    unowned = copy.deepcopy(active_state)
    pending_row = next(
        row
        for row in unowned["pending_work"]
        if row["work_type"] == "SCHEDULED_INFORMATION"
    )
    pending_row["key"]["source_component_id"] = "ENGINE_MARKET_MECHANICS_V1"
    from kirby2.full_day.events import ScheduledWorkKeyV1

    forged_key = ScheduledWorkKeyV1.from_dict(pending_row["key"])
    pending_row["causal_parent_id"] = forged_key.work_id
    probe(
        "pending unowned scheduled event",
        lambda: FullDayRuntime.from_checkpoint_state(unowned),
    )

    orphan = copy.deepcopy(active_state)
    orphan["events"][-1]["causal_parent_ids"] = ["work:" + "a" * 64]
    _wo31e1_refresh_cut_digests(orphan)
    probe(
        "orphan outer-event causal parent",
        lambda: FullDayRuntime.from_checkpoint_state(orphan),
    )

    inactive_plan = replace(_wo31e1_plan(), participant_schedule=())
    inactive = FullDayRuntime.create(inactive_plan)
    inactive.advance_to(0)
    inactive.capture_quiescent_cut("WO31-E1-HOSTILE-INACTIVE")
    split_truth = copy.deepcopy(inactive.checkpoint_state())
    mechanics_row = next(
        row
        for row in split_truth["native_ledger"]
        if row["reference"]["owner_component_id"]
        == "ENGINE_MARKET_MECHANICS_V1"
    )
    mechanics_row["payload"]["data"]["reason"] = "TAMPERED_BUT_CANONICAL"
    ledger_key = mechanics_row["reference"]["event_id"]
    outer = next(
        row
        for row in split_truth["events"]
        if row["payload"]["native_event"] is not None
        and row["payload"]["native_event"]["event_id"] == ledger_key
    )
    outer["payload"]["data"]["native_payload_sha256"] = canonical_sha256(
        mechanics_row["payload"]
    )
    _wo31e1_refresh_cut_digests(split_truth)
    probe(
        "split mechanics/native ledger truth",
        lambda: FullDayRuntime.from_checkpoint_state(split_truth),
    )

    halt_runtime, _halt_cut = _wo31e1_prepare_boundary("halt")
    forged_owner_reason = copy.deepcopy(
        halt_runtime.runtime_owner_checkpoint_state()
    )
    forged_transition = next(
        row
        for row in forged_owner_reason["native_ledger"]
        if row["reference"]["owner_component_id"]
        == "ENGINE_MARKET_MECHANICS_V1"
        and row["reference"]["event_type"] == "SESSION_STATE_CHANGED"
        and row["payload"]["data"]["current_state"] == "HALTED"
    )
    forged_transition["payload"]["data"]["reason"] = (
        "FORGED_BUT_CANONICAL"
    )
    forged_event_id = forged_transition["reference"]["event_id"]
    forged_outer = next(
        row
        for row in forged_owner_reason["events"]
        if row["payload"]["native_event"] is not None
        and row["payload"]["native_event"]["event_id"] == forged_event_id
    )
    forged_outer["payload"]["data"]["native_payload_sha256"] = canonical_sha256(
        forged_transition["payload"]
    )
    _wo31e1_refresh_cut_digests(forged_owner_reason)
    probe(
        "runtime-owner forged session transition cause",
        lambda: FullDayRuntime.validate_runtime_owner_checkpoint_state(
            forged_owner_reason
        ),
    )
    missing_state_batch = copy.deepcopy(active_state)
    missing_state_batch["state_scheduled_time"] = (
        active_state["clock"]["current_time_us"] + 1
    )
    probe(
        "runtime-owner orphan state batch marker",
        lambda: FullDayRuntime.validate_runtime_owner_checkpoint_state(
            {
                key: value
                for key, value in missing_state_batch.items()
                if key not in {"agent_scheduler", "engine", "engine_state_sha256"}
            }
        ),
    )

    forged_executed_owner_runtime, forged_time = _wo31e1_prepare_boundary(
        "agent_inventories"
    )
    forged_executed_owner = copy.deepcopy(
        forged_executed_owner_runtime.checkpoint_state()
    )
    first_agent_arrival_event = next(
        row
        for row in forged_executed_owner["events"]
        if row["simulation_time_us"] == forged_time and row["stage"] == 5
    )
    original_parent_id = first_agent_arrival_event["causal_parent_ids"][0]
    executed_agent_arrival = next(
        row
        for row in forged_executed_owner["executed_work"]
        if ScheduledWorkKeyV1.from_dict(row["key"]).work_id
        == original_parent_id
    )
    executed_agent_arrival["key"]["source_component_id"] = (
        "FULL_DAY_RUNTIME_V1"
    )
    executed_agent_arrival["key"]["component_local_sequence"] = (
        forged_executed_owner["component_sequences"]["FULL_DAY_RUNTIME_V1"]
    )
    forged_executed_key = ScheduledWorkKeyV1.from_dict(
        executed_agent_arrival["key"]
    )
    executed_agent_arrival["causal_parent_id"] = forged_executed_key.work_id
    first_agent_arrival_event["causal_parent_ids"] = [
        forged_executed_key.work_id
    ]
    _wo31e1_refresh_cut_digests(forged_executed_owner)
    probe(
        "executed agent work reowned by runtime",
        lambda: FullDayRuntime.from_checkpoint_state(forged_executed_owner),
    )

    native_jump = copy.deepcopy(active_state)
    native_jump["native_sequences"] = {
        "AGENT_SCHEDULER_V1": 999,
        "EVIL_OWNER": 7,
    }
    probe(
        "native allocator owner/highwater jump",
        lambda: FullDayRuntime.from_checkpoint_state(native_jump),
    )
    allocator_jump = copy.deepcopy(active_state)
    allocator_jump["order_id_allocator"]["next_sequence"] = 999
    probe(
        "order allocator highwater jump",
        lambda: FullDayRuntime.from_checkpoint_state(allocator_jump),
    )
    deadline_jump = copy.deepcopy(active_state)
    scheduler_state = deadline_jump["agent_scheduler"]["state"]
    next_decisions = scheduler_state["state"]["next_decision_us"]
    maker_deadline = next_decisions["AUDIT_MAKER"]
    next_decisions["AUDIT_MAKER"] = maker_deadline + 1
    deadline_jump["agent_scheduler"]["state_sha256"] = canonical_sha256(
        scheduler_state
    )
    probe(
        "scheduler/runtime deadline cross-bind",
        lambda: FullDayRuntime.from_checkpoint_state(deadline_jump),
    )
    def cut_runtime():
        return _wo31e1_prepare_boundary("participant_activation")[0]

    horizon = cut_runtime()
    atomic_probe(
        "submit beyond plan calendar",
        horizon,
        lambda: horizon.submit_request(
            _wo31e1_limit("E1-BEYOND-HORIZON", Side.BUY, 1, 9_999),
            at_time_us=horizon.plan.calendar.end_time_us + 1,
        ),
    )
    horizon = cut_runtime()
    atomic_probe(
        "checkpoint beyond plan calendar",
        horizon,
        lambda: horizon.capture_quiescent_cut(
            "WO31-E1-BEYOND-HORIZON-CUT",
            at_time_us=horizon.plan.calendar.end_time_us + 1,
        ),
    )
    horizon = cut_runtime()
    atomic_probe(
        "cancel beyond plan calendar",
        horizon,
        lambda: horizon.cancel_order(
            "E1-NONEXISTENT-CANCEL",
            at_time_us=horizon.plan.calendar.end_time_us + 1,
        ),
    )
    horizon = cut_runtime()
    atomic_probe(
        "replace beyond plan calendar",
        horizon,
        lambda: horizon.replace_order(
            "E1-NONEXISTENT-REPLACE",
            new_order_id="E1-NONEXISTENT-REPLACEMENT",
            new_quantity=1,
            at_time_us=horizon.plan.calendar.end_time_us + 1,
        ),
    )
    horizon = cut_runtime()
    invalid_gtt = replace(
        _wo31e1_limit("E1-GTT-BEYOND-HORIZON", Side.BUY, 1, 9_999),
        time_in_force=OrderInstruction.GOOD_UNTIL_TIME,
        good_until_time_us=horizon.plan.calendar.end_time_us + 1,
    )
    atomic_probe(
        "GTT expiry beyond plan calendar",
        horizon,
        lambda: horizon.submit_request(
            invalid_gtt,
            at_time_us=horizon.clock.current_time_us + 1,
        ),
    )
    horizon = cut_runtime()
    atomic_probe(
        "external replacement claims runtime order namespace",
        horizon,
        lambda: horizon.replace_order(
            "E1-NONEXISTENT-RESERVED",
            new_order_id="FD-O-0000000001",
            new_quantity=1,
            at_time_us=horizon.clock.current_time_us + 1,
        ),
    )

    request = FullDayRuntimeRestoreRequestV1.capture(
        runtime,
        suffix_targets_us=(runtime.clock.current_time_us + 1,),
        final_checkpoint_request_id="WO31-E1-HOSTILE-WIRE-FINAL",
    )
    probe(
        "noncanonical restore wire",
        lambda: FullDayRuntimeRestoreRequestV1.from_json_bytes(
            request.canonical_bytes() + b"\n"
        ),
    )
    return FullDayAuditCase(
        "full_day_hostile_owner_protocol_refusals",
        (
            f"refusals={refused} active_omission=true duplicate_core_owners=true "
            "unowned_work=true split_truth=true allocator_bindings=true "
            "failure_atomicity=true owner_session_cause=true state_batch=true "
            "executed_work_contracts=true"
        ),
        tuple(failures),
    )


def _wo31e1_protocol_case() -> FullDayAuditCase:
    import inspect

    from kirby2.full_day.restore import (
        FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID,
        full_day_runtime_restore_worker_main,
    )

    failures: list[str] = []
    source = inspect.getsource(full_day_runtime_restore_worker_main)
    if any(token in source for token in ("open(", "Path(", "write_bytes", "write_text")):
        failures.append("fresh restore worker exposes a filesystem surface")
    if "stdin.buffer.read" not in source or "stdout.buffer.write" not in source:
        failures.append("fresh restore worker is not a single stdin/stdout protocol")
    return FullDayAuditCase(
        "full_day_restore_worker_protocol_scope",
        (
            f"format={FULL_DAY_RUNTIME_RESTORE_REQUEST_FORMAT_ID} "
            "stdin=one_checkpoint_plus_suffix stdout=one_canonical_result "
            "prefix_regeneration=absent filesystem_writes=absent"
        ),
        tuple(failures),
    )


def audit_wo31e1_runtime_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise the complete E1 owner spine and every fixed fresh-process cut."""

    boundaries = (
        "auction_imbalance",
        "auction_uncross",
        "halt",
        "reopen",
        "participant_activation",
        "participant_withdrawal",
        "active_metaorder",
        "agent_inventories",
        "next_scheduled_decision",
        "agent_substream_state",
        "order_allocator",
        "exchange_queues",
        "same_time_microsteps",
    )
    return (
        _wo31e1_composition_case(),
        _wo31e1_one_shot_case(),
        *(
            _wo31e1_fresh_boundary_case(boundary)
            for boundary in boundaries
        ),
        _wo31e1_inactive_case(),
        _wo31e1_hostile_case(),
        _wo31e1_protocol_case(),
    )


def _wo31e2_simple_configuration():
    from kirby2.full_day.components_flow import SimpleFlowConfigurationV1

    return SimpleFlowConfigurationV1(
        schema_version=1,
        configuration_id="AUDIT_SIMPLE_FLOW_V1",
        configuration_version=1,
        limit_buy_microevents_per_second=5_000,
        limit_sell_microevents_per_second=5_000,
        market_buy_microevents_per_second=5_000,
        market_sell_microevents_per_second=5_000,
        cancel_bid_microevents_per_second=5_000,
        cancel_ask_microevents_per_second=5_000,
        minimum_quantity=1,
        maximum_quantity=5,
        minimum_placement_depth_ticks=0,
        maximum_placement_depth_ticks=2,
        account_id="WO31-E2-FLOW",
    )


def _wo31e2_simple_plan():
    from kirby2.full_day.components_flow import SIMPLE_FLOW_RNG_LABEL
    from kirby2.full_day.composition import (
        FLOW_PROFILE_ID,
        FLOW_SIMPLE_COMPONENT,
        executable_simple_flow_composition_matrix,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        SubstreamDeclarationV1,
        VersionedReferenceV1,
        derive_substream_seed,
    )

    base = _wo31e1_plan()
    configuration = _wo31e2_simple_configuration()
    matrix = executable_simple_flow_composition_matrix()
    declaration = SubstreamDeclarationV1(
        SIMPLE_FLOW_RNG_LABEL,
        derive_substream_seed(
            base.seed_policy.root_seed,
            base.seed_policy.policy_version,
            SIMPLE_FLOW_RNG_LABEL,
        ),
    )
    return replace(
        base,
        composition_profile=VersionedReferenceV1(
            FLOW_PROFILE_ID,
            1,
            matrix.sha256,
        ),
        component_configurations=tuple(
            sorted(
                (
                    *base.component_configurations,
                    ComponentConfigurationBindingV1(
                        FLOW_SIMPLE_COMPONENT,
                        configuration.reference,
                    ),
                ),
                key=lambda item: item.sort_key,
            )
        ),
        seed_policy=replace(
            base.seed_policy,
            substreams=tuple(
                sorted(
                    (*base.seed_policy.substreams, declaration),
                    key=lambda item: item.semantic_path,
                )
            ),
        ),
    )


def _wo31e2_simple_runtime():
    from kirby2.full_day.runtime import FullDayRuntime

    plan = _wo31e2_simple_plan()
    return FullDayRuntime.create_with_agent_scheduler(
        plan,
        _wo31e1_population(plan),
        simple_flow_configuration=_wo31e2_simple_configuration(),
    )


def _wo31e2_simple_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_flow import (
        HawkesFlowComponentAdapterV1,
        QueueReactiveFlowComponentAdapterV1,
        SimpleFlowComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import (
        MarketMechanicsComponentAdapterV1,
    )
    from kirby2.full_day.composition import (
        FLOW_HAWKES_COMPONENT,
        FLOW_PROFILE_ID,
        FLOW_QUEUE_REACTIVE_COMPONENT,
        FLOW_SIMPLE_COMPONENT,
        executable_agent_mechanics_composition_matrix,
        executable_simple_flow_composition_matrix,
    )

    failures: list[str] = []
    previous = executable_agent_mechanics_composition_matrix()
    matrix = executable_simple_flow_composition_matrix()
    if tuple(row.canonical_bytes() for row in matrix.profiles[:2]) != tuple(
        row.canonical_bytes() for row in previous.profiles
    ):
        failures.append("simple-flow matrix rewrote an E1 composition row")
    profile = matrix.profile(FLOW_PROFILE_ID, 1)
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
            HawkesFlowComponentAdapterV1(),
            QueueReactiveFlowComponentAdapterV1(),
            SimpleFlowComponentAdapterV1(),
        ),
        plan=_wo31e2_simple_plan(),
        profile=profile,
    )
    if FLOW_SIMPLE_COMPONENT not in graph.active_component_ids or any(
        component_id in graph.active_component_ids
        for component_id in (
            FLOW_HAWKES_COMPONENT,
            FLOW_QUEUE_REACTIVE_COMPONENT,
        )
    ):
        failures.append("exactly-one flow activation does not select only simple flow")
    statuses = {
        component.component_id: component.implementation_status
        for component in profile.components
        if component.component_id
        in {
            FLOW_HAWKES_COMPONENT,
            FLOW_QUEUE_REACTIVE_COMPONENT,
            FLOW_SIMPLE_COMPONENT,
        }
    }
    if statuses != {
        FLOW_HAWKES_COMPONENT: "CONTRACT_ONLY",
        FLOW_QUEUE_REACTIVE_COMPONENT: "CONTRACT_ONLY",
        FLOW_SIMPLE_COMPONENT: "EXECUTABLE",
    }:
        failures.append("flow adapter statuses overclaim the bounded simple slice")
    return FullDayAuditCase(
        "full_day_simple_flow_composition",
        (
            f"matrix_v2_sha256={previous.sha256} matrix_v3_sha256={matrix.sha256} "
            f"active_components={graph.active_component_ids} statuses={statuses}"
        ),
        tuple(failures),
    )


def _wo31e2_simple_one_shot_case() -> FullDayAuditCase:
    from kirby2.full_day.events import FullDayEventTypeV1

    failures: list[str] = []
    one_shot = _wo31e2_simple_runtime()
    subdivided = _wo31e2_simple_runtime()
    target = 1_300_000_000
    one_shot.advance_to(target)
    for time_us in (
        0,
        300_000_000,
        600_000_000,
        900_000_000,
        1_200_000_000,
        1_250_000_000,
        target,
    ):
        subdivided.advance_to(time_us)
    one_shot.capture_quiescent_cut("WO31-E2-SIMPLE-EQUIVALENCE")
    subdivided.capture_quiescent_cut("WO31-E2-SIMPLE-EQUIVALENCE")
    if one_shot.canonical_state_bytes() != subdivided.canonical_state_bytes():
        failures.append("simple flow differs under one-shot and subdivided advance")
    proposal_events = tuple(
        event
        for event in one_shot.events
        if event.event_type is FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL
    )
    owner = one_shot.simple_flow
    if (
        owner is None
        or not proposal_events
        or owner.applied_count == 0
        or owner.rejected_count == 0
        or owner.pending_proposal is None
    ):
        failures.append("simple flow did not exercise applied/rejected/pending states")
    if any(
        "price_ticks" in entry.payload["proposal"]
        for entry in one_shot.native_event_ledger.values()
        if entry.reference.owner_component_id == "FLOW_SIMPLE_V1"
    ):
        failures.append("a flow proposal smuggled an absolute price command")
    perturbed_plan = _wo31e2_simple_plan()
    perturbed_plan = replace(
        perturbed_plan,
        participant_schedule=tuple(
            sorted(
                (
                    replace(entry, simulation_time_us=200)
                    if entry.schedule_id == "E1_MAKER_ACTIVATE"
                    else entry
                    for entry in perturbed_plan.participant_schedule
                ),
                key=lambda entry: (
                    entry.simulation_time_us,
                    entry.participant_id,
                    entry.schedule_id,
                ),
            )
        ),
    )
    from kirby2.full_day.runtime import FullDayRuntime

    perturbed = FullDayRuntime.create_with_agent_scheduler(
        perturbed_plan,
        _wo31e1_population(perturbed_plan),
        simple_flow_configuration=_wo31e2_simple_configuration(),
    )
    perturbed.advance_to(target)

    def proposal_draw_core(flow_owner) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                row.get("family"),
                row.get("scheduled_time_us"),
                row.get("quantity"),
                row.get("placement_depth_ticks"),
                row.get("rng_state_after_sha256"),
            )
            for row in flow_owner.diagnostic_draw_sequence
        )

    if owner is not None and (
        proposal_draw_core(owner) != proposal_draw_core(perturbed.simple_flow)
        or owner.rng.state_sha256() != perturbed.simple_flow.rng.state_sha256()
    ):
        failures.append("participant schedule change reseeded or reordered simple flow")
    return FullDayAuditCase(
        "full_day_simple_flow_one_shot_subdivided",
        (
            f"target_us={target} proposals={len(proposal_events)} "
            f"applied={0 if owner is None else owner.applied_count} "
            f"rejected={0 if owner is None else owner.rejected_count} "
            "participant_schedule_rng_independence=true"
        ),
        tuple(failures),
    )


def _wo31e2_simple_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        canonical_json_bytes,
        parse_canonical_json_object,
    )
    from kirby2.full_day.restore import (
        FullDayRuntimeRestoreRequestV1,
        execute_uninterrupted_full_day_runtime_suffix,
    )

    failures: list[str] = []
    runtime = _wo31e2_simple_runtime()
    cut_time = 1_250_000_000
    target = 1_300_000_000
    runtime.advance_to(cut_time)
    runtime.capture_quiescent_cut("WO31-E2-SIMPLE-CUT")
    flow_state = runtime.simple_flow.checkpoint_state()
    request = FullDayRuntimeRestoreRequestV1.capture(
        runtime,
        suffix_targets_us=(target,),
        final_checkpoint_request_id="WO31-E2-SIMPLE-FINAL",
    )
    returncode, stdout, stderr, wrote_files = _wo31e1_run_worker(
        request.canonical_bytes()
    )
    expected = execute_uninterrupted_full_day_runtime_suffix(runtime, request)
    actual: dict[str, object] | None = None
    if returncode != 0:
        failures.append(
            f"fresh simple-flow worker returned {returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    else:
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"fresh simple-flow worker emitted invalid JSON: {error}")
    if stderr:
        failures.append("successful simple-flow worker wrote stderr diagnostics")
    if wrote_files:
        failures.append("simple-flow restore worker wrote into its empty directory")
    if actual is not None and (
        canonical_json_bytes(actual) != stdout or actual != expected
    ):
        failures.append("fresh simple-flow suffix differs from uninterrupted suffix")
    if (
        flow_state["pending_proposal"] is None
        or not flow_state["diagnostic_draw_sequence"]
        or not flow_state["rng_state"]["internal_state"]
    ):
        failures.append("simple-flow cut omits pending/draw/RNG state")
    digest = "ABSENT" if actual is None else str(actual["invariant_sha256"])
    return FullDayAuditCase(
        "full_day_simple_flow_fresh_process_restore",
        (
            f"cut_us={cut_time} target_us={target} fresh_process=true "
            f"invariant_sha256={digest}"
        ),
        tuple(failures),
    )


def _wo31e2_simple_ownership_case() -> FullDayAuditCase:
    from kirby2.simulation.clock import SimulationClock
    from kirby2.full_day.components_flow import SimpleFlowComponentAdapterV1
    from kirby2.full_day.runtime import FullDayRuntime

    failures: list[str] = []
    refused = 0
    runtime = _wo31e2_simple_runtime()
    runtime.advance_to(1_300_000_000)
    runtime.capture_quiescent_cut("WO31-E2-SIMPLE-OWNERSHIP")
    owner = runtime.simple_flow
    if owner is None:
        failures.append("simple-flow owner is absent")
        return FullDayAuditCase(
            "full_day_simple_flow_ownership_refusals",
            "owner=ABSENT",
            tuple(failures),
        )
    adapter = SimpleFlowComponentAdapterV1()
    snapshot = adapter.snapshot(owner)
    restored = adapter.restore(snapshot, plan=runtime.plan)
    if restored.canonical_state_bytes() != owner.canonical_state_bytes():
        failures.append("simple-flow component adapter is not a fixed point")
    owner.clock = SimulationClock()
    try:
        runtime.assert_invariants()
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("simple flow smuggled a second clock")
    finally:
        del owner.clock
    forged = copy.deepcopy(snapshot.as_dict())
    forged["state"]["proposal_sequence"] += 1
    try:
        type(snapshot).from_dict(forged)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("forged simple-flow snapshot digest was accepted")

    plan = runtime.plan
    hawkes_binding = replace(
        next(
            binding
            for binding in plan.component_configurations
            if binding.component_id == "FLOW_SIMPLE_V1"
        ),
        component_id="FLOW_HAWKES_V1",
    )
    hawkes_binding = replace(
        hawkes_binding,
        configuration=replace(
            hawkes_binding.configuration,
            reference_id="AUDIT_HAWKES_FLOW_V1",
        ),
    )
    two_flow_plan = replace(
        plan,
        component_configurations=tuple(
            sorted(
                (*plan.component_configurations, hawkes_binding),
                key=lambda item: item.sort_key,
            )
        ),
    )
    try:
        FullDayRuntime.create_with_agent_scheduler(
            two_flow_plan,
            _wo31e1_population(two_flow_plan),
            simple_flow_configuration=_wo31e2_simple_configuration(),
        )
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("two selected flow adapters bypassed exactly-one activation")
    atomic = _wo31e2_simple_runtime()
    atomic.advance_to(0)
    pending = atomic.simple_flow.pending_proposal
    if pending is None:
        failures.append("failure-atomicity probe has no pending flow proposal")
    else:
        pre_failure_time = pending.scheduled_time_us - 1
        atomic.advance_to(pre_failure_time)
        atomic.capture_quiescent_cut("WO31-E2-SIMPLE-ATOMIC-PREFIX")
        before = atomic.canonical_state_bytes()
        original_emit_native = atomic._emit_native

        def refuse_flow_native(**kwargs):
            if kwargs.get("owner_component_id") == "FLOW_SIMPLE_V1":
                raise RuntimeError("AUDIT_FORCED_FLOW_NATIVE_FAILURE")
            return original_emit_native(**kwargs)

        atomic._emit_native = refuse_flow_native
        try:
            atomic.advance_to(pending.scheduled_time_us)
        except RuntimeError as error:
            if "AUDIT_FORCED_FLOW_NATIVE_FAILURE" not in str(error):
                failures.append("flow failure-atomicity probe raised the wrong error")
            else:
                refused += 1
        else:
            failures.append("flow native-ledger failure was not propagated")
        finally:
            del atomic._emit_native
        if atomic.canonical_state_bytes() != before:
            failures.append("flow failure changed owner, engine, RNG, or allocator state")
    if owner.rng is runtime.agent_scheduler.agents["AUDIT_MAKER"].rng:
        failures.append("simple flow shares an agent RNG object")
    return FullDayAuditCase(
        "full_day_simple_flow_ownership_refusals",
        (
            f"refusals={refused} second_clock=true forged_state=true "
            "multiple_flow_selection=true failure_atomicity=true "
            "agent_rng_separation=true"
        ),
        tuple(failures),
    )


def audit_wo31e2_simple_flow_slice() -> tuple[FullDayAuditCase, ...]:
    """Exercise the bounded simple adapter without promoting all of WO31-E2."""

    return (
        _wo31e2_simple_composition_case(),
        _wo31e2_simple_one_shot_case(),
        _wo31e2_simple_fresh_restore_case(),
        _wo31e2_simple_ownership_case(),
    )


def _wo31e2_hawkes_configuration():
    from kirby2.full_day.components_flow import HawkesFlowConfigurationV1

    return HawkesFlowConfigurationV1.from_accepted_profile(
        configuration_id="AUDIT_HAWKES_FLOW_V1",
        configuration_version=1,
        accepted_profile_id="balanced",
        limit_buy_microevents_per_second=5_000,
        limit_sell_microevents_per_second=5_000,
        market_buy_microevents_per_second=5_000,
        market_sell_microevents_per_second=5_000,
        cancel_bid_microevents_per_second=5_000,
        cancel_ask_microevents_per_second=5_000,
        minimum_quantity=1,
        maximum_quantity=5,
        minimum_placement_depth_ticks=0,
        maximum_placement_depth_ticks=2,
        account_id="WO31-E2-HAWKES",
    )


def _wo31e2_hawkes_plan():
    from kirby2.full_day.components_flow import HAWKES_FLOW_RNG_LABEL
    from kirby2.full_day.composition import (
        FLOW_HAWKES_COMPONENT,
        FLOW_PROFILE_ID,
        executable_hawkes_flow_composition_matrix,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        SubstreamDeclarationV1,
        VersionedReferenceV1,
        derive_substream_seed,
    )

    base = _wo31e1_plan()
    configuration = _wo31e2_hawkes_configuration()
    matrix = executable_hawkes_flow_composition_matrix()
    declaration = SubstreamDeclarationV1(
        HAWKES_FLOW_RNG_LABEL,
        derive_substream_seed(
            base.seed_policy.root_seed,
            base.seed_policy.policy_version,
            HAWKES_FLOW_RNG_LABEL,
        ),
    )
    return replace(
        base,
        composition_profile=VersionedReferenceV1(
            FLOW_PROFILE_ID,
            2,
            matrix.sha256,
        ),
        component_configurations=tuple(
            sorted(
                (
                    *base.component_configurations,
                    ComponentConfigurationBindingV1(
                        FLOW_HAWKES_COMPONENT,
                        configuration.reference,
                    ),
                ),
                key=lambda item: item.sort_key,
            )
        ),
        seed_policy=replace(
            base.seed_policy,
            substreams=tuple(
                sorted(
                    (*base.seed_policy.substreams, declaration),
                    key=lambda item: item.semantic_path,
                )
            ),
        ),
    )


def _wo31e2_hawkes_runtime():
    from kirby2.full_day.runtime import FullDayRuntime

    plan = _wo31e2_hawkes_plan()
    return FullDayRuntime.create_with_agent_scheduler(
        plan,
        _wo31e1_population(plan),
        hawkes_flow_configuration=_wo31e2_hawkes_configuration(),
    )


def _wo31e2_hawkes_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_flow import (
        HawkesFlowComponentAdapterV2,
        QueueReactiveFlowComponentAdapterV1,
        SimpleFlowComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import (
        MarketMechanicsComponentAdapterV1,
    )
    from kirby2.full_day.composition import (
        FLOW_HAWKES_COMPONENT,
        FLOW_PROFILE_ID,
        FLOW_QUEUE_REACTIVE_COMPONENT,
        FLOW_SIMPLE_COMPONENT,
        executable_hawkes_flow_composition_matrix,
        executable_simple_flow_composition_matrix,
    )

    failures: list[str] = []
    previous = executable_simple_flow_composition_matrix()
    matrix = executable_hawkes_flow_composition_matrix()
    if tuple(row.canonical_bytes() for row in matrix.profiles[:3]) != tuple(
        row.canonical_bytes() for row in previous.profiles
    ):
        failures.append("Hawkes matrix rewrote a previously published profile")
    profile = matrix.profile(FLOW_PROFILE_ID, 2)
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
            HawkesFlowComponentAdapterV2(),
            QueueReactiveFlowComponentAdapterV1(),
            SimpleFlowComponentAdapterV1(),
        ),
        plan=_wo31e2_hawkes_plan(),
        profile=profile,
    )
    if FLOW_HAWKES_COMPONENT not in graph.active_component_ids or any(
        component_id in graph.active_component_ids
        for component_id in (FLOW_QUEUE_REACTIVE_COMPONENT, FLOW_SIMPLE_COMPONENT)
    ):
        failures.append("exactly-one flow activation does not select only Hawkes")
    statuses = {
        component.component_id: component.implementation_status
        for component in profile.components
        if component.component_id
        in {
            FLOW_HAWKES_COMPONENT,
            FLOW_QUEUE_REACTIVE_COMPONENT,
            FLOW_SIMPLE_COMPONENT,
        }
    }
    if statuses != {
        FLOW_HAWKES_COMPONENT: "EXECUTABLE",
        FLOW_QUEUE_REACTIVE_COMPONENT: "CONTRACT_ONLY",
        FLOW_SIMPLE_COMPONENT: "EXECUTABLE",
    }:
        failures.append("Hawkes profile overclaims the queue-reactive adapter")
    return FullDayAuditCase(
        "full_day_hawkes_flow_composition",
        (
            f"matrix_v3_sha256={previous.sha256} matrix_v4_sha256={matrix.sha256} "
            f"active_components={graph.active_component_ids} statuses={statuses}"
        ),
        tuple(failures),
    )


def _wo31e2_hawkes_one_shot_case() -> FullDayAuditCase:
    from kirby2.full_day.events import FullDayEventTypeV1
    from kirby2.full_day.runtime import FullDayRuntime

    failures: list[str] = []
    one_shot = _wo31e2_hawkes_runtime()
    subdivided = _wo31e2_hawkes_runtime()
    target = 1_300_000_000
    one_shot.advance_to(target)
    for time_us in (
        0,
        300_000_000,
        600_000_000,
        900_000_000,
        1_200_000_000,
        1_250_000_000,
        target,
    ):
        subdivided.advance_to(time_us)
    one_shot.capture_quiescent_cut("WO31-E2-HAWKES-EQUIVALENCE")
    subdivided.capture_quiescent_cut("WO31-E2-HAWKES-EQUIVALENCE")
    if one_shot.canonical_state_bytes() != subdivided.canonical_state_bytes():
        failures.append("Hawkes flow differs under one-shot/subdivided advance")
    proposal_events = tuple(
        event
        for event in one_shot.events
        if event.event_type is FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL
        and event.source_component_id == "FLOW_HAWKES_V1"
    )
    owner = one_shot.hawkes_flow
    if (
        owner is None
        or not proposal_events
        or owner.applied_count == 0
        or owner.rejected_count == 0
        or owner.pending_proposal is None
    ):
        failures.append("Hawkes flow did not exercise applied/rejected/pending states")
    if any(
        "price_ticks" in entry.payload["proposal"]
        for entry in one_shot.native_event_ledger.values()
        if entry.reference.owner_component_id == "FLOW_HAWKES_V1"
    ):
        failures.append("a Hawkes proposal smuggled an absolute price command")
    if owner is not None:
        state = owner.checkpoint_state()
        excitation = state["excitation_state"]
        if not any(
            value != "0x0.0p+0"
            for row in excitation
            for value in row
        ):
            failures.append("Hawkes flow did not preserve nonzero excitation")
        if not all(
            type(value) is str and value == float.fromhex(value).hex()
            for row in excitation
            for value in row
        ):
            failures.append("Hawkes excitation is not canonical binary64 hex")

    perturbed_plan = _wo31e2_hawkes_plan()
    perturbed_plan = replace(
        perturbed_plan,
        participant_schedule=tuple(
            sorted(
                (
                    replace(entry, simulation_time_us=200)
                    if entry.schedule_id == "E1_MAKER_ACTIVATE"
                    else entry
                    for entry in perturbed_plan.participant_schedule
                ),
                key=lambda entry: (
                    entry.simulation_time_us,
                    entry.participant_id,
                    entry.schedule_id,
                ),
            )
        ),
    )
    perturbed = FullDayRuntime.create_with_agent_scheduler(
        perturbed_plan,
        _wo31e1_population(perturbed_plan),
        hawkes_flow_configuration=_wo31e2_hawkes_configuration(),
    )
    perturbed.advance_to(target)

    def proposal_draw_core(flow_owner) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                row.get("family"),
                row.get("scheduled_time_us"),
                row.get("quantity"),
                row.get("placement_depth_ticks"),
                row.get("intensity_state_before_hex"),
                row.get("rng_state_after_sha256"),
            )
            for row in flow_owner.diagnostic_draw_sequence
        )

    if owner is not None and (
        proposal_draw_core(owner) != proposal_draw_core(perturbed.hawkes_flow)
        or owner.rng.state_sha256() != perturbed.hawkes_flow.rng.state_sha256()
    ):
        failures.append("participant schedule change reseeded/reordered Hawkes flow")
    return FullDayAuditCase(
        "full_day_hawkes_flow_one_shot_subdivided",
        (
            f"target_us={target} proposals={len(proposal_events)} "
            f"applied={0 if owner is None else owner.applied_count} "
            f"rejected={0 if owner is None else owner.rejected_count} "
            "excitation_preserved=true participant_schedule_rng_independence=true"
        ),
        tuple(failures),
    )


def _wo31e2_hawkes_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        canonical_json_bytes,
        parse_canonical_json_object,
    )
    from kirby2.full_day.restore import (
        FullDayRuntimeRestoreRequestV1,
        execute_uninterrupted_full_day_runtime_suffix,
    )

    failures: list[str] = []
    runtime = _wo31e2_hawkes_runtime()
    cut_time = 1_250_000_000
    target = 1_300_000_000
    runtime.advance_to(cut_time)
    runtime.capture_quiescent_cut("WO31-E2-HAWKES-CUT")
    flow_state = runtime.hawkes_flow.checkpoint_state()
    request = FullDayRuntimeRestoreRequestV1.capture(
        runtime,
        suffix_targets_us=(target,),
        final_checkpoint_request_id="WO31-E2-HAWKES-FINAL",
    )
    returncode, stdout, stderr, wrote_files = _wo31e1_run_worker(
        request.canonical_bytes()
    )
    expected = execute_uninterrupted_full_day_runtime_suffix(runtime, request)
    actual: dict[str, object] | None = None
    if returncode != 0:
        failures.append(
            f"fresh Hawkes worker returned {returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    else:
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"fresh Hawkes worker emitted invalid JSON: {error}")
    if stderr:
        failures.append("successful Hawkes worker wrote stderr diagnostics")
    if wrote_files:
        failures.append("Hawkes restore worker wrote into its empty directory")
    if actual is not None and (
        canonical_json_bytes(actual) != stdout or actual != expected
    ):
        failures.append("fresh Hawkes suffix differs from uninterrupted suffix")
    if (
        flow_state["pending_proposal"] is None
        or not flow_state["diagnostic_draw_sequence"]
        or not flow_state["rng_state"]["internal_state"]
        or not flow_state["excitation_state"]
        or flow_state["last_decay_time_us"] <= 0
    ):
        failures.append("Hawkes cut omits pending/draw/RNG/excitation/decay state")
    digest = "ABSENT" if actual is None else str(actual["invariant_sha256"])
    return FullDayAuditCase(
        "full_day_hawkes_flow_fresh_process_restore",
        (
            f"cut_us={cut_time} target_us={target} fresh_process=true "
            f"invariant_sha256={digest}"
        ),
        tuple(failures),
    )


def _wo31e2_hawkes_ownership_case() -> FullDayAuditCase:
    from kirby2.simulation.clock import SimulationClock
    from kirby2.full_day.components_flow import (
        FlowObservationCutV1,
        HawkesFlowComponentAdapterV2,
        HawkesFlowConfigurationV1,
        HawkesFlowOwnerV1,
        SIMPLE_FLOW_RNG_LABEL,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        SubstreamDeclarationV1,
        derive_substream_seed,
    )
    from kirby2.full_day.runtime import FullDayRuntime

    failures: list[str] = []
    refused = 0
    runtime = _wo31e2_hawkes_runtime()
    runtime.advance_to(1_300_000_000)
    runtime.capture_quiescent_cut("WO31-E2-HAWKES-OWNERSHIP")
    owner = runtime.hawkes_flow
    if owner is None:
        return FullDayAuditCase(
            "full_day_hawkes_flow_ownership_refusals",
            "owner=ABSENT",
            ("Hawkes-flow owner is absent",),
        )
    adapter = HawkesFlowComponentAdapterV2()
    snapshot = adapter.snapshot(owner)
    restored = adapter.restore(snapshot, plan=runtime.plan)
    if restored.canonical_state_bytes() != owner.canonical_state_bytes():
        failures.append("Hawkes component adapter is not a fixed point")
    owner.clock = SimulationClock()
    try:
        runtime.assert_invariants()
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("Hawkes flow smuggled a second clock")
    finally:
        del owner.clock
    forged = copy.deepcopy(snapshot.as_dict())
    forged["state"]["model_id_version"]["runtime_state"]["excitation"][0][0] = (
        "0x1.0000000000000p+0"
    )
    try:
        type(snapshot).from_dict(forged)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("forged Hawkes snapshot digest was accepted")

    wrong_model = owner.checkpoint_state()
    wrong_model["model_id_version"]["model_id"] = "SIMPLE_POISSON_FLOW_V1"
    try:
        HawkesFlowOwnerV1.from_checkpoint_state(wrong_model, plan=runtime.plan)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("wrong-model Hawkes owner state was accepted")

    corrupt_excitation = owner.checkpoint_state()
    corrupt_excitation["model_id_version"]["runtime_state"]["excitation"][0][0] = (
        "not-binary64"
    )
    corrupt_excitation["excitation_state"][0][0] = "not-binary64"
    try:
        HawkesFlowOwnerV1.from_checkpoint_state(
            corrupt_excitation,
            plan=runtime.plan,
        )
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("corrupt Hawkes excitation state was accepted")

    stale_owner = HawkesFlowOwnerV1(runtime.plan, _wo31e2_hawkes_configuration())
    initial_cut = FlowObservationCutV1(
        schema_version=1,
        simulation_time_us=0,
        best_bid_ticks=None,
        best_ask_ticks=None,
        reference_price_ticks=runtime.engine.rules.reference_price_ticks,
        cancellable_bid_order_ids=(),
        cancellable_ask_order_ids=(),
    )
    stale_proposal = stale_owner.plan_next(
        initial_cut,
        horizon_us=runtime.plan.calendar.end_time_us,
    )
    if stale_proposal is None:
        failures.append("stale-observation probe did not schedule a proposal")
    else:
        stale_owner.resolve_pending(
            applied=False,
            rejection_reason="AUDIT_STALE_OBSERVATION_SETUP",
        )
        stale_cut = replace(
            initial_cut,
            simulation_time_us=stale_proposal.scheduled_time_us - 1,
        )
        try:
            stale_owner.plan_next(
                stale_cut,
                horizon_us=runtime.plan.calendar.end_time_us,
            )
        except (TypeError, ValueError, RuntimeError):
            refused += 1
        else:
            failures.append("stale Hawkes observation cutoff was accepted")

    configuration = _wo31e2_hawkes_configuration()
    forged_profile = configuration.as_dict()
    forged_profile["accepted_profile_sha256"] = "0" * 64
    try:
        HawkesFlowConfigurationV1.from_dict(forged_profile)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("forged accepted Hawkes profile digest was accepted")

    plan = runtime.plan
    simple_configuration = _wo31e2_simple_configuration()
    two_flow_plan = replace(
        plan,
        component_configurations=tuple(
            sorted(
                (
                    *plan.component_configurations,
                    ComponentConfigurationBindingV1(
                        "FLOW_SIMPLE_V1",
                        simple_configuration.reference,
                    ),
                ),
                key=lambda item: item.sort_key,
            )
        ),
        seed_policy=replace(
            plan.seed_policy,
            substreams=tuple(
                sorted(
                    (
                        *plan.seed_policy.substreams,
                        SubstreamDeclarationV1(
                            SIMPLE_FLOW_RNG_LABEL,
                            derive_substream_seed(
                                plan.seed_policy.root_seed,
                                plan.seed_policy.policy_version,
                                SIMPLE_FLOW_RNG_LABEL,
                            ),
                        ),
                    ),
                    key=lambda item: item.semantic_path,
                )
            ),
        ),
    )
    try:
        FullDayRuntime.create_with_agent_scheduler(
            two_flow_plan,
            _wo31e1_population(two_flow_plan),
            simple_flow_configuration=simple_configuration,
            hawkes_flow_configuration=configuration,
        )
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("two executable flow adapters bypassed exactly-one selection")

    atomic = _wo31e2_hawkes_runtime()
    atomic.advance_to(0)
    pending = atomic.hawkes_flow.pending_proposal
    if pending is None:
        failures.append("Hawkes failure-atomicity probe has no pending proposal")
    else:
        pre_failure_time = pending.scheduled_time_us - 1
        atomic.advance_to(pre_failure_time)
        atomic.capture_quiescent_cut("WO31-E2-HAWKES-ATOMIC-PREFIX")
        before = atomic.canonical_state_bytes()
        original_emit_native = atomic._emit_native

        def refuse_flow_native(**kwargs):
            if kwargs.get("owner_component_id") == "FLOW_HAWKES_V1":
                raise RuntimeError("AUDIT_FORCED_HAWKES_NATIVE_FAILURE")
            return original_emit_native(**kwargs)

        atomic._emit_native = refuse_flow_native
        try:
            atomic.advance_to(pending.scheduled_time_us)
        except RuntimeError as error:
            if "AUDIT_FORCED_HAWKES_NATIVE_FAILURE" not in str(error):
                failures.append("Hawkes failure-atomicity raised the wrong error")
            else:
                refused += 1
        else:
            failures.append("Hawkes native-ledger failure was not propagated")
        finally:
            del atomic._emit_native
        if atomic.canonical_state_bytes() != before:
            failures.append("Hawkes failure changed owner/engine/RNG/allocator state")
    if owner.rng is runtime.agent_scheduler.agents["AUDIT_MAKER"].rng:
        failures.append("Hawkes flow shares an agent RNG object")
    return FullDayAuditCase(
        "full_day_hawkes_flow_ownership_refusals",
        (
            f"refusals={refused} second_clock=true forged_state=true "
            "wrong_model=true corrupt_excitation=true stale_observation=true "
            "accepted_profile_binding=true multiple_flow_selection=true "
            "failure_atomicity=true agent_rng_separation=true"
        ),
        tuple(failures),
    )


def audit_wo31e2_hawkes_flow_slice() -> tuple[FullDayAuditCase, ...]:
    """Exercise Hawkes execution/restore without promoting all of WO31-E2."""

    return (
        _wo31e2_hawkes_composition_case(),
        _wo31e2_hawkes_one_shot_case(),
        _wo31e2_hawkes_fresh_restore_case(),
        _wo31e2_hawkes_ownership_case(),
    )


def _wo31e2_queue_configuration():
    from kirby2.full_day.components_flow import (
        QueueReactiveFlowConfigurationV1,
    )

    return QueueReactiveFlowConfigurationV1.from_default_profile(
        configuration_id="AUDIT_QUEUE_REACTIVE_FLOW_V1",
        configuration_version=1,
        limit_buy_microevents_per_second=5_000,
        limit_sell_microevents_per_second=5_000,
        market_buy_microevents_per_second=5_000,
        market_sell_microevents_per_second=5_000,
        cancel_bid_microevents_per_second=5_000,
        cancel_ask_microevents_per_second=5_000,
        minimum_quantity=1,
        maximum_quantity=5,
        minimum_placement_depth_ticks=0,
        maximum_placement_depth_ticks=2,
        account_id="WO31-E2-QUEUE",
    )


def _wo31e2_queue_plan():
    from kirby2.full_day.components_flow import QUEUE_REACTIVE_FLOW_RNG_LABEL
    from kirby2.full_day.composition import (
        FLOW_PROFILE_ID,
        FLOW_QUEUE_REACTIVE_COMPONENT,
        executable_queue_reactive_flow_composition_matrix,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        SubstreamDeclarationV1,
        VersionedReferenceV1,
        derive_substream_seed,
    )

    base = _wo31e1_plan()
    configuration = _wo31e2_queue_configuration()
    matrix = executable_queue_reactive_flow_composition_matrix()
    declaration = SubstreamDeclarationV1(
        QUEUE_REACTIVE_FLOW_RNG_LABEL,
        derive_substream_seed(
            base.seed_policy.root_seed,
            base.seed_policy.policy_version,
            QUEUE_REACTIVE_FLOW_RNG_LABEL,
        ),
    )
    return replace(
        base,
        composition_profile=VersionedReferenceV1(
            FLOW_PROFILE_ID,
            3,
            matrix.sha256,
        ),
        component_configurations=tuple(
            sorted(
                (
                    *base.component_configurations,
                    ComponentConfigurationBindingV1(
                        FLOW_QUEUE_REACTIVE_COMPONENT,
                        configuration.reference,
                    ),
                ),
                key=lambda item: item.sort_key,
            )
        ),
        seed_policy=replace(
            base.seed_policy,
            substreams=tuple(
                sorted(
                    (*base.seed_policy.substreams, declaration),
                    key=lambda item: item.semantic_path,
                )
            ),
        ),
    )


def _wo31e2_queue_runtime():
    from kirby2.full_day.runtime import FullDayRuntime

    plan = _wo31e2_queue_plan()
    return FullDayRuntime.create_with_agent_scheduler(
        plan,
        _wo31e1_population(plan),
        queue_reactive_flow_configuration=_wo31e2_queue_configuration(),
    )


def _wo31e2_queue_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_flow import (
        HawkesFlowComponentAdapterV2,
        QueueReactiveFlowComponentAdapterV2,
        SimpleFlowComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import (
        MarketMechanicsComponentAdapterV1,
    )
    from kirby2.full_day.composition import (
        FLOW_COMPONENT_IDS,
        FLOW_HAWKES_COMPONENT,
        FLOW_PROFILE_ID,
        FLOW_QUEUE_REACTIVE_COMPONENT,
        FLOW_SIMPLE_COMPONENT,
        executable_hawkes_flow_composition_matrix,
        executable_queue_reactive_flow_composition_matrix,
    )

    failures: list[str] = []
    previous = executable_hawkes_flow_composition_matrix()
    matrix = executable_queue_reactive_flow_composition_matrix()
    if tuple(row.canonical_bytes() for row in matrix.profiles[:4]) != tuple(
        row.canonical_bytes() for row in previous.profiles
    ):
        failures.append("queue-reactive matrix rewrote a published profile")
    profile = matrix.profile(FLOW_PROFILE_ID, 3)
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
            HawkesFlowComponentAdapterV2(),
            QueueReactiveFlowComponentAdapterV2(),
            SimpleFlowComponentAdapterV1(),
        ),
        plan=_wo31e2_queue_plan(),
        profile=profile,
    )
    if FLOW_QUEUE_REACTIVE_COMPONENT not in graph.active_component_ids or any(
        component_id in graph.active_component_ids
        for component_id in (FLOW_HAWKES_COMPONENT, FLOW_SIMPLE_COMPONENT)
    ):
        failures.append("exactly-one flow activation does not select only queue flow")
    statuses = {
        component.component_id: component.implementation_status
        for component in profile.components
        if component.component_id in FLOW_COMPONENT_IDS
    }
    if statuses != {component_id: "EXECUTABLE" for component_id in FLOW_COMPONENT_IDS}:
        failures.append("the complete flow profile does not promote all three models")
    return FullDayAuditCase(
        "full_day_queue_reactive_flow_composition",
        (
            f"matrix_v4_sha256={previous.sha256} matrix_v5_sha256={matrix.sha256} "
            f"active_components={graph.active_component_ids} statuses={statuses}"
        ),
        tuple(failures),
    )


def _wo31e2_queue_one_shot_case() -> FullDayAuditCase:
    from kirby2.full_day.events import FullDayEventTypeV1

    failures: list[str] = []
    one_shot = _wo31e2_queue_runtime()
    subdivided = _wo31e2_queue_runtime()
    target = 1_300_000_000
    one_shot.advance_to(target)
    for time_us in (
        0,
        300_000_000,
        600_000_000,
        900_000_000,
        1_200_000_000,
        1_250_000_000,
        target,
    ):
        subdivided.advance_to(time_us)
    one_shot.capture_quiescent_cut("WO31-E2-QUEUE-EQUIVALENCE")
    subdivided.capture_quiescent_cut("WO31-E2-QUEUE-EQUIVALENCE")
    if one_shot.canonical_state_bytes() != subdivided.canonical_state_bytes():
        failures.append("queue flow differs under one-shot/subdivided advance")
    owner = one_shot.queue_reactive_flow
    proposal_events = tuple(
        event
        for event in one_shot.events
        if event.event_type is FullDayEventTypeV1.BACKGROUND_FLOW_PROPOSAL
        and event.source_component_id == "FLOW_QUEUE_REACTIVE_V1"
    )
    if (
        owner is None
        or not proposal_events
        or owner.applied_count == 0
        or owner.rejected_count == 0
        or owner.pending_proposal is None
    ):
        failures.append("queue flow did not exercise applied/rejected/pending states")
    if any(
        "price_ticks" in entry.payload["proposal"]
        for entry in one_shot.native_event_ledger.values()
        if entry.reference.owner_component_id == "FLOW_QUEUE_REACTIVE_V1"
    ):
        failures.append("a queue proposal smuggled an absolute price command")
    distinct_inspections = set()
    if owner is not None:
        for row in owner.diagnostic_draw_sequence:
            inspection = row.get("intensity_inspection")
            if isinstance(inspection, Mapping):
                distinct_inspections.add(
                    repr(inspection.get("channels"))
                )
        state = owner.checkpoint_state()
        windows = state["retained_windows"]
        if (
            not windows["queue_changes"]
            or any(len(rows) > 10_000 for rows in windows.values())
        ):
            failures.append("queue retained windows are empty or unbounded")
        if len(distinct_inspections) < 2:
            failures.append("queue state never changed an intensity inspection")
        expected_seed = one_shot.plan.seed_policy.derive(owner.rng_label)
        if owner.rng.seed != expected_seed:
            failures.append("queue RNG is not derived from its declared substream")
    return FullDayAuditCase(
        "full_day_queue_reactive_one_shot_subdivided",
        (
            f"target_us={target} proposals={len(proposal_events)} "
            f"applied={0 if owner is None else owner.applied_count} "
            f"rejected={0 if owner is None else owner.rejected_count} "
            f"distinct_inspections={len(distinct_inspections)} bounded_windows=true"
        ),
        tuple(failures),
    )


def _wo31e2_queue_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import (
        canonical_json_bytes,
        parse_canonical_json_object,
    )
    from kirby2.full_day.restore import (
        FullDayRuntimeRestoreRequestV1,
        execute_uninterrupted_full_day_runtime_suffix,
    )

    failures: list[str] = []
    runtime = _wo31e2_queue_runtime()
    cut_time = 1_250_000_000
    target = 1_300_000_000
    runtime.advance_to(cut_time)
    runtime.capture_quiescent_cut("WO31-E2-QUEUE-CUT")
    flow_state = runtime.queue_reactive_flow.checkpoint_state()
    request = FullDayRuntimeRestoreRequestV1.capture(
        runtime,
        suffix_targets_us=(target,),
        final_checkpoint_request_id="WO31-E2-QUEUE-FINAL",
    )
    returncode, stdout, stderr, wrote_files = _wo31e1_run_worker(
        request.canonical_bytes()
    )
    expected = execute_uninterrupted_full_day_runtime_suffix(runtime, request)
    actual: dict[str, object] | None = None
    if returncode != 0:
        failures.append(
            f"fresh queue worker returned {returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    else:
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"fresh queue worker emitted invalid JSON: {error}")
    if stderr:
        failures.append("successful queue worker wrote stderr diagnostics")
    if wrote_files:
        failures.append("queue restore worker wrote into its empty directory")
    if actual is not None and (
        canonical_json_bytes(actual) != stdout or actual != expected
    ):
        failures.append("fresh queue suffix differs from uninterrupted suffix")
    windows = flow_state["retained_windows"]
    if (
        flow_state["pending_proposal"] is None
        or not flow_state["diagnostic_draw_sequence"]
        or not flow_state["rng_state"]["internal_state"]
        or not windows["queue_changes"]
        or flow_state["model_id_version"]["last_intensity_inspection"] is None
    ):
        failures.append("queue cut omits pending/draw/RNG/window/intensity state")
    digest = "ABSENT" if actual is None else str(actual["invariant_sha256"])
    return FullDayAuditCase(
        "full_day_queue_reactive_fresh_process_restore",
        (
            f"cut_us={cut_time} target_us={target} fresh_process=true "
            f"invariant_sha256={digest}"
        ),
        tuple(failures),
    )


def _wo31e2_queue_ownership_case() -> FullDayAuditCase:
    from kirby2.simulation.clock import SimulationClock
    from kirby2.full_day.components_flow import (
        QueueReactiveFlowComponentAdapterV2,
        QueueReactiveFlowOwnerV1,
        QueueReactiveObservationCutV1,
        SIMPLE_FLOW_RNG_LABEL,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        SubstreamDeclarationV1,
        derive_substream_seed,
    )
    from kirby2.full_day.runtime import FullDayRuntime

    failures: list[str] = []
    refused = 0
    runtime = _wo31e2_queue_runtime()
    runtime.advance_to(1_300_000_000)
    runtime.capture_quiescent_cut("WO31-E2-QUEUE-OWNERSHIP")
    owner = runtime.queue_reactive_flow
    if owner is None:
        return FullDayAuditCase(
            "full_day_queue_reactive_ownership_refusals",
            "owner=ABSENT",
            ("queue-reactive owner is absent",),
        )
    adapter = QueueReactiveFlowComponentAdapterV2()
    snapshot = adapter.snapshot(owner)
    restored = adapter.restore(snapshot, plan=runtime.plan)
    if restored.canonical_state_bytes() != owner.canonical_state_bytes():
        failures.append("queue component adapter is not a fixed point")
    owner.clock = SimulationClock()
    try:
        runtime.assert_invariants()
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("queue flow smuggled a second clock")
    finally:
        del owner.clock
    forged = copy.deepcopy(snapshot.as_dict())
    forged["state"]["retained_windows"]["queue_changes"][-1][1] += 1
    try:
        type(snapshot).from_dict(forged)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("forged queue snapshot digest was accepted")
    wrong_model = owner.checkpoint_state()
    wrong_model["model_id_version"]["model_id"] = "SIMPLE_POISSON_FLOW_V1"
    try:
        QueueReactiveFlowOwnerV1.from_checkpoint_state(
            wrong_model,
            plan=runtime.plan,
        )
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("wrong-model queue state was accepted")
    corrupt_window = owner.checkpoint_state()
    corrupt_window["retained_windows"]["queue_changes"][-1][1] += 1
    try:
        QueueReactiveFlowOwnerV1.from_checkpoint_state(
            corrupt_window,
            plan=runtime.plan,
        )
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("corrupt queue retained window was accepted")

    stale_owner = QueueReactiveFlowOwnerV1(
        runtime.plan,
        _wo31e2_queue_configuration(),
    )
    initial = QueueReactiveObservationCutV1(
        schema_version=1,
        simulation_time_us=0,
        best_bid_ticks=None,
        best_ask_ticks=None,
        best_bid_size=0,
        best_ask_size=0,
        depth_near_touch_bid=0,
        depth_near_touch_ask=0,
        cumulative_trade_count=0,
        cumulative_aggressive_buy_volume=0,
        cumulative_aggressive_sell_volume=0,
        reference_price_ticks=runtime.engine.rules.reference_price_ticks,
        cancellable_bid_order_ids=(),
        cancellable_ask_order_ids=(),
    )
    proposal = stale_owner.plan_next(
        initial,
        horizon_us=runtime.plan.calendar.end_time_us,
    )
    if proposal is None:
        failures.append("queue stale-observation probe scheduled no proposal")
    else:
        stale_owner.resolve_pending(
            applied=False,
            rejection_reason="AUDIT_QUEUE_STALE_SETUP",
        )
        for label, cut in (
            ("stale", replace(initial, simulation_time_us=0)),
            (
                "rollback",
                replace(
                    initial,
                    simulation_time_us=proposal.scheduled_time_us,
                    cumulative_trade_count=1,
                    cumulative_aggressive_buy_volume=1,
                ),
            ),
        ):
            if label == "rollback":
                accepted = stale_owner.plan_next(
                    cut,
                    horizon_us=runtime.plan.calendar.end_time_us,
                )
                if accepted is not None:
                    stale_owner.resolve_pending(
                        applied=False,
                        rejection_reason="AUDIT_QUEUE_ROLLBACK_SETUP",
                    )
                rollback_cut = replace(
                    cut,
                    simulation_time_us=cut.simulation_time_us + 1,
                    cumulative_trade_count=0,
                    cumulative_aggressive_buy_volume=0,
                )
                operation = lambda value=rollback_cut: stale_owner.plan_next(
                    value,
                    horizon_us=runtime.plan.calendar.end_time_us,
                )
            else:
                operation = lambda value=cut: stale_owner.plan_next(
                    value,
                    horizon_us=runtime.plan.calendar.end_time_us,
                )
            try:
                operation()
            except (TypeError, ValueError, RuntimeError):
                refused += 1
            else:
                failures.append(f"{label} queue observation was accepted")

    simple_configuration = _wo31e2_simple_configuration()
    plan = runtime.plan
    two_flow_plan = replace(
        plan,
        component_configurations=tuple(
            sorted(
                (
                    *plan.component_configurations,
                    ComponentConfigurationBindingV1(
                        "FLOW_SIMPLE_V1",
                        simple_configuration.reference,
                    ),
                ),
                key=lambda item: item.sort_key,
            )
        ),
        seed_policy=replace(
            plan.seed_policy,
            substreams=tuple(
                sorted(
                    (
                        *plan.seed_policy.substreams,
                        SubstreamDeclarationV1(
                            SIMPLE_FLOW_RNG_LABEL,
                            derive_substream_seed(
                                plan.seed_policy.root_seed,
                                plan.seed_policy.policy_version,
                                SIMPLE_FLOW_RNG_LABEL,
                            ),
                        ),
                    ),
                    key=lambda item: item.semantic_path,
                )
            ),
        ),
    )
    try:
        FullDayRuntime.create_with_agent_scheduler(
            two_flow_plan,
            _wo31e1_population(two_flow_plan),
            simple_flow_configuration=simple_configuration,
            queue_reactive_flow_configuration=_wo31e2_queue_configuration(),
        )
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("two executable flows bypassed exactly-one selection")

    atomic = _wo31e2_queue_runtime()
    pending = atomic.queue_reactive_flow.pending_proposal
    if pending is None:
        failures.append("queue failure-atomicity probe has no pending proposal")
    else:
        atomic.advance_to(pending.scheduled_time_us - 1)
        atomic.capture_quiescent_cut("WO31-E2-QUEUE-ATOMIC-PREFIX")
        before = atomic.canonical_state_bytes()
        original_emit_native = atomic._emit_native

        def refuse_flow_native(**kwargs):
            if kwargs.get("owner_component_id") == "FLOW_QUEUE_REACTIVE_V1":
                raise RuntimeError("AUDIT_FORCED_QUEUE_NATIVE_FAILURE")
            return original_emit_native(**kwargs)

        atomic._emit_native = refuse_flow_native
        try:
            atomic.advance_to(pending.scheduled_time_us)
        except RuntimeError as error:
            if "AUDIT_FORCED_QUEUE_NATIVE_FAILURE" not in str(error):
                failures.append("queue failure-atomicity raised the wrong error")
            else:
                refused += 1
        else:
            failures.append("queue native-ledger failure was not propagated")
        finally:
            del atomic._emit_native
        if atomic.canonical_state_bytes() != before:
            failures.append("queue failure changed owner/engine/RNG/allocator state")
    if owner.rng is runtime.agent_scheduler.agents["AUDIT_MAKER"].rng:
        failures.append("queue flow shares an agent RNG object")
    return FullDayAuditCase(
        "full_day_queue_reactive_ownership_refusals",
        (
            f"refusals={refused} second_clock=true forged_state=true "
            "wrong_model=true corrupt_window=true stale_observation=true "
            "cumulative_rollback=true multiple_flow_selection=true "
            "failure_atomicity=true agent_rng_separation=true"
        ),
        tuple(failures),
    )


def audit_wo31e2_queue_reactive_flow_slice() -> tuple[FullDayAuditCase, ...]:
    """Exercise the final queue-reactive execution and restore slice."""

    return (
        _wo31e2_queue_composition_case(),
        _wo31e2_queue_one_shot_case(),
        _wo31e2_queue_fresh_restore_case(),
        _wo31e2_queue_ownership_case(),
    )


def audit_wo31e2_flow_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise all three exactly-one restorable full-day flow models."""

    return (
        *audit_wo31e2_simple_flow_slice(),
        *audit_wo31e2_hawkes_flow_slice(),
        *audit_wo31e2_queue_reactive_flow_slice(),
    )


def _wo31e3_delivery_configuration():
    from kirby2.full_day.components_delivery import DeliveryConfigurationV1

    return DeliveryConfigurationV1.from_builtin(
        configuration_id="AUDIT_DELIVERY_ASYNC_V1",
        configuration_version=1,
        latency_profile_name="NORMAL",
    )


def _wo31e3_flow_configuration():
    """Keep the required flow adapter active without crowding delivery probes."""

    return replace(
        _wo31e2_simple_configuration(),
        configuration_id="AUDIT_DELIVERY_BACKGROUND_FLOW_V1",
        limit_buy_microevents_per_second=1,
        limit_sell_microevents_per_second=1,
        market_buy_microevents_per_second=1,
        market_sell_microevents_per_second=1,
        cancel_bid_microevents_per_second=1,
        cancel_ask_microevents_per_second=1,
    )


def _wo31e3_plan():
    from kirby2.full_day.components_delivery import DELIVERY_RNG_LABEL
    from kirby2.full_day.composition import (
        DELIVERY_ASYNC_COMPONENT,
        DELIVERY_PROFILE_ID,
        FLOW_SIMPLE_COMPONENT,
        executable_delivery_composition_matrix,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        SubstreamDeclarationV1,
        VersionedReferenceV1,
        derive_substream_seed,
    )

    base = _wo31e2_simple_plan()
    flow = _wo31e3_flow_configuration()
    delivery = _wo31e3_delivery_configuration()
    matrix = executable_delivery_composition_matrix()
    bindings = tuple(
        replace(binding, configuration=flow.reference)
        if binding.component_id == FLOW_SIMPLE_COMPONENT
        else binding
        for binding in base.component_configurations
    )
    bindings = tuple(
        sorted(
            (
                *bindings,
                ComponentConfigurationBindingV1(
                    DELIVERY_ASYNC_COMPONENT,
                    delivery.reference,
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    declaration = SubstreamDeclarationV1(
        DELIVERY_RNG_LABEL,
        derive_substream_seed(
            base.seed_policy.root_seed,
            base.seed_policy.policy_version,
            DELIVERY_RNG_LABEL,
        ),
    )
    return replace(
        base,
        # All participants remain declared but inactive.  This leaves exactly
        # one flow selected while removing agent timing noise from delivery cuts.
        participant_schedule=(),
        composition_profile=VersionedReferenceV1(
            DELIVERY_PROFILE_ID,
            1,
            matrix.sha256,
        ),
        component_configurations=bindings,
        seed_policy=replace(
            base.seed_policy,
            substreams=tuple(
                sorted(
                    (*base.seed_policy.substreams, declaration),
                    key=lambda item: item.semantic_path,
                )
            ),
        ),
    )


def _wo31e3_runtime():
    from kirby2.full_day.components_delivery import DeliveryOwnerV1
    from kirby2.full_day.components_flow import SimpleFlowOwnerV1
    from kirby2.full_day.runtime import FullDayRuntime

    plan = _wo31e3_plan()
    return FullDayRuntime.create(
        plan,
        simple_flow=SimpleFlowOwnerV1(plan, _wo31e3_flow_configuration()),
        delivery=DeliveryOwnerV1(plan, _wo31e3_delivery_configuration()),
    )


def _wo31e3_limit_request(
    runtime,
    order_id: str,
    *,
    side: Side,
    quantity: int,
) -> AdvancedOrderRequest:
    price = (
        runtime.engine.rules.upper_price_band_ticks
        if side is Side.SELL
        else runtime.engine.rules.lower_price_band_ticks
    )
    return AdvancedOrderRequest(
        order_id=order_id,
        side=side,
        quantity=quantity,
        instruction=OrderInstruction.LIMIT,
        owner=OrderOwner.PLAYER,
        account_id="WO31-E3-CLIENT",
        price_ticks=price,
        time_in_force=OrderInstruction.DAY,
    )


def _wo31e3_market_request(
    order_id: str,
    *,
    side: Side,
    quantity: int,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id=order_id,
        side=side,
        quantity=quantity,
        instruction=OrderInstruction.MARKET,
        owner=OrderOwner.PLAYER,
        account_id="WO31-E3-CLIENT",
        time_in_force=OrderInstruction.DAY,
    )


def _wo31e3_drain_delivery(runtime) -> None:
    while runtime.delivery.pending_messages:
        runtime.advance_to(
            min(
                message.delivery_time_us
                for message in runtime.delivery.pending_messages.values()
            )
        )


def _wo31e3_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_delivery import DeliveryComponentAdapterV1
    from kirby2.full_day.components_flow import (
        HawkesFlowComponentAdapterV2,
        QueueReactiveFlowComponentAdapterV2,
        SimpleFlowComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import MarketMechanicsComponentAdapterV1
    from kirby2.full_day.composition import (
        DELIVERY_ASYNC_COMPONENT,
        DELIVERY_PROFILE_ID,
        FLOW_SIMPLE_COMPONENT,
        executable_delivery_composition_matrix,
        executable_queue_reactive_flow_composition_matrix,
    )

    failures: list[str] = []
    previous = executable_queue_reactive_flow_composition_matrix()
    matrix = executable_delivery_composition_matrix()
    if tuple(row.canonical_bytes() for row in matrix.profiles[:-1]) != tuple(
        row.canonical_bytes() for row in previous.profiles
    ):
        failures.append("delivery matrix rewrote a published E1/E2 profile")
    profile = matrix.profile(DELIVERY_PROFILE_ID, 1)
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
            HawkesFlowComponentAdapterV2(),
            QueueReactiveFlowComponentAdapterV2(),
            SimpleFlowComponentAdapterV1(),
            DeliveryComponentAdapterV1(),
        ),
        plan=_wo31e3_plan(),
        profile=profile,
    )
    if DELIVERY_ASYNC_COMPONENT not in graph.active_component_ids:
        failures.append("configured delivery adapter did not activate")
    active_flows = set(graph.active_component_ids).intersection(
        {"FLOW_HAWKES_V1", "FLOW_QUEUE_REACTIVE_V1", "FLOW_SIMPLE_V1"}
    )
    if active_flows != {FLOW_SIMPLE_COMPONENT}:
        failures.append("delivery profile does not retain exactly one active flow")
    if "ASYNCHRONOUS_EXECUTION_SESSION" not in profile.refused_component_ids:
        failures.append("legacy second-session owner is no longer refused")
    runtime = _wo31e3_runtime()
    if runtime.agent_scheduler is not None or runtime.delivery is None:
        failures.append("delivery audit plan activated an unexpected scheduler owner")
    latency_free = _wo31e2_simple_runtime()
    latency_free.advance_to(0)
    latency_free.capture_quiescent_cut("WO31-E3-LATENCY-FREE-ABSENCE")
    if "delivery" in latency_free.checkpoint_state():
        failures.append("latency-free flow profile serialized delivery state")
    return FullDayAuditCase(
        "full_day_delivery_composition",
        (
            f"matrix_v5_sha256={previous.sha256} matrix_v6_sha256={matrix.sha256} "
            f"active_components={graph.active_component_ids} "
            "one_runtime=true one_clock=true one_book=true"
        ),
        tuple(failures),
    )


def _wo31e3_timeline_case() -> FullDayAuditCase:
    from kirby2.full_day.events import FullDayEventTypeV1
    from kirby2.simulation.rng import SeededRng

    failures: list[str] = []
    one_shot = _wo31e3_runtime()
    subdivided = _wo31e3_runtime()
    target = 1_300_000_000
    one_shot.advance_to(target)
    for time_us in (0, 600_000_000, 1_200_000_000, 1_250_000_000, target):
        subdivided.advance_to(time_us)
    one_shot.capture_quiescent_cut("WO31-E3-EQUIVALENCE")
    subdivided.capture_quiescent_cut("WO31-E3-EQUIVALENCE")
    if one_shot.canonical_state_bytes() != subdivided.canonical_state_bytes():
        failures.append("delivery differs under one-shot and subdivided advance")

    runtime = _wo31e3_runtime()
    runtime.advance_to(1_200_000_000)
    maker = _wo31e3_limit_request(
        runtime,
        "E3-MAKER",
        side=Side.SELL,
        quantity=10,
    )
    route_work = runtime.submit_request(maker)
    if "E3-MAKER" in {order.request.order_id for order in runtime.engine.orders}:
        failures.append("client submission reached venue before its route work")
    runtime.advance_to(route_work.key.simulation_time_us)
    pending_kinds = {message.kind for message in runtime.delivery.pending_messages.values()}
    if "ORDER_ACK" not in pending_kinds or runtime.engine.get_order("E3-MAKER").status != "WORKING":
        failures.append("venue truth and pending acknowledgement were not separated")
    _wo31e3_drain_delivery(runtime)
    if runtime.delivery.client_known_orders.get("E3-MAKER", {}).get("status") != "WORKING":
        failures.append("delivered acknowledgement did not establish client working state")

    partial = runtime.submit_request(
        _wo31e3_market_request("E3-PARTIAL", side=Side.BUY, quantity=4)
    )
    runtime.advance_to(partial.key.simulation_time_us)
    venue_status = runtime.engine.get_order("E3-MAKER").status
    client_status = runtime.delivery.client_known_orders["E3-MAKER"]["status"]
    if venue_status != "PARTIALLY_FILLED" or client_status != "WORKING":
        failures.append("partial fill did not produce a stale client-known order state")
    latest_market = runtime.delivery.latest_market_state
    if (
        not isinstance(latest_market, Mapping)
        or type(latest_market.get("simulation_time_us")) is not int
        or latest_market["simulation_time_us"] >= runtime.clock.current_time_us
    ):
        failures.append("client market projection was not stale while venue truth advanced")
    if not any(message.kind == "FILL_REPORT" for message in runtime.delivery.pending_messages.values()):
        failures.append("partial fill did not schedule a delayed fill report")
    _wo31e3_drain_delivery(runtime)
    if runtime.delivery.client_known_orders["E3-MAKER"]["status"] != "PARTIALLY_FILLED":
        failures.append("partial-fill delivery did not update client order state")

    cancel = runtime.cancel_order("E3-MAKER", reason="AUDIT_CANCEL_WINS")
    racing_fill = runtime.submit_request(
        _wo31e3_market_request("E3-RACE-FILL", side=Side.BUY, quantity=6)
    )
    if cancel.key.simulation_time_us != racing_fill.key.simulation_time_us:
        failures.append("cancel/fill race did not share one venue timestamp")
    runtime.advance_to(cancel.key.simulation_time_us)
    if runtime.engine.get_order("E3-MAKER").status != "CANCELLED":
        failures.append("deterministic cancel-wins route ordering changed")
    _wo31e3_drain_delivery(runtime)
    if runtime.delivery.client_known_orders["E3-MAKER"]["status"] != "CANCELLED":
        failures.append("cancel acknowledgement did not reach client state")

    # Force one real acknowledgement onto the closing calendar timestamp.  The
    # NORMAL route path is fixed; cloning the owned RNG predicts only the next
    # bounded downlink draw and does not mutate authoritative state.
    simultaneous = _wo31e3_runtime()
    boundary = 4_800_000_000
    simultaneous.advance_to(boundary - 4_000)
    clone = SeededRng.from_runtime_state(simultaneous.delivery.rng.runtime_state())
    acknowledgement_delay = clone.integer(300, 700)
    source = boundary - 2_700 - acknowledgement_delay
    simultaneous.advance_to(source)
    work = simultaneous.submit_request(
        _wo31e3_limit_request(
            simultaneous,
            "E3-BOUNDARY",
            side=Side.BUY,
            quantity=1,
        )
    )
    simultaneous.advance_to(work.key.simulation_time_us)
    boundary_ack = tuple(
        message
        for message in simultaneous.delivery.pending_messages.values()
        if message.kind == "ORDER_ACK" and message.delivery_time_us == boundary
    )
    if len(boundary_ack) != 1:
        failures.append("acknowledgement was not scheduled on the calendar boundary")
    simultaneous.advance_to(boundary)
    boundary_events = tuple(
        event.event_type
        for event in simultaneous.events
        if event.simulation_time_us == boundary
    )
    if (
        FullDayEventTypeV1.CALENDAR_BOUNDARY not in boundary_events
        or FullDayEventTypeV1.OBSERVABLE_DELIVERY not in boundary_events
        or boundary_events.index(FullDayEventTypeV1.CALENDAR_BOUNDARY)
        > boundary_events.index(FullDayEventTypeV1.OBSERVABLE_DELIVERY)
    ):
        failures.append("calendar/delivery stage ordering is not deterministic")

    return FullDayAuditCase(
        "full_day_delivery_timelines_and_races",
        (
            f"equivalence_target_us={target} routes={runtime.delivery.route_sequence} "
            f"messages={runtime.delivery.message_sequence} pending_ack=true "
            "partial_fill=true cancel_fill_race=cancel_wins stale_quote=true "
            "calendar_delivery_same_time=true"
        ),
        tuple(failures),
    )


def _wo31e3_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes, parse_canonical_json_object
    from kirby2.full_day.restore import (
        FullDayRuntimeRestoreRequestV1,
        execute_uninterrupted_full_day_runtime_suffix,
    )

    failures: list[str] = []
    digests: list[str] = []

    def verify(runtime, label: str, target: int) -> None:
        runtime.capture_quiescent_cut(f"WO31-E3-{label}-CUT")
        request = FullDayRuntimeRestoreRequestV1.capture(
            runtime,
            suffix_targets_us=(target,),
            final_checkpoint_request_id=f"WO31-E3-{label}-FINAL",
        )
        returncode, stdout, stderr, wrote_files = _wo31e1_run_worker(
            request.canonical_bytes()
        )
        expected = execute_uninterrupted_full_day_runtime_suffix(runtime, request)
        if returncode != 0:
            failures.append(
                f"{label} fresh worker returned {returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
            return
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"{label} fresh worker emitted invalid JSON: {error}")
            return
        if stderr or wrote_files:
            failures.append(f"{label} fresh worker produced side effects")
        if canonical_json_bytes(actual) != stdout or actual != expected:
            failures.append(f"{label} fresh suffix differs from uninterrupted suffix")
        digests.append(str(actual["invariant_sha256"]))

    pending_route = _wo31e3_runtime()
    pending_route.advance_to(1_200_000_000)
    route = pending_route.submit_request(
        _wo31e3_limit_request(
            pending_route,
            "E3-RESTORE-ROUTE",
            side=Side.SELL,
            quantity=10,
        )
    )
    verify(pending_route, "PENDING-ROUTE", route.key.simulation_time_us + 2_000)

    pending_ack = _wo31e3_runtime()
    pending_ack.advance_to(1_200_000_000)
    route = pending_ack.submit_request(
        _wo31e3_limit_request(
            pending_ack,
            "E3-RESTORE-ACK",
            side=Side.SELL,
            quantity=10,
        )
    )
    pending_ack.advance_to(route.key.simulation_time_us)
    ack_target = max(
        message.delivery_time_us
        for message in pending_ack.delivery.pending_messages.values()
    )
    verify(pending_ack, "PENDING-ACK", ack_target)

    partial_race = _wo31e3_runtime()
    partial_race.advance_to(1_200_000_000)
    route = partial_race.submit_request(
        _wo31e3_limit_request(
            partial_race,
            "E3-RESTORE-RACE",
            side=Side.SELL,
            quantity=10,
        )
    )
    partial_race.advance_to(route.key.simulation_time_us)
    _wo31e3_drain_delivery(partial_race)
    route = partial_race.submit_request(
        _wo31e3_market_request("E3-RESTORE-PARTIAL", side=Side.BUY, quantity=4)
    )
    partial_race.advance_to(route.key.simulation_time_us)
    cancel = partial_race.cancel_order("E3-RESTORE-RACE", reason="RESTORE_RACE")
    verify(partial_race, "PARTIAL-CANCEL-RACE", cancel.key.simulation_time_us + 5_000)

    return FullDayAuditCase(
        "full_day_delivery_fresh_process_restore",
        (
            f"fresh_process_boundaries=3 invariant_sha256={','.join(digests)} "
            "pending_route=true pending_ack=true partial_cancel_race=true"
        ),
        tuple(failures),
    )


def _wo31e3_ownership_case() -> FullDayAuditCase:
    from kirby2.full_day.components import ComponentSnapshotV1
    from kirby2.full_day.components_delivery import (
        DeliveryComponentAdapterV1,
        DeliveryConfigurationV1,
        DeliveryMessageV1,
        DeliveryOwnerV1,
    )
    from kirby2.full_day.runtime import FullDayRuntime
    from kirby2.simulation.clock import SimulationClock

    failures: list[str] = []
    refused = 0
    runtime = _wo31e3_runtime()
    runtime.advance_to(1_200_000_000)
    route = runtime.submit_request(
        _wo31e3_limit_request(runtime, "E3-HOSTILE", side=Side.SELL, quantity=5)
    )
    runtime.capture_quiescent_cut("WO31-E3-HOSTILE-PREFIX")
    adapter = DeliveryComponentAdapterV1()
    snapshot = adapter.snapshot(runtime.delivery)
    restored = adapter.restore(snapshot, plan=runtime.plan)
    if restored.canonical_state_bytes() != runtime.delivery.canonical_state_bytes():
        failures.append("delivery adapter restore is not a fixed point")

    runtime.delivery.clock = SimulationClock()
    try:
        runtime.assert_invariants()
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("delivery component smuggled a second clock")
    finally:
        del runtime.delivery.clock

    forged = copy.deepcopy(snapshot.as_dict())
    forged["state"]["route_sequence"] += 1
    try:
        ComponentSnapshotV1.from_dict(forged)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("forged delivery snapshot digest was accepted")

    unknown_kind = copy.deepcopy(runtime.delivery.checkpoint_state())
    if unknown_kind["pending_messages"]:
        unknown_kind["pending_messages"][0]["kind"] = "UNKNOWN_CLIENT_KIND"
    else:
        runtime.advance_to(route.key.simulation_time_us)
        unknown_kind = copy.deepcopy(runtime.delivery.checkpoint_state())
        unknown_kind["pending_messages"][0]["kind"] = "UNKNOWN_CLIENT_KIND"
    try:
        DeliveryOwnerV1.from_checkpoint_state(unknown_kind, plan=runtime.plan)
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("unknown delivery message kind was restored")

    runtime.advance_to(route.key.simulation_time_us)
    if runtime.delivery.pending_messages:
        message_id = next(iter(runtime.delivery.pending_messages))
        original = runtime.delivery.pending_messages[message_id]
        payload = original.as_dict()
        payload["causal_outer_event_ids"] = ["event:999999999"]
        runtime.delivery.pending_messages[message_id] = DeliveryMessageV1.from_dict(payload)
        try:
            runtime.assert_invariants()
        except (TypeError, ValueError, RuntimeError):
            refused += 1
        else:
            failures.append("orphan delivery causal ID was accepted")
        runtime.delivery.pending_messages[message_id] = original

    atomic = _wo31e3_runtime()
    atomic.advance_to(1_200_000_000)
    due = atomic.submit_request(
        _wo31e3_limit_request(atomic, "E3-ATOMIC", side=Side.BUY, quantity=1)
    )
    atomic.capture_quiescent_cut("WO31-E3-ATOMIC-PREFIX")
    atomic_before = atomic.canonical_state_bytes()
    atomic_original_emit = atomic._emit_native

    def refuse_atomic_delivery(**kwargs):
        if kwargs.get("owner_component_id") == "DELIVERY_ASYNC_V1":
            raise RuntimeError("AUDIT_FORCED_DELIVERY_NATIVE_FAILURE")
        return atomic_original_emit(**kwargs)

    atomic._emit_native = refuse_atomic_delivery
    try:
        atomic.advance_to(due.key.simulation_time_us)
    except RuntimeError as error:
        if "AUDIT_FORCED_DELIVERY_NATIVE_FAILURE" in str(error):
            refused += 1
        else:
            failures.append("delivery atomicity probe raised the wrong error")
    else:
        failures.append("delivery native failure was not propagated")
    finally:
        del atomic._emit_native
    if atomic.canonical_state_bytes() != atomic_before:
        failures.append("delivery failure changed runtime, queue, RNG, or allocator state")

    try:
        DeliveryConfigurationV1.from_builtin(
            configuration_id="ZERO_DELIVERY_IS_ABSENT",
            configuration_version=1,
            latency_profile_name="ZERO_LATENCY",
        )
    except (TypeError, ValueError):
        refused += 1
    else:
        failures.append("zero-latency active delivery configuration was accepted")

    _wo31e3_drain_delivery(runtime)
    runtime.capture_quiescent_cut("WO31-E3-EMPTY-ACTIVE")
    union = runtime.checkpoint_state()["delivery"]
    if union.get("status") != "PRESERVED" or union["state"]["pending_messages"]:
        failures.append("active empty delivery queue collapsed into absence")
    if runtime.delivery.rng is runtime.simple_flow.rng:
        failures.append("delivery and flow share one RNG object")
    return FullDayAuditCase(
        "full_day_delivery_ownership_refusals",
        (
            f"refusals={refused} second_clock=true forged_digest=true "
            "unknown_kind=true orphan_causal_id=true zero_latency_absent=true "
            "active_empty_preserved=true failure_atomicity=true rng_separation=true"
        ),
        tuple(failures),
    )


def audit_wo31e3_delivery_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise passive venue routing and independently delayed client state."""

    return (
        _wo31e3_composition_case(),
        _wo31e3_timeline_case(),
        _wo31e3_fresh_restore_case(),
        _wo31e3_ownership_case(),
    )


def _wo31e4_research_configuration():
    from kirby2.full_day.components_research import ResearchConfigurationV1

    source = """\
machine full_day_research
window 1s
initial WATCH
state WATCH signal WAIT entry DENY exit ALLOW
state GO signal GREEN entry ALLOW exit ALLOW
transition WATCH -> GO when for 500ms
    working_order_count >= 1
transition GO -> WATCH when
    working_order_count < 1
"""
    return ResearchConfigurationV1.create(
        configuration_id="AUDIT_FEATURE_STRATEGY_PLAYER_V1",
        configuration_version=1,
        strategy_source=source,
        feature_windows_us=(500_000, 1_000_000),
        depth_levels=5,
    )


def _wo31e4_plan():
    from kirby2.full_day.composition import (
        FEATURE_STRATEGY_PLAYER_COMPONENT,
        RESEARCH_PROFILE_ID,
        executable_research_composition_matrix,
    )
    from kirby2.full_day.models import (
        ComponentConfigurationBindingV1,
        VersionedReferenceV1,
    )

    base = _wo31e3_plan()
    configuration = _wo31e4_research_configuration()
    matrix = executable_research_composition_matrix()
    bindings = tuple(
        sorted(
            (
                *base.component_configurations,
                ComponentConfigurationBindingV1(
                    FEATURE_STRATEGY_PLAYER_COMPONENT,
                    configuration.reference,
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    return replace(
        base,
        composition_profile=VersionedReferenceV1(
            RESEARCH_PROFILE_ID,
            1,
            matrix.sha256,
        ),
        component_configurations=bindings,
    )


def _wo31e4_runtime():
    from kirby2.full_day.components_delivery import DeliveryOwnerV1
    from kirby2.full_day.components_flow import SimpleFlowOwnerV1
    from kirby2.full_day.components_research import ResearchOwnerV1
    from kirby2.full_day.runtime import FullDayRuntime

    plan = _wo31e4_plan()
    return FullDayRuntime.create(
        plan,
        simple_flow=SimpleFlowOwnerV1(plan, _wo31e3_flow_configuration()),
        delivery=DeliveryOwnerV1(plan, _wo31e3_delivery_configuration()),
        research=ResearchOwnerV1(plan, _wo31e4_research_configuration()),
    )


def _wo31e4_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.components import (
        AgentSchedulerComponentAdapterV1,
        ComponentAdapterGraphV1,
        FullDayRuntimeComponentAdapterV1,
    )
    from kirby2.full_day.components_delivery import DeliveryComponentAdapterV1
    from kirby2.full_day.components_flow import (
        HawkesFlowComponentAdapterV2,
        QueueReactiveFlowComponentAdapterV2,
        SimpleFlowComponentAdapterV1,
    )
    from kirby2.full_day.components_mechanics import MarketMechanicsComponentAdapterV1
    from kirby2.full_day.components_research import ResearchComponentAdapterV1
    from kirby2.full_day.composition import (
        FEATURE_STRATEGY_PLAYER_COMPONENT,
        RESEARCH_PROFILE_ID,
        executable_delivery_composition_matrix,
        executable_research_composition_matrix,
    )

    failures: list[str] = []
    previous = executable_delivery_composition_matrix()
    matrix = executable_research_composition_matrix()
    if tuple(row.canonical_bytes() for row in matrix.profiles[:-1]) != tuple(
        row.canonical_bytes() for row in previous.profiles
    ):
        failures.append("research matrix rewrote a published E1/E2/E3 profile")
    profile = matrix.profile(RESEARCH_PROFILE_ID, 1)
    graph = ComponentAdapterGraphV1(
        (
            FullDayRuntimeComponentAdapterV1(),
            MarketMechanicsComponentAdapterV1(),
            AgentSchedulerComponentAdapterV1(),
            HawkesFlowComponentAdapterV2(),
            QueueReactiveFlowComponentAdapterV2(),
            SimpleFlowComponentAdapterV1(),
            DeliveryComponentAdapterV1(),
            ResearchComponentAdapterV1(),
        ),
        plan=_wo31e4_plan(),
        profile=profile,
    )
    if FEATURE_STRATEGY_PLAYER_COMPONENT not in graph.active_component_ids:
        failures.append("configured research adapter did not activate")
    research_spec = next(
        item
        for item in profile.components
        if item.component_id == FEATURE_STRATEGY_PLAYER_COMPONENT
    )
    forbidden = {"ORDER_BOOK", "AUCTION_BOOK", "MARKET_MECHANICS_ENGINE", "CASH_LEDGER"}
    if forbidden.intersection(research_spec.owned_resources):
        failures.append("research component claims venue truth or cash ownership")
    runtime = _wo31e4_runtime()
    if (
        runtime.research is None
        or runtime.delivery is None
        or runtime.research.client_view() is runtime.engine.book
    ):
        failures.append("research owner is not detached from venue truth")
    return FullDayAuditCase(
        "full_day_research_composition",
        (
            f"matrix_v6_sha256={previous.sha256} matrix_v7_sha256={matrix.sha256} "
            f"active_components={graph.active_component_ids} "
            "passive_client_cut=true cash_ledger=false"
        ),
        tuple(failures),
    )


def _wo31e4_observable_decisions_case() -> FullDayAuditCase:
    from kirby2.full_day.events import FullDayEventTypeV1

    failures: list[str] = []
    runtime = _wo31e4_runtime()
    runtime.advance_to(1_200_000_000)
    route = runtime.submit_request(
        _wo31e3_limit_request(
            runtime,
            "E4-WORKING-ORDER",
            side=Side.SELL,
            quantity=10,
        )
    )
    runtime.advance_to(route.key.simulation_time_us)
    _wo31e3_drain_delivery(runtime)
    if (
        runtime.research.processed_message_ids
        != [row["message_id"] for row in runtime.delivery.delivered_messages]
        or len(runtime.research.feature_batches)
        != len(runtime.delivery.delivered_messages)
    ):
        failures.append("research features do not exactly cover delivered messages")
    if any(
        row["information_cutoff_us"] > row["delivery_time_us"]
        for row in runtime.research.feature_batches
    ):
        failures.append("research feature evidence sees beyond client delivery time")
    deadline = runtime.research.next_strategy_deadline_us
    if deadline is None:
        failures.append("observable working order did not schedule TRUE_FOR deadline")
    else:
        runtime.advance_to(deadline - 1)
        if runtime.research.strategy.current.machine_state != "WATCH":
            failures.append("strategy transitioned before its exact timer")
        runtime.advance_to(deadline)
        if runtime.research.strategy.current.machine_state != "GO":
            failures.append("strategy did not transition on its exact timer")

    request = _wo31e3_limit_request(
        runtime,
        "E4-PLAYER-DECISION",
        side=Side.BUY,
        quantity=2,
    )
    decision = runtime.schedule_player_decision(
        action="SUBMIT",
        action_payload={"request": request.as_dict()},
        at_time_us=runtime.clock.current_time_us + 1_000,
    )
    if decision.information_cutoff_us != runtime.research.last_information_cutoff_us:
        failures.append("player decision did not bind the current observable cutoff")
    runtime.advance_to(decision.scheduled_time_us)
    completed = runtime.research.completed_decisions[-1]
    route_work_id = completed["route_work_id"]
    routed = runtime._pending.get(route_work_id)
    if routed is None or routed.key.source_component_id != "DELIVERY_ASYNC_V1":
        failures.append("player decision bypassed the ordinary delivery route")

    fill_route = runtime.submit_request(
        _wo31e3_market_request(
            "E4-PLAYER-FILL",
            side=Side.BUY,
            quantity=4,
        )
    )
    runtime.advance_to(fill_route.key.simulation_time_us)
    _wo31e3_drain_delivery(runtime)
    if (
        runtime.research.fill_report_cursor
        != len(runtime.delivery.client_fill_reports)
        or runtime.research.player_position.position
        != runtime.delivery.client_position
        or not runtime.research.player_position.fills
    ):
        failures.append("player fill/position projection differs from client reports")
    feature_events = sum(
        event.event_type is FullDayEventTypeV1.FEATURE_UPDATED
        for event in runtime.events
    )
    deadline_events = sum(
        event.event_type is FullDayEventTypeV1.STRATEGY_ALGORITHM_DEADLINE
        for event in runtime.events
    )
    if feature_events != len(runtime.research.feature_batches):
        failures.append("feature outer events differ from feature batches")
    return FullDayAuditCase(
        "full_day_research_observable_decisions",
        (
            f"features={feature_events} deadlines_and_decisions={deadline_events} "
            f"fill_reports={runtime.research.fill_report_cursor} "
            "cutoff_bound=true routed_actions=true timer_exact=true"
        ),
        tuple(failures),
    )


def _wo31e4_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes, parse_canonical_json_object
    from kirby2.full_day.restore import (
        FullDayRuntimeRestoreRequestV1,
        execute_uninterrupted_full_day_runtime_suffix,
    )

    failures: list[str] = []
    digests: list[str] = []

    def verify(runtime, label: str, target: int) -> None:
        runtime.capture_quiescent_cut(f"WO31-E4-{label}-CUT")
        request = FullDayRuntimeRestoreRequestV1.capture(
            runtime,
            suffix_targets_us=(target,),
            final_checkpoint_request_id=f"WO31-E4-{label}-FINAL",
        )
        returncode, stdout, stderr, wrote_files = _wo31e1_run_worker(
            request.canonical_bytes()
        )
        expected = execute_uninterrupted_full_day_runtime_suffix(runtime, request)
        if returncode != 0:
            failures.append(
                f"{label} fresh worker returned {returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
            return
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"{label} fresh worker emitted invalid JSON: {error}")
            return
        if stderr or wrote_files:
            failures.append(f"{label} fresh worker produced side effects")
        if canonical_json_bytes(actual) != stdout or actual != expected:
            failures.append(f"{label} fresh suffix differs from uninterrupted suffix")
        digests.append(str(actual["invariant_sha256"]))

    pending_feature = _wo31e4_runtime()
    pending_feature.advance_to(1_200_000_000)
    route = pending_feature.submit_request(
        _wo31e3_limit_request(
            pending_feature,
            "E4-RESTORE-FEATURE",
            side=Side.SELL,
            quantity=5,
        )
    )
    pending_feature.advance_to(route.key.simulation_time_us)
    feature_target = max(
        message.delivery_time_us
        for message in pending_feature.delivery.pending_messages.values()
    )
    verify(pending_feature, "PENDING-FEATURE", feature_target)

    pending_deadline = _wo31e4_runtime()
    pending_deadline.advance_to(1_200_000_000)
    route = pending_deadline.submit_request(
        _wo31e3_limit_request(
            pending_deadline,
            "E4-RESTORE-DEADLINE",
            side=Side.SELL,
            quantity=5,
        )
    )
    pending_deadline.advance_to(route.key.simulation_time_us)
    _wo31e3_drain_delivery(pending_deadline)
    deadline_target = pending_deadline.research.next_strategy_deadline_us
    if deadline_target is None:
        failures.append("deadline restore setup did not create a timer")
    else:
        verify(pending_deadline, "PENDING-DEADLINE", deadline_target)

    pending_decision = _wo31e4_runtime()
    pending_decision.advance_to(1_200_000_000)
    request = _wo31e3_limit_request(
        pending_decision,
        "E4-RESTORE-DECISION",
        side=Side.BUY,
        quantity=2,
    )
    decision = pending_decision.schedule_player_decision(
        action="SUBMIT",
        action_payload={"request": request.as_dict()},
        at_time_us=pending_decision.clock.current_time_us + 1_000,
    )
    verify(pending_decision, "PENDING-DECISION", decision.scheduled_time_us)

    return FullDayAuditCase(
        "full_day_research_fresh_process_restore",
        (
            f"fresh_process_boundaries={len(digests)} "
            f"invariant_sha256={','.join(digests)} "
            "pending_feature=true pending_timer=true pending_decision=true"
        ),
        tuple(failures),
    )


def _wo31e4_ownership_case() -> FullDayAuditCase:
    from kirby2.full_day.components_research import ResearchOwnerV1
    from kirby2.full_day.composition import ABSENT_REASON_SYNTHETIC_NO_HISTORICAL_CURSOR

    failures: list[str] = []
    refused = 0
    runtime = _wo31e4_runtime()
    runtime.advance_to(1_200_000_000)
    route = runtime.submit_request(
        _wo31e3_limit_request(
            runtime,
            "E4-HOSTILE",
            side=Side.SELL,
            quantity=5,
        )
    )
    runtime.advance_to(route.key.simulation_time_us)
    _wo31e3_drain_delivery(runtime)
    runtime.capture_quiescent_cut("WO31-E4-HOSTILE-PREFIX")
    research_state = runtime.research.checkpoint_state()

    probes: list[tuple[str, Callable[[], object]]] = []
    unknown_version = copy.deepcopy(research_state)
    unknown_version["configuration"]["strategy_version"] = 999
    probes.append(
        (
            "unknown strategy version",
            lambda: ResearchOwnerV1.from_checkpoint_state(
                unknown_version,
                plan=runtime.plan,
                delivery=runtime.delivery,
            ),
        )
    )
    future_cutoff = copy.deepcopy(research_state)
    future_cutoff["last_information_cutoff_us"] = (
        future_cutoff["last_observation_time_us"] + 1
    )
    probes.append(
        (
            "future information cutoff",
            lambda: ResearchOwnerV1.from_checkpoint_state(
                future_cutoff,
                plan=runtime.plan,
                delivery=runtime.delivery,
            ),
        )
    )
    cash = copy.deepcopy(research_state)
    cash["cash_ledger"] = {"balance": 0}
    probes.append(
        (
            "cash ledger smuggling",
            lambda: ResearchOwnerV1.from_checkpoint_state(
                cash,
                plan=runtime.plan,
                delivery=runtime.delivery,
            ),
        )
    )
    orphan = copy.deepcopy(research_state)
    orphan["working_order_ids"] = [*orphan["working_order_ids"], "ORPHAN"]
    orphan["working_order_ids"].sort()
    probes.append(
        (
            "orphan working order",
            lambda: ResearchOwnerV1.from_checkpoint_state(
                orphan,
                plan=runtime.plan,
                delivery=runtime.delivery,
            ),
        )
    )
    conservation = copy.deepcopy(research_state)
    conservation["player_position"]["position"] += 1
    probes.append(
        (
            "player position conservation",
            lambda: ResearchOwnerV1.from_checkpoint_state(
                conservation,
                plan=runtime.plan,
                delivery=runtime.delivery,
            ),
        )
    )
    for label, probe in probes:
        refusal = _expect_refusal(probe, label)
        if refusal is None:
            refused += 1
        else:
            failures.append(refusal)

    runtime.research.book = runtime.engine.book
    try:
        runtime.assert_invariants()
    except (TypeError, ValueError, RuntimeError):
        refused += 1
    else:
        failures.append("direct authoritative-book access was accepted")
    finally:
        del runtime.research.book

    atomic = _wo31e4_runtime()
    atomic.advance_to(1_200_000_000)
    route = atomic.submit_request(
        _wo31e3_limit_request(
            atomic,
            "E4-ATOMIC",
            side=Side.SELL,
            quantity=2,
        )
    )
    atomic.advance_to(route.key.simulation_time_us)
    atomic.capture_quiescent_cut("WO31-E4-ATOMIC-PREFIX")
    before = atomic.canonical_state_bytes()
    due = min(
        message.delivery_time_us
        for message in atomic.delivery.pending_messages.values()
    )
    original_emit = atomic._emit_native

    def refuse_research_native(**kwargs):
        if kwargs.get("owner_component_id") == "FEATURE_STRATEGY_PLAYER_V1":
            raise RuntimeError("AUDIT_FORCED_RESEARCH_NATIVE_FAILURE")
        return original_emit(**kwargs)

    atomic._emit_native = refuse_research_native
    try:
        atomic.advance_to(due)
    except RuntimeError as error:
        if "AUDIT_FORCED_RESEARCH_NATIVE_FAILURE" in str(error):
            refused += 1
        else:
            failures.append("research atomicity probe raised the wrong error")
    else:
        failures.append("research native failure was not propagated")
    finally:
        del atomic._emit_native
    if atomic.canonical_state_bytes() != before:
        failures.append("research failure changed runtime or component state")

    presence = runtime.checkpoint_state()["component_presence"]
    historical = next(
        row for row in presence if row["component_id"] == "HISTORICAL_REPLAY"
    )
    if (
        historical["status"] != "ABSENT"
        or historical["reason"] != ABSENT_REASON_SYNTHETIC_NO_HISTORICAL_CURSOR
    ):
        failures.append("synthetic research checkpoint gained a historical cursor")
    return FullDayAuditCase(
        "full_day_research_ownership_refusals",
        (
            f"refusals={refused} second_book=true unknown_version=true "
            "future_cutoff=true orphan_work=true conservation=true cash_absent=true "
            "failure_atomicity=true historical_cursor_absent=true"
        ),
        tuple(failures),
    )


def audit_wo31e4_research_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise client-cut features, strategy timers, and player state."""

    return (
        _wo31e4_composition_case(),
        _wo31e4_observable_decisions_case(),
        _wo31e4_fresh_restore_case(),
        _wo31e4_ownership_case(),
    )


def _wo31e5_owner(boundary: str):
    from kirby2.full_day.components_multivenue import MultiVenueHiddenOwnerV1
    from kirby2.latency import LatencyProfileName, get_latency_profile
    from kirby2.multivenue import MarketCoordinator, RoutePolicy, RoutingRequest, VenueConfig
    from kirby2.observability import (
        HiddenLiquidityRules,
        HiddenOrderRequest,
        IcebergDefinition,
        IcebergRefreshBehavior,
        LiquidityKind,
        RefreshEventVisibility,
    )

    rules = HiddenLiquidityRules(feed_delay_us=500)
    coordinator = MarketCoordinator(
        (
            VenueConfig(
                "A",
                get_latency_profile(LatencyProfileName.LOW_LATENCY),
                hidden_rules=rules,
            ),
            VenueConfig(
                "B",
                get_latency_profile(LatencyProfileName.NORMAL),
                hidden_rules=rules,
            ),
        ),
        seed=31_005,
        depth_subscriptions=frozenset({"A", "B"}),
    )
    coordinator.add_resting_order(
        "A",
        HiddenOrderRequest(
            "E5-A-BID",
            Side.BUY,
            LiquidityKind.DISPLAYED_LIMIT,
            OrderOwner.SIMULATED,
            "E5-SIM",
            200,
            99,
        ),
    )
    coordinator.add_resting_order(
        "A",
        HiddenOrderRequest(
            "E5-A-ICE",
            Side.SELL,
            LiquidityKind.ICEBERG,
            OrderOwner.SIMULATED,
            "E5-SIM",
            200,
            101,
            IcebergDefinition(
                50,
                150,
                50,
                IcebergRefreshBehavior.AUTOMATIC,
                RefreshEventVisibility.QUOTE_UPDATE_ONLY,
            ),
        ),
    )
    coordinator.add_resting_order(
        "B",
        HiddenOrderRequest(
            "E5-B-BID",
            Side.BUY,
            LiquidityKind.DISPLAYED_LIMIT,
            OrderOwner.SIMULATED,
            "E5-SIM",
            200,
            98,
        ),
    )
    coordinator.add_resting_order(
        "B",
        HiddenOrderRequest(
            "E5-B-ASK",
            Side.SELL,
            LiquidityKind.DISPLAYED_LIMIT,
            OrderOwner.SIMULATED,
            "E5-SIM",
            200,
            102,
        ),
    )
    coordinator.advance_to(1_000)
    if boundary == "pending_route":
        coordinator.submit_route(
            RoutingRequest(
                "E5-PREFIX-ROUTE",
                Side.BUY,
                120,
                RoutePolicy.SWEEP,
                max_venues=2,
            )
        )
        coordinator.execute_simulated_market(
            "A",
            "E5-PREFIX-STALE-HIT",
            Side.BUY,
            10,
        )
    elif boundary in {"pending_feed", "reserve"}:
        coordinator.execute_simulated_market(
            "A",
            "E5-PREFIX-HIT",
            Side.BUY,
            60,
        )
        if boundary == "reserve":
            coordinator.advance_to(1_500)
    else:
        raise ValueError(f"unknown WO31-E5 boundary: {boundary}")
    return MultiVenueHiddenOwnerV1(coordinator)


def _wo31e5_suffix(boundary: str):
    from kirby2.multivenue import MultiVenueCommand, RoutePolicy, RoutingRequest

    if boundary == "pending_route":
        first = MultiVenueCommand(1, 5_000, "ADVANCE", {})
    elif boundary == "pending_feed":
        first = MultiVenueCommand(1, 1_500, "ADVANCE", {})
    elif boundary == "reserve":
        first = MultiVenueCommand(
            1,
            2_000,
            "SIM_MARKET",
            {
                "order_id": "E5-SUFFIX-HIT",
                "quantity": 80,
                "side": Side.BUY.value,
                "venue_id": "A",
            },
        )
    else:
        raise ValueError(f"unknown WO31-E5 boundary: {boundary}")
    route_time = 6_000 if boundary == "pending_route" else 3_000
    route = MultiVenueCommand(
        2,
        route_time,
        "ROUTE",
        {
            "request": RoutingRequest(
                f"E5-{boundary.upper()}-SUFFIX-ROUTE",
                Side.BUY,
                20,
                RoutePolicy.BEST_DISPLAYED_PRICE,
            ).as_dict()
        },
    )
    complete = MultiVenueCommand(3, 8_000, "COMPLETE", {})
    return (first, route, complete), 100_000


def _wo31e5_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.composition import (
        FULL_DAY_RUNTIME_COMPONENT,
        MULTIVENUE_HIDDEN_COMPONENT,
        MULTIVENUE_HIDDEN_PROFILE_ID,
        ComponentSpecV1,
        CompositionProfileV1,
        executable_research_composition_matrix,
        restorable_multivenue_hidden_composition_matrix,
    )

    failures: list[str] = []
    previous = executable_research_composition_matrix()
    matrix = restorable_multivenue_hidden_composition_matrix()
    profile = matrix.profile(MULTIVENUE_HIDDEN_PROFILE_ID, 1)
    component = next(
        row for row in profile.components if row.component_id == MULTIVENUE_HIDDEN_COMPONENT
    )
    if matrix.previous_matrix_sha256 != previous.sha256:
        failures.append("E5 composition matrix does not bind the exact E4 matrix")
    if matrix.profiles[:-1] != previous.profiles:
        failures.append("E5 composition matrix rewrote a prior profile")
    if profile.implementation_status != "CONTRACT_ONLY":
        failures.append("mixed multivenue research profile overclaims executable status")
    if component.implementation_status != "RESTORABLE_COMPONENT_ONLY":
        failures.append("multivenue owner lacks exact component-only restore status")
    if set(component.checkpoint_state_ids) != {"HIDDEN_LIQUIDITY_V1", "MULTIVENUE_V1"}:
        failures.append("multivenue checkpoint ownership inventory is incomplete")
    if "HISTORICAL_REPLAY" not in profile.refused_component_ids:
        failures.append("multivenue profile does not refuse historical mixing")

    e4 = previous.profile("SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1", 1)
    duplicate_owner_refused = False
    try:
        CompositionProfileV1(
            schema_version=1,
            profile_id="HOSTILE_DOUBLE_EXCHANGE_OWNER_V1",
            profile_version=1,
            implementation_status="CONTRACT_ONLY",
            runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
            components=tuple(sorted((*e4.components, component), key=lambda row: row.component_id)),
            refused_component_ids=e4.refused_component_ids,
            exactly_one_component_groups=e4.exactly_one_component_groups,
        )
    except (TypeError, ValueError):
        duplicate_owner_refused = True
    if not duplicate_owner_refused:
        failures.append("composition accepted two exchange/book/clock owners")

    return FullDayAuditCase(
        "full_day_multivenue_component_composition",
        (
            f"matrix_version={matrix.matrix_version} profile={profile.profile_id} "
            f"profile_status={profile.implementation_status} "
            f"component_status={component.implementation_status} "
            "prior_profiles_immutable=true double_owner_refused=true "
            "historical_mixing_refused=true"
        ),
        tuple(failures),
    )


def _wo31e5_checkpoint_case() -> FullDayAuditCase:
    from kirby2.full_day.components_multivenue import MultiVenueHiddenOwnerV1

    failures: list[str] = []
    owner = _wo31e5_owner("reserve")
    state = owner.checkpoint_state()
    restored = MultiVenueHiddenOwnerV1.from_canonical_state_bytes(
        owner.canonical_state_bytes()
    )
    if restored.checkpoint_state() != state:
        failures.append("multivenue component checkpoint did not round trip exactly")
    coordinator = state["coordinator"]
    assert isinstance(coordinator, dict)
    venue_a = coordinator["venues"]["A"]
    engine = venue_a["engine"]
    reserve = sum(row["reserve_remaining"] for row in engine["orders"])
    if reserve <= 0:
        failures.append("checkpoint fixture lacks live hidden reserve")
    if not coordinator["observable_cursors"] or not coordinator["truth_cursors"]:
        failures.append("checkpoint omitted observable or truth cursors")
    public = restored.public_projection()
    serialized = str(public).lower()
    if any(token in serialized for token in ("reserve_remaining", "hidden_remaining")):
        failures.append("public restored projection exposed hidden reserve")
    return FullDayAuditCase(
        "full_day_multivenue_checkpoint_privacy_and_conservation",
        (
            f"venue_count={len(restored.coordinator.venues)} reserve_quantity={reserve} "
            f"state_sha256={restored.state_sha256()} cursors_preserved=true "
            "public_reserve_absent=true conservation=true"
        ),
        tuple(failures),
    )


def _wo31e5_run_worker(raw: bytes):
    import subprocess
    import sys
    from pathlib import Path
    from tempfile import TemporaryDirectory

    repository = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    prior_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(repository)
        if not prior_path
        else str(repository) + os.pathsep + prior_path
    )
    environment.pop("PYTHONPYCACHEPREFIX", None)
    script = (
        "from kirby2.full_day.restore import "
        "multivenue_hidden_restore_worker_main as main; "
        "raise SystemExit(main())"
    )
    with TemporaryDirectory(prefix="kirby2-wo31e5-worker-") as temporary:
        directory = Path(temporary)
        before = tuple(directory.rglob("*"))
        completed = subprocess.run(
            [sys.executable, "-c", script],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=directory,
            env=environment,
            check=False,
            timeout=30,
        )
        after = tuple(directory.rglob("*"))
    return completed.returncode, completed.stdout, completed.stderr, before != after


def _wo31e5_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes, parse_canonical_json_object
    from kirby2.full_day.restore import (
        MultiVenueHiddenRestoreRequestV1,
        execute_uninterrupted_multivenue_hidden_suffix,
    )

    failures: list[str] = []
    digests: list[str] = []
    for boundary in ("pending_route", "pending_feed", "reserve"):
        owner = _wo31e5_owner(boundary)
        commands, completed_time_us = _wo31e5_suffix(boundary)
        request = MultiVenueHiddenRestoreRequestV1.capture(
            owner,
            suffix_commands=commands,
            completed_time_us=completed_time_us,
        )
        returncode, stdout, stderr, wrote_files = _wo31e5_run_worker(
            request.canonical_bytes()
        )
        expected = execute_uninterrupted_multivenue_hidden_suffix(owner, request)
        if returncode != 0:
            failures.append(
                f"{boundary} fresh worker returned {returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
            continue
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"{boundary} fresh worker emitted invalid JSON: {error}")
            continue
        if stderr or wrote_files:
            failures.append(f"{boundary} fresh worker produced side effects")
        if canonical_json_bytes(actual) != stdout or actual != expected:
            failures.append(f"{boundary} fresh suffix differs from uninterrupted suffix")
        public = actual["final"]["public_projection"]
        if any(
            token in str(public).lower()
            for token in ("reserve_remaining", "hidden_remaining", "priority_sequence")
        ):
            failures.append(f"{boundary} fresh result leaked hidden venue truth")
        digests.append(str(actual["invariant_sha256"]))
    return FullDayAuditCase(
        "full_day_multivenue_fresh_process_restore",
        (
            f"fresh_process_boundaries={len(digests)} "
            f"invariant_sha256={','.join(digests)} "
            "pending_route=true pending_feed=true reserve_state=true "
            "prefix_replay=false"
        ),
        tuple(failures),
    )


def _wo31e5_hostile_case() -> FullDayAuditCase:
    from kirby2.full_day.components_multivenue import MultiVenueHiddenOwnerV1

    owner = _wo31e5_owner("pending_route")
    state = owner.checkpoint_state()
    before = owner.canonical_state_bytes()
    probes: list[tuple[str, dict[str, object]]] = []

    unknown_component = copy.deepcopy(state)
    unknown_component["schema_version"] = 999
    probes.append(("unknown component schema", unknown_component))

    unknown_coordinator = copy.deepcopy(state)
    unknown_coordinator["coordinator"]["schema_version"] = 999
    probes.append(("unknown coordinator schema", unknown_coordinator))

    unknown_venue = copy.deepcopy(state)
    unknown_venue["coordinator"]["venues"]["A"]["schema_version"] = 999
    probes.append(("unknown venue schema", unknown_venue))

    orphan_route = copy.deepcopy(state)
    orphan_route["coordinator"]["pending_route_legs"][0]["route_id"] = "R999999"
    probes.append(("orphan route", orphan_route))

    early_delivery = copy.deepcopy(state)
    current_time = early_delivery["coordinator"]["clock"]["current_time_us"]
    early_delivery["coordinator"]["pending_route_legs"][0]["due_time_us"] = current_time - 1
    probes.append(("early route delivery", early_delivery))

    reserve_leak = copy.deepcopy(state)
    pending = reserve_leak["coordinator"]["venues"]["A"]["engine"]["pending_observable"]
    pending[0]["data"]["reserve_remaining"] = 1
    probes.append(("reserve leakage", reserve_leak))

    nonconserving = copy.deepcopy(state)
    orders = nonconserving["coordinator"]["venues"]["A"]["engine"]["orders"]
    iceberg = next(row for row in orders if row["request"]["kind"] == "ICEBERG")
    iceberg["reserve_remaining"] += 1
    probes.append(("hidden quantity conservation", nonconserving))

    unknown_venue_id = copy.deepcopy(state)
    venue_state = unknown_venue_id["coordinator"]["venues"].pop("A")
    unknown_venue_id["coordinator"]["venues"]["UNKNOWN"] = venue_state
    probes.append(("unknown venue identity", unknown_venue_id))

    historical_mix = copy.deepcopy(state)
    historical_mix["historical_cursor"] = {"row": 1}
    probes.append(("historical state mixing", historical_mix))

    failures: list[str] = []
    refused = 0
    for label, hostile in probes:
        refusal = _expect_refusal(
            lambda hostile=hostile: MultiVenueHiddenOwnerV1.from_checkpoint_state(hostile),
            label,
        )
        if refusal is None:
            refused += 1
        else:
            failures.append(refusal)
    if owner.canonical_state_bytes() != before:
        failures.append("hostile restore probes mutated the authoritative owner")
    return FullDayAuditCase(
        "full_day_multivenue_hostile_refusals",
        (
            f"refusals={refused} unknown_schema=true unknown_venue=true "
            "orphan_route=true early_delivery=true reserve_leak=true "
            "conservation=true historical_mixing=true failure_atomicity=true"
        ),
        tuple(failures),
    )


def audit_wo31e5_multivenue_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise standalone fragmented-market restoration and refusal boundaries."""

    return (
        _wo31e5_composition_case(),
        _wo31e5_checkpoint_case(),
        _wo31e5_fresh_restore_case(),
        _wo31e5_hostile_case(),
    )


def _wo31e6_owner(boundary: str, *, algorithm_name="immediate"):
    from kirby2.algorithms import (
        ExecutionObjective,
        RiskLimits,
        create_algorithm,
        default_algorithm_manifest,
        get_benchmark_scenario,
    )
    from kirby2.full_day.components_algorithms import ExecutionAlgorithmOwnerV1
    from kirby2.multivenue import MarketCoordinator

    scenario = get_benchmark_scenario(
        "balanced_execution",
        duration_us=1_000_000,
        decision_interval_us=250_000,
    )
    coordinator = MarketCoordinator(
        scenario.venue_configs,
        seed=31_006,
        depth_subscriptions=frozenset(
            config.venue_id for config in scenario.venue_configs
        ),
    )
    for venue_id, request in scenario.initial_orders():
        coordinator.add_resting_order(venue_id, request)
    coordinator.advance_to(scenario.start_time_us)
    feed = coordinator.consolidated_feed()
    if feed.best_bid_ticks is None or feed.best_ask_ticks is None:
        raise RuntimeError("WO31-E6 fixture lacks a two-sided observable market")
    objective = ExecutionObjective(
        Side.BUY,
        200,
        scenario.start_time_us,
        scenario.deadline_us,
        feed.best_bid_ticks + feed.best_ask_ticks,
    )
    owner = ExecutionAlgorithmOwnerV1.create(
        coordinator,
        create_algorithm(default_algorithm_manifest(algorithm_name)),
        objective,
        RiskLimits(200, 200, 200, 10),
        scenario.volume_profile_bps,
        scenario.decision_interval_us,
    )
    if boundary == "pending_deadline":
        pass
    elif boundary == "pending_action":
        owner.capture_decision()
    elif boundary == "outstanding_child":
        owner.capture_decision()
        decision = owner.apply_pending_action()
        if decision.resulting_route_id is None or owner.tracker.pending_route_quantity <= 0:
            raise RuntimeError("WO31-E6 fixture lacks an outstanding child route")
    else:
        raise ValueError(f"unknown WO31-E6 boundary: {boundary}")
    return owner


def _wo31e6_suffix(boundary: str):
    from kirby2.full_day.components_algorithms import (
        ExecutionAlgorithmSuffixCommandV1,
    )

    start = 1_000
    next_deadline = 251_000
    if boundary == "pending_deadline":
        commands = (
            ExecutionAlgorithmSuffixCommandV1(1, start, "DECIDE", {}),
            ExecutionAlgorithmSuffixCommandV1(2, start, "APPLY", {}),
        )
        completed_time_us = start
    elif boundary == "pending_action":
        commands = (ExecutionAlgorithmSuffixCommandV1(1, start, "APPLY", {}),)
        completed_time_us = start
    elif boundary == "outstanding_child":
        commands = (
            ExecutionAlgorithmSuffixCommandV1(1, next_deadline, "DECIDE", {}),
            ExecutionAlgorithmSuffixCommandV1(2, next_deadline, "APPLY", {}),
        )
        completed_time_us = next_deadline
    else:
        raise ValueError(f"unknown WO31-E6 boundary: {boundary}")
    return commands, completed_time_us


def _wo31e6_composition_case() -> FullDayAuditCase:
    from kirby2.full_day.composition import (
        EXECUTION_ALGORITHM_COMPONENT,
        EXECUTION_ALGORITHM_PROFILE_ID,
        MULTIVENUE_HIDDEN_PROFILE_ID,
        CompositionProfileV1,
        restorable_execution_algorithm_composition_matrix,
        restorable_multivenue_hidden_composition_matrix,
    )

    failures: list[str] = []
    previous = restorable_multivenue_hidden_composition_matrix()
    matrix = restorable_execution_algorithm_composition_matrix()
    profile = matrix.profile(EXECUTION_ALGORITHM_PROFILE_ID, 1)
    component = next(
        row for row in profile.components if row.component_id == EXECUTION_ALGORITHM_COMPONENT
    )
    if matrix.previous_matrix_sha256 != previous.sha256:
        failures.append("E6 composition matrix does not bind the exact E5 matrix")
    if matrix.profiles[:-1] != previous.profiles:
        failures.append("E6 composition matrix rewrote a prior profile")
    if profile.implementation_status != "CONTRACT_ONLY":
        failures.append("standalone algorithm profile overclaims executable status")
    if component.implementation_status != "RESTORABLE_COMPONENT_ONLY":
        failures.append("algorithm owner lacks exact component-only restore status")
    if component.checkpoint_state_ids != ("EXECUTION_ALGORITHM_V1",):
        failures.append("algorithm checkpoint ownership inventory is not exact")
    if not {"FULL_DAY_RUNTIME_V1", "HISTORICAL_REPLAY"} <= set(
        profile.refused_component_ids
    ):
        failures.append("algorithm profile does not refuse full-day and historical mixing")

    e5 = previous.profile(MULTIVENUE_HIDDEN_PROFILE_ID, 1)
    e5_component = e5.components[0]
    double_owner_refused = False
    try:
        CompositionProfileV1(
            schema_version=1,
            profile_id="HOSTILE_ALGORITHM_MULTIVENUE_COOWNER_V1",
            profile_version=1,
            implementation_status="CONTRACT_ONLY",
            runtime_owner_component_id=EXECUTION_ALGORITHM_COMPONENT,
            components=tuple(sorted((component, e5_component), key=lambda row: row.component_id)),
            refused_component_ids=("HISTORICAL_REPLAY",),
            exactly_one_component_groups=(),
        )
    except (TypeError, ValueError):
        double_owner_refused = True
    if not double_owner_refused:
        failures.append("composition accepted duplicate coordinator/feed/clock owners")

    return FullDayAuditCase(
        "full_day_algorithm_component_composition",
        (
            f"matrix_version={matrix.matrix_version} profile={profile.profile_id} "
            f"profile_status={profile.implementation_status} "
            f"component_status={component.implementation_status} "
            "prior_profiles_immutable=true double_owner_refused=true "
            "full_day_refused=true historical_refused=true"
        ),
        tuple(failures),
    )


def _wo31e6_checkpoint_case() -> FullDayAuditCase:
    from kirby2.algorithms import AlgorithmName
    from kirby2.full_day.components_algorithms import ExecutionAlgorithmOwnerV1

    failures: list[str] = []
    owner = _wo31e6_owner("pending_action")
    state = owner.checkpoint_state()
    restored = ExecutionAlgorithmOwnerV1.from_canonical_state_bytes(
        owner.canonical_state_bytes()
    )
    if restored.checkpoint_state() != state:
        failures.append("algorithm component checkpoint did not round trip exactly")
    pending = state["pending_action"]
    assert isinstance(pending, dict)
    if pending["information_cutoff_time_us"] != state["schedule"][
        "next_decision_time_us"
    ]:
        failures.append("pending action is not bound to its decision deadline")
    policy = state["policy"]
    assert isinstance(policy, dict)
    if policy["objective"] != state["objective"]:
        failures.append("policy objective differs from component objective")
    tracker = state["tracker"]
    assert isinstance(tracker, dict)
    if tracker["sequence"] != 1 or state["tracker_metrics"][
        "client_observation_sequence"
    ] != 1:
        failures.append("client tracker observation progress was not preserved")
    frozen_digest = pending["observation_sha256"]
    frozen_action = pending["action"]
    decision = restored.apply_pending_action()
    if (
        decision.observation_sha256 != frozen_digest
        or decision.action.as_dict() != frozen_action
    ):
        failures.append("restored pending action was recomputed or rebound")
    metrics = restored.checkpoint_state()["tracker_metrics"]
    known_quantity = (
        metrics["observed_fill_quantity"]
        + metrics["working_quantity"]
        + metrics["pending_route_quantity"]
    )
    if known_quantity > restored.objective.target_quantity:
        failures.append("restored client-known quantity does not conserve")
    restored_policy_count = 0
    for algorithm_name in AlgorithmName:
        policy_owner = _wo31e6_owner(
            "pending_action",
            algorithm_name=algorithm_name,
        )
        policy_state = policy_owner.checkpoint_state()
        policy_restored = ExecutionAlgorithmOwnerV1.from_checkpoint_state(policy_state)
        if policy_restored.algorithm.checkpoint_state() != policy_state["policy"]:
            failures.append(
                f"{algorithm_name.value} policy progress did not restore exactly"
            )
        else:
            restored_policy_count += 1
    return FullDayAuditCase(
        "full_day_algorithm_checkpoint_cutoff_and_conservation",
        (
            f"state_sha256={owner.state_sha256()} policy={owner.algorithm.manifest.algorithm.value} "
            f"observation_sequence={tracker['sequence']} policies={restored_policy_count} "
            "pending_action=true "
            f"known_quantity={known_quantity} cutoff_bound=true recomputation=false "
            "objective_risk_schedule_tracker_preserved=true"
        ),
        tuple(failures),
    )


def _wo31e6_run_worker(raw: bytes):
    import subprocess
    import sys
    from pathlib import Path
    from tempfile import TemporaryDirectory

    repository = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    prior_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(repository)
        if not prior_path
        else str(repository) + os.pathsep + prior_path
    )
    environment.pop("PYTHONPYCACHEPREFIX", None)
    script = (
        "from kirby2.full_day.restore import "
        "execution_algorithm_restore_worker_main as main; "
        "raise SystemExit(main())"
    )
    with TemporaryDirectory(prefix="kirby2-wo31e6-worker-") as temporary:
        directory = Path(temporary)
        before = tuple(directory.rglob("*"))
        completed = subprocess.run(
            [sys.executable, "-c", script],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=directory,
            env=environment,
            check=False,
            timeout=30,
        )
        after = tuple(directory.rglob("*"))
    return completed.returncode, completed.stdout, completed.stderr, before != after


def _wo31e6_fresh_restore_case() -> FullDayAuditCase:
    from kirby2.full_day.models import canonical_json_bytes, parse_canonical_json_object
    from kirby2.full_day.restore import (
        ExecutionAlgorithmRestoreRequestV1,
        execute_uninterrupted_execution_algorithm_suffix,
    )

    failures: list[str] = []
    digests: list[str] = []
    for boundary in ("pending_deadline", "pending_action", "outstanding_child"):
        owner = _wo31e6_owner(boundary)
        commands, completed_time_us = _wo31e6_suffix(boundary)
        request = ExecutionAlgorithmRestoreRequestV1.capture(
            owner,
            suffix_commands=commands,
            completed_time_us=completed_time_us,
        )
        returncode, stdout, stderr, wrote_files = _wo31e6_run_worker(
            request.canonical_bytes()
        )
        expected = execute_uninterrupted_execution_algorithm_suffix(owner, request)
        if returncode != 0:
            failures.append(
                f"{boundary} fresh worker returned {returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
            continue
        try:
            actual = parse_canonical_json_object(stdout)
        except (TypeError, ValueError) as error:
            failures.append(f"{boundary} fresh worker emitted invalid JSON: {error}")
            continue
        if stderr or wrote_files:
            failures.append(f"{boundary} fresh worker produced side effects")
        if canonical_json_bytes(actual) != stdout or actual != expected:
            failures.append(f"{boundary} fresh suffix differs from uninterrupted suffix")
        digests.append(str(actual["invariant_sha256"]))
    return FullDayAuditCase(
        "full_day_algorithm_fresh_process_restore",
        (
            f"fresh_process_boundaries={len(digests)} "
            f"invariant_sha256={','.join(digests)} "
            "pending_deadline=true pending_action=true outstanding_child=true "
            "prefix_replay=false later_observation_recompute=false"
        ),
        tuple(failures),
    )


def _wo31e6_hostile_case() -> FullDayAuditCase:
    from kirby2.full_day.components_algorithms import ExecutionAlgorithmOwnerV1

    pending = _wo31e6_owner("pending_action")
    state = pending.checkpoint_state()
    before = pending.canonical_state_bytes()
    probes: list[tuple[str, dict[str, object]]] = []

    unknown_component = copy.deepcopy(state)
    unknown_component["schema_version"] = 999
    probes.append(("unknown component schema", unknown_component))

    unknown_policy_schema = copy.deepcopy(state)
    unknown_policy_schema["policy"]["schema_version"] = 999
    probes.append(("unknown policy schema", unknown_policy_schema))

    unknown_policy_version = copy.deepcopy(state)
    unknown_policy_version["policy"]["policy_version"] = 999
    probes.append(("unknown policy version", unknown_policy_version))

    unknown_policy_id = copy.deepcopy(state)
    unknown_policy_id["policy"]["policy_id"] = "UNKNOWN_POLICY"
    probes.append(("unknown policy ID", unknown_policy_id))

    objective_mismatch = copy.deepcopy(state)
    objective_mismatch["objective"]["target_quantity"] += 1
    probes.append(("policy objective mismatch", objective_mismatch))

    cutoff_moved = copy.deepcopy(state)
    cutoff_moved["pending_action"]["information_cutoff_time_us"] += 1
    probes.append(("pending information cutoff moved", cutoff_moved))

    early_deadline = _wo31e6_owner("pending_deadline").checkpoint_state()
    early_deadline["schedule"]["next_decision_time_us"] -= 1
    probes.append(("early decision deadline", early_deadline))

    late_deadline = _wo31e6_owner("pending_deadline").checkpoint_state()
    late_deadline["schedule"]["next_decision_time_us"] += 1
    probes.append(("late decision deadline", late_deadline))

    outstanding = _wo31e6_owner("outstanding_child").checkpoint_state()
    orphan_child = copy.deepcopy(outstanding)
    orphan_child["allocators"]["route_ids"].append("R999999")
    orphan_child["allocators"]["child_request_sequence"] += 1
    probes.append(("orphan child route", orphan_child))

    orphan_order = copy.deepcopy(outstanding)
    route_id = orphan_order["allocators"]["route_ids"][0]
    fake_order_id = f"ALG-E6-000001-{route_id}-L999"
    orphan_order["tracker"]["route_order_allocations"][route_id] = {
        fake_order_id: 1
    }
    orphan_order["tracker"]["order_sides"][fake_order_id] = Side.BUY.value
    orphan_order["tracker"]["order_decision_midpoints_x2"][fake_order_id] = 20_000
    orphan_order["tracker_metrics"]["pending_route_quantity"] -= 1
    probes.append(("orphan child order", orphan_order))

    nonconserving = copy.deepcopy(outstanding)
    route_id = nonconserving["allocators"]["route_ids"][0]
    nonconserving["tracker"]["route_quantities"][route_id] += 1
    nonconserving["tracker_metrics"]["pending_route_quantity"] += 1
    probes.append(("client quantity nonconservation", nonconserving))

    historical_mix = copy.deepcopy(state)
    historical_mix["historical_cursor"] = {"row": 1}
    probes.append(("historical state mixing", historical_mix))

    failures: list[str] = []
    refused = 0
    for label, hostile in probes:
        refusal = _expect_refusal(
            lambda hostile=hostile: ExecutionAlgorithmOwnerV1.from_checkpoint_state(
                hostile
            ),
            label,
        )
        if refusal is None:
            refused += 1
        else:
            failures.append(refusal)
    if pending.canonical_state_bytes() != before:
        failures.append("hostile algorithm restores mutated the authoritative owner")
    return FullDayAuditCase(
        "full_day_algorithm_hostile_refusals",
        (
            f"refusals={refused} unknown_schema=true unknown_policy_version=true "
            "objective_mismatch=true cutoff_moved=true early_deadline=true "
            "late_deadline=true orphan_child_route=true orphan_child_order=true "
            "conservation=true "
            "historical_mixing=true failure_atomicity=true"
        ),
        tuple(failures),
    )


def audit_wo31e6_execution_algorithm_restore() -> tuple[FullDayAuditCase, ...]:
    """Exercise standalone execution-algorithm restoration and refusals."""

    return (
        _wo31e6_composition_case(),
        _wo31e6_checkpoint_case(),
        _wo31e6_fresh_restore_case(),
        _wo31e6_hostile_case(),
    )


__all__ = [
    "FullDayAuditCase",
    "audit_dev0002_anchor_transition_ordering",
    "audit_dev0003_state_checkpoint_inventory",
    "audit_dev0004_atomic_boundary_replay",
    "audit_wo31a_contracts",
    "audit_wo31b_transitions",
    "audit_wo31c_checkpoints",
    "audit_wo31d_core_restore",
    "audit_wo31e1_runtime_restore",
    "audit_wo31e2_hawkes_flow_slice",
    "audit_wo31e2_queue_reactive_flow_slice",
    "audit_wo31e2_simple_flow_slice",
    "audit_wo31e2_flow_restore",
    "audit_wo31e3_delivery_restore",
    "audit_wo31e4_research_restore",
    "audit_wo31e5_multivenue_restore",
    "audit_wo31e6_execution_algorithm_restore",
]
