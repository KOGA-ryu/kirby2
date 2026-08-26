"""Load the deliberately small local historical-mode fixture set."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .models import (
    ExactOrderMessage,
    ExactReplayFixture,
    ExpectedTrade,
    HistoricalConstraints,
    HistoricalDataMode,
    HistoricalProvenance,
    ReconstructionFixture,
    SpreadObservation,
    TradePrintObservation,
)


FIXTURE_DIRECTORY = Path(__file__).with_name("fixtures")


def _provenance(payload: dict[str, object]) -> HistoricalProvenance:
    return HistoricalProvenance(
        dataset_id=str(payload["dataset_id"]),
        source_name=str(payload["source_name"]),
        source_locator=str(payload["source_locator"]),
        description=str(payload["description"]),
        license_note=str(payload["license_note"]),
        real_market_data=_boolean(payload, "real_market_data"),
        provides_order_events=_boolean(payload, "provides_order_events"),
        provides_trade_events=_boolean(payload, "provides_trade_events"),
        provides_book_events=_boolean(payload, "provides_book_events"),
    )


def load_exact_fixture(path: Path | None = None) -> ExactReplayFixture:
    actual_path = path or FIXTURE_DIRECTORY / "exact_demo.json"
    payload = _payload(actual_path, HistoricalDataMode.EXACT_REPLAY)
    provenance = payload["provenance"]
    messages = payload["messages"]
    expected_trades = payload["expected_trades"]
    if not isinstance(provenance, dict):
        raise ValueError("exact fixture provenance must be an object")
    if not isinstance(messages, list) or any(
        not isinstance(message, dict) for message in messages
    ):
        raise ValueError("exact fixture messages must be objects")
    if not isinstance(expected_trades, list) or any(
        not isinstance(trade, dict) for trade in expected_trades
    ):
        raise ValueError("exact fixture expected trades must be objects")
    return ExactReplayFixture(
        fixture_id=str(payload["fixture_id"]),
        label=str(payload["label"]),
        duration_us=int(payload["duration_us"]),
        tick_size=Decimal(str(payload["tick_size"])),
        session_start=str(payload["session_start"]),
        provenance=_provenance(provenance),
        messages=tuple(
            ExactOrderMessage(
                sequence=int(message["sequence"]),
                timestamp_us=int(message["timestamp_us"]),
                action=str(message["action"]),
                order_id=str(message["order_id"]),
                side=None if message.get("side") is None else str(message["side"]),
                quantity=int(message.get("quantity", 0)),
                price_ticks=(
                    None
                    if message.get("price_ticks") is None
                    else int(message["price_ticks"])
                ),
                target_order_id=(
                    None
                    if message.get("target_order_id") is None
                    else str(message["target_order_id"])
                ),
            )
            for message in messages
        ),
        expected_trades=tuple(
            ExpectedTrade(
                trade_id=str(trade["trade_id"]),
                price_ticks=int(trade["price_ticks"]),
                quantity=int(trade["quantity"]),
                maker_order_id=str(trade["maker_order_id"]),
                taker_order_id=str(trade["taker_order_id"]),
                taker_side=str(trade["taker_side"]),
            )
            for trade in expected_trades
        ),
    )


def load_reconstruction_fixture(path: Path | None = None) -> ReconstructionFixture:
    actual_path = path or FIXTURE_DIRECTORY / "reconstruction_demo.json"
    payload = _payload(actual_path, HistoricalDataMode.RECONSTRUCTION)
    provenance = payload["provenance"]
    constraints = payload["constraints"]
    if not isinstance(provenance, dict) or not isinstance(constraints, dict):
        raise ValueError("reconstruction provenance and constraints must be objects")
    spreads = constraints.get("spread_observations")
    prints = constraints.get("trade_prints")
    known_events = constraints.get("known_market_events")
    if not isinstance(spreads, list) or any(not isinstance(item, dict) for item in spreads):
        raise ValueError("spread observations must be objects")
    if not isinstance(prints, list) or any(not isinstance(item, dict) for item in prints):
        raise ValueError("trade print observations must be objects")
    if not isinstance(known_events, list):
        raise ValueError("known market events must be an array")
    return ReconstructionFixture(
        fixture_id=str(payload["fixture_id"]),
        label=str(payload["label"]),
        seed=int(payload["seed"]),
        provenance=_provenance(provenance),
        constraints=HistoricalConstraints(
            session_start=str(constraints["session_start"]),
            duration_us=int(constraints["duration_us"]),
            tick_size=Decimal(str(constraints["tick_size"])),
            open_ticks=int(constraints["open_ticks"]),
            high_ticks=int(constraints["high_ticks"]),
            low_ticks=int(constraints["low_ticks"]),
            close_ticks=int(constraints["close_ticks"]),
            aggregate_volume=int(constraints["aggregate_volume"]),
            realized_volatility_bps=Decimal(
                str(constraints["realized_volatility_bps"])
            ),
            spread_observations=tuple(
                SpreadObservation(
                    timestamp_us=int(item["timestamp_us"]),
                    spread_ticks=int(item["spread_ticks"]),
                )
                for item in spreads
            ),
            trade_prints=tuple(
                TradePrintObservation(
                    timestamp_us=int(item["timestamp_us"]),
                    price_ticks=int(item["price_ticks"]),
                    quantity=int(item["quantity"]),
                )
                for item in prints
            ),
            known_market_events=tuple(str(value) for value in known_events),
        ),
    )


def load_historical_fixtures() -> dict[str, ExactReplayFixture | ReconstructionFixture]:
    fixtures = (load_exact_fixture(), load_reconstruction_fixture())
    by_id = {fixture.fixture_id: fixture for fixture in fixtures}
    if len(by_id) != 2:
        raise RuntimeError("historical fixture IDs must be unique")
    return by_id


def _payload(path: Path, expected_mode: HistoricalDataMode) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("historical fixture must contain an object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported historical fixture schema")
    if HistoricalDataMode.parse(str(payload.get("mode"))) is not expected_mode:
        raise ValueError("historical fixture mode does not match loader")
    return payload


def _boolean(payload: dict[str, object], key: str) -> bool:
    value = payload[key]
    if type(value) is not bool:
        raise ValueError(f"historical provenance {key} must be a JSON boolean")
    return value
