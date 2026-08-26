"""Adapters into the canonical normalized observable market-event schema."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from kirby2.exchange import Order, OrderBook, OrderType, Side
from kirby2.historical import ExactReplayFixture, ReconstructionFixture
from kirby2.simulation import SimulationResult

from .models import (
    BookLevel,
    NormalizedEventType,
    NormalizedMarketEvent,
    NormalizedMarketStream,
    ObservationCapability,
)


@dataclass(slots=True)
class _StreamBuilder:
    source_id: str
    source_kind: str
    duration_us: int
    tick_size: Decimal
    real_market_data: bool
    capabilities: tuple[ObservationCapability, ...]
    book: OrderBook = field(default_factory=OrderBook)
    events: list[NormalizedMarketEvent] = field(default_factory=list)

    def command(self, timestamp_us: int, command: dict[str, object]) -> None:
        order_type = str(command["order_type"])
        order_id = str(command.get("order_id") or command.get("command_id"))
        if order_id == "None":
            raise ValueError("normalized command lacks an order or command ID")
        target: str | None = None
        cancelled_quantity: int | None = None
        if order_type == OrderType.LIMIT.value:
            order = Order.limit(
                order_id,
                Side(str(command["side"])),
                int(command["quantity"]),
                int(command["price_ticks"]),
            )
        elif order_type == OrderType.MARKET.value:
            order = Order.market(
                order_id,
                Side(str(command["side"])),
                int(command["quantity"]),
            )
        elif order_type == OrderType.CANCEL.value:
            target = str(
                command.get("target_order_id") or command.get("cancel_target_id")
            )
            active = self.book.active_orders.get(target)
            if active is None:
                raise ValueError(
                    f"normalized cancel targets inactive order: {target}"
                )
            cancelled_quantity = active.remaining_quantity
            order = Order.cancel(order_id, target)
        else:
            raise ValueError(f"unsupported normalized command type: {order_type}")

        prior_trade_count = len(self.book.trades)
        self.book.process(order)
        if order_type == OrderType.LIMIT.value:
            active = self.book.active_orders.get(order_id)
            self._append(
                timestamp_us,
                NormalizedEventType.LIMIT,
                side=str(command["side"]),
                quantity=int(command["quantity"]),
                price_ticks=int(command["price_ticks"]),
                order_id=order_id,
                remaining_quantity=0 if active is None else active.remaining_quantity,
            )
        elif order_type == OrderType.MARKET.value:
            self._append(
                timestamp_us,
                NormalizedEventType.MARKET,
                side=str(command["side"]),
                quantity=int(command["quantity"]),
                order_id=order_id,
            )
        else:
            self._append(
                timestamp_us,
                NormalizedEventType.CANCEL,
                quantity=cancelled_quantity,
                order_id=order_id,
                target_order_id=target,
            )

        for trade in self.book.trades[prior_trade_count:]:
            self._append(
                timestamp_us,
                NormalizedEventType.TRADE,
                aggressor_side=trade.taker_side.value,
                maker_order_id=trade.maker_order_id,
                price_ticks=trade.price_ticks,
                quantity=trade.quantity,
                taker_order_id=trade.taker_order_id,
            )
        self.snapshot(timestamp_us)

    def observed_trade(
        self,
        timestamp_us: int,
        price_ticks: int,
        quantity: int,
        aggressor_side: str | None = None,
    ) -> None:
        self._append(
            timestamp_us,
            NormalizedEventType.TRADE,
            aggressor_side=aggressor_side,
            price_ticks=price_ticks,
            quantity=quantity,
        )

    def observed_spread(
        self,
        timestamp_us: int,
        spread_ticks: int,
    ) -> None:
        self._append(
            timestamp_us,
            NormalizedEventType.BOOK,
            spread_ticks=spread_ticks,
        )

    def snapshot(self, timestamp_us: int) -> None:
        snapshot = self.book.snapshot()
        self._append(
            timestamp_us,
            NormalizedEventType.BOOK,
            bid_levels=_levels(snapshot["bids"]),
            ask_levels=_levels(snapshot["asks"]),
        )

    def build(self) -> NormalizedMarketStream:
        return NormalizedMarketStream(
            source_id=self.source_id,
            source_kind=self.source_kind,
            duration_us=self.duration_us,
            tick_size=self.tick_size,
            events=tuple(self.events),
            capabilities=self.capabilities,
            real_market_data=self.real_market_data,
        )

    def _append(
        self,
        timestamp_us: int,
        event_type: NormalizedEventType,
        **kwargs: object,
    ) -> None:
        self.events.append(
            NormalizedMarketEvent(
                sequence=len(self.events) + 1,
                timestamp_us=timestamp_us,
                event_type=event_type,
                **kwargs,
            )
        )


def normalize_simulation(
    simulation: SimulationResult,
    source_id: str = "kirby2_simulation",
) -> NormalizedMarketStream:
    return normalize_kirby_replay(simulation.replay_json_lines(), source_id)


def normalize_kirby_replay(
    replay_json_lines: str,
    source_id: str = "kirby2_replay",
) -> NormalizedMarketStream:
    records = [json.loads(line) for line in replay_json_lines.splitlines() if line.strip()]
    if not records or records[0].get("record_type") != "simulation_config":
        raise ValueError("Kirby2 replay lacks a simulation configuration header")
    header = records[0]
    config = header["config"]
    if not isinstance(config, dict):
        raise ValueError("Kirby2 replay configuration must be an object")
    builder = _StreamBuilder(
        source_id=source_id,
        source_kind="KIRBY2_SYNTHETIC",
        duration_us=int(header["duration_seconds"]) * 1_000_000,
        tick_size=Decimal(str(config["tick_size"])),
        real_market_data=False,
        capabilities=tuple(ObservationCapability),
    )

    first_flow_index = next(
        (index for index, record in enumerate(records) if record.get("record_type") == "flow_event"),
        len(records),
    )
    for record in records[1:first_flow_index]:
        if record.get("record_type") != "exchange_event" or record.get("type") != "ORDER_SUBMITTED":
            continue
        data = record.get("data")
        if not isinstance(data, dict) or data.get("order_type") != "limit":
            continue
        builder.command(
            0,
            {
                "order_id": data["order_id"],
                "order_type": "limit",
                "price_ticks": data["price_ticks"],
                "quantity": data["original_quantity"],
                "side": data["side"],
            },
        )

    for record in records[first_flow_index:]:
        if record.get("record_type") != "flow_event" or not record.get("applied"):
            continue
        command = record.get("command")
        if not isinstance(command, dict):
            raise ValueError("applied Kirby2 flow event lacks command data")
        builder.command(int(record["simulation_time_us"]), command)
    return builder.build()


def normalize_exact_fixture(fixture: ExactReplayFixture) -> NormalizedMarketStream:
    builder = _StreamBuilder(
        source_id=fixture.provenance.dataset_id,
        source_kind="REFERENCE_EXACT_MESSAGES",
        duration_us=fixture.duration_us,
        tick_size=fixture.tick_size,
        real_market_data=fixture.provenance.real_market_data,
        capabilities=tuple(ObservationCapability),
    )
    for message in fixture.messages:
        command: dict[str, object] = {
            "order_id": message.order_id,
            "order_type": message.action,
        }
        if message.action == "cancel":
            command["target_order_id"] = message.target_order_id
        else:
            command.update(
                {
                    "quantity": message.quantity,
                    "side": message.side,
                }
            )
            if message.price_ticks is not None:
                command["price_ticks"] = message.price_ticks
        builder.command(message.timestamp_us, command)
    actual_trades = tuple(
        (
            trade.price_ticks,
            trade.quantity,
            trade.maker_order_id,
            trade.taker_order_id,
            trade.taker_side.value,
        )
        for trade in builder.book.trades
    )
    expected_trades = tuple(
        (
            trade.price_ticks,
            trade.quantity,
            trade.maker_order_id,
            trade.taker_order_id,
            trade.taker_side,
        )
        for trade in fixture.expected_trades
    )
    if actual_trades != expected_trades:
        raise RuntimeError("exact fixture trades diverged during normalization")
    return builder.build()


def normalize_reconstruction_fixture(
    fixture: ReconstructionFixture,
) -> NormalizedMarketStream:
    """Normalize only source observations; do not relabel reconstruction as observed L2."""

    constraints = fixture.constraints
    builder = _StreamBuilder(
        source_id=fixture.provenance.dataset_id,
        source_kind="REFERENCE_AGGREGATE_OBSERVATIONS",
        duration_us=constraints.duration_us,
        tick_size=constraints.tick_size,
        real_market_data=fixture.provenance.real_market_data,
        capabilities=(
            ObservationCapability.TRADE_EVENTS,
            ObservationCapability.BOOK_SPREAD,
        ),
    )
    records: list[tuple[int, str, object]] = []
    records.extend((item.timestamp_us, "spread", item) for item in constraints.spread_observations)
    records.extend((item.timestamp_us, "trade", item) for item in constraints.trade_prints)
    for _, kind, item in sorted(records, key=lambda value: (value[0], value[1])):
        if kind == "spread":
            builder.observed_spread(item.timestamp_us, item.spread_ticks)
        else:
            builder.observed_trade(
                item.timestamp_us,
                item.price_ticks,
                item.quantity,
                None,
            )
    return builder.build()


def _levels(raw: object) -> tuple[BookLevel, ...]:
    if not isinstance(raw, list):
        raise ValueError("exchange snapshot levels must be arrays")
    return tuple(
        BookLevel(int(level["price_ticks"]), int(level["total_quantity"]))
        for level in raw
        if isinstance(level, dict)
    )
