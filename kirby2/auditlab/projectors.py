"""Independent ledger projections used by generated structural checks."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from kirby2.exchange import Fill, OrderOwner, Side
from kirby2.session import EventType, SimulationEvent


@dataclass(frozen=True, slots=True)
class PlayerLedgerProjection:
    """One immutable player cash-and-position projection."""

    bought_shares: int
    sold_shares: int
    position_shares: int
    cash_tick_shares: int

    def as_dict(self) -> dict[str, int]:
        return {
            "bought_shares": self.bought_shares,
            "cash_tick_shares": self.cash_tick_shares,
            "position_shares": self.position_shares,
            "sold_shares": self.sold_shares,
        }


class FillLedgerProjector:
    """Project player inventory directly from the immutable fill ledger."""

    @staticmethod
    def project(fills: Iterable[Fill]) -> PlayerLedgerProjection:
        bought_shares = 0
        sold_shares = 0
        position_shares = 0
        cash_tick_shares = 0
        for fill in fills:
            if not isinstance(fill, Fill):
                raise TypeError("fill-ledger projector requires Fill records")
            if fill.owner is not OrderOwner.PLAYER:
                continue
            if fill.side is Side.BUY:
                bought_shares += fill.quantity
                position_shares += fill.quantity
                cash_tick_shares -= fill.price_ticks * fill.quantity
            else:
                sold_shares += fill.quantity
                position_shares -= fill.quantity
                cash_tick_shares += fill.price_ticks * fill.quantity
        return PlayerLedgerProjection(
            bought_shares,
            sold_shares,
            position_shares,
            cash_tick_shares,
        )


class EventLedgerProjector:
    """Project player inventory independently from submitted/fill events."""

    @staticmethod
    def project(events: Iterable[SimulationEvent]) -> PlayerLedgerProjection:
        player_sides: dict[str, Side] = {}
        bought_shares = 0
        sold_shares = 0
        position_shares = 0
        cash_tick_shares = 0
        for event in events:
            if not isinstance(event, SimulationEvent):
                raise TypeError("event-ledger projector requires SimulationEvent records")
            data = event.data
            if (
                event.event_type is EventType.ORDER_SUBMITTED
                and data.get("owner") == OrderOwner.PLAYER.value
                and data.get("side") is not None
            ):
                player_sides[str(data["order_id"])] = Side(str(data["side"]))
                continue
            if event.event_type not in {
                EventType.PARTIAL_FILL,
                EventType.FULL_FILL,
            }:
                continue
            side = player_sides.get(str(data["order_id"]))
            if side is None:
                continue
            quantity = int(data["fill_quantity"])
            price_ticks = int(data["price_ticks"])
            if side is Side.BUY:
                bought_shares += quantity
                position_shares += quantity
                cash_tick_shares -= price_ticks * quantity
            else:
                sold_shares += quantity
                position_shares -= quantity
                cash_tick_shares += price_ticks * quantity
        return PlayerLedgerProjection(
            bought_shares,
            sold_shares,
            position_shares,
            cash_tick_shares,
        )
