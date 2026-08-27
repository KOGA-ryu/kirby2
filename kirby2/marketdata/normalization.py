"""Strict normalization and data-quality classification for imported market data."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kirby2.research.toml_codec import canonical_digest, decode_payload

from .adapters import RawDataset
from .models import (
    CAPABILITY_RECORD_TYPES,
    RECORD_FIELDS,
    DataGap,
    DataQualityIssue,
    DataQualityReport,
    NormalizedDataset,
    NormalizedMarketRecord,
    QualitySeverity,
    RecordType,
    ReplayCapabilityDecision,
    ReplayMode,
    SourceCapability,
    TimestampPrecision,
)


_TIMESTAMP = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?(?P<zone>Z|[+-]\d{2}:\d{2})?$"
)
_ENVELOPE_FIELDS = frozenset(
    {
        "record_type",
        "source_timestamp",
        "source_timezone",
        "timestamp_precision",
        "source_sequence",
        "symbol",
        "session_id",
    }
)
_INTEGER_FIELDS = frozenset(
    {
        "ask_price_ticks",
        "ask_quantity",
        "bid_price_ticks",
        "bid_quantity",
        "close_ticks",
        "high_ticks",
        "imbalance",
        "interval_ns",
        "level",
        "lot_size",
        "low_ticks",
        "open_ticks",
        "price_ticks",
        "quantity",
        "session_close_ns",
        "session_open_ns",
        "snapshot_sequence",
        "volume",
    }
)
_POSITIVE_FIELDS = frozenset(
    {
        "ask_price_ticks",
        "bid_price_ticks",
        "close_ticks",
        "high_ticks",
        "interval_ns",
        "lot_size",
        "low_ticks",
        "open_ticks",
        "price_ticks",
        "session_close_ns",
        "session_open_ns",
    }
)


def normalize_raw_dataset(raw: RawDataset) -> NormalizedDataset:
    preliminary: list[NormalizedMarketRecord | None] = []
    rejections: list[DataQualityIssue] = []
    for source_row, raw_row in enumerate(raw.rows, start=1):
        try:
            preliminary.append(_normalize_row(raw, source_row, raw_row))
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
            preliminary.append(None)
            rejections.append(
                _issue(
                    "INVALID_RECORD",
                    QualitySeverity.REJECTION,
                    source_row,
                    str(error),
                )
            )

    session_bounds = {
        record.session_id: (
            int(record.fields["session_open_ns"]),
            int(record.fields["session_close_ns"]),
        )
        for record in preliminary
        if record is not None and record.record_type is RecordType.SESSION_METADATA
    }
    accepted: list[NormalizedMarketRecord] = []
    warnings: list[DataQualityIssue] = []
    seen_digests: dict[str, int] = {}
    seen_sequences: dict[int, int] = {}
    last_timestamp: int | None = None
    last_sequence: int | None = None
    rejected_row_numbers = {
        row for issue in rejections for row in issue.source_rows
    }
    for record in preliminary:
        if record is None:
            continue
        row_rejections: list[DataQualityIssue] = []
        fingerprint = canonical_digest(
            {
                key: value
                for key, value in record.as_dict().items()
                if key != "source_row"
            }
        )
        if fingerprint in seen_digests:
            row_rejections.append(
                DataQualityIssue(
                    "DUPLICATE_RECORD",
                    QualitySeverity.REJECTION,
                    (seen_digests[fingerprint], record.source_row),
                    "record duplicates an earlier normalized source fact",
                )
            )
        if record.source_sequence is not None:
            if record.source_sequence in seen_sequences:
                row_rejections.append(
                    DataQualityIssue(
                        "DUPLICATE_SEQUENCE",
                        QualitySeverity.REJECTION,
                        (
                            seen_sequences[record.source_sequence],
                            record.source_row,
                        ),
                        "source sequence number is reused",
                    )
                )
            if last_sequence is not None and record.source_sequence < last_sequence:
                row_rejections.append(
                    _issue(
                        "OUT_OF_ORDER_RECORD",
                        QualitySeverity.REJECTION,
                        record.source_row,
                        "source sequence moved backward; no implicit reorder was applied",
                    )
                )
        if (
            last_timestamp is not None
            and record.normalized_timestamp_ns < last_timestamp
        ):
            row_rejections.append(
                _issue(
                    "TIMESTAMP_REVERSAL",
                    QualitySeverity.REJECTION,
                    record.source_row,
                    "normalized timestamp moved backward; no implicit reorder was applied",
                )
            )
        bounds = session_bounds.get(record.session_id)
        if (
            bounds is not None
            and record.record_type is not RecordType.SESSION_METADATA
            and not bounds[0] <= record.normalized_timestamp_ns <= bounds[1]
        ):
            row_rejections.append(
                _issue(
                    "SESSION_BOUNDARY",
                    QualitySeverity.REJECTION,
                    record.source_row,
                    "record timestamp is outside its declared session boundary",
                )
            )
        row_rejections.extend(_semantic_rejections(record))
        if row_rejections:
            rejections.extend(row_rejections)
            rejected_row_numbers.add(record.source_row)
            continue
        seen_digests[fingerprint] = record.source_row
        if record.source_sequence is not None:
            seen_sequences[record.source_sequence] = record.source_row
            last_sequence = record.source_sequence
        last_timestamp = record.normalized_timestamp_ns
        accepted.append(record)
        if (
            record.record_type is RecordType.TRADE
            and record.fields.get("aggressor_side") is None
        ):
            warnings.append(
                _issue(
                    "UNKNOWN_AGGRESSOR_SIDE",
                    QualitySeverity.WARNING,
                    record.source_row,
                    "trade aggressor side is unavailable and remains unknown",
                )
            )

    gaps = list(_sequence_gaps(accepted))
    gaps.extend(_snapshot_gaps(accepted, raw.expected_snapshot_interval_ns))
    warnings.extend(_gap_warnings(gaps))
    time_range = (
        None
        if not accepted
        else (
            min(item.normalized_timestamp_ns for item in accepted),
            max(item.normalized_timestamp_ns for item in accepted),
        )
    )
    symbols = tuple(sorted({item.symbol for item in accepted}))
    sessions = {item.session_id for item in accepted}
    report = DataQualityReport(
        input_rows=len(raw.rows),
        accepted_rows=len(accepted),
        rejected_rows=len(rejected_row_numbers),
        warnings=tuple(warnings),
        rejections=tuple(rejections),
        capability_level=raw.capability,
        time_range_ns=time_range,
        symbols=symbols,
        session_count=len(sessions),
        gaps=tuple(gaps),
        repairs=(),
        source_digest=raw.source_digest,
    )
    replay = replay_capability(raw.capability, tuple(accepted), report)
    return NormalizedDataset(
        adapter=raw.adapter,
        source_locator=raw.source_locator,
        source_name=raw.source_name,
        license_note=raw.license_note,
        real_market_data=raw.real_market_data,
        capability=raw.capability,
        tick_size=raw.tick_size,
        records=tuple(accepted),
        report=report,
        replay=replay,
    )


def replay_capability(
    capability: SourceCapability,
    records: tuple[NormalizedMarketRecord, ...],
    report: DataQualityReport,
) -> ReplayCapabilityDecision:
    if capability is SourceCapability.MARKET_BY_ORDER:
        order_records = tuple(
            item for item in records if item.record_type is RecordType.ORDER_EVENT
        )
        sequences = tuple(
            item.source_sequence
            for item in order_records
            if item.source_sequence is not None
        )
        complete = (
            bool(order_records)
            and len(sequences) == len(order_records)
            and sequences == tuple(range(1, len(sequences) + 1))
        )
        reasons: list[str] = []
        if not complete:
            reasons.append("market-by-order source lacks a complete ordered message sequence")
        if report.rejected_rows:
            reasons.append("one or more source rows were rejected")
        if any(gap.gap_type == "MISSING_SEQUENCE" for gap in report.gaps):
            reasons.append("source sequence contains gaps")
        if not reasons:
            return ReplayCapabilityDecision(
                ReplayMode.EXACT_REPLAY,
                True,
                (
                    "source declares market-by-order messages",
                    "ordered source sequence is complete",
                    "quality gate has no rejected rows or sequence gaps",
                ),
            )
        return ReplayCapabilityDecision(
            ReplayMode.RECONSTRUCTION,
            False,
            tuple(reasons),
        )
    if capability in {
        SourceCapability.LEVEL2_SNAPSHOTS,
        SourceCapability.LEVEL2_DELTAS,
    }:
        reason = (
            "snapshots do not reveal every event between observations"
            if capability is SourceCapability.LEVEL2_SNAPSHOTS
            else "market-by-price deltas do not reveal individual queue ordering"
        )
        return ReplayCapabilityDecision(
            ReplayMode.RECONSTRUCTION,
            False,
            (reason, "historical reconstruction is not exact replay"),
        )
    reasons = {
        SourceCapability.BARS_ONLY: (
            "bars do not provide actual trades, quotes, book state, or queue events",
        ),
        SourceCapability.TRADES: (
            "trade prints do not provide quotes, book state, or queue events",
        ),
        SourceCapability.TRADES_AND_QUOTES: (
            "trades and quotes do not provide Level 2 queues or individual order events",
        ),
    }[capability]
    return ReplayCapabilityDecision(
        ReplayMode.PARTIAL_OBSERVATION,
        False,
        (*reasons, "partial observations cannot be labeled exact Level 2 replay"),
    )


def _normalize_row(
    raw: RawDataset,
    source_row: int,
    row: dict[str, object],
) -> NormalizedMarketRecord:
    record_type = RecordType(str(row["record_type"]).upper())
    if record_type not in CAPABILITY_RECORD_TYPES[raw.capability]:
        raise ValueError(
            f"{record_type.value} exceeds declared {raw.capability.value} capability"
        )
    precision = TimestampPrecision(str(row["timestamp_precision"]).upper())
    source_timestamp = str(row["source_timestamp"])
    source_timezone = str(row.get("source_timezone", raw.source_timezone))
    normalized_timestamp_ns = normalize_timestamp(
        source_timestamp,
        source_timezone,
        precision,
    )
    source_sequence = _optional_int(row.get("source_sequence"))
    symbol = str(row.get("symbol", "UNKNOWN"))
    session_id = str(row.get("session_id", "UNKNOWN"))
    allowed = _ENVELOPE_FIELDS | RECORD_FIELDS[record_type]
    unknown = set(row) - allowed
    if unknown:
        raise ValueError(f"source row contains unknown columns {sorted(unknown)}")
    fields = {
        key: _coerce_field(key, value)
        for key, value in row.items()
        if key in RECORD_FIELDS[record_type]
    }
    return NormalizedMarketRecord(
        record_type=record_type,
        source_row=source_row,
        source_timestamp=source_timestamp,
        normalized_timestamp_ns=normalized_timestamp_ns,
        source_timezone=source_timezone,
        timestamp_precision=precision,
        source_sequence=source_sequence,
        symbol=symbol,
        session_id=session_id,
        fields=fields,
    )


def normalize_timestamp(
    source_timestamp: str,
    source_timezone: str,
    precision: TimestampPrecision,
) -> int:
    match = _TIMESTAMP.fullmatch(source_timestamp)
    if match is None:
        raise ValueError("source timestamp must be ISO-8601 with at most 9 decimals")
    fraction = match.group("fraction") or ""
    required_digits = {
        TimestampPrecision.SECOND: 0,
        TimestampPrecision.MILLISECOND: 3,
        TimestampPrecision.MICROSECOND: 6,
        TimestampPrecision.NANOSECOND: 9,
    }[precision]
    if len(fraction) != required_digits:
        raise ValueError(
            f"timestamp text does not preserve declared {precision.value} precision"
        )
    zone_text = match.group("zone") or ""
    parse_text = match.group("base")
    if fraction:
        parse_text += "." + fraction[:6]
    if zone_text:
        parse_text += "+00:00" if zone_text == "Z" else zone_text
    source_zone = ZoneInfo(source_timezone)
    parsed = datetime.fromisoformat(parse_text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_zone)
    elif parsed.utcoffset() != parsed.astimezone(source_zone).utcoffset():
        raise ValueError("source timestamp offset contradicts declared source timezone")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    nanoseconds = (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )
    if len(fraction) > 6:
        nanoseconds += int(fraction[6:].ljust(3, "0"))
    if nanoseconds < 0:
        raise ValueError("timestamps before the Unix epoch are not supported")
    if nanoseconds % precision.quantum_ns:
        raise ValueError("normalized timestamp exceeds declared precision")
    return nanoseconds


def _semantic_rejections(
    record: NormalizedMarketRecord,
) -> list[DataQualityIssue]:
    fields = record.fields
    failures: list[str] = []
    for key, value in fields.items():
        if key in _INTEGER_FIELDS:
            if type(value) is not int:
                failures.append(f"{key} must be an integer")
            elif value < (1 if key in _POSITIVE_FIELDS else 0):
                failures.append(f"{key} has an invalid negative or zero value")
    if record.record_type is RecordType.BAR:
        low = int(fields["low_ticks"])
        high = int(fields["high_ticks"])
        opened = int(fields["open_ticks"])
        closed = int(fields["close_ticks"])
        if low > min(opened, closed) or high < max(opened, closed) or low > high:
            failures.append("bar OHLC prices are inconsistent")
    elif record.record_type is RecordType.QUOTE:
        if int(fields["bid_price_ticks"]) >= int(fields["ask_price_ticks"]):
            failures.append("crossed or locked quote is not accepted")
    elif record.record_type is RecordType.BOOK_SNAPSHOT:
        failures.extend(_validate_snapshot(fields))
    elif record.record_type is RecordType.BOOK_DELTA:
        if fields.get("side") not in {"buy", "sell"}:
            failures.append("book delta side must be buy or sell")
        if fields.get("update_action") not in {"add", "change", "delete"}:
            failures.append("book delta update action is invalid")
    elif record.record_type is RecordType.ORDER_EVENT:
        action = fields.get("order_action")
        if action not in {"LIMIT", "MARKET", "CANCEL", "DELETE", "REPLACE"}:
            failures.append("order event action is unsupported")
        if action in {"LIMIT", "MARKET"}:
            if fields.get("side") not in {"buy", "sell"}:
                failures.append("trading order event requires buy or sell side")
            if type(fields.get("quantity")) is not int or int(fields["quantity"]) <= 0:
                failures.append("trading order event requires positive quantity")
        if action == "LIMIT" and (
            type(fields.get("price_ticks")) is not int
            or int(fields["price_ticks"]) <= 0
        ):
            failures.append("limit order event requires positive tick price")
        if action in {"CANCEL", "DELETE", "REPLACE"} and not fields.get(
            "target_order_id"
        ):
            failures.append("cancel/delete/replace requires target order ID")
    elif record.record_type is RecordType.TRADE:
        if fields.get("aggressor_side") not in {None, "buy", "sell"}:
            failures.append("trade aggressor side must be buy, sell, or unknown")
    elif record.record_type is RecordType.SESSION_METADATA:
        if int(fields["session_close_ns"]) <= int(fields["session_open_ns"]):
            failures.append("session close must be after session open")
    return [
        _issue(
            _semantic_issue_code(message),
            QualitySeverity.REJECTION,
            record.source_row,
            message,
        )
        for message in failures
    ]


def _validate_snapshot(fields: dict[str, object]) -> list[str]:
    failures: list[str] = []
    bids = fields.get("bid_levels")
    asks = fields.get("ask_levels")
    if not isinstance(bids, list) or not isinstance(asks, list):
        return ["book snapshot levels must be arrays"]
    for name, levels, descending in (("bid", bids, True), ("ask", asks, False)):
        try:
            prices = [int(item["price_ticks"]) for item in levels]
            quantities = [int(item["quantity"]) for item in levels]
        except (KeyError, TypeError, ValueError):
            failures.append(f"{name} snapshot levels are malformed")
            continue
        if prices != sorted(prices, reverse=descending) or len(prices) != len(
            set(prices)
        ):
            failures.append(f"{name} snapshot prices are incorrectly ordered")
        if any(price <= 0 for price in prices):
            failures.append(f"{name} snapshot contains invalid prices")
        if any(quantity < 0 for quantity in quantities):
            failures.append(f"{name} snapshot contains negative quantities")
    if bids and asks:
        try:
            if int(bids[0]["price_ticks"]) >= int(asks[0]["price_ticks"]):
                failures.append("book snapshot is crossed or locked")
        except (KeyError, TypeError, ValueError):
            pass
    return failures


def _sequence_gaps(
    records: list[NormalizedMarketRecord],
) -> tuple[DataGap, ...]:
    sequenced = sorted(
        (
            item.source_sequence,
            item.normalized_timestamp_ns,
        )
        for item in records
        if item.source_sequence is not None
    )
    gaps: list[DataGap] = []
    for (before, before_time), (after, after_time) in zip(
        sequenced,
        sequenced[1:],
    ):
        if after > before + 1:
            gaps.append(
                DataGap(
                    "MISSING_SEQUENCE",
                    before_time,
                    after_time,
                    after - before - 1,
                    f"source sequence jumps from {before} to {after}",
                )
            )
    return tuple(gaps)


def _snapshot_gaps(
    records: list[NormalizedMarketRecord],
    expected_interval_ns: int | None,
) -> tuple[DataGap, ...]:
    if expected_interval_ns is None or expected_interval_ns <= 0:
        return ()
    snapshots = sorted(
        item.normalized_timestamp_ns
        for item in records
        if item.record_type is RecordType.BOOK_SNAPSHOT
    )
    return tuple(
        DataGap(
            "SNAPSHOT_GAP",
            before,
            after,
            max(0, (after - before) // expected_interval_ns - 1),
            f"snapshot interval {after - before}ns exceeds expected "
            f"{expected_interval_ns}ns",
        )
        for before, after in zip(snapshots, snapshots[1:])
        if after - before > expected_interval_ns
    )


def _gap_warnings(gaps: list[DataGap]) -> tuple[DataQualityIssue, ...]:
    return tuple(
        DataQualityIssue(
            gap.gap_type,
            QualitySeverity.WARNING,
            (1,),
            gap.details,
        )
        for gap in gaps
    )


def _coerce_field(key: str, value: object) -> object:
    if key in _INTEGER_FIELDS:
        return int(value)
    if key in {"bid_levels", "ask_levels"} and isinstance(value, str):
        if value.lstrip().startswith("{"):
            decoded = decode_payload(value)
            levels = decoded.get("levels")
            if not isinstance(levels, list):
                raise ValueError(f"{key} encoded payload lacks levels")
            return levels
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError(f"{key} JSON value must be an array")
        return parsed
    if key in {"side", "aggressor_side", "update_action"}:
        return str(value).lower()
    if key == "order_action":
        return str(value).upper()
    return value


def _semantic_issue_code(message: str) -> str:
    if "crossed or locked" in message:
        return "CROSSED_QUOTE"
    if "price" in message:
        return "INVALID_PRICE"
    if "quantity" in message or "volume" in message:
        return "INVALID_QUANTITY"
    return "INVALID_RECORD"


def _optional_int(value: Any) -> int | None:
    return None if value in {None, ""} else int(value)


def _issue(
    code: str,
    severity: QualitySeverity,
    source_row: int,
    message: str,
) -> DataQualityIssue:
    return DataQualityIssue(code, severity, (source_row,), message)
