"""Player position ledger derived exclusively from exchange fills."""

from __future__ import annotations

from dataclasses import dataclass, field

from kirby2.exchange.models import Fill, OrderOwner, Side


@dataclass(slots=True)
class PlayerPosition:
    position: int = 0
    bought_quantity: int = 0
    sold_quantity: int = 0
    fills: list[Fill] = field(default_factory=list)

    def apply(self, fill: Fill) -> None:
        if fill.owner is not OrderOwner.PLAYER:
            return
        self.fills.append(fill)
        if fill.side is Side.BUY:
            self.bought_quantity += fill.quantity
        else:
            self.sold_quantity += fill.quantity
        self.position += fill.side.sign * fill.quantity

    def snapshot(self) -> dict[str, int]:
        return {
            "bought_quantity": self.bought_quantity,
            "position": self.position,
            "sold_quantity": self.sold_quantity,
        }
