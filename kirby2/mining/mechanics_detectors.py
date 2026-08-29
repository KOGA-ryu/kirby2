"""Operational WO33-B2 auction, halt/reopen, and participant-flow detectors."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .models import (
    CandidateDirectionV1,
    CandidateSideV1,
    ratio_ppm,
    unsigned_share_ppm,
)
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
    time_weighted_nearest_rank_p50,
)


def detect_auction_imbalance_change(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "new_imbalance_shares",
            "old_imbalance_shares",
            "publication_interval_us",
        },
    )
    old = measurement_int(values, "old_imbalance_shares")
    new = measurement_int(values, "new_imbalance_shares")
    interval = measurement_int(values, "publication_interval_us")
    if interval < 0:
        raise ValueError("auction publication interval must be nonnegative")
    signed_change = new - old
    absolute_change = abs(signed_change)
    relative_change = unsigned_share_ppm(absolute_change, max(1, abs(old)))
    signs_differ = (old < 0 < new) or (new < 0 < old)
    magnitude_threshold = threshold_int(row, "sign_change_minimum_magnitude")
    absolute_relative_branch = (
        absolute_change >= threshold_int(row, "absolute_share_change")
        and relative_change >= threshold_int(row, "relative_change_ppm")
    )
    sign_change_branch = (
        signs_differ
        and abs(old) >= magnitude_threshold
        and abs(new) >= magnitude_threshold
    )
    event_times = {
        event.event_id: event.timestamp_us for event in opportunity.contributing_events
    }
    witness_interval = (
        event_times[opportunity.witness_ids[1]]
        - event_times[opportunity.witness_ids[0]]
    )
    expected_direction = _signed_direction(signed_change)
    return evaluation(
        (
            (
                interval <= threshold_int(row, "publication_interval"),
                "AUCTION_PUBLICATION_INTERVAL_EXCEEDED",
            ),
            (
                interval == witness_interval
                == opportunity.activation_us - opportunity.active_start_us,
                "AUCTION_PUBLICATION_INTERVAL_DIFFERS_FROM_EVENTS",
            ),
            (
                absolute_relative_branch or sign_change_branch,
                "AUCTION_IMBALANCE_CHANGE_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is expected_direction
                and opportunity.side.value == expected_direction.value
                and opportunity.venue == "CONSOLIDATED"
                and opportunity.price == "NOT_APPLICABLE"
                and opportunity.witness_kind == "AUCTION_PUBLICATION_PAIR",
                "AUCTION_IMBALANCE_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("absolute_share_change", absolute_change),
        DetectorMeasurementV1(
            "absolute_relative_branch_satisfied",
            absolute_relative_branch,
        ),
        DetectorMeasurementV1("relative_change_ppm", relative_change),
        DetectorMeasurementV1("sign_change_branch_satisfied", sign_change_branch),
    )


def detect_halt_reopening(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "first_post_reopen_trade_ticks",
            "halt_time_us",
            "last_pre_halt_trade_ticks",
            "post_spread_durations_us",
            "post_spread_ticks",
            "post_window_coverage_us",
            "pre_spread_durations_us",
            "pre_spread_ticks",
            "pre_window_coverage_us",
            "reopen_time_us",
        },
    )
    halt_time = measurement_int(values, "halt_time_us")
    reopen_time = measurement_int(values, "reopen_time_us")
    last_pre_trade = measurement_int(values, "last_pre_halt_trade_ticks")
    first_post_trade = measurement_int(values, "first_post_reopen_trade_ticks")
    pre_values = measurement_int_tuple(values, "pre_spread_ticks")
    pre_durations = measurement_int_tuple(values, "pre_spread_durations_us")
    post_values = measurement_int_tuple(values, "post_spread_ticks")
    post_durations = measurement_int_tuple(values, "post_spread_durations_us")
    pre_coverage = measurement_int(values, "pre_window_coverage_us")
    post_coverage = measurement_int(values, "post_window_coverage_us")
    pre_window = threshold_int(row, "pre_spread_window")
    post_window = threshold_int(row, "post_spread_window")
    event_times = {
        event.event_id: event.timestamp_us for event in opportunity.contributing_events
    }
    complete_evidence = (
        halt_time >= 0
        and reopen_time > halt_time
        and last_pre_trade > 0
        and first_post_trade > 0
        and len(pre_values) == len(pre_durations) > 0
        and len(post_values) == len(post_durations) > 0
        and all(value > 0 for value in (*pre_values, *post_values))
        and all(duration > 0 for duration in (*pre_durations, *post_durations))
        and pre_coverage == pre_window == sum(pre_durations)
        and post_coverage == post_window == sum(post_durations)
        and opportunity.active_start_us == halt_time
        and opportunity.activation_us == reopen_time + post_window
        and event_times[opportunity.witness_ids[0]] == halt_time
        and event_times[opportunity.witness_ids[1]] == reopen_time
    )
    if not complete_evidence:
        return RuleEvaluationV1(False, ("INSUFFICIENT_EVIDENCE",))
    pre_p50 = time_weighted_nearest_rank_p50(pre_values, pre_durations)
    post_p50 = time_weighted_nearest_rank_p50(post_values, post_durations)
    if pre_p50 == 0:
        return RuleEvaluationV1(False, ("INSUFFICIENT_EVIDENCE",))
    signed_gap = first_post_trade - last_pre_trade
    absolute_gap = abs(signed_gap)
    spread_ratio = ratio_ppm(post_p50, pre_p50)
    price_branch = absolute_gap >= threshold_int(row, "price_gap")
    spread_branch = spread_ratio >= threshold_int(row, "spread_ratio_ppm")
    expected_direction = (
        _signed_direction(signed_gap)
        if price_branch
        else CandidateDirectionV1.NOT_APPLICABLE
    )
    return evaluation(
        (
            (
                price_branch or spread_branch,
                "HALT_REOPENING_CHANGE_BELOW_THRESHOLD",
            ),
            (
                opportunity.direction is expected_direction
                and opportunity.side is CandidateSideV1.NOT_APPLICABLE
                and opportunity.venue == "CONSOLIDATED"
                and opportunity.price == "NOT_APPLICABLE"
                and opportunity.witness_kind == "HALT_REOPEN_PAIR",
                "HALT_REOPENING_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("absolute_price_gap_ticks", absolute_gap),
        DetectorMeasurementV1("post_spread_p50_ticks", post_p50),
        DetectorMeasurementV1("pre_spread_p50_ticks", pre_p50),
        DetectorMeasurementV1("price_gap_branch_satisfied", price_branch),
        DetectorMeasurementV1("signed_price_gap_ticks", signed_gap),
        DetectorMeasurementV1("spread_branch_satisfied", spread_branch),
        DetectorMeasurementV1("spread_ratio_ppm", spread_ratio),
    )


def detect_distressed_liquidation(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "authoritative_participant_identity",
            "distressed_buy_quantity",
            "distressed_sell_quantity",
            "elapsed_us",
            "first_mid_x2",
            "last_mid_x2",
        },
    )
    buys = measurement_int(values, "distressed_buy_quantity")
    sells = measurement_int(values, "distressed_sell_quantity")
    elapsed = measurement_int(values, "elapsed_us")
    first_mid = measurement_int(values, "first_mid_x2")
    last_mid = measurement_int(values, "last_mid_x2")
    if min(buys, sells, elapsed, first_mid, last_mid) < 0:
        raise ValueError("distressed-liquidation evidence must be nonnegative")
    positive_infinity = buys == 0 and sells > 0
    sell_to_buy_ratio = 0 if buys == 0 else ratio_ppm(sells, buys)
    ratio_satisfied = positive_infinity or sell_to_buy_ratio >= threshold_int(
        row,
        "sell_to_buy_ratio_ppm",
    )
    signed_movement = last_mid - first_mid
    return evaluation(
        (
            (
                measurement_bool(values, "authoritative_participant_identity"),
                "AUTHORITATIVE_PARTICIPANT_IDENTITY_MISSING",
            ),
            (
                sells >= threshold_int(row, "distressed_sell_quantity"),
                "DISTRESSED_SELL_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                elapsed <= threshold_int(row, "window"),
                "DISTRESSED_LIQUIDATION_WINDOW_EXCEEDED",
            ),
            (
                elapsed == opportunity.activation_us - opportunity.active_start_us,
                "DISTRESSED_LIQUIDATION_EVIDENCE_SPAN_DIFFERS",
            ),
            (ratio_satisfied, "DISTRESSED_SELL_TO_BUY_RATIO_BELOW_THRESHOLD"),
            (
                signed_movement <= threshold_int(row, "signed_mid_x2_movement"),
                "DISTRESSED_MID_MOVEMENT_ABOVE_THRESHOLD",
            ),
            (
                opportunity.direction is CandidateDirectionV1.SELL
                and opportunity.side is CandidateSideV1.SELL
                and opportunity.venue == "CONSOLIDATED"
                and opportunity.price == "NOT_APPLICABLE"
                and opportunity.witness_kind == "NOT_APPLICABLE",
                "DISTRESSED_LIQUIDATION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1(
            "sell_to_buy_positive_infinity",
            positive_infinity,
        ),
        DetectorMeasurementV1("sell_to_buy_ratio_ppm", sell_to_buy_ratio),
        DetectorMeasurementV1("signed_mid_x2_movement", signed_movement),
    )


def _signed_direction(value: int) -> CandidateDirectionV1:
    if value > 0:
        return CandidateDirectionV1.BUY
    if value < 0:
        return CandidateDirectionV1.SELL
    return CandidateDirectionV1.NOT_APPLICABLE


MECHANICS_DETECTOR_HANDLERS_V1: Mapping[str, DetectorHandlerV1] = MappingProxyType(
    {
        "AUCTION_IMBALANCE_CHANGE": detect_auction_imbalance_change,
        "DISTRESSED_LIQUIDATION": detect_distressed_liquidation,
        "HALT_REOPENING": detect_halt_reopening,
    }
)


__all__ = [
    "MECHANICS_DETECTOR_HANDLERS_V1",
    "detect_auction_imbalance_change",
    "detect_distressed_liquidation",
    "detect_halt_reopening",
]
