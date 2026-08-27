"""Executable audit for distribution units, consumers, and deterministic traces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kirby2.scenarios import get_scenario_definition, run_market_scenario
from kirby2.session import EventType
from kirby2.simulation.distribution_framework import (
    INTER_EVENT_TIMING_SCALE,
    DistributionProfile,
    DistributionPurpose,
    FixedDistribution,
)
from kirby2.simulation.distribution_profiles import balanced_distribution_profile
from kirby2.simulation.flow import FlowEventFamily, SimulationResult


_SEEDS = (11, 42, 97)
_SECONDS = 4
_BASE_VALUES = {
    DistributionPurpose.ORDER_SIZE: 100,
    DistributionPurpose.TRADE_SIZE: 100,
    DistributionPurpose.CANCEL_SIZE: 100,
    DistributionPurpose.QUEUE_DEPTH: 100,
    DistributionPurpose.LIMIT_PLACEMENT_DEPTH: 2,
    DistributionPurpose.INTER_EVENT_TIMING_MODIFIER: INTER_EVENT_TIMING_SCALE,
    DistributionPurpose.SPREAD_STATE_DURATION: 10_000_000,
}
_EXTREMES = {
    DistributionPurpose.ORDER_SIZE: (10, 1_000),
    DistributionPurpose.TRADE_SIZE: (10, 1_000),
    DistributionPurpose.CANCEL_SIZE: (1, 2_000),
    DistributionPurpose.QUEUE_DEPTH: (10, 1_000),
    DistributionPurpose.LIMIT_PLACEMENT_DEPTH: (0, 6),
    DistributionPurpose.INTER_EVENT_TIMING_MODIFIER: (500, 2_000),
    DistributionPurpose.SPREAD_STATE_DURATION: (100_000, 10_000_000),
}


@dataclass(frozen=True, slots=True)
class DistributionTruthAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_distribution_truth() -> tuple[DistributionTruthAuditCase, ...]:
    cases = [
        _extreme_case(purpose, low, high)
        for purpose, (low, high) in _EXTREMES.items()
    ]
    cases.extend(
        (
            _draw_trace_and_replay_case(),
            _cancellation_integrity_case(),
            _profile_validation_case(),
        )
    )
    return tuple(cases)


def _extreme_case(
    purpose: DistributionPurpose,
    low: int,
    high: int,
) -> DistributionTruthAuditCase:
    low_runs = tuple(_run_fixed(purpose, low, seed) for seed in _SEEDS)
    high_runs = tuple(_run_fixed(purpose, high, seed) for seed in _SEEDS)
    low_behavior = _behavior(purpose, low_runs)
    high_behavior = _behavior(purpose, high_runs)
    failures: list[str] = []
    if not _has_observations(purpose, low_behavior):
        failures.append("low fixed variant did not reach its runtime consumer")
    if not _has_observations(purpose, high_behavior):
        failures.append("high fixed variant did not reach its runtime consumer")
    if low_behavior == high_behavior:
        failures.append("extreme fixed variants produced identical declared behavior")
    low_trace_values = _purpose_trace_values(low_runs, purpose)
    high_trace_values = _purpose_trace_values(high_runs, purpose)
    if not low_trace_values or set(low_trace_values) != {low}:
        failures.append("low fixed value was absent or altered in the draw trace")
    if not high_trace_values or set(high_trace_values) != {high}:
        failures.append("high fixed value was absent or altered in the draw trace")
    for run in (*low_runs, *high_runs):
        run.book.assert_invariants()
    failures.extend(_directional_failures(purpose, low_behavior, high_behavior))
    return DistributionTruthAuditCase(
        f"extreme_{purpose.value}",
        {
            "high": high,
            "high_behavior": _compact_behavior(purpose, high_behavior),
            "high_draw_count": len(high_trace_values),
            "low": low,
            "low_behavior": _compact_behavior(purpose, low_behavior),
            "low_draw_count": len(low_trace_values),
            "seeds": list(_SEEDS),
            "unit": purpose.unit,
        },
        tuple(failures),
    )


def _draw_trace_and_replay_case() -> DistributionTruthAuditCase:
    definition = get_scenario_definition("balanced")
    profile = balanced_distribution_profile()
    first = run_market_scenario(
        definition,
        seed=42,
        seconds=3,
        distribution_profile=profile,
    ).simulation
    second = run_market_scenario(
        definition,
        seed=42,
        seconds=3,
        distribution_profile=profile,
    ).simulation
    different = run_market_scenario(
        definition,
        seed=43,
        seconds=3,
        distribution_profile=profile,
    ).simulation
    first_trace = tuple(draw.as_dict() for draw in first.distribution_draws)
    second_trace = tuple(draw.as_dict() for draw in second.distribution_draws)
    different_trace = tuple(draw.as_dict() for draw in different.distribution_draws)
    purposes = {draw.purpose for draw in first.distribution_draws}
    failures: list[str] = []
    if first_trace != second_trace:
        failures.append("same seed and profile produced different draw traces")
    if first.replay_json_lines() != second.replay_json_lines():
        failures.append("same seed and profile produced different replay bytes")
    if first_trace == different_trace or first.replay_sha256() == different.replay_sha256():
        failures.append("different seeds did not produce different valid behavior")
    if purposes != set(DistributionPurpose):
        failures.append("runtime trace did not exercise every distribution purpose")
    if [draw.sequence for draw in first.distribution_draws] != list(
        range(1, len(first.distribution_draws) + 1)
    ):
        failures.append("draw sequence is not contiguous and monotonic")
    if any(
        earlier.simulation_time_us > later.simulation_time_us
        for earlier, later in zip(
            first.distribution_draws,
            first.distribution_draws[1:],
        )
    ):
        failures.append("draw simulation time moved backward")
    if '"record_type":"distribution_draw"' not in first.replay_json_lines():
        failures.append("replay omitted inspectable distribution draw records")
    for run in (first, second, different):
        run.book.assert_invariants()
    return DistributionTruthAuditCase(
        "draw_trace_seed_and_replay",
        {
            "different_seed_draw_count": len(different_trace),
            "different_seed_replay_sha256": different.replay_sha256(),
            "draw_count": len(first_trace),
            "purposes": sorted(purpose.value for purpose in purposes),
            "same_seed_replay_sha256": first.replay_sha256(),
            "timing_scale": INTER_EVENT_TIMING_SCALE,
            "trace_head": list(first_trace[:3]),
        },
        tuple(failures),
    )


def _cancellation_integrity_case() -> DistributionTruthAuditCase:
    runs = tuple(
        _run_fixed(DistributionPurpose.CANCEL_SIZE, value, seed)
        for value in _EXTREMES[DistributionPurpose.CANCEL_SIZE]
        for seed in _SEEDS
    )
    failures: list[str] = []
    cancelled_ids: set[tuple[int, int, str]] = set()
    cancel_commands = 0
    affected_orders = 0
    for run_index, run in enumerate(runs):
        allowed_types = {"limit", "market", "cancel"}
        for event in run.flow_events:
            payload = event.command if event.command is not None else event.diagnostic
            if payload is None:
                continue
            if payload.get("order_type") not in allowed_types:
                failures.append("flow emitted an unsupported direct command type")
            if event.family not in {
                FlowEventFamily.CANCEL_BID,
                FlowEventFamily.CANCEL_ASK,
            }:
                continue
            cancel_commands += 1
            command = payload
            affected = command["affected_orders"]
            affected_orders += len(affected)
            requested = int(command["requested_cancel_quantity"])
            actual = int(command["actual_cancelled_quantity"])
            quantities = sum(int(item["cancelled_quantity"]) for item in affected)
            ids = [str(item["target_order_id"]) for item in affected]
            if actual != quantities or actual < 0:
                failures.append("cancellation actual quantity did not reconcile")
            if len(ids) != len(set(ids)):
                failures.append("one cancellation budget targeted an order twice")
            if int(command["overshoot_quantity"]) != max(0, actual - requested):
                failures.append("cancellation overshoot did not reconcile")
            if int(command["unfulfilled_quantity"]) != max(0, requested - actual):
                failures.append("cancellation unfulfilled quantity did not reconcile")
            for order_id in ids:
                key = (run_index, run.seed, order_id)
                if key in cancelled_ids:
                    failures.append("an already-cancelled order was targeted again")
                cancelled_ids.add(key)
            exchange_slice = (
                ()
                if event.exchange_event_start is None or event.exchange_event_end is None
                else run.book.journal.events[
                    event.exchange_event_start - 1 : event.exchange_event_end
                ]
            )
            emitted_ids = [
                str(exchange_event.data["order_id"])
                for exchange_event in exchange_slice
                if exchange_event.event_type is EventType.ORDER_CANCELLED
            ]
            if emitted_ids != ids:
                failures.append("affected IDs did not match exchange cancellation events")
        run.book.assert_invariants()
    if cancel_commands == 0 or affected_orders == 0:
        failures.append("seed matrix emitted no applied cancellation budgets")
    return DistributionTruthAuditCase(
        "whole_order_cancellation_integrity",
        {
            "affected_order_count": affected_orders,
            "cancel_command_count": cancel_commands,
            "crossed_resting_books": 0,
            "direct_price_commands": 0,
            "seed_count": len(runs),
        },
        tuple(failures),
    )


def _profile_validation_case() -> DistributionTruthAuditCase:
    rejected: dict[str, bool] = {}
    complete = {
        purpose: FixedDistribution(value)
        for purpose, value in _BASE_VALUES.items()
    }
    invalid_profiles: dict[str, dict[Any, FixedDistribution]] = {
        "missing_purpose": {
            purpose: distribution
            for purpose, distribution in complete.items()
            if purpose is not DistributionPurpose.CANCEL_SIZE
        },
        "unsupported_purpose": {**complete, "unsupported": FixedDistribution(1)},
        "zero_cancel_budget": {
            **complete,
            DistributionPurpose.CANCEL_SIZE: FixedDistribution(0),
        },
        "zero_timing_modifier": {
            **complete,
            DistributionPurpose.INTER_EVENT_TIMING_MODIFIER: FixedDistribution(0),
        },
        "zero_spread_duration": {
            **complete,
            DistributionPurpose.SPREAD_STATE_DURATION: FixedDistribution(0),
        },
    }
    for name, distributions in invalid_profiles.items():
        try:
            DistributionProfile(f"invalid_{name}", distributions)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            rejected[name] = True
        else:
            rejected[name] = False
    units = {purpose.value: purpose.unit for purpose in DistributionPurpose}
    failures = [
        f"invalid profile accepted: {name}"
        for name, was_rejected in rejected.items()
        if not was_rejected
    ]
    if set(units) != {purpose.value for purpose in DistributionPurpose}:
        failures.append("not every purpose has a documented unit")
    return DistributionTruthAuditCase(
        "units_and_fail_closed_profile_validation",
        {
            "rejected": rejected,
            "timing_scale": INTER_EVENT_TIMING_SCALE,
            "units": units,
        },
        tuple(failures),
    )


def _fixed_profile(
    changed_purpose: DistributionPurpose,
    changed_value: int,
) -> DistributionProfile:
    values = {**_BASE_VALUES, changed_purpose: changed_value}
    return DistributionProfile(
        profile_id=f"audit_{changed_purpose.value}_{changed_value}",
        distributions={
            purpose: FixedDistribution(value)
            for purpose, value in values.items()
        },
    )


def _run_fixed(
    purpose: DistributionPurpose,
    value: int,
    seed: int,
) -> SimulationResult:
    return run_market_scenario(
        get_scenario_definition("balanced"),
        seed=seed,
        seconds=_SECONDS,
        distribution_profile=_fixed_profile(purpose, value),
    ).simulation


def _behavior(
    purpose: DistributionPurpose,
    runs: tuple[SimulationResult, ...],
) -> tuple[Any, ...]:
    if purpose is DistributionPurpose.QUEUE_DEPTH:
        return tuple(
            int(event.data["remaining_quantity"])
            for run in runs
            for event in run.book.journal.events[: run.initial_exchange_event_count]
            if event.event_type is EventType.ORDER_ADDED
        )
    if purpose is DistributionPurpose.INTER_EVENT_TIMING_MODIFIER:
        return tuple(
            (run.seed, event.simulation_time_us)
            for run in runs
            for event in run.flow_events
        )
    if purpose is DistributionPurpose.SPREAD_STATE_DURATION:
        duration_draws = sum(
            draw.purpose is purpose
            for run in runs
            for draw in run.distribution_draws
        )
        placements = tuple(
            int(event.command["depth"])
            for run in runs
            for event in run.flow_events
            if event.command is not None
            and event.family in {
                FlowEventFamily.LIMIT_BUY,
                FlowEventFamily.LIMIT_SELL,
            }
        )
        return duration_draws, placements
    if purpose is DistributionPurpose.CANCEL_SIZE:
        return tuple(
            (
                int(_cancel_payload(event)["requested_cancel_quantity"]),
                int(_cancel_payload(event)["actual_cancelled_quantity"]),
                int(_cancel_payload(event)["overshoot_quantity"]),
                int(_cancel_payload(event)["unfulfilled_quantity"]),
                tuple(
                    str(value)
                    for value in _cancel_payload(event)["affected_order_ids"]
                ),
            )
            for run in runs
            for event in run.flow_events
            if _cancel_payload(event) is not None
            and event.family in {
                FlowEventFamily.CANCEL_BID,
                FlowEventFamily.CANCEL_ASK,
            }
        )
    if purpose is DistributionPurpose.LIMIT_PLACEMENT_DEPTH:
        return tuple(
            int(event.command["depth"])
            for run in runs
            for event in run.flow_events
            if event.command is not None
            and event.family in {
                FlowEventFamily.LIMIT_BUY,
                FlowEventFamily.LIMIT_SELL,
            }
        )
    selected_families = (
        {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}
        if purpose is DistributionPurpose.ORDER_SIZE
        else {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
    )
    return tuple(
        int(event.command["quantity"])
        for run in runs
        for event in run.flow_events
        if event.command is not None and event.family in selected_families
    )


def _cancel_payload(event: Any) -> dict[str, Any] | None:
    return event.command if event.command is not None else event.diagnostic


def _purpose_trace_values(
    runs: tuple[SimulationResult, ...],
    purpose: DistributionPurpose,
) -> tuple[int, ...]:
    return tuple(
        draw.sampled_value
        for run in runs
        for draw in run.distribution_draws
        if draw.purpose is purpose
    )


def _has_observations(purpose: DistributionPurpose, behavior: tuple[Any, ...]) -> bool:
    if purpose is DistributionPurpose.SPREAD_STATE_DURATION:
        return bool(behavior[0]) and bool(behavior[1])
    return bool(behavior)


def _directional_failures(
    purpose: DistributionPurpose,
    low: tuple[Any, ...],
    high: tuple[Any, ...],
) -> list[str]:
    if purpose is DistributionPurpose.SPREAD_STATE_DURATION:
        return (
            []
            if int(low[0]) > int(high[0]) and low[1] != high[1]
            else ["spread duration did not alter state cadence and quote placement"]
        )
    if purpose is DistributionPurpose.INTER_EVENT_TIMING_MODIFIER:
        return (
            []
            if len(low) > len(high)
            else ["shorter interarrival modifier did not produce more arrivals"]
        )
    if purpose is DistributionPurpose.CANCEL_SIZE:
        low_requested = {item[0] for item in low}
        high_requested = {item[0] for item in high}
        return (
            []
            if low_requested == {_EXTREMES[purpose][0]}
            and high_requested == {_EXTREMES[purpose][1]}
            else ["cancellation commands did not preserve requested budgets"]
        )
    numeric_low = tuple(int(value) for value in low)
    numeric_high = tuple(int(value) for value in high)
    return (
        []
        if numeric_low and numeric_high and max(numeric_low) < min(numeric_high)
        else ["fixed low/high values were not ordered in declared output"]
    )


def _compact_behavior(
    purpose: DistributionPurpose,
    behavior: tuple[Any, ...],
) -> dict[str, object]:
    if purpose is DistributionPurpose.SPREAD_STATE_DURATION:
        placements = tuple(int(value) for value in behavior[1])
        return {
            "placement_count": len(placements),
            "placement_depths": sorted(set(placements)),
            "state_duration_draw_count": int(behavior[0]),
        }
    if purpose is DistributionPurpose.CANCEL_SIZE:
        return {
            "affected_order_count": sum(len(item[4]) for item in behavior),
            "command_count": len(behavior),
            "requested_values": sorted({int(item[0]) for item in behavior}),
            "unfulfilled_total": sum(int(item[3]) for item in behavior),
        }
    if purpose is DistributionPurpose.INTER_EVENT_TIMING_MODIFIER:
        return {
            "event_count": len(behavior),
            "last_time_by_seed": {
                str(seed): max(time for item_seed, time in behavior if item_seed == seed)
                for seed in _SEEDS
                if any(item_seed == seed for item_seed, _ in behavior)
            },
        }
    numeric = tuple(int(value) for value in behavior)
    return {
        "count": len(numeric),
        "maximum": max(numeric) if numeric else None,
        "minimum": min(numeric) if numeric else None,
        "values": sorted(set(numeric))[:12],
    }
