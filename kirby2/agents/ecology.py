"""Deterministic scheduler and exchange gateway for interacting synthetic agents."""

from __future__ import annotations

import hashlib
import heapq
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from kirby2.exchange import (
    AdvancedOrderRequest,
    InstrumentRules,
    ManagedOrder,
    MarketMechanicsEngine,
    MechanicsEvent,
    MechanicsEventType,
    OrderInstruction,
    OrderOwner,
    OrderType,
    SelfTradePreventionMode,
    SessionState,
    Side,
)
from kirby2.multivenue.models import canonical_sha256
from kirby2.simulation.clock import SimulationClock

from .base import MarketAgent
from .families import (
    create_agent_with_seed,
    derive_agent_seed,
    restore_agent_runtime_state,
)
from .models import (
    AGENT_ECOLOGY_SCHEMA_VERSION,
    SYNTHETIC_VENUE_ID,
    AgentActionStatus,
    AgentFamily,
    AgentIntent,
    AgentIntentType,
    AgentObservation,
    AgentSpec,
    AgentTruthEvent,
    OwnOrderView,
    PopulationDefinition,
    PublicEcologyEvent,
    PublicTradeView,
)


AGENT_SCHEDULER_COMPONENT_ID = "AGENT_SCHEDULER_V1"
AGENT_SCHEDULER_CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(order=True, frozen=True, slots=True)
class ScheduledAgentIntent:
    arrival_time_us: int
    sequence: int
    decision_time_us: int = field(compare=False)
    agent_id: str = field(compare=False)
    intent: AgentIntent = field(compare=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "arrival_time_us": self.arrival_time_us,
            "decision_time_us": self.decision_time_us,
            "intent": self.intent.as_dict(),
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ScheduledAgentIntent:
        expected = {
            "agent_id",
            "arrival_time_us",
            "decision_time_us",
            "intent",
            "sequence",
        }
        if set(payload) != expected:
            raise ValueError("scheduled-agent-intent fields are not exact")
        raw_intent = payload["intent"]
        if not isinstance(raw_intent, Mapping):
            raise TypeError("scheduled agent intent payload must be an object")
        values = {
            name: payload[name]
            for name in ("arrival_time_us", "decision_time_us", "sequence")
        }
        if any(type(value) is not int for value in values.values()):
            raise TypeError("scheduled agent intent times and sequence must be integers")
        if (
            values["decision_time_us"] < 0
            or values["arrival_time_us"] < values["decision_time_us"]
            or values["sequence"] <= 0
        ):
            raise ValueError("scheduled agent intent ordering fields are invalid")
        agent_id = payload["agent_id"]
        if type(agent_id) is not str or not agent_id:
            raise ValueError("scheduled agent intent requires an agent ID")
        return cls(
            arrival_time_us=values["arrival_time_us"],
            sequence=values["sequence"],
            decision_time_us=values["decision_time_us"],
            agent_id=agent_id,
            intent=AgentIntent.from_dict(raw_intent),
        )


@dataclass(frozen=True, slots=True)
class AgentSchedulerWorkResult:
    work_id: str
    simulation_time_us: int
    work_kind: str
    scheduled_intents: tuple[ScheduledAgentIntent, ...] = ()
    truth_events: tuple[AgentTruthEvent, ...] = ()

    def __post_init__(self) -> None:
        if type(self.work_id) is not str or not self.work_id:
            raise ValueError("scheduler work result requires a work ID")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("scheduler work-result time must be nonnegative")
        if self.work_kind not in {"PENDING_VENUE_ARRIVAL", "PARTICIPANT_DECISION"}:
            raise ValueError("scheduler work-result kind is unsupported")
        if self.work_kind == "PENDING_VENUE_ARRIVAL" and self.scheduled_intents:
            raise ValueError("arrival work cannot report newly scheduled intents")
        if self.work_kind == "PARTICIPANT_DECISION" and self.truth_events:
            raise ValueError("decision work cannot report delivered truth events")


@dataclass(frozen=True, slots=True)
class EcologySummary:
    population_id: str
    seed: int
    duration_us: int
    agent_count: int
    action_count: int
    accepted_action_count: int
    rejected_action_count: int
    trade_count: int
    traded_volume: int
    price_change_count: int
    low_trade_price_ticks: int | None
    high_trade_price_ticks: int | None
    last_trade_price_ticks: int | None
    ending_best_bid_ticks: int | None
    ending_best_ask_ticks: int | None
    ending_displayed_depth: int
    family_action_counts: dict[str, int]
    starting_book_sha256: str
    state_sha256: str
    public_event_sha256: str
    truth_event_sha256: str
    invariant_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_action_count": self.accepted_action_count,
            "action_count": self.action_count,
            "agent_count": self.agent_count,
            "duration_us": self.duration_us,
            "ending_best_ask_ticks": self.ending_best_ask_ticks,
            "ending_best_bid_ticks": self.ending_best_bid_ticks,
            "ending_displayed_depth": self.ending_displayed_depth,
            "family_action_counts": dict(sorted(self.family_action_counts.items())),
            "high_trade_price_ticks": self.high_trade_price_ticks,
            "invariant_status": self.invariant_status,
            "last_trade_price_ticks": self.last_trade_price_ticks,
            "low_trade_price_ticks": self.low_trade_price_ticks,
            "population_id": self.population_id,
            "price_change_count": self.price_change_count,
            "public_event_sha256": self.public_event_sha256,
            "rejected_action_count": self.rejected_action_count,
            "seed": self.seed,
            "starting_book_sha256": self.starting_book_sha256,
            "state_sha256": self.state_sha256,
            "trade_count": self.trade_count,
            "traded_volume": self.traded_volume,
            "truth_event_sha256": self.truth_event_sha256,
        }

    def ecology_metrics(self) -> dict[str, object]:
        """Outcome-only metrics used to compare participant compositions."""

        return {
            "ending_best_ask_ticks": self.ending_best_ask_ticks,
            "ending_best_bid_ticks": self.ending_best_bid_ticks,
            "ending_displayed_depth": self.ending_displayed_depth,
            "high_trade_price_ticks": self.high_trade_price_ticks,
            "last_trade_price_ticks": self.last_trade_price_ticks,
            "low_trade_price_ticks": self.low_trade_price_ticks,
            "price_change_count": self.price_change_count,
            "trade_count": self.trade_count,
            "traded_volume": self.traded_volume,
        }


@dataclass(frozen=True, slots=True)
class EcologyRunResult:
    definition: PopulationDefinition
    seed: int
    summary: EcologySummary
    public_events: tuple[PublicEcologyEvent, ...]
    truth_events: tuple[AgentTruthEvent, ...]
    post_session_analysis: dict[str, object]
    result_sha256: str

    def public_player_record(self) -> dict[str, object]:
        return {
            "events": [event.as_dict() for event in self.public_events],
            "information_boundary": "AGGREGATED_MARKET_STATE_WITHOUT_ACTOR_IDENTITY_OR_INTENT",
            "population_id": "WITHHELD_DURING_SESSION",
            "summary": {
                key: value
                for key, value in self.summary.as_dict().items()
                if key
                not in {
                    "family_action_counts",
                    "population_id",
                    "state_sha256",
                    "truth_event_sha256",
                }
            },
        }


