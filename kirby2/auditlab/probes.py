"""Targeted real-subsystem probes complementing the high-throughput kernel."""

from __future__ import annotations

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
from kirby2.exchange import run_mechanics_scenario
from kirby2.latency import run_cancel_race
from kirby2.multivenue import run_multivenue_scenario
from kirby2.observability import run_hidden_liquidity_scenario
from kirby2.scenarios import get_scenario_definition
from kirby2.simulation import compare_flow_models, load_accepted_hawkes_configs

from .generator import generate_configurations
from .models import GeneratedConfiguration, canonical_sha256


def run_subsystem_probes(seed: int) -> dict[str, dict[str, object]]:
    probes = {
        "advanced_order_instructions": _instruction_probe(),
        "auction_allocation": _auction_probe(),
        "asynchronous_races": _latency_probe(seed),
        "branch_parent_consistency": _counterfactual_probe(),
        "calibration_holdout": _calibration_probe(seed),
        "data_quality_faults": _market_data_probe(),
        "explicit_fault_semantics": _fault_semantics_probe(),
        "hidden_observability": _hidden_probe(),
        "multi_venue_reconciliation": _multivenue_probe(),
        "owned_agent_rng": _agent_probe(seed),
        "hawkes_certification": _hawkes_probe(seed),
    }
    return dict(sorted(probes.items()))


def _probe(status: bool, evidence: dict[str, object]) -> dict[str, object]:
    return {
        "evidence": evidence,
        "evidence_sha256": canonical_sha256(evidence),
        "status": "PASS" if status else "FAIL",
    }


def _auction_probe() -> dict[str, object]:
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
    return _probe(passed, evidence)


def _instruction_probe() -> dict[str, object]:
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
    return _probe(passed, evidence)


def _latency_probe(seed: int) -> dict[str, object]:
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
    return _probe(passed, evidence)


def _counterfactual_probe() -> dict[str, object]:
    reports = audit_counterfactuals()
    evidence = {
        "case_count": len(reports),
        "cases": [item.as_dict() for item in reports],
    }
    return _probe(all(item.passed for item in reports), evidence)


def _calibration_probe(seed: int) -> dict[str, object]:
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
    return _probe(bool(evidence["seed_sets_disjoint"]), evidence)


def _market_data_probe() -> dict[str, object]:
    reports = audit_market_data()
    evidence = {
        "case_count": len(reports),
        "cases": [item.as_dict() for item in reports],
    }
    return _probe(all(item.passed for item in reports), evidence)


def _fault_semantics_probe() -> dict[str, object]:
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
    invalid["schema_version"] = 2
    schema_rejected = False
    try:
        GeneratedConfiguration.from_dict(invalid)
    except ValueError:
        schema_rejected = True
    evidence["schema_version_gate"] = {
        "status": "PASS" if schema_rejected else "FAIL",
        "unsupported_schema_rejected": schema_rejected,
    }
    return _probe(all(item["status"] == "PASS" for item in evidence.values()), evidence)


def _hidden_probe() -> dict[str, object]:
    result = run_hidden_liquidity_scenario("iceberg-absorption")
    result.venue.assert_invariants()
    evidence = {
        "observable_feed_sha256": result.venue.observable_feed().sha256(),
        "recording_sha256": result.recording.sha256(),
        "replay": result.replay.passed,
        "summary": result.summary,
    }
    return _probe(result.replay.passed, evidence)


def _multivenue_probe() -> dict[str, object]:
    result = run_multivenue_scenario("passive-routing-two-venues")
    result.coordinator.assert_invariants()
    evidence = {
        "event_stream_sha256": result.coordinator.event_stream_sha256(),
        "recording_sha256": result.recording.sha256(),
        "replay": result.replay.passed,
        "summary": result.summary,
    }
    return _probe(result.replay.passed, evidence)


def _agent_probe(seed: int) -> dict[str, object]:
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
    return _probe(passed, evidence)


def _hawkes_probe(seed: int) -> dict[str, object]:
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
    return _probe(passed, evidence)
