"""Runtime acceptance audit for observable-only execution algorithm benchmarks."""

from __future__ import annotations

import inspect
import json
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.algorithms import (
    AlgorithmActionType,
    AlgorithmName,
    AlgorithmParameterManifest,
    AlgorithmRunStore,
    BenchmarkManifest,
    ExecutionAlgorithm,
    RiskLimits,
    default_algorithm_manifest,
    manual_manifest_from_session_recording,
    run_execution_benchmark,
)
from kirby2.exchange.models import Side
from kirby2.scenarios import get_scenario_definition
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.replay import SessionRecording


@dataclass(frozen=True, slots=True)
class ExecutionAlgorithmAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_execution_algorithms() -> tuple[ExecutionAlgorithmAuditCase, ...]:
    with TemporaryDirectory(prefix="kirby2-algorithm-audit-") as temporary:
        root = Path(temporary)
        manifest = _acceptance_manifest()
        result = run_execution_benchmark(manifest, store_root=root / "first")
        repeat = run_execution_benchmark(manifest, store_root=root / "repeat")
        store = AlgorithmRunStore(root / "first")
        repeat_store = AlgorithmRunStore(root / "repeat")
        reports = tuple(store.verify_run(run.run_id) for run in result.runs)
        decisions = _load_decisions(store, result)
        cases = (
            _acceptance_matrix_case(result),
            _common_interface_case(decisions),
            _observable_boundary_case(decisions),
            _fork_control_case(result),
            _immutable_records_case(result, reports),
            _metrics_case(result),
            _determinism_case(result, repeat, store, repeat_store),
            _different_seed_case(result),
            _manual_replay_case(result),
            _player_recording_adapter_case(root / "player-recording"),
            _cancel_action_case(root / "cancel-action"),
            _risk_limits_case(root / "risk"),
            _parameter_and_price_limit_case(root / "parameters"),
            _winner_refusal_case(result),
            _tamper_refusal_case(store, result.runs[0].run_id),
        )
        return cases


def _acceptance_manifest() -> BenchmarkManifest:
    return BenchmarkManifest(
        "wo27-acceptance",
        ("opening_momentum", "balanced_execution"),
        tuple(default_algorithm_manifest(name) for name in AlgorithmName),
        (100, 101),
        300,
        2_000_000,
        250_000,
        Side.BUY,
        RiskLimits(300, 600, 600, 10),
    )


def _acceptance_matrix_case(result) -> ExecutionAlgorithmAuditCase:
    expected = (
        len(result.manifest.scenario_names)
        * len(result.manifest.seeds)
        * len(result.manifest.algorithm_manifests)
    )
    algorithms = {run.algorithm for run in result.runs}
    failures: list[str] = []
    if len(result.runs) != expected or len(algorithms) != len(AlgorithmName):
        failures.append("benchmark matrix does not contain every policy/seed/scenario run")
    if not all(run.replay_verified for run in result.runs):
        failures.append("one or more benchmark runs failed replay")
    return ExecutionAlgorithmAuditCase(
        "multi_seed_multi_scenario_all_algorithm_matrix",
        {
            "algorithm_count": len(algorithms),
            "algorithms": sorted(item.value for item in algorithms),
            "run_count": len(result.runs),
            "scenario_count": len(result.manifest.scenario_names),
            "seed_count": len(result.manifest.seeds),
        },
        tuple(failures),
    )


def _common_interface_case(decisions) -> ExecutionAlgorithmAuditCase:
    signature = tuple(inspect.signature(ExecutionAlgorithm.decide).parameters)
    action_types = {
        str(item["action"]["action_type"])
        for trace in decisions.values()
        for item in trace
    }
    allowed = {item.value for item in AlgorithmActionType}
    failures: list[str] = []
    if signature != ("self", "observation"):
        failures.append("execution algorithm interface accepts a non-client dependency")
    if not action_types <= allowed or not action_types:
        failures.append("algorithm emitted an action outside the common output contract")
    return ExecutionAlgorithmAuditCase(
        "common_observation_to_action_interface",
        {
            "allowed_action_types": sorted(allowed),
            "emitted_action_types": sorted(action_types),
            "interface_parameters": list(signature),
        },
        tuple(failures),
    )