class AgentScheduler:
    """Agent-owned deterministic state injected into one authoritative engine.

    The scheduler never constructs or advances an exchange, never owns the global
    full-day event allocator, and never performs an auction uncross.  It only makes
    causal decisions and routes their ordinary submit/cancel requests through the
    injected mechanics interface when their owned latency queue becomes due.
    """

    COMPONENT_ID = AGENT_SCHEDULER_COMPONENT_ID
    CHECKPOINT_SCHEMA_VERSION = AGENT_SCHEDULER_CHECKPOINT_SCHEMA_VERSION

    def __init__(
        self,
        definition: PopulationDefinition,
        seed: int,
        *,
        engine: MarketMechanicsEngine,
        clock: SimulationClock | None = None,
        active_agent_ids: Sequence[str] | None = None,
        agent_seeds: Mapping[str, int] | None = None,
        rng_labels: Mapping[str, str] | None = None,
        order_id_allocator: Callable[[], str] | None = None,
        compatibility_mode: bool = False,
        _restored_order_agent: Mapping[str, str] | None = None,
        _restored_agent_specs: Mapping[str, AgentSpec] | None = None,
        _restored_next_decision_us: Mapping[str, int | None] | None = None,
        _defer_initial_validation: bool = False,
    ) -> None:
        if type(seed) is not int or seed < 0:
            raise ValueError("agent scheduler seed must be a nonnegative integer")
        if type(engine) is not MarketMechanicsEngine:
            raise TypeError("agent scheduler requires one MarketMechanicsEngine")
        selected_clock = engine.clock if clock is None else clock
        if type(selected_clock) is not SimulationClock or selected_clock is not engine.clock:
            raise ValueError("agent scheduler clock must be the injected engine clock")
        if type(compatibility_mode) is not bool:
            raise TypeError("agent scheduler compatibility mode must be boolean")
        if type(_defer_initial_validation) is not bool:
            raise TypeError("agent scheduler validation defer must be boolean")
        if not compatibility_mode and order_id_allocator is None:
            raise ValueError("injected agent scheduler requires the runtime order allocator")
        self.definition = definition
        self.seed = seed
        self.engine = engine
        self.clock = selected_clock
        self._compatibility_mode = compatibility_mode
        self._allocator_owner = (
            "COMPATIBILITY_WRAPPER" if compatibility_mode else "INJECTED_RUNTIME"
        )
        self._order_id_allocator = order_id_allocator
        ids = tuple(spec.agent_id for spec in definition.agents)
        seed_map = (
            {spec.agent_id: derive_agent_seed(spec, seed) for spec in definition.agents}
            if agent_seeds is None
            else dict(agent_seeds)
        )
        if set(seed_map) != set(ids) or any(
            type(value) is not int or value < 0 for value in seed_map.values()
        ):
            raise ValueError("agent substream seeds must cover every agent exactly")
        label_map = (
            {
                spec.agent_id: (
                    f"agent_ecology/{definition.population_id}/"
                    f"{spec.agent_id}/decision"
                )
                for spec in definition.agents
            }
            if rng_labels is None
            else dict(rng_labels)
        )
        if set(label_map) != set(ids) or any(
            type(value) is not str or not value for value in label_map.values()
        ):
            raise ValueError("agent RNG labels must cover every agent exactly")
        if len(set(label_map.values())) != len(label_map):
            raise ValueError("agent RNG labels must be unique")
        self._agent_seeds = dict(sorted(seed_map.items()))
        self._rng_labels = dict(sorted(label_map.items()))
        initial_specs = {spec.agent_id: spec for spec in definition.agents}
        runtime_specs = (
            initial_specs
            if _restored_agent_specs is None
            else dict(_restored_agent_specs)
        )
        if set(runtime_specs) != set(ids) or any(
            type(spec) is not AgentSpec
            or spec.agent_id != agent_id
            or spec.family is not initial_specs[agent_id].family
            for agent_id, spec in runtime_specs.items()
        ):
            raise ValueError("restored agent specifications are invalid")
        self.agents: dict[str, MarketAgent] = {
            agent_id: create_agent_with_seed(
                runtime_specs[agent_id], seed_map[agent_id]
            )
            for agent_id in ids
        }
        selected_active_ids = (
            ids if active_agent_ids is None else tuple(active_agent_ids)
        )
        active_ids = set(selected_active_ids)
        if not active_ids.issubset(ids) or len(active_ids) != len(
            selected_active_ids
        ):
            raise ValueError("active agent IDs must be unique registered agents")
        self._active: dict[str, bool] = {
            agent_id: agent_id in active_ids for agent_id in ids
        }
        if _restored_next_decision_us is None:
            self._next_decision_us = {}
            for agent_id in ids:
                bounds = runtime_specs[agent_id].bounds
                candidate = max(
                    self.clock.current_time_us,
                    bounds.lifetime_start_us,
                )
                self._next_decision_us[agent_id] = (
                    candidate
                    if self._active[agent_id]
                    and candidate + bounds.latency_us <= bounds.lifetime_end_us
                    else None
                )
        else:
            restored_next = dict(_restored_next_decision_us)
            if set(restored_next) != set(ids) or any(
                value is not None and type(value) is not int
                for value in restored_next.values()
            ):
                raise ValueError("restored next-decision inventory is invalid")
            self._next_decision_us = dict(sorted(restored_next.items()))
        self._pending: list[ScheduledAgentIntent] = []
        self._pending_sequence = 0
        self._order_sequence = 0
        self._truth_events: list[AgentTruthEvent] = []
        self._public_events: list[PublicEcologyEvent] = []
        self._public_trades: list[PublicTradeView] = []
        self._mechanics_event_cursor = 0
        self._event_sequence_offset = 0
        self._hidden_mechanics_event_sequences: set[int] = set()
        restored_order_agent = (
            {} if _restored_order_agent is None else dict(_restored_order_agent)
        )
        if any(
            type(order_id) is not str
            or not order_id
            or type(agent_id) is not str
            or agent_id not in self.agents
            for order_id, agent_id in restored_order_agent.items()
        ):
            raise ValueError("restored order-to-agent inventory is invalid")
        self._order_agent: dict[str, str] = dict(sorted(restored_order_agent.items()))
        self._complete = False
        self._starting_book_sha256 = ""
        self._reconcile_agents()
        if not _defer_initial_validation:
            self.assert_invariants()

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def public_events(self) -> tuple[PublicEcologyEvent, ...]:
        return tuple(self._public_events)

    @property
    def truth_events(self) -> tuple[AgentTruthEvent, ...]:
        if not self.complete:
            raise RuntimeError("agent identity and intent are unavailable before session completion")
        return tuple(self._truth_events)

    @property
    def next_decision_time_us(self) -> int | None:
        candidates = tuple(
            value for value in self._next_decision_us.values() if value is not None
        )
        return min(candidates) if candidates else None

    @property
    def next_pending_arrival_time_us(self) -> int | None:
        return None if not self._pending else self._pending[0].arrival_time_us

    @property
    def next_scheduled_time_us(self) -> int | None:
        candidates = tuple(
            value
            for value in (
                self.next_pending_arrival_time_us,
                self.next_decision_time_us,
            )
            if value is not None
        )
        return min(candidates) if candidates else None

    def mark_complete(self) -> None:
        if self._complete:
            raise RuntimeError("agent scheduler is already complete")
        self._complete = True
        self._append_public("SESSION_COMPLETE", {"status": "COMPLETE"})
        self._assert_owned_invariants()

    def summary(self) -> EcologySummary:
        if not self.complete:
            raise RuntimeError("ecology summary is available only after completion")
        prices = [item.price_ticks for item in self._public_trades]
        price_changes = sum(
            first != second for first, second in zip(prices, prices[1:])
        )
        book = self._aggregated_book()
        family_counts: dict[str, int] = {}
        for event in self._truth_events:
            family_counts[event.family.value] = family_counts.get(event.family.value, 0) + 1
        accepted = sum(
            event.status is AgentActionStatus.ACCEPTED for event in self._truth_events
        )
        return EcologySummary(
            self.definition.population_id,
            self.seed,
            self.definition.duration_us,
            len(self.agents),
            len(self._truth_events),
            accepted,
            len(self._truth_events) - accepted,
            len(self._public_trades),
            sum(item.quantity for item in self._public_trades),
            price_changes,
            min(prices) if prices else None,
            max(prices) if prices else None,
            prices[-1] if prices else None,
            self.engine.book.best_bid,
            self.engine.book.best_ask,
            sum(
                int(level["quantity"])
                for side in ("bids", "asks")
                for level in book[side]
            ),
            family_counts,
            self._starting_book_sha256,
            self.state_sha256(),
            canonical_sha256([item.as_dict() for item in self.public_events]),
            canonical_sha256([item.as_dict() for item in self.truth_events]),
            "PASS",
        )

    def post_session_ground_truth(self) -> dict[str, object]:
        if not self.complete:
            raise RuntimeError("agent ground truth is unavailable before completion")
        actor_summaries = []
        managed_by_id = {
            item.request.order_id: item for item in self.engine.orders
        }
        for agent_id, agent in sorted(self.agents.items()):
            actions = [item for item in self._truth_events if item.agent_id == agent_id]
            owned = [
                managed_by_id[order_id]
                for order_id, owner_id in self._order_agent.items()
                if owner_id == agent_id and order_id in managed_by_id
            ]
            actor_summaries.append(
                {
                    "accepted_action_count": sum(
                        item.status is AgentActionStatus.ACCEPTED for item in actions
                    ),
                    "action_count": len(actions),
                    "agent_id": agent_id,
                    "family": agent.spec.family.value,
                    "final_inventory": agent.inventory,
                    "filled_quantity": sum(item.filled_quantity for item in owned),
                    "remaining_quantity_budget": agent.remaining_budget,
                    "safety_class": agent.spec.safety_class.value,
                }
            )
        family_attribution: dict[str, dict[str, int]] = {}
        for actor in actor_summaries:
            family = str(actor["family"])
            aggregate = family_attribution.setdefault(
                family,
                {
                    "accepted_action_count": 0,
                    "action_count": 0,
                    "actor_count": 0,
                    "filled_quantity": 0,
                    "net_inventory": 0,
                },
            )
            aggregate["accepted_action_count"] += int(actor["accepted_action_count"])
            aggregate["action_count"] += int(actor["action_count"])
            aggregate["actor_count"] += 1
            aggregate["filled_quantity"] += int(actor["filled_quantity"])
            aggregate["net_inventory"] += int(actor["final_inventory"])
        return {
            "actor_summaries": actor_summaries,
            "agent_actions": [item.as_dict() for item in self._truth_events],
            "explanation": self.definition.post_session_explanation,
            "family_attribution": dict(sorted(family_attribution.items())),
            "label": "SIMULATOR_GROUND_TRUTH_POST_SESSION",
            "population_id": self.definition.population_id,
            "recognition_drill": self.definition.recognition_drill,
            "schema_version": AGENT_ECOLOGY_SCHEMA_VERSION,
            "venue_scope": SYNTHETIC_VENUE_ID,
        }

    def _owned_runtime_state(self) -> dict[str, object]:
        return {
            "active_agents": dict(sorted(self._active.items())),
            "agents": {
                agent_id: agent.runtime_state()
                for agent_id, agent in sorted(self.agents.items())
            },
            "agent_seeds": dict(self._agent_seeds),
            "allocator_owner": self._allocator_owner,
            "clock_us": self.clock.current_time_us,
            "complete": self.complete,
            "event_sequence_offset": self._event_sequence_offset,
            "hidden_mechanics_event_sequences": sorted(
                self._hidden_mechanics_event_sequences
            ),
            "mechanics_event_cursor": self._mechanics_event_cursor,
            "next_decision_us": dict(sorted(self._next_decision_us.items())),
            "order_agent": dict(sorted(self._order_agent.items())),
            "order_sequence": self._order_sequence,
            "pending": [item.as_dict() for item in sorted(self._pending)],
            "pending_sequence": self._pending_sequence,
            "public_events": [item.as_dict() for item in self._public_events],
            "public_trades": [item.as_dict() for item in self._public_trades],
            "rng_labels": dict(self._rng_labels),
            "starting_book_sha256": self._starting_book_sha256,
            "truth_events": [item.as_dict() for item in self._truth_events],
        }

    def checkpoint_state(
        self, *, _prevalidated_engine_state_sha256: str | None = None
    ) -> dict[str, object]:
        """Return detached, strict state for dependency-ordered fresh restore."""

        from kirby2.full_day.models import validate_strict_json

        self._assert_owned_invariants()
        engine_state_sha256 = _prevalidated_engine_state_sha256
        if engine_state_sha256 is None:
            engine_state_sha256 = _engine_checkpoint_sha256(self.engine)
        elif (
            type(engine_state_sha256) is not str
            or len(engine_state_sha256) != 64
            or any(character not in "0123456789abcdef" for character in engine_state_sha256)
        ):
            raise ValueError("prevalidated engine checkpoint digest is malformed")
        payload = {
            "component_id": self.COMPONENT_ID,
            "definition": self.definition.identity_dict(),
            "definition_sha256": self.definition.sha256(),
            "engine_state_sha256": engine_state_sha256,
            "schema_version": self.CHECKPOINT_SCHEMA_VERSION,
            "seed": self.seed,
            "state": self._owned_runtime_state(),
        }
        validate_strict_json(payload)
        return payload

    snapshot_state = checkpoint_state

    def canonical_state_bytes(self) -> bytes:
        from kirby2.full_day.models import canonical_json_bytes

        return canonical_json_bytes(self.checkpoint_state())

    def state_sha256(self) -> str:
        return canonical_sha256(self.checkpoint_state())

    @classmethod
    def _restore_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        engine: MarketMechanicsEngine,
        clock: SimulationClock | None = None,
        order_id_allocator: Callable[[], str] | None = None,
        _prevalidated_engine_state_sha256: str | None = None,
        _construction_token: object | None,
    ) -> AgentScheduler:
        from kirby2.full_day.models import canonical_json_bytes, validate_strict_json

        defer_full_day_fixed_point = _construction_token is not None
        if defer_full_day_fixed_point:
            from kirby2.full_day.runtime import _NESTED_RESTORE_CONSTRUCTION_TOKEN

            if _construction_token is not _NESTED_RESTORE_CONSTRUCTION_TOKEN:
                raise TypeError("scheduler nested-restore construction token differs")
        if not isinstance(payload, Mapping):
            raise TypeError("agent scheduler checkpoint must be an object")
        validate_strict_json(payload)
        expected = {
            "component_id",
            "definition",
            "definition_sha256",
            "engine_state_sha256",
            "schema_version",
            "seed",
            "state",
        }
        if set(payload) != expected:
            raise ValueError("agent scheduler checkpoint fields are not exact")
        if payload["component_id"] != cls.COMPONENT_ID:
            raise ValueError("agent scheduler checkpoint has the wrong component ID")
        if payload["schema_version"] != cls.CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported agent scheduler checkpoint schema")
        raw_definition = payload["definition"]
        raw_state = payload["state"]
        if not isinstance(raw_definition, Mapping) or not isinstance(raw_state, Mapping):
            raise TypeError("agent scheduler definition and state must be objects")
        definition = PopulationDefinition.from_dict(raw_definition)
        if canonical_json_bytes(definition.identity_dict()) != canonical_json_bytes(
            raw_definition
        ):
            raise ValueError("agent scheduler definition is not canonical")
        if payload["definition_sha256"] != definition.sha256():
            raise ValueError("agent scheduler definition digest does not match")
        engine_state_sha256 = _prevalidated_engine_state_sha256
        if engine_state_sha256 is None:
            engine_state_sha256 = _engine_checkpoint_sha256(engine)
        elif (
            type(engine_state_sha256) is not str
            or len(engine_state_sha256) != 64
            or any(character not in "0123456789abcdef" for character in engine_state_sha256)
        ):
            raise ValueError("prevalidated engine checkpoint digest is malformed")
        if payload["engine_state_sha256"] != engine_state_sha256:
            raise ValueError("agent scheduler checkpoint targets a different engine state")
        seed = payload["seed"]
        if type(seed) is not int or seed < 0:
            raise ValueError("agent scheduler checkpoint seed must be nonnegative")

        state_expected = {
            "active_agents",
            "agent_seeds",
            "agents",
            "allocator_owner",
            "clock_us",
            "complete",
            "event_sequence_offset",
            "hidden_mechanics_event_sequences",
            "mechanics_event_cursor",
            "next_decision_us",
            "order_agent",
            "order_sequence",
            "pending",
            "pending_sequence",
            "public_events",
            "public_trades",
            "rng_labels",
            "starting_book_sha256",
            "truth_events",
        }
        if set(raw_state) != state_expected:
            raise ValueError("agent scheduler owned-state fields are not exact")

        def object_map(name: str) -> Mapping[str, object]:
            value = raw_state[name]
            if not isinstance(value, Mapping) or any(
                type(key) is not str for key in value
            ):
                raise TypeError(f"agent scheduler {name} must be an object")
            return value

        ids = tuple(spec.agent_id for spec in definition.agents)
        active_map = object_map("active_agents")
        seeds_map = object_map("agent_seeds")
        labels_map = object_map("rng_labels")
        raw_order_agent = object_map("order_agent")
        raw_agents = object_map("agents")
        next_map = object_map("next_decision_us")
        if set(active_map) != set(ids) or any(
            type(value) is not bool for value in active_map.values()
        ):
            raise ValueError("restored agent activation inventory is invalid")
        if set(seeds_map) != set(ids) or any(
            type(value) is not int or value < 0 for value in seeds_map.values()
        ):
            raise ValueError("restored agent seed inventory is invalid")
        if set(labels_map) != set(ids) or any(
            type(value) is not str or not value for value in labels_map.values()
        ):
            raise ValueError("restored agent RNG-label inventory is invalid")
        if any(type(value) is not str for value in raw_order_agent.values()):
            raise TypeError("restored order-to-agent mapping values must be strings")
        if set(raw_agents) != set(ids) or any(
            not isinstance(value, Mapping) for value in raw_agents.values()
        ):
            raise ValueError("restored agent runtime inventory is invalid")
        if set(next_map) != set(ids) or any(
            value is not None and type(value) is not int
            for value in next_map.values()
        ):
            raise ValueError("restored next-decision inventory is invalid")
        initial_specs = {spec.agent_id: spec for spec in definition.agents}
        restored_specs: dict[str, AgentSpec] = {}
        for agent_id in ids:
            raw_agent = raw_agents[agent_id]
            assert isinstance(raw_agent, Mapping)
            raw_spec = raw_agent.get("spec")
            if not isinstance(raw_spec, Mapping):
                raise TypeError("restored agent specification must be an object")
            restored_spec = AgentSpec.from_dict(raw_spec)
            if canonical_json_bytes(restored_spec.identity_dict()) != canonical_json_bytes(
                raw_spec
            ):
                raise ValueError("restored agent specification is not canonical")
            if (
                restored_spec.agent_id != agent_id
                or restored_spec.family is not initial_specs[agent_id].family
            ):
                raise ValueError("restored agent specification changed identity or family")
            restored_specs[agent_id] = restored_spec
        allocator_owner = raw_state["allocator_owner"]
        if allocator_owner not in {"COMPATIBILITY_WRAPPER", "INJECTED_RUNTIME"}:
            raise ValueError("restored agent allocator owner is invalid")
        compatibility_mode = allocator_owner == "COMPATIBILITY_WRAPPER"
        if not compatibility_mode and order_id_allocator is None:
            raise ValueError("restoring injected scheduler requires runtime order allocator")
        scheduler = cls(
            definition,
            seed,
            engine=engine,
            clock=clock,
            active_agent_ids=tuple(
                agent_id for agent_id in ids if active_map[agent_id]
            ),
            agent_seeds={key: int(value) for key, value in seeds_map.items()},
            rng_labels={key: str(value) for key, value in labels_map.items()},
            order_id_allocator=order_id_allocator,
            compatibility_mode=compatibility_mode,
            _restored_order_agent={
                key: str(value) for key, value in raw_order_agent.items()
            },
            _restored_agent_specs=restored_specs,
            _restored_next_decision_us={
                key: value if type(value) is int else None
                for key, value in next_map.items()
            },
            _defer_initial_validation=True,
        )
        if raw_state["clock_us"] != scheduler.clock.current_time_us:
            raise ValueError("agent scheduler clock binding moved")

        scheduler.agents = {
            agent_id: restore_agent_runtime_state(
                restored_specs[agent_id],
                scheduler._agent_seeds[agent_id],
                raw_agents[agent_id],  # type: ignore[arg-type]
            )
            for agent_id in ids
        }
        scheduler._next_decision_us = {
            key: value if type(value) is int else None
            for key, value in next_map.items()
        }
        raw_pending = raw_state["pending"]
        raw_public = raw_state["public_events"]
        raw_trades = raw_state["public_trades"]
        raw_truth = raw_state["truth_events"]
        for value, label in (
            (raw_pending, "pending intents"),
            (raw_public, "public events"),
            (raw_trades, "public trades"),
            (raw_truth, "truth events"),
        ):
            if type(value) is not list or any(
                not isinstance(item, Mapping) for item in value
            ):
                raise TypeError(f"restored agent scheduler {label} must be object arrays")
        scheduler._pending = [
            ScheduledAgentIntent.from_dict(item) for item in raw_pending
        ]
        heapq.heapify(scheduler._pending)
        scheduler._public_events = [
            PublicEcologyEvent.from_dict(item) for item in raw_public
        ]
        scheduler._public_trades = [
            PublicTradeView.from_dict(item) for item in raw_trades
        ]
        scheduler._truth_events = [
            AgentTruthEvent.from_dict(item) for item in raw_truth
        ]
        scheduler._order_agent = {
            key: str(value) for key, value in raw_order_agent.items()
        }
        integer_fields = (
            "event_sequence_offset",
            "mechanics_event_cursor",
            "order_sequence",
            "pending_sequence",
        )
        if any(
            type(raw_state[name]) is not int or raw_state[name] < 0
            for name in integer_fields
        ):
            raise ValueError("restored scheduler allocators/cursors must be nonnegative")
        scheduler._event_sequence_offset = int(raw_state["event_sequence_offset"])
        hidden_sequences = raw_state["hidden_mechanics_event_sequences"]
        if type(hidden_sequences) is not list or any(
            type(value) is not int or value <= 0 for value in hidden_sequences
        ):
            raise ValueError("restored hidden mechanics-event sequence list is invalid")
        if hidden_sequences != sorted(set(hidden_sequences)):
            raise ValueError("hidden mechanics-event sequences must be sorted and unique")
        scheduler._hidden_mechanics_event_sequences = set(hidden_sequences)
        scheduler._mechanics_event_cursor = int(raw_state["mechanics_event_cursor"])
        scheduler._order_sequence = int(raw_state["order_sequence"])
        scheduler._pending_sequence = int(raw_state["pending_sequence"])
        if type(raw_state["complete"]) is not bool:
            raise TypeError("restored scheduler complete flag must be boolean")
        scheduler._complete = bool(raw_state["complete"])
        starting_book = raw_state["starting_book_sha256"]
        if type(starting_book) is not str or (
            starting_book and (
                len(starting_book) != 64
                or any(character not in "0123456789abcdef" for character in starting_book)
            )
        ):
            raise ValueError("restored starting-book digest is invalid")
        scheduler._starting_book_sha256 = starting_book
        scheduler.assert_invariants()
        if (
            not defer_full_day_fixed_point
            and canonical_json_bytes(scheduler.checkpoint_state())
            != canonical_json_bytes(payload)
        ):
            raise ValueError("agent scheduler checkpoint is not canonical")
        return scheduler

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        engine: MarketMechanicsEngine,
        clock: SimulationClock | None = None,
        order_id_allocator: Callable[[], str] | None = None,
        _prevalidated_engine_state_sha256: str | None = None,
    ) -> AgentScheduler:
        """Restore one standalone scheduler with its own canonical fixed point."""

        return cls._restore_checkpoint_state(
            payload,
            engine=engine,
            clock=clock,
            order_id_allocator=order_id_allocator,
            _prevalidated_engine_state_sha256=(
                _prevalidated_engine_state_sha256
            ),
            _construction_token=None,
        )

    @classmethod
    def _from_full_day_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        engine: MarketMechanicsEngine,
        clock: SimulationClock | None,
        order_id_allocator: Callable[[], str] | None,
        _prevalidated_engine_state_sha256: str,
        _construction_token: object,
    ) -> AgentScheduler:
        """Restore beneath FullDayRuntime's complete outer fixed point."""

        return cls._restore_checkpoint_state(
            payload,
            engine=engine,
            clock=clock,
            order_id_allocator=order_id_allocator,
            _prevalidated_engine_state_sha256=(
                _prevalidated_engine_state_sha256
            ),
            _construction_token=_construction_token,
        )

    @classmethod
    def from_canonical_state_bytes(
        cls,
        payload: bytes,
        *,
        engine: MarketMechanicsEngine,
        clock: SimulationClock | None = None,
        order_id_allocator: Callable[[], str] | None = None,
    ) -> AgentScheduler:
        from kirby2.full_day.models import parse_canonical_json_object

        return cls.from_checkpoint_state(
            parse_canonical_json_object(payload),
            engine=engine,
            clock=clock,
            order_id_allocator=order_id_allocator,
        )

    def assert_invariants(self) -> None:
        if self.clock is not self.engine.clock:
            raise RuntimeError("agent scheduler has a second clock")
        self.engine.assert_invariants()

        self._assert_owned_invariants()

    def _assert_owned_invariants(self) -> None:
        if self.clock is not self.engine.clock:
            raise RuntimeError("agent scheduler has a second clock")
        now = self.clock.current_time_us
        initial_specs = {spec.agent_id: spec for spec in self.definition.agents}
        if set(initial_specs) != set(self.agents):
            raise RuntimeError("agent runtime inventory differs from its population")
        for agent_id, agent in self.agents.items():
            agent.assert_invariants(now)
            if agent.spec.agent_id != agent_id:
                raise RuntimeError("agent policy changed its owned identity")
            initial_spec = initial_specs[agent_id]
            if (
                agent.spec.family is not initial_spec.family
                or agent.spec.bounds.lifetime_end_us > self.definition.duration_us
            ):
                raise RuntimeError("retuned agent specification changed its owned identity")
            action_times = getattr(agent, "_action_times_us", ())
            if any(type(value) is not int or value > now for value in action_times):
                raise RuntimeError("agent action history extends beyond scheduler time")
        if len(self.agents) != len(set(self.agents)):
            raise RuntimeError("agent IDs are duplicated")
        if any(agent_id not in self.agents for agent_id in self._order_agent.values()):
            raise RuntimeError("order-to-agent truth mapping references an unknown actor")
        engine_agent_orders = {
            item.request.order_id: item.request.account_id
            for item in self.engine.orders
            if item.request.account_id in self.agents
        }
        if self._order_agent != engine_agent_orders:
            raise RuntimeError("order-to-agent truth mapping is not complete")
        if set(self._active) != set(self.agents) or any(
            type(value) is not bool for value in self._active.values()
        ):
            raise RuntimeError("agent activation inventory is incomplete")
        if set(self._next_decision_us) != set(self.agents):
            raise RuntimeError("next-decision inventory is incomplete")
        if any(
            not self._active[agent_id] and time_us is not None
            for agent_id, time_us in self._next_decision_us.items()
        ):
            raise RuntimeError("inactive agent retained a scheduled decision")
        for agent_id, time_us in self._next_decision_us.items():
            if time_us is None:
                continue
            bounds = self.agents[agent_id].spec.bounds
            if (
                time_us < now
                or time_us < bounds.lifetime_start_us
                or time_us + bounds.latency_us > bounds.lifetime_end_us
            ):
                raise RuntimeError("next agent decision is outside its causal lifetime")
        if set(self._agent_seeds) != set(self.agents) or set(self._rng_labels) != set(
            self.agents
        ):
            raise RuntimeError("agent RNG substream inventory is incomplete")
        if len(set(self._rng_labels.values())) != len(self._rng_labels):
            raise RuntimeError("agent RNG labels are duplicated")
        if self._allocator_owner not in {
            "COMPATIBILITY_WRAPPER",
            "INJECTED_RUNTIME",
        }:
            raise RuntimeError("agent scheduler order allocator has no owner")
        if self._allocator_owner == "INJECTED_RUNTIME" and self._order_id_allocator is None:
            raise RuntimeError("injected runtime order allocator is unbound")
        if self._allocator_owner == "INJECTED_RUNTIME" and self._order_sequence != 0:
            raise RuntimeError("injected scheduler allocated compatibility order IDs")
        if self._allocator_owner == "INJECTED_RUNTIME" and (
            self._compatibility_mode is not False
            or
            self._event_sequence_offset != 0
            or self._hidden_mechanics_event_sequences
            or self._mechanics_event_cursor != len(self.engine.events)
            or self._starting_book_sha256 != ""
        ):
            raise RuntimeError(
                "injected scheduler retained compatibility-only projection state"
            )
        if (
            type(self._event_sequence_offset) is not int
            or self._event_sequence_offset < 0
            or self._event_sequence_offset > self._mechanics_event_cursor
        ):
            raise RuntimeError("scheduler compatibility event offset is invalid")
        if (
            self._event_sequence_offset
            != len(self._hidden_mechanics_event_sequences)
            or any(
                sequence > self._mechanics_event_cursor
                for sequence in self._hidden_mechanics_event_sequences
            )
        ):
            raise RuntimeError("scheduler hidden mechanics-event inventory is invalid")
        if not 0 <= self._mechanics_event_cursor <= len(self.engine.events):
            raise RuntimeError("scheduler mechanics-event cursor is invalid")
        truth_sequences = [item.sequence for item in self._truth_events]
        if truth_sequences != list(range(1, len(truth_sequences) + 1)):
            raise RuntimeError("agent truth sequence is not contiguous")
        public_sequences = [item.sequence for item in self._public_events]
        if public_sequences != list(range(1, len(public_sequences) + 1)):
            raise RuntimeError("public ecology sequence is not contiguous")
        if [item.simulation_time_us for item in self._public_events] != sorted(
            item.simulation_time_us for item in self._public_events
        ):
            raise RuntimeError("public ecology event time moved backward")
        if any(item.simulation_time_us > now for item in self._public_events):
            raise RuntimeError("public ecology event extends beyond scheduler time")
        if [item.simulation_time_us for item in self._public_trades] != sorted(
            item.simulation_time_us for item in self._public_trades
        ) or any(item.simulation_time_us > now for item in self._public_trades):
            raise RuntimeError("public trade history is not causal")
        forbidden = {"agent_id", "family", "rationale", "account_id", "owner", "intent"}
        if any(_contains_forbidden_key(item.as_dict(), forbidden) for item in self._public_events):
            raise RuntimeError("player-visible ecology log leaked agent identity or intent")
        if any(item.agent_id not in self.agents for item in self._truth_events):
            raise RuntimeError("agent truth event contains an unregistered identity")
        if any(item.arrival_time_us > now for item in self._truth_events):
            raise RuntimeError("agent truth event extends beyond scheduler time")
        visible_mechanics_events = tuple(
            event
            for event in self.engine.events[: self._mechanics_event_cursor]
            if event.sequence not in self._hidden_mechanics_event_sequences
        )
        for item in self._truth_events:
            if item.family is not self.agents[item.agent_id].spec.family:
                raise RuntimeError("agent truth family differs from its owned policy")
            if item.exchange_event_start is not None:
                assert item.exchange_event_end is not None
                if item.exchange_event_end > len(visible_mechanics_events) or any(
                    event.simulation_time_us != item.arrival_time_us
                    for event in visible_mechanics_events[
                        item.exchange_event_start - 1 : item.exchange_event_end
                    ]
                ):
                    raise RuntimeError(
                        "agent truth exchange range differs from its arrival frontier"
                    )
        if self._allocator_owner == "INJECTED_RUNTIME":
            mechanics_public_types = {
                "AUCTION_INDICATION",
                "AUCTION_TRADE",
                "AUCTION_UNCROSS",
                "HALT",
                "RESUME",
                "SESSION_STATE_CHANGED",
                "TRADE",
            }
            expected_market_public: list[tuple[int, str, dict[str, object]]] = []
            expected_public_trades: list[PublicTradeView] = []
            for event in self.engine.events[: self._mechanics_event_cursor]:
                if event.event_type is MechanicsEventType.TRADE:
                    data = {
                        "price_ticks": int(event.data["price_ticks"]),
                        "quantity": int(event.data["quantity"]),
                    }
                    expected_market_public.append(
                        (event.simulation_time_us, "TRADE", data)
                    )
                    expected_public_trades.append(
                        PublicTradeView(
                            event.simulation_time_us,
                            data["price_ticks"],
                            data["quantity"],
                        )
                    )
                elif event.event_type is MechanicsEventType.AUCTION_FILL:
                    data = {
                        "price_ticks": int(event.data["price_ticks"]),
                        "quantity": int(event.data["quantity"]),
                    }
                    expected_market_public.append(
                        (event.simulation_time_us, "AUCTION_TRADE", data)
                    )
                    expected_public_trades.append(
                        PublicTradeView(
                            event.simulation_time_us,
                            data["price_ticks"],
                            data["quantity"],
                        )
                    )
                elif event.event_type in {
                    MechanicsEventType.SESSION_STATE_CHANGED,
                    MechanicsEventType.HALT,
                    MechanicsEventType.RESUME,
                    MechanicsEventType.AUCTION_INDICATION,
                    MechanicsEventType.AUCTION_UNCROSS,
                }:
                    expected_market_public.append(
                        (
                            event.simulation_time_us,
                            event.event_type.value,
                            _market_safe_mechanics_data(event),
                        )
                    )
            observed_market_public = [
                (item.simulation_time_us, item.event_type, dict(item.data))
                for item in self._public_events
                if item.event_type in mechanics_public_types
            ]
            if observed_market_public != expected_market_public:
                raise RuntimeError(
                    "public market projection differs from mechanics history"
                )
            if self._public_trades != expected_public_trades:
                raise RuntimeError("public trades differ from mechanics history")
        for pending in self._pending:
            if pending.agent_id not in self.agents:
                raise RuntimeError("pending agent intent is invalid")
            bounds = self.agents[pending.agent_id].spec.bounds
            if (
                not self._active[pending.agent_id]
                or pending.arrival_time_us < now
                or pending.decision_time_us < bounds.lifetime_start_us
                or pending.arrival_time_us
                != pending.decision_time_us + bounds.latency_us
                or pending.arrival_time_us > bounds.lifetime_end_us
            ):
                raise RuntimeError("pending agent intent is invalid")
        pending_sequences = [item.sequence for item in self._pending]
        if len(pending_sequences) != len(set(pending_sequences)) or any(
            not 1 <= sequence <= self._pending_sequence
            for sequence in pending_sequences
        ):
            raise RuntimeError("pending agent intent sequence is invalid")
        if self._pending_sequence < len(self._pending):
            raise RuntimeError("pending agent allocator moved backward")
        if self.complete and (
            self._pending
            or any(value is not None for value in self._next_decision_us.values())
        ):
            raise RuntimeError("completed scheduler retained due participant work")
        self._assert_inventory_reconciliation()

    def decide_due(self, simulation_time_us: int) -> tuple[ScheduledAgentIntent, ...]:
        self._require_current_time(simulation_time_us)
        if self.complete:
            raise RuntimeError("completed agent scheduler cannot make decisions")
        pending_start = self._pending_sequence
        due = sorted(
            agent_id
            for agent_id, time_us in self._next_decision_us.items()
            if time_us == simulation_time_us
        )
        if not due:
            return ()
        observations = {
            agent_id: self._observation(self.agents[agent_id]) for agent_id in due
        }
        for agent_id in due:
            agent = self.agents[agent_id]
            intents = agent.decide(observations[agent_id])
            for intent in intents:
                self._pending_sequence += 1
                heapq.heappush(
                    self._pending,
                    ScheduledAgentIntent(
                        simulation_time_us + agent.spec.bounds.latency_us,
                        self._pending_sequence,
                        simulation_time_us,
                        agent_id,
                        intent,
                    ),
                )
            next_time = simulation_time_us + agent.spec.bounds.decision_interval_us
            self._next_decision_us[agent_id] = (
                next_time
                if next_time + agent.spec.bounds.latency_us
                <= agent.spec.bounds.lifetime_end_us
                else None
            )
        return tuple(
            item
            for item in sorted(self._pending)
            if item.sequence > pending_start
        )

    def deliver_due(self, simulation_time_us: int) -> tuple[AgentTruthEvent, ...]:
        self._require_current_time(simulation_time_us)
        if self.complete:
            raise RuntimeError("completed agent scheduler cannot deliver orders")
        truth_start = len(self._truth_events)
        while self._pending and self._pending[0].arrival_time_us == simulation_time_us:
            pending = heapq.heappop(self._pending)
            self._deliver(pending)
            self._reconcile_agents()
        return tuple(self._truth_events[truth_start:])

    def execute_due_work(self, work: object) -> AgentSchedulerWorkResult:
        """Execute one runtime-dequeued scheduler stage without moving its clock."""

        from kirby2.full_day.events import ScheduledWorkKeyV1, WorkStageV1

        if type(work) is not ScheduledWorkKeyV1:
            raise TypeError("agent scheduler work must use ScheduledWorkKeyV1")
        if work.source_component_id != self.COMPONENT_ID:
            raise ValueError("agent scheduler cannot execute another component's work")
        self._require_current_time(work.simulation_time_us)
        if work.stage_ordinal is WorkStageV1.PENDING_VENUE_ARRIVAL:
            return AgentSchedulerWorkResult(
                work_id=work.work_id,
                simulation_time_us=work.simulation_time_us,
                work_kind="PENDING_VENUE_ARRIVAL",
                truth_events=self.deliver_due(work.simulation_time_us),
            )
        if work.stage_ordinal is WorkStageV1.ENDOGENOUS_PARTICIPANT_DECISION:
            return AgentSchedulerWorkResult(
                work_id=work.work_id,
                simulation_time_us=work.simulation_time_us,
                work_kind="PARTICIPANT_DECISION",
                scheduled_intents=self.decide_due(work.simulation_time_us),
            )
        raise ValueError("agent scheduler work must use arrival or decision stage")

    def activate_agent(self, agent_id: str, *, simulation_time_us: int) -> None:
        self._require_current_time(simulation_time_us)
        agent = self._agent(agent_id)
        if self._active[agent_id]:
            raise ValueError(f"agent {agent_id} is already active")
        bounds = agent.spec.bounds
        if not bounds.lifetime_start_us <= simulation_time_us <= bounds.lifetime_end_us:
            raise ValueError("agent activation lies outside its bounded lifetime")
        self._active[agent_id] = True
        first_decision = max(simulation_time_us, bounds.lifetime_start_us)
        self._next_decision_us[agent_id] = (
            first_decision
            if first_decision + bounds.latency_us <= bounds.lifetime_end_us
            else None
        )
        self._assert_owned_invariants()

    def deactivate_agent(
        self,
        agent_id: str,
        *,
        simulation_time_us: int,
        cancel_working: bool = True,
    ) -> tuple[str, ...]:
        self._require_current_time(simulation_time_us)
        agent = self._agent(agent_id)
        if not self._active[agent_id]:
            raise ValueError(f"agent {agent_id} is already inactive")
        if type(cancel_working) is not bool:
            raise TypeError("deactivation cancel_working must be boolean")
        self._active[agent_id] = False
        self._next_decision_us[agent_id] = None
        self._pending = [item for item in self._pending if item.agent_id != agent_id]
        heapq.heapify(self._pending)
        cancelled: list[str] = []
        if cancel_working:
            before = self._aggregated_book()
            for own_order in agent.own_orders:
                if self.engine.cancel(
                    own_order.order_id,
                    reason="SYNTHETIC_AGENT_DEACTIVATED",
                ):
                    cancelled.append(own_order.order_id)
            self._capture_public_changes(before)
        self._reconcile_agents()
        self._assert_owned_invariants()
        return tuple(cancelled)

    def retune_agent(
        self,
        agent_id: str,
        replacement_spec: object,
        *,
        simulation_time_us: int,
    ) -> None:
        """Replace one policy without changing its identity, RNG stream, or fills."""

        self._require_current_time(simulation_time_us)
        current = self._agent(agent_id)
        if type(replacement_spec) is not AgentSpec:
            raise TypeError("agent retune requires AgentSpec")
        if replacement_spec.agent_id != agent_id:
            raise ValueError("agent retune cannot change participant identity")
        if replacement_spec.family is not current.spec.family:
            raise ValueError("agent retune cannot change the policy family")
        if any(item.agent_id == agent_id for item in self._pending):
            raise ValueError("agent retune requires an empty pending-decision queue")
        state = current.runtime_state()
        accepted_quantity = int(state["accepted_quantity"])
        if accepted_quantity > replacement_spec.bounds.quantity_budget:
            raise ValueError("replacement budget is smaller than accepted quantity")
        state["spec"] = replacement_spec.identity_dict()
        state["remaining_budget"] = (
            replacement_spec.bounds.quantity_budget - accepted_quantity
        )
        self.agents[agent_id] = restore_agent_runtime_state(
            replacement_spec,
            self._agent_seeds[agent_id],
            state,
        )
        if self._active[agent_id]:
            bounds = replacement_spec.bounds
            current_next = self._next_decision_us[agent_id]
            candidate = max(
                simulation_time_us,
                bounds.lifetime_start_us,
                simulation_time_us if current_next is None else current_next,
            )
            self._next_decision_us[agent_id] = (
                candidate
                if candidate + bounds.latency_us <= bounds.lifetime_end_us
                else None
            )
        self._assert_owned_invariants()

    def _require_current_time(self, simulation_time_us: int) -> None:
        if type(simulation_time_us) is not int or simulation_time_us < 0:
            raise ValueError("agent scheduler time must be nonnegative microseconds")
        if self.clock is not self.engine.clock:
            raise RuntimeError("agent scheduler lost its authoritative clock binding")
        if simulation_time_us != self.clock.current_time_us:
            raise ValueError("runtime must advance the authoritative clock before scheduler work")

    def _agent(self, agent_id: str) -> MarketAgent:
        if type(agent_id) is not str:
            raise TypeError("agent ID must be a string")
        try:
            return self.agents[agent_id]
        except KeyError as error:
            raise KeyError(f"unknown scheduler agent: {agent_id}") from error

    def _deliver(self, pending: ScheduledAgentIntent) -> None:
        agent = self.agents[pending.agent_id]
        rejection = self._gateway_rejection(agent, pending.intent, pending.arrival_time_us)
        if rejection not in {"MAX_AGENT_ORDER_RATE", "OUTSIDE_BOUNDED_LIFETIME"}:
            agent.register_action(pending.arrival_time_us)
        order_id: str | None = None
        exchange_start = len(self.engine.events) - self._event_sequence_offset
        before = self._aggregated_book()
        accepted = False
        reason = rejection or "ACCEPTED"
        if rejection is None and pending.intent.intent_type is AgentIntentType.CANCEL:
            target = str(pending.intent.cancel_target_order_id)
            order_id = target
            accepted = self.engine.cancel(target, reason="SYNTHETIC_AGENT_CANCEL")
            reason = "CANCELLED" if accepted else "CANCEL_REJECTED"
        elif rejection is None:
            order_id = self._next_order_id()
            self._order_agent[order_id] = pending.agent_id
            request = self._request(order_id, pending.agent_id, pending.intent)
            managed = self.engine.submit(request)
            accepted = managed is not None and managed.status != "REJECTED"
            if accepted:
                agent.register_accepted_quantity(int(pending.intent.quantity))
                reason = managed.status
            else:
                reason = "EXCHANGE_REJECTED"
        exchange_end = len(self.engine.events) - self._event_sequence_offset
        self._capture_public_changes(before)
        self._truth_events.append(
            AgentTruthEvent(
                len(self._truth_events) + 1,
                pending.decision_time_us,
                pending.arrival_time_us,
                pending.agent_id,
                agent.spec.family,
                pending.intent,
                (
                    AgentActionStatus.ACCEPTED
                    if accepted
                    else AgentActionStatus.REJECTED
                ),
                reason,
                order_id,
                exchange_start + 1 if exchange_end > exchange_start else None,
                exchange_end if exchange_end > exchange_start else None,
            )
        )

    def _gateway_rejection(
        self,
        agent: MarketAgent,
        intent: AgentIntent,
        arrival_time_us: int,
    ) -> str | None:
        bounds = agent.spec.bounds
        if not bounds.lifetime_start_us <= arrival_time_us <= bounds.lifetime_end_us:
            return "OUTSIDE_BOUNDED_LIFETIME"
        if not agent.can_register_action(arrival_time_us):
            return "MAX_AGENT_ORDER_RATE"
        if intent.intent_type is AgentIntentType.CANCEL:
            if intent.cancel_target_order_id not in {
                item.order_id for item in agent.own_orders
            }:
                return "CANCEL_NOT_OWN_ACTIVE_ORDER"
            return None
        quantity = int(intent.quantity)
        if quantity > bounds.max_order_quantity:
            return "MAX_AGENT_ORDER_QUANTITY"
        if quantity > agent.remaining_budget:
            return "AGENT_QUANTITY_BUDGET"
        working = sum(item.remaining_quantity for item in agent.own_orders)
        if working + quantity > bounds.max_working_quantity:
            return "MAX_AGENT_WORKING_QUANTITY"
        buys = sum(
            item.remaining_quantity for item in agent.own_orders if item.side is Side.BUY
        )
        sells = sum(
            item.remaining_quantity for item in agent.own_orders if item.side is Side.SELL
        )
        if intent.side is Side.BUY:
            buys += quantity
        else:
            sells += quantity
        if (
            agent.inventory + buys > bounds.max_abs_inventory
            or agent.inventory - sells < -bounds.max_abs_inventory
        ):
            return "MAX_AGENT_INVENTORY_RISK"
        if intent.price_ticks is not None:
            midpoint_x2 = self._observation(agent).midpoint_x2
            reference = (
                self.definition.initial_mid_ticks
                if midpoint_x2 is None
                else midpoint_x2 // 2
            )
            if abs(intent.price_ticks - reference) > bounds.max_price_distance_ticks:
                return "MAX_AGENT_PRICE_RISK"
        if intent.auction_only and agent.spec.family is not AgentFamily.AUCTION_PARTICIPANT:
            return "AUCTION_ACCESS_RESTRICTED_TO_AUCTION_PARTICIPANT"
        return None

    def _request(
        self,
        order_id: str,
        agent_id: str,
        intent: AgentIntent,
    ) -> AdvancedOrderRequest:
        return AdvancedOrderRequest(
            order_id=order_id,
            side=intent.side,  # type: ignore[arg-type]
            quantity=int(intent.quantity),
            instruction=(
                OrderInstruction.MARKET
                if intent.order_type is OrderType.MARKET
                else OrderInstruction.LIMIT
            ),
            owner=OrderOwner.SIMULATED,
            account_id=agent_id,
            price_ticks=intent.price_ticks,
            time_in_force=OrderInstruction.DAY,
            auction_only=intent.auction_only,
        )

    def _next_order_id(self) -> str:
        if self._allocator_owner == "COMPATIBILITY_WRAPPER":
            self._order_sequence += 1
            order_id = f"SIM-O-{self._order_sequence:08d}"
        else:
            allocator = self._order_id_allocator
            if allocator is None:
                raise RuntimeError("runtime order allocator is not bound")
            order_id = allocator()
        if type(order_id) is not str or not order_id or not order_id.isascii():
            raise ValueError("runtime order allocator returned an invalid order ID")
        if order_id in self.engine.book.all_orders or order_id in {
            item.request.order_id for item in self.engine.orders
        }:
            raise ValueError("runtime order allocator returned a duplicate order ID")
        return order_id

    def _observation(self, agent: MarketAgent) -> AgentObservation:
        book = self._aggregated_book()
        indication = (
            self.engine.auction_indication().as_dict()
            if self.engine.session_state
            in {
                SessionState.PREOPEN,
                SessionState.OPENING_AUCTION,
                SessionState.REOPENING_AUCTION,
                SessionState.CLOSING_AUCTION,
            }
            else None
        )
        return AgentObservation(
            self.engine.clock.current_time_us,
            self.engine.session_state,
            tuple(
                (int(item["price_ticks"]), int(item["quantity"]))
                for item in book["bids"]
            ),
            tuple(
                (int(item["price_ticks"]), int(item["quantity"]))
                for item in book["asks"]
            ),
            tuple(self._public_trades[-16:]),
            agent.inventory,
            agent.remaining_budget,
            agent.own_orders,
            indication,
        )

    def _reconcile_agents(self) -> None:
        managed_by_id = {
            item.request.order_id: item for item in self.engine.orders
        }
        active_statuses = {"ACTIVE", "PARTIALLY_FILLED", "AUCTION_WORKING"}
        if self._allocator_owner == "INJECTED_RUNTIME":
            active_statuses.add("WORKING")
        for agent_id, agent in self.agents.items():
            owned = [
                managed_by_id[order_id]
                for order_id, owner_id in self._order_agent.items()
                if owner_id == agent_id and order_id in managed_by_id
            ]
            inventory = sum(
                item.request.side.sign * item.filled_quantity for item in owned
            )
            active = tuple(
                OwnOrderView(
                    item.request.order_id,
                    item.request.side,
                    item.request.price_ticks,
                    item.remaining_quantity,
                    item.request.auction_only,
                )
                for item in sorted(owned, key=lambda value: value.arrival_sequence)
                if item.remaining_quantity > 0
                and item.status in active_statuses
            )
            agent.reconcile(inventory, active)

    def _capture_public_changes(self, before_book: dict[str, object] | None) -> None:
        new_events = self.engine.events[self._mechanics_event_cursor :]
        if not new_events:
            return
        for event in new_events:
            if event.sequence not in self._hidden_mechanics_event_sequences:
                self._capture_mechanics_event(event)
        self._mechanics_event_cursor = len(self.engine.events)
        after = self._aggregated_book()
        if before_book is None or before_book != after:
            self._append_public(
                "BOOK_SNAPSHOT",
                {
                    "asks": after["asks"],
                    "bids": after["bids"],
                    "representation": "AGGREGATED_DISPLAYED_DEPTH",
                },
            )

    def market_snapshot(self) -> dict[str, list[dict[str, int]]]:
        """Return the current aggregate used to detect external book changes."""

        return self._aggregated_book()

    def synchronize_external_mechanics(
        self, before_book: dict[str, object] | None
    ) -> None:
        """Project mechanics caused by the sole full-day runtime owner."""

        if self._allocator_owner != "INJECTED_RUNTIME":
            raise RuntimeError(
                "external mechanics synchronization requires an injected scheduler"
            )
        self._capture_public_changes(before_book)
        self._reconcile_agents()
        self._assert_owned_invariants()

    def _aggregated_book(self) -> dict[str, list[dict[str, int]]]:
        return {
            "asks": [
                {
                    "price_ticks": price,
                    "quantity": self.engine.book.asks[price].total_quantity,
                }
                for price in self.engine.book.ask_prices
            ],
            "bids": [
                {
                    "price_ticks": price,
                    "quantity": self.engine.book.bids[price].total_quantity,
                }
                for price in self.engine.book.bid_prices
            ],
        }

    def _capture_mechanics_event(self, event: MechanicsEvent) -> None:
        if event.event_type is MechanicsEventType.TRADE:
            trade = PublicTradeView(
                event.simulation_time_us,
                int(event.data["price_ticks"]),
                int(event.data["quantity"]),
            )
            self._public_trades.append(trade)
            self._append_public(
                "TRADE",
                {"price_ticks": trade.price_ticks, "quantity": trade.quantity},
                simulation_time_us=event.simulation_time_us,
            )
        elif event.event_type is MechanicsEventType.AUCTION_FILL:
            trade = PublicTradeView(
                event.simulation_time_us,
                int(event.data["price_ticks"]),
                int(event.data["quantity"]),
            )
            self._public_trades.append(trade)
            self._append_public(
                "AUCTION_TRADE",
                {"price_ticks": trade.price_ticks, "quantity": trade.quantity},
                simulation_time_us=event.simulation_time_us,
            )
        elif event.event_type in {
            MechanicsEventType.SESSION_STATE_CHANGED,
            MechanicsEventType.HALT,
            MechanicsEventType.RESUME,
            MechanicsEventType.AUCTION_INDICATION,
            MechanicsEventType.AUCTION_UNCROSS,
        }:
            data = _market_safe_mechanics_data(event)
            self._append_public(
                event.event_type.value,
                data,
                simulation_time_us=event.simulation_time_us,
            )

    def _append_public(
        self,
        event_type: str,
        data: dict[str, object],
        *,
        simulation_time_us: int | None = None,
    ) -> None:
        self._public_events.append(
            PublicEcologyEvent(
                len(self._public_events) + 1,
                (
                    self.engine.clock.current_time_us
                    if simulation_time_us is None
                    else simulation_time_us
                ),
                event_type,
                data,
            )
        )

    def _assert_inventory_reconciliation(self) -> None:
        managed_by_id = {
            item.request.order_id: item for item in self.engine.orders
        }
        active_statuses = {"ACTIVE", "PARTIALLY_FILLED", "AUCTION_WORKING"}
        if self._allocator_owner == "INJECTED_RUNTIME":
            active_statuses.add("WORKING")
        for agent_id, agent in self.agents.items():
            owned = [
                managed_by_id[order_id]
                for order_id, owner_id in self._order_agent.items()
                if owner_id == agent_id and order_id in managed_by_id
            ]
            expected = sum(
                item.request.side.sign * item.filled_quantity for item in owned
            )
            if expected != agent.inventory:
                raise RuntimeError(f"agent {agent_id} inventory does not reconcile to fills")
            expected_orders = tuple(
                sorted(
                    (
                        OwnOrderView(
                            item.request.order_id,
                            item.request.side,
                            item.request.price_ticks,
                            item.remaining_quantity,
                            item.request.auction_only,
                        )
                        for item in owned
                        if item.remaining_quantity > 0
                        and item.status in active_statuses
                    ),
                    key=lambda item: item.order_id,
                )
            )
            if agent.own_orders != expected_orders:
                raise RuntimeError(
                    f"agent {agent_id} working orders do not reconcile to engine state"
                )


