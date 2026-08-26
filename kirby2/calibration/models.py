"""Canonical normalized market events and descriptive calibration reports."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


NORMALIZED_MARKET_SCHEMA_VERSION = 1
CALIBRATION_REPORT_SCHEMA_VERSION = 1


class NormalizedEventType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    CANCEL = "CANCEL"
    TRADE = "TRADE"
    BOOK = "BOOK"


class ObservationCapability(str, Enum):
    ORDER_EVENTS = "ORDER_EVENTS"
    TRADE_EVENTS = "TRADE_EVENTS"
    AGGRESSOR_SIDE = "AGGRESSOR_SIDE"
    BOOK_SPREAD = "BOOK_SPREAD"
    BOOK_PRICES = "BOOK_PRICES"
    BOOK_DEPTH = "BOOK_DEPTH"


@dataclass(frozen=True, slots=True)
class BookLevel:
    price_ticks: int
    quantity: int

    def __post_init__(self) -> None:
        if type(self.price_ticks) is not int or self.price_ticks <= 0:
            raise ValueError("book-level price must be positive integer ticks")
        if type(self.quantity) is not int or self.quantity < 0:
            raise ValueError("book-level quantity must be a nonnegative integer")

    def as_dict(self) -> dict[str, int]:
        return {"price_ticks": self.price_ticks, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class NormalizedMarketEvent:
    sequence: int
    timestamp_us: int
    event_type: NormalizedEventType
    side: str | None = None
    quantity: int | None = None
    price_ticks: int | None = None
    order_id: str | None = None
    target_order_id: str | None = None
    maker_order_id: str | None = None
    taker_order_id: str | None = None
    aggressor_side: str | None = None
    remaining_quantity: int | None = None
    spread_ticks: int | None = None
    bid_levels: tuple[BookLevel, ...] = ()
    ask_levels: tuple[BookLevel, ...] = ()

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("normalized sequence must be a positive integer")
        if type(self.timestamp_us) is not int or self.timestamp_us < 0:
            raise ValueError("normalized timestamp must be nonnegative microseconds")
        if self.side not in {None, "buy", "sell"}:
            raise ValueError("normalized side must be buy, sell, or null")
        if self.aggressor_side not in {None, "buy", "sell"}:
            raise ValueError("aggressor side must be buy, sell, or null")
        for value, name, positive in (
            (self.quantity, "quantity", True),
            (self.price_ticks, "price_ticks", True),
            (self.remaining_quantity, "remaining_quantity", False),
            (self.spread_ticks, "spread_ticks", True),
        ):
            if value is not None and (
                type(value) is not int or value < (1 if positive else 0)
            ):
                raise ValueError(f"normalized {name} is invalid")
        if self.event_type is NormalizedEventType.BOOK:
            _ordered_levels(self.bid_levels, descending=True)
            _ordered_levels(self.ask_levels, descending=False)
        elif self.event_type is NormalizedEventType.TRADE:
            if (
                self.quantity is None
                or self.price_ticks is None
            ):
                raise ValueError("trade event requires price and size")
        elif self.event_type in {NormalizedEventType.LIMIT, NormalizedEventType.MARKET}:
            if self.quantity is None or self.side is None or not self.order_id:
                raise ValueError("order event requires ID, side, and quantity")
            if self.event_type is NormalizedEventType.LIMIT and self.price_ticks is None:
                raise ValueError("limit event requires a tick price")
        elif self.event_type is NormalizedEventType.CANCEL:
            if self.quantity is None or not self.target_order_id:
                raise ValueError("cancel event requires target ID and cancelled size")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_type": self.event_type.value,
            "sequence": self.sequence,
            "timestamp_us": self.timestamp_us,
        }
        optional = {
            "aggressor_side": self.aggressor_side,
            "maker_order_id": self.maker_order_id,
            "order_id": self.order_id,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "remaining_quantity": self.remaining_quantity,
            "side": self.side,
            "spread_ticks": self.spread_ticks,
            "taker_order_id": self.taker_order_id,
            "target_order_id": self.target_order_id,
        }
        result.update({key: value for key, value in optional.items() if value is not None})
        if self.event_type is NormalizedEventType.BOOK:
            result["ask_levels"] = [level.as_dict() for level in self.ask_levels]
            result["bid_levels"] = [level.as_dict() for level in self.bid_levels]
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NormalizedMarketEvent:
        def levels(key: str) -> tuple[BookLevel, ...]:
            raw = payload.get(key, [])
            if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
                raise ValueError(f"normalized {key} must be an array of objects")
            return tuple(
                BookLevel(int(item["price_ticks"]), int(item["quantity"]))
                for item in raw
            )

        return cls(
            sequence=int(payload["sequence"]),
            timestamp_us=int(payload["timestamp_us"]),
            event_type=NormalizedEventType(str(payload["event_type"]).upper()),
            side=_optional_string(payload, "side"),
            quantity=_optional_int(payload, "quantity"),
            price_ticks=_optional_int(payload, "price_ticks"),
            order_id=_optional_string(payload, "order_id"),
            target_order_id=_optional_string(payload, "target_order_id"),
            maker_order_id=_optional_string(payload, "maker_order_id"),
            taker_order_id=_optional_string(payload, "taker_order_id"),
            aggressor_side=_optional_string(payload, "aggressor_side"),
            remaining_quantity=_optional_int(payload, "remaining_quantity"),
            spread_ticks=_optional_int(payload, "spread_ticks"),
            bid_levels=levels("bid_levels"),
            ask_levels=levels("ask_levels"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedMarketStream:
    source_id: str
    source_kind: str
    duration_us: int
    tick_size: Decimal
    events: tuple[NormalizedMarketEvent, ...]
    capabilities: tuple[ObservationCapability, ...]
    real_market_data: bool = False

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_kind:
            raise ValueError("normalized stream source identity is required")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("normalized stream duration must be positive")
        if not isinstance(self.tick_size, Decimal) or not self.tick_size.is_finite():
            raise TypeError("normalized tick size must be a finite Decimal")
        if self.tick_size <= 0:
            raise ValueError("normalized tick size must be positive")
        if type(self.real_market_data) is not bool:
            raise TypeError("real-market-data marker must be boolean")
        if (
            not self.capabilities
            or any(
                not isinstance(capability, ObservationCapability)
                for capability in self.capabilities
            )
            or len(self.capabilities) != len(set(self.capabilities))
        ):
            raise ValueError("normalized observation capabilities must be nonempty and unique")
        sequences = tuple(event.sequence for event in self.events)
        if sequences != tuple(range(1, len(self.events) + 1)):
            raise ValueError("normalized event sequences must be contiguous")
        timestamps = tuple(event.timestamp_us for event in self.events)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("normalized timestamps must be monotonic")
        if timestamps and timestamps[-1] > self.duration_us:
            raise ValueError("normalized event exceeds stream duration")

    def json_lines(self) -> str:
        header = {
            "duration_us": self.duration_us,
            "capabilities": [capability.value for capability in self.capabilities],
            "real_market_data": self.real_market_data,
            "record_type": "normalized_market_stream",
            "schema_version": NORMALIZED_MARKET_SCHEMA_VERSION,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "tick_size": str(self.tick_size),
        }
        lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
        lines.extend(
            json.dumps(
                {"record_type": "normalized_market_event", **event.as_dict()},
                sort_keys=True,
                separators=(",", ":"),
            )
            for event in self.events
        )
        return "\n".join(lines)

    def sha256(self) -> str:
        return hashlib.sha256(self.json_lines().encode("utf-8")).hexdigest()

    @classmethod
    def from_json_lines(cls, text: str) -> NormalizedMarketStream:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not records or not isinstance(records[0], dict):
            raise ValueError("normalized stream is empty")
        header = records[0]
        if (
            header.get("record_type") != "normalized_market_stream"
            or header.get("schema_version") != NORMALIZED_MARKET_SCHEMA_VERSION
        ):
            raise ValueError("unsupported normalized market stream header")
        raw_capabilities = header.get("capabilities")
        real_market_data = header.get("real_market_data", False)
        if not isinstance(raw_capabilities, list):
            raise ValueError("normalized stream capabilities must be an array")
        if type(real_market_data) is not bool:
            raise ValueError("normalized real_market_data must be a JSON boolean")
        events: list[NormalizedMarketEvent] = []
        for record in records[1:]:
            if not isinstance(record, dict) or record.get("record_type") != "normalized_market_event":
                raise ValueError("normalized stream contains an unknown record")
            events.append(NormalizedMarketEvent.from_dict(record))
        return cls(
            source_id=str(header["source_id"]),
            source_kind=str(header["source_kind"]),
            duration_us=int(header["duration_us"]),
            tick_size=Decimal(str(header["tick_size"])),
            events=tuple(events),
            capabilities=tuple(
                ObservationCapability(str(value))
                for value in raw_capabilities
            ),
            real_market_data=real_market_data,
        )


@dataclass(frozen=True, slots=True)
class CalibrationMetric:
    name: str
    unit: str
    sample_count: int
    value: object | None
    available: bool = True
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.unit:
            raise ValueError("calibration metric name and unit are required")
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("metric sample count must be nonnegative")
        if type(self.available) is not bool:
            raise TypeError("metric availability must be boolean")
        if self.available and self.value is None:
            raise ValueError("available metric must contain a value")
        _finite_tree(self.value)

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "name": self.name,
            "note": self.note,
            "sample_count": self.sample_count,
            "unit": self.unit,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    source_id: str
    source_kind: str
    real_market_data: bool
    duration_us: int
    normalized_stream_sha256: str
    observation_capabilities: tuple[ObservationCapability, ...]
    metrics: tuple[CalibrationMetric, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_kind or not self.normalized_stream_sha256:
            raise ValueError("calibration report source fields are required")
        names = tuple(metric.name for metric in self.metrics)
        if len(names) != len(set(names)):
            raise ValueError("calibration metric names must be unique")

    def metric(self, name: str) -> CalibrationMetric:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        raise KeyError(name)

    def as_dict(self) -> dict[str, object]:
        return {
            "descriptive_only": True,
            "duration_us": self.duration_us,
            "metrics": {metric.name: metric.as_dict() for metric in self.metrics},
            "normalized_stream_sha256": self.normalized_stream_sha256,
            "observation_capabilities": [
                capability.value for capability in self.observation_capabilities
            ],
            "real_market_data": self.real_market_data,
            "schema_version": CALIBRATION_REPORT_SCHEMA_VERSION,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "statistical_equivalence_claimed": False,
            "warnings": list(self.warnings),
        }


def _ordered_levels(levels: tuple[BookLevel, ...], *, descending: bool) -> None:
    prices = tuple(level.price_ticks for level in levels)
    expected = tuple(sorted(prices, reverse=descending))
    if prices != expected or len(prices) != len(set(prices)):
        raise ValueError("normalized book levels are not correctly ordered")


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    return None if value is None else int(value)


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return None if value is None else str(value)


def _finite_tree(value: object | None) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("calibration metric values must be finite")
    if isinstance(value, dict):
        for child in value.values():
            _finite_tree(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _finite_tree(child)
