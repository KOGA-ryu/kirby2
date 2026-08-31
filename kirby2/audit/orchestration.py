"""Executable audits for deterministic orchestration identity and planning."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import json
import socket
import ssl
import stat
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.discovery.access import (
    PartitionAccessDecisionV1,
    PartitionAccessPurposeV1,
    PartitionAccessReasonV1,
    PartitionAccessRecordV1,
)
from kirby2.discovery.experiment import ExperimentPhaseV1
from kirby2.discovery.partitions import StrategyPartitionV1
from kirby2.orchestration.aggregation import (
    ExperimentAggregateV1,
    MetricValueKindV1,
    aggregate_registered_results,
)
from kirby2.orchestration.artifacts import (
    ContentRequestV1,
    ResultBundleManifestV1,
)
from kirby2.orchestration.commands import ORCHESTRATION_COMMAND_MODULE
from kirby2.orchestration.compatibility import (
    ConditionalTransferAuthorizationV1,
    OrchestrationCompatibilityRefused,
    build_content_request,
    pack_redistribution_decision_identity,
    validate_pack_transfer_authorization,
    validate_pack_transfer_completeness,
    validate_required_content_references,
)
from kirby2.orchestration.content_store import (
    ContentStoreRefused,
    OrchestrationContentStoreV1,
    ResultAttemptStageV1,
)
from kirby2.orchestration.coordinator import (
    CoordinatorRunResultV1,
    OrchestrationCoordinatorV1,
    VerifiedWorkResultV1,
    build_verified_result_manifest,
)
from kirby2.orchestration.local import (
    LOCAL_WORKER_MODULE_V1,
    LocalSubprocessBackendV1,
    SingleProcessBackendV1,
)
from kirby2.orchestration.lan import (
    FramedTlsChannelV1,
    LanCoordinatorBackendV1,
    LanWorkerServiceV1,
)
from kirby2.orchestration.leases import (
    CoordinatorStateSnapshotV1,
    CoordinatorStateStoreV1,
    CoordinatorWorkStateRecordV1,
    CoordinatorWorkStateV1,
    LeaseBookV1,
    LeaseHeartbeatV1,
    LeasePolicyV1,
    LeaseRefused,
)
from kirby2.orchestration.models import (
    DigestReferenceV1,
    ExperimentWorkPlanV1,
    LogicalWorkCellV1,
    LogicalWorkUnit,
    WorkAttempt,
    WorkAttemptOutcomeV1,
    WorkKindV1,
)
from kirby2.orchestration.planner import (
    COORDINATOR_RESPONSIBILITY_CONTRACT_V1,
    COORDINATOR_RESPONSIBILITY_SEQUENCE_V1,
    AttemptResultResolutionStatusV1,
    AttemptResultResolutionV1,
    CoordinatorResponsibilityContractV1,
    CoordinatorResponsibilityV1,
    build_experiment_work_plan,
    canonical_aggregation_order,
    resolve_successful_attempts,
)
from kirby2.orchestration.protocol import (
    InlineArtifactV1,
    MAX_LAN_PAYLOAD_BYTES_V1,
    LanMessageKindV1,
    LanProtocolEnvelopeV1,
    RuntimeAuditStatusV1,
    WorkRequestV1,
    WorkerCompatibilityV1,
    WorkerResultManifestV1,
    WorkerResultStatusV1,
    WorkerResultV1,
)
from kirby2.orchestration.resources import (
    ExperimentCancellationV1,
    ResourceAdmissionStatusV1,
    ResourceClaimV1,
    ResourceControllerV1,
    ResourceDecisionCodeV1,
    ResourceLimitsV1,
    WorkerResourceAdvertisementV1,
    record_from_canonical_bytes,
)
from kirby2.orchestration.recovery import (
    RecoveryCheckpointV1,
    RecoveryCompletionOrderV1,
    RecoveryCoordinatorV1,
    RecoveryEventKindV1,
    RecoveryExperimentStatusV1,
    RecoveryRefused,
    RecoveryWorkRecordV1,
    RecoveryWorkStateV1,
)
from kirby2.orchestration.seeds import (
    SeedDerivationV1,
    StableCellIdentityV1,
    build_master_seed_identity,
    derive_logical_cell_seed_batch,
)
from kirby2.orchestration.security import (
    DEFAULT_LAN_BIND_HOST_V1,
    TEST_PKI_CERTIFICATE_SHA256S_V1,
    TEST_PKI_ROOT_V1,
    ArtifactAccessScopeV1,
    AuthenticatedSessionV1,
    CredentialUseV1,
    LanPeerRoleV1,
    LanTlsConfigurationV1,
    SecurityRefused,
    SessionHelloV1,
    SessionReplayGuardV1,
    build_client_ssl_context,
    build_server_ssl_context,
    certificate_sha256,
    derive_authenticated_session,
    protocol_sha256,
    validate_artifact_access,
)
from kirby2.orchestration.worker import (
    complete_run_expected_output_identities,
    complete_run_runtime_audit_identities,
    execute_work_request,
    measure_local_worker_compatibility,
    run_data_only_stdio_worker,
)
from kirby2.packs.formats import canonical_manifest_bytes
from kirby2.packs.models import (
    PackContentModeV1,
    PackRedistributionPolicyV1,
)
from kirby2.research.paths import DataPaths


WO38A_AUDIT_CASE_COUNT = 5
WO38B_AUDIT_CASE_COUNT = 5
WO38C_ORCHESTRATION_AUDIT_CASE_COUNT = 4
WO38D_AUDIT_CASE_COUNT = 6
WO38E_AUDIT_CASE_COUNT = 5


@dataclass(frozen=True, slots=True)
class OrchestrationAuditCase:
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


def audit_logical_work_and_attempt_identity() -> tuple[OrchestrationAuditCase, ...]:
    """Exercise the fixed WO38-A scientific and operational identity boundary."""

    cells = _fixture_cells()
    plan = _fixture_plan(cells)
    cases = (
        _permutation_invariant_planning_case(cells, plan),
        _semantic_seed_derivation_case(cells, plan),
        _attempt_identity_separation_case(plan),
        _successful_attempt_resolution_case(plan),
        _closed_coordinator_contract_case(plan),
    )
    expected_names = (
        "planning_is_permutation_invariant_and_has_no_worker_count_input",
        "seeds_are_unique_complete_and_bound_to_stable_cell_identity",
        "operational_attempt_metadata_cannot_change_logical_identity",
        "differing_successful_results_are_determinism_failures",
        "coordinator_contract_distributes_only_complete_independent_work",
    )
    if len(cases) != WO38A_AUDIT_CASE_COUNT:
        raise RuntimeError("WO38-A audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO38-A audit case order or identity changed")
    return cases


def audit_local_orchestration() -> tuple[OrchestrationAuditCase, ...]:
    """Exercise the fixed WO38-B data-only local execution boundary."""

    compatibility = measure_local_worker_compatibility()
    plan = _local_execution_plan(compatibility)
    matrix = _execute_backend_matrix(plan, compatibility)
    cases = (
        _data_only_protocol_case(plan),
        _exact_worker_compatibility_case(plan, compatibility),
        _backend_parity_case(plan, matrix),
        _coordinator_independent_verification_case(
            plan,
            compatibility,
            matrix.single,
        ),
        _worker_authority_boundary_case(plan),
    )
    expected_names = (
        "protocol_is_canonical_data_only_and_rejects_executable_surfaces",
        "worker_compatibility_is_exact_and_forgery_is_refused",
        "single_and_local_backends_match_across_worker_counts_and_order",
        "coordinator_replays_and_refuses_self_consistent_forged_bytes",
        "worker_cannot_select_work_or_weaken_runtime_contracts",
    )
    if len(cases) != WO38B_AUDIT_CASE_COUNT:
        raise RuntimeError("WO38-B audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO38-B audit case order or identity changed")
    return cases


def audit_verified_content_exchange() -> tuple[OrchestrationAuditCase, ...]:
    """Exercise the fixed WO38-C path-free transfer and immutable CAS boundary."""

    compatibility = measure_local_worker_compatibility()
    complete_plan = _local_execution_plan(compatibility)
    plan = ExperimentWorkPlanV1(
        master_seed_identity=complete_plan.master_seed_identity,
        experiment_identity=complete_plan.experiment_identity,
        logical_units=(complete_plan.logical_units[0],),
    )
    run = OrchestrationCoordinatorV1().execute(
        plan,
        SingleProcessBackendV1(compatibility=compatibility),
    )
    logical_unit = plan.logical_units[0]
    verified_result = run.verified_results[0]
    cases = (
        _path_free_content_request_case(logical_unit),
        _immutable_result_registration_case(logical_unit, verified_result),
        _result_attempt_lifecycle_case(logical_unit, verified_result),
        _pack_redistribution_policy_case(),
    )
    expected_names = (
        "content_requests_are_exact_digest_only_and_path_free",
        "coordinator_results_register_as_immutable_verified_cas_objects",
        "result_attempts_are_private_discardable_and_never_delete_registered_data",
        "pack_redistribution_and_clean_root_completeness_fail_closed",
    )
    if len(cases) != WO38C_ORCHESTRATION_AUDIT_CASE_COUNT:
        raise RuntimeError("WO38-C orchestration audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO38-C orchestration audit case order or identity changed")
    return cases


def audit_authenticated_lan_orchestration() -> tuple[OrchestrationAuditCase, ...]:
    """Exercise the fixed WO38-D authenticated operational LAN boundary."""

    compatibility = measure_local_worker_compatibility()
    complete_plan = _local_execution_plan(compatibility)
    plan = ExperimentWorkPlanV1(
        master_seed_identity=complete_plan.master_seed_identity,
        experiment_identity=complete_plan.experiment_identity,
        logical_units=(complete_plan.logical_units[0],),
    )
    reference = OrchestrationCoordinatorV1().execute(
        plan,
        SingleProcessBackendV1(compatibility=compatibility),
    )
    logical_unit = plan.logical_units[0]
    cases = (
        _tls_configuration_and_fixture_case(),
        _lan_protocol_and_replay_case(compatibility),
        _authenticated_loopback_parity_case(plan, compatibility, reference),
        _lease_and_restart_state_case(plan, logical_unit),
        _sealed_artifact_access_case(),
        _resource_backpressure_and_cancellation_case(logical_unit),
    )
    expected_names = (
        "lan_requires_explicit_tls13_mtls_and_rejects_fixture_production_use",
        "lan_envelopes_are_canonical_bounded_nonexecutable_and_replay_safe",
        "authenticated_loopback_preserves_single_process_result_identity",
        "leases_and_restart_snapshots_are_operational_chained_and_replay_safe",
        "active_search_workers_cannot_receive_sealed_holdout_content",
        "resource_limits_backpressure_abort_and_cancel_without_scientific_success",
    )
    if len(cases) != WO38D_AUDIT_CASE_COUNT:
        raise RuntimeError("WO38-D audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO38-D audit case order or identity changed")
    return cases


def audit_distributed_recovery() -> tuple[OrchestrationAuditCase, ...]:
    """Exercise the fixed WO38-E recovery and aggregation contract."""

    compatibility = measure_local_worker_compatibility()
    plan = _local_execution_plan(compatibility)
    demo_case = _distributed_recovery_demo_case()
    with TemporaryDirectory(prefix="kirby2-wo38e-audit-") as raw_root:
        rooted_cases = _recovery_root_cases(
            plan,
            compatibility,
            Path(raw_root).resolve(),
        )
    cases = (demo_case, *rooted_cases)
    expected_names = (
        "killed_worker_restart_recovers_the_complete_multiseed_experiment",
        "late_identical_success_is_idempotent_and_conflict_is_quarantined",
        "whole_experiment_aggregation_is_exact_complete_and_order_independent",
        "cleanup_removes_only_unregistered_attempt_staging",
        "recovery_commands_emit_durable_operational_events_without_identity_drift",
    )
    if len(cases) != WO38E_AUDIT_CASE_COUNT:
        raise RuntimeError("WO38-E audit case inventory changed")
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO38-E audit case order or identity changed")
    return cases


def _path_free_content_request_case(
    logical_unit: LogicalWorkUnit,
) -> OrchestrationAuditCase:
    request = build_content_request(logical_unit)
    expected = request.content_references
    extra = tuple(
        sorted(
            (
                *expected,
                DigestReferenceV1(
                    name="dataset.unrequested",
                    sha256=_digest("WO38-C unrequested content"),
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    mismatched = tuple(
        replace(
            item,
            sha256=_digest("WO38-C mismatched requested digest"),
        )
        if item == expected[0]
        else item
        for item in expected
    )
    missing_code = _capture_compatibility_refusal_code(
        lambda: validate_required_content_references(
            logical_unit,
            expected[:-1],
        )
    )
    extra_code = _capture_compatibility_refusal_code(
        lambda: validate_required_content_references(logical_unit, extra)
    )
    digest_code = _capture_compatibility_refusal_code(
        lambda: validate_required_content_references(logical_unit, mismatched)
    )
    noncanonical_code = _capture_compatibility_refusal_code(
        lambda: validate_required_content_references(
            logical_unit,
            tuple(reversed(expected)),
        )
    )
    path_refusal = _capture_exception_text(
        lambda: ContentRequestV1(
            content_references=(
                DigestReferenceV1(
                    name="escape/path",
                    sha256=_digest("WO38-C path-like content reference"),
                ),
            )
        )
    )
    checks = {
        "request_is_the_exact_required_content_projection": (
            expected
            == validate_required_content_references(logical_unit, expected)
        ),
        "request_has_only_digest_contract_fields_and_no_path_selector": (
            frozenset(request.as_dict())
            == {
                "content_references",
                "content_request_id",
                "schema_id",
                "schema_version",
            }
            and all(
                "/" not in item.name
                and "\\" not in item.name
                and not item.name.casefold().startswith("file:")
                for item in expected
            )
        ),
        "request_round_trips_as_exact_canonical_bytes": (
            ContentRequestV1.from_canonical_bytes(request.canonical_bytes())
            == request
        ),
        "missing_extra_and_digest_substitution_are_typed_refusals": (
            missing_code == "REQUIRED_CONTENT_MISSING"
            and extra_code == "REQUIRED_CONTENT_EXTRA"
            and digest_code == "REQUIRED_CONTENT_DIGEST_MISMATCH"
        ),
        "noncanonical_order_and_path_like_names_are_refused": (
            noncanonical_code == "REQUIRED_CONTENT_NONCANONICAL"
            and path_refusal is not None
            and "canonical non-path identifier" in path_refusal
        ),
    }
    return _case(
        "content_requests_are_exact_digest_only_and_path_free",
        f"request={request.content_request_id} references={len(expected)}",
        checks,
        {
            "content_request": request.as_dict(),
            "digest_refusal": digest_code,
            "extra_refusal": extra_code,
            "missing_refusal": missing_code,
            "noncanonical_refusal": noncanonical_code,
            "path_refusal": path_refusal,
        },
    )


def _immutable_result_registration_case(
    logical_unit: LogicalWorkUnit,
    verified_result: VerifiedWorkResultV1,
) -> OrchestrationAuditCase:
    attempt_id = "wo38c-coordinator-registration"
    with TemporaryDirectory(prefix="kirby2-wo38c-result-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        paths = DataPaths(root)
        store = OrchestrationContentStoreV1(paths=paths)
        registered = OrchestrationCoordinatorV1().register_verified_result(
            logical_work_unit=logical_unit,
            verified_result=verified_result,
            content_store=store,
            attempt_id=attempt_id,
        )
        restored_manifest = store.read_result_manifest(
            registered.manifest_sha256
        )
        expected_bytes = {
            item.artifact_id: item.payload_bytes
            for item in verified_result.artifacts
        }
        restored_bytes = {
            descriptor.artifact_id: store.read_result_artifact(
                registered.manifest_sha256,
                descriptor,
            )
            for descriptor in restored_manifest.artifacts
        }
        immutable_files = tuple(
            path
            for path in paths.runs.rglob("*")
            if path.is_file() and path.name != ".content-store.lock"
        )
        relative_objects = tuple(
            sorted(str(path.relative_to(paths.runs)) for path in immutable_files)
        )
        manifest_raw = restored_manifest.canonical_bytes()
        checks = {
            "registration_binds_exact_independent_coordinator_verification": (
                restored_manifest.coordinator_verification_sha256
                == verified_result.scientific_result_sha256
                and restored_manifest.worker_compatibility_sha256
                == verified_result.worker_compatibility_sha256
            ),
            "registered_manifest_and_every_artifact_read_back_exactly": (
                restored_manifest == registered.manifest
                and restored_bytes == expected_bytes
                and ResultBundleManifestV1.from_canonical_bytes(manifest_raw)
                == restored_manifest
            ),
            "result_objects_are_digest_named_and_read_only": (
                bool(immutable_files)
                and all(_is_sha256(path.name) for path in immutable_files)
                and all(
                    not path.is_symlink()
                    and (stat.S_IMODE(path.stat().st_mode) & 0o222) == 0
                    for path in immutable_files
                )
            ),
            "manifest_is_the_registration_point_and_attempt_is_not_scientific": (
                registered.manifest_sha256
                == registered.manifest.manifest_sha256
                and registered.artifact_count == len(verified_result.artifacts)
                and attempt_id.encode("ascii") not in manifest_raw
                and not _attempt_stage_leaves(paths)
            ),
            "result_registration_uses_runs_not_the_source_transport_cache": (
                paths.runs.exists() and not paths.cache.exists()
            ),
        }
    return _case(
        "coordinator_results_register_as_immutable_verified_cas_objects",
        (
            f"manifest={registered.manifest_sha256} "
            f"artifacts={registered.artifact_count}"
        ),
        checks,
        {
            "coordinator_verification_sha256": (
                verified_result.scientific_result_sha256
            ),
            "manifest_sha256": registered.manifest_sha256,
            "relative_cas_objects": list(relative_objects),
        },
    )


def _result_attempt_lifecycle_case(
    logical_unit: LogicalWorkUnit,
    verified_result: VerifiedWorkResultV1,
) -> OrchestrationAuditCase:
    manifest = build_verified_result_manifest(logical_unit, verified_result)
    artifacts_by_id = {
        item.artifact_id: item for item in verified_result.artifacts
    }
    with TemporaryDirectory(prefix="kirby2-wo38c-attempt-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        paths = DataPaths(root)
        store = OrchestrationContentStoreV1(paths=paths)

        published_attempt = store.begin_result_attempt(
            attempt_id="wo38c-published-attempt",
            work_request_id=manifest.work_request_id,
            logical_work_unit_id=manifest.logical_work_unit_id,
        )
        for descriptor in manifest.artifacts:
            store.stage_result_artifact(
                published_attempt,
                descriptor,
                artifacts_by_id[descriptor.artifact_id].payload_bytes,
            )
        registered = store.register_result_bundle(
            published_attempt,
            manifest,
            logical_work_unit=logical_unit,
            coordinator_verification=verified_result,
        )
        published_bytes = tuple(
            store.read_result_artifact(registered.manifest_sha256, descriptor)
            for descriptor in manifest.artifacts
        )
        published_discard_code = _capture_content_store_refusal_code(
            lambda: store.discard_result_attempt(published_attempt)
        )

        disposable_attempt = store.begin_result_attempt(
            attempt_id="wo38c-disposable-attempt",
            work_request_id=manifest.work_request_id,
            logical_work_unit_id=manifest.logical_work_unit_id,
        )
        first_descriptor = manifest.artifacts[0]
        first_payload = artifacts_by_id[
            first_descriptor.artifact_id
        ].payload_bytes
        corrupted = bytearray(first_payload)
        corrupted[len(corrupted) // 2] ^= 0x01
        corruption_refusal = _capture_exception_text(
            lambda: store.stage_result_artifact(
                disposable_attempt,
                first_descriptor,
                bytes(corrupted),
            )
        )
        attempt_objects = _attempt_object_root(paths, disposable_attempt)
        names_after_corruption = _file_names(attempt_objects)
        store.stage_result_artifact(
            disposable_attempt,
            first_descriptor,
            first_payload,
        )
        staged_names = _file_names(attempt_objects)
        store.discard_result_attempt(disposable_attempt)
        repeated_discard_code = _capture_content_store_refusal_code(
            lambda: store.discard_result_attempt(disposable_attempt)
        )
        restored_manifest = store.read_result_manifest(
            registered.manifest_sha256
        )
        restored_bytes = tuple(
            store.read_result_artifact(registered.manifest_sha256, descriptor)
            for descriptor in restored_manifest.artifacts
        )
        checks = {
            "corrupted_bytes_are_refused_before_the_first_stage_write": (
                corruption_refusal is not None
                and "differs from descriptor digest" in corruption_refusal
                and names_after_corruption == ()
            ),
            "one_attempt_stages_only_its_exact_digest_named_artifact": (
                staged_names == (first_descriptor.sha256,)
                and disposable_attempt.stage_key_sha256
                != published_attempt.stage_key_sha256
            ),
            "unregistered_attempt_is_discardable_once_then_absent": (
                repeated_discard_code == "ATTEMPT_NOT_FOUND"
                and not _attempt_stage_leaves(paths)
            ),
            "registered_attempt_capability_cannot_delete_published_content": (
                published_discard_code == "ATTEMPT_NOT_FOUND"
                and restored_manifest == manifest
                and restored_bytes == published_bytes
            ),
            "operational_attempt_ids_never_enter_registered_scientific_bytes": (
                published_attempt.attempt_id.encode("ascii")
                not in restored_manifest.canonical_bytes()
                and disposable_attempt.attempt_id.encode("ascii")
                not in restored_manifest.canonical_bytes()
            ),
        }
    return _case(
        "result_attempts_are_private_discardable_and_never_delete_registered_data",
        f"manifest={registered.manifest_sha256} attempt_cleanup=EXACT",
        checks,
        {
            "corruption_refusal": corruption_refusal,
            "published_discard_refusal": published_discard_code,
            "registered_manifest_sha256": registered.manifest_sha256,
            "repeated_discard_refusal": repeated_discard_code,
            "staged_artifact_sha256": first_descriptor.sha256,
        },
    )


def _pack_redistribution_policy_case() -> OrchestrationAuditCase:
    from kirby2.audit.packs import build_clean_root_transfer_audit_fixture

    allowed, _ = build_clean_root_transfer_audit_fixture()
    prohibited = replace(
        allowed,
        license=replace(
            allowed.license,
            redistribution_policy=PackRedistributionPolicyV1.PROHIBITED,
        ),
    )
    unknown = replace(
        allowed,
        license=replace(
            allowed.license,
            redistribution_policy=PackRedistributionPolicyV1.UNKNOWN,
        ),
    )
    conditional = replace(
        allowed,
        license=replace(
            allowed.license,
            redistribution_policy=PackRedistributionPolicyV1.CONDITIONAL,
        ),
    )
    reference_only = replace(
        allowed,
        license=replace(
            allowed.license,
            content_mode=PackContentModeV1.REFERENCE_ONLY,
        ),
    )
    authorization = ConditionalTransferAuthorizationV1(
        authorization_id="wo38c-transfer-authorization",
        policy_id="wo38c-license-policy",
        pack_id=conditional.pack_id,
        manifest_sha256=hashlib.sha256(
            canonical_manifest_bytes(conditional)
        ).hexdigest(),
        authorization_evidence_sha256=_digest(
            "WO38-C conditional transfer evidence"
        ),
    )
    mismatched_authorization = replace(
        authorization,
        pack_id=_digest("WO38-C foreign authorization pack"),
    )
    allowed_decision = pack_redistribution_decision_identity(allowed)
    conditional_decision = pack_redistribution_decision_identity(
        conditional,
        authorization,
    )
    validated_authorization = validate_pack_transfer_authorization(
        conditional,
        authorization,
    )
    reference_only_authorization = validate_pack_transfer_authorization(
        reference_only
    )
    refusal_codes = {
        "conditional_missing": _capture_compatibility_refusal_code(
            lambda: validate_pack_transfer_authorization(conditional)
        ),
        "conditional_mismatch": _capture_compatibility_refusal_code(
            lambda: validate_pack_transfer_authorization(
                conditional,
                mismatched_authorization,
            )
        ),
        "prohibited": _capture_compatibility_refusal_code(
            lambda: validate_pack_transfer_authorization(prohibited)
        ),
        "reference_only": _capture_compatibility_refusal_code(
            lambda: validate_pack_transfer_completeness(reference_only)
        ),
        "unexpected_authorization": _capture_compatibility_refusal_code(
            lambda: validate_pack_transfer_authorization(
                allowed,
                authorization,
            )
        ),
        "unknown": _capture_compatibility_refusal_code(
            lambda: validate_pack_transfer_authorization(unknown)
        ),
    }
    checks = {
        "allowed_self_contained_pack_needs_no_extra_authorization": (
            validate_pack_transfer_authorization(allowed) is None
            and validate_pack_transfer_completeness(allowed) == allowed
        ),
        "conditional_permission_is_exact_digest_bound_evidence": (
            validated_authorization == authorization
            and validate_pack_transfer_completeness(conditional)
            == conditional
            and ConditionalTransferAuthorizationV1.from_canonical_bytes(
                authorization.canonical_bytes()
            )
            == authorization
            and conditional_decision.sha256
            != allowed_decision.sha256
        ),
        "prohibited_and_unknown_redistribution_fail_closed": (
            refusal_codes["prohibited"] == "REDISTRIBUTION_PROHIBITED"
            and refusal_codes["unknown"] == "REDISTRIBUTION_UNKNOWN"
        ),
        "conditional_transfer_requires_matching_authorization_only": (
            refusal_codes["conditional_missing"]
            == "CONDITIONAL_AUTHORIZATION_REQUIRED"
            and refusal_codes["conditional_mismatch"]
            == "CONDITIONAL_AUTHORIZATION_MISMATCH"
            and refusal_codes["unexpected_authorization"]
            == "CONDITIONAL_AUTHORIZATION_UNEXPECTED"
        ),
        "reference_only_bytes_cannot_claim_clean_root_reproduction": (
            reference_only_authorization is None
            and refusal_codes["reference_only"]
            == "REFERENCE_ONLY_CONTENT_INCOMPLETE"
        ),
    }
    return _case(
        "pack_redistribution_and_clean_root_completeness_fail_closed",
        (
            f"allowed={allowed.pack_id} "
            f"conditional={conditional.pack_id}"
        ),
        checks,
        {
            "allowed_decision": allowed_decision.as_dict(),
            "conditional_authorization_sha256": (
                authorization.authorization_sha256
            ),
            "conditional_decision": conditional_decision.as_dict(),
            "refusal_codes": refusal_codes,
        },
    )


def _tls_configuration_and_fixture_case() -> OrchestrationAuditCase:
    limits = _lan_audit_resource_limits()
    coordinator, worker = _lan_fixture_configurations(
        _available_loopback_port()
    )
    manifest_raw = TEST_PKI_ROOT_V1.joinpath(
        "fixture_manifest.json"
    ).read_bytes()
    fixture = json.loads(manifest_raw.decode("ascii"))
    canonical_fixture_raw = json.dumps(
        fixture,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    declared_file_hashes = fixture["file_sha256"]
    actual_file_hashes = {
        name: hashlib.sha256(TEST_PKI_ROOT_V1.joinpath(name).read_bytes()).hexdigest()
        for name in declared_file_hashes
    }
    actual_certificate_hashes = {
        "ca.cert.pem": certificate_sha256(
            TEST_PKI_ROOT_V1 / "ca.cert.pem"
        ),
        "coordinator.cert.pem": certificate_sha256(
            TEST_PKI_ROOT_V1 / "coordinator.cert.pem"
        ),
        "worker.cert.pem": certificate_sha256(
            TEST_PKI_ROOT_V1 / "worker.cert.pem"
        ),
    }
    disabled_code = _capture_security_refusal_code(
        lambda: build_server_ssl_context(
            replace(coordinator, enabled=False),
            allow_audit_fixture=True,
        )
    )
    production_fixture_code = _capture_security_refusal_code(
        lambda: build_server_ssl_context(
            replace(
                coordinator,
                credential_use=CredentialUseV1.OPERATOR_PRODUCTION,
            ),
            allow_audit_fixture=False,
        )
    )
    nonloopback_fixture_code = _capture_security_refusal_code(
        lambda: build_server_ssl_context(
            replace(coordinator, host="192.0.2.10"),
            allow_audit_fixture=True,
        )
    )
    fixture_without_opt_in_code = _capture_security_refusal_code(
        lambda: build_server_ssl_context(
            coordinator,
            allow_audit_fixture=False,
        )
    )
    plaintext_refusal = _capture_exception_text(
        lambda: FramedTlsChannelV1(object(), limits)
    )
    server_context: ssl.SSLContext | None = None
    client_context: ssl.SSLContext | None = None
    context_error: str | None = None
    try:
        server_context = build_server_ssl_context(
            coordinator,
            allow_audit_fixture=True,
        )
        client_context = build_client_ssl_context(
            worker,
            allow_audit_fixture=True,
        )
    except SecurityRefused as error:
        context_error = f"{error.code.value}:{error.detail}"
    certificate_set = frozenset(actual_certificate_hashes.values())
    configured_paths = {
        coordinator.ca_certificate,
        coordinator.certificate,
        coordinator.private_key,
        worker.ca_certificate,
        worker.certificate,
        worker.private_key,
    }
    checks = {
        "loopback_is_the_only_default_and_lan_requires_explicit_enablement": (
            DEFAULT_LAN_BIND_HOST_V1 == "127.0.0.1"
            and coordinator.host == DEFAULT_LAN_BIND_HOST_V1
            and worker.host == DEFAULT_LAN_BIND_HOST_V1
            and disabled_code == "LAN_NOT_EXPLICITLY_ENABLED"
        ),
        "fixture_packet_is_exact_packaged_and_excludes_ca_key_from_runtime": (
            manifest_raw == canonical_fixture_raw + b"\n"
            and actual_file_hashes == declared_file_hashes
            and actual_certificate_hashes
            == fixture["certificate_der_sha256"]
            and certificate_set == TEST_PKI_CERTIFICATE_SHA256S_V1
            and TEST_PKI_ROOT_V1.joinpath("ca.key.pem")
            not in configured_paths
        ),
        "fixture_credentials_are_audit_only_loopback_only_and_explicit": (
            production_fixture_code == "TEST_CREDENTIAL_REFUSED"
            and nonloopback_fixture_code == "NON_LOOPBACK_DEFAULT_REFUSED"
            and fixture_without_opt_in_code == "TEST_CREDENTIAL_REFUSED"
        ),
        "both_contexts_pin_exact_tls13_mtls_and_hostname_validation": (
            context_error is None
            and server_context is not None
            and client_context is not None
            and server_context.minimum_version
            is ssl.TLSVersion.TLSv1_3
            and server_context.maximum_version
            is ssl.TLSVersion.TLSv1_3
            and client_context.minimum_version
            is ssl.TLSVersion.TLSv1_3
            and client_context.maximum_version
            is ssl.TLSVersion.TLSv1_3
            and server_context.verify_mode is ssl.CERT_REQUIRED
            and client_context.verify_mode is ssl.CERT_REQUIRED
            and not server_context.check_hostname
            and client_context.check_hostname
            and worker.pinned_coordinator_certificate_sha256
            == certificate_sha256(coordinator.certificate)
        ),
        "framed_protocol_has_no_plaintext_socket_fallback": (
            plaintext_refusal is not None
            and "requires ssl.SSLSocket" in plaintext_refusal
        ),
    }
    return _case(
        "lan_requires_explicit_tls13_mtls_and_rejects_fixture_production_use",
        (
            f"protocol={protocol_sha256()} "
            f"tls13={'READY' if context_error is None else 'UNSUPPORTED'}"
        ),
        checks,
        {
            "certificate_sha256s": actual_certificate_hashes,
            "context_error": context_error,
            "disabled_refusal": disabled_code,
            "fixture_schema_id": fixture["schema_id"],
            "fixture_without_opt_in_refusal": fixture_without_opt_in_code,
            "nonloopback_fixture_refusal": nonloopback_fixture_code,
            "plaintext_refusal": plaintext_refusal,
            "production_fixture_refusal": production_fixture_code,
            "protocol_sha256": protocol_sha256(),
        },
    )


def _lan_protocol_and_replay_case(
    compatibility: WorkerCompatibilityV1,
) -> OrchestrationAuditCase:
    protocol_digest = protocol_sha256()
    resources = WorkerResourceAdvertisementV1(
        worker_id="worker.test.kirby2.invalid",
        worker_compatibility_sha256=compatibility.compatibility_sha256,
        resource_classes=("cpu-small",),
        limits=_lan_audit_resource_limits(),
        advertisement_nonce=_digest("WO38-D resource advertisement nonce"),
    )
    coordinator_hello = SessionHelloV1(
        role=LanPeerRoleV1.COORDINATOR,
        peer_identity="coordinator.test.kirby2.invalid",
        certificate_sha256=certificate_sha256(
            TEST_PKI_ROOT_V1 / "coordinator.cert.pem"
        ),
        hello_nonce=_digest("WO38-D coordinator hello"),
        protocol_sha256=protocol_digest,
        compatibility_sha256=None,
        resource_advertisement_sha256=None,
    )
    worker_hello = SessionHelloV1(
        role=LanPeerRoleV1.WORKER,
        peer_identity="worker.test.kirby2.invalid",
        certificate_sha256=certificate_sha256(
            TEST_PKI_ROOT_V1 / "worker.cert.pem"
        ),
        hello_nonce=_digest("WO38-D worker hello"),
        protocol_sha256=protocol_digest,
        compatibility_sha256=compatibility.compatibility_sha256,
        resource_advertisement_sha256=resources.advertisement_sha256,
    )
    session = derive_authenticated_session(coordinator_hello, worker_hello)
    guard = SessionReplayGuardV1(session.session_id)
    first_nonce = _digest("WO38-D first accepted message")
    guard.accept(
        session_id=session.session_id,
        sequence=1,
        nonce=first_nonce,
    )
    replay_code = _capture_security_refusal_code(
        lambda: guard.accept(
            session_id=session.session_id,
            sequence=2,
            nonce=first_nonce,
        )
    )
    gap_code = _capture_security_refusal_code(
        lambda: guard.accept(
            session_id=session.session_id,
            sequence=3,
            nonce=_digest("WO38-D sequence gap"),
        )
    )
    foreign_session_code = _capture_security_refusal_code(
        lambda: guard.accept(
            session_id=_digest("WO38-D foreign session"),
            sequence=2,
            nonce=_digest("WO38-D foreign session nonce"),
        )
    )
    envelope = LanProtocolEnvelopeV1(
        message_kind=LanMessageKindV1.WORK_REQUEST,
        session_id=session.session_id,
        sequence=1,
        nonce=_digest("WO38-D envelope nonce"),
        payload_bytes=b'{"record_id":"wo38d-audit"}',
    )
    tampered = envelope.as_dict()
    tampered["payload_sha256"] = _digest("WO38-D forged payload digest")
    tamper_refusal = _capture_exception_text(
        lambda: LanProtocolEnvelopeV1.from_dict(tampered)
    )
    executable_refusal = _capture_exception_text(
        lambda: LanProtocolEnvelopeV1(
            message_kind=LanMessageKindV1.WORK_REQUEST,
            session_id=session.session_id,
            sequence=1,
            nonce=_digest("WO38-D executable request nonce"),
            payload_bytes=b'{"python_module":"evil.module"}',
        )
    )
    oversized_refusal = _capture_exception_text(
        lambda: LanProtocolEnvelopeV1(
            message_kind=LanMessageKindV1.WORK_REQUEST,
            session_id=session.session_id,
            sequence=1,
            nonce=_digest("WO38-D oversized request nonce"),
            payload_bytes=b"x" * (MAX_LAN_PAYLOAD_BYTES_V1 + 1),
        )
    )
    message_kinds = frozenset(item.value for item in LanMessageKindV1)
    checks = {
        "hellos_bind_roles_certificates_compatibility_resources_and_protocol": (
            session.coordinator_identity == coordinator_hello.peer_identity
            and session.worker_identity == worker_hello.peer_identity
            and session.protocol_sha256 == protocol_digest
            and AuthenticatedSessionV1.from_dict(session.as_dict()) == session
        ),
        "every_bound_envelope_is_exact_canonical_and_digest_bound": (
            LanProtocolEnvelopeV1.from_canonical_bytes(
                envelope.canonical_bytes()
            )
            == envelope
            and envelope.session_id == session.session_id
            and tamper_refusal is not None
            and "payload digest differs" in tamper_refusal
        ),
        "nonces_sequences_and_session_identity_reject_replay": (
            replay_code == "SESSION_REPLAY"
            and gap_code == "SESSION_SEQUENCE_INVALID"
            and foreign_session_code == "SESSION_BINDING_MISMATCH"
        ),
        "executable_and_oversized_payloads_fail_before_dispatch": (
            executable_refusal is not None
            and "forbidden executable payload field" in executable_refusal
            and oversized_refusal is not None
            and "exceeds its V1 limit" in oversized_refusal
        ),
        "message_vocabulary_is_closed_data_records_without_shell_dispatch": (
            "WORK_REQUEST" in message_kinds
            and "CONTENT_REQUEST" in message_kinds
            and not (
                message_kinds
                & {"COMMAND", "EXEC", "PYTHON", "SHELL", "SOURCE"}
            )
            and _is_sha256(protocol_digest)
        ),
    }
    return _case(
        "lan_envelopes_are_canonical_bounded_nonexecutable_and_replay_safe",
        f"session={session.session_id} envelope={envelope.envelope_sha256}",
        checks,
        {
            "envelope_sha256": envelope.envelope_sha256,
            "executable_refusal": executable_refusal,
            "foreign_session_refusal": foreign_session_code,
            "replay_refusal": replay_code,
            "sequence_refusal": gap_code,
            "session_id": session.session_id,
            "tamper_refusal": tamper_refusal,
        },
    )


def _authenticated_loopback_parity_case(
    plan: ExperimentWorkPlanV1,
    compatibility: WorkerCompatibilityV1,
    reference: CoordinatorRunResultV1,
) -> OrchestrationAuditCase:
    port = _available_loopback_port()
    coordinator_configuration, worker_configuration = (
        _lan_fixture_configurations(port)
    )
    limits = _lan_audit_resource_limits()
    resources = WorkerResourceAdvertisementV1(
        worker_id=worker_configuration.local_identity,
        worker_compatibility_sha256=compatibility.compatibility_sha256,
        resource_classes=("cpu-small",),
        limits=limits,
        advertisement_nonce=_digest("WO38-D loopback resource advertisement"),
    )
    lease_policy = LeasePolicyV1(
        lease_seconds=30,
        heartbeat_interval_seconds=1,
        maximum_missed_heartbeats=3,
    )
    backend = LanCoordinatorBackendV1(
        configuration=coordinator_configuration,
        compatibility=compatibility,
        plan_id=plan.plan_id,
        worker_count=1,
        transport_limits=limits,
        lease_policy=lease_policy,
        claim_memory_bytes=limits.maximum_memory_bytes_per_run,
        claim_disk_bytes=limits.maximum_disk_bytes_per_run,
        claim_elapsed_seconds=30,
        connection_timeout_seconds=15,
        allow_audit_fixture=True,
    )
    worker = LanWorkerServiceV1(
        configuration=worker_configuration,
        compatibility=compatibility,
        resources=resources,
        connection_timeout_seconds=15,
        allow_audit_fixture=True,
    )
    incompatible_worker_refusal = _capture_exception_text(
        lambda: LanWorkerServiceV1(
            configuration=worker_configuration,
            compatibility=compatibility,
            resources=replace(
                resources,
                worker_compatibility_sha256=_digest(
                    "WO38-D incompatible worker advertisement"
                ),
            ),
            connection_timeout_seconds=15,
            allow_audit_fixture=True,
        )
    )
    temporary_before = _lan_attempt_temporary_roots()
    lan_result: CoordinatorRunResultV1 | None = None
    worker_results: tuple[WorkerResultV1, ...] = ()
    execution_error: str | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            worker_future = executor.submit(_run_lan_worker_with_retry, worker)
            lan_result = OrchestrationCoordinatorV1().execute(plan, backend)
            worker_results = worker_future.result(timeout=20)
    except (
        FutureTimeout,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        execution_error = f"{type(error).__name__}:{error}"
    temporary_after = _lan_attempt_temporary_roots()
    checks = {
        "one_explicit_outbound_worker_completes_authenticated_loopback": (
            execution_error is None
            and lan_result is not None
            and lan_result.backend_id == "authenticated-lan-v1"
            and len(worker_results) == len(plan.logical_units)
        ),
        "lan_and_single_have_identical_verified_scientific_results": (
            lan_result is not None
            and lan_result.verified_results == reference.verified_results
            and lan_result.aggregate_sha256 == reference.aggregate_sha256
        ),
        "worker_results_bind_the_exact_requests_and_compatibility": (
            len(worker_results) == len(plan.logical_units)
            and all(
                result.request.logical_work_unit == logical_unit
                and result.worker_compatibility == compatibility
                for result, logical_unit in zip(
                    worker_results,
                    plan.logical_units,
                    strict=True,
                )
            )
        ),
        "incompatible_worker_advertisement_is_refused_before_connection": (
            incompatible_worker_refusal is not None
            and "advertisement compatibility differs"
            in incompatible_worker_refusal
        ),
        "lan_operational_identity_does_not_enter_scientific_aggregate": (
            lan_result is not None
            and lan_result.backend_id != reference.backend_id
            and lan_result.scientific_dict() == reference.scientific_dict()
        ),
        "worker_attempt_directories_are_removed_after_session_completion": (
            temporary_after <= temporary_before
        ),
    }
    return _case(
        "authenticated_loopback_preserves_single_process_result_identity",
        (
            f"plan={plan.plan_id} "
            f"aggregate={reference.aggregate_sha256}"
        ),
        checks,
        {
            "execution_error": execution_error,
            "incompatible_worker_refusal": incompatible_worker_refusal,
            "lan_aggregate_sha256": (
                None if lan_result is None else lan_result.aggregate_sha256
            ),
            "loopback_port": port,
            "reference_aggregate_sha256": reference.aggregate_sha256,
            "temporary_roots_after": len(temporary_after),
            "temporary_roots_before": len(temporary_before),
            "worker_result_count": len(worker_results),
        },
    )


def _lease_and_restart_state_case(
    plan: ExperimentWorkPlanV1,
    logical_unit: LogicalWorkUnit,
) -> OrchestrationAuditCase:
    request = WorkRequestV1(
        logical_work_unit=logical_unit,
        required_runtime_audits=complete_run_runtime_audit_identities(),
    )
    policy = LeasePolicyV1(
        lease_seconds=60,
        heartbeat_interval_seconds=5,
        maximum_missed_heartbeats=3,
    )
    lease_book = LeaseBookV1(policy)
    grant = lease_book.grant(
        plan_id=plan.plan_id,
        work_request_id=request.work_request_id,
        logical_work_unit_id=logical_unit.logical_work_unit_id,
        attempt_number=1,
        worker_id="worker.wo38d.audit",
        session_id=_digest("WO38-D lease session"),
        issued_at_utc="2026-01-01T00:00:00Z",
    )
    heartbeat = LeaseHeartbeatV1(
        lease_id=grant.lease_id,
        attempt_id=grant.attempt_id,
        worker_id=grant.worker_id,
        session_id=grant.session_id,
        heartbeat_sequence=1,
        sent_at_utc="2026-01-01T00:00:01Z",
        heartbeat_nonce=_digest("WO38-D heartbeat one"),
    )
    accepted_grant = lease_book.heartbeat(heartbeat)
    heartbeat_replay_code = _capture_lease_refusal_code(
        lambda: lease_book.heartbeat(heartbeat)
    )
    heartbeat_gap_code = _capture_lease_refusal_code(
        lambda: lease_book.heartbeat(
            replace(
                heartbeat,
                heartbeat_sequence=3,
                heartbeat_nonce=_digest("WO38-D heartbeat gap"),
            )
        )
    )
    completed_grant = lease_book.complete(grant.lease_id)
    duplicate_completion_code = _capture_lease_refusal_code(
        lambda: lease_book.complete(grant.lease_id)
    )

    records = _coordinator_state_inventory()
    first_snapshot = CoordinatorStateSnapshotV1(
        plan_id=plan.plan_id,
        revision=1,
        previous_snapshot_sha256=None,
        records=records,
    )
    second_snapshot = CoordinatorStateSnapshotV1(
        plan_id=plan.plan_id,
        revision=2,
        previous_snapshot_sha256=first_snapshot.snapshot_sha256,
        records=records,
        cancellation_sha256s=(_digest("WO38-D cancellation ledger"),),
    )
    with TemporaryDirectory(prefix="kirby2-wo38d-state-") as raw_root:
        root = Path(raw_root).resolve()
        root.chmod(0o700)
        paths = DataPaths(root)
        store = CoordinatorStateStoreV1(paths)
        first_saved = store.save(first_snapshot)
        second_saved = store.save(second_snapshot)
        restored = store.load(plan.plan_id)
        stale_refusal = _capture_exception_text(
            lambda: store.save(second_snapshot)
        )
        temporary_state_files = tuple(
            path.name
            for path in paths.checkpoints.rglob("*")
            if path.name.startswith(".coordinator-state-tmp-")
        )
    logical_raw = logical_unit.canonical_bytes()
    checks = {
        "lease_and_heartbeat_bind_attempt_worker_session_and_exact_sequence": (
            accepted_grant == grant
            and completed_grant == grant
            and heartbeat_replay_code == "HEARTBEAT_REPLAYED"
            and heartbeat_gap_code == "HEARTBEAT_SEQUENCE_GAP"
            and duplicate_completion_code == "UNKNOWN_LEASE"
        ),
        "leases_and_heartbeats_are_operational_not_scientific_identity": (
            grant.attempt_id.encode("ascii") not in logical_raw
            and grant.lease_id.encode("ascii") not in logical_raw
            and heartbeat.heartbeat_nonce.encode("ascii") not in logical_raw
        ),
        "restart_state_distinguishes_every_required_work_state": (
            frozenset(item.state for item in records)
            == frozenset(CoordinatorWorkStateV1)
        ),
        "restart_snapshots_are_canonical_atomically_saved_and_hash_chained": (
            first_saved == first_snapshot.snapshot_sha256
            and second_saved == second_snapshot.snapshot_sha256
            and restored == second_snapshot
            and CoordinatorStateSnapshotV1.from_canonical_bytes(
                second_snapshot.canonical_bytes()
            )
            == second_snapshot
            and temporary_state_files == ()
        ),
        "stale_or_forked_restart_revision_is_refused": (
            stale_refusal is not None
            and "does not extend the stored revision" in stale_refusal
        ),
    }
    return _case(
        "leases_and_restart_snapshots_are_operational_chained_and_replay_safe",
        f"lease={grant.lease_id} snapshot={second_snapshot.snapshot_sha256}",
        checks,
        {
            "duplicate_completion_refusal": duplicate_completion_code,
            "heartbeat_gap_refusal": heartbeat_gap_code,
            "heartbeat_replay_refusal": heartbeat_replay_code,
            "lease_id": grant.lease_id,
            "snapshot_sha256": second_snapshot.snapshot_sha256,
            "stale_snapshot_refusal": stale_refusal,
            "work_states": [item.state.value for item in records],
        },
    )


def _sealed_artifact_access_case() -> OrchestrationAuditCase:
    sealed_reference = DigestReferenceV1(
        name="dataset.holdout",
        sha256=_digest("WO38-D sealed holdout content"),
    )
    open_reference = DigestReferenceV1(
        name="dataset.training",
        sha256=_digest("WO38-D open training content"),
    )
    sealed_request = ContentRequestV1(
        content_references=tuple(
            sorted(
                (sealed_reference, open_reference),
                key=lambda item: item.sort_key,
            )
        )
    )
    open_request = ContentRequestV1(content_references=(open_reference,))
    search_scope = ArtifactAccessScopeV1(
        experiment_id="wo38d-access-audit",
        experiment_version=1,
        phase=ExperimentPhaseV1.SEARCH_OPEN,
        purpose=PartitionAccessPurposeV1.SEARCH_TRAIN,
        partition=StrategyPartitionV1.HOLDOUT,
        access_record_sha256=None,
    )
    search_refusal = _capture_security_refusal_code(
        lambda: validate_artifact_access(
            sealed_request,
            search_scope,
            sealed_content_sha256s=(sealed_reference.sha256,),
        )
    )
    terminal_access = PartitionAccessRecordV1(
        experiment_id=search_scope.experiment_id,
        experiment_version=search_scope.experiment_version,
        partition_manifest_sha256=_digest("WO38-D partition manifest"),
        access_ordinal=1,
        previous_access_sha256=None,
        state_before_sha256=_digest("WO38-D terminal state before access"),
        phase_before=ExperimentPhaseV1.TERMINAL_EVALUATION,
        phase_after=ExperimentPhaseV1.TERMINAL_EVALUATION,
        partition=StrategyPartitionV1.HOLDOUT,
        purpose=PartitionAccessPurposeV1.TERMINAL_EVALUATION,
        requested_member_ids=("holdout-member-0001",),
        validation_schedule_id=None,
        decision=PartitionAccessDecisionV1.GRANTED,
        reason=PartitionAccessReasonV1.GRANTED,
        metrics_visible=True,
        granted_member_ids=("holdout-member-0001",),
        candidate_freeze_sha256=_digest("WO38-D candidate freeze"),
    )
    terminal_scope = ArtifactAccessScopeV1.from_access_record(terminal_access)
    terminal_result = validate_artifact_access(
        sealed_request,
        terminal_scope,
        sealed_content_sha256s=(sealed_reference.sha256,),
        access_record=terminal_access,
    )
    mismatched_record_refusal = _capture_security_refusal_code(
        lambda: validate_artifact_access(
            sealed_request,
            terminal_scope,
            sealed_content_sha256s=(sealed_reference.sha256,),
            access_record=replace(
                terminal_access,
                experiment_id="wo38d-foreign-experiment",
            ),
        )
    )
    forged_search_scope_refusal = _capture_exception_text(
        lambda: replace(
            search_scope,
            access_record_sha256=terminal_access.access_sha256,
        )
    )
    open_result = validate_artifact_access(
        open_request,
        search_scope,
        sealed_content_sha256s=(sealed_reference.sha256,),
    )
    checks = {
        "open_search_content_remains_available_without_holdout_authority": (
            open_result == open_request
        ),
        "active_search_scope_cannot_receive_any_requested_sealed_digest": (
            search_refusal == "SEALED_ARTIFACT_REFUSED"
        ),
        "terminal_holdout_access_requires_one_exact_granted_wo35_record": (
            terminal_result == sealed_request
            and terminal_scope.access_record_sha256
            == terminal_access.access_sha256
            and mismatched_record_refusal == "SEALED_ARTIFACT_REFUSED"
        ),
        "search_open_scope_cannot_smuggle_a_terminal_access_digest": (
            forged_search_scope_refusal is not None
            and "cannot carry a sealed access record"
            in forged_search_scope_refusal
        ),
        "artifact_scope_and_request_round_trip_as_canonical_data": (
            ArtifactAccessScopeV1.from_canonical_bytes(
                terminal_scope.canonical_bytes()
            )
            == terminal_scope
            and ContentRequestV1.from_canonical_bytes(
                sealed_request.canonical_bytes()
            )
            == sealed_request
        ),
    }
    return _case(
        "active_search_workers_cannot_receive_sealed_holdout_content",
        (
            f"sealed={sealed_reference.sha256} "
            f"access={terminal_access.access_sha256}"
        ),
        checks,
        {
            "forged_search_scope_refusal": forged_search_scope_refusal,
            "mismatched_record_refusal": mismatched_record_refusal,
            "search_refusal": search_refusal,
            "sealed_content_sha256": sealed_reference.sha256,
            "terminal_access_sha256": terminal_access.access_sha256,
        },
    )


def _resource_backpressure_and_cancellation_case(
    logical_unit: LogicalWorkUnit,
) -> OrchestrationAuditCase:
    limits = ResourceLimitsV1(
        maximum_concurrent_runs=1,
        maximum_queue_depth=1,
        maximum_memory_bytes_per_run=1024,
        maximum_disk_bytes_per_run=2048,
        maximum_elapsed_seconds_per_run=60,
        maximum_message_bytes=4096,
        maximum_stream_bytes=16 * 1024,
    )
    controller = ResourceControllerV1(
        limits=limits,
        resource_classes=(logical_unit.resource_class,),
    )
    experiment_id = logical_unit.experiment_identity.sha256
    first = _resource_claim(
        experiment_id,
        "first",
        logical_unit.resource_class,
    )
    second = _resource_claim(
        experiment_id,
        "second",
        logical_unit.resource_class,
    )
    third = _resource_claim(
        experiment_id,
        "third",
        logical_unit.resource_class,
    )
    oversized = replace(
        _resource_claim(
            experiment_id,
            "oversized",
            logical_unit.resource_class,
        ),
        memory_bytes=limits.maximum_memory_bytes_per_run + 1,
    )
    first_decision = controller.admit(first)
    second_decision = controller.admit(second)
    third_decision = controller.admit(third)
    oversized_decision = controller.admit(oversized)
    abort_decisions = controller.observe_usage(
        first.claim_id,
        memory_bytes=first.memory_bytes + 1,
        disk_bytes=first.disk_bytes,
        elapsed_seconds=first.elapsed_seconds,
    )
    cancellation = ExperimentCancellationV1(
        experiment_id=experiment_id,
        cancellation_id=_digest("WO38-D whole experiment cancellation"),
        reason_code="AUDIT_OPERATOR_CANCELLED",
        sequence=1,
    )
    cancellation_decisions = controller.cancel_experiment(cancellation)
    after_cancel = _resource_claim(
        experiment_id,
        "after-cancel",
        logical_unit.resource_class,
    )
    after_cancel_decision = controller.admit(after_cancel)
    decision_inventory = (
        first_decision,
        second_decision,
        third_decision,
        oversized_decision,
        *abort_decisions,
        *cancellation_decisions,
        after_cancel_decision,
    )
    scientific_before = logical_unit.logical_work_unit_id
    round_trips = tuple(
        record_from_canonical_bytes(
            type(decision),
            decision.canonical_bytes(),
        )
        for decision in decision_inventory
    )
    checks = {
        "finite_concurrency_queues_once_then_refuses_queue_overflow": (
            first_decision.status is ResourceAdmissionStatusV1.ADMITTED
            and second_decision.status is ResourceAdmissionStatusV1.QUEUED
            and third_decision.status is ResourceAdmissionStatusV1.REFUSED
            and third_decision.code is ResourceDecisionCodeV1.QUEUE_FULL
        ),
        "oversized_claim_is_refused_before_admission": (
            oversized_decision.status is ResourceAdmissionStatusV1.REFUSED
            and oversized_decision.code
            is ResourceDecisionCodeV1.MEMORY_LIMIT_EXCEEDED
        ),
        "observed_overrun_aborts_active_attempt_and_promotes_waiting_work": (
            tuple(item.status for item in abort_decisions)
            == (
                ResourceAdmissionStatusV1.ABORTED,
                ResourceAdmissionStatusV1.ADMITTED,
            )
            and abort_decisions[0].code
            is ResourceDecisionCodeV1.MEMORY_LIMIT_EXCEEDED
        ),
        "whole_experiment_cancellation_removes_active_and_blocks_future_work": (
            len(cancellation_decisions) == 1
            and cancellation_decisions[0].status
            is ResourceAdmissionStatusV1.CANCELLED
            and after_cancel_decision.status
            is ResourceAdmissionStatusV1.CANCELLED
            and after_cancel_decision.cancellation_id
            == cancellation.cancellation_id
        ),
        "resource_decisions_are_canonical_operational_records_not_results": (
            tuple(round_trips) == decision_inventory
            and logical_unit.logical_work_unit_id == scientific_before
            and all(
                not frozenset(decision.as_dict())
                & {
                    "artifacts",
                    "manifest",
                    "runtime_audit_results",
                    "scientific_result_sha256",
                    "worker_result",
                }
                for decision in decision_inventory
            )
        ),
    }
    return _case(
        "resource_limits_backpressure_abort_and_cancel_without_scientific_success",
        (
            f"experiment={experiment_id} "
            f"decisions={len(decision_inventory)}"
        ),
        checks,
        {
            "abort_statuses": [item.status.value for item in abort_decisions],
            "after_cancel_status": after_cancel_decision.status.value,
            "cancellation_id": cancellation.cancellation_id,
            "first_status": first_decision.status.value,
            "oversized_code": oversized_decision.code.value,
            "second_status": second_decision.status.value,
            "third_code": third_decision.code.value,
        },
    )


def _distributed_recovery_demo_case() -> OrchestrationAuditCase:
    demo = next(
        command
        for command in ORCHESTRATION_COMMAND_MODULE.commands
        if command.name == "distributed-demo"
    )
    output = StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = demo.handler(
            argparse.Namespace(seed=42, kill_worker=True, workers=3)
        )
    payload = json.loads(output.getvalue())
    expected_fields = frozenset(
        {
            "aggregate_sha256",
            "completion_order",
            "coordinator_restarted",
            "lan",
            "local_worker_count",
            "logical_work_unit_count",
            "plan_id",
            "reference_backend",
            "recovered_backend",
            "retry_attempt_numbers",
            "schema_id",
            "schema_version",
            "seed_count",
            "status",
            "strategy_identity",
            "worker_killed",
        }
    )
    retry_attempt_numbers = payload.get("retry_attempt_numbers")
    strategy_identity = payload.get("strategy_identity")
    lan = payload.get("lan")
    checks = {
        "demo_uses_the_exact_bounded_machine_readable_contract": (
            exit_code == 0
            and type(payload) is dict
            and frozenset(payload) == expected_fields
            and payload.get("schema_id")
            == "KIRBY2_DISTRIBUTED_RECOVERY_DEMO_V1"
            and payload.get("schema_version") == 1
            and payload.get("status") == "PASS"
            and _is_sha256(payload.get("plan_id"))
            and _is_sha256(payload.get("aggregate_sha256"))
        ),
        "one_real_worker_is_killed_before_coordinator_reconstruction": (
            payload.get("worker_killed") is True
            and payload.get("coordinator_restarted") is True
        ),
        "all_six_unique_seeded_strategy_units_are_reissued_exactly_once": (
            payload.get("logical_work_unit_count") == 6
            and payload.get("seed_count") == 6
            and type(retry_attempt_numbers) is list
            and retry_attempt_numbers == [2] * 6
            and type(strategy_identity) is dict
            and DigestReferenceV1.from_dict(strategy_identity).name
            == "demo-strategy:passive-observer-v1"
        ),
        "single_and_three_process_reverse_completion_match_whole_aggregate": (
            payload.get("reference_backend") == "single-process-v1"
            and payload.get("recovered_backend") == "local-subprocess-v1"
            and payload.get("local_worker_count") == 3
            and payload.get("completion_order") == "REVERSE"
        ),
        "unconfigured_lan_is_truthfully_not_exercised": (
            lan
            == {
                "reason_code": "NO_EXPLICIT_LAN_CONFIGURATION",
                "status": "NOT_EXERCISED",
            }
        ),
    }
    return _case(
        "killed_worker_restart_recovers_the_complete_multiseed_experiment",
        (
            f"plan={payload.get('plan_id')} "
            f"aggregate={payload.get('aggregate_sha256')}"
        ),
        checks,
        payload,
    )


def _recovery_root_cases(
    plan: ExperimentWorkPlanV1,
    compatibility: WorkerCompatibilityV1,
    root: Path,
) -> tuple[OrchestrationAuditCase, ...]:
    root.chmod(0o700)
    paths = DataPaths(root / "primary")
    recovery = RecoveryCoordinatorV1(paths)
    plan_raw = plan.canonical_bytes()
    submitted = recovery.submit(
        plan,
        recorded_at_utc="2026-01-01T00:00:00Z",
    )
    observed = recovery.status(plan.plan_id)
    direct = OrchestrationCoordinatorV1().execute(
        plan,
        SingleProcessBackendV1(compatibility=compatibility),
    )
    completed = recovery.resume(
        plan.plan_id,
        SingleProcessBackendV1(compatibility=compatibility),
        completion_order=RecoveryCompletionOrderV1.REVERSE,
        recorded_at_utc="2026-01-01T00:01:00Z",
    )
    aggregate = completed.aggregate
    if aggregate is None:
        raise RuntimeError("WO38-E audit fixture did not complete its aggregate")
    content = OrchestrationContentStoreV1(paths=paths)
    manifest_map = {
        item.logical_work_unit_id: item.selected_manifest_sha256
        for item in completed.records
        if item.selected_manifest_sha256 is not None
    }
    reordered_aggregate = aggregate_registered_results(
        plan,
        dict(reversed(tuple(manifest_map.items()))),
        content,
    )

    target_result = direct.verified_results[0]
    target_unit = next(
        item
        for item in plan.logical_units
        if item.logical_work_unit_id == target_result.logical_work_unit_id
    )
    target_manifest = build_verified_result_manifest(
        target_unit,
        target_result,
    )
    target_manifest_sha256 = target_manifest.manifest_sha256
    registered_manifest_before = content.read_result_manifest(
        target_manifest_sha256
    )
    registered_artifacts_before = {
        descriptor.artifact_id: content.read_result_artifact(
            target_manifest_sha256,
            descriptor,
        )
        for descriptor in registered_manifest_before.artifacts
    }
    attempt = content.begin_result_attempt(
        attempt_id="wo38e-audit-cleanup-0001",
        work_request_id=target_result.work_request_id,
        logical_work_unit_id=target_result.logical_work_unit_id,
    )
    staged_descriptor = target_manifest.artifacts[0]
    staged_artifact = next(
        item
        for item in target_result.artifacts
        if item.artifact_id == staged_descriptor.artifact_id
    )
    content.stage_result_artifact(
        attempt,
        staged_descriptor,
        staged_artifact.payload_bytes,
    )
    stage_leaves_before = _attempt_stage_leaves(paths)
    cleaned = recovery.cleanup_unregistered_attempt(
        attempt,
        plan_id=plan.plan_id,
        recorded_at_utc="2026-01-01T00:02:00Z",
    )
    stage_leaves_after = _attempt_stage_leaves(paths)
    registered_manifest_after = content.read_result_manifest(
        target_manifest_sha256
    )
    registered_artifacts_after = {
        descriptor.artifact_id: content.read_result_artifact(
            target_manifest_sha256,
            descriptor,
        )
        for descriptor in registered_manifest_after.artifacts
    }

    duplicate = recovery.record_success(
        plan.plan_id,
        target_result,
        attempt_number=1,
        recorded_at_utc="2026-01-01T00:03:00Z",
    )
    conflicting_result = _conflicting_verified_result(target_result)
    quarantined = recovery.record_success(
        plan.plan_id,
        conflicting_result,
        attempt_number=1,
        recorded_at_utc="2026-01-01T00:04:00Z",
    )

    cancellation_paths = DataPaths(root / "cancelled")
    cancellation = RecoveryCoordinatorV1(cancellation_paths)
    cancellation_submitted = cancellation.submit(
        plan,
        recorded_at_utc="2026-01-01T00:05:00Z",
    )
    cancellation_observed = cancellation.status(plan.plan_id)
    cancelled = cancellation.cancel(
        plan.plan_id,
        reason_code="AUDIT_OPERATOR_CANCELLED",
        recorded_at_utc="2026-01-01T00:06:00Z",
    )
    cancelled_again = cancellation.cancel(
        plan.plan_id,
        reason_code="AUDIT_OPERATOR_CANCELLED",
        recorded_at_utc="2026-01-01T00:07:00Z",
    )
    cancelled_resume_refusal = _capture_recovery_refusal_code(
        lambda: cancellation.resume(
            plan.plan_id,
            SingleProcessBackendV1(compatibility=compatibility),
            recorded_at_utc="2026-01-01T00:08:00Z",
        )
    )

    return (
        _recovery_idempotence_case(
            plan,
            cleaned,
            duplicate,
            quarantined,
            target_result,
            conflicting_result,
            recovery.status(plan.plan_id),
        ),
        _recovery_aggregation_case(
            plan,
            direct,
            completed,
            reordered_aggregate,
        ),
        _recovery_cleanup_case(
            plan,
            completed,
            cleaned,
            attempt,
            stage_leaves_before,
            stage_leaves_after,
            registered_manifest_before,
            registered_manifest_after,
            registered_artifacts_before,
            registered_artifacts_after,
        ),
        _recovery_command_and_event_case(
            plan,
            plan_raw,
            submitted,
            observed,
            quarantined,
            cancellation_submitted,
            cancellation_observed,
            cancelled,
            cancelled_again,
            cancelled_resume_refusal,
            paths,
            cancellation_paths,
        ),
    )


def _recovery_idempotence_case(
    plan: ExperimentWorkPlanV1,
    prior: RecoveryCheckpointV1,
    duplicate: RecoveryCheckpointV1,
    quarantined: RecoveryCheckpointV1,
    original_result: VerifiedWorkResultV1,
    conflicting_result: VerifiedWorkResultV1,
    restored: RecoveryCheckpointV1,
) -> OrchestrationAuditCase:
    duplicate_record = _recovery_record(
        duplicate,
        original_result.logical_work_unit_id,
    )
    quarantined_record = _recovery_record(
        quarantined,
        original_result.logical_work_unit_id,
    )
    successful_digests = tuple(
        sorted(
            (
                original_result.scientific_result_sha256,
                conflicting_result.scientific_result_sha256,
            )
        )
    )
    checks = {
        "identical_late_success_adds_only_one_operational_event": (
            duplicate.status is RecoveryExperimentStatusV1.COMPLETED
            and duplicate.aggregate == prior.aggregate
            and duplicate.records == prior.records
            and duplicate.revision == prior.revision + 1
            and duplicate.previous_checkpoint_sha256
            == prior.checkpoint_sha256
            and duplicate.events[:-1] == prior.events
            and duplicate.events[-1].kind
            is RecoveryEventKindV1.LATE_RESULT_IDEMPOTENT
        ),
        "identical_success_retains_one_selected_result_and_manifest": (
            duplicate_record.selected_result_sha256
            == original_result.scientific_result_sha256
            and duplicate_record.successful_result_sha256s
            == (original_result.scientific_result_sha256,)
            and len(duplicate_record.registered_manifest_sha256s) == 1
        ),
        "different_success_quarantines_without_selecting_a_result": (
            quarantined.status
            is RecoveryExperimentStatusV1.QUARANTINED
            and quarantined.aggregate is None
            and quarantined_record.state
            is RecoveryWorkStateV1.QUARANTINED
            and quarantined_record.selected_result_sha256 is None
            and quarantined_record.selected_manifest_sha256 is None
            and quarantined_record.last_failure_code
            == "DETERMINISM_FAILURE"
        ),
        "both_conflicting_successes_are_retained_as_immutable_evidence": (
            quarantined_record.successful_result_sha256s
            == successful_digests
            and len(quarantined_record.registered_manifest_sha256s) == 2
            and RecoveryEventKindV1.DETERMINISM_FAILURE
            in tuple(item.kind for item in quarantined.events)
        ),
        "quarantined_checkpoint_is_canonical_durable_and_plan_exact": (
            restored == quarantined
            and RecoveryCheckpointV1.from_canonical_bytes(
                quarantined.canonical_bytes()
            )
            == quarantined
            and quarantined.plan == plan
        ),
    }
    return _case(
        "late_identical_success_is_idempotent_and_conflict_is_quarantined",
        (
            f"plan={plan.plan_id} "
            f"status={quarantined.status.value}"
        ),
        checks,
        {
            "conflicting_result_sha256": (
                conflicting_result.scientific_result_sha256
            ),
            "duplicate_checkpoint_sha256": duplicate.checkpoint_sha256,
            "original_result_sha256": original_result.scientific_result_sha256,
            "quarantined_checkpoint_sha256": quarantined.checkpoint_sha256,
            "registered_manifest_sha256s": list(
                quarantined_record.registered_manifest_sha256s
            ),
        },
    )


def _recovery_aggregation_case(
    plan: ExperimentWorkPlanV1,
    direct: CoordinatorRunResultV1,
    completed: RecoveryCheckpointV1,
    reordered: ExperimentAggregateV1,
) -> OrchestrationAuditCase:
    aggregate = completed.aggregate
    if aggregate is None:
        raise RuntimeError("WO38-E aggregate case received no aggregate")
    expected_result_digests = tuple(
        item.scientific_result_sha256 for item in direct.verified_results
    )
    checks = {
        "aggregate_covers_every_planned_logical_unit_once_in_id_order": (
            tuple(item.logical_work_unit_id for item in aggregate.members)
            == tuple(item.logical_work_unit_id for item in plan.logical_units)
            and len(aggregate.members) == len(plan.logical_units)
            and len({item.logical_work_unit_id for item in aggregate.members})
            == len(plan.logical_units)
        ),
        "member_results_are_the_independently_verified_reference_results": (
            tuple(
                item.scientific_result_sha256 for item in aggregate.members
            )
            == expected_result_digests
        ),
        "manifest_mapping_and_completion_order_cannot_change_aggregate": (
            reordered == aggregate
            and reordered.canonical_bytes() == aggregate.canonical_bytes()
            and reordered.aggregate_sha256 == aggregate.aggregate_sha256
        ),
        "metric_reductions_are_complete_exact_and_binary_float_free": (
            all(
                item.value_count == len(plan.logical_units)
                for item in aggregate.metric_columns
            )
            and all(
                item.exact_numeric_sum is not None
                for item in aggregate.metric_columns
                if item.value_kind
                in {MetricValueKindV1.INTEGER, MetricValueKindV1.DECIMAL}
            )
            and not _contains_binary_float(aggregate.as_dict())
        ),
        "whole_aggregate_round_trips_as_one_canonical_identity": (
            ExperimentAggregateV1.from_canonical_bytes(
                aggregate.canonical_bytes()
            )
            == aggregate
            and completed.status is RecoveryExperimentStatusV1.COMPLETED
        ),
    }
    return _case(
        "whole_experiment_aggregation_is_exact_complete_and_order_independent",
        (
            f"plan={plan.plan_id} "
            f"aggregate={aggregate.aggregate_sha256}"
        ),
        checks,
        {
            "aggregate_sha256": aggregate.aggregate_sha256,
            "logical_work_unit_ids": [
                item.logical_work_unit_id for item in aggregate.members
            ],
            "metric_columns": [
                item.as_dict() for item in aggregate.metric_columns
            ],
        },
    )


def _recovery_cleanup_case(
    plan: ExperimentWorkPlanV1,
    completed: RecoveryCheckpointV1,
    cleaned: RecoveryCheckpointV1,
    attempt: ResultAttemptStageV1,
    stage_leaves_before: tuple[str, ...],
    stage_leaves_after: tuple[str, ...],
    manifest_before: ResultBundleManifestV1,
    manifest_after: ResultBundleManifestV1,
    artifacts_before: dict[str, bytes],
    artifacts_after: dict[str, bytes],
) -> OrchestrationAuditCase:
    aggregate = completed.aggregate
    if aggregate is None:
        raise RuntimeError("WO38-E cleanup case received no aggregate")
    checks = {
        "one_private_attempt_stage_exists_before_exact_cleanup": (
            stage_leaves_before == (attempt.stage_key_sha256,)
        ),
        "cleanup_removes_the_attempt_stage_and_records_the_operation": (
            stage_leaves_after == ()
            and cleaned.events[-1].kind
            is RecoveryEventKindV1.STAGING_DISCARDED
            and cleaned.events[-1].logical_work_unit_id
            == attempt.logical_work_unit_id
        ),
        "registered_manifest_and_artifact_bytes_survive_cleanup_exactly": (
            manifest_after == manifest_before
            and artifacts_after == artifacts_before
            and all(
                hashlib.sha256(payload).hexdigest()
                == next(
                    item.sha256
                    for item in manifest_after.artifacts
                    if item.artifact_id == artifact_id
                )
                for artifact_id, payload in artifacts_after.items()
            )
        ),
        "cleanup_changes_only_operational_checkpoint_history": (
            cleaned.status is RecoveryExperimentStatusV1.COMPLETED
            and cleaned.aggregate == aggregate
            and cleaned.records == completed.records
            and cleaned.plan == plan
        ),
        "attempt_identity_never_enters_registered_or_aggregate_bytes": (
            attempt.attempt_id.encode("ascii")
            not in manifest_after.canonical_bytes()
            and attempt.attempt_id.encode("ascii")
            not in aggregate.canonical_bytes()
        ),
    }
    return _case(
        "cleanup_removes_only_unregistered_attempt_staging",
        (
            f"attempt={attempt.stage_key_sha256} "
            f"manifest={manifest_after.manifest_sha256}"
        ),
        checks,
        {
            "aggregate_sha256": aggregate.aggregate_sha256,
            "attempt_stage_key_sha256": attempt.stage_key_sha256,
            "manifest_sha256": manifest_after.manifest_sha256,
            "staging_after": list(stage_leaves_after),
            "staging_before": list(stage_leaves_before),
        },
    )


def _recovery_command_and_event_case(
    plan: ExperimentWorkPlanV1,
    plan_raw: bytes,
    submitted: RecoveryCheckpointV1,
    observed: RecoveryCheckpointV1,
    final_primary: RecoveryCheckpointV1,
    cancellation_submitted: RecoveryCheckpointV1,
    cancellation_observed: RecoveryCheckpointV1,
    cancelled: RecoveryCheckpointV1,
    cancelled_again: RecoveryCheckpointV1,
    cancelled_resume_refusal: str | None,
    primary_paths: DataPaths,
    cancellation_paths: DataPaths,
) -> OrchestrationAuditCase:
    orchestrate = next(
        command
        for command in ORCHESTRATION_COMMAND_MODULE.commands
        if command.name == "orchestrate"
    )
    if orchestrate.configure is None:
        raise RuntimeError("orchestrate command has no parser configuration")
    parser = argparse.ArgumentParser(add_help=False)
    orchestrate.configure(parser)
    action = next(
        item
        for item in parser._actions
        if isinstance(item, argparse._SubParsersAction)
    )
    action_names = tuple(action.choices)
    required_event_kinds = {
        RecoveryEventKindV1.SUBMITTED,
        RecoveryEventKindV1.RESUMED,
        RecoveryEventKindV1.ATTEMPT_STARTED,
        RecoveryEventKindV1.RESULT_ACCEPTED,
        RecoveryEventKindV1.RESULT_REGISTERED,
        RecoveryEventKindV1.COMPLETED,
        RecoveryEventKindV1.STAGING_DISCARDED,
        RecoveryEventKindV1.LATE_RESULT_IDEMPOTENT,
        RecoveryEventKindV1.DETERMINISM_FAILURE,
    }
    observed_event_kinds = {item.kind for item in final_primary.events}
    temporary_files = tuple(
        sorted(
            path.name
            for paths in (primary_paths, cancellation_paths)
            for path in paths.checkpoints.rglob("*")
            if path.name.startswith(".recovery-tmp-")
        )
    )
    checks = {
        "central_command_registry_exposes_the_complete_modular_surface": (
            ORCHESTRATION_COMMAND_MODULE.module_id
            == "DISTRIBUTED_ORCHESTRATION"
            and tuple(
                command.name
                for command in ORCHESTRATION_COMMAND_MODULE.commands
            )
            == ("orchestrate", "orchestration-demo", "distributed-demo")
            and action_names
            == (
                "plan",
                "coordinator",
                "worker",
                "submit",
                "status",
                "cancel",
                "resume",
                "lan-worker",
            )
        ),
        "status_reads_do_not_mutate_checkpoint_state": (
            observed == submitted
            and cancellation_observed == cancellation_submitted
        ),
        "every_primary_state_change_has_a_closed_operational_event": (
            required_event_kinds <= observed_event_kinds
            and tuple(item.sequence for item in final_primary.events)
            == tuple(range(1, len(final_primary.events) + 1))
        ),
        "whole_experiment_cancel_is_durable_idempotent_and_terminal": (
            cancelled.status is RecoveryExperimentStatusV1.CANCELLED
            and cancelled_again == cancelled
            and all(
                item.state is RecoveryWorkStateV1.CANCELLED
                for item in cancelled.records
            )
            and cancelled.events[-1].kind is RecoveryEventKindV1.CANCELLED
            and cancelled_resume_refusal == "PLAN_CANCELLED"
        ),
        "operational_events_and_atomic_storage_do_not_change_work_identity": (
            plan.canonical_bytes() == plan_raw
            and submitted.plan == final_primary.plan == cancelled.plan == plan
            and temporary_files == ()
            and RecoveryCheckpointV1.from_canonical_bytes(
                cancelled.canonical_bytes()
            )
            == cancelled
        ),
    }
    return _case(
        "recovery_commands_emit_durable_operational_events_without_identity_drift",
        (
            f"actions={len(action_names)} "
            f"events={len(final_primary.events)}"
        ),
        checks,
        {
            "action_names": list(action_names),
            "cancelled_checkpoint_sha256": cancelled.checkpoint_sha256,
            "cancelled_resume_refusal": cancelled_resume_refusal,
            "event_kinds": [item.kind.value for item in final_primary.events],
            "temporary_files": list(temporary_files),
        },
    )


def _permutation_invariant_planning_case(
    cells: tuple[LogicalWorkCellV1, ...],
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    rotated = (*cells[1:], cells[0])
    permutations = (
        cells,
        tuple(reversed(cells)),
        rotated,
        (cell for cell in rotated),
    )
    plans = tuple(_fixture_plan(candidate) for candidate in permutations)
    aggregation_orders = tuple(
        canonical_aggregation_order(reversed(candidate.logical_units))
        for candidate in plans
    )
    planner_parameters = frozenset(
        inspect.signature(build_experiment_work_plan).parameters
    )
    operational_parameters = tuple(
        sorted(
            planner_parameters
            & {
                "attempt_number",
                "completion_order",
                "lease_id",
                "wall_clock_utc",
                "worker_count",
                "worker_id",
            }
        )
    )
    checks = {
        "all_input_permutations_produce_identical_plan_bytes": all(
            candidate.canonical_bytes() == plan.canonical_bytes()
            for candidate in plans
        ),
        "plan_ids_and_logical_unit_ids_are_identical": all(
            candidate.plan_id == plan.plan_id
            and candidate.logical_units == plan.logical_units
            for candidate in plans
        ),
        "aggregation_is_sorted_only_by_logical_id": all(
            order
            == tuple(sorted(item.logical_work_unit_id for item in plan.logical_units))
            for order in aggregation_orders
        ),
        "planner_accepts_no_operational_or_worker_count_inputs": (
            operational_parameters == ()
        ),
        "canonical_plan_round_trips_exactly": (
            ExperimentWorkPlanV1.from_dict(plan.as_dict()) == plan
        ),
    }
    return _case(
        "planning_is_permutation_invariant_and_has_no_worker_count_input",
        f"plan={plan.plan_id} logical_units={len(plan.logical_units)}",
        checks,
        {
            "aggregation_order": list(aggregation_orders[0]),
            "logical_work_unit_ids": [
                item.logical_work_unit_id for item in plan.logical_units
            ],
            "operational_planner_parameters": list(operational_parameters),
            "plan_id": plan.plan_id,
        },
    )


def _semantic_seed_derivation_case(
    cells: tuple[LogicalWorkCellV1, ...],
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    stable_cells = tuple(
        StableCellIdentityV1(
            partition_id=cell.partition_id,
            cell_id=cell.cell_id,
        )
        for cell in cells
    )
    first = derive_logical_cell_seed_batch(
        plan.master_seed_identity,
        plan.experiment_identity.sha256,
        stable_cells,
    )
    second = derive_logical_cell_seed_batch(
        plan.master_seed_identity,
        plan.experiment_identity.sha256,
        reversed(stable_cells),
    )
    derivation_seeds = {
        item.cell_identity.canonical_key: item.derived_seed for item in first
    }
    plan_seeds = {
        (item.partition_id, item.cell_id): item.seed
        for item in plan.logical_units
    }

    changed_cell = replace(
        cells[0],
        configuration={
            **dict(cells[0].configuration),
            "scientific_variant": "changed",
        },
    )
    changed_plan = _fixture_plan((changed_cell, *cells[1:]))
    baseline_unit = _unit_for_cell(plan, cells[0])
    changed_unit = _unit_for_cell(changed_plan, changed_cell)
    duplicate_refusal = _capture_exception_text(
        lambda: derive_logical_cell_seed_batch(
            plan.master_seed_identity,
            plan.experiment_identity.sha256,
            (*stable_cells, stable_cells[0]),
        )
    )
    checks = {
        "seed_batch_is_permutation_invariant": (
            tuple(item.as_dict() for item in first)
            == tuple(item.as_dict() for item in second)
        ),
        "every_cell_has_one_unique_seed": (
            len(first) == len(cells)
            and len({item.derived_seed for item in first}) == len(cells)
        ),
        "plan_seeds_match_versioned_semantic_derivations": (
            derivation_seeds == plan_seeds
        ),
        "configuration_changes_work_identity_not_stable_cell_seed": (
            baseline_unit.seed == changed_unit.seed
            and baseline_unit.logical_work_unit_id
            != changed_unit.logical_work_unit_id
        ),
        "duplicate_stable_cell_identity_is_refused": (
            duplicate_refusal is not None
            and "duplicate stable cell identity" in duplicate_refusal
        ),
        "seed_derivation_records_round_trip_exactly": all(
            SeedDerivationV1.from_dict(item.as_dict()) == item for item in first
        ),
    }
    return _case(
        "seeds_are_unique_complete_and_bound_to_stable_cell_identity",
        (
            f"master={plan.master_seed_identity.identity_sha256} "
            f"seed_count={len(first)}"
        ),
        checks,
        {
            "derivations": [item.as_dict() for item in first],
            "duplicate_refusal": duplicate_refusal,
            "master_seed_identity_sha256": (
                plan.master_seed_identity.identity_sha256
            ),
        },
    )


def _attempt_identity_separation_case(
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    unit = plan.logical_units[0]
    original_unit_bytes = unit.canonical_bytes()
    artifact = _digest("WO38-A successful artifact A")
    first = _attempt(unit, 1, "worker-a", artifact=artifact)
    changed_metadata = replace(
        first,
        worker_id="worker-b",
        lease_id="lease-b",
        heartbeat_sequence=2,
        last_heartbeat_at_utc="2026-01-01T00:02:00Z",
        diagnostics={"heartbeat_lag_microseconds": 7000},
        recorded_at_utc="2026-01-01T00:03:00Z",
    )
    retry = replace(
        changed_metadata,
        attempt_number=2,
        worker_id="worker-c",
        lease_id="lease-c",
    )
    rejected_configurations = (
        {"worker_count": 8},
        {"scientific": {"leaseId": "hidden-lease"}},
        {"attempt": {"number": 2}},
    )
    operational_refusals = tuple(
        _capture_exception_text(
            lambda configuration=configuration, index=index: LogicalWorkCellV1(
                partition_id="refusal-partition",
                cell_id=f"refusal-cell-{index}",
                work_kind=WorkKindV1.COMPLETE_RUN,
                configuration=configuration,
            )
        )
        for index, configuration in enumerate(rejected_configurations)
    )
    checks = {
        "worker_lease_heartbeat_and_diagnostics_change_only_record_digest": (
            first.attempt_id == changed_metadata.attempt_id
            and first.record_sha256 != changed_metadata.record_sha256
        ),
        "reissue_changes_attempt_identity_but_retains_logical_identity": (
            retry.attempt_id != first.attempt_id
            and retry.logical_work_unit_id == first.logical_work_unit_id
            == unit.logical_work_unit_id
        ),
        "attempt_activity_leaves_scientific_bytes_unchanged": (
            unit.canonical_bytes() == original_unit_bytes
        ),
        "nested_and_normalized_operational_configuration_keys_are_refused": (
            all(
                refusal is not None and "operational key" in refusal
                for refusal in operational_refusals
            )
        ),
        "attempt_records_round_trip_exactly": (
            WorkAttempt.from_dict(first.as_dict()) == first
            and WorkAttempt.from_dict(changed_metadata.as_dict())
            == changed_metadata
            and WorkAttempt.from_dict(retry.as_dict()) == retry
        ),
    }
    return _case(
        "operational_attempt_metadata_cannot_change_logical_identity",
        f"logical={unit.logical_work_unit_id} attempts=2",
        checks,
        {
            "first_attempt_id": first.attempt_id,
            "first_record_sha256": first.record_sha256,
            "operational_refusals": list(operational_refusals),
            "retry_attempt_id": retry.attempt_id,
            "retry_record_sha256": retry.record_sha256,
        },
    )


def _successful_attempt_resolution_case(
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    unit = plan.logical_units[0]
    selected_artifact = _digest("WO38-A successful artifact A")
    conflicting_artifact = _digest("WO38-A successful artifact B")
    first = _attempt(unit, 1, "worker-a", artifact=selected_artifact)
    second = _attempt(unit, 2, "worker-b", artifact=selected_artifact)
    conflicting = _attempt(unit, 3, "worker-c", artifact=conflicting_artifact)
    failed = _attempt(
        unit,
        4,
        "worker-d",
        outcome=WorkAttemptOutcomeV1.FAILED,
    )

    selected = resolve_successful_attempts(
        unit.logical_work_unit_id,
        (failed, second, first),
    )
    selected_reversed = resolve_successful_attempts(
        unit.logical_work_unit_id,
        (first, second, failed),
    )
    selected_with_duplicate = resolve_successful_attempts(
        unit.logical_work_unit_id,
        (first, second, first, failed),
    )
    conflict = resolve_successful_attempts(
        unit.logical_work_unit_id,
        (conflicting, failed, first),
    )
    no_success = resolve_successful_attempts(
        unit.logical_work_unit_id,
        (failed,),
    )
    foreign_attempt = replace(
        failed,
        logical_work_unit_id=plan.logical_units[1].logical_work_unit_id,
    )
    mixed_identity_refusal = _capture_exception_text(
        lambda: resolve_successful_attempts(
            unit.logical_work_unit_id,
            (failed, foreign_attempt),
        )
    )
    checks = {
        "same_successful_digest_selects_one_result_order_independently": (
            selected.status is AttemptResultResolutionStatusV1.RESULT_SELECTED
            and selected.selected_artifact_sha256 == selected_artifact
            and selected == selected_reversed == selected_with_duplicate
        ),
        "differing_successful_digests_select_nothing": (
            conflict.status
            is AttemptResultResolutionStatusV1.DETERMINISM_FAILURE
            and conflict.selected_artifact_sha256 is None
        ),
        "every_conflicting_success_record_is_quarantined": (
            conflict.quarantined_attempt_record_sha256s
            == tuple(sorted((first.record_sha256, conflicting.record_sha256)))
        ),
        "failed_attempts_do_not_create_scientific_results": (
            no_success.status
            is AttemptResultResolutionStatusV1.NO_SUCCESSFUL_RESULT
            and no_success.selected_artifact_sha256 is None
        ),
        "attempt_history_cannot_span_logical_units": (
            mixed_identity_refusal is not None
            and "spans more than one logical work unit" in mixed_identity_refusal
        ),
        "resolution_records_round_trip_exactly": (
            AttemptResultResolutionV1.from_dict(selected.as_dict()) == selected
            and AttemptResultResolutionV1.from_dict(conflict.as_dict())
            == conflict
        ),
    }
    return _case(
        "differing_successful_results_are_determinism_failures",
        (
            f"selected={selected.resolution_sha256} "
            f"quarantined={len(conflict.quarantined_attempt_record_sha256s)}"
        ),
        checks,
        {
            "conflict": conflict.as_dict(),
            "mixed_identity_refusal": mixed_identity_refusal,
            "selected": selected.as_dict(),
        },
    )


def _closed_coordinator_contract_case(
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    contract = COORDINATOR_RESPONSIBILITY_CONTRACT_V1
    expected_responsibilities = tuple(CoordinatorResponsibilityV1)
    expected_work_kinds = (
        WorkKindV1.COMPLETE_RUN,
        WorkKindV1.COUNTERFACTUAL_BRANCH,
        WorkKindV1.CALIBRATION,
        WorkKindV1.STRATEGY_EVALUATION,
    )
    scientific_identity_fields = frozenset(
        {
            "capabilities",
            "cell",
            "compiler_identity",
            "datasets",
            "dependency_identity",
            "engine_identity",
            "expected_outputs",
            "experiment_identity",
            "market_profile",
            "packs",
            "resource_class",
            "runtime_identity",
            "scenario",
            "schema_id",
            "schema_version",
            "schemas",
            "seed",
            "software_version",
            "source_version",
            "strategies",
        }
    )
    operational_fields = {
        "attempt_id",
        "attempt_number",
        "heartbeat",
        "lease_id",
        "wall_clock_utc",
        "worker_count",
        "worker_id",
    }
    checks = {
        "coordinator_owns_the_exact_closed_responsibility_sequence": (
            contract.responsibilities
            == COORDINATOR_RESPONSIBILITY_SEQUENCE_V1
            == expected_responsibilities
            and len(set(contract.responsibilities))
            == len(contract.responsibilities)
        ),
        "coordinator_contract_round_trips_and_hashes_canonical_bytes": (
            CoordinatorResponsibilityContractV1.from_dict(contract.as_dict())
            == contract
            and contract.contract_sha256
            == hashlib.sha256(contract.canonical_bytes()).hexdigest()
        ),
        "work_kind_inventory_is_exactly_the_four_independent_v1_units": (
            tuple(WorkKindV1) == expected_work_kinds
            and tuple(sorted(item.work_kind.value for item in plan.logical_units))
            == tuple(sorted(item.value for item in expected_work_kinds))
        ),
        "logical_identity_binds_every_required_scientific_field": all(
            frozenset(item.identity_dict()) == scientific_identity_fields
            for item in plan.logical_units
        ),
        "logical_identity_contains_no_attempt_worker_lease_or_clock_field": all(
            not operational_fields.intersection(item.identity_dict())
            for item in plan.logical_units
        ),
    }
    return _case(
        "coordinator_contract_distributes_only_complete_independent_work",
        (
            f"responsibilities={len(contract.responsibilities)} "
            f"work_kinds={len(expected_work_kinds)}"
        ),
        checks,
        {
            "contract_sha256": contract.contract_sha256,
            "responsibilities": [
                item.value for item in contract.responsibilities
            ],
            "work_kinds": [item.value for item in expected_work_kinds],
        },
    )


@dataclass(frozen=True, slots=True)
class _BackendExecutionMatrixV1:
    single: CoordinatorRunResultV1
    local_one: CoordinatorRunResultV1
    local_three: CoordinatorRunResultV1
    reversed_completion: CoordinatorRunResultV1


def _data_only_protocol_case(
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    unit = plan.logical_units[0]
    required_audits = complete_run_runtime_audit_identities()
    request = WorkRequestV1(
        logical_work_unit=unit,
        required_runtime_audits=required_audits,
    )
    executable_fields = (
        "shell_command",
        "python_source",
        "pickle_payload",
        "dynamic_import",
        "module_path",
    )
    executable_refusals = tuple(
        _capture_exception_text(
            lambda field=field: _request_with_configuration(
                unit,
                required_audits,
                {**dict(unit.configuration), field: "forbidden"},
            )
        )
        for field in executable_fields
    )
    inert_identity_request = _request_with_configuration(
        unit,
        required_audits,
        {**dict(unit.configuration), "source_version": "digest-bound-v1"},
    )

    input_stream = StringIO(request.canonical_bytes().decode("ascii") + "\n")
    output_stream = StringIO()
    run_data_only_stdio_worker(
        lambda payload: {
            "accepted": True,
            "work_request_id": payload["work_request_id"],
        },
        input_stream=input_stream,
        output_stream=output_stream,
    )
    expected_output = (
        '{"accepted":true,"work_request_id":"'
        + request.work_request_id
        + '"}\n'
    )
    malformed_refusals = tuple(
        _stdio_refusal(raw)
        for raw in (
            '{"b":1,"a":2}',
            '{"a":1,"a":2}',
            '{"value":NaN}',
            '{"a":1}\n{"b":2}\n',
        )
    )
    unknown_payload = request.as_dict()
    unknown_payload["shell_command"] = "python -c forbidden"
    unknown_field_refusal = _capture_exception_text(
        lambda: WorkRequestV1.from_dict(unknown_payload)
    )
    checks = {
        "request_is_exact_canonical_typed_data": (
            WorkRequestV1.from_dict(request.as_dict()) == request
            and request.canonical_bytes().decode("ascii").startswith("{")
        ),
        "shell_source_pickle_import_and_module_path_fields_are_refused": (
            all(
                refusal is not None
                and "forbidden executable payload field" in refusal
                for refusal in executable_refusals
            )
        ),
        "inert_digest_or_version_identity_metadata_remains_data": (
            inert_identity_request.logical_work_unit.configuration[
                "source_version"
            ]
            == "digest-bound-v1"
        ),
        "stdio_accepts_one_canonical_frame_and_emits_one_final_lf": (
            output_stream.getvalue() == expected_output
        ),
        "noncanonical_duplicate_nonfinite_and_multiframe_json_are_refused": (
            all(item is not None for item in malformed_refusals)
        ),
        "unknown_protocol_fields_are_refused": (
            unknown_field_refusal is not None
            and "fields differ" in unknown_field_refusal
        ),
    }
    return _case(
        "protocol_is_canonical_data_only_and_rejects_executable_surfaces",
        f"request={request.work_request_id} executable_refusals=5",
        checks,
        {
            "executable_refusals": list(executable_refusals),
            "malformed_refusals": list(malformed_refusals),
            "request_sha256": hashlib.sha256(
                request.canonical_bytes()
            ).hexdigest(),
            "unknown_field_refusal": unknown_field_refusal,
            "worker_module": LOCAL_WORKER_MODULE_V1,
        },
    )


def _exact_worker_compatibility_case(
    plan: ExperimentWorkPlanV1,
    compatibility: WorkerCompatibilityV1,
) -> OrchestrationAuditCase:
    unit = plan.logical_units[0]
    request = WorkRequestV1(
        logical_work_unit=unit,
        required_runtime_audits=complete_run_runtime_audit_identities(),
    )
    forged_engine = replace(
        compatibility.engine_identity,
        sha256=_digest("WO38-B forged engine identity"),
    )
    forged_compatibility = replace(
        compatibility,
        engine_identity=forged_engine,
    )
    forged_compatibilities = (
        forged_compatibility,
        replace(
            compatibility,
            runtime_identity=replace(
                compatibility.runtime_identity,
                sha256=_digest("WO38-B forged runtime identity"),
            ),
        ),
        replace(
            compatibility,
            dependency_identity=replace(
                compatibility.dependency_identity,
                sha256=_digest("WO38-B forged dependency identity"),
            ),
        ),
        replace(
            compatibility,
            compiler_identity=replace(
                compatibility.compiler_identity,
                sha256=_digest("WO38-B forged compiler identity"),
            ),
        ),
        replace(
            compatibility,
            schemas=(
                replace(
                    compatibility.schemas[0],
                    sha256=_digest("WO38-B forged schema identity"),
                ),
                *compatibility.schemas[1:],
            ),
        ),
        replace(
            compatibility,
            capabilities=(
                replace(
                    compatibility.capabilities[0],
                    sha256=_digest("WO38-B forged capability identity"),
                ),
                *compatibility.capabilities[1:],
            ),
        ),
    )
    forged_unit = replace(unit, engine_identity=forged_engine)
    forged_request = WorkRequestV1(
        logical_work_unit=forged_unit,
        required_runtime_audits=request.required_runtime_audits,
    )
    worker_refusal = execute_work_request(forged_request)
    backend_refusal = _capture_exception_text(
        lambda: SingleProcessBackendV1(
            compatibility=forged_compatibility
        ).execute_many((request,))
    )
    coordinator_refusals = tuple(
        _capture_exception_text(
            lambda candidate=candidate: OrchestrationCoordinatorV1().execute(
                plan,
                SingleProcessBackendV1(compatibility=candidate),
            )
        )
        for candidate in forged_compatibilities
    )
    display_only_change = replace(unit, software_version="999.0.0")
    identity_fields = frozenset(compatibility.identity_dict())
    checks = {
        "compatibility_binds_every_exact_executable_identity": identity_fields
        == {
            "capabilities",
            "compiler_identity",
            "dependency_identity",
            "engine_identity",
            "runtime_identity",
            "schema_id",
            "schema_version",
            "schemas",
        },
        "compatibility_round_trips_canonical_bytes": (
            WorkerCompatibilityV1.from_dict(compatibility.as_dict())
            == compatibility
        ),
        "display_version_is_not_an_identity_substitute": (
            compatibility.matches_logical_work_unit(display_only_change)
            and display_only_change.logical_work_unit_id
            != unit.logical_work_unit_id
        ),
        "worker_refuses_a_forged_engine_identity_before_execution": (
            worker_refusal.status
            is WorkerResultStatusV1.COMPATIBILITY_REFUSED
            and worker_refusal.manifest is None
            and worker_refusal.artifacts == ()
            and tuple(item.code for item in worker_refusal.diagnostics)
            == ("EXACT_COMPATIBILITY_MISMATCH",)
        ),
        "backend_remeasures_instead_of_trusting_declared_compatibility": (
            backend_refusal is not None
            and "differs from the measured local executable" in backend_refusal
        ),
        "coordinator_rejects_every_exact_identity_mismatch_before_dispatch": all(
            refusal is not None
            and "backend compatibility differs from logical work" in refusal
            for refusal in coordinator_refusals
        ),
    }
    return _case(
        "worker_compatibility_is_exact_and_forgery_is_refused",
        f"compatibility={compatibility.compatibility_sha256}",
        checks,
        {
            "backend_refusal": backend_refusal,
            "compatibility": compatibility.as_dict(),
            "coordinator_refusals": list(coordinator_refusals),
            "worker_refusal": worker_refusal.as_dict(),
        },
    )


def _execute_backend_matrix(
    plan: ExperimentWorkPlanV1,
    compatibility: WorkerCompatibilityV1,
) -> _BackendExecutionMatrixV1:
    coordinator = OrchestrationCoordinatorV1()
    return _BackendExecutionMatrixV1(
        single=coordinator.execute(
            plan,
            SingleProcessBackendV1(compatibility=compatibility),
        ),
        local_one=coordinator.execute(
            plan,
            LocalSubprocessBackendV1(
                worker_count=1,
                compatibility=compatibility,
            ),
        ),
        local_three=coordinator.execute(
            plan,
            LocalSubprocessBackendV1(
                worker_count=3,
                compatibility=compatibility,
            ),
        ),
        reversed_completion=coordinator.execute(
            plan,
            _ReversedCompletionBackendV1(compatibility=compatibility),
        ),
    )


def _backend_parity_case(
    plan: ExperimentWorkPlanV1,
    matrix: _BackendExecutionMatrixV1,
) -> OrchestrationAuditCase:
    results = (
        matrix.single,
        matrix.local_one,
        matrix.local_three,
        matrix.reversed_completion,
    )
    baseline = matrix.single
    artifact_sets = tuple(
        tuple(
            (
                verified.logical_work_unit_id,
                tuple(
                    (artifact.artifact_id, artifact.sha256)
                    for artifact in verified.artifacts
                ),
            )
            for verified in result.verified_results
        )
        for result in results
    )
    checks = {
        "all_backends_complete_every_logical_unit": all(
            len(result.verified_results) == len(plan.logical_units)
            for result in results
        ),
        "single_and_local_worker_counts_have_identical_scientific_results": all(
            result.verified_results == baseline.verified_results
            for result in results[1:]
        ),
        "aggregate_digest_excludes_backend_and_completion_order": all(
            result.aggregate_sha256 == baseline.aggregate_sha256
            for result in results
        ),
        "artifact_bytes_and_digests_match_across_backends": all(
            artifact_set == artifact_sets[0]
            for artifact_set in artifact_sets[1:]
        ),
        "results_are_always_in_canonical_logical_id_order": all(
            tuple(item.logical_work_unit_id for item in result.verified_results)
            == tuple(
                sorted(
                    item.logical_work_unit_id
                    for item in result.verified_results
                )
            )
            for result in results
        ),
        "coordinator_run_records_round_trip_exactly": all(
            CoordinatorRunResultV1.from_dict(result.as_dict()) == result
            for result in results
        ),
    }
    return _case(
        "single_and_local_backends_match_across_worker_counts_and_order",
        (
            f"plan={plan.plan_id} aggregate={baseline.aggregate_sha256} "
            f"units={len(plan.logical_units)}"
        ),
        checks,
        {
            "aggregate_sha256": baseline.aggregate_sha256,
            "backend_ids": [item.backend_id for item in results],
            "logical_work_unit_ids": [
                item.logical_work_unit_id for item in baseline.verified_results
            ],
        },
    )


def _coordinator_independent_verification_case(
    plan: ExperimentWorkPlanV1,
    compatibility: WorkerCompatibilityV1,
    accepted: CoordinatorRunResultV1,
) -> OrchestrationAuditCase:
    forgery_refusal = _capture_exception_text(
        lambda: OrchestrationCoordinatorV1().execute(
            plan,
            _SelfConsistentForgeryBackendV1(compatibility=compatibility),
        )
    )
    expected_artifact_names = tuple(
        item.name for item in complete_run_expected_output_identities()
    )
    expected_audits = complete_run_runtime_audit_identities()
    checks = {
        "accepted_artifact_payloads_match_their_coordinator_digests": all(
            hashlib.sha256(artifact.payload_bytes).hexdigest()
            == artifact.sha256
            for result in accepted.verified_results
            for artifact in result.artifacts
        ),
        "accepted_results_have_exact_outputs_and_passed_required_audits": all(
            tuple(item.artifact_id for item in result.artifacts)
            == expected_artifact_names
            and tuple(item.audit_identity for item in result.runtime_audit_results)
            == expected_audits
            and all(
                item.status is RuntimeAuditStatusV1.PASSED
                for item in result.runtime_audit_results
            )
            for result in accepted.verified_results
        ),
        "verified_results_round_trip_exactly": all(
            VerifiedWorkResultV1.from_dict(result.as_dict()) == result
            for result in accepted.verified_results
        ),
        "self_consistent_forged_manifest_and_bytes_fail_independent_replay": (
            forgery_refusal is not None
            and "worker bytes differ from independent coordinator replay"
            in forgery_refusal
        ),
    }
    return _case(
        "coordinator_replays_and_refuses_self_consistent_forged_bytes",
        f"verified={len(accepted.verified_results)} forgery=REFUSED",
        checks,
        {
            "aggregate_sha256": accepted.aggregate_sha256,
            "forgery_refusal": forgery_refusal,
            "verified_result_sha256s": [
                item.scientific_result_sha256
                for item in accepted.verified_results
            ],
        },
    )


def _worker_authority_boundary_case(
    plan: ExperimentWorkPlanV1,
) -> OrchestrationAuditCase:
    unit = plan.logical_units[0]
    required_audits = complete_run_runtime_audit_identities()
    unsupported_cell = replace(
        unit.cell,
        work_kind=WorkKindV1.COUNTERFACTUAL_BRANCH,
        configuration={"branch_id": "worker-cannot-select-this-adapter"},
    )
    unsupported = execute_work_request(
        WorkRequestV1(
            logical_work_unit=replace(unit, cell=unsupported_cell),
            required_runtime_audits=required_audits,
        )
    )
    weakened_audits = execute_work_request(
        WorkRequestV1(
            logical_work_unit=unit,
            required_runtime_audits=(required_audits[0],),
        )
    )
    changed_outputs = execute_work_request(
        WorkRequestV1(
            logical_work_unit=replace(
                unit,
                expected_outputs=(_reference("output.worker-selected"),),
            ),
            required_runtime_audits=required_audits,
        )
    )
    refusals = (unsupported, weakened_audits, changed_outputs)
    checks = {
        "worker_supports_only_the_statically_declared_complete_run_adapter": (
            unsupported.status is WorkerResultStatusV1.WORK_KIND_REFUSED
            and tuple(item.code for item in unsupported.diagnostics)
            == ("UNSUPPORTED_WORK_KIND",)
        ),
        "worker_cannot_drop_a_required_runtime_audit": (
            weakened_audits.status is WorkerResultStatusV1.EXECUTION_FAILED
            and tuple(item.code for item in weakened_audits.diagnostics)
            == ("RUNTIME_AUDIT_CONTRACT_MISMATCH",)
        ),
        "worker_cannot_substitute_its_own_output_contract": (
            changed_outputs.status is WorkerResultStatusV1.EXECUTION_FAILED
            and tuple(item.code for item in changed_outputs.diagnostics)
            == ("EXPECTED_OUTPUT_CONTRACT_MISMATCH",)
        ),
        "pre_execution_refusals_return_no_partial_artifacts_or_audit_claims": all(
            item.manifest is None
            and item.artifacts == ()
            and item.runtime_audit_results == ()
            for item in refusals
        ),
        "local_process_argv_target_is_fixed_not_request_controlled": (
            LOCAL_WORKER_MODULE_V1 == "kirby2.orchestration.worker"
        ),
    }
    return _case(
        "worker_cannot_select_work_or_weaken_runtime_contracts",
        "unsupported_work=REFUSED weakened_audit=REFUSED changed_output=REFUSED",
        checks,
        {
            "changed_output": changed_outputs.as_dict(),
            "unsupported_work": unsupported.as_dict(),
            "weakened_audits": weakened_audits.as_dict(),
        },
    )


@dataclass(frozen=True, slots=True)
class _ReversedCompletionBackendV1:
    compatibility: WorkerCompatibilityV1

    @property
    def backend_id(self) -> str:
        return "reversed-completion-audit-v1"

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]:
        results = SingleProcessBackendV1(
            compatibility=self.compatibility
        ).execute_many(requests)
        return tuple(reversed(results))


@dataclass(frozen=True, slots=True)
class _SelfConsistentForgeryBackendV1:
    compatibility: WorkerCompatibilityV1

    @property
    def backend_id(self) -> str:
        return "self-consistent-forgery-audit-v1"

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]:
        results = SingleProcessBackendV1(
            compatibility=self.compatibility
        ).execute_many(requests)
        target = results[0]
        forged_metrics = InlineArtifactV1.from_json_object(
            "metrics.json",
            {
                "forged": True,
                "logical_work_unit_id": (
                    target.request.logical_work_unit.logical_work_unit_id
                ),
            },
        )
        artifacts = tuple(
            sorted(
                (
                    forged_metrics,
                    *(
                        item
                        for item in target.artifacts
                        if item.artifact_id != "metrics.json"
                    ),
                ),
                key=lambda item: item.sort_key,
            )
        )
        manifest = WorkerResultManifestV1.for_success(
            request=target.request,
            worker_compatibility=target.worker_compatibility,
            artifacts=artifacts,
            runtime_audit_results=target.runtime_audit_results,
        )
        forged = WorkerResultV1(
            request=target.request,
            worker_compatibility=target.worker_compatibility,
            status=WorkerResultStatusV1.SUCCEEDED,
            manifest=manifest,
            artifacts=artifacts,
            runtime_audit_results=target.runtime_audit_results,
            diagnostics=(),
        )
        return (forged, *results[1:])


def _local_execution_plan(
    compatibility: WorkerCompatibilityV1,
) -> ExperimentWorkPlanV1:
    configuration = {
        "duration_seconds": 1,
        "liquidity": "NORMAL",
        "relative_volume": "1.00x",
        "scenario_name": "balanced",
    }
    cells = tuple(
        LogicalWorkCellV1(
            partition_id="local-audit",
            cell_id=f"run-{index:04d}",
            work_kind=WorkKindV1.COMPLETE_RUN,
            configuration=configuration,
        )
        for index in range(1, 3)
    )
    return build_experiment_work_plan(
        master_seed_identity=build_master_seed_identity(38_002),
        experiment_identity=_reference("experiment.wo38b"),
        cells=cells,
        scenario=_reference("scenario.balanced"),
        market_profile=_reference("market-profile.normal"),
        datasets=(),
        strategies=(),
        packs=(),
        software_version="0.1.0",
        source_version=compatibility.engine_identity.sha256,
        engine_identity=compatibility.engine_identity,
        runtime_identity=compatibility.runtime_identity,
        dependency_identity=compatibility.dependency_identity,
        compiler_identity=compatibility.compiler_identity,
        schemas=compatibility.schemas,
        capabilities=compatibility.capabilities,
        expected_outputs=complete_run_expected_output_identities(),
        resource_class="cpu-small",
    )


def _request_with_configuration(
    unit: LogicalWorkUnit,
    required_audits: tuple[DigestReferenceV1, ...],
    configuration: dict[str, object],
) -> WorkRequestV1:
    cell = replace(unit.cell, configuration=configuration)
    return WorkRequestV1(
        logical_work_unit=replace(unit, cell=cell),
        required_runtime_audits=required_audits,
    )


def _stdio_refusal(raw: str) -> str | None:
    return _capture_exception_text(
        lambda: run_data_only_stdio_worker(
            lambda payload: payload,
            input_stream=StringIO(raw),
            output_stream=StringIO(),
        )
    )


def _fixture_cells() -> tuple[LogicalWorkCellV1, ...]:
    definitions = (
        (
            "partition-b",
            "strategy-evaluation",
            WorkKindV1.STRATEGY_EVALUATION,
            {"strategy_id": "maker-v1", "trial_count": 16},
        ),
        (
            "partition-a",
            "complete-run",
            WorkKindV1.COMPLETE_RUN,
            {"duration_seconds": 3600, "scenario_id": "flow-v1"},
        ),
        (
            "partition-b",
            "calibration",
            WorkKindV1.CALIBRATION,
            {"candidate_count": 8, "objective_id": "spread-fit-v1"},
        ),
        (
            "partition-a",
            "counterfactual",
            WorkKindV1.COUNTERFACTUAL_BRANCH,
            {"branch_id": "latency-plus-one", "latency_ticks": 1},
        ),
    )
    return tuple(
        LogicalWorkCellV1(
            partition_id=partition_id,
            cell_id=cell_id,
            work_kind=work_kind,
            configuration=configuration,
        )
        for partition_id, cell_id, work_kind, configuration in definitions
    )


def _fixture_plan(
    cells: Iterable[LogicalWorkCellV1],
) -> ExperimentWorkPlanV1:
    return build_experiment_work_plan(
        master_seed_identity=build_master_seed_identity(38_001),
        experiment_identity=_reference("experiment.wo38a"),
        cells=cells,
        scenario=_reference("scenario.primary"),
        market_profile=_reference("market-profile.synthetic"),
        datasets=_references("dataset.events", "dataset.reference"),
        strategies=_references("strategy.maker", "strategy.taker"),
        packs=_references("pack.scenario", "pack.strategy"),
        software_version="0.1.0",
        source_version="source-v1",
        engine_identity=_reference("engine.kirby2"),
        runtime_identity=_reference("runtime.python"),
        dependency_identity=_reference("dependencies.lock"),
        compiler_identity=_reference("compiler.scenario"),
        schemas=_references("schema.events", "schema.metrics"),
        capabilities=_references("capability.deterministic-simulation"),
        expected_outputs=_references("output.events", "output.metrics"),
        resource_class="cpu-standard-v1",
    )


def _attempt(
    unit: LogicalWorkUnit,
    attempt_number: int,
    worker_id: str,
    *,
    artifact: str | None = None,
    outcome: WorkAttemptOutcomeV1 = WorkAttemptOutcomeV1.SUCCEEDED,
) -> WorkAttempt:
    return WorkAttempt(
        logical_work_unit_id=unit.logical_work_unit_id,
        attempt_number=attempt_number,
        worker_id=worker_id,
        lease_id=f"lease-{attempt_number}",
        lease_issued_at_utc="2026-01-01T00:00:00Z",
        lease_expires_at_utc="2026-01-01T00:10:00Z",
        heartbeat_sequence=1,
        last_heartbeat_at_utc="2026-01-01T00:01:00Z",
        outcome=outcome,
        diagnostics={"attempt_number_observed": attempt_number},
        returned_artifact_sha256=artifact,
        recorded_at_utc="2026-01-01T00:02:00Z",
    )


def _unit_for_cell(
    plan: ExperimentWorkPlanV1,
    cell: LogicalWorkCellV1,
) -> LogicalWorkUnit:
    return next(
        item
        for item in plan.logical_units
        if (item.partition_id, item.cell_id)
        == (cell.partition_id, cell.cell_id)
    )


def _reference(name: str) -> DigestReferenceV1:
    return DigestReferenceV1(name=name, sha256=_digest(f"WO38-A {name}"))


def _references(*names: str) -> tuple[DigestReferenceV1, ...]:
    return tuple(
        sorted(
            (_reference(name) for name in names),
            key=lambda item: item.sort_key,
        )
    )


def _capture_exception_text(operation) -> str | None:
    try:
        operation()
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
        return str(error)
    return None


def _capture_compatibility_refusal_code(operation) -> str | None:
    try:
        operation()
    except OrchestrationCompatibilityRefused as error:
        return error.code.value
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
        return f"UNEXPECTED:{type(error).__name__}:{error}"
    return None


def _capture_content_store_refusal_code(operation) -> str | None:
    try:
        operation()
    except ContentStoreRefused as error:
        return error.refusal.code.value
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
        return f"UNEXPECTED:{type(error).__name__}:{error}"
    return None


def _capture_security_refusal_code(operation) -> str | None:
    try:
        operation()
    except SecurityRefused as error:
        return error.code.value
    except (
        KeyError,
        LookupError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        return f"UNEXPECTED:{type(error).__name__}:{error}"
    return None


def _capture_lease_refusal_code(operation) -> str | None:
    try:
        operation()
    except LeaseRefused as error:
        return error.code.value
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
        return f"UNEXPECTED:{type(error).__name__}:{error}"
    return None


def _capture_recovery_refusal_code(operation) -> str | None:
    try:
        operation()
    except RecoveryRefused as error:
        return error.code
    except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
        return f"UNEXPECTED:{type(error).__name__}:{error}"
    return None


def _lan_audit_resource_limits() -> ResourceLimitsV1:
    return ResourceLimitsV1(
        maximum_concurrent_runs=1,
        maximum_queue_depth=0,
        maximum_memory_bytes_per_run=16 * 1024 * 1024 * 1024,
        maximum_disk_bytes_per_run=2 * 1024 * 1024 * 1024,
        maximum_elapsed_seconds_per_run=60,
        maximum_message_bytes=8 * 1024 * 1024,
        maximum_stream_bytes=64 * 1024 * 1024,
    )


def _lan_fixture_configurations(
    port: int,
) -> tuple[LanTlsConfigurationV1, LanTlsConfigurationV1]:
    coordinator_identity = "coordinator.test.kirby2.invalid"
    worker_identity = "worker.test.kirby2.invalid"
    ca_certificate = TEST_PKI_ROOT_V1 / "ca.cert.pem"
    coordinator_certificate = TEST_PKI_ROOT_V1 / "coordinator.cert.pem"
    coordinator = LanTlsConfigurationV1(
        role=LanPeerRoleV1.COORDINATOR,
        enabled=True,
        host=DEFAULT_LAN_BIND_HOST_V1,
        port=port,
        ca_certificate=ca_certificate,
        certificate=coordinator_certificate,
        private_key=TEST_PKI_ROOT_V1 / "coordinator.key.pem",
        local_identity=coordinator_identity,
        expected_peer_identities=(worker_identity,),
        credential_use=CredentialUseV1.AUDIT_LOOPBACK_FIXTURE,
    )
    worker = LanTlsConfigurationV1(
        role=LanPeerRoleV1.WORKER,
        enabled=True,
        host=DEFAULT_LAN_BIND_HOST_V1,
        port=port,
        ca_certificate=ca_certificate,
        certificate=TEST_PKI_ROOT_V1 / "worker.cert.pem",
        private_key=TEST_PKI_ROOT_V1 / "worker.key.pem",
        local_identity=worker_identity,
        expected_peer_identities=(coordinator_identity,),
        credential_use=CredentialUseV1.AUDIT_LOOPBACK_FIXTURE,
        server_hostname=coordinator_identity,
        pinned_coordinator_certificate_sha256=certificate_sha256(
            coordinator_certificate
        ),
    )
    return coordinator, worker


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((DEFAULT_LAN_BIND_HOST_V1, 0))
        port = listener.getsockname()[1]
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("loopback port allocation returned an invalid port")
    return port


def _run_lan_worker_with_retry(
    worker: LanWorkerServiceV1,
) -> tuple[WorkerResultV1, ...]:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return worker.run()
        except ConnectionRefusedError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _lan_attempt_temporary_roots() -> frozenset[Path]:
    temporary_root = Path(tempfile.gettempdir())
    return frozenset(
        path.resolve()
        for path in temporary_root.glob("kirby2-lan-attempt-*")
        if path.is_dir()
    )


def _coordinator_state_inventory() -> tuple[CoordinatorWorkStateRecordV1, ...]:
    records: list[CoordinatorWorkStateRecordV1] = []
    leased_states = {
        CoordinatorWorkStateV1.LEASED,
        CoordinatorWorkStateV1.COMPLETED_UNVERIFIED,
        CoordinatorWorkStateV1.REGISTERED,
        CoordinatorWorkStateV1.QUARANTINED,
    }
    returned_states = {
        CoordinatorWorkStateV1.COMPLETED_UNVERIFIED,
        CoordinatorWorkStateV1.REGISTERED,
        CoordinatorWorkStateV1.QUARANTINED,
    }
    failure_states = {
        CoordinatorWorkStateV1.FAILED,
        CoordinatorWorkStateV1.CANCELLED,
        CoordinatorWorkStateV1.QUARANTINED,
    }
    for index, state in enumerate(CoordinatorWorkStateV1, start=1):
        leased = state in leased_states
        records.append(
            CoordinatorWorkStateRecordV1(
                work_request_id=_digest(
                    f"WO38-D state work request {index}"
                ),
                logical_work_unit_id=_digest(
                    f"WO38-D state logical work {index}"
                ),
                state=state,
                attempt_id=(
                    _digest(f"WO38-D state attempt {index}")
                    if leased
                    else None
                ),
                worker_id=(f"worker.wo38d.state-{index}" if leased else None),
                lease_id=(
                    _digest(f"WO38-D state lease {index}")
                    if leased
                    else None
                ),
                returned_result_sha256=(
                    _digest(f"WO38-D state result {index}")
                    if state in returned_states
                    else None
                ),
                registered_manifest_sha256=(
                    _digest(f"WO38-D state manifest {index}")
                    if state is CoordinatorWorkStateV1.REGISTERED
                    else None
                ),
                failure_code=(
                    f"{state.value}_AUDIT"
                    if state in failure_states
                    else None
                ),
            )
        )
    return tuple(sorted(records, key=lambda item: item.sort_key))


def _resource_claim(
    experiment_id: str,
    label: str,
    resource_class: str,
) -> ResourceClaimV1:
    return ResourceClaimV1(
        experiment_id=experiment_id,
        work_request_id=_digest(f"WO38-D resource claim {label}"),
        resource_class=resource_class,
        memory_bytes=512,
        disk_bytes=1024,
        elapsed_seconds=30,
    )


def _conflicting_verified_result(
    original: VerifiedWorkResultV1,
) -> VerifiedWorkResultV1:
    metrics = tuple(
        item for item in original.artifacts if item.artifact_id == "metrics.json"
    )
    if len(metrics) != 1:
        raise RuntimeError("WO38-E conflict fixture requires one metrics artifact")
    changed_metrics = InlineArtifactV1.from_json_object(
        "metrics.json",
        {
            "baseline_metrics_sha256": metrics[0].sha256,
            "scientific_conflict": "WO38-E_AUDIT_CONFLICT",
        },
    )
    artifacts = tuple(
        sorted(
            (
                changed_metrics,
                *(
                    item
                    for item in original.artifacts
                    if item.artifact_id != "metrics.json"
                ),
            ),
            key=lambda item: item.sort_key,
        )
    )
    manifest = WorkerResultManifestV1(
        work_request_id=original.work_request_id,
        logical_work_unit_id=original.logical_work_unit_id,
        worker_compatibility_sha256=original.worker_compatibility_sha256,
        artifacts=tuple(item.digest_reference for item in artifacts),
        runtime_audit_results=tuple(
            item.result_reference for item in original.runtime_audit_results
        ),
    )
    return VerifiedWorkResultV1(
        work_request_id=original.work_request_id,
        logical_work_unit_id=original.logical_work_unit_id,
        worker_compatibility_sha256=original.worker_compatibility_sha256,
        worker_result_manifest_sha256=manifest.manifest_sha256,
        artifacts=artifacts,
        runtime_audit_results=original.runtime_audit_results,
    )


def _recovery_record(
    checkpoint: RecoveryCheckpointV1,
    logical_work_unit_id: str,
) -> RecoveryWorkRecordV1:
    records = tuple(
        item
        for item in checkpoint.records
        if item.logical_work_unit_id == logical_work_unit_id
    )
    if len(records) != 1:
        raise RuntimeError("WO38-E checkpoint lost its exact recovery record")
    return records[0]


def _contains_binary_float(value: object) -> bool:
    if type(value) is float:
        return True
    if type(value) is dict:
        return any(_contains_binary_float(item) for item in value.values())
    if type(value) in {list, tuple}:
        return any(_contains_binary_float(item) for item in value)
    return False


def _attempt_object_root(
    paths: DataPaths,
    attempt: ResultAttemptStageV1,
) -> Path:
    return paths.staging.joinpath(
        "orchestration-content-v1",
        "results",
        "attempts",
        attempt.stage_key_sha256,
        "objects",
    )


def _attempt_stage_leaves(paths: DataPaths) -> tuple[str, ...]:
    attempts = paths.staging.joinpath(
        "orchestration-content-v1",
        "results",
        "attempts",
    )
    if not attempts.exists():
        return ()
    return tuple(
        sorted(path.name for path in attempts.iterdir() if path.is_dir())
    )


def _file_names(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(path.name for path in root.iterdir() if path.is_file()))


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _case(
    name: str,
    detail: str,
    checks: dict[str, bool],
    evidence: dict[str, object],
) -> OrchestrationAuditCase:
    return OrchestrationAuditCase(
        name=name,
        detail=detail,
        evidence=evidence,
        failures=tuple(label for label, passed in checks.items() if not passed),
    )


__all__ = [
    "WO38A_AUDIT_CASE_COUNT",
    "WO38B_AUDIT_CASE_COUNT",
    "WO38C_ORCHESTRATION_AUDIT_CASE_COUNT",
    "WO38D_AUDIT_CASE_COUNT",
    "WO38E_AUDIT_CASE_COUNT",
    "OrchestrationAuditCase",
    "audit_authenticated_lan_orchestration",
    "audit_distributed_recovery",
    "audit_local_orchestration",
    "audit_logical_work_and_attempt_identity",
    "audit_verified_content_exchange",
]
