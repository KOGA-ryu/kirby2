"""Strict market-data capabilities, records, quality, and replay contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from kirby2.research.toml_codec import canonical_digest


MARKET_DATA_SCHEMA_VERSION = 1
DATASET_MANIFEST_SCHEMA_VERSION = 1
DATA_QUALITY_SCHEMA_VERSION = 1
DATASET_SCHEMA_VERSIONS = {
    "dataset_manifest": DATASET_MANIFEST_SCHEMA_VERSION,
    "normalized_record": MARKET_DATA_SCHEMA_VERSION,
    "quality_issue": DATA_QUALITY_SCHEMA_VERSION,
    "quality_report": DATA_QUALITY_SCHEMA_VERSION,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATASET_ID = re.compile(r"^dataset-[0-9a-f]{24}$")


class SourceCapability(str, Enum):
    BARS_ONLY = "BARS_ONLY"
    TRADES = "TRADES"
    TRADES_AND_QUOTES = "TRADES_AND_QUOTES"
    LEVEL2_SNAPSHOTS = "LEVEL2_SNAPSHOTS"
    LEVEL2_DELTAS = "LEVEL2_DELTAS"
    MARKET_BY_ORDER = "MARKET_BY_ORDER"


class RecordType(str, Enum):
    BAR = "BAR"
    TRADE = "TRADE"
    QUOTE = "QUOTE"
    BOOK_SNAPSHOT = "BOOK_SNAPSHOT"
    BOOK_DELTA = "BOOK_DELTA"
    ORDER_EVENT = "ORDER_EVENT"
    AUCTION = "AUCTION"
    HALT = "HALT"
    RESUME = "RESUME"
    SYMBOL_METADATA = "SYMBOL_METADATA"
    SESSION_METADATA = "SESSION_METADATA"


class TimestampPrecision(str, Enum):
    SECOND = "SECOND"
    MILLISECOND = "MILLISECOND"
    MICROSECOND = "MICROSECOND"
    NANOSECOND = "NANOSECOND"

    @property
    def quantum_ns(self) -> int:
        return {
            TimestampPrecision.SECOND: 1_000_000_000,
            TimestampPrecision.MILLISECOND: 1_000_000,
            TimestampPrecision.MICROSECOND: 1_000,
            TimestampPrecision.NANOSECOND: 1,
        }[self]


class QualitySeverity(str, Enum):
    WARNING = "WARNING"
    REJECTION = "REJECTION"


class ReplayMode(str, Enum):
    EXACT_REPLAY = "EXACT_REPLAY"
    RECONSTRUCTION = "RECONSTRUCTION"
    PARTIAL_OBSERVATION = "PARTIAL_OBSERVATION"


CAPABILITY_RECORD_TYPES: dict[SourceCapability, frozenset[RecordType]] = {
    SourceCapability.BARS_ONLY: frozenset(
        {RecordType.BAR, RecordType.SYMBOL_METADATA, RecordType.SESSION_METADATA}
    ),
    SourceCapability.TRADES: frozenset(
        {
            RecordType.TRADE,
            RecordType.AUCTION,
            RecordType.HALT,
            RecordType.RESUME,
            RecordType.SYMBOL_METADATA,
            RecordType.SESSION_METADATA,
        }
    ),
    SourceCapability.TRADES_AND_QUOTES: frozenset(
        {
            RecordType.TRADE,
            RecordType.QUOTE,
            RecordType.AUCTION,
            RecordType.HALT,
            RecordType.RESUME,
            RecordType.SYMBOL_METADATA,
            RecordType.SESSION_METADATA,
        }
    ),
    SourceCapability.LEVEL2_SNAPSHOTS: frozenset(
        {
            RecordType.TRADE,
            RecordType.QUOTE,
            RecordType.BOOK_SNAPSHOT,
            RecordType.AUCTION,
            RecordType.HALT,
            RecordType.RESUME,
            RecordType.SYMBOL_METADATA,
            RecordType.SESSION_METADATA,
        }
    ),
    SourceCapability.LEVEL2_DELTAS: frozenset(
        {
            RecordType.TRADE,
            RecordType.QUOTE,
            RecordType.BOOK_SNAPSHOT,
            RecordType.BOOK_DELTA,
            RecordType.AUCTION,
            RecordType.HALT,
            RecordType.RESUME,
            RecordType.SYMBOL_METADATA,
            RecordType.SESSION_METADATA,
        }
    ),
    SourceCapability.MARKET_BY_ORDER: frozenset(RecordType),
}


_REQUIRED_FIELDS: dict[RecordType, frozenset[str]] = {
    RecordType.BAR: frozenset(
        {
            "open_ticks",
            "high_ticks",
            "low_ticks",
            "close_ticks",
            "volume",
            "interval_ns",
        }
    ),
    RecordType.TRADE: frozenset({"price_ticks", "quantity"}),
    RecordType.QUOTE: frozenset(
        {"bid_price_ticks", "bid_quantity", "ask_price_ticks", "ask_quantity"}
    ),
    RecordType.BOOK_SNAPSHOT: frozenset({"bid_levels", "ask_levels"}),
    RecordType.BOOK_DELTA: frozenset(
        {"side", "price_ticks", "quantity", "update_action"}
    ),
    RecordType.ORDER_EVENT: frozenset({"order_id", "order_action"}),
    RecordType.AUCTION: frozenset({"auction_type", "event_status"}),
    RecordType.HALT: frozenset({"reason"}),
    RecordType.RESUME: frozenset({"reason"}),
    RecordType.SYMBOL_METADATA: frozenset({"instrument_type", "currency"}),
    RecordType.SESSION_METADATA: frozenset({"session_open_ns", "session_close_ns"}),
}

RECORD_FIELDS: dict[RecordType, frozenset[str]] = {
    RecordType.BAR: _REQUIRED_FIELDS[RecordType.BAR],
    RecordType.TRADE: frozenset({"price_ticks", "quantity", "aggressor_side"}),
    RecordType.QUOTE: _REQUIRED_FIELDS[RecordType.QUOTE],
    RecordType.BOOK_SNAPSHOT: frozenset(
        {"bid_levels", "ask_levels", "snapshot_sequence"}
    ),
    RecordType.BOOK_DELTA: frozenset(
        {"side", "price_ticks", "quantity", "update_action", "level"}
    ),
    RecordType.ORDER_EVENT: frozenset(
        {
            "order_id",
            "order_action",
            "side",
            "price_ticks",
            "quantity",
            "target_order_id",
        }
    ),
    RecordType.AUCTION: frozenset(
        {"auction_type", "event_status", "price_ticks", "quantity", "imbalance"}
    ),
    RecordType.HALT: frozenset({"reason", "event_status"}),
    RecordType.RESUME: frozenset({"reason", "event_status"}),
    RecordType.SYMBOL_METADATA: frozenset(
        {"instrument_type", "currency", "venue", "lot_size"}
    ),
    RecordType.SESSION_METADATA: frozenset(
        {"session_open_ns", "session_close_ns", "venue", "session_type"}
    ),
}


@dataclass(frozen=True, slots=True)
class NormalizedMarketRecord:
    record_type: RecordType
    source_row: int
    source_timestamp: str
    normalized_timestamp_ns: int
    source_timezone: str
    timestamp_precision: TimestampPrecision
    source_sequence: int | None
    symbol: str
    session_id: str
    fields: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.source_row) is not int or self.source_row <= 0:
            raise ValueError("normalized source row must be positive")
        if (
            type(self.normalized_timestamp_ns) is not int
            or self.normalized_timestamp_ns < 0
        ):
            raise ValueError("normalized timestamp must be epoch nanoseconds")
        if self.normalized_timestamp_ns % self.timestamp_precision.quantum_ns:
            raise ValueError("normalized timestamp exceeds declared source precision")
        if self.source_sequence is not None and (
            type(self.source_sequence) is not int or self.source_sequence < 0
        ):
            raise ValueError("source sequence must be nonnegative or absent")
        if any(
            not value
            for value in (
                self.source_timestamp,
                self.source_timezone,
                self.symbol,
                self.session_id,
            )
        ):
            raise ValueError("normalized source identity fields must not be empty")
        missing = _REQUIRED_FIELDS[self.record_type] - set(self.fields)
        if missing:
            raise ValueError(
                f"{self.record_type.value} missing required fields {sorted(missing)}"
            )
        unknown = set(self.fields) - RECORD_FIELDS[self.record_type]
        if unknown:
            raise ValueError(
                f"{self.record_type.value} contains unknown fields {sorted(unknown)}"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "fields": dict(self.fields),
            "normalized_timestamp_ns": self.normalized_timestamp_ns,
            "record_type": self.record_type.value,
            "session_id": self.session_id,
            "source_row": self.source_row,
            "source_sequence": self.source_sequence,
            "source_timestamp": self.source_timestamp,
            "source_timezone": self.source_timezone,
            "symbol": self.symbol,
            "timestamp_precision": self.timestamp_precision.value,
        }


@dataclass(frozen=True, slots=True)
class DataQualityIssue:
    code: str
    severity: QualitySeverity
    source_rows: tuple[int, ...]
    message: str

    def __post_init__(self) -> None:
        if not self.code or not self.message or not self.source_rows:
            raise ValueError("quality issue requires a code, rows, and message")
        if any(type(row) is not int or row <= 0 for row in self.source_rows):
            raise ValueError("quality issue source rows must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "source_rows": list(self.source_rows),
        }


@dataclass(frozen=True, slots=True)
class DataGap:
    gap_type: str
    start_timestamp_ns: int
    end_timestamp_ns: int
    missing_count: int | None
    details: str

    def __post_init__(self) -> None:
        if (
            not self.gap_type
            or not self.details
            or type(self.start_timestamp_ns) is not int
            or type(self.end_timestamp_ns) is not int
            or self.start_timestamp_ns < 0
            or self.end_timestamp_ns < self.start_timestamp_ns
        ):
            raise ValueError("data gap bounds or identity are invalid")
        if self.missing_count is not None and (
            type(self.missing_count) is not int or self.missing_count < 0
        ):
            raise ValueError("data gap missing count is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "details": self.details,
            "end_timestamp_ns": self.end_timestamp_ns,
            "gap_type": self.gap_type,
            "missing_count": self.missing_count,
            "start_timestamp_ns": self.start_timestamp_ns,
        }


@dataclass(frozen=True, slots=True)
class RepairRecord:
    repair_type: str
    source_rows: tuple[int, ...]
    before_digest: str
    after_digest: str
    reason: str

    def __post_init__(self) -> None:
        if not self.repair_type or not self.reason or not self.source_rows:
            raise ValueError("repair record identity is invalid")
        if any(
            not _SHA256.fullmatch(value)
            for value in (self.before_digest, self.after_digest)
        ):
            raise ValueError("repair record digests are invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "after_digest": self.after_digest,
            "before_digest": self.before_digest,
            "reason": self.reason,
            "repair_type": self.repair_type,
            "source_rows": list(self.source_rows),
        }


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    input_rows: int
    accepted_rows: int
    rejected_rows: int
    warnings: tuple[DataQualityIssue, ...]
    rejections: tuple[DataQualityIssue, ...]
    capability_level: SourceCapability
    time_range_ns: tuple[int, int] | None
    symbols: tuple[str, ...]
    session_count: int
    gaps: tuple[DataGap, ...]
    repairs: tuple[RepairRecord, ...]
    source_digest: str
    schema_version: int = DATA_QUALITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.input_rows != self.accepted_rows + self.rejected_rows:
            raise ValueError("quality row accounting does not reconcile")
        if self.schema_version != DATA_QUALITY_SCHEMA_VERSION:
            raise ValueError("unsupported quality report schema")
        if not _SHA256.fullmatch(self.source_digest):
            raise ValueError("quality report source digest is invalid")
        if self.session_count < 0 or self.session_count > self.accepted_rows:
            raise ValueError("quality report session count is invalid")
        if any(item.severity is not QualitySeverity.WARNING for item in self.warnings):
            raise ValueError("warning inventory contains a non-warning issue")
        if any(
            item.severity is not QualitySeverity.REJECTION for item in self.rejections
        ):
            raise ValueError("rejection inventory contains a non-rejection issue")

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted_rows": self.accepted_rows,
            "capability_level": self.capability_level.value,
            "gaps": [item.as_dict() for item in self.gaps],
            "input_rows": self.input_rows,
            "rejected_rows": self.rejected_rows,
            "rejections": [item.as_dict() for item in self.rejections],
            "repairs": [item.as_dict() for item in self.repairs],
            "schema_version": self.schema_version,
            "session_count": self.session_count,
            "source_digest": self.source_digest,
            "symbols": list(self.symbols),
            "time_range_ns": None
            if self.time_range_ns is None
            else list(self.time_range_ns),
            "warnings": [item.as_dict() for item in self.warnings],
        }


@dataclass(frozen=True, slots=True)
class ReplayCapabilityDecision:
    mode: ReplayMode
    exact_replay_allowed: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.exact_replay_allowed) is not bool or not self.reasons:
            raise ValueError("replay capability decision is incomplete")
        if self.exact_replay_allowed != (self.mode is ReplayMode.EXACT_REPLAY):
            raise ValueError("replay capability mode contradicts exact permission")

    def as_dict(self) -> dict[str, object]:
        return {
            "exact_replay_allowed": self.exact_replay_allowed,
            "mode": self.mode.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class NormalizedDataset:
    adapter: str
    source_locator: str
    source_name: str
    license_note: str
    real_market_data: bool
    capability: SourceCapability
    tick_size: Decimal
    records: tuple[NormalizedMarketRecord, ...]
    report: DataQualityReport
    replay: ReplayCapabilityDecision

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.adapter,
                self.source_locator,
                self.source_name,
                self.license_note,
            )
        ):
            raise ValueError("normalized dataset source identity is required")
        if type(self.real_market_data) is not bool:
            raise TypeError("real-market-data provenance flag must be boolean")
        if not self.tick_size.is_finite() or self.tick_size <= 0:
            raise ValueError("dataset tick size must be a positive finite Decimal")
        if self.report.accepted_rows != len(self.records):
            raise ValueError("dataset records do not reconcile to quality report")
        if self.report.capability_level is not self.capability:
            raise ValueError("dataset capability differs from quality report")

    @property
    def records_digest(self) -> str:
        return canonical_digest({"records": [item.as_dict() for item in self.records]})


@dataclass(frozen=True, slots=True)
class DatasetArtifactReference:
    name: str
    relative_path: str
    sha256: str
    schema_version: int
    media_type: str
    row_count: int | None

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if (
            not self.name
            or path.is_absolute()
            or ".." in path.parts
            or not _SHA256.fullmatch(self.sha256)
            or type(self.schema_version) is not int
            or self.schema_version <= 0
            or not self.media_type
        ):
            raise ValueError("dataset artifact reference is invalid")
        if self.row_count is not None and (
            type(self.row_count) is not int or self.row_count < 0
        ):
            raise ValueError("dataset artifact row count is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "schema_version": self.schema_version,
            "media_type": self.media_type,
            "row_count": self.row_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_id: str
    adapter: str
    source_locator: str
    source_name: str
    license_note: str
    real_market_data: bool
    capability: SourceCapability
    tick_size: str
    source_digest: str
    records_digest: str
    quality_digest: str
    replay_mode: ReplayMode
    exact_replay_allowed: bool
    time_start_ns: int | None
    time_end_ns: int | None
    symbols: tuple[str, ...]
    session_count: int
    schema_versions: dict[str, int]
    creation_timestamp_utc: str
    artifacts: tuple[DatasetArtifactReference, ...]
    schema_version: int = DATASET_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _DATASET_ID.fullmatch(self.dataset_id):
            raise ValueError("dataset ID is invalid")
        if self.schema_version != DATASET_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported dataset manifest schema")
        if self.schema_versions != DATASET_SCHEMA_VERSIONS:
            raise ValueError("dataset schema inventory is incomplete or unsupported")
        if any(
            not _SHA256.fullmatch(value)
            for value in (self.source_digest, self.records_digest, self.quality_digest)
        ):
            raise ValueError("dataset manifest digest is invalid")
        if any(
            not value
            for value in (
                self.adapter,
                self.source_locator,
                self.source_name,
                self.license_note,
                self.tick_size,
                self.creation_timestamp_utc,
            )
        ):
            raise ValueError("dataset manifest source identity is incomplete")
        try:
            tick_size = Decimal(self.tick_size)
            datetime.fromisoformat(
                self.creation_timestamp_utc.replace("Z", "+00:00")
            )
        except (ValueError, ArithmeticError) as error:
            raise ValueError("dataset tick size or timestamp is invalid") from error
        if not tick_size.is_finite() or tick_size <= 0:
            raise ValueError("dataset tick size must be positive and finite")
        if (self.time_start_ns is None) != (self.time_end_ns is None):
            raise ValueError("dataset time range must be completely present or absent")
        if (
            self.time_start_ns is not None
            and self.time_end_ns is not None
            and (
                self.time_start_ns < 0
                or self.time_end_ns < self.time_start_ns
            )
        ):
            raise ValueError("dataset time range is invalid")
        if self.session_count < 0 or len(self.symbols) != len(set(self.symbols)):
            raise ValueError("dataset session or symbol inventory is invalid")
        if type(self.real_market_data) is not bool:
            raise TypeError("dataset real-market-data flag must be boolean")
        if self.exact_replay_allowed != (self.replay_mode is ReplayMode.EXACT_REPLAY):
            raise ValueError("dataset replay mode contradicts exact-replay permission")
        if self.exact_replay_allowed and self.capability is not SourceCapability.MARKET_BY_ORDER:
            raise ValueError("only market-by-order evidence may allow exact replay")
        expected_artifacts = {
            "quality_report": (
                "quality_report.toml",
                DATA_QUALITY_SCHEMA_VERSION,
                "application/toml",
                None,
            ),
            "normalized_records": (
                "normalized_records.parquet",
                MARKET_DATA_SCHEMA_VERSION,
                "application/vnd.apache.parquet",
                "rows",
            ),
            "quality_issues": (
                "quality_issues.parquet",
                DATA_QUALITY_SCHEMA_VERSION,
                "application/vnd.apache.parquet",
                "rows",
            ),
        }
        actual_artifacts = {
            item.name: (
                item.relative_path,
                item.schema_version,
                item.media_type,
                None if item.row_count is None else "rows",
            )
            for item in self.artifacts
        }
        if actual_artifacts != expected_artifacts:
            raise ValueError("dataset artifact inventory is incomplete or unsupported")
        if self.dataset_id != self.derive_dataset_id(self.identity_dict()):
            raise ValueError("dataset ID does not match immutable identity")

    @staticmethod
    def derive_dataset_id(identity: dict[str, object]) -> str:
        return "dataset-" + canonical_digest(identity)[:24]

    def identity_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "capability": self.capability.value,
            "exact_replay_allowed": self.exact_replay_allowed,
            "license_note": self.license_note,
            "quality_digest": self.quality_digest,
            "real_market_data": self.real_market_data,
            "records_digest": self.records_digest,
            "replay_mode": self.replay_mode.value,
            "schema_version": self.schema_version,
            "schema_versions": self.schema_versions,
            "session_count": self.session_count,
            "source_digest": self.source_digest,
            "source_locator": self.source_locator,
            "source_name": self.source_name,
            "symbols": list(self.symbols),
            "tick_size": self.tick_size,
            "time_end_ns": self.time_end_ns,
            "time_start_ns": self.time_start_ns,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "artifacts": [item.as_dict() for item in self.artifacts],
            "creation_timestamp_utc": self.creation_timestamp_utc,
            "dataset_id": self.dataset_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DatasetManifest:
        raw_artifacts = payload.get("artifacts")
        raw_symbols = payload.get("symbols")
        raw_schemas = payload.get("schema_versions")
        if (
            not isinstance(raw_artifacts, list)
            or not isinstance(raw_symbols, list)
            or not isinstance(raw_schemas, dict)
        ):
            raise ValueError("dataset manifest arrays are invalid")
        if type(payload.get("real_market_data")) is not bool or type(
            payload.get("exact_replay_allowed")
        ) is not bool:
            raise ValueError("dataset manifest replay/provenance flags must be booleans")
        artifacts = tuple(
            DatasetArtifactReference(
                name=str(item["name"]),
                relative_path=str(item["relative_path"]),
                sha256=str(item["sha256"]),
                schema_version=int(item["schema_version"]),
                media_type=str(item["media_type"]),
                row_count=None
                if item.get("row_count") is None
                else int(item["row_count"]),
            )
            for item in raw_artifacts
            if isinstance(item, dict)
        )
        start = payload.get("time_start_ns")
        end = payload.get("time_end_ns")
        return cls(
            dataset_id=str(payload["dataset_id"]),
            adapter=str(payload["adapter"]),
            source_locator=str(payload["source_locator"]),
            source_name=str(payload["source_name"]),
            license_note=str(payload["license_note"]),
            real_market_data=payload["real_market_data"],
            capability=SourceCapability(str(payload["capability"])),
            tick_size=str(payload["tick_size"]),
            source_digest=str(payload["source_digest"]),
            records_digest=str(payload["records_digest"]),
            quality_digest=str(payload["quality_digest"]),
            replay_mode=ReplayMode(str(payload["replay_mode"])),
            exact_replay_allowed=payload["exact_replay_allowed"],
            time_start_ns=None if start is None else int(start),
            time_end_ns=None if end is None else int(end),
            symbols=tuple(str(item) for item in raw_symbols),
            session_count=int(payload["session_count"]),
            schema_versions={str(key): int(value) for key, value in raw_schemas.items()},
            creation_timestamp_utc=str(payload["creation_timestamp_utc"]),
            artifacts=artifacts,
            schema_version=int(payload["schema_version"]),
        )


def field_value(record: NormalizedMarketRecord, key: str) -> Any:
    return record.fields.get(key)
