"""Runtime acceptance audit for the generative model-risk laboratory."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.auditlab import AuditLabStore, FaultKind, run_audit_lab


@dataclass(frozen=True, slots=True)
class ModelRiskLabAuditCase:
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


def audit_model_risk_lab() -> tuple[ModelRiskLabAuditCase, ...]:
    with TemporaryDirectory(prefix="kirby2-model-risk-audit-") as temporary:
        root = Path(temporary)
        result = run_audit_lab(
            budget=256,
            seed=771,
            store_root=root,
            save_failures=True,
            fresh_process_samples=2,
        )
        packet = result.packet
        if packet is None:
            raise RuntimeError("model-risk audit did not create its evidence packet")
        store = AuditLabStore(root)
        superseding = replace(
            result.acceptance,
            record_id="acceptance-audit-supersession-probe",
            reviewer_decision="AUDIT_SUPERSESSION_PROBE",
            supersedes_record_id=result.acceptance.record_id,
        )
        superseding_path = store.record_acceptance(superseding)
        overwrite_rejected = False
        try:
            store.record_acceptance(
                replace(superseding, reviewer_decision="FORGED_OVERWRITE")
            )
        except RuntimeError:
            overwrite_rejected = True
        ledger_verification = store.verify_ledgers()
        initial_verification = store.verify(packet.packet_id)
        artifact = packet.directory / "statistics.json"
        original = artifact.read_bytes()
        artifact.write_bytes(original + b"tamper")
        tampered_verification = store.verify(packet.packet_id)
        artifact.write_bytes(original)
        restored_verification = store.verify(packet.packet_id)
        cases = (
            _coverage_case(result),
            _structural_case(result),
            _fault_case(result),
            _determinism_case(result),
            _minimization_case(result),
            _statistics_case(result),
            _probe_case(result),
            _immutable_case(
                packet.as_dict(),
                initial_verification.verification_status,
                tampered_verification.verification_status,
                restored_verification.verification_status,
                superseding_path.is_file(),
                len(store.acceptance_ledger.read_text(encoding="utf-8").splitlines()),
                overwrite_rejected,
                ledger_verification,
            ),
        )
        return cases


def _coverage_case(result) -> ModelRiskLabAuditCase:
    missing = [name for name, item in result.coverage.items() if item["status"] != "PASS"]
    return ModelRiskLabAuditCase(
        "all_fourteen_configuration_dimensions_and_fault_families_vary",
        {
            "required_axis_count": 14,
            "extra_minimization_axis": "agent_count",
            "coverage": result.coverage,
            "generated_cases": result.budget,
        },
        () if not missing else (f"partial generated coverage: {missing}",),
    )


def _structural_case(result) -> ModelRiskLabAuditCase:
    failed_checks = sorted(
        {
            name
            for case in result.cases
            for name, passed in case["invariant_checks"].items()
            if not passed
        }
    )
    return ModelRiskLabAuditCase(
        "structural_invariants_enforced_on_every_generated_case",
        {
            "case_count": len(result.cases),
            "failed_check_names": failed_checks,
            "unexpected_violation_count": len(result.unexpected_violations),
        },
        () if not failed_checks and not result.unexpected_violations else (
            "one or more structural invariants failed",
        ),
    )


def _fault_case(result) -> ModelRiskLabAuditCase:
    observed = {
        case["fault_evidence"]["fault"]
        for case in result.cases
        if case["fault_evidence"] is not None
    }
    expected = {item.value for item in FaultKind}
    failures = []
    if observed != expected:
        failures.append("not all explicit fault types were injected")
    if result.fault_summary["status"] != "PASS":
        failures.append("one or more explicit faults escaped detection")
    return ModelRiskLabAuditCase(
        "all_faults_explicit_recorded_and_detected",
        {"fault_summary": result.fault_summary, "observed_faults": sorted(observed)},
        tuple(failures),
    )


def _determinism_case(result) -> ModelRiskLabAuditCase:
    failures = []
    if result.determinism["status"] != "PASS":
        failures.append("fresh-process determinism failed")
    if result.replay_parity["status"] != "PASS":
        failures.append("loaded replay parity failed")
    return ModelRiskLabAuditCase(
        "fresh_process_determinism_and_loaded_replay_parity",
        {"determinism": result.determinism, "replay_parity": result.replay_parity},
        tuple(failures),
    )


def _minimization_case(result) -> ModelRiskLabAuditCase:
    failures = []
    if len(result.minimized_failures) != len(FaultKind):
        failures.append("one minimized reproducer per stable injected-fault signature is absent")
    if any(not item.preserved for item in result.minimized_failures):
        failures.append("a minimization lost its violation signature")
    return ModelRiskLabAuditCase(
        "every_discovered_signature_has_a_preserving_minimal_reproducer",
        {
            "minimized": [item.as_dict() for item in result.minimized_failures],
            "signature_count": len(result.minimized_failures),
        },
        tuple(failures),
    )


def _statistics_case(result) -> ModelRiskLabAuditCase:
    failed = [item.name for item in result.statistics if item.status == "FAIL"]
    return ModelRiskLabAuditCase(
        "train_holdout_drift_overfit_seed_and_pathology_screens",
        {"checks": [item.as_dict() for item in result.statistics]},
        () if not failed else (f"statistical risk gates failed: {failed}",),
    )


def _probe_case(result) -> ModelRiskLabAuditCase:
    failed = [name for name, item in result.probes.items() if item["status"] != "PASS"]
    return ModelRiskLabAuditCase(
        "real_subsystem_probes_cover_non_kernel_semantics",
        {"probes": result.probes},
        () if not failed else (f"subsystem probes failed: {failed}",),
    )


def _immutable_case(
    packet,
    initial,
    tampered,
    restored,
    superseding_exists,
    acceptance_ledger_rows,
    overwrite_rejected,
    ledger_verification,
) -> ModelRiskLabAuditCase:
    passed = (
        initial == "PASS"
        and tampered.startswith("FAIL")
        and restored == "PASS"
        and superseding_exists
        and acceptance_ledger_rows == 2
        and overwrite_rejected
        and ledger_verification["status"] == "PASS"
    )
    return ModelRiskLabAuditCase(
        "content_addressed_packet_and_tamper_evident_ledger",
        {
            "initial_verification": initial,
            "ledger_verification": ledger_verification,
            "acceptance_ledger_rows": acceptance_ledger_rows,
            "acceptance_overwrite_rejected": overwrite_rejected,
            "packet": packet,
            "restored_verification": restored,
            "superseding_record_exists": superseding_exists,
            "tampered_verification": tampered,
        },
        () if passed else ("immutable packet did not detect and recover from audit tamper probe",),
    )
