"""Executable WO37-A audit for pseudonymous profiles and local consent."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kirby2.curriculum.evidence import LearnerEvidenceLedgerV1
from kirby2.curriculum.learner import build_learner_projection_v1
from kirby2.instructor.consent import (
    CONSENT_DECISION_ID_PREFIX,
    DIRECT_IDENTITY_POLICY,
    PSEUDONYMIZATION_CLAIM,
    ConsentDecisionReasonV1,
    ConsentDecisionStatusV1,
    ConsentRecordV1,
    ConsentScopeV1,
    ConsentStateV1,
    EvidenceExportClassV1,
    EvidenceExportPermissionV1,
    EvidenceRetentionPolicyV1,
    WithdrawalPolicyV1,
    create_consent_record,
    decide_evidence_export,
    decide_profile_deletion,
    decide_retention_after_profile_deletion,
    revise_consent_record,
    withdraw_consent,
)
from kirby2.instructor.identity import (
    DEFAULT_EXPORT_AREA_IDS,
    DEFAULT_PACKAGE_AREA_IDS,
    IDENTITY_DELETION_RECEIPT_DIRECTORY,
    DirectIdentifierV1,
    DirectIdentityV1,
    IdentityMappingV1,
    create_identity_mapping,
    create_local_learner_identity,
    delete_identity_mapping,
    identity_deletion_receipt_path,
    identity_mapping_path,
    is_default_export_area,
    recover_pending_identity_deletions,
    resolve_identity_deletion_receipt,
    resolve_identity_mapping,
)
from kirby2.instructor.models import (
    INSTRUCTOR_RECORD_TYPES,
    Assignment,
    AssignmentAttempt,
    Cohort,
    CurriculumPlan,
    InstructorProfile,
    LearnerProfile,
    ResearchStudy,
    ReviewAnnotation,
    Rubric,
    create_assignment_attempt_revision,
    create_assignment_revision,
    create_cohort_revision,
    create_curriculum_plan_revision,
    create_instructor_profile,
    create_learner_profile,
    create_research_study_revision,
    create_review_annotation_revision,
    create_rubric_revision,
)
from kirby2.research.paths import (
    ERASABLE_IDENTITY_AREA_IDS,
    IMMUTABLE_EVIDENCE_AREA_IDS,
    DataAreaId,
    DataPaths,
)
from kirby2.research.store import LearnerArtifactStore


WO37A_AUDIT_CASE_COUNT = 6
WO37B_AUDIT_CASE_COUNT = 4
WO37C_AUDIT_CASE_COUNT = 4
WO37D_AUDIT_CASE_COUNT = 4
WO37E_AUDIT_CASE_COUNT = 4

_LEARNER_ENTROPY = bytes(range(32))
_INSTRUCTOR_ENTROPY = bytes(range(32, 64))
_OTHER_LEARNER_ENTROPY = bytes(reversed(range(32)))
_DIRECT_MARKERS = (
    b"Ada Learner",
    b"ada.learner@example.invalid",
    b"student-0007",
)


@dataclass(frozen=True, slots=True)
class InstructorConsoleAuditCase:
    name: str
    detail: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_pseudonymous_profiles_and_consent() -> tuple[
    InstructorConsoleAuditCase,
    ...,
]:
    """Run the fixed WO37-A model, policy, and identity-store inventory."""

    cases = (
        _versioned_model_vocabulary_case(),
        _consent_permission_case(),
        _withdrawal_and_expiry_case(),
        _identity_mapping_separation_case(),
        _mapping_deletion_receipt_case(),
        _identity_boundary_attack_case(),
    )
    if len(cases) != WO37A_AUDIT_CASE_COUNT:
        raise RuntimeError("WO37-A audit case inventory changed")
    expected_names = (
        "nine_versioned_types_use_opaque_profiles_and_successor_lineage",
        "consent_retention_and_export_permissions_are_exact_and_fail_closed",
        "withdrawal_scope_and_expiry_policies_preserve_immutable_history",
        "direct_identity_exists_only_in_the_separate_local_mapping_area",
        "profile_deletion_removes_only_mapping_and_writes_a_safe_receipt",
        "identity_store_rejects_invalid_bindings_rebinding_and_foreign_authority",
    )
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO37-A audit case order or identity changed")
    return cases


def _raises(operation) -> bool:
    try:
        operation()
    except (
        AttributeError,
        FileExistsError,
        FileNotFoundError,
        OSError,
        PermissionError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return True
    return False


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _versioned_model_vocabulary_case() -> InstructorConsoleAuditCase:
    learner = create_learner_profile(_LEARNER_ENTROPY)
    repeated_learner = create_learner_profile(_LEARNER_ENTROPY)
    instructor = create_instructor_profile(_INSTRUCTOR_ENTROPY)
    builders = (
        (Assignment, create_assignment_revision),
        (AssignmentAttempt, create_assignment_attempt_revision),
        (ReviewAnnotation, create_review_annotation_revision),
        (Rubric, create_rubric_revision),
        (CurriculumPlan, create_curriculum_plan_revision),
        (Cohort, create_cohort_revision),
        (ResearchStudy, create_research_study_revision),
    )
    revisions: list[object] = []
    lineage_checks: list[bool] = []
    refusal_checks: list[bool] = []
    for ordinal, (record_type, builder) in enumerate(builders, start=1):
        first = builder(_digest(f"wo37a-content-{ordinal}-v1"))
        successor = builder(
            _digest(f"wo37a-content-{ordinal}-v2"),
            predecessor=first,
        )
        revisions.append(first)
        lineage_checks.append(
            type(first) is record_type
            and type(successor) is record_type
            and first.schema_version == 1
            and successor.lineage_id == first.lineage_id
            and successor.revision == 2
            and successor.predecessor_record_id == first.record_id
            and successor.predecessor_sha256 == first.sha256
            and successor.record_id != first.record_id
        )
        refusal_checks.extend(
            (
                _raises(lambda builder=builder, first=first: builder(
                    first.content_sha256,
                    predecessor=first,
                )),
                _raises(lambda first=first: setattr(first, "revision", 9)),
            )
        )
        force_mutated_predecessor = builder(
            _digest(f"wo37a-force-mutated-{ordinal}-v1")
        )
        object.__setattr__(
            force_mutated_predecessor,
            "content_sha256",
            _digest(f"wo37a-force-mutated-{ordinal}-changed"),
        )
        object.__setattr__(
            force_mutated_predecessor,
            "record_id",
            force_mutated_predecessor.record_id[:-64]
            + hashlib.sha256(
                _canonical_bytes(force_mutated_predecessor.identity_dict())
            ).hexdigest(),
        )
        refusal_checks.append(
            _raises(
                lambda builder=builder, predecessor=force_mutated_predecessor: builder(
                    _digest("wo37a-force-mutated-successor"),
                    predecessor=predecessor,
                )
            )
        )
        force_mutated_second_initial = builder(
            _digest(f"wo37a-force-mutated-r2-{ordinal}-v1")
        )
        force_mutated_second = builder(
            _digest(f"wo37a-force-mutated-r2-{ordinal}-v2"),
            predecessor=force_mutated_second_initial,
        )
        object.__setattr__(
            force_mutated_second,
            "content_sha256",
            _digest(f"wo37a-force-mutated-r2-{ordinal}-changed"),
        )
        object.__setattr__(
            force_mutated_second,
            "record_id",
            force_mutated_second.record_id[:-64]
            + hashlib.sha256(
                _canonical_bytes(force_mutated_second.identity_dict())
            ).hexdigest(),
        )
        refusal_checks.append(
            _raises(
                lambda builder=builder, predecessor=force_mutated_second: builder(
                    _digest("wo37a-force-mutated-r2-successor"),
                    predecessor=predecessor,
                )
            )
        )

    records = (instructor, learner, *revisions)
    schemas_are_versioned = all(
        type(record.schema_id) is str
        and bool(record.schema_id)
        and type(record.schema_version) is int
        and record.schema_version == 1
        for record in records
    )
    profile_bytes = instructor.canonical_bytes() + learner.canonical_bytes()
    profile_payload_is_opaque = all(
        marker not in profile_bytes for marker in _DIRECT_MARKERS
    ) and set(learner.as_dict()) == {
        "profile_id",
        "profile_kind",
        "schema_id",
        "schema_version",
    }
    checks = {
        "distinct_role_namespaces": (
            learner.profile_id.startswith("learner-profile-")
            and instructor.profile_id.startswith("instructor-profile-")
            and learner.profile_id != instructor.profile_id
        ),
        "deterministic_injected_entropy": learner == repeated_learner,
        "exact_nine_type_vocabulary": (
            len(INSTRUCTOR_RECORD_TYPES) == 9
            and tuple(type(record) for record in records) == INSTRUCTOR_RECORD_TYPES
        ),
        "profile_payload_is_opaque": profile_payload_is_opaque,
        "schemas_are_versioned": schemas_are_versioned,
        "successor_lineage_is_exact": all(lineage_checks),
        "mutation_same_content_and_stale_predecessor_refused": all(
            refusal_checks
        ),
    }
    failures = tuple(
        name.replace("_", " ") for name, passed in checks.items() if not passed
    )
    return InstructorConsoleAuditCase(
        "nine_versioned_types_use_opaque_profiles_and_successor_lineage",
        (
            f"types={len(records)} lineages={len(revisions)} "
            f"opaque={profile_payload_is_opaque}"
        ),
        {
            "checks": checks,
            "instructor_profile_id": instructor.profile_id,
            "learner_profile_id": learner.profile_id,
            "record_schema_ids": [record.schema_id for record in records],
        },
        failures,
    )


def _active_consent() -> ConsentRecordV1:
    learner = create_learner_profile(_LEARNER_ENTROPY)
    return create_consent_record(
        pseudonymous_profile_id=learner.profile_id,
        scopes=(
            ConsentScopeV1.INSTRUCTIONAL_EVIDENCE,
            ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        ),
        recorded_at_utc="2030-01-01T00:00:00Z",
        retention_policy=EvidenceRetentionPolicyV1.RETAIN_UNTIL_UTC,
        retention_until_utc="2031-01-01T00:00:00Z",
        retain_pseudonymous_evidence_after_profile_deletion=True,
        export_permission=(
            EvidenceExportPermissionV1.PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY
        ),
        withdrawal_policy=(
            WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
        ),
    )


def _consent_permission_case() -> InstructorConsoleAuditCase:
    consent = _active_consent()
    retention = decide_retention_after_profile_deletion(
        consent,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        decision_time_utc="2030-06-01T00:00:00Z",
    )
    allowed_export = decide_evidence_export(
        consent,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        requested_export=EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE,
        decision_time_utc="2030-06-01T00:00:00Z",
    )
    refused_exports = tuple(
        decide_evidence_export(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_export=export_class,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        for export_class in (
            EvidenceExportClassV1.DIRECT_IDENTITY,
            EvidenceExportClassV1.IDENTITY_MAPPING,
            EvidenceExportClassV1.UNREDACTED_EVIDENCE,
        )
    )
    missing_scope = decide_evidence_export(
        consent,
        required_scope=ConsentScopeV1.INSTRUCTOR_REVIEW,
        requested_export=EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE,
        decision_time_utc="2030-06-01T00:00:00Z",
    )
    deletion = decide_profile_deletion(
        consent,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        requested_pseudonymous_evidence_retention=True,
        decision_time_utc="2030-06-01T00:00:00Z",
    )
    denied_consent = create_consent_record(
        pseudonymous_profile_id=consent.pseudonymous_profile_id,
        scopes=(ConsentScopeV1.LOCAL_RESEARCH_STUDY,),
        recorded_at_utc="2030-01-01T00:00:00Z",
        retention_policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
        retention_until_utc=None,
        retain_pseudonymous_evidence_after_profile_deletion=False,
        export_permission=EvidenceExportPermissionV1.DENIED,
        withdrawal_policy=(
            WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
        ),
    )
    object.__setattr__(
        denied_consent,
        "export_permission",
        EvidenceExportPermissionV1.PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY,
    )
    object.__setattr__(
        denied_consent,
        "consent_id",
        denied_consent.consent_id[:-24]
        + hashlib.sha256(
            _canonical_bytes(denied_consent.identity_dict())
        ).hexdigest()[:24],
    )
    payload = consent.canonical_bytes()
    checks = {
        "canonical_round_trip": ConsentRecordV1.from_json_bytes(payload) == consent,
        "active_retention_authorized": retention.allowed,
        "redacted_pseudonymous_export_authorized": allowed_export.allowed,
        "direct_and_mapping_exports_refused": all(
            not decision.allowed
            and decision.status is ConsentDecisionStatusV1.REFUSED
            for decision in refused_exports
        ),
        "missing_scope_refused": (
            not missing_scope.allowed
            and missing_scope.reason is ConsentDecisionReasonV1.SCOPE_NOT_GRANTED
        ),
        "retaining_deletion_binds_prior_decision": (
            deletion.allowed
            and deletion.retention_decision_id == retention.decision_id
            and deletion.retention_decision_sha256 == retention.decision_sha256
        ),
        "privacy_claim_is_pseudonymous": (
            consent.pseudonymization_claim == PSEUDONYMIZATION_CLAIM
            and PSEUDONYMIZATION_CLAIM == "PSEUDONYMOUS_NOT_ANONYMOUS"
            and consent.direct_identity_policy == DIRECT_IDENTITY_POLICY
        ),
        "no_direct_identity_in_consent": all(
            marker not in payload for marker in _DIRECT_MARKERS
        ),
        "invalid_retention_combination_refused": _raises(
            lambda: create_consent_record(
                pseudonymous_profile_id=consent.pseudonymous_profile_id,
                scopes=(ConsentScopeV1.INSTRUCTIONAL_EVIDENCE,),
                recorded_at_utc="2030-01-01T00:00:00Z",
                retention_policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
                retention_until_utc=None,
                retain_pseudonymous_evidence_after_profile_deletion=True,
                export_permission=EvidenceExportPermissionV1.DENIED,
                withdrawal_policy=(
                    WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
                ),
            )
        ),
        "force_mutated_consent_refused": _raises(
            lambda: decide_evidence_export(
                denied_consent,
                required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
                requested_export=(
                    EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE
                ),
                decision_time_utc="2030-06-01T00:00:00Z",
            )
        ),
    }
    failures = tuple(
        name.replace("_", " ") for name, passed in checks.items() if not passed
    )
    return InstructorConsoleAuditCase(
        "consent_retention_and_export_permissions_are_exact_and_fail_closed",
        (
            f"authorized=retention+redacted-export "
            f"refused_exports={sum(not item.allowed for item in refused_exports)}"
        ),
        {
            "checks": checks,
            "consent_id": consent.consent_id,
            "deletion_decision_id": deletion.decision_id,
            "refused_export_reasons": [item.reason.value for item in refused_exports],
        },
        failures,
    )


def _withdrawal_and_expiry_case() -> InstructorConsoleAuditCase:
    active = _active_consent()
    withdrawn = withdraw_consent(
        active,
        recorded_at_utc="2030-07-01T00:00:00Z",
    )
    withdrawn_retention = decide_retention_after_profile_deletion(
        withdrawn,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        decision_time_utc="2030-08-01T00:00:00Z",
    )
    withdrawn_export = decide_evidence_export(
        withdrawn,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        requested_export=EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE,
        decision_time_utc="2030-08-01T00:00:00Z",
    )
    expired_retention = decide_retention_after_profile_deletion(
        active,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        decision_time_utc="2031-01-01T00:00:01Z",
    )
    refused_deletion = decide_profile_deletion(
        active,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        requested_pseudonymous_evidence_retention=True,
        decision_time_utc="2031-01-01T00:00:01Z",
    )
    nonretaining_deletion = decide_profile_deletion(
        withdrawn,
        required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
        requested_pseudonymous_evidence_retention=False,
        decision_time_utc="2030-08-01T00:00:00Z",
    )
    force_mutated_revision_source = _active_consent()
    object.__setattr__(
        force_mutated_revision_source,
        "pseudonymous_profile_id",
        create_learner_profile(_OTHER_LEARNER_ENTROPY).profile_id,
    )
    force_mutated_revision_refused = _raises(
        lambda: revise_consent_record(
            force_mutated_revision_source,
            scopes=(ConsentScopeV1.LOCAL_RESEARCH_STUDY,),
            recorded_at_utc="2030-07-01T00:00:00Z",
            retention_policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
            retention_until_utc=None,
            retain_pseudonymous_evidence_after_profile_deletion=False,
            export_permission=EvidenceExportPermissionV1.DENIED,
            withdrawal_policy=(
                WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
            ),
        )
    )
    force_mutated_withdrawal_refused = _raises(
        lambda: withdraw_consent(
            force_mutated_revision_source,
            recorded_at_utc="2030-07-01T00:00:00Z",
        )
    )
    checks = {
        "withdrawal_is_successor": (
            withdrawn.state is ConsentStateV1.WITHDRAWN
            and withdrawn.revision == active.revision + 1
            and withdrawn.predecessor_consent_id == active.consent_id
            and withdrawn.predecessor_sha256 == active.consent_sha256
        ),
        "source_grant_remains_unchanged": active.state is ConsentStateV1.GRANTED,
        "revoking_withdrawal_refuses_retention": (
            not withdrawn_retention.allowed
            and withdrawn_retention.reason
            is ConsentDecisionReasonV1.CONSENT_WITHDRAWN
        ),
        "withdrawal_always_refuses_export": (
            not withdrawn_export.allowed
            and withdrawn_export.reason is ConsentDecisionReasonV1.CONSENT_WITHDRAWN
        ),
        "expired_retention_refused": (
            not expired_retention.allowed
            and expired_retention.reason
            is ConsentDecisionReasonV1.RETENTION_EXPIRED
        ),
        "unauthorized_retaining_deletion_refused": not refused_deletion.allowed,
        "mapping_only_deletion_still_authorized": (
            nonretaining_deletion.allowed
            and not nonretaining_deletion.requested_pseudonymous_evidence_retention
        ),
        "backdated_decision_refused": _raises(
            lambda: decide_evidence_export(
                active,
                required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
                requested_export=(
                    EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE
                ),
                decision_time_utc="2029-12-31T23:59:59Z",
            )
        ),
        "consent_is_frozen": _raises(
            lambda: setattr(active, "state", ConsentStateV1.WITHDRAWN)
        ),
        "stale_force_mutated_predecessor_refused": (
            force_mutated_revision_refused
            and force_mutated_withdrawal_refused
        ),
    }
    failures = tuple(
        name.replace("_", " ") for name, passed in checks.items() if not passed
    )
    return InstructorConsoleAuditCase(
        "withdrawal_scope_and_expiry_policies_preserve_immutable_history",
        (
            f"revision={withdrawn.revision} withdrawn_retention="
            f"{withdrawn_retention.status.value} "
            f"expired={expired_retention.status.value}"
        ),
        {
            "checks": checks,
            "withdrawal_consent_id": withdrawn.consent_id,
            "withdrawal_policy": withdrawn.withdrawal_policy.value,
        },
        failures,
    )


def _identity_mapping_separation_case() -> InstructorConsoleAuditCase:
    direct_identity = _direct_identity()
    with TemporaryDirectory(prefix="kirby2-wo37a-separation-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        profile = creation.profile
        mapping = creation.mapping
        mapping_path = identity_mapping_path(paths, profile.profile_id)
        mapping_bytes = mapping_path.read_bytes()
        resolved = resolve_identity_mapping(paths, profile.profile_id)
        file_mode = stat.S_IMODE(mapping_path.stat().st_mode)
        area_mode = stat.S_IMODE(paths.identity_mappings.stat().st_mode)
        nonmapping_material = (
            profile.canonical_bytes()
            + _active_consent().canonical_bytes()
        )
        checks = {
            "create_and_resolve_are_exact": (
                resolved == mapping
                and resolved.canonical_bytes() == mapping.canonical_bytes()
            ),
            "mapping_is_in_erasable_area": (
                mapping_path.parent == paths.identity_mappings
                and mapping_path.name == f"{profile.profile_id}.json"
            ),
            "direct_identity_is_only_in_mapping_bytes": (
                all(marker in mapping_bytes for marker in _DIRECT_MARKERS)
                and all(marker not in nonmapping_material for marker in _DIRECT_MARKERS)
            ),
            "representations_redact_direct_values": all(
                marker.decode("ascii") not in repr(value)
                for marker in _DIRECT_MARKERS
                for value in (direct_identity, mapping)
            ),
            "default_package_excludes_mapping_area": (
                DataAreaId.IDENTITY_MAPPINGS not in DEFAULT_PACKAGE_AREA_IDS
                and DataAreaId.IDENTITY_MAPPINGS not in DEFAULT_EXPORT_AREA_IDS
                and not is_default_export_area(DataAreaId.IDENTITY_MAPPINGS)
            ),
            "default_inventory_is_closed": (
                DEFAULT_PACKAGE_AREA_IDS
                == IMMUTABLE_EVIDENCE_AREA_IDS
                and DEFAULT_EXPORT_AREA_IDS == DEFAULT_PACKAGE_AREA_IDS
                and ERASABLE_IDENTITY_AREA_IDS
                == (DataAreaId.IDENTITY_MAPPINGS,)
            ),
            "sensitive_permissions_are_private": (
                area_mode == 0o700 and file_mode == 0o600
            ),
            "unrelated_areas_not_created": (
                not paths.runs.exists() and not paths.evidence.exists()
            ),
        }
        failures = tuple(
            name.replace("_", " ")
            for name, passed in checks.items()
            if not passed
        )
        evidence = {
            "checks": checks,
            "default_area_ids": [item.value for item in DEFAULT_EXPORT_AREA_IDS],
            "identity_area_mode": oct(area_mode),
            "mapping_file_mode": oct(file_mode),
            "profile_id": profile.profile_id,
        }
    return InstructorConsoleAuditCase(
        "direct_identity_exists_only_in_the_separate_local_mapping_area",
        (
            f"profile={profile.profile_id} mapping_mode={oct(file_mode)} "
            f"default_export=excluded"
        ),
        evidence,
        failures,
    )


def _mapping_deletion_receipt_case() -> InstructorConsoleAuditCase:
    direct_identity = _direct_identity()
    with TemporaryDirectory(prefix="kirby2-wo37a-deletion-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        profile = creation.profile
        mapping = creation.mapping
        ledger = LearnerEvidenceLedgerV1(profile.profile_id, ())
        projection = build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=0,
        )
        learner_store = LearnerArtifactStore(paths.root)
        manifest = learner_store.record_update(
            ledger,
            projection,
            seed=37,
            repository=Path(__file__).resolve().parents[2],
        )
        run_directory = learner_store.run_directory(manifest.run_id)
        run_files = tuple(
            path for path in sorted(run_directory.rglob("*")) if path.is_file()
        )
        run_snapshot = {path: path.read_bytes() for path in run_files}
        run_bytes = b"".join(
            path.relative_to(run_directory).as_posix().encode("ascii")
            + b"\x00"
            + run_snapshot[path]
            for path in run_files
        )
        paths.ensure((DataAreaId.EVIDENCE,))
        receipt_directory = (
            paths.evidence / IDENTITY_DELETION_RECEIPT_DIRECTORY
        )
        receipt_directory.mkdir(mode=0o777)
        os.chmod(receipt_directory, 0o777)
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        receipt = delete_identity_mapping(
            paths,
            profile.profile_id,
            decision,
            consent,
            deletion_time_utc="2030-06-01T00:00:01Z",
        )
        receipt_path = identity_deletion_receipt_path(paths, receipt.receipt_id)
        receipt_bytes = receipt_path.read_bytes()
        receipt_directory_mode = stat.S_IMODE(receipt_directory.stat().st_mode)
        receipt_file_metadata = receipt_path.stat()
        restored = resolve_identity_deletion_receipt(paths, receipt.receipt_id)
        receipt_payload = restored.as_dict()
        receipt_keys = set(receipt_payload)
        forbidden_receipt_keys = {
            "direct_identity",
            "display_name",
            "direct_identifiers",
            "mapping_id",
            "mapping_sha256",
        }
        evidence_files = tuple(
            path
            for path in paths.evidence.rglob("*")
            if path.is_file()
        )
        immutable_files = (*run_files, *evidence_files)
        loaded_ledger, loaded_projection = learner_store.load_update(
            manifest.run_id
        )
        checks = {
            "mapping_deleted": (
                not identity_mapping_path(paths, profile.profile_id).exists()
                and _raises(
                    lambda: resolve_identity_mapping(paths, profile.profile_id)
                )
            ),
            "run_evidence_is_byte_identical": (
                all(path.read_bytes() == raw for path, raw in run_snapshot.items())
                and learner_store.verify_run(manifest.run_id).passed
            ),
            "run_contains_only_pseudonymous_profile": (
                profile.profile_id.encode("ascii") in run_bytes
                and all(marker not in run_bytes for marker in _DIRECT_MARKERS)
                and loaded_ledger.learner_id == profile.profile_id
                and loaded_projection.learner_id == profile.profile_id
            ),
            "receipt_is_exact_and_content_addressed": (
                restored == receipt
                and restored.canonical_bytes() == receipt_bytes
                and receipt_path.name == f"{receipt.receipt_id}.json"
            ),
            "receipt_storage_is_private_and_unaliased": (
                receipt_directory_mode == 0o700
                and stat.S_IMODE(receipt_file_metadata.st_mode) == 0o600
                and receipt_file_metadata.st_nlink == 1
            ),
            "receipt_binds_consent_and_decision": (
                receipt.consent_id == consent.consent_id
                and receipt.consent_sha256 == consent.consent_sha256
                and receipt.deletion_decision_id == decision.decision_id
                and receipt.deletion_decision_sha256 == decision.decision_sha256
            ),
            "receipt_has_no_deleted_payload_commitment": (
                not (receipt_keys & forbidden_receipt_keys)
                and mapping.mapping_id.encode("ascii") not in receipt_bytes
                and mapping.mapping_sha256.encode("ascii") not in receipt_bytes
                and all(marker not in receipt_bytes for marker in _DIRECT_MARKERS)
            ),
            "all_immutable_files_exclude_direct_identity": all(
                all(marker not in path.read_bytes() for marker in _DIRECT_MARKERS)
                for path in immutable_files
            ),
            "second_delete_is_refused": _raises(
                lambda: delete_identity_mapping(
                    paths,
                    profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:02Z",
                )
            ),
        }
        failures = tuple(
            name.replace("_", " ")
            for name, passed in checks.items()
            if not passed
        )
        evidence = {
            "checks": checks,
            "evidence_file_count": len(evidence_files),
            "profile_id": profile.profile_id,
            "receipt_id": receipt.receipt_id,
            "run_sha256": hashlib.sha256(run_bytes).hexdigest(),
            "stored_run_id": manifest.run_id,
        }
    return InstructorConsoleAuditCase(
        "profile_deletion_removes_only_mapping_and_writes_a_safe_receipt",
        (
            f"mapping=deleted run_files={len(run_files)} "
            f"evidence_files={len(evidence_files)} "
            f"receipt={receipt.receipt_id}"
        ),
        evidence,
        failures,
    )


def _identity_boundary_attack_case() -> InstructorConsoleAuditCase:
    direct_identity = _direct_identity()
    with TemporaryDirectory(prefix="kirby2-wo37a-attacks-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        first = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        second = create_local_learner_identity(
            paths,
            DirectIdentityV1(
                "Grace Learner",
                (DirectIdentifierV1("student_id", "student-0008"),),
            ),
            opaque_entropy=_OTHER_LEARNER_ENTROPY,
        )
        refused_consent = create_consent_record(
            pseudonymous_profile_id=first.profile.profile_id,
            scopes=(ConsentScopeV1.LOCAL_RESEARCH_STUDY,),
            recorded_at_utc="2030-01-01T00:00:00Z",
            retention_policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
            retention_until_utc=None,
            retain_pseudonymous_evidence_after_profile_deletion=False,
            export_permission=EvidenceExportPermissionV1.DENIED,
            withdrawal_policy=(
                WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
            ),
        )
        refused_decision = decide_profile_deletion(
            refused_consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-02-01T00:00:00Z",
        )
        refused_delete = _raises(
            lambda: delete_identity_mapping(
                paths,
                first.profile.profile_id,
                refused_decision,
                refused_consent,
                deletion_time_utc="2030-02-01T00:00:01Z",
            )
        )
        mapping_survived_refusal = (
            resolve_identity_mapping(paths, first.profile.profile_id)
            == first.mapping
        )
        mutated_refusal = decide_profile_deletion(
            refused_consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-02-01T00:00:00Z",
        )
        object.__setattr__(mutated_refusal, "allowed", True)
        object.__setattr__(
            mutated_refusal,
            "status",
            ConsentDecisionStatusV1.AUTHORIZED,
        )
        object.__setattr__(
            mutated_refusal,
            "reason",
            ConsentDecisionReasonV1.PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION,
        )
        object.__setattr__(
            mutated_refusal,
            "decision_id",
            CONSENT_DECISION_ID_PREFIX
            + hashlib.sha256(
                _canonical_bytes(mutated_refusal.identity_dict())
            ).hexdigest()[:24],
        )
        force_mutated_delete = _raises(
            lambda: delete_identity_mapping(
                paths,
                first.profile.profile_id,
                mutated_refusal,
                refused_consent,
                deletion_time_utc="2030-02-01T00:00:01Z",
            )
        )
        mapping_survived_force_mutation = (
            resolve_identity_mapping(paths, first.profile.profile_id)
            == first.mapping
        )
        stale_grant = _active_consent()
        stale_decision = decide_profile_deletion(
            stale_grant,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        current_withdrawal = withdraw_consent(
            stale_grant,
            recorded_at_utc="2030-07-01T00:00:00Z",
        )
        stale_grant_delete = _raises(
            lambda: delete_identity_mapping(
                paths,
                first.profile.profile_id,
                stale_decision,
                current_withdrawal,
                deletion_time_utc="2030-08-01T00:00:00Z",
            )
        )
        mapping_survived_stale_grant = (
            resolve_identity_mapping(paths, first.profile.profile_id)
            == first.mapping
        )

        foreign_consent = create_consent_record(
            pseudonymous_profile_id=second.profile.profile_id,
            scopes=(ConsentScopeV1.LOCAL_RESEARCH_STUDY,),
            recorded_at_utc="2030-01-01T00:00:00Z",
            retention_policy=EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE,
            retention_until_utc=None,
            retain_pseudonymous_evidence_after_profile_deletion=False,
            export_permission=EvidenceExportPermissionV1.DENIED,
            withdrawal_policy=(
                WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
            ),
        )
        foreign_decision = decide_profile_deletion(
            foreign_consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=False,
            decision_time_utc="2030-02-01T00:00:00Z",
        )
        foreign_delete = _raises(
            lambda: delete_identity_mapping(
                paths,
                first.profile.profile_id,
                foreign_decision,
                foreign_consent,
                deletion_time_utc="2030-02-01T00:00:01Z",
            )
        )
        mapping_survived_foreign_authority = (
            resolve_identity_mapping(paths, first.profile.profile_id)
            == first.mapping
        )
        duplicate_refused = _raises(
            lambda: create_identity_mapping(
                paths,
                first.profile,
                direct_identity,
                opaque_entropy=_LEARNER_ENTROPY,
            )
        )
        wrong_entropy_refused = _raises(
            lambda: create_identity_mapping(
                paths,
                first.profile,
                direct_identity,
                opaque_entropy=_OTHER_LEARNER_ENTROPY,
            )
        )
        mapping_path = identity_mapping_path(paths, first.profile.profile_id)
        os.chmod(mapping_path, 0o644)
        overexposed_refused = _raises(
            lambda: resolve_identity_mapping(paths, first.profile.profile_id)
        )
        os.chmod(mapping_path, 0o600)
        hardlink_alias = paths.identity_mappings / "hardlink-alias.json"
        os.link(mapping_path, hardlink_alias)
        hardlinked_mapping_refused = _raises(
            lambda: resolve_identity_mapping(paths, first.profile.profile_id)
        )
        hardlink_alias.unlink()

        raw_mapping = json.loads(mapping_path.read_text("ascii"))
        raw_mapping["profile_sha256"] = "f" * 64
        identity_payload = dict(raw_mapping)
        identity_payload.pop("mapping_id")
        raw_mapping["mapping_id"] = (
            "identity-mapping-"
            + hashlib.sha256(_canonical_bytes(identity_payload)).hexdigest()
        )
        mapping_path.write_bytes(_canonical_bytes(raw_mapping))
        os.chmod(mapping_path, 0o600)
        repinned_profile_cotamper_refused = _raises(
            lambda: resolve_identity_mapping(paths, first.profile.profile_id)
        )

        with TemporaryDirectory(prefix="kirby2-wo37a-symlink-") as raw_symlink:
            symlink_root = Path(raw_symlink).resolve()
            target = symlink_root / "outside"
            target.mkdir()
            (symlink_root / DataAreaId.IDENTITY_MAPPINGS.value).symlink_to(
                target,
                target_is_directory=True,
            )
            symlink_rebind_refused = _raises(lambda: DataPaths(symlink_root))
            symlink_target_untouched = not tuple(target.iterdir())

        with TemporaryDirectory(prefix="kirby2-wo37a-recovery-") as raw_recovery:
            recovery_paths = DataPaths(Path(raw_recovery).resolve())
            recovery_creation = create_local_learner_identity(
                recovery_paths,
                direct_identity,
                opaque_entropy=_LEARNER_ENTROPY,
            )
            recovery_consent = _active_consent()
            recovery_decision = decide_profile_deletion(
                recovery_consent,
                required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
                requested_pseudonymous_evidence_retention=True,
                decision_time_utc="2030-06-01T00:00:00Z",
            )
            with patch(
                "kirby2.instructor.identity.os.rename",
                side_effect=OSError("injected receipt publish interruption"),
            ):
                interrupted_publish = _raises(
                    lambda: delete_identity_mapping(
                        recovery_paths,
                        recovery_creation.profile.profile_id,
                        recovery_decision,
                        recovery_consent,
                        deletion_time_utc="2030-06-01T00:00:01Z",
                    )
                )
            mapping_absent_after_interruption = not identity_mapping_path(
                recovery_paths,
                recovery_creation.profile.profile_id,
            ).exists()
            recovered_receipts = recover_pending_identity_deletions(
                recovery_paths
            )
            recovered_receipt = (
                recovered_receipts[0] if len(recovered_receipts) == 1 else None
            )
            recovery_complete = (
                recovered_receipt is not None
                and resolve_identity_deletion_receipt(
                    recovery_paths,
                    recovered_receipt.receipt_id,
                )
                == recovered_receipt
                and not tuple(
                    recovery_paths.evidence.rglob(".pending-*.json")
                )
                and all(
                    marker not in recovered_receipt.canonical_bytes()
                    for marker in _DIRECT_MARKERS
                )
            )

        expiry_checks = _expired_delete_and_completed_tombstone_checks(
            direct_identity
        )
        pre_unlink_checks = _pre_unlink_pending_fsync_cleanup_checks(
            direct_identity
        )
        fsync_checks = _post_unlink_fsync_recovery_checks(direct_identity)
        fstat_checks = _post_unlink_fstat_recovery_checks(direct_identity)
        root_generation_checks = _root_generation_swap_checks(direct_identity)
        recreation_checks = _interrupted_deletion_recreation_checks(
            direct_identity
        )
        conflict_checks = _pending_live_mapping_conflict_checks(
            direct_identity
        )
        hardlink_race_checks = _hardlink_insertion_race_checks(direct_identity)
        lock_checks = _lock_failure_cleanup_checks(direct_identity)

        checks = {
            "unauthorized_retention_delete_refused": (
                not refused_decision.allowed
                and refused_delete
                and mapping_survived_refusal
            ),
            "force_mutated_refusal_revalidated": (
                force_mutated_delete and mapping_survived_force_mutation
            ),
            "stale_grant_rejected_against_current_withdrawal": (
                stale_grant_delete and mapping_survived_stale_grant
            ),
            "foreign_profile_authority_refused": (
                foreign_delete and mapping_survived_foreign_authority
            ),
            "duplicate_mapping_refused": duplicate_refused,
            "wrong_entropy_proof_refused": wrong_entropy_refused,
            "overexposed_mapping_refused": overexposed_refused,
            "hardlinked_mapping_refused": hardlinked_mapping_refused,
            "repinned_profile_digest_cotamper_refused": (
                repinned_profile_cotamper_refused
            ),
            "symlink_rebind_refused_and_untouched": (
                symlink_rebind_refused and symlink_target_untouched
            ),
            "interrupted_receipt_publish_is_recoverable": (
                interrupted_publish
                and mapping_absent_after_interruption
                and recovery_complete
            ),
            **expiry_checks,
            **pre_unlink_checks,
            **fsync_checks,
            **fstat_checks,
            **root_generation_checks,
            **recreation_checks,
            **conflict_checks,
            **hardlink_race_checks,
            **lock_checks,
        }
        failures = tuple(
            name.replace("_", " ")
            for name, passed in checks.items()
            if not passed
        )
        evidence = {
            "attack_count": len(checks),
            "checks": checks,
            "first_profile_id": first.profile.profile_id,
            "second_profile_id": second.profile.profile_id,
        }
    return InstructorConsoleAuditCase(
        "identity_store_rejects_invalid_bindings_rebinding_and_foreign_authority",
        f"attacks={len(checks)} rejected={sum(checks.values())}",
        evidence,
        failures,
    )


def _expired_delete_and_completed_tombstone_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-expiry-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        retaining = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        expired_refused = _raises(
            lambda: delete_identity_mapping(
                paths,
                creation.profile.profile_id,
                retaining,
                consent,
                deletion_time_utc="2031-01-01T00:00:01Z",
            )
        )
        mapping_survived = (
            resolve_identity_mapping(paths, creation.profile.profile_id)
            == creation.mapping
        )
        nonretaining = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=False,
            decision_time_utc="2031-01-01T00:00:01Z",
        )
        receipt = delete_identity_mapping(
            paths,
            creation.profile.profile_id,
            nonretaining,
            consent,
            deletion_time_utc="2031-01-01T00:00:02Z",
        )
        tombstoned_recreation_refused = _raises(
            lambda: create_identity_mapping(
                paths,
                creation.profile,
                direct_identity,
                opaque_entropy=_LEARNER_ENTROPY,
            )
        )
        return {
            "expired_retention_rechecked_at_deletion_time": (
                expired_refused and mapping_survived
            ),
            "nonretaining_delete_remains_allowed_after_expiry": (
                not receipt.pseudonymous_evidence_retained
                and resolve_identity_deletion_receipt(paths, receipt.receipt_id)
                == receipt
            ),
            "completed_deletion_tombstone_prevents_recreation": (
                tombstoned_recreation_refused
                and not identity_mapping_path(
                    paths,
                    creation.profile.profile_id,
                ).exists()
            ),
        }


def _post_unlink_fsync_recovery_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-fsync-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        mapping_path = identity_mapping_path(paths, creation.profile.profile_id)
        identity_metadata = paths.identity_mappings.stat()
        real_fsync = os.fsync
        injected = False

        def fail_after_mapping_unlink(descriptor: int) -> None:
            nonlocal injected
            metadata = os.fstat(descriptor)
            if (
                not injected
                and metadata.st_dev == identity_metadata.st_dev
                and metadata.st_ino == identity_metadata.st_ino
                and not mapping_path.exists()
            ):
                injected = True
                raise OSError("injected post-unlink identity-directory fsync failure")
            real_fsync(descriptor)

        with patch(
            "kirby2.instructor.identity.os.fsync",
            side_effect=fail_after_mapping_unlink,
        ):
            deletion_raised = _raises(
                lambda: delete_identity_mapping(
                    paths,
                    creation.profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:01Z",
                )
            )
        pending_before_recovery = tuple(
            paths.evidence.rglob(".pending-*.json")
        )
        recovered = recover_pending_identity_deletions(paths)
        pending_after_recovery = tuple(
            paths.evidence.rglob(".pending-*.json")
        )
        final_receipts = tuple(
            (paths.evidence / IDENTITY_DELETION_RECEIPT_DIRECTORY).glob(
                "identity-deletion-receipt-*.json"
            )
        )
        return {
            "post_unlink_fsync_failure_keeps_recoverable_intent": (
                injected
                and deletion_raised
                and not mapping_path.exists()
                and len(pending_before_recovery) == 1
                and len(recovered) == 1
                and not pending_after_recovery
                and len(final_receipts) == 1
                and resolve_identity_deletion_receipt(
                    paths,
                    recovered[0].receipt_id,
                )
                == recovered[0]
            )
        }


def _pre_unlink_pending_fsync_cleanup_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-pre-unlink-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        real_fsync = os.fsync
        injected = False

        def fail_pending_directory_fsync(descriptor: int) -> None:
            nonlocal injected
            metadata = os.fstat(descriptor)
            if not injected and stat.S_ISDIR(metadata.st_mode):
                with os.scandir(descriptor) as entries:
                    names = tuple(entry.name for entry in entries)
                if any(name.startswith(".pending-") for name in names):
                    injected = True
                    raise OSError("injected pre-unlink pending-directory fsync failure")
            real_fsync(descriptor)

        with patch(
            "kirby2.instructor.identity.os.fsync",
            side_effect=fail_pending_directory_fsync,
        ):
            deletion_raised = _raises(
                lambda: delete_identity_mapping(
                    paths,
                    creation.profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:01Z",
                )
            )

        receipt_directory = (
            paths.evidence / IDENTITY_DELETION_RECEIPT_DIRECTORY
        )
        residue = tuple(receipt_directory.glob("*.json")) + tuple(
            receipt_directory.glob(".pending-*.json")
        )
        mapping_survived = (
            resolve_identity_mapping(paths, creation.profile.profile_id)
            == creation.mapping
        )
        retry_receipt = delete_identity_mapping(
            paths,
            creation.profile.profile_id,
            decision,
            consent,
            deletion_time_utc="2030-06-01T00:00:01Z",
        )
        return {
            "pre_unlink_pending_fsync_failure_rolls_back_and_retries": (
                injected
                and deletion_raised
                and mapping_survived
                and not residue
                and resolve_identity_deletion_receipt(
                    paths,
                    retry_receipt.receipt_id,
                )
                == retry_receipt
            )
        }


def _post_unlink_fstat_recovery_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-fstat-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        mapping_path = identity_mapping_path(paths, creation.profile.profile_id)
        mapping_metadata = mapping_path.stat()
        real_fstat = os.fstat
        injected = False

        def fail_mapping_fstat_after_unlink(descriptor: int):
            nonlocal injected
            metadata = real_fstat(descriptor)
            if (
                not injected
                and metadata.st_dev == mapping_metadata.st_dev
                and metadata.st_ino == mapping_metadata.st_ino
                and not mapping_path.exists()
            ):
                injected = True
                raise OSError("injected post-unlink mapping fstat failure")
            return metadata

        with patch(
            "kirby2.instructor.identity.os.fstat",
            side_effect=fail_mapping_fstat_after_unlink,
        ):
            deletion_raised = _raises(
                lambda: delete_identity_mapping(
                    paths,
                    creation.profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:01Z",
                )
            )
        pending_before_recovery = tuple(
            paths.evidence.rglob(".pending-*.json")
        )
        recovered = recover_pending_identity_deletions(paths)
        pending_after_recovery = tuple(
            paths.evidence.rglob(".pending-*.json")
        )
        return {
            "post_unlink_fstat_failure_keeps_recoverable_intent": (
                injected
                and deletion_raised
                and not mapping_path.exists()
                and len(pending_before_recovery) == 1
                and len(recovered) == 1
                and not pending_after_recovery
                and resolve_identity_deletion_receipt(
                    paths,
                    recovered[0].receipt_id,
                )
                == recovered[0]
            )
        }


def _root_generation_swap_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    """Prove one recovery cannot cross into a path-rebound root generation."""

    with TemporaryDirectory(prefix="kirby2-wo37a-root-swap-") as raw_parent:
        parent = Path(raw_parent).resolve()
        victim_root = parent / "victim"
        attacker_root = parent / "attacker"
        victim_paths = DataPaths(victim_root)
        attacker_paths = DataPaths(attacker_root)

        def leave_pending(paths: DataPaths, entropy: bytes):
            creation = create_local_learner_identity(
                paths,
                direct_identity,
                opaque_entropy=entropy,
            )
            consent = create_consent_record(
                pseudonymous_profile_id=creation.profile.profile_id,
                scopes=(ConsentScopeV1.LOCAL_RESEARCH_STUDY,),
                recorded_at_utc="2030-01-01T00:00:00Z",
                retention_policy=EvidenceRetentionPolicyV1.RETAIN_UNTIL_UTC,
                retention_until_utc="2031-01-01T00:00:00Z",
                retain_pseudonymous_evidence_after_profile_deletion=True,
                export_permission=EvidenceExportPermissionV1.DENIED,
                withdrawal_policy=(
                    WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
                ),
            )
            decision = decide_profile_deletion(
                consent,
                required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
                requested_pseudonymous_evidence_retention=True,
                decision_time_utc="2030-06-01T00:00:00Z",
            )
            with patch(
                "kirby2.instructor.identity.os.rename",
                side_effect=OSError("injected receipt publish interruption"),
            ):
                interrupted = _raises(
                    lambda: delete_identity_mapping(
                        paths,
                        creation.profile.profile_id,
                        decision,
                        consent,
                        deletion_time_utc="2030-06-01T00:00:01Z",
                    )
                )
            return creation, interrupted

        victim, victim_interrupted = leave_pending(
            victim_paths,
            _LEARNER_ENTROPY,
        )
        attacker, attacker_interrupted = leave_pending(
            attacker_paths,
            _OTHER_LEARNER_ENTROPY,
        )
        real_open_area = __import__(
            "kirby2.instructor.identity",
            fromlist=["_open_governed_area_from_root"],
        )._open_governed_area_from_root
        old_victim_root = parent / "victim-old"
        swapped = False

        def swap_before_evidence(
            paths: DataPaths,
            root_descriptor: int,
            area_id: DataAreaId,
            label: str,
            *,
            optional: bool = False,
        ):
            nonlocal swapped
            if area_id is DataAreaId.EVIDENCE and not swapped:
                os.rename(victim_root, old_victim_root)
                os.rename(attacker_root, victim_root)
                swapped = True
            return real_open_area(
                paths,
                root_descriptor,
                area_id,
                label,
                optional=optional,
            )

        with patch(
            "kirby2.instructor.identity._open_governed_area_from_root",
            side_effect=swap_before_evidence,
        ):
            recovered = recover_pending_identity_deletions(victim_paths)

        victim_receipt_directory = (
            old_victim_root
            / DataAreaId.EVIDENCE.value
            / IDENTITY_DELETION_RECEIPT_DIRECTORY
        )
        attacker_receipt_directory = (
            victim_root
            / DataAreaId.EVIDENCE.value
            / IDENTITY_DELETION_RECEIPT_DIRECTORY
        )
        victim_pending = tuple(victim_receipt_directory.glob(".pending-*.json"))
        victim_finals = tuple(
            victim_receipt_directory.glob("identity-deletion-receipt-*.json")
        )
        attacker_pending = tuple(attacker_receipt_directory.glob(".pending-*.json"))
        attacker_finals = tuple(
            attacker_receipt_directory.glob("identity-deletion-receipt-*.json")
        )
        return {
            "root_generation_swap_never_recovers_attacker_receipt": (
                victim_interrupted
                and attacker_interrupted
                and swapped
                and len(recovered) == 1
                and recovered[0].pseudonymous_profile_id
                == victim.profile.profile_id
                and not victim_pending
                and len(victim_finals) == 1
                and len(attacker_pending) == 1
                and not attacker_finals
                and attacker_pending[0].read_bytes()
                != victim_finals[0].read_bytes()
                and attacker.profile.profile_id
                in attacker_pending[0].read_text("ascii")
            )
        }


def _interrupted_deletion_recreation_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-tombstone-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        with patch(
            "kirby2.instructor.identity.os.rename",
            side_effect=OSError("injected receipt publish interruption"),
        ):
            interrupted = _raises(
                lambda: delete_identity_mapping(
                    paths,
                    creation.profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:01Z",
                )
            )
        recreation_refused = _raises(
            lambda: create_identity_mapping(
                paths,
                creation.profile,
                direct_identity,
                opaque_entropy=_LEARNER_ENTROPY,
            )
        )
        recover_pending_identity_deletions(paths)
        receipt_directory = (
            paths.evidence / IDENTITY_DELETION_RECEIPT_DIRECTORY
        )
        pending = tuple(receipt_directory.glob(".pending-*.json"))
        finals = tuple(
            receipt_directory.glob("identity-deletion-receipt-*.json")
        )
        final_resolves = False
        if len(finals) == 1:
            receipt_id = finals[0].name.removesuffix(".json")
            final_resolves = (
                resolve_identity_deletion_receipt(paths, receipt_id).receipt_id
                == receipt_id
            )
        return {
            "interrupted_deletion_tombstone_prevents_recreation": (
                interrupted
                and recreation_refused
                and not identity_mapping_path(
                    paths,
                    creation.profile.profile_id,
                ).exists()
                and not pending
                and len(finals) == 1
                and final_resolves
            )
        }


def _lock_failure_cleanup_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-lock-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        captured_descriptors: list[int] = []

        def fail_lock(descriptor: int, operation: int) -> None:
            del operation
            captured_descriptors.append(descriptor)
            raise OSError("injected identity-store lock failure")

        with patch(
            "kirby2.instructor.identity.fcntl.flock",
            side_effect=fail_lock,
        ):
            creation_refused = _raises(
                lambda: create_local_learner_identity(
                    paths,
                    direct_identity,
                    opaque_entropy=_LEARNER_ENTROPY,
                )
            )
        descriptor_closed = bool(captured_descriptors) and _raises(
            lambda: os.fstat(captured_descriptors[0])
        )
        mappings = (
            ()
            if not paths.identity_mappings.exists()
            else tuple(
                path
                for path in paths.identity_mappings.glob("*.json")
                if not path.name.startswith(".identity-store")
            )
        )
        return {
            "lock_acquisition_failure_closes_descriptor": (
                creation_refused and descriptor_closed and not mappings
            )
        }


def _pending_live_mapping_conflict_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-conflict-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=True,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        with patch(
            "kirby2.instructor.identity.os.rename",
            side_effect=OSError("injected receipt publish interruption"),
        ):
            interrupted = _raises(
                lambda: delete_identity_mapping(
                    paths,
                    creation.profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:01Z",
                )
            )
        mapping_path = identity_mapping_path(paths, creation.profile.profile_id)
        mapping_path.write_bytes(creation.mapping.canonical_bytes())
        os.chmod(mapping_path, 0o600)
        recovery_refused = _raises(
            lambda: recover_pending_identity_deletions(paths)
        )
        pending = tuple(paths.evidence.rglob(".pending-*.json"))
        return {
            "pending_receipt_live_mapping_conflict_fails_closed": (
                interrupted
                and recovery_refused
                and mapping_path.exists()
                and len(pending) == 1
            )
        }


def _hardlink_insertion_race_checks(
    direct_identity: DirectIdentityV1,
) -> dict[str, bool]:
    with TemporaryDirectory(prefix="kirby2-wo37a-hardlink-race-") as raw_root:
        paths = DataPaths(Path(raw_root).resolve())
        creation = create_local_learner_identity(
            paths,
            direct_identity,
            opaque_entropy=_LEARNER_ENTROPY,
        )
        consent = _active_consent()
        decision = decide_profile_deletion(
            consent,
            required_scope=ConsentScopeV1.LOCAL_RESEARCH_STUDY,
            requested_pseudonymous_evidence_retention=False,
            decision_time_utc="2030-06-01T00:00:00Z",
        )
        mapping_path = identity_mapping_path(paths, creation.profile.profile_id)
        alias_path = paths.identity_mappings / "injected-hardlink-alias.json"
        real_unlink = os.unlink

        def inject_alias_before_unlink(
            filename: str,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if filename == mapping_path.name and dir_fd is not None:
                os.link(mapping_path, alias_path)
            real_unlink(filename, dir_fd=dir_fd)

        with patch(
            "kirby2.instructor.identity.os.unlink",
            side_effect=inject_alias_before_unlink,
        ):
            deletion_refused = _raises(
                lambda: delete_identity_mapping(
                    paths,
                    creation.profile.profile_id,
                    decision,
                    consent,
                    deletion_time_utc="2030-06-01T00:00:01Z",
                )
            )
        receipt_directory = (
            paths.evidence / IDENTITY_DELETION_RECEIPT_DIRECTORY
        )
        receipt_files = tuple(receipt_directory.glob("*.json"))
        return {
            "hardlink_inserted_during_delete_never_gets_deleted_receipt": (
                deletion_refused
                and not mapping_path.exists()
                and alias_path.exists()
                and not receipt_files
                and all(
                    marker in alias_path.read_bytes()
                    for marker in _DIRECT_MARKERS
                )
            )
        }


def _direct_identity() -> DirectIdentityV1:
    return DirectIdentityV1(
        "Ada Learner",
        (
            DirectIdentifierV1("email", "ada.learner@example.invalid"),
            DirectIdentifierV1("student_id", "student-0007"),
        ),
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def audit_versioned_assignments_rubrics_reviews() -> tuple[
    InstructorConsoleAuditCase,
    ...,
]:
    """Exercise WO37-B through its public immutable instructor workflow."""

    from kirby2.instructor.commands import build_instructor_demo

    demo = build_instructor_demo(42)
    cases = (
        _wo37b_round_trip_case(demo),
        _wo37b_assignment_lock_case(demo),
        _wo37b_review_immutability_case(demo),
        _wo37b_rubric_correction_case(demo),
    )
    expected_names = (
        "assignment_attempt_review_artifacts_round_trip_exactly",
        "attempt_runtime_parameters_cannot_bypass_assignment_locks",
        "completed_reviews_preserve_attempts_and_refuse_later_operations",
        "rubric_correction_creates_successor_sidecars_without_mutation",
    )
    if len(cases) != WO37B_AUDIT_CASE_COUNT:
        raise RuntimeError("WO37-B audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO37-B audit case order or identity changed")
    return cases


def _case_from_checks(
    name: str,
    detail: str,
    checks: dict[str, bool],
    evidence: dict[str, object],
) -> InstructorConsoleAuditCase:
    return InstructorConsoleAuditCase(
        name=name,
        detail=detail,
        evidence={"checks": checks, **evidence},
        failures=tuple(
            key.replace("_", " ") for key, passed in checks.items() if not passed
        ),
    )


def _wo37b_round_trip_case(demo: object) -> InstructorConsoleAuditCase:
    from kirby2.instructor.commands import InstructorDemoV1, build_instructor_demo

    raw = demo.canonical_bytes()
    restored = InstructorDemoV1.from_json_bytes(raw)
    repeated = build_instructor_demo(42)
    checks = {
        "canonical_bytes_round_trip": restored.canonical_bytes() == raw,
        "deterministic_rebuild": repeated.canonical_bytes() == raw,
        "one_assignment_six_attempts": (
            restored.counts["assignments"] == 1
            and restored.counts["attempts"] == 6
        ),
        "one_rubric_six_complete_reviews": (
            restored.counts["rubrics"] == 1
            and restored.counts["completed_reviews"] == 6
        ),
        "every_attempt_binds_exact_assignment": all(
            item.assignment_revision.sha256 == restored.assignment.sha256
            for item in restored.attempts
        ),
    }
    return _case_from_checks(
        "assignment_attempt_review_artifacts_round_trip_exactly",
        (
            f"demo={restored.demo_id} attempts={len(restored.attempts)} "
            f"reviews={len(restored.review_bundle.reviews)}"
        ),
        checks,
        {"demo_id": restored.demo_id, "demo_sha256": restored.sha256},
    )


def _wo37b_assignment_lock_case(demo: object) -> InstructorConsoleAuditCase:
    from dataclasses import replace

    from kirby2.instructor.assignments import bind_assignment_attempt

    attempt = demo.attempts[0]
    original_bytes = attempt.canonical_bytes()
    tampered = replace(
        attempt.runtime_parameters,
        objective=attempt.runtime_parameters.objective + " [tampered]",
    )
    refused = _raises(
        lambda: bind_assignment_attempt(
            assignment_revision=attempt.assignment_revision,
            learner_profile_id=attempt.learner_profile_id,
            attempt_number=attempt.attempt_number,
            run_id=attempt.run_id,
            selected_lesson=attempt.selected_lesson,
            selected_scenario_variation=attempt.selected_scenario_variation,
            runtime_parameters=tampered,
            consent_evidence=attempt.consent_evidence,
            recorded_at_utc=attempt.recorded_at_utc,
            deadline_clock_evidence=attempt.deadline_clock_evidence,
        )
    )
    checks = {
        "all_eight_lock_families_are_bound": set(
            attempt.assignment_revision.spec.locks.as_dict()
        )
        == {
            "hidden_state_reveal_policy",
            "latency_sha256",
            "liquidity_sha256",
            "objective",
            "seed_policy",
            "strategy_sha256",
            "venue_count",
            "volume_sha256",
        },
        "runtime_snapshot_matches_assignment": (
            attempt.runtime_parameters.lock_snapshot()
            == attempt.assignment_revision.spec.locks
        ),
        "changed_objective_is_refused": refused,
        "failed_bypass_did_not_mutate_attempt": attempt.canonical_bytes() == original_bytes,
    }
    return _case_from_checks(
        "attempt_runtime_parameters_cannot_bypass_assignment_locks",
        f"attempt={attempt.attempt_id} bypass_refused={refused}",
        checks,
        {
            "assignment_id": attempt.assignment_revision.assignment_id,
            "attempt_id": attempt.attempt_id,
        },
    )


def _wo37b_review_immutability_case(demo: object) -> InstructorConsoleAuditCase:
    from kirby2.instructor.reviews import ReviewOperationKindV1, mark_complete

    review = demo.review_bundle.reviews[0]
    attempt = next(
        item for item in demo.attempts if item.attempt_id == review.attempt_id
    )
    review_bytes = review.canonical_bytes()
    attempt_bytes = attempt.canonical_bytes()
    operation_kinds = tuple(item.operation for item in review.sidecar.operations)
    refused = _raises(lambda: mark_complete(review))
    checks = {
        "review_binds_exact_attempt": (
            review.sidecar.attempt.attempt_id == attempt.attempt_id
            and review.sidecar.attempt.attempt_sha256 == attempt.sha256
        ),
        "review_is_opened_replayed_traced_annotated_scored_and_completed": (
            operation_kinds
            == (
                ReviewOperationKindV1.OPEN_ATTEMPT,
                ReviewOperationKindV1.REPLAY_ATTEMPT,
                ReviewOperationKindV1.INSPECT_CAUSAL_TRACE,
                ReviewOperationKindV1.ANNOTATE_TIMELINE,
                ReviewOperationKindV1.ATTACH_RUBRIC_RESULT,
                ReviewOperationKindV1.MARK_COMPLETE,
            )
        ),
        "second_completion_is_refused": refused,
        "refusal_preserves_review": review.canonical_bytes() == review_bytes,
        "review_workflow_preserves_source_attempt": attempt.canonical_bytes() == attempt_bytes,
    }
    return _case_from_checks(
        "completed_reviews_preserve_attempts_and_refuse_later_operations",
        f"review={review.review_id} operations={len(operation_kinds)} sealed={refused}",
        checks,
        {"operation_kinds": [item.value for item in operation_kinds]},
    )


def _wo37b_rubric_correction_case(demo: object) -> InstructorConsoleAuditCase:
    from dataclasses import replace

    from kirby2.instructor.rubrics import correct_rubric

    source_rubric = demo.rubric
    source_score = demo.review_bundle.scores[0]
    rubric_bytes = source_rubric.canonical_bytes()
    score_bytes = source_score.canonical_bytes()
    corrected_content = replace(
        source_rubric.content,
        title=source_rubric.content.title + " corrected",
        scoring_version=source_rubric.content.scoring_version + 1,
    )
    correction = correct_rubric(
        source_rubric,
        corrected_content,
        source_score=source_score,
        corrected_item_scores=source_score.item_scores,
    )
    checks = {
        "rubric_revision_advances_once": (
            correction.corrected_rubric.revision == source_rubric.revision + 1
        ),
        "corrected_rubric_binds_predecessor": (
            correction.corrected_rubric.rubric.predecessor_record_id
            == source_rubric.rubric_id
            and correction.corrected_rubric.rubric.predecessor_sha256
            == source_rubric.record_sha256
        ),
        "corrected_score_supersedes_source": (
            correction.corrected_score.supersedes_score_id == source_score.score_id
            and correction.corrected_score.supersedes_score_sha256 == source_score.sha256
        ),
        "attempt_binding_is_unchanged": (
            correction.corrected_score.assignment_attempt_id
            == source_score.assignment_attempt_id
            and correction.corrected_score.assignment_attempt_sha256
            == source_score.assignment_attempt_sha256
        ),
        "source_rubric_is_immutable": source_rubric.canonical_bytes() == rubric_bytes,
        "source_score_is_immutable": source_score.canonical_bytes() == score_bytes,
    }
    return _case_from_checks(
        "rubric_correction_creates_successor_sidecars_without_mutation",
        (
            f"source_revision={source_rubric.revision} "
            f"corrected_revision={correction.corrected_rubric.revision}"
        ),
        checks,
        {
            "corrected_rubric_id": correction.corrected_rubric.rubric_id,
            "corrected_score_id": correction.corrected_score.score_id,
        },
    )


def audit_reproducible_local_studies() -> tuple[
    InstructorConsoleAuditCase,
    ...,
]:
    """Exercise WO37-C protocol locks, amendments, and claim boundaries."""

    from kirby2.instructor.commands import build_instructor_demo

    demo = build_instructor_demo(42)
    cases = (
        _wo37c_round_trip_case(demo),
        _wo37c_protocol_lock_case(demo),
        _wo37c_compatibility_case(demo),
        _wo37c_claim_scope_case(demo),
    )
    expected_names = (
        "study_protocol_ledger_and_cohort_summary_round_trip_exactly",
        "locked_studies_refuse_mutation_and_record_visible_amendments",
        "incompatible_versions_refuse_pooling_or_stratify_explicitly",
        "descriptive_design_refuses_unsupported_causal_language",
    )
    if len(cases) != WO37C_AUDIT_CASE_COUNT:
        raise RuntimeError("WO37-C audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO37-C audit case order or identity changed")
    return cases


def _wo37c_round_trip_case(demo: object) -> InstructorConsoleAuditCase:
    from kirby2.instructor.cohorts import CohortSummaryV1
    from kirby2.instructor.studies import StudyExecutionLedgerV1, StudyRevisionV1

    study_raw = demo.study_revision.canonical_bytes()
    ledger_raw = demo.study_ledger.canonical_bytes()
    summary_raw = demo.cohort_summary.canonical_bytes()
    study = StudyRevisionV1.from_json_bytes(study_raw)
    ledger = StudyExecutionLedgerV1.from_json_bytes(ledger_raw)
    summary = CohortSummaryV1.from_json_bytes(summary_raw)
    checks = {
        "study_round_trip": study.canonical_bytes() == study_raw,
        "ledger_round_trip": ledger.canonical_bytes() == ledger_raw,
        "cohort_summary_round_trip": summary.canonical_bytes() == summary_raw,
        "all_six_attempts_are_bound": len(ledger.included_attempts) == 6,
        "counts_denominators_and_uncertainty_are_explicit": (
            summary.member_count == 2
            and summary.eligible_denominator == 6
            and summary.included_count + summary.missing_count == 6
            and len(summary.uncertainty) == len(summary.estimates)
        ),
        "protocol_identity_is_consistent": (
            ledger.study_revision.sha256 == study.sha256
            and summary.protocol_lock_sha256 == ledger.protocol_lock.sha256
        ),
    }
    return _case_from_checks(
        "study_protocol_ledger_and_cohort_summary_round_trip_exactly",
        (
            f"study={study.study_id} attempts={len(ledger.included_attempts)} "
            f"denominator={summary.eligible_denominator}"
        ),
        checks,
        {
            "protocol_lock_id": ledger.protocol_lock.protocol_lock_id,
            "study_id": study.study_id,
        },
    )


def _wo37c_protocol_lock_case(demo: object) -> InstructorConsoleAuditCase:
    from dataclasses import replace

    from kirby2.instructor.studies import append_study_amendment, revise_study

    study = demo.study_revision
    ledger = demo.study_ledger
    study_bytes = study.canonical_bytes()
    ledger_bytes = ledger.canonical_bytes()
    changed_manifest = replace(
        study.manifest,
        hypothesis=study.manifest.hypothesis + " Post-observation mutation.",
    )
    lock_refused = _raises(
        lambda: revise_study(
            study,
            changed_manifest,
            protocol_lock=ledger.protocol_lock,
        )
    )
    execution_refused = _raises(
        lambda: revise_study(
            study,
            changed_manifest,
            execution_ledger=ledger,
        )
    )
    amended = append_study_amendment(
        ledger,
        amended_at_utc="2026-01-02T00:00:00Z",
        rationale="Record a prospective analysis clarification without rewriting history.",
        changed_fields=("hypothesis",),
        replacement_protocol_sha256=_digest("wo37c-prospective-amendment"),
        prospective_only=True,
    )
    checks = {
        "protocol_lock_refuses_revision": lock_refused,
        "executed_ledger_refuses_revision": execution_refused,
        "amendment_is_appended": (
            len(amended.amendments) == len(ledger.amendments) + 1
            and amended.amendments[-1].prospective_only
        ),
        "amendment_binds_original_protocol": (
            amended.amendments[-1].protocol_lock_sha256
            == ledger.protocol_lock.sha256
        ),
        "source_study_is_unchanged": study.canonical_bytes() == study_bytes,
        "source_ledger_is_unchanged": ledger.canonical_bytes() == ledger_bytes,
        "included_attempts_are_preserved": (
            amended.included_attempts == ledger.included_attempts
        ),
    }
    return _case_from_checks(
        "locked_studies_refuse_mutation_and_record_visible_amendments",
        (
            f"locked_refused={lock_refused} executed_refused={execution_refused} "
            f"amendments={len(amended.amendments)}"
        ),
        checks,
        {"amendment_id": amended.amendments[-1].amendment_id},
    )


def _wo37c_compatibility_case(demo: object) -> InstructorConsoleAuditCase:
    from dataclasses import replace

    from kirby2.instructor.statistics import (
        CompatibilityActionV1,
        CompatibilityRefusalV1,
        CompatibilityResolutionV1,
        summarize_observations,
    )

    first, second = demo.cohort_summary.observations[:2]
    changed_signature = replace(
        second.version_signature,
        score_version=second.version_signature.score_version + 1,
        score_sha256=_digest("wo37c-incompatible-score-version"),
    )
    incompatible = replace(second, version_signature=changed_signature)
    observations = (first, incompatible)
    refusal = None
    try:
        summarize_observations(
            observations,
            compatibility_action=CompatibilityActionV1.POOL,
        )
    except CompatibilityRefusalV1 as error:
        refusal = error.decision
    stratified = summarize_observations(
        observations,
        compatibility_action=CompatibilityActionV1.STRATIFY,
    )
    checks = {
        "pooling_is_refused": (
            refusal is not None
            and refusal.resolution is CompatibilityResolutionV1.REFUSED
            and not refusal.can_pool
        ),
        "refusal_records_both_signatures": (
            refusal is not None and refusal.signature_count == 2
        ),
        "explicit_stratification_succeeds": (
            stratified.compatibility_decision.resolution
            is CompatibilityResolutionV1.STRATIFIED
            and len(stratified.estimates) == 2
        ),
        "source_observations_are_preserved": (
            first == demo.cohort_summary.observations[0]
            and second == demo.cohort_summary.observations[1]
        ),
    }
    return _case_from_checks(
        "incompatible_versions_refuse_pooling_or_stratify_explicitly",
        (
            f"signatures={len(stratified.compatibility_decision.signatures)} "
            f"resolution={stratified.compatibility_decision.resolution.value}"
        ),
        checks,
        {
            "stratified_summary_sha256": hashlib.sha256(
                stratified.canonical_bytes()
            ).hexdigest()
        },
    )


def _wo37c_claim_scope_case(demo: object) -> InstructorConsoleAuditCase:
    from kirby2.instructor.statistics import (
        AnalysisCapabilityV1,
        UnsupportedCausalClaimError,
        require_claim_capability,
    )

    causal_refused = False
    try:
        require_claim_capability(
            requested_capability=AnalysisCapabilityV1.CAUSAL,
            design_capability=demo.study_revision.manifest.design,
            analysis_capability=AnalysisCapabilityV1.DESCRIPTIVE,
        )
    except UnsupportedCausalClaimError:
        causal_refused = True
    descriptive_allowed = True
    try:
        require_claim_capability(
            requested_capability=AnalysisCapabilityV1.DESCRIPTIVE,
            design_capability=demo.study_revision.manifest.design,
            analysis_capability=AnalysisCapabilityV1.DESCRIPTIVE,
        )
    except (TypeError, ValueError):
        descriptive_allowed = False
    checks = {
        "causal_language_is_refused": causal_refused,
        "descriptive_language_is_allowed": descriptive_allowed,
        "study_declares_descriptive_design": (
            demo.study_revision.manifest.design.capability.value == "DESCRIPTIVE"
        ),
        "summary_remains_descriptive": (
            demo.cohort_summary.requested_capability
            is AnalysisCapabilityV1.DESCRIPTIVE
            and demo.cohort_summary.analysis_capability
            is AnalysisCapabilityV1.DESCRIPTIVE
        ),
    }
    return _case_from_checks(
        "descriptive_design_refuses_unsupported_causal_language",
        (
            f"design={demo.study_revision.manifest.design.capability.value} "
            f"causal_refused={causal_refused}"
        ),
        checks,
        {"study_id": demo.study_revision.study_id},
    )


def audit_instructor_research_console_queries() -> tuple[
    InstructorConsoleAuditCase,
    ...,
]:
    """Exercise WO37-D inventory, as-of isolation, and comparison queries."""

    from kirby2.instructor.commands import build_instructor_demo

    demo = build_instructor_demo(42)
    cases = (
        _wo37d_demo_inventory_case(demo),
        _wo37d_as_of_isolation_case(demo),
        _wo37d_comparison_matrix_case(demo),
        _wo37d_query_refusal_case(demo),
    )
    expected_names = (
        "instructor_demo_has_the_exact_local_workflow_inventory",
        "explicit_as_of_queries_are_deterministic_and_profile_isolated",
        "all_six_descriptive_comparison_shapes_are_queryable",
        "one_attempt_and_cross_profile_self_queries_are_refused",
    )
    if len(cases) != WO37D_AUDIT_CASE_COUNT:
        raise RuntimeError("WO37-D audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO37-D audit case order or identity changed")
    return cases


def _wo37d_demo_inventory_case(demo: object) -> InstructorConsoleAuditCase:
    counts = demo.counts
    checks = {
        "two_pseudonymous_learners": counts["learner_profiles"] == 2,
        "one_assignment_and_rubric": (
            counts["assignments"] == 1 and counts["rubrics"] == 1
        ),
        "three_attempts_per_learner": (
            counts["attempts"] == 6 and counts["attempts_per_learner"] == 3
        ),
        "every_attempt_is_reviewed_and_annotated": (
            counts["completed_reviews"] == 6
            and counts["annotated_reviews"] == 6
        ),
        "one_cohort_study_and_comparison": (
            counts["cohorts"] == 1
            and counts["studies"] == 1
            and counts["cohort_comparisons"] == 1
        ),
        "workflow_claim_is_bounded": (
            demo.claim_scope == "WORKFLOW_AND_PSEUDONYMOUS_PROFILE_ISOLATION_ONLY"
            and not demo.learner_difference_claim
            and not demo.cohort_difference_claim
            and not demo.causal_claim
        ),
        "external_services_are_absent": (
            demo.external_service_policy == "LOCAL_ONLY_NO_EXTERNAL_SERVICES_V1"
        ),
    }
    return _case_from_checks(
        "instructor_demo_has_the_exact_local_workflow_inventory",
        (
            f"learners={counts['learner_profiles']} attempts={counts['attempts']} "
            f"reviews={counts['completed_reviews']}"
        ),
        checks,
        {"counts": counts, "demo_id": demo.demo_id},
    )


def _wo37d_as_of_isolation_case(demo: object) -> InstructorConsoleAuditCase:
    from kirby2.instructor.query import (
        InstructorQueryScopeKindV1,
        InstructorQueryScopeV1,
        query_console_artifacts,
    )

    as_of = demo.cohort_comparison.as_of_sequence
    learner_views = []
    visible_learner_ids = []
    for learner in demo.learner_profiles:
        scope = InstructorQueryScopeV1(
            scope_kind=InstructorQueryScopeKindV1.LEARNER_SELF,
            principal_profile_id=learner.profile_id,
            learner_profile_id=learner.profile_id,
        )
        first = query_console_artifacts(
            demo.console_ledger,
            scope=scope,
            as_of=as_of,
        )
        repeated = query_console_artifacts(
            demo.console_ledger,
            scope=scope,
            as_of=as_of,
        )
        learner_views.append((first, repeated))
        visible_learner_ids.append(
            {
                source.source_id
                for row in first.rows
                for source in row.reference.source_identities
                if source.source_id.startswith("learner-profile-")
            }
        )
    first_view, first_repeat = learner_views[0]
    second_view, second_repeat = learner_views[1]
    checks = {
        "same_as_of_is_byte_identical": (
            first_view.canonical_bytes() == first_repeat.canonical_bytes()
            and second_view.canonical_bytes() == second_repeat.canonical_bytes()
        ),
        "each_scope_exposes_only_its_learner": (
            visible_learner_ids[0] == {demo.learner_profiles[0].profile_id}
            and visible_learner_ids[1] == {demo.learner_profiles[1].profile_id}
        ),
        "scopes_have_distinct_rows": (
            first_view.canonical_bytes() != second_view.canonical_bytes()
        ),
        "ledger_point_is_exact": (
            first_view.as_of_sequence == as_of
            and second_view.as_of_sequence == as_of
            and first_view.ledger_sha256
            == demo.console_ledger.as_of(as_of).head_sha256
            and second_view.ledger_sha256 == first_view.ledger_sha256
        ),
        "every_row_discloses_source_and_versions": all(
            row.reference.source_identities
            and row.reference.content_version
            and row.reference.capability.value
            for view, _ in learner_views
            for row in view.rows
        ),
    }
    return _case_from_checks(
        "explicit_as_of_queries_are_deterministic_and_profile_isolated",
        (
            f"as_of={as_of} learner_rows="
            f"{len(first_view.rows)},{len(second_view.rows)}"
        ),
        checks,
        {
            "learner_view_sha256": [
                hashlib.sha256(view.canonical_bytes()).hexdigest()
                for view in (first_view, second_view)
            ]
        },
    )


def _wo37d_comparison_matrix_case(demo: object) -> InstructorConsoleAuditCase:
    from dataclasses import replace

    from kirby2.instructor.query import (
        ComparisonExecutionModeV1,
        ComparisonViewKindV1,
        ComparisonViewV1,
        InstructorQueryScopeKindV1,
        InstructorQueryScopeV1,
        build_comparison_view,
    )

    sources = demo.cohort_comparison.sources
    learner_id = demo.learner_profiles[0].profile_id
    same_learner = tuple(
        item for item in sources if item.learner_profile_id == learner_id
    )
    local_scope = InstructorQueryScopeV1(
        scope_kind=InstructorQueryScopeKindV1.INSTRUCTOR_LOCAL,
        principal_profile_id=demo.reviewer_profile.profile_id,
    )
    research_scope = demo.cohort_comparison.scope
    manual_algorithm = (
        same_learner[0],
        replace(
            same_learner[1],
            execution_mode=ComparisonExecutionModeV1.BENCHMARK_ALGORITHM,
        ),
    )
    source_matrix = (
        (
            ComparisonViewKindV1.SAME_LEARNER_ACROSS_ATTEMPTS,
            local_scope,
            same_learner,
        ),
        (
            ComparisonViewKindV1.SAME_LESSON_ACROSS_LEARNERS,
            research_scope,
            sources,
        ),
        (
            ComparisonViewKindV1.SAME_SKILL_ACROSS_SCENARIOS,
            local_scope,
            same_learner,
        ),
        (
            ComparisonViewKindV1.SAME_HOTKEY_LAYOUT_ACROSS_SESSIONS,
            local_scope,
            same_learner,
        ),
        (
            ComparisonViewKindV1.SAME_STRATEGY_ACROSS_VOLUME_REGIMES,
            local_scope,
            same_learner,
        ),
        (
            ComparisonViewKindV1.MANUAL_EXECUTION_VS_BENCHMARK_ALGORITHM,
            local_scope,
            manual_algorithm,
        ),
    )
    views = tuple(
        build_comparison_view(
            demo.console_ledger,
            view_kind=kind,
            scope=scope,
            sources=tuple(
                sorted(
                    selected_sources,
                    key=lambda item: (
                        item.attempt_id,
                        item.attempt_sha256,
                        item.source_sha256,
                    ),
                )
            ),
            as_of=demo.cohort_comparison.as_of_sequence,
        )
        for kind, scope, selected_sources in source_matrix
    )
    restored = tuple(
        ComparisonViewV1.from_canonical_bytes(item.canonical_bytes())
        for item in views
    )
    checks = {
        "all_six_kinds_are_present": (
            tuple(item.view_kind for item in views) == tuple(ComparisonViewKindV1)
        ),
        "every_view_round_trips": all(
            left.canonical_bytes() == right.canonical_bytes()
            for left, right in zip(views, restored, strict=True)
        ),
        "every_view_is_descriptive": all(
            item.capability.value == "DESCRIPTIVE" for item in views
        ),
        "every_view_has_multiple_exact_sources": all(
            len(item.sources) >= 2 and item.sample_count >= 2 for item in views
        ),
        "every_view_uses_the_requested_ledger_point": all(
            item.as_of_sequence == demo.cohort_comparison.as_of_sequence
            for item in views
        ),
    }
    return _case_from_checks(
        "all_six_descriptive_comparison_shapes_are_queryable",
        f"views={len(views)} kinds={','.join(item.view_kind.value for item in views)}",
        checks,
        {"comparison_ids": [item.comparison_id for item in views]},
    )


def _wo37d_query_refusal_case(demo: object) -> InstructorConsoleAuditCase:
    from kirby2.instructor.query import (
        ComparisonViewKindV1,
        InstructorQueryScopeKindV1,
        InstructorQueryScopeV1,
        build_comparison_view,
    )

    sources = demo.cohort_comparison.sources
    as_of = demo.cohort_comparison.as_of_sequence
    learner = demo.learner_profiles[0]
    learner_scope = InstructorQueryScopeV1(
        scope_kind=InstructorQueryScopeKindV1.LEARNER_SELF,
        principal_profile_id=learner.profile_id,
        learner_profile_id=learner.profile_id,
    )
    one_attempt_refused = _raises(
        lambda: build_comparison_view(
            demo.console_ledger,
            view_kind=ComparisonViewKindV1.SAME_LEARNER_ACROSS_ATTEMPTS,
            scope=learner_scope,
            sources=(
                next(item for item in sources if item.learner_profile_id == learner.profile_id),
            ),
            as_of=as_of,
        )
    )
    cross_profile = tuple(
        next(
            item
            for item in sources
            if item.learner_profile_id == profile.profile_id
        )
        for profile in demo.learner_profiles
    )
    cross_profile_refused = _raises(
        lambda: build_comparison_view(
            demo.console_ledger,
            view_kind=ComparisonViewKindV1.SAME_LESSON_ACROSS_LEARNERS,
            scope=learner_scope,
            sources=tuple(
                sorted(
                    cross_profile,
                    key=lambda item: (
                        item.attempt_id,
                        item.attempt_sha256,
                        item.source_sha256,
                    ),
                )
            ),
            as_of=as_of,
        )
    )
    future_as_of_refused = _raises(
        lambda: demo.console_ledger.as_of(demo.console_ledger.head_sequence + 1)
    )
    checks = {
        "one_attempt_ranking_is_refused": one_attempt_refused,
        "learner_self_cross_profile_query_is_refused": cross_profile_refused,
        "future_ledger_point_is_refused": future_as_of_refused,
        "source_ledger_is_unchanged": (
            demo.console_ledger.head_sequence == demo.counts["console_entries"]
        ),
    }
    return _case_from_checks(
        "one_attempt_and_cross_profile_self_queries_are_refused",
        (
            f"one_attempt={one_attempt_refused} cross_profile={cross_profile_refused} "
            f"future_as_of={future_as_of_refused}"
        ),
        checks,
        {"ledger_id": demo.console_ledger.ledger_id},
    )


def audit_redacted_export_and_profile_deletion() -> tuple[
    InstructorConsoleAuditCase,
    ...,
]:
    """Exercise WO37-E export, clean import, and identity-only deletion."""

    from kirby2.instructor.commands import (
        load_privacy_export_fixture,
        run_instructor_export_demo,
    )
    from kirby2.instructor.deletion import ProfileDeletionResultV1
    from kirby2.instructor.export import (
        EvidenceExportManifestV1,
        ExportConsentDecisionV1,
        ExportInventoryV1,
        ImportedExportV1,
    )
    from kirby2.instructor.redaction import RedactionManifestV1

    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "instructor"
        / "fixtures"
        / "privacy_export.toml"
    )
    fixture = load_privacy_export_fixture(fixture_path)
    result = run_instructor_export_demo(fixture, seed=42)
    manifest = EvidenceExportManifestV1.from_dict(result["export_manifest"])
    inventory = ExportInventoryV1.from_dict(result["inventory"])
    redaction = RedactionManifestV1.from_dict(result["redaction_manifest"])
    consent = ExportConsentDecisionV1.from_dict(result["consent_decision"])
    imported = ImportedExportV1.from_dict(result["import_receipt"])
    deletion = ProfileDeletionResultV1.from_dict(result["deletion_result"])
    cases = (
        _wo37e_export_binding_case(manifest, inventory, redaction, consent, result),
        _wo37e_redaction_case(fixture, manifest, redaction, consent),
        _wo37e_clean_import_case(manifest, inventory, imported),
        _wo37e_deletion_case(manifest, deletion, result),
    )
    expected_names = (
        "export_manifest_inventory_redaction_and_consent_bind_exactly",
        "allowlist_and_omission_manifests_exclude_prohibited_content",
        "clean_root_import_binds_every_verified_export_digest",
        "profile_deletion_removes_identity_without_mutating_retained_evidence",
    )
    if len(cases) != WO37E_AUDIT_CASE_COUNT:
        raise RuntimeError("WO37-E audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO37-E audit case order or identity changed")
    return cases


def _wo37e_export_binding_case(
    manifest: object,
    inventory: object,
    redaction: object,
    consent: object,
    result: dict[str, object],
) -> InstructorConsoleAuditCase:
    checks = {
        "result_schema_is_exact": (
            result["schema_id"] == "KIRBY2_INSTRUCTOR_EXPORT_DEMO_V1"
            and result["schema_version"] == 1
            and result["seed"] == 42
        ),
        "manifest_binds_inventory": (
            manifest.inventory_id == inventory.inventory_id
            and manifest.inventory_sha256 == inventory.sha256
        ),
        "manifest_binds_redaction": (
            manifest.redaction_document_id == redaction.document_id
            and manifest.redaction_manifest_sha256 == redaction.sha256
            and manifest.redaction_policy_id == redaction.policy_id
        ),
        "manifest_binds_consent": (
            manifest.consent_decision_id == consent.decision_id
            and manifest.consent_decision_sha256 == consent.sha256
        ),
        "three_payload_artifacts_are_inventoried": len(inventory.artifacts) == 3,
        "software_and_limitations_are_explicit": (
            manifest.software_version == "0.1.0" and bool(manifest.limitations)
        ),
    }
    return _case_from_checks(
        "export_manifest_inventory_redaction_and_consent_bind_exactly",
        (
            f"export={manifest.export_id} artifacts={len(inventory.artifacts)} "
            f"redactions={len(redaction.entries)}"
        ),
        checks,
        {
            "export_id": manifest.export_id,
            "inventory_id": inventory.inventory_id,
        },
    )


def _wo37e_redaction_case(
    fixture: object,
    manifest: object,
    redaction: object,
    consent: object,
) -> InstructorConsoleAuditCase:
    omission_kinds = tuple(sorted(item.item_kind for item in manifest.omissions))
    checks = {
        "consent_explicitly_authorizes_redacted_export": (
            consent.allowed
            and consent.status.value == "AUTHORIZED"
            and consent.requested_export.value
            == "PSEUDONYMOUS_REDACTED_EVIDENCE"
        ),
        "policy_uses_exact_allowlist": (
            manifest.redaction_policy.allowlisted_paths == fixture.allowlisted_paths
        ),
        "no_hidden_path_is_authorized": (
            manifest.redaction_policy.authorized_hidden_paths == ()
            and fixture.authorized_hidden_paths == ()
        ),
        "all_prohibited_categories_are_explicit_omissions": (
            omission_kinds == fixture.omitted_categories
        ),
        "field_manifest_is_complete_and_nonempty": (
            redaction.retained_count > 0
            and redaction.omitted_count > 0
            and redaction.retained_count + redaction.omitted_count
            == len(redaction.entries)
        ),
        "prohibited_content_policy_is_closed": (
            manifest.prohibited_content_policy
            == "NO_DIRECT_IDENTITY_IDENTITY_MAPPING_SECRETS_LOCAL_PATHS_OR_"
            "UNAUTHORIZED_HIDDEN_REVEAL_DATA_V1"
        ),
    }
    return _case_from_checks(
        "allowlist_and_omission_manifests_exclude_prohibited_content",
        (
            f"retained={redaction.retained_count} omitted={redaction.omitted_count} "
            f"categories={len(omission_kinds)}"
        ),
        checks,
        {"omission_kinds": list(omission_kinds)},
    )


def _wo37e_clean_import_case(
    manifest: object,
    inventory: object,
    imported: object,
) -> InstructorConsoleAuditCase:
    checks = {
        "import_binds_export_id": imported.export_id == manifest.export_id,
        "import_binds_manifest_digest": (
            imported.manifest_sha256 == manifest.sha256
        ),
        "import_binds_inventory_digest": (
            imported.inventory_sha256 == inventory.sha256
        ),
        "import_uses_clean_evidence_slot": (
            imported.relative_directory == f"evidence/{manifest.export_id}"
        ),
        "import_receipt_round_trips": (
            type(imported).from_canonical_bytes(imported.canonical_bytes()) == imported
        ),
    }
    return _case_from_checks(
        "clean_root_import_binds_every_verified_export_digest",
        f"export={imported.export_id} slot={imported.relative_directory}",
        checks,
        {"manifest_sha256": imported.manifest_sha256},
    )


def _wo37e_deletion_case(
    manifest: object,
    deletion: object,
    result: dict[str, object],
) -> InstructorConsoleAuditCase:
    plan = deletion.plan
    sidecar = deletion.receipt_sidecar
    checks = {
        "deletion_completed": deletion.execution_status == "COMPLETED",
        "identity_mapping_only_is_deleted": (
            plan.direct_identity_action
            == "DELETE_SEPARATELY_ERASABLE_IDENTITY_MAPPING_ONLY"
            and sidecar.direct_identity_action == plan.direct_identity_action
        ),
        "retained_evidence_bytes_are_never_mutated": (
            plan.evidence_bytes_action
            == "NEVER_MUTATE_RETAINED_RUN_OR_EVIDENCE_BYTES"
            and sidecar.evidence_bytes_action == plan.evidence_bytes_action
            and result["retained_export_unchanged"] is True
        ),
        "retention_is_explicitly_authorized": (
            plan.requested_pseudonymous_evidence_retention
            and sidecar.pseudonymous_evidence_retained
            and sidecar.evidence_disposition == "AUTHORIZED_RETAINED_UNCHANGED"
        ),
        "retained_reference_binds_export": (
            len(sidecar.retained_evidence_references) == 1
            and sidecar.retained_evidence_references[0].evidence_id
            == manifest.export_id
            and sidecar.retained_evidence_references[0].evidence_sha256
            == manifest.sha256
        ),
        "deletion_result_round_trips": (
            type(deletion).from_canonical_bytes(deletion.canonical_bytes()) == deletion
        ),
    }
    return _case_from_checks(
        "profile_deletion_removes_identity_without_mutating_retained_evidence",
        (
            f"result={deletion.result_id} retained="
            f"{sidecar.pseudonymous_evidence_retained}"
        ),
        checks,
        {"deletion_result_id": deletion.result_id},
    )


__all__ = [
    "WO37A_AUDIT_CASE_COUNT",
    "WO37B_AUDIT_CASE_COUNT",
    "WO37C_AUDIT_CASE_COUNT",
    "WO37D_AUDIT_CASE_COUNT",
    "WO37E_AUDIT_CASE_COUNT",
    "InstructorConsoleAuditCase",
    "audit_pseudonymous_profiles_and_consent",
    "audit_reproducible_local_studies",
    "audit_instructor_research_console_queries",
    "audit_redacted_export_and_profile_deletion",
    "audit_versioned_assignments_rubrics_reviews",
]