class _LegacyMechanicsView:
    """Read-through compatibility view with the pre-checkpoint legacy state digest."""

    def __init__(
        self,
        engine: MarketMechanicsEngine,
        bootstrap_order_ids: tuple[str, ...],
        hidden_event_sequences: frozenset[int],
    ) -> None:
        self._engine = engine
        self._bootstrap_order_ids = bootstrap_order_ids
        self._hidden_event_sequences = hidden_event_sequences

    def __getattr__(self, name: str) -> object:
        return getattr(self._engine, name)

    @property
    def events(self) -> tuple[MechanicsEvent, ...]:
        return tuple(
            MechanicsEvent(
                sequence,
                event.simulation_time_us,
                event.event_type,
                event.data,
            )
            for sequence, event in enumerate(
                (
                    item
                    for item in self._engine.events
                    if item.sequence not in self._hidden_event_sequences
                ),
                start=1,
            )
        )

    @property
    def orders(self) -> tuple[ManagedOrder, ...]:
        bootstrap = set(self._bootstrap_order_ids)
        offset = len(bootstrap)
        return tuple(
            ManagedOrder(
                order.request,
                order.arrival_sequence - offset,
                order.status,
                order.filled_quantity,
                order.cancelled_quantity,
                order.expired_quantity,
                (
                    order.resting_sequence - offset
                    if order.request.auction_only
                    and order.resting_sequence is not None
                    else order.resting_sequence
                ),
            )
            for order in self._engine.orders
            if order.request.order_id not in bootstrap
        )

    def get_order(self, order_id: str) -> ManagedOrder:
        match = next(
            (order for order in self.orders if order.request.order_id == order_id),
            None,
        )
        if match is None:
            raise ValueError(f"unknown managed order: {order_id}")
        return match

    def state_sha256(self) -> str:
        return _legacy_engine_state_sha256(
            self._engine,
            self._bootstrap_order_ids,
            self._hidden_event_sequences,
        )


