"""Accepted deterministic regime-scenario audit command support."""

from __future__ import annotations

from dataclasses import dataclass

from kirby2.scenarios.market import (
    evaluate_behavioral_envelope,
    load_scenario_definitions,
    run_market_scenario,
)


@dataclass(frozen=True, slots=True)
class ScenarioAuditReport:
    name: str
    digest: str
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def audit_accepted_scenarios() -> tuple[ScenarioAuditReport, ...]:
    reports: list[ScenarioAuditReport] = []
    for name, definition in load_scenario_definitions().items():
        run = run_market_scenario(definition)
        metrics = run.metrics()
        digest = run.simulation.replay_sha256()
        failures: list[str] = []
        if digest != definition.accepted_replay_sha256:
            failures.append(
                f"digest mismatch: expected {definition.accepted_replay_sha256}, got {digest}"
            )
        if metrics["invariant_status"] != "PASS":
            failures.append("runtime invariants did not pass")
        failures.extend(evaluate_behavioral_envelope(definition, metrics))
        if definition.regime.value in run.replay_json_lines().upper():
            failures.append("hidden regime label leaked into raw replay stream")
        reports.append(ScenarioAuditReport(name, digest, tuple(failures)))
    return tuple(reports)