def _observable_boundary_case(decisions) -> ExecutionAlgorithmAuditCase:
    payload = json.dumps(decisions, sort_keys=True).lower()
    forbidden = {
        "ground_truth",
        "hidden_quantity",
        "hidden_regime",
        "reserve_quantity",
        "future_events",
        "future_historical_prices",
        "priority_sequence",
    }
    leaked = sorted(value for value in forbidden if value in payload)
    representations = {
        item["observation"]["representation"]
        for trace in decisions.values()
        for item in trace
    }
    failures = () if (
        not leaked and representations == {"ALGORITHM_CLIENT_OBSERVATION"}
    ) else ("algorithm decision evidence crossed the observable boundary",)
    return ExecutionAlgorithmAuditCase(
        "no_hidden_regime_future_or_ground_truth_inputs",
        {
            "forbidden_fields_found": leaked,
            "representations": sorted(representations),
        },
        failures,
    )


def _fork_control_case(result) -> ExecutionAlgorithmAuditCase:
    cells: dict[tuple[str, int], set[str]] = {}
    backgrounds: dict[tuple[str, int], set[str]] = {}
    for run in result.runs:
        key = (run.scenario_name, run.seed)
        cells.setdefault(key, set()).add(run.fork_state_sha256)
        backgrounds.setdefault(key, set()).add(run.background_path_sha256)
    failures: list[str] = []
    if any(len(values) != 1 for values in cells.values()):
        failures.append("algorithm cell did not share one deterministic fork state")
    if any(len(values) != 1 for values in backgrounds.values()):
        failures.append("algorithm cell did not share one exogenous background recipe")
    return ExecutionAlgorithmAuditCase(
        "identical_fork_and_background_per_comparison_cell",
        {
            "cells": {
                f"{scenario}:{seed}": {
                    "background_sha256": next(iter(backgrounds[(scenario, seed)])),
                    "fork_state_sha256": next(iter(values)),
                }
                for (scenario, seed), values in sorted(cells.items())
            }
        },
        tuple(failures),
    )


def _immutable_records_case(result, reports) -> ExecutionAlgorithmAuditCase:
    failures: list[str] = []
    if len({run.run_id for run in result.runs}) != len(result.runs):
        failures.append("immutable algorithm run IDs are duplicated")
    if not all(report.passed for report in reports):
        failures.append("one or more immutable algorithm records failed verification")
    return ExecutionAlgorithmAuditCase(
        "content_addressed_immutable_run_records",
        {
            "record_count": len(reports),
            "run_ids": [run.run_id for run in result.runs],
            "verification_pass_count": sum(report.passed for report in reports),
        },
        tuple(failures),
    )


def _metrics_case(result) -> ExecutionAlgorithmAuditCase:
    required = {
        "adverse_selection_x2_tick_shares",
        "average_fill_price_denominator",
        "average_fill_price_numerator_x2",
        "cancel_count",
        "completed_quantity",
        "completion_bps",
        "deadline_failure",
        "elapsed_time_us",
        "fees_micros",
        "fill_uncertainty_quantity",
        "implementation_shortfall_x2_tick_shares",
        "market_impact_x2_ticks",
        "rebates_micros",
        "risk_rejection_count",
        "spread_paid_x2_tick_shares",
        "target_quantity",
    }
    inventories = {frozenset(run.metrics.as_dict()) for run in result.runs}
    failures: list[str] = []
    if inventories != {frozenset(required)}:
        failures.append("benchmark metric inventory is incomplete")
    if len(result.aggregate_by_algorithm) != len(result.manifest.algorithm_manifests):
        failures.append("aggregate output lacks an algorithm")
    return ExecutionAlgorithmAuditCase(
        "per_seed_and_aggregate_metric_inventory",
        {
            "aggregate_count": len(result.aggregate_by_algorithm),
            "metric_names": sorted(required),
            "per_seed_count": len(result.runs),
        },
        tuple(failures),
    )


def _determinism_case(first, repeat, first_store, repeat_store) -> ExecutionAlgorithmAuditCase:
    first_runs = [run.as_dict() for run in first.runs]
    repeat_runs = [run.as_dict() for run in repeat.runs]
    artifact_names = (
        "configuration.json",
        "decisions.json",
        "manifest.json",
        "metrics.json",
        "recording.json",
    )
    immutable_bytes_identical = all(
        (
            first_store.runs_directory / run.run_id / name
        ).read_bytes()
        == (
            repeat_store.runs_directory / run.run_id / name
        ).read_bytes()
        for run in first.runs
        for name in artifact_names
    )
    failures = () if (
        first.result_sha256 == repeat.result_sha256
        and first_runs == repeat_runs
        and immutable_bytes_identical
    ) else ("identical benchmark manifest did not reproduce structurally",)
    return ExecutionAlgorithmAuditCase(
        "benchmark_determinism",
        {
            "first_result_sha256": first.result_sha256,
            "immutable_run_directories_byte_identical": immutable_bytes_identical,
            "repeat_result_sha256": repeat.result_sha256,
            "run_records_identical": first_runs == repeat_runs,
        },
        failures,
    )


