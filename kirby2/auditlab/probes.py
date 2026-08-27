"""Typed checks from real subsystems outside generated executor lanes."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.agents import (
    POPULATION_IDS,
    EcologyRecording,
    get_population,
    replay_agent_ecology,
    run_agent_ecology,
)
from kirby2.audit.counterfactuals import audit_counterfactuals
from kirby2.audit.latency import audit_latency
from kirby2.audit.market_data import audit_market_data
from kirby2.audit.market_mechanics import audit_market_mechanics
from kirby2.audit.multivenue import audit_multivenue
from kirby2.calibration import CalibrationConfig, calibrate_market, resolve_measurement_source
from kirby2.counterfactual import (
    ActionMutation,
    CounterfactualMode,
    CounterfactualStore,
    MutationManifest,
    run_counterfactual,
)
from kirby2.exchange import run_mechanics_scenario
from kirby2.latency import run_cancel_race
from kirby2.multivenue import run_multivenue_scenario
from kirby2.observability import run_hidden_liquidity_scenario
from kirby2.research import RunStore
from kirby2.scenarios import get_scenario_definition
from kirby2.session.bindings import SessionCommand
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.replay import SessionRecording
from kirby2.simulation import compare_flow_models, load_accepted_hawkes_configs

from .generator import generate_configurations
from .models import (
    AUDIT_LAB_SCHEMA_VERSION,
    CheckResult,
    CheckStatus,
    GeneratedConfiguration,
    canonical_sha256,
)


def run_subsystem_probes(seed: int) -> tuple[CheckResult, ...]:
    probes = (
        _instruction_probe(),
        _auction_probe(),
        _latency_probe(seed),
        _counterfactual_probe(),
        _calibration_probe(seed),
        _market_data_probe(),
        _fault_semantics_probe(),
        _hidden_probe(),
        _multivenue_probe(),
        _agent_probe(seed),
        _hawkes_probe(seed),
    )
    return tuple(sorted(probes, key=lambda item: item.name))


def _probe(
    name: str,
    status: bool,
    evidence: dict[str, object],
) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.PASS if status else CheckStatus.FAIL,
        required=True,
        detail=(
            f"real subsystem probe passed: {name}"
            if status
            else f"real subsystem probe failed: {name}"
        ),
        evidence={"source": "run_subsystem_probes", **evidence},
    )


def _auction_probe() -> CheckResult:
    evidence = {}
    passed = True
    for name in ("opening-auction", "closing-auction", "reopening-gap"):
        result = run_mechanics_scenario(name)
        result.engine.assert_invariants()
        evidence[name] = {
            "event_stream_sha256": result.engine.event_stream_sha256(),
            "replay": result.replay.passed,
            "summary": result.summary,
        }
        passed = passed and result.replay.passed
    return _probe("auction_allocation", passed, evidence)


def _instruction_probe() -> CheckResult:
    evidence = {}
    passed = True
    for name in ("ioc-partial-fill", "fok-rejection", "post-only-rejection"):
        result = run_mechanics_scenario(name)
        result.engine.assert_invariants()
        evidence[name] = {
            "event_stream_sha256": result.engine.event_stream_sha256(),
            "replay": result.replay.passed,
            "summary": result.summary,
        }
        passed = passed and result.replay.passed
    return _probe("advanced_order_instructions", passed, evidence)


def _latency_probe(seed: int) -> CheckResult:
    evidence = {}
    passed = True
    for name in ("cancel-wins", "fill-wins"):
        result = run_cancel_race(name, seed=seed)
        result.session.assert_invariants()
        evidence[name] = {
            "event_stream_sha256": result.session.event_stream_sha256(),
            "outcome": result.order.cancel_race_outcome,
            "replay": result.replay.passed,
        }
        passed = passed and result.replay.passed
    return _probe("asynchronous_races", passed, evidence)


def _counterfactual_probe() -> CheckResult:
    reports = audit_counterfactuals()
    with TemporaryDirectory(prefix="kirby2-branch-parent-probe-") as temporary:
        root = Path(temporary)
        layout = HotkeyLayout.default()
        session = LiveMarketSession(
            get_scenario_definition("balanced"),
            seed=42,
            duration_seconds=2,
            initial_quantity=200,
            quantity_options=(100, 200, 500),
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
        parent_store = RunStore(root / "parent")
        parent_manifest = parent_store.record_session(recording, session)
        parent_verification = parent_store.verify_run(parent_manifest.run_id)
        branch = run_counterfactual(
            parent_manifest.run_id,
            MutationManifest(
                (
                    ActionMutation(
                        1,
                        expected_command=SessionCommand.BUY_ASK,
                        command=SessionCommand.BUY_BID,
                        quantity=200,
                    ),
                )
            ),
            CounterfactualMode.ENDOGENOUS_FORK,
            parent_store_root=parent_store.root,
        )
        branch_store = CounterfactualStore(root / "branches")
        branch_manifest = branch_store.record(branch)
        branch_verification = branch_store.verify_run(branch_manifest.run_id)

    divergence_index = branch.first_divergence.index
    original_timeline = tuple(
        item.as_dict() for item in branch.original.timeline
    )
    branch_timeline = tuple(item.as_dict() for item in branch.branch.timeline)
    prefix_end = (
        min(len(original_timeline), len(branch_timeline))
        if divergence_index is None
        else divergence_index
    )
    exact_prefix = (
        original_timeline[:prefix_end] == branch_timeline[:prefix_end]
    )
    fork_entry_equal = all(
        (
            bool(original_timeline),
            bool(branch_timeline),
            original_timeline[0] == branch_timeline[0],
            original_timeline[0]["kind"] == "FORK",
            original_timeline[0]["simulation_time_us"]
            == branch.snapshot.fork_time_us,
        )
    )
    mutated_actions = [
        item
        for item in branch_timeline
        if item["kind"] == "PLAYER_ACTION"
        and item["payload"]["action"]["origin"] != "PARENT"
    ]
    mutation_only_after_fork = bool(mutated_actions) and all(
        int(item["simulation_time_us"]) >= branch.snapshot.fork_time_us
        for item in mutated_actions
    )
    parent_link = all(
        (
            parent_verification.passed,
            branch.parent_run_id == parent_manifest.run_id,
            branch.snapshot.parent_run_id == parent_manifest.run_id,
            branch_manifest.parent_run_id == parent_manifest.run_id,
            len(parent_manifest.result_digest) == 64,
        )
    )
    passed = all(
        (
            all(item.passed for item in reports),
            parent_link,
            branch.snapshot_reconstruction_match,
            exact_prefix,
            fork_entry_equal,
            mutation_only_after_fork,
            branch_verification.passed,
        )
    )
    evidence = {
        "audit_case_count": len(reports),
        "audit_cases_sha256": canonical_sha256(
            [item.as_dict() for item in reports]
        ),
        "branch_parent_run_id": branch.parent_run_id,
        "branch_run_id": branch_manifest.run_id,
        "branch_verification": branch_verification.as_dict(),
        "exact_prefix_equality_through_fork": exact_prefix and fork_entry_equal,
        "first_divergence_index": divergence_index,
        "fork_time_us": branch.snapshot.fork_time_us,
        "immutable_branch_verified": branch_verification.passed,
        "mutation_only_after_fork": mutation_only_after_fork,
        "mutated_action_count": len(mutated_actions),
        "parent_link_consistent": parent_link,
        "parent_result_digest": parent_manifest.result_digest,
        "parent_run_id": parent_manifest.run_id,
        "parent_verification": parent_verification.as_dict(),
        "prefix_entry_count": prefix_end,
        "snapshot_digest": branch.snapshot.sha256(),
        "snapshot_reconstruction_match": branch.snapshot_reconstruction_match,
    }
    return _probe("branch_parent_consistency", passed, evidence)


def _calibration_probe(seed: int) -> CheckResult:
    reference = resolve_measurement_source(
        "scenario:balanced",
        seed=seed,
        seconds=1,
    )
    run = calibrate_market(
        reference,
        CalibrationConfig(
            scenario_name="balanced",
            seconds=1,
            stages=(1,),
            fitting_seeds=(101, 202),
            heldout_seeds=(303,),
            search_seed=seed,
            candidate_count_per_stage=2,
            profile_id="audit_lab_holdout_probe",
        ),
    )
    evidence = {
        "final_fitting_loss": run.final_fitting.mean_loss,
        "final_heldout_loss": run.final_heldout.mean_loss,
        "fitting_seeds": list(run.fitting_seeds),
        "heldout_improved": run.heldout_improved,
        "heldout_seeds": list(run.heldout_seeds),
        "run_sha256": run.sha256(),
        "seed_sets_disjoint": not set(run.fitting_seeds) & set(run.heldout_seeds),
    }
    return _probe(
        "calibration_holdout",
        bool(evidence["seed_sets_disjoint"]),
        evidence,
    )


def _market_data_probe() -> CheckResult:
    reports = audit_market_data()
    evidence = {
        "case_count": len(reports),
        "cases": [item.as_dict() for item in reports],
    }
    return _probe(
        "data_quality_faults",
        all(item.passed for item in reports),
        evidence,
    )


def _fault_semantics_probe() -> CheckResult:
    suites = {
        "asynchronous_latency": audit_latency(),
        "market_mechanics": audit_market_mechanics(),
        "multi_venue": audit_multivenue(),
    }
    evidence = {
        name: {
            "case_names": [item.name for item in reports],
            "report_sha256": canonical_sha256([item.as_dict() for item in reports]),
            "status": "PASS" if all(item.passed for item in reports) else "FAIL",
        }
        for name, reports in suites.items()
    }
    invalid = generate_configurations(1, 1)[0].as_dict()
    invalid["schema_version"] = AUDIT_LAB_SCHEMA_VERSION + 1
    schema_rejected = False
    try:
        GeneratedConfiguration.from_dict(invalid)
    except ValueError:
        schema_rejected = True
    evidence["schema_version_gate"] = {
        "status": "PASS" if schema_rejected else "FAIL",
        "unsupported_schema_rejected": schema_rejected,
    }
    return _probe(
        "explicit_fault_semantics",
        all(item["status"] == "PASS" for item in evidence.values()),
        evidence,
    )


def _hidden_probe() -> CheckResult:
    result = run_hidden_liquidity_scenario("iceberg-absorption")
    result.venue.assert_invariants()
    evidence = {
        "observable_feed_sha256": result.venue.observable_feed().sha256(),
        "recording_sha256": result.recording.sha256(),
        "replay": result.replay.passed,
        "summary": result.summary,
    }
    return _probe("hidden_observability", result.replay.passed, evidence)


def _multivenue_probe() -> CheckResult:
    result = run_multivenue_scenario("passive-routing-two-venues")
    result.coordinator.assert_invariants()
    evidence = {
        "event_stream_sha256": result.coordinator.event_stream_sha256(),
        "recording_sha256": result.recording.sha256(),
        "replay": result.replay.passed,
        "summary": result.summary,
    }
    return _probe("multi_venue_reconciliation", result.replay.passed, evidence)


def _agent_probe(seed: int) -> CheckResult:
    evidence = {}
    passed = True
    for population in POPULATION_IDS:
        result = run_agent_ecology(get_population(population), seed=seed)
        replay = replay_agent_ecology(EcologyRecording.capture(result))
        evidence[population] = {
            "public_event_sha256": result.summary.public_event_sha256,
            "result_sha256": result.result_sha256,
            "replay": replay.passed,
            "state_sha256": result.summary.state_sha256,
        }
        passed = passed and replay.passed
    passed = passed and len({item["state_sha256"] for item in evidence.values()}) == len(evidence)
    return _probe("owned_agent_rng", passed, evidence)


def _hawkes_probe(seed: int) -> CheckResult:
    certifications = {
        name: config.stability_certification.as_dict()
        for name, config in sorted(load_accepted_hawkes_configs().items())
    }
    passed = all(
        not str(item["classification"]).startswith("REJECT")
        for item in certifications.values()
    )
    comparison = compare_flow_models(get_scenario_definition("balanced"), seed)
    evidence = {
        "certifications": certifications,
        "clustering_delta": comparison.clustering_delta(),
        "models": [item.as_dict() for item in comparison.models],
    }
    return _probe("hawkes_certification", passed, evidence)
