"""Operational WO33-B2 venue-fragmentation and route-choice detectors."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .models import CandidateDirectionV1, CandidateSideV1
from .runtime import (
    DetectorHandlerV1,
    DetectorMeasurementV1,
    DetectorOpportunityV1,
    RuleEvaluationV1,
    evaluation,
    exact_measurements,
    measurement_int,
    measurement_int_tuple,
    threshold_int,
)


def detect_multi_venue_fragmentation(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "persistence_us",
            "venue_best_ask_ticks",
            "venue_best_bid_ticks",
            "venue_executable_quantities",
        },
    )
    bids = measurement_int_tuple(values, "venue_best_bid_ticks")
    asks = measurement_int_tuple(values, "venue_best_ask_ticks")
    quantities = measurement_int_tuple(values, "venue_executable_quantities")
    persistence = measurement_int(values, "persistence_us")
    if not (len(bids) == len(asks) == len(quantities) == 2):
        raise ValueError("multi-venue fragmentation requires exactly two venues")
    if (
        persistence < 0
        or any(bid <= 0 for bid in bids)
        or any(ask <= 0 for ask in asks)
        or any(quantity < 0 for quantity in quantities)
        or any(bid >= ask for bid, ask in zip(bids, asks, strict=True))
    ):
        raise ValueError("multi-venue quote/depth evidence is inconsistent")
    bid_difference = max(bids) - min(bids)
    ask_difference = max(asks) - min(asks)
    affected_difference = {
        CandidateSideV1.BUY: bid_difference,
        CandidateSideV1.SELL: ask_difference,
    }.get(opportunity.side, -1)
    price_threshold = threshold_int(row, "best_price_difference")
    return evaluation(
        (
            (
                all(
                    quantity
                    >= threshold_int(row, "per_venue_executable_quantity")
                    for quantity in quantities
                ),
                "PER_VENUE_EXECUTABLE_QUANTITY_BELOW_THRESHOLD",
            ),
            (
                affected_difference >= price_threshold,
                "AFFECTED_VENUE_PRICE_DIFFERENCE_BELOW_THRESHOLD",
            ),
            (
                persistence >= threshold_int(row, "persistence"),
                "VENUE_FRAGMENTATION_PERSISTENCE_BELOW_THRESHOLD",
            ),
            (
                persistence
                == opportunity.activation_us - opportunity.active_start_us,
                "VENUE_FRAGMENTATION_EVIDENCE_SPAN_DIFFERS",
            ),
            (
                opportunity.direction is CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side is not CandidateSideV1.NOT_APPLICABLE
                and opportunity.venue == "NOT_APPLICABLE"
                and opportunity.price == "NOT_APPLICABLE"
                and opportunity.witness_kind == "VENUE_PAIR",
                "VENUE_FRAGMENTATION_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("ask_price_difference_ticks", ask_difference),
        DetectorMeasurementV1("bid_price_difference_ticks", bid_difference),
    )


def detect_routing_dilemma(
    opportunity: DetectorOpportunityV1,
    row: Mapping[str, object],
) -> RuleEvaluationV1:
    values = exact_measurements(
        opportunity,
        {
            "route_a_executable_quantity",
            "route_a_expected_receipt_time_us",
            "route_a_fee_adjusted_cost_milliticks_per_share",
            "route_b_executable_quantity",
            "route_b_expected_receipt_time_us",
            "route_b_fee_adjusted_cost_milliticks_per_share",
        },
    )
    cost_a = measurement_int(
        values,
        "route_a_fee_adjusted_cost_milliticks_per_share",
    )
    cost_b = measurement_int(
        values,
        "route_b_fee_adjusted_cost_milliticks_per_share",
    )
    quantity_a = measurement_int(values, "route_a_executable_quantity")
    quantity_b = measurement_int(values, "route_b_executable_quantity")
    receipt_a = measurement_int(values, "route_a_expected_receipt_time_us")
    receipt_b = measurement_int(values, "route_b_expected_receipt_time_us")
    if (
        min(cost_a, cost_b, receipt_a, receipt_b) < 0
        or quantity_a <= 0
        or quantity_b <= 0
    ):
        raise ValueError("routing-dilemma route evidence is inconsistent")
    a_better_axes = sum(
        (
            cost_a < cost_b,
            quantity_a > quantity_b,
            receipt_a < receipt_b,
        )
    )
    a_worse_axes = sum(
        (
            cost_a > cost_b,
            quantity_a < quantity_b,
            receipt_a > receipt_b,
        )
    )
    cost_difference = abs(cost_a - cost_b)
    quantity_difference = abs(quantity_a - quantity_b)
    receipt_difference = abs(receipt_a - receipt_b)
    material_difference = (
        cost_difference >= threshold_int(row, "absolute_cost_difference")
        or quantity_difference >= threshold_int(row, "absolute_quantity_difference")
        or receipt_difference
        >= threshold_int(row, "absolute_receipt_time_difference")
    )
    return evaluation(
        (
            (
                a_better_axes >= 1 and a_worse_axes >= 1,
                "ROUTES_ARE_NOT_PARETO_INCOMPARABLE",
            ),
            (
                material_difference,
                "ROUTE_DIFFERENCES_ARE_IMMATERIAL",
            ),
            (
                opportunity.direction is not CandidateDirectionV1.NOT_APPLICABLE
                and opportunity.side.value == opportunity.direction.value
                and opportunity.venue == "NOT_APPLICABLE"
                and opportunity.price == "NOT_APPLICABLE"
                and opportunity.witness_kind == "ROUTE_PAIR",
                "ROUTING_DILEMMA_KEY_MISMATCH",
            ),
        ),
        DetectorMeasurementV1("absolute_cost_difference", cost_difference),
        DetectorMeasurementV1("absolute_quantity_difference", quantity_difference),
        DetectorMeasurementV1(
            "absolute_receipt_time_difference_us",
            receipt_difference,
        ),
        DetectorMeasurementV1("route_a_strictly_better_axes", a_better_axes),
        DetectorMeasurementV1("route_a_strictly_worse_axes", a_worse_axes),
    )


VENUE_DETECTOR_HANDLERS_V1: Mapping[str, DetectorHandlerV1] = MappingProxyType(
    {
        "MULTI_VENUE_FRAGMENTATION": detect_multi_venue_fragmentation,
        "ROUTING_DILEMMA": detect_routing_dilemma,
    }
)


__all__ = [
    "VENUE_DETECTOR_HANDLERS_V1",
    "detect_multi_venue_fragmentation",
    "detect_routing_dilemma",
]