def _different_seed_case(result) -> ExecutionAlgorithmAuditCase:
    by_scenario: dict[str, set[str]] = {}
    for run in result.runs:
        by_scenario.setdefault(run.scenario_name, set()).add(run.background_path_sha256)
    failures = () if all(len(values) == len(result.manifest.seeds) for values in by_scenario.values()) else (
        "different seeds did not produce different valid background paths",
    )
    return ExecutionAlgorithmAuditCase(
        "different_seed_background_paths",
        {key: sorted(value) for key, value in sorted(by_scenario.items())},
        failures,
    )


def _manual_replay_case(result) -> ExecutionAlgorithmAuditCase:
    runs = [run for run in result.runs if run.algorithm is AlgorithmName.MANUAL_REPLAY]
    failures = () if (
        len(runs) == len(result.manifest.scenario_names) * len(result.manifest.seeds)
        and all(run.run_id.startswith("run-") for run in runs)
    ) else ("manual player replay was not included as an immutable policy run",)
    return ExecutionAlgorithmAuditCase(
        "manual_replay_policy_slot",
        {
            "run_count": len(runs),
            "run_ids": [run.run_id for run in runs],
        },
        failures,
    )


def _risk_limits_case(root: Path) -> ExecutionAlgorithmAuditCase:
    manifest = BenchmarkManifest(
        "risk-refusal",
        ("opening_momentum",),
        (default_algorithm_manifest(AlgorithmName.IMMEDIATE_MARKET),),
        (1, 2),
        200,
        1_000_000,
        250_000,
        Side.BUY,
        RiskLimits(50, 200, 200, 10),
    )
    result = run_execution_benchmark(manifest, store_root=root)
    failures = () if all(
        run.metrics.risk_rejection_count > 0
        and run.metrics.completed_quantity == 0
        for run in result.runs
    ) else ("runtime risk limits did not reject oversized algorithm children",)
    return ExecutionAlgorithmAuditCase(
        "runtime_risk_limits_fail_closed",
        {
            "completed_quantities": [run.metrics.completed_quantity for run in result.runs],
            "risk_rejection_counts": [run.metrics.risk_rejection_count for run in result.runs],
        },
        failures,
    )


def _cancel_action_case(root: Path) -> ExecutionAlgorithmAuditCase:
    manual = AlgorithmParameterManifest(
        AlgorithmName.MANUAL_REPLAY,
        {
            "replay_actions": [
                {
                    "action_type": "SUBMIT",
                    "elapsed_time_us": 0,
                    "quantity": 100,
                    "route_policy": "PASSIVE_QUEUE",
                    "route_style": "PASSIVE",
                },
                {
                    "action_type": "CANCEL",
                    "elapsed_time_us": 250_000,
                },
            ],
            "replay_provenance": {
                "source_sha256": "1" * 64,
                "source_type": "AUDIT_ACTION_SCHEDULE",
                "source_verification": "BUILTIN_CANONICAL",
                "translation_version": 1,
            },
        },
    )
    result = run_execution_benchmark(
        BenchmarkManifest(
            "cancel-action",
            ("balanced_execution",),
            (manual,),
            (31, 32),
            200,
            1_000_000,
            250_000,
            Side.BUY,
            RiskLimits(200, 400, 400, 10),
        ),
        store_root=root,
    )
    store = AlgorithmRunStore(root)
    decisions = _load_decisions(store, result)
    cancel_decisions = [
        item
        for trace in decisions.values()
        for item in trace
        if item["action"]["action_type"] == "CANCEL"
    ]
    failures = () if cancel_decisions and all(
        item["action_accepted"] for item in cancel_decisions
    ) else ("observable cancel output was not accepted against a working order",)
    return ExecutionAlgorithmAuditCase(
        "cancel_output_semantics",
        {
            "accepted_cancel_count": sum(
                item["action_accepted"] for item in cancel_decisions
            ),
            "cancel_decision_count": len(cancel_decisions),
        },
        failures,
    )


