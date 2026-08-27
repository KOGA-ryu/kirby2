"""Runtime acceptance audit for bounded causal synthetic participant ecologies."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, fields

from kirby2.agents import (
    ADVERSARIAL_DRILL_IDS,
    POPULATION_IDS,
    AgentEcology,
    AgentFamily,
    EcologyRecording,
    MarketAgent,
    compose_population,
    get_population,
    replay_agent_ecology,
)
from kirby2.agents.models import AgentIntent, AgentIntentType, AgentObservation
from kirby2.exchange import OrderType, Side
from kirby2.multivenue.models import canonical_sha256


@dataclass(frozen=True, slots=True)
class AgentEcologyAuditCase:
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


def audit_agent_ecology() -> tuple[AgentEcologyAuditCase, ...]:
    names = (*POPULATION_IDS, *ADVERSARIAL_DRILL_IDS)
    ecologies: dict[str, AgentEcology] = {}
    results = {}
    for name in names:
        ecology = AgentEcology(get_population(name), 42)
        ecologies[name] = ecology
        results[name] = ecology.run()
    return (
        _interface_case(ecologies),
        _family_case(ecologies),
        _bounds_case(ecologies),
        _composition_case(results),
        _determinism_and_replay_case(results),
        _different_seed_case(results),
        _deceptive_safety_case(results),
        _information_case(ecologies),
        _drills_case(results),
        _truth_boundary_case(results),
        _ordinary_exchange_case(ecologies, results),
        _record_tamper_case(results),
    )


def _interface_case(ecologies) -> AgentEcologyAuditCase:
    signature = inspect.signature(MarketAgent.decide)
    parameters = list(signature.parameters)
    agent_fields = {
        key
        for ecology in ecologies.values()
        for agent in ecology.agents.values()
        for key in vars(agent)
    }
    forbidden_state = sorted(
        key for key in agent_fields if "engine" in key or key in {"book", "truth_log"}
    )
    failures: list[str] = []
    if parameters != ["self", "observation"]:
        failures.append("MarketAgent decision interface accepts more than one observation")
    if forbidden_state:
        failures.append("an agent retained a direct exchange or truth reference")
    return AgentEcologyAuditCase(
        "causal_market_agent_observation_to_ordinary_intent_interface",
        {
            "decision_parameters": parameters,
            "forbidden_agent_state": forbidden_state,
            "interface": "MarketAgent",
        },
        tuple(failures),
    )


def _family_case(ecologies) -> AgentEcologyAuditCase:
    required = {
        "NOISE_TRADER",
        "PASSIVE_MARKET_MAKER",
        "INVENTORY_SENSITIVE_MARKET_MAKER",
        "MOMENTUM_TRADER",
        "MEAN_REVERSION_TRADER",
        "SCHEDULED_METAORDER",
        "DISTRESSED_LIQUIDATOR",
        "LIQUIDITY_WITHDRAWER",
        "LATENT_VALUE_TRADER",
        "AUCTION_PARTICIPANT",
        "DECEPTIVE_DISPLAY",
    }
    instantiated = {
        agent.spec.family.value
        for ecology in ecologies.values()
        for agent in ecology.agents.values()
    }
    failures = () if instantiated == required else (
        "canonical agent-family inventory was not fully instantiated",
    )
    return AgentEcologyAuditCase(
        "all_initial_agent_families",
        {"families": sorted(instantiated), "family_count": len(instantiated)},
        failures,
    )


def _bounds_case(ecologies) -> AgentEcologyAuditCase:
    failures: list[str] = []
    max_observed_rate = 0
    for ecology in ecologies.values():
        for agent in ecology.agents.values():
            try:
                agent.assert_invariants(ecology.engine.clock.current_time_us)
            except RuntimeError as error:
                failures.append(str(error))
            state = agent.runtime_state()
            times = list(state["action_times_us"])
            for time_us in times:
                rate = sum(
                    time_us - 1_000_000 < candidate <= time_us
                    for candidate in times
                )
                max_observed_rate = max(max_observed_rate, rate)
            bounds = agent.spec.bounds
            if abs(agent.inventory) > bounds.max_abs_inventory:
                failures.append(f"{agent.spec.agent_id} exceeded inventory")
            if agent.remaining_budget < 0:
                failures.append(f"{agent.spec.agent_id} exceeded quantity budget")
            if any(
                not bounds.lifetime_start_us <= time <= bounds.lifetime_end_us
                for time in times
            ):
                failures.append(f"{agent.spec.agent_id} acted outside lifetime")
        probe_agent = next(iter(ecology.agents.values()))
        over_limit = AgentIntent(
            AgentIntentType.SUBMIT,
            "AUDIT_OVERSIZE_PROBE",
            order_type=OrderType.MARKET,
            side=Side.BUY,
            quantity=probe_agent.spec.bounds.max_order_quantity + 1,
        )
        rejection = ecology._gateway_rejection(  # noqa: SLF001 - invariant audit probe
            probe_agent,
            over_limit,
            probe_agent.spec.bounds.lifetime_start_us,
        )
        if rejection != "MAX_AGENT_ORDER_QUANTITY":
            failures.append("gateway did not refuse an oversize agent order")
    return AgentEcologyAuditCase(
        "capital_quantity_inventory_rate_risk_information_latency_lifetime_bounds",
        {
            "agent_instance_count": sum(len(value.agents) for value in ecologies.values()),
            "max_observed_rolling_action_rate": max_observed_rate,
            "oversize_gateway_refusal": not any(
                "oversize" in failure for failure in failures
            ),
        },
        tuple(failures),
    )


def _composition_case(results) -> AgentEcologyAuditCase:
    selected = [results[name] for name in POPULATION_IDS]
    starting = {item.summary.starting_book_sha256 for item in selected}
    metrics = {item.definition.population_id: item.summary.ecology_metrics() for item in selected}
    pairwise_changes: dict[str, int] = {}
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            count = sum(
                left.summary.ecology_metrics()[key]
                != right.summary.ecology_metrics()[key]
                for key in left.summary.ecology_metrics()
            )
            pairwise_changes[f"{left.definition.population_id}:{right.definition.population_id}"] = count
    failures: list[str] = []
    if len(starting) != 1:
        failures.append("population comparison did not use an identical starting book")
    if any(count < 3 for count in pairwise_changes.values()):
        failures.append("participant composition did not materially change ecology outcomes")
    if len({item.summary.state_sha256 for item in selected}) != len(selected):
        failures.append("different participant compositions converged to identical states")
    return AgentEcologyAuditCase(
        "participant_composition_materially_changes_order_flow_ecology",
        {
            "outcome_metrics": metrics,
            "pairwise_changed_metric_counts": pairwise_changes,
            "starting_book_sha256": next(iter(starting)) if starting else None,
        },
        tuple(failures),
    )


def _determinism_and_replay_case(results) -> AgentEcologyAuditCase:
    failures: list[str] = []
    evidence = {}
    for name, result in results.items():
        recording = EcologyRecording.capture(result)
        replay = replay_agent_ecology(
            EcologyRecording.from_dict(recording.as_dict())
        )
        evidence[name] = {
            "result_sha256": result.result_sha256,
            "replay_status": "PASS" if replay.passed else "FAIL",
        }
        if not replay.passed:
            failures.append(f"{name} failed exact ecology replay")
    return AgentEcologyAuditCase(
        "owned_seeded_rng_and_exact_population_replay",
        evidence,
        tuple(failures),
    )


def _different_seed_case(results) -> AgentEcologyAuditCase:
    baseline = results[POPULATION_IDS[0]]
    different = AgentEcology(get_population(POPULATION_IDS[0]), 43).run()
    failures = () if (
        baseline.result_sha256 != different.result_sha256
        and baseline.summary.starting_book_sha256
        == different.summary.starting_book_sha256
    ) else ("different agent seed failed to change the ecology path",)
    return AgentEcologyAuditCase(
        "different_seed_different_path_same_start",
        {
            "seed_42_result_sha256": baseline.result_sha256,
            "seed_43_result_sha256": different.result_sha256,
            "starting_book_equal": (
                baseline.summary.starting_book_sha256
                == different.summary.starting_book_sha256
            ),
        },
        failures,
    )


def _deceptive_safety_case(results) -> AgentEcologyAuditCase:
    generic_refused = False
    try:
        compose_population("unsafe", {AgentFamily.DECEPTIVE_DISPLAY: 1})
    except ValueError:
        generic_refused = True
    deceptive_definitions = [
        get_population(name)
        for name in ADVERSARIAL_DRILL_IDS
        if any(
            agent.family is AgentFamily.DECEPTIVE_DISPLAY
            for agent in get_population(name).agents
        )
    ]
    redacted = all(
        agent["policy"] == "REDACTED_SIMULATOR_RECOGNITION_POLICY"
        for definition in deceptive_definitions
        for agent in definition.public_manifest()["agents"]
        if agent["family"] == AgentFamily.DECEPTIVE_DISPLAY.value
    )
    public_logs = [
        results[definition.population_id].public_player_record()
        for definition in deceptive_definitions
    ]
    public_text = repr(public_logs)
    failures: list[str] = []
    if not generic_refused:
        failures.append("deceptive display was available to generic population composition")
    if not redacted:
        failures.append("deceptive-display parameters were exported")
    if "DECEPTIVE_DISPLAY" in public_text or "RECOGNITION-DISPLAY" in public_text:
        failures.append("player record exposed deceptive actor identity")
    if any(not definition.recognition_drill for definition in deceptive_definitions):
        failures.append("deceptive display escaped recognition-drill scope")
    return AgentEcologyAuditCase(
        "deceptive_display_simulator_only_nonexportable_recognition_scope",
        {
            "canonical_recognition_drills": [
                item.population_id for item in deceptive_definitions
            ],
            "generic_composition_refused": generic_refused,
            "policy_export": "REDACTED",
            "venue_scope": "KIRBY2_SYNTHETIC_ONLY",
        },
        tuple(failures),
    )


def _information_case(ecologies) -> AgentEcologyAuditCase:
    controlled = []
    violations = []
    for ecology in ecologies.values():
        for agent in ecology.agents.values():
            info = agent.spec.bounds.information_set.value
            if info == "CONTROLLED_LATENT_VALUE":
                controlled.append(agent.spec.agent_id)
                if agent.spec.family is not AgentFamily.LATENT_VALUE_TRADER:
                    violations.append(agent.spec.agent_id)
            elif agent.spec.policy.latent_value_ticks is not None:
                violations.append(agent.spec.agent_id)
    observation_fields = {item.name for item in fields(AgentObservation)}
    failures = tuple(
        ["non-latent agent received controlled latent information"] if violations else []
    )
    return AgentEcologyAuditCase(
        "bounded_information_sets_and_explicit_controlled_latent_actor",
        {
            "controlled_latent_actor_ids": sorted(set(controlled)),
            "observation_excludes_future_truth_and_actor_registry": all(
                token not in observation_fields for token in ("future", "truth", "agents")
            ),
            "violations": violations,
        },
        failures,
    )


def _drills_case(results) -> AgentEcologyAuditCase:
    evidence = {}
    failures: list[str] = []
    for name in ADVERSARIAL_DRILL_IDS:
        result = results[name]
        event_types = {item.event_type for item in result.public_events}
        families = {item.family.value for item in result.truth_events}
        evidence[name] = {
            "action_count": result.summary.action_count,
            "families": sorted(families),
            "public_event_types": sorted(event_types),
            "trade_count": result.summary.trade_count,
        }
        if not result.definition.recognition_drill or not result.truth_events:
            failures.append(f"{name} did not execute as a recognition drill")
    if "AUCTION_UNCROSS" not in {
        item.event_type for item in results["auction_imbalance_reversal"].public_events
    }:
        failures.append("auction imbalance drill omitted a genuine auction uncross")
    halt_types = {
        item.event_type for item in results["halt_disorderly_reopen"].public_events
    }
    if not {"HALT", "RESUME", "AUCTION_UNCROSS"} <= halt_types:
        failures.append("halt/reopen drill omitted halt, resume, or reopening uncross")
    reserve_actions = [
        item
        for item in results["absorption_hidden_reserve"].truth_events
        if item.intent.rationale == "CONTROLLED_RESERVE_REPLENISHMENT"
        and item.status.value == "ACCEPTED"
    ]
    if len(reserve_actions) < 2:
        failures.append("absorption drill did not causally replenish displayed slices")
    return AgentEcologyAuditCase(
        "eight_adversarial_execution_recognition_drills",
        evidence,
        tuple(failures),
    )


def _truth_boundary_case(results) -> AgentEcologyAuditCase:
    pre_completion_blocked = False
    ecology = AgentEcology(get_population("liquidity_provision"), 99)
    try:
        ecology.post_session_ground_truth()
    except RuntimeError:
        pre_completion_blocked = True
    forbidden = {"agent_id", "family", "rationale", "account_id", "owner", "intent"}
    leaked = {
        name: sorted(_all_keys(result.public_player_record()) & forbidden)
        for name, result in results.items()
    }
    failures: list[str] = []
    if not pre_completion_blocked:
        failures.append("agent ground truth was accessible before completion")
    if any(leaked.values()):
        failures.append("player record exposed agent identity or intent")
    if any(
        result.post_session_analysis["label"]
        != "SIMULATOR_GROUND_TRUTH_POST_SESSION"
        for result in results.values()
    ):
        failures.append("post-session attribution lacks the ground-truth label")
    return AgentEcologyAuditCase(
        "hidden_identity_and_intent_with_post_session_actor_attribution",
        {
            "leaked_keys_by_population": leaked,
            "post_session_labels": sorted(
                {result.post_session_analysis["label"] for result in results.values()}
            ),
            "pre_completion_reveal_blocked": pre_completion_blocked,
        },
        tuple(failures),
    )


def _ordinary_exchange_case(ecologies, results) -> AgentEcologyAuditCase:
    failures: list[str] = []
    intent_types = set()
    price_event_types = set()
    for result in results.values():
        intent_types.update(
            item.intent.intent_type.value for item in result.truth_events
        )
        price_event_types.update(item.event_type for item in result.public_events)
    for ecology in ecologies.values():
        if any("book" in key or "engine" in key for agent in ecology.agents.values() for key in vars(agent)):
            failures.append("agent retained a direct price or queue mutation object")
    if intent_types - {"SUBMIT", "CANCEL"}:
        failures.append("agent emitted a non-exchange mutation action")
    if any(value in price_event_types for value in {"PRICE_SET", "QUEUE_SET", "REGIME_PRICE_MOVE"}):
        failures.append("scenario directly assigned a price or queue")
    return AgentEcologyAuditCase(
        "prices_and_queues_change_only_through_exchange_orders_and_matching",
        {
            "agent_intent_types": sorted(intent_types),
            "forbidden_direct_mutation_events": sorted(
                price_event_types & {"PRICE_SET", "QUEUE_SET", "REGIME_PRICE_MOVE"}
            ),
            "mechanics_state_sha256": {
                name: ecology.engine.state_sha256() for name, ecology in ecologies.items()
            },
        },
        tuple(failures),
    )


def _record_tamper_case(results) -> AgentEcologyAuditCase:
    recording = EcologyRecording.capture(results[POPULATION_IDS[0]])
    payload = recording.as_dict()
    payload["population_definition_sha256"] = "0" * 64
    refused = False
    try:
        EcologyRecording.from_dict(payload)
    except ValueError:
        refused = True
    failures = () if refused else ("forged population digest was accepted",)
    return AgentEcologyAuditCase(
        "canonical_population_provenance_and_tamper_refusal",
        {
            "definition_sha256": recording.population_definition_sha256,
            "forged_definition_digest_refused": refused,
            "record_sha256": canonical_sha256(recording.as_dict()),
        },
        failures,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for item in value.values() for key in _all_keys(item)
        }
    if isinstance(value, (list, tuple)):
        return {key for item in value for key in _all_keys(item)}
    return set()
