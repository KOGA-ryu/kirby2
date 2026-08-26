"""Deterministic staged bounded search over declared simulator parameters."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from kirby2.scenarios import get_scenario_definition
from kirby2.simulation import SeededRng

from .measurements import measure_stream
from .models import CalibrationMetric, CalibrationReport, NormalizedMarketStream
from .profiles import (
    CalibrationEvaluation,
    CalibrationObjective,
    CalibrationRun,
    CalibrationStageOutcome,
    DistanceKind,
    MarketProfile,
    ObjectiveTerm,
    ParameterSpec,
)
from .runtime import run_parameterized_market


MISSING_METRIC_PENALTY = 4.0
QUANTILE_COMPONENTS = ("minimum", "q25", "median", "q75", "maximum")
VOLUME_COMPONENTS = tuple(f"bucket_{index:02d}" for index in range(1, 11))


PARAMETER_SPECS = (
    ParameterSpec("event_intensity", 1, (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)),
    ParameterSpec("order_size_scale", 1, (0.5, 0.75, 1.0, 1.5, 2.0)),
    ParameterSpec("initial_queue_scale", 1, (0.5, 0.75, 1.0, 1.5, 2.0)),
    ParameterSpec("initial_half_spread_ticks", 1, (1.0, 2.0, 3.0)),
    ParameterSpec("cancel_rate_scale", 2, (0.5, 0.75, 1.0, 1.5, 2.5)),
    ParameterSpec("placement_depth_scale", 2, (0.5, 0.75, 1.0, 1.5, 2.0)),
    ParameterSpec("imbalance_feedback_scale", 2, (0.0, 0.5, 1.0, 1.5, 2.0)),
    ParameterSpec("market_size_scale", 3, (0.5, 0.75, 1.0, 1.5, 2.5)),
    ParameterSpec("market_rate_scale", 3, (0.5, 0.75, 1.0, 1.5, 2.0)),
    ParameterSpec("trend_feedback_scale", 3, (0.5, 1.0, 1.5, 2.0)),
    ParameterSpec("hawkes_excitation_scale", 4, (0.0, 0.4, 0.7, 1.0)),
    ParameterSpec("queue_response_scale", 4, (0.0, 0.5, 1.0, 1.5)),
)


DEFAULT_OBJECTIVE = CalibrationObjective(
    "market_microstructure_staged_v1",
    (
        ObjectiveTerm(
            "volume_profile",
            DistanceKind.VOLUME_PROFILE,
            2.0,
            1,
            VOLUME_COMPONENTS,
        ),
        ObjectiveTerm(
            "event_rates",
            DistanceKind.COMPONENT_VECTOR,
            2.0,
            1,
            ("limit", "market", "trade"),
        ),
        ObjectiveTerm(
            "trade_size_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.5,
            1,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "top_of_book_depth_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            1,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "spread_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            1,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "cancellation_rate",
            DistanceKind.SYMMETRIC_SCALAR,
            2.0,
            2,
        ),
        ObjectiveTerm(
            "cancel_size_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            2,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "limit_placement_depth_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            2,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "imbalance_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            2,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "price_impact",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            3,
            QUANTILE_COMPONENTS,
        ),
        ObjectiveTerm(
            "realized_volatility",
            DistanceKind.SYMMETRIC_SCALAR,
            1.5,
            3,
        ),
        ObjectiveTerm(
            "trade_clustering",
            DistanceKind.COMPONENT_VECTOR,
            1.0,
            3,
            ("index_of_dispersion", "mean_bucket_count"),
        ),
        ObjectiveTerm(
            "cancel_clustering",
            DistanceKind.COMPONENT_VECTOR,
            1.0,
            3,
            ("index_of_dispersion", "mean_bucket_count"),
        ),
        ObjectiveTerm(
            "inter_event_time_distribution",
            DistanceKind.QUANTILE_SHAPE,
            1.0,
            4,
            QUANTILE_COMPONENTS,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    scenario_name: str
    seconds: int
    stages: tuple[int, ...] = (1, 2, 3, 4)
    fitting_seeds: tuple[int, ...] = (101, 202, 303)
    heldout_seeds: tuple[int, ...] = (404, 505)
    search_seed: int = 17
    candidate_count_per_stage: int = 24
    profile_id: str = "calibrated_market_v1"
    fixed_parameters: Mapping[str, float] = field(default_factory=dict)
    objective: CalibrationObjective = DEFAULT_OBJECTIVE

    def __post_init__(self) -> None:
        get_scenario_definition(self.scenario_name)
        if type(self.seconds) is not int or self.seconds <= 0:
            raise ValueError("calibration duration must be positive whole seconds")
        if (
            not self.stages
            or self.stages != tuple(range(1, max(self.stages) + 1))
            or max(self.stages) > 4
        ):
            raise ValueError("calibration stages must be contiguous from stage 1")
        if len(self.fitting_seeds) < 2:
            raise ValueError("calibration fitting requires at least two seeds")
        if not self.heldout_seeds or set(self.fitting_seeds) & set(self.heldout_seeds):
            raise ValueError("held-out seeds must be nonempty and disjoint from fitting seeds")
        if any(type(seed) is not int for seed in (*self.fitting_seeds, *self.heldout_seeds)):
            raise TypeError("calibration seeds must be integers")
        if type(self.search_seed) is not int:
            raise TypeError("calibration search seed must be an integer")
        if type(self.candidate_count_per_stage) is not int or self.candidate_count_per_stage < 2:
            raise ValueError("each calibration stage requires at least two candidates")
        if not self.profile_id:
            raise ValueError("calibrated profile ID must not be empty")
        selected = {spec.name: spec for spec in selected_parameter_specs(self.stages)}
        unknown = set(self.fixed_parameters) - set(selected)
        if unknown:
            raise ValueError(f"fixed parameters are outside selected stages: {sorted(unknown)}")
        for name, value in self.fixed_parameters.items():
            selected[name].validate(float(value))


class _ReportCache:
    def __init__(self, scenario_name: str, seconds: int) -> None:
        self.scenario_name = scenario_name
        self.seconds = seconds
        self._reports: dict[tuple[tuple[tuple[str, float], ...], int], CalibrationReport] = {}

    def report(self, parameters: Mapping[str, float], seed: int) -> CalibrationReport:
        key = (_parameter_key(parameters), seed)
        if key not in self._reports:
            _, report = run_parameterized_market(
                self.scenario_name,
                parameters,
                seed=seed,
                seconds=self.seconds,
            )
            self._reports[key] = report
        return self._reports[key]


def calibrate_market(
    reference_stream: NormalizedMarketStream,
    config: CalibrationConfig,
) -> CalibrationRun:
    reference = measure_stream(reference_stream)
    definition = get_scenario_definition(config.scenario_name)
    specs = selected_parameter_specs(config.stages)
    fixed = {
        name: next(spec for spec in specs if spec.name == name).validate(value)
        for name, value in config.fixed_parameters.items()
    }
    initial_parameters = {spec.name: spec.default for spec in specs}
    initial_parameters.update(fixed)
    current = dict(initial_parameters)
    cache = _ReportCache(config.scenario_name, config.seconds)
    rng = SeededRng(config.search_seed)
    outcomes: list[CalibrationStageOutcome] = []

    for stage in config.stages:
        stage_objective = config.objective.through_stage(stage)
        stage_specs = tuple(
            spec
            for spec in specs
            if spec.stage == stage and spec.name not in fixed
        )
        candidates = _search_candidates(
            current,
            stage_specs,
            config.candidate_count_per_stage,
            rng,
        )
        evaluations = [
            _evaluate(
                candidate,
                config.fitting_seeds,
                reference,
                stage_objective,
                cache,
            )
            for candidate in candidates
        ]
        best_index = min(range(len(evaluations)), key=lambda index: evaluations[index].mean_loss)
        outcomes.append(
            CalibrationStageOutcome(
                stage=stage,
                fitted_parameters=tuple(spec.name for spec in stage_specs),
                candidate_count=len(candidates),
                starting_loss=evaluations[0].mean_loss,
                best_loss=evaluations[best_index].mean_loss,
            )
        )
        current = dict(evaluations[best_index].parameters)

    final_objective = config.objective.through_stage(max(config.stages))
    initial_fitting = _evaluate(
        initial_parameters,
        config.fitting_seeds,
        reference,
        final_objective,
        cache,
    )
    final_fitting = _evaluate(
        current,
        config.fitting_seeds,
        reference,
        final_objective,
        cache,
    )
    initial_heldout = _evaluate(
        initial_parameters,
        config.heldout_seeds,
        reference,
        final_objective,
        cache,
    )
    final_heldout = _evaluate(
        current,
        config.heldout_seeds,
        reference,
        final_objective,
        cache,
    )
    profile = MarketProfile(
        profile_id=config.profile_id,
        scenario_name=definition.name,
        regime=definition.regime.value,
        parameters=current,
        fixed_parameters=tuple(sorted(fixed)),
        reference_dataset_id=reference.source_id,
        objective_id=final_objective.objective_id,
    )
    return CalibrationRun(
        reference_report=reference,
        scenario_definition=definition.as_dict(),
        parameter_specs=specs,
        fixed_parameters=fixed,
        fitting_seeds=config.fitting_seeds,
        heldout_seeds=config.heldout_seeds,
        search_seed=config.search_seed,
        search_method="deterministic_bounded_discrete_search",
        objective=final_objective,
        stage_outcomes=tuple(outcomes),
        initial_fitting=initial_fitting,
        final_fitting=final_fitting,
        initial_heldout=initial_heldout,
        final_heldout=final_heldout,
        market_profile=profile,
    )


def selected_parameter_specs(stages: tuple[int, ...]) -> tuple[ParameterSpec, ...]:
    selected = set(stages)
    return tuple(spec for spec in PARAMETER_SPECS if spec.stage in selected)


def objective_loss(
    reference: CalibrationReport,
    candidate: CalibrationReport,
    objective: CalibrationObjective,
) -> float:
    total = 0.0
    total_weight = 0.0
    for term in objective.terms:
        reference_metric = reference.metric(term.metric)
        if not reference_metric.available or not _components_available(
            reference_metric,
            term.components,
        ):
            continue
        candidate_metric = candidate.metric(term.metric)
        if candidate_metric.unit != reference_metric.unit:
            raise ValueError(
                f"calibration metric unit mismatch for {term.metric}: "
                f"{reference_metric.unit!r} vs {candidate_metric.unit!r}"
            )
        distance = (
            MISSING_METRIC_PENALTY
            if not candidate_metric.available
            or not _components_available(candidate_metric, term.components)
            else _metric_distance(reference_metric, candidate_metric, term)
        )
        total += term.weight * distance
        total_weight += term.weight
    if total_weight == 0:
        raise ValueError("calibration objective has no measurements available in reference")
    return round(total / total_weight, 9)


def _evaluate(
    parameters: Mapping[str, float],
    seeds: tuple[int, ...],
    reference: CalibrationReport,
    objective: CalibrationObjective,
    cache: _ReportCache,
) -> CalibrationEvaluation:
    reports = tuple(cache.report(parameters, seed) for seed in seeds)
    losses = tuple(
        (seed, objective_loss(reference, report, objective))
        for seed, report in zip(seeds, reports)
    )
    return CalibrationEvaluation(
        parameters=dict(parameters),
        mean_loss=round(sum(loss for _, loss in losses) / len(losses), 9),
        seed_losses=losses,
        reports=reports,
    )


def _search_candidates(
    current: Mapping[str, float],
    stage_specs: tuple[ParameterSpec, ...],
    requested_count: int,
    rng: SeededRng,
) -> tuple[dict[str, float], ...]:
    candidates: list[dict[str, float]] = [dict(current)]
    seen = {_parameter_key(current)}
    for spec in stage_specs:
        for value in spec.grid:
            candidate = dict(current)
            candidate[spec.name] = value
            _append_candidate(candidates, seen, candidate, requested_count)
            if len(candidates) >= requested_count:
                return tuple(candidates)
    maximum_combinations = math.prod(len(spec.grid) for spec in stage_specs) if stage_specs else 1
    attempts = 0
    while len(candidates) < min(requested_count, maximum_combinations) and attempts < requested_count * 50:
        attempts += 1
        candidate = dict(current)
        for spec in stage_specs:
            candidate[spec.name] = spec.grid[rng.index(len(spec.grid))]
        _append_candidate(candidates, seen, candidate, requested_count)
    return tuple(candidates)


def _append_candidate(
    candidates: list[dict[str, float]],
    seen: set[tuple[tuple[str, float], ...]],
    candidate: dict[str, float],
    maximum: int,
) -> None:
    key = _parameter_key(candidate)
    if len(candidates) < maximum and key not in seen:
        candidates.append(candidate)
        seen.add(key)


def _metric_distance(
    reference: CalibrationMetric,
    candidate: CalibrationMetric,
    term: ObjectiveTerm,
) -> float:
    if term.distance is DistanceKind.SYMMETRIC_SCALAR:
        return _symmetric(_number(reference.value), _number(candidate.value))
    reference_values = _mapping(reference)
    candidate_values = _mapping(candidate)
    if term.distance is DistanceKind.QUANTILE_SHAPE:
        return sum(
            _symmetric(
                _number(reference_values[component]),
                _number(candidate_values[component]),
            )
            for component in term.components
        ) / len(term.components)
    if term.distance is DistanceKind.COMPONENT_VECTOR:
        return sum(
            _symmetric(
                _number(reference_values[component]),
                _number(candidate_values[component]),
            )
            for component in term.components
        ) / len(term.components)
    if term.distance is DistanceKind.VOLUME_PROFILE:
        reference_vector = [_number(reference_values[key]) for key in term.components]
        candidate_vector = [_number(candidate_values[key]) for key in term.components]
        reference_total = sum(reference_vector)
        candidate_total = sum(candidate_vector)
        total_distance = _symmetric(reference_total, candidate_total)
        if reference_total <= 0 or candidate_total <= 0:
            return total_distance
        shape_distance = sum(
            abs(reference_value / reference_total - candidate_value / candidate_total)
            for reference_value, candidate_value in zip(
                reference_vector,
                candidate_vector,
            )
        ) / 2.0
        return (total_distance + shape_distance) / 2.0
    raise ValueError(f"unsupported calibration distance: {term.distance}")


def _components_available(metric: CalibrationMetric, components: tuple[str, ...]) -> bool:
    if not components:
        return metric.value is not None
    if not isinstance(metric.value, dict):
        return False
    return all(
        component in metric.value and metric.value[component] is not None
        for component in components
    )


def _mapping(metric: CalibrationMetric) -> dict[str, object]:
    if not isinstance(metric.value, dict):
        raise ValueError(f"calibration metric is not structured: {metric.name}")
    return metric.value


def _number(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("calibration objective component must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("calibration objective component must be finite")
    return parsed


def _symmetric(reference: float, candidate: float) -> float:
    return 2.0 * abs(candidate - reference) / (abs(reference) + abs(candidate) + 1e-9)


def _parameter_key(parameters: Mapping[str, float]) -> tuple[tuple[str, float], ...]:
    return tuple(sorted((name, float(value)) for name, value in parameters.items()))
