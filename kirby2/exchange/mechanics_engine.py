"""Configurable deterministic session, protection, instruction, and auction engine."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from kirby2.immutable import freeze_json, thaw_json
from kirby2.session.events import EventType
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
from .models import Order, OrderOwner, OrderStatus, OrderView, Side


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


MARKET_MECHANICS_CHECKPOINT_SCHEMA_VERSION = 1
_MECHANICS_COMMAND_ID = re.compile(r"^MECH-[A-Z]+-([0-9]{6})$")


def _validate_strict_checkpoint_json(
    value: object,
    active: set[int] | None = None,
) -> None:
    active = set() if active is None else active
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("checkpoint JSON strings must be NFC-normalized")
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("checkpoint JSON strings must be Unicode scalar values")
        return
    if type(value) is float:
        raise TypeError("binary floats are forbidden in checkpoint JSON")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("checkpoint JSON object keys must be strings")
        identity = id(value)
        if identity in active:
            raise ValueError("checkpoint JSON must not contain reference cycles")
        active.add(identity)
        try:
            for key in sorted(value):
                _validate_strict_checkpoint_json(key, active)
                _validate_strict_checkpoint_json(value[key], active)
        finally:
            active.remove(identity)
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise ValueError("checkpoint JSON must not contain reference cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_strict_checkpoint_json(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported checkpoint JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: object) -> bytes:
    _validate_strict_checkpoint_json(value)
    detached = thaw_json(freeze_json(value))
    return json.dumps(
        detached,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_canonical_json_object(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes:
        raise TypeError("canonical market-mechanics state must be bytes")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_float=lambda _value: (_ for _ in ()).throw(
                TypeError("decimal JSON numbers are forbidden in checkpoint state")
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number is forbidden: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("market-mechanics state is not canonical UTF-8 JSON") from error
    if type(value) is not dict:
        raise ValueError("market-mechanics state must be a JSON object")
    if _canonical_json_bytes(value) != payload:
        raise ValueError("market-mechanics state bytes are not canonical")
    return value


def _require_exact_fields(
    payload: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )


def _require_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        bound = "" if minimum is None else f" at least {minimum}"
        raise ValueError(f"{label} must be an integer{bound}")
    return value


def _require_optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, label, minimum=1)


def _plain_json_object(value: object, label: str) -> dict[str, object]:
    _validate_strict_checkpoint_json(value)
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{label} must be an object")
    detached = thaw_json(frozen)
    if type(detached) is not dict:
        raise RuntimeError("detached checkpoint object is not a dictionary")
    return detached


def _managed_order_from_checkpoint(payload: Mapping[str, object]) -> ManagedOrder:
    _require_exact_fields(
        payload,
        frozenset(
            {
                "arrival_sequence",
                "cancelled_quantity",
                "expired_quantity",
                "filled_quantity",
                "remaining_quantity",
                "request",
                "resting_sequence",
                "status",
            }
        ),
        "managed order",
    )
    raw_request = _plain_json_object(payload["request"], "managed order request")
    request = AdvancedOrderRequest.from_dict(raw_request)
    if _canonical_json_bytes(request.as_dict()) != _canonical_json_bytes(raw_request):
        raise ValueError("managed order request is not strict canonical state")
    status = payload["status"]
    if type(status) is not str or not status:
        raise ValueError("managed order status must be a nonempty string")
    resting_sequence = payload["resting_sequence"]
    if resting_sequence is not None:
        resting_sequence = _require_int(
            resting_sequence,
            "managed resting sequence",
            minimum=1,
        )
    order = ManagedOrder(
        request=request,
        arrival_sequence=_require_int(
            payload["arrival_sequence"],
            "managed arrival sequence",
            minimum=1,
        ),
        status=status,
        filled_quantity=_require_int(
            payload["filled_quantity"],
            "managed filled quantity",
            minimum=0,
        ),
        cancelled_quantity=_require_int(
            payload["cancelled_quantity"],
            "managed cancelled quantity",
            minimum=0,
        ),
        expired_quantity=_require_int(
            payload["expired_quantity"],
            "managed expired quantity",
            minimum=0,
        ),
        resting_sequence=resting_sequence,
    )
    _require_int(
        payload["remaining_quantity"],
        "managed remaining quantity",
        minimum=0,
    )
    if _canonical_json_bytes(order.as_dict()) != _canonical_json_bytes(payload):
        raise ValueError("managed order quantities or canonical fields are inconsistent")
    return order


def _mechanics_event_from_checkpoint(payload: Mapping[str, object]) -> MechanicsEvent:
    _require_exact_fields(
        payload,
        frozenset({"data", "event_type", "sequence", "simulation_time_us"}),
        "market-mechanics event",
    )
    event_type = payload["event_type"]
    if type(event_type) is not str:
        raise ValueError("market-mechanics event type must be a string")
    data = _plain_json_object(payload["data"], "market-mechanics event data")
    event = MechanicsEvent(
        sequence=_require_int(
            payload["sequence"],
            "market-mechanics event sequence",
            minimum=1,
        ),
        simulation_time_us=_require_int(
            payload["simulation_time_us"],
            "market-mechanics event time",
            minimum=0,
        ),
        event_type=MechanicsEventType(event_type),
        data=data,
    )
    if _canonical_json_bytes(event.as_dict()) != _canonical_json_bytes(payload):
        raise ValueError("market-mechanics event is not strict canonical state")
    return event


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
        self._validating_outer_replay = False

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
        self.assert_invariants()
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
        if (
            type(self.clock) is not SimulationClock
            or type(self.clock.current_time_us) is not int
            or self.clock.current_time_us < 0
        ):
            raise RuntimeError("market-mechanics clock state is invalid")
        self.book.assert_invariants()
        self.auction.assert_invariants()
        sequences = [event.sequence for event in self._events]
        times = [event.simulation_time_us for event in self._events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeError("market-mechanics event sequence is not contiguous")
        if times != sorted(times):
            raise RuntimeError("market-mechanics event times are not monotonic")
        if times and times[-1] > self.clock.current_time_us:
            raise RuntimeError("market-mechanics event occurs after its clock")
        arrival_sequences = [order.arrival_sequence for order in self.orders]
        if len(arrival_sequences) != len(set(arrival_sequences)):
            raise RuntimeError("managed order arrival sequences are duplicated")
        if arrival_sequences != list(range(1, len(arrival_sequences) + 1)):
            raise RuntimeError("managed order arrival sequence is not contiguous")
        if self._arrival_sequence != len(arrival_sequences):
            raise RuntimeError("managed order arrival allocator is inconsistent")
        if len(self._orders) != len({order.request.order_id for order in self.orders}):
            raise RuntimeError("managed order IDs are duplicated")
        if any(order.status == "PENDING" for order in self.orders):
            raise RuntimeError("pending managed order cannot be checkpointed")

        transitions = self.rules.session_schedule.transitions
        if not 0 <= self._schedule_index <= len(transitions):
            raise RuntimeError("session schedule cursor is out of range")
        if any(
            item.simulation_time_us > self.clock.current_time_us
            for item in transitions[: self._schedule_index]
        ) or any(
            item.simulation_time_us < self.clock.current_time_us
            for item in transitions[self._schedule_index :]
        ):
            raise RuntimeError("session schedule cursor disagrees with the clock")

        observed_command_sequences: list[int] = []
        observed_command_ids: list[str] = []
        for event in self._events:
            command_id = event.data.get("command_id")
            if command_id is None:
                continue
            if type(command_id) is not str:
                raise RuntimeError("mechanics command ID is not a string")
            match = _MECHANICS_COMMAND_ID.fullmatch(command_id)
            if match is None:
                raise RuntimeError("mechanics command ID is malformed")
            observed_command_ids.append(command_id)
            observed_command_sequences.append(int(match.group(1)))
        if self._command_sequence != max(observed_command_sequences, default=0):
            raise RuntimeError("mechanics command allocator is inconsistent")
        if len(observed_command_ids) != len(set(observed_command_ids)):
            raise RuntimeError("mechanics command IDs are duplicated")
        core_command_ids = {
            order_id
            for order_id, order in self.book.all_orders.items()
            if order.order_type.value == "cancel"
        }
        if core_command_ids != set(observed_command_ids):
            raise RuntimeError(
                "mechanics commands differ from the FIFO command ledger"
            )

        for managed in self.orders:
            if managed.status not in _MANAGED_ORDER_STATUSES:
                raise RuntimeError("managed order status is outside the lifecycle vocabulary")
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
            if managed.status == "REJECTED":
                if core is not None:
                    raise RuntimeError("rejected managed order exists in FIFO ledger")
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
            expected_status = {
                OrderStatus.NEW: "PENDING",
                OrderStatus.ACTIVE: "WORKING",
                OrderStatus.PARTIALLY_FILLED: "PARTIALLY_FILLED",
                OrderStatus.FILLED: "FILLED",
                OrderStatus.CANCELLED: "CANCELLED",
                OrderStatus.EXPIRED: "EXPIRED",
            }.get(core.status, core.status.value.upper())
            if core.status is OrderStatus.EXPIRED or managed.expired_quantity:
                expected_status = "EXPIRED"
            if managed.status != expected_status:
                raise RuntimeError("managed order status differs from FIFO ledger")

        for order_id, core in self.book.all_orders.items():
            if core.order_type.value == "cancel":
                continue
            managed = self._orders.get(order_id)
            if managed is None or managed.request.auction_only:
                raise RuntimeError(
                    "FIFO trading order is absent from the continuous managed ledger"
                )
            expected_type = (
                "market"
                if managed.request.instruction is OrderInstruction.MARKET
                else "limit"
            )
            if (
                core.order_type.value != expected_type
                or core.original_quantity != managed.request.quantity
                or core.side is not managed.request.side
                or core.price_ticks != managed.request.price_ticks
                or core.owner is not managed.request.owner
            ):
                raise RuntimeError(
                    "FIFO order identity differs from its managed request"
                )

        expected_auction_ids = {
            order.request.order_id
            for order in self.orders
            if order.request.auction_only and order.status != "REJECTED"
        }
        actual_auction_ids = {
            order.request.order_id for order in self.auction.orders
        }
        if actual_auction_ids != expected_auction_ids:
            raise RuntimeError("auction order inventory differs from managed orders")
        if any(
            self._orders[order.request.order_id] is not order
            for order in self.auction.orders
        ):
            raise RuntimeError("auction and mechanics do not share owned order records")

        expected_player_fills = tuple(
            fill for fill in self.book.fills if fill.owner is OrderOwner.PLAYER
        )
        if tuple(self.book.player_position.fills) != expected_player_fills:
            raise RuntimeError("player fill history differs from the FIFO fill ledger")

        mechanics_trades = [
            event for event in self._events if event.event_type is MechanicsEventType.TRADE
        ]
        if len(mechanics_trades) != len(self.book.trades):
            raise RuntimeError("mechanics trade history differs from FIFO trades")
        for event, trade in zip(mechanics_trades, self.book.trades, strict=True):
            if (
                event.data.get("trade_id") != trade.trade_id
                or event.data.get("maker_order_id") != trade.maker_order_id
                or event.data.get("taker_order_id") != trade.taker_order_id
                or event.data.get("price_ticks") != trade.price_ticks
                or event.data.get("quantity") != trade.quantity
            ):
                raise RuntimeError("mechanics trade row differs from FIFO trade")

        mechanics_auction_fills = [
            event
            for event in self._events
            if event.event_type is MechanicsEventType.AUCTION_FILL
        ]
        if len(mechanics_auction_fills) != len(self.auction.executions):
            raise RuntimeError(
                "mechanics auction-fill history differs from auction executions"
            )
        for event, execution in zip(
            mechanics_auction_fills,
            self.auction.executions,
            strict=True,
        ):
            if (
                event.data.get("trade_id") != execution.trade_id
                or event.data.get("buy_order_id") != execution.buy_order_id
                or event.data.get("sell_order_id") != execution.sell_order_id
                or event.data.get("price_ticks") != execution.price_ticks
                or event.data.get("quantity") != execution.quantity
            ):
                raise RuntimeError(
                    "mechanics auction-fill row differs from auction execution"
                )

        derived_last_trade: int | None = None
        for event in self._events:
            if event.event_type in {
                MechanicsEventType.TRADE,
                MechanicsEventType.AUCTION_FILL,
            }:
                price = event.data.get("price_ticks")
                if type(price) is not int or price <= 0:
                    raise RuntimeError("mechanics trade price is invalid")
                derived_last_trade = price
        if self._last_trade_price_ticks != derived_last_trade:
            raise RuntimeError("last-trade price differs from mechanics events")

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
        if not self._validating_outer_replay:
            _validate_outer_mechanics_lifecycle(self, strict_schedule=False)

    def event_stream_sha256(self) -> str:
        canonical = json.dumps(
            [event.as_dict() for event in self.events],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def checkpoint_state(self) -> dict[str, object]:
        """Return the complete mechanics-core state for fresh-process restore.

        This deliberately remains separate from :meth:`state_sha256`'s legacy
        projection.  The checkpoint owns allocator high-water marks and nests
        the strict book, journal, player-position, clock, and auction states.
        """

        self.assert_invariants()
        _validate_outer_mechanics_lifecycle(self, strict_schedule=True)
        payload: dict[str, object] = {
            "allocators": {
                "arrival_sequence": self._arrival_sequence,
                "command_sequence": self._command_sequence,
            },
            "auction": self.auction.checkpoint_state(),
            "auction_player_position": self._auction_player_position,
            "book": self.book.checkpoint_state(),
            "clock": self.clock.checkpoint_state(),
            "events": [event.as_dict() for event in self._events],
            "last_trade_price_ticks": self._last_trade_price_ticks,
            "managed_orders": [order.as_dict() for order in self.orders],
            "rules": self.rules.as_dict(),
            "schedule_index": self._schedule_index,
            "schema_version": MARKET_MECHANICS_CHECKPOINT_SCHEMA_VERSION,
            "session_state": self.session_state.value,
        }
        _validate_strict_checkpoint_json(payload)
        return payload

    def canonical_state_bytes(self) -> bytes:
        return _canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> MarketMechanicsEngine:
        """Validate and detach a checkpoint before returning a restored engine."""

        if not isinstance(payload, Mapping):
            raise TypeError("market-mechanics checkpoint state must be a mapping")
        _validate_strict_checkpoint_json(payload)
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "allocators",
                    "auction",
                    "auction_player_position",
                    "book",
                    "clock",
                    "events",
                    "last_trade_price_ticks",
                    "managed_orders",
                    "rules",
                    "schedule_index",
                    "schema_version",
                    "session_state",
                }
            ),
            "market-mechanics checkpoint",
        )
        schema_version = _require_int(
            payload["schema_version"],
            "market-mechanics schema version",
            minimum=1,
        )
        if schema_version != MARKET_MECHANICS_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported market-mechanics checkpoint schema")

        raw_rules = _plain_json_object(payload["rules"], "instrument rules")
        rules = InstrumentRules.from_dict(raw_rules)
        if _canonical_json_bytes(rules.as_dict()) != _canonical_json_bytes(raw_rules):
            raise ValueError("instrument rules are not strict canonical state")

        raw_clock = _plain_json_object(payload["clock"], "simulation clock")
        clock = SimulationClock.from_checkpoint_state(raw_clock)

        raw_events = payload["events"]
        if type(raw_events) is not list:
            raise ValueError("market-mechanics events must be an ordered array")
        events = [
            _mechanics_event_from_checkpoint(raw_event)
            if type(raw_event) is dict
            else (_ for _ in ()).throw(
                ValueError("market-mechanics event rows must be objects")
            )
            for raw_event in raw_events
        ]
        if [event.sequence for event in events] != list(
            range(1, len(events) + 1)
        ):
            raise ValueError("market-mechanics events must form a contiguous prefix")
        event_times = [event.simulation_time_us for event in events]
        if event_times != sorted(event_times):
            raise ValueError("market-mechanics event times must be monotonic")
        if event_times and event_times[-1] > clock.current_time_us:
            raise ValueError("market-mechanics event occurs after the restored clock")

        raw_orders = payload["managed_orders"]
        if type(raw_orders) is not list:
            raise ValueError("managed orders must be an ordered array")
        orders = [
            _managed_order_from_checkpoint(raw_order)
            if type(raw_order) is dict
            else (_ for _ in ()).throw(ValueError("managed order rows must be objects"))
            for raw_order in raw_orders
        ]
        order_ids = [order.request.order_id for order in orders]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("managed order IDs must be unique")
        arrivals = [order.arrival_sequence for order in orders]
        if arrivals != list(range(1, len(orders) + 1)):
            raise ValueError(
                "managed orders must be in contiguous arrival-sequence order"
            )
        orders_by_id = {order.request.order_id: order for order in orders}

        allocators = _plain_json_object(payload["allocators"], "mechanics allocators")
        _require_exact_fields(
            allocators,
            frozenset({"arrival_sequence", "command_sequence"}),
            "mechanics allocators",
        )
        arrival_sequence = _require_int(
            allocators["arrival_sequence"],
            "mechanics arrival allocator",
            minimum=0,
        )
        command_sequence = _require_int(
            allocators["command_sequence"],
            "mechanics command allocator",
            minimum=0,
        )
        if arrival_sequence != len(orders):
            raise ValueError(
                "mechanics arrival allocator does not follow managed-order history"
            )
        observed_command_sequences: list[int] = []
        for event in events:
            command_id = event.data.get("command_id")
            if command_id is None:
                continue
            if type(command_id) is not str:
                raise ValueError("mechanics command event has a non-string command ID")
            match = _MECHANICS_COMMAND_ID.fullmatch(command_id)
            if match is None:
                raise ValueError("mechanics command event has a malformed command ID")
            observed_command_sequences.append(int(match.group(1)))
        expected_command_sequence = max(observed_command_sequences, default=0)
        if command_sequence != expected_command_sequence:
            raise ValueError(
                "mechanics command allocator does not follow emitted commands"
            )

        session_state_value = payload["session_state"]
        if type(session_state_value) is not str:
            raise ValueError("mechanics session state must be a string")
        session_state = SessionState(session_state_value)
        schedule_index = _require_int(
            payload["schedule_index"],
            "mechanics schedule cursor",
            minimum=0,
        )
        transitions = rules.session_schedule.transitions
        if schedule_index > len(transitions):
            raise ValueError("mechanics schedule cursor exceeds its schedule")
        if any(
            item.simulation_time_us > clock.current_time_us
            for item in transitions[:schedule_index]
        ) or any(
            item.simulation_time_us < clock.current_time_us
            for item in transitions[schedule_index:]
        ):
            raise ValueError("mechanics schedule cursor disagrees with simulation time")

        last_trade_price_ticks = _require_optional_positive_int(
            payload["last_trade_price_ticks"],
            "mechanics last-trade price",
        )
        derived_last_trade: int | None = None
        for event in events:
            if event.event_type not in {
                MechanicsEventType.TRADE,
                MechanicsEventType.AUCTION_FILL,
            }:
                continue
            derived_last_trade = _require_int(
                event.data.get("price_ticks"),
                "mechanics trade-event price",
                minimum=1,
            )
        if last_trade_price_ticks != derived_last_trade:
            raise ValueError("mechanics last-trade price disagrees with its event prefix")

        auction_player_position = _require_int(
            payload["auction_player_position"],
            "mechanics auction player position",
        )
        expected_auction_position = 0
        for event in events:
            if event.event_type is not MechanicsEventType.AUCTION_FILL:
                continue
            buy_order_id = event.data.get("buy_order_id")
            sell_order_id = event.data.get("sell_order_id")
            quantity = _require_int(
                event.data.get("quantity"),
                "auction fill quantity",
                minimum=1,
            )
            if type(buy_order_id) is not str or type(sell_order_id) is not str:
                raise ValueError("auction fill order IDs must be strings")
            try:
                buy = orders_by_id[buy_order_id]
                sell = orders_by_id[sell_order_id]
            except KeyError as error:
                raise ValueError(
                    "auction fill references an unknown managed order"
                ) from error
            if buy.request.owner is OrderOwner.PLAYER:
                expected_auction_position += quantity
            if sell.request.owner is OrderOwner.PLAYER:
                expected_auction_position -= quantity
        if auction_player_position != expected_auction_position:
            raise ValueError(
                "mechanics auction position does not reconcile to auction fills"
            )

        raw_book = _plain_json_object(payload["book"], "order-book checkpoint")
        book = OrderBook.from_checkpoint_state(raw_book)
        raw_auction = _plain_json_object(payload["auction"], "auction-book checkpoint")
        auction = AuctionBook.from_checkpoint_state(
            raw_auction,
            managed_orders=orders_by_id,
        )
        restored_orders_by_id = dict(orders_by_id)
        restored_auction_ids: set[str] = set()
        for auction_order in auction.orders:
            order_id = auction_order.request.order_id
            if order_id in restored_auction_ids:
                raise ValueError("restored auction contains a duplicate managed order")
            witness = orders_by_id.get(order_id)
            if witness is None or not witness.request.auction_only:
                raise ValueError(
                    "restored auction order has no auction managed-order witness"
                )
            if auction_order is witness:
                raise ValueError("restored auction retained a managed-order witness")
            restored_auction_ids.add(order_id)
            restored_orders_by_id[order_id] = auction_order
        expected_auction_ids = {
            order_id
            for order_id, order in orders_by_id.items()
            if order.request.auction_only and order.status != "REJECTED"
        }
        if restored_auction_ids != expected_auction_ids:
            raise ValueError(
                "restored auction inventory differs from managed-order witnesses"
            )

        engine = cls(rules=rules)
        engine.clock = clock
        engine.book = book
        engine.auction = auction
        engine.session_state = session_state
        engine._events = events
        engine._orders = restored_orders_by_id
        engine._arrival_sequence = arrival_sequence
        engine._command_sequence = command_sequence
        engine._schedule_index = schedule_index
        engine._last_trade_price_ticks = last_trade_price_ticks
        engine._auction_player_position = auction_player_position
        engine.assert_invariants()
        if _canonical_json_bytes(engine.checkpoint_state()) != _canonical_json_bytes(
            payload
        ):
            raise ValueError("market-mechanics checkpoint state is not canonical")
        return engine

    @classmethod
    def from_canonical_state_bytes(
        cls,
        payload: bytes,
    ) -> MarketMechanicsEngine:
        return cls.from_checkpoint_state(_load_canonical_json_object(payload))

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
        makers: list[OrderView],
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
        makers: list[OrderView],
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

    def _potential_makers(self, request: AdvancedOrderRequest) -> list[OrderView]:
        prices = self.book.ask_prices if request.side is Side.BUY else self.book.bid_prices
        levels = self.book.asks if request.side is Side.BUY else self.book.bids
        remaining = request.quantity
        makers: list[OrderView] = []
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
            classified_expired = managed.expired_quantity
            if classified_expired > core.cancelled_quantity:
                raise RuntimeError(
                    "managed expired quantity exceeds core closed quantity"
                )
            managed.filled_quantity = core.filled_quantity
            managed.resting_sequence = core.resting_sequence
            managed.cancelled_quantity = (
                core.cancelled_quantity - classified_expired
            )
            managed.expired_quantity = classified_expired
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
            elif managed.expired_quantity:
                managed.status = "EXPIRED"

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


_EVENT_FIELD_VARIANTS: dict[
    MechanicsEventType,
    tuple[frozenset[str], ...],
] = {
    MechanicsEventType.ORDER_ACCEPTED: (
        frozenset({"core_event_end", "core_event_start", "order_id", "status"}),
    ),
    MechanicsEventType.ORDER_REJECTED: (
        frozenset({"order_id", "reason"}),
        frozenset({"filled_quantity", "order_id", "reason"}),
    ),
    MechanicsEventType.ORDER_CANCELLED: (
        frozenset({"cancelled_quantity", "command_id", "order_id", "reason"}),
    ),
    MechanicsEventType.ORDER_EXPIRED: (
        frozenset({"expired_quantity", "order_id", "reason"}),
        frozenset({"command_id", "expired_quantity", "order_id", "reason"}),
    ),
    MechanicsEventType.ORDER_REPLACED: (
        frozenset(
            {
                "new_order_id",
                "old_order_id",
                "priority_preserved",
                "replacement_request_id",
            }
        ),
        frozenset(
            {
                "filled_quantity_before_replace",
                "new_order_id",
                "new_total_quantity",
                "old_order_id",
                "replacement_accepted",
                "replacement_leaves_quantity",
            }
        ),
    ),
    MechanicsEventType.PRIORITY_PRESERVED: (
        frozenset(
            {
                "command_id",
                "new_total_quantity",
                "order_id",
                "replacement_request_id",
                "resting_sequence",
            }
        ),
    ),
    MechanicsEventType.PRIORITY_LOST: (
        frozenset({"new_order_id", "old_order_id", "reason"}),
    ),
    MechanicsEventType.SELF_TRADE_PREVENTION: (
        frozenset({"mode", "order_id", "phase"}),
        frozenset({"aggressor_order_id", "mode", "resting_order_ids"}),
        frozenset(
            {
                "aggressor_order_id",
                "command_id",
                "mode",
                "resting_order_id",
            }
        ),
    ),
    MechanicsEventType.TRADE: (
        frozenset(
            {
                "maker_order_id",
                "price_ticks",
                "quantity",
                "taker_order_id",
                "trade_id",
            }
        ),
    ),
    MechanicsEventType.SESSION_STATE_CHANGED: (
        frozenset({"current_state", "previous_state", "reason"}),
    ),
    MechanicsEventType.AUCTION_ORDER_ADDED: (
        frozenset({"order_id", "request"}),
    ),
    MechanicsEventType.AUCTION_ORDER_CANCELLED: (
        frozenset({"cancelled_quantity", "order_id", "reason"}),
    ),
    MechanicsEventType.AUCTION_INDICATION: (
        frozenset({"indication", "reason", "session_state"}),
    ),
    MechanicsEventType.AUCTION_UNCROSS: (
        frozenset({"actual_matched_quantity", "indication", "session_state"}),
    ),
    MechanicsEventType.AUCTION_FILL: (
        frozenset(
            {
                "buy_order_id",
                "price_ticks",
                "quantity",
                "sell_order_id",
                "trade_id",
            }
        ),
    ),
    MechanicsEventType.PROTECTION_TRIGGERED: (
        frozenset({"order_id", "protection", "reason"}),
        frozenset(
            {
                "order_id",
                "protection",
                "reference_price_ticks",
                "violating_price_ticks",
            }
        ),
    ),
    MechanicsEventType.HALT: (frozenset({"reason"}),),
    MechanicsEventType.RESUME: (frozenset({"reason"}),),
}

_MANAGED_ORDER_STATUSES = frozenset(
    {
        "AUCTION_WORKING",
        "CANCELLED",
        "CANCELLED_STP",
        "EXPIRED",
        "FILLED",
        "PARTIALLY_FILLED",
        "REJECTED",
        "WORKING",
    }
)


def _event_text(data: Mapping[str, object], field: str) -> str:
    value = data[field]
    if type(value) is not str or not value:
        raise RuntimeError(f"mechanics event {field} must be nonempty text")
    return value


def _event_int(
    data: Mapping[str, object],
    field: str,
    *,
    minimum: int = 0,
) -> int:
    value = data[field]
    if type(value) is not int or value < minimum:
        raise RuntimeError(
            f"mechanics event {field} must be an integer >= {minimum}"
        )
    return value


def _validate_indication_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError("auction indication must be an object")
    _require_exact_fields(
        value,
        frozenset(
            {
                "clearing_price_ticks",
                "imbalance_quantity",
                "imbalance_side",
                "matched_quantity",
            }
        ),
        "auction indication",
    )
    price = value["clearing_price_ticks"]
    if price is not None and (type(price) is not int or price <= 0):
        raise RuntimeError("auction clearing price must be positive ticks or null")
    _event_int(value, "imbalance_quantity")
    _event_int(value, "matched_quantity")
    side = value["imbalance_side"]
    if side is not None:
        if type(side) is not str:
            raise RuntimeError("auction imbalance side must be text or null")
        Side(side)
    if price is None and (
        value["matched_quantity"] != 0
        or value["imbalance_quantity"] != 0
        or side is not None
    ):
        raise RuntimeError("empty auction indication carries nonempty values")
    return dict(value)


def _validate_mechanics_event_shape(event: MechanicsEvent) -> None:
    variants = _EVENT_FIELD_VARIANTS[event.event_type]
    fields = frozenset(event.data)
    if fields not in variants:
        raise RuntimeError(
            f"{event.event_type.value} fields are not exact: {sorted(fields)}"
        )
    data = event.data
    for field in fields:
        if field.endswith("order_id") or field in {
            "aggressor_order_id",
            "command_id",
            "new_order_id",
            "old_order_id",
            "replacement_request_id",
            "trade_id",
        }:
            _event_text(data, field)
    for field in fields:
        if field.endswith("quantity") or field in {
            "actual_matched_quantity",
            "core_event_end",
            "core_event_start",
            "filled_quantity_before_replace",
            "new_total_quantity",
            "replacement_leaves_quantity",
            "resting_sequence",
        }:
            minimum = 1 if field in {
                "cancelled_quantity",
                "core_event_end",
                "core_event_start",
                "expired_quantity",
                "new_total_quantity",
                "quantity",
                "replacement_leaves_quantity",
                "resting_sequence",
            } else 0
            _event_int(data, field, minimum=minimum)
    for field in fields & {
        "price_ticks",
        "reference_price_ticks",
        "violating_price_ticks",
    }:
        _event_int(data, field, minimum=1)
    for field in fields & {
        "current_state",
        "mode",
        "phase",
        "previous_state",
        "protection",
        "reason",
        "session_state",
        "status",
    }:
        _event_text(data, field)
    if "priority_preserved" in fields and data["priority_preserved"] is not True:
        raise RuntimeError("priority-preserved event must carry true")
    if "replacement_accepted" in fields and type(data["replacement_accepted"]) is not bool:
        raise RuntimeError("replacement accepted flag must be boolean")
    if "resting_order_ids" in fields:
        values = data["resting_order_ids"]
        if (
            type(values) is not tuple
            or not values
            or any(type(value) is not str or not value for value in values)
            or len(values) != len(set(values))
        ):
            raise RuntimeError("resting order IDs must be unique nonempty text")
    if "request" in fields:
        request = data["request"]
        if not isinstance(request, Mapping):
            raise RuntimeError("auction-added request must be an object")
        detached_request = thaw_json(request)
        if type(detached_request) is not dict:
            raise RuntimeError("auction-added request did not detach to an object")
        decoded = AdvancedOrderRequest.from_dict(detached_request)
        if decoded.as_dict() != detached_request:
            raise RuntimeError("auction-added request is not canonical")
    if "indication" in fields:
        _validate_indication_payload(data["indication"])


def _core_command_targets(engine: MarketMechanicsEngine) -> dict[str, str]:
    return {
        order_id: str(order.cancel_target_id)
        for order_id, order in engine.book.all_orders.items()
        if order.order_type.value == "cancel"
    }


def _core_submission_groups(
    engine: MarketMechanicsEngine,
) -> dict[str, tuple[int, int, tuple[object, ...]]]:
    journal = engine.book.journal.events
    starts = [
        index
        for index, event in enumerate(journal)
        if event.event_type is EventType.ORDER_SUBMITTED
    ]
    groups: dict[str, tuple[int, int, tuple[object, ...]]] = {}
    for offset, start_index in enumerate(starts):
        end_index = starts[offset + 1] if offset + 1 < len(starts) else len(journal)
        submitted = journal[start_index]
        order_id = submitted.data.get("order_id")
        if type(order_id) is not str or not order_id or order_id in groups:
            raise RuntimeError("FIFO submission history has an invalid order identity")
        groups[order_id] = (
            start_index + 1,
            end_index,
            tuple(journal[start_index:end_index]),
        )
    return groups


def _core_command_outcome(
    core_groups: Mapping[str, tuple[int, int, tuple[object, ...]]],
    command_id: str,
    expected_type: EventType,
):
    try:
        _start, _end, group = core_groups[command_id]
    except KeyError as error:
        raise RuntimeError("mechanics command is absent from FIFO history") from error
    outcomes = [event for event in group if event.event_type is expected_type]
    if len(outcomes) != 1:
        raise RuntimeError("mechanics command has no unique FIFO outcome")
    return outcomes[0]


def _core_acceptance_status(
    group: tuple[object, ...],
    order_id: str,
) -> str:
    relevant = [
        event
        for event in group
        if getattr(event, "data", {}).get("order_id") == order_id
    ]
    if any(event.event_type is EventType.ORDER_EXPIRED for event in relevant):
        return "EXPIRED"
    if any(event.event_type is EventType.FULL_FILL for event in relevant):
        return "FILLED"
    if any(event.event_type is EventType.ORDER_ADDED for event in relevant):
        return (
            "PARTIALLY_FILLED"
            if any(event.event_type is EventType.PARTIAL_FILL for event in relevant)
            else "WORKING"
        )
    raise RuntimeError("accepted order has no terminal FIFO submission outcome")


def _validate_continuous_acceptance_groups(
    engine: MarketMechanicsEngine,
    orders: Mapping[str, ManagedOrder],
    core_groups: Mapping[str, tuple[int, int, tuple[object, ...]]],
) -> set[int]:
    """Bind every accepted command to its synchronous outer-event microstep."""

    events = engine.events
    claimed_trade_indices: set[int] = set()
    claimed_expiry_indices: set[int] = set()
    for index, accepted in enumerate(events):
        if accepted.event_type is not MechanicsEventType.ORDER_ACCEPTED:
            continue
        order_id = _event_text(accepted.data, "order_id")
        managed = orders.get(order_id)
        if managed is None or managed.request.auction_only:
            raise RuntimeError("continuous acceptance has no continuous managed order")
        try:
            _start, _end, group = core_groups[order_id]
        except KeyError as error:
            raise RuntimeError("continuous acceptance has no FIFO submission group") from error
        cursor = index + 1
        local_trades = [event for event in group if event.event_type is EventType.TRADE]
        for local_trade in local_trades:
            if cursor >= len(events):
                raise RuntimeError("continuous acceptance omits an outer trade")
            outer_trade = events[cursor]
            expected_data = {
                "maker_order_id": local_trade.data["maker_order_id"],
                "price_ticks": local_trade.data["price_ticks"],
                "quantity": local_trade.data["quantity"],
                "taker_order_id": local_trade.data["taker_order_id"],
                "trade_id": local_trade.data["trade_id"],
            }
            if (
                outer_trade.event_type is not MechanicsEventType.TRADE
                or thaw_json(outer_trade.data) != expected_data
                or outer_trade.simulation_time_us != accepted.simulation_time_us
            ):
                raise RuntimeError(
                    "continuous trade is not in its acceptance-time outer group"
                )
            claimed_trade_indices.add(cursor)
            cursor += 1

        local_expirations = [
            event
            for event in group
            if event.event_type is EventType.ORDER_EXPIRED
            and event.data.get("order_id") == order_id
        ]
        expects_ioc_expiry = (
            managed.request.time_in_force is OrderInstruction.IOC
            and any(
                event.event_type is EventType.ORDER_ADDED
                and event.data.get("order_id") == order_id
                for event in group
            )
        )
        if local_expirations or expects_ioc_expiry:
            if cursor >= len(events):
                raise RuntimeError("continuous acceptance omits its immediate expiry")
            expiry = events[cursor]
            expected_reason = (
                "IOC_REMAINDER"
                if managed.request.time_in_force is OrderInstruction.IOC
                else "MARKET_UNFILLED_REMAINDER"
            )
            if (
                expiry.event_type is not MechanicsEventType.ORDER_EXPIRED
                or expiry.data.get("order_id") != order_id
                or expiry.data.get("reason") != expected_reason
                or expiry.simulation_time_us != accepted.simulation_time_us
                or ("command_id" in expiry.data) is not expects_ioc_expiry
            ):
                raise RuntimeError(
                    "continuous immediate expiry is outside its acceptance group"
                )
            if local_expirations:
                if len(local_expirations) != 1 or expiry.data.get(
                    "expired_quantity"
                ) != local_expirations[0].data.get("unfilled_quantity"):
                    raise RuntimeError(
                        "continuous unfilled expiry differs from FIFO submission"
                    )
            claimed_expiry_indices.add(cursor)

    actual_trade_indices = {
        index
        for index, event in enumerate(events)
        if event.event_type is MechanicsEventType.TRADE
    }
    if claimed_trade_indices != actual_trade_indices:
        raise RuntimeError("outer trades are not exactly partitioned by acceptances")
    return claimed_expiry_indices


def _validate_command_target(
    event: MechanicsEvent,
    command_targets: Mapping[str, str],
    target_field: str,
    *,
    prefix: str,
) -> None:
    command_id = _event_text(event.data, "command_id")
    if not command_id.startswith(f"{prefix}-"):
        raise RuntimeError("mechanics command uses the wrong allocator namespace")
    target = _event_text(event.data, target_field)
    if command_targets.get(command_id) != target:
        raise RuntimeError("mechanics command target differs from FIFO command")


def _validate_auction_outer_lifecycle(
    engine: MarketMechanicsEngine,
    orders: Mapping[str, ManagedOrder],
) -> None:
    """Replay every auction mutation from outer events into a fresh book.

    AuctionBook validates its own allocation history.  This replay binds that
    history to the *outer* mechanics journal as well: add/cancel/expiry
    indications, STP rows, fills, uncross rows, their ordering, and the rolling
    reference price must all be the events the public engine actually emits.
    """

    shadow = AuctionBook()
    session = SessionState.CLOSED
    reference_price = engine.rules.reference_price_ticks
    pending_indication: tuple[str, int] | None = None
    uncross_buffer: list[MechanicsEvent] = []

    def is_auction_expiry(event: MechanicsEvent) -> bool:
        if event.event_type is not MechanicsEventType.ORDER_EXPIRED:
            return False
        order_id = event.data.get("order_id")
        return (
            type(order_id) is str
            and order_id in orders
            and orders[order_id].request.auction_only
        )

    def expire_shadow(event: MechanicsEvent) -> None:
        order_id = _event_text(event.data, "order_id")
        try:
            quantity = shadow.expire(order_id)
        except ValueError as error:
            raise RuntimeError("auction expiry event does not target live liquidity") from error
        if quantity != _event_int(event.data, "expired_quantity", minimum=1):
            raise RuntimeError("auction expiry quantity differs from replay")

    def validate_uncross(event: MechanicsEvent) -> None:
        nonlocal reference_price, uncross_buffer
        if session not in _AUCTION_UNCROSS_STATES:
            raise RuntimeError("auction uncross event occurs outside an uncross phase")
        generated = shadow.uncross(reference_price, engine.rules.stp_mode)
        expected: list[tuple[MechanicsEventType, dict[str, object]]] = []
        expected.extend(
            (
                MechanicsEventType.SELF_TRADE_PREVENTION,
                {"mode": mode, "order_id": order_id, "phase": session.value},
            )
            for order_id, mode in generated.stp_cancellations
        )
        expected.extend(
            (
                MechanicsEventType.AUCTION_FILL,
                {
                    "buy_order_id": execution.buy_order_id,
                    "price_ticks": execution.price_ticks,
                    "quantity": execution.quantity,
                    "sell_order_id": execution.sell_order_id,
                    "trade_id": execution.trade_id,
                },
            )
            for execution in generated.executions
        )
        expected.extend(
            (
                MechanicsEventType.ORDER_EXPIRED,
                {
                    "expired_quantity": quantity,
                    "order_id": order_id,
                    "reason": "AUCTION_REMAINDER",
                },
            )
            for order_id, quantity in generated.expirations
        )
        expected.append(
            (
                MechanicsEventType.AUCTION_UNCROSS,
                {
                    "actual_matched_quantity": generated.matched_quantity,
                    "indication": generated.indication.as_dict(),
                    "session_state": session.value,
                },
            )
        )
        actual = [*uncross_buffer, event]
        if len(actual) != len(expected):
            raise RuntimeError("auction uncross outer-event group has the wrong length")
        for actual_event, (expected_type, expected_data) in zip(
            actual,
            expected,
            strict=True,
        ):
            if (
                actual_event.event_type is not expected_type
                or thaw_json(actual_event.data) != expected_data
                or actual_event.simulation_time_us != event.simulation_time_us
            ):
                raise RuntimeError("auction uncross outer events differ from replay")
        for execution in generated.executions:
            reference_price = execution.price_ticks
        uncross_buffer = []

    for event in engine.events:
        event_type = event.event_type
        data = event.data

        if pending_indication is not None:
            reason, cause_time = pending_indication
            if is_auction_expiry(event) and data.get("reason") == reason:
                if event.simulation_time_us != cause_time:
                    raise RuntimeError("auction expiry batch spans simulation times")
                expire_shadow(event)
                continue
            if event_type is not MechanicsEventType.AUCTION_INDICATION:
                raise RuntimeError("auction mutation is not followed by its indication")
            if (
                _event_text(data, "reason") != reason
                or event.simulation_time_us != cause_time
                or SessionState(_event_text(data, "session_state")) is not session
                or _validate_indication_payload(data["indication"])
                != shadow.indication(reference_price).as_dict()
            ):
                raise RuntimeError("auction indication differs from causal replay")
            pending_indication = None
            continue

        is_uncross_component = (
            event_type is MechanicsEventType.AUCTION_FILL
            or (
                event_type is MechanicsEventType.SELF_TRADE_PREVENTION
                and "order_id" in data
            )
            or (is_auction_expiry(event) and data.get("reason") == "AUCTION_REMAINDER")
        )
        if uncross_buffer:
            if is_uncross_component:
                uncross_buffer.append(event)
                continue
            if event_type is MechanicsEventType.AUCTION_UNCROSS:
                validate_uncross(event)
                continue
            raise RuntimeError("auction uncross outer-event group is not contiguous")
        if is_uncross_component:
            uncross_buffer.append(event)
            continue
        if event_type is MechanicsEventType.AUCTION_UNCROSS:
            validate_uncross(event)
            continue

        if event_type is MechanicsEventType.SESSION_STATE_CHANGED:
            session = SessionState(_event_text(data, "current_state"))
        elif event_type is MechanicsEventType.TRADE:
            reference_price = _event_int(data, "price_ticks", minimum=1)
        elif event_type is MechanicsEventType.AUCTION_ORDER_ADDED:
            order_id = _event_text(data, "order_id")
            managed = orders.get(order_id)
            if managed is None or not managed.request.auction_only:
                raise RuntimeError("auction add event has no auction managed order")
            shadow.add(ManagedOrder(managed.request, managed.arrival_sequence))
            pending_indication = ("ORDER_ADDED", event.simulation_time_us)
        elif event_type is MechanicsEventType.AUCTION_ORDER_CANCELLED:
            order_id = _event_text(data, "order_id")
            try:
                quantity = shadow.cancel(order_id)
            except ValueError as error:
                raise RuntimeError(
                    "auction cancellation event does not target live liquidity"
                ) from error
            if quantity != _event_int(data, "cancelled_quantity", minimum=1):
                raise RuntimeError("auction cancellation quantity differs from replay")
            pending_indication = ("ORDER_CANCELLED", event.simulation_time_us)
        elif is_auction_expiry(event):
            expire_shadow(event)
            pending_indication = (
                _event_text(data, "reason"),
                event.simulation_time_us,
            )
        elif event_type is MechanicsEventType.AUCTION_INDICATION:
            raise RuntimeError("auction indication has no causal mutation")

    if pending_indication is not None:
        raise RuntimeError("mechanics event prefix ends before an auction indication")
    if uncross_buffer:
        raise RuntimeError("mechanics event prefix ends during an auction uncross")
    if shadow.checkpoint_state() != engine.auction.checkpoint_state():
        raise RuntimeError("auction state differs from outer-event replay")


_REPLACE_PREVALIDATION_REASONS = frozenset(
    {
        "BELOW_MINIMUM_QUANTITY",
        "CONTINUOUS_ORDER_OUTSIDE_CONTINUOUS_SESSION",
        "FAT_FINGER_PRICE_DISTANCE",
        "GOOD_UNTIL_TIME_ALREADY_EXPIRED",
        "MAXIMUM_ORDER_SIZE_EXCEEDED",
        "PRICE_OUTSIDE_EXECUTION_COLLAR",
        "PRICE_OUTSIDE_INSTRUMENT_BAND",
        "QUANTITY_NOT_ALIGNED_TO_LOT_SIZE",
    }
)


def _outer_rows(events: tuple[MechanicsEvent, ...]) -> list[dict[str, object]]:
    return [event.as_dict() for event in events]


def _validate_outer_command_replay(
    engine: MarketMechanicsEngine,
    *,
    strict: bool,
) -> None:
    """Regenerate every state-changing public operation into a fresh engine.

    This is the authoritative causal check for outer mechanics events.  Exact
    generated groups bind event ordering and simulation time as well as data.
    During a nested public operation, ordinary runtime invariants may observe an
    exact prefix of the generated group; durable checkpoints may not.
    """

    source = engine.events
    shadow = MarketMechanicsEngine(rules=engine.rules)
    shadow._validating_outer_replay = True
    source_orders = engine.orders
    next_arrival = 0
    cursor = 0

    def sync_arrivals() -> None:
        nonlocal next_arrival
        shadow_ids = {order.request.order_id for order in shadow.orders}
        while (
            next_arrival < len(source_orders)
            and source_orders[next_arrival].request.order_id in shadow_ids
        ):
            next_arrival += 1

    def consume_generated(before: int) -> bool:
        """Return true only for an allowed non-strict in-flight prefix."""

        nonlocal cursor
        generated = shadow.events[before:]
        remaining = len(source) - cursor
        compare_count = min(len(generated), remaining)
        for offset in range(compare_count):
            if generated[offset].as_dict() != source[cursor + offset].as_dict():
                raise RuntimeError(
                    "outer mechanics group differs from deterministic public replay"
                )
        if remaining < len(generated):
            if not strict and cursor + remaining == len(source):
                return True
            raise RuntimeError("outer mechanics prefix ends within a public operation")
        cursor += len(generated)
        sync_arrivals()
        return False

    def next_same_time(event_type: MechanicsEventType) -> MechanicsEvent | None:
        time_us = source[cursor].simulation_time_us
        for candidate in source[cursor:]:
            if candidate.simulation_time_us != time_us:
                break
            if candidate.event_type is event_type:
                return candidate
        return None

    def replacement_candidate(
        managed: ManagedOrder,
        *,
        new_order_id: str,
        new_quantity: int,
        new_price_ticks: int | None,
    ) -> AdvancedOrderRequest:
        resolved_price = (
            managed.request.price_ticks
            if new_price_ticks is None
            else new_price_ticks
        )
        return replace(
            managed.request,
            order_id=new_order_id,
            price_ticks=resolved_price,
            quantity=new_quantity,
        )

    def active_replacement_orders() -> tuple[tuple[str, ManagedOrder, object], ...]:
        rows: list[tuple[str, ManagedOrder, object]] = []
        for active_order in shadow.book.active_orders.values():
            old_order_id = active_order.order_id
            managed = shadow._orders.get(old_order_id)
            if managed is not None and not managed.request.auction_only:
                rows.append((old_order_id, managed, active_order))
        return tuple(rows)

    def prevalidation_witness(
        new_order_id: str,
        reason: str,
    ) -> tuple[str, int, int | None] | None:
        minimum = shadow.rules.minimum_quantity
        reference = shadow._reference_price()
        if reason == "MAXIMUM_ORDER_SIZE_EXCEEDED":
            argument_rows = ((shadow.rules.maximum_quantity + 1, None),)
        elif reason == "BELOW_MINIMUM_QUANTITY":
            argument_rows = (
                ()
                if minimum == 1
                else ((minimum - 1, None),)
            )
        elif reason == "QUANTITY_NOT_ALIGNED_TO_LOT_SIZE":
            argument_rows = (
                ((minimum + 1, None),)
                if shadow.rules.lot_size > 1
                and minimum + 1 <= shadow.rules.maximum_quantity
                else ()
            )
        elif reason == "PRICE_OUTSIDE_INSTRUMENT_BAND":
            price_candidates = []
            if shadow.rules.lower_price_band_ticks > 1:
                price_candidates.append(shadow.rules.lower_price_band_ticks - 1)
            price_candidates.append(shadow.rules.upper_price_band_ticks + 1)
            argument_rows = tuple((minimum, price) for price in price_candidates)
        elif reason == "FAT_FINGER_PRICE_DISTANCE":
            distance = shadow.rules.fat_finger_ticks
            argument_rows = (
                ()
                if distance is None
                else tuple(
                    (minimum, price)
                    for price in (reference - distance - 1, reference + distance + 1)
                    if price > 0
                )
            )
        elif reason == "PRICE_OUTSIDE_EXECUTION_COLLAR":
            distance = shadow.rules.price_collar_ticks
            argument_rows = (
                ()
                if distance is None
                else tuple(
                    (minimum, price)
                    for price in (reference - distance - 1, reference + distance + 1)
                    if price > 0
                )
            )
        else:
            argument_rows = ((minimum, reference),)

        for old_order_id, managed, _active_order in active_replacement_orders():
            for new_quantity, proposed_price in argument_rows:
                if managed.request.price_ticks is None:
                    if reason in {
                        "FAT_FINGER_PRICE_DISTANCE",
                        "PRICE_OUTSIDE_EXECUTION_COLLAR",
                        "PRICE_OUTSIDE_INSTRUMENT_BAND",
                    }:
                        continue
                    new_price_ticks = None
                else:
                    new_price_ticks = proposed_price
                try:
                    candidate = replacement_candidate(
                        managed,
                        new_order_id=new_order_id,
                        new_quantity=new_quantity,
                        new_price_ticks=new_price_ticks,
                    )
                except (TypeError, ValueError):
                    continue
                validation = shadow._validate_request(candidate)
                if validation is not None and validation[0] == reason:
                    return old_order_id, new_quantity, new_price_ticks
        return None

    def valid_replacement_witness(
        new_order_id: str,
        *,
        filled_quantity: int | None = None,
        duplicate_required: bool = False,
    ) -> tuple[str, int, int | None] | None:
        if duplicate_required and (
            new_order_id not in shadow._orders
            and new_order_id not in shadow.book.all_orders
        ):
            return None
        reference = shadow._reference_price()
        for old_order_id, managed, active_order in active_replacement_orders():
            if (
                filled_quantity is not None
                and active_order.filled_quantity != filled_quantity
            ):
                continue
            if filled_quantity is None:
                new_quantity = (
                    active_order.filled_quantity + active_order.remaining_quantity
                )
            else:
                new_quantity = shadow.rules.minimum_quantity
                if new_quantity > filled_quantity:
                    continue
            new_price_ticks = (
                None if managed.request.price_ticks is None else reference
            )
            try:
                candidate = replacement_candidate(
                    managed,
                    new_order_id=new_order_id,
                    new_quantity=new_quantity,
                    new_price_ticks=new_price_ticks,
                )
            except (TypeError, ValueError):
                continue
            if shadow._validate_request(candidate) is None:
                return old_order_id, new_quantity, new_price_ticks
        return None

    while cursor < len(source):
        target_time = source[cursor].simulation_time_us
        before = len(shadow.events)
        shadow.advance_to(target_time)
        if len(shadow.events) != before:
            if consume_generated(before):
                return
            continue

        event = source[cursor]
        data = event.data
        event_type = event.event_type

        if next_arrival < len(source_orders):
            next_managed = source_orders[next_arrival]
            next_id = next_managed.request.order_id
            initializes_next = (
                event_type
                in {
                    MechanicsEventType.AUCTION_ORDER_ADDED,
                    MechanicsEventType.ORDER_ACCEPTED,
                    MechanicsEventType.ORDER_REJECTED,
                    MechanicsEventType.PROTECTION_TRIGGERED,
                }
                and data.get("order_id") == next_id
            ) or (
                event_type is MechanicsEventType.SELF_TRADE_PREVENTION
                and data.get("aggressor_order_id") == next_id
            )
            if initializes_next:
                before = len(shadow.events)
                shadow.submit(next_managed.request)
                if consume_generated(before):
                    return
                continue

        if event_type is MechanicsEventType.PRIORITY_PRESERVED:
            before = len(shadow.events)
            shadow.replace_order(
                _event_text(data, "order_id"),
                new_order_id=_event_text(data, "replacement_request_id"),
                new_quantity=_event_int(data, "new_total_quantity", minimum=1),
            )
            if consume_generated(before):
                return
            continue

        if event_type is MechanicsEventType.PRIORITY_LOST:
            old_order_id = _event_text(data, "old_order_id")
            new_order_id = _event_text(data, "new_order_id")
            try:
                new_managed = next(
                    order
                    for order in source_orders
                    if order.request.order_id == new_order_id
                )
                old_managed = next(
                    order
                    for order in source_orders
                    if order.request.order_id == old_order_id
                )
            except StopIteration as error:
                if not strict and source[-1].event_type is MechanicsEventType.ORDER_CANCELLED:
                    return
                raise RuntimeError("replacement replay lacks managed rows") from error
            before = len(shadow.events)
            shadow.replace_order(
                old_order_id,
                new_order_id=new_order_id,
                new_quantity=old_managed.filled_quantity + new_managed.request.quantity,
                new_price_ticks=new_managed.request.price_ticks,
            )
            if consume_generated(before):
                return
            continue

        if event_type in {
            MechanicsEventType.ORDER_CANCELLED,
            MechanicsEventType.AUCTION_ORDER_CANCELLED,
        }:
            before = len(shadow.events)
            shadow.cancel(
                _event_text(data, "order_id"),
                reason=_event_text(data, "reason"),
            )
            if consume_generated(before):
                return
            continue

        auction_uncross_component = (
            event_type in {
                MechanicsEventType.AUCTION_FILL,
                MechanicsEventType.AUCTION_UNCROSS,
            }
            or (
                event_type is MechanicsEventType.SELF_TRADE_PREVENTION
                and "order_id" in data
            )
            or (
                event_type is MechanicsEventType.ORDER_EXPIRED
                and data.get("reason") == "AUCTION_REMAINDER"
            )
        )
        if auction_uncross_component:
            before = len(shadow.events)
            shadow.uncross_auction()
            if consume_generated(before):
                return
            continue

        session_event = next_same_time(MechanicsEventType.SESSION_STATE_CHANGED)
        if event_type in {
            MechanicsEventType.ORDER_EXPIRED,
            MechanicsEventType.AUCTION_INDICATION,
            MechanicsEventType.SESSION_STATE_CHANGED,
        } and session_event is not None:
            before = len(shadow.events)
            shadow.transition_session(
                SessionState(_event_text(session_event.data, "current_state")),
                reason=_event_text(session_event.data, "reason"),
            )
            if consume_generated(before):
                return
            continue

        if event_type is MechanicsEventType.ORDER_REJECTED:
            reason = _event_text(data, "reason")
            order_id = _event_text(data, "order_id")
            before = len(shadow.events)
            if reason == "CANCEL_NOT_ACTIVE":
                shadow.cancel(order_id)
            elif reason == "DUPLICATE_ORDER_ID":
                try:
                    duplicate = shadow.get_order(order_id).request
                except ValueError as error:
                    raise RuntimeError(
                        "duplicate-order rejection has no prior order"
                    ) from error
                shadow.submit(duplicate)
            elif reason == "REPLACE_NOT_ACTIVE_CONTINUOUS_ORDER":
                shadow.replace_order(
                    order_id,
                    new_order_id="__KIRBY2_VALIDATION_ONLY_REPLACEMENT__",
                    new_quantity=1,
                )
            elif reason == "DUPLICATE_REPLACEMENT_ORDER_ID":
                witness = valid_replacement_witness(
                    order_id,
                    duplicate_required=True,
                )
                if witness is None:
                    raise RuntimeError(
                        "duplicate replacement rejection is not historically reachable"
                    )
                old_order_id, new_quantity, new_price_ticks = witness
                shadow.replace_order(
                    old_order_id,
                    new_order_id=order_id,
                    new_quantity=new_quantity,
                    new_price_ticks=new_price_ticks,
                )
            elif reason == "REPLACE_QUANTITY_NOT_ABOVE_FILLED":
                filled = _event_int(data, "filled_quantity", minimum=0)
                witness = valid_replacement_witness(
                    order_id,
                    filled_quantity=filled,
                )
                if witness is None:
                    raise RuntimeError(
                        "replacement quantity rejection has no valid public-call witness"
                    )
                old_order_id, new_quantity, new_price_ticks = witness
                shadow.replace_order(
                    old_order_id,
                    new_order_id=order_id,
                    new_quantity=new_quantity,
                    new_price_ticks=new_price_ticks,
                )
            elif reason in _REPLACE_PREVALIDATION_REASONS:
                witness = prevalidation_witness(order_id, reason)
                if witness is None:
                    raise RuntimeError(
                        "replacement validation rejection has no exact public-call witness"
                    )
                old_order_id, new_quantity, new_price_ticks = witness
                shadow.replace_order(
                    old_order_id,
                    new_order_id=order_id,
                    new_quantity=new_quantity,
                    new_price_ticks=new_price_ticks,
                )
            else:
                raise RuntimeError("state-neutral rejection reason is unreachable")
            if consume_generated(before):
                return
            continue

        if event_type is MechanicsEventType.PROTECTION_TRIGGERED:
            if "reason" not in data or cursor + 1 >= len(source):
                raise RuntimeError("state-neutral protection is not replacement validation")
            reason = _event_text(data, "reason")
            order_id = _event_text(data, "order_id")
            witness = prevalidation_witness(order_id, reason)
            if witness is None:
                raise RuntimeError("state-neutral protection is not historically reachable")
            old_order_id, new_quantity, new_price_ticks = witness
            before = len(shadow.events)
            shadow.replace_order(
                old_order_id,
                new_order_id=order_id,
                new_quantity=new_quantity,
                new_price_ticks=new_price_ticks,
            )
            if consume_generated(before):
                return
            continue

        raise RuntimeError(
            f"outer event is not reachable from a public operation: {event_type.value}"
        )

    before = len(shadow.events)
    shadow.advance_to(engine.clock.current_time_us)
    if len(shadow.events) != before:
        raise RuntimeError("restored clock omits due deterministic outer events")
    sync_arrivals()
    if next_arrival != len(source_orders):
        raise RuntimeError("outer replay omits managed-order arrivals")
    if not strict:
        due_schedule_rows = sum(
            item.simulation_time_us <= engine.clock.current_time_us
            for item in engine.rules.session_schedule.transitions
        )
        if engine._schedule_index != due_schedule_rows:
            # transition_session asserts before advance_to advances the schedule
            # cursor.  The emitted group is complete but the composite timer
            # operation is still intentionally in flight.
            return

    source_projection = {
        "allocators": {
            "arrival_sequence": engine._arrival_sequence,
            "command_sequence": engine._command_sequence,
        },
        "auction": engine.auction.checkpoint_state(),
        "auction_player_position": engine._auction_player_position,
        "book": engine.book.checkpoint_state(),
        "clock": engine.clock.checkpoint_state(),
        "events": _outer_rows(engine.events),
        "last_trade_price_ticks": engine._last_trade_price_ticks,
        "managed_orders": [order.as_dict() for order in engine.orders],
        "schedule_index": engine._schedule_index,
        "session_state": engine.session_state.value,
    }
    replay_projection = {
        "allocators": {
            "arrival_sequence": shadow._arrival_sequence,
            "command_sequence": shadow._command_sequence,
        },
        "auction": shadow.auction.checkpoint_state(),
        "auction_player_position": shadow._auction_player_position,
        "book": shadow.book.checkpoint_state(),
        "clock": shadow.clock.checkpoint_state(),
        "events": _outer_rows(shadow.events),
        "last_trade_price_ticks": shadow._last_trade_price_ticks,
        "managed_orders": [order.as_dict() for order in shadow.orders],
        "schedule_index": shadow._schedule_index,
        "session_state": shadow.session_state.value,
    }
    if _canonical_json_bytes(source_projection) != _canonical_json_bytes(
        replay_projection
    ):
        raise RuntimeError("restored mechanics state differs from public-operation replay")


def _validate_outer_mechanics_lifecycle(
    engine: MarketMechanicsEngine,
    *,
    strict_schedule: bool,
) -> None:
    events = engine.events
    orders = {order.request.order_id: order for order in engine.orders}
    command_targets = _core_command_targets(engine)
    core_groups = _core_submission_groups(engine)
    _validate_continuous_acceptance_groups(engine, orders, core_groups)
    session = SessionState.CLOSED
    expected_followup: tuple[MechanicsEventType, str, str | None, int] | None = None
    initialized: list[str] = []
    initialized_ids: set[str] = set()
    accepted_ranges: list[tuple[int, int]] = []
    configured_rows_seen = 0
    pending_replacement: tuple[str, str, str, int] | None = None
    replacement_stage: str | None = None
    replacement_expected_trades: list[dict[str, object]] = []
    replacement_trade_index = 0
    auction_cancelled: dict[str, int] = {}
    auction_expired: dict[str, int] = {}
    auction_stp_ids: set[str] = set()

    for index, event in enumerate(events):
        _validate_mechanics_event_shape(event)
        if expected_followup is not None:
            expected_type, expected_reason, expected_order_id, expected_time = (
                expected_followup
            )
            if event.event_type is not expected_type:
                raise RuntimeError(
                    f"{expected_type.value} must immediately follow its cause"
                )
            if _event_text(event.data, "reason") != expected_reason:
                raise RuntimeError("mechanics follow-up reason differs from its cause")
            if expected_order_id is not None and event.data.get("order_id") != expected_order_id:
                raise RuntimeError("mechanics follow-up order differs from its cause")
            if event.simulation_time_us != expected_time:
                raise RuntimeError("mechanics follow-up time differs from its cause")
            expected_followup = None

        data = event.data
        event_type = event.event_type
        if pending_replacement is not None:
            old_replacement_id, new_replacement_id, _reason, lost_index = (
                pending_replacement
            )
            if index == lost_index + 1:
                if (
                    event_type is not MechanicsEventType.ORDER_CANCELLED
                    or data.get("order_id") != old_replacement_id
                    or data.get("reason") != "REPLACE_CANCEL_LEG"
                ):
                    raise RuntimeError(
                        "lost-priority replacement is not followed by its cancel leg"
                    )
                replacement_stage = "PRE_INIT"
            elif event_type is MechanicsEventType.ORDER_REPLACED:
                if replacement_stage not in {
                    "ACCEPTED",
                    "ACCEPTED_EXPIRED",
                    "REJECTED",
                } or replacement_trade_index != len(replacement_expected_trades):
                    raise RuntimeError(
                        "replacement completion precedes its exact new-order lifecycle"
                    )
            elif replacement_stage == "PRE_INIT":
                if event_type is MechanicsEventType.SELF_TRADE_PREVENTION:
                    if data.get("aggressor_order_id") != new_replacement_id:
                        raise RuntimeError(
                            "replacement STP event belongs to another aggressor"
                        )
                elif event_type is MechanicsEventType.PROTECTION_TRIGGERED:
                    if data.get("order_id") != new_replacement_id:
                        raise RuntimeError(
                            "replacement protection event belongs to another order"
                        )
                    replacement_stage = "PRE_REJECTION"
                elif event_type is MechanicsEventType.ORDER_ACCEPTED:
                    if data.get("order_id") != new_replacement_id:
                        raise RuntimeError(
                            "replacement window initializes another accepted order"
                        )
                    _start, _end, new_group = core_groups[new_replacement_id]
                    replacement_expected_trades = [
                        {
                            "maker_order_id": local.data["maker_order_id"],
                            "price_ticks": local.data["price_ticks"],
                            "quantity": local.data["quantity"],
                            "taker_order_id": local.data["taker_order_id"],
                            "trade_id": local.data["trade_id"],
                        }
                        for local in new_group
                        if local.event_type is EventType.TRADE
                    ]
                    replacement_trade_index = 0
                    replacement_stage = "ACCEPTED"
                elif event_type is MechanicsEventType.ORDER_REJECTED:
                    if data.get("order_id") != new_replacement_id:
                        raise RuntimeError(
                            "replacement window initializes another rejected order"
                        )
                    replacement_stage = (
                        "EXPECT_VOLATILITY_SESSION"
                        if data.get("reason") == "VOLATILITY_INTERRUPTION"
                        else "REJECTED"
                    )
                else:
                    raise RuntimeError(
                        "replacement window contains an unrelated outer event"
                    )
            elif replacement_stage == "PRE_REJECTION":
                if (
                    event_type is not MechanicsEventType.ORDER_REJECTED
                    or data.get("order_id") != new_replacement_id
                ):
                    raise RuntimeError(
                        "replacement protection is not followed by its rejection"
                    )
                replacement_stage = (
                    "EXPECT_VOLATILITY_SESSION"
                    if data.get("reason") == "VOLATILITY_INTERRUPTION"
                    else "REJECTED"
                )
            elif replacement_stage == "ACCEPTED":
                if event_type is MechanicsEventType.TRADE:
                    if (
                        replacement_trade_index >= len(replacement_expected_trades)
                        or thaw_json(data)
                        != replacement_expected_trades[replacement_trade_index]
                    ):
                        raise RuntimeError(
                            "replacement trade sequence differs from FIFO submission"
                        )
                    replacement_trade_index += 1
                elif event_type is MechanicsEventType.ORDER_EXPIRED:
                    if data.get("order_id") != new_replacement_id:
                        raise RuntimeError(
                            "replacement expiry belongs to another order"
                        )
                    replacement_stage = "ACCEPTED_EXPIRED"
                else:
                    raise RuntimeError(
                        "replacement window contains an unrelated outer event"
                    )
            elif replacement_stage == "EXPECT_VOLATILITY_SESSION":
                if (
                    event_type is not MechanicsEventType.SESSION_STATE_CHANGED
                    or data.get("current_state") != SessionState.HALTED.value
                    or data.get("reason") != "VOLATILITY_INTERRUPTION"
                ):
                    raise RuntimeError(
                        "volatility replacement lacks its halt transition"
                    )
                replacement_stage = "EXPECT_VOLATILITY_HALT"
            elif replacement_stage == "EXPECT_VOLATILITY_HALT":
                if (
                    event_type is not MechanicsEventType.HALT
                    or data.get("reason") != "VOLATILITY_INTERRUPTION"
                ):
                    raise RuntimeError(
                        "volatility replacement lacks its HALT marker"
                    )
                replacement_stage = "REJECTED"
            else:
                raise RuntimeError("replacement window is in an invalid lifecycle stage")
        if event_type is MechanicsEventType.SESSION_STATE_CHANGED:
            previous = SessionState(_event_text(data, "previous_state"))
            current = SessionState(_event_text(data, "current_state"))
            reason = _event_text(data, "reason")
            if previous is not session or current not in _ALLOWED_SESSION_TRANSITIONS[previous]:
                raise RuntimeError("session transition event is not a legal contiguous path")
            session = current
            if reason == "CONFIGURED_SESSION_SCHEDULE":
                transitions = engine.rules.session_schedule.transitions
                match_index = next(
                    (
                        row_index
                        for row_index in range(configured_rows_seen, len(transitions))
                        if transitions[row_index].simulation_time_us
                        == event.simulation_time_us
                        and transitions[row_index].state is current
                    ),
                    None,
                )
                if match_index is None:
                    raise RuntimeError(
                        "configured session event has no matching schedule transition"
                    )
                configured_rows_seen = match_index + 1
            if current is SessionState.HALTED:
                expected_followup = (
                    MechanicsEventType.HALT,
                    reason,
                    None,
                    event.simulation_time_us,
                )
            elif previous in {
                SessionState.HALTED,
                SessionState.REOPENING_AUCTION,
            } and current is SessionState.CONTINUOUS:
                expected_followup = (
                    MechanicsEventType.RESUME,
                    reason,
                    None,
                    event.simulation_time_us,
                )
        elif event_type in {MechanicsEventType.HALT, MechanicsEventType.RESUME}:
            if index == 0 or events[index - 1].event_type is not MechanicsEventType.SESSION_STATE_CHANGED:
                raise RuntimeError("HALT/RESUME event is detached from a session transition")
        elif event_type is MechanicsEventType.PROTECTION_TRIGGERED:
            order_id = _event_text(data, "order_id")
            if "reason" in data:
                rejection_reason = _event_text(data, "reason")
            elif data["protection"] == "VOLATILITY_INTERRUPTION":
                rejection_reason = "VOLATILITY_INTERRUPTION"
            else:
                rejection_reason = "EXECUTION_PRICE_OUTSIDE_COLLAR"
            expected_followup = (
                MechanicsEventType.ORDER_REJECTED,
                rejection_reason,
                order_id,
                event.simulation_time_us,
            )

        if event_type in {
            MechanicsEventType.ORDER_ACCEPTED,
            MechanicsEventType.ORDER_REJECTED,
            MechanicsEventType.AUCTION_ORDER_ADDED,
        }:
            order_id = _event_text(data, "order_id")
            managed = orders.get(order_id)
            if managed is not None and order_id not in initialized_ids:
                expected_type = (
                    MechanicsEventType.ORDER_REJECTED
                    if managed.status == "REJECTED"
                    else MechanicsEventType.AUCTION_ORDER_ADDED
                    if managed.request.auction_only
                    else MechanicsEventType.ORDER_ACCEPTED
                )
                if event_type is not expected_type:
                    raise RuntimeError("managed order initial outcome event is inconsistent")
                initialized_ids.add(order_id)
                initialized.append(order_id)
                if managed.status == "REJECTED" and (
                    managed.filled_quantity != 0
                    or managed.cancelled_quantity != 0
                    or managed.expired_quantity != 0
                    or managed.remaining_quantity != managed.request.quantity
                    or managed.resting_sequence is not None
                ):
                    raise RuntimeError("rejected managed order has lifecycle state")
                if event_type is MechanicsEventType.AUCTION_ORDER_ADDED:
                    if thaw_json(data["request"]) != managed.request.as_dict():
                        raise RuntimeError("auction-added request differs from managed order")
                    if session not in _AUCTION_ACCEPTING_STATES:
                        raise RuntimeError("auction order was added outside an auction phase")
                elif event_type is MechanicsEventType.ORDER_ACCEPTED:
                    if session is not SessionState.CONTINUOUS:
                        raise RuntimeError(
                            "continuous order acceptance occurs outside CONTINUOUS"
                        )
                    start = _event_int(data, "core_event_start", minimum=1)
                    end = _event_int(data, "core_event_end", minimum=start)
                    try:
                        expected_start, expected_end, core_group = core_groups[order_id]
                    except KeyError as error:
                        raise RuntimeError(
                            "accepted order is absent from FIFO submission history"
                        ) from error
                    if (start, end) != (expected_start, expected_end):
                        raise RuntimeError(
                            "accepted order core-event range is not its exact FIFO group"
                        )
                    if data["status"] != _core_acceptance_status(core_group, order_id):
                        raise RuntimeError(
                            "accepted order status differs from its FIFO submission outcome"
                        )
                    accepted_ranges.append((start, end))
            elif event_type in {
                MechanicsEventType.ORDER_ACCEPTED,
                MechanicsEventType.AUCTION_ORDER_ADDED,
            }:
                raise RuntimeError("accepted/add event is duplicate or has no managed order")

        if event_type is MechanicsEventType.ORDER_CANCELLED:
            _validate_command_target(
                event,
                command_targets,
                "order_id",
                prefix="MECH-CANCEL",
            )
            order_id = _event_text(data, "order_id")
            managed = orders.get(order_id)
            if managed is None or managed.request.auction_only or managed.status != "CANCELLED":
                raise RuntimeError("continuous cancellation event has invalid managed state")
            if data["cancelled_quantity"] != managed.cancelled_quantity:
                raise RuntimeError("continuous cancellation quantity is inconsistent")
            outcome = _core_command_outcome(
                core_groups,
                _event_text(data, "command_id"),
                EventType.ORDER_CANCELLED,
            )
            if outcome.data.get("order_id") != order_id:
                raise RuntimeError("continuous cancellation differs from FIFO outcome")
        elif event_type is MechanicsEventType.AUCTION_ORDER_CANCELLED:
            order_id = _event_text(data, "order_id")
            auction_cancelled[order_id] = auction_cancelled.get(order_id, 0) + _event_int(
                data, "cancelled_quantity", minimum=1
            )
        elif event_type is MechanicsEventType.ORDER_EXPIRED:
            order_id = _event_text(data, "order_id")
            managed = orders.get(order_id)
            if managed is None or managed.status != "EXPIRED":
                raise RuntimeError("expiration event has invalid managed state")
            quantity = _event_int(data, "expired_quantity", minimum=1)
            if "command_id" in data:
                if managed.request.auction_only:
                    raise RuntimeError("auction expiration cannot own a FIFO command")
                _validate_command_target(
                    event,
                    command_targets,
                    "order_id",
                    prefix="MECH-EXPIRE",
                )
                outcome = _core_command_outcome(
                    core_groups,
                    _event_text(data, "command_id"),
                    EventType.ORDER_CANCELLED,
                )
                if (
                    outcome.data.get("order_id") != order_id
                    or outcome.data.get("cancelled_quantity") != quantity
                ):
                    raise RuntimeError("continuous expiration differs from FIFO outcome")
            if managed.request.auction_only:
                auction_expired[order_id] = auction_expired.get(order_id, 0) + quantity
            elif quantity != managed.expired_quantity:
                raise RuntimeError("continuous expiration quantity is inconsistent")
        elif event_type is MechanicsEventType.PRIORITY_PRESERVED:
            _validate_command_target(
                event,
                command_targets,
                "order_id",
                prefix="MECH-REDUCE",
            )
            outcome = _core_command_outcome(
                core_groups,
                _event_text(data, "command_id"),
                EventType.ORDER_REDUCED,
            )
            if any(
                outcome.data.get(field) != data[field]
                for field in ("new_total_quantity", "order_id", "resting_sequence")
            ) or outcome.data.get("priority_preserved") is not True:
                raise RuntimeError("priority-preserved event differs from FIFO outcome")
        elif event_type is MechanicsEventType.PRIORITY_LOST:
            if pending_replacement is not None:
                raise RuntimeError("replacement lifecycles overlap")
            pending_replacement = (
                _event_text(data, "old_order_id"),
                _event_text(data, "new_order_id"),
                _event_text(data, "reason"),
                index,
            )
            replacement_stage = "AWAIT_CANCEL"
            replacement_expected_trades = []
            replacement_trade_index = 0
        elif event_type is MechanicsEventType.ORDER_REPLACED:
            if "priority_preserved" in data:
                previous = events[index - 1] if index else None
                if (
                    previous is None
                    or previous.event_type is not MechanicsEventType.PRIORITY_PRESERVED
                    or previous.data.get("order_id") != data["old_order_id"]
                    or previous.data.get("replacement_request_id")
                    != data["replacement_request_id"]
                    or data["old_order_id"] != data["new_order_id"]
                    or data["new_order_id"] != previous.data.get("order_id")
                    or data["priority_preserved"] is not True
                ):
                    raise RuntimeError("preserved replacement is detached from priority event")
            else:
                if pending_replacement is None or pending_replacement[:2] != (
                    data["old_order_id"],
                    data["new_order_id"],
                ):
                    raise RuntimeError("replacement completion differs from priority-loss event")
                old_order_id, new_order_id, priority_reason, lost_index = pending_replacement
                old = orders.get(old_order_id)
                new = orders.get(new_order_id)
                if (
                    old is None
                    or new is None
                    or old.request.auction_only
                    or new.request.auction_only
                    or new_order_id not in initialized_ids
                ):
                    raise RuntimeError("replacement rows are absent from managed history")
                shared_request_fields = (
                    "account_id",
                    "auction_only",
                    "good_until_time_us",
                    "instruction",
                    "modifiers",
                    "owner",
                    "side",
                    "time_in_force",
                )
                old_request = old.request.as_dict()
                new_request = new.request.as_dict()
                if any(
                    old_request[field] != new_request[field]
                    for field in shared_request_fields
                ):
                    raise RuntimeError("replacement request changes immutable order fields")
                cancel_event = events[lost_index + 1]
                cancel_outcome = _core_command_outcome(
                    core_groups,
                    _event_text(cancel_event.data, "command_id"),
                    EventType.ORDER_CANCELLED,
                )
                cancelled_on_replace = cancel_outcome.data.get("cancelled_quantity")
                if type(cancelled_on_replace) is not int or cancelled_on_replace <= 0:
                    raise RuntimeError("replacement cancel leg has invalid closed quantity")
                current_total_before_replace = (
                    old.filled_quantity + cancelled_on_replace
                )
                expected_new_total = old.filled_quantity + new.request.quantity
                expected_reason = (
                    "PRICE_CHANGE"
                    if old.request.price_ticks != new.request.price_ticks
                    else "QUANTITY_INCREASE"
                    if expected_new_total >= current_total_before_replace
                    else "VENUE_REDUCTION_RULE"
                )
                if (
                    priority_reason != expected_reason
                    or data["filled_quantity_before_replace"]
                    != old.filled_quantity
                    or data["replacement_leaves_quantity"]
                    != new.request.quantity
                    or data["new_total_quantity"] != expected_new_total
                    or data["replacement_accepted"]
                    is not (new.status != "REJECTED")
                ):
                    raise RuntimeError(
                        "replacement completion fields differ from managed lifecycle"
                    )
                pending_replacement = None
                replacement_stage = None
                replacement_expected_trades = []
                replacement_trade_index = 0
        elif event_type is MechanicsEventType.SELF_TRADE_PREVENTION:
            mode = SelfTradePreventionMode(_event_text(data, "mode"))
            if mode is SelfTradePreventionMode.NONE:
                raise RuntimeError("self-trade event cannot use NONE")
            if "command_id" in data:
                _validate_command_target(
                    event,
                    command_targets,
                    "resting_order_id",
                    prefix="MECH-STP",
                )
                outcome = _core_command_outcome(
                    core_groups,
                    _event_text(data, "command_id"),
                    EventType.ORDER_CANCELLED,
                )
                if outcome.data.get("order_id") != data["resting_order_id"]:
                    raise RuntimeError("continuous STP event differs from FIFO outcome")
            elif "order_id" in data:
                order_id = _event_text(data, "order_id")
                auction_stp_ids.add(order_id)
                if SessionState(_event_text(data, "phase")) is not session:
                    raise RuntimeError("auction STP phase differs from session history")

        if event_type in {
            MechanicsEventType.AUCTION_INDICATION,
            MechanicsEventType.AUCTION_UNCROSS,
        } and SessionState(_event_text(data, "session_state")) is not session:
            raise RuntimeError("auction event session state differs from session history")

    if expected_followup is not None:
        raise RuntimeError("mechanics event prefix ends before required follow-up")
    # A lost-priority replacement intentionally calls the public cancel/submit
    # operations before it can append ORDER_REPLACED.  Those nested operations
    # run the ordinary invariants against a valid in-flight prefix.  A durable
    # checkpoint, however, must never capture that incomplete composite action.
    if strict_schedule and pending_replacement is not None:
        raise RuntimeError("mechanics event prefix ends during a replacement")
    if session is not engine.session_state:
        raise RuntimeError("terminal session state differs from transition history")
    expected_arrivals = [order.request.order_id for order in engine.orders]
    if initialized != expected_arrivals:
        raise RuntimeError("managed order initial events differ from arrival history")
    for previous, current in zip(accepted_ranges, accepted_ranges[1:]):
        if previous[1] >= current[0]:
            raise RuntimeError("accepted order core-event ranges overlap or regress")
    if strict_schedule:
        expected_cursor = sum(
            item.simulation_time_us <= engine.clock.current_time_us
            for item in engine.rules.session_schedule.transitions
        )
        if engine._schedule_index != expected_cursor:
            raise RuntimeError(
                "session schedule cursor does not include every due transition"
            )
        if configured_rows_seen != engine._schedule_index:
            raise RuntimeError(
                "configured session events do not exactly cover the schedule cursor"
            )

    _validate_auction_outer_lifecycle(engine, orders)

    for managed in engine.auction.orders:
        order_id = managed.request.order_id
        if managed.expired_quantity:
            expected_status = "EXPIRED"
            if auction_expired.get(order_id, 0) != managed.expired_quantity:
                raise RuntimeError("auction expiry state differs from outer events")
        elif managed.cancelled_quantity:
            expected_status = "CANCELLED_STP" if order_id in auction_stp_ids else "CANCELLED"
            if expected_status == "CANCELLED" and auction_cancelled.get(order_id, 0) != managed.cancelled_quantity:
                raise RuntimeError("auction cancellation state differs from outer events")
        elif managed.filled_quantity == managed.request.quantity:
            expected_status = "FILLED"
        elif managed.filled_quantity:
            expected_status = "PARTIALLY_FILLED"
        else:
            expected_status = "AUCTION_WORKING"
        if managed.status != expected_status:
            raise RuntimeError("auction managed status differs from outer lifecycle")

    _validate_outer_command_replay(engine, strict=strict_schedule)


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
    if isinstance(indication, Mapping):
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
