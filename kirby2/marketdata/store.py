"""Immutable normalized-dataset storage beside the Work Order 21 run ledger."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kirby2.research import DEFAULT_RESEARCH_STORE, RunStore
from kirby2.research.toml_codec import (
    canonical_digest,
    canonical_toml,
    decode_payload,
    encode_payload,
    file_sha256,
    load_toml,
)

from .adapters import load_raw_dataset
from .models import (
    DATA_QUALITY_SCHEMA_VERSION,
    DATASET_MANIFEST_SCHEMA_VERSION,
    DATASET_SCHEMA_VERSIONS,
    MARKET_DATA_SCHEMA_VERSION,
    DataQualityReport,
    DatasetArtifactReference,
    DatasetManifest,
    NormalizedDataset,
    NormalizedMarketRecord,
    RecordType,
    ReplayCapabilityDecision,
    ReplayMode,
    SourceCapability,
    TimestampPrecision,
)
from .normalization import normalize_raw_dataset


@dataclass(frozen=True, slots=True)
class DatasetVerificationReport:
    dataset_id: str
    manifest_loaded: bool
    references_exist: bool
    artifact_digests_match: bool
    row_counts_match: bool
    records_digest_match: bool
    quality_digest_match: bool
    schemas_supported: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.manifest_loaded,
                self.references_exist,
                self.artifact_digests_match,
                self.row_counts_match,
                self.records_digest_match,
                self.quality_digest_match,
                self.schemas_supported,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests_match": self.artifact_digests_match,
            "dataset_id": self.dataset_id,
            "failures": list(self.failures),
            "manifest_loaded": self.manifest_loaded,
            "quality_digest_match": self.quality_digest_match,
            "records_digest_match": self.records_digest_match,
            "references_exist": self.references_exist,
            "row_counts_match": self.row_counts_match,
            "schemas_supported": self.schemas_supported,
            "status": "PASS" if self.passed else "FAIL",
        }

    def render(self) -> str:
        lines = [f"KIRBY2_VALIDATE_DATASET dataset_id={self.dataset_id}"]
        lines.extend(
            f"{key.upper()} {str(value).lower()}"
            for key, value in self.as_dict().items()
            if isinstance(value, bool)
        )
        lines.extend(f"FAILURE {failure}" for failure in self.failures)
        lines.append(
            f"VALIDATE_DATASET {'PASS' if self.passed else 'FAIL'} "
            f"failures={len(self.failures)}"
        )
        return "\n".join(lines)


class MarketDataStore:
    def __init__(self, root: Path = DEFAULT_RESEARCH_STORE) -> None:
        self.root = root
        self.datasets_directory = self.root / "datasets"
        self.staging_directory = self.root / ".staging"
        self.datasets_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)

    def ingest(self, adapter: str, source: Path) -> DatasetManifest:
        dataset = normalize_raw_dataset(load_raw_dataset(adapter, source))
        return self.persist(dataset)

    def persist(self, dataset: NormalizedDataset) -> DatasetManifest:
        quality_payload = dataset.report.as_dict()
        quality_digest = canonical_digest(quality_payload)
        start, end = (
            (None, None)
            if dataset.report.time_range_ns is None
            else dataset.report.time_range_ns
        )
        identity = {
            "adapter": dataset.adapter,
            "capability": dataset.capability.value,
            "exact_replay_allowed": dataset.replay.exact_replay_allowed,
            "license_note": dataset.license_note,
            "quality_digest": quality_digest,
            "real_market_data": dataset.real_market_data,
            "records_digest": dataset.records_digest,
            "replay_mode": dataset.replay.mode.value,
            "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
            "schema_versions": dict(DATASET_SCHEMA_VERSIONS),
            "session_count": dataset.report.session_count,
            "source_digest": dataset.report.source_digest,
            "source_locator": dataset.source_locator,
            "source_name": dataset.source_name,
            "symbols": list(dataset.report.symbols),
            "tick_size": str(dataset.tick_size),
            "time_end_ns": end,
            "time_start_ns": start,
        }
        dataset_id = DatasetManifest.derive_dataset_id(identity)
        target = self.dataset_directory(dataset_id)
        if target.exists():
            verification = self.verify_dataset(dataset_id)
            if not verification.passed:
                raise RuntimeError(
                    "existing immutable dataset is invalid and will not be overwritten: "
                    + "; ".join(verification.failures)
                )
            manifest = self.load_manifest(dataset_id)
            if manifest.identity_dict() != identity:
                raise RuntimeError("content-derived dataset ID collision")
            RunStore(self.root).refresh_catalog()
            return manifest
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix=f"{dataset_id}-",
        ) as temporary:
            staging = Path(temporary) / dataset_id
            staging.mkdir()
            quality_path = staging / "quality_report.toml"
            quality_path.write_text(canonical_toml(quality_payload), encoding="utf-8")
            records_path = staging / "normalized_records.parquet"
            issues_path = staging / "quality_issues.parquet"
            _write_records(records_path, dataset_id, dataset.records)
            _write_issues(issues_path, dataset_id, dataset.report)
            artifacts = tuple(
                DatasetArtifactReference(
                    name=name,
                    relative_path=path.name,
                    sha256=file_sha256(path),
                    schema_version=schema_version,
                    media_type=media_type,
                    row_count=row_count,
                )
                for name, path, schema_version, media_type, row_count in (
                    (
                        "quality_report",
                        quality_path,
                        DATA_QUALITY_SCHEMA_VERSION,
                        "application/toml",
                        None,
                    ),
                    (
                        "normalized_records",
                        records_path,
                        MARKET_DATA_SCHEMA_VERSION,
                        "application/vnd.apache.parquet",
                        len(dataset.records),
                    ),
                    (
                        "quality_issues",
                        issues_path,
                        DATA_QUALITY_SCHEMA_VERSION,
                        "application/vnd.apache.parquet",
                        len(dataset.report.warnings) + len(dataset.report.rejections),
                    ),
                )
            )
            manifest = DatasetManifest(
                dataset_id=dataset_id,
                adapter=dataset.adapter,
                source_locator=dataset.source_locator,
                source_name=dataset.source_name,
                license_note=dataset.license_note,
                real_market_data=dataset.real_market_data,
                capability=dataset.capability,
                tick_size=str(dataset.tick_size),
                source_digest=dataset.report.source_digest,
                records_digest=dataset.records_digest,
                quality_digest=quality_digest,
                replay_mode=dataset.replay.mode,
                exact_replay_allowed=dataset.replay.exact_replay_allowed,
                time_start_ns=start,
                time_end_ns=end,
                symbols=dataset.report.symbols,
                session_count=dataset.report.session_count,
                schema_versions=dict(DATASET_SCHEMA_VERSIONS),
                creation_timestamp_utc=_utc_now(),
                artifacts=artifacts,
            )
            (staging / "dataset_manifest.toml").write_text(
                canonical_toml(manifest.as_dict()),
                encoding="utf-8",
            )
            staging.rename(target)
        verification = self.verify_dataset(dataset_id)
        if not verification.passed:
            raise RuntimeError(
                "new immutable dataset failed verification: "
                + "; ".join(verification.failures)
            )
        RunStore(self.root).refresh_catalog()
        return self.load_manifest(dataset_id)

    def dataset_directory(self, dataset_id: str) -> Path:
        if not re.fullmatch(r"dataset-[0-9a-f]{24}", dataset_id):
            raise ValueError("invalid dataset ID")
        return self.datasets_directory / dataset_id

    def load_manifest(self, dataset_id: str) -> DatasetManifest:
        path = self.dataset_directory(dataset_id) / "dataset_manifest.toml"
        if not path.is_file():
            raise ValueError(f"unknown dataset ID: {dataset_id}")
        return DatasetManifest.from_dict(load_toml(path))

    def load_records(self, dataset_id: str) -> tuple[NormalizedMarketRecord, ...]:
        path = self.dataset_directory(dataset_id) / "normalized_records.parquet"
        return _read_records(path)

    def verify_dataset(self, dataset_id: str) -> DatasetVerificationReport:
        failures: list[str] = []
        manifest_loaded = False
        references_exist = False
        artifact_digests_match = False
        row_counts_match = False
        records_digest_match = False
        quality_digest_match = False
        schemas_supported = False
        try:
            manifest = self.load_manifest(dataset_id)
            manifest_loaded = True
            if manifest.dataset_id != dataset_id:
                failures.append("manifest dataset ID does not match requested directory")
        except (OSError, TypeError, ValueError) as error:
            failures.append(f"manifest invalid: {error}")
            return DatasetVerificationReport(
                dataset_id,
                manifest_loaded,
                references_exist,
                artifact_digests_match,
                row_counts_match,
                records_digest_match,
                quality_digest_match,
                schemas_supported,
                tuple(failures),
            )
        schemas_supported = (
            manifest.schema_version == DATASET_MANIFEST_SCHEMA_VERSION
            and manifest.schema_versions == DATASET_SCHEMA_VERSIONS
        )
        directory = self.dataset_directory(dataset_id)
        paths = tuple((item, directory / item.relative_path) for item in manifest.artifacts)
        references_exist = all(path.is_file() for _item, path in paths)
        if not references_exist:
            failures.append("one or more dataset artifacts are missing")
        if references_exist:
            artifact_digests_match = all(
                file_sha256(path) == item.sha256 for item, path in paths
            )
            if not artifact_digests_match:
                failures.append("one or more dataset artifact digests differ")
            try:
                row_counts_match = all(
                    item.row_count is None
                    or _parquet_count(path) == item.row_count
                    for item, path in paths
                )
            except Exception as error:
                failures.append(f"dataset row count could not be read: {error}")
            if not row_counts_match and not any(
                item.startswith("dataset row count") for item in failures
            ):
                failures.append("one or more dataset row counts differ")
        try:
            records = self.load_records(dataset_id)
            records_digest_match = (
                canonical_digest({"records": [item.as_dict() for item in records]})
                == manifest.records_digest
            )
            if not records_digest_match:
                failures.append("normalized record digest differs")
        except Exception as error:
            failures.append(f"normalized records invalid: {error}")
        try:
            quality = load_toml(directory / "quality_report.toml")
            schemas_supported = schemas_supported and (
                quality.get("schema_version") == DATA_QUALITY_SCHEMA_VERSION
                and _parquet_schema_versions(
                    directory / "quality_issues.parquet"
                )
                <= {DATA_QUALITY_SCHEMA_VERSION}
            )
            quality_digest_match = canonical_digest(quality) == manifest.quality_digest
            if not quality_digest_match:
                failures.append("quality report digest differs")
            if int(quality["accepted_rows"]) != len(records):
                failures.append("quality report accepted count does not reconcile")
        except Exception as error:
            failures.append(f"quality report invalid: {error}")
        if not schemas_supported:
            failures.append("dataset or artifact schema is unsupported")
        return DatasetVerificationReport(
            dataset_id,
            manifest_loaded,
            references_exist,
            artifact_digests_match,
            row_counts_match,
            records_digest_match,
            quality_digest_match,
            schemas_supported,
            tuple(failures),
        )

    def inspect_dataset(self, dataset_id: str) -> dict[str, object]:
        manifest = self.load_manifest(dataset_id)
        quality = load_toml(
            self.dataset_directory(dataset_id) / "quality_report.toml"
        )
        return {
            "dataset_directory": str(self.dataset_directory(dataset_id).resolve()),
            "manifest": manifest.as_dict(),
            "quality_report": quality,
            "replay_capability": self.replay_decision(dataset_id).as_dict(),
            "verification": self.verify_dataset(dataset_id).as_dict(),
        }

    def replay_decision(self, dataset_id: str) -> ReplayCapabilityDecision:
        manifest = self.load_manifest(dataset_id)
        reasons = _replay_reasons(manifest.capability, manifest.exact_replay_allowed)
        return ReplayCapabilityDecision(
            manifest.replay_mode,
            manifest.exact_replay_allowed,
            reasons,
        )


def _replay_reasons(
    capability: SourceCapability,
    exact_replay_allowed: bool,
) -> tuple[str, ...]:
    if exact_replay_allowed:
        return (
            "source declares market-by-order messages",
            "stored quality gate supports exact ordered replay",
        )
    return {
        SourceCapability.BARS_ONLY: (
            "bars do not provide actual trades, quotes, Level 2 state, or queue events",
            "exact Level 2 replay refused",
        ),
        SourceCapability.TRADES: (
            "trade prints do not provide Level 2 state or queue events",
            "exact Level 2 replay refused",
        ),
        SourceCapability.TRADES_AND_QUOTES: (
            "trades and quotes do not provide Level 2 queues or order messages",
            "exact Level 2 replay refused",
        ),
        SourceCapability.LEVEL2_SNAPSHOTS: (
            "snapshots omit events between observations",
            "historical reconstruction is not exact replay",
        ),
        SourceCapability.LEVEL2_DELTAS: (
            "market-by-price deltas omit individual queue identity",
            "historical reconstruction is not exact replay",
        ),
        SourceCapability.MARKET_BY_ORDER: (
            "market-by-order quality or sequence gate failed",
            "exact Level 2 replay refused",
        ),
    }[capability]


def _write_records(
    path: Path,
    dataset_id: str,
    records: tuple[NormalizedMarketRecord, ...],
) -> None:
    columns = (
        ("dataset_id", "VARCHAR"),
        ("schema_version", "INTEGER"),
        ("record_sequence", "BIGINT"),
        ("record_type", "VARCHAR"),
        ("source_row", "BIGINT"),
        ("source_timestamp", "VARCHAR"),
        ("normalized_timestamp_ns", "BIGINT"),
        ("source_timezone", "VARCHAR"),
        ("timestamp_precision", "VARCHAR"),
        ("source_sequence", "BIGINT"),
        ("symbol", "VARCHAR"),
        ("session_id", "VARCHAR"),
        ("fields_toml", "VARCHAR"),
    )
    rows = tuple(
        (
            dataset_id,
            MARKET_DATA_SCHEMA_VERSION,
            sequence,
            item.record_type.value,
            item.source_row,
            item.source_timestamp,
            item.normalized_timestamp_ns,
            item.source_timezone,
            item.timestamp_precision.value,
            item.source_sequence,
            item.symbol,
            item.session_id,
            encode_payload({"fields": item.fields}),
        )
        for sequence, item in enumerate(records, start=1)
    )
    _write_parquet(path, columns, rows)


def _write_issues(
    path: Path,
    dataset_id: str,
    report: DataQualityReport,
) -> None:
    columns = (
        ("dataset_id", "VARCHAR"),
        ("schema_version", "INTEGER"),
        ("issue_sequence", "BIGINT"),
        ("severity", "VARCHAR"),
        ("code", "VARCHAR"),
        ("source_rows_toml", "VARCHAR"),
        ("message", "VARCHAR"),
    )
    issues = (*report.warnings, *report.rejections)
    rows = tuple(
        (
            dataset_id,
            DATA_QUALITY_SCHEMA_VERSION,
            sequence,
            issue.severity.value,
            issue.code,
            encode_payload({"source_rows": list(issue.source_rows)}),
            issue.message,
        )
        for sequence, issue in enumerate(issues, start=1)
    )
    _write_parquet(path, columns, rows)


def _write_parquet(
    path: Path,
    columns: tuple[tuple[str, str], ...],
    rows: tuple[tuple[object, ...], ...],
) -> None:
    duckdb = _duckdb()
    connection = duckdb.connect(":memory:")
    try:
        definition = ", ".join(f'"{name}" {kind}' for name, kind in columns)
        connection.execute(f"CREATE TABLE facts ({definition})")
        if rows:
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO facts VALUES ({placeholders})",
                rows,
            )
        escaped = str(path.resolve()).replace("'", "''")
        connection.execute(
            f"COPY facts TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()


def _read_records(path: Path) -> tuple[NormalizedMarketRecord, ...]:
    duckdb = _duckdb()
    connection = duckdb.connect(":memory:")
    try:
        cursor = connection.execute(
            "SELECT * FROM read_parquet(?) ORDER BY record_sequence",
            [str(path.resolve())],
        )
        columns = tuple(item[0] for item in cursor.description)
        rows = tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())
    finally:
        connection.close()
    records: list[NormalizedMarketRecord] = []
    for row in rows:
        if int(row["schema_version"]) != MARKET_DATA_SCHEMA_VERSION:
            raise ValueError("unsupported normalized record schema")
        decoded = decode_payload(str(row["fields_toml"]))
        fields = decoded.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("normalized record fields payload is invalid")
        records.append(
            NormalizedMarketRecord(
                record_type=RecordType(str(row["record_type"])),
                source_row=int(row["source_row"]),
                source_timestamp=str(row["source_timestamp"]),
                normalized_timestamp_ns=int(row["normalized_timestamp_ns"]),
                source_timezone=str(row["source_timezone"]),
                timestamp_precision=TimestampPrecision(
                    str(row["timestamp_precision"])
                ),
                source_sequence=None
                if row["source_sequence"] is None
                else int(row["source_sequence"]),
                symbol=str(row["symbol"]),
                session_id=str(row["session_id"]),
                fields=fields,
            )
        )
    return tuple(records)


def _parquet_count(path: Path) -> int:
    duckdb = _duckdb()
    connection = duckdb.connect(":memory:")
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)",
                [str(path.resolve())],
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _parquet_schema_versions(path: Path) -> set[int]:
    duckdb = _duckdb()
    connection = duckdb.connect(":memory:")
    try:
        return {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT schema_version FROM read_parquet(?)",
                [str(path.resolve())],
            ).fetchall()
        }
    finally:
        connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _duckdb():
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError("DuckDB is required for market-data storage") from error
    return duckdb
