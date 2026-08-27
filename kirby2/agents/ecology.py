"""Deterministic scheduler and exchange gateway for interacting synthetic agents."""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from kirby2.exchange import (
    AdvancedOrderRequest,
    InstrumentRules,
    MarketMechanicsEngine,
    MechanicsEvent,
    MechanicsEventType,
    Order,
    OrderInstruction,
    OrderOwner,
    OrderType,
    SelfTradePreventionMode,
    SessionState,
    Side,
)
from kirby2.multivenue.models import canonical_sha256

from .base import MarketAgent
from .families import create_agent
from .models import (
    AGENT_ECOLOGY_SCHEMA_VERSION,
    SYNTHETIC_VENUE_ID,
    AgentActionStatus,
    AgentFamily,
    AgentIntent,
    AgentIntentType,
    AgentObservation,
    AgentTruthEvent,
    OwnOrderView,
    PopulationDefinition,
    PublicEcologyEvent,
    PublicTradeView,
)


@dataclass(order=True, frozen=True, slots=True)
class _PendingIntent:
    arrival_time_us: int
    sequence: int
    decision_time_us: int = field(compare=False)
    agent_id: str = field(compare=False)
    intent: AgentIntent = field(compare=False)


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


class AgentEcology:
    """One synthetic venue whose state changes only through exchange requests."""

    def __init__(self, definition: PopulationDefinition, seed: int) -> None:
        if type(seed) is not int or seed < 0:
            raise ValueError("agent ecology seed must be a nonnegative integer")
        self.definition = definition
        self.seed = seed
        account_stp = tuple(
            (agent.agent_id, SelfTradePreventionMode.CANCEL_RESTING)
            for agent in definition.agents
        )
        self.engine = MarketMechanicsEngine(
            InstrumentRules(
                tick_size=Decimal("0.01"),
                lower_price_band_ticks=max(1, definition.initial_mid_ticks - 1_000),
                upper_price_band_ticks=definition.initial_mid_ticks + 1_000,
                reference_price_ticks=definition.initial_mid_ticks,
                account_stp_modes=account_stp,
            )
        )
        self.agents: dict[str, MarketAgent] = {
            spec.agent_id: create_agent(spec, seed) for spec in definition.agents
        }
        self._next_decision_us: dict[str, int | None] = {
            spec.agent_id: (
                spec.bounds.lifetime_start_us
                if spec.bounds.lifetime_start_us + spec.bounds.latency_us
                <= spec.bounds.lifetime_end_us
                else None
            )
            for spec in definition.agents
        }
        self._pending: list[_PendingIntent] = []
        self._pending_sequence = 0
        self._order_sequence = 0
        self._truth_events: list[AgentTruthEvent] = []
        self._public_events: list[PublicEcologyEvent] = []
        self._public_trades: list[PublicTradeView] = []
        self._mechanics_event_cursor = 0
        self._transition_index = 0
        self._order_agent: dict[str, str] = {}
        self._complete = False
        self._starting_book_sha256 = ""
        self._initialize_market()
        self._reconcile_agents()
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

    def run(self) -> EcologyRunResult:
        if self.complete:
            raise RuntimeError("a completed agent ecology instance cannot be rerun")
        while self.engine.clock.current_time_us < self.definition.duration_us:
            next_time = self._next_event_time()
            if next_time is None or next_time > self.definition.duration_us:
                next_time = self.definition.duration_us
            self.engine.advance_to(next_time)
            self._apply_transitions_at(next_time)
            self._deliver_pending_at(next_time)
            self._decide_at(next_time)
            self._deliver_pending_at(next_time)
            self._reconcile_agents()
            self.assert_invariants()
            if next_time == self.definition.duration_us:
                break
        self._complete = True
        self._append_public("SESSION_COMPLETE", {"status": "COMPLETE"})
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

    def branch_runtime_state(self) -> dict[str, object]:
        """Complete agent-owned state for deterministic future branch snapshots."""

        return {
            "agents": {
                agent_id: agent.runtime_state()
                for agent_id, agent in sorted(self.agents.items())
            },
            "clock_us": self.engine.clock.current_time_us,
            "complete": self.complete,
            "definition_sha256": self.definition.sha256(),
            "engine_state_sha256": self.engine.state_sha256(),
            "next_decision_us": dict(sorted(self._next_decision_us.items())),
            "mechanics_event_cursor": self._mechanics_event_cursor,
            "order_agent": dict(sorted(self._order_agent.items())),
            "order_sequence": self._order_sequence,
            "pending": [
                {
                    "agent_id": item.agent_id,
                    "arrival_time_us": item.arrival_time_us,
                    "decision_time_us": item.decision_time_us,
                    "intent": item.intent.as_dict(),
                    "sequence": item.sequence,
                }
                for item in sorted(self._pending)
            ],
            "pending_sequence": self._pending_sequence,
            "public_events": [item.as_dict() for item in self._public_events],
            "public_trades": [
                {
                    "price_ticks": item.price_ticks,
                    "quantity": item.quantity,
                    "simulation_time_us": item.simulation_time_us,
                }
                for item in self._public_trades
            ],
            "seed": self.seed,
            "starting_book_sha256": self._starting_book_sha256,
            "transition_index": self._transition_index,
            "truth_events": [item.as_dict() for item in self._truth_events],
        }

    def state_sha256(self) -> str:
        return canonical_sha256(self.branch_runtime_state())

    def assert_invariants(self) -> None:
        self.engine.assert_invariants()
        now = self.engine.clock.current_time_us
        for agent in self.agents.values():
            agent.assert_invariants(now)
        if len(self.agents) != len(set(self.agents)):
            raise RuntimeError("agent IDs are duplicated")
        if any(agent_id not in self.agents for agent_id in self._order_agent.values()):
            raise RuntimeError("order-to-agent truth mapping references an unknown actor")
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
        forbidden = {"agent_id", "family", "rationale", "account_id", "owner", "intent"}
        if any(_contains_forbidden_key(item.as_dict(), forbidden) for item in self._public_events):
            raise RuntimeError("player-visible ecology log leaked agent identity or intent")
        if any(item.agent_id not in self.agents for item in self._truth_events):
            raise RuntimeError("agent truth event contains an unregistered identity")
        for pending in self._pending:
            if pending.agent_id not in self.agents or pending.arrival_time_us < now:
                raise RuntimeError("pending agent intent is invalid")
        self._assert_inventory_reconciliation()

    def _initialize_market(self) -> None:
        self.engine.transition_session(SessionState.PREOPEN, reason="AGENT_ECOLOGY_START")
        if self.definition.start_state is SessionState.CONTINUOUS:
            self.engine.transition_session(
                SessionState.OPENING_AUCTION,
                reason="AGENT_ECOLOGY_EMPTY_OPENING_CALL",
            )
            self.engine.uncross_auction()
            self.engine.transition_session(
                SessionState.CONTINUOUS,
                reason="AGENT_ECOLOGY_CONTINUOUS_START",
            )
        elif self.definition.start_state is not SessionState.PREOPEN:
            raise ValueError("agent ecology starts in PREOPEN or CONTINUOUS")
        for depth in range(self.definition.initial_depth_levels):
            quantity = self.definition.initial_level_quantity
            self.engine.book.process(
                Order.limit(
                    f"INITIAL-BID-{depth + 1:02d}",
                    Side.BUY,
                    quantity,
                    self.definition.initial_mid_ticks - depth - 1,
                )
            )
            self.engine.book.process(
                Order.limit(
                    f"INITIAL-ASK-{depth + 1:02d}",
                    Side.SELL,
                    quantity,
                    self.definition.initial_mid_ticks + depth + 1,
                )
            )
        self._starting_book_sha256 = canonical_sha256(self.engine.book.snapshot())
        self._capture_public_changes(None)

    def _next_event_time(self) -> int | None:
        candidates: list[int] = []
        if self._pending:
            candidates.append(self._pending[0].arrival_time_us)
        candidates.extend(
            value for value in self._next_decision_us.values() if value is not None
        )
        if self._transition_index < len(self.definition.transitions):
            candidates.append(
                self.definition.transitions[self._transition_index].simulation_time_us
            )
        return min(candidates) if candidates else None

    def _apply_transitions_at(self, simulation_time_us: int) -> None:
        before = self._aggregated_book()
        changed = False
        while (
            self._transition_index < len(self.definition.transitions)
            and self.definition.transitions[self._transition_index].simulation_time_us
            == simulation_time_us
        ):
            transition = self.definition.transitions[self._transition_index]
            if transition.uncross_before:
                self.engine.uncross_auction()
            self.engine.transition_session(
                transition.state,
                reason="CONFIGURED_AGENT_ECOLOGY_DRILL",
            )
            self._transition_index += 1
            changed = True
        if changed:
            self._capture_public_changes(before)
            self._reconcile_agents()

    def _decide_at(self, simulation_time_us: int) -> None:
        due = sorted(
            agent_id
            for agent_id, time_us in self._next_decision_us.items()
            if time_us == simulation_time_us
        )
        if not due:
            return
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
                    _PendingIntent(
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

    def _deliver_pending_at(self, simulation_time_us: int) -> None:
        while self._pending and self._pending[0].arrival_time_us == simulation_time_us:
            pending = heapq.heappop(self._pending)
            self._deliver(pending)
            self._reconcile_agents()

    def _deliver(self, pending: _PendingIntent) -> None:
        agent = self.agents[pending.agent_id]
        rejection = self._gateway_rejection(agent, pending.intent, pending.arrival_time_us)
        if rejection not in {"MAX_AGENT_ORDER_RATE", "OUTSIDE_BOUNDED_LIFETIME"}:
            agent.register_action(pending.arrival_time_us)
        order_id: str | None = None
        exchange_start = len(self.engine.events)
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
        exchange_end = len(self.engine.events)
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
        self._order_sequence += 1
        return f"SIM-O-{self._order_sequence:08d}"

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
                and item.status
                in {"ACTIVE", "PARTIALLY_FILLED", "AUCTION_WORKING"}
            )
            agent.reconcile(inventory, active)

    def _capture_public_changes(self, before_book: dict[str, object] | None) -> None:
        new_events = self.engine.events[self._mechanics_event_cursor :]
        for event in new_events:
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
        for agent_id, agent in self.agents.items():
            expected = sum(
                managed_by_id[order_id].request.side.sign
                * managed_by_id[order_id].filled_quantity
                for order_id, owner_id in self._order_agent.items()
                if owner_id == agent_id and order_id in managed_by_id
            )
            if expected != agent.inventory:
                raise RuntimeError(f"agent {agent_id} inventory does not reconcile to fills")


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