class AgentEcology:
    """Legacy one-venue wrapper over one engine and its injected scheduler."""

    def __init__(self, definition: PopulationDefinition, seed: int) -> None:
        if type(seed) is not int or seed < 0:
            raise ValueError("agent ecology seed must be a nonnegative integer")
        self.definition = definition
        self.seed = seed
        account_stp = tuple(
            (agent.agent_id, SelfTradePreventionMode.CANCEL_RESTING)
            for agent in definition.agents
        )
        self._engine = MarketMechanicsEngine(
            InstrumentRules(
                tick_size=Decimal("0.01"),
                lower_price_band_ticks=max(1, definition.initial_mid_ticks - 1_000),
                upper_price_band_ticks=definition.initial_mid_ticks + 1_000,
                reference_price_ticks=definition.initial_mid_ticks,
                account_stp_modes=account_stp,
            )
        )
        self.scheduler = AgentScheduler(
            definition,
            seed,
            engine=self._engine,
            clock=self._engine.clock,
            compatibility_mode=True,
        )
        self._transition_index = 0
        self._bootstrap_order_ids: tuple[str, ...] = ()
        self._initialize_market()
        self._legacy_engine_view = _LegacyMechanicsView(
            self._engine,
            self._bootstrap_order_ids,
            frozenset(self.scheduler._hidden_mechanics_event_sequences),
        )
        self.scheduler._reconcile_agents()
        self.assert_invariants()

    def __getattr__(self, name: str) -> object:
        scheduler = self.__dict__.get("scheduler")
        if scheduler is None:
            raise AttributeError(name)
        return getattr(scheduler, name)

    @property
    def engine(self) -> _LegacyMechanicsView:
        """Stable read-through compatibility identity over the one real engine."""

        return self._legacy_engine_view

    @property
    def agents(self) -> dict[str, MarketAgent]:
        return self.scheduler.agents

    @property
    def complete(self) -> bool:
        return self.scheduler.complete

    @property
    def public_events(self) -> tuple[PublicEcologyEvent, ...]:
        return self.scheduler.public_events

    @property
    def truth_events(self) -> tuple[AgentTruthEvent, ...]:
        return self.scheduler.truth_events

    def run(self) -> EcologyRunResult:
        if self.complete:
            raise RuntimeError("a completed agent ecology instance cannot be rerun")
        while self._engine.clock.current_time_us < self.definition.duration_us:
            next_time = self._next_event_time()
            if next_time is None or next_time > self.definition.duration_us:
                next_time = self.definition.duration_us
            if self._engine._validating_outer_replay:
                raise RuntimeError("legacy ecology entered during mechanics replay")
            self._engine._validating_outer_replay = True
            try:
                self._engine.advance_to(next_time)
                self._apply_transitions_at(next_time)
                self.scheduler.deliver_due(next_time)
                self.scheduler.decide_due(next_time)
                self.scheduler.deliver_due(next_time)
                self.scheduler._reconcile_agents()
                self.scheduler._assert_owned_invariants()
            finally:
                self._engine._validating_outer_replay = False
            if next_time == self.definition.duration_us:
                break
        self.scheduler.mark_complete()
        self.assert_invariants()
        summary = self.summary()
        analysis = self.post_session_ground_truth()
        result_sha256 = canonical_sha256(
            {
                "analysis": analysis,
                "definition_sha256": self.definition.sha256(),
                "public_events": [item.as_dict() for item in self.public_events],
                "seed": self.seed,
                "summary": summary.as_dict(),
                "truth_events": [item.as_dict() for item in self.truth_events],
            }
        )
        return EcologyRunResult(
            self.definition,
            self.seed,
            summary,
            self.public_events,
            self.truth_events,
            analysis,
            result_sha256,
        )

    def summary(self) -> EcologySummary:
        if not self.complete:
            raise RuntimeError("ecology summary is available only after completion")
        prices = [item.price_ticks for item in self.scheduler._public_trades]
        price_changes = sum(
            first != second for first, second in zip(prices, prices[1:])
        )
        book = self.scheduler._aggregated_book()
        family_counts: dict[str, int] = {}
        for event in self.scheduler._truth_events:
            family_counts[event.family.value] = family_counts.get(event.family.value, 0) + 1
        accepted = sum(
            event.status is AgentActionStatus.ACCEPTED
            for event in self.scheduler._truth_events
        )
        return EcologySummary(
            self.definition.population_id,
            self.seed,
            self.definition.duration_us,
            len(self.agents),
            len(self.scheduler._truth_events),
            accepted,
            len(self.scheduler._truth_events) - accepted,
            len(self.scheduler._public_trades),
            sum(item.quantity for item in self.scheduler._public_trades),
            price_changes,
            min(prices) if prices else None,
            max(prices) if prices else None,
            prices[-1] if prices else None,
            self._engine.book.best_bid,
            self._engine.book.best_ask,
            sum(
                int(level["quantity"])
                for side in ("bids", "asks")
                for level in book[side]
            ),
            family_counts,
            self.scheduler._starting_book_sha256,
            self.state_sha256(),
            canonical_sha256([item.as_dict() for item in self.public_events]),
            canonical_sha256([item.as_dict() for item in self.truth_events]),
            "PASS",
        )

    def post_session_ground_truth(self) -> dict[str, object]:
        return self.scheduler.post_session_ground_truth()

    def branch_runtime_state(self) -> dict[str, object]:
        """Preserve the original compatibility projection byte-for-byte."""

        state = self.scheduler._owned_runtime_state()
        return {
            "agents": state["agents"],
            "clock_us": state["clock_us"],
            "complete": state["complete"],
            "definition_sha256": self.definition.sha256(),
            "engine_state_sha256": self.engine.state_sha256(),
            "next_decision_us": state["next_decision_us"],
            "mechanics_event_cursor": (
                self.scheduler._mechanics_event_cursor
                - self.scheduler._event_sequence_offset
            ),
            "order_agent": state["order_agent"],
            "order_sequence": state["order_sequence"],
            "pending": state["pending"],
            "pending_sequence": state["pending_sequence"],
            "public_events": state["public_events"],
            "public_trades": state["public_trades"],
            "seed": self.seed,
            "starting_book_sha256": state["starting_book_sha256"],
            "transition_index": self._transition_index,
            "truth_events": state["truth_events"],
        }

    def state_sha256(self) -> str:
        return canonical_sha256(self.branch_runtime_state())

    def assert_invariants(self) -> None:
        self.scheduler.assert_invariants()
        if not 0 <= self._transition_index <= len(self.definition.transitions):
            raise RuntimeError("agent ecology transition cursor is invalid")

    def _initialize_market(self) -> None:
        self._engine.transition_session(
            SessionState.PREOPEN,
            reason="AGENT_ECOLOGY_START",
        )
        hidden_start: int | None = None
        if self.definition.start_state is SessionState.CONTINUOUS:
            self._engine.transition_session(
                SessionState.OPENING_AUCTION,
                reason="AGENT_ECOLOGY_EMPTY_OPENING_CALL",
            )
            self._engine.uncross_auction()
            self._engine.transition_session(
                SessionState.CONTINUOUS,
                reason="AGENT_ECOLOGY_CONTINUOUS_START",
            )
        elif self.definition.start_state is SessionState.PREOPEN:
            hidden_start = len(self._engine.events) + 1
            self._engine.transition_session(
                SessionState.OPENING_AUCTION,
                reason="AGENT_ECOLOGY_MANAGED_BOOTSTRAP",
            )
            self._engine.uncross_auction()
            self._engine.transition_session(
                SessionState.CONTINUOUS,
                reason="AGENT_ECOLOGY_MANAGED_BOOTSTRAP",
            )
        else:
            raise ValueError("agent ecology starts in PREOPEN or CONTINUOUS")
        bootstrap_ids: list[str] = []
        for depth in range(self.definition.initial_depth_levels):
            quantity = self.definition.initial_level_quantity
            for side, price_ticks, side_label in (
                (
                    Side.BUY,
                    self.definition.initial_mid_ticks - depth - 1,
                    "BID",
                ),
                (
                    Side.SELL,
                    self.definition.initial_mid_ticks + depth + 1,
                    "ASK",
                ),
            ):
                order_id = f"INITIAL-{side_label}-{depth + 1:02d}"
                managed = self._engine.submit(
                    AdvancedOrderRequest(
                        order_id=order_id,
                        side=side,
                        quantity=quantity,
                        instruction=OrderInstruction.LIMIT,
                        owner=OrderOwner.SIMULATED,
                        account_id="AGENT-ECOLOGY-INITIAL-BOOK",
                        price_ticks=price_ticks,
                        time_in_force=(
                            OrderInstruction.GTC
                            if self.definition.start_state is SessionState.PREOPEN
                            else OrderInstruction.DAY
                        ),
                    )
                )
                if managed is None or managed.status == "REJECTED":
                    raise RuntimeError("agent ecology initial liquidity was rejected")
                bootstrap_ids.append(order_id)
        if self.definition.start_state is SessionState.PREOPEN:
            self._engine.transition_session(
                SessionState.POSTCLOSE,
                reason="AGENT_ECOLOGY_MANAGED_BOOTSTRAP",
            )
            self._engine.transition_session(
                SessionState.CLOSED,
                reason="AGENT_ECOLOGY_MANAGED_BOOTSTRAP",
            )
            self._engine.transition_session(
                SessionState.PREOPEN,
                reason="AGENT_ECOLOGY_MANAGED_BOOTSTRAP",
            )
        self._bootstrap_order_ids = tuple(bootstrap_ids)
        if hidden_start is None:
            hidden_sequences = {
                event.sequence
                for event in self._engine.events
                if event.event_type is MechanicsEventType.ORDER_ACCEPTED
                and event.data.get("order_id") in set(bootstrap_ids)
            }
        else:
            hidden_sequences = set(range(hidden_start, len(self._engine.events) + 1))
        self.scheduler._hidden_mechanics_event_sequences = hidden_sequences
        self.scheduler._starting_book_sha256 = canonical_sha256(
            self._engine.book.snapshot()
        )
        self.scheduler._capture_public_changes(None)
        self.scheduler._event_sequence_offset = len(hidden_sequences)

    def _next_event_time(self) -> int | None:
        candidates = [
            value
            for value in (self.scheduler.next_scheduled_time_us,)
            if value is not None
        ]
        if self._transition_index < len(self.definition.transitions):
            candidates.append(
                self.definition.transitions[self._transition_index].simulation_time_us
            )
        return min(candidates) if candidates else None

    def _apply_transitions_at(self, simulation_time_us: int) -> None:
        before = self.scheduler._aggregated_book()
        changed = False
        while (
            self._transition_index < len(self.definition.transitions)
            and self.definition.transitions[self._transition_index].simulation_time_us
            == simulation_time_us
        ):
            transition = self.definition.transitions[self._transition_index]
            if transition.uncross_before:
                self._engine.uncross_auction()
            self._engine.transition_session(
                transition.state,
                reason="CONFIGURED_AGENT_ECOLOGY_DRILL",
            )
            self._transition_index += 1
            changed = True
        if changed:
            self.scheduler._capture_public_changes(before)
            self.scheduler._reconcile_agents()


