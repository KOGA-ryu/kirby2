"""Executable audits for deterministic orchestration identity and planning."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable
from dataclasses import dataclass, replace
from io import StringIO

from kirby2.orchestration.coordinator import (
    CoordinatorRunResultV1,
    OrchestrationCoordinatorV1,
    VerifiedWorkResultV1,
)
from kirby2.orchestration.local import (
    LOCAL_WORKER_MODULE_V1,
    LocalSubprocessBackendV1,
    SingleProcessBackendV1,
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
    RuntimeAuditStatusV1,
    WorkRequestV1,
    WorkerCompatibilityV1,
    WorkerResultManifestV1,
    WorkerResultStatusV1,
    WorkerResultV1,
)
from kirby2.orchestration.seeds import (
    SeedDerivationV1,
    StableCellIdentityV1,
    build_master_seed_identity,
    derive_logical_cell_seed_batch,
)
from kirby2.orchestration.worker import (
    complete_run_expected_output_identities,
    complete_run_runtime_audit_identities,
    execute_work_request,
    measure_local_worker_compatibility,
    run_data_only_stdio_worker,
)


WO38A_AUDIT_CASE_COUNT = 5
WO38B_AUDIT_CASE_COUNT = 5


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
    "OrchestrationAuditCase",
    "audit_local_orchestration",
    "audit_logical_work_and_attempt_identity",
]
