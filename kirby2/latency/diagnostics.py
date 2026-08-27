"""Stable diagnostics derived from real asynchronous execution state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.immutable import freeze_json, thaw_json

from .models import AsyncOrder, LatencyMetrics


@dataclass(frozen=True, slots=True)
class LatencyDiagnostic:
    """A machine-readable finding emitted by a latency production gate."""

    code: str
    gate: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.code or not self.gate:
            raise ValueError("latency diagnostic requires a code and gate")
        frozen = freeze_json(self.evidence)
        if not isinstance(frozen, Mapping) or not frozen:
            raise ValueError("latency diagnostic requires source evidence")
        object.__setattr__(self, "evidence", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "evidence": thaw_json(self.evidence),
            "gate": self.gate,
        }


def acknowledgement_budget_diagnostic(
    metrics: LatencyMetrics,
    *,
    budget_us: int,
) -> LatencyDiagnostic | None:
    """Classify a real venue acknowledgement that exceeded its declared budget."""

    if not isinstance(metrics, LatencyMetrics):
        raise TypeError("acknowledgement diagnostic requires LatencyMetrics")
    if type(budget_us) is not int or budget_us < 0:
        raise ValueError("acknowledgement budget must be nonnegative microseconds")
    observed_us = metrics.send_to_ack_latency_us
    if observed_us is None or observed_us <= budget_us:
        return None
    return LatencyDiagnostic(
        code="ACK_LATENCY_BUDGET_EXCEEDED",
        gate="AsynchronousAcknowledgementBudgetGate",
        evidence={
            "budget_us": budget_us,
            "metrics": metrics.as_dict(),
            "observed_us": observed_us,
            "order_id": metrics.order_id,
        },
    )


def terminal_race_diagnostic(order: AsyncOrder) -> LatencyDiagnostic | None:
    """Classify a terminal outcome recorded by the asynchronous race engine."""

    if not isinstance(order, AsyncOrder):
        raise TypeError("terminal-race diagnostic requires AsyncOrder")
    outcome = order.cancel_race_outcome
    if outcome not in {
        "CANCEL_WON",
        "FILL_BEFORE_CANCEL",
        "PARTIAL_FILL_THEN_CANCELLED",
    }:
        return None
    if not order.state.terminal:
        return None
    return LatencyDiagnostic(
        code="TERMINAL_RACE_CLASSIFIED",
        gate="AsynchronousTerminalRaceGate",
        evidence={
            "order": order.as_dict(),
            "outcome": outcome,
            "terminal_state": order.state.value,
        },
    )
