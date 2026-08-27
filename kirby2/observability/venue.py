"""Deterministic hidden-liquidity venue with a one-way observable projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from kirby2.exchange.models import OrderOwner, Side
from kirby2.session.events import EventType, SimulationEvent
from kirby2.simulation.clock import SimulationClock

from .models import (
    DISPLAY_DECREASE_POSSIBLE_CAUSES,
    GroundTruthExchangeState,
    GroundTruthOrderState,
    HiddenLiquidityRules,
    HiddenOrderRequest,
    HiddenPriority,
    IcebergRefreshBehavior,
    IcebergRefreshPriority,
    LiquidityKind,
    ObservableDepthBook,
    ObservableEvent,
    ObservableEventType,
    ObservableMarketFeed,
    ObservablePriceLevel,
    ObservablePlayerPosition,
    ObservabilityScore,
    OwnOrderState,
    PublicTrade,
    RefreshEventVisibility,
    TruthEvent,
    TruthEventType,
)


@dataclass(slots=True)
class _VenueOrder:
    request: HiddenOrderRequest
    arrival_sequence: int
    priority_sequence: int
    displayed_remaining: int
    reserve_remaining: int
    hidden_remaining: int
    filled_quantity: int = 0
    cancelled_quantity: int = 0
    refresh_count: int = 0
    status: str = "WORKING"

    @property
    def remaining_quantity(self) -> int:
        return (
            self.displayed_remaining
            + self.reserve_remaining
            + self.hidden_remaining
        )

    @classmethod
    def create(
        cls,
        request: HiddenOrderRequest,
        sequence: int,
    ) -> _VenueOrder:
        if request.kind is LiquidityKind.DISPLAYED_LIMIT:
            displayed, reserve, hidden = request.quantity, 0, 0
        elif request.kind is LiquidityKind.ICEBERG:
            if request.iceberg is None:  # pragma: no cover - request validates
                raise RuntimeError("iceberg request lost its definition")
            displayed = request.iceberg.display_quantity
            reserve = request.iceberg.reserve_quantity
            hidden = 0
        else:
            displayed, reserve, hidden = 0, 0, request.quantity
        return cls(
            request,
            sequence,
            sequence,
            displayed,
            reserve,
            hidden,
        )

    def ground_truth(self) -> GroundTruthOrderState:
        return GroundTruthOrderState(
            order_id=self.request.order_id,
            side=self.request.side,
            kind=self.request.kind,
            owner=self.request.owner,
            price_ticks=self.request.price_ticks,
            original_quantity=self.request.quantity,
            displayed_quantity=self.displayed_remaining,
            reserve_quantity=self.reserve_remaining,
            hidden_quantity=self.hidden_remaining,
            filled_quantity=self.filled_quantity,
            cancelled_quantity=self.cancelled_quantity,
            priority_sequence=self.priority_sequence,
            status=self.status,
        )

    def own_state(self) -> OwnOrderState:
        return OwnOrderState(
            order_id=self.request.order_id,
            side=self.request.side,
            price_ticks=self.request.price_ticks,
            acknowledged=True,
            status=self.status,
            original_quantity=self.request.quantity,
            filled_quantity=self.filled_quantity,
            remaining_quantity=self.remaining_quantity,
            displayed_quantity=self.displayed_remaining,
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    order: _VenueOrder
    price_x2: int
    source: str


@dataclass(frozen=True, slots=True)
class _PendingObservable:
    due_time_us: int
    source_time_us: int
    ordinal: int
    event_type: ObservableEventType
    data: dict[str, object]
    book: ObservableDepthBook | None = None
    trade: PublicTrade | None = None
    own_order: OwnOrderState | None = None
    player_position: ObservablePlayerPosition | None = None
    strategy_event_type: EventType | None = None
    strategy_data: dict[str, object] | None = None


class HiddenLiquidityVenue:
    """Owns ground truth and publishes a sanitized aggregate feed.

    The mutable order ledger is private. Runtime consumers receive only fresh
    immutable `ObservableMarketFeed` values. Full order truth is unavailable until
    `complete_session()` has sealed the exercise.
    """

    def __init__(self, rules: HiddenLiquidityRules | None = None) -> None:
        self.rules = rules or HiddenLiquidityRules()
        self.clock = SimulationClock()
        self._orders: dict[str, _VenueOrder] = {}
        self._seen_order_ids: set[str] = set()
        self._arrival_sequence = 0
        self._priority_sequence = 0
        self._trade_sequence = 0
        self._truth_events: list[TruthEvent] = []
        self._pending: list[_PendingObservable] = []
        self._pending_ordinal = 0
        self._observable_events: list[ObservableEvent] = []
        self._strategy_events: list[SimulationEvent] = []
        self._public_tape: list[PublicTrade] = []
        self._published_book = ObservableDepthBook()
        self._published_own_orders: dict[str, OwnOrderState] = {}
        self._published_player_position = ObservablePlayerPosition(0, 0, 0)
        self._player_position = 0
        self._player_bought_quantity = 0
        self._player_sold_quantity = 0
        self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def player_position(self) -> int:
        return self._player_position

    def advance_to(self, simulation_time_us: int) -> None:
        if (
            type(simulation_time_us) is not int
            or simulation_time_us < self.clock.current_time_us
        ):
            raise ValueError("hidden-liquidity venue time cannot move backward")
        self.clock.advance_to(simulation_time_us)
        self._flush_observable()
        self.assert_invariants()

    def submit_resting(self, request: HiddenOrderRequest) -> bool:
        self._require_open()
        if request.order_id in self._seen_order_ids:
            raise ValueError(f"duplicate hidden-liquidity order ID: {request.order_id}")
        if (
            request.kind is LiquidityKind.HIDDEN_LIMIT
            and not self.rules.allow_fully_hidden
        ):
            raise ValueError("venue rules do not permit fully hidden orders")
        if (
            request.kind is LiquidityKind.MIDPOINT_HIDDEN
            and not self.rules.allow_midpoint_hidden
        ):
            raise ValueError("venue rules do not permit midpoint hidden orders")
        self._reject_crossed_resting(request)
        before = self._visible_book()
        self._arrival_sequence += 1
        self._priority_sequence += 1
        order = _VenueOrder.create(request, self._arrival_sequence)
        order.priority_sequence = self._priority_sequence
        self._orders[request.order_id] = order
        self._seen_order_ids.add(request.order_id)
        self._emit_truth(
            TruthEventType.ORDER_ACCEPTED,
            order=request.as_dict(),
            priority_sequence=order.priority_sequence,
        )
        after = self._visible_book()
        self._publish_book_change(before, after)
        if request.owner is OrderOwner.PLAYER:
            self._schedule_observable(
                ObservableEventType.OWN_ORDER_ACKNOWLEDGED,
                {"own_order": order.own_state().as_dict()},
                own_order=order.own_state(),
            )
        self._flush_observable()
        self.assert_invariants()
        return True

    def execute_market(
        self,
        order_id: str,
        side: Side,
        quantity: int,
        *,
        owner: OrderOwner = OrderOwner.SIMULATED,
        account_id: str = "AGGRESSOR",
    ) -> int:
        self._require_open()
        if not order_id or not account_id:
            raise ValueError("market order identity and account are required")
        if order_id in self._seen_order_ids:
            raise ValueError(f"duplicate hidden-liquidity order ID: {order_id}")
        if not isinstance(side, Side) or not isinstance(owner, OrderOwner):
            raise TypeError("market side and owner must use canonical enums")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("market quantity must be a positive integer")
        self._emit_truth(
            TruthEventType.ORDER_ACCEPTED,
            account_id=account_id,
            aggressive=True,
            order_id=order_id,
            owner=owner.value,
            quantity=quantity,
            side=side.value,
        )
        self._seen_order_ids.add(order_id)
        if owner is OrderOwner.PLAYER:
            self._schedule_observable(
                ObservableEventType.OWN_ORDER_ACKNOWLEDGED,
                {
                    "aggressive": True,
                    "order_id": order_id,
                    "quantity": quantity,
                    "side": side.value,
                },
            )
        remaining = quantity
        while remaining > 0:
            candidates = self._market_candidates(side)
            if not candidates:
                break
            candidate = candidates[0]
            available = self._candidate_quantity(candidate)
            fill_quantity = min(remaining, available)
            self._execute_fill(
                candidate,
                taker_order_id=order_id,
                taker_side=side,
                taker_owner=owner,
                quantity=fill_quantity,
            )
            remaining -= fill_quantity
        self._flush_observable()
        self.assert_invariants()
        return quantity - remaining

    def cancel(self, order_id: str) -> int:
        self._require_open()
        order = self._active_order(order_id)
        before = self._visible_book()
        quantity = order.remaining_quantity
        order.cancelled_quantity += quantity
        order.displayed_remaining = 0
        order.reserve_remaining = 0
        order.hidden_remaining = 0
        order.status = "CANCELLED"
        self._emit_truth(
            TruthEventType.ORDER_CANCELLED,
            cancelled_quantity=quantity,
            order_id=order_id,
        )
        after = self._visible_book()
        self._publish_book_change(before, after)
        if order.request.owner is OrderOwner.PLAYER:
            self._schedule_observable(
                ObservableEventType.OWN_ORDER_CANCELLED,
                {"own_order": order.own_state().as_dict()},
                own_order=order.own_state(),
            )
        self._flush_observable()
        self.assert_invariants()
        return quantity

    def refresh_order(self, order_id: str) -> int:
        self._require_open()
        order = self._active_order(order_id)
        if order.request.kind is not LiquidityKind.ICEBERG:
            raise ValueError("manual refresh requires an iceberg order")
        definition = order.request.iceberg
        if definition is None:  # pragma: no cover - request validates
            raise RuntimeError("iceberg definition is absent")
        if definition.refresh_behavior is not IcebergRefreshBehavior.MANUAL:
            raise ValueError("manual refresh requires MANUAL refresh behavior")
        if order.displayed_remaining or not order.reserve_remaining:
            raise ValueError("iceberg order is not waiting for manual refresh")
        quantity = self._refresh_iceberg(order)
        self._flush_observable()
        self.assert_invariants()
        return quantity

    def observable_feed(
        self,
        *,
        after_event_sequence: int = 0,
        after_strategy_sequence: int = 0,
    ) -> ObservableMarketFeed:
        if (
            type(after_event_sequence) is not int
            or after_event_sequence < 0
            or type(after_strategy_sequence) is not int
            or after_strategy_sequence < 0
        ):
            raise ValueError("observable event cursors must be nonnegative")
        self._flush_observable()
        events = tuple(
            event
            for event in self._observable_events
            if event.sequence > after_event_sequence
        )
        strategy_events = tuple(
            event
            for event in self._strategy_events
            if event.sequence > after_strategy_sequence
        )
        return ObservableMarketFeed(
            simulation_time_us=self.clock.current_time_us,
            book=self._published_book,
            tape=tuple(self._public_tape),
            events=events,
            own_orders=tuple(
                self._published_own_orders[key]
                for key in sorted(self._published_own_orders)
            ),
            player_position=self._published_player_position,
            strategy_events=strategy_events,
        )

    def complete_session(self) -> None:
        if self._complete:
            return
        self._complete = True
        self._emit_truth(TruthEventType.SESSION_COMPLETE)
        self._schedule_observable(ObservableEventType.SESSION_COMPLETE, {})
        self._flush_observable()
        self.assert_invariants()

    def post_session_ground_truth(self) -> GroundTruthExchangeState:
        if not self._complete:
            raise RuntimeError(
                "ground truth is reveal-only and unavailable before session completion"
            )
        return GroundTruthExchangeState(
            simulation_time_us=self.clock.current_time_us,
            orders=tuple(order.ground_truth() for order in self._ordered_orders()),
            player_position=self._player_position,
            events=tuple(self._truth_events),
        )

    def score_observable_execution(
        self,
        *,
        target_quantity: int,
        completed_quantity: int,
        observable_liquidity_at_decisions: int,
    ) -> ObservabilityScore:
        if min(
            target_quantity,
            completed_quantity,
            observable_liquidity_at_decisions,
        ) < 0:
            raise ValueError("observability score quantities cannot be negative")
        hidden_fills = sum(
            int(event.data["quantity"])
            for event in self._truth_events
            if event.event_type is TruthEventType.TRADE
            and str(event.data["liquidity_source"])
            in {
                "HIDDEN_LIMIT",
                "ICEBERG_REFRESH",
                "MIDPOINT_HIDDEN",
            }
        )
        missed_observable = max(
            0,
            min(target_quantity, observable_liquidity_at_decisions)
            - completed_quantity,
        )
        return ObservabilityScore(
            target_quantity=target_quantity,
            completed_quantity=completed_quantity,
            observable_liquidity_at_decisions=observable_liquidity_at_decisions,
            missed_observable_liquidity=missed_observable,
            revealed_hidden_liquidity=hidden_fills,
        )

    def truth_event_sha256(self) -> str:
        return _sha256([event.as_dict() for event in self._truth_events])

    def observable_event_sha256(self) -> str:
        self._flush_observable()
        return _sha256([event.as_dict() for event in self._observable_events])

    def state_sha256(self) -> str:
        payload = {
            "clock_us": self.clock.current_time_us,
            "complete": self._complete,
            "orders": [order.ground_truth().as_dict() for order in self._ordered_orders()],
            "pending": [
                {
                    "data": item.data,
                    "due_time_us": item.due_time_us,
                    "event_type": item.event_type.value,
                    "ordinal": item.ordinal,
                    "source_time_us": item.source_time_us,
                }
                for item in sorted(
                    self._pending,
                    key=lambda value: (value.due_time_us, value.ordinal),
                )
            ],
            "player_position": self._player_position,
            "published_feed": self.observable_feed().as_dict(),
            "rules": self.rules.as_dict(),
            "seen_order_ids": sorted(self._seen_order_ids),
            "truth_events": [event.as_dict() for event in self._truth_events],
        }
        return _sha256(payload)

    def assert_invariants(self) -> None:
        arrival = [order.arrival_sequence for order in self._ordered_orders()]
        if len(arrival) != len(set(arrival)):
            raise RuntimeError("ground-truth arrival sequences are duplicated")
        active_priorities = [
            order.priority_sequence
            for order in self._ordered_orders()
            if order.remaining_quantity > 0
        ]
        if len(active_priorities) != len(set(active_priorities)):
            raise RuntimeError("active ground-truth priorities are duplicated")
        for order in self._ordered_orders():
            quantities = (
                order.displayed_remaining,
                order.reserve_remaining,
                order.hidden_remaining,
                order.filled_quantity,
                order.cancelled_quantity,
            )
            if min(quantities) < 0 or sum(quantities) != order.request.quantity:
                raise RuntimeError("hidden-liquidity order quantity does not conserve")
            if order.request.kind is LiquidityKind.DISPLAYED_LIMIT and (
                order.reserve_remaining or order.hidden_remaining
            ):
                raise RuntimeError("displayed order contains undisplayed quantity")
            if order.request.kind is LiquidityKind.ICEBERG and order.hidden_remaining:
                raise RuntimeError("iceberg quantity escaped display/reserve buckets")
            if order.request.kind in {
                LiquidityKind.HIDDEN_LIMIT,
                LiquidityKind.MIDPOINT_HIDDEN,
            } and (order.displayed_remaining or order.reserve_remaining):
                raise RuntimeError("fully hidden order leaked into displayed buckets")
            if order.remaining_quantity == 0 and order.status not in {
                "CANCELLED",
                "FILLED",
            }:
                raise RuntimeError("closed ground-truth order has a live status")
        visible = self._visible_book()
        if visible.best_bid is not None and visible.best_ask is not None:
            if visible.best_bid >= visible.best_ask:
                raise RuntimeError("ground-truth displayed book is locked or crossed")
        truth_sequences = [event.sequence for event in self._truth_events]
        truth_times = [event.simulation_time_us for event in self._truth_events]
        if truth_sequences != list(range(1, len(truth_sequences) + 1)):
            raise RuntimeError("ground-truth event sequence is not contiguous")
        if truth_times != sorted(truth_times):
            raise RuntimeError("ground-truth event times are not monotonic")
        public_sequences = [event.sequence for event in self._observable_events]
        receive_times = [event.received_time_us for event in self._observable_events]
        if public_sequences != list(range(1, len(public_sequences) + 1)):
            raise RuntimeError("observable event sequence is not contiguous")
        if receive_times != sorted(receive_times):
            raise RuntimeError("observable receive times are not monotonic")
        public_payload = json.dumps(
            self.observable_feed().as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).lower()
        forbidden = (
            "reserve_quantity",
            "hidden_quantity",
            "priority_sequence",
            "ground_truth",
            "maker_order_id",
            "liquidity_source",
        )
        if any(field in public_payload for field in forbidden):
            raise RuntimeError("observable feed leaked simulator ground truth")
        expected_position = 0
        expected_bought = 0
        expected_sold = 0
        delivered_position = 0
        delivered_bought = 0
        delivered_sold = 0
        for event in self._truth_events:
            if event.event_type is not TruthEventType.TRADE:
                continue
            quantity = int(event.data["quantity"])
            if event.data["maker_owner"] == OrderOwner.PLAYER.value:
                maker_side = Side(str(event.data["maker_side"]))
                expected_position += maker_side.sign * quantity
                if maker_side is Side.BUY:
                    expected_bought += quantity
                else:
                    expected_sold += quantity
            if event.data["taker_owner"] == OrderOwner.PLAYER.value:
                taker_side = Side(str(event.data["taker_side"]))
                expected_position += taker_side.sign * quantity
                if taker_side is Side.BUY:
                    expected_bought += quantity
                else:
                    expected_sold += quantity
            if (
                event.simulation_time_us + self.rules.feed_delay_us
                <= self.clock.current_time_us
            ):
                if event.data["maker_owner"] == OrderOwner.PLAYER.value:
                    maker_side = Side(str(event.data["maker_side"]))
                    delivered_position += maker_side.sign * quantity
                    if maker_side is Side.BUY:
                        delivered_bought += quantity
                    else:
                        delivered_sold += quantity
                if event.data["taker_owner"] == OrderOwner.PLAYER.value:
                    taker_side = Side(str(event.data["taker_side"]))
                    delivered_position += taker_side.sign * quantity
                    if taker_side is Side.BUY:
                        delivered_bought += quantity
                    else:
                        delivered_sold += quantity
        if expected_position != self._player_position:
            raise RuntimeError("player position does not reconcile to hidden fills")
        if (
            expected_bought != self._player_bought_quantity
            or expected_sold != self._player_sold_quantity
        ):
            raise RuntimeError("player buy/sell totals do not reconcile to hidden fills")
        if self._published_player_position != ObservablePlayerPosition(
            delivered_position,
            delivered_bought,
            delivered_sold,
        ):
            raise RuntimeError("published player position escaped feed latency")
        public_trade_quantity = sum(trade.quantity for trade in self._public_tape)
        truth_trade_quantity = sum(
            int(event.data["quantity"])
            for event in self._truth_events
            if event.event_type is TruthEventType.TRADE
            and event.simulation_time_us + self.rules.feed_delay_us
            <= self.clock.current_time_us
        )
        if public_trade_quantity != truth_trade_quantity:
            raise RuntimeError("public tape does not reconcile to delivered truth trades")

    def _execute_fill(
        self,
        candidate: _Candidate,
        *,
        taker_order_id: str,
        taker_side: Side,
        taker_owner: OrderOwner,
        quantity: int,
    ) -> None:
        maker = candidate.order
        before = self._visible_book()
        if candidate.source in {"DISPLAYED", "ICEBERG_INITIAL", "ICEBERG_REFRESH"}:
            if quantity > maker.displayed_remaining:
                raise RuntimeError("displayed fill exceeds displayed slice")
            maker.displayed_remaining -= quantity
        else:
            if quantity > maker.hidden_remaining:
                raise RuntimeError("hidden fill exceeds hidden quantity")
            maker.hidden_remaining -= quantity
        maker.filled_quantity += quantity
        maker.status = "FILLED" if maker.remaining_quantity == 0 else "PARTIALLY_FILLED"
        self._trade_sequence += 1
        trade_id = f"HT{self._trade_sequence:06d}"
        self._emit_truth(
            TruthEventType.TRADE,
            liquidity_source=candidate.source,
            maker_order_id=maker.request.order_id,
            maker_owner=maker.request.owner.value,
            maker_side=maker.request.side.value,
            price_x2=candidate.price_x2,
            quantity=quantity,
            taker_order_id=taker_order_id,
            taker_owner=taker_owner.value,
            taker_side=taker_side.value,
            trade_id=trade_id,
        )
        if maker.request.owner is OrderOwner.PLAYER:
            self._apply_player_fill(maker.request.side, quantity)
        if taker_owner is OrderOwner.PLAYER:
            self._apply_player_fill(taker_side, quantity)
        public_trade = PublicTrade(
            trade_id,
            self.clock.current_time_us,
            candidate.price_x2,
            quantity,
            taker_side,
        )
        self._schedule_observable(
            ObservableEventType.TRADE,
            public_trade.as_dict(),
            trade=public_trade,
            strategy_event_type=EventType.TRADE,
            strategy_data={
                "price_ticks": str(public_trade.price_ticks),
                "price_x2": public_trade.price_x2,
                "quantity": quantity,
                "taker_side": taker_side.value,
                "trade_id": trade_id,
            },
        )
        if maker.request.owner is OrderOwner.PLAYER:
            self._schedule_observable(
                ObservableEventType.OWN_ORDER_FILL,
                {
                    "fill_quantity": quantity,
                    "own_order": maker.own_state().as_dict(),
                    "price_x2": candidate.price_x2,
                    "trade_id": trade_id,
                },
                own_order=maker.own_state(),
                player_position=self._current_player_position(),
            )
        if taker_owner is OrderOwner.PLAYER:
            self._schedule_observable(
                ObservableEventType.OWN_ORDER_FILL,
                {
                    "fill_quantity": quantity,
                    "order_id": taker_order_id,
                    "price_x2": candidate.price_x2,
                    "trade_id": trade_id,
                },
                player_position=self._current_player_position(),
            )
        after_fill = self._visible_book()
        self._publish_book_change(before, after_fill)
        if (
            maker.request.kind is LiquidityKind.ICEBERG
            and maker.displayed_remaining == 0
            and maker.reserve_remaining > 0
            and maker.request.iceberg is not None
            and maker.request.iceberg.refresh_behavior
            is IcebergRefreshBehavior.AUTOMATIC
        ):
            self._refresh_iceberg(maker)

    def _refresh_iceberg(self, order: _VenueOrder) -> int:
        definition = order.request.iceberg
        if definition is None:  # pragma: no cover - request validates
            raise RuntimeError("iceberg definition is absent")
        before = self._visible_book()
        quantity = min(definition.refresh_quantity, order.reserve_remaining)
        order.reserve_remaining -= quantity
        order.displayed_remaining += quantity
        order.refresh_count += 1
        order.status = "PARTIALLY_FILLED" if order.filled_quantity else "WORKING"
        if self.rules.iceberg_refresh_priority is IcebergRefreshPriority.LOSE:
            self._priority_sequence += 1
            order.priority_sequence = self._priority_sequence
        self._emit_truth(
            TruthEventType.ICEBERG_REFRESHED,
            event_visibility=definition.event_visibility.value,
            order_id=order.request.order_id,
            priority_rule=self.rules.iceberg_refresh_priority.value,
            priority_sequence=order.priority_sequence,
            refresh_count=order.refresh_count,
            refreshed_quantity=quantity,
            reserve_remaining=order.reserve_remaining,
        )
        after = self._visible_book()
        self._publish_book_change(before, after)
        if definition.event_visibility is RefreshEventVisibility.EXPLICIT_REPLENISHMENT:
            self._schedule_observable(
                ObservableEventType.EXPLICIT_REPLENISHMENT,
                {
                    "price_ticks": order.request.price_ticks,
                    "quantity": quantity,
                    "side": order.request.side.value,
                },
                strategy_event_type=EventType.ORDER_ADDED,
                strategy_data={
                    "remaining_quantity": quantity,
                    "side": order.request.side.value,
                },
            )
        if order.request.owner is OrderOwner.PLAYER:
            self._schedule_observable(
                ObservableEventType.OWN_ORDER_ACKNOWLEDGED,
                {"own_order": order.own_state().as_dict(), "refresh": True},
                own_order=order.own_state(),
            )
        return quantity

    def _market_candidates(self, taker_side: Side) -> list[_Candidate]:
        opposite = Side.SELL if taker_side is Side.BUY else Side.BUY
        midpoint_x2 = self._displayed_midpoint_x2()
        candidates: list[_Candidate] = []
        for order in self._ordered_orders():
            if order.request.side is not opposite or order.remaining_quantity <= 0:
                continue
            if order.displayed_remaining:
                source = (
                    "DISPLAYED"
                    if order.request.kind is LiquidityKind.DISPLAYED_LIMIT
                    else "ICEBERG_INITIAL"
                    if order.refresh_count == 0
                    else "ICEBERG_REFRESH"
                )
                candidates.append(
                    _Candidate(
                        order,
                        int(order.request.price_ticks) * 2,
                        source,
                    )
                )
            elif (
                order.request.kind is LiquidityKind.HIDDEN_LIMIT
                and order.hidden_remaining
            ):
                candidates.append(
                    _Candidate(
                        order,
                        int(order.request.price_ticks) * 2,
                        "HIDDEN_LIMIT",
                    )
                )
            elif (
                order.request.kind is LiquidityKind.MIDPOINT_HIDDEN
                and order.hidden_remaining
                and midpoint_x2 is not None
            ):
                candidates.append(
                    _Candidate(order, midpoint_x2, "MIDPOINT_HIDDEN")
                )
        return sorted(candidates, key=lambda item: self._candidate_key(item, taker_side))

    def _candidate_key(
        self,
        candidate: _Candidate,
        taker_side: Side,
    ) -> tuple[int, int, int]:
        economic_price = (
            candidate.price_x2
            if taker_side is Side.BUY
            else -candidate.price_x2
        )
        hidden = candidate.source in {"HIDDEN_LIMIT", "MIDPOINT_HIDDEN"}
        hidden_rank = (
            int(hidden)
            if self.rules.hidden_priority is HiddenPriority.AFTER_DISPLAYED
            else int(not hidden)
        )
        return economic_price, hidden_rank, candidate.order.priority_sequence

    @staticmethod
    def _candidate_quantity(candidate: _Candidate) -> int:
        if candidate.source in {"DISPLAYED", "ICEBERG_INITIAL", "ICEBERG_REFRESH"}:
            return candidate.order.displayed_remaining
        return candidate.order.hidden_remaining

    def _reject_crossed_resting(self, request: HiddenOrderRequest) -> None:
        if request.kind is LiquidityKind.MIDPOINT_HIDDEN:
            return
        opposite = Side.SELL if request.side is Side.BUY else Side.BUY
        opposite_prices = [
            int(order.request.price_ticks)
            for order in self._ordered_orders()
            if order.request.side is opposite
            and order.remaining_quantity > 0
            and order.request.price_ticks is not None
        ]
        if not opposite_prices:
            return
        best = min(opposite_prices) if request.side is Side.BUY else max(opposite_prices)
        if request.side is Side.BUY and int(request.price_ticks) >= best:
            raise ValueError("crossed buy must execute rather than rest")
        if request.side is Side.SELL and int(request.price_ticks) <= best:
            raise ValueError("crossed sell must execute rather than rest")

    def _visible_book(self) -> ObservableDepthBook:
        totals: dict[tuple[Side, int], int] = {}
        for order in self._ordered_orders():
            if order.displayed_remaining <= 0:
                continue
            price = int(order.request.price_ticks)
            key = (order.request.side, price)
            totals[key] = totals.get(key, 0) + order.displayed_remaining
        bids = tuple(
            ObservablePriceLevel(price, Side.BUY, quantity)
            for (side, price), quantity in sorted(
                totals.items(),
                key=lambda item: -item[0][1],
            )
            if side is Side.BUY
        )
        asks = tuple(
            ObservablePriceLevel(price, Side.SELL, quantity)
            for (side, price), quantity in sorted(
                totals.items(),
                key=lambda item: item[0][1],
            )
            if side is Side.SELL
        )
        return ObservableDepthBook(bids, asks)

    def _displayed_midpoint_x2(self) -> int | None:
        book = self._visible_book()
        if book.best_bid is None or book.best_ask is None:
            return None
        return book.best_bid + book.best_ask

    def _publish_book_change(
        self,
        before: ObservableDepthBook,
        after: ObservableDepthBook,
    ) -> None:
        if before == after:
            return
        self._schedule_observable(
            ObservableEventType.BOOK_SNAPSHOT,
            {"book": after.as_dict(), "update_kind": "SNAPSHOT_REPLACEMENT"},
            book=after,
        )
        previous = _level_quantities(before)
        current = _level_quantities(after)
        for side, price in sorted(
            set(previous) | set(current),
            key=lambda item: (item[0].value, item[1]),
        ):
            old = previous.get((side, price), 0)
            new = current.get((side, price), 0)
            if old == new:
                continue
            data: dict[str, object] = {
                "new_displayed_quantity": new,
                "previous_displayed_quantity": old,
                "price_ticks": price,
                "side": side.value,
            }
            if new < old:
                data["possible_causes"] = list(DISPLAY_DECREASE_POSSIBLE_CAUSES)
                data["cause_attribution"] = "UNRESOLVED_FROM_PUBLIC_FEED"
            self._schedule_observable(
                ObservableEventType.DISPLAY_QUANTITY_CHANGED,
                data,
            )

    def _schedule_observable(
        self,
        event_type: ObservableEventType,
        data: dict[str, object],
        *,
        book: ObservableDepthBook | None = None,
        trade: PublicTrade | None = None,
        own_order: OwnOrderState | None = None,
        player_position: ObservablePlayerPosition | None = None,
        strategy_event_type: EventType | None = None,
        strategy_data: dict[str, object] | None = None,
    ) -> None:
        self._pending_ordinal += 1
        source = self.clock.current_time_us
        self._pending.append(
            _PendingObservable(
                source + self.rules.feed_delay_us,
                source,
                self._pending_ordinal,
                event_type,
                dict(data),
                book,
                trade,
                own_order,
                player_position,
                strategy_event_type,
                strategy_data,
            )
        )

    def _flush_observable(self) -> None:
        due = sorted(
            (
                item
                for item in self._pending
                if item.due_time_us <= self.clock.current_time_us
            ),
            key=lambda item: (item.due_time_us, item.ordinal),
        )
        if not due:
            return
        due_ordinals = {item.ordinal for item in due}
        self._pending = [
            item for item in self._pending if item.ordinal not in due_ordinals
        ]
        for item in due:
            if item.book is not None:
                self._published_book = item.book
            if item.trade is not None:
                self._public_tape.append(item.trade)
            if item.own_order is not None:
                self._published_own_orders[item.own_order.order_id] = item.own_order
            if item.player_position is not None:
                self._published_player_position = item.player_position
            event = ObservableEvent(
                len(self._observable_events) + 1,
                item.source_time_us,
                item.due_time_us,
                item.event_type,
                item.data,
            )
            self._observable_events.append(event)
            if item.strategy_event_type is not None:
                self._strategy_events.append(
                    SimulationEvent(
                        len(self._strategy_events) + 1,
                        item.strategy_event_type,
                        {} if item.strategy_data is None else item.strategy_data,
                    )
                )

    def _emit_truth(self, event_type: TruthEventType, **data: object) -> None:
        self._truth_events.append(
            TruthEvent(
                len(self._truth_events) + 1,
                self.clock.current_time_us,
                event_type,
                dict(data),
            )
        )

    def _apply_player_fill(self, side: Side, quantity: int) -> None:
        self._player_position += side.sign * quantity
        if side is Side.BUY:
            self._player_bought_quantity += quantity
        else:
            self._player_sold_quantity += quantity

    def _current_player_position(self) -> ObservablePlayerPosition:
        return ObservablePlayerPosition(
            self._player_position,
            self._player_bought_quantity,
            self._player_sold_quantity,
        )

    def _ordered_orders(self) -> tuple[_VenueOrder, ...]:
        return tuple(
            self._orders[key]
            for key in sorted(
                self._orders,
                key=lambda item: self._orders[item].arrival_sequence,
            )
        )

    def _active_order(self, order_id: str) -> _VenueOrder:
        order = self._orders.get(order_id)
        if order is None or order.remaining_quantity <= 0:
            raise ValueError(f"hidden-liquidity order is not active: {order_id}")
        return order

    def _require_open(self) -> None:
        if self._complete:
            raise RuntimeError("hidden-liquidity session is already complete")


def _level_quantities(book: ObservableDepthBook) -> dict[tuple[Side, int], int]:
    return {
        (level.side, level.price_ticks): level.total_quantity
        for level in (*book.bid_levels, *book.ask_levels)
    }


def _sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
