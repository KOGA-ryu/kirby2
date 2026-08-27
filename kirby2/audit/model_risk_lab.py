"""Runtime acceptance audit for the generative model-risk laboratory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.agents.models import PublicEcologyEvent
from kirby2.auditlab import AuditLabStore, FaultKind, run_audit_lab
from kirby2.auditlab.executors import (
    CAPABILITY_MATRIX,
    EXECUTOR_REGISTRY,
    ExecutorRegistry,
)
from kirby2.auditlab.generator import (
    AXES,
    evidence_coverage_report,
    generate_configurations,
)
from kirby2.auditlab.models import (
    AUDIT_LAB_SCHEMA_VERSION,
    AUDIT_PACKET_SCHEMA_VERSION,
    LEGACY_AUDIT_PACKET_SCHEMA_VERSION,
    AcceptanceRecord,
    AutomatedStatus,
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExecutorLane,
    ExperimentPartition,
    ExerciseRecord,
    ExerciseStatus,
    FailureKind,
    FailureObservation,
    FaultEvidence,
    GeneratedCaseResult,
    GeneratedConfiguration,
    KernelResult,
    StatisticalCheck,
    canonical_json,
    canonical_sha256,
)
from kirby2.counterfactual.models import (
    ActionMutation,
    BranchSnapshot,
    ComponentStatus,
    CounterfactualMode,
    CounterfactualOutcome,
    CounterfactualReport,
    CounterfactualTimelineEntry,
    FirstDivergence,
    MutationManifest,
    SnapshotComponent,
    TimingSweepCell,
)
from kirby2.exchange import Order, OrderBook, OrderOwner, OrderStatus, Side
from kirby2.exchange.mechanics_models import MechanicsEvent, MechanicsEventType
from kirby2.immutable import freeze_json, thaw_json
from kirby2.latency.models import LatencyEvent, LatencyEventType
from kirby2.multivenue.models import CoordinatorEvent, CoordinatorEventType
from kirby2.observability.models import (
    ObservableEvent,
    ObservableEventType,
    TruthEvent,
    TruthEventType,
)
from kirby2.observability.venue import _PendingObservable
from kirby2.session.events import EventJournal, EventType
from kirby2.session.records import (
    InputRecord,
    MarketStateRecord,
    TimelineKind,
    TimelineRecord,
)
from kirby2.simulation.flow import FlowEvent, FlowEventFamily


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
            _truthful_execution_contract_case(),
            _structural_case(result),
            _fault_case(result),
            _determinism_case(result),
            _minimization_case(result),
            _statistics_case(result),
            _probe_case(result),
            _core_replay_payload_ownership_case(),
            _subsystem_evidence_payload_ownership_case(),
            _exchange_state_ownership_case(),
            _artifact_path_boundary_case(root / "artifact-path-boundary"),
            _packet_identity_scope_case(
                root / "packet-identity-scope",
                packet,
            ),
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


def _truthful_execution_contract_case() -> ModelRiskLabAuditCase:
    schedule = generate_configurations(seed=771, budget=420)
    repeated_schedule = generate_configurations(seed=771, budget=420)
    axis_schedule = generate_configurations(seed=771, budget=4_200)
    schedule_wire = canonical_json([item.as_dict() for item in schedule])
    repeated_wire = canonical_json(
        [item.as_dict() for item in repeated_schedule]
    )

    scientific_cells: dict[
        tuple[ExecutorLane, str], list[GeneratedConfiguration]
    ] = {}
    fault_cells: dict[str, list[GeneratedConfiguration]] = {}
    for configuration in schedule:
        if configuration.lane is ExecutorLane.FAULT:
            fault_cells.setdefault(configuration.cell_id, []).append(configuration)
        else:
            scientific_cells.setdefault(
                (configuration.lane, configuration.cell_id), []
            ).append(configuration)

    nonseed_fields = {
        "partition",
        "replicate_index",
        "seed",
        "sequence",
    }

    def nonseed_wire(configuration: GeneratedConfiguration) -> str:
        payload = configuration.as_dict()
        for name in nonseed_fields:
            payload.pop(name)
        return canonical_json(payload)

    complete_scientific_cells = sum(
        all(
            (
                len(configurations) == 6,
                {item.replicate_index for item in configurations} == set(range(6)),
                {
                    item.replicate_index
                    for item in configurations
                    if item.partition is ExperimentPartition.TRAIN
                }
                == {0, 1, 2},
                {
                    item.replicate_index
                    for item in configurations
                    if item.partition is ExperimentPartition.HOLDOUT
                }
                == {3, 4, 5},
                len({nonseed_wire(item) for item in configurations}) == 1,
            )
        )
        for configurations in scientific_cells.values()
    )
    complete_fault_cells = sum(
        all(
            (
                len(configurations) == len(FaultKind),
                {item.replicate_index for item in configurations}
                == set(range(len(FaultKind))),
                {item.partition for item in configurations}
                == {ExperimentPartition.FAULT},
                {item.injected_fault for item in configurations} == set(FaultKind),
            )
        )
        for configurations in fault_cells.values()
    )
    train_seeds = {
        item.seed
        for item in schedule
        if item.partition is ExperimentPartition.TRAIN
    }
    holdout_seeds = {
        item.seed
        for item in schedule
        if item.partition is ExperimentPartition.HOLDOUT
    }

    lane_axis_coverage: dict[str, dict[str, object]] = {}
    lane_axes_pass = True
    for lane, lane_capability in CAPABILITY_MATRIX.items():
        if lane is ExecutorLane.FAULT:
            continue
        lane_configurations = tuple(
            item
            for item in axis_schedule
            if item.lane is lane and item.replicate_index == 0
        )
        dimensions: dict[str, object] = {}
        for dimension in lane_capability.credited_dimensions:
            observed = {getattr(item, dimension) for item in lane_configurations}
            if dimension in AXES:
                expected = set(AXES[dimension])
                passed_dimension = observed == expected
                expected_rendered: object = sorted(
                    str(value) for value in expected
                )
                observed_rendered: object = sorted(
                    str(value) for value in observed
                )
            elif dimension == "agent_count":
                expected = set(range(1, 9))
                passed_dimension = observed == expected
                expected_rendered = sorted(expected)
                observed_rendered = sorted(observed)
            elif dimension == "seed":
                passed_dimension = len(observed) == len(lane_configurations)
                expected_rendered = "one unique seed per representative cell"
                observed_rendered = {
                    "count": len(observed),
                    "maximum": max(observed),
                    "minimum": min(observed),
                }
            elif dimension == "duration_us":
                passed_dimension = len(observed) > 1
                expected_rendered = "more than one simulation duration"
                observed_rendered = sorted(observed)
            else:
                passed_dimension = False
                expected_rendered = "declared audit expectation"
                observed_rendered = sorted(str(value) for value in observed)
            lane_axes_pass = lane_axes_pass and passed_dimension
            dimensions[dimension] = {
                "expected": expected_rendered,
                "observed": observed_rendered,
                "status": "PASS" if passed_dimension else "PARTIAL",
            }
        lane_axis_coverage[lane.value] = dimensions

    round_trip = all(
        GeneratedConfiguration.from_dict(item.as_dict()) == item
        for item in schedule
    )
    schema_refusals: dict[str, bool] = {}
    schema_mutations: dict[str, dict[str, object]] = {}
    missing_field = schedule[0].as_dict()
    missing_field.pop("lane")
    schema_mutations["missing_field"] = missing_field
    unknown_field = schedule[0].as_dict()
    unknown_field["undeclared"] = "forbidden"
    schema_mutations["unknown_field"] = unknown_field
    old_schema = schedule[0].as_dict()
    old_schema["schema_version"] = AUDIT_LAB_SCHEMA_VERSION - 1
    schema_mutations["old_schema"] = old_schema
    noninteger_schema = schedule[0].as_dict()
    noninteger_schema["schema_version"] = float(AUDIT_LAB_SCHEMA_VERSION)
    schema_mutations["noninteger_schema"] = noninteger_schema
    unknown_lane = schedule[0].as_dict()
    unknown_lane["lane"] = "PLACEHOLDER"
    schema_mutations["unknown_lane"] = unknown_lane
    for name, payload in schema_mutations.items():
        try:
            GeneratedConfiguration.from_dict(payload)
        except (TypeError, ValueError):
            schema_refusals[name] = True
        else:
            schema_refusals[name] = False

    core_configuration = next(
        item for item in schedule if item.lane is ExecutorLane.CORE_FLOW
    )
    capability = CAPABILITY_MATRIX[ExecutorLane.CORE_FLOW]
    expected_core_dimensions = (
        "seed",
        "duration_us",
        "flow_model",
        "regime",
        "volume",
        "liquidity",
    )
    refusal_checks = tuple(
        CheckResult(
            name=name,
            status=CheckStatus.NOT_EXERCISED,
            required=True,
            detail="no real executor reported this required check",
            evidence={"source": "truthful-contract-runtime-audit"},
        )
        for name in capability.required_checks
    )
    unreported = GeneratedCaseResult(
        configuration=core_configuration,
        lane=core_configuration.lane,
        recording=CaseRecording(
            lane=core_configuration.lane,
            recording_type="CONTRACT_PROBE",
            payload={"configuration_sha256": core_configuration.sha256},
        ),
        event_projection=(),
        final_state_projection={"status": "NOT_EXERCISED"},
        metrics={},
        exercises=(),
        checks=refusal_checks,
        failures=(),
        observable_projection={"status": "NOT_EXERCISED"},
    )
    unreported_coverage = evidence_coverage_report((unreported,))
    unreported_core = unreported_coverage["lanes"][ExecutorLane.CORE_FLOW.value]
    no_declared_dimension_credit = all(
        not item["exercised_values"] and item["status"] == "PARTIAL"
        for item in unreported_core["dimensions"].values()
    )
    no_unexercised_check_credit = all(
        item["exercise_count"] == 0 and item["status"] == "PARTIAL"
        for item in unreported_core["checks"].values()
    )

    evidence_source = {
        "source": "truthful-contract-runtime-audit",
        "detail": {"event_count": 0},
    }
    exercised_flow = ExerciseRecord(
        lane=core_configuration.lane,
        capability="flow_model",
        configured_value=core_configuration.flow_model,
        status=ExerciseStatus.EXERCISED,
        evidence=evidence_source,
    )
    exercise_wire = canonical_json(exercised_flow.as_dict())
    evidence_source["detail"]["event_count"] = 999
    caller_mutation_preserved = (
        canonical_json(exercised_flow.as_dict()) == exercise_wire
    )
    direct_mutation_rejected = False
    try:
        exercised_flow.evidence["detail"]["event_count"] = 999  # type: ignore[index]
    except TypeError:
        direct_mutation_rejected = True
    exercise_export = exercised_flow.as_dict()
    exercise_export["evidence"]["detail"]["event_count"] = 777  # type: ignore[index]
    export_mutation_preserved = (
        canonical_json(exercised_flow.as_dict()) == exercise_wire
    )

    reported = replace(unreported, exercises=(exercised_flow,))
    reported_coverage = evidence_coverage_report((reported,))
    reported_dimensions = reported_coverage["lanes"][
        ExecutorLane.CORE_FLOW.value
    ]["dimensions"]
    only_reported_dimension_credited = all(
        item["status"] == ("PASS" if name == "flow_model" else "PARTIAL")
        for name, item in reported_dimensions.items()
    )

    boolean_status_refusals: dict[str, bool] = {}
    for name, constructor in {
        "check": lambda: CheckResult(
            name="boolean_status",
            status=True,  # type: ignore[arg-type]
            required=True,
            detail="Boolean status must be rejected",
            evidence={"source": "truthful-contract-runtime-audit"},
        ),
        "exercise": lambda: ExerciseRecord(
            lane=ExecutorLane.CORE_FLOW,
            capability="boolean_status",
            configured_value="simple",
            status=True,  # type: ignore[arg-type]
            evidence={"source": "truthful-contract-runtime-audit"},
        ),
    }.items():
        try:
            constructor()
        except TypeError:
            boolean_status_refusals[name] = True
        else:
            boolean_status_refusals[name] = False

    failed_check_result = replace(
        reported,
        checks=(
            CheckResult(
                name="explicit_failure",
                status=CheckStatus.FAIL,
                required=False,
                detail="runtime contract failure probe",
                evidence={"source": "truthful-contract-runtime-audit"},
            ),
        ),
    )
    failure_observation_result = replace(
        reported,
        checks=(),
        failures=(
            FailureObservation(
                kind=FailureKind.INVARIANT_VIOLATION,
                code="CONTRACT_PROBE",
                message="runtime contract failure probe",
                evidence={"source": "truthful-contract-runtime-audit"},
            ),
        ),
    )

    empty_registry = ExecutorRegistry()
    empty_registry_refused = False
    try:
        empty_registry.execute(core_configuration)
    except LookupError:
        empty_registry_refused = True

    passed = all(
        (
            schedule_wire == repeated_wire,
            capability.credited_dimensions == expected_core_dimensions,
            len(schedule) == 420,
            len({item.seed for item in schedule}) == len(schedule),
            len(scientific_cells) == 60,
            complete_scientific_cells == len(scientific_cells),
            len(fault_cells) == 6,
            complete_fault_cells == len(fault_cells),
            train_seeds.isdisjoint(holdout_seeds),
            lane_axes_pass,
            round_trip,
            all(schema_refusals.values()),
            len(schema_refusals) == len(schema_mutations),
            unreported.automated_status is AutomatedStatus.FAIL,
            no_declared_dimension_credit,
            no_unexercised_check_credit,
            only_reported_dimension_credited,
            all(boolean_status_refusals.values()),
            failed_check_result.automated_status is AutomatedStatus.FAIL,
            failure_observation_result.automated_status is AutomatedStatus.FAIL,
            caller_mutation_preserved,
            direct_mutation_rejected,
            export_mutation_preserved,
            empty_registry_refused,
            not EXECUTOR_REGISTRY.registered_lanes,
        )
    )
    return ModelRiskLabAuditCase(
        "truthful_typed_execution_contracts_and_independent_lane_schedule",
        {
            "boolean_status_refusals": boolean_status_refusals,
            "complete_fault_cells": complete_fault_cells,
            "complete_scientific_cells": complete_scientific_cells,
            "core_flow_credited_dimensions": list(
                capability.credited_dimensions
            ),
            "declared_without_evidence_status": unreported.automated_status.value,
            "deterministic_schedule_sha256": hashlib.sha256(
                schedule_wire.encode("utf-8")
            ).hexdigest(),
            "empty_registry_refused_execution": empty_registry_refused,
            "fault_cell_count": len(fault_cells),
            "immutable_contract_payload": {
                "caller_mutation_preserved": caller_mutation_preserved,
                "direct_mutation_rejected": direct_mutation_rejected,
                "export_mutation_preserved": export_mutation_preserved,
            },
            "lane_axis_coverage": lane_axis_coverage,
            "no_declared_dimension_credit": no_declared_dimension_credit,
            "no_unexercised_check_credit": no_unexercised_check_credit,
            "only_reported_dimension_credited": only_reported_dimension_credited,
            "registered_executor_lanes": [
                item.value for item in EXECUTOR_REGISTRY.registered_lanes
            ],
            "round_trip_schema_v2": round_trip,
            "schema_refusals": schema_refusals,
            "scientific_cell_count": len(scientific_cells),
            "train_holdout_seed_overlap": len(train_seeds.intersection(holdout_seeds)),
            "unique_seed_count": len({item.seed for item in schedule}),
        },
        () if passed else ("truthful execution contract proof failed",),
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


def _core_replay_payload_ownership_case() -> ModelRiskLabAuditCase:
    expected_payload = {
        "nested": {
            "queue": [
                {
                    "flags": [True, None],
                    "quantity": 10,
                }
            ]
        }
    }
    caller_payload = {
        "nested": {
            "queue": [
                {
                    "flags": [True, None],
                    "quantity": 10,
                }
            ]
        }
    }
    journal = EventJournal()
    event = journal.emit(EventType.ORDER_SUBMITTED, payload=caller_payload)
    input_record = InputRecord(
        sequence=1,
        simulation_time_us=10,
        input_key="B",
        resolved_command="BUY_BID",
        order_parameters={"payload": caller_payload},
        market_state_id="MS-payload-ownership",
        latency_reference_time_us=9,
        action_latency_us=1,
        accepted=True,
        rejection_reason=None,
        resulting_order_id="PLAYER-O-000001",
        resulting_order_ids=("PLAYER-O-000001",),
    )
    market_state = MarketStateRecord(
        state_id="MS-payload-ownership",
        simulation_time_us=10,
        observed_state_time_us=9,
        exchange_event_sequence=1,
        snapshot={"payload": caller_payload},
    )
    timeline_record = TimelineRecord(
        sequence=1,
        simulation_time_us=10,
        kind=TimelineKind.COMMAND,
        message="immutable payload probe",
        data={"payload": caller_payload},
    )

    def record_wire() -> str:
        return canonical_json(
            {
                "event": event.as_dict(),
                "input": input_record.as_dict(),
                "market_state": market_state.as_dict(),
                "timeline": timeline_record.as_dict(),
            }
        )

    wire_before = record_wire()
    journal_before = journal.canonical_json_lines()
    expected_journal = canonical_json(
        {
            "data": {"payload": expected_payload},
            "sequence": 1,
            "type": EventType.ORDER_SUBMITTED.value,
        }
    )

    caller_payload["nested"]["queue"][0]["quantity"] = 999
    wire_after_caller_mutation = record_wire()
    journal_after_caller_mutation = journal.canonical_json_lines()

    frozen_roots = {
        "input_record": input_record.order_parameters,
        "market_state_record": market_state.snapshot,
        "simulation_event": event.data,
        "timeline_record": timeline_record.data,
    }
    direct_mutation_rejected: dict[str, bool] = {}
    for name, root in frozen_roots.items():
        try:
            root["payload"]["nested"]["queue"][0]["quantity"] = 777  # type: ignore[index]
        except TypeError:
            direct_mutation_rejected[name] = True
        else:
            direct_mutation_rejected[name] = False

    event_export = event.as_dict()
    input_export = input_record.as_dict()
    market_export = market_state.as_dict()
    timeline_export = timeline_record.as_dict()
    event_export["data"]["payload"]["nested"]["queue"][0]["quantity"] = 501  # type: ignore[index]
    input_export["order_parameters"]["payload"]["nested"]["queue"][0]["quantity"] = 502  # type: ignore[index]
    market_export["snapshot"]["payload"]["nested"]["queue"][0]["quantity"] = 503  # type: ignore[index]
    timeline_export["data"]["payload"]["nested"]["queue"][0]["quantity"] = 504  # type: ignore[index]
    wire_after_export_mutation = record_wire()
    journal_after_export_mutation = journal.canonical_json_lines()

    invalid_json_rejected: dict[str, bool] = {}
    cyclic: list[object] = []
    cyclic.append(cyclic)
    for name, value in {
        "cyclic_sequence": cyclic,
        "non_finite_float": float("nan"),
        "non_string_key": {1: "invalid"},
        "unsupported_object": object(),
    }.items():
        try:
            freeze_json(value)
        except (TypeError, ValueError):
            invalid_json_rejected[name] = True
        else:
            invalid_json_rejected[name] = False
    sorted_mapping = freeze_json({"z": 1, "a": 2})
    sorted_mapping_keys = (
        list(sorted_mapping)
        if hasattr(sorted_mapping, "__iter__")
        else []
    )

    passed = all(
        (
            journal_before == expected_journal,
            journal_after_caller_mutation == journal_before,
            journal_after_export_mutation == journal_before,
            wire_after_caller_mutation == wire_before,
            wire_after_export_mutation == wire_before,
            all(direct_mutation_rejected.values()),
            set(direct_mutation_rejected) == set(frozen_roots),
            all(invalid_json_rejected.values()),
            len(invalid_json_rejected) == 4,
            sorted_mapping_keys == ["a", "z"],
        )
    )
    return ModelRiskLabAuditCase(
        "core_replay_payloads_are_deeply_owned_and_detached",
        {
            "caller_mutation_preserved_journal": (
                journal_after_caller_mutation == journal_before
            ),
            "direct_mutation_rejected": direct_mutation_rejected,
            "export_mutation_preserved_journal": (
                journal_after_export_mutation == journal_before
            ),
            "invalid_json_rejected": invalid_json_rejected,
            "journal_sha256": hashlib.sha256(
                journal_before.encode("utf-8")
            ).hexdigest(),
            "sorted_mapping_keys": sorted_mapping_keys,
            "wire_sha256": hashlib.sha256(wire_before.encode("utf-8")).hexdigest(),
            "wire_stable_after_caller_mutation": (
                wire_after_caller_mutation == wire_before
            ),
            "wire_stable_after_export_mutation": (
                wire_after_export_mutation == wire_before
            ),
        },
        () if passed else ("core replay payload ownership boundary failed",),
    )


def _subsystem_evidence_payload_ownership_case() -> ModelRiskLabAuditCase:
    probes: dict[str, dict[str, object]] = {}

    def nested_payload() -> dict[str, object]:
        return {
            "nested": {
                "queue": [
                    {
                        "flags": [True, None],
                        "quantity": 10,
                    }
                ]
            }
        }

    def set_nested_quantity(root, quantity: int) -> None:
        root["nested"]["queue"][0]["quantity"] = quantity

    def set_artifact_digest(root, value: int) -> None:
        root["artifact.json"] = f"{value:064x}"

    def probe(
        name,
        source,
        stored,
        render,
        export_root,
        mutate=set_nested_quantity,
    ) -> None:
        wire_before = canonical_json(render())
        mutate(source, 991)
        caller_stable = canonical_json(render()) == wire_before

        direct_mutation_rejected = False
        try:
            mutate(stored, 992)
        except (AttributeError, TypeError):
            direct_mutation_rejected = True

        exported = render()
        export_mutable = True
        try:
            mutate(export_root(exported), 993)
        except (AttributeError, IndexError, KeyError, TypeError):
            export_mutable = False
        export_stable = canonical_json(render()) == wire_before
        passed = all(
            (
                caller_stable,
                direct_mutation_rejected,
                export_mutable,
                export_stable,
            )
        )
        probes[name] = {
            "caller_mutation_preserved_wire": caller_stable,
            "direct_mutation_rejected": direct_mutation_rejected,
            "export_is_detached_and_mutable": export_mutable,
            "export_mutation_preserved_wire": export_stable,
            "passed": passed,
            "wire_sha256": hashlib.sha256(wire_before.encode("utf-8")).hexdigest(),
        }

    flow_command = nested_payload()
    flow_diagnostic = nested_payload()
    flow_event = FlowEvent(
        sequence=1,
        simulation_time_us=10,
        family=FlowEventFamily.LIMIT_BUY,
        applied=True,
        command=flow_command,
        reason=None,
        exchange_event_start=1,
        exchange_event_end=2,
        diagnostic=flow_diagnostic,
    )
    probe(
        "flow_event.command",
        flow_command,
        flow_event.command,
        flow_event.as_dict,
        lambda payload: payload["command"],
    )
    probe(
        "flow_event.diagnostic",
        flow_diagnostic,
        flow_event.diagnostic,
        flow_event.as_dict,
        lambda payload: payload["diagnostic"],
    )

    mechanics_data = nested_payload()
    mechanics_event = MechanicsEvent(
        1,
        10,
        MechanicsEventType.ORDER_ACCEPTED,
        mechanics_data,
    )
    probe(
        "mechanics_event.data",
        mechanics_data,
        mechanics_event.data,
        mechanics_event.as_dict,
        lambda payload: payload["data"],
    )

    latency_data = nested_payload()
    latency_event = LatencyEvent(
        1,
        10,
        LatencyEventType.KEY_PRESSED,
        "PLAYER-O-000001",
        latency_data,
    )
    probe(
        "latency_event.data",
        latency_data,
        latency_event.data,
        latency_event.as_dict,
        lambda payload: payload["data"],
    )

    observable_data = nested_payload()
    observable_event = ObservableEvent(
        1,
        10,
        11,
        ObservableEventType.BOOK_SNAPSHOT,
        observable_data,
    )
    probe(
        "observable_event.data",
        observable_data,
        observable_event.data,
        observable_event.as_dict,
        lambda payload: payload["data"],
    )

    truth_data = nested_payload()
    truth_event = TruthEvent(
        1,
        10,
        TruthEventType.ORDER_ACCEPTED,
        truth_data,
    )
    probe(
        "truth_event.data",
        truth_data,
        truth_event.data,
        truth_event.as_dict,
        lambda payload: payload["data"],
    )

    coordinator_data = nested_payload()
    coordinator_event = CoordinatorEvent(
        1,
        10,
        CoordinatorEventType.ROUTE_DECISION,
        coordinator_data,
    )
    probe(
        "coordinator_event.data",
        coordinator_data,
        coordinator_event.data,
        coordinator_event.as_dict,
        lambda payload: payload["data"],
    )

    ecology_data = nested_payload()
    ecology_event = PublicEcologyEvent(1, 10, "BOOK_SNAPSHOT", ecology_data)
    probe(
        "public_ecology_event.data",
        ecology_data,
        ecology_event.data,
        ecology_event.as_dict,
        lambda payload: payload["data"],
    )

    queued_data = nested_payload()
    queued_strategy_data = nested_payload()
    pending = _PendingObservable(
        due_time_us=11,
        source_time_us=10,
        ordinal=1,
        event_type=ObservableEventType.BOOK_SNAPSHOT,
        data=queued_data,
        strategy_event_type=EventType.ORDER_SUBMITTED,
        strategy_data=queued_strategy_data,
    )

    def render_pending() -> dict[str, object]:
        return {
            "data": thaw_json(pending.data),
            "strategy_data": thaw_json(pending.strategy_data),
        }

    probe(
        "pending_observable.data",
        queued_data,
        pending.data,
        render_pending,
        lambda payload: payload["data"],
    )
    probe(
        "pending_observable.strategy_data",
        queued_strategy_data,
        pending.strategy_data,
        render_pending,
        lambda payload: payload["strategy_data"],
    )

    component_data = nested_payload()
    component = SnapshotComponent(
        "agent_state",
        ComponentStatus.PRESERVED,
        component_data,
        "subsystem payload ownership probe",
    )
    probe(
        "snapshot_component.payload",
        component_data,
        component.payload,
        component.as_dict,
        lambda payload: payload["payload"],
    )

    timeline_data = nested_payload()
    timeline = CounterfactualTimelineEntry(1, 10, "PROBE", timeline_data)
    probe(
        "counterfactual_timeline.payload",
        timeline_data,
        timeline.payload,
        timeline.as_dict,
        lambda payload: payload["payload"],
    )

    divergence_original = nested_payload()
    divergence_branch = nested_payload()
    divergence = FirstDivergence(
        0,
        divergence_original,
        divergence_branch,
        "payload ownership probe",
    )
    probe(
        "first_divergence.original",
        divergence_original,
        divergence.original,
        divergence.as_dict,
        lambda payload: payload["original"],
    )
    probe(
        "first_divergence.branch",
        divergence_branch,
        divergence.branch,
        divergence.as_dict,
        lambda payload: payload["branch"],
    )

    outcome_metrics = nested_payload()
    outcome = CounterfactualOutcome(
        state_sha256="1" * 64,
        timeline_sha256="2" * 64,
        metrics=outcome_metrics,
        timeline=(timeline,),
        invariant_status="PASS",
    )
    probe(
        "counterfactual_outcome.metrics",
        outcome_metrics,
        outcome.metrics,
        outcome.as_dict,
        lambda payload: payload["metrics"],
    )

    sweep_metrics = nested_payload()
    sweep_cell = TimingSweepCell(
        0,
        "run-payload-ownership",
        "3" * 64,
        sweep_metrics,
        0,
    )
    probe(
        "timing_sweep_cell.branch_metrics",
        sweep_metrics,
        sweep_cell.branch_metrics,
        sweep_cell.as_dict,
        lambda payload: payload["branch_metrics"],
    )

    component_names = (
        "agent_state",
        "all_venue_states",
        "exchange_state",
        "feature_windows",
        "flow_model_state",
        "hawkes_decay_state",
        "historical_replay_cursor",
        "pending_latency_messages",
        "player_state",
        "rng_state",
        "simulation_clock",
        "strategy_state",
        "working_orders",
    )
    snapshot = BranchSnapshot(
        "run-payload-ownership",
        10,
        (component,)
        + tuple(
            SnapshotComponent(
                name,
                ComponentStatus.PRESERVED,
                {"component": name},
                "subsystem payload ownership probe",
            )
            for name in component_names[1:]
        ),
    )
    comparison = nested_payload()
    hindsight_guard = nested_payload()
    report = CounterfactualReport(
        parent_run_id="run-payload-ownership",
        mode=CounterfactualMode.ENDOGENOUS_FORK,
        mutation_manifest=MutationManifest((ActionMutation(1, timing_delta_us=1),)),
        snapshot=snapshot,
        snapshot_reconstruction_match=True,
        original=outcome,
        branch=outcome,
        first_divergence=divergence,
        comparison=comparison,
        exogenous_reference_path_sha256=None,
        hindsight_guard=hindsight_guard,
    )
    probe(
        "counterfactual_report.comparison",
        comparison,
        report.comparison,
        report.as_dict,
        lambda payload: payload["comparison"],
    )
    probe(
        "counterfactual_report.hindsight_guard",
        hindsight_guard,
        report.hindsight_guard,
        report.as_dict,
        lambda payload: payload["hindsight_guard"],
    )

    fault_details = nested_payload()
    fault = FaultEvidence(
        FaultKind.DUPLICATE_MESSAGE,
        "payload_ownership_probe",
        "DUPLICATE_MESSAGE",
        "DUPLICATE_MESSAGE",
        1,
        fault_details,
    )
    probe(
        "fault_evidence.details",
        fault_details,
        fault.details,
        fault.as_dict,
        lambda payload: payload["details"],
    )

    configuration = GeneratedConfiguration(
        sequence=1,
        lane=ExecutorLane.CORE_FLOW,
        cell_id="core_flow-payload-ownership",
        replicate_index=0,
        partition=ExperimentPartition.TRAIN,
        seed=771,
        duration_us=1_000,
        duration_events=1,
        agent_count=1,
        flow_model="POISSON",
        regime="BASELINE",
        volume="NORMAL",
        liquidity="NORMAL",
        latency="ZERO",
        session_phase="CONTINUOUS",
        order_types="LIMIT",
        hidden_liquidity="NONE",
        venue_count=1,
        auction_state="NONE",
        agent_population="ONE",
        strategy="NONE",
        objective="PAYLOAD_OWNERSHIP",
    )
    kernel_event = nested_payload()
    kernel_state = nested_payload()
    kernel_observable = nested_payload()
    kernel = KernelResult(
        configuration=configuration,
        event_stream=(kernel_event,),
        venue_states=(kernel_state,),
        observable_layer=kernel_observable,
        metrics={"event_count": 1},
        invariant_checks={"payload_ownership": True},
        violations=(),
        fault_evidence=fault,
    )
    probe(
        "kernel_result.event_stream",
        kernel_event,
        kernel.event_stream[0],
        kernel.as_dict,
        lambda payload: payload["event_stream"][0],
    )
    probe(
        "kernel_result.venue_states",
        kernel_state,
        kernel.venue_states[0],
        kernel.as_dict,
        lambda payload: payload["venue_states"][0],
    )
    probe(
        "kernel_result.observable_layer",
        kernel_observable,
        kernel.observable_layer,
        kernel.as_dict,
        lambda payload: payload["observable_layer"],
    )

    statistic_evidence = nested_payload()
    statistic = StatisticalCheck(
        "payload_ownership_probe",
        "PASS",
        statistic_evidence,
        "immutable",
    )
    probe(
        "statistical_check.evidence",
        statistic_evidence,
        statistic.evidence,
        statistic.as_dict,
        lambda payload: payload["evidence"],
    )

    acceptance_digests = {"artifact.json": "0" * 64}
    acceptance = AcceptanceRecord(
        "acceptance-payload-ownership-probe",
        1,
        771,
        "PENDING_HUMAN_REVIEW",
        ("payload ownership probe",),
        (),
        acceptance_digests,
    )
    probe(
        "acceptance_record.artifact_digests",
        acceptance_digests,
        acceptance.artifact_digests,
        acceptance.as_dict,
        lambda payload: payload["artifact_digests"],
        set_artifact_digest,
    )

    required_event_probes = {
        "coordinator_event.data",
        "flow_event.command",
        "flow_event.diagnostic",
        "latency_event.data",
        "mechanics_event.data",
        "observable_event.data",
        "public_ecology_event.data",
        "truth_event.data",
    }
    failed = sorted(name for name, evidence in probes.items() if not evidence["passed"])
    missing_event_probes = sorted(required_event_probes.difference(probes))
    failures = []
    if failed:
        failures.append(f"mutable or aliased subsystem payloads: {failed}")
    if missing_event_probes:
        failures.append(f"unexercised subsystem event payloads: {missing_event_probes}")
    return ModelRiskLabAuditCase(
        "subsystem_evidence_payloads_are_deeply_owned_and_detached",
        {
            "named_event_family_probes": sorted(required_event_probes),
            "probe_count": len(probes),
            "probes": probes,
        },
        tuple(failures),
    )


def _exchange_state_ownership_case() -> ModelRiskLabAuditCase:
    book = OrderBook()
    submitted = Order.limit(
        "OWNERSHIP-BID",
        Side.BUY,
        100,
        100,
        OrderOwner.PLAYER,
    )
    book.process(submitted)
    caller_remained_pristine = all(
        (
            submitted.remaining_quantity == 100,
            submitted.filled_quantity == 0,
            submitted.cancelled_quantity == 0,
            submitted.resting_sequence is None,
            submitted.status is OrderStatus.NEW,
        )
    )
    state_before_attacks = book.state_sha256()
    events_before_attacks = book.journal.canonical_json_lines()
    order_view = book.active_orders[submitted.order_id]
    level_view = book.bids[100]
    active_mapping = book.active_orders
    all_mapping = book.all_orders
    bid_mapping = book.bids

    submitted.apply_fill(10)
    submitted.price_ticks = 999
    caller_mutation_preserved_state = book.state_sha256() == state_before_attacks
    caller_mutation_preserved_events = (
        book.journal.canonical_json_lines() == events_before_attacks
    )

    public_mutation_rejected: dict[str, bool] = {}
    try:
        order_view.remaining_quantity = 1  # type: ignore[misc]
    except (AttributeError, TypeError):
        public_mutation_rejected["order_view"] = True
    else:
        public_mutation_rejected["order_view"] = False
    try:
        level_view.orders.clear()  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        public_mutation_rejected["price_level_queue"] = True
    else:
        public_mutation_rejected["price_level_queue"] = False
    try:
        active_mapping["FORGED"] = order_view  # type: ignore[index]
    except (AttributeError, TypeError):
        public_mutation_rejected["active_order_mapping"] = True
    else:
        public_mutation_rejected["active_order_mapping"] = False
    try:
        all_mapping["FORGED"] = order_view  # type: ignore[index]
    except (AttributeError, TypeError):
        public_mutation_rejected["all_order_mapping"] = True
    else:
        public_mutation_rejected["all_order_mapping"] = False
    try:
        bid_mapping[999] = level_view  # type: ignore[index]
    except (AttributeError, TypeError):
        public_mutation_rejected["price_level_mapping"] = True
    else:
        public_mutation_rejected["price_level_mapping"] = False

    forged_process = Order.limit("FORGED-PROCESS", Side.BUY, 10, 99)
    forged_process.apply_fill(1)
    forged_process_rejected = False
    try:
        book.process(forged_process)
    except ValueError:
        forged_process_rejected = True

    forged_replacement = Order.limit("FORGED-REPLACEMENT", Side.BUY, 10, 99)
    forged_replacement.apply_fill(1)
    forged_replacement_rejected = False
    try:
        book.replace(
            submitted.order_id,
            forged_replacement,
            "FORGED-REPLACE-CANCEL",
        )
    except ValueError:
        forged_replacement_rejected = True

    attacks_preserved_state = book.state_sha256() == state_before_attacks
    attacks_preserved_events = (
        book.journal.canonical_json_lines() == events_before_attacks
    )
    owned_state_before_command = book.all_orders[submitted.order_id]

    book.process(Order.market("OWNERSHIP-SELL", Side.SELL, 40))
    owned_state_after_command = book.all_orders[submitted.order_id]
    state_after_command = book.state_sha256()
    events_after_command = book.journal.canonical_json_lines()
    genuine_command_changed_state = state_after_command != state_before_attacks
    genuine_command_changed_events = events_after_command != events_before_attacks
    genuine_command_reconciled = all(
        (
            owned_state_before_command.remaining_quantity == 100,
            owned_state_after_command.remaining_quantity == 60,
            owned_state_after_command.filled_quantity == 40,
            owned_state_after_command.status is OrderStatus.PARTIALLY_FILLED,
            order_view.remaining_quantity == 100,
            book.player_position.position == 40,
        )
    )

    immutable_ledger_rejected: dict[str, bool] = {}
    trades = book.trades
    fills = book.fills
    try:
        trades.clear()  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        immutable_ledger_rejected["trades_tuple"] = True
    else:
        immutable_ledger_rejected["trades_tuple"] = False
    try:
        fills.clear()  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        immutable_ledger_rejected["fills_tuple"] = True
    else:
        immutable_ledger_rejected["fills_tuple"] = False
    try:
        fills[0].quantity = 999  # type: ignore[misc]
    except (AttributeError, TypeError):
        immutable_ledger_rejected["frozen_fill"] = True
    else:
        immutable_ledger_rejected["frozen_fill"] = False
    ledger_attacks_preserved_state = book.state_sha256() == state_after_command
    ledger_attacks_preserved_events = (
        book.journal.canonical_json_lines() == events_after_command
    )
    book.assert_invariants()

    passed = all(
        (
            caller_remained_pristine,
            caller_mutation_preserved_state,
            caller_mutation_preserved_events,
            all(public_mutation_rejected.values()),
            len(public_mutation_rejected) == 5,
            forged_process_rejected,
            forged_replacement_rejected,
            attacks_preserved_state,
            attacks_preserved_events,
            genuine_command_changed_state,
            genuine_command_changed_events,
            genuine_command_reconciled,
            all(immutable_ledger_rejected.values()),
            len(immutable_ledger_rejected) == 3,
            ledger_attacks_preserved_state,
            ledger_attacks_preserved_events,
        )
    )
    return ModelRiskLabAuditCase(
        "exchange_state_changes_only_through_owned_journaled_commands",
        {
            "attacks_preserved_events": attacks_preserved_events,
            "attacks_preserved_state": attacks_preserved_state,
            "caller_mutation_preserved_events": caller_mutation_preserved_events,
            "caller_mutation_preserved_state": caller_mutation_preserved_state,
            "caller_remained_pristine_after_process": caller_remained_pristine,
            "event_sha256_after_command": hashlib.sha256(
                events_after_command.encode("utf-8")
            ).hexdigest(),
            "event_sha256_before_attacks": hashlib.sha256(
                events_before_attacks.encode("utf-8")
            ).hexdigest(),
            "forged_process_rejected": forged_process_rejected,
            "forged_replacement_rejected": forged_replacement_rejected,
            "genuine_command_changed_events": genuine_command_changed_events,
            "genuine_command_changed_state": genuine_command_changed_state,
            "genuine_command_reconciled": genuine_command_reconciled,
            "immutable_ledger_rejected": immutable_ledger_rejected,
            "ledger_attacks_preserved_events": ledger_attacks_preserved_events,
            "ledger_attacks_preserved_state": ledger_attacks_preserved_state,
            "public_mutation_rejected": public_mutation_rejected,
            "state_sha256_after_command": state_after_command,
            "state_sha256_before_attacks": state_before_attacks,
        },
        () if passed else ("exchange state ownership boundary failed",),
    )


def _artifact_path_boundary_case(root: Path) -> ModelRiskLabAuditCase:
    record_root = root / "record"
    store = AuditLabStore(record_root)
    identity = {"budget": 1, "probe": "artifact-path-boundary", "seed": 771}
    relative_escape = record_root / "relative-escape.txt"
    absolute_escape = record_root / "absolute-escape.txt"
    invalid_names = {
        "absolute_path": str(absolute_escape.resolve()),
        "backslash_traversal": r"nested\..\escape.txt",
        "deep_parent_escape": "../../../relative-escape.txt",
        "dot_segment": "safe/./artifact.txt",
        "parent_segment": "../escape.txt",
        "reserved_manifest": "manifest.json",
        "windows_drive": "C:/escape.txt",
    }
    rejected: dict[str, str] = {}
    for label, name in invalid_names.items():
        try:
            store.record(identity, {name: "escape"})
        except (TypeError, ValueError) as error:
            rejected[label] = str(error)

    staging_entries_after_rejection = sorted(
        path.relative_to(store.staging).as_posix()
        for path in store.staging.rglob("*")
    )
    packet_entries_after_rejection = sorted(
        path.name for path in store.packets.iterdir()
    )
    safe = store.record(
        {"budget": 1, "probe": "safe-nested-artifact", "seed": 771},
        {
            "failures/example.json": "{}\n",
            "summary.txt": "safe nested artifact\n",
        },
    )
    safe_verification = store.verify(safe.packet_id)

    traversal_store = AuditLabStore(root / "verify-traversal")
    traversal_identity = {
        "budget": 1,
        "probe": "manifest-traversal",
        "seed": 771,
    }
    traversal_packet_id = f"audit-{canonical_sha256(traversal_identity)[:24]}"
    traversal_directory = traversal_store.packets / traversal_packet_id
    traversal_directory.mkdir()
    traversal_outside = traversal_store.packets / "outside.txt"
    traversal_content = "outside packet\n"
    traversal_outside.write_text(traversal_content, encoding="utf-8")
    traversal_manifest = {
        "artifacts": {
            "../outside.txt": {
                "bytes": len(traversal_content.encode("utf-8")),
                "sha256": hashlib.sha256(
                    traversal_content.encode("utf-8")
                ).hexdigest(),
            }
        },
        "identity": traversal_identity,
        "packet_id": traversal_packet_id,
        "record_type": "IMMUTABLE_KIRBY2_MODEL_RISK_PACKET",
        "schema_version": 1,
    }
    (traversal_directory / "manifest.json").write_text(
        canonical_json(traversal_manifest) + "\n",
        encoding="utf-8",
    )
    traversal_verification = traversal_store.verify(traversal_packet_id)

    symlink_store = AuditLabStore(root / "verify-symlink")
    symlink_identity = {"budget": 1, "probe": "symlink", "seed": 771}
    symlink_packet_id = f"audit-{canonical_sha256(symlink_identity)[:24]}"
    symlink_directory = symlink_store.packets / symlink_packet_id
    symlink_directory.mkdir()
    symlink_outside = symlink_store.root / "outside.txt"
    symlink_content = "symlink target\n"
    symlink_outside.write_text(symlink_content, encoding="utf-8")
    (symlink_directory / "link.txt").symlink_to(symlink_outside)
    symlink_manifest = {
        "artifacts": {
            "link.txt": {
                "bytes": len(symlink_content.encode("utf-8")),
                "sha256": hashlib.sha256(
                    symlink_content.encode("utf-8")
                ).hexdigest(),
            }
        },
        "identity": symlink_identity,
        "packet_id": symlink_packet_id,
        "record_type": "IMMUTABLE_KIRBY2_MODEL_RISK_PACKET",
        "schema_version": 1,
    }
    (symlink_directory / "manifest.json").write_text(
        canonical_json(symlink_manifest) + "\n",
        encoding="utf-8",
    )
    symlink_verification = symlink_store.verify(symlink_packet_id)

    packet_id_traversal_rejected = False
    try:
        store.verify("../escape")
    except ValueError:
        packet_id_traversal_rejected = True

    passed = all(
        (
            set(rejected) == set(invalid_names),
            not relative_escape.exists(),
            not absolute_escape.exists(),
            not staging_entries_after_rejection,
            not packet_entries_after_rejection,
            safe_verification.verification_status == "PASS",
            (safe.directory / "failures" / "example.json").is_file(),
            not any(store.staging.iterdir()),
            traversal_verification.verification_status.startswith("FAIL"),
            "invalid artifact name" in traversal_verification.verification_status,
            symlink_verification.verification_status.startswith("FAIL"),
            "symlink" in symlink_verification.verification_status,
            packet_id_traversal_rejected,
        )
    )
    return ModelRiskLabAuditCase(
        "packet_artifact_paths_are_contained_before_write_and_during_verify",
        {
            "absolute_escape_exists": absolute_escape.exists(),
            "invalid_names": invalid_names,
            "packet_entries_after_rejection": packet_entries_after_rejection,
            "packet_id_traversal_rejected": packet_id_traversal_rejected,
            "rejected": rejected,
            "relative_escape_exists": relative_escape.exists(),
            "safe_nested_artifact_exists": (
                safe.directory / "failures" / "example.json"
            ).is_file(),
            "safe_verification": safe_verification.verification_status,
            "staging_entries_after_rejection": staging_entries_after_rejection,
            "symlink_verification": symlink_verification.verification_status,
            "traversal_verification": traversal_verification.verification_status,
        },
        () if passed else ("audit packet path containment boundary failed",),
    )


def _packet_identity_scope_case(
    root: Path,
    persisted_packet,
) -> ModelRiskLabAuditCase:
    v2_store = AuditLabStore(root / "v2")
    v2_identity = {"budget": 1, "probe": "packet-v2", "seed": 771}
    alpha_artifacts = {"result.txt": "alpha\n"}
    alpha = v2_store.record(v2_identity, alpha_artifacts)
    alpha_manifest_before = (alpha.directory / "manifest.json").read_bytes()
    alpha_idempotent = v2_store.record(v2_identity, dict(alpha_artifacts))
    alpha_manifest_after = (alpha.directory / "manifest.json").read_bytes()
    beta = v2_store.record(v2_identity, {"result.txt": "beta\n"})
    v2_store.acceptance_ledger.write_text("", encoding="utf-8")
    v2_ledger = v2_store.verify_ledgers()

    alpha_path = alpha.directory / "result.txt"
    alpha_bytes = alpha_path.read_bytes()
    alpha_path.write_bytes(alpha_bytes + b"tamper")
    tampered = v2_store.verify(alpha.packet_id)
    tampered_ledger = v2_store.verify_ledgers()
    alpha_path.write_bytes(alpha_bytes)
    restored = v2_store.verify(alpha.packet_id)
    restored_ledger = v2_store.verify_ledgers()

    legacy_store = AuditLabStore(root / "legacy")
    legacy_identity = {"budget": 1, "probe": "packet-v1", "seed": 771}
    legacy_name = "legacy.txt"
    legacy_content = b"legacy identity-only packet\n"
    legacy_packet_id = f"audit-{canonical_sha256(legacy_identity)[:24]}"
    legacy_directory = legacy_store.packets / legacy_packet_id
    legacy_directory.mkdir()
    legacy_artifact = legacy_directory / legacy_name
    legacy_artifact.write_bytes(legacy_content)
    legacy_manifest = {
        "artifacts": {
            legacy_name: {
                "bytes": len(legacy_content),
                "sha256": hashlib.sha256(legacy_content).hexdigest(),
            }
        },
        "identity": legacy_identity,
        "packet_id": legacy_packet_id,
        "record_type": "IMMUTABLE_KIRBY2_MODEL_RISK_PACKET",
        "schema_version": LEGACY_AUDIT_PACKET_SCHEMA_VERSION,
    }
    legacy_manifest_path = legacy_directory / "manifest.json"
    legacy_manifest_path.write_text(
        canonical_json(legacy_manifest) + "\n",
        encoding="utf-8",
    )
    legacy_manifest_bytes = legacy_manifest_path.read_bytes()
    legacy_artifact_bytes = legacy_artifact.read_bytes()
    legacy_stats = (
        legacy_manifest_path.stat().st_ino,
        legacy_manifest_path.stat().st_mtime_ns,
        legacy_artifact.stat().st_ino,
        legacy_artifact.stat().st_mtime_ns,
    )
    legacy_verification = legacy_store.verify(legacy_packet_id)
    promoted = legacy_store.record(
        legacy_identity,
        {legacy_name: legacy_content.decode("utf-8")},
    )
    legacy_restored_verification = legacy_store.verify(legacy_packet_id)
    legacy_stats_after = (
        legacy_manifest_path.stat().st_ino,
        legacy_manifest_path.stat().st_mtime_ns,
        legacy_artifact.stat().st_ino,
        legacy_artifact.stat().st_mtime_ns,
    )
    legacy_unchanged = (
        legacy_manifest_path.read_bytes() == legacy_manifest_bytes
        and legacy_artifact.read_bytes() == legacy_artifact_bytes
        and legacy_stats_after == legacy_stats
    )

    persisted_report = (persisted_packet.directory / "report.txt").read_text(
        encoding="utf-8"
    )
    stored_report_uses_manifest = (
        "PACKET id=SEE_MANIFEST" in persisted_report
        and persisted_packet.packet_id not in persisted_report
    )
    passed = all(
        (
            alpha.packet_id == alpha_idempotent.packet_id,
            alpha.packet_id != beta.packet_id,
            alpha_manifest_before == alpha_manifest_after,
            alpha.schema_version == AUDIT_PACKET_SCHEMA_VERSION,
            alpha.identity_scope == "IDENTITY_AND_ARTIFACTS",
            v2_ledger["status"] == "PASS",
            tampered.verification_status.startswith("FAIL"),
            tampered_ledger["status"] == "FAIL",
            restored.verification_status == "PASS",
            restored_ledger["status"] == "PASS",
            legacy_verification.verification_status == "PASS",
            legacy_verification.schema_version
            == LEGACY_AUDIT_PACKET_SCHEMA_VERSION,
            legacy_verification.identity_scope == "IDENTITY_ONLY_LEGACY",
            legacy_restored_verification.verification_status == "PASS",
            legacy_unchanged,
            promoted.packet_id != legacy_packet_id,
            promoted.schema_version == AUDIT_PACKET_SCHEMA_VERSION,
            stored_report_uses_manifest,
        )
    )
    return ModelRiskLabAuditCase(
        "packet_v2_identity_binds_artifacts_and_preserves_v1_read_only",
        {
            "different_bytes_have_different_ids": (
                alpha.packet_id != beta.packet_id
            ),
            "idempotent_packet_id": alpha_idempotent.packet_id,
            "legacy_packet_id": legacy_packet_id,
            "legacy_promoted_v2_packet_id": promoted.packet_id,
            "legacy_unchanged": legacy_unchanged,
            "legacy_verification": legacy_verification.as_dict(),
            "persisted_packet": persisted_packet.as_dict(),
            "stored_report_uses_manifest": stored_report_uses_manifest,
            "tampered_ledger": tampered_ledger,
            "tampered_verification": tampered.verification_status,
            "v2_alpha_packet": alpha.as_dict(),
            "v2_beta_packet": beta.as_dict(),
            "v2_ledger": v2_ledger,
            "v2_restored_ledger": restored_ledger,
            "v2_restored_verification": restored.verification_status,
        },
        () if passed else ("packet schema-v2 identity boundary failed",),
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
