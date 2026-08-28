"""Player position ledger derived exclusively from exchange fills."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from kirby2.exchange.models import Fill, OrderOwner, Side
from kirby2.immutable import freeze_json, thaw_json


PLAYER_POSITION_CHECKPOINT_SCHEMA_VERSION = 1


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
        raise TypeError("canonical player-position state must be bytes")
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
        raise ValueError("player-position state is not canonical UTF-8 JSON") from error
    if type(value) is not dict:
        raise ValueError("player-position state must be a JSON object")
    if _canonical_json_bytes(value) != payload:
        raise ValueError("player-position state bytes are not canonical")
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


def _require_int(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or (positive and value <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{label} must be a {qualifier}integer")
    return value


def _fill_state(fill: Fill) -> dict[str, object]:
    return {
        "liquidity": fill.liquidity,
        "order_id": fill.order_id,
        "owner": fill.owner.value,
        "price_ticks": fill.price_ticks,
        "quantity": fill.quantity,
        "side": fill.side.value,
        "trade_id": fill.trade_id,
    }


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

    def apply_reported_fill(
        self,
        *,
        trade_id: str,
        order_id: str,
        side: Side,
        price_ticks: int,
        quantity: int,
        liquidity: str,
    ) -> Fill:
        """Apply one client-delivered player fill using the ordinary fill ledger."""

        if type(trade_id) is not str or not trade_id:
            raise ValueError("reported player fill requires a trade ID")
        if type(order_id) is not str or not order_id:
            raise ValueError("reported player fill requires an order ID")
        if not isinstance(side, Side):
            raise TypeError("reported player fill side must use Side")
        if type(price_ticks) is not int or price_ticks <= 0:
            raise ValueError("reported player fill price must be positive ticks")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("reported player fill quantity must be positive")
        if type(liquidity) is not str or liquidity not in {"maker", "taker"}:
            raise ValueError("reported player fill liquidity must be maker or taker")
        fill = Fill(
            trade_id=trade_id,
            order_id=order_id,
            owner=OrderOwner.PLAYER,
            side=side,
            price_ticks=price_ticks,
            quantity=quantity,
            liquidity=liquidity,
        )
        self.apply(fill)
        self.assert_invariants()
        return fill

    def snapshot(self) -> dict[str, int]:
        payload: dict[str, object] = {
            "bought_quantity": self.bought_quantity,
            "position": self.position,
            "sold_quantity": self.sold_quantity,
        }
        _validate_strict_checkpoint_json(payload)
        return payload

    def checkpoint_state(self) -> dict[str, object]:
        """Return totals plus the authoritative ordered player-fill history."""

        self.assert_invariants()
        payload: dict[str, object] = {
            "bought_quantity": self.bought_quantity,
            "fills": [_fill_state(fill) for fill in self.fills],
            "position": self.position,
            "schema_version": PLAYER_POSITION_CHECKPOINT_SCHEMA_VERSION,
            "sold_quantity": self.sold_quantity,
        }
        _validate_strict_checkpoint_json(payload)
        return payload

    def assert_invariants(self) -> None:
        identities: set[tuple[str, str, str]] = set()
        bought_quantity = 0
        sold_quantity = 0
        for fill in self.fills:
            if type(fill) is not Fill or fill.owner is not OrderOwner.PLAYER:
                raise RuntimeError("player position contains a non-player fill")
            identity = (fill.trade_id, fill.order_id, fill.liquidity)
            if identity in identities:
                raise RuntimeError("player position contains a duplicate fill")
            identities.add(identity)
            if fill.side is Side.BUY:
                bought_quantity += fill.quantity
            elif fill.side is Side.SELL:
                sold_quantity += fill.quantity
            else:  # pragma: no cover - valid Fill instances use Side
                raise RuntimeError("player position contains an invalid fill side")
        if (
            type(self.bought_quantity) is not int
            or type(self.sold_quantity) is not int
            or type(self.position) is not int
            or self.bought_quantity != bought_quantity
            or self.sold_quantity != sold_quantity
            or self.position != bought_quantity - sold_quantity
        ):
            raise RuntimeError("player position totals do not reconcile to fill history")

    def canonical_state_bytes(self) -> bytes:
        return _canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> PlayerPosition:
        """Recompute all totals before constructing a detached position ledger."""

        if not isinstance(payload, Mapping):
            raise TypeError("player-position checkpoint state must be a mapping")
        _validate_strict_checkpoint_json(payload)
        _require_exact_fields(
            payload,
            frozenset(
                {
                    "bought_quantity",
                    "fills",
                    "position",
                    "schema_version",
                    "sold_quantity",
                }
            ),
            "player-position checkpoint",
        )
        schema_version = _require_int(
            payload["schema_version"],
            "player-position schema version",
        )
        if schema_version != PLAYER_POSITION_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported player-position checkpoint schema")
        raw_fills = payload["fills"]
        if type(raw_fills) is not list:
            raise ValueError("player fill history must be an ordered array")
        fills: list[Fill] = []
        identities: set[tuple[str, str, str]] = set()
        for raw_fill in raw_fills:
            if type(raw_fill) is not dict:
                raise ValueError("player fill rows must be objects")
            _require_exact_fields(
                raw_fill,
                frozenset(
                    {
                        "liquidity",
                        "order_id",
                        "owner",
                        "price_ticks",
                        "quantity",
                        "side",
                        "trade_id",
                    }
                ),
                "player fill",
            )
            for name in ("liquidity", "order_id", "owner", "side", "trade_id"):
                if type(raw_fill[name]) is not str or not raw_fill[name]:
                    raise ValueError(f"player fill {name} must be a nonempty string")
            owner = OrderOwner(raw_fill["owner"])
            if owner is not OrderOwner.PLAYER:
                raise ValueError("player fill history cannot contain non-player fills")
            fill = Fill(
                trade_id=raw_fill["trade_id"],
                order_id=raw_fill["order_id"],
                owner=owner,
                side=Side(raw_fill["side"]),
                price_ticks=_require_int(
                    raw_fill["price_ticks"],
                    "player fill price",
                    positive=True,
                ),
                quantity=_require_int(
                    raw_fill["quantity"],
                    "player fill quantity",
                    positive=True,
                ),
                liquidity=raw_fill["liquidity"],
            )
            identity = (fill.trade_id, fill.order_id, fill.liquidity)
            if identity in identities:
                raise ValueError("player fill history contains a duplicate fill")
            identities.add(identity)
            fills.append(fill)
        bought_quantity = sum(
            fill.quantity for fill in fills if fill.side is Side.BUY
        )
        sold_quantity = sum(
            fill.quantity for fill in fills if fill.side is Side.SELL
        )
        position = bought_quantity - sold_quantity
        recorded = (
            _require_int(payload["bought_quantity"], "player bought quantity"),
            _require_int(payload["sold_quantity"], "player sold quantity"),
            _require_int(payload["position"], "player net position"),
        )
        if min(recorded[:2]) < 0:
            raise ValueError("player bought and sold quantities cannot be negative")
        if recorded != (bought_quantity, sold_quantity, position):
            raise ValueError("player position totals do not reconcile to fill history")
        restored = cls(
            position=position,
            bought_quantity=bought_quantity,
            sold_quantity=sold_quantity,
            fills=list(fills),
        )
        if _canonical_json_bytes(restored.checkpoint_state()) != _canonical_json_bytes(
            payload
        ):
            raise ValueError("player-position checkpoint state is not canonical")
        return restored

    @classmethod
    def from_canonical_state_bytes(cls, payload: bytes) -> PlayerPosition:
        return cls.from_checkpoint_state(_load_canonical_json_object(payload))
