"""Real synthetic-agent ecology executor for generated audit cases."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.agents.ecology import AgentEcology, EcologyRunResult
from kirby2.agents.models import AgentInformationSet, AgentIntentType
from kirby2.agents.populations import (
    BOUNDED_POPULATION_TEMPLATES,
    compose_bounded_population,
)
from kirby2.agents.replay import (
    EcologyRecording,
    replay_agent_ecology,
)
from kirby2.immutable import thaw_json

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


ECOLOGY_RECORDING_TYPE = "NATIVE_ECOLOGY_RECORDING"
_RECORDING_FIELDS = frozenset(
    {
        "configuration",
        "different_seed_probe",
        "native_recording",
    }
)
_ALTERNATE_SEED_MASK = 0x9E3779B97F4A7C15
_FORBIDDEN_OBSERVABLE_KEYS = frozenset(
    {
        "account_id",
        "agent_id",
        "agent_ids",
        "family",
        "future",
        "future_decision",
        "future_decisions",
        "intent",
        "latent_value",
        "latent_value_ticks",
        "owner",
        "policy",
        "rationale",
        "reserve_price",
        "reserve_price_ticks",
    }
)


@dataclass(frozen=True, slots=True)
class _Scenario:
    ecology: AgentEcology
    result: EcologyRunResult


class EcologyExecutor:
    """Exercise generated population and count axes on the production ecology."""

    lane = ExecutorLane.ECOLOGY

    def execute(
        self,
        configuration: GeneratedConfiguration,
    ) -> GeneratedCaseResult:
        self._require_configuration(configuration)
        definition = compose_bounded_population(
            configuration.agent_population,
            configuration.agent_count,
            duration_us=configuration.duration_us,
        )
        scenario = _run_scenario(definition, configuration.seed)
        native = EcologyRecording.from_dict(
            EcologyRecording.capture(scenario.result).as_dict()
        )
        different_seed_probe = _different_seed_probe(
            definition,
            configuration.seed,
        )
        recording = CaseRecording(
            lane=self.lane,
            recording_type=ECOLOGY_RECORDING_TYPE,
            payload={
                "configuration": configuration.as_dict(),
                "different_seed_probe": different_seed_probe,
                "native_recording": native.as_dict(),
            },
        )
        return _result(
            configuration,
            recording,
            native,
            scenario,
            different_seed_probe,
            replay_mismatches=(),
        )

    def replay(self, recording: CaseRecording) -> GeneratedCaseResult:
        if not isinstance(recording, CaseRecording):
            raise TypeError("ecology replay requires CaseRecording")
        if recording.lane is not self.lane:
            raise ValueError("ecology replay received a different lane")
        if recording.recording_type != ECOLOGY_RECORDING_TYPE:
            raise ValueError("unsupported ecology recording type")
        payload = thaw_json(recording.payload)
        if not isinstance(payload, dict):
            raise TypeError("ecology recording payload must be an object")
        if set(payload) != _RECORDING_FIELDS:
            raise ValueError("ecology recording fields are not exact")
        raw_configuration = payload["configuration"]
        raw_native = payload["native_recording"]
        raw_probe = payload["different_seed_probe"]
        if not isinstance(raw_configuration, dict):
            raise TypeError("ecology configuration must be an object")
        if not isinstance(raw_native, dict):
            raise TypeError("native ecology recording must be an object")
        if not isinstance(raw_probe, dict):
            raise TypeError("different-seed probe must be an object")
        configuration = GeneratedConfiguration.from_dict(raw_configuration)
        self._require_configuration(configuration)
        native = EcologyRecording.from_dict(raw_native)
        native_replay = replay_agent_ecology(native)
        scenario = _run_scenario(native.definition, native.seed)
        recomputed_probe = _different_seed_probe(
            native.definition,
            native.seed,
        )
        mismatches: list[str] = []
        for name, matches in {
            "native_replay": native_replay.passed,
            "native_population": (
                native.population_id == configuration.agent_population
            ),
            "native_agent_count": (
                len(native.definition.agents) == configuration.agent_count
            ),
            "native_duration": (
                native.definition.duration_us == configuration.duration_us
            ),
            "native_seed": native.seed == configuration.seed,
            "reconstructed_result": (
                scenario.result.result_sha256
                == native_replay.result.result_sha256
            ),
            "different_seed_probe": recomputed_probe == raw_probe,
        }.items():
            if not matches:
                mismatches.append(name)
        return _result(
            configuration,
            recording,
            native,
            scenario,
            dict(raw_probe),
            replay_mismatches=tuple(mismatches),
        )

    def _require_configuration(
        self,
        configuration: GeneratedConfiguration,
    ) -> None:
        if not isinstance(configuration, GeneratedConfiguration):
            raise TypeError("ecology executor requires GeneratedConfiguration")
        if configuration.lane is not self.lane:
            raise ValueError("ecology executor received a different lane")
        if configuration.agent_population not in BOUNDED_POPULATION_TEMPLATES:
            raise ValueError("unsupported generated agent population")
        if not 1 <= configuration.agent_count <= 8:
            raise ValueError("ecology executor supports one through eight agents")


def _run_scenario(definition, seed: int) -> _Scenario:
    ecology = AgentEcology(definition, seed)
    result = ecology.run()
    return _Scenario(ecology, result)


def _different_seed_probe(definition, seed: int) -> dict[str, object]:
    alternate_seed = seed ^ _ALTERNATE_SEED_MASK
    result = AgentEcology(definition, alternate_seed).run()
    return {
        "public_event_sha256": result.summary.public_event_sha256,
        "result_sha256": result.result_sha256,
        "seed": alternate_seed,
        "state_sha256": result.summary.state_sha256,
        "truth_event_sha256": result.summary.truth_event_sha256,
    }


def _result(
    configuration: GeneratedConfiguration,
    recording: CaseRecording,
    native: EcologyRecording,
    scenario: _Scenario,
    different_seed_probe: dict[str, object],
    *,
    replay_mismatches: tuple[str, ...],
) -> GeneratedCaseResult:
    scenario.ecology.assert_invariants()
    observable_projection = scenario.result.public_player_record()
    exercises = _exercises(configuration, recording, scenario)
    checks = _checks(
        configuration,
        native,
        scenario,
        different_seed_probe,
        observable_projection,
    )
    failures = [
        FailureObservation(
            kind=(
                FailureKind.OBSERVABILITY_LEAK
                if check.name == "observable_projection_boundary"
                else FailureKind.INVARIANT_VIOLATION
            ),
            code=f"ECOLOGY_{check.name.upper()}",
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
            code=f"ECOLOGY_{exercise.capability.upper()}_NOT_EXERCISED",
            message=(
                "configured agent-ecology dimension was not exercised: "
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
                code="ECOLOGY_REPLAY_MISMATCH",
                message="native ecology recording did not replay exactly",
                evidence={"mismatches": list(replay_mismatches)},
            )
        )
    result = scenario.result
    return GeneratedCaseResult(
        configuration=configuration,
        lane=ExecutorLane.ECOLOGY,
        recording=recording,
        event_projection=_event_projection(scenario),
        final_state_projection={
            "agent_runtime": _agent_runtime_projection(scenario.ecology),
            "complete": scenario.ecology.complete,
            "definition_sha256": result.definition.sha256(),
            "engine_state_sha256": scenario.ecology.engine.state_sha256(),
            "ground_truth": result.post_session_analysis,
            "result_sha256": result.result_sha256,
            "state_sha256": result.summary.state_sha256,
            "summary": result.summary.as_dict(),
        },
        metrics={
            "accepted_action_count": result.summary.accepted_action_count,
            "action_count": result.summary.action_count,
            "agent_count": result.summary.agent_count,
            "decision_count": sum(
                int(agent.runtime_state()["decision_count"])
                for agent in scenario.ecology.agents.values()
            ),
            "ending_displayed_depth": result.summary.ending_displayed_depth,
            "public_event_count": len(result.public_events),
            "rejected_action_count": result.summary.rejected_action_count,
            "simulation_duration_us": result.summary.duration_us,
            "trade_count": result.summary.trade_count,
            "traded_volume_shares": result.summary.traded_volume,
            "truth_event_count": len(result.truth_events),
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
    definition = scenario.result.definition
    actual_counts = Counter(agent.family for agent in definition.agents)
    template = BOUNDED_POPULATION_TEMPLATES[configuration.agent_population]
    expected_counts = Counter(
        template[index % len(template)]
        for index in range(configuration.agent_count)
    )
    runtime = {
        agent_id: agent.runtime_state()
        for agent_id, agent in sorted(scenario.ecology.agents.items())
    }
    decisions = {
        agent_id: int(state["decision_count"])
        for agent_id, state in runtime.items()
    }
    actor_summaries = scenario.result.post_session_analysis["actor_summaries"]
    population_exercised = all(
        (
            definition.population_id == configuration.agent_population,
            actual_counts == expected_counts,
            bool(runtime),
            all(count > 0 for count in decisions.values()),
        )
    )
    count_exercised = all(
        (
            len(definition.agents) == configuration.agent_count,
            len(scenario.ecology.agents) == configuration.agent_count,
            len(actor_summaries) == configuration.agent_count,
            len(set(scenario.ecology.agents)) == configuration.agent_count,
        )
    )
    common = {
        "executor": type(scenario.ecology).__name__,
        "recording_sha256": recording.sha256,
    }
    return (
        ExerciseRecord(
            ExecutorLane.ECOLOGY,
            "agent_population",
            configuration.agent_population,
            (
                ExerciseStatus.EXERCISED
                if population_exercised
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "actual_family_counts": {
                    family.value: count
                    for family, count in sorted(
                        actual_counts.items(),
                        key=lambda item: item[0].value,
                    )
                },
                "decision_counts": decisions,
                "expected_family_counts": {
                    family.value: count
                    for family, count in sorted(
                        expected_counts.items(),
                        key=lambda item: item[0].value,
                    )
                },
                "truth_action_count": len(scenario.result.truth_events),
            },
        ),
        ExerciseRecord(
            ExecutorLane.ECOLOGY,
            "agent_count",
            configuration.agent_count,
            (
                ExerciseStatus.EXERCISED
                if count_exercised
                else ExerciseStatus.NOT_EXERCISED
            ),
            {
                **common,
                "actor_summary_count": len(actor_summaries),
                "agent_ids": sorted(scenario.ecology.agents),
                "configured_count": configuration.agent_count,
                "runtime_agent_count": len(runtime),
            },
        ),
    )


def _checks(
    configuration: GeneratedConfiguration,
    native: EcologyRecording,
    scenario: _Scenario,
    different_seed_probe: dict[str, object],
    observable_projection: dict[str, object],
) -> tuple[CheckResult, ...]:
    bounds_ok, bounds_evidence = _agent_risk_bounds(scenario)
    inventory_ok, inventory_evidence = _inventory_reconciliation(scenario)
    leaked = _forbidden_observable_keys(observable_projection)
    rng_ok, rng_evidence = _owned_rng_determinism(
        configuration,
        native,
        scenario,
        different_seed_probe,
    )
    time_ok, time_evidence = _monotonic_event_time(scenario)
    return (
        _check("agent_risk_bounds", bounds_ok, bounds_evidence),
        _check(
            "agent_inventory_reconciliation",
            inventory_ok,
            inventory_evidence,
        ),
        _check(
            "observable_projection_boundary",
            not leaked,
            {
                "forbidden_fields_found": leaked,
                "information_boundary": observable_projection[
                    "information_boundary"
                ],
                "observable_projection_sha256": canonical_sha256(
                    observable_projection
                ),
            },
        ),
        _check("owned_rng_determinism", rng_ok, rng_evidence),
        _check("monotonic_event_time", time_ok, time_evidence),
    )


def _agent_risk_bounds(scenario: _Scenario) -> tuple[bool, dict[str, object]]:
    per_agent: dict[str, object] = {}
    passed = True
    for agent_id, agent in sorted(scenario.ecology.agents.items()):
        agent.assert_invariants(scenario.ecology.engine.clock.current_time_us)
        bounds = agent.spec.bounds
        state = agent.runtime_state()
        actions = [
            event
            for event in scenario.result.truth_events
            if event.agent_id == agent_id
        ]
        accepted_submits = [
            event
            for event in actions
            if event.status.value == "ACCEPTED"
            and event.intent.intent_type is AgentIntentType.SUBMIT
        ]
        accepted_quantity = sum(
            int(event.intent.quantity) for event in accepted_submits
        )
        quantities = [
            int(event.intent.quantity)
            for event in actions
            if event.intent.intent_type is AgentIntentType.SUBMIT
        ]
        action_times = [int(value) for value in state["action_times_us"]]
        max_observed_rate = max(
            (
                sum(
                    time_us - 1_000_000 < candidate <= time_us
                    for candidate in action_times
                )
                for time_us in action_times
            ),
            default=0,
        )
        working_quantity = sum(
            int(item["remaining_quantity"]) for item in state["own_orders"]
        )
        information_ok = (
            bounds.information_set
            is AgentInformationSet.PUBLIC_MARKET_AND_OWN_STATE
            and agent.spec.policy.latent_value_ticks is None
            and agent.spec.policy.reserve_price_ticks is None
        )
        lifetime_ok = all(
            bounds.lifetime_start_us <= value <= bounds.lifetime_end_us
            for value in action_times
        ) and all(
            bounds.lifetime_start_us
            <= event.decision_time_us
            <= event.arrival_time_us
            <= bounds.lifetime_end_us
            for event in actions
        )
        latency_ok = all(
            event.arrival_time_us - event.decision_time_us == bounds.latency_us
            for event in actions
        )
        agent_ok = all(
            (
                accepted_quantity == int(state["accepted_quantity"]),
                accepted_quantity <= bounds.quantity_budget,
                all(0 < quantity <= bounds.max_order_quantity for quantity in quantities),
                max_observed_rate <= bounds.max_orders_per_second,
                working_quantity <= bounds.max_working_quantity,
                abs(agent.inventory) <= bounds.max_abs_inventory,
                int(state["remaining_budget"])
                == bounds.quantity_budget - accepted_quantity,
                lifetime_ok,
                latency_ok,
                information_ok,
            )
        )
        passed = passed and agent_ok
        per_agent[agent_id] = {
            "accepted_quantity": accepted_quantity,
            "action_count": len(actions),
            "information_set": bounds.information_set.value,
            "inventory": agent.inventory,
            "latency_reconciled": latency_ok,
            "lifetime_reconciled": lifetime_ok,
            "max_observed_rolling_rate": max_observed_rate,
            "max_orders_per_second": bounds.max_orders_per_second,
            "remaining_budget": state["remaining_budget"],
            "status": "PASS" if agent_ok else "FAIL",
            "working_quantity": working_quantity,
        }
    return passed, {"agent_count": len(per_agent), "agents": per_agent}


def _inventory_reconciliation(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    actor_summaries = {
        str(item["agent_id"]): item
        for item in scenario.result.post_session_analysis["actor_summaries"]
    }
    per_agent: dict[str, object] = {}
    passed = True
    for agent_id, agent in sorted(scenario.ecology.agents.items()):
        orders = [
            order
            for order in scenario.ecology.engine.orders
            if order.request.account_id == agent_id
        ]
        signed_fill_inventory = sum(
            order.request.side.sign * order.filled_quantity for order in orders
        )
        filled_quantity = sum(order.filled_quantity for order in orders)
        actor = actor_summaries[agent_id]
        agent_ok = all(
            (
                signed_fill_inventory == agent.inventory,
                signed_fill_inventory == int(actor["final_inventory"]),
                filled_quantity == int(actor["filled_quantity"]),
                all(
                    order.request.quantity
                    == order.filled_quantity
                    + order.cancelled_quantity
                    + order.expired_quantity
                    + order.remaining_quantity
                    for order in orders
                ),
            )
        )
        passed = passed and agent_ok
        per_agent[agent_id] = {
            "actor_final_inventory": actor["final_inventory"],
            "filled_quantity": filled_quantity,
            "managed_order_count": len(orders),
            "runtime_inventory": agent.inventory,
            "signed_fill_inventory": signed_fill_inventory,
            "status": "PASS" if agent_ok else "FAIL",
        }
    return passed, {"agent_count": len(per_agent), "agents": per_agent}


def _owned_rng_determinism(
    configuration: GeneratedConfiguration,
    native: EcologyRecording,
    scenario: _Scenario,
    different_seed_probe: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    rng_states = {
        agent_id: state["rng"]
        for agent_id, state in _raw_agent_states(scenario.ecology).items()
    }
    rng_digests = {
        agent_id: canonical_sha256(state)
        for agent_id, state in rng_states.items()
    }
    alternate_seed = int(different_seed_probe["seed"])
    state_changed = (
        different_seed_probe["state_sha256"]
        != scenario.result.summary.state_sha256
    )
    behavior_changed = any(
        different_seed_probe[name] != getattr(scenario.result.summary, name)
        for name in ("public_event_sha256", "truth_event_sha256")
    )
    passed = all(
        (
            native.seed == configuration.seed == scenario.result.seed,
            native.expected_result_sha256 == scenario.result.result_sha256,
            alternate_seed == (configuration.seed ^ _ALTERNATE_SEED_MASK),
            alternate_seed != configuration.seed,
            len(rng_digests) == configuration.agent_count,
            len(set(rng_digests.values())) == len(rng_digests),
            state_changed,
        )
    )
    return passed, {
        "alternate_behavior_changed": behavior_changed,
        "alternate_public_event_sha256": different_seed_probe[
            "public_event_sha256"
        ],
        "alternate_seed": alternate_seed,
        "alternate_state_sha256": different_seed_probe["state_sha256"],
        "alternate_truth_event_sha256": different_seed_probe[
            "truth_event_sha256"
        ],
        "owned_agent_rng_sha256": rng_digests,
        "primary_public_event_sha256": scenario.result.summary.public_event_sha256,
        "primary_seed": configuration.seed,
        "primary_state_sha256": scenario.result.summary.state_sha256,
        "primary_truth_event_sha256": scenario.result.summary.truth_event_sha256,
        "state_changed": state_changed,
    }


def _monotonic_event_time(
    scenario: _Scenario,
) -> tuple[bool, dict[str, object]]:
    public = scenario.result.public_events
    truth = scenario.result.truth_events
    mechanics = scenario.ecology.engine.events
    public_times = [event.simulation_time_us for event in public]
    arrival_times = [event.arrival_time_us for event in truth]
    mechanics_times = [event.simulation_time_us for event in mechanics]
    per_agent_decisions = {
        agent_id: [
            event.decision_time_us
            for event in truth
            if event.agent_id == agent_id
        ]
        for agent_id in sorted(scenario.ecology.agents)
    }
    sequence_ok = all(
        (
            [event.sequence for event in public]
            == list(range(1, len(public) + 1)),
            [event.sequence for event in truth]
            == list(range(1, len(truth) + 1)),
            [event.sequence for event in mechanics]
            == list(range(1, len(mechanics) + 1)),
        )
    )
    causal = all(
        event.decision_time_us <= event.arrival_time_us
        <= scenario.result.definition.duration_us
        for event in truth
    )
    passed = all(
        (
            sequence_ok,
            public_times == sorted(public_times),
            arrival_times == sorted(arrival_times),
            mechanics_times == sorted(mechanics_times),
            all(times == sorted(times) for times in per_agent_decisions.values()),
            causal,
        )
    )
    return passed, {
        "agent_decision_ranges": {
            agent_id: (
                None
                if not times
                else {"count": len(times), "first_us": times[0], "last_us": times[-1]}
            )
            for agent_id, times in per_agent_decisions.items()
        },
        "causal_decision_arrival_pairs": causal,
        "mechanics_event_count": len(mechanics),
        "public_event_count": len(public),
        "sequence_contiguity": sequence_ok,
        "truth_event_count": len(truth),
    }


def _raw_agent_states(ecology: AgentEcology) -> dict[str, dict[str, object]]:
    return {
        agent_id: agent.runtime_state()
        for agent_id, agent in sorted(ecology.agents.items())
    }


def _agent_runtime_projection(ecology: AgentEcology) -> dict[str, object]:
    projection: dict[str, object] = {}
    for agent_id, state in _raw_agent_states(ecology).items():
        projection[agent_id] = {
            "accepted_quantity": state["accepted_quantity"],
            "action_times_us": state["action_times_us"],
            "decision_count": state["decision_count"],
            "inventory": state["inventory"],
            "own_orders": state["own_orders"],
            "remaining_budget": state["remaining_budget"],
            "rng_state_sha256": canonical_sha256(state["rng"]),
            "spec": state["spec"],
        }
    return projection


def _event_projection(scenario: _Scenario) -> tuple[dict[str, object], ...]:
    projected = [
        {"record_type": "public_ecology_event", **event.as_dict()}
        for event in scenario.result.public_events
    ]
    projected.extend(
        {"record_type": "agent_truth_event", **event.as_dict()}
        for event in scenario.result.truth_events
    )
    projected.extend(
        {"record_type": "mechanics_event", **event.as_dict()}
        for event in scenario.ecology.engine.events
    )
    return tuple(projected)


def _forbidden_observable_keys(value: object) -> list[str]:
    found: set[str] = set()

    def visit(item: object, path: str) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).lower()
                next_path = f"{path}.{key}" if path else str(key)
                if normalized in _FORBIDDEN_OBSERVABLE_KEYS:
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
            f"real agent-ecology check passed: {name}"
            if passed
            else f"real agent-ecology check failed: {name}"
        ),
        evidence={"source": "EcologyExecutor", **evidence},
    )
