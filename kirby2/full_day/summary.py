"""Deterministic, versioned summaries of verified full-day event ledgers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isqrt

from .events import FullDayEventTypeV1, FullDayEventV1, NativeLedgerEntryV1
from .models import (
    FullDayPlanV1,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)


DAY_SUMMARY_SCHEMA_VERSION = 1
PERIOD_SUMMARY_SCHEMA_VERSION = 1
DAY_SUMMARY_FORMULA_SET_ID = "KIRBY2_DAY_SUMMARY_FORMULAS_V1"
UNAVAILABLE = "UNAVAILABLE"
FIXED_POINT_SCALE = 1_000_000
DEPTH_LEVEL_COUNT = 5
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _metric(value: object, field: str) -> int | str:
    if value == UNAVAILABLE:
        return UNAVAILABLE
    return _exact_int(value, field)


def _round_half_up(numerator: int, denominator: int) -> int:
    if denominator <= 0 or numerator < 0:
        raise ValueError("fixed-point division requires nonnegative numerator/positive denominator")
    return (numerator + denominator // 2) // denominator


@dataclass(frozen=True, slots=True)
class PeriodSummaryV1:
    schema_version: int
    period_id: str
    start_time_us: int
    end_time_us: int
    open_price_ticks: int | str
    high_price_ticks: int | str
    low_price_ticks: int | str
    close_price_ticks: int | str
    volume_shares: int
    trade_count: int
    volatility_ticks_fixed: int | str
    volatility_difference_count: int
    quote_observed_duration_us: int
    time_weighted_spread_ticks_fixed: int | str
    depth_observed_duration_us: int
    time_weighted_n_level_depth_shares_fixed: int | str

    def __post_init__(self) -> None:
        if self.schema_version != PERIOD_SUMMARY_SCHEMA_VERSION:
            raise ValueError("period summary schema version must be 1")
        if type(self.period_id) is not str or not self.period_id:
            raise ValueError("period summary ID is required")
        start = _exact_int(self.start_time_us, "start_time_us")
        end = _exact_int(self.end_time_us, "end_time_us")
        if end < start:
            raise ValueError("period summary bounds are reversed")
        prices = tuple(
            _metric(value, field)
            for value, field in (
                (self.open_price_ticks, "open_price_ticks"),
                (self.high_price_ticks, "high_price_ticks"),
                (self.low_price_ticks, "low_price_ticks"),
                (self.close_price_ticks, "close_price_ticks"),
            )
        )
        unavailable = tuple(value == UNAVAILABLE for value in prices)
        if any(unavailable) and not all(unavailable):
            raise ValueError("period OHLC prices must be jointly available or unavailable")
        volume = _exact_int(self.volume_shares, "volume_shares")
        count = _exact_int(self.trade_count, "trade_count")
        if (count == 0) != all(unavailable):
            raise ValueError("period OHLC availability differs from its trade count")
        if (count == 0 and volume != 0) or (count > 0 and volume < count):
            raise ValueError("period volume does not conserve positive trade quantities")
        if count:
            open_price, high_price, low_price, close_price = prices
            assert all(type(value) is int for value in prices)
            if not (
                low_price <= open_price <= high_price
                and low_price <= close_price <= high_price
            ):
                raise ValueError("period OHLC extrema are inconsistent")
        _metric(self.volatility_ticks_fixed, "volatility_ticks_fixed")
        differences = _exact_int(
            self.volatility_difference_count, "volatility_difference_count"
        )
        if differences != max(0, count - 1):
            raise ValueError("volatility denominator differs from consecutive trades")
        if (differences == 0) != (self.volatility_ticks_fixed == UNAVAILABLE):
            raise ValueError("volatility availability differs from its sample denominator")
        spread_duration = _exact_int(
            self.quote_observed_duration_us, "quote_observed_duration_us"
        )
        depth_duration = _exact_int(
            self.depth_observed_duration_us, "depth_observed_duration_us"
        )
        duration = end - start
        if spread_duration > duration or depth_duration > duration:
            raise ValueError("period quote/depth observation exceeds its duration")
        _metric(
            self.time_weighted_spread_ticks_fixed,
            "time_weighted_spread_ticks_fixed",
        )
        _metric(
            self.time_weighted_n_level_depth_shares_fixed,
            "time_weighted_n_level_depth_shares_fixed",
        )
        if (spread_duration == 0) != (
            self.time_weighted_spread_ticks_fixed == UNAVAILABLE
        ):
            raise ValueError("spread availability differs from observed duration")
        if (depth_duration == 0) != (
            self.time_weighted_n_level_depth_shares_fixed == UNAVAILABLE
        ):
            raise ValueError("depth availability differs from observed duration")

    def as_dict(self) -> dict[str, object]:
        return {
            "close_price_ticks": self.close_price_ticks,
            "depth_observed_duration_us": self.depth_observed_duration_us,
            "end_time_us": self.end_time_us,
            "high_price_ticks": self.high_price_ticks,
            "low_price_ticks": self.low_price_ticks,
            "open_price_ticks": self.open_price_ticks,
            "period_id": self.period_id,
            "quote_observed_duration_us": self.quote_observed_duration_us,
            "schema_version": self.schema_version,
            "start_time_us": self.start_time_us,
            "time_weighted_n_level_depth_shares_fixed": (
                self.time_weighted_n_level_depth_shares_fixed
            ),
            "time_weighted_spread_ticks_fixed": (
                self.time_weighted_spread_ticks_fixed
            ),
            "trade_count": self.trade_count,
            "volatility_difference_count": self.volatility_difference_count,
            "volatility_ticks_fixed": self.volatility_ticks_fixed,
            "volume_shares": self.volume_shares,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PeriodSummaryV1:
        expected = {
            "close_price_ticks",
            "depth_observed_duration_us",
            "end_time_us",
            "high_price_ticks",
            "low_price_ticks",
            "open_price_ticks",
            "period_id",
            "quote_observed_duration_us",
            "schema_version",
            "start_time_us",
            "time_weighted_n_level_depth_shares_fixed",
            "time_weighted_spread_ticks_fixed",
            "trade_count",
            "volatility_difference_count",
            "volatility_ticks_fixed",
            "volume_shares",
        }
        if set(payload) != expected:
            raise ValueError("period summary fields differ from schema v1")
        return cls(**{key: payload[key] for key in expected})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class DaySummaryV1:
    schema_version: int
    formula_set_id: str
    formula_contract: Mapping[str, object]
    semantic_plan_sha256: str
    day: PeriodSummaryV1
    phases: tuple[PeriodSummaryV1, ...]
    day_state_occupancy_us: Mapping[str, int]
    local_state_occupancy_us: Mapping[str, int]
    session_state_occupancy_us: Mapping[str, int]
    halt_count: int
    auction_uncross_count: int
    participant_decision_count: int
    liquidity_withdrawal_count: int
    maximum_absolute_price_move_ticks: int | str
    maximum_absolute_price_move_global_sequence: int | str
    invariant_status: str
    replay_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != DAY_SUMMARY_SCHEMA_VERSION:
            raise ValueError("day summary schema version must be 1")
        if self.formula_set_id != DAY_SUMMARY_FORMULA_SET_ID:
            raise ValueError("day summary formula set is unsupported")
        validate_strict_json(self.formula_contract)
        if (
            type(self.semantic_plan_sha256) is not str
            or _SHA256.fullmatch(self.semantic_plan_sha256) is None
        ):
            raise ValueError("day summary requires a semantic plan SHA-256")
        if type(self.day) is not PeriodSummaryV1:
            raise TypeError("day summary requires one PeriodSummaryV1")
        if type(self.phases) is not tuple or not self.phases or any(
            type(item) is not PeriodSummaryV1 for item in self.phases
        ):
            raise TypeError("day summary phases must be PeriodSummaryV1 values")
        if len({item.period_id for item in self.phases}) != len(self.phases):
            raise ValueError("day summary phase IDs must be unique")
        if (
            self.phases[0].start_time_us != self.day.start_time_us
            or self.phases[-1].end_time_us != self.day.end_time_us
            or any(
                left.end_time_us != right.start_time_us
                for left, right in zip(self.phases, self.phases[1:])
            )
        ):
            raise ValueError("day summary phases must contiguously partition the day")
        duration = self.day.end_time_us - self.day.start_time_us
        for name, occupancy in (
            ("day", self.day_state_occupancy_us),
            ("local", self.local_state_occupancy_us),
            ("session", self.session_state_occupancy_us),
        ):
            if not isinstance(occupancy, Mapping) or any(
                type(key) is not str or not key or type(value) is not int or value < 0
                for key, value in occupancy.items()
            ):
                raise ValueError(f"{name} state occupancy is invalid")
            if sum(occupancy.values()) != duration:
                raise ValueError(f"{name} state occupancy does not conserve day duration")
        for value, field in (
            (self.halt_count, "halt_count"),
            (self.auction_uncross_count, "auction_uncross_count"),
            (self.participant_decision_count, "participant_decision_count"),
            (self.liquidity_withdrawal_count, "liquidity_withdrawal_count"),
        ):
            _exact_int(value, field)
        move = _metric(
            self.maximum_absolute_price_move_ticks,
            "maximum_absolute_price_move_ticks",
        )
        sequence = _metric(
            self.maximum_absolute_price_move_global_sequence,
            "maximum_absolute_price_move_global_sequence",
        )
        if (move == UNAVAILABLE) != (sequence == UNAVAILABLE):
            raise ValueError("absolute move magnitude and tie-break sequence disagree")
        if sequence != UNAVAILABLE and sequence < 1:
            raise ValueError("absolute move tie-break sequence must be positive")
        if self.invariant_status != "PASS":
            raise ValueError("persisted full-day summaries require passing invariants")
        if (
            type(self.replay_digest) is not str
            or _SHA256.fullmatch(self.replay_digest) is None
        ):
            raise ValueError("day summary replay digest is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "auction_uncross_count": self.auction_uncross_count,
            "day": self.day.as_dict(),
            "day_state_occupancy_us": dict(sorted(self.day_state_occupancy_us.items())),
            "formula_contract": dict(self.formula_contract),
            "formula_set_id": self.formula_set_id,
            "halt_count": self.halt_count,
            "invariant_status": self.invariant_status,
            "liquidity_withdrawal_count": self.liquidity_withdrawal_count,
            "local_state_occupancy_us": dict(sorted(self.local_state_occupancy_us.items())),
            "maximum_absolute_price_move_global_sequence": (
                self.maximum_absolute_price_move_global_sequence
            ),
            "maximum_absolute_price_move_ticks": self.maximum_absolute_price_move_ticks,
            "participant_decision_count": self.participant_decision_count,
            "phases": [item.as_dict() for item in self.phases],
            "replay_digest": self.replay_digest,
            "schema_version": self.schema_version,
            "semantic_plan_sha256": self.semantic_plan_sha256,
            "session_state_occupancy_us": dict(
                sorted(self.session_state_occupancy_us.items())
            ),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def semantic_sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DaySummaryV1:
        expected = {
            "auction_uncross_count",
            "day",
            "day_state_occupancy_us",
            "formula_contract",
            "formula_set_id",
            "halt_count",
            "invariant_status",
            "liquidity_withdrawal_count",
            "local_state_occupancy_us",
            "maximum_absolute_price_move_global_sequence",
            "maximum_absolute_price_move_ticks",
            "participant_decision_count",
            "phases",
            "replay_digest",
            "schema_version",
            "semantic_plan_sha256",
            "session_state_occupancy_us",
        }
        if set(payload) != expected:
            raise ValueError("day summary fields differ from schema v1")
        raw_day = payload["day"]
        raw_phases = payload["phases"]
        if not isinstance(raw_day, Mapping) or type(raw_phases) is not list or any(
            not isinstance(item, Mapping) for item in raw_phases
        ):
            raise TypeError("day summary period payloads are invalid")
        mappings = (
            payload["formula_contract"],
            payload["day_state_occupancy_us"],
            payload["local_state_occupancy_us"],
            payload["session_state_occupancy_us"],
        )
        if any(not isinstance(item, Mapping) for item in mappings):
            raise TypeError("day summary mapping payloads are invalid")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            formula_set_id=str(payload["formula_set_id"]),
            formula_contract=dict(payload["formula_contract"]),  # type: ignore[arg-type]
            semantic_plan_sha256=str(payload["semantic_plan_sha256"]),
            day=PeriodSummaryV1.from_dict(raw_day),
            phases=tuple(PeriodSummaryV1.from_dict(item) for item in raw_phases),
            day_state_occupancy_us={
                str(key): _exact_int(value, f"day occupancy {key}")
                for key, value in payload["day_state_occupancy_us"].items()  # type: ignore[union-attr]
            },
            local_state_occupancy_us={
                str(key): _exact_int(value, f"local occupancy {key}")
                for key, value in payload["local_state_occupancy_us"].items()  # type: ignore[union-attr]
            },
            session_state_occupancy_us={
                str(key): _exact_int(value, f"session occupancy {key}")
                for key, value in payload["session_state_occupancy_us"].items()  # type: ignore[union-attr]
            },
            halt_count=_exact_int(payload["halt_count"], "halt_count"),
            auction_uncross_count=_exact_int(
                payload["auction_uncross_count"], "auction_uncross_count"
            ),
            participant_decision_count=_exact_int(
                payload["participant_decision_count"], "participant_decision_count"
            ),
            liquidity_withdrawal_count=_exact_int(
                payload["liquidity_withdrawal_count"], "liquidity_withdrawal_count"
            ),
            maximum_absolute_price_move_ticks=_metric(
                payload["maximum_absolute_price_move_ticks"],
                "maximum_absolute_price_move_ticks",
            ),
            maximum_absolute_price_move_global_sequence=_metric(
                payload["maximum_absolute_price_move_global_sequence"],
                "maximum_absolute_price_move_global_sequence",
            ),
            invariant_status=str(payload["invariant_status"]),
            replay_digest=str(payload["replay_digest"]),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> DaySummaryV1:
        return cls.from_dict(parse_canonical_json_object(raw))


def _native_rows_in_global_order(
    events: Sequence[FullDayEventV1],
    native_entries: Sequence[NativeLedgerEntryV1],
) -> tuple[tuple[FullDayEventV1, NativeLedgerEntryV1], ...]:
    by_key: dict[tuple[str, str, str], NativeLedgerEntryV1] = {}
    for entry in native_entries:
        if type(entry) is not NativeLedgerEntryV1 or entry.ledger_key in by_key:
            raise ValueError("native ledger entries must be unique typed rows")
        by_key[entry.ledger_key] = entry
    rows: list[tuple[FullDayEventV1, NativeLedgerEntryV1]] = []
    used: set[tuple[str, str, str]] = set()
    for event in events:
        reference = event.payload.native_event
        if reference is None:
            continue
        entry = by_key.get(reference.ledger_key)
        if entry is None or entry.reference != reference:
            raise ValueError("outer event references an absent or different native row")
        rows.append((event, entry))
        used.add(reference.ledger_key)
    if used != set(by_key):
        raise ValueError("native ledger contains rows absent from the outer event stream")
    return tuple(rows)


def _trade_rows(
    native_rows: Sequence[tuple[FullDayEventV1, NativeLedgerEntryV1]],
) -> tuple[dict[str, int], ...]:
    result: list[dict[str, int]] = []
    for outer, entry in native_rows:
        if entry.reference.owner_component_id != "ENGINE_MARKET_MECHANICS_V1":
            continue
        if entry.reference.event_type not in {"TRADE", "AUCTION_FILL"}:
            continue
        payload = entry.payload
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise TypeError("mechanics trade native payload lacks data")
        result.append(
            {
                "global_event_sequence": outer.global_event_sequence,
                "price_ticks": _exact_int(data.get("price_ticks"), "trade price", minimum=1),
                "quantity": _exact_int(data.get("quantity"), "trade quantity", minimum=1),
                "simulation_time_us": outer.simulation_time_us,
            }
        )
    return tuple(result)


def _quote_observations(
    native_rows: Sequence[tuple[FullDayEventV1, NativeLedgerEntryV1]],
) -> tuple[dict[str, object], ...]:
    by_time: dict[int, dict[str, object]] = {}
    for outer, entry in native_rows:
        if (
            entry.reference.owner_component_id != "DELIVERY_ASYNC_V1"
            or entry.reference.event_type != "CLIENT_MESSAGE_DELIVERED"
        ):
            continue
        payload = entry.payload
        if payload.get("kind") != "MARKET_STATE":
            continue
        client = payload.get("client_payload")
        market = None if not isinstance(client, Mapping) else client.get("market_state")
        if not isinstance(market, Mapping):
            raise TypeError("delivered market state payload is malformed")
        source_time = _exact_int(payload.get("source_time_us"), "quote source time")
        if market.get("simulation_time_us") != source_time:
            raise ValueError("delivered market state time differs from its source cut")
        by_time[source_time] = {
            "ask_levels": market.get("ask_levels"),
            "best_ask_ticks": market.get("best_ask_ticks"),
            "best_bid_ticks": market.get("best_bid_ticks"),
            "bid_levels": market.get("bid_levels"),
            "global_event_sequence": outer.global_event_sequence,
            "time_us": source_time,
        }
    return tuple(by_time[time_us] for time_us in sorted(by_time))


def _level_depth(levels: object) -> int | None:
    if type(levels) is not list and type(levels) is not tuple:
        return None
    total = 0
    for row in levels[:DEPTH_LEVEL_COUNT]:
        if not isinstance(row, Mapping):
            return None
        total += _exact_int(row.get("quantity"), "quote level quantity")
    return total


def _period_summary(
    period_id: str,
    start_time_us: int,
    end_time_us: int,
    *,
    trades: Sequence[Mapping[str, int]],
    quotes: Sequence[Mapping[str, object]],
    horizon_end_us: int,
) -> PeriodSummaryV1:
    selected = tuple(
        row
        for row in trades
        if start_time_us <= row["simulation_time_us"] < end_time_us
    )
    prices = tuple(row["price_ticks"] for row in selected)
    if prices:
        open_price: int | str = prices[0]
        high_price: int | str = max(prices)
        low_price: int | str = min(prices)
        close_price: int | str = prices[-1]
    else:
        open_price = high_price = low_price = close_price = UNAVAILABLE
    differences = tuple(
        current - previous for previous, current in zip(prices, prices[1:])
    )
    if differences:
        mean_square_scaled = _round_half_up(
            sum(value * value for value in differences) * FIXED_POINT_SCALE**2,
            len(differences),
        )
        volatility: int | str = isqrt(mean_square_scaled)
    else:
        volatility = UNAVAILABLE

    spread_numerator = 0
    spread_denominator = 0
    depth_numerator = 0
    depth_denominator = 0
    for index, quote in enumerate(quotes):
        quote_start = int(quote["time_us"])
        quote_end = (
            int(quotes[index + 1]["time_us"])
            if index + 1 < len(quotes)
            else horizon_end_us
        )
        overlap_start = max(start_time_us, quote_start)
        overlap_end = min(end_time_us, quote_end)
        duration = overlap_end - overlap_start
        if duration <= 0:
            continue
        bid = quote["best_bid_ticks"]
        ask = quote["best_ask_ticks"]
        if type(bid) is int and type(ask) is int and ask >= bid:
            spread_numerator += (ask - bid) * duration
            spread_denominator += duration
        bid_depth = _level_depth(quote["bid_levels"])
        ask_depth = _level_depth(quote["ask_levels"])
        if bid_depth is not None and ask_depth is not None:
            depth_numerator += (bid_depth + ask_depth) * duration
            depth_denominator += duration

    spread: int | str = (
        UNAVAILABLE
        if spread_denominator == 0
        else _round_half_up(
            spread_numerator * FIXED_POINT_SCALE, spread_denominator
        )
    )
    depth: int | str = (
        UNAVAILABLE
        if depth_denominator == 0
        else _round_half_up(
            depth_numerator * FIXED_POINT_SCALE, depth_denominator
        )
    )
    return PeriodSummaryV1(
        schema_version=1,
        period_id=period_id,
        start_time_us=start_time_us,
        end_time_us=end_time_us,
        open_price_ticks=open_price,
        high_price_ticks=high_price,
        low_price_ticks=low_price,
        close_price_ticks=close_price,
        volume_shares=sum(row["quantity"] for row in selected),
        trade_count=len(selected),
        volatility_ticks_fixed=volatility,
        volatility_difference_count=len(differences),
        quote_observed_duration_us=spread_denominator,
        time_weighted_spread_ticks_fixed=spread,
        depth_observed_duration_us=depth_denominator,
        time_weighted_n_level_depth_shares_fixed=depth,
    )


def _occupancy(
    initial_state: str,
    changes: Sequence[tuple[int, int, str]],
    end_time_us: int,
) -> dict[str, int]:
    current = initial_state
    cursor = 0
    totals: dict[str, int] = {current: 0}
    for time_us, _sequence, new_state in sorted(changes):
        if time_us < cursor or time_us > end_time_us:
            raise ValueError("state occupancy change lies outside the day")
        totals[current] = totals.get(current, 0) + time_us - cursor
        current = new_state
        totals.setdefault(current, 0)
        cursor = time_us
    totals[current] = totals.get(current, 0) + end_time_us - cursor
    return dict(sorted(totals.items()))


def _maximum_absolute_move(
    trades: Sequence[Mapping[str, int]],
) -> tuple[int | str, int | str]:
    """Return magnitude and earliest global sequence under the V1 tie rule."""

    moves = tuple(
        (
            abs(current["price_ticks"] - previous["price_ticks"]),
            current["global_event_sequence"],
        )
        for previous, current in zip(trades, trades[1:])
    )
    if not moves:
        return UNAVAILABLE, UNAVAILABLE
    maximum = max(value for value, _sequence in moves)
    return maximum, next(sequence for value, sequence in moves if value == maximum)


def summarize_full_day(
    plan: FullDayPlanV1,
    events: Sequence[FullDayEventV1],
    native_entries: Sequence[NativeLedgerEntryV1],
) -> DaySummaryV1:
    """Fold one verified ledger into exact V1 day/phase metrics."""

    if type(plan) is not FullDayPlanV1:
        raise TypeError("full-day summary requires FullDayPlanV1")
    event_rows = tuple(events)
    if any(type(event) is not FullDayEventV1 for event in event_rows):
        raise TypeError("full-day summary requires FullDayEventV1 rows")
    native_rows = _native_rows_in_global_order(event_rows, tuple(native_entries))
    trades = _trade_rows(native_rows)
    quotes = _quote_observations(native_rows)
    end_time_us = plan.calendar.end_time_us
    day = _period_summary(
        "DAY",
        0,
        end_time_us,
        trades=trades,
        quotes=quotes,
        horizon_end_us=end_time_us,
    )
    phases = tuple(
        _period_summary(
            phase.phase_id,
            phase.start.simulation_time_us,
            phase.end.simulation_time_us,
            trades=trades,
            quotes=quotes,
            horizon_end_us=end_time_us,
        )
        for phase in plan.calendar.phases
    )

    day_changes: list[tuple[int, int, str]] = []
    local_changes: list[tuple[int, int, str]] = []
    session_changes: list[tuple[int, int, str]] = []
    for event in event_rows:
        data = event.payload.data
        if event.event_type is FullDayEventTypeV1.DAY_STATE_ANCHOR_RESET:
            day_changes.append(
                (event.simulation_time_us, event.global_event_sequence, str(data["anchored_state"]))
            )
        elif event.event_type is FullDayEventTypeV1.DAY_STATE_TRANSITION:
            day_changes.append(
                (event.simulation_time_us, event.global_event_sequence, str(data["new_state"]))
            )
        elif event.event_type is FullDayEventTypeV1.LOCAL_STATE_TRANSITION:
            local_changes.append(
                (event.simulation_time_us, event.global_event_sequence, str(data["new_state"]))
            )
    for outer, entry in native_rows:
        if (
            entry.reference.owner_component_id == "ENGINE_MARKET_MECHANICS_V1"
            and entry.reference.event_type == "SESSION_STATE_CHANGED"
        ):
            data = entry.payload.get("data")
            if not isinstance(data, Mapping):
                raise ValueError("mechanics session-state transition payload is malformed")
            session_changes.append(
                (
                    outer.simulation_time_us,
                    outer.global_event_sequence,
                    str(data["current_state"]),
                )
            )

    mechanics_types = tuple(
        entry.reference.event_type
        for _outer, entry in native_rows
        if entry.reference.owner_component_id == "ENGINE_MARKET_MECHANICS_V1"
    )
    participant_kind = {
        item.participant_id: item.participant_kind.value
        for item in plan.participant_definitions
    }
    liquidity_withdrawals = sum(
        event.event_type is FullDayEventTypeV1.PARTICIPANT_DEACTIVATED
        and participant_kind.get(str(event.payload.data["participant_id"]))
        == "LIQUIDITY_PROVIDER"
        for event in event_rows
    )
    maximum_move_value, maximum_sequence = _maximum_absolute_move(trades)

    replay_digest = canonical_sha256(
        {
            "native_ledger": [entry.as_dict() for _event, entry in native_rows],
            "outer_event_ledger": [event.as_dict() for event in event_rows],
            "semantic_plan_sha256": plan.semantic_sha256,
        }
    )
    formula_contract = {
        "absolute_price_move": {
            "formula": "ABS_CONSECUTIVE_EXECUTED_TRADE_TICK_DIFFERENCE",
            "tie_break": "EARLIEST_GLOBAL_EVENT_SEQUENCE",
        },
        "depth": {
            "formula": "SUM_BID_AND_ASK_QUANTITY_FIRST_N_LEVELS",
            "interval": "HALF_OPEN_SOURCE_TIME",
            "level_count": DEPTH_LEVEL_COUNT,
            "rounding": "HALF_UP",
            "same_source_time_tie_break": "LATEST_GLOBAL_EVENT_SEQUENCE",
            "scale": FIXED_POINT_SCALE,
        },
        "event_counts": {
            "auction_uncross": "MECHANICS_NATIVE_EVENT_TYPE_AUCTION_UNCROSS",
            "halt": "MECHANICS_NATIVE_EVENT_TYPE_HALT",
            "liquidity_withdrawal": (
                "OUTER_PARTICIPANT_DEACTIVATED_WITH_PLAN_KIND_LIQUIDITY_PROVIDER"
            ),
            "participation": "OUTER_EVENT_TYPE_PARTICIPANT_DECISION",
        },
        "ohlc": {
            "formula": "FIRST_MAX_MIN_LAST_EXECUTED_TRADE_IN_GLOBAL_ORDER",
            "missing": UNAVAILABLE,
        },
        "spread": {
            "formula": "ASK_TICKS_MINUS_BID_TICKS",
            "interval": "HALF_OPEN_SOURCE_TIME",
            "rounding": "HALF_UP",
            "same_source_time_tie_break": "LATEST_GLOBAL_EVENT_SEQUENCE",
            "scale": FIXED_POINT_SCALE,
        },
        "state_occupancy": {
            "formula": "HALF_OPEN_INTEGER_MICROSECOND_INTERVALS",
            "ordering": "SIMULATION_TIME_THEN_GLOBAL_EVENT_SEQUENCE",
            "session_source": "MECHANICS_NATIVE_SESSION_STATE_CHANGED",
        },
        "trades": {
            "eligibility": "MECHANICS_NATIVE_TRADE_OR_AUCTION_FILL",
            "order": "OUTER_GLOBAL_EVENT_SEQUENCE",
            "trade_count": "COUNT_ELIGIBLE_EXECUTIONS",
            "volume": "SUM_ELIGIBLE_EXECUTED_QUANTITY_SHARES",
        },
        "volatility": {
            "denominator": "CONSECUTIVE_DIFFERENCE_COUNT",
            "formula": "RMS_CONSECUTIVE_EXECUTED_TRADE_TICK_DIFFERENCE",
            "rounding": "HALF_UP_MEAN_SQUARE_THEN_INTEGER_SQRT_FLOOR",
            "scale": FIXED_POINT_SCALE,
        },
    }
    return DaySummaryV1(
        schema_version=1,
        formula_set_id=DAY_SUMMARY_FORMULA_SET_ID,
        formula_contract=formula_contract,
        semantic_plan_sha256=plan.semantic_sha256,
        day=day,
        phases=phases,
        day_state_occupancy_us=_occupancy(
            plan.state_model.initial_day_state.value,
            day_changes,
            end_time_us,
        ),
        local_state_occupancy_us=_occupancy(
            plan.state_model.initial_local_state.value,
            local_changes,
            end_time_us,
        ),
        session_state_occupancy_us=_occupancy(
            "CLOSED",
            session_changes,
            end_time_us,
        ),
        halt_count=mechanics_types.count("HALT"),
        auction_uncross_count=mechanics_types.count("AUCTION_UNCROSS"),
        participant_decision_count=sum(
            event.event_type is FullDayEventTypeV1.PARTICIPANT_DECISION
            for event in event_rows
        ),
        liquidity_withdrawal_count=liquidity_withdrawals,
        maximum_absolute_price_move_ticks=maximum_move_value,
        maximum_absolute_price_move_global_sequence=maximum_sequence,
        invariant_status="PASS",
        replay_digest=replay_digest,
    )


__all__ = [
    "DAY_SUMMARY_FORMULA_SET_ID",
    "DAY_SUMMARY_SCHEMA_VERSION",
    "DEPTH_LEVEL_COUNT",
    "DaySummaryV1",
    "FIXED_POINT_SCALE",
    "PERIOD_SUMMARY_SCHEMA_VERSION",
    "PeriodSummaryV1",
    "UNAVAILABLE",
    "summarize_full_day",
]