def _player_recording_adapter_case(root: Path) -> ExecutionAlgorithmAuditCase:
    layout = HotkeyLayout.default()
    session = LiveMarketSession(
        get_scenario_definition("balanced"),
        seed=57,
        duration_seconds=1,
        initial_quantity=100,
        quantity_options=(100,),
    )
    session.start()
    session.advance_by(200_000)
    source_input = session.handle_input("d", layout.bindings)
    session.advance_by(800_000)
    recording = SessionRecording.capture(session, layout, auto_start=True)
    projected = manual_manifest_from_session_recording(
        recording,
        objective_side=Side.BUY,
        benchmark_duration_us=1_000_000,
        decision_interval_us=250_000,
    )
    result = _single_policy_result("player-recording", projected, root)
    provenance = projected.parameters["replay_provenance"]
    actions = projected.parameters["replay_actions"]
    failures: list[str] = []
    if not source_input.accepted:
        failures.append("source player action was not accepted")
    if not isinstance(provenance, Mapping) or (
        provenance.get("source_verification") != "EXACT_SESSION_REPLAY"
    ):
        failures.append("player source recording was not preserved as exactly verified")
    if not isinstance(actions, (list, tuple)) or actions[0].get("elapsed_time_us") != 250_000:
        failures.append("player action was not audibly projected onto the decision grid")
    if not all(run.replay_verified for run in result.runs):
        failures.append("projected player policy run failed immutable replay")
    collision_session = LiveMarketSession(
        get_scenario_definition("balanced"),
        seed=58,
        duration_seconds=1,
        initial_quantity=100,
        quantity_options=(100,),
    )
    collision_session.start()
    collision_session.advance_by(100_000)
    collision_session.handle_input("d", layout.bindings)
    collision_session.advance_by(100_000)
    collision_session.handle_input("d", layout.bindings)
    collision_session.advance_by(800_000)
    collision_recording = SessionRecording.capture(
        collision_session,
        layout,
        auto_start=True,
    )
    collision_refused = False
    try:
        manual_manifest_from_session_recording(
            collision_recording,
            objective_side=Side.BUY,
            benchmark_duration_us=1_000_000,
            decision_interval_us=250_000,
        )
    except ValueError as error:
        collision_refused = "same benchmark decision time" in str(error)
    if not collision_refused:
        failures.append("manual replay did not refuse decision-grid action collision")
    return ExecutionAlgorithmAuditCase(
        "verified_player_recording_policy_adapter",
        {
            "action_count": len(actions) if isinstance(actions, (list, tuple)) else 0,
            "effective_elapsed_time_us": (
                actions[0].get("elapsed_time_us")
                if isinstance(actions, (list, tuple)) and actions
                else None
            ),
            "run_ids": [run.run_id for run in result.runs],
            "same_grid_time_collision_refused": collision_refused,
            "source_sha256": (
                provenance.get("source_sha256")
                if isinstance(provenance, Mapping)
                else None
            ),
            "source_verification": (
                provenance.get("source_verification")
                if isinstance(provenance, Mapping)
                else None
            ),
        },
        tuple(failures),
    )


