"""Typed executor protocol, registry, and sole capability matrix."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from kirby2.immutable import thaw_json

from ..models import (
    CaseRecording,
    ExecutorLane,
    GeneratedCaseResult,
    GeneratedConfiguration,
)


@dataclass(frozen=True, slots=True)
class LaneCapabilitySpec:
    credited_dimensions: tuple[str, ...]
    required_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.credited_dimensions:
            raise ValueError("executor lane must credit at least one dimension")
        if len(self.credited_dimensions) != len(set(self.credited_dimensions)):
            raise ValueError("executor credited dimensions must be unique")
        if len(self.required_checks) != len(set(self.required_checks)):
            raise ValueError("executor required checks must be unique")


CAPABILITY_MATRIX: Mapping[ExecutorLane, LaneCapabilitySpec] = MappingProxyType(
    {
        ExecutorLane.CORE_FLOW: LaneCapabilitySpec(
            (
                "seed",
                "duration_us",
                "flow_model",
                "regime",
                "volume",
                "liquidity",
            ),
            (
                "quantity_conservation",
                "fifo_book_ordering",
                "non_crossed_book",
                "contiguous_sequences",
                "player_position_reconciliation",
                "player_cash_reconciliation",
                "hawkes_stability",
                "event_rate_cap",
                "observable_projection_boundary",
            ),
        ),
        ExecutorLane.MECHANICS: LaneCapabilitySpec(
            ("session_phase", "order_types", "auction_state"),
            (
                "order_lifecycle_reconciliation",
                "quantity_conservation",
                "fifo_book_ordering",
                "auction_allocation_reconciliation",
                "auction_indication_reconciliation",
                "monotonic_event_time",
            ),
        ),
        ExecutorLane.LATENCY: LaneCapabilitySpec(
            ("latency",),
            (
                "causal_timestamps",
                "async_lifecycle_reconciliation",
                "quantity_conservation",
                "terminal_cancel_fill_ordering",
                "cancel_race_reconciliation",
                "latency_metric_reconciliation",
            ),
        ),
        ExecutorLane.FRAGMENTED: LaneCapabilitySpec(
            ("hidden_liquidity", "venue_count"),
            (
                "venue_invariants",
                "global_position_reconciliation",
                "route_leg_conservation",
                "observable_quote_construction",
                "observable_projection_boundary",
                "crossed_composite_intervals_recorded",
            ),
        ),
        ExecutorLane.ECOLOGY: LaneCapabilitySpec(
            ("agent_population", "agent_count"),
            (
                "agent_risk_bounds",
                "agent_inventory_reconciliation",
                "observable_projection_boundary",
                "owned_rng_determinism",
                "monotonic_event_time",
            ),
        ),
        ExecutorLane.ALGORITHM: LaneCapabilitySpec(
            ("strategy", "objective"),
            (
                "observation_boundary",
                "objective_quantity_conservation",
                "client_venue_fill_reconciliation",
                "control_fork_identity",
                "native_recording_replay",
            ),
        ),
        ExecutorLane.FAULT: LaneCapabilitySpec(
            ("injected_fault",),
            (
                "fault_injected",
                "production_detector_exercised",
                "unrelated_invariants_survive",
            ),
        ),
    }
)


@runtime_checkable
class AuditCaseExecutor(Protocol):
    lane: ExecutorLane

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult: ...

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult: ...


class ExecutorRegistry:
    """Lane-keyed registry that accepts real executors only when registered."""

    def __init__(self) -> None:
        self._executors: dict[ExecutorLane, AuditCaseExecutor] = {}

    @property
    def registered_lanes(self) -> tuple[ExecutorLane, ...]:
        return tuple(lane for lane in ExecutorLane if lane in self._executors)

    def register(self, executor: AuditCaseExecutor) -> None:
        if not isinstance(executor, AuditCaseExecutor):
            raise TypeError("executor does not implement execute and replay")
        if not isinstance(executor.lane, ExecutorLane):
            raise TypeError("executor lane must use ExecutorLane")
        if executor.lane in self._executors:
            raise ValueError(
                f"executor lane is already registered: {executor.lane.value}"
            )
        self._executors[executor.lane] = executor

    def execute(self, configuration: GeneratedConfiguration) -> GeneratedCaseResult:
        executor = self._executor(configuration.lane)
        result = executor.execute(configuration)
        if result.configuration != configuration:
            raise RuntimeError(
                "executor returned a result for a different configuration"
            )
        return result

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not recording.expected_outputs:
            raise ValueError("executor replay requires finalized expected outputs")
        executor = self._executor(recording.lane)
        result = executor.replay(recording)
        if result.recording.sha256 != recording.sha256:
            raise RuntimeError("executor replay changed the native recording")
        return result

    def _executor(self, lane: ExecutorLane) -> AuditCaseExecutor:
        try:
            return self._executors[lane]
        except KeyError as error:
            raise LookupError(f"no real executor is registered for {lane.value}") from error


def finalize_recording(
    draft: CaseRecording,
    result_factory: Callable[[CaseRecording], GeneratedCaseResult],
) -> GeneratedCaseResult:
    """Finalize schema-v2 expectations without making the wrapper hash recursive."""

    if draft.expected_outputs:
        raise ValueError("case recording draft is already finalized")
    provisional = result_factory(draft)
    finalized = draft.with_expected_outputs(provisional.replay_expectations())
    result = result_factory(finalized)
    if thaw_json(result.replay_expectations()) != thaw_json(
        finalized.expected_outputs
    ):
        raise RuntimeError("final case recording expectations are unstable")
    return result
