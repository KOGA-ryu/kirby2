"""Read local source formats without assigning replay guarantees."""

from __future__ import annotations

import csv
import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .models import SourceCapability


ADAPTER_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RawDataset:
    adapter: str
    source_locator: str
    source_digest: str
    source_name: str
    license_note: str
    real_market_data: bool
    capability: SourceCapability
    tick_size: Decimal
    source_timezone: str
    expected_snapshot_interval_ns: int | None
    rows: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.adapter,
                self.source_locator,
                self.source_digest,
                self.source_name,
                self.license_note,
                self.source_timezone,
            )
        ):
            raise ValueError("raw dataset source declaration is incomplete")
        if type(self.real_market_data) is not bool:
            raise TypeError("raw dataset real-market-data flag must be boolean")
        if not self.tick_size.is_finite() or self.tick_size <= 0:
            raise ValueError("raw dataset tick size must be positive and finite")


class MarketDataAdapter(Protocol):
    name: str

    def read(self, source: Path) -> RawDataset: ...


class NormalizedCsvAdapter:
    name = "normalized-csv-v1"

    def read(self, source: Path) -> RawDataset:
        metadata = _sidecar(source)
        with source.open("r", encoding="utf-8", newline="") as stream:
            rows = tuple(
                {
                    key: value
                    for key, value in row.items()
                    if key is not None and value not in {None, ""}
                }
                for row in csv.DictReader(stream)
            )
        return _raw_dataset(self.name, source, metadata, rows)


class NormalizedParquetAdapter:
    name = "normalized-parquet-v1"

    def read(self, source: Path) -> RawDataset:
        metadata = _sidecar(source)
        duckdb = _duckdb()
        connection = duckdb.connect(":memory:")
        try:
            cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(source)])
            columns = tuple(item[0] for item in cursor.description)
            rows = tuple(
                {
                    key: value
                    for key, value in zip(columns, row, strict=True)
                    if value is not None
                }
                for row in cursor.fetchall()
            )
        finally:
            connection.close()
        return _raw_dataset(self.name, source, metadata, rows)


class KirbyMessageFixtureAdapter:
    """Adapt Kirby2's already-local representative order-message fixture."""

    name = "kirby-message-fixture-v1"

    def read(self, source: Path) -> RawDataset:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("mode") != "EXACT_REPLAY":
            raise ValueError("Kirby message adapter requires an exact-event fixture")
        provenance = payload.get("provenance")
        messages = payload.get("messages")
        if not isinstance(provenance, dict) or not isinstance(messages, list):
            raise ValueError("Kirby message fixture structure is invalid")
        if not bool(provenance.get("provides_order_events")):
            raise ValueError("fixture does not declare source order messages")
        real_market_data = provenance.get("real_market_data")
        if type(real_market_data) is not bool:
            raise ValueError("fixture real-market-data declaration must be boolean")
        session_start = _parse_utc(str(payload["session_start"]))
        duration_us = int(payload["duration_us"])
        session_close = session_start + timedelta(microseconds=duration_us)
        symbol = str(payload.get("symbol", "K2FIX"))
        session_id = str(payload.get("fixture_id", "local-message-session"))
        rows: list[dict[str, object]] = [
            {
                "currency": "USD",
                "instrument_type": "PEDAGOGICAL_FIXTURE",
                "record_type": "SYMBOL_METADATA",
                "session_id": session_id,
                "source_timestamp": _iso_z(session_start),
                "symbol": symbol,
                "timestamp_precision": "MICROSECOND",
            },
            {
                "record_type": "SESSION_METADATA",
                "session_close_ns": _epoch_ns(session_close),
                "session_id": session_id,
                "session_open_ns": _epoch_ns(session_start),
                "source_timestamp": _iso_z(session_start),
                "symbol": symbol,
                "timestamp_precision": "MICROSECOND",
            },
        ]
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Kirby message fixture contains a non-object message")
            timestamp = session_start + timedelta(
                microseconds=int(message["timestamp_us"])
            )
            action = str(message["action"]).upper()
            row: dict[str, object] = {
                "order_action": action,
                "order_id": str(message["order_id"]),
                "record_type": "ORDER_EVENT",
                "session_id": session_id,
                "source_sequence": int(message["sequence"]),
                "source_timestamp": _iso_z(timestamp),
                "symbol": symbol,
                "timestamp_precision": "MICROSECOND",
            }
            for key in (
                "price_ticks",
                "quantity",
                "side",
                "target_order_id",
            ):
                if message.get(key) is not None:
                    row[key] = message[key]
            rows.append(row)
        return RawDataset(
            adapter=self.name,
            source_locator=str(source.resolve()),
            source_digest=_file_sha256(source),
            source_name=str(provenance["source_name"]),
            license_note=str(provenance["license_note"]),
            real_market_data=real_market_data,
            capability=SourceCapability.MARKET_BY_ORDER,
            tick_size=Decimal(str(payload["tick_size"])),
            source_timezone="UTC",
            expected_snapshot_interval_ns=None,
            rows=tuple(rows),
        )


