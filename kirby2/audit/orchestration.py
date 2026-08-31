"""Executable audits for deterministic orchestration identity and planning."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Iterable
from dataclasses import dataclass, replace

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
from kirby2.orchestration.seeds import (
    SeedDerivationV1,
    StableCellIdentityV1,
    build_master_seed_identity,
    derive_logical_cell_seed_batch,
)


WO38A_AUDIT_CASE_COUNT = 5


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
    "OrchestrationAuditCase",
    "audit_logical_work_and_attempt_identity",
]