def _engine_checkpoint_sha256(engine: MarketMechanicsEngine) -> str:
    """Bind scheduler state to the complete strict mechanics checkpoint bytes."""

    return hashlib.sha256(engine.canonical_state_bytes()).hexdigest()


def _legacy_engine_state_sha256(
    engine: MarketMechanicsEngine,
    bootstrap_order_ids: tuple[str, ...],
    hidden_event_sequences: frozenset[int],
) -> str:
    """Project strict managed bootstrap rows onto the pre-WO31-D legacy digest."""

    return canonical_sha256(
        _legacy_engine_state_projection(
            engine,
            bootstrap_order_ids,
            hidden_event_sequences,
        )
    )


def _legacy_engine_state_projection(
    engine: MarketMechanicsEngine,
    bootstrap_order_ids: tuple[str, ...],
    hidden_event_sequences: frozenset[int],
) -> dict[str, object]:
    """Build the compatibility payload while retaining strict state internally."""

    bootstrap = set(bootstrap_order_ids)
    core_orders = {
        order_id: {
            "cancelled_quantity": order.cancelled_quantity,
            "filled_quantity": order.filled_quantity,
            "original_quantity": order.original_quantity,
            "owner": order.owner.value,
            "price_ticks": order.price_ticks,
            "remaining_quantity": order.remaining_quantity,
            "resting_sequence": order.resting_sequence,
            "side": None if order.side is None else order.side.value,
            "status": order.status.value,
            "type": order.order_type.value,
        }
        for order_id, order in sorted(engine.book.all_orders.items())
    }
    kept_events = [
        event
        for event in engine.events
        if event.sequence not in hidden_event_sequences
    ]
    projected_events = []
    for sequence, event in enumerate(kept_events, start=1):
        row = event.as_dict()
        row["sequence"] = sequence
        projected_events.append(row)
    managed_rows = []
    for order in engine.orders:
        if order.request.order_id in bootstrap:
            continue
        row = order.as_dict()
        row["arrival_sequence"] = int(row["arrival_sequence"]) - len(bootstrap)
        if order.request.auction_only and row["resting_sequence"] is not None:
            row["resting_sequence"] = int(row["resting_sequence"]) - len(bootstrap)
        managed_rows.append(row)
    return {
        "auction_player_position": engine._auction_player_position,
        "book": engine.book.snapshot(),
        "clock_us": engine.clock.current_time_us,
        "counters": {
            "arrival": engine._arrival_sequence - len(bootstrap),
            "auction_trade": len(
                [
                    event
                    for event in kept_events
                    if event.event_type is MechanicsEventType.AUCTION_FILL
                ]
            ),
            "command": engine._command_sequence,
        },
        "core_events": [event.as_dict() for event in engine.book.journal.events],
        "core_orders": core_orders,
        "events": projected_events,
        "last_trade_price_ticks": engine._last_trade_price_ticks,
        "managed_orders": managed_rows,
        "player_position": engine.player_position,
        "rules": engine.rules.as_dict(),
        "schedule_index": engine._schedule_index,
        "session_state": engine.session_state.value,
    }