ADAPTERS: dict[str, MarketDataAdapter] = {
    "csv": NormalizedCsvAdapter(),
    "parquet": NormalizedParquetAdapter(),
    "kirby-mbo": KirbyMessageFixtureAdapter(),
}


def load_raw_dataset(adapter_name: str, source: Path) -> RawDataset:
    try:
        adapter = ADAPTERS[adapter_name]
    except KeyError as error:
        raise ValueError(f"unknown market-data adapter: {adapter_name}") from error
    if not source.is_file():
        raise ValueError(f"market-data source does not exist: {source}")
    return adapter.read(source)


def _sidecar(source: Path) -> dict[str, Any]:
    path = source.with_suffix(".toml")
    if not path.is_file():
        raise ValueError(f"normalized fixture requires metadata sidecar: {path}")
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    if payload.get("schema_version") != ADAPTER_SCHEMA_VERSION:
        raise ValueError("unsupported normalized fixture sidecar schema")
    required = {
        "capability",
        "license_note",
        "real_market_data",
        "schema_version",
        "source_name",
        "source_timezone",
        "tick_size",
    }
    allowed = required | {"expected_snapshot_interval_ns"}
    if not required <= set(payload) or set(payload) - allowed:
        raise ValueError("normalized fixture sidecar fields are incomplete or unknown")
    if type(payload["real_market_data"]) is not bool:
        raise ValueError("sidecar real-market-data declaration must be boolean")
    return payload


def _raw_dataset(
    adapter: str,
    source: Path,
    metadata: dict[str, Any],
    rows: tuple[dict[str, object], ...],
) -> RawDataset:
    expected = metadata.get("expected_snapshot_interval_ns")
    return RawDataset(
        adapter=adapter,
        source_locator=str(source.resolve()),
        source_digest=_source_bundle_sha256(source, source.with_suffix(".toml")),
        source_name=str(metadata["source_name"]),
        license_note=str(metadata["license_note"]),
        real_market_data=metadata["real_market_data"],
        capability=SourceCapability(str(metadata["capability"])),
        tick_size=Decimal(str(metadata["tick_size"])),
        source_timezone=str(metadata["source_timezone"]),
        expected_snapshot_interval_ns=None if expected is None else int(expected),
        rows=rows,
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("fixture session start must include a timezone")
    return parsed.astimezone(timezone.utc)


def _epoch_ns(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - epoch
    return (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_bundle_sha256(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        name = path.name.encode("utf-8")
        content_digest = bytes.fromhex(_file_sha256(path))
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(content_digest)
    return digest.hexdigest()


def _duckdb():
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError(
            "DuckDB is required for Parquet market-data fixtures"
        ) from error
    return duckdb
