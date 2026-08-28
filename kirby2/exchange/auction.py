"""Deterministic single-price auction book with documented allocation rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable

from .book import (
    _canonical_json_bytes,
    _parse_canonical_json_object,
    _require_exact_fields,
    _validate_strict_json,
    _wire_array,
    _wire_int,
    _wire_object,
    _wire_optional_int,
    _wire_string,
)
from .mechanics_models import (
    AdvancedOrderRequest,
    AuctionIndication,
    ManagedOrder,
    OrderInstruction,
    SelfTradePreventionMode,
)
from .models import OrderOwner, Side


AUCTION_BOOK_CHECKPOINT_SCHEMA_VERSION = 1
_AUCTION_ACTIVE_STATUSES = frozenset({"AUCTION_WORKING", "PARTIALLY_FILLED"})
_AUCTION_CLOSED_STATUSES = frozenset(
    {"CANCELLED", "CANCELLED_STP", "EXPIRED", "FILLED"}
)
_AUCTION_STORED_STATUSES = _AUCTION_ACTIVE_STATUSES | _AUCTION_CLOSED_STATUSES


@dataclass(frozen=True, slots=True)
class AuctionExecution:
    trade_id: str
    price_ticks: int
    quantity: int
    buy_order_id: str
    sell_order_id: str


@dataclass(frozen=True, slots=True)
class AuctionUncrossResult:
    indication: AuctionIndication
    executions: tuple[AuctionExecution, ...]
    stp_cancellations: tuple[tuple[str, str], ...]
    expirations: tuple[tuple[str, int], ...]

    @property
    def matched_quantity(self) -> int:
        return sum(item.quantity for item in self.executions)


class AuctionBook:
    """One auction pool using price priority and FIFO within equal prices.

    The clearing-price tie break is: maximum match, minimum absolute imbalance,
    closest to the configured reference, then the lower tick. Allocation puts
    market orders first, then better-priced limits, then FIFO at a price. If the
    two allocation heads share an account, the later arrival is the deterministic
    aggressor for self-trade prevention. Unmatched auction-only quantity expires.
    """

    def __init__(self) -> None:
        self._orders: dict[str, ManagedOrder] = {}
        self._trade_sequence = 0
        self._executions: list[AuctionExecution] = []
        self._uncross_history: list[dict[str, object]] = []
        self._validating_uncross_history = False

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
    def active_orders(self) -> tuple[ManagedOrder, ...]:
        return tuple(order for order in self.orders if order.remaining_quantity > 0)

    @property
    def executions(self) -> tuple[AuctionExecution, ...]:
        return tuple(self._executions)

    def add(self, order: ManagedOrder) -> None:
        order_id = order.request.order_id
        if order_id in self._orders:
            raise ValueError(f"duplicate auction order ID: {order_id}")
        if order.remaining_quantity <= 0:
            raise ValueError("auction order must have positive remaining quantity")
        order.status = "AUCTION_WORKING"
        order.resting_sequence = order.arrival_sequence
        self._orders[order_id] = order
        self.assert_invariants()

    def cancel(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        if order is None or order.remaining_quantity <= 0:
            raise ValueError(f"auction order is not active: {order_id}")
        quantity = order.remaining_quantity
        order.cancelled_quantity += quantity
        order.status = "CANCELLED"
        self.assert_invariants()
        return quantity

    def expire(self, order_id: str) -> int:
        order = self._orders.get(order_id)
        if order is None or order.remaining_quantity <= 0:
            raise ValueError(f"auction order is not active: {order_id}")
        quantity = order.remaining_quantity
        order.expired_quantity += quantity
        order.status = "EXPIRED"
        self.assert_invariants()
        return quantity

    def indication(self, reference_price_ticks: int) -> AuctionIndication:
        active = self.active_orders
        if not active:
            return AuctionIndication(None, 0, 0, None)
        candidates = {
            order.request.price_ticks
            for order in active
            if order.request.price_ticks is not None
        }
        candidates.add(reference_price_ticks)
        outcomes = []
        for price in sorted(candidates):
            demand = sum(
                order.remaining_quantity
                for order in active
                if order.request.side is Side.BUY
                and _auction_marketable(order, price)
            )
            supply = sum(
                order.remaining_quantity
                for order in active
                if order.request.side is Side.SELL
                and _auction_marketable(order, price)
            )
            matched = min(demand, supply)
            imbalance = demand - supply
            outcomes.append((price, matched, imbalance))
        price, matched, signed_imbalance = min(
            outcomes,
            key=lambda item: (
                -item[1],
                abs(item[2]),
                abs(item[0] - reference_price_ticks),
                item[0],
            ),
        )
        imbalance_side = (
            Side.BUY
            if signed_imbalance > 0
            else Side.SELL
            if signed_imbalance < 0
            else None
        )
        return AuctionIndication(
            price,
            matched,
            abs(signed_imbalance),
            imbalance_side,
        )

    def uncross(
        self,
        reference_price_ticks: int,
        stp_mode: Callable[[str], SelfTradePreventionMode],
    ) -> AuctionUncrossResult:
        if type(reference_price_ticks) is not int or reference_price_ticks <= 0:
            raise ValueError("auction reference price must be positive integer ticks")
        active_before = [order.as_dict() for order in self.active_orders]
        resolved_modes: dict[str, SelfTradePreventionMode] = {}

        def resolve_mode(account_id: str) -> SelfTradePreventionMode:
            mode = stp_mode(account_id)
            if type(mode) is not SelfTradePreventionMode:
                raise TypeError("auction STP callback must return the canonical enum")
            previous = resolved_modes.get(account_id)
            if previous is not None and previous is not mode:
                raise ValueError("auction STP callback changed within one uncross")
            resolved_modes[account_id] = mode
            return mode

        indication = self.indication(reference_price_ticks)
        price = indication.clearing_price_ticks
        if price is None:
            return AuctionUncrossResult(indication, (), (), ())
        buys = sorted(
            (
                order
                for order in self.active_orders
                if order.request.side is Side.BUY
                and _auction_marketable(order, price)
            ),
            key=_buy_priority,
        )
        sells = sorted(
            (
                order
                for order in self.active_orders
                if order.request.side is Side.SELL
                and _auction_marketable(order, price)
            ),
            key=_sell_priority,
        )
        executions: list[AuctionExecution] = []
        stp_cancellations: list[tuple[str, str]] = []
        buy_index = 0
        sell_index = 0
        while buy_index < len(buys) and sell_index < len(sells):
            buy = buys[buy_index]
            sell = sells[sell_index]
            if not buy.remaining_quantity:
                buy_index += 1
                continue
            if not sell.remaining_quantity:
                sell_index += 1
                continue
            if buy.request.account_id == sell.request.account_id:
                mode = resolve_mode(buy.request.account_id)
                if mode is not SelfTradePreventionMode.NONE:
                    cancelled = self._apply_auction_stp(buy, sell, mode)
                    stp_cancellations.extend(cancelled)
                    continue
            quantity = min(buy.remaining_quantity, sell.remaining_quantity)
            buy.filled_quantity += quantity
            sell.filled_quantity += quantity
            buy.status = "FILLED" if not buy.remaining_quantity else "PARTIALLY_FILLED"
            sell.status = (
                "FILLED" if not sell.remaining_quantity else "PARTIALLY_FILLED"
            )
            self._trade_sequence += 1
            executions.append(
                AuctionExecution(
                    f"AT{self._trade_sequence:06d}",
                    price,
                    quantity,
                    buy.request.order_id,
                    sell.request.order_id,
                )
            )
        expirations: list[tuple[str, int]] = []
        for order in self.active_orders:
            quantity = order.remaining_quantity
            order.expired_quantity += quantity
            order.status = "EXPIRED"
            expirations.append((order.request.order_id, quantity))
        self._executions.extend(executions)
        result = AuctionUncrossResult(
            indication,
            tuple(executions),
            tuple(stp_cancellations),
            tuple(expirations),
        )
        self._uncross_history.append(
            _uncross_history_state(
                sequence=len(self._uncross_history) + 1,
                reference_price_ticks=reference_price_ticks,
                active_orders_before=active_before,
                resolved_modes=resolved_modes,
                result=result,
            )
        )
        self.assert_invariants()
        return result

    def checkpoint_state(self) -> dict[str, object]:
        self.assert_invariants()
        payload: dict[str, object] = {
            "executions": [_execution_state(item) for item in self._executions],
            "orders": [order.as_dict() for order in self.orders],
            "schema_version": AUCTION_BOOK_CHECKPOINT_SCHEMA_VERSION,
            "trade_sequence": self._trade_sequence,
            "uncross_history": [
                _detached_record(item) for item in self._uncross_history
            ],
        }
        _decode_auction_state(payload, managed_orders=self._orders)
        return payload

    def canonical_state_bytes(self) -> bytes:
        return _canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        managed_orders: Mapping[str, ManagedOrder] | None = None,
    ) -> AuctionBook:
        """Restore detached state after optionally validating an engine ledger.

        ``managed_orders`` is an exact-state witness only.  Its mutable values
        are never retained by the restored auction.
        """

        orders, executions, trade_sequence, uncross_history = _decode_auction_state(
            payload,
            managed_orders=managed_orders,
        )
        restored = cls()
        restored._orders = orders
        restored._executions = executions
        restored._trade_sequence = trade_sequence
        restored._uncross_history = uncross_history
        restored.assert_invariants()
        return restored

    @classmethod
    def from_canonical_state_bytes(
        cls,
        raw: bytes,
        *,
        managed_orders: Mapping[str, ManagedOrder] | None = None,
    ) -> AuctionBook:
        return cls.from_checkpoint_state(
            _parse_canonical_json_object(raw),
            managed_orders=managed_orders,
        )

    def assert_invariants(self) -> None:
        sequences = [order.arrival_sequence for order in self.orders]
        if len(sequences) != len(set(sequences)):
            raise RuntimeError("auction arrival sequences are not unique")
        for order in self.orders:
            if min(
                order.filled_quantity,
                order.cancelled_quantity,
                order.expired_quantity,
                order.remaining_quantity,
            ) < 0:
                raise RuntimeError("auction order contains negative quantity")
            if (
                order.filled_quantity
                + order.cancelled_quantity
                + order.expired_quantity
                + order.remaining_quantity
                != order.request.quantity
            ):
                raise RuntimeError("auction order quantity does not conserve")
            if order.remaining_quantity > 0 and order.status not in {
                "AUCTION_WORKING",
                "PARTIALLY_FILLED",
            }:
                raise RuntimeError("inactive auction order retains quantity")
        if self._trade_sequence != len(self._executions):
            raise RuntimeError("auction trade sequence disagrees with execution history")
        try:
            _validate_execution_history(self._orders, self._executions)
            if not self._validating_uncross_history:
                _validate_uncross_history(
                    self._uncross_history,
                    self._orders,
                    self._executions,
                )
        except (TypeError, ValueError) as error:
            raise RuntimeError(str(error)) from error

    def _apply_auction_stp(
        self,
        buy: ManagedOrder,
        sell: ManagedOrder,
        mode: SelfTradePreventionMode,
    ) -> tuple[tuple[str, str], ...]:
        earlier, later = sorted(
            (buy, sell),
            key=lambda order: order.arrival_sequence,
        )
        if mode is SelfTradePreventionMode.CANCEL_AGGRESSOR:
            targets = (later,)
        elif mode is SelfTradePreventionMode.CANCEL_RESTING:
            targets = (earlier,)
        elif mode is SelfTradePreventionMode.CANCEL_BOTH:
            targets = (earlier, later)
        else:  # pragma: no cover - caller excludes NONE
            raise RuntimeError("unsupported auction self-trade prevention mode")
        cancelled: list[tuple[str, str]] = []
        for order in targets:
            quantity = order.remaining_quantity
            order.cancelled_quantity += quantity
            order.status = "CANCELLED_STP"
            cancelled.append((order.request.order_id, mode.value))
        return tuple(cancelled)


def _auction_marketable(order: ManagedOrder, price_ticks: int) -> bool:
    if order.request.instruction is OrderInstruction.MARKET:
        return True
    if order.request.side is Side.BUY:
        return order.request.price_ticks >= price_ticks  # type: ignore[operator]
    return order.request.price_ticks <= price_ticks  # type: ignore[operator]


def _buy_priority(order: ManagedOrder) -> tuple[int, int, int]:
    if order.request.instruction is OrderInstruction.MARKET:
        return (0, 0, order.arrival_sequence)
    return (1, -int(order.request.price_ticks), order.arrival_sequence)


def _sell_priority(order: ManagedOrder) -> tuple[int, int, int]:
    if order.request.instruction is OrderInstruction.MARKET:
        return (0, 0, order.arrival_sequence)
    return (1, int(order.request.price_ticks), order.arrival_sequence)


def _execution_state(execution: AuctionExecution) -> dict[str, object]:
    return {
        "buy_order_id": execution.buy_order_id,
        "price_ticks": execution.price_ticks,
        "quantity": execution.quantity,
        "sell_order_id": execution.sell_order_id,
        "trade_id": execution.trade_id,
    }


def _detached_record(payload: Mapping[str, object]) -> dict[str, object]:
    return _parse_canonical_json_object(_canonical_json_bytes(payload))


def _uncross_result_state(result: AuctionUncrossResult) -> dict[str, object]:
    return {
        "executions": [_execution_state(item) for item in result.executions],
        "expirations": [
            {"order_id": order_id, "quantity": quantity}
            for order_id, quantity in result.expirations
        ],
        "indication": result.indication.as_dict(),
        "stp_cancellations": [
            {"mode": mode, "order_id": order_id}
            for order_id, mode in result.stp_cancellations
        ],
    }


def _uncross_history_state(
    *,
    sequence: int,
    reference_price_ticks: int,
    active_orders_before: list[dict[str, object]],
    resolved_modes: Mapping[str, SelfTradePreventionMode],
    result: AuctionUncrossResult,
) -> dict[str, object]:
    return {
        "active_orders_before": active_orders_before,
        "reference_price_ticks": reference_price_ticks,
        "result": _uncross_result_state(result),
        "sequence": sequence,
        "stp_modes": [
            {"account_id": account_id, "mode": resolved_modes[account_id].value}
            for account_id in sorted(resolved_modes)
        ],
    }


def _decode_request(payload: Mapping[str, object]) -> AdvancedOrderRequest:
    _require_exact_fields(
        payload,
        {
            "account_id",
            "auction_only",
            "good_until_time_us",
            "instruction",
            "modifiers",
            "order_id",
            "owner",
            "price_ticks",
            "quantity",
            "side",
            "time_in_force",
        },
        "AuctionBook.AdvancedOrderRequest",
    )
    raw_modifiers = _wire_array(payload, "modifiers")
    if any(type(value) is not str for value in raw_modifiers):
        raise TypeError("serialized advanced-order modifiers must be strings")
    modifier_values = list(raw_modifiers)
    if (
        modifier_values != sorted(modifier_values)
        or len(modifier_values) != len(set(modifier_values))
    ):
        raise ValueError("serialized advanced-order modifiers must be sorted and unique")
    raw_price = _wire_optional_int(payload, "price_ticks")
    raw_expiry = _wire_optional_int(payload, "good_until_time_us")
    auction_only = payload["auction_only"]
    if type(auction_only) is not bool:
        raise TypeError("serialized auction_only must be a boolean")
    request = AdvancedOrderRequest(
        order_id=_wire_string(payload, "order_id"),
        side=Side(_wire_string(payload, "side")),
        quantity=_wire_int(payload, "quantity", minimum=1),
        instruction=OrderInstruction(_wire_string(payload, "instruction")),
        owner=OrderOwner(_wire_string(payload, "owner")),
        account_id=_wire_string(payload, "account_id"),
        price_ticks=raw_price,
        time_in_force=OrderInstruction(_wire_string(payload, "time_in_force")),
        modifiers=frozenset(OrderInstruction(value) for value in modifier_values),
        good_until_time_us=raw_expiry,
        auction_only=auction_only,
    )
    if not request.auction_only:
        raise ValueError("AuctionBook state cannot contain a continuous order")
    if request.as_dict() != dict(payload):
        raise ValueError("serialized advanced order did not round-trip exactly")
    return request


def _decode_managed_order(row: object, index: int) -> ManagedOrder:
    if not isinstance(row, Mapping):
        raise TypeError(f"serialized auction orders[{index}] must be an object")
    _require_exact_fields(
        row,
        {
            "arrival_sequence",
            "cancelled_quantity",
            "expired_quantity",
            "filled_quantity",
            "remaining_quantity",
            "request",
            "resting_sequence",
            "status",
        },
        f"AuctionBook.orders[{index}]",
    )
    request = _decode_request(_wire_object(row, "request"))
    arrival = _wire_int(row, "arrival_sequence", minimum=1)
    filled = _wire_int(row, "filled_quantity")
    cancelled = _wire_int(row, "cancelled_quantity")
    expired = _wire_int(row, "expired_quantity")
    serialized_remaining = _wire_int(row, "remaining_quantity")
    resting = _wire_optional_int(row, "resting_sequence")
    if resting != arrival:
        raise ValueError("serialized auction resting sequence must equal arrival sequence")
    status = _wire_string(row, "status")
    if status not in _AUCTION_STORED_STATUSES:
        raise ValueError("serialized auction order status is unsupported")
    managed = ManagedOrder(
        request=request,
        arrival_sequence=arrival,
        status=status,
        filled_quantity=filled,
        cancelled_quantity=cancelled,
        expired_quantity=expired,
        resting_sequence=resting,
    )
    if managed.remaining_quantity != serialized_remaining:
        raise ValueError("serialized auction order quantities do not conserve")
    if managed.remaining_quantity > 0:
        if status not in _AUCTION_ACTIVE_STATUSES:
            raise ValueError("serialized inactive auction status retains live quantity")
    elif status not in _AUCTION_CLOSED_STATUSES:
        raise ValueError("serialized active auction status has no live quantity")
    if status == "PARTIALLY_FILLED" and filled <= 0:
        raise ValueError("PARTIALLY_FILLED auction order requires filled quantity")
    if status == "FILLED" and filled != request.quantity:
        raise ValueError("FILLED auction order quantity is inconsistent")
    if status in {"CANCELLED", "CANCELLED_STP"} and cancelled <= 0:
        raise ValueError("cancelled auction order requires cancelled quantity")
    if status == "EXPIRED" and expired <= 0:
        raise ValueError("expired auction order requires expired quantity")
    if managed.as_dict() != dict(row):
        raise ValueError("serialized auction order did not round-trip exactly")
    return managed


def _decode_execution(row: object, index: int) -> AuctionExecution:
    if not isinstance(row, Mapping):
        raise TypeError(f"serialized auction executions[{index}] must be an object")
    _require_exact_fields(
        row,
        {"buy_order_id", "price_ticks", "quantity", "sell_order_id", "trade_id"},
        f"AuctionBook.executions[{index}]",
    )
    execution = AuctionExecution(
        trade_id=_wire_string(row, "trade_id"),
        price_ticks=_wire_int(row, "price_ticks", minimum=1),
        quantity=_wire_int(row, "quantity", minimum=1),
        buy_order_id=_wire_string(row, "buy_order_id"),
        sell_order_id=_wire_string(row, "sell_order_id"),
    )
    if execution.trade_id != f"AT{index + 1:06d}":
        raise ValueError("serialized auction trade IDs are not contiguous")
    if _execution_state(execution) != dict(row):
        raise ValueError("serialized auction execution did not round-trip exactly")
    return execution


def _validate_execution_history(
    orders: Mapping[str, ManagedOrder],
    executions: list[AuctionExecution] | tuple[AuctionExecution, ...],
) -> None:
    filled_by_order = {order_id: 0 for order_id in orders}
    for index, execution in enumerate(executions):
        if execution.trade_id != f"AT{index + 1:06d}":
            raise ValueError("auction trade IDs are not contiguous")
        if type(execution.price_ticks) is not int or execution.price_ticks <= 0:
            raise ValueError("auction execution price must be positive integer ticks")
        if type(execution.quantity) is not int or execution.quantity <= 0:
            raise ValueError("auction execution quantity must be positive")
        try:
            buy = orders[execution.buy_order_id]
            sell = orders[execution.sell_order_id]
        except KeyError as error:
            raise ValueError("auction execution references an unknown order") from error
        if (
            buy is sell
            or buy.request.side is not Side.BUY
            or sell.request.side is not Side.SELL
        ):
            raise ValueError("auction execution sides are inconsistent")
        if not _auction_marketable(buy, execution.price_ticks) or not _auction_marketable(
            sell,
            execution.price_ticks,
        ):
            raise ValueError("auction execution price violates an order limit")
        filled_by_order[buy.request.order_id] += execution.quantity
        filled_by_order[sell.request.order_id] += execution.quantity
    if any(
        order.filled_quantity != filled_by_order[order_id]
        for order_id, order in orders.items()
    ):
        raise ValueError("auction order fills disagree with execution history")


def _decode_indication(payload: Mapping[str, object]) -> AuctionIndication:
    _require_exact_fields(
        payload,
        {
            "clearing_price_ticks",
            "imbalance_quantity",
            "imbalance_side",
            "matched_quantity",
        },
        "AuctionBook uncross indication",
    )
    price = _wire_optional_int(payload, "clearing_price_ticks")
    if price is not None and price <= 0:
        raise ValueError("auction clearing price must be positive or null")
    raw_side = payload["imbalance_side"]
    if raw_side is not None and type(raw_side) is not str:
        raise TypeError("auction imbalance side must be a string or null")
    indication = AuctionIndication(
        clearing_price_ticks=price,
        matched_quantity=_wire_int(payload, "matched_quantity"),
        imbalance_quantity=_wire_int(payload, "imbalance_quantity"),
        imbalance_side=None if raw_side is None else Side(raw_side),
    )
    if indication.as_dict() != dict(payload):
        raise ValueError("auction indication did not round-trip exactly")
    return indication


def _decode_stp_rows(raw: object) -> tuple[tuple[str, str], ...]:
    if type(raw) is not list:
        raise TypeError("auction STP cancellation history must be an array")
    result: list[tuple[str, str]] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise TypeError(f"auction STP cancellation {index} must be an object")
        _require_exact_fields(
            row,
            {"mode", "order_id"},
            f"AuctionBook stp_cancellations[{index}]",
        )
        order_id = _wire_string(row, "order_id")
        mode = SelfTradePreventionMode(_wire_string(row, "mode")).value
        result.append((order_id, mode))
    return tuple(result)


def _decode_expiration_rows(raw: object) -> tuple[tuple[str, int], ...]:
    if type(raw) is not list:
        raise TypeError("auction expiration history must be an array")
    result: list[tuple[str, int]] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise TypeError(f"auction expiration {index} must be an object")
        _require_exact_fields(
            row,
            {"order_id", "quantity"},
            f"AuctionBook expirations[{index}]",
        )
        result.append(
            (
                _wire_string(row, "order_id"),
                _wire_int(row, "quantity", minimum=1),
            )
        )
    return tuple(result)


def _validate_uncross_history(
    history: list[dict[str, object]],
    orders: Mapping[str, ManagedOrder],
    executions: list[AuctionExecution] | tuple[AuctionExecution, ...],
) -> None:
    """Replay each historical call from its immutable pre-uncross snapshot."""

    if type(history) is not list or any(not isinstance(row, Mapping) for row in history):
        raise TypeError("auction uncross history must be an object array")
    expected_execution_rows: list[dict[str, object]] = []
    covered_order_ids: set[str] = set()
    execution_offset = 0
    for history_index, raw_record in enumerate(history):
        assert isinstance(raw_record, Mapping)
        _require_exact_fields(
            raw_record,
            {
                "active_orders_before",
                "reference_price_ticks",
                "result",
                "sequence",
                "stp_modes",
            },
            f"AuctionBook.uncross_history[{history_index}]",
        )
        if _wire_int(raw_record, "sequence", minimum=1) != history_index + 1:
            raise ValueError("auction uncross sequence is not contiguous")
        reference_price = _wire_int(
            raw_record,
            "reference_price_ticks",
            minimum=1,
        )
        raw_pre_orders = _wire_array(raw_record, "active_orders_before")
        if not raw_pre_orders:
            raise ValueError("recorded auction uncross must have active orders")
        pre_orders = [
            _decode_managed_order(row, index)
            for index, row in enumerate(raw_pre_orders)
        ]
        arrivals = [order.arrival_sequence for order in pre_orders]
        pre_ids = [order.request.order_id for order in pre_orders]
        if arrivals != sorted(arrivals) or len(arrivals) != len(set(arrivals)):
            raise ValueError("pre-uncross orders are not in unique arrival order")
        if len(pre_ids) != len(set(pre_ids)) or covered_order_ids.intersection(pre_ids):
            raise ValueError("an auction order appears in multiple uncross histories")
        for pre_order in pre_orders:
            if (
                pre_order.status != "AUCTION_WORKING"
                or pre_order.filled_quantity
                or pre_order.cancelled_quantity
                or pre_order.expired_quantity
                or pre_order.remaining_quantity != pre_order.request.quantity
            ):
                raise ValueError("pre-uncross order is not a pristine active auction order")
            try:
                final_order = orders[pre_order.request.order_id]
            except KeyError as error:
                raise ValueError("uncross history references an unknown final order") from error
            if (
                final_order.request != pre_order.request
                or final_order.arrival_sequence != pre_order.arrival_sequence
                or final_order.resting_sequence != pre_order.resting_sequence
            ):
                raise ValueError("uncross pre-state disagrees with final order identity")

        raw_modes = _wire_array(raw_record, "stp_modes")
        modes: dict[str, SelfTradePreventionMode] = {}
        mode_order: list[str] = []
        for mode_index, raw_mode in enumerate(raw_modes):
            if not isinstance(raw_mode, Mapping):
                raise TypeError(f"auction stp_modes[{mode_index}] must be an object")
            _require_exact_fields(
                raw_mode,
                {"account_id", "mode"},
                f"AuctionBook.stp_modes[{mode_index}]",
            )
            account_id = _wire_string(raw_mode, "account_id")
            mode = SelfTradePreventionMode(_wire_string(raw_mode, "mode"))
            if account_id in modes:
                raise ValueError("auction STP history contains a duplicate account")
            modes[account_id] = mode
            mode_order.append(account_id)
        if mode_order != sorted(mode_order):
            raise ValueError("auction STP history must be sorted by account")

        raw_result = _wire_object(raw_record, "result")
        _require_exact_fields(
            raw_result,
            {"executions", "expirations", "indication", "stp_cancellations"},
            f"AuctionBook.uncross_history[{history_index}].result",
        )
        indication = _decode_indication(_wire_object(raw_result, "indication"))
        raw_execution_rows = _wire_array(raw_result, "executions")
        expected_executions = [
            _decode_execution(row, execution_offset + index)
            for index, row in enumerate(raw_execution_rows)
        ]
        expected_stp = _decode_stp_rows(raw_result["stp_cancellations"])
        expected_expirations = _decode_expiration_rows(raw_result["expirations"])

        replayed = AuctionBook()
        replayed._validating_uncross_history = True
        for pre_order in pre_orders:
            replayed.add(pre_order)
        accessed_modes: set[str] = set()

        def replay_mode(account_id: str) -> SelfTradePreventionMode:
            try:
                mode = modes[account_id]
            except KeyError as error:
                raise ValueError("auction STP history omits a consulted account") from error
            accessed_modes.add(account_id)
            return mode

        generated = replayed.uncross(reference_price, replay_mode)
        if accessed_modes != set(modes):
            raise ValueError("auction STP history contains an unconsulted account")
        if generated.indication != indication:
            raise ValueError("auction clearing indication differs from historical replay")
        generated_execution_core = [
            (
                item.price_ticks,
                item.quantity,
                item.buy_order_id,
                item.sell_order_id,
            )
            for item in generated.executions
        ]
        expected_execution_core = [
            (
                item.price_ticks,
                item.quantity,
                item.buy_order_id,
                item.sell_order_id,
            )
            for item in expected_executions
        ]
        if generated_execution_core != expected_execution_core:
            raise ValueError("auction execution price/FIFO allocation history is corrupt")
        if generated.stp_cancellations != expected_stp:
            raise ValueError("auction STP cancellation history is corrupt")
        if generated.expirations != expected_expirations:
            raise ValueError("auction expiration history is corrupt")
        for generated_order in replayed.orders:
            final_order = orders[generated_order.request.order_id]
            if generated_order.as_dict() != final_order.as_dict():
                raise ValueError("auction final order differs from historical uncross replay")

        expected_execution_rows.extend(
            _execution_state(item) for item in expected_executions
        )
        execution_offset += len(expected_executions)
        covered_order_ids.update(pre_ids)

    if expected_execution_rows != [_execution_state(item) for item in executions]:
        raise ValueError("global auction executions differ from uncross histories")
    if any(
        order.filled_quantity > 0 and order_id not in covered_order_ids
        for order_id, order in orders.items()
    ):
        raise ValueError("filled auction order lacks an uncross history")


def _decode_auction_state(
    payload: Mapping[str, object],
    *,
    managed_orders: Mapping[str, ManagedOrder] | None,
) -> tuple[
    dict[str, ManagedOrder],
    list[AuctionExecution],
    int,
    list[dict[str, object]],
]:
    _validate_strict_json(payload)
    _require_exact_fields(
        payload,
        {
            "executions",
            "orders",
            "schema_version",
            "trade_sequence",
            "uncross_history",
        },
        "AuctionBookCheckpointV1",
    )
    if _wire_int(payload, "schema_version", minimum=1) != AUCTION_BOOK_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("AuctionBook checkpoint schema version is unsupported")
    raw_rows = _wire_array(payload, "orders")
    decoded = [_decode_managed_order(row, index) for index, row in enumerate(raw_rows)]
    arrival_sequences = [order.arrival_sequence for order in decoded]
    order_ids = [order.request.order_id for order in decoded]
    if arrival_sequences != sorted(arrival_sequences):
        raise ValueError("serialized auction orders are not in arrival order")
    if len(arrival_sequences) != len(set(arrival_sequences)):
        raise ValueError("serialized auction arrival sequences are duplicated")
    if len(order_ids) != len(set(order_ids)):
        raise ValueError("serialized auction order IDs are duplicated")
    detached = {order.request.order_id: order for order in decoded}

    if managed_orders is not None:
        if not isinstance(managed_orders, Mapping) or any(
            type(key) is not str for key in managed_orders
        ):
            raise TypeError("managed_orders must be a string-keyed mapping")
        for index, row in enumerate(raw_rows):
            assert isinstance(row, Mapping)
            order_id = decoded[index].request.order_id
            try:
                managed = managed_orders[order_id]
            except KeyError as error:
                raise ValueError("managed-order ledger omits an auction order") from error
            if type(managed) is not ManagedOrder:
                raise TypeError("managed-order ledger contains a non-ManagedOrder value")
            if managed.request.order_id != order_id or managed.as_dict() != dict(row):
                raise ValueError("managed-order ledger disagrees with auction state")
        for key, managed in managed_orders.items():
            if type(managed) is not ManagedOrder or managed.request.order_id != key:
                raise ValueError("managed-order ledger key is inconsistent")
            if (
                managed.request.auction_only
                and managed.status in _AUCTION_STORED_STATUSES
                and key not in detached
            ):
                raise ValueError("managed-order ledger has an unowned accepted auction order")

    # The optional ledger is a validation witness, never an ownership transfer.
    # Returning the decoded graph prevents later caller mutation from changing a
    # successfully restored auction.  Cross-component identity must be assembled
    # by the enclosing engine from these newly owned rows after validation.
    selected = detached
    raw_executions = _wire_array(payload, "executions")
    executions = [
        _decode_execution(row, index) for index, row in enumerate(raw_executions)
    ]
    trade_sequence = _wire_int(payload, "trade_sequence")
    if trade_sequence != len(executions):
        raise ValueError("serialized auction trade allocator is rolled back")
    _validate_execution_history(selected, executions)
    raw_history = _wire_array(payload, "uncross_history")
    uncross_history: list[dict[str, object]] = []
    for index, row in enumerate(raw_history):
        if not isinstance(row, Mapping):
            raise TypeError(f"auction uncross_history[{index}] must be an object")
        uncross_history.append(_detached_record(row))
    _validate_uncross_history(uncross_history, selected, executions)
    return selected, executions, trade_sequence, uncross_history
