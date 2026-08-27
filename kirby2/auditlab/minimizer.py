"""Deterministic delta reduction for stable violation signatures."""

from __future__ import annotations

from dataclasses import replace

from .kernel import failure_signatures, run_generated_case
from .models import GeneratedConfiguration, MinimizedFailure


def minimize_failure(
    configuration: GeneratedConfiguration,
    signature: str,
) -> MinimizedFailure:
    current = configuration
    attempts = 0
    reductions = (
        {"duration_us": 1_000},
        {"duration_events": 1},
        {"agent_count": 1},
        {"venue_count": 1},
        {"agent_population": "liquidity_provision"},
        {"flow_model": "simple"},
        {"regime": "BALANCED"},
        {"volume": "0.25x"},
        {"liquidity": "VERY_THIN"},
        {"latency": "ZERO_LATENCY"},
        {"session_phase": "CONTINUOUS"},
        {"order_types": "LIMIT_ONLY"},
        {"hidden_liquidity": "NONE"},
        {"auction_state": "NONE"},
        {"strategy": "OBSERVE"},
        {"objective": "OBSERVE_ONLY"},
    )
    for change in reductions:
        candidate = replace(current, **change)
        if candidate == current:
            continue
        attempts += 1
        if signature in failure_signatures(run_generated_case(candidate)):
            current = candidate
    final_result = run_generated_case(current)
    attempts += 1
    return MinimizedFailure(
        signature=signature,
        source_configuration_sha256=configuration.sha256,
        minimized_configuration=current,
        attempts=attempts,
        preserved=signature in failure_signatures(final_result),
        result_digest=final_result.result_sha256,
    )
