"""Configurable deterministic session, protection, instruction, and auction engine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from kirby2.simulation.clock import SimulationClock

from .auction import AuctionBook, AuctionUncrossResult
from .book import OrderBook
from .mechanics_models import (
    AdvancedOrderRequest,
    InstrumentRules,
    ManagedOrder,
    MechanicsEvent,
    MechanicsEventType,
    OrderInstruction,
    SelfTradePreventionMode,
    SessionState,
)
from .models import Order, OrderOwner, OrderStatus, Side


_ALLOWED_SESSION_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CLOSED: frozenset({SessionState.PREOPEN}),
    SessionState.PREOPEN: frozenset(
        {SessionState.OPENING_AUCTION, SessionState.CLOSED}
    ),
    SessionState.OPENING_AUCTION: frozenset(
        {SessionState.CONTINUOUS, SessionState.HALTED, SessionState.CLOSED}
    ),
    SessionState.CONTINUOUS: frozenset(
        {
            SessionState.HALTED,
            SessionState.CLOSING_AUCTION,
            SessionState.POSTCLOSE,
        }
    ),
    SessionState.HALTED: frozenset(
        {
            SessionState.REOPENING_AUCTION,
            SessionState.CLOSED,
            SessionState.POSTCLOSE,
        }
    ),
    SessionState.REOPENING_AUCTION: frozenset(
        {SessionState.CONTINUOUS, SessionState.HALTED, SessionState.CLOSED}
    ),
    SessionState.CLOSING_AUCTION: frozenset(
        {SessionState.POSTCLOSE, SessionState.HALTED}
    ),
    SessionState.POSTCLOSE: frozenset(
        {SessionState.CLOSED, SessionState.PREOPEN}
    ),
}


_AUCTION_ACCEPTING_STATES = frozenset(
    {
        SessionState.PREOPEN,
        SessionState.OPENING_AUCTION,
        SessionState.HALTED,
        SessionState.REOPENING_AUCTION,
        SessionState.CLOSING_AUCTION,
    }
)


_AUCTION_UNCROSS_STATES = frozenset(
    {
        SessionState.OPENING_AUCTION,
        SessionState.REOPENING_AUCTION,
        SessionState.CLOSING_AUCTION,
    }
)


class MarketMechanicsEngine:
    """A deterministic policy layer over the original continuous FIFO book."""

    def __init__(self, rules: InstrumentRules | None = None) -> None:
        self.rules = rules or InstrumentRules()
        self.clock = SimulationClock()
        self.book = OrderBook()
        self.auction = AuctionBook()
        self.session_state = SessionState.CLOSED
        self._events: list[MechanicsEvent] = []
        self._orders: dict[str, ManagedOrder] = {}
        self._arrival_sequence = 0
        self._command_sequence = 0
        self._schedule_index = 0
        self._last_trade_price_ticks: int | None = None
        self._auction_player_position = 0

    @property
    def events(self) -> tuple[MechanicsEvent, ...]:
        return tuple(self._events)

    @property
    def orders(self) -> tuple[ManagedOrder, ...]:
        return tuple(
            self._orders[key]
            for key in sorted(
                self._orders,
                key=lambda item: self._orders[item].arrival_sequence,
            )
        )

    @property
    def player_position(self) -> int:
        return self.book.player_position.position + self._auction_player_position

    @property
    def last_trade_price_ticks(self) -> int | None:
        return self._last_trade_price_ticks

    def get_order(self, order_id: str) -> ManagedOrder:
        try:
            return self._orders[order_id]
        except KeyError as error:
            raise ValueError(f"unknown managed order: {order_id}") from error

    def advance_to(self, simulation_time_us: int) -> None:
        if (
            type(simulation_time_us) is not int
            or simulation_time_us < self.clock.current_time_us
        ):
            raise ValueError("market-mechanics time cannot move backward")
        while True:
            due_times: list[int] = []
            transitions = self.rules.session_schedule.transitions
            if self._schedule_index < len(transitions):
                transition_time = transitions[self._schedule_index].simulation_time_us
                if transition_time <= simulation_time_us:
                    due_times.append(transition_time)
            expirations = [
                order.request.good_until_time_us
                for order in self.orders
                if self._is_managed_active(order)
                and order.request.time_in_force
                is OrderInstruction.GOOD_UNTIL_TIME
                and order.request.good_until_time_us is not None
                and order.request.good_until_time_us <= simulation_time_us
            ]
            due_times.extend(int(value) for value in expirations)
            if not due_times:
                break
            next_time = min(due_times)
            self.clock.advance_to(next_time)
            while (
                self._schedule_index < len(transitions)
                and transitions[self._schedule_index].simulation_time_us == next_time
            ):
                scheduled = transitions[self._schedule_index]
                self.transition_session(
                    scheduled.state,
                    reason="CONFIGURED_SESSION_SCHEDULE",
                )
                self._schedule_index += 1
            self._expire_good_until(next_time)
        self.clock.advance_to(simulation_time_us)
        self.assert_invariants()

    def transition_session(self, state: SessionState, *, reason: str) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("session state must use the canonical enum")
        if not reason:
            raise ValueError("session transition reason is required")
        previous = self.session_state
        if state is previous:
            return
        if state not in _ALLOWED_SESSION_TRANSITIONS[previous]:
            raise ValueError(
                f"invalid session transition {previous.value}->{state.value}"
            )
        if previous is SessionState.CONTINUOUS and state is not SessionState.CONTINUOUS:
            self._expire_time_in_force(
                {OrderInstruction.SESSION},
                f"SESSION_END_{state.value}",
            )
        if (
            previous in _AUCTION_UNCROSS_STATES
            and state not in _AUCTION_UNCROSS_STATES
        ) or state in {SessionState.POSTCLOSE, SessionState.CLOSED}:
            self._expire_all_auction_orders(
                f"AUCTION_PHASE_END_{state.value}",
            )
        if state in {SessionState.POSTCLOSE, SessionState.CLOSED}:
            self._expire_time_in_force(
                {OrderInstruction.DAY, OrderInstruction.SESSION},
                f"DAY_END_{state.value}",
            )
        self.session_state = state
        self._emit(
            MechanicsEventType.SESSION_STATE_CHANGED,
            current_state=state.value,
            previous_state=previous.value,
            reason=reason,
        )
        if state is SessionState.HALTED:
            self._emit(MechanicsEventType.HALT, reason=reason)
        elif previous in {
            SessionState.HALTED,
            SessionState.REOPENING_AUCTION,
        } and state is SessionState.CONTINUOUS:
            self._emit(MechanicsEventType.RESUME, reason=reason)
        self.assert_invariants()

    def submit(self, request: AdvancedOrderRequest) -> ManagedOrder | None:
        if request.order_id in self._orders or request.order_id in self.book.all_orders:
            self._emit(
                MechanicsEventType.ORDER_REJECTED,
                order_id=request.order_id,
                reason="DUPLICATE_ORDER_ID",
            )
            return None
        self._arrival_sequence += 1
        managed = ManagedOrder(request, self._arrival_sequence)
        self._orders[request.order_id] = managed
        rejection = self._validate_request(request)
        if rejection is not None:
            reason, protection = rejection
            self._reject(managed, reason, protection=protection)
            return managed
        if request.auction_only:
            self.auction.add(managed)
            self._emit(
                MechanicsEventType.AUCTION_ORDER_ADDED,
                order_id=request.order_id,
                request=request.as_dict(),
            )
            self._emit_auction_indication("ORDER_ADDED")
            self.assert_invariants()
            return managed
        stp_rejection = self._apply_continuous_stp(managed)
        if stp_rejection is not None:
            self._reject(managed, stp_rejection)
            return managed
        makers = self._potential_makers(request)
        if self._execution_collar_interrupted(managed, makers):
            return managed
        if request.post_only and makers:
            self._reject(managed, "POST_ONLY_WOULD_CROSS")
            return managed
        if (
            request.instruction is OrderInstruction.MARKETABLE_LIMIT
            and not makers
        ):
            self._reject(managed, "MARKETABLE_LIMIT_NOT_MARKETABLE")
            return managed
        if request.time_in_force is OrderInstruction.FOK:
            available = sum(order.remaining_quantity for order in makers)
            if available < request.quantity:
                self._reject(
                    managed,
                    "FOK_INSUFFICIENT_IMMEDIATE_QUANTITY",
                )
                return managed
        if self._volatility_interruption(managed, makers):
            return managed
        self._process_continuous(managed)
        self.assert_invariants()
        return managed

    def cancel(self, order_id: str, *, reason: str = "USER_CANCEL") -> bool:
        managed = self._orders.get(order_id)
        if managed is None or managed.remaining_quantity <= 0:
            self._emit(
                MechanicsEventType.ORDER_REJECTED,
                order_id=order_id,
                reason="CANCEL_NOT_ACTIVE",
            )
            return False
        if managed.request.auction_only:
            quantity = self.auction.cancel(order_id)
            self._emit(
                MechanicsEventType.AUCTION_ORDER_CANCELLED,
                cancelled_quantity=quantity,
                order_id=order_id,
                reason=reason,
            )
            self._emit_auction_indication("ORDER_CANCELLED")
        else:
            command_id = self._next_command_id("MECH-CANCEL")
            self.book.cancel(order_id, command_id)
            self._sync_continuous_orders()
            self._emit(
                MechanicsEventType.ORDER_CANCELLED,
                cancelled_quantity=managed.cancelled_quantity,
                command_id=command_id,
                order_id=order_id,
                reason=reason,
            )
        self.assert_invariants()
        return True

    def replace_order(
        self,
        order_id: str,
        *,
        new_order_id: str,
        new_quantity: int,
        new_price_ticks: int | None = None,
    ) -> ManagedOrder | None:
        managed = self._orders.get(order_id)
        core = self.book.active_orders.get(order_id)
        if managed is None or core is None or managed.request.auction_only:
            self._emit(
                MechanicsEventType.ORDER_REJECTED,
                order_id=order_id,
                reason="REPLACE_NOT_ACTIVE_CONTINUOUS_ORDER",
            )
            return None
        price_ticks = (
            managed.request.price_ticks
            if new_price_ticks is None
            else new_price_ticks
        )
        replacement = replace(
            managed.request,
            order_id=new_order_id,
            quantity=new_quantity,
            price_ticks=price_ticks,
        )
        validation = self._validate_request(replacement)
        if validation is not None:
            reason, protection = validation
            if protection is not None:
                self._emit(
                    MechanicsEventType.PROTECTION_TRIGGERED,
                    order_id=new_order_id,
                    protection=protection,
                    reason=reason,
                )
            self._emit(
                MechanicsEventType.ORDER_REJECTED,
                order_id=new_order_id,
                reason=reason,
            )
            return None
        current_total = core.filled_quantity + core.remaining_quantity
        if new_quantity <= core.filled_quantity:
            self._emit(
                MechanicsEventType.ORDER_REJECTED,
                filled_quantity=core.filled_quantity,
                order_id=new_order_id,
                reason="REPLACE_QUANTITY_NOT_ABOVE_FILLED",
            )
            return None
        preserves = (
            price_ticks == managed.request.price_ticks
            and new_quantity < current_total
            and new_quantity > core.filled_quantity
            and self.rules.preserve_priority_on_quantity_reduction
        )
        if preserves:
            command_id = self._next_command_id("MECH-REDUCE")
            previous_sequence = core.resting_sequence
            self.book.reduce_order(order_id, new_quantity, command_id)
            self._sync_continuous_orders()
            self._emit(
                MechanicsEventType.PRIORITY_PRESERVED,
                command_id=command_id,
                new_total_quantity=new_quantity,
                order_id=order_id,
                replacement_request_id=new_order_id,
                resting_sequence=previous_sequence,
            )
            self._emit(
                MechanicsEventType.ORDER_REPLACED,
                new_order_id=order_id,
                old_order_id=order_id,
                priority_preserved=True,
                replacement_request_id=new_order_id,
            )
            self.assert_invariants()
            return managed
        if new_order_id in self._orders or new_order_id in self.book.all_orders:
            self._emit(
                MechanicsEventType.ORDER_REJECTED,
                order_id=new_order_id,
                reason="DUPLICATE_REPLACEMENT_ORDER_ID",
            )
            return None
        reason = (
            "PRICE_CHANGE"
            if price_ticks != managed.request.price_ticks
            else "QUANTITY_INCREASE"
            if new_quantity >= current_total
            else "VENUE_REDUCTION_RULE"
        )
        self._emit(
            MechanicsEventType.PRIORITY_LOST,
            new_order_id=new_order_id,
            old_order_id=order_id,
            reason=reason,
        )
        self.cancel(order_id, reason="REPLACE_CANCEL_LEG")
        replacement_leaves = replace(
            replacement,
            quantity=new_quantity - core.filled_quantity,
        )
        submitted = self.submit(replacement_leaves)
        self._emit(
            MechanicsEventType.ORDER_REPLACED,
            filled_quantity_before_replace=core.filled_quantity,
            new_order_id=new_order_id,
            new_total_quantity=new_quantity,
            old_order_id=order_id,
            replacement_leaves_quantity=replacement_leaves.quantity,
            replacement_accepted=(submitted is not None and submitted.status != "REJECTED"),
        )
        return submitted

    def auction_indication(self):
        return self.auction.indication(self._reference_price())

    def uncross_auction(self) -> AuctionUncrossResult:
        if self.session_state not in _AUCTION_UNCROSS_STATES:
            raise RuntimeError("auction can uncross only in an auction session state")
        result = self.auction.uncross(
            self._reference_price(),
            self.rules.stp_mode,
        )
        for order_id, mode in result.stp_cancellations:
            self._emit(
                MechanicsEventType.SELF_TRADE_PREVENTION,
                mode=mode,
                order_id=order_id,
                phase=self.session_state.value,
            )
        for execution in result.executions:
            buy = self._orders[execution.buy_order_id]
            sell = self._orders[execution.sell_order_id]
            if buy.request.owner is OrderOwner.PLAYER:
                self._auction_player_position += execution.quantity
            if sell.request.owner is OrderOwner.PLAYER:
                self._auction_player_position -= execution.quantity
            self._last_trade_price_ticks = execution.price_ticks
            self._emit(
                MechanicsEventType.AUCTION_FILL,
                buy_order_id=execution.buy_order_id,
                price_ticks=execution.price_ticks,
                quantity=execution.quantity,
                sell_order_id=execution.sell_order_id,
                trade_id=execution.trade_id,
            )
        for order_id, quantity in result.expirations:
            self._emit(
                MechanicsEventType.ORDER_EXPIRED,
                expired_quantity=quantity,
                order_id=order_id,
                reason="AUCTION_REMAINDER",
            )
        self._emit(
            MechanicsEventType.AUCTION_UNCROSS,
            actual_matched_quantity=result.matched_quantity,
            indication=result.indication.as_dict(),
            session_state=self.session_state.value,
        )
        self.assert_invariants()
        return result

    def assert_invariants(self) -> None:
        self.book.assert_invariants()
        self.auction.assert_invariants()
        sequences = [event.sequence for event in self._events]
        times = [event.simulation_time_us for event in self._events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeError("market-mechanics event sequence is not contiguous")
        if times != sorted(times):
            raise RuntimeError("market-mechanics event times are not monotonic")
        arrival_sequences = [order.arrival_sequence for order in self.orders]
        if len(arrival_sequences) != len(set(arrival_sequences)):
            raise RuntimeError("managed order arrival sequences are duplicated")
        for managed in self.orders:
            quantities = (
                managed.filled_quantity,
                managed.cancelled_quantity,
                managed.expired_quantity,
                managed.remaining_quantity,
            )
            if min(quantities) < 0 or sum(quantities) != managed.request.quantity:
                raise RuntimeError("managed order quantities do not conserve")
            if managed.request.auction_only:
                continue
            core = self.book.all_orders.get(managed.request.order_id)
            if managed.status in {"REJECTED", "PENDING"}:
                continue
            if core is None:
                raise RuntimeError("accepted managed order is absent from FIFO ledger")
            if managed.filled_quantity != core.filled_quantity:
                raise RuntimeError("managed fill quantity differs from FIFO ledger")
            if managed.remaining_quantity != core.remaining_quantity:
                raise RuntimeError("managed live quantity differs from FIFO ledger")
            if (
                managed.cancelled_quantity + managed.expired_quantity
                != core.cancelled_quantity
            ):
                raise RuntimeError("managed closed quantity differs from FIFO ledger")
        expected_auction_position = 0
        for event in self._events:
            if event.event_type is not MechanicsEventType.AUCTION_FILL:
                continue
            buy = self._orders[str(event.data["buy_order_id"])]
            sell = self._orders[str(event.data["sell_order_id"])]
            quantity = int(event.data["quantity"])
            if buy.request.owner is OrderOwner.PLAYER:
                expected_auction_position += quantity
            if sell.request.owner is OrderOwner.PLAYER:
                expected_auction_position -= quantity
        if expected_auction_position != self._auction_player_position:
            raise RuntimeError("auction player position does not reconcile to fills")

    def event_stream_sha256(self) -> str:
        canonical = json.dumps(
            [event.as_dict() for event in self.events],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def state_sha256(self) -> str:
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
            for order_id, order in sorted(self.book.all_orders.items())
        }
        payload = {
            "auction_player_position": self._auction_player_position,
            "book": self.book.snapshot(),
            "clock_us": self.clock.current_time_us,
            "counters": {
                "arrival": self._arrival_sequence,
                "auction_trade": len(
                    [
                        event
                        for event in self._events
                        if event.event_type is MechanicsEventType.AUCTION_FILL
                    ]
                ),
                "command": self._command_sequence,
            },
            "core_events": [event.as_dict() for event in self.book.journal.events],
            "core_orders": core_orders,
            "events": [event.as_dict() for event in self.events],
            "last_trade_price_ticks": self._last_trade_price_ticks,
            "managed_orders": [order.as_dict() for order in self.orders],
            "player_position": self.player_position,
            "rules": self.rules.as_dict(),
            "schedule_index": self._schedule_index,
            "session_state": self.session_state.value,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _validate_request(
        self,
        request: AdvancedOrderRequest,
    ) -> tuple[str, str | None] | None:
        fields = self._validate_order_fields(request)
        if fields is not None:
            return fields
        if request.auction_only:
            if self.session_state not in _AUCTION_ACCEPTING_STATES:
                return ("AUCTION_ORDER_OUTSIDE_AUCTION_PHASE", None)
            if request.time_in_force in {
                OrderInstruction.IOC,
                OrderInstruction.FOK,
            } or request.post_only or (
                request.instruction is OrderInstruction.MARKETABLE_LIMIT
            ):
                return ("UNSUPPORTED_AUCTION_INSTRUCTION_COMBINATION", None)
        elif self.session_state is not SessionState.CONTINUOUS:
            return ("CONTINUOUS_ORDER_OUTSIDE_CONTINUOUS_SESSION", None)
        if request.post_only and (
            request.instruction is OrderInstruction.MARKETABLE_LIMIT
            or request.time_in_force
            in {OrderInstruction.IOC, OrderInstruction.FOK}
        ):
            return ("POST_ONLY_CONFLICTS_WITH_IMMEDIATE_EXECUTION", None)
        if (
            request.time_in_force is OrderInstruction.GOOD_UNTIL_TIME
            and request.good_until_time_us <= self.clock.current_time_us  # type: ignore[operator]
        ):
            return ("GOOD_UNTIL_TIME_ALREADY_EXPIRED", None)
        return None

    def _validate_order_fields(
        self,
        request: AdvancedOrderRequest,
    ) -> tuple[str, str | None] | None:
        instructions = {
            request.instruction,
            request.time_in_force,
            *request.modifiers,
        }
        if not instructions <= self.rules.supported_order_instructions:
            return ("UNSUPPORTED_ORDER_INSTRUCTION", None)
        if request.quantity > self.rules.maximum_quantity:
            return ("MAXIMUM_ORDER_SIZE_EXCEEDED", "MAXIMUM_ORDER_SIZE")
        if request.quantity < self.rules.minimum_quantity:
            return ("BELOW_MINIMUM_QUANTITY", None)
        if request.quantity % self.rules.lot_size:
            return ("QUANTITY_NOT_ALIGNED_TO_LOT_SIZE", None)
        if request.price_ticks is None:
            return None
        if not (
            self.rules.lower_price_band_ticks
            <= request.price_ticks
            <= self.rules.upper_price_band_ticks
        ):
            return ("PRICE_OUTSIDE_INSTRUMENT_BAND", "ORDER_PRICE_REJECTION")
        reference = self._reference_price()
        if (
            self.rules.fat_finger_ticks is not None
            and abs(request.price_ticks - reference) > self.rules.fat_finger_ticks
        ):
            return ("FAT_FINGER_PRICE_DISTANCE", "FAT_FINGER_PROTECTION")
        if (
            self.rules.price_collar_ticks is not None
            and abs(request.price_ticks - reference) > self.rules.price_collar_ticks
        ):
            return ("PRICE_OUTSIDE_EXECUTION_COLLAR", "PRICE_COLLAR")
        return None

    def _reject(
        self,
        managed: ManagedOrder,
        reason: str,
        *,
        protection: str | None = None,
    ) -> None:
        managed.status = "REJECTED"
        if protection is not None:
            self._emit(
                MechanicsEventType.PROTECTION_TRIGGERED,
                order_id=managed.request.order_id,
                protection=protection,
                reason=reason,
            )
        self._emit(
            MechanicsEventType.ORDER_REJECTED,
            order_id=managed.request.order_id,
            reason=reason,
        )
        self.assert_invariants()

    def _apply_continuous_stp(self, managed: ManagedOrder) -> str | None:
        request = managed.request
        mode = self.rules.stp_mode(request.account_id)
        if mode is SelfTradePreventionMode.NONE:
            return None
        self_matches = [
            maker
            for maker in self._potential_makers(request)
            if self._account_for_order(maker.order_id) == request.account_id
        ]
        if not self_matches:
            return None
        if mode is SelfTradePreventionMode.CANCEL_AGGRESSOR:
            self._emit(
                MechanicsEventType.SELF_TRADE_PREVENTION,
                aggressor_order_id=request.order_id,
                mode=mode.value,
                resting_order_ids=[order.order_id for order in self_matches],
            )
            return "SELF_TRADE_CANCEL_AGGRESSOR"
        for resting in self_matches:
            command_id = self._next_command_id("MECH-STP")
            self.book.cancel(resting.order_id, command_id)
            self._sync_continuous_orders()
            self._emit(
                MechanicsEventType.SELF_TRADE_PREVENTION,
                aggressor_order_id=request.order_id,
                command_id=command_id,
                mode=mode.value,
                resting_order_id=resting.order_id,
            )
        if mode is SelfTradePreventionMode.CANCEL_BOTH:
            return "SELF_TRADE_CANCEL_BOTH"
        return None

    def _volatility_interruption(
        self,
        managed: ManagedOrder,
        makers: list[Order],
    ) -> bool:
        threshold = self.rules.volatility_interruption_ticks
        if threshold is None or not makers:
            return False
        reference = self._reference_price()
        violating_price = next(
            (
                int(order.price_ticks)
                for order in makers
                if abs(int(order.price_ticks) - reference) > threshold
            ),
            None,
        )
        if violating_price is None:
            return False
        self._emit(
            MechanicsEventType.PROTECTION_TRIGGERED,
            order_id=managed.request.order_id,
            protection="VOLATILITY_INTERRUPTION",
            reference_price_ticks=reference,
            violating_price_ticks=violating_price,
        )
        self._reject(managed, "VOLATILITY_INTERRUPTION")
        self.transition_session(
            SessionState.HALTED,
            reason="VOLATILITY_INTERRUPTION",
        )
        return True

    def _execution_collar_interrupted(
        self,
        managed: ManagedOrder,
        makers: list[Order],
    ) -> bool:
        collar = self.rules.price_collar_ticks
        if collar is None or not makers:
            return False
        reference = self._reference_price()
        violating_price = next(
            (
                int(order.price_ticks)
                for order in makers
                if abs(int(order.price_ticks) - reference) > collar
            ),
            None,
        )
        if violating_price is None:
            return False
        self._emit(
            MechanicsEventType.PROTECTION_TRIGGERED,
            order_id=managed.request.order_id,
            protection="PRICE_COLLAR",
            reference_price_ticks=reference,
            violating_price_ticks=violating_price,
        )
        self._reject(managed, "EXECUTION_PRICE_OUTSIDE_COLLAR")
        return True

    def _process_continuous(self, managed: ManagedOrder) -> None:
        request = managed.request
        trade_start = len(self.book.trades)
        core = (
            Order.market(
                request.order_id,
                request.side,
                request.quantity,
                request.owner,
            )
            if request.instruction is OrderInstruction.MARKET
            else Order.limit(
                request.order_id,
                request.side,
                request.quantity,
                int(request.price_ticks),
                request.owner,
            )
        )
        event_start = len(self.book.journal.events)
        self.book.process(core)
        self._sync_continuous_orders()
        self._emit(
            MechanicsEventType.ORDER_ACCEPTED,
            core_event_end=len(self.book.journal.events),
            core_event_start=event_start + 1,
            order_id=request.order_id,
            status=managed.status,
        )
        for trade in self.book.trades[trade_start:]:
            self._last_trade_price_ticks = trade.price_ticks
            self._emit(
                MechanicsEventType.TRADE,
                maker_order_id=trade.maker_order_id,
                price_ticks=trade.price_ticks,
                quantity=trade.quantity,
                taker_order_id=trade.taker_order_id,
                trade_id=trade.trade_id,
            )
        core_state = self.book.all_orders[request.order_id]
        if (
            request.time_in_force is OrderInstruction.IOC
            and request.order_id in self.book.active_orders
        ):
            self._expire_continuous(managed, "IOC_REMAINDER")
        elif core_state.status is OrderStatus.EXPIRED:
            managed.status = "EXPIRED"
            managed.expired_quantity = core_state.cancelled_quantity
            managed.cancelled_quantity = 0
            self._emit(
                MechanicsEventType.ORDER_EXPIRED,
                expired_quantity=managed.expired_quantity,
                order_id=request.order_id,
                reason=(
                    "IOC_REMAINDER"
                    if request.time_in_force is OrderInstruction.IOC
                    else "MARKET_UNFILLED_REMAINDER"
                ),
            )

    def _potential_makers(self, request: AdvancedOrderRequest) -> list[Order]:
        prices = self.book.ask_prices if request.side is Side.BUY else self.book.bid_prices
        levels = self.book.asks if request.side is Side.BUY else self.book.bids
        remaining = request.quantity
        makers: list[Order] = []
        for price in prices:
            if request.instruction is not OrderInstruction.MARKET:
                if (
                    request.side is Side.BUY
                    and request.price_ticks < price  # type: ignore[operator]
                ):
                    break
                if (
                    request.side is Side.SELL
                    and request.price_ticks > price  # type: ignore[operator]
                ):
                    break
            for order in levels[price].orders:
                makers.append(order)
                remaining -= min(remaining, order.remaining_quantity)
                if remaining == 0:
                    return makers
        return makers

    def _sync_continuous_orders(self) -> None:
        all_orders = self.book.all_orders
        for managed in self.orders:
            if managed.request.auction_only:
                continue
            core = all_orders.get(managed.request.order_id)
            if core is None:
                continue
            managed.filled_quantity = core.filled_quantity
            managed.resting_sequence = core.resting_sequence
            managed.cancelled_quantity = core.cancelled_quantity
            managed.expired_quantity = 0
            managed.status = {
                OrderStatus.NEW: "PENDING",
                OrderStatus.ACTIVE: "WORKING",
                OrderStatus.PARTIALLY_FILLED: "PARTIALLY_FILLED",
                OrderStatus.FILLED: "FILLED",
                OrderStatus.CANCELLED: "CANCELLED",
                OrderStatus.EXPIRED: "EXPIRED",
            }.get(core.status, core.status.value.upper())
            if core.status is OrderStatus.EXPIRED:
                managed.expired_quantity = core.cancelled_quantity
                managed.cancelled_quantity = 0

    def _expire_continuous(self, managed: ManagedOrder, reason: str) -> None:
        remaining = managed.remaining_quantity
        if remaining <= 0 or managed.request.order_id not in self.book.active_orders:
            return
        previous_cancelled = managed.cancelled_quantity
        command_id = self._next_command_id("MECH-EXPIRE")
        self.book.cancel(managed.request.order_id, command_id)
        self._sync_continuous_orders()
        managed.expired_quantity += remaining
        managed.cancelled_quantity = previous_cancelled
        managed.status = "EXPIRED"
        self._emit(
            MechanicsEventType.ORDER_EXPIRED,
            command_id=command_id,
            expired_quantity=remaining,
            order_id=managed.request.order_id,
            reason=reason,
        )

    def _expire_time_in_force(
        self,
        instructions: set[OrderInstruction],
        reason: str,
    ) -> None:
        for managed in self.orders:
            if (
                self._is_managed_active(managed)
                and not managed.request.auction_only
                and managed.request.time_in_force in instructions
            ):
                self._expire_continuous(managed, reason)

    def _expire_good_until(self, simulation_time_us: int) -> None:
        for managed in self.orders:
            if (
                not self._is_managed_active(managed)
                or managed.request.time_in_force
                is not OrderInstruction.GOOD_UNTIL_TIME
                or managed.request.good_until_time_us is None
                or managed.request.good_until_time_us > simulation_time_us
            ):
                continue
            if managed.request.auction_only:
                quantity = self.auction.expire(managed.request.order_id)
                self._emit(
                    MechanicsEventType.ORDER_EXPIRED,
                    expired_quantity=quantity,
                    order_id=managed.request.order_id,
                    reason="GOOD_UNTIL_TIME",
                )
                self._emit_auction_indication("GOOD_UNTIL_TIME")
            else:
                self._expire_continuous(managed, "GOOD_UNTIL_TIME")

    def _expire_all_auction_orders(self, reason: str) -> None:
        expired = False
        for managed in self.auction.active_orders:
            quantity = self.auction.expire(managed.request.order_id)
            self._emit(
                MechanicsEventType.ORDER_EXPIRED,
                expired_quantity=quantity,
                order_id=managed.request.order_id,
                reason=reason,
            )
            expired = True
        if expired:
            self._emit_auction_indication(reason)

    def _is_managed_active(self, managed: ManagedOrder) -> bool:
        if managed.remaining_quantity <= 0:
            return False
        if managed.request.auction_only:
            return managed.status in {"AUCTION_WORKING", "PARTIALLY_FILLED"}
        return managed.request.order_id in self.book.active_orders

    def _emit_auction_indication(self, reason: str) -> None:
        indication = self.auction.indication(self._reference_price())
        self._emit(
            MechanicsEventType.AUCTION_INDICATION,
            indication=indication.as_dict(),
            reason=reason,
            session_state=self.session_state.value,
        )

    def _account_for_order(self, order_id: str) -> str | None:
        managed = self._orders.get(order_id)
        return None if managed is None else managed.request.account_id

    def _next_command_id(self, prefix: str) -> str:
        while True:
            self._command_sequence += 1
            command_id = f"{prefix}-{self._command_sequence:06d}"
            if command_id not in self.book.all_orders and command_id not in self._orders:
                return command_id

    def _reference_price(self) -> int:
        return (
            self.rules.reference_price_ticks
            if self._last_trade_price_ticks is None
            else self._last_trade_price_ticks
        )

    def _emit(self, event_type: MechanicsEventType, **data: object) -> None:
        self._events.append(
            MechanicsEvent(
                len(self._events) + 1,
                self.clock.current_time_us,
                event_type,
                dict(data),
            )
        )


class MechanicsTimelineInspector:
    def __init__(self, events: tuple[MechanicsEvent, ...]) -> None:
        self.events = events

    def render(self) -> str:
        lines = []
        for event in self.events:
            details = _timeline_details(event)
            lines.append(
                f"{_market_time(event.simulation_time_us)}  "
                f"{event.event_type.value}{details}"
            )
        return "\n".join(lines)


def _timeline_details(event: MechanicsEvent) -> str:
    keys = (
        "order_id",
        "reason",
        "current_state",
        "status",
        "price_ticks",
        "quantity",
        "expired_quantity",
        "actual_matched_quantity",
        "protection",
    )
    values = [f"{key}={event.data[key]}" for key in keys if key in event.data]
    indication = event.data.get("indication")
    if isinstance(indication, dict):
        values.extend(
            f"indicative_{key}={indication.get(key)}"
            for key in (
                "clearing_price_ticks",
                "matched_quantity",
                "imbalance_quantity",
                "imbalance_side",
            )
        )
    return "" if not values else " " + " ".join(values)


def _market_time(simulation_time_us: int) -> str:
    total_us = (9 * 60 * 60 + 30 * 60) * 1_000_000 + simulation_time_us
    hours, remainder = divmod(total_us, 3_600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    seconds, microseconds = divmod(remainder, 1_000_000)
    return (
        f"{hours % 24:02d}:{minutes:02d}:{seconds:02d}."
        f"{microseconds:06d}"
    )
