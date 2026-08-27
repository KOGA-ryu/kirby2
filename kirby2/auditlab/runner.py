"""Orchestration, replay, fresh-process determinism, reduction, and reporting."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kirby2.immutable import thaw_json

from .generator import evidence_coverage_report, generate_configurations
from .fault_oracle import FaultEvaluation, evaluate_fault_observation
from .kernel import failure_signatures, run_generated_case
from .minimizer import minimize_failure
from .models import (
    AUDIT_LAB_SCHEMA_VERSION,
    CASE_RECORDING_SCHEMA_VERSION,
    AcceptanceRecord,
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExecutorLane,
    FailureIdentity,
    FailureKind,
    FailurePredicateKind,
    FaultKind,
    GeneratedCaseResult,
    GeneratedConfiguration,
    MinimizedFailure,
    StatisticalCheck,
    canonical_json,
    canonical_sha256,
)
from .probes import run_subsystem_probes
from .statistics import (
    STATISTICAL_THRESHOLD_MANIFEST_SHA256,
    statistical_checks,
    statistical_threshold_manifest,
    volume_histogram_label,
)
from .store import DEFAULT_AUDIT_LAB_STORE, AuditLabStore, PacketRecord


@dataclass(frozen=True, slots=True)
class AuditLabResult:
    seed: int
    budget: int
    cases: tuple[dict[str, object], ...]
    coverage: dict[str, object]
    determinism: dict[str, object]
    replay_parity: dict[str, object]
    fault_summary: dict[str, object]
    fault_observations: tuple[dict[str, object], ...]
    provenance: dict[str, object]
    minimized_failures: tuple[MinimizedFailure, ...]
    statistics: tuple[StatisticalCheck, ...]
    probes: tuple[CheckResult, ...]
    acceptance: AcceptanceRecord
    unexpected_violations: tuple[dict[str, object], ...]
    packet: PacketRecord | None

    @property
    def passed(self) -> bool:
        return all(
            (
                not self.unexpected_violations,
                self.coverage["status"] == "PASS",
                self.determinism["status"] == "PASS",
                self.replay_parity["status"] == "PASS",
                self.fault_summary["status"] == "PASS",
                all(item.preserved for item in self.minimized_failures),
                all(
                    item.status in {"PASS", "WARNING"}
                    for item in self.statistics
                ),
                all(
                    item.status is CheckStatus.PASS or not item.required
                    for item in self.probes
                ),
                self.packet is None or self.packet.verification_status == "PASS",
            )
        )

    def summary_dict(self) -> dict[str, object]:
        return {
            "acceptance_record": self.acceptance.as_dict(),
            "budget": self.budget,
            "case_result_sha256": canonical_sha256(self.cases),
            "coverage_status": (
                "PASS"
                if self.coverage["status"] == "PASS"
                else "PARTIAL"
            ),
            "determinism": self.determinism,
            "fault_summary": self.fault_summary,
            "fault_observation_sha256": canonical_sha256(
                self.fault_observations
            ),
            "provenance": self.provenance,
            "minimized_failure_count": len(self.minimized_failures),
            "packet": None if self.packet is None else self.packet.as_dict(),
            "probe_status": {
                item.name: item.status.value for item in self.probes
            },
            "replay_parity": self.replay_parity,
            "seed": self.seed,
            "statistical_status": {
                item.name: item.status for item in self.statistics
            },
            "statistical_threshold_manifest_sha256": (
                STATISTICAL_THRESHOLD_MANIFEST_SHA256
            ),
            "status": "PASS" if self.passed else "FAIL",
            "unexpected_violation_count": len(self.unexpected_violations),
        }

    def render(self) -> str:
        summary = self.summary_dict()
        lines = [
            "KIRBY2_MODEL_RISK_LAB",
            f"RUN seed={self.seed} budget={self.budget} status={summary['status']}",
            (
                "CASES "
                f"executed={self.budget} unexpected_violations={len(self.unexpected_violations)} "
                f"replay_parity={self.replay_parity['status']}"
            ),
            (
                "FAULTS "
                f"injected={self.fault_summary['injected_count']} "
                f"detected={self.fault_summary['detected_count']} "
                f"signatures={self.fault_summary['signature_count']} "
                f"minimized={len(self.minimized_failures)}"
            ),
            (
                "DETERMINISM "
                f"status={self.determinism['status']} "
                f"fresh_process_configurations={self.determinism['sample_count']} "
                f"process_runs={self.determinism['process_run_count']}"
            ),
            (
                "PROVENANCE "
                f"git_commit={self.provenance['git_commit']} "
                f"working_tree_dirty={str(self.provenance['working_tree_dirty']).lower()} "
                f"implementation_sha256={self.provenance['implementation_sha256']}"
            ),
            "STATISTICS " + " ".join(
                f"{item.name}={item.status}" for item in self.statistics
            ),
            "PROBES " + " ".join(
                f"{item.name}={item.status.value}" for item in self.probes
            ),
            (
                "MANUAL_ACCEPTANCE "
                f"decision={self.acceptance.reviewer_decision} "
                f"record_id={self.acceptance.record_id}"
            ),
        ]
        if self.packet is not None:
            lines.append(
                f"PACKET id={self.packet.packet_id} path={self.packet.directory} "
                f"verification={self.packet.verification_status}"
            )
        lines.append(f"RUNTIME_INVARIANTS {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def run_audit_lab(
    *,
    budget: int = 10_000,
    seed: int = 771,
    store_root: Path | None = None,
    save_failures: bool = False,
    persist: bool = True,
    fresh_process_samples: int = 3,
    subsystem_probes: bool = True,
) -> AuditLabResult:
    configurations = generate_configurations(seed, budget)
    generated_results: list[GeneratedCaseResult] = []
    cases: list[dict[str, object]] = []
    unexpected: list[dict[str, object]] = []
    failure_sources: dict[
        str,
        tuple[GeneratedConfiguration, FailureIdentity],
    ] = {}
    fault_observations: list[dict[str, object]] = []
    injected_count = 0
    detected_count = 0
    replay_failures: list[dict[str, object]] = []
    loaded_replay_count = 0
    for configuration in configurations:
        result = run_generated_case(configuration)
        generated_results.append(result)
        try:
            serialized_recording = canonical_json(result.recording.as_dict())
            loaded_recording = CaseRecording.from_dict(
                json.loads(serialized_recording)
            )
            from .executors import EXECUTOR_REGISTRY

            replay = EXECUTOR_REGISTRY.replay(loaded_recording)
            parity = _recording_replay_parity(loaded_recording, replay)
            loaded_replay_count += 1
        except Exception as error:  # report the exact loader/replay refusal
            parity = _recording_replay_exception(error)
        replay_match = parity["status"] == "PASS"
        if not replay_match:
            replay_failures.append(
                {
                    "configuration_sha256": configuration.sha256,
                    **parity,
                }
            )
        fault_evaluation: FaultEvaluation | None = None
        if result.fault_observation is not None:
            fault_evaluation = evaluate_fault_observation(
                result.fault_observation
            )
            injected_count += 1
            detected_count += fault_evaluation.detected
            fault_observations.append(
                {
                    "configuration_sha256": configuration.sha256,
                    "evaluation": fault_evaluation.as_dict(),
                    "observation": result.fault_observation.as_dict(),
                }
            )
        represented_fields: set[str] = set()
        for failure in result.failures:
            predicate = (
                FailurePredicateKind.REPLAY_MISMATCH
                if failure.kind is FailureKind.REPLAY_MISMATCH
                else FailurePredicateKind.DETERMINISM_MISMATCH
                if failure.kind is FailureKind.DETERMINISM_MISMATCH
                else FailurePredicateKind.STRUCTURAL_CHECK
            )
            field_name = _failure_field(failure.evidence, failure.code)
            if field_name == failure.code and predicate in {
                FailurePredicateKind.REPLAY_MISMATCH,
                FailurePredicateKind.DETERMINISM_MISMATCH,
            }:
                field_name = "declared_outputs"
            represented_fields.add(field_name)
            identity = failure.identity(
                predicate=predicate,
                lane=result.lane,
                field_name=field_name,
                source_configuration_sha256=configuration.sha256,
                source_recording_sha256=result.recording.sha256,
            )
            _register_unexpected(
                failure_sources,
                unexpected,
                configuration,
                identity,
            )
        for check in result.checks:
            if (
                check.name in represented_fields
                or check.status is CheckStatus.PASS
                or (
                    check.status is CheckStatus.NOT_EXERCISED
                    and not check.required
                )
            ):
                continue
            identity = FailureIdentity(
                predicate=FailurePredicateKind.STRUCTURAL_CHECK,
                kind=FailureKind.INVARIANT_VIOLATION,
                code=f"{result.lane.value}_{check.name.upper()}",
                lane=result.lane,
                field_name=check.name,
                source_configuration_sha256=configuration.sha256,
                source_recording_sha256=result.recording.sha256,
                predicate_parameters={},
            )
            _register_unexpected(
                failure_sources,
                unexpected,
                configuration,
                identity,
            )
        if fault_evaluation is not None and not fault_evaluation.detected:
            fault = result.fault_observation
            if fault is None:
                raise RuntimeError("fault evaluation lost its observation")
            identity = FailureIdentity(
                predicate=FailurePredicateKind.FAULT_MISS,
                kind=FailureKind.DATA_INTEGRITY,
                code="FAULT_MISS",
                lane=result.lane,
                field_name=fault.fault.value,
                source_configuration_sha256=configuration.sha256,
                source_recording_sha256=result.recording.sha256,
                predicate_parameters={"fault": fault.fault.value},
            )
            _register_unexpected(
                failure_sources,
                unexpected,
                configuration,
                identity,
            )
        if not replay_match:
            identity = FailureIdentity(
                predicate=FailurePredicateKind.REPLAY_MISMATCH,
                kind=FailureKind.REPLAY_MISMATCH,
                code="REPLAY_DIGEST_MISMATCH",
                lane=result.lane,
                field_name=str(parity["first_differing_field"]),
                source_configuration_sha256=configuration.sha256,
                source_recording_sha256=result.recording.sha256,
                predicate_parameters={},
            )
            _register_unexpected(
                failure_sources,
                unexpected,
                configuration,
                identity,
            )
        cases.append(_compact_case(result, parity, fault_evaluation))
    coverage = evidence_coverage_report(tuple(generated_results))
    statistics = statistical_checks(tuple(cases), seed)
    probes = (
        run_subsystem_probes(seed)
        if subsystem_probes
        else (
            CheckResult(
                name="subsystem_probes",
                status=CheckStatus.NOT_EXERCISED,
                required=False,
                detail="subsystem probes explicitly disabled by caller",
                evidence={"source": "run_audit_lab", "reason": "caller_disabled"},
            ),
        )
    )
    determinism = _fresh_process_determinism(
        configurations,
        max(1, fresh_process_samples),
    )
    result_by_configuration = {
        item.configuration.sha256: item for item in generated_results
    }
    configuration_by_sha256 = {
        item.sha256: item for item in configurations
    }
    for evidence in determinism["evidence"]:
        if evidence["matched"]:
            continue
        configuration = configuration_by_sha256[
            str(evidence["configuration_sha256"])
        ]
        source = result_by_configuration[configuration.sha256]
        identity = FailureIdentity(
            predicate=FailurePredicateKind.DETERMINISM_MISMATCH,
            kind=FailureKind.DETERMINISM_MISMATCH,
            code="FRESH_PROCESS_DETERMINISM_MISMATCH",
            lane=configuration.lane,
            field_name="declared_outputs",
            source_configuration_sha256=configuration.sha256,
            source_recording_sha256=source.recording.sha256,
            predicate_parameters={},
        )
        _register_unexpected(
            failure_sources,
            unexpected,
            configuration,
            identity,
        )
    representative_configuration = configurations[0]
    representative_result = generated_results[0]
    for probe in probes:
        if not probe.required or probe.status is CheckStatus.PASS:
            continue
        identity = FailureIdentity(
            predicate=FailurePredicateKind.SUBSYSTEM_PROBE,
            kind=FailureKind.INVARIANT_VIOLATION,
            code="SUBSYSTEM_PROBE_FAILURE",
            lane=representative_configuration.lane,
            field_name=probe.name,
            source_configuration_sha256=representative_configuration.sha256,
            source_recording_sha256=representative_result.recording.sha256,
            predicate_parameters={"probe_seed": seed},
        )
        _register_unexpected(
            failure_sources,
            unexpected,
            representative_configuration,
            identity,
        )
    minimized_items: list[MinimizedFailure] = []
    reproducible_signatures: set[str] = set()
    for signature, (configuration, identity) in sorted(
        failure_sources.items()
    ):
        minimized_item = minimize_failure(configuration, identity)
        if minimized_item is None:
            continue
        minimized_items.append(minimized_item)
        reproducible_signatures.add(signature)
    minimized = tuple(minimized_items)
    unexpected = [
        {
            **item,
            "reproducible": item["signature"] in reproducible_signatures,
        }
        for item in unexpected
    ]
    replay_parity = {
        "failed_count": len(replay_failures),
        "failures": replay_failures,
        "loaded_replay_count": loaded_replay_count,
        "passed_count": budget - len(replay_failures),
        "recording_schema_version": CASE_RECORDING_SCHEMA_VERSION,
        "status": "PASS" if not replay_failures else "FAIL",
    }
    fault_summary = {
        "detected_count": detected_count,
        "injected_count": injected_count,
        "miss_count": injected_count - detected_count,
        "observation_count": len(fault_observations),
        "signature_count": len(
            {
                item["evaluation"]["observed_code"]
                for item in fault_observations
                if item["evaluation"]["observed_code"] is not None
            }
        ),
        "status": "PASS" if detected_count == injected_count else "FAIL",
    }
    provenance = _repository_provenance()
    acceptance = _acceptance_record(
        seed,
        tuple(cases),
        coverage,
        determinism,
        replay_parity,
        fault_summary,
        tuple(fault_observations),
        tuple(minimized),
        statistics,
        probes,
        tuple(unexpected),
        provenance,
    )
    result = AuditLabResult(
        seed,
        budget,
        tuple(cases),
        coverage,
        determinism,
        replay_parity,
        fault_summary,
        tuple(fault_observations),
        provenance,
        minimized,
        statistics,
        probes,
        acceptance,
        tuple(unexpected),
        None,
    )
    if not persist:
        return result
    store = AuditLabStore(store_root or DEFAULT_AUDIT_LAB_STORE)
    packet = _persist_result(store, result, save_failures)
    return AuditLabResult(
        result.seed,
        result.budget,
        result.cases,
        result.coverage,
        result.determinism,
        result.replay_parity,
        result.fault_summary,
        result.fault_observations,
        result.provenance,
        result.minimized_failures,
        result.statistics,
        result.probes,
        result.acceptance,
        result.unexpected_violations,
        packet,
    )


def _register_unexpected(
    sources: dict[str, tuple[GeneratedConfiguration, FailureIdentity]],
    unexpected: list[dict[str, object]],
    configuration: GeneratedConfiguration,
    identity: FailureIdentity,
) -> None:
    signature = identity.signature
    if signature in sources:
        return
    sources[signature] = (configuration, identity)
    unexpected.append(
        {
            "configuration_sha256": configuration.sha256,
            "identity": identity.as_dict(),
            "reproducible": None,
            "signature": signature,
        }
    )


def _failure_field(evidence: Mapping[str, object], fallback: str) -> str:
    for name in ("check", "field_name", "field", "capability"):
        value = evidence.get(name)
        if type(value) is str and value:
            return value
    return fallback


def _compact_case(
    result: GeneratedCaseResult,
    replay_parity: dict[str, object],
    fault_evaluation: FaultEvaluation | None,
) -> dict[str, object]:
    replay_match = replay_parity["status"] == "PASS"
    exercises = [
        {
            **item.as_dict(),
            "evidence_sha256": canonical_sha256(item.as_dict()["evidence"]),
        }
        for item in result.exercises
    ]
    checks = [
        {
            **item.as_dict(),
            "evidence_sha256": canonical_sha256(item.as_dict()["evidence"]),
        }
        for item in result.checks
    ]
    typed_metrics = result.declared_outputs()["metrics"]
    if not isinstance(typed_metrics, dict):
        raise TypeError("generated case metrics must serialize as an object")
    legacy_metrics = _legacy_statistical_projection(result, typed_metrics)
    return {
        "configuration": result.configuration.as_dict(),
        "configuration_sha256": result.configuration.sha256,
        "event_digest": result.event_sha256,
        "exercises": exercises,
        "fault_evaluation": (
            None
            if fault_evaluation is None
            else fault_evaluation.as_dict()
        ),
        "fault_observation": (
            None
            if result.fault_observation is None
            else result.fault_observation.as_dict()
        ),
        "failures": [item.as_dict() for item in result.failures],
        "invariant_checks": checks,
        "lane": result.lane.value,
        "metrics": legacy_metrics,
        "observable_projection_sha256": result.declared_outputs()[
            "observable_projection_sha256"
        ],
        "recording_sha256": result.recording.sha256,
        "recording_schema_version": result.recording.schema_version,
        "recording_type": result.recording.recording_type,
        "replay_parity": replay_parity,
        "replay_digest_parity": replay_match,
        "result_digest": result.result_sha256,
        "source_evidence_sha256": canonical_sha256(
            {"checks": checks, "exercises": exercises}
        ),
        "state_digest": result.state_sha256,
        "statistical_evidence": _statistical_evidence_projection(
            result,
            legacy_metrics,
        ),
        "status": "PASS" if result.passed and replay_match else "FAIL",
        "typed_metrics": typed_metrics,
        "violations": list(failure_signatures(result)),
    }


_REPLAY_DIGEST_FIELDS = (
    ("event", "event_sha256"),
    ("state", "state_sha256"),
    ("observable", "observable_sha256"),
    ("metrics", "metrics_sha256"),
    ("declared_outputs", "declared_outputs_sha256"),
)


def _recording_replay_parity(
    recording: CaseRecording,
    replay: GeneratedCaseResult,
) -> dict[str, object]:
    expected = recording.expected_outputs
    expected_digests = expected.get("digests")
    actual = replay.replay_expectations()
    actual_digests = actual.get("digests")
    if not isinstance(expected_digests, Mapping):
        raise TypeError("recording expected digests must be an object")
    if not isinstance(actual_digests, Mapping):
        raise TypeError("replay digests must be an object")
    field_digests: dict[str, dict[str, object]] = {}
    first_differing_field: str | None = None
    for field_name, digest_name in _REPLAY_DIGEST_FIELDS:
        expected_sha256 = expected_digests.get(digest_name)
        actual_sha256 = actual_digests.get(digest_name)
        matches = expected_sha256 == actual_sha256
        field_digests[field_name] = {
            "actual_sha256": actual_sha256,
            "expected_sha256": expected_sha256,
            "matches": matches,
        }
        if not matches and first_differing_field is None:
            first_differing_field = field_name
    return {
        "field_digests": field_digests,
        "first_differing_field": first_differing_field,
        "loaded_recording_sha256": recording.sha256,
        "status": "PASS" if first_differing_field is None else "FAIL",
    }


def _recording_replay_exception(error: Exception) -> dict[str, object]:
    return {
        "exception_message": str(error),
        "exception_type": type(error).__name__,
        "field_digests": {},
        "first_differing_field": "replay_exception",
        "loaded_recording_sha256": None,
        "status": "FAIL",
    }


def _legacy_statistical_projection(
    result: GeneratedCaseResult,
    typed_metrics: dict[str, object],
) -> dict[str, object]:
    """Preserve pre-ATR-17 screens without inventing subsystem behavior."""

    metrics = dict(typed_metrics)
    metrics.setdefault("event_count", len(result.event_projection))
    metrics.setdefault(
        "trade_count",
        typed_metrics.get("core_trade_count", 0),
    )
    metrics.setdefault(
        "traded_volume",
        next(
            (
                typed_metrics[name]
                for name in (
                    "traded_volume_shares",
                    "route_completed_quantity",
                    "client_filled_quantity",
                    "auction_matched_volume_shares",
                )
                if name in typed_metrics
            ),
            0,
        ),
    )
    metrics.setdefault("price_displacement_ticks", 0)
    ending_bid = typed_metrics.get("ending_best_bid_ticks")
    ending_ask = typed_metrics.get("ending_best_ask_ticks")
    metrics.setdefault(
        "spread_ticks",
        (
            ending_ask - ending_bid
            if type(ending_bid) is int and type(ending_ask) is int
            else None
        ),
    )
    return dict(sorted(metrics.items()))


def _statistical_evidence_projection(
    result: GeneratedCaseResult,
    metrics: dict[str, object],
) -> dict[str, object]:
    payload = _thawed_object(result.recording.payload, "recording payload")
    final_state = _thawed_object(
        result.final_state_projection,
        "final state projection",
    )
    observable = _thawed_object(
        result.observable_projection,
        "observable projection",
    )
    events = tuple(
        _thawed_object(item, "event projection")
        for item in result.event_projection
    )
    reference = _recorded_price_evidence(
        result.lane,
        payload,
        final_state,
        observable,
        events,
    )
    duration = metrics.get("simulation_duration_us")
    if type(duration) is not int:
        per_leg = metrics.get("simulation_duration_us_per_leg")
        leg_count = metrics.get("leg_count")
        duration = (
            per_leg * leg_count
            if type(per_leg) is int and type(leg_count) is int
            else None
        )
    trade_count = metrics.get("trade_count")
    if result.lane is ExecutorLane.ALGORITHM:
        raw_legs = payload.get("legs")
        if isinstance(raw_legs, list):
            trade_count = sum(
                len(leg.get("client_fills", []))
                for leg in raw_legs
                if isinstance(leg, dict)
            )
    if type(trade_count) is not int:
        trade_count = 0
    evidence: dict[str, object] = {
        **reference,
        "continuous_trade_eligible": _continuous_trade_eligible(result),
        "sensitivity_event_count": len(events),
        "simulation_duration_us": duration,
        "trade_count": trade_count,
    }

    if result.lane is ExecutorLane.CORE_FLOW:
        family_histogram: Counter[str] = Counter()
        volume_histogram: Counter[str] = Counter()
        for event in events:
            if event.get("record_type") != "flow_event" or event.get("applied") is not True:
                continue
            family = event.get("family")
            if type(family) is str:
                family_histogram[family] += 1
            raw_command = event.get("command")
            quantity = 0
            if isinstance(raw_command, dict):
                raw_quantity = raw_command.get("quantity", 0)
                if type(raw_quantity) is int and raw_quantity >= 0:
                    quantity = raw_quantity
            volume_histogram[volume_histogram_label(quantity)] += 1
        configured_cap: object = None
        for check in result.checks:
            if check.name == "event_rate_cap":
                configured_cap = check.evidence.get(
                    "configured_cap_events_per_second"
                )
                break
        raw_flow_count = metrics.get("flow_event_count")
        evidence.update(
            {
                "configured_event_rate_cap_eps": configured_cap,
                "core_flow_event_count": (
                    raw_flow_count if type(raw_flow_count) is int else None
                ),
                "core_flow_event_family_histogram": dict(
                    sorted(family_histogram.items())
                ),
                "core_flow_volume_histogram": dict(
                    sorted(volume_histogram.items())
                ),
            }
        )

    if result.lane is ExecutorLane.FRAGMENTED:
        raw_intervals = payload.get("observable_crossed_intervals")
        evidence.update(
            {
                "crossed_composite_intervals": (
                    raw_intervals if isinstance(raw_intervals, list) else []
                ),
                "maximum_configured_market_data_latency_us": (
                    _maximum_market_data_latency_us(payload)
                ),
            }
        )

    if result.lane is ExecutorLane.ALGORITHM:
        mapping = payload.get("execution_mapping")
        raw_legs = payload.get("legs")
        numerator = 0
        denominator = 0
        if isinstance(raw_legs, list):
            for leg in raw_legs:
                if not isinstance(leg, dict):
                    continue
                raw_metrics = leg.get("metrics")
                if not isinstance(raw_metrics, dict):
                    continue
                raw_cost = raw_metrics.get(
                    "implementation_shortfall_x2_tick_shares"
                )
                raw_target = raw_metrics.get("target_quantity")
                if type(raw_cost) is int and type(raw_target) is int:
                    numerator += raw_cost
                    denominator += raw_target
        if isinstance(mapping, dict):
            evidence.update(
                {
                    "algorithm_cost_denominator_shares": denominator,
                    "algorithm_cost_numerator_x2_ticks": numerator,
                    "algorithm_objective": mapping.get("configured_objective"),
                    "algorithm_scenario": mapping.get("scenario_name"),
                    "algorithm_strategy": mapping.get("strategy"),
                }
            )
    return dict(sorted(evidence.items()))


def _continuous_trade_eligible(result: GeneratedCaseResult) -> bool:
    configuration = result.configuration
    if result.lane in {
        ExecutorLane.CORE_FLOW,
        ExecutorLane.LATENCY,
        ExecutorLane.FRAGMENTED,
        ExecutorLane.ECOLOGY,
    }:
        return True
    if result.lane is ExecutorLane.MECHANICS:
        return configuration.session_phase == "CONTINUOUS"
    if result.lane is ExecutorLane.ALGORITHM:
        return configuration.objective != "OBSERVE_ONLY"
    return False


def _recorded_price_evidence(
    lane: ExecutorLane,
    payload: dict[str, object],
    final_state: dict[str, object],
    observable: dict[str, object],
    events: tuple[dict[str, object], ...],
) -> dict[str, object]:
    initial: int | None = None
    source: str | None = None
    samples: list[int] = []
    if lane is ExecutorLane.CORE_FLOW:
        raw_initial_count = final_state.get("initial_exchange_event_count")
        bid: int | None = None
        ask: int | None = None
        for event in events:
            if event.get("record_type") != "exchange_event":
                continue
            raw_sequence = event.get("sequence")
            event_type = event.get("type")
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            new_price = data.get("new_price_ticks")
            if event_type == "BEST_BID_CHANGED":
                bid = new_price if type(new_price) is int else None
            elif event_type == "BEST_ASK_CHANGED":
                ask = new_price if type(new_price) is int else None
            elif event_type == "TRADE" and type(data.get("price_ticks")) is int:
                samples.append(int(data["price_ticks"]) * 2)
            if bid is not None and ask is not None:
                samples.append(bid + ask)
            if (
                initial is None
                and type(raw_initial_count) is int
                and type(raw_sequence) is int
                and raw_sequence == raw_initial_count
                and bid is not None
                and ask is not None
            ):
                initial = bid + ask
        if initial is None and bid is not None and ask is not None:
            initial = bid + ask
        source = "recorded_initial_exchange_event_prefix"
    elif lane is ExecutorLane.MECHANICS:
        native = payload.get("native_recording")
        commands = native.get("commands") if isinstance(native, dict) else None
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, dict):
                    continue
                parameters = command.get("parameters")
                request = (
                    parameters.get("request")
                    if isinstance(parameters, dict)
                    else None
                )
                price = request.get("price_ticks") if isinstance(request, dict) else None
                if type(price) is int:
                    initial = price * 2
                    break
        for event in events:
            if event.get("event_type") not in {"TRADE", "AUCTION_FILL"}:
                continue
            data = event.get("data")
            price = data.get("price_ticks") if isinstance(data, dict) else None
            if type(price) is int:
                samples.append(price * 2)
        source = "native_mechanics_first_price_bearing_order"
    elif lane is ExecutorLane.LATENCY:
        native = payload.get("native_recording")
        if isinstance(native, dict):
            bid = native.get("initial_bid_ticks")
            ask = native.get("initial_ask_ticks")
            if type(bid) is int and type(ask) is int:
                initial = bid + ask
        samples.extend(_book_trade_prices_x2(final_state.get("book")))
        source = "native_latency_initial_bid_ask"
    elif lane is ExecutorLane.FRAGMENTED:
        initial = _fragmented_initial_reference_x2(payload)
        for event in events:
            if event.get("record_type") != "venue_truth_event" or event.get(
                "event_type"
            ) != "TRADE":
                continue
            data = event.get("data")
            price = data.get("price_x2") if isinstance(data, dict) else None
            if type(price) is int:
                samples.append(price)
        feed = observable.get("consolidated_feed")
        if isinstance(feed, dict):
            bid = feed.get("best_bid_ticks")
            ask = feed.get("best_ask_ticks")
            if type(bid) is int and type(ask) is int:
                samples.append(bid + ask)
        source = "native_fragmented_time_zero_composite"
    elif lane is ExecutorLane.ECOLOGY:
        native = payload.get("native_recording")
        population = (
            native.get("population_definition")
            if isinstance(native, dict)
            else None
        )
        summary = native.get("expected_summary") if isinstance(native, dict) else None
        if isinstance(population, dict) and type(population.get("initial_mid_ticks")) is int:
            initial = int(population["initial_mid_ticks"]) * 2
        if isinstance(summary, dict):
            for name in (
                "low_trade_price_ticks",
                "high_trade_price_ticks",
                "last_trade_price_ticks",
            ):
                price = summary.get(name)
                if type(price) is int:
                    samples.append(price * 2)
            bid = summary.get("ending_best_bid_ticks")
            ask = summary.get("ending_best_ask_ticks")
            if type(bid) is int and type(ask) is int:
                samples.append(bid + ask)
        source = "native_ecology_population_initial_mid"
    elif lane is ExecutorLane.ALGORITHM:
        raw_legs = payload.get("legs")
        if isinstance(raw_legs, list):
            for leg in raw_legs:
                if not isinstance(leg, dict):
                    continue
                objective = leg.get("objective")
                reference = (
                    objective.get("arrival_midpoint_x2")
                    if isinstance(objective, dict)
                    else None
                )
                if initial is None and type(reference) is int:
                    initial = reference
                fills = leg.get("client_fills")
                if isinstance(fills, list):
                    for fill in fills:
                        price = fill.get("price_x2") if isinstance(fill, dict) else None
                        if type(price) is int:
                            samples.append(price)
                decisions = leg.get("decisions")
                if isinstance(decisions, list):
                    for decision in decisions:
                        observation = (
                            decision.get("observation")
                            if isinstance(decision, dict)
                            else None
                        )
                        features = (
                            observation.get("observable_market_features")
                            if isinstance(observation, dict)
                            else None
                        )
                        midpoint = (
                            features.get("midpoint_x2")
                            if isinstance(features, dict)
                            else None
                        )
                        if type(midpoint) is int:
                            samples.append(midpoint)
        source = "native_algorithm_objective_arrival_midpoint"
    if initial is not None:
        samples.append(initial)
    displacement = (
        None
        if initial is None
        else max((abs(item - initial) for item in samples), default=0)
    )
    return {
        "initial_reference_source": source,
        "initial_reference_x2_ticks": initial,
        "maximum_reference_displacement_x2_ticks": displacement,
    }


def _fragmented_initial_reference_x2(payload: dict[str, object]) -> int | None:
    native = payload.get("native_recording")
    commands = native.get("commands") if isinstance(native, dict) else None
    if not isinstance(commands, list):
        return None
    bids: list[int] = []
    asks: list[int] = []
    for command in commands:
        if not isinstance(command, dict) or command.get("simulation_time_us") != 0:
            continue
        if command.get("command_type") != "ADD":
            continue
        parameters = command.get("parameters")
        request = parameters.get("request") if isinstance(parameters, dict) else None
        if not isinstance(request, dict):
            continue
        price = request.get("price_ticks")
        side = request.get("side")
        if type(price) is not int:
            continue
        if side == "buy":
            bids.append(price)
        elif side == "sell":
            asks.append(price)
    return max(bids) + min(asks) if bids and asks else None


def _maximum_market_data_latency_us(payload: dict[str, object]) -> int:
    native = payload.get("native_recording")
    venue_configs = native.get("venue_configs") if isinstance(native, dict) else None
    maximum = 0
    if not isinstance(venue_configs, list):
        return maximum
    for venue in venue_configs:
        profile = venue.get("latency_profile") if isinstance(venue, dict) else None
        components = profile.get("components") if isinstance(profile, dict) else None
        distribution = (
            components.get("market_data_publication_latency")
            if isinstance(components, dict)
            else None
        )
        upper = distribution.get("upper_us") if isinstance(distribution, dict) else None
        if type(upper) is int:
            maximum = max(maximum, upper)
    return maximum


def _book_trade_prices_x2(value: object) -> list[int]:
    if not isinstance(value, dict):
        return []
    trades = value.get("trades")
    if not isinstance(trades, list):
        return []
    return [
        int(item["price_ticks"]) * 2
        for item in trades
        if isinstance(item, dict) and type(item.get("price_ticks")) is int
    ]


def _thawed_object(value: object, name: str) -> dict[str, object]:
    thawed = thaw_json(value)
    if not isinstance(thawed, dict):
        raise TypeError(f"{name} must be an object")
    return thawed


def _fresh_process_determinism(
    configurations: tuple[GeneratedConfiguration, ...],
    sample_count: int,
) -> dict[str, object]:
    indices = _determinism_sample_indices(configurations, sample_count)
    evidence = []
    failures = []
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "0"
    for index in indices:
        configuration = configurations[index]
        expected = canonical_json(
            run_generated_case(configuration).declared_outputs()
        )
        outputs = []
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, "-m", "kirby2.auditlab.worker"],
                input=canonical_json(configuration.as_dict()),
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            if completed.returncode != 0:
                failures.append(
                    f"configuration {configuration.sha256} worker exit {completed.returncode}: "
                    + completed.stderr.strip()
                )
                outputs.append("")
            else:
                outputs.append(completed.stdout.strip())
        matched = len(set(outputs)) == 1 and outputs[0] == expected
        if not matched:
            failures.append(f"configuration {configuration.sha256} fresh-process mismatch")
        evidence.append(
            {
                "configuration_sha256": configuration.sha256,
                "declared_output_sha256": canonical_sha256(expected),
                "fault": (
                    None
                    if configuration.injected_fault is None
                    else configuration.injected_fault.value
                ),
                "lane": configuration.lane.value,
                "matched": matched,
            }
        )
    return {
        "evidence": evidence,
        "failures": failures,
        "process_run_count": len(indices) * 2,
        "sample_count": len(indices),
        "status": "PASS" if not failures else "FAIL",
    }


def _determinism_sample_indices(
    configurations: tuple[GeneratedConfiguration, ...],
    requested_count: int,
) -> tuple[int, ...]:
    selected: set[int] = set()
    for lane in ExecutorLane:
        index = next(
            (
                index
                for index, configuration in enumerate(configurations)
                if configuration.lane is lane
            ),
            None,
        )
        if index is not None:
            selected.add(index)
    for fault in FaultKind:
        index = next(
            (
                index
                for index, configuration in enumerate(configurations)
                if configuration.injected_fault is fault
            ),
            None,
        )
        if index is not None:
            selected.add(index)
    target = min(len(configurations), max(requested_count, len(selected)))
    if len(selected) < target:
        for index in range(len(configurations)):
            selected.add(index)
            if len(selected) == target:
                break
    return tuple(sorted(selected))


def _acceptance_record(
    seed,
    cases,
    coverage,
    determinism,
    replay_parity,
    fault_summary,
    fault_observations,
    minimized,
    statistics,
    probes,
    unexpected,
    provenance,
) -> AcceptanceRecord:
    warning_names = tuple(item.name for item in statistics if item.status == "WARNING")
    failed = (
        bool(unexpected)
        or coverage["status"] != "PASS"
        or determinism["status"] != "PASS"
        or replay_parity["status"] != "PASS"
        or fault_summary["status"] != "PASS"
        or any(
            item.status in {"FAIL", "NOT_EXERCISED"}
            for item in statistics
        )
        or any(
            item.required and item.status is not CheckStatus.PASS
            for item in probes
        )
        or any(not item.preserved for item in minimized)
    )
    artifacts = {
        "cases": canonical_sha256(cases),
        "coverage": canonical_sha256(coverage),
        "determinism": canonical_sha256(determinism),
        "fault_summary": canonical_sha256(fault_summary),
        "fault_observations": canonical_sha256(fault_observations),
        "minimized_failures": canonical_sha256([item.as_dict() for item in minimized]),
        "probes": canonical_sha256([item.as_dict() for item in probes]),
        "replay_parity": canonical_sha256(replay_parity),
        "statistics": canonical_sha256([item.as_dict() for item in statistics]),
        "statistical_threshold_manifest": (
            STATISTICAL_THRESHOLD_MANIFEST_SHA256
        ),
        "implementation": str(provenance["implementation_sha256"]),
    }
    identity = {
        "artifact_digests": artifacts,
        "seed": seed,
        "warnings": warning_names,
    }
    return AcceptanceRecord(
        record_id=f"acceptance-{canonical_sha256(identity)[:20]}",
        scenario_version=AUDIT_LAB_SCHEMA_VERSION,
        seed=seed,
        reviewer_decision=(
            "REJECT_AUTOMATED_PRECHECK" if failed else "PENDING_HUMAN_REVIEW"
        ),
        observed_characteristics=(
            f"{len(cases)} generated configurations executed",
            f"{len(minimized)} stable violation signatures minimized",
            "fresh-process determinism and loaded replay parity measured",
            "player-observable layer checked by explicit field allowlist",
        ),
        known_defects=(
            *(f"STATISTICAL_WARNING:{name}" for name in warning_names),
            *(f"UNEXPECTED:{item['signature']}" for item in unexpected),
        ),
        artifact_digests=artifacts,
        supersedes_record_id=None,
    )


def _persist_result(
    store: AuditLabStore,
    result: AuditLabResult,
    save_failures: bool,
) -> PacketRecord:
    cases_jsonl = "\n".join(canonical_json(item) for item in result.cases) + "\n"
    artifacts = {
        "acceptance_record.json": canonical_json(result.acceptance.as_dict()) + "\n",
        "cases.jsonl": cases_jsonl,
        "coverage.json": canonical_json(result.coverage) + "\n",
        "determinism.json": canonical_json(result.determinism) + "\n",
        "faults.jsonl": "\n".join(
            canonical_json(item) for item in result.fault_observations
        )
        + "\n",
        "minimized_failures.json": canonical_json(
            [item.as_dict() for item in result.minimized_failures]
        )
        + "\n",
        "probes.json": canonical_json(
            [item.as_dict() for item in result.probes]
        )
        + "\n",
        "replay_parity.json": canonical_json(result.replay_parity) + "\n",
        "statistics.json": canonical_json(
            [item.as_dict() for item in result.statistics]
        )
        + "\n",
        "statistical_thresholds.json": canonical_json(
            statistical_threshold_manifest()
        )
        + "\n",
        "unexpected_violations.json": canonical_json(result.unexpected_violations) + "\n",
    }
    if save_failures:
        for minimized in result.minimized_failures:
            name = canonical_sha256(minimized.signature)[:20]
            artifacts[f"failures/{name}.json"] = canonical_json(
                {
                    "minimization": minimized.as_dict(),
                    "stored_reproducer": {
                        "final_recording": minimized.final_recording.as_dict(),
                        "verification_digests": list(
                            minimized.verification_digests
                        ),
                        "verification_reproduced": list(
                            minimized.verification_reproduced
                        ),
                    },
                }
            ) + "\n"
    identity = {
        "acceptance_record_sha256": canonical_sha256(result.acceptance.as_dict()),
        "budget": result.budget,
        "case_result_sha256": canonical_sha256(result.cases),
        "determinism_sha256": canonical_sha256(result.determinism),
        "fault_summary": result.fault_summary,
        "fault_observations_sha256": canonical_sha256(
            result.fault_observations
        ),
        "provenance": result.provenance,
        "minimized_failures_sha256": canonical_sha256(
            [item.as_dict() for item in result.minimized_failures]
        ),
        "probe_sha256": canonical_sha256(
            [item.as_dict() for item in result.probes]
        ),
        "save_failures": save_failures,
        "schema_version": AUDIT_LAB_SCHEMA_VERSION,
        "seed": result.seed,
        "statistics_sha256": canonical_sha256(
            [item.as_dict() for item in result.statistics]
        ),
    }
    human = result.render().replace(
        "RUNTIME_INVARIANTS",
        "PACKET id=SEE_MANIFEST\nRUNTIME_INVARIANTS",
    )
    artifacts["report.txt"] = human + "\n"
    packet = store.record(identity, artifacts)
    store.record_acceptance(result.acceptance)
    ledger_verification = store.verify_ledgers()
    if ledger_verification["status"] != "PASS":
        raise RuntimeError(
            "immutable audit ledgers failed verification: "
            + "; ".join(ledger_verification["failures"])
        )
    return packet


def _repository_provenance() -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    implementation_paths = (
        repository / "pyproject.toml",
        repository / "kirby2" / "__main__.py",
        repository / "kirby2" / "audit" / "model_risk_lab.py",
        *sorted((repository / "kirby2" / "auditlab").glob("*.py")),
        repository / "kirby2" / "auditlab" / "README.md",
        repository / "kirby2" / "latency" / "diagnostics.py",
        repository / "kirby2" / "multivenue" / "diagnostics.py",
    )
    implementation = {
        str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in implementation_paths
    }

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else "UNAVAILABLE"

    status = git("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "dirty_state_sha256": canonical_sha256(status.splitlines()),
        "git_commit": git("rev-parse", "HEAD"),
        "implementation_file_count": len(implementation),
        "implementation_sha256": canonical_sha256(implementation),
        "software_version": "0.1.0",
        "working_tree_dirty": status not in {"", "UNAVAILABLE"},
    }
