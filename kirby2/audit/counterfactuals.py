"""Runtime acceptance audit for causal counterfactual execution branches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.algorithms import (
    AlgorithmName,
    BenchmarkManifest,
    RiskLimits,
    default_algorithm_manifest,
    run_execution_benchmark,
)
from kirby2.counterfactual import (
    CAUTIOUS_INTERPRETATION,
    ActionMutation,
    CounterfactualMode,
    CounterfactualStore,
    MutationManifest,
    run_counterfactual,
    run_timing_sweep,
)
from kirby2.exchange import OrderType, Side
from kirby2.immutable import thaw_json
from kirby2.research import RunStore
from kirby2.scenarios import get_scenario_definition
from kirby2.session.bindings import SessionCommand
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.objectives import ObjectiveType, SessionObjective
from kirby2.session.replay import SessionRecording
from kirby2.strategy import parse_strategy


_STRATEGY_SOURCE = """\
machine counterfactual_guard
window 1s
initial WATCH
state WATCH signal WAIT entry ALLOW exit ALLOW
state ACTIVE signal GREEN entry ALLOW exit ALLOW
transition WATCH -> ACTIVE when for 500ms
    spread_ticks >= 1
transition ACTIVE -> WATCH when
    spread_ticks < 1
"""


@dataclass(frozen=True, slots=True)
class CounterfactualAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": thaw_json(self.evidence),
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_counterfactuals() -> tuple[CounterfactualAuditCase, ...]:
    with TemporaryDirectory(prefix="kirby2-counterfactual-audit-") as temporary:
        root = Path(temporary)
        session_store, parent_run_id = _session_parent(root / "session")
        base = MutationManifest(
            (
                ActionMutation(
                    1,
                    expected_command=SessionCommand.BUY_ASK,
                    command=SessionCommand.BUY_BID,
                    quantity=200,
                ),
            )
        )
        exogenous = run_counterfactual(
            parent_run_id,
            base,
            CounterfactualMode.EXOGENOUS_REPLAY,
            parent_store_root=session_store,
        )
        endogenous = run_counterfactual(
            parent_run_id,
            base,
            CounterfactualMode.ENDOGENOUS_FORK,
            parent_store_root=session_store,
        )
        repeat = run_counterfactual(
            parent_run_id,
            base,
            CounterfactualMode.ENDOGENOUS_FORK,
            parent_store_root=session_store,
        )
        branch_store = CounterfactualStore(
            session_store / "counterfactual_runs"
        )
        immutable = branch_store.record(endogenous)
        immutable_repeat = branch_store.record(repeat)
        immutable_verification = branch_store.verify_run(immutable.run_id)
        sweep = run_timing_sweep(
            parent_run_id,
            1,
            CounterfactualMode.EXOGENOUS_REPLAY,
            parent_store_root=session_store,
        )
        mutation_reports = _mutation_reports(parent_run_id, session_store)
        multivenue = _multi_venue_branch(root / "algorithm")
        tamper = _tamper_case(branch_store, immutable.run_id)
        return (
            _snapshot_case(exogenous),
            _mode_case(exogenous, endogenous),
            _mutation_surface_case(mutation_reports, multivenue),
            _timeline_and_metrics_case(endogenous),
            _sweep_case(sweep, session_store),
            _hindsight_case(exogenous, endogenous, multivenue),
            _immutable_case(
                immutable,
                immutable_repeat,
                immutable_verification,
                tamper,
            ),
            _determinism_case(endogenous, repeat),
            _venue_case(multivenue),
            _language_and_invariants_case(exogenous, endogenous, multivenue),
        )


def _session_parent(root: Path) -> tuple[Path, str]:
    layout = HotkeyLayout.default()
    session = LiveMarketSession(
        get_scenario_definition("balanced"),
        seed=42,
        duration_seconds=2,
        initial_quantity=200,
        quantity_options=(100, 200, 500),
        strategy_definition=parse_strategy(_STRATEGY_SOURCE),
        objective=SessionObjective(
            ObjectiveType.ACQUIRE,
            200,
            2_000_000,
            2,
        ),
    )
    session.start()
    session.advance_by(600_000)
    session.handle_input("s", layout.bindings)
    session.advance_by(300_000)
    session.handle_input("c", layout.bindings)
    session.advance_by(300_000)
    session.handle_input("j", layout.bindings)
    session.advance_by(800_000)
    recording = SessionRecording.capture(session, layout, auto_start=True)
    store = RunStore(root)
    manifest = store.record_session(recording, session)
    return root, manifest.run_id


def _mutation_reports(parent_run_id: str, store: Path):
    manifests = {
        "command_size_timing": MutationManifest(
            (
                ActionMutation(
                    1,
                    expected_command=SessionCommand.BUY_ASK,
                    command=SessionCommand.BUY_BID,
                    quantity=100,
                    timing_delta_us=50_000,
                ),
            )
        ),
        "order_type_price": MutationManifest(
            (
                ActionMutation(
                    1,
                    expected_command=SessionCommand.BUY_ASK,
                    order_type=OrderType.LIMIT,
                    price_ticks=9_998,
                ),
            )
        ),
        "remove": MutationManifest((ActionMutation(1, remove=True),)),
        "insert": MutationManifest(
            (
                ActionMutation(
                    2,
                    command=SessionCommand.MARKET_BUY,
                    quantity=100,
                    timing_delta_us=100_000,
                    insert=True,
                ),
            )
        ),
        "hotkey_outcome": MutationManifest(
            (
                ActionMutation(
                    3,
                    expected_command=SessionCommand.SELL_ASK,
                    hotkey_outcome=SessionCommand.MARKET_SELL,
                    quantity=100,
                ),
            )
        ),
    }
    return {
        name: run_counterfactual(
            parent_run_id,
            manifest,
            CounterfactualMode.ENDOGENOUS_FORK,
            parent_store_root=store,
        )
        for name, manifest in manifests.items()
    }


def _multi_venue_branch(root: Path):
    manifest = BenchmarkManifest(
        "wo28-route-mutation",
        ("opening_momentum",),
        (default_algorithm_manifest(AlgorithmName.TWAP),),
        (73, 74),
        200,
        1_000_000,
        250_000,
        Side.BUY,
        RiskLimits(200, 200, 200, 10),
    )
    benchmark = run_execution_benchmark(manifest, store_root=root)
    parent = benchmark.runs[0].run_id
    report = run_counterfactual(
        parent,
        MutationManifest(
            (
                ActionMutation(
                    1,
                    expected_command=SessionCommand.BUY_ASK,
                    venue_id="DEEP",
                ),
            )
        ),
        CounterfactualMode.EXOGENOUS_REPLAY,
        parent_store_root=root,
    )
    immutable = CounterfactualStore(root / "counterfactual_runs").record(report)
    return report, immutable.run_id


def _snapshot_case(report) -> CounterfactualAuditCase:
    components = {item.name: item for item in report.snapshot.components}
    required = {
        "agent_state",
        "all_venue_states",
        "exchange_state",
        "feature_windows",
        "flow_model_state",
        "hawkes_decay_state",
        "historical_replay_cursor",
        "pending_latency_messages",
        "player_state",
        "rng_state",
        "simulation_clock",
        "strategy_state",
        "working_orders",
    }
    preserved = {
        "all_venue_states",
        "exchange_state",
        "feature_windows",
        "flow_model_state",
        "player_state",
        "rng_state",
        "simulation_clock",
        "strategy_state",
        "working_orders",
    }
    failures: list[str] = []
    if set(components) != required:
        failures.append("branch snapshot component inventory is incomplete")
    if any(components[name].status.value != "PRESERVED" for name in preserved):
        failures.append("an active session component was not preserved")
    if not report.snapshot_reconstruction_match:
        failures.append("independent fork reconstructions did not match")
    return CounterfactualAuditCase(
        "complete_digest_verified_branch_snapshot",
        {
            "component_count": len(components),
            "fork_time_us": report.snapshot.fork_time_us,
            "preserved": sorted(
                name for name, item in components.items() if item.status.value == "PRESERVED"
            ),
            "snapshot_sha256": report.snapshot.sha256(),
        },
        tuple(failures),
    )


def _mode_case(exogenous, endogenous) -> CounterfactualAuditCase:
    failures: list[str] = []
    if exogenous.exogenous_reference_path_sha256 is None:
        failures.append("exogenous branch lacks its fixed reference-path digest")
    if endogenous.exogenous_reference_path_sha256 is not None:
        failures.append("endogenous branch falsely claims a fixed external path")
    if exogenous.branch.state_sha256 == exogenous.original.state_sha256:
        failures.append("exogenous mutation did not change its outcome state")
    if endogenous.branch.state_sha256 == endogenous.original.state_sha256:
        failures.append("endogenous mutation did not change its outcome state")
    if exogenous.mode is endogenous.mode:
        failures.append("counterfactual modes were conflated")
    return CounterfactualAuditCase(
        "explicit_exogenous_and_endogenous_semantics",
        {
            "endogenous_branch_sha256": endogenous.branch.state_sha256,
            "endogenous_flow_digest": endogenous.exogenous_reference_path_sha256,
            "exogenous_branch_sha256": exogenous.branch.state_sha256,
            "exogenous_reference_path_sha256": exogenous.exogenous_reference_path_sha256,
            "original_fill": exogenous.original.metrics["fill"],
            "mutated_fill": exogenous.branch.metrics["fill"],
        },
        tuple(failures),
    )


def _mutation_surface_case(reports, multivenue) -> CounterfactualAuditCase:
    expected = {
        "command_size_timing",
        "hotkey_outcome",
        "insert",
        "order_type_price",
        "remove",
    }
    failures: list[str] = []
    if set(reports) != expected:
        failures.append("session mutation family inventory is incomplete")
    if any(report.first_divergence.index is None for report in reports.values()):
        failures.append("one or more session mutations did not enter the paired timeline")
    venue_report, _run_id = multivenue
    if venue_report.first_divergence.index is None:
        failures.append("venue mutation did not enter the multi-venue timeline")
    return CounterfactualAuditCase(
        "command_type_price_size_venue_timing_remove_insert_hotkey_mutations",
        {
            "session_mutation_families": sorted(reports),
            "venue_fees_changed": venue_report.comparison["fees"]["changed"],
            "venue_position_changed": venue_report.comparison["position"]["changed"],
        },
        tuple(failures),
    )


def _timeline_and_metrics_case(report) -> CounterfactualAuditCase:
    required = {
        "adverse_selection",
        "completion",
        "deadline",
        "fees",
        "fill",
        "pnl",
        "position",
        "risk",
        "slippage",
        "traffic_light_state",
    }
    failures: list[str] = []
    if set(report.comparison) != required:
        failures.append("counterfactual comparison metric inventory is incomplete")
    if report.first_divergence.index is None:
        failures.append("paired timelines omitted their first divergence")
    if not report.original.timeline or not report.branch.timeline:
        failures.append("paired timelines are empty")
    return CounterfactualAuditCase(
        "paired_timelines_and_complete_comparison_inventory",
        {
            "branch_timeline_count": len(report.branch.timeline),
            "changed_metrics": sorted(
                key for key, value in report.comparison.items() if value["changed"]
            ),
            "first_divergence_index": report.first_divergence.index,
            "metric_names": sorted(report.comparison),
            "original_timeline_count": len(report.original.timeline),
        },
        tuple(failures),
    )


def _sweep_case(sweep, session_store) -> CounterfactualAuditCase:
    offsets = tuple(cell.timing_delta_us for cell in sweep.cells)
    branch_store = CounterfactualStore(session_store / "counterfactual_runs")
    verified = [branch_store.verify_run(cell.branch_run_id).passed for cell in sweep.cells]
    failures: list[str] = []
    if offsets != (-500_000, -250_000, 0, 250_000, 500_000):
        failures.append("timing sweep offsets are not canonical")
    if not all(verified):
        failures.append("one or more timing sweep branches lack immutable evidence")
    return CounterfactualAuditCase(
        "five_cell_timing_sensitivity_sweep",
        {
            "branch_run_ids": [cell.branch_run_id for cell in sweep.cells],
            "offsets_us": list(offsets),
            "verified_count": sum(verified),
        },
        tuple(failures),
    )


def _hindsight_case(*reports) -> CounterfactualAuditCase:
    failures: list[str] = []
    evidence = []
    for value in reports:
        report = value[0] if isinstance(value, tuple) else value
        guard = report.hindsight_guard
        records = guard["decision_records"]
        if (
            guard.get("status") != "PASS"
            or guard.get("privileged_snapshot_accessible_to_policy") is not False
            or any(
                item["uses_future_observations"] for item in records
            )
        ):
            failures.append(f"{report.mode.value} branch permitted hindsight")
        evidence.append(
            {
                "decision_count": len(records),
                "mode": report.mode.value,
                "policy": guard["policy_information"],
            }
        )
    return CounterfactualAuditCase(
        "decision_time_information_boundary",
        {"branches": evidence},
        tuple(failures),
    )


def _immutable_case(first, repeat, verification, tamper) -> CounterfactualAuditCase:
    failures: list[str] = []
    if first.run_id != repeat.run_id:
        failures.append("identical branch did not resolve to one content identity")
    if not verification.passed:
        failures.append("immutable counterfactual evidence failed verification")
    if tamper.passed:
        failures.append("tampered counterfactual evidence was accepted")
    return CounterfactualAuditCase(
        "parent_linked_content_addressed_immutable_branch_records",
        {
            "parent_run_id": first.parent_run_id,
            "run_id": first.run_id,
            "tamper_failures": list(tamper.failures),
            "verification": verification.as_dict(),
        },
        tuple(failures),
    )


def _tamper_case(store: CounterfactualStore, run_id: str):
    path = store.run_directory(run_id) / "report.toml"
    path.write_text(path.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    return store.verify_run(run_id)


def _determinism_case(first, repeat) -> CounterfactualAuditCase:
    identical = first.as_dict() == repeat.as_dict()
    failures = () if identical else ("same parent/mutation/mode changed report bytes",)
    return CounterfactualAuditCase(
        "byte_structural_determinism",
        {
            "byte_structural_identical": identical,
            "result_sha256": first.result_sha256(),
            "repeat_result_sha256": repeat.result_sha256(),
        },
        failures,
    )


def _venue_case(multivenue) -> CounterfactualAuditCase:
    report, run_id = multivenue
    pending = next(
        item for item in report.snapshot.components if item.name == "pending_latency_messages"
    )
    venues = next(
        item for item in report.snapshot.components if item.name == "all_venue_states"
    )
    failures: list[str] = []
    if pending.status.value != "PRESERVED" or venues.status.value != "PRESERVED":
        failures.append("multi-venue snapshot omitted latency or venue state")
    if not report.comparison["fees"]["changed"]:
        failures.append("direct venue mutation did not expose its fee consequence")
    return CounterfactualAuditCase(
        "multi_venue_route_mutation_with_latency_and_fee_effects",
        {
            "branch_run_id": run_id,
            "fee_comparison": report.comparison["fees"],
            "pending_latency_sha256": pending.sha256,
            "venue_state_sha256": venues.sha256,
        },
        tuple(failures),
    )


def _language_and_invariants_case(*reports) -> CounterfactualAuditCase:
    failures: list[str] = []
    for value in reports:
        report = value[0] if isinstance(value, tuple) else value
        if report.cautious_interpretation != CAUTIOUS_INTERPRETATION:
            failures.append("counterfactual report omitted the model-evidence caveat")
        if report.original.invariant_status != "PASS" or report.branch.invariant_status != "PASS":
            failures.append("counterfactual continuation failed runtime invariants")
    return CounterfactualAuditCase(
        "cautious_language_and_runtime_invariants",
        {
            "caveat": CAUTIOUS_INTERPRETATION,
            "report_count": len(reports),
        },
        tuple(failures),
    )