def _parameter_and_price_limit_case(root: Path) -> ExecutionAlgorithmAuditCase:
    default = default_algorithm_manifest(AlgorithmName.IMMEDIATE_MARKET)
    bounded = AlgorithmParameterManifest(
        AlgorithmName.IMMEDIATE_MARKET,
        {
            **default.parameters,
            "maximum_slice": 50,
            "price_limit_ticks": 9_999,
        },
    )
    first = _single_policy_result("parameter-default", default, root / "default")
    second = _single_policy_result("parameter-bounded", bounded, root / "bounded")
    failures: list[str] = []
    if first.runs[0].decision_trace_sha256 == second.runs[0].decision_trace_sha256:
        failures.append("changed parameter manifest did not change decision trace")
    if any(run.metrics.completed_quantity for run in second.runs):
        failures.append("venue matching violated the aggressive integer-tick price limit")
    unknown_parameter_refused = False
    try:
        AlgorithmParameterManifest(
            AlgorithmName.IMMEDIATE_MARKET,
            {**default.parameters, "unsupported_noop": 1},
        )
    except ValueError as error:
        unknown_parameter_refused = "unknown=['unsupported_noop']" in str(error)
    if not unknown_parameter_refused:
        failures.append("algorithm manifest accepted an unsupported no-op parameter")
    manifest_mutation_refused = False
    try:
        default.parameters["maximum_slice"] = 9_999  # type: ignore[index]
    except TypeError:
        manifest_mutation_refused = True
    if not manifest_mutation_refused:
        failures.append("algorithm parameter manifest remained mutable after validation")

    sweep_default = default_algorithm_manifest(AlgorithmName.SWEEP)
    hard_risk_sweep = AlgorithmParameterManifest(
        AlgorithmName.SWEEP,
        {
            **sweep_default.parameters,
            "maximum_slice": 1_500,
            "minimum_slice": 1,
        },
    )
    hard_risk = run_execution_benchmark(
        BenchmarkManifest(
            "hard-risk-price-limit",
            ("balanced_execution",),
            (hard_risk_sweep,),
            (41, 42),
            1_500,
            1_000_000,
            250_000,
            Side.BUY,
            RiskLimits(1_500, 1_500, 1_500, 10, price_limit_ticks=10_001),
        ),
        store_root=root / "hard-risk",
    )
    hard_risk_prices_within_limit = all(
        run.metrics.average_fill_price_numerator_x2
        <= 10_001 * 2 * run.metrics.average_fill_price_denominator
        and run.metrics.completed_quantity < run.metrics.target_quantity
        for run in hard_risk.runs
    )
    if not hard_risk_prices_within_limit:
        failures.append("runtime risk price limit was not enforced at venue matching")
    unsafe_cancel_timing_refused = False
    try:
        run_execution_benchmark(
            BenchmarkManifest(
                "unsafe-cancel-timing",
                ("balanced_execution",),
                (default_algorithm_manifest(AlgorithmName.JOIN_BEST),),
                (51, 52),
                100,
                1_000_000,
                1_000,
                Side.BUY,
                RiskLimits(100, 100, 100, 10),
            ),
            store_root=root / "unsafe-cancel-timing",
        )
    except ValueError as error:
        unsafe_cancel_timing_refused = "synchronous cancel-all latency" in str(error)
    if not unsafe_cancel_timing_refused:
        failures.append("unsafe cancel timing was not refused before path divergence")
    return ExecutionAlgorithmAuditCase(
        "parameter_manifest_changes_behavior_and_price_limit_is_hard",
        {
            "bounded_completed_quantities": [run.metrics.completed_quantity for run in second.runs],
            "bounded_manifest_sha256": bounded.sha256(),
            "default_manifest_sha256": default.sha256(),
            "decision_trace_changed": first.runs[0].decision_trace_sha256 != second.runs[0].decision_trace_sha256,
            "hard_risk_completed_quantities": [
                run.metrics.completed_quantity for run in hard_risk.runs
            ],
            "hard_risk_prices_within_limit": hard_risk_prices_within_limit,
            "manifest_mutation_refused": manifest_mutation_refused,
            "unsafe_cancel_timing_refused": unsafe_cancel_timing_refused,
            "unknown_parameter_refused": unknown_parameter_refused,
        },
        tuple(failures),
    )


def _single_policy_result(experiment_id, algorithm_manifest, root):
    return run_execution_benchmark(
        BenchmarkManifest(
            experiment_id,
            ("balanced_execution",),
            (algorithm_manifest,),
            (21, 22),
            200,
            1_000_000,
            250_000,
            Side.BUY,
            RiskLimits(200, 400, 400, 10),
        ),
        store_root=root,
    )


def _winner_refusal_case(result) -> ExecutionAlgorithmAuditCase:
    declaration = result.winner_declaration
    failures = () if (
        declaration["status"] == "NOT_DECLARED"
        and declaration["winner"] is None
    ) else ("benchmark declared a universally best algorithm",)
    return ExecutionAlgorithmAuditCase(
        "no_universal_winner_claim",
        declaration,
        failures,
    )


def _tamper_refusal_case(store: AlgorithmRunStore, run_id: str) -> ExecutionAlgorithmAuditCase:
    path = store.runs_directory / run_id / "metrics.json"
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    report = store.verify_run(run_id)
    manifest_path = store.runs_directory / run_id / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["artifact_sha256"]["metrics.json"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    forged_report = store.verify_run(run_id)
    failures = () if (
        not report.passed
        and not report.artifact_digests_match
        and not forged_report.passed
        and not forged_report.content_identity_match
    ) else (
        "immutable run verification accepted a modified metrics artifact",
    )
    return ExecutionAlgorithmAuditCase(
        "immutable_record_tamper_refusal",
        {
            "artifact_tamper": report.as_dict(),
            "forged_manifest_hash_inventory": forged_report.as_dict(),
        },
        failures,
    )


def _load_decisions(store: AlgorithmRunStore, result) -> dict[str, object]:
    payload: dict[str, object] = {}
    for run in result.runs:
        path = store.runs_directory / run.run_id / "decisions.json"
        payload[run.run_id] = json.loads(path.read_text(encoding="utf-8"))
    return payload
