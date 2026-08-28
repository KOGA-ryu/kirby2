"""Preregistered WO31-H full-day profile and qualification policy.

This module is deliberately a policy/validation boundary.  It defines exact integer
arithmetic, loads three byte-canonical TOML manifests, and refuses any manifest that
does not match the policy frozen by WO31-H.  It does not run development,
qualification, holdout, review-selection, or performance workloads.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Final

from kirby2.full_day.models import (
    canonical_json_bytes,
    canonical_sha256,
    validate_strict_json,
)
from kirby2.research.toml_codec import canonical_toml


POLICY_SCALE_PPM: Final = 1_000_000
FULL_DAY_PROFILE_POLICY_VERSION: Final = "FULL_DAY_PROFILE_V1"
FULL_DAY_PERFORMANCE_POLICY_VERSION: Final = "FULL_DAY_PERFORMANCE_V1"
PROFILE_CANDIDATES_MANIFEST_ID: Final = "WO31_H_PROFILE_CANDIDATES_V1"
PROFILE_ENVELOPES_MANIFEST_ID: Final = "WO31_H_PROFILE_ENVELOPES_V1"
PERFORMANCE_THRESHOLDS_MANIFEST_ID: Final = (
    "WO31_H_PERFORMANCE_THRESHOLDS_V1"
)

QUIET_RANGE_PRESSURE: Final = "QUIET_RANGE_PRESSURE"
TREND_PRESSURE: Final = "TREND_PRESSURE"
EVENT_SHOCK_PRESSURE: Final = "EVENT_SHOCK_PRESSURE"
DISORDERLY_OPEN_STABILIZATION_PRESSURE: Final = (
    "DISORDERLY_OPEN_STABILIZATION_PRESSURE"
)
CANDIDATE_IDS: Final = (
    QUIET_RANGE_PRESSURE,
    TREND_PRESSURE,
    EVENT_SHOCK_PRESSURE,
    DISORDERLY_OPEN_STABILIZATION_PRESSURE,
)
DISPLAY_LABELS: Final = {
    QUIET_RANGE_PRESSURE: "QUIET_RANGE_DAY",
    TREND_PRESSURE: "TREND_DAY",
    EVENT_SHOCK_PRESSURE: "EVENT_SHOCK_AND_RECOVERY",
    DISORDERLY_OPEN_STABILIZATION_PRESSURE: (
        "DISORDERLY_OPEN_STABILIZING"
    ),
}

DEVELOPMENT_ROOTS: Final = tuple(range(3_101_000, 3_101_004))
QUALIFICATION_ROOTS: Final = tuple(range(3_102_000, 3_102_008))
HOLDOUT_ROOTS: Final = tuple(range(3_103_000, 3_103_004))
REVIEW_SELECTION_ROOT: Final = 3_199_001
REVIEW_SELECTION_LABEL: Final = "full_day/review"

BASE_PLAN_ID: Final = "WO31_F_COMPLETE_DAY_AUDIT_V1"
BASE_PLAN_VERSION: Final = 1
BASE_PLAN_SHA256: Final = (
    "24ebad3b86eebdd0db1ff8dea33fbf9f4d57ee92478354944de9c0e48fefb860"
)
COMPOSITION_PROFILE_ID: Final = (
    "SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1"
)
COMPOSITION_PROFILE_VERSION: Final = 1
COMPOSITION_PROFILE_SHA256: Final = (
    "c32ba6793b9598221e625a210bfc29924f8f3b24ff7245ca95019554aa93dd08"
)
COMPOSITION_MATRIX_SHA256: Final = COMPOSITION_PROFILE_SHA256

INSUFFICIENT_EVIDENCE: Final = "INSUFFICIENT_EVIDENCE"
NOT_APPLICABLE: Final = "NOT_APPLICABLE"

# Filled with the self-digests produced by the exact bodies below.  They are pinned
# in source as well as recorded in TOML so recomputing a modified manifest's own
# digest cannot make it acceptable.
PROFILE_CANDIDATES_SEMANTIC_SHA256: Final = (
    "f3ab33d06a65fa4488e337460893d5ccae2d987bb3f6472da1b4f661b410237e"
)
PROFILE_CANDIDATES_MANIFEST_SHA256: Final = (
    "850f69efafa92b32d4a2f11978e0daf941a60fa0851b5ec610d815c3ed20aaf7"
)
PROFILE_ENVELOPES_SEMANTIC_SHA256: Final = (
    "2f6f5a3287bac4a23c395af124e0bdf8eba1aab1f0afd2d8606ab3ce8446cada"
)
PROFILE_ENVELOPES_MANIFEST_SHA256: Final = (
    "35daf755163585fd9201ca920283625d18a60d03924f434ef74859e98eacef90"
)
PERFORMANCE_THRESHOLDS_SEMANTIC_SHA256: Final = (
    "83ab02d8aeefce42a2fc97a36aeafe29d2165372a18a0c5834321cb6752869bd"
)
PERFORMANCE_THRESHOLDS_MANIFEST_SHA256: Final = (
    "0fd8f61ddb51376e1609f8bac1303a29e370603bf88efb8f577b765adca3cd9f"
)


class InsufficientEvidenceError(ValueError):
    """Raised when an exact V1 formula has its declared missing denominator."""


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact integer")
    return value


def round_div_even(numerator: int, denominator: int) -> int:
    """Divide using the normative signed ties-to-even rule."""

    numerator = _exact_int(numerator, "numerator")
    denominator = _exact_int(denominator, "denominator")
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    quotient, remainder = divmod(abs(numerator), denominator)
    if 2 * remainder > denominator or (
        2 * remainder == denominator and quotient % 2 == 1
    ):
        quotient += 1
    return -quotient if numerator < 0 else quotient


def round_div_ceiling(numerator: int, denominator: int) -> int:
    numerator = _exact_int(numerator, "numerator")
    denominator = _exact_int(denominator, "denominator")
    if numerator < 0 or denominator <= 0:
        raise ValueError("ceiling division requires numerator >= 0 and denominator > 0")
    return (numerator + denominator - 1) // denominator


def mul_ppm(left: int, right: int) -> int:
    return round_div_even(
        _exact_int(left, "left") * _exact_int(right, "right"),
        POLICY_SCALE_PPM,
    )


def ratio_ppm(numerator: int, denominator: int) -> int:
    numerator = _exact_int(numerator, "numerator")
    denominator = _exact_int(denominator, "denominator")
    if denominator == 0:
        raise InsufficientEvidenceError(INSUFFICIENT_EVIDENCE)
    if denominator < 0:
        raise ValueError("ratio denominator cannot be negative")
    return round_div_even(numerator * POLICY_SCALE_PPM, denominator)


def share_ppm(numerator: int, denominator: int) -> int:
    return max(-POLICY_SCALE_PPM, min(POLICY_SCALE_PPM, ratio_ppm(numerator, denominator)))


def unsigned_share_ppm(numerator: int, denominator: int) -> int:
    return max(0, min(POLICY_SCALE_PPM, ratio_ppm(numerator, denominator)))


def nearest_rank(values: Sequence[int], percentile_ppm: int) -> int:
    percentile_ppm = _exact_int(percentile_ppm, "percentile_ppm")
    if not 0 <= percentile_ppm <= POLICY_SCALE_PPM:
        raise ValueError("percentile_ppm must lie in [0, 1000000]")
    if not values:
        raise InsufficientEvidenceError(INSUFFICIENT_EVIDENCE)
    ordered = sorted(_exact_int(value, "percentile value") for value in values)
    if percentile_ppm == 0:
        return ordered[0]
    rank = max(
        1,
        round_div_ceiling(percentile_ppm * len(ordered), POLICY_SCALE_PPM),
    )
    return ordered[rank - 1]


def time_weighted_nearest_rank(
    rows: Sequence[tuple[int, int, str]], percentile_ppm: int
) -> int:
    percentile_ppm = _exact_int(percentile_ppm, "percentile_ppm")
    if not 0 <= percentile_ppm <= POLICY_SCALE_PPM:
        raise ValueError("percentile_ppm must lie in [0, 1000000]")
    if not rows:
        raise InsufficientEvidenceError(INSUFFICIENT_EVIDENCE)
    checked: list[tuple[int, int, str]] = []
    for value, duration_us, segment_order in rows:
        value = _exact_int(value, "weighted value")
        duration_us = _exact_int(duration_us, "duration_us")
        if duration_us <= 0:
            raise InsufficientEvidenceError(INSUFFICIENT_EVIDENCE)
        if type(segment_order) is not str or not segment_order:
            raise TypeError("canonical segment order must be a nonempty string")
        if unicodedata.normalize("NFC", segment_order) != segment_order:
            raise ValueError("canonical segment order must be NFC text")
        checked.append((value, duration_us, segment_order))
    ordered = sorted(checked, key=lambda row: (row[0], row[2]))
    if percentile_ppm == 0:
        return ordered[0][0]
    total_duration = sum(row[1] for row in ordered)
    if total_duration <= 0:
        raise InsufficientEvidenceError(INSUFFICIENT_EVIDENCE)
    target = max(
        1,
        round_div_ceiling(percentile_ppm * total_duration, POLICY_SCALE_PPM),
    )
    cumulative = 0
    for value, duration_us, _order in ordered:
        cumulative += duration_us
        if cumulative >= target:
            return value
    raise AssertionError("positive weighted duration did not reach its target")


def median(values: Sequence[int]) -> int:
    return nearest_rank(values, 500_000)


def median_absolute_deviation(values: Sequence[int]) -> int:
    center = median(values)
    return median([abs(value - center) for value in values])


def policy_tie_digest(
    policy_version: str,
    context_id: str,
    selection_root_seed: int,
    semantic_digest: str,
) -> bytes:
    for value, name in (
        (policy_version, "policy_version"),
        (context_id, "context_id"),
    ):
        if type(value) is not str or not value:
            raise TypeError(f"{name} must be a nonempty string")
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"{name} must be NFC text")
    selection_root_seed = _exact_int(selection_root_seed, "selection_root_seed")
    if not 0 <= selection_root_seed < 2**64:
        raise ValueError("selection_root_seed must fit unsigned u64")
    if (
        type(semantic_digest) is not str
        or len(semantic_digest) != 64
        or any(character not in "0123456789abcdef" for character in semantic_digest)
    ):
        raise ValueError("semantic_digest must be 64 lowercase hexadecimal characters")
    return hashlib.sha256(
        b"KIRBY2_POLICY_TIE_V1\0"
        + policy_version.encode("utf-8")
        + b"\0"
        + context_id.encode("utf-8")
        + b"\0"
        + selection_root_seed.to_bytes(8, "big")
        + b"\0"
        + bytes.fromhex(semantic_digest)
    ).digest()


def derive_labeled_seed(root_seed: int, policy_version: str, label: str) -> int:
    root_seed = _exact_int(root_seed, "root_seed")
    if not 0 <= root_seed <= 2**63 - 1:
        raise ValueError("root_seed must lie in [0, 2**63-1]")
    for value, name in ((policy_version, "policy_version"), (label, "label")):
        if type(value) is not str or not value:
            raise TypeError(f"{name} must be a nonempty string")
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError(f"{name} must be NFC text")
    digest = hashlib.sha256(
        root_seed.to_bytes(8, "big")
        + b"\0"
        + policy_version.encode("utf-8")
        + b"\0"
        + label.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def normalized_time_ppm(
    simulation_time_us: int,
    continuous_start_us: int,
    continuous_duration_us: int,
) -> int:
    simulation_time_us = _exact_int(simulation_time_us, "simulation_time_us")
    continuous_start_us = _exact_int(continuous_start_us, "continuous_start_us")
    continuous_duration_us = _exact_int(
        continuous_duration_us, "continuous_duration_us"
    )
    if continuous_duration_us <= 0:
        raise ValueError("continuous_duration_us must be positive")
    return (
        (simulation_time_us - continuous_start_us) * POLICY_SCALE_PPM
        // continuous_duration_us
    )


def normalized_boundary_time_us(
    boundary_ppm: int,
    continuous_start_us: int,
    continuous_duration_us: int,
) -> int:
    boundary_ppm = _exact_int(boundary_ppm, "boundary_ppm")
    continuous_start_us = _exact_int(continuous_start_us, "continuous_start_us")
    continuous_duration_us = _exact_int(
        continuous_duration_us, "continuous_duration_us"
    )
    if not 0 <= boundary_ppm <= POLICY_SCALE_PPM:
        raise ValueError("boundary_ppm must lie in [0, 1000000]")
    if continuous_duration_us <= 0:
        raise ValueError("continuous_duration_us must be positive")
    return continuous_start_us + round_div_ceiling(
        boundary_ppm * continuous_duration_us,
        POLICY_SCALE_PPM,
    )


def apply_multiplier_chain(base_value: int, multipliers_ppm: Iterable[int]) -> int:
    value = _exact_int(base_value, "base_value")
    for multiplier in multipliers_ppm:
        value = mul_ppm(value, _exact_int(multiplier, "multiplier_ppm"))
    return value


def _interval(
    start_ppm: int,
    end_ppm: int,
    volume_ppm: int,
    liquidity_ppm: int,
    volatility_ppm: int,
    aggressive_mode: str,
    aggressive_primary_ppm: int,
    aggressive_other_ppm: int,
    cancel_ppm: int,
) -> dict[str, object]:
    return {
        "aggressive_mode": aggressive_mode,
        "aggressive_other_ppm": aggressive_other_ppm,
        "aggressive_primary_ppm": aggressive_primary_ppm,
        "cancel_ppm": cancel_ppm,
        "end_ppm": end_ppm,
        "liquidity_ppm": liquidity_ppm,
        "start_ppm": start_ppm,
        "volatility_ppm": volatility_ppm,
        "volume_ppm": volume_ppm,
    }


def _candidate_policy_body() -> dict[str, object]:
    symmetric_none = {
        "active_end_ppm": NOT_APPLICABLE,
        "active_start_ppm": NOT_APPLICABLE,
        "publication_ppm": NOT_APPLICABLE,
        "side_label": NOT_APPLICABLE,
        "side_rule": NOT_APPLICABLE,
    }
    return _candidate_policy_body_impl(symmetric_none)


def _envelope_policy_body() -> dict[str, object]:
    return {
        "aggregation": {
            "common_order": "ASCENDING_SEMANTIC_ID_THEN_ROOT_THEN_EVENT_OR_ATTEMPT_ORDINAL",
            "disorderly_open": (
                "POOL_OPEN_AND_MIDDAY_SPREAD_DISTRIBUTIONS;POOL_CANCEL_COUNTS_AND_ELIGIBLE_DURATIONS;"
                "RATIO_PPM(open_cancel_count*mid_eligible_us,mid_cancel_count*open_eligible_us);"
                "POOL_FINAL_OCCUPANCY_AND_SPREAD"
            ),
            "event": (
                "POOL_SHOCK_AND_PRE_AGGRESSIVE_SHARES_AND_DURATIONS;P50_PER_ROOT_RANGE_RATIO;"
                "POOL_RECOVERY_OCCUPANCY;COMPARE_POOLED_RECOVERY_AND_SHOCK_SPREAD_MEDIANS"
            ),
            "forbidden_implicit_reductions": "NO_MEAN_OF_RATIOS_NO_WORST_ROOT_NO_UNDECLARED_REDUCTION",
            "maximum": "MAXIMUM_OF_PER_ROOT_MAXIMA",
            "nonadditive_range_ratio": "COMPUTE_PER_ROOT_THEN_REDUCE_P50",
            "occupancy": "POOL_OCCUPIED_DURATION_OVER_ELIGIBLE_DURATION",
            "paired_distributions": "POOL_SEPARATELY_BEFORE_COMPARISON",
            "quiet": "POOL_SPREAD_DURATIONS_AND_AGGRESSIVE_NUMERATORS_DENOMINATORS;MAXIMUM_DISPLACEMENT_ACROSS_ROOTS",
            "ratio_share": "POOL_RAW_INTEGER_NUMERATORS_AND_DENOMINATORS_THEN_DIVIDE_ONCE",
            "time_weighted_distribution": "POOL_POSITIVE_DURATION_SEGMENTS_IN_ASCENDING_ROOT_TIME_ORDER",
            "trend": "POOL_FAVORED_AND_TOTAL_AGGRESSIVE_SHARES;PER_ROOT_SIGNED_DISPLACEMENT_REDUCED_P50;COUNT_STRICTLY_POSITIVE_ROOTS",
        },
        "behavioral_gates": {
            DISORDERLY_OPEN_STABILIZATION_PRESSURE: {
                "behavioral_miss": "WARNING_AND_AUTOMATED_READY_FALSE",
                "final_eighty_percent_median_spread_max_ticks": 8,
                "final_eighty_percent_two_sided_occupancy_min_ppm": 950_000,
                "first_eight_percent_cancel_rate_over_midday_min_ppm": 1_500_000,
                "first_eight_percent_median_spread_vs_midday": "GREATER_THAN_OR_EQUAL",
            },
            EVENT_SHOCK_PRESSURE: {
                "behavioral_miss": "WARNING_AND_AUTOMATED_READY_FALSE",
                "recovery_median_spread_vs_shock": "LESS_THAN_OR_EQUAL",
                "recovery_two_sided_occupancy_min_ppm": 900_000,
                "shock_over_pre_aggressive_volume_min_ppm": 1_500_000,
                "shock_over_pre_range_min_ppm": 1_200_000,
            },
            QUIET_RANGE_PRESSURE: {
                "absolute_aggressive_volume_imbalance_max_ppm": 250_000,
                "behavioral_miss": "WARNING_AND_AUTOMATED_READY_FALSE",
                "maximum_absolute_trade_displacement_ticks": 80,
                "spread_p50_max_ticks": 4,
                "spread_p95_max_ticks": 8,
            },
            TREND_PRESSURE: {
                "behavioral_miss": "WARNING_AND_AUTOMATED_READY_FALSE",
                "favored_aggressive_volume_share_min_ppm": 600_000,
                "holdout_positive_root_count_min": 3,
                "median_favored_signed_displacement_min_ticks": 2,
                "qualification_positive_root_count_min": 6,
            },
        },
        "behavioral_windows": {
            DISORDERLY_OPEN_STABILIZATION_PRESSURE: {
                "final_eighty_percent": [200_000, POLICY_SCALE_PPM],
                "first_eight_percent": [0, 80_000],
                "midday": [350_000, 650_000],
            },
            EVENT_SHOCK_PRESSURE: {
                "halt_rule": "AFFECTED_RATIO_IS_INSUFFICIENT_EVIDENCE_NO_WINDOW_RESIZE",
                "pre_event": [350_000, 450_000],
                "pre_shock_equal_duration_before_halt_exclusion": True,
                "recovery": [550_000, 750_000],
                "shock": [450_000, 550_000],
            },
            QUIET_RANGE_PRESSURE: {"continuous": [0, POLICY_SCALE_PPM]},
            TREND_PRESSURE: {
                "continuous": [0, POLICY_SCALE_PPM],
                "first_last_requires_trade_count": 2,
            },
        },
        "execution_scope": {
            "automated_readiness": "NOT_EXERCISED",
            "behavioral_qualification": "NOT_EXERCISED",
            "holdout": "NOT_EXERCISED",
            "human_acceptance": "PENDING",
            "qualification": "NOT_EXERCISED",
            "review_selection": "NOT_EXERCISED",
        },
        "formulas": {
            "aggressive_imbalance": "abs(share_ppm(buy_shares-sell_shares,buy_shares+sell_shares))",
            "cancel_rate": "cancellation_event_count/eligible_nonhalt_duration_us",
            "disorderly_cancel_rate_ratio": "ratio_ppm(open_cancel_count*mid_eligible_us,mid_cancel_count*open_eligible_us)",
            "disorderly_cancel_ratio_missing": "INSUFFICIENT_IF_EITHER_ELIGIBLE_DURATION_ZERO_OR_MID_CANCEL_COUNT_ZERO;ZERO_OPEN_CANCEL_WITH_VALID_DENOMINATOR_IS_ZERO",
            "event_range_basis": "SPREAD_RANGE_WHEN_BOTH_PERIODS_HAVE_QUOTES_ELSE_TRADE_RANGE",
            "favored_side_share": "unsigned_share_ppm(favored_shares,total_aggressive_shares)",
            "maximum_displacement": "max(abs(trade_price_ticks-first_continuous_trade_ticks))",
            "median": "P50_NEAREST_RANK",
            "missing_component": INSUFFICIENT_EVIDENCE,
            "nonadditive_ratio_reduction": "P50_PER_ROOT",
            "occupancy": "unsigned_share_ppm(sum(occupied_us),sum(eligible_us))",
            "occupancy_eligible_duration": "NAMED_CONTINUOUS_WINDOW_INTERSECT_CONTINUOUS_SESSION_NOT_HALTED",
            "occupancy_occupied_duration": "POSITIVE_DURATION_SEGMENTS_WITH_BOTH_BEST_BID_AND_BEST_ASK",
            "percentile": "NEAREST_RANK_V1",
            "ratio": "UNBOUNDED_RATIO_PPM_V1",
            "rounding": "ROUND_DIV_EVEN_V1",
            "spread_distribution": "TWO_SIDED_QUOTE_DURATION_SEGMENTS_ONLY",
            "time_weighted_percentile": "TIME_WEIGHTED_NEAREST_RANK_V1",
            "trade_ranges": "ALL_TRADES_IN_NAMED_HALF_OPEN_WINDOW",
            "trend_buy_displacement": "last_trade_ticks-first_trade_ticks",
            "trend_sell_displacement": "first_trade_ticks-last_trade_ticks",
            "zero_denominator": INSUFFICIENT_EVIDENCE,
        },
        "manifest_id": PROFILE_ENVELOPES_MANIFEST_ID,
        "manifest_version": 1,
        "metric_contract": {
            "fixed_point_scale": POLICY_SCALE_PPM,
            "formula_version": "FULL_DAY_QUALIFICATION_FORMULAS_V1",
            "integer_units": [
                "ATTEMPT_ORDINAL",
                "BASIS_VALUE",
                "BYTES",
                "EVENT_COUNT",
                "MICROSECONDS",
                "MILLITICKS",
                "SHARES",
                "TICKS",
            ],
            "mad": "P50(abs(x-P50(x)))",
            "metric_version": "FULL_DAY_PROFILE_METRICS_V1",
            "nearest_rank": "rank=max(1,ceil(p_ppm*n/S));p=0_returns_minimum",
            "not_applicable_reduction": "OMIT_FROM_WEIGHTED_NUMERATOR_AND_DENOMINATOR",
            "percentile_domain_ppm": [0, POLICY_SCALE_PPM],
            "time_weighted_nearest_rank": "SORT_VALUE_THEN_CANONICAL_SEGMENT_ORDER;TARGET=max(1,ceil(p_ppm*D/S));p=0_returns_minimum",
        },
        "policy_version": FULL_DAY_PROFILE_POLICY_VERSION,
        "profile_candidates_manifest_sha256": _build_document(
            _candidate_policy_body()
        )["manifest_sha256"],
        "review_policy": {
            "blind_fields": [
                "CANDIDATE_ID",
                "EVENT_TYPE",
                "FUTURE_OUTCOME",
                "PRESSURE_CONTROLS",
                "ROOT_SEED",
                "TRUTH",
            ],
            "candidate_stratum_applicability": {
                DISORDERLY_OPEN_STABILIZATION_PRESSURE: {
                    "event_post_event": NOT_APPLICABLE
                },
                EVENT_SHOCK_PRESSURE: {"event_post_event": "APPLICABLE"},
                QUIET_RANGE_PRESSURE: {"event_post_event": NOT_APPLICABLE},
                TREND_PRESSURE: {"event_post_event": NOT_APPLICABLE},
            },
            "context_template": "WO31_REVIEW/<candidate_id>/<stratum>/<run_digest>/<start_us>",
            "eligible_start_step_us": 1_000_000,
            "event_window_offsets_us": [-120_000_000, 480_000_000],
            "intersection_over_union_max_ppm": 500_000,
            "packet_order": "DISPLAY_LABEL_ORDER_THEN_STRATUM_ORDER_THEN_SELECTION_DIGEST",
            "retained_fields": ["OBSERVABLE_FEED", "PHASE_RELATIVE_TIME"],
            "review_label": REVIEW_SELECTION_LABEL,
            "reviewer_rubric": {
                "criteria": [
                    "MARKET_DYNAMICS_COHERENCE",
                    "VISIBLE_LIQUIDITY_CONTINUITY",
                    "EVENT_RESPONSE_PLAUSIBILITY_WHEN_APPLICABLE",
                    "ARTIFACT_OR_DATA_QUALITY",
                ],
                "outcomes": [
                    "PLAUSIBLE",
                    "QUESTIONABLE",
                    "IMPLAUSIBLE",
                    INSUFFICIENT_EVIDENCE,
                ],
                "rubric_cannot_set_human_acceptance": True,
            },
            "selected_window_manifest_fields": [
                "schema_version",
                "selection_policy_version",
                "profile_candidates_manifest_sha256",
                "profile_envelopes_manifest_sha256",
                "run_digest",
                "stratum",
                "start_us",
                "end_us",
                "selection_context",
                "selection_digest",
                "observable_window_sha256",
                "blind_fields",
                "shortfall_status",
            ],
            "selected_window_manifest_schema": "FULL_DAY_SELECTED_WINDOW_MANIFEST_V1",
            "selection_algorithm": "EXHAUSTIVE_ENUMERATE_FILTER_RANK_THEN_GREEDY_IOU_V1",
            "selection_digest_policy": "COMMON_POLICY_TIE_DIGEST_V1",
            "selection_root": REVIEW_SELECTION_ROOT,
            "shortfall_rule": "REPORT_WITHOUT_REPLACEMENT",
            "strata": {
                "close": "[900000,S)_PLUS_CLOSING_AUCTION",
                "event_post_event": "[event_time-120000000,event_time+480000000)",
                "midday": "[350000,600000)",
                "opening": "OPENING_AUCTION_PLUS_[0,100000)",
                "ordinary_afternoon": "[600000,900000)_EXCLUDING_EVENT_POST_EVENT",
                "ordinary_morning": "[100000,350000)_EXCLUDING_EVENT_POST_EVENT",
            },
            "stratum_order": [
                "opening",
                "ordinary_morning",
                "midday",
                "event_post_event",
                "ordinary_afternoon",
                "close",
            ],
            "universe": "EVERY_SESSION_START_PLUS_K_SECONDS_ASCENDING",
            "universe_boundary_rule": "ENTIRE_HALF_OPEN_WINDOW_IN_EXACTLY_ONE_STRATUM;NO_HALT_OR_SESSION_BOUNDARY_IN_OPEN_INTERIOR;AUCTION_OR_CONTINUOUS_ENDPOINT_MAY_BE_WINDOW_ENDPOINT",
            "window_count_per_applicable_stratum": 2,
            "window_duration_us": 60_000_000,
        },
        "schema_version": 1,
        "semantic_version": 1,
        "stopping_and_disposition": {
            "behavioral_failure_semantics": "WARNING_NOT_ENGINE_DEFECT_OR_REALISM_CONCLUSION",
            "behavioral_pass": "EVERY_AGGREGATE_RULE_PASSES",
            "early_stop": "FORBIDDEN",
            "holdout_reduction": "IDENTICAL_TO_QUALIFICATION",
            "root_substitution": "FORBIDDEN",
            "universal_miss": "FAIL",
        },
        "universal_gates": {
            "continuous_two_sided_quote_occupancy_min_ppm": 950_000,
            "exact_replay": "PASS",
            "forced_trade_operations_max": 0,
            "maximum_continuous_spread_ticks": 20,
            "maximum_nonhalt_empty_side_episode_us": 5_000_000,
            "minimum_trade_count": 100,
            "runtime_invariants": "PASS",
            "safety_abort_count_max": 0,
            "target_price_operations_max": 0,
            "universal_miss": "FAIL",
        },
    }


def _performance_policy_body() -> dict[str, object]:
    gib = 1024**3
    mib = 1024**2
    return {
        "aggregation": {
            "evaluation_order": "PASS_FIRST_ELSE_WARNING_ELSE_FAIL",
            "inclusive_limits": True,
            "result_rule": "FAIL_IF_ANY_FAIL_ELSE_WARNING_IF_ANY_WARNING_ELSE_PASS",
            "statuses_eligible": ["PASS", "WARNING", "FAIL"],
            "statuses_ineligible": ["UNSUPPORTED", "NOT_RUN"],
        },
        "execution_scope": {
            "deterministic_correctness": "NOT_EXERCISED",
            "performance_measurement": "NOT_EXERCISED",
            "platform_qualification": "NOT_EXERCISED",
            "source_card_action": "VALIDATE_MANIFEST_ONLY",
        },
        "hard_aborts": {
            "abort_outcome": "EMIT_DETERMINISTIC_FAILURE_ARTIFACT_AND_REFUSE_UNCOMMITTED_OPERATION",
            "complete_staged_run_bytes": 12 * gib,
            "deterministic_operation_trigger": "BEFORE_ACCEPTING_OPERATION_THAT_WOULD_EXCEED_LIMIT",
            "generation_elapsed_ns": 900_000_000_000,
            "maximum_canonical_checkpoint_bytes": 512 * mib,
            "outer_event_count": 5_000_000,
            "operational_abort_data_in_semantic_identity": False,
            "operational_measurement_trigger": "AFTER_ELAPSED_OR_RSS_BECOMES_STRICTLY_GREATER_THAN_LIMIT",
            "peak_rss_bytes": 8 * gib,
            "pending_item_count": 250_000,
            "replay_elapsed_ns": 600_000_000_000,
            "timestamp_distinct_microsteps": 128,
            "timestamp_emitted_event_count": 100_000,
        },
        "manifest_id": PERFORMANCE_THRESHOLDS_MANIFEST_ID,
        "manifest_version": 1,
        "measurement": {
            "artifact_size": "EXACT_ARTIFACT_BYTE_LENGTH",
            "generation_elapsed_clock": "time.perf_counter_ns",
            "peak_rss": "MAXIMUM_FRESH_CHILD_RESOURCE_GETRUSAGE_RUSAGE_SELF_RU_MAXRSS",
            "peak_rss_normalization": "DARWIN_RU_MAXRSS_VALUE_IS_BYTES",
            "replay_count_per_measured_artifact": 1,
            "rss_and_timing_in_semantic_identity": False,
            "throughput": "round_div_even(sum(outer_event_count)*1000000000,sum(generation_elapsed_ns))",
        },
        "platform_predicate": {
            "free_governed_store_bytes_min": 12 * gib,
            "logical_cpu_count_min": 4,
            "machine": "arm64",
            "physical_memory_bytes_min": 8 * gib,
            "python_implementation": "CPython",
            "python_major": 3,
            "python_minor": 14,
            "record_exact_fields": [
                "SYSTEM",
                "MACHINE",
                "PYTHON_IMPLEMENTATION",
                "PYTHON_MAJOR_MINOR_PATCH",
                "PYTHON_RUNTIME",
                "LOGICAL_CPU_COUNT",
                "PHYSICAL_MEMORY_BYTES",
                "FREE_GOVERNED_STORE_BYTES_BEFORE_WARMUP",
                "RU_MAXRSS_NORMALIZATION_RULE",
            ],
            "system": "Darwin",
            "unsupported_rule": "RAW_MEASUREMENTS_PLUS_UNSUPPORTED_NEVER_THRESHOLD_PASS",
        },
        "policy_version": FULL_DAY_PERFORMANCE_POLICY_VERSION,
        "profile_candidates_manifest_sha256": _build_document(
            _candidate_policy_body()
        )["manifest_sha256"],
        "profile_envelopes_manifest_sha256": _build_document(
            _envelope_policy_body()
        )["manifest_sha256"],
        "schema_version": 1,
        "semantic_version": 1,
        "thresholds": {
            "complete_run_bytes": {
                "direction": "MAXIMUM",
                "fail_beyond": 12 * gib,
                "pass_inclusive": 4 * gib,
                "unit": "BYTES",
                "warning_inclusive": 12 * gib,
            },
            "generation_p50_elapsed_ns": {
                "direction": "MAXIMUM",
                "fail_beyond": 900_000_000_000,
                "pass_inclusive": 300_000_000_000,
                "unit": "NANOSECONDS",
                "warning_inclusive": 900_000_000_000,
            },
            "generation_throughput_events_per_second": {
                "direction": "MINIMUM",
                "fail_below": 100,
                "pass_inclusive": 500,
                "unit": "EVENTS_PER_SECOND",
                "warning_inclusive": 100,
            },
            "largest_checkpoint_bytes": {
                "direction": "MAXIMUM",
                "fail_beyond": 512 * mib,
                "pass_inclusive": 256 * mib,
                "unit": "BYTES",
                "warning_inclusive": 512 * mib,
            },
            "peak_rss_bytes": {
                "direction": "MAXIMUM",
                "fail_beyond": 8 * gib,
                "pass_inclusive": 4 * gib,
                "unit": "BYTES",
                "warning_inclusive": 8 * gib,
            },
            "replay_p50_elapsed_ns": {
                "direction": "MAXIMUM",
                "fail_beyond": 600_000_000_000,
                "pass_inclusive": 180_000_000_000,
                "unit": "NANOSECONDS",
                "warning_inclusive": 600_000_000_000,
            },
        },
        "workload": {
            "candidate_id": QUIET_RANGE_PRESSURE,
            "checkpoint_coincident_cut_rule": "EMIT_ONE_CHECKPOINT",
            "checkpoint_interval_continuous_us": 900_000_000,
            "checkpoint_phase_boundaries": True,
            "checkpoint_start": "INITIALIZATION",
            "composition_matrix_sha256": COMPOSITION_MATRIX_SHA256,
            "composition_profile_id": COMPOSITION_PROFILE_ID,
            "composition_profile_sha256": COMPOSITION_PROFILE_SHA256,
            "composition_profile_version": COMPOSITION_PROFILE_VERSION,
            "generation_order": "SEQUENTIAL_FRESH_AUDIT_OWNED_STORE_ROOTS",
            "measured_generation_count": 3,
            "plan_id": BASE_PLAN_ID,
            "plan_sha256": BASE_PLAN_SHA256,
            "replay_process": "FRESH_PROCESS_PER_MEASURED_ARTIFACT",
            "retry_or_sample_substitution": "FORBIDDEN",
            "root_seed": 3_102_000,
            "warmup_generation_count": 1,
            "workload_id": FULL_DAY_PERFORMANCE_POLICY_VERSION,
        },
    }


def _build_document(body: Mapping[str, object]) -> dict[str, object]:
    validate_strict_json(body)
    semantic_identity = {
        key: value for key, value in body.items() if key != "manifest_version"
    }
    semantic_sha256 = canonical_sha256(semantic_identity)
    manifest_identity = {**dict(body), "semantic_sha256": semantic_sha256}
    manifest_sha256 = canonical_sha256(manifest_identity)
    return {**manifest_identity, "manifest_sha256": manifest_sha256}


def render_profile_candidates_manifest_bytes() -> bytes:
    return canonical_toml(_build_document(_candidate_policy_body())).encode("utf-8")


def render_profile_envelopes_manifest_bytes() -> bytes:
    return canonical_toml(_build_document(_envelope_policy_body())).encode("utf-8")


def render_performance_thresholds_manifest_bytes() -> bytes:
    return canonical_toml(_build_document(_performance_policy_body())).encode("utf-8")


def _payload_copy(payload_json: bytes) -> dict[str, object]:
    payload = json.loads(payload_json.decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("validated manifest payload ceased to be an object")
    return payload


def _load_exact_document(
    raw: bytes,
    *,
    filename: str,
    expected_body: Mapping[str, object],
    pinned_semantic_sha256: str,
    pinned_manifest_sha256: str,
) -> tuple[bytes, bytes]:
    if type(raw) is not bytes:
        raise TypeError(f"{filename} bytes must be exact bytes")
    try:
        text = raw.decode("utf-8")
        payload = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{filename} is not strict UTF-8 TOML") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{filename} root must be a table")
    validate_strict_json(payload)
    canonical = canonical_toml(payload).encode("utf-8")
    if canonical != raw:
        raise ValueError(f"{filename} is not canonical TOML")
    expected = _build_document(expected_body)
    if canonical_json_bytes(payload) != canonical_json_bytes(expected):
        raise ValueError(f"{filename} differs from the frozen WO31-H policy")
    if payload["semantic_sha256"] != pinned_semantic_sha256:
        raise ValueError(f"{filename} semantic digest is not source-pinned")
    if payload["manifest_sha256"] != pinned_manifest_sha256:
        raise ValueError(f"{filename} manifest digest is not source-pinned")
    return canonical_json_bytes(payload), raw


@dataclass(frozen=True, slots=True)
class PressureIntervalV1:
    start_ppm: int
    end_ppm: int
    volume_ppm: int
    liquidity_ppm: int
    volatility_ppm: int
    aggressive_mode: str
    aggressive_primary_ppm: int
    aggressive_other_ppm: int
    cancel_ppm: int

    def __post_init__(self) -> None:
        for name in (
            "start_ppm",
            "end_ppm",
            "volume_ppm",
            "liquidity_ppm",
            "volatility_ppm",
            "aggressive_primary_ppm",
            "aggressive_other_ppm",
            "cancel_ppm",
        ):
            value = _exact_int(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not 0 <= self.start_ppm < self.end_ppm <= POLICY_SCALE_PPM:
            raise ValueError("pressure interval must be a nonempty subset of [0,S)")
        if self.aggressive_mode not in {"SYMMETRIC", "FAVORED_SIDE", "SHOCK_SIDE"}:
            raise ValueError("pressure interval has an unknown aggressive mode")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PressureIntervalV1:
        expected = {
            "aggressive_mode",
            "aggressive_other_ppm",
            "aggressive_primary_ppm",
            "cancel_ppm",
            "end_ppm",
            "liquidity_ppm",
            "start_ppm",
            "volatility_ppm",
            "volume_ppm",
        }
        if set(payload) != expected:
            raise ValueError("pressure interval fields are not exact")
        return cls(
            start_ppm=_exact_int(payload["start_ppm"], "start_ppm"),
            end_ppm=_exact_int(payload["end_ppm"], "end_ppm"),
            volume_ppm=_exact_int(payload["volume_ppm"], "volume_ppm"),
            liquidity_ppm=_exact_int(payload["liquidity_ppm"], "liquidity_ppm"),
            volatility_ppm=_exact_int(payload["volatility_ppm"], "volatility_ppm"),
            aggressive_mode=str(payload["aggressive_mode"]),
            aggressive_primary_ppm=_exact_int(
                payload["aggressive_primary_ppm"], "aggressive_primary_ppm"
            ),
            aggressive_other_ppm=_exact_int(
                payload["aggressive_other_ppm"], "aggressive_other_ppm"
            ),
            cancel_ppm=_exact_int(payload["cancel_ppm"], "cancel_ppm"),
        )


@dataclass(frozen=True, slots=True)
class ProfileCandidateV1:
    candidate_id: str
    display_label: str
    profile_version: int
    intervals: tuple[PressureIntervalV1, ...]
    controls: tuple[tuple[str, int | str], ...]

    def __post_init__(self) -> None:
        if self.candidate_id not in CANDIDATE_IDS:
            raise ValueError("unknown full-day candidate ID")
        if self.display_label != DISPLAY_LABELS[self.candidate_id]:
            raise ValueError("candidate display label does not match its hypothesis")
        if self.profile_version != 1:
            raise ValueError("candidate profile version must be 1")
        if not self.intervals or self.intervals[0].start_ppm != 0:
            raise ValueError("candidate intervals must start at zero")
        if self.intervals[-1].end_ppm != POLICY_SCALE_PPM:
            raise ValueError("candidate intervals must end at S")
        if any(
            previous.end_ppm != current.start_ppm
            for previous, current in zip(self.intervals, self.intervals[1:])
        ):
            raise ValueError("candidate intervals must form a contiguous partition")
        if self.controls != tuple(sorted(self.controls)):
            raise ValueError("candidate controls must use canonical key order")

    def interval_at(self, u_ppm: int) -> PressureIntervalV1:
        u_ppm = _exact_int(u_ppm, "u_ppm")
        if not 0 <= u_ppm < POLICY_SCALE_PPM:
            raise ValueError("u_ppm must lie in [0,S)")
        for interval in self.intervals:
            if interval.start_ppm <= u_ppm < interval.end_ppm:
                return interval
        raise AssertionError("validated candidate partition did not cover u_ppm")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ProfileCandidateV1:
        expected = {
            "candidate_id",
            "controls",
            "display_label",
            "display_label_semantics",
            "intervals",
            "profile_version",
        }
        if set(payload) != expected:
            raise ValueError("profile candidate fields are not exact")
        if payload["display_label_semantics"] != "REQUESTED_HYPOTHESIS_NOT_GUARANTEED_PATH":
            raise ValueError("display label was promoted beyond a requested hypothesis")
        rows = payload["intervals"]
        controls = payload["controls"]
        if type(rows) is not list or not isinstance(controls, Mapping):
            raise TypeError("candidate intervals/controls have invalid containers")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            display_label=str(payload["display_label"]),
            profile_version=_exact_int(payload["profile_version"], "profile_version"),
            intervals=tuple(
                PressureIntervalV1.from_dict(row)
                for row in rows
                if isinstance(row, Mapping)
            ),
            controls=tuple(sorted((str(key), value) for key, value in controls.items())),
        )


@dataclass(frozen=True, slots=True)
class ProfileCandidatesManifestV1:
    candidates: tuple[ProfileCandidateV1, ...]
    semantic_sha256: str
    manifest_sha256: str
    file_sha256: str
    _payload_json: bytes
    _canonical_bytes: bytes

    def __post_init__(self) -> None:
        if tuple(candidate.candidate_id for candidate in self.candidates) != CANDIDATE_IDS:
            raise ValueError("candidate definitions are not in canonical identity order")

    def as_dict(self) -> dict[str, object]:
        return _payload_copy(self._payload_json)

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    def candidate(self, candidate_id: str) -> ProfileCandidateV1:
        for candidate in self.candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        raise KeyError(candidate_id)

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> ProfileCandidatesManifestV1:
        payload_json, canonical = _load_exact_document(
            raw,
            filename="profile_candidates.toml",
            expected_body=_candidate_policy_body(),
            pinned_semantic_sha256=PROFILE_CANDIDATES_SEMANTIC_SHA256,
            pinned_manifest_sha256=PROFILE_CANDIDATES_MANIFEST_SHA256,
        )
        payload = _payload_copy(payload_json)
        candidate_rows = payload["candidates"]
        if not isinstance(candidate_rows, Mapping):
            raise TypeError("profile candidates must be a table")
        return cls(
            candidates=tuple(
                ProfileCandidateV1.from_dict(candidate_rows[candidate_id])
                for candidate_id in CANDIDATE_IDS
                if isinstance(candidate_rows[candidate_id], Mapping)
            ),
            semantic_sha256=str(payload["semantic_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            _payload_json=payload_json,
            _canonical_bytes=canonical,
        )


@dataclass(frozen=True, slots=True)
class ProfileEnvelopesManifestV1:
    semantic_sha256: str
    manifest_sha256: str
    file_sha256: str
    _payload_json: bytes
    _canonical_bytes: bytes

    def as_dict(self) -> dict[str, object]:
        return _payload_copy(self._payload_json)

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> ProfileEnvelopesManifestV1:
        payload_json, canonical = _load_exact_document(
            raw,
            filename="profile_envelopes.toml",
            expected_body=_envelope_policy_body(),
            pinned_semantic_sha256=PROFILE_ENVELOPES_SEMANTIC_SHA256,
            pinned_manifest_sha256=PROFILE_ENVELOPES_MANIFEST_SHA256,
        )
        payload = _payload_copy(payload_json)
        return cls(
            semantic_sha256=str(payload["semantic_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            _payload_json=payload_json,
            _canonical_bytes=canonical,
        )


@dataclass(frozen=True, slots=True)
class PerformanceThresholdsManifestV1:
    semantic_sha256: str
    manifest_sha256: str
    file_sha256: str
    _payload_json: bytes
    _canonical_bytes: bytes

    def as_dict(self) -> dict[str, object]:
        return _payload_copy(self._payload_json)

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    def classify(self, metric_id: str, value: int) -> str:
        value = _exact_int(value, "performance metric value")
        if value < 0:
            raise ValueError("performance metric value cannot be negative")
        thresholds = self.as_dict()["thresholds"]
        if not isinstance(thresholds, Mapping) or metric_id not in thresholds:
            raise KeyError(metric_id)
        row = thresholds[metric_id]
        if not isinstance(row, Mapping):
            raise TypeError("performance threshold row must be a table")
        if row["direction"] == "MAXIMUM":
            if value <= row["pass_inclusive"]:
                return "PASS"
            if value <= row["warning_inclusive"]:
                return "WARNING"
            return "FAIL"
        if row["direction"] == "MINIMUM":
            if value >= row["pass_inclusive"]:
                return "PASS"
            if value >= row["warning_inclusive"]:
                return "WARNING"
            return "FAIL"
        raise ValueError("performance threshold direction is invalid")

    @classmethod
    def from_toml_bytes(cls, raw: bytes) -> PerformanceThresholdsManifestV1:
        payload_json, canonical = _load_exact_document(
            raw,
            filename="performance_thresholds.toml",
            expected_body=_performance_policy_body(),
            pinned_semantic_sha256=PERFORMANCE_THRESHOLDS_SEMANTIC_SHA256,
            pinned_manifest_sha256=PERFORMANCE_THRESHOLDS_MANIFEST_SHA256,
        )
        payload = _payload_copy(payload_json)
        return cls(
            semantic_sha256=str(payload["semantic_sha256"]),
            manifest_sha256=str(payload["manifest_sha256"]),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            _payload_json=payload_json,
            _canonical_bytes=canonical,
        )


@dataclass(frozen=True, slots=True)
class PerformancePlatformFingerprintV1:
    system: str
    machine: str
    python_implementation: str
    python_major: int
    python_minor: int
    python_patch: int
    python_runtime: str
    logical_cpu_count: int
    physical_memory_bytes: int
    free_governed_store_bytes_before_warmup: int
    ru_maxrss_normalization_rule: str

    def __post_init__(self) -> None:
        for name in (
            "system",
            "machine",
            "python_implementation",
            "python_runtime",
            "ru_maxrss_normalization_rule",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise TypeError(f"{name} must be a nonempty string")
            if unicodedata.normalize("NFC", value) != value:
                raise ValueError(f"{name} must be NFC text")
        for name in (
            "python_major",
            "python_minor",
            "python_patch",
            "logical_cpu_count",
            "physical_memory_bytes",
            "free_governed_store_bytes_before_warmup",
        ):
            if _exact_int(getattr(self, name), name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def threshold_eligible(self) -> bool:
        return (
            self.system == "Darwin"
            and self.machine == "arm64"
            and self.python_implementation == "CPython"
            and (self.python_major, self.python_minor) == (3, 14)
            and self.logical_cpu_count >= 4
            and self.physical_memory_bytes >= 8 * 1024**3
            and self.free_governed_store_bytes_before_warmup >= 12 * 1024**3
            and self.ru_maxrss_normalization_rule
            == "DARWIN_RU_MAXRSS_VALUE_IS_BYTES"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "FREE_GOVERNED_STORE_BYTES_BEFORE_WARMUP": (
                self.free_governed_store_bytes_before_warmup
            ),
            "LOGICAL_CPU_COUNT": self.logical_cpu_count,
            "MACHINE": self.machine,
            "PHYSICAL_MEMORY_BYTES": self.physical_memory_bytes,
            "PYTHON_IMPLEMENTATION": self.python_implementation,
            "PYTHON_MAJOR_MINOR_PATCH": [
                self.python_major,
                self.python_minor,
                self.python_patch,
            ],
            "PYTHON_RUNTIME": self.python_runtime,
            "RU_MAXRSS_NORMALIZATION_RULE": self.ru_maxrss_normalization_rule,
            "SYSTEM": self.system,
        }


@dataclass(frozen=True, slots=True)
class FullDayProfileBundleV1:
    candidates: ProfileCandidatesManifestV1
    envelopes: ProfileEnvelopesManifestV1
    performance: PerformanceThresholdsManifestV1

    def __post_init__(self) -> None:
        envelopes = self.envelopes.as_dict()
        performance = self.performance.as_dict()
        if (
            envelopes["profile_candidates_manifest_sha256"]
            != self.candidates.manifest_sha256
        ):
            raise ValueError("envelopes do not bind the candidate manifest")
        if (
            performance["profile_candidates_manifest_sha256"]
            != self.candidates.manifest_sha256
            or performance["profile_envelopes_manifest_sha256"]
            != self.envelopes.manifest_sha256
        ):
            raise ValueError("performance thresholds do not bind both policy manifests")

    @property
    def bundle_sha256(self) -> str:
        return canonical_sha256(
            {
                "performance_manifest_sha256": self.performance.manifest_sha256,
                "profile_candidates_manifest_sha256": self.candidates.manifest_sha256,
                "profile_envelopes_manifest_sha256": self.envelopes.manifest_sha256,
            }
        )


def load_profile_candidates() -> ProfileCandidatesManifestV1:
    raw = files("kirby2.full_day").joinpath("profile_candidates.toml").read_bytes()
    return ProfileCandidatesManifestV1.from_toml_bytes(raw)


def load_profile_envelopes() -> ProfileEnvelopesManifestV1:
    raw = files("kirby2.full_day").joinpath("profile_envelopes.toml").read_bytes()
    return ProfileEnvelopesManifestV1.from_toml_bytes(raw)


def load_performance_thresholds() -> PerformanceThresholdsManifestV1:
    raw = files("kirby2.full_day").joinpath("performance_thresholds.toml").read_bytes()
    return PerformanceThresholdsManifestV1.from_toml_bytes(raw)


def load_full_day_profile_bundle() -> FullDayProfileBundleV1:
    return FullDayProfileBundleV1(
        candidates=load_profile_candidates(),
        envelopes=load_profile_envelopes(),
        performance=load_performance_thresholds(),
    )


def validate_bounded_search_transition_rows(
    rows: Mapping[str, Mapping[str, int]],
) -> None:
    if not isinstance(rows, Mapping) or not rows:
        raise ValueError("bounded-search transition rows must be a nonempty mapping")
    for source_id in sorted(rows):
        destinations = rows[source_id]
        if not isinstance(destinations, Mapping) or not destinations:
            raise ValueError("each local transition row must be a nonempty mapping")
        weights = tuple(
            _exact_int(destinations[destination], "transition weight")
            for destination in sorted(destinations)
        )
        positive = tuple(weight for weight in weights if weight > 0)
        if len(positive) < 2:
            raise ValueError("each local transition row needs two positive destinations")
        if sum(weights) != POLICY_SCALE_PPM:
            raise ValueError("each local transition row must sum exactly to S")
        if any(weight < 200_001 for weight in weights):
            raise ValueError("each destination weight must be at least 200001")


def aggregate_performance_status(statuses: Sequence[str]) -> str:
    if not statuses:
        raise ValueError("performance aggregation requires at least one status")
    if any(status not in {"PASS", "WARNING", "FAIL"} for status in statuses):
        raise ValueError("only eligible-platform threshold statuses aggregate")
    if "FAIL" in statuses:
        return "FAIL"
    if "WARNING" in statuses:
        return "WARNING"
    return "PASS"


__all__ = [
    "BASE_PLAN_SHA256",
    "CANDIDATE_IDS",
    "COMPOSITION_MATRIX_SHA256",
    "COMPOSITION_PROFILE_ID",
    "COMPOSITION_PROFILE_SHA256",
    "DEVELOPMENT_ROOTS",
    "DISORDERLY_OPEN_STABILIZATION_PRESSURE",
    "DISPLAY_LABELS",
    "EVENT_SHOCK_PRESSURE",
    "FULL_DAY_PERFORMANCE_POLICY_VERSION",
    "FULL_DAY_PROFILE_POLICY_VERSION",
    "FullDayProfileBundleV1",
    "HOLDOUT_ROOTS",
    "INSUFFICIENT_EVIDENCE",
    "InsufficientEvidenceError",
    "NOT_APPLICABLE",
    "PERFORMANCE_THRESHOLDS_MANIFEST_ID",
    "PERFORMANCE_THRESHOLDS_MANIFEST_SHA256",
    "PERFORMANCE_THRESHOLDS_SEMANTIC_SHA256",
    "PerformancePlatformFingerprintV1",
    "POLICY_SCALE_PPM",
    "PROFILE_CANDIDATES_MANIFEST_ID",
    "PROFILE_CANDIDATES_MANIFEST_SHA256",
    "PROFILE_CANDIDATES_SEMANTIC_SHA256",
    "PROFILE_ENVELOPES_MANIFEST_ID",
    "PROFILE_ENVELOPES_MANIFEST_SHA256",
    "PROFILE_ENVELOPES_SEMANTIC_SHA256",
    "PerformanceThresholdsManifestV1",
    "PressureIntervalV1",
    "ProfileCandidateV1",
    "ProfileCandidatesManifestV1",
    "ProfileEnvelopesManifestV1",
    "QUALIFICATION_ROOTS",
    "QUIET_RANGE_PRESSURE",
    "REVIEW_SELECTION_LABEL",
    "REVIEW_SELECTION_ROOT",
    "TREND_PRESSURE",
    "aggregate_performance_status",
    "apply_multiplier_chain",
    "derive_labeled_seed",
    "load_full_day_profile_bundle",
    "load_performance_thresholds",
    "load_profile_candidates",
    "load_profile_envelopes",
    "median",
    "median_absolute_deviation",
    "mul_ppm",
    "nearest_rank",
    "normalized_boundary_time_us",
    "normalized_time_ppm",
    "policy_tie_digest",
    "ratio_ppm",
    "render_performance_thresholds_manifest_bytes",
    "render_profile_candidates_manifest_bytes",
    "render_profile_envelopes_manifest_bytes",
    "round_div_ceiling",
    "round_div_even",
    "share_ppm",
    "time_weighted_nearest_rank",
    "unsigned_share_ppm",
    "validate_bounded_search_transition_rows",
]


def _candidate_policy_body_impl(
    symmetric_none: Mapping[str, object],
) -> dict[str, object]:
    return {
        "arithmetic": {
            "boundary_mapping": (
                "continuous_start_us+floor((b*continuous_duration_us+S-1)/S)"
            ),
            "boundary_mapping_application": "ONCE_BEFORE_INTERVAL_COMPARISON",
            "fixed_point_scale": POLICY_SCALE_PPM,
            "multiplier_operation": "mul_ppm(a,b)=round_div_even(a*b,S)",
            "normalized_time": (
                "floor((t-continuous_start_us)*S/continuous_duration_us)"
            ),
            "rounding": "ROUND_DIV_EVEN_V1",
        },
        "base_plan": {
            "composition_matrix_sha256": COMPOSITION_MATRIX_SHA256,
            "composition_profile_id": COMPOSITION_PROFILE_ID,
            "composition_profile_sha256": COMPOSITION_PROFILE_SHA256,
            "composition_profile_version": COMPOSITION_PROFILE_VERSION,
            "plan_id": BASE_PLAN_ID,
            "plan_sha256": BASE_PLAN_SHA256,
            "plan_version": BASE_PLAN_VERSION,
        },
        "candidate_order": list(CANDIDATE_IDS),
        "candidates": {
            QUIET_RANGE_PRESSURE: {
                "candidate_id": QUIET_RANGE_PRESSURE,
                "controls": dict(symmetric_none),
                "display_label": DISPLAY_LABELS[QUIET_RANGE_PRESSURE],
                "display_label_semantics": "REQUESTED_HYPOTHESIS_NOT_GUARANTEED_PATH",
                "intervals": [
                    _interval(
                        0,
                        POLICY_SCALE_PPM,
                        700_000,
                        1_300_000,
                        650_000,
                        "SYMMETRIC",
                        700_000,
                        700_000,
                        800_000,
                    )
                ],
                "profile_version": 1,
            },
            TREND_PRESSURE: {
                "candidate_id": TREND_PRESSURE,
                "controls": {
                    "active_end_ppm": 800_000,
                    "active_start_ppm": 200_000,
                    "publication_ppm": NOT_APPLICABLE,
                    "side_label": "full_day/TREND_PRESSURE/favored_side",
                    "side_rule": "LABELED_SEED_LOW_BIT_ZERO_BUY_ONE_SELL",
                },
                "display_label": DISPLAY_LABELS[TREND_PRESSURE],
                "display_label_semantics": "REQUESTED_HYPOTHESIS_NOT_GUARANTEED_PATH",
                "intervals": [
                    _interval(0, 150_000, 950_000, 1_000_000, 900_000, "SYMMETRIC", 1_000_000, 1_000_000, 1_000_000),
                    _interval(150_000, 850_000, 1_250_000, 900_000, 1_100_000, "FAVORED_SIDE", 1_600_000, 700_000, 1_100_000),
                    _interval(850_000, POLICY_SCALE_PPM, 1_000_000, 1_000_000, 900_000, "FAVORED_SIDE", 1_200_000, 850_000, 900_000),
                ],
                "profile_version": 1,
            },
            EVENT_SHOCK_PRESSURE: {
                "candidate_id": EVENT_SHOCK_PRESSURE,
                "controls": {
                    "active_end_ppm": 550_000,
                    "active_start_ppm": 450_000,
                    "publication_ppm": 450_000,
                    "side_label": "full_day/EVENT_SHOCK_PRESSURE/shock_side",
                    "side_rule": "LABELED_SEED_LOW_BIT_ZERO_BUY_ONE_SELL",
                },
                "display_label": DISPLAY_LABELS[EVENT_SHOCK_PRESSURE],
                "display_label_semantics": "REQUESTED_HYPOTHESIS_NOT_GUARANTEED_PATH",
                "intervals": [
                    _interval(0, 450_000, 1_000_000, 1_000_000, 1_000_000, "SYMMETRIC", 1_000_000, 1_000_000, 1_000_000),
                    _interval(450_000, 550_000, 2_200_000, 550_000, 2_500_000, "SHOCK_SIDE", 1_800_000, 800_000, 1_800_000),
                    _interval(550_000, 750_000, 1_300_000, 850_000, 1_400_000, "SHOCK_SIDE", 1_200_000, 900_000, 1_200_000),
                    _interval(750_000, POLICY_SCALE_PPM, 1_000_000, 1_000_000, 1_000_000, "SYMMETRIC", 1_000_000, 1_000_000, 1_000_000),
                ],
                "profile_version": 1,
            },
            DISORDERLY_OPEN_STABILIZATION_PRESSURE: {
                "candidate_id": DISORDERLY_OPEN_STABILIZATION_PRESSURE,
                "controls": dict(symmetric_none),
                "display_label": DISPLAY_LABELS[DISORDERLY_OPEN_STABILIZATION_PRESSURE],
                "display_label_semantics": "REQUESTED_HYPOTHESIS_NOT_GUARANTEED_PATH",
                "intervals": [
                    _interval(0, 80_000, 2_200_000, 550_000, 2_200_000, "SYMMETRIC", 1_600_000, 1_600_000, 2_000_000),
                    _interval(80_000, 200_000, 1_500_000, 800_000, 1_500_000, "SYMMETRIC", 1_250_000, 1_250_000, 1_300_000),
                    _interval(200_000, POLICY_SCALE_PPM, 1_000_000, 1_000_000, 1_000_000, "SYMMETRIC", 1_000_000, 1_000_000, 1_000_000),
                ],
                "profile_version": 1,
            },
        },
        "execution_scope": {
            "action": "VALIDATE_MANIFESTS_ONLY",
            "automated_readiness": "NOT_EXERCISED",
            "development_evidence": "NOT_EXECUTED",
            "holdout": "NOT_EXERCISED",
            "human_acceptance": "PENDING",
            "qualification": "NOT_EXERCISED",
        },
        "manifest_id": PROFILE_CANDIDATES_MANIFEST_ID,
        "manifest_version": 1,
        "policy_version": FULL_DAY_PROFILE_POLICY_VERSION,
        "scaling_rules": {
            "aggressive_and_cancel_modify_rates_only": True,
            "controls_affect_normal_rates_and_participants_only": True,
            "controls_never_set_price_or_force_trade": True,
            "derived_child_quantities_scaled_again": False,
            "integer_quantity_rule": "CLAMP_TO_ONE_ONLY_IF_BASE_POSITIVE_AND_REQUIRED",
            "left_to_right_round_after_each_multiplier": True,
            "no_other_field_changes": True,
            "price_spread_target_or_forced_trade_controls": "FORBIDDEN",
            "targets": {
                "INITIAL_QUEUE_DISTRIBUTION_VALUES": {
                    "multiplier_order": ["LIQUIDITY"],
                    "paths": ["/base_flow/initial_queue_sizes/values/*"],
                },
                "LIMIT_RATE_BUY_SELL": {
                    "multiplier_order": ["VOLUME", "LIQUIDITY"],
                    "paths": [
                        "/base_flow/rates/limit_buy",
                        "/base_flow/rates/limit_sell",
                    ],
                },
                "LIMIT_SIZE_DISTRIBUTION_VALUES": {
                    "multiplier_order": ["VOLUME", "LIQUIDITY"],
                    "paths": [
                        "/base_flow/order_sizes/limit_buy/values/*",
                        "/base_flow/order_sizes/limit_sell/values/*",
                    ],
                },
                "MARKET_RATE_BUY_SELL": {
                    "multiplier_order": ["VOLUME", "VOLATILITY", "SIDE_AGGRESSIVE"],
                    "paths": [
                        "/base_flow/rates/market_buy",
                        "/base_flow/rates/market_sell",
                    ],
                },
                "MARKET_SIZE_DISTRIBUTION_VALUES": {
                    "multiplier_order": ["VOLUME", "VOLATILITY"],
                    "paths": [
                        "/base_flow/order_sizes/market_buy/values/*",
                        "/base_flow/order_sizes/market_sell/values/*",
                    ],
                },
                "SCHEDULED_PARTICIPANT_PARENT_QUANTITY": {
                    "multiplier_order": ["VOLUME"],
                    "paths": ["/scheduled_participants/*/parent_quantity_shares"],
                },
                "SCHEDULED_SHOCK_ORDER_QUANTITY": {
                    "multiplier_order": ["VOLUME", "VOLATILITY"],
                    "paths": ["/scheduled_events/*/quantity_shares"],
                },
                "SIDE_CANCEL_RATE": {
                    "multiplier_order": ["VOLUME", "CANCEL"],
                    "paths": [
                        "/base_flow/rates/cancel_bid",
                        "/base_flow/rates/cancel_ask",
                    ],
                },
            },
            "zero_rate_remains_zero": True,
        },
        "schema_version": 1,
        "seed_policy": {
            "candidate_component_label": "full_day/<candidate_id>/<component_label>",
            "development_roots": list(DEVELOPMENT_ROOTS),
            "holdout_roots": list(HOLDOUT_ROOTS),
            "partitions_disjoint": True,
            "qualification_roots": list(QUALIFICATION_ROOTS),
            "review_label": REVIEW_SELECTION_LABEL,
            "review_root": REVIEW_SELECTION_ROOT,
            "review_stream_may_consume_generator_stream": False,
            "root_replacement": "FORBIDDEN",
            "simulation_seed_formula": (
                "int.from_bytes(SHA256(u64be(root)||NUL||utf8_nfc(policy_version)||NUL||utf8_nfc(label))[0:8],big)&((1<<63)-1)"
            ),
            "shared_numeric_root_cross_candidate_substream": False,
        },
        "semantic_version": 1,
        "transition_source_eligibility": {
            "base_plan_eligible": False,
            "ineligibility_reason": "LOCAL_TRANSITION_ROWS_HAVE_FEWER_THAN_TWO_POSITIVE_DESTINATIONS",
            "minimum_positive_destination_weight": 200_001,
            "minimum_positive_destinations_per_local_row": 2,
            "required_row_weight_sum": POLICY_SCALE_PPM,
            "source_policy_id": "BOUNDED_SEARCH_CONTROLLED_V1",
        },
    }
