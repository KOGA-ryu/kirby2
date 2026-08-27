"""Runtime acceptance audit for market-data normalization and ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.marketdata import (
    CAPABILITY_RECORD_TYPES,
    MarketDataStore,
    RawDataset,
    RecordType,
    ReplayMode,
    SourceCapability,
    TimestampPrecision,
    normalize_raw_dataset,
    normalize_timestamp,
)
from kirby2.research import RunStore
from kirby2.research.toml_codec import load_toml


@dataclass(frozen=True, slots=True)
class MarketDataAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_market_data() -> tuple[MarketDataAuditCase, ...]:
    return (
        _fixture_adapter_case(),
        _quality_detection_case(),
        _timestamp_precision_case(),
        _capability_boundary_case(),
        _immutable_provenance_case(),
    )


def _fixture_adapter_case() -> MarketDataAuditCase:
    failures: list[str] = []
    package = Path(__file__).resolve().parents[1]
    sources = (
        ("csv", package / "marketdata" / "fixtures" / "bars.csv"),
        (
            "parquet",
            package / "marketdata" / "fixtures" / "trades_quotes.parquet",
        ),
        (
            "kirby-mbo",
            package / "historical" / "fixtures" / "exact_demo.json",
        ),
    )
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = MarketDataStore(root)
        manifests = tuple(store.ingest(adapter, source) for adapter, source in sources)
        verifications = tuple(
            store.verify_dataset(item.dataset_id) for item in manifests
        )
        quality_reports = tuple(
            load_toml(
                store.dataset_directory(item.dataset_id) / "quality_report.toml"
            )
            for item in manifests
        )
        if not all(item.passed for item in verifications):
            failures.append("one or more local fixture imports failed verification")
        expected_capabilities = (
            SourceCapability.BARS_ONLY,
            SourceCapability.TRADES_AND_QUOTES,
            SourceCapability.MARKET_BY_ORDER,
        )
        if tuple(item.capability for item in manifests) != expected_capabilities:
            failures.append("fixture adapter changed declared capability")
        if manifests[0].exact_replay_allowed or manifests[1].exact_replay_allowed:
            failures.append("bar or quote fixture was mislabeled exact replay")
        if not manifests[2].exact_replay_allowed:
            failures.append("clean market-by-order fixture did not pass exact gate")
        accepted_counts = tuple(int(item["accepted_rows"]) for item in quality_reports)
        rejected_counts = tuple(int(item["rejected_rows"]) for item in quality_reports)
        if accepted_counts != (4, 6, 13) or rejected_counts != (0, 0, 0):
            failures.append("fixture adapter rejected or lost expected source rows")
        RunStore(root).refresh_catalog()
        import duckdb

        connection = duckdb.connect(str(root / "catalog.duckdb"), read_only=True)
        try:
            provenance_rows = int(
                connection.execute("SELECT COUNT(*) FROM dataset_provenance").fetchone()[0]
            )
        finally:
            connection.close()
        if provenance_rows != 3:
            failures.append("shared research ledger lost dataset provenance")
        evidence = {
            "capabilities": [item.capability.value for item in manifests],
            "accepted_rows": list(accepted_counts),
            "dataset_ids": [item.dataset_id for item in manifests],
            "exact_replay": [item.exact_replay_allowed for item in manifests],
            "provenance_ledger_rows": provenance_rows,
            "replay_modes": [item.replay_mode.value for item in manifests],
            "verification": [item.passed for item in verifications],
        }
    return MarketDataAuditCase("all_local_fixture_adapters", evidence, tuple(failures))


def _quality_detection_case() -> MarketDataAuditCase:
    raw = RawDataset(
        adapter="audit-source-v1",
        source_locator="memory://quality-defects",
        source_digest="0" * 64,
        source_name="Audit defect source",
        license_note="Local audit fixture",
        real_market_data=False,
        capability=SourceCapability.MARKET_BY_ORDER,
        tick_size=Decimal("0.01"),
        source_timezone="UTC",
        expected_snapshot_interval_ns=1_000_000_000,
        rows=_defect_rows(),
    )
    dataset = normalize_raw_dataset(raw)
    codes = {
        issue.code
        for issue in (*dataset.report.warnings, *dataset.report.rejections)
    }
    required = {
        "DUPLICATE_RECORD",
        "CROSSED_QUOTE",
        "OUT_OF_ORDER_RECORD",
        "MISSING_SEQUENCE",
        "INVALID_PRICE",
        "INVALID_QUANTITY",
        "TIMESTAMP_REVERSAL",
        "SESSION_BOUNDARY",
        "UNKNOWN_AGGRESSOR_SIDE",
        "SNAPSHOT_GAP",
    }
    failures: list[str] = []
    if not required <= codes:
        failures.append("required source-quality failures were not all detected")
    if dataset.report.repairs:
        failures.append("questionable source rows were silently repaired")
    if dataset.replay.exact_replay_allowed:
        failures.append("defective market-by-order source passed exact replay gate")
    if dataset.report.input_rows != (
        dataset.report.accepted_rows + dataset.report.rejected_rows
    ):
        failures.append("quality report row accounting did not reconcile")
    evidence = {
        "accepted_rows": dataset.report.accepted_rows,
        "detected_codes": sorted(codes),
        "exact_replay_allowed": dataset.replay.exact_replay_allowed,
        "input_rows": dataset.report.input_rows,
        "repairs": len(dataset.report.repairs),
        "rejected_rows": dataset.report.rejected_rows,
    }
    return MarketDataAuditCase(
        "quality_failures_detected_without_silent_repair",
        evidence,
        tuple(failures),
    )


def _timestamp_precision_case() -> MarketDataAuditCase:
    failures: list[str] = []
    second_ns = normalize_timestamp(
        "2024-01-02T09:30:00",
        "America/New_York",
        TimestampPrecision.SECOND,
    )
    millisecond_ns = normalize_timestamp(
        "2024-01-02T14:30:00.125Z",
        "UTC",
        TimestampPrecision.MILLISECOND,
    )
    fake_precision_rejected = False
    try:
        normalize_timestamp(
            "2024-01-02T14:30:00.125Z",
            "UTC",
            TimestampPrecision.NANOSECOND,
        )
    except ValueError:
        fake_precision_rejected = True
    if second_ns != 1_704_205_800_000_000_000:
        failures.append("named source timezone did not normalize to UTC epoch ns")
    if millisecond_ns != 1_704_205_800_125_000_000:
        failures.append("millisecond timestamp normalization diverged")
    if not fake_precision_rejected:
        failures.append("millisecond source was upgraded to fake nanosecond precision")
    evidence = {
        "fake_nanosecond_precision_rejected": fake_precision_rejected,
        "millisecond_normalized_ns": millisecond_ns,
        "second_normalized_ns": second_ns,
    }
    return MarketDataAuditCase(
        "timestamp_timezone_and_precision_preserved",
        evidence,
        tuple(failures),
    )


def _capability_boundary_case() -> MarketDataAuditCase:
    failures: list[str] = []
    if CAPABILITY_RECORD_TYPES[SourceCapability.BARS_ONLY] != {
        RecordType.BAR,
        RecordType.SYMBOL_METADATA,
        RecordType.SESSION_METADATA,
    }:
        failures.append("bar capability exposes facts it cannot contain")
    expected_types = set(RecordType)
    if CAPABILITY_RECORD_TYPES[SourceCapability.MARKET_BY_ORDER] != expected_types:
        failures.append("normalized record family inventory is incomplete")
    package = Path(__file__).resolve().parents[1]
    with TemporaryDirectory() as directory:
        store = MarketDataStore(Path(directory))
        bars = store.ingest(
            "csv", package / "marketdata" / "fixtures" / "bars.csv"
        )
        quotes = store.ingest(
            "parquet",
            package / "marketdata" / "fixtures" / "trades_quotes.parquet",
        )
        bar_decision = store.replay_decision(bars.dataset_id)
        quote_decision = store.replay_decision(quotes.dataset_id)
        if bar_decision.mode is not ReplayMode.PARTIAL_OBSERVATION:
            failures.append("bar source was not routed to partial observation")
        if quote_decision.exact_replay_allowed:
            failures.append("quote-only Level 1 evidence was called exact Level 2 replay")
        evidence = {
            "bar_exact": bar_decision.exact_replay_allowed,
            "bar_mode": bar_decision.mode.value,
            "normalized_record_types": sorted(item.value for item in RecordType),
            "quote_exact": quote_decision.exact_replay_allowed,
            "quote_mode": quote_decision.mode.value,
        }
    return MarketDataAuditCase(
        "capability_hierarchy_refuses_overclaiming",
        evidence,
        tuple(failures),
    )


def _immutable_provenance_case() -> MarketDataAuditCase:
    failures: list[str] = []
    package = Path(__file__).resolve().parents[1]
    source = package / "marketdata" / "fixtures" / "bars.csv"
    with TemporaryDirectory() as directory:
        store = MarketDataStore(Path(directory))
        first = store.ingest("csv", source)
        manifest_path = store.dataset_directory(first.dataset_id) / "dataset_manifest.toml"
        original = manifest_path.read_bytes()
        second = store.ingest("csv", source)
        if first.dataset_id != second.dataset_id or manifest_path.read_bytes() != original:
            failures.append("identical source import overwrote or duplicated its dataset")
        records_path = (
            store.dataset_directory(first.dataset_id) / "normalized_records.parquet"
        )
        records_path.write_bytes(records_path.read_bytes() + b"tamper")
        verification = store.verify_dataset(first.dataset_id)
        overwrite_rejected = False
        try:
            store.ingest("csv", source)
        except RuntimeError:
            overwrite_rejected = True
        if verification.passed or not overwrite_rejected:
            failures.append("dataset tamper did not fail closed")
        evidence = {
            "dataset_id": first.dataset_id,
            "idempotent_identity": first.dataset_id == second.dataset_id,
            "overwrite_rejected": overwrite_rejected,
            "tamper_verification": "FAIL_EXPECTED" if not verification.passed else "PASS",
        }
    return MarketDataAuditCase(
        "immutable_dataset_provenance_and_tamper_detection",
        evidence,
        tuple(failures),
    )


def _defect_rows() -> tuple[dict[str, object], ...]:
    snapshot_fields = {
        "ask_levels": [{"price_ticks": 101, "quantity": 100}],
        "bid_levels": [{"price_ticks": 99, "quantity": 100}],
    }
    envelope = {
        "session_id": "QUALITY",
        "source_timezone": "UTC",
        "symbol": "K2Q",
        "timestamp_precision": "MICROSECOND",
    }
    return (
        {
            **envelope,
            "currency": "USD",
            "instrument_type": "STOCK",
            "record_type": "SYMBOL_METADATA",
            "source_timestamp": "2024-01-02T14:30:00.000000Z",
        },
        {
            **envelope,
            "record_type": "SESSION_METADATA",
            "session_close_ns": 1_704_205_810_000_000_000,
            "session_open_ns": 1_704_205_800_000_000_000,
            "source_timestamp": "2024-01-02T14:30:00.000000Z",
        },
        {
            **envelope,
            **snapshot_fields,
            "record_type": "BOOK_SNAPSHOT",
            "source_timestamp": "2024-01-02T14:30:00.000000Z",
        },
        {
            **envelope,
            "order_action": "LIMIT",
            "order_id": "Q-1",
            "price_ticks": 99,
            "quantity": 100,
            "record_type": "ORDER_EVENT",
            "side": "buy",
            "source_sequence": 1,
            "source_timestamp": "2024-01-02T14:30:01.000000Z",
        },
        {
            **envelope,
            "order_action": "LIMIT",
            "order_id": "Q-3",
            "price_ticks": 101,
            "quantity": 100,
            "record_type": "ORDER_EVENT",
            "side": "sell",
            "source_sequence": 3,
            "source_timestamp": "2024-01-02T14:30:02.000000Z",
        },
        {
            **envelope,
            **snapshot_fields,
            "record_type": "BOOK_SNAPSHOT",
            "source_timestamp": "2024-01-02T14:30:03.000000Z",
        },
        {
            **envelope,
            **snapshot_fields,
            "record_type": "BOOK_SNAPSHOT",
            "source_timestamp": "2024-01-02T14:30:03.000000Z",
        },
        {
            **envelope,
            "ask_price_ticks": 100,
            "ask_quantity": 10,
            "bid_price_ticks": 101,
            "bid_quantity": 10,
            "record_type": "QUOTE",
            "source_timestamp": "2024-01-02T14:30:04.000000Z",
        },
        {
            **envelope,
            "price_ticks": 100,
            "quantity": -1,
            "record_type": "TRADE",
            "source_timestamp": "2024-01-02T14:30:05.000000Z",
        },
        {
            **envelope,
            "price_ticks": 100,
            "quantity": 5,
            "record_type": "TRADE",
            "source_timestamp": "2024-01-02T14:30:06.000000Z",
        },
        {
            **envelope,
            "price_ticks": 0,
            "quantity": 5,
            "record_type": "TRADE",
            "source_timestamp": "2024-01-02T14:30:07.000000Z",
        },
        {
            **envelope,
            "price_ticks": 100,
            "quantity": 5,
            "record_type": "TRADE",
            "source_timestamp": "2024-01-02T14:30:05.500000Z",
        },
        {
            **envelope,
            "order_action": "MARKET",
            "order_id": "Q-2",
            "quantity": 10,
            "record_type": "ORDER_EVENT",
            "side": "buy",
            "source_sequence": 2,
            "source_timestamp": "2024-01-02T14:30:08.000000Z",
        },
        {
            **envelope,
            "price_ticks": 100,
            "quantity": 5,
            "record_type": "TRADE",
            "source_timestamp": "2024-01-02T14:30:11.000000Z",
        },
    )