def run_agent_ecology(
    definition: PopulationDefinition,
    *,
    seed: int,
) -> EcologyRunResult:
    return AgentEcology(definition, seed).run()


def _market_safe_mechanics_data(event: MechanicsEvent) -> dict[str, object]:
    if event.event_type is MechanicsEventType.SESSION_STATE_CHANGED:
        return {
            "current_state": event.data["current_state"],
            "previous_state": event.data["previous_state"],
        }
    if event.event_type in {MechanicsEventType.HALT, MechanicsEventType.RESUME}:
        return {"session_state_event": event.event_type.value}
    if event.event_type is MechanicsEventType.AUCTION_INDICATION:
        return {
            "indication": event.data["indication"],
            "session_state": event.data["session_state"],
        }
    if event.event_type is MechanicsEventType.AUCTION_UNCROSS:
        return {
            "actual_matched_quantity": event.data["actual_matched_quantity"],
            "indication": event.data["indication"],
            "session_state": event.data["session_state"],
        }
    return {}


def _contains_forbidden_key(value: object, forbidden: set[str]) -> bool:
    if isinstance(value, Mapping):
        return bool(set(value) & forbidden) or any(
            _contains_forbidden_key(item, forbidden) for item in value.values()
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return any(_contains_forbidden_key(item, forbidden) for item in value)
    return False
