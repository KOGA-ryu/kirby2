"""Controlled deterministic statistical risk experiments over generated cells."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from statistics import fmean, median

from kirby2.calibration import (
    CalibrationConfig,
    calibrate_market,
    resolve_measurement_source,
)
from kirby2.immutable import freeze_json, thaw_json
from kirby2.simulation import load_accepted_hawkes_configs

from .generator import derive_calibration_seeds, scientific_match_parameters
from .models import ExecutorLane, StatisticalCheck, canonical_json, canonical_sha256


_THRESHOLD_MANIFEST_SOURCE = {
    "schema_version": 2,
    "matched_cell_design": {
        "holdout_replicates": 3,
        "required_replicates": 6,
        "train_replicates": 3,
        "unit": "replicates_per_cell",
    },
    "calibration_design": {
        "candidate_count": 2,
        "duration_seconds": 1,
        "heldout_seed_count": 2,
        "stage_count": 1,
        "train_seed_count": 2,
        "unit": "production_calibration_run",
    },
    "checks": {
        "calibration_train_vs_holdout": {
            "description": (
                "finite losses and disjoint derived seed sets; warn when the "
                "heldout/fitting final-loss ratio exceeds 4.0 or heldout loss "
                "does not improve"
            ),
            "warning_if_heldout_not_improved": True,
            "warning_above_loss_ratio": 4.0,
            "unit": "heldout_loss_per_fitting_loss",
        },
        "distribution_drift": {
            "description": (
                "maximum matched-cell train/holdout histogram total variation "
                "<= 7500 basis points"
            ),
            "warning_above_total_variation_bps": 7_500,
            "unit": "total_variation_basis_points",
        },
        "scenario_overfitting": {
            "description": (
                "matched scenario/objective strategy-rank concordance >= 5000 "
                "basis points"
            ),
            "warning_below_rank_concordance_bps": 5_000,
            "unit": "concordant_strategy_pair_basis_points",
        },
        "seed_sensitivity": {
            "description": (
                "maximum within-cell event-count range/median <= 50000 basis points"
            ),
            "warning_above_normalized_range_bps": 50_000,
            "unit": "range_per_absolute_median_basis_points",
        },
        "unstable_hawkes": {
            "description": (
                "production Hawkes certifications contain no rejected profile; "
                "near-critical certifications remain warnings"
            ),
            "pass_classification": "PASS_SUBCRITICAL",
            "rejection_prefix": "REJECT",
            "warning_classifications": ["WARNING_NEAR_CRITICAL"],
            "unit": "production_stability_classification",
        },
        "unrealistic_event_explosion": {
            "description": (
                "capped core-flow counts do not breach the one-sided Poisson-"
                "dominating envelope at Bonferroni-corrected family-wise "
                "alpha 1e-6"
            ),
            "comparison_correction": "BONFERRONI",
            "family_wise_alpha_parts_per_billion": 1_000,
            "method": "POISSON_DOMINATING_INTEGER_COUNT_UPPER_TAIL",
            "unit": "bonferroni_adjusted_one_sided_probability",
        },
        "degenerate_no_trade": {
            "description": (
                "no-trade rate <= 9000 basis points in continuous "
                "trade-eligible matched cells"
            ),
            "warning_above_no_trade_rate_bps": 9_000,
            "unit": "no_trade_case_basis_points",
        },
        "price_runaway": {
            "description": (
                "maximum absolute displacement from each recorded initial "
                "reference <= 200 x2 ticks"
            ),
            "fail_above_absolute_displacement_x2_ticks": 200,
            "unit": "x2_ticks",
        },
        "permanent_crossed_composite_quote": {
            "description": (
                "no continuous locked/crossed episode exceeds max(100000 us, "
                "4 * maximum configured market-data latency)"
            ),
            "latency_multiplier": 4,
            "minimum_duration_us": 100_000,
            "unit": "simulation_microseconds",
        },
    },
    "deterministic_quantiles": {
        "method": "nearest_index_half_up",
        "points_bps": [0, 2_500, 5_000, 7_500, 10_000],
        "unit": "distribution_basis_points",
    },
    "volume_histogram": {
        "bins": [
            {"label": "zero", "lower_shares": 0, "upper_shares": 0},
            {"label": "1_99", "lower_shares": 1, "upper_shares": 99},
            {"label": "100_199", "lower_shares": 100, "upper_shares": 199},
            {"label": "200_499", "lower_shares": 200, "upper_shares": 499},
            {"label": "500_plus", "lower_shares": 500, "upper_shares": None},
        ],
        "unit": "shares_per_applied_core_flow_command",
    },
}
STATISTICAL_THRESHOLD_MANIFEST = freeze_json(_THRESHOLD_MANIFEST_SOURCE)
STATISTICAL_THRESHOLD_MANIFEST_SHA256 = canonical_sha256(
    _THRESHOLD_MANIFEST_SOURCE
)


@dataclass(frozen=True, slots=True)
class _MatchedCell:
    lane: ExecutorLane
    cell_id: str
    match_parameters: dict[str, object]
    rows: tuple[dict[str, object], ...]

    @property
    def train(self) -> tuple[dict[str, object], ...]:
        return tuple(
            item
            for item in self.rows
            if _configuration(item)["partition"] == "TRAIN"
        )

    @property
    def holdout(self) -> tuple[dict[str, object], ...]:
        return tuple(
            item
            for item in self.rows
            if _configuration(item)["partition"] == "HOLDOUT"
        )

    def design_evidence(self) -> dict[str, object]:
        train_seeds = sorted(int(_configuration(item)["seed"]) for item in self.train)
        holdout_seeds = sorted(
            int(_configuration(item)["seed"]) for item in self.holdout
        )
        return {
            "cell_id": self.cell_id,
            "cell_ids": [self.cell_id],
            "holdout_replicate_indices": sorted(
                int(_configuration(item)["replicate_index"])
                for item in self.holdout
            ),
            "holdout_seeds": holdout_seeds,
            "lane": self.lane.value,
            "match_parameters": self.match_parameters,
            "match_parameters_sha256": canonical_sha256(self.match_parameters),
            "non_seed_exercised_parameters_match": True,
            "seed_sets_disjoint": set(train_seeds).isdisjoint(holdout_seeds),
            "train_replicate_indices": sorted(
                int(_configuration(item)["replicate_index"])
                for item in self.train
            ),
            "train_seeds": train_seeds,
        }


def statistical_threshold_manifest() -> dict[str, object]:
    manifest = thaw_json(STATISTICAL_THRESHOLD_MANIFEST)
    if not isinstance(manifest, dict):
        raise TypeError("statistical threshold manifest must be an object")
    return manifest


def statistical_checks(
    cases: tuple[dict[str, object], ...],
    master_seed: int,
) -> tuple[StatisticalCheck, ...]:
    if not cases:
        raise ValueError("statistical checks require at least one case")
    if type(master_seed) is not int or master_seed < 0:
        raise ValueError("statistical master seed must be nonnegative")
    cells = _complete_cells(cases)
    return (
        _calibration_holdout(master_seed),
        _distribution_drift(cells),
        _scenario_overfit(cells),
        _seed_sensitivity(cells),
        _hawkes_stability(),
        _event_explosion(cells),
        _degenerate_no_trade(cells),
        _price_runaway(cells),
        _permanent_cross(cells),
    )


def classify_cross_episode(
    duration_us: int,
    maximum_configured_market_data_latency_us: int,
) -> dict[str, object]:
    if type(duration_us) is not int or duration_us < 0:
        raise ValueError("cross duration must be nonnegative microseconds")
    if (
        type(maximum_configured_market_data_latency_us) is not int
        or maximum_configured_market_data_latency_us < 0
    ):
        raise ValueError("market-data latency must be nonnegative microseconds")
    spec = _check_spec("permanent_crossed_composite_quote")
    threshold_us = max(
        int(spec["minimum_duration_us"]),
        int(spec["latency_multiplier"])
        * maximum_configured_market_data_latency_us,
    )
    permanent = duration_us > threshold_us
    return {
        "classification": "PERMANENT" if permanent else "SHORT_EPISODE",
        "duration_us": duration_us,
        "exceeds_threshold": permanent,
        "maximum_configured_market_data_latency_us": (
            maximum_configured_market_data_latency_us
        ),
        "threshold_us": threshold_us,
    }


def classify_capped_event_count(
    *,
    event_count: int,
    simulated_duration_us: int,
    configured_intensity_cap_eps: float,
    comparison_count: int,
) -> dict[str, object]:
    """Classify a realized count against a capped-intensity envelope."""

    if type(event_count) is not int or event_count < 0:
        raise ValueError("capped event count must be a nonnegative integer")
    if type(simulated_duration_us) is not int or simulated_duration_us <= 0:
        raise ValueError("capped event duration must be positive microseconds")
    if (
        type(configured_intensity_cap_eps) not in {int, float}
        or not math.isfinite(float(configured_intensity_cap_eps))
        or float(configured_intensity_cap_eps) <= 0
    ):
        raise ValueError("configured intensity cap must be finite and positive")
    if type(comparison_count) is not int or comparison_count <= 0:
        raise ValueError("capped event comparison count must be positive")

    spec = _check_spec("unrealistic_event_explosion")
    alpha_ppb = spec["family_wise_alpha_parts_per_billion"]
    if type(alpha_ppb) is not int or not 0 < alpha_ppb < 1_000_000_000:
        raise RuntimeError("event-count family-wise alpha is invalid")
    if spec["comparison_correction"] != "BONFERRONI":
        raise RuntimeError("unsupported event-count comparison correction")
    if spec["method"] != "POISSON_DOMINATING_INTEGER_COUNT_UPPER_TAIL":
        raise RuntimeError("unsupported capped event-count method")

    cap = float(configured_intensity_cap_eps)
    duration_seconds = simulated_duration_us / 1_000_000
    dominating_mean = cap * duration_seconds
    tail_probability = _poisson_upper_tail_probability(
        event_count,
        dominating_mean,
    )
    family_wise_alpha = alpha_ppb / 1_000_000_000
    per_comparison_alpha = family_wise_alpha / comparison_count
    adjusted_probability = min(1.0, tail_probability * comparison_count)
    realized_eps = event_count / duration_seconds
    observed_to_cap_bps = round(realized_eps * 10_000 / cap)
    failed = tail_probability < per_comparison_alpha
    return {
        "bonferroni_adjusted_upper_tail_probability": adjusted_probability,
        "poisson_dominating_mean_events": round(dominating_mean, 12),
        "classification": "FAIL" if failed else "PASS",
        "comparison_count": comparison_count,
        "configured_intensity_cap_events_per_second": cap,
        "event_count": event_count,
        "events_per_simulated_second": round(realized_eps, 9),
        "exceeds_poisson_dominating_mean": event_count > dominating_mean,
        "family_wise_alpha": family_wise_alpha,
        "family_wise_alpha_parts_per_billion": alpha_ppb,
        "method": str(spec["method"]),
        "observed_to_cap_bps": observed_to_cap_bps,
        "per_comparison_alpha": per_comparison_alpha,
        "poisson_dominating_upper_tail_probability": tail_probability,
        "simulated_duration_us": simulated_duration_us,
    }


def volume_histogram_label(quantity_shares: int) -> str:
    if type(quantity_shares) is not int or quantity_shares < 0:
        raise ValueError("volume histogram quantity must be nonnegative shares")
    raw_bins = _manifest_section("volume_histogram")["bins"]
    if not isinstance(raw_bins, tuple):
        raise TypeError("volume histogram bins must be immutable records")
    for raw_bin in raw_bins:
        if not isinstance(raw_bin, Mapping):
            raise TypeError("volume histogram bin must be an object")
        lower = raw_bin["lower_shares"]
        upper = raw_bin["upper_shares"]
        if type(lower) is not int or (upper is not None and type(upper) is not int):
            raise TypeError("volume histogram bounds must be integer shares")
        if quantity_shares >= lower and (upper is None or quantity_shares <= upper):
            return str(raw_bin["label"])
    raise RuntimeError("volume histogram manifest does not cover the quantity")


def _complete_cells(
    cases: tuple[dict[str, object], ...],
) -> tuple[_MatchedCell, ...]:
    design = _manifest_section("matched_cell_design")
    required_replicates = int(design["required_replicates"])
    train_replicates = int(design["train_replicates"])
    holdout_replicates = int(design["holdout_replicates"])
    if train_replicates + holdout_replicates != required_replicates:
        raise RuntimeError("matched-cell threshold manifest is inconsistent")
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for case in cases:
        configuration = _configuration(case)
        lane_name = configuration.get("lane")
        cell_id = configuration.get("cell_id")
        if type(lane_name) is not str or type(cell_id) is not str:
            continue
        if lane_name == ExecutorLane.FAULT.value:
            continue
        if configuration.get("injected_fault") is not None:
            continue
        grouped[(lane_name, cell_id)].append(case)

    complete: list[_MatchedCell] = []
    for (lane_name, cell_id), raw_rows in sorted(grouped.items()):
        if len(raw_rows) != required_replicates:
            continue
        rows = tuple(
            sorted(
                raw_rows,
                key=lambda item: int(_configuration(item)["replicate_index"]),
            )
        )
        configurations = tuple(_configuration(item) for item in rows)
        if {int(item["replicate_index"]) for item in configurations} != set(
            range(required_replicates)
        ):
            continue
        if {
            str(item["partition"])
            for item in configurations[:train_replicates]
        } != {"TRAIN"}:
            continue
        if {
            str(item["partition"])
            for item in configurations[train_replicates:]
        } != {"HOLDOUT"}:
            continue
        try:
            match_parameters = tuple(
                scientific_match_parameters(item) for item in configurations
            )
        except (KeyError, TypeError, ValueError):
            continue
        wires = {canonical_json(item) for item in match_parameters}
        if len(wires) != 1:
            continue
        seeds = tuple(int(item["seed"]) for item in configurations)
        if len(set(seeds)) != required_replicates or not set(
            seeds[:train_replicates]
        ).isdisjoint(seeds[train_replicates:]):
            continue
        complete.append(
            _MatchedCell(
                ExecutorLane(lane_name),
                cell_id,
                match_parameters[0],
                rows,
            )
        )
    return tuple(complete)


def _calibration_holdout(master_seed: int) -> StatisticalCheck:
    design = _manifest_section("calibration_design")
    seconds = int(design["duration_seconds"])
    stage_count = int(design["stage_count"])
    candidate_count = int(design["candidate_count"])
    roles = derive_calibration_seeds(master_seed)
    fitting = tuple(int(item) for item in roles["fitting_seeds"])
    heldout = tuple(int(item) for item in roles["heldout_seeds"])
    if len(fitting) != int(design["train_seed_count"]) or len(heldout) != int(
        design["heldout_seed_count"]
    ):
        raise RuntimeError("calibration seed manifest is inconsistent")
    reference = resolve_measurement_source(
        "scenario:balanced",
        seed=int(roles["reference_seed"]),
        seconds=seconds,
    )
    run = calibrate_market(
        reference,
        CalibrationConfig(
            scenario_name="balanced",
            seconds=seconds,
            stages=tuple(range(1, stage_count + 1)),
            fitting_seeds=fitting,
            heldout_seeds=heldout,
            search_seed=int(roles["search_seed"]),
            candidate_count_per_stage=candidate_count,
            profile_id="audit_lab_statistical_holdout",
        ),
    )
    losses = (
        run.initial_fitting.mean_loss,
        run.final_fitting.mean_loss,
        run.initial_heldout.mean_loss,
        run.final_heldout.mean_loss,
    )
    finite = all(math.isfinite(value) for value in losses)
    disjoint = set(fitting).isdisjoint(heldout)
    ratio = run.final_heldout.mean_loss / max(
        1e-12,
        run.final_fitting.mean_loss,
    )
    calibration_spec = _check_spec("calibration_train_vs_holdout")
    warning_above = float(calibration_spec["warning_above_loss_ratio"])
    warn_without_improvement = bool(
        calibration_spec["warning_if_heldout_not_improved"]
    )
    status = (
        "FAIL"
        if not finite or not disjoint
        else "WARNING"
        if ratio > warning_above
        or (warn_without_improvement and not run.heldout_improved)
        else "PASS"
    )
    return _make_check(
        "calibration_train_vs_holdout",
        status,
        {
            "design": "production_calibrate_market_disjoint_seed_holdout",
            "final_fitting_loss": round(run.final_fitting.mean_loss, 9),
            "final_heldout_loss": round(run.final_heldout.mean_loss, 9),
            "fitting_seeds": list(fitting),
            "heldout_improved": run.heldout_improved,
            "heldout_seeds": list(heldout),
            "initial_fitting_loss": round(run.initial_fitting.mean_loss, 9),
            "initial_heldout_loss": round(run.initial_heldout.mean_loss, 9),
            "losses_finite": finite,
            "master_seed": master_seed,
            "reference_seed": int(roles["reference_seed"]),
            "run_sha256": run.sha256(),
            "search_seed": int(roles["search_seed"]),
            "seed_sets_disjoint": disjoint,
            "final_heldout_to_fitting_loss_ratio": round(ratio, 9),
        },
    )


def _distribution_drift(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    comparisons: list[dict[str, object]] = []
    for cell in cells:
        if cell.lane is not ExecutorLane.CORE_FLOW:
            continue
        try:
            train_family = _summed_histogram(
                cell.train,
                "core_flow_event_family_histogram",
            )
            holdout_family = _summed_histogram(
                cell.holdout,
                "core_flow_event_family_histogram",
            )
            train_volume = _summed_histogram(
                cell.train,
                "core_flow_volume_histogram",
            )
            holdout_volume = _summed_histogram(
                cell.holdout,
                "core_flow_volume_histogram",
            )
        except (KeyError, TypeError, ValueError):
            continue
        family_tv = _total_variation_bps(train_family, holdout_family)
        volume_tv = _total_variation_bps(train_volume, holdout_volume)
        comparisons.append(
            {
                **cell.design_evidence(),
                "event_family": {
                    "holdout_histogram": holdout_family,
                    "total_variation_bps": family_tv,
                    "train_histogram": train_family,
                },
                "volume": {
                    "holdout_histogram": holdout_volume,
                    "total_variation_bps": volume_tv,
                    "train_histogram": train_volume,
                },
            }
        )
    if not comparisons:
        return _make_check(
            "distribution_drift",
            "NOT_EXERCISED",
            {
                "complete_core_flow_cell_count": 0,
                "reason": "no complete six-replicate core-flow histogram cell",
            },
        )
    maximum = max(
        max(
            int(item["event_family"]["total_variation_bps"]),
            int(item["volume"]["total_variation_bps"]),
        )
        for item in comparisons
    )
    warning_above = int(
        _check_spec("distribution_drift")[
            "warning_above_total_variation_bps"
        ]
    )
    return _make_check(
        "distribution_drift",
        "WARNING" if maximum > warning_above else "PASS",
        {
            "comparison_count": len(comparisons),
            "comparisons": comparisons,
            "design": "within_cell_train_vs_holdout_histogram_total_variation",
            "maximum_total_variation_bps": maximum,
        },
    )


def _scenario_overfit(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    samples: dict[tuple[str, str], dict[str, list[dict[str, object]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for cell in cells:
        if cell.lane is not ExecutorLane.ALGORITHM:
            continue
        first = _statistical_evidence(cell.rows[0])
        scenario = first.get("algorithm_scenario")
        objective = first.get("algorithm_objective")
        strategy = first.get("algorithm_strategy")
        if any(type(item) is not str for item in (scenario, objective, strategy)):
            continue
        if objective == "OBSERVE_ONLY":
            continue
        partition_scores: dict[str, float] = {}
        valid = True
        for partition_name, rows in (
            ("train", cell.train),
            ("holdout", cell.holdout),
        ):
            numerator = 0
            denominator = 0
            for row in rows:
                evidence = _statistical_evidence(row)
                raw_numerator = evidence.get("algorithm_cost_numerator_x2_ticks")
                raw_denominator = evidence.get("algorithm_cost_denominator_shares")
                if type(raw_numerator) is not int or type(raw_denominator) is not int:
                    valid = False
                    break
                numerator += raw_numerator
                denominator += raw_denominator
            if not valid or denominator <= 0:
                valid = False
                break
            partition_scores[partition_name] = numerator / denominator
        if not valid:
            continue
        samples[(str(scenario), str(objective))][str(strategy)].append(
            {
                **cell.design_evidence(),
                "holdout_cost_x2_ticks_per_target_share": round(
                    partition_scores["holdout"],
                    9,
                ),
                "train_cost_x2_ticks_per_target_share": round(
                    partition_scores["train"],
                    9,
                ),
            }
        )

    contexts: list[dict[str, object]] = []
    concordant_total = 0
    comparable_total = 0
    for (scenario, objective), strategies in sorted(samples.items()):
        if len(strategies) < 2:
            continue
        scores = {
            strategy: {
                "cell_comparisons": sorted(
                    rows,
                    key=lambda item: str(item["cell_id"]),
                ),
                "holdout": fmean(
                    float(item["holdout_cost_x2_ticks_per_target_share"])
                    for item in rows
                ),
                "train": fmean(
                    float(item["train_cost_x2_ticks_per_target_share"])
                    for item in rows
                ),
            }
            for strategy, rows in sorted(strategies.items())
        }
        concordant = 0
        comparable = 0
        for left, right in combinations(sorted(scores), 2):
            train_relation = _relation(
                float(scores[left]["train"]),
                float(scores[right]["train"]),
            )
            holdout_relation = _relation(
                float(scores[left]["holdout"]),
                float(scores[right]["holdout"]),
            )
            comparable += 1
            concordant += train_relation == holdout_relation
        train_ranking = sorted(
            scores,
            key=lambda strategy: (float(scores[strategy]["train"]), strategy),
        )
        holdout_ranking = sorted(
            scores,
            key=lambda strategy: (float(scores[strategy]["holdout"]), strategy),
        )
        context_bps = round(10_000 * concordant / comparable)
        concordant_total += concordant
        comparable_total += comparable
        contexts.append(
            {
                "concordant_pair_count": concordant,
                "holdout_ranking": holdout_ranking,
                "objective": objective,
                "pair_count": comparable,
                "rank_concordance_bps": context_bps,
                "scenario": scenario,
                "strategy_evidence": scores,
                "train_ranking": train_ranking,
            }
        )
    if not contexts or comparable_total == 0:
        return _make_check(
            "scenario_overfitting",
            "NOT_EXERCISED",
            {
                "reason": (
                    "fewer than two strategies shared a complete "
                    "scenario/objective context"
                )
            },
        )
    concordance_bps = round(10_000 * concordant_total / comparable_total)
    warning_below = int(
        _check_spec("scenario_overfitting")[
            "warning_below_rank_concordance_bps"
        ]
    )
    return _make_check(
        "scenario_overfitting",
        "WARNING" if concordance_bps < warning_below else "PASS",
        {
            "context_count": len(contexts),
            "contexts": contexts,
            "cost_definition": (
                "implementation_shortfall_x2_tick_shares / target_quantity"
            ),
            "design": "within_scenario_and_objective_rank_concordance",
            "rank_concordance_bps": concordance_bps,
            "universal_winner_declared": False,
        },
    )


def _seed_sensitivity(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    summaries: list[dict[str, object]] = []
    for cell in cells:
        values: list[float] = []
        valid = True
        for row in cell.rows:
            value = _statistical_evidence(row).get("sensitivity_event_count")
            if type(value) not in {int, float}:
                valid = False
                break
            values.append(float(value))
        if not valid:
            continue
        middle = float(median(values))
        spread = max(values) - min(values)
        mad = float(median(abs(value - middle) for value in values))
        normalized_range_bps = round(
            spread * 10_000 / max(1.0, abs(middle))
        )
        summaries.append(
            {
                **cell.design_evidence(),
                "changed_with_seed": spread > 0,
                "median": round(middle, 6),
                "median_absolute_deviation": round(mad, 6),
                "metric": "recorded_event_count",
                "normalized_range_bps": normalized_range_bps,
                "range": round(spread, 6),
                "values_by_replicate": [
                    {
                        "replicate_index": int(
                            _configuration(row)["replicate_index"]
                        ),
                        "seed": int(_configuration(row)["seed"]),
                        "value": values[index],
                    }
                    for index, row in enumerate(cell.rows)
                ],
            }
        )
    if not summaries:
        return _make_check(
            "seed_sensitivity",
            "NOT_EXERCISED",
            {"reason": "no complete cell had a recorded sensitivity metric"},
        )
    maximum = max(int(item["normalized_range_bps"]) for item in summaries)
    warning_above = int(
        _check_spec("seed_sensitivity")[
            "warning_above_normalized_range_bps"
        ]
    )
    return _make_check(
        "seed_sensitivity",
        "WARNING" if maximum > warning_above else "PASS",
        {
            "cell_count": len(summaries),
            "cells": summaries,
            "changed_cell_count": sum(
                bool(item["changed_with_seed"]) for item in summaries
            ),
            "cross_cell_quantiles": {
                "median": _quantiles(
                    [float(item["median"]) for item in summaries]
                ),
                "median_absolute_deviation": _quantiles(
                    [
                        float(item["median_absolute_deviation"])
                        for item in summaries
                    ]
                ),
                "normalized_range_bps": _quantiles(
                    [float(item["normalized_range_bps"]) for item in summaries]
                ),
                "range": _quantiles(
                    [float(item["range"]) for item in summaries]
                ),
            },
            "design": "within_cell_seed_only_exercised_parameter_variation",
            "maximum_normalized_range_bps": maximum,
        },
    )


def _hawkes_stability() -> StatisticalCheck:
    certifications = {
        name: config.stability_certification.as_dict()
        for name, config in sorted(load_accepted_hawkes_configs().items())
    }
    spec = _check_spec("unstable_hawkes")
    rejection_prefix = str(spec["rejection_prefix"])
    raw_warnings = spec["warning_classifications"]
    if not isinstance(raw_warnings, tuple):
        raise TypeError("Hawkes warning classifications must be immutable")
    warning_classifications = {str(item) for item in raw_warnings}
    rejected = [
        name
        for name, item in certifications.items()
        if str(item["classification"]).startswith(rejection_prefix)
    ]
    warnings = [
        name
        for name, item in certifications.items()
        if str(item["classification"]) in warning_classifications
    ]
    return _make_check(
        "unstable_hawkes",
        "FAIL" if rejected else "WARNING" if warnings else "PASS",
        {
            "certifications": certifications,
            "production_certification_source": (
                "load_accepted_hawkes_configs().stability_certification"
            ),
            "rejected_profiles": rejected,
            "warning_profiles": warnings,
        },
    )


def _event_explosion(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    candidates: list[
        tuple[
            _MatchedCell,
            float,
            int,
            int,
            tuple[dict[str, object], ...],
            str,
        ]
    ] = []
    uncapped: list[str] = []
    invalid: list[dict[str, object]] = []
    for cell in cells:
        if cell.lane is not ExecutorLane.CORE_FLOW:
            continue
        try:
            rows = tuple(_statistical_evidence(item) for item in cell.rows)
            cap_sources = tuple(
                _event_rate_cap_source(item) for item in cell.rows
            )
        except (TypeError, ValueError) as error:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "detail": str(error),
                    "reason": "missing or malformed statistical evidence",
                }
            )
            continue
        flow_model = cell.match_parameters.get("flow_model")
        if flow_model not in {"hawkes", "simple"}:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "unknown core-flow model for cap inference",
                    "value": repr(flow_model),
                }
            )
            continue
        raw_caps = [item.get("configured_event_rate_cap_eps") for item in rows]
        source_caps = [
            item.get("configured_cap_events_per_second") for item in cap_sources
        ]
        raw_timing_transforms = [
            item.get("core_flow_arrival_timing_transform") for item in rows
        ]
        source_timing_transforms = [
            item.get("arrival_timing_transform") for item in cap_sources
        ]
        if raw_caps != source_caps:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "projected_values": [repr(item) for item in raw_caps],
                    "reason": "projected intensity cap does not match source check",
                    "source_values": [repr(item) for item in source_caps],
                }
            )
            continue
        if raw_timing_transforms != source_timing_transforms:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "projected_values": [
                        repr(item) for item in raw_timing_transforms
                    ],
                    "reason": "projected timing path does not match source check",
                    "source_values": [
                        repr(item) for item in source_timing_transforms
                    ],
                }
            )
            continue
        if all(item is None for item in raw_caps):
            if flow_model == "simple":
                uncapped.append(cell.cell_id)
            else:
                invalid.append(
                    {
                        **cell.design_evidence(),
                        "reason": "Hawkes cell omitted its configured intensity cap",
                    }
                )
            continue
        if flow_model != "hawkes":
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "uncapped simple-flow cell declared an intensity cap",
                    "values": [repr(item) for item in raw_caps],
                }
            )
            continue
        if any(item is None for item in raw_caps):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "inconsistent configured intensity caps",
                    "values": [repr(item) for item in raw_caps],
                }
            )
            continue
        if any(type(item) not in {int, float} for item in raw_caps):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "invalid configured intensity cap",
                    "values": [repr(item) for item in raw_caps],
                }
            )
            continue
        numeric_caps = [float(item) for item in raw_caps]
        if any(not math.isfinite(item) or item <= 0 for item in numeric_caps):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "invalid configured intensity cap",
                    "values": [repr(item) for item in raw_caps],
                }
            )
            continue
        if len(set(numeric_caps)) != 1:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "inconsistent configured intensity caps",
                    "values": [repr(item) for item in raw_caps],
                }
            )
            continue
        if not all(
            item == "NO_POST_MODEL_INTERVAL_COMPRESSION"
            for item in raw_timing_transforms
        ):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "capped count lacks an eligible identity timing path",
                    "values": [repr(item) for item in raw_timing_transforms],
                }
            )
            continue
        timing_transform = "NO_POST_MODEL_INTERVAL_COMPRESSION"
        cap = numeric_caps[0]
        durations = [item.get("simulation_duration_us") for item in rows]
        counts = [item.get("core_flow_event_count") for item in rows]
        if any(type(item) is not int or item <= 0 for item in durations):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "invalid simulation duration evidence",
                    "values": [repr(item) for item in durations],
                }
            )
            continue
        if any(type(item) is not int or item < 0 for item in counts):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "invalid core-flow count evidence",
                    "values": [repr(item) for item in counts],
                }
            )
            continue
        try:
            configurations = tuple(_configuration(item) for item in cell.rows)
            typed_metrics = tuple(_typed_metrics(item) for item in cell.rows)
        except (TypeError, ValueError) as error:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "detail": str(error),
                    "reason": "missing or malformed source evidence",
                }
            )
            continue
        configured_durations = [
            item.get("duration_us") for item in configurations
        ]
        typed_durations = [
            item.get("simulation_duration_us") for item in typed_metrics
        ]
        typed_counts = [item.get("flow_event_count") for item in typed_metrics]
        if any(
            type(item) is not int or item <= 0
            for item in (*configured_durations, *typed_durations)
        ) or any(type(item) is not int or item < 0 for item in typed_counts):
            invalid.append(
                {
                    **cell.design_evidence(),
                    "configured_durations": [
                        repr(item) for item in configured_durations
                    ],
                    "reason": "invalid configuration or typed-metric source evidence",
                    "typed_counts": [repr(item) for item in typed_counts],
                    "typed_durations": [repr(item) for item in typed_durations],
                }
            )
            continue
        if durations != configured_durations or durations != typed_durations:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "configured_durations": configured_durations,
                    "reason": "simulation duration evidence does not reconcile",
                    "statistical_durations": durations,
                    "typed_durations": typed_durations,
                }
            )
            continue
        if counts != typed_counts:
            invalid.append(
                {
                    **cell.design_evidence(),
                    "reason": "core-flow count evidence does not reconcile",
                    "statistical_counts": counts,
                    "typed_counts": typed_counts,
                }
            )
            continue
        duration_us = sum(int(item) for item in durations)
        event_count = sum(int(item) for item in counts)
        candidates.append(
            (
                cell,
                cap,
                duration_us,
                event_count,
                rows,
                timing_transform,
            )
        )

    if not candidates:
        return _make_check(
            "unrealistic_event_explosion",
            "FAIL" if invalid else "NOT_EXERCISED",
            {
                "invalid_cells": invalid,
                "reason": (
                    "malformed capped core-flow evidence"
                    if invalid
                    else "no complete core-flow cell had a configured intensity cap"
                ),
                "uncapped_cell_ids": sorted(uncapped),
            },
        )

    comparison_count = len(candidates)
    measured: list[dict[str, object]] = []
    for cell, cap, duration_us, event_count, rows, timing_transform in candidates:
        classification = classify_capped_event_count(
            event_count=event_count,
            simulated_duration_us=duration_us,
            configured_intensity_cap_eps=cap,
            comparison_count=comparison_count,
        )
        measured.append(
            {
                **cell.design_evidence(),
                **classification,
                "arrival_timing_transform": timing_transform,
                "replicates": [
                    {
                        "event_count": int(
                            row.get("core_flow_event_count", 0)
                        ),
                        "replicate_index": int(
                            _configuration(source)["replicate_index"]
                        ),
                        "seed": int(_configuration(source)["seed"]),
                        "simulation_duration_us": int(
                            row.get("simulation_duration_us", 0)
                        ),
                    }
                    for source, row in zip(cell.rows, rows, strict=True)
                ],
            }
        )
    failed_cell_ids = sorted(
        str(item["cell_id"])
        for item in measured
        if item["classification"] == "FAIL"
    )
    return _make_check(
        "unrealistic_event_explosion",
        "FAIL" if failed_cell_ids or invalid else "PASS",
        {
            "cells": measured,
            "comparison_count": comparison_count,
            "design": (
                "one_sided_poisson_dominating_count_tail_with_bonferroni_"
                "family_wise_control"
            ),
            "failed_cell_ids": failed_cell_ids,
            "invalid_cells": invalid,
            "maximum_observed_to_cap_bps": max(
                int(item["observed_to_cap_bps"]) for item in measured
            ),
            "minimum_bonferroni_adjusted_upper_tail_probability": min(
                float(item["bonferroni_adjusted_upper_tail_probability"])
                for item in measured
            ),
            "uncapped_cell_ids": sorted(uncapped),
        },
    )


def _degenerate_no_trade(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    eligible: list[dict[str, object]] = []
    for cell in cells:
        case_rows: list[dict[str, object]] = []
        for row in cell.rows:
            evidence = _statistical_evidence(row)
            if evidence.get("continuous_trade_eligible") is not True:
                continue
            trade_count = evidence.get("trade_count")
            if type(trade_count) is not int or trade_count < 0:
                continue
            case_rows.append(
                {
                    "replicate_index": int(
                        _configuration(row)["replicate_index"]
                    ),
                    "seed": int(_configuration(row)["seed"]),
                    "trade_count": trade_count,
                }
            )
        if case_rows:
            eligible.append(
                {
                    **cell.design_evidence(),
                    "cases": case_rows,
                }
            )
    total = sum(len(item["cases"]) for item in eligible)
    if total == 0:
        return _make_check(
            "degenerate_no_trade",
            "NOT_EXERCISED",
            {"reason": "no continuous trade-eligible complete cell"},
        )
    no_trade = sum(
        int(case["trade_count"]) == 0
        for cell in eligible
        for case in cell["cases"]
    )
    rate_bps = round(no_trade * 10_000 / total)
    warning_above = int(
        _check_spec("degenerate_no_trade")[
            "warning_above_no_trade_rate_bps"
        ]
    )
    return _make_check(
        "degenerate_no_trade",
        "WARNING" if rate_bps > warning_above else "PASS",
        {
            "continuous_trade_eligible_cells": eligible,
            "eligible_case_count": total,
            "excluded_design": (
                "non-continuous mechanics and observe-only algorithm cells"
            ),
            "no_trade_case_count": no_trade,
            "no_trade_rate_bps": rate_bps,
        },
    )


def _price_runaway(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    measured: list[dict[str, object]] = []
    eligible_lanes = {cell.lane.value for cell in cells}
    measured_lanes: set[str] = set()
    for cell in cells:
        rows: list[dict[str, object]] = []
        valid = True
        for row in cell.rows:
            evidence = _statistical_evidence(row)
            initial = evidence.get("initial_reference_x2_ticks")
            displacement = evidence.get("maximum_reference_displacement_x2_ticks")
            source = evidence.get("initial_reference_source")
            if type(initial) is not int or type(displacement) is not int:
                valid = False
                break
            rows.append(
                {
                    "initial_reference_source": source,
                    "initial_reference_x2_ticks": initial,
                    "maximum_absolute_displacement_x2_ticks": displacement,
                    "replicate_index": int(
                        _configuration(row)["replicate_index"]
                    ),
                    "seed": int(_configuration(row)["seed"]),
                }
            )
        if not valid:
            continue
        measured_lanes.add(cell.lane.value)
        measured.append({**cell.design_evidence(), "cases": rows})
    missing_lanes = sorted(eligible_lanes - measured_lanes)
    if not measured or missing_lanes:
        return _make_check(
            "price_runaway",
            "NOT_EXERCISED",
            {
                "measured_cell_count": len(measured),
                "missing_lane_measurements": missing_lanes,
                "reason": "not every complete scientific lane had a recorded reference",
            },
        )
    maximum = max(
        int(case["maximum_absolute_displacement_x2_ticks"])
        for cell in measured
        for case in cell["cases"]
    )
    fail_above = int(
        _check_spec("price_runaway")[
            "fail_above_absolute_displacement_x2_ticks"
        ]
    )
    return _make_check(
        "price_runaway",
        "FAIL" if maximum > fail_above else "PASS",
        {
            "cells": measured,
            "maximum_absolute_displacement_x2_ticks": maximum,
            "measured_lanes": sorted(measured_lanes),
            "reference_contract": "recorded_initial_reference_per_lane",
        },
    )


def _permanent_cross(cells: tuple[_MatchedCell, ...]) -> StatisticalCheck:
    timeline_cases: list[dict[str, object]] = []
    permanent: list[dict[str, object]] = []
    shorter: list[dict[str, object]] = []
    for cell in cells:
        if cell.lane is not ExecutorLane.FRAGMENTED:
            continue
        for row in cell.rows:
            evidence = _statistical_evidence(row)
            intervals = evidence.get("crossed_composite_intervals")
            latency_us = evidence.get(
                "maximum_configured_market_data_latency_us"
            )
            if not isinstance(intervals, list) or type(latency_us) is not int:
                continue
            case_record = {
                **cell.design_evidence(),
                "interval_count": len(intervals),
                "replicate_index": int(
                    _configuration(row)["replicate_index"]
                ),
                "seed": int(_configuration(row)["seed"]),
            }
            timeline_cases.append(case_record)
            for interval in intervals:
                if not isinstance(interval, Mapping):
                    continue
                duration_us = interval.get("duration_us")
                if type(duration_us) is not int:
                    continue
                classification = classify_cross_episode(duration_us, latency_us)
                record = {
                    "cell_id": cell.cell_id,
                    "episode_sequence": interval.get("episode_sequence"),
                    "locked_or_crossed_at_start": (
                        type(interval.get("start_best_bid_ticks")) is int
                        and type(interval.get("start_best_ask_ticks")) is int
                        and int(interval["start_best_bid_ticks"])
                        >= int(interval["start_best_ask_ticks"])
                    ),
                    "replicate_index": int(
                        _configuration(row)["replicate_index"]
                    ),
                    **classification,
                }
                (permanent if classification["exceeds_threshold"] else shorter).append(
                    record
                )
    if not timeline_cases:
        return _make_check(
            "permanent_crossed_composite_quote",
            "NOT_EXERCISED",
            {"reason": "no complete fragmented cell exposed an episode timeline"},
        )
    return _make_check(
        "permanent_crossed_composite_quote",
        "FAIL" if permanent else "PASS",
        {
            "episode_timeline_case_count": len(timeline_cases),
            "permanent_episode_count": len(permanent),
            "permanent_episodes": permanent,
            "short_episode_count": len(shorter),
            "short_episodes": shorter,
            "timeline_cases": timeline_cases,
        },
    )


def _poisson_upper_tail_probability(
    event_count: int,
    mean: float,
) -> float:
    """Return P[Poisson(mean) >= event_count] without upper-tail cancellation."""

    if event_count <= 0:
        return 1.0
    if mean <= 0:
        return 0.0
    if event_count <= mean:
        term = math.exp(-mean)
        lower_cumulative = term
        for index in range(1, event_count):
            term *= mean / index
            lower_cumulative += term
        return min(1.0, max(0.0, 1.0 - lower_cumulative))

    log_term = (
        -mean
        + event_count * math.log(mean)
        - math.lgamma(event_count + 1)
    )
    term = math.exp(log_term)
    upper_tail = term
    index = event_count
    for _ in range(100_000):
        index += 1
        term *= mean / index
        updated = upper_tail + term
        if updated == upper_tail:
            break
        upper_tail = updated
    else:
        raise RuntimeError("Poisson upper-tail calculation did not converge")
    return min(1.0, max(0.0, upper_tail))


def _configuration(case: Mapping[str, object]) -> dict[str, object]:
    value = case.get("configuration")
    if not isinstance(value, Mapping):
        raise TypeError("case configuration is missing")
    return dict(value)


def _statistical_evidence(case: Mapping[str, object]) -> dict[str, object]:
    value = case.get("statistical_evidence")
    if not isinstance(value, Mapping):
        raise TypeError("case statistical evidence is missing")
    return dict(value)


def _typed_metrics(case: Mapping[str, object]) -> dict[str, object]:
    value = case.get("typed_metrics")
    if not isinstance(value, Mapping):
        raise TypeError("case typed metrics are missing")
    return dict(value)


def _event_rate_cap_source(case: Mapping[str, object]) -> dict[str, object]:
    raw_checks = case.get("invariant_checks")
    if not isinstance(raw_checks, (list, tuple)):
        raise TypeError("case invariant checks are missing")
    matches = [
        item
        for item in raw_checks
        if isinstance(item, Mapping) and item.get("name") == "event_rate_cap"
    ]
    if len(matches) != 1:
        raise ValueError("case must have exactly one event-rate-cap source check")
    check = matches[0]
    if check.get("required") is not True or check.get("status") != "PASS":
        raise ValueError("event-rate-cap source check is not a required pass")
    evidence = check.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TypeError("event-rate-cap source evidence is missing")
    return dict(evidence)


def _summed_histogram(
    rows: tuple[dict[str, object], ...],
    name: str,
) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        raw = _statistical_evidence(row).get(name)
        if not isinstance(raw, Mapping):
            raise TypeError(f"{name} is not a histogram")
        for key, value in raw.items():
            if type(key) is not str or type(value) is not int or value < 0:
                raise ValueError(f"{name} contains an invalid count")
            totals[key] += value
    return dict(sorted(totals.items()))


def _total_variation_bps(
    left: Mapping[str, int],
    right: Mapping[str, int],
) -> int:
    left_total = sum(left.values())
    right_total = sum(right.values())
    if left_total == 0 and right_total == 0:
        return 0
    if left_total == 0 or right_total == 0:
        return 10_000
    distance = sum(
        abs(left.get(key, 0) / left_total - right.get(key, 0) / right_total)
        for key in set(left) | set(right)
    )
    return round(5_000 * distance)


def _relation(left: float, right: float) -> int:
    return -1 if left < right else 1 if left > right else 0


def _quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    points = tuple(
        int(item)
        for item in _manifest_section("deterministic_quantiles")["points_bps"]
    )
    result: dict[str, float] = {}
    for point in points:
        index = (point * (len(ordered) - 1) + 5_000) // 10_000
        result[str(point)] = round(ordered[index], 6)
    return result


def _manifest_section(name: str) -> Mapping[str, object]:
    value = STATISTICAL_THRESHOLD_MANIFEST[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"statistical manifest section {name} is invalid")
    return value


def _check_spec(name: str) -> Mapping[str, object]:
    checks = _manifest_section("checks")
    value = checks[name]
    if not isinstance(value, Mapping):
        raise TypeError(f"statistical threshold {name} is invalid")
    return value


def _make_check(
    name: str,
    status: str,
    evidence: dict[str, object],
) -> StatisticalCheck:
    spec = _check_spec(name)
    return StatisticalCheck(
        name,
        status,
        {
            **evidence,
            "threshold_id": name,
            "threshold_manifest_sha256": STATISTICAL_THRESHOLD_MANIFEST_SHA256,
        },
        str(spec["description"]),
    )
