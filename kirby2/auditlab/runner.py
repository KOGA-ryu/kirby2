"""Orchestration, replay, fresh-process determinism, reduction, and reporting."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .generator import evidence_coverage_report, generate_configurations
from .kernel import failure_signatures, run_generated_case
from .minimizer import minimize_failure
from .models import (
    AUDIT_LAB_SCHEMA_VERSION,
    AcceptanceRecord,
    CheckResult,
    CheckStatus,
    GeneratedCaseResult,
    GeneratedConfiguration,
    MinimizedFailure,
    StatisticalCheck,
    canonical_json,
    canonical_sha256,
)
from .probes import run_subsystem_probes
from .statistics import statistical_checks
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
                all(item.status != "FAIL" for item in self.statistics),
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
    signature_sources: dict[str, GeneratedConfiguration] = {}
    injected_count = 0
    detected_count = 0
    replay_failures = 0
    for configuration in configurations:
        result = run_generated_case(configuration)
        generated_results.append(result)
        replay_configuration = GeneratedConfiguration.from_dict(
            json.loads(canonical_json(configuration.as_dict()))
        )
        replay = run_generated_case(replay_configuration)
        replay_match = replay.result_sha256 == result.result_sha256
        replay_failures += not replay_match
        signatures = failure_signatures(result)
        if result.expected_fault is not None:
            injected_count += 1
            detected_count += result.expected_fault.detected
        for signature in signatures:
            signature_sources.setdefault(signature, configuration)
            if not signature.startswith("EXPECTED_FAULT:"):
                unexpected.append(
                    {
                        "configuration_sha256": configuration.sha256,
                        "signature": signature,
                    }
                )
        if not replay_match:
            signature = "REPLAY_DIGEST_MISMATCH"
            signature_sources.setdefault(signature, configuration)
            unexpected.append(
                {
                    "configuration_sha256": configuration.sha256,
                    "signature": signature,
                }
            )
        cases.append(_compact_case(result, replay_match))
    coverage = evidence_coverage_report(tuple(generated_results))
    minimized = tuple(
        minimize_failure(configuration, signature)
        for signature, configuration in sorted(signature_sources.items())
    )
    statistics = statistical_checks(tuple(cases))
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
    replay_parity = {
        "failed_count": replay_failures,
        "passed_count": budget - replay_failures,
        "status": "PASS" if replay_failures == 0 else "FAIL",
    }
    fault_summary = {
        "detected_count": detected_count,
        "injected_count": injected_count,
        "signature_count": sum(
            signature.startswith("EXPECTED_FAULT:") for signature in signature_sources
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
        result.provenance,
        result.minimized_failures,
        result.statistics,
        result.probes,
        result.acceptance,
        result.unexpected_violations,
        packet,
    )


def _compact_case(
    result: GeneratedCaseResult,
    replay_match: bool,
) -> dict[str, object]:
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
    return {
        "configuration": result.configuration.as_dict(),
        "configuration_sha256": result.configuration.sha256,
        "event_digest": result.event_sha256,
        "exercises": exercises,
        "fault_evidence": (
            None
            if result.expected_fault is None
            else result.expected_fault.as_dict()
        ),
        "failures": [item.as_dict() for item in result.failures],
        "invariant_checks": checks,
        "lane": result.lane.value,
        "metrics": _legacy_statistical_projection(result, typed_metrics),
        "observable_projection_sha256": result.declared_outputs()[
            "observable_projection_sha256"
        ],
        "recording_sha256": result.recording.sha256,
        "recording_type": result.recording.recording_type,
        "replay_digest_parity": replay_match,
        "result_digest": result.result_sha256,
        "source_evidence_sha256": canonical_sha256(
            {"checks": checks, "exercises": exercises}
        ),
        "state_digest": result.state_sha256,
        "status": "PASS" if result.passed and replay_match else "FAIL",
        "typed_metrics": typed_metrics,
        "violations": list(failure_signatures(result)),
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


def _fresh_process_determinism(
    configurations: tuple[GeneratedConfiguration, ...],
    sample_count: int,
) -> dict[str, object]:
    count = min(sample_count, len(configurations))
    indices = sorted(
        {
            round(index * (len(configurations) - 1) / max(1, count - 1))
            for index in range(count)
        }
    )
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


def _acceptance_record(
    seed,
    cases,
    coverage,
    determinism,
    replay_parity,
    fault_summary,
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
        or any(item.status == "FAIL" for item in statistics)
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
        "minimized_failures": canonical_sha256([item.as_dict() for item in minimized]),
        "probes": canonical_sha256([item.as_dict() for item in probes]),
        "replay_parity": canonical_sha256(replay_parity),
        "statistics": canonical_sha256([item.as_dict() for item in statistics]),
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
    fault_rows = [
        {
            "configuration_sha256": item["configuration_sha256"],
            "fault_evidence": item["fault_evidence"],
        }
        for item in result.cases
        if item["fault_evidence"] is not None
    ]
    artifacts = {
        "acceptance_record.json": canonical_json(result.acceptance.as_dict()) + "\n",
        "cases.jsonl": cases_jsonl,
        "coverage.json": canonical_json(result.coverage) + "\n",
        "determinism.json": canonical_json(result.determinism) + "\n",
        "faults.jsonl": "\n".join(canonical_json(item) for item in fault_rows) + "\n",
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
        "unexpected_violations.json": canonical_json(result.unexpected_violations) + "\n",
    }
    if save_failures:
        for minimized in result.minimized_failures:
            reproducer = run_generated_case(minimized.minimized_configuration)
            name = canonical_sha256(minimized.signature)[:20]
            artifacts[f"failures/{name}.json"] = canonical_json(
                {
                    "minimization": minimized.as_dict(),
                    "reproducer": reproducer.as_dict(),
                }
            ) + "\n"
    identity = {
        "acceptance_record_sha256": canonical_sha256(result.acceptance.as_dict()),
        "budget": result.budget,
        "case_result_sha256": canonical_sha256(result.cases),
        "determinism_sha256": canonical_sha256(result.determinism),
        "fault_summary": result.fault_summary,
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
