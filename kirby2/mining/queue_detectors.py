"""Operational WO33-B1 queue, absorption, and liquidity detectors."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .models import CandidateDirectionV1, CandidateSideV1, round_div_even, unsigned_share_ppm
from .runtime import (
    DetectorHandlerV1,
    DetectorMeasurementV1,
    DetectorOpportunityV1,
    RuleEvaluationV1,
    evaluation,
    exact_measurements,
    measurement_bool,
    measurement_int,
    measurement_int_tuple,
    threshold_int,
)


def detect_strong_queue_imbalance(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "ask_top",
            "best_ask_ticks",
            "best_bid_ticks",
            "bid_top",
            "continuous_duration_us",
        },
    )
    bid_top = measurement_int(values, "bid_top")
    ask_top = measurement_int(values, "ask_top")
    best_bid = measurement_int(values, "best_bid_ticks")
    best_ask = measurement_int(values, "best_ask_ticks")
    duration_us = measurement_int(values, "continuous_duration_us")
    if min(bid_top, ask_top, best_bid, best_ask, duration_us) < 0:
        raise ValueError("queue-imbalance evidence must be nonnegative")
    total = bid_top + ask_top
    imbalance = 0 if total == 0 else round_div_even((bid_top - ask_top) * 1_000_000, total)
    expected_direction = (
        CandidateDirectionV1.BUY
        if imbalance > 0
        else CandidateDirectionV1.SELL
        if imbalance < 0
        else CandidateDirectionV1.NOT_APPLICABLE
    )
    expected_side = CandidateSideV1(expected_direction.value)
    expected_price = best_bid if imbalance > 0 else best_ask
    return evaluation(
        (
            (total > 0, "ZERO_TOP_QUANTITY_DENOMINATOR"),
            (
                total >= threshold_int(row, "top_total"),
                "TOP_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                abs(imbalance)
                >= threshold_int(row, "absolute_queue_imbalance_ppm"),
                "QUEUE_IMBALANCE_BELOW_THRESHOLD",
            ),
            (
                duration_us >= threshold_int(row, "continuous_duration"),
                "QUEUE_IMBALANCE_DURATION_BELOW_THRESHOLD",
            ),
            (
                duration_us == opportunity.activation_us - opportunity.active_start_us,
                "QUEUE_IMBALANCE_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                opportunity.direction is expected_direction
                and opportunity.side is expected_side
                and opportunity.price == expected_price,
                "QUEUE_IMBALANCE_KEY_DIRECTION_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("queue_imbalance_ppm", imbalance),
        DetectorMeasurementV1("top_total", total),
    )


def detect_queue_depletion(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {"elapsed_us", "minimum_displayed_quantity", "starting_displayed_quantity"},
    )
    start = measurement_int(values, "starting_displayed_quantity")
    minimum = measurement_int(values, "minimum_displayed_quantity")
    elapsed_us = measurement_int(values, "elapsed_us")
    if start < 0 or minimum < 0 or minimum > start or elapsed_us < 0:
        raise ValueError("queue-depletion evidence is inconsistent")
    depletion = 0 if start == 0 else unsigned_share_ppm(start - minimum, start)
    return evaluation(
        (
            (
                start >= threshold_int(row, "starting_displayed_quantity"),
                "STARTING_QUEUE_BELOW_THRESHOLD",
            ),
            (
                depletion >= threshold_int(row, "depletion_share_ppm"),
                "QUEUE_DEPLETION_BELOW_THRESHOLD",
            ),
            (
                elapsed_us <= threshold_int(row, "window"),
                "QUEUE_DEPLETION_WINDOW_EXCEEDED",
            ),
            (
                elapsed_us == opportunity.activation_us - opportunity.active_start_us,
                "QUEUE_DEPLETION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE,
                "QUEUE_DEPLETION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("depletion_share_ppm", depletion),
    )


def detect_queue_replenishment(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "cumulative_add_and_refresh",
            "cycle_minimum_quantities",
            "cycle_return_elapsed_us",
            "cycle_return_quantities",
            "cycle_start_quantities",
            "elapsed_us",
            "greedy_nonoverlapping_cycles",
        },
    )
    starts = measurement_int_tuple(values, "cycle_start_quantities")
    minima = measurement_int_tuple(values, "cycle_minimum_quantities")
    returns = measurement_int_tuple(values, "cycle_return_quantities")
    return_elapsed = measurement_int_tuple(values, "cycle_return_elapsed_us")
    if not (len(starts) == len(minima) == len(returns) == len(return_elapsed)):
        raise ValueError("queue-replenishment cycle vectors differ in length")
    depletion_floor = threshold_int(row, "cycle_depletion_share_ppm")
    return_deadline = threshold_int(row, "return_deadline")
    divisor = threshold_int(row, "return_quantity_divisor")
    qualifying_cycles = 0
    depletion_values: list[int] = []
    for start, minimum, returned, elapsed in zip(
        starts,
        minima,
        returns,
        return_elapsed,
        strict=True,
    ):
        if start <= 0 or not 0 <= minimum <= start or returned < 0 or elapsed < 0:
            raise ValueError("queue-replenishment cycle evidence is inconsistent")
        depletion = unsigned_share_ppm(start - minimum, start)
        depletion_values.append(depletion)
        if (
            depletion >= depletion_floor
            and returned >= round_div_even(start, divisor)
            and elapsed <= return_deadline
        ):
            qualifying_cycles += 1
    elapsed_us = measurement_int(values, "elapsed_us")
    cumulative = measurement_int(values, "cumulative_add_and_refresh")
    if elapsed_us < 0 or cumulative < 0:
        raise ValueError("queue-replenishment totals must be nonnegative")
    return evaluation(
        (
            (
                measurement_bool(values, "greedy_nonoverlapping_cycles"),
                "QUEUE_CYCLES_NOT_GREEDY_NONOVERLAPPING",
            ),
            (
                elapsed_us <= threshold_int(row, "window"),
                "QUEUE_REPLENISHMENT_WINDOW_EXCEEDED",
            ),
            (
                elapsed_us == opportunity.activation_us - opportunity.active_start_us,
                "QUEUE_REPLENISHMENT_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                qualifying_cycles >= threshold_int(row, "cycle_count"),
                "QUEUE_REPLENISHMENT_CYCLE_COUNT_BELOW_THRESHOLD",
            ),
            (
                cumulative >= threshold_int(row, "cumulative_add_and_refresh"),
                "QUEUE_REPLENISHMENT_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE,
                "QUEUE_REPLENISHMENT_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("qualifying_cycle_count", qualifying_cycles),
        DetectorMeasurementV1(
            "minimum_cycle_depletion_share_ppm",
            min(depletion_values, default=0),
        ),
    )


def detect_bid_absorption(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "aggressive_sell_quantity",
            "bid_add_and_refresh_at_opening_price",
            "elapsed_us",
            "minimum_best_bid_ticks",
            "opening_best_bid_ticks",
        },
    )
    aggressive = measurement_int(values, "aggressive_sell_quantity")
    added = measurement_int(values, "bid_add_and_refresh_at_opening_price")
    elapsed = measurement_int(values, "elapsed_us")
    opening = measurement_int(values, "opening_best_bid_ticks")
    minimum = measurement_int(values, "minimum_best_bid_ticks")
    if min(aggressive, added, elapsed, opening, minimum) < 0:
        raise ValueError("bid-absorption evidence must be nonnegative")
    decrease = max(0, opening - minimum)
    return evaluation(
        (
            (
                aggressive >= threshold_int(row, "aggressive_sell_quantity"),
                "AGGRESSIVE_SELL_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "window"),
                "BID_ABSORPTION_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "BID_ABSORPTION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                decrease == threshold_int(row, "best_bid_decrease"),
                "BEST_BID_DECREASED",
            ),
            (
                added >= threshold_int(row, "bid_add_and_refresh"),
                "BID_ADD_AND_REFRESH_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is CandidateDirectionV1.BUY
                and opportunity.side is CandidateSideV1.BUY
                and opportunity.price == opening,
                "BID_ABSORPTION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("best_bid_decrease_ticks", decrease),
    )


def detect_ask_absorption(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "aggressive_buy_quantity",
            "ask_add_and_refresh_at_opening_price",
            "elapsed_us",
            "maximum_best_ask_ticks",
            "opening_best_ask_ticks",
        },
    )
    aggressive = measurement_int(values, "aggressive_buy_quantity")
    added = measurement_int(values, "ask_add_and_refresh_at_opening_price")
    elapsed = measurement_int(values, "elapsed_us")
    opening = measurement_int(values, "opening_best_ask_ticks")
    maximum = measurement_int(values, "maximum_best_ask_ticks")
    if min(aggressive, added, elapsed, opening, maximum) < 0:
        raise ValueError("ask-absorption evidence must be nonnegative")
    increase = max(0, maximum - opening)
    return evaluation(
        (
            (
                aggressive >= threshold_int(row, "aggressive_buy_quantity"),
                "AGGRESSIVE_BUY_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "window"),
                "ASK_ABSORPTION_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "ASK_ABSORPTION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                increase == threshold_int(row, "best_ask_increase"),
                "BEST_ASK_INCREASED",
            ),
            (
                added >= threshold_int(row, "ask_add_and_refresh"),
                "ASK_ADD_AND_REFRESH_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is CandidateDirectionV1.SELL
                and opportunity.side is CandidateSideV1.SELL
                and opportunity.price == opening,
                "ASK_ABSORPTION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("best_ask_increase_ticks", increase),
    )


def detect_liquidity_vacuum(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "cancelled_quantity",
            "elapsed_us",
            "ending_spread_ticks",
            "executed_quantity",
            "minimum_three_level_depth",
            "side_empty",
            "starting_spread_ticks",
            "starting_three_level_depth",
        },
    )
    start = measurement_int(values, "starting_three_level_depth")
    minimum = measurement_int(values, "minimum_three_level_depth")
    cancelled = measurement_int(values, "cancelled_quantity")
    executed = measurement_int(values, "executed_quantity")
    elapsed = measurement_int(values, "elapsed_us")
    starting_spread = measurement_int(values, "starting_spread_ticks")
    ending_spread = measurement_int(values, "ending_spread_ticks")
    if (
        start < 0
        or minimum < 0
        or minimum > start
        or min(cancelled, executed, elapsed, starting_spread, ending_spread) < 0
    ):
        raise ValueError("liquidity-vacuum evidence is inconsistent")
    depletion = 0 if start == 0 else unsigned_share_ppm(start - minimum, start)
    removed = cancelled + executed
    cancel_share = 0 if removed == 0 else unsigned_share_ppm(cancelled, removed)
    spread_expansion = ending_spread - starting_spread
    return evaluation(
        (
            (
                start > threshold_int(row, "starting_three_level_depth"),
                "STARTING_DEPTH_NOT_POSITIVE",
            ),
            (
                depletion >= threshold_int(row, "depletion_share_ppm"),
                "LIQUIDITY_DEPLETION_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "window"),
                "LIQUIDITY_VACUUM_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "LIQUIDITY_VACUUM_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                removed > 0,
                "ZERO_REMOVED_QUANTITY_DENOMINATOR",
            ),
            (
                cancel_share >= threshold_int(row, "cancel_share_ppm"),
                "LIQUIDITY_CANCEL_SHARE_BELOW_THRESHOLD",
            ),
            (
                measurement_bool(values, "side_empty")
                or spread_expansion >= threshold_int(row, "spread_expansion"),
                "LIQUIDITY_FINAL_BRANCH_NOT_SATISFIED",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE,
                "LIQUIDITY_VACUUM_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("cancel_share_ppm", cancel_share),
        DetectorMeasurementV1("depletion_share_ppm", depletion),
        DetectorMeasurementV1("spread_expansion_ticks", spread_expansion),
    )


def detect_spread_expansion(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "expanded_spread_ticks",
            "persistence_us",
            "starting_spread_ticks",
            "transition_elapsed_us",
        },
    )
    starting = measurement_int(values, "starting_spread_ticks")
    expanded = measurement_int(values, "expanded_spread_ticks")
    elapsed = measurement_int(values, "transition_elapsed_us")
    persistence = measurement_int(values, "persistence_us")
    if min(starting, expanded, elapsed, persistence) < 0:
        raise ValueError("spread-expansion evidence must be nonnegative")
    return evaluation(
        (
            (
                starting <= threshold_int(row, "starting_spread"),
                "STARTING_SPREAD_ABOVE_THRESHOLD",
            ),
            (
                expanded >= threshold_int(row, "expanded_spread"),
                "EXPANDED_SPREAD_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "transition_window"),
                "SPREAD_TRANSITION_WINDOW_EXCEEDED",
            ),
            (
                persistence >= threshold_int(row, "persistence"),
                "SPREAD_EXPANSION_PERSISTENCE_BELOW_THRESHOLD",
            ),
            (
                elapsed + persistence
                == opportunity.activation_us - opportunity.active_start_us,
                "SPREAD_EXPANSION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is CandidateSideV1.NOT_APPLICABLE,
                "SPREAD_EXPANSION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("spread_expansion_ticks", expanded - starting),
    )


def detect_spread_recovery(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {"persistence_us", "recovered_spread_ticks", "recovery_elapsed_us"},
    )
    recovered = measurement_int(values, "recovered_spread_ticks")
    elapsed = measurement_int(values, "recovery_elapsed_us")
    persistence = measurement_int(values, "persistence_us")
    if min(recovered, elapsed, persistence) < 0:
        raise ValueError("spread-recovery evidence must be nonnegative")
    return evaluation(
        (
            (
                recovered <= threshold_int(row, "recovered_spread"),
                "RECOVERED_SPREAD_ABOVE_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "recovery_deadline"),
                "SPREAD_RECOVERY_DEADLINE_EXCEEDED",
            ),
            (
                persistence >= threshold_int(row, "persistence"),
                "SPREAD_RECOVERY_PERSISTENCE_BELOW_THRESHOLD",
            ),
            (
                elapsed + persistence
                == opportunity.activation_us - opportunity.active_start_us,
                "SPREAD_RECOVERY_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is CandidateSideV1.NOT_APPLICABLE
                and opportunity.witness_kind == "SPREAD_EXPANSION_PARENT",
                "SPREAD_RECOVERY_KEY_MISMATCH",
            ),
        ),
    )


def detect_hidden_reserve_refresh(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "authoritative_refresh_labels",
            "elapsed_us",
            "executed_quantity",
            "maximum_displayed_quantity",
            "reserve_refresh_after_execution_count",
        },
    )
    executed = measurement_int(values, "executed_quantity")
    elapsed = measurement_int(values, "elapsed_us")
    maximum = measurement_int(values, "maximum_displayed_quantity")
    refreshes = measurement_int(values, "reserve_refresh_after_execution_count")
    if min(executed, elapsed, maximum, refreshes) < 0:
        raise ValueError("hidden-refresh evidence must be nonnegative")
    return evaluation(
        (
            (
                measurement_bool(values, "authoritative_refresh_labels"),
                "RESERVE_REFRESH_LABELS_NOT_AUTHORITATIVE",
            ),
            (
                executed >= threshold_int(row, "executed_quantity"),
                "HIDDEN_EXECUTED_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "window"),
                "HIDDEN_REFRESH_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "HIDDEN_REFRESH_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                maximum <= threshold_int(row, "maximum_displayed_quantity"),
                "HIDDEN_DISPLAYED_QUANTITY_ABOVE_THRESHOLD",
            ),
            (
                refreshes >= threshold_int(row, "reserve_refresh_count"),
                "HIDDEN_REFRESH_COUNT_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE,
                "HIDDEN_REFRESH_KEY_MISMATCH",
            ),
        ),
    )


def detect_apparent_liquidity_mirage(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "cohort_cancelled_quantity",
            "cohort_displayed_peak",
            "cohort_executed_quantity",
            "cohort_frozen_at_first_boundary",
            "elapsed_us",
        },
    )
    peak = measurement_int(values, "cohort_displayed_peak")
    cancelled = measurement_int(values, "cohort_cancelled_quantity")
    executed = measurement_int(values, "cohort_executed_quantity")
    elapsed = measurement_int(values, "elapsed_us")
    if (
        peak < 0
        or cancelled < 0
        or executed < 0
        or elapsed < 0
        or cancelled + executed > peak
    ):
        raise ValueError("apparent-mirage cohort quantities are inconsistent")
    cancelled_share = 0 if peak == 0 else unsigned_share_ppm(cancelled, peak)
    executed_share = 0 if peak == 0 else unsigned_share_ppm(executed, peak)
    return evaluation(
        (
            (
                measurement_bool(values, "cohort_frozen_at_first_boundary"),
                "MIRAGE_COHORT_NOT_FROZEN_AT_FIRST_BOUNDARY",
            ),
            (
                peak >= threshold_int(row, "cohort_displayed_sum"),
                "MIRAGE_COHORT_PEAK_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "window"),
                "MIRAGE_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "MIRAGE_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                cancelled_share >= threshold_int(row, "cohort_cancelled_share_ppm"),
                "MIRAGE_CANCELLED_SHARE_BELOW_THRESHOLD",
            ),
            (
                executed_share <= threshold_int(row, "cohort_executed_share_ppm"),
                "MIRAGE_EXECUTED_SHARE_ABOVE_THRESHOLD",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE
                and opportunity.witness_kind == "ORDER_COHORT",
                "MIRAGE_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("cohort_cancelled_share_ppm", cancelled_share),
        DetectorMeasurementV1("cohort_executed_share_ppm", executed_share),
    )


QUEUE_DETECTOR_HANDLERS_V1: Mapping[str, DetectorHandlerV1] = MappingProxyType(
    {
        "APPARENT_LIQUIDITY_MIRAGE": detect_apparent_liquidity_mirage,
        "ASK_ABSORPTION": detect_ask_absorption,
        "BID_ABSORPTION": detect_bid_absorption,
        "HIDDEN_RESERVE_REFRESH": detect_hidden_reserve_refresh,
        "LIQUIDITY_VACUUM": detect_liquidity_vacuum,
        "QUEUE_DEPLETION": detect_queue_depletion,
        "QUEUE_REPLENISHMENT": detect_queue_replenishment,
        "SPREAD_EXPANSION": detect_spread_expansion,
        "SPREAD_RECOVERY": detect_spread_recovery,
        "STRONG_QUEUE_IMBALANCE": detect_strong_queue_imbalance,
    }
)


__all__ = [
    "QUEUE_DETECTOR_HANDLERS_V1",
    "detect_apparent_liquidity_mirage",
    "detect_ask_absorption",
    "detect_bid_absorption",
    "detect_hidden_reserve_refresh",
    "detect_liquidity_vacuum",
    "detect_queue_depletion",
    "detect_queue_replenishment",
    "detect_spread_expansion",
    "detect_spread_recovery",
    "detect_strong_queue_imbalance",
]
