"""Deterministic passive and forked strategy-experiment runner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Iterable

from kirby2 import __version__
from kirby2.exchange import Side
from kirby2.scenarios import get_scenario_definition
from kirby2.session.bindings import BindingMap
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import DEFAULT_QUANTITIES, LiveMarketSession, SessionSnapshot
from kirby2.session.objectives import ObjectiveType, SessionObjective
from kirby2.session.replay import SessionRecording, replay_recording
from kirby2.strategy import TrafficState, parse_strategy

from .models import ExperimentManifest, ExperimentMode, StrategyVariant


EXPERIMENT_RESULT_SCHEMA_VERSION = 1
EXPERIMENT_SOFTWARE_VERSION = f"kirby2-{__version__}+strategy-experiment-v1"


@dataclass(frozen=True, slots=True)
class StrategyRunMetrics:
    entry_time_us: int | None
    entry_delay_from_fork_us: int | None
    exit_time_us: int | None
    holding_time_us: int | None
    trade_count: int
    filled_quantity: int
    fill_quality_spread_paid_ticks: Decimal | None
    implementation_shortfall_tick_shares: Decimal | None
    adverse_selection_ticks: Decimal | None
    completion_percentage: Decimal | None
    completed: bool | None
    realized_pnl_tick_shares: Decimal | None
    discipline_violations: int
    ending_position: int

    def as_dict(self) -> dict[str, object]:
        return {
            "adverse_selection_ticks": _decimal_text(self.adverse_selection_ticks),
            "completed": self.completed,
            "completion_percentage": _decimal_text(self.completion_percentage),
            "discipline_violations": self.discipline_violations,
            "ending_position": self.ending_position,
            "entry_delay_from_fork_us": self.entry_delay_from_fork_us,
            "entry_time_us": self.entry_time_us,
            "exit_time_us": self.exit_time_us,
            "fill_quality_spread_paid_ticks": _decimal_text(
                self.fill_quality_spread_paid_ticks
            ),
            "filled_quantity": self.filled_quantity,
            "holding_time_us": self.holding_time_us,
            "implementation_shortfall_tick_shares": _decimal_text(
                self.implementation_shortfall_tick_shares
            ),
            "realized_pnl_tick_shares": _decimal_text(
                self.realized_pnl_tick_shares
            ),
            "trade_count": self.trade_count,
        }


@dataclass(frozen=True, slots=True)
class StrategyRunResult:
    scenario_name: str
    seed: int
    variant_name: str
    metrics: StrategyRunMetrics
    market_path_sha256: str
    state_sha256: str
    timeline_sha256: str
    replay_verified: bool
    recording: SessionRecording

    @property
    def recording_relative_path(self) -> str:
        return (
            f"recordings/{self.scenario_name}/seed-{self.seed}/"
            f"{self.variant_name}.json"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "market_path_sha256": self.market_path_sha256,
            "metrics": self.metrics.as_dict(),
            "recording": self.recording_relative_path,
            "replay_verified": self.replay_verified,
            "scenario_name": self.scenario_name,
            "seed": self.seed,
            "state_sha256": self.state_sha256,
            "timeline_sha256": self.timeline_sha256,
            "variant_name": self.variant_name,
        }


@dataclass(frozen=True, slots=True)
class TrafficAgreement:
    scenario_name: str
    seed: int
    fork_state_sha256: str
    pre_fork_samples: int
    post_fork_samples: int
    pre_fork_unanimous: int
    post_fork_unanimous: int
    pairwise: dict[str, dict[str, int]]

    def as_dict(self) -> dict[str, object]:
        return {
            "fork_state_sha256": self.fork_state_sha256,
            "pairwise": {
                key: dict(value) for key, value in sorted(self.pairwise.items())
            },
            "post_fork_samples": self.post_fork_samples,
            "post_fork_unanimous_ratio": _ratio_text(
                self.post_fork_unanimous,
                self.post_fork_samples,
            ),
            "pre_fork_samples": self.pre_fork_samples,
            "pre_fork_unanimous_ratio": _ratio_text(
                self.pre_fork_unanimous,
                self.pre_fork_samples,
            ),
            "scenario_name": self.scenario_name,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class StrategyExperimentResult:
    manifest: ExperimentManifest
    runs: tuple[StrategyRunResult, ...]
    traffic_agreements: tuple[TrafficAgreement, ...]
    aggregate_by_variant: tuple[dict[str, object], ...]
    aggregate_by_scenario: tuple[dict[str, object], ...]
    pairwise_rule_deltas: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        expected = self.manifest.comparison_count * len(self.manifest.strategies)
        if len(self.runs) != expected:
            raise ValueError("experiment result does not contain every controlled run")
        if len(self.traffic_agreements) != self.manifest.comparison_count:
            raise ValueError("experiment result lacks traffic-agreement cells")
        if not all(run.replay_verified for run in self.runs):
            raise ValueError("experiment result contains an unverified replay")

    @property
    def winner_declaration(self) -> dict[str, object]:
        reason = (
            "INSUFFICIENT_SCENARIOS: one scenario cannot support a winner claim"
            if not self.manifest.winner_eligible
            else "DESCRIPTIVE_ONLY: paired evidence is reported without automatic winner selection"
        )
        return {"reason": reason, "status": "NOT_DECLARED", "winner": None}

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregate_by_scenario": list(self.aggregate_by_scenario),
            "aggregate_by_variant": list(self.aggregate_by_variant),
            "manifest": self.manifest.as_dict(),
            "manifest_sha256": self.manifest.sha256,
            "market_path_control": {
                "fork_cells_verified": len(self.traffic_agreements),
                "mode": self.manifest.mode.value,
                "status": "PASS",
            },
            "pairwise_rule_deltas": list(self.pairwise_rule_deltas),
            "per_seed": [run.as_dict() for run in self.runs],
            "recording_count": len(self.runs),
            "recordings_complete": True,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "software_version": EXPERIMENT_SOFTWARE_VERSION,
            "traffic_agreement": [
                agreement.as_dict() for agreement in self.traffic_agreements
            ],
            "winner_declaration": self.winner_declaration,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=False)
        _write_json(directory / "manifest.resolved.json", self.manifest.as_dict())
        for run in self.runs:
            run.recording.save(directory / run.recording_relative_path)
        _write_json(directory / "result.json", self.as_dict())
        return directory

    def render(self) -> str:
        lines = [
            "KIRBY2_STRATEGY_EXPERIMENT",
            (
                f"MANIFEST id={self.manifest.experiment_id} "
                f"mode={self.manifest.mode.value} "
                f"scenarios={len(self.manifest.scenario_names)} "
                f"seeds={len(self.manifest.seeds)} "
                f"strategies={len(self.manifest.strategies)}"
            ),
            (
                "MARKET_PATH_CONTROL PASS "
                f"verified_forks={len(self.traffic_agreements)}"
            ),
        ]
        lines.extend(
            "PER_SEED " + json.dumps(run.as_dict(), sort_keys=True, separators=(",", ":"))
            for run in self.runs
        )
        lines.extend(
            "AGGREGATE " + json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in self.aggregate_by_variant
        )
        lines.extend(
            "RULE_DELTA " + json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in self.pairwise_rule_deltas
        )
        lines.extend(
            (
                "WINNER none " + str(self.winner_declaration["reason"]),
                f"REPLAYS PASS count={len(self.runs)}",
                f"RESULT_SHA256 {self.sha256}",
                "EXPERIMENT PASS",
            )
        )
        return "\n".join(lines)


@dataclass(slots=True)
class _PolicyState:
    entry_attempted: bool = False
    exit_attempted: bool = False
    entry_time_us: int | None = None
    exit_time_us: int | None = None
    discipline_violations: int = 0


class _AgreementAccumulator:
    def __init__(self, variant_names: tuple[str, ...]) -> None:
        self.variant_names = variant_names
        self.pre_samples = 0
        self.post_samples = 0
        self.pre_unanimous = 0
        self.post_unanimous = 0
        self.pairwise: dict[str, dict[str, int]] = {
            _pair_key(first, second): {
                "post_fork_matches": 0,
                "post_fork_samples": 0,
                "pre_fork_matches": 0,
                "pre_fork_samples": 0,
            }
            for first, second in combinations(variant_names, 2)
        }

    def observe(self, states: dict[str, str], *, pre_fork: bool) -> None:
        if set(states) != set(self.variant_names):
            raise ValueError("traffic agreement sample lacks a strategy")
        if pre_fork:
            self.pre_samples += 1
            self.pre_unanimous += len(set(states.values())) == 1
        else:
            self.post_samples += 1
            self.post_unanimous += len(set(states.values())) == 1
        for first, second in combinations(self.variant_names, 2):
            prefix = "pre_fork" if pre_fork else "post_fork"
            counts = self.pairwise[_pair_key(first, second)]
            counts[f"{prefix}_samples"] += 1
            counts[f"{prefix}_matches"] += states[first] == states[second]


def run_strategy_experiment(
    manifest: ExperimentManifest,
) -> StrategyExperimentResult:
    runs: list[StrategyRunResult] = []
    agreements: list[TrafficAgreement] = []
    for scenario_name in manifest.scenario_names:
        definition = get_scenario_definition(scenario_name)
        for seed in manifest.seeds:
            cell_runs, agreement = _run_controlled_cell(
                manifest,
                definition.name,
                seed,
            )
            runs.extend(cell_runs)
            agreements.append(agreement)
    captured = tuple(runs)
    return StrategyExperimentResult(
        manifest=manifest,
        runs=captured,
        traffic_agreements=tuple(agreements),
        aggregate_by_variant=_aggregate_variants(manifest, captured),
        aggregate_by_scenario=_aggregate_scenarios(manifest, captured),
        pairwise_rule_deltas=_pairwise_deltas(manifest, captured, tuple(agreements)),
    )


def _run_controlled_cell(
    manifest: ExperimentManifest,
    scenario_name: str,
    seed: int,
) -> tuple[tuple[StrategyRunResult, ...], TrafficAgreement]:
    sessions: dict[str, LiveMarketSession] = {}
    policy: dict[str, _PolicyState] = {}
    quantity_options = tuple(sorted(set((*DEFAULT_QUANTITIES, manifest.quantity))))
    for variant in manifest.strategies:
        objective = (
            SessionObjective.observe_only(manifest.duration_us // 1_000_000)
            if manifest.mode is ExperimentMode.PASSIVE_OBSERVER
            else SessionObjective(
                ObjectiveType.ROUND_TRIP,
                manifest.quantity,
                manifest.duration_us,
                preferred_slippage_ticks=10_000,
            )
        )
        session = LiveMarketSession(
            get_scenario_definition(scenario_name),
            seed=seed,
            duration_seconds=manifest.duration_us // 1_000_000,
            initial_quantity=manifest.quantity,
            quantity_options=quantity_options,
            strategy_definition=parse_strategy(variant.source),
            objective=objective,
        )
        session.start()
        sessions[variant.name] = session
        policy[variant.name] = _PolicyState()

    names = tuple(variant.name for variant in manifest.strategies)
    agreement = _AgreementAccumulator(names)
    initial_states = {
        name: session.snapshot().traffic_light for name, session in sessions.items()
    }
    agreement.observe(initial_states, pre_fork=True)
    initial_digests = {_market_path_sha256(session) for session in sessions.values()}
    if len(initial_digests) != 1:
        raise RuntimeError("strategy sessions did not start from one market state")
    fork_state_sha256 = next(iter(initial_digests)) if manifest.fork_time_us == 0 else ""
    if manifest.mode is ExperimentMode.FORKED_EXECUTION and manifest.fork_time_us == 0:
        _apply_execution_policies(manifest, sessions, policy)

    current_time_us = 0
    while current_time_us < manifest.duration_us:
        target_time_us = min(
            manifest.duration_us,
            current_time_us + manifest.decision_interval_us,
        )
        if current_time_us < manifest.fork_time_us < target_time_us:
            target_time_us = manifest.fork_time_us
        delta_us = target_time_us - current_time_us
        for session in sessions.values():
            session.advance_by(delta_us)
        current_time_us = target_time_us
        pre_fork = current_time_us <= manifest.fork_time_us
        states = {
            name: session.snapshot().traffic_light
            for name, session in sessions.items()
        }
        agreement.observe(states, pre_fork=pre_fork)
        if manifest.mode is ExperimentMode.PASSIVE_OBSERVER or pre_fork:
            controlled_digests = {
                _market_path_sha256(session) for session in sessions.values()
            }
            if len(controlled_digests) != 1:
                raise RuntimeError("controlled strategies no longer share one market path")
            if current_time_us == manifest.fork_time_us:
                fork_state_sha256 = next(iter(controlled_digests))
        if (
            manifest.mode is ExperimentMode.FORKED_EXECUTION
            and current_time_us >= manifest.fork_time_us
            and current_time_us < manifest.duration_us
        ):
            _apply_execution_policies(manifest, sessions, policy)

    if not fork_state_sha256:
        raise RuntimeError("experiment never captured its controlled fork state")
    layout = HotkeyLayout.default()
    results: list[StrategyRunResult] = []
    for variant in manifest.strategies:
        session = sessions[variant.name]
        session.engine.book.assert_invariants()
        recording = SessionRecording.capture(session, layout, auto_start=True)
        replay = replay_recording(recording)
        if not replay.passed:
            raise RuntimeError(
                f"strategy replay failed for {scenario_name}/{seed}/{variant.name}"
            )
        results.append(
            StrategyRunResult(
                scenario_name,
                seed,
                variant.name,
                _run_metrics(manifest, session, policy[variant.name]),
                _market_path_sha256(session),
                session.state_sha256(),
                session.timeline_sha256(),
                True,
                recording,
            )
        )
    return tuple(results), TrafficAgreement(
        scenario_name=scenario_name,
        seed=seed,
        fork_state_sha256=fork_state_sha256,
        pre_fork_samples=agreement.pre_samples,
        post_fork_samples=agreement.post_samples,
        pre_fork_unanimous=agreement.pre_unanimous,
        post_fork_unanimous=agreement.post_unanimous,
        pairwise=agreement.pairwise,
    )


def _apply_execution_policies(
    manifest: ExperimentManifest,
    sessions: dict[str, LiveMarketSession],
    policies: dict[str, _PolicyState],
) -> None:
    for name, session in sessions.items():
        state = policies[name]
        snapshot = session.snapshot()
        if not state.entry_attempted and _entry_allowed(snapshot):
            state.entry_attempted = True
            outcome = session.handle_input(
                _entry_key(manifest.entry_side),
                BindingMap.default(),
            )
            if outcome.accepted:
                state.entry_time_us = session.simulation_time_us
            else:
                state.discipline_violations += 1
            continue
        if (
            state.entry_attempted
            and not state.exit_attempted
            and snapshot.position != 0
            and _exit_allowed(snapshot, manifest.exit_signals)
        ):
            state.exit_attempted = True
            side = Side.SELL if snapshot.position > 0 else Side.BUY
            outcome = session.handle_input(_entry_key(side), BindingMap.default())
            if outcome.accepted:
                state.exit_time_us = session.simulation_time_us
            else:
                state.discipline_violations += 1


def _entry_allowed(snapshot: SessionSnapshot) -> bool:
    return snapshot.traffic_light == TrafficState.GREEN.value and (
        snapshot.strategy_entry_permission in {"ALLOW", "UNRESTRICTED"}
    )


def _exit_allowed(
    snapshot: SessionSnapshot,
    exit_signals: tuple[TrafficState, ...],
) -> bool:
    return snapshot.traffic_light in {signal.value for signal in exit_signals} and (
        snapshot.strategy_exit_permission in {"ALLOW", "UNRESTRICTED"}
    )


def _entry_key(side: Side) -> str:
    return "d" if side is Side.BUY else "l"


def _run_metrics(
    manifest: ExperimentManifest,
    session: LiveMarketSession,
    policy: _PolicyState,
) -> StrategyRunMetrics:
    if manifest.mode is ExperimentMode.PASSIVE_OBSERVER:
        return StrategyRunMetrics(
            None,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            policy.discipline_violations,
            0,
        )
    tracker = session.execution_tracker
    if tracker is None:
        raise RuntimeError("forked strategy session lacks an execution tracker")
    fills = tracker.fills
    metrics = tracker.metrics(session.simulation_time_us)
    bought = sum(fill.quantity for fill in fills if fill.side is Side.BUY)
    sold = sum(fill.quantity for fill in fills if fill.side is Side.SELL)
    round_trip_quantity = min(bought, sold)
    completion = (
        Decimal(round_trip_quantity) * Decimal(100) / Decimal(manifest.quantity)
    )
    position = session.engine.book.player_position.position
    realized_pnl = None
    if position == 0 and bought > 0 and sold > 0:
        realized_pnl = Decimal(
            sum(
                (1 if fill.side is Side.SELL else -1)
                * fill.price_ticks
                * fill.quantity
                for fill in fills
            )
        )
    return StrategyRunMetrics(
        entry_time_us=policy.entry_time_us,
        entry_delay_from_fork_us=(
            None
            if policy.entry_time_us is None
            else policy.entry_time_us - manifest.fork_time_us
        ),
        exit_time_us=policy.exit_time_us,
        holding_time_us=(
            None
            if policy.entry_time_us is None or policy.exit_time_us is None
            else policy.exit_time_us - policy.entry_time_us
        ),
        trade_count=len(fills),
        filled_quantity=sum(fill.quantity for fill in fills),
        fill_quality_spread_paid_ticks=metrics.spread_paid_ticks,
        implementation_shortfall_tick_shares=metrics.implementation_shortfall,
        adverse_selection_ticks=metrics.adverse_selection_after_fill_ticks,
        completion_percentage=min(Decimal(100), completion),
        completed=round_trip_quantity >= manifest.quantity,
        realized_pnl_tick_shares=realized_pnl,
        discipline_violations=policy.discipline_violations,
        ending_position=position,
    )


def _aggregate_variants(
    manifest: ExperimentManifest,
    runs: tuple[StrategyRunResult, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        _aggregate_run_group(
            variant.name,
            tuple(run for run in runs if run.variant_name == variant.name),
        )
        for variant in manifest.strategies
    )


def _aggregate_scenarios(
    manifest: ExperimentManifest,
    runs: tuple[StrategyRunResult, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            **_aggregate_run_group(
                variant.name,
                tuple(
                    run
                    for run in runs
                    if run.variant_name == variant.name
                    and run.scenario_name == scenario_name
                ),
            ),
            "scenario_name": scenario_name,
        }
        for scenario_name in manifest.scenario_names
        for variant in manifest.strategies
    )


def _aggregate_run_group(
    variant_name: str,
    runs: tuple[StrategyRunResult, ...],
) -> dict[str, object]:
    metric_names = (
        "entry_delay_from_fork_us",
        "holding_time_us",
        "trade_count",
        "filled_quantity",
        "fill_quality_spread_paid_ticks",
        "implementation_shortfall_tick_shares",
        "adverse_selection_ticks",
        "completion_percentage",
        "realized_pnl_tick_shares",
        "discipline_violations",
    )
    means = {
        f"mean_{name}": _mean_text(
            getattr(run.metrics, name) for run in runs
        )
        for name in metric_names
    }
    completed = [run.metrics.completed for run in runs if run.metrics.completed is not None]
    return {
        **means,
        "completion_rate": (
            None
            if not completed
            else _ratio_text(sum(completed), len(completed))
        ),
        "run_count": len(runs),
        "variant_name": variant_name,
    }


def _pairwise_deltas(
    manifest: ExperimentManifest,
    runs: tuple[StrategyRunResult, ...],
    agreements: tuple[TrafficAgreement, ...],
) -> tuple[dict[str, object], ...]:
    by_identity = {
        (run.scenario_name, run.seed, run.variant_name): run
        for run in runs
    }
    metric_names = (
        "entry_delay_from_fork_us",
        "holding_time_us",
        "trade_count",
        "filled_quantity",
        "fill_quality_spread_paid_ticks",
        "implementation_shortfall_tick_shares",
        "adverse_selection_ticks",
        "completion_percentage",
        "realized_pnl_tick_shares",
        "discipline_violations",
    )
    result: list[dict[str, object]] = []
    for first, second in combinations(manifest.strategies, 2):
        deltas: dict[str, list[Decimal]] = {name: [] for name in metric_names}
        paired_runs = 0
        for scenario_name in manifest.scenario_names:
            for seed in manifest.seeds:
                first_run = by_identity[(scenario_name, seed, first.name)]
                second_run = by_identity[(scenario_name, seed, second.name)]
                paired_runs += 1
                for name in metric_names:
                    first_value = getattr(first_run.metrics, name)
                    second_value = getattr(second_run.metrics, name)
                    if first_value is None or second_value is None:
                        continue
                    deltas[name].append(Decimal(second_value) - Decimal(first_value))
        pair_key = _pair_key(first.name, second.name)
        pre_matches = sum(
            agreement.pairwise[pair_key]["pre_fork_matches"]
            for agreement in agreements
        )
        pre_samples = sum(
            agreement.pairwise[pair_key]["pre_fork_samples"]
            for agreement in agreements
        )
        post_matches = sum(
            agreement.pairwise[pair_key]["post_fork_matches"]
            for agreement in agreements
        )
        post_samples = sum(
            agreement.pairwise[pair_key]["post_fork_samples"]
            for agreement in agreements
        )
        result.append(
            {
                "mean_delta_b_minus_a": {
                    name: _mean_text(values) for name, values in deltas.items()
                },
                "paired_run_count": paired_runs,
                "post_fork_traffic_agreement": _ratio_text(
                    post_matches,
                    post_samples,
                ),
                "pre_fork_traffic_agreement": _ratio_text(
                    pre_matches,
                    pre_samples,
                ),
                "variant_a": first.name,
                "variant_b": second.name,
            }
        )
    return tuple(result)


def _market_path_sha256(session: LiveMarketSession) -> str:
    payload = {
        "book": session.engine.book.snapshot(),
        "clock_us": session.simulation_time_us,
        "exchange_events": [
            event.as_dict() for event in session.engine.book.journal.events
        ],
        "flow_events": [event.as_dict() for event in session.engine.flow_events],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mean_text(values: Iterable[int | Decimal | None]) -> str | None:
    captured = [Decimal(value) for value in values if value is not None]
    if not captured:
        return None
    return str(sum(captured) / Decimal(len(captured)))


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ratio_text(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str(Decimal(numerator) / Decimal(denominator))


def _pair_key(first: str, second: str) -> str:
    return f"{first}|{second}"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
