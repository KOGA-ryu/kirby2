"""Operational WO33-B2 latency and cancel/fill replay detectors."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .models import CandidateDirectionV1, CandidateSideV1, unsigned_share_ppm
from .runtime import (
    DetectorHandlerV1,
    DetectorMeasurementV1,
    DetectorOpportunityV1,
    RuleEvaluationV1,
    evaluation,
    exact_measurements,
    measurement_bool,
    measurement_int,
    threshold_int,
)


def detect_latency_sensitive_opportunity(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "action_identical",
            "checkpoint_information_identical",
            "fast_fee_adjusted_average_cost_milliticks_per_share",
            "fast_filled_quantity",
            "fast_latency_us",
            "objective_shares",
            "slow_fee_adjusted_average_cost_milliticks_per_share",
            "slow_filled_quantity",
            "slow_latency_us",
        },
    )
    objective = measurement_int(values, "objective_shares")
    fast_filled = measurement_int(values, "fast_filled_quantity")
    slow_filled = measurement_int(values, "slow_filled_quantity")
    fast_cost = measurement_int(
        values,
        "fast_fee_adjusted_average_cost_milliticks_per_share",
    )
    slow_cost = measurement_int(
        values,
        "slow_fee_adjusted_average_cost_milliticks_per_share",
    )
    fast_latency = measurement_int(values, "fast_latency_us")
    slow_latency = measurement_int(values, "slow_latency_us")
    if (
        objective <= 0
        or not 0 <= fast_filled <= objective
        or not 0 <= slow_filled <= objective
        or min(fast_cost, slow_cost, fast_latency, slow_latency) < 0
    ):
        raise ValueError("latency-opportunity replay evidence is inconsistent")
    fill_difference_share = unsigned_share_ppm(
        abs(fast_filled - slow_filled),
        objective,
    )
    average_cost_difference = abs(fast_cost - slow_cost)
    fill_branch = fill_difference_share >= threshold_int(
        row,
        "fill_difference_share_ppm",
    )
    cost_branch = average_cost_difference >= threshold_int(
        row,
        "average_cost_difference",
    )
    return evaluation(
        (
            (
                measurement_bool(values, "checkpoint_information_identical"),
                "LATENCY_CHECKPOINT_INFORMATION_DIFFERS",
            ),
            (
                measurement_bool(values, "action_identical"),
                "LATENCY_REPLAY_ACTION_DIFFERS",
            ),
            (
                fast_latency == threshold_int(row, "fast_latency"),
                "FAST_LATENCY_INTERVENTION_DIFFERS",
            ),
            (
                slow_latency == threshold_int(row, "slow_latency"),
                "SLOW_LATENCY_INTERVENTION_DIFFERS",
            ),
            (
                fill_branch or cost_branch,
                "LATENCY_EFFECT_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is not CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side.value == opportunity.direction.value
                and opportunity.price == "NOT_APPLICABLE"
                and opportunity.witness_kind == "LATENCY_ACTION",
                "LATENCY_OPPORTUNITY_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1(
            "average_cost_difference_milliticks_per_share",
            average_cost_difference,
        ),
        DetectorMeasurementV1(
            "cost_difference_branch_satisfied",
            cost_branch,
        ),
        DetectorMeasurementV1(
            "fill_difference_branch_satisfied",
            fill_branch,
        ),
        DetectorMeasurementV1(
            "fill_difference_share_ppm",
            fill_difference_share,
        ),
    )


def detect_cancel_fill_race(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "baseline_cancel_arrival_us",
            "baseline_cancel_latency_us",
            "baseline_opposing_fill_arrival_us",
            "checkpoint_information_identical",
            "fast_cancel_latency_us",
            "fast_cancelled_quantity",
            "fast_effective_cancel_source_sequence",
            "fast_effective_cancel_us",
            "fast_filled_quantity",
            "fast_opposing_fill_arrival_source_sequence",
            "fast_opposing_fill_arrival_us",
            "original_quantity",
            "slow_cancel_latency_us",
            "slow_cancelled_quantity",
            "slow_effective_cancel_source_sequence",
            "slow_effective_cancel_us",
            "slow_filled_quantity",
            "slow_opposing_fill_arrival_source_sequence",
            "slow_opposing_fill_arrival_us",
        },
    )
    baseline_cancel_arrival = measurement_int(values, "baseline_cancel_arrival_us")
    baseline_fill_arrival = measurement_int(
        values,
        "baseline_opposing_fill_arrival_us",
    )
    baseline_latency = measurement_int(values, "baseline_cancel_latency_us")
    fast_latency = measurement_int(values, "fast_cancel_latency_us")
    slow_latency = measurement_int(values, "slow_cancel_latency_us")
    original_quantity = measurement_int(values, "original_quantity")
    if min(
        baseline_cancel_arrival,
        baseline_fill_arrival,
        baseline_latency,
        fast_latency,
        slow_latency,
    ) < 0 or original_quantity <= 0:
        raise ValueError("cancel/fill replay timing or quantity is invalid")
    intervention_delta = threshold_int(row, "latency_intervention_delta")
    fast_outcome = _terminal_cancel_fill_outcome(values, "fast", original_quantity)
    slow_outcome = _terminal_cancel_fill_outcome(values, "slow", original_quantity)
    arrival_difference = abs(baseline_cancel_arrival - baseline_fill_arrival)
    fast_opposing_arrival = _replay_order(values, "fast", "opposing_fill_arrival")
    slow_opposing_arrival = _replay_order(values, "slow", "opposing_fill_arrival")
    baseline_send_us = baseline_cancel_arrival - baseline_latency
    fast_send_us = (
        measurement_int(values, "fast_effective_cancel_us") - fast_latency
    )
    slow_send_us = (
        measurement_int(values, "slow_effective_cancel_us") - slow_latency
    )
    return evaluation(
        (
            (
                measurement_bool(values, "checkpoint_information_identical"),
                "CANCEL_FILL_CHECKPOINT_INFORMATION_DIFFERS",
            ),
            (
                fast_opposing_arrival == slow_opposing_arrival
                and fast_opposing_arrival[0] == baseline_fill_arrival,
                "CANCEL_FILL_OPPOSING_ARRIVAL_DIFFERS",
            ),
            (
                baseline_send_us >= 0
                and baseline_send_us == fast_send_us == slow_send_us,
                "CANCEL_FILL_COMMAND_CHECKPOINT_DIFFERS",
            ),
            (
                arrival_difference
                <= threshold_int(row, "baseline_arrival_difference"),
                "BASELINE_CANCEL_FILL_ARRIVALS_TOO_FAR_APART",
            ),
            (
                fast_latency == max(0, baseline_latency - intervention_delta),
                "FAST_CANCEL_LATENCY_INTERVENTION_DIFFERS",
            ),
            (
                slow_latency == baseline_latency + intervention_delta,
                "SLOW_CANCEL_LATENCY_INTERVENTION_DIFFERS",
            ),
            (
                fast_outcome != slow_outcome,
                "CANCEL_FILL_TERMINAL_WINNER_UNCHANGED",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE
                and type(opportunity.price) is int
                and opportunity.witness_kind == "CANCEL_FILL_TUPLE",
                "CANCEL_FILL_RACE_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1(
            "baseline_arrival_difference_us",
            arrival_difference,
        ),
        DetectorMeasurementV1("fast_terminal_outcome", fast_outcome),
        DetectorMeasurementV1("slow_terminal_outcome", slow_outcome),
    )


def _terminal_cancel_fill_outcome(
    values: Mapping[str, object],
    prefix: str,
    original_quantity: int,
) -> str:
    filled = measurement_int(values, f"{prefix}_filled_quantity")
    cancelled = measurement_int(values, f"{prefix}_cancelled_quantity")
    fill_order = _replay_order(values, prefix, "opposing_fill_arrival")
    cancel_order = _replay_order(values, prefix, "effective_cancel")
    if (
        filled < 0
        or cancelled < 0
        or filled + cancelled != original_quantity
        or min(*fill_order, *cancel_order) < 0
    ):
        raise ValueError("cancel/fill terminal quantities or ordering are inconsistent")
    if filled == original_quantity and cancelled == 0:
        if fill_order >= cancel_order:
            raise ValueError("full fill did not precede effective cancellation")
        return "FULL_FILL"
    if cancelled > 0:
        return "CANCEL"
    raise ValueError("cancel/fill replay lacks a canonical terminal outcome")


def _replay_order(
    values: Mapping[str, object],
    prefix: str,
    event_name: str,
) -> tuple[int, int]:
    return (
        measurement_int(values, f"{prefix}_{event_name}_us"),
        measurement_int(values, f"{prefix}_{event_name}_source_sequence"),
    )


LATENCY_DETECTOR_HANDLERS_V1: Mapping[str, DetectorHandlerV1] = MappingProxyType(
    {
        "CANCEL_FILL_RACE": detect_cancel_fill_race,
        "LATENCY_SENSITIVE_OPPORTUNITY": detect_latency_sensitive_opportunity,
    }
)


__all__ = [
    "LATENCY_DETECTOR_HANDLERS_V1",
    "detect_cancel_fill_race",
    "detect_latency_sensitive_opportunity",
]
