"""Coverage-oriented deterministic generation of audit configurations."""

from __future__ import annotations

import math
from enum import Enum

from kirby2.agents import POPULATION_IDS
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
    "strategy": ("PASSIVE", "AGGRESSIVE", "ADAPTIVE", "OBSERVE"),
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
            coverage_index = cell_index * len(_LANES) + lane_ordinal
        else:
            cell_index = lane_index // 6
            replicate_index = lane_index % 6
            partition = (
                ExperimentPartition.TRAIN
                if replicate_index < 3
                else ExperimentPartition.HOLDOUT
            )
            fault = None
            scientific_ordinal = _SCIENTIFIC_LANES.index(lane)
            coverage_index = (
                cell_index * len(_SCIENTIFIC_LANES) + scientific_ordinal
            )
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


def coverage_report(configurations: tuple[GeneratedConfiguration, ...]) -> dict[str, object]:
    """Legacy declaration coverage retained only until the ATR-13 cutover."""

    report: dict[str, object] = {}
    seeds = {config.seed for config in configurations}
    report["seed"] = {
        "expected": "more than one explicitly owned seed",
        "observed_count": len(seeds),
        "status": "PASS" if len(seeds) > 1 else "PARTIAL",
    }
    for name, options in AXES.items():
        observed = {getattr(config, name) for config in configurations}
        expected = set(options)
        report[name] = {
            "expected": sorted(str(value) for value in expected),
            "observed": sorted(str(value) for value in observed),
            "status": "PASS" if observed == expected else "PARTIAL",
        }
    observed_faults = {
        config.injected_fault
        for config in configurations
        if config.injected_fault is not None
    }
    report["faults"] = {
        "expected": sorted(item.value for item in FaultKind),
        "observed": sorted(item.value for item in observed_faults),
        "status": "PASS" if observed_faults == set(FaultKind) else "PARTIAL",
    }
    agent_counts = {config.agent_count for config in configurations}
    report["agent_count_reduction_axis"] = {
        "expected": list(range(1, 9)),
        "observed": sorted(agent_counts),
        "status": "PASS" if agent_counts == set(range(1, 9)) else "PARTIAL",
    }
    return report


def evidence_coverage_report(
    results: tuple[GeneratedCaseResult, ...],
) -> dict[str, object]:
    """Credit configured values only when a real executor reports exercise."""

    lanes: dict[str, object] = {}
    overall_passed = True
    for lane, capability in CAPABILITY_MATRIX.items():
        lane_results = tuple(result for result in results if result.lane is lane)
        dimensions: dict[str, object] = {}
        for dimension in capability.credited_dimensions:
            configured: dict[str, object] = {}
            exercised: set[str] = set()
            mismatched_records = 0
            for result in lane_results:
                value = _configuration_dimension(result.configuration, dimension)
                key = canonical_json(value)
                configured[key] = value
                for record in result.exercises:
                    if (
                        record.capability != dimension
                        or record.status is not ExerciseStatus.EXERCISED
                    ):
                        continue
                    record_key = canonical_json(record.configured_value)
                    if record_key == key:
                        exercised.add(key)
                    else:
                        mismatched_records += 1
            missing = sorted(set(configured).difference(exercised))
            status = "PASS" if configured and not missing else "PARTIAL"
            overall_passed = overall_passed and status == "PASS"
            dimensions[dimension] = {
                "configured_values": [configured[key] for key in sorted(configured)],
                "exercised_values": [configured[key] for key in sorted(exercised)],
                "mismatched_record_count": mismatched_records,
                "missing_values": [configured[key] for key in missing],
                "status": status,
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
            status = "PASS" if exercise_count else "PARTIAL"
            overall_passed = overall_passed and status == "PASS"
            checks[name] = {
                "exercise_count": exercise_count,
                "failed_count": failed_count,
                "not_exercised_count": sum(
                    check.status is CheckStatus.NOT_EXERCISED for check in reported
                ),
                "reported_count": len(reported),
                "status": status,
            }
        lane_passed = all(
            item["status"] == "PASS"
            for group in (dimensions, checks)
            for item in group.values()
        )
        lanes[lane.value] = {
            "case_count": len(lane_results),
            "checks": checks,
            "dimensions": dimensions,
            "status": "PASS" if lane_passed else "PARTIAL",
        }
    return {
        "lane_count": len(lanes),
        "lanes": lanes,
        "result_count": len(results),
        "status": "PASS" if overall_passed else "PARTIAL",
    }


def _configuration_dimension(
    configuration: GeneratedConfiguration,
    dimension: str,
) -> object:
    value = getattr(configuration, dimension)
    return value.value if isinstance(value, Enum) else value


def minimum_full_coverage_budget(seed: int, limit: int = 10_000) -> int | None:
    for budget in range(1, limit + 1):
        report = coverage_report(generate_configurations(seed, budget))
        if all(item["status"] == "PASS" for item in report.values()):
            return budget
    return None


def _mix(seed: int, lane: int) -> int:
    value = (seed + 0x9E3779B97F4A7C15 + lane * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
    return value ^ (value >> 31)
