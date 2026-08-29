"""Operational WO33-B1 flow, breakout, and transition detectors."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .models import (
    CandidateDirectionV1,
    CandidateSideV1,
    ratio_ppm,
    round_div_even,
    unsigned_share_ppm,
)
from .runtime import (
    DetectorHandlerV1,
    DetectorMeasurementV1,
    DetectorOpportunityV1,
    RuleEvaluationV1,
    evaluation,
    exact_measurements,
    measurement_int,
    measurement_int_tuple,
    nearest_rank_p50,
    threshold_int,
)


def detect_failed_breakout(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "first_breakout_mid_x2",
            "last_beyond_extreme_elapsed_us",
            "prior_extreme_lookback_us",
            "prior_extreme_mid_x2",
            "return_elapsed_us",
            "return_mid_x2",
        },
    )
    prior = measurement_int(values, "prior_extreme_mid_x2")
    breakout = measurement_int(values, "first_breakout_mid_x2")
    returned = measurement_int(values, "return_mid_x2")
    last_beyond = measurement_int(values, "last_beyond_extreme_elapsed_us")
    return_elapsed = measurement_int(values, "return_elapsed_us")
    lookback = measurement_int(values, "prior_extreme_lookback_us")
    if min(last_beyond, return_elapsed, lookback) < 0:
        raise ValueError("failed-breakout elapsed evidence must be nonnegative")
    orientation = _direction_sign(opportunity.direction)
    breakout_distance = orientation * (breakout - prior)
    return_distance = orientation * (prior - returned)
    return evaluation(
        (
            (
                lookback == threshold_int(row, "prior_extreme_lookback"),
                "FAILED_BREAKOUT_LOOKBACK_DIFFERS",
            ),
            (
                breakout_distance >= threshold_int(row, "breakout_mid_x2_distance"),
                "BREAKOUT_DISTANCE_BELOW_THRESHOLD",
            ),
            (
                last_beyond < threshold_int(row, "beyond_extreme_span"),
                "BREAKOUT_EXTREME_SPAN_EXCEEDED",
            ),
            (
                return_distance >= threshold_int(row, "return_mid_x2_distance"),
                "BREAKOUT_RETURN_DISTANCE_BELOW_THRESHOLD",
            ),
            (
                return_elapsed <= threshold_int(row, "return_deadline"),
                "BREAKOUT_RETURN_DEADLINE_EXCEEDED",
            ),
            (
                return_elapsed
                == opportunity.activation_us - opportunity.active_start_us,
                "FAILED_BREAKOUT_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                opportunity.side is CandidateSideV1.NOT_APPLICABLE,
                "FAILED_BREAKOUT_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("breakout_distance_mid_x2", breakout_distance),
        DetectorMeasurementV1("return_distance_mid_x2", return_distance),
    )


def detect_aggressive_flow_burst(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "active_buy_quantity",
            "active_sell_quantity",
            "group_duration_us",
            "trailing_group_volumes",
        },
    )
    buy = measurement_int(values, "active_buy_quantity")
    sell = measurement_int(values, "active_sell_quantity")
    duration = measurement_int(values, "group_duration_us")
    trailing = measurement_int_tuple(values, "trailing_group_volumes")
    if buy < 0 or sell < 0 or duration < 0:
        raise ValueError("aggressive-flow evidence must be nonnegative")
    expected_count = threshold_int(row, "trailing_group_count")
    trailing_count_valid = len(trailing) == expected_count
    trailing_p50 = nearest_rank_p50(trailing) if trailing_count_valid else 0
    active_volume = buy + sell
    imbalance = (
        0
        if active_volume == 0
        else round_div_even((buy - sell) * 1_000_000, active_volume)
    )
    required_volume = max(
        threshold_int(row, "active_aggressive_volume_floor"),
        threshold_int(row, "trailing_p50_multiplier") * trailing_p50,
    )
    expected_direction = _signed_direction(imbalance)
    expected_side = CandidateSideV1(expected_direction.value)
    return evaluation(
        (
            (
                duration == 1_000_000,
                "AGGRESSIVE_FLOW_GROUP_DURATION_DIFFERS",
            ),
            (
                duration == opportunity.activation_us - opportunity.active_start_us,
                "AGGRESSIVE_FLOW_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                trailing_count_valid,
                "AGGRESSIVE_FLOW_TRAILING_GROUP_COUNT_DIFFERS",
            ),
            (
                active_volume >= required_volume,
                "AGGRESSIVE_FLOW_VOLUME_BELOW_THRESHOLD",
            ),
            (
                abs(imbalance)
                >= threshold_int(row, "absolute_aggressive_flow_imbalance_ppm"),
                "AGGRESSIVE_FLOW_IMBALANCE_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is expected_direction
                and opportunity.side is expected_side,
                "AGGRESSIVE_FLOW_KEY_DIRECTION_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("active_aggressive_volume", active_volume),
        DetectorMeasurementV1("aggressive_flow_imbalance_ppm", imbalance),
        DetectorMeasurementV1("required_aggressive_volume", required_volume),
        DetectorMeasurementV1("trailing_volume_p50", trailing_p50),
    )


def detect_cancellation_burst(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "active_added_quantity",
            "active_cancelled_quantity",
            "group_duration_us",
            "trailing_group_cancelled_quantities",
        },
    )
    cancelled = measurement_int(values, "active_cancelled_quantity")
    added = measurement_int(values, "active_added_quantity")
    duration = measurement_int(values, "group_duration_us")
    trailing = measurement_int_tuple(values, "trailing_group_cancelled_quantities")
    if cancelled < 0 or added < 0 or duration < 0:
        raise ValueError("cancellation-burst evidence must be nonnegative")
    expected_count = threshold_int(row, "trailing_group_count")
    trailing_count_valid = len(trailing) == expected_count
    trailing_p50 = nearest_rank_p50(trailing) if trailing_count_valid else 0
    required_cancelled = max(
        threshold_int(row, "active_cancelled_quantity_floor"),
        threshold_int(row, "trailing_p50_multiplier") * trailing_p50,
    )
    positive_infinity = added == 0 and cancelled > 0
    cancel_to_add_ratio = (
        0 if added == 0 else ratio_ppm(cancelled, added)
    )
    ratio_satisfied = positive_infinity or cancel_to_add_ratio >= threshold_int(
        row,
        "cancel_to_add_ratio_ppm",
    )
    return evaluation(
        (
            (
                duration == 1_000_000,
                "CANCELLATION_GROUP_DURATION_DIFFERS",
            ),
            (
                duration == opportunity.activation_us - opportunity.active_start_us,
                "CANCELLATION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                trailing_count_valid,
                "CANCELLATION_TRAILING_GROUP_COUNT_DIFFERS",
            ),
            (
                cancelled >= required_cancelled,
                "CANCELLATION_VOLUME_BELOW_THRESHOLD",
            ),
            (ratio_satisfied, "CANCEL_TO_ADD_RATIO_BELOW_THRESHOLD"),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE,
                "CANCELLATION_BURST_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("cancel_to_add_positive_infinity", positive_infinity),
        DetectorMeasurementV1("cancel_to_add_ratio_ppm", cancel_to_add_ratio),
        DetectorMeasurementV1("required_cancelled_quantity", required_cancelled),
        DetectorMeasurementV1("trailing_cancelled_p50", trailing_p50),
    )


def detect_momentum_exhaustion(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "additional_mid_x2_movement",
            "forward_aggressive_flow_imbalance_ppm",
            "forward_window_us",
            "initial_aggressive_flow_imbalance_ppm",
            "initial_mid_x2_movement",
            "initial_window_us",
        },
    )
    orientation = _direction_sign(opportunity.direction)
    initial_movement = orientation * measurement_int(
        values,
        "initial_mid_x2_movement",
    )
    initial_imbalance = orientation * measurement_int(
        values,
        "initial_aggressive_flow_imbalance_ppm",
    )
    additional_movement = orientation * measurement_int(
        values,
        "additional_mid_x2_movement",
    )
    forward_imbalance = measurement_int(
        values,
        "forward_aggressive_flow_imbalance_ppm",
    )
    initial_window = measurement_int(values, "initial_window_us")
    forward_window = measurement_int(values, "forward_window_us")
    if initial_window < 0 or forward_window < 0:
        raise ValueError("momentum-exhaustion windows must be nonnegative")
    return evaluation(
        (
            (
                initial_window == threshold_int(row, "initial_window"),
                "MOMENTUM_INITIAL_WINDOW_DIFFERS",
            ),
            (
                initial_movement >= threshold_int(row, "initial_mid_x2_movement"),
                "MOMENTUM_INITIAL_MOVEMENT_BELOW_THRESHOLD",
            ),
            (
                initial_imbalance >= threshold_int(row, "same_direction_imbalance_ppm"),
                "MOMENTUM_INITIAL_IMBALANCE_BELOW_THRESHOLD",
            ),
            (
                forward_window == threshold_int(row, "forward_window"),
                "MOMENTUM_FORWARD_WINDOW_DIFFERS",
            ),
            (
                initial_window + forward_window
                == opportunity.activation_us - opportunity.active_start_us,
                "MOMENTUM_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                additional_movement
                <= threshold_int(row, "additional_same_direction_movement"),
                "MOMENTUM_ADDITIONAL_MOVEMENT_ABOVE_THRESHOLD",
            ),
            (
                abs(forward_imbalance)
                <= threshold_int(row, "forward_absolute_imbalance_ppm"),
                "MOMENTUM_FORWARD_IMBALANCE_ABOVE_THRESHOLD",
            ),
            (
                opportunity.side is CandidateSideV1(opportunity.direction.value),
                "MOMENTUM_EXHAUSTION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("oriented_additional_movement", additional_movement),
        DetectorMeasurementV1("oriented_initial_imbalance_ppm", initial_imbalance),
        DetectorMeasurementV1("oriented_initial_movement", initial_movement),
    )


def detect_mean_reversion_transition(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "activation_aggressive_flow_imbalance_ppm",
            "final_displacement_mid_x2",
            "initial_displacement_mid_x2",
            "return_aggressive_flow_imbalance_ppm",
            "return_elapsed_us",
            "trailing_p50_window_us",
        },
    )
    orientation = _direction_sign(opportunity.direction)
    initial = orientation * measurement_int(values, "initial_displacement_mid_x2")
    final = orientation * measurement_int(values, "final_displacement_mid_x2")
    activation_imbalance = orientation * measurement_int(
        values,
        "activation_aggressive_flow_imbalance_ppm",
    )
    return_imbalance = orientation * measurement_int(
        values,
        "return_aggressive_flow_imbalance_ppm",
    )
    elapsed = measurement_int(values, "return_elapsed_us")
    lookback = measurement_int(values, "trailing_p50_window_us")
    if elapsed < 0 or lookback < 0:
        raise ValueError("mean-reversion windows must be nonnegative")
    toward = max(0, initial - final)
    return_share = 0 if initial <= 0 else unsigned_share_ppm(toward, initial)
    imbalance_floor = threshold_int(row, "imbalance_signed_magnitude")
    return evaluation(
        (
            (
                lookback == threshold_int(row, "trailing_p50_window"),
                "MEAN_REVERSION_LOOKBACK_DIFFERS",
            ),
            (
                initial >= threshold_int(row, "initial_displacement"),
                "MEAN_REVERSION_DISPLACEMENT_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "return_window"),
                "MEAN_REVERSION_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "MEAN_REVERSION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                return_share >= threshold_int(row, "return_share_ppm"),
                "MEAN_REVERSION_RETURN_SHARE_BELOW_THRESHOLD",
            ),
            (
                activation_imbalance >= imbalance_floor
                and return_imbalance <= -imbalance_floor,
                "MEAN_REVERSION_IMBALANCE_SIGN_CHANGE_MISSING",
            ),
            (
                opportunity.side is CandidateSideV1.NOT_APPLICABLE,
                "MEAN_REVERSION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("oriented_initial_displacement", initial),
        DetectorMeasurementV1("return_share_ppm", return_share),
        DetectorMeasurementV1("toward_frozen_p50_mid_x2", toward),
    )


def _direction_sign(direction: CandidateDirectionV1) -> int:
    if direction is CandidateDirectionV1.BUY:
        return 1
    if direction is CandidateDirectionV1.SELL:
        return -1
    raise ValueError("directional flow detector requires BUY or SELL")


def _signed_direction(value: int) -> CandidateDirectionV1:
    if value > 0:
        return CandidateDirectionV1.BUY
    if value < 0:
        return CandidateDirectionV1.SELL
    return CandidateDirectionV1.NOT_APPLICABLE


FLOW_DETECTOR_HANDLERS_V1: Mapping[str, DetectorHandlerV1] = MappingProxyType(
    {
        "AGGRESSIVE_FLOW_BURST": detect_aggressive_flow_burst,
        "CANCELLATION_BURST": detect_cancellation_burst,
        "FAILED_BREAKOUT": detect_failed_breakout,
        "MEAN_REVERSION_TRANSITION": detect_mean_reversion_transition,
        "MOMENTUM_EXHAUSTION": detect_momentum_exhaustion,
    }
)


__all__ = [
    "FLOW_DETECTOR_HANDLERS_V1",
    "detect_aggressive_flow_burst",
    "detect_cancellation_burst",
    "detect_failed_breakout",
    "detect_mean_reversion_transition",
    "detect_momentum_exhaustion",
]
