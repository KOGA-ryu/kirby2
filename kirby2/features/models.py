"""Canonical microstructure feature vocabulary and metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class FeatureKey(str, Enum):
    MID_PRICE = "mid_price"
    MICROPRICE = "microprice"
    SPREAD_TICKS = "spread_ticks"
    TOP_LEVEL_IMBALANCE = "top_level_imbalance"
    MULTI_LEVEL_IMBALANCE = "multi_level_imbalance"
    WEIGHTED_DEPTH_BID = "weighted_depth_bid"
    WEIGHTED_DEPTH_ASK = "weighted_depth_ask"
    AGGRESSIVE_BUY_VOLUME = "aggressive_buy_volume"
    AGGRESSIVE_SELL_VOLUME = "aggressive_sell_volume"
    TRADE_IMBALANCE = "trade_imbalance"
    TRADE_VELOCITY = "trade_velocity"
    CANCEL_VELOCITY_BID = "cancel_velocity_bid"
    CANCEL_VELOCITY_ASK = "cancel_velocity_ask"
    QUEUE_DEPLETION_BID = "queue_depletion_bid"
    QUEUE_DEPLETION_ASK = "queue_depletion_ask"
    QUEUE_REPLENISHMENT_BID = "queue_replenishment_bid"
    QUEUE_REPLENISHMENT_ASK = "queue_replenishment_ask"
    SHORT_TERM_RETURN = "short_term_return"
    SHORT_TERM_VOLATILITY = "short_term_volatility"
    RELATIVE_VOLUME = "relative_volume"
    PRICE_VELOCITY = "price_velocity"
    PRICE_ACCELERATION = "price_acceleration"
    BEST_BID_SIZE = "best_bid_size"
    BEST_ASK_SIZE = "best_ask_size"
    BUY_SELL_RATIO = "buy_sell_ratio"
    SHORT_TERM_PRICE_CHANGE_TICKS = "short_term_price_change_ticks"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    key: FeatureKey
    units: str
    input_source: str
    update_rule: str
    windowed: bool
    valid_minimum: Decimal | None = None
    valid_maximum: Decimal | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "feature": self.key.value,
            "input_source": self.input_source,
            "units": self.units,
            "update_rule": self.update_rule,
            "valid_range": {
                "maximum": None
                if self.valid_maximum is None
                else str(self.valid_maximum),
                "minimum": None
                if self.valid_minimum is None
                else str(self.valid_minimum),
            },
            "window": "required" if self.windowed else "instantaneous",
        }


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    simulation_time_us: int
    windows_us: tuple[int, ...]
    values: dict[tuple[FeatureKey, int | None], Decimal | None]

    def __post_init__(self) -> None:
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("feature frame time must be nonnegative microseconds")
        if (
            not self.windows_us
            or tuple(sorted(set(self.windows_us))) != self.windows_us
            or any(type(window) is not int or window <= 0 for window in self.windows_us)
        ):
            raise ValueError("feature frame windows must be unique sorted positives")
        expected = {
            (definition.key, window if definition.windowed else None)
            for definition in FEATURE_CATALOG.values()
            for window in (self.windows_us if definition.windowed else (None,))
        }
        if set(self.values) != expected:
            raise ValueError("feature frame values do not match the canonical catalog")

    def value(self, key: FeatureKey, window_us: int | None = None) -> Decimal | None:
        definition = FEATURE_CATALOG[key]
        actual_window = window_us if definition.windowed else None
        if definition.windowed and actual_window not in self.windows_us:
            raise ValueError(f"feature {key.value} requires a configured window")
        return self.values[(key, actual_window)]

    def as_dict(self) -> dict[str, str | None]:
        return {
            feature_field_name(key, window): None if value is None else str(value)
            for (key, window), value in sorted(
                self.values.items(),
                key=lambda item: feature_field_name(*item[0]),
            )
        }

    def sha256(self) -> str:
        canonical = json.dumps(
            {
                "simulation_time_us": self.simulation_time_us,
                "values": self.as_dict(),
                "windows_us": self.windows_us,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def feature_field_name(key: FeatureKey, window_us: int | None) -> str:
    if window_us is None:
        return key.value
    return f"{key.value}_{window_label(window_us)}"


def window_label(window_us: int) -> str:
    if window_us % 1_000_000 == 0:
        return f"{window_us // 1_000_000}s"
    if window_us % 1_000 == 0:
        return f"{window_us // 1_000}ms"
    return f"{window_us}us"


def feature_catalog_as_dict() -> dict[str, object]:
    return {
        key.value: FEATURE_CATALOG[key].as_dict()
        for key in FeatureKey
    }


def feature_catalog_sha256() -> str:
    canonical = json.dumps(
        feature_catalog_as_dict(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _definition(
    key: FeatureKey,
    units: str,
    source: str,
    rule: str,
    *,
    windowed: bool = False,
    minimum: str | None = None,
    maximum: str | None = None,
) -> FeatureDefinition:
    return FeatureDefinition(
        key=key,
        units=units,
        input_source=source,
        update_rule=rule,
        windowed=windowed,
        valid_minimum=None if minimum is None else Decimal(minimum),
        valid_maximum=None if maximum is None else Decimal(maximum),
    )


FEATURE_CATALOG = {
    item.key: item
    for item in (
        _definition(FeatureKey.MID_PRICE, "ticks", "best bid and ask", "(bid + ask) / 2"),
        _definition(
            FeatureKey.MICROPRICE,
            "ticks",
            "best prices and displayed top sizes",
            "(ask * bid_size + bid * ask_size) / (bid_size + ask_size); opposing-queue convention",
        ),
        _definition(FeatureKey.SPREAD_TICKS, "ticks", "best bid and ask", "ask - bid", minimum="1"),
        _definition(
            FeatureKey.TOP_LEVEL_IMBALANCE,
            "ratio",
            "displayed top sizes",
            "(bid_size - ask_size) / (bid_size + ask_size)",
            minimum="-1",
            maximum="1",
        ),
        _definition(
            FeatureKey.MULTI_LEVEL_IMBALANCE,
            "ratio",
            "displayed depth over configured levels",
            "(bid_depth - ask_depth) / total_depth",
            minimum="-1",
            maximum="1",
        ),
        _definition(FeatureKey.WEIGHTED_DEPTH_BID, "weighted shares", "bid depth", "sum(quantity / level_rank)"),
        _definition(FeatureKey.WEIGHTED_DEPTH_ASK, "weighted shares", "ask depth", "sum(quantity / level_rank)"),
        _definition(FeatureKey.AGGRESSIVE_BUY_VOLUME, "shares", "TRADE taker side", "rolling sum", windowed=True, minimum="0"),
        _definition(FeatureKey.AGGRESSIVE_SELL_VOLUME, "shares", "TRADE taker side", "rolling sum", windowed=True, minimum="0"),
        _definition(FeatureKey.TRADE_IMBALANCE, "ratio", "aggressive trade volume", "(buy - sell) / total", windowed=True, minimum="-1", maximum="1"),
        _definition(FeatureKey.TRADE_VELOCITY, "trades/second", "TRADE events", "rolling count / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.CANCEL_VELOCITY_BID, "shares/second", "ORDER_CANCELLED bid events", "rolling cancelled quantity / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.CANCEL_VELOCITY_ASK, "shares/second", "ORDER_CANCELLED ask events", "rolling cancelled quantity / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.QUEUE_DEPLETION_BID, "shares/second", "sell-aggressor TRADE events", "rolling executed bid quantity / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.QUEUE_DEPLETION_ASK, "shares/second", "buy-aggressor TRADE events", "rolling executed ask quantity / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.QUEUE_REPLENISHMENT_BID, "shares/second", "ORDER_ADDED bid events", "rolling added quantity / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.QUEUE_REPLENISHMENT_ASK, "shares/second", "ORDER_ADDED ask events", "rolling added quantity / window seconds", windowed=True, minimum="0"),
        _definition(FeatureKey.SHORT_TERM_RETURN, "ratio", "causal midpoint samples", "current midpoint / oldest in-window midpoint - 1", windowed=True),
        _definition(FeatureKey.SHORT_TERM_VOLATILITY, "basis points", "causal midpoint samples", "sqrt(sum(squared consecutive returns)) * 10000", windowed=True, minimum="0"),
        _definition(FeatureKey.RELATIVE_VOLUME, "ratio", "scenario/session dimensions", "current configured relative-volume factor", minimum="0"),
        _definition(FeatureKey.PRICE_VELOCITY, "ticks/second", "causal midpoint samples", "midpoint change / elapsed window time", windowed=True),
        _definition(FeatureKey.PRICE_ACCELERATION, "ticks/second^2", "causal midpoint samples", "recent-half velocity change / half-window time", windowed=True),
        _definition(FeatureKey.BEST_BID_SIZE, "shares", "best bid queue", "displayed total quantity", minimum="0"),
        _definition(FeatureKey.BEST_ASK_SIZE, "shares", "best ask queue", "displayed total quantity", minimum="0"),
        _definition(FeatureKey.BUY_SELL_RATIO, "ratio", "aggressive rolling volume", "(buy + 1) / (sell + 1)", windowed=True, minimum="0"),
        _definition(FeatureKey.SHORT_TERM_PRICE_CHANGE_TICKS, "ticks", "causal midpoint samples", "current midpoint - oldest in-window midpoint", windowed=True),
    )
}
