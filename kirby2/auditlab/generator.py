"""Coverage-oriented deterministic generation of audit configurations."""

from __future__ import annotations

import math

from kirby2.agents import POPULATION_IDS
from kirby2.exchange import SessionState
from kirby2.latency import LatencyProfileName
from kirby2.session.objectives import ObjectiveType
from kirby2.simulation import LiquidityPreset, Regime, VolumePreset

from .models import FaultKind, GeneratedConfiguration


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


def generate_configurations(seed: int, budget: int) -> tuple[GeneratedConfiguration, ...]:
    if type(seed) is not int or seed < 0:
        raise ValueError("audit-lab seed must be a nonnegative integer")
    if type(budget) is not int or budget <= 0:
        raise ValueError("audit-lab budget must be positive")
    primes = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)
    configs: list[GeneratedConfiguration] = []
    for index in range(budget):
        values = {}
        for axis_index, (name, options) in enumerate(AXES.items()):
            offset = _mix(seed, axis_index) % len(options)
            step = 1
            if len(options) > 1:
                step = 1 + primes[axis_index] % (len(options) - 1)
                while math.gcd(step, len(options)) != 1:
                    step = 1 + (step % (len(options) - 1))
            values[name] = options[(offset + index * step) % len(options)]
        fault_options: tuple[FaultKind | None, ...] = (None, *tuple(FaultKind))
        fault = fault_options[(_mix(seed, 31) + index) % len(fault_options)]
        configs.append(
            GeneratedConfiguration(
                sequence=index + 1,
                seed=_mix(seed, index + 101) & 0x7FFF_FFFF,
                duration_us=8_000 + ((_mix(seed, index + 181) + index) % 25) * 1_000,
                duration_events=4 + ((_mix(seed, index + 211) + index) % 9),
                agent_count=1 + ((_mix(seed, index + 241) + index) % 8),
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


def coverage_report(configurations: tuple[GeneratedConfiguration, ...]) -> dict[str, object]:
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
        config.injected_fault for config in configurations if config.injected_fault is not None
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
