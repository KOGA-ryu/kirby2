"""Real execution-algorithm and objective executor for generated audit cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, fields

from kirby2.algorithms import (
    AlgorithmAction,
    AlgorithmActionType,
    AlgorithmDecision,
    AlgorithmName,
    AlgorithmObservation,
    AlgorithmParameterManifest,
    ClientFill,
    ExecutionBenchmarkMetrics,
    ExecutionCellResult,
    ExecutionObjective,
    RiskLimits,
    default_algorithm_manifest,
    run_execution_cell,
)
from kirby2.exchange import Side
from kirby2.immutable import thaw_json
from kirby2.multivenue import (
    MultiVenueRecording,
    RoutePolicy,
    RouteStyle,
    replay_multivenue_recording,
)
from kirby2.session.objectives import ObjectiveType

from ..models import (
    CaseRecording,
    CheckResult,
    CheckStatus,
    ExerciseRecord,
    ExerciseStatus,
    ExecutorLane,
    FailureKind,
    FailureObservation,
    GeneratedCaseResult,
    GeneratedConfiguration,
    canonical_sha256,
)
from .base import finalize_recording


ALGORITHM_RECORDING_TYPE = "NATIVE_ALGORITHM_RECORDINGS"
_RECORDING_FIELDS = frozenset(
    {
        "configuration",
        "execution_mapping",
        "legs",
        "winner_declaration",
    }
)
_LEG_FIELDS = frozenset(
    {
        "background_path_sha256",
        "client_fills",
        "control_final_state_sha256",
        "decisions",
        "final_signed_position",
        "fork_state_sha256",
        "leg_sequence",
        "manifest",
        "metrics",
        "native_recording",
        "objective",
        "observe_only",
        "scenario_name",
        "seed",
    }
)
_MAPPING_VERSION = 1
_TARGET_QUANTITY = 300
_DURATION_US = 1_000_000
_DECISION_INTERVAL_US = 250_000
_RISK_LIMITS = RiskLimits(
    maximum_child_quantity=200,
    maximum_working_quantity=300,
    maximum_position=300,
    maximum_spread_ticks=10,
)
_SCENARIO_BY_OBJECTIVE = {
    ObjectiveType.ACQUIRE: "opening_momentum",
    ObjectiveType.LIQUIDATE: "balanced_execution",
    ObjectiveType.ROUND_TRIP: "balanced_execution",
    ObjectiveType.OBSERVE_ONLY: "opening_momentum",
}
_SIDES_BY_OBJECTIVE = {
    ObjectiveType.ACQUIRE: (Side.BUY,),
    ObjectiveType.LIQUIDATE: (Side.SELL,),
    ObjectiveType.ROUND_TRIP: (Side.BUY, Side.SELL),
    ObjectiveType.OBSERVE_ONLY: (Side.BUY,),
}
_WINNER_DECLARATION = {
    "reason": (
        "SINGLE_GENERATED_CASE_IS_DESCRIPTIVE_ONLY_AND_CANNOT_RANK_STRATEGIES"
    ),
    "status": "NOT_DECLARED",
    "winner": None,
}
_FORBIDDEN_OBSERVATION_KEYS = frozenset(
    {
        "future_events",
        "future_historical_prices",
        "ground_truth",
        "hidden_quantity",
        "hidden_regime",
        "priority_sequence",
        "reserve_quantity",
    }
)


@dataclass(frozen=True, slots=True)
class _Scenario:
    configuration: GeneratedConfiguration
    objective: ObjectiveType
    legs: tuple[ExecutionCellResult, ...]
    execution_mapping: dict[str, object]


class AlgorithmExecutor:
    """Exercise production algorithms and objectives on benchmark cells."""

    lane = ExecutorLane.ALGORITHM

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        scenario = _build_scenario(configuration)
        scenario_payload = _scenario_payload(scenario)
        recording = CaseRecording(
            lane=self.lane,
            recording_type=ALGORITHM_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                **scenario_payload,
            },
        )
        return finalize_recording(
            recording,
            lambda finalized: _result(
                configuration,
                finalized,
                scenario,
                replay_mismatches=(),
            ),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("algorithm replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("algorithm replay received a different lane")
        if recording.recording_type != ALGORITHM_RECORDING_TYPE:
            raise ValueError("unsupported algorithm recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict):
            raise TypeError("algorithm recording payload must be an object")
        if set(payload) != _RECORDING_FIELDS:
            raise ValueError("algorithm recording fields are not exact")
        raw_configuration = payload["configuration"]
        raw_mapping = payload["execution_mapping"]
        raw_legs = payload["legs"]
        raw_winner = payload["winner_declaration"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("algorithm configuration must be an object")
        if not isinstance(raw_mapping, dict):
            raise TypeError("algorithm execution mapping must be an object")
        if not isinstance(raw_legs, list) or any(
            not isinstance(item, dict) for item in raw_legs
        ):
            raise TypeError("algorithm legs must be objects")
        if not isinstance(raw_winner, dict):
            raise TypeError("algorithm winner declaration must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        mismatches: list[str] = []
        legs = tuple(
            _load_recorded_leg(raw_leg, index, mismatches)
            for index, raw_leg in enumerate(raw_legs, start=1)
        )
        scenario = _Scenario(
            configuration,
            ObjectiveType(configuration.objective),
            legs,
            dict(raw_mapping),
        )
        if raw_mapping != _execution_mapping(configuration):
            mismatches.append("execution_mapping")
        if raw_winner != _WINNER_DECLARATION:
            mismatches.append("winner_declaration")
        if _scenario_payload(scenario) != {
            "execution_mapping": raw_mapping,
            "legs": raw_legs,
            "winner_declaration": raw_winner,
        }:
            mismatches.append("loaded_scenario_projection")
        return _result(
            configuration,
            recording,
            scenario,
            replay_mismatches=tuple(mismatches),
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("algorithm executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("algorithm executor received a different lane")
        try:
            algorithm = AlgorithmName(configuration.strategy)
        except ValueError as error:
            raise ValueError("unsupported generated execution algorithm") from error
        if algorithm is AlgorithmName.MANUAL_REPLAY:
            raise ValueError("manual replay is not an automated strategy axis value")
        try:
            ObjectiveType(configuration.objective)
        except ValueError as error:
            raise ValueError("unsupported generated execution objective") from error


def _build_scenario(configuration: GeneratedConfiguration) -> _Scenario:
    objective = ObjectiveType(configuration.objective)
    algorithm = AlgorithmName(configuration.strategy)
    manifest = default_algorithm_manifest(algorithm)
    scenario_name = _SCENARIO_BY_OBJECTIVE[objective]
    observe_only = objective is ObjectiveType.OBSERVE_ONLY
    legs = tuple(
        run_execution_cell(
            scenario_name=scenario_name,
            seed=configuration.seed,
            algorithm_manifest=manifest,
            side=side,
            target_quantity=_TARGET_QUANTITY,
            duration_us=_DURATION_US,
            decision_interval_us=_DECISION_INTERVAL_US,
            risk_limits=_RISK_LIMITS,
            observe_only=observe_only,
        )
        for side in _SIDES_BY_OBJECTIVE[objective]
    )
    mapping = _execution_mapping(configuration)
    return _Scenario(configuration, objective, legs, mapping)


def _execution_mapping(
    configuration: GeneratedConfiguration,
) -> dict[str, object]:
    objective = ObjectiveType(configuration.objective)
    algorithm = AlgorithmName(configuration.strategy)
    manifest = default_algorithm_manifest(algorithm)
    scenario_name = _SCENARIO_BY_OBJECTIVE[objective]
    observe_only = objective is ObjectiveType.OBSERVE_ONLY
    return {
        "algorithm_manifest": manifest.as_dict(),
        "algorithm_manifest_sha256": manifest.sha256(),
        "configured_objective": objective.value,
        "configured_target_quantity": (
            0 if observe_only else _TARGET_QUANTITY
        ),
        "decision_interval_us": _DECISION_INTERVAL_US,
        "duration_us": _DURATION_US,
        "leg_sides": [side.value for side in _SIDES_BY_OBJECTIVE[objective]],
        "mapping_version": _MAPPING_VERSION,
        "observation_reference_quantity": _TARGET_QUANTITY,
        "observe_only": observe_only,
        "risk_limits": _RISK_LIMITS.as_dict(),
        "scenario_name": scenario_name,
        "seed": configuration.seed,
        "strategy": algorithm.value,
    }


def _load_recorded_leg(
    raw_leg: dict[str, object],
    expected_sequence: int,
    mismatches: list[str],
) -> ExecutionCellResult:
    if set(raw_leg) != _LEG_FIELDS:
        raise ValueError("algorithm leg fields are not exact")
    if raw_leg["leg_sequence"] != expected_sequence:
        mismatches.append(f"leg_{expected_sequence}_sequence")
    raw_manifest = _object(raw_leg, "manifest")
    raw_objective = _object(raw_leg, "objective")
    raw_decisions = _object_array(raw_leg, "decisions")
    raw_fills = _object_array(raw_leg, "client_fills")
    raw_metrics = _object(raw_leg, "metrics")
    raw_native = _object(raw_leg, "native_recording")
    manifest = AlgorithmParameterManifest(
        algorithm=AlgorithmName(str(raw_manifest["algorithm"])),
        parameters=_object(raw_manifest, "parameters"),
        simulator_only=bool(raw_manifest["simulator_only"]),
    )
    objective = ExecutionObjective(
        side=Side(str(raw_objective["side"])),
        target_quantity=int(raw_objective["target_quantity"]),
        start_time_us=int(raw_objective["start_time_us"]),
        deadline_us=int(raw_objective["deadline_us"]),
        arrival_midpoint_x2=int(raw_objective["arrival_midpoint_x2"]),
    )
    native = MultiVenueRecording.from_dict(raw_native)
    native_replay = replay_multivenue_recording(native)
    if not native_replay.passed:
        mismatches.append(f"native_leg_{expected_sequence}")
    leg = ExecutionCellResult(
        scenario_name=str(raw_leg["scenario_name"]),
        seed=int(raw_leg["seed"]),
        manifest=manifest,
        objective=objective,
        fork_state_sha256=str(raw_leg["fork_state_sha256"]),
        background_path_sha256=str(raw_leg["background_path_sha256"]),
        control_final_state_sha256=str(raw_leg["control_final_state_sha256"]),
        decisions=tuple(_load_decision(item) for item in raw_decisions),
        client_fills=tuple(_load_fill(item) for item in raw_fills),
        recording=native,
        metrics=ExecutionBenchmarkMetrics(**raw_metrics),
        observe_only=bool(raw_leg["observe_only"]),
    )
    if leg.final_signed_position != raw_leg["final_signed_position"]:
        mismatches.append(f"leg_{expected_sequence}_position")
    return leg


def _load_decision(raw: dict[str, object]) -> AlgorithmDecision:
    raw_action = _object(raw, "action")
    action_type = AlgorithmActionType(str(raw_action["action_type"]))
    raw_route_policy = raw_action["route_policy"]
    raw_route_style = raw_action["route_style"]
    action = AlgorithmAction(
        action_type=action_type,
        reason=str(raw_action["reason"]),
        quantity=int(raw_action["quantity"]),
        route_policy=(
            None
            if raw_route_policy is None
            else RoutePolicy(str(raw_route_policy))
        ),
        route_style=(
            None
            if raw_route_style is None
            else RouteStyle(str(raw_route_style))
        ),
        direct_venue_id=(
            None
            if raw_action["direct_venue_id"] is None
            else str(raw_action["direct_venue_id"])
        ),
        limit_price_ticks=(
            None
            if raw_action["limit_price_ticks"] is None
            else int(raw_action["limit_price_ticks"])
        ),
        maximum_venues=int(raw_action["maximum_venues"]),
        target_order_ids=tuple(str(item) for item in raw_action["target_order_ids"]),
    )
    observation = _object(raw, "observation")
    return AlgorithmDecision(
        sequence=int(raw["sequence"]),
        simulation_time_us=int(raw["simulation_time_us"]),
        observation_sha256=str(raw["observation_sha256"]),
        observation=observation,
        manifest_sha256=str(raw["manifest_sha256"]),
        action=action,
        action_accepted=bool(raw["action_accepted"]),
        rejection_reason=(
            None
            if raw["rejection_reason"] is None
            else str(raw["rejection_reason"])
        ),
        resulting_route_id=(
            None
            if raw["resulting_route_id"] is None
            else str(raw["resulting_route_id"])
        ),
    )


def _load_fill(raw: dict[str, object]) -> ClientFill:
    midpoint = raw["observed_midpoint_x2_at_decision"]
    return ClientFill(
        venue_id=str(raw["venue_id"]),
        trade_id=str(raw["trade_id"]),
        order_id=str(raw["order_id"]),
        side=Side(str(raw["side"])),
        price_x2=int(raw["price_x2"]),
        quantity=int(raw["quantity"]),
        received_time_us=int(raw["received_time_us"]),
        observed_midpoint_x2_at_decision=(
            None if midpoint is None else int(midpoint)
        ),
    )


def _object(payload: Mapping[str, object], name: str) -> dict[str, object]:
    value = payload[name]
    if not isinstance(value, dict):
        raise TypeError(f"serialized algorithm {name} must be an object")
    return value


def _object_array(
    payload: Mapping[str, object],
    name: str,
) -> list[dict[str, object]]:
    value = payload[name]
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise TypeError(f"serialized algorithm {name} must be an object array")
    return value


def _scenario_payload(scenario: _Scenario) -> dict[str, object]:
    return {
        "execution_mapping": scenario.execution_mapping,
        "legs": [
            {"leg_sequence": index, **leg.as_dict()}
            for index, leg in enumerate(scenario.legs, start=1)
        ],
        "winner_declaration": _WINNER_DECLARATION,
    }


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    scenario: _Scenario,
    *,
    replay_mismatches: tuple[str, ...],
) -> GeneratedCaseResult:
    observable_projection = _observable_projection(scenario)
    exercises = _exercises(configuration, recording, scenario)
    checks = _checks(scenario, observable_projection)
    failures = [
        FailureObservation(
            kind=(
                FailureKind.OBSERVABILITY_LEAK
                if check.name == "observation_boundary"
                else FailureKind.INVARIANT_VIOLATION
            ),
            code=f"ALGORITHM_{check.name.upper()}",
            message=check.detail,
            evidence={
                "check": check.name,
                "check_evidence_sha256": canonical_sha256(
                    check.as_dict()["evidence"]
                ),
            },
        )
        for check in checks
        if check.status is CheckStatus.FAIL
    ]
    failures.extend(
        FailureObservation(
            kind=FailureKind.EXECUTION_ERROR,
            code=f"ALGORITHM_{exercise.capability.upper()}_NOT_EXERCISED",
            message=(
                "configured execution dimension was not exercised: "
                f"{exercise.capability}"
            ),
            evidence={
                "capability": exercise.capability,
                "configured_value": thaw_json(exercise.configured_value),
            },
        )
        for exercise in exercises
        if exercise.status is ExerciseStatus.NOT_EXERCISED
    )
    if replay_mismatches:
        failures.append(
            FailureObservation(
                kind=FailureKind.REPLAY_MISMATCH,
                code="ALGORITHM_REPLAY_MISMATCH",
                message="native algorithm recordings did not replay exactly",
                evidence={"mismatches": list(replay_mismatches)},
            )
        )
    client_filled = sum(
        fill.quantity for leg in scenario.legs for fill in leg.client_fills
    )
    submitted = sum(
        decision.action.quantity
        for leg in scenario.legs
        for decision in leg.decisions
        if decision.action_accepted
        and decision.action.action_type
        in {AlgorithmActionType.SUBMIT, AlgorithmActionType.REPLACE}
    )
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.ALGORITHM,
        recording=recording,
        event_projection=_event_projection(scenario),
        final_state_projection={
            "execution_mapping": scenario.execution_mapping,
            "legs": [
                {
                    "background_path_sha256": leg.background_path_sha256,
                    "client_fills": [item.as_dict() for item in leg.client_fills],
                    "control_final_state_sha256": (
                        leg.control_final_state_sha256
                    ),
                    "final_signed_position": leg.final_signed_position,
                    "fork_state_sha256": leg.fork_state_sha256,
                    "metrics": leg.metrics.as_dict(),
                    "native_ground_truth": leg.recording.expected_ground_truth,
                    "native_state_sha256": (
                        leg.recording.expected_state_sha256
                    ),
                    "side": leg.objective.side.value,
                }
                for leg in scenario.legs
            ],
            "net_signed_position": sum(
                leg.final_signed_position for leg in scenario.legs
            ),
            "winner_declaration": _WINNER_DECLARATION,
        },
        metrics={
            "accepted_child_quantity": submitted,
            "client_filled_quantity": client_filled,
            "configured_target_quantity": scenario.execution_mapping[
                "configured_target_quantity"
            ],
            "decision_count": sum(len(leg.decisions) for leg in scenario.legs),
            "leg_count": len(scenario.legs),
            "native_route_count": sum(
                len(leg.recording.route_ids) for leg in scenario.legs
            ),
            "net_signed_position_shares": sum(
                leg.final_signed_position for leg in scenario.legs
            ),
            "simulation_duration_us_per_leg": _DURATION_US,
        },
        exercises=exercises,
        checks=checks,
        failures=tuple(failures),
        observable_projection=observable_projection,
    )


def _exercises(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    scenario: _Scenario,
) -> tuple[ExerciseRecord, ...]:
    algorithm = AlgorithmName(configuration.strategy)
    default_manifest = default_algorithm_manifest(algorithm)
    decisions = [
        decision for leg in scenario.legs for decision in leg.decisions
    ]
    routed = [
        decision
        for decision in decisions
        if decision.action_accepted and decision.resulting_route_id is not None
    ]
    observe_only = scenario.objective is ObjectiveType.OBSERVE_ONLY
    strategy_exercised = all(
        (
            all(leg.manifest == default_manifest for leg in scenario.legs),
            bool(decisions),
            all(
                decision.manifest_sha256 == default_manifest.sha256()
                for decision in decisions
            ),
            (
                all(
                    decision.action.action_type is AlgorithmActionType.WAIT
                    for decision in decisions
                )
                if observe_only
                else bool(routed)
            ),
        )
    )
    expected_sides = tuple(
        side.value for side in _SIDES_BY_OBJECTIVE[scenario.objective]
    )
    actual_sides = tuple(leg.objective.side.value for leg in scenario.legs)
    objective_exercised = all(
        (
            actual_sides == expected_sides,
            len(scenario.legs) == len(expected_sides),
            (
                not routed
                and all(not leg.client_fills for leg in scenario.legs)
                if observe_only
                else bool(routed)
                and all(
                    leg.objective.target_quantity == _TARGET_QUANTITY
                    for leg in scenario.legs
                )
            ),
        )
    )
    common = {
        "executor": "run_execution_cell",
        "recording_sha256": recording.sha256,
    }
    return (
        ExerciseRecord(
            ExecutorLane.ALGORITHM,
            "strategy",
            configuration.strategy,
            (
                ExerciseStatus.EXERCISED
                if strategy_exercised
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "accepted_route_decision_count": len(routed),
                "algorithm": algorithm.value,
                "decision_count": len(decisions),
                "manifest_sha256": default_manifest.sha256(),
                "observe_only_zero_action_proof": observe_only and not routed,
            },
        ),
        ExerciseRecord(
            ExecutorLane.ALGORITHM,
            "objective",
            configuration.objective,
            (
                ExerciseStatus.EXERCISED
                if objective_exercised
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "actual_leg_sides": list(actual_sides),
                "client_filled_quantity_by_leg": [
                    sum(fill.quantity for fill in leg.client_fills)
                    for leg in scenario.legs
                ],
                "configured_target_quantity": scenario.execution_mapping[
                    "configured_target_quantity"
                ],
                "expected_leg_sides": list(expected_sides),
                "leg_count": len(scenario.legs),
                "native_route_count": sum(
                    len(leg.recording.route_ids) for leg in scenario.legs
                ),
            },
        ),
    )


def _checks(
    scenario: _Scenario,
    observable_projection: dict[str, object],
) -> tuple[CheckResult, ...]:
    observation_ok, observation_evidence = _observation_boundary(scenario)
    quantity_ok, quantity_evidence = _objective_quantity_conservation(scenario)
    fill_ok, fill_evidence = _client_venue_fill_reconciliation(scenario)
    fork_ok, fork_evidence = _control_fork_identity(scenario)
    replay_ok, replay_evidence = _native_recording_replay(scenario)
    leaked = _forbidden_keys(observable_projection)
    observation_ok = observation_ok and not leaked
    observation_evidence["observable_projection_forbidden_fields"] = leaked
    return (
        _check("observation_boundary", observation_ok, observation_evidence),
        _check(
            "objective_quantity_conservation",
            quantity_ok,
            quantity_evidence,
        ),
        _check(
            "client_venue_fill_reconciliation",
            fill_ok,
            fill_evidence,
        ),
        _check("control_fork_identity", fork_ok, fork_evidence),
        _check("native_recording_replay", replay_ok, replay_evidence),
    )


def _observation_boundary(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    forbidden_fields: set[str] = set()
    representation_values: set[str] = set()
    digest_matches = True
    sequence_matches = True
    manifest_matches = True
    decision_count = 0
    for leg in scenario.legs:
        for expected_sequence, decision in enumerate(leg.decisions, start=1):
            decision_count += 1
            observation = decision.observation
            representation_values.add(str(observation.get("representation")))
            forbidden_fields.update(_forbidden_keys(observation))
            digest_matches = digest_matches and (
                canonical_sha256(observation) == decision.observation_sha256
            )
            sequence_matches = sequence_matches and all(
                (
                    decision.sequence == expected_sequence,
                    observation.get("sequence") == expected_sequence,
                    observation.get("simulation_time_us")
                    == decision.simulation_time_us,
                )
            )
            manifest_matches = manifest_matches and (
                decision.manifest_sha256 == leg.manifest.sha256()
            )
    observation_contract_fields = {item.name for item in fields(AlgorithmObservation)}
    contract_clean = not observation_contract_fields.intersection(
        _FORBIDDEN_OBSERVATION_KEYS
    )
    passed = all(
        (
            decision_count > 0,
            representation_values == {"ALGORITHM_CLIENT_OBSERVATION"},
            not forbidden_fields,
            digest_matches,
            sequence_matches,
            manifest_matches,
            contract_clean,
        )
    )
    return passed, {
        "algorithm_observation_contract_fields": sorted(
            observation_contract_fields
        ),
        "decision_count": decision_count,
        "forbidden_fields_found": sorted(forbidden_fields),
        "manifest_digest_matches": manifest_matches,
        "observation_digest_matches": digest_matches,
        "representation_values": sorted(representation_values),
        "sequence_and_time_matches": sequence_matches,
    }


def _objective_quantity_conservation(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    per_leg: list[dict[str, object]] = []
    passed = True
    for index, leg in enumerate(scenario.legs, start=1):
        accepted_routes = [
            decision
            for decision in leg.decisions
            if decision.action_accepted
            and decision.resulting_route_id is not None
        ]
        risk_ok = True
        for decision in accepted_routes:
            observation = decision.observation
            action = decision.action
            risk = observation["risk_limits"]
            remaining = int(observation["remaining_quantity"])
            working = sum(
                int(item["remaining_quantity"])
                for item in observation["working_orders"]
            )
            available = (
                remaining
                if action.action_type is AlgorithmActionType.REPLACE
                else max(0, remaining - working)
            )
            risk_ok = risk_ok and all(
                (
                    action.quantity <= available,
                    action.quantity <= int(risk["maximum_child_quantity"]),
                    action.maximum_venues <= len(observation["venue_state"]),
                    int(risk["maximum_working_quantity"])
                    >= action.quantity
                    + (
                        0
                        if action.action_type is AlgorithmActionType.REPLACE
                        else working
                    ),
                )
            )
        client_filled = sum(fill.quantity for fill in leg.client_fills)
        metrics_ok = all(
            (
                leg.metrics.target_quantity == _TARGET_QUANTITY,
                leg.metrics.completed_quantity == client_filled,
                leg.metrics.fill_uncertainty_quantity
                == _TARGET_QUANTITY - client_filled,
                0 <= client_filled <= _TARGET_QUANTITY,
            )
        )
        observe_ok = (
            not leg.recording.route_ids
            and not leg.client_fills
            and all(
                decision.action.action_type is AlgorithmActionType.WAIT
                for decision in leg.decisions
            )
            if scenario.objective is ObjectiveType.OBSERVE_ONLY
            else bool(accepted_routes)
        )
        leg_ok = risk_ok and metrics_ok and observe_ok
        passed = passed and leg_ok
        per_leg.append(
            {
                "accepted_route_count": len(accepted_routes),
                "client_filled_quantity": client_filled,
                "leg_sequence": index,
                "metrics_reconciled": metrics_ok,
                "risk_limits_reconciled": risk_ok,
                "side": leg.objective.side.value,
                "status": "PASS" if leg_ok else "FAIL",
            }
        )
    configured_target = int(
        scenario.execution_mapping["configured_target_quantity"]
    )
    objective_shape_ok = (
        configured_target == 0
        and len(scenario.legs) == 1
        if scenario.objective is ObjectiveType.OBSERVE_ONLY
        else configured_target == _TARGET_QUANTITY
        and tuple(leg.objective.side for leg in scenario.legs)
        == _SIDES_BY_OBJECTIVE[scenario.objective]
    )
    winner_refused = _WINNER_DECLARATION["status"] == "NOT_DECLARED"
    passed = passed and objective_shape_ok and winner_refused
    return passed, {
        "configured_objective": scenario.objective.value,
        "configured_target_quantity": configured_target,
        "legs": per_leg,
        "objective_shape_reconciled": objective_shape_ok,
        "winner_declaration": _WINNER_DECLARATION,
    }


def _client_venue_fill_reconciliation(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    per_leg: list[dict[str, object]] = []
    passed = True
    for index, leg in enumerate(scenario.legs, start=1):
        client = Counter(_client_fill_key(fill.as_dict()) for fill in leg.client_fills)
        truth = Counter(_truth_player_fill_keys(leg.recording))
        client_position = leg.final_signed_position
        venue_position = int(
            leg.recording.expected_ground_truth["global_player_position"]
        )
        scored_quantity = sum(
            int(score["completed_quantity"])
            for score in leg.recording.expected_scores.values()
        )
        filled_quantity = sum(fill.quantity for fill in leg.client_fills)
        leg_ok = all(
            (
                client == truth,
                client_position == venue_position,
                scored_quantity == filled_quantity,
            )
        )
        passed = passed and leg_ok
        per_leg.append(
            {
                "client_fill_count": sum(client.values()),
                "client_position": client_position,
                "fill_ledger_sha256": canonical_sha256(
                    sorted((list(key), value) for key, value in client.items())
                ),
                "filled_quantity": filled_quantity,
                "leg_sequence": index,
                "route_score_completed_quantity": scored_quantity,
                "status": "PASS" if leg_ok else "FAIL",
                "truth_fill_count": sum(truth.values()),
                "venue_position": venue_position,
            }
        )
    aggregate_client = sum(leg.final_signed_position for leg in scenario.legs)
    aggregate_venue = sum(
        int(leg.recording.expected_ground_truth["global_player_position"])
        for leg in scenario.legs
    )
    passed = passed and aggregate_client == aggregate_venue
    return passed, {
        "aggregate_client_position": aggregate_client,
        "aggregate_venue_position": aggregate_venue,
        "legs": per_leg,
    }


def _control_fork_identity(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    fork_digests = {leg.fork_state_sha256 for leg in scenario.legs}
    background_digests = {leg.background_path_sha256 for leg in scenario.legs}
    observe_control_match = all(
        leg.recording.expected_state_sha256 == leg.control_final_state_sha256
        for leg in scenario.legs
    ) if scenario.objective is ObjectiveType.OBSERVE_ONLY else True
    passed = all(
        (
            len(fork_digests) == 1,
            len(background_digests) == 1,
            all(leg.seed == scenario.configuration.seed for leg in scenario.legs),
            observe_control_match,
            _WINNER_DECLARATION["winner"] is None,
        )
    )
    return passed, {
        "background_path_sha256": sorted(background_digests),
        "fork_state_sha256": sorted(fork_digests),
        "leg_count": len(scenario.legs),
        "observe_only_matches_control_final_state": observe_control_match,
        "seed": scenario.configuration.seed,
        "winner_declaration": _WINNER_DECLARATION,
    }


def _native_recording_replay(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    evidence: list[dict[str, object]] = []
    passed = True
    for index, leg in enumerate(scenario.legs, start=1):
        replay = replay_multivenue_recording(
            MultiVenueRecording.from_dict(leg.recording.as_dict())
        )
        passed = passed and replay.passed
        evidence.append(
            {
                "events_match": replay.events_match,
                "feed_match": replay.feed_match,
                "ground_truth_match": replay.ground_truth_match,
                "leg_sequence": index,
                "recording_sha256": leg.recording.sha256(),
                "scores_match": replay.scores_match,
                "state_match": replay.state_match,
            }
        )
    return passed, {"legs": evidence, "native_recording_count": len(evidence)}


def _client_fill_key(fill: Mapping[str, object]) -> tuple[object, ...]:
    return (
        fill["venue_id"],
        fill["trade_id"],
        fill["order_id"],
        fill["side"],
        fill["price_x2"],
        fill["quantity"],
    )


def _truth_player_fill_keys(
    recording: MultiVenueRecording,
) -> tuple[tuple[object, ...], ...]:
    fills: list[tuple[object, ...]] = []
    for venue in recording.expected_ground_truth["venues"]:
        venue_id = str(venue["venue_id"])
        for event in venue["state"]["events"]:
            if event["event_type"] != "TRADE":
                continue
            data = event["data"]
            for role in ("maker", "taker"):
                if data[f"{role}_owner"] != "player":
                    continue
                fills.append(
                    (
                        venue_id,
                        data["trade_id"],
                        data[f"{role}_order_id"],
                        data[f"{role}_side"],
                        data["price_x2"],
                        data["quantity"],
                    )
                )
    return tuple(fills)


def _event_projection(scenario: _Scenario) -> tuple[dict[str, object], ...]:
    projected: list[dict[str, object]] = []
    for leg_sequence, leg in enumerate(scenario.legs, start=1):
        projected.extend(
            {
                "leg_sequence": leg_sequence,
                "record_type": "algorithm_decision",
                **decision.as_dict(),
            }
            for decision in leg.decisions
        )
        projected.extend(
            {
                "leg_sequence": leg_sequence,
                "record_type": "coordinator_event",
                **event,
            }
            for event in leg.recording.expected_events
        )
    return tuple(projected)


def _observable_projection(scenario: _Scenario) -> dict[str, object]:
    return {
        "configured_objective": scenario.objective.value,
        "configured_target_quantity": scenario.execution_mapping[
            "configured_target_quantity"
        ],
        "legs": [
            {
                "actions": [
                    {
                        **decision.action.as_dict(),
                        "accepted": decision.action_accepted,
                        "rejection_reason": decision.rejection_reason,
                        "resulting_route_id": decision.resulting_route_id,
                        "sequence": decision.sequence,
                    }
                    for decision in leg.decisions
                ],
                "observations": [
                    decision.observation for decision in leg.decisions
                ],
                "side": leg.objective.side.value,
            }
            for leg in scenario.legs
        ],
        "representation": "ALGORITHM_CLIENT_AUDIT_PROJECTION",
        "strategy": scenario.configuration.strategy,
        "winner_declaration": _WINNER_DECLARATION,
    }


def _forbidden_keys(value: object) -> list[str]:
    found: set[str] = set()

    def visit(item: object, path: str) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).lower()
                next_path = f"{path}.{key}" if path else str(key)
                if normalized in _FORBIDDEN_OBSERVATION_KEYS:
                    found.add(next_path)
                visit(nested, next_path)
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value, "")
    return sorted(found)


def _check(
    name: str,
    passed: bool,
    evidence: dict[str, object],
) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        required=True,
        detail=(
            f"real execution-algorithm check passed: {name}"
            if passed
            else f"real execution-algorithm check failed: {name}"
        ),
        evidence={"source": "AlgorithmExecutor", **evidence},
    )
