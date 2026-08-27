"""Coverage-oriented deterministic generation of audit configurations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import Enum
from itertools import combinations

from kirby2.agents import POPULATION_IDS
from kirby2.algorithms.models import AlgorithmName
from kirby2.exchange import SessionState
from kirby2.latency import LatencyProfileName
from kirby2.session.objectives import ObjectiveType
from kirby2.simulation import LiquidityPreset, Regime, VolumePreset

from .executors.base import CAPABILITY_MATRIX
from .models import (
    CheckStatus,
    ExecutorLane,
    ExperimentPartition,
    ExerciseStatus,
    FaultKind,
    GeneratedCaseResult,
    GeneratedConfiguration,
    canonical_json,
)


AXES: dict[str, tuple[object, ...]] = {
    "flow_model": ("simple", "hawkes"),
    "regime": tuple(item.value for item in Regime),
    "volume": tuple(item.value for item in VolumePreset),
    "liquidity": tuple(item.value for item in LiquidityPreset),
    "latency": tuple(item.value for item in LatencyProfileName),
    "session_phase": tuple(item.value for item in SessionState),
    "order_types": (
        "LIMIT_ONLY",
        "MARKET_AND_LIMIT",
        "IOC_FOK_POST_ONLY",
        "CANCEL_REPLACE",
    ),
    "hidden_liquidity": ("NONE", "ICEBERG", "HIDDEN_MIDPOINT"),
    "venue_count": (1, 2, 3, 4),
    "auction_state": ("NONE", "OPENING", "REOPENING", "CLOSING"),
    "agent_population": tuple(POPULATION_IDS),
    "strategy": tuple(
        item.value
        for item in AlgorithmName
        if item is not AlgorithmName.MANUAL_REPLAY
    ),
    "objective": tuple(item.value for item in ObjectiveType),
}

_LANES = tuple(ExecutorLane)
_SCIENTIFIC_LANES = tuple(
    lane for lane in ExecutorLane if lane is not ExecutorLane.FAULT
)
_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)


def generate_configurations(
    seed: int,
    budget: int,
) -> tuple[GeneratedConfiguration, ...]:
    if type(seed) is not int or seed < 0:
        raise ValueError("audit-lab seed must be a nonnegative integer")
    if type(budget) is not int or budget <= 0:
        raise ValueError("audit-lab budget must be positive")
    configs: list[GeneratedConfiguration] = []
    lane_indices = {lane: 0 for lane in _LANES}
    seed_base = (_mix(seed, 9_001) & 0x7FFF_FFFF) << 32
    for index in range(budget):
        lane = _LANES[index % len(_LANES)]
        lane_index = lane_indices[lane]
        lane_indices[lane] += 1
        lane_ordinal = _LANES.index(lane)
        if lane is ExecutorLane.FAULT:
            cell_index = lane_index // len(FaultKind)
            replicate_index = lane_index % len(FaultKind)
            partition = ExperimentPartition.FAULT
            fault = tuple(FaultKind)[replicate_index]
            coverage_index = lane_ordinal * len(_SCIENTIFIC_LANES) + cell_index
        else:
            cell_index = lane_index // 6
            replicate_index = lane_index % 6
            partition = (
                ExperimentPartition.TRAIN
                if replicate_index < 3
                else ExperimentPartition.HOLDOUT
            )
            fault = None
            coverage_index = lane_ordinal * len(_SCIENTIFIC_LANES) + cell_index
        values = _axis_values(seed, coverage_index)
        cell_token = _mix(seed, 1_000 + lane_ordinal) & 0xFFFF_FFFF
        configs.append(
            GeneratedConfiguration(
                sequence=index + 1,
                lane=lane,
                cell_id=(
                    f"{lane.value.lower()}-{cell_index:08d}-{cell_token:08x}"
                ),
                replicate_index=replicate_index,
                partition=partition,
                seed=seed_base + index + 1,
                duration_us=(
                    8_000
                    + (
                        (_mix(seed, coverage_index + 181) + coverage_index) % 25
                    )
                    * 1_000
                ),
                duration_events=(
                    4 + ((_mix(seed, coverage_index + 211) + coverage_index) % 9)
                ),
                agent_count=(
                    1 + ((_mix(seed, coverage_index + 241) + coverage_index) % 8)
                ),
                flow_model=str(values["flow_model"]),
                regime=str(values["regime"]),
                volume=str(values["volume"]),
                liquidity=str(values["liquidity"]),
                latency=str(values["latency"]),
                session_phase=str(values["session_phase"]),
                order_types=str(values["order_types"]),
                hidden_liquidity=str(values["hidden_liquidity"]),
                venue_count=int(values["venue_count"]),
                auction_state=str(values["auction_state"]),
                agent_population=str(values["agent_population"]),
                strategy=str(values["strategy"]),
                objective=str(values["objective"]),
                injected_fault=fault,
            )
        )
    return tuple(configs)


def derive_calibration_seeds(master_seed: int) -> dict[str, object]:
    """Derive disjoint calibration roles from one explicitly owned seed."""

    if type(master_seed) is not int or master_seed < 0:
        raise ValueError("calibration master seed must be nonnegative")
    candidates: list[int] = []
    used: set[int] = set()
    for ordinal in range(6):
        candidate = _mix(master_seed, 50_000 + ordinal) & 0x7FFF_FFFF
        while candidate in used:
            candidate = (candidate + 1) & 0x7FFF_FFFF
        used.add(candidate)
        candidates.append(candidate)
    return {
        "fitting_seeds": candidates[:2],
        "heldout_seeds": candidates[2:4],
        "reference_seed": candidates[4],
        "search_seed": candidates[5],
    }


def scientific_match_parameters(
    configuration: Mapping[str, object],
) -> dict[str, object]:
    """Return exactly the non-seed parameters exercised by a scientific lane."""

    raw_lane = configuration.get("lane")
    if type(raw_lane) is not str:
        raise TypeError("statistical configuration lane must be a string")
    lane = ExecutorLane(raw_lane)
    if lane is ExecutorLane.FAULT:
        raise ValueError("fault configurations do not form scientific cells")
    dimensions = tuple(
        name
        for name in CAPABILITY_MATRIX[lane].credited_dimensions
        if name != "seed"
    )
    missing = [name for name in dimensions if name not in configuration]
    if missing:
        raise ValueError(
            f"statistical configuration is missing exercised fields: {missing}"
        )
    return {
        "lane": lane.value,
        **{name: configuration[name] for name in dimensions},
    }


def _axis_values(seed: int, cell_index: int) -> dict[str, object]:
    values: dict[str, object] = {}
    for axis_index, (name, options) in enumerate(AXES.items()):
        offset = _mix(seed, axis_index) % len(options)
        step = 1
        if len(options) > 1:
            step = 1 + _PRIMES[axis_index] % (len(options) - 1)
            while math.gcd(step, len(options)) != 1:
                step = 1 + (step % (len(options) - 1))
        values[name] = options[(offset + cell_index * step) % len(options)]
    return values


def evidence_coverage_report(
    results: tuple[GeneratedCaseResult, ...],
) -> dict[str, object]:
    """Credit configured values only when a real executor reports exercise."""

    lanes: dict[str, object] = {}
    overall_passed = True
    missing_configured_value_count = 0
    unexercised_required_check_count = 0
    failed_required_check_count = 0
    for lane, capability in CAPABILITY_MATRIX.items():
        lane_results = tuple(result for result in results if result.lane is lane)
        dimensions: dict[str, object] = {}
        for dimension in capability.credited_dimensions:
            configured: dict[str, object] = {}
            exercised: set[str] = set()
            mismatched_records = 0
            unexercised_case_count = 0
            for result in lane_results:
                value = _configuration_dimension(result.configuration, dimension)
                key = canonical_json(value)
                configured[key] = value
                exact_exercise = False
                for record in result.exercises:
                    if (
                        record.capability != dimension
                        or record.status is not ExerciseStatus.EXERCISED
                    ):
                        continue
                    record_key = canonical_json(record.configured_value)
                    if record_key == key:
                        exercised.add(key)
                        exact_exercise = True
                    else:
                        mismatched_records += 1
                unexercised_case_count += not exact_exercise
            missing = sorted(set(configured).difference(exercised))
            missing_configured_value_count += len(missing)
            status = (
                "PASS"
                if configured
                and not missing
                and mismatched_records == 0
                and unexercised_case_count == 0
                else "PARTIAL"
            )
            overall_passed = overall_passed and status == "PASS"
            dimensions[dimension] = {
                "configured_values": [configured[key] for key in sorted(configured)],
                "exercised_values": [configured[key] for key in sorted(exercised)],
                "mismatched_record_count": mismatched_records,
                "missing_values": [configured[key] for key in missing],
                "status": status,
                "unexercised_case_count": unexercised_case_count,
            }

        checks: dict[str, object] = {}
        for name in capability.required_checks:
            reported = [
                check
                for result in lane_results
                for check in result.checks
                if check.name == name
            ]
            exercise_count = sum(
                check.status is not CheckStatus.NOT_EXERCISED for check in reported
            )
            failed_count = sum(check.status is CheckStatus.FAIL for check in reported)
            not_exercised_count = sum(
                check.status is CheckStatus.NOT_EXERCISED for check in reported
            )
            missing_case_count = len(lane_results) - len(reported)
            unexercised_required_check_count += (
                not_exercised_count + missing_case_count
            )
            failed_required_check_count += failed_count
            status = (
                "PASS"
                if lane_results
                and len(reported) == len(lane_results)
                and exercise_count == len(lane_results)
                and failed_count == 0
                else "PARTIAL"
            )
            overall_passed = overall_passed and status == "PASS"
            checks[name] = {
                "exercise_count": exercise_count,
                "failed_count": failed_count,
                "missing_case_count": missing_case_count,
                "not_exercised_count": not_exercised_count,
                "reported_count": len(reported),
                "status": status,
            }
        supported_pairs: dict[str, object] = {}
        for left, right in combinations(capability.credited_dimensions, 2):
            configured_pairs: dict[str, tuple[object, object]] = {}
            exercised_pairs: set[str] = set()
            for result in lane_results:
                pair = (
                    _configuration_dimension(result.configuration, left),
                    _configuration_dimension(result.configuration, right),
                )
                key = canonical_json(pair)
                configured_pairs[key] = pair
                successful = {
                    record.capability: canonical_json(record.configured_value)
                    for record in result.exercises
                    if record.status is ExerciseStatus.EXERCISED
                    and record.capability in {left, right}
                }
                if successful == {
                    left: canonical_json(pair[0]),
                    right: canonical_json(pair[1]),
                }:
                    exercised_pairs.add(key)
            missing_pairs = sorted(
                set(configured_pairs).difference(exercised_pairs)
            )
            pair_status = (
                "PASS"
                if configured_pairs and not missing_pairs
                else "PARTIAL"
            )
            overall_passed = overall_passed and pair_status == "PASS"
            supported_pairs[f"{left}__{right}"] = {
                "configured_pair_count": len(configured_pairs),
                "exercised_pair_count": len(exercised_pairs),
                "missing_pairs": [
                    list(configured_pairs[key]) for key in missing_pairs
                ],
                "status": pair_status,
            }
        lane_passed = all(
            item["status"] == "PASS"
            for group in (dimensions, checks, supported_pairs)
            for item in group.values()
        )
        lanes[lane.value] = {
            "case_count": len(lane_results),
            "checks": checks,
            "dimensions": dimensions,
            "supported_within_lane_pairs": supported_pairs,
            "status": "PASS" if lane_passed else "PARTIAL",
        }
    return {
        "covered_lane_count": sum(
            item["status"] == "PASS" for item in lanes.values()
        ),
        "failed_required_check_count": failed_required_check_count,
        "lane_count": len(lanes),
        "lanes": lanes,
        "missing_configured_value_count": missing_configured_value_count,
        "result_count": len(results),
        "status": "PASS" if overall_passed else "PARTIAL",
        "unexercised_required_check_count": unexercised_required_check_count,
        "unsupported_cross_lane_interactions": {
            "credited_pair_count": 0,
            "pairs": [],
            "status": "ABSENT_BY_DESIGN",
        },
    }


def _configuration_dimension(
    configuration: GeneratedConfiguration,
    dimension: str,
) -> object:
    value = getattr(configuration, dimension)
    return value.value if isinstance(value, Enum) else value


def _mix(seed: int, lane: int) -> int:
    value = (seed + 0x9E3779B97F4A7C15 + lane * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
    return value ^ (value >> 31)
