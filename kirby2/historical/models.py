"""Historical-mode contracts with explicit observation and provenance boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from kirby2.exchange import OrderBook
from kirby2.session.events import SimulationEvent


class HistoricalDataMode(str, Enum):
    EXACT_REPLAY = "EXACT_REPLAY"
    RECONSTRUCTION = "RECONSTRUCTION"

    @classmethod
    def parse(cls, value: str) -> HistoricalDataMode:
        return cls(value.upper())


@dataclass(frozen=True, slots=True)
class HistoricalProvenance:
    dataset_id: str
    source_name: str
    source_locator: str
    description: str
    license_note: str
    real_market_data: bool
    provides_order_events: bool
    provides_trade_events: bool
    provides_book_events: bool
    provides_trade_aggressor_side: bool = False

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.dataset_id,
                self.source_name,
                self.source_locator,
                self.description,
                self.license_note,
            )
        ):
            raise ValueError("historical provenance text fields must not be empty")
        flags = (
            self.real_market_data,
            self.provides_order_events,
            self.provides_trade_events,
            self.provides_book_events,
            self.provides_trade_aggressor_side,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("historical provenance capability flags must be booleans")
        if self.provides_trade_aggressor_side and not self.provides_trade_events:
            raise ValueError("trade aggressor side requires source trade events")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "description": self.description,
            "license_note": self.license_note,
            "provides_book_events": self.provides_book_events,
            "provides_order_events": self.provides_order_events,
            "provides_trade_events": self.provides_trade_events,
            "provides_trade_aggressor_side": self.provides_trade_aggressor_side,
            "real_market_data": self.real_market_data,
            "source_locator": self.source_locator,
            "source_name": self.source_name,
        }


@dataclass(frozen=True, slots=True)
class ExactOrderMessage:
    sequence: int
    timestamp_us: int
    action: str
    order_id: str
    side: str | None = None
    quantity: int = 0
    price_ticks: int | None = None
    target_order_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("exact message sequence must be positive")
        if type(self.timestamp_us) is not int or self.timestamp_us < 0:
            raise ValueError("exact message timestamp must be nonnegative")
        if self.action not in {"limit", "market", "cancel"}:
            raise ValueError("exact message action must be limit, market, or cancel")
        if not self.order_id:
            raise ValueError("exact message order ID must not be empty")
        if self.action == "cancel":
            if not self.target_order_id:
                raise ValueError("exact cancel message requires a target order")
            if self.side is not None or self.quantity != 0 or self.price_ticks is not None:
                raise ValueError("exact cancel message cannot carry trading fields")
        else:
            if self.side not in {"buy", "sell"}:
                raise ValueError("exact trading message requires buy or sell side")
            if type(self.quantity) is not int or self.quantity <= 0:
                raise ValueError("exact trading quantity must be positive")
            if self.action == "limit":
                if type(self.price_ticks) is not int or self.price_ticks <= 0:
                    raise ValueError("exact limit price must be positive integer ticks")
            elif self.price_ticks is not None:
                raise ValueError("exact market message cannot carry a price")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "order_id": self.order_id,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "sequence": self.sequence,
            "side": self.side,
            "target_order_id": self.target_order_id,
            "timestamp_us": self.timestamp_us,
        }


@dataclass(frozen=True, slots=True)
class ExpectedTrade:
    trade_id: str
    price_ticks: int
    quantity: int
    maker_order_id: str
    taker_order_id: str
    taker_side: str

    def __post_init__(self) -> None:
        if not self.trade_id or not self.maker_order_id or not self.taker_order_id:
            raise ValueError("expected trade identifiers must not be empty")
        if type(self.price_ticks) is not int or self.price_ticks <= 0:
            raise ValueError("expected trade price must be positive integer ticks")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("expected trade quantity must be positive")
        if self.taker_side not in {"buy", "sell"}:
            raise ValueError("expected trade taker side must be buy or sell")

    def as_dict(self) -> dict[str, object]:
        return {
            "maker_order_id": self.maker_order_id,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "taker_order_id": self.taker_order_id,
            "taker_side": self.taker_side,
            "trade_id": self.trade_id,
        }


@dataclass(frozen=True, slots=True)
class ExactReplayFixture:
    fixture_id: str
    label: str
    duration_us: int
    tick_size: Decimal
    session_start: str
    provenance: HistoricalProvenance
    messages: tuple[ExactOrderMessage, ...]
    expected_trades: tuple[ExpectedTrade, ...]

    def __post_init__(self) -> None:
        if not self.fixture_id or not self.label or not self.session_start:
            raise ValueError("exact fixture identity fields must not be empty")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("exact fixture duration must be positive")
        if not isinstance(self.tick_size, Decimal) or self.tick_size <= 0:
            raise ValueError("exact fixture tick size must be a positive Decimal")
        if not self.provenance.provides_order_events:
            raise ValueError("EXACT_REPLAY requires source-provided order events")
        if not self.messages:
            raise ValueError("exact fixture requires messages")
        sequences = tuple(message.sequence for message in self.messages)
        if sequences != tuple(range(1, len(self.messages) + 1)):
            raise ValueError("exact message sequences must be contiguous")
        timestamps = tuple(message.timestamp_us for message in self.messages)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("exact message timestamps must be monotonic")
        if timestamps[-1] > self.duration_us:
            raise ValueError("exact message exceeds fixture duration")
        order_ids = tuple(message.order_id for message in self.messages)
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("exact fixture command IDs must be unique")
        trade_ids = tuple(trade.trade_id for trade in self.expected_trades)
        if len(trade_ids) != len(set(trade_ids)):
            raise ValueError("exact fixture trade IDs must be unique")


@dataclass(frozen=True, slots=True)
class TradePrintObservation:
    timestamp_us: int
    price_ticks: int
    quantity: int
    aggressor_side: str | None = None

    def __post_init__(self) -> None:
        if type(self.timestamp_us) is not int or self.timestamp_us < 0:
            raise ValueError("print timestamp must be nonnegative")
        if type(self.price_ticks) is not int or self.price_ticks <= 0:
            raise ValueError("print price must be positive integer ticks")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("print quantity must be positive")
        if self.aggressor_side not in {None, "buy", "sell"}:
            raise ValueError("print aggressor side must be buy, sell, or unavailable")

    def as_dict(self) -> dict[str, int | str | None]:
        return {
            "aggressor_side": self.aggressor_side,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "timestamp_us": self.timestamp_us,
        }


@dataclass(frozen=True, slots=True)
class SpreadObservation:
    timestamp_us: int
    spread_ticks: int

    def __post_init__(self) -> None:
        if type(self.timestamp_us) is not int or self.timestamp_us < 0:
            raise ValueError("spread timestamp must be nonnegative")
        if type(self.spread_ticks) is not int or self.spread_ticks <= 0:
            raise ValueError("spread must be positive integer ticks")

    def as_dict(self) -> dict[str, int]:
        return {
            "spread_ticks": self.spread_ticks,
            "timestamp_us": self.timestamp_us,
        }


@dataclass(frozen=True, slots=True)
class HistoricalConstraints:
    session_start: str
    duration_us: int
    tick_size: Decimal
    open_ticks: int
    high_ticks: int
    low_ticks: int
    close_ticks: int
    aggregate_volume: int
    realized_volatility_bps: Decimal
    spread_observations: tuple[SpreadObservation, ...]
    trade_prints: tuple[TradePrintObservation, ...]
    known_market_events: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.session_start:
            raise ValueError("historical constraints require a session start")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("historical constraint duration must be positive")
        if not isinstance(self.tick_size, Decimal) or self.tick_size <= 0:
            raise ValueError("historical constraint tick size must be positive")
        prices = (self.open_ticks, self.high_ticks, self.low_ticks, self.close_ticks)
        if any(type(price) is not int or price <= 0 for price in prices):
            raise ValueError("historical OHLC values must be positive integer ticks")
        if self.low_ticks > min(self.open_ticks, self.close_ticks):
            raise ValueError("historical low must not exceed open or close")
        if self.high_ticks < max(self.open_ticks, self.close_ticks):
            raise ValueError("historical high must not be below open or close")
        if type(self.aggregate_volume) is not int or self.aggregate_volume < 0:
            raise ValueError("historical aggregate volume must be nonnegative")
        if (
            not isinstance(self.realized_volatility_bps, Decimal)
            or self.realized_volatility_bps < 0
        ):
            raise ValueError("historical realized volatility must be nonnegative")
        for observation in (*self.spread_observations, *self.trade_prints):
            if observation.timestamp_us > self.duration_us:
                raise ValueError("historical observation exceeds constraint duration")
        if any(not event for event in self.known_market_events):
            raise ValueError("known market event descriptions must not be empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregate_volume": self.aggregate_volume,
            "close_ticks": self.close_ticks,
            "duration_us": self.duration_us,
            "high_ticks": self.high_ticks,
            "known_market_events": list(self.known_market_events),
            "low_ticks": self.low_ticks,
            "open_ticks": self.open_ticks,
            "realized_volatility_bps": str(self.realized_volatility_bps),
            "session_start": self.session_start,
            "spread_observations": [
                observation.as_dict() for observation in self.spread_observations
            ],
            "tick_size": str(self.tick_size),
            "trade_prints": [observation.as_dict() for observation in self.trade_prints],
        }


@dataclass(frozen=True, slots=True)
class ReconstructionFixture:
    fixture_id: str
    label: str
    seed: int
    provenance: HistoricalProvenance
    constraints: HistoricalConstraints

    def __post_init__(self) -> None:
        if not self.fixture_id or not self.label:
            raise ValueError("reconstruction fixture identity must not be empty")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("reconstruction seed must be nonnegative")
        if self.provenance.provides_order_events:
            raise ValueError("reconstruction fixture cannot claim source order events")


@dataclass(frozen=True, slots=True)
class HistoricalCommandRecord:
    sequence: int
    simulation_time_us: int
    action: str
    applied: bool
    command: dict[str, object] | None
    exchange_event_start: int | None
    exchange_event_end: int | None
    order_provenance: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("historical command sequence must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("historical command time must be nonnegative")
        if not self.action or not self.order_provenance:
            raise ValueError("historical command labels must not be empty")
        if type(self.applied) is not bool:
            raise TypeError("historical command applied flag must be boolean")
        event_bounds = (self.exchange_event_start, self.exchange_event_end)
        if self.applied:
            if self.command is None:
                raise ValueError("applied historical command requires command data")
            if any(type(value) is not int or value <= 0 for value in event_bounds):
                raise ValueError("applied command requires positive event bounds")
            if self.exchange_event_start > self.exchange_event_end:  # type: ignore[operator]
                raise ValueError("historical command event bounds are reversed")
        elif self.command is not None or any(value is not None for value in event_bounds):
            raise ValueError("skipped historical arrival cannot claim a command or events")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "applied": self.applied,
            "command": self.command,
            "exchange_event_end": self.exchange_event_end,
            "exchange_event_start": self.exchange_event_start,
            "historical_command_sequence": self.sequence,
            "order_provenance": self.order_provenance,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(slots=True)
class HistoricalRun:
    fixture_id: str
    label: str
    mode: HistoricalDataMode
    provenance: HistoricalProvenance
    duration_us: int
    tick_size: Decimal
    book: OrderBook
    source_message_count: int
    source_trade_count: int
    synthetic_command_count: int
    commands: tuple[HistoricalCommandRecord, ...]
    spread_samples_ticks: tuple[int, ...]
    constraints: HistoricalConstraints | None = None
    reconstruction_seed: int | None = None
    reconstruction_config: dict[str, object] | None = None
    initial_trade_count: int = 0

    def __post_init__(self) -> None:
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("historical run duration must be positive")
        if not isinstance(self.tick_size, Decimal) or self.tick_size <= 0:
            raise ValueError("historical run tick size must be a positive Decimal")
        counts = (
            self.source_message_count,
            self.source_trade_count,
            self.synthetic_command_count,
            self.initial_trade_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("historical run counts must be nonnegative integers")
        if any(type(spread) is not int or spread <= 0 for spread in self.spread_samples_ticks):
            raise ValueError("historical spread samples must be positive integer ticks")
        sequences = tuple(command.sequence for command in self.commands)
        if sequences != tuple(range(1, len(self.commands) + 1)):
            raise ValueError("historical command sequences must be contiguous")
        timestamps = tuple(command.simulation_time_us for command in self.commands)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("historical command times must be monotonic")
        if timestamps and timestamps[-1] > self.duration_us:
            raise ValueError("historical command exceeds run duration")
        covered_events = tuple(
            event_sequence
            for command in self.commands
            if command.exchange_event_start is not None
            and command.exchange_event_end is not None
            for event_sequence in range(
                command.exchange_event_start,
                command.exchange_event_end + 1,
            )
        )
        expected_events = tuple(range(1, len(self.exchange_events) + 1))
        if covered_events != expected_events:
            raise ValueError("historical commands must map every exchange event exactly once")
        expected_provenance = self.order_provenance_label
        if any(
            command.order_provenance != expected_provenance
            for command in self.commands
        ):
            raise ValueError("historical command provenance does not match run mode")
        if self.mode is HistoricalDataMode.EXACT_REPLAY:
            if self.constraints is not None or self.reconstruction_seed is not None:
                raise ValueError("exact replay cannot carry reconstruction inputs")
            if self.synthetic_command_count != 0:
                raise ValueError("exact replay cannot count synthetic commands")
            if self.source_message_count != len(self.commands):
                raise ValueError("exact replay source count does not match commands")
        else:
            if self.constraints is None or self.reconstruction_seed is None:
                raise ValueError("reconstruction requires constraints and a seed")
            if self.source_message_count != 0 or self.source_trade_count != 0:
                raise ValueError("reconstruction cannot claim source order or trade messages")
            if self.synthetic_command_count != len(self.commands):
                raise ValueError("reconstruction synthetic count does not match commands")

    @property
    def exchange_events(self) -> tuple[SimulationEvent, ...]:
        return self.book.journal.events

    @property
    def generated_trades(self):
        return self.book.trades[self.initial_trade_count :]

    @property
    def order_provenance_label(self) -> str:
        if self.mode is HistoricalDataMode.EXACT_REPLAY:
            return "SOURCE_FIXTURE_EXACT_MESSAGES"
        return "SYNTHETIC_RECONSTRUCTION"

    def replay_sha256(self) -> str:
        return hashlib.sha256(self.replay_json_lines().encode("utf-8")).hexdigest()

    def replay_json_lines(self) -> str:
        header = {
            "constraints": None if self.constraints is None else self.constraints.as_dict(),
            "duration_us": self.duration_us,
            "fixture_id": self.fixture_id,
            "historical_mode": self.mode.value,
            "order_provenance": self.order_provenance_label,
            "provenance": self.provenance.as_dict(),
            "reconstruction_config": self.reconstruction_config,
            "reconstruction_seed": self.reconstruction_seed,
            "record_type": "historical_run",
            "tick_size": str(self.tick_size),
        }
        lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
        exchange_events = {event.sequence: event for event in self.exchange_events}
        for command in self.commands:
            lines.append(
                json.dumps(
                    {"record_type": "historical_command", **command.as_dict()},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if command.exchange_event_start is None or command.exchange_event_end is None:
                continue
            for sequence in range(
                command.exchange_event_start,
                command.exchange_event_end + 1,
            ):
                lines.append(
                    json.dumps(
                        {
                            "exchange_event": exchange_events[sequence].as_dict(),
                            "historical_command_sequence": command.sequence,
                            "historical_mode": self.mode.value,
                            "order_provenance": command.order_provenance,
                            "record_type": "exchange_event",
                            "simulation_time_us": command.simulation_time_us,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        return "\n".join(lines)
