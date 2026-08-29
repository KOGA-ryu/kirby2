"""Canonical Parquet table schemas and deterministic DuckDB I/O."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import TABLE_SCHEMA_VERSION, ArtifactType, RunManifest
from .toml_codec import encode_payload


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    columns: tuple[tuple[str, str], ...]
    order_by: tuple[str, ...]
    schema_version: int = TABLE_SCHEMA_VERSION


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "events",
        (
            ("run_id", "VARCHAR"),
            ("event_sequence", "BIGINT"),
            ("simulation_time_us", "BIGINT"),
            ("event_type", "VARCHAR"),
            ("origin", "VARCHAR"),
            ("payload_toml", "VARCHAR"),
        ),
        ("event_sequence",),
    ),
    TableSpec(
        "book_snapshots",
        (
            ("run_id", "VARCHAR"),
            ("snapshot_id", "VARCHAR"),
            ("snapshot_kind", "VARCHAR"),
            ("simulation_time_us", "BIGINT"),
            ("observed_state_time_us", "BIGINT"),
            ("exchange_event_sequence", "BIGINT"),
            ("snapshot_toml", "VARCHAR"),
        ),
        ("simulation_time_us", "snapshot_kind", "snapshot_id"),
    ),
    TableSpec(
        "orders",
        (
            ("run_id", "VARCHAR"),
            ("order_id", "VARCHAR"),
            ("order_type", "VARCHAR"),
            ("owner", "VARCHAR"),
            ("side", "VARCHAR"),
            ("price_ticks", "BIGINT"),
            ("original_quantity", "BIGINT"),
            ("filled_quantity", "BIGINT"),
            ("remaining_quantity", "BIGINT"),
            ("cancelled_quantity", "BIGINT"),
            ("status", "VARCHAR"),
            ("resting_sequence", "BIGINT"),
            ("cancel_target_id", "VARCHAR"),
        ),
        ("order_id",),
    ),
    TableSpec(
        "fills",
        (
            ("run_id", "VARCHAR"),
            ("fill_sequence", "BIGINT"),
            ("trade_id", "VARCHAR"),
            ("order_id", "VARCHAR"),
            ("owner", "VARCHAR"),
            ("side", "VARCHAR"),
            ("price_ticks", "BIGINT"),
            ("quantity", "BIGINT"),
            ("liquidity", "VARCHAR"),
        ),
        ("fill_sequence",),
    ),
    TableSpec(
        "trades",
        (
            ("run_id", "VARCHAR"),
            ("trade_sequence", "BIGINT"),
            ("trade_id", "VARCHAR"),
            ("price_ticks", "BIGINT"),
            ("quantity", "BIGINT"),
            ("maker_order_id", "VARCHAR"),
            ("taker_order_id", "VARCHAR"),
            ("taker_side", "VARCHAR"),
        ),
        ("trade_sequence",),
    ),
    TableSpec(
        "player_actions",
        (
            ("run_id", "VARCHAR"),
            ("action_sequence", "BIGINT"),
            ("simulation_time_us", "BIGINT"),
            ("input_key", "VARCHAR"),
            ("resolved_command", "VARCHAR"),
            ("parameters_toml", "VARCHAR"),
            ("market_state_id", "VARCHAR"),
            ("latency_reference_time_us", "BIGINT"),
            ("action_latency_us", "BIGINT"),
            ("accepted", "BOOLEAN"),
            ("rejection_reason", "VARCHAR"),
            ("resulting_order_id", "VARCHAR"),
            ("resulting_order_ids_toml", "VARCHAR"),
        ),
        ("action_sequence",),
    ),
    TableSpec(
        "traffic_light_transitions",
        (
            ("run_id", "VARCHAR"),
            ("transition_sequence", "BIGINT"),
            ("simulation_time_us", "BIGINT"),
            ("message", "VARCHAR"),
            ("data_toml", "VARCHAR"),
        ),
        ("transition_sequence",),
    ),
    TableSpec(
        "strategy_states",
        (
            ("run_id", "VARCHAR"),
            ("state_sequence", "BIGINT"),
            ("simulation_time_us", "BIGINT"),
            ("message", "VARCHAR"),
            ("data_toml", "VARCHAR"),
        ),
        ("state_sequence",),
    ),
    TableSpec(
        "features",
        (
            ("run_id", "VARCHAR"),
            ("feature_sequence", "BIGINT"),
            ("simulation_time_us", "BIGINT"),
            ("feature_name", "VARCHAR"),
            ("window_us", "BIGINT"),
            ("value_text", "VARCHAR"),
            ("availability", "VARCHAR"),
            ("provenance", "VARCHAR"),
        ),
        ("feature_sequence",),
    ),
    TableSpec(
        "latency_messages",
        (
            ("run_id", "VARCHAR"),
            ("message_sequence", "BIGINT"),
            ("simulation_time_us", "BIGINT"),
            ("message_type", "VARCHAR"),
            ("status", "VARCHAR"),
            ("data_toml", "VARCHAR"),
        ),
        ("message_sequence",),
    ),
    TableSpec(
        "scores",
        (
            ("run_id", "VARCHAR"),
            ("score_name", "VARCHAR"),
            ("score_value", "VARCHAR"),
            ("status", "VARCHAR"),
            ("heuristic", "BOOLEAN"),
            ("explanation", "VARCHAR"),
            ("components_toml", "VARCHAR"),
        ),
        ("score_name",),
    ),
    TableSpec(
        "calibration_metrics",
        (
            ("run_id", "VARCHAR"),
            ("metric_name", "VARCHAR"),
            ("metric_value", "VARCHAR"),
            ("unit", "VARCHAR"),
            ("provenance", "VARCHAR"),
        ),
        ("metric_name",),
    ),
    TableSpec(
        "experiment_membership",
        (
            ("run_id", "VARCHAR"),
            ("experiment_id", "VARCHAR"),
            ("variant_id", "VARCHAR"),
            ("comparison_group", "VARCHAR"),
            ("parent_run_id", "VARCHAR"),
        ),
        ("experiment_id", "variant_id"),
    ),
    TableSpec(
        "data_provenance",
        (
            ("run_id", "VARCHAR"),
            ("provenance_sequence", "BIGINT"),
            ("dataset_reference", "VARCHAR"),
            ("capability", "VARCHAR"),
            ("provenance_type", "VARCHAR"),
            ("content_sha256", "VARCHAR"),
            ("notes", "VARCHAR"),
        ),
        ("provenance_sequence",),
    ),
)

TABLE_SPEC_BY_NAME = {spec.name: spec for spec in TABLE_SPECS}


RUN_ARTIFACT_REGISTRY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "VARCHAR"),
    ("artifact_type", "VARCHAR"),
    ("artifact_name", "VARCHAR"),
    ("relative_path", "VARCHAR"),
    ("sha256", "VARCHAR"),
    ("schema_version", "BIGINT"),
    ("row_count", "BIGINT"),
    ("media_type", "VARCHAR"),
)


FULL_DAY_QUALIFICATION_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.FULL_DAY_PROFILE_QUALIFICATION,
        ArtifactType.FULL_DAY_QUALIFICATION_RUN_PROOFS,
        ArtifactType.FULL_DAY_REVIEW_SOURCE,
        ArtifactType.FULL_DAY_REVIEW_SELECTION,
        ArtifactType.FULL_DAY_REVIEW_PACKET,
        ArtifactType.FULL_DAY_PERFORMANCE_EVIDENCE,
        ArtifactType.FULL_DAY_QUALIFICATION_LEDGER,
        ArtifactType.FULL_DAY_REVEAL_TOKEN,
        ArtifactType.FULL_DAY_REVIEWER_SIDECAR,
    }
)


LESSON_MINING_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.LESSON_MINING_SOURCE_MATRIX,
        ArtifactType.LESSON_MINING_SOURCE_VALIDATION,
        ArtifactType.LESSON_MINING_CANDIDATES,
        ArtifactType.LESSON_MINING_SELECTION,
        ArtifactType.LESSON_TECHNICAL_REVIEW_PACKET,
        ArtifactType.LESSON_REVIEW_SIDECAR,
        ArtifactType.LESSON_BUILD_PROPOSAL,
    }
)


LEARNER_ARTIFACT_TYPES = frozenset(
    {
        ArtifactType.LEARNER_EVIDENCE_UPDATE,
        ArtifactType.LEARNER_STATE_PROJECTION,
    }
)


def artifact_registry_rows(manifests: list[RunManifest]) -> list[tuple[object, ...]]:
    """Project typed immutable artifacts into the rebuildable research catalog."""

    if any(type(manifest) is not RunManifest for manifest in manifests):
        raise TypeError("artifact registry projection requires RunManifest values")
    return [
        (
            manifest.run_id,
            artifact.artifact_type.value,
            artifact.name,
            artifact.relative_path,
            artifact.sha256,
            artifact.schema_version,
            artifact.row_count,
            artifact.media_type,
        )
        for manifest in sorted(manifests, key=lambda item: item.run_id)
        for artifact in sorted(
            manifest.artifacts,
            key=lambda item: (item.artifact_type.value, item.name),
        )
    ]


def qualification_artifact_registry_rows(
    manifests: list[RunManifest],
) -> list[tuple[object, ...]]:
    """Project only explicitly typed qualification artifacts, never filenames."""

    return [
        row
        for row in artifact_registry_rows(manifests)
        if ArtifactType(str(row[1])) in FULL_DAY_QUALIFICATION_ARTIFACT_TYPES
    ]


def lesson_mining_artifact_registry_rows(
    manifests: list[RunManifest],
) -> list[tuple[object, ...]]:
    """Project only explicitly typed lesson mining, review, and build artifacts."""

    return [
        row
        for row in artifact_registry_rows(manifests)
        if ArtifactType(str(row[1])) in LESSON_MINING_ARTIFACT_TYPES
    ]


def learner_artifact_registry_rows(
    manifests: list[RunManifest],
) -> list[tuple[object, ...]]:
    """Project only typed learner evidence updates and derived projections."""

    return [
        row
        for row in artifact_registry_rows(manifests)
        if ArtifactType(str(row[1])) in LEARNER_ARTIFACT_TYPES
    ]


def attach_run_id(
    run_id: str,
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    _validate_table_inventory(tables)
    return {
        name: [{"run_id": run_id, **row} for row in rows]
        for name, rows in tables.items()
    }


def evidence_digest(tables: dict[str, list[dict[str, Any]]]) -> str:
    _validate_table_inventory(tables)
    canonical = encode_payload(
        {
            "tables": {
                name: {"rows": _sorted_rows(TABLE_SPEC_BY_NAME[name], rows)}
                for name, rows in sorted(tables.items())
            }
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_parquet_tables(
    directory: Path,
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    duckdb = _duckdb()
    _validate_table_inventory(tables)
    directory.mkdir(parents=True, exist_ok=False)
    counts: dict[str, int] = {}
    connection = duckdb.connect(":memory:")
    try:
        for spec in TABLE_SPECS:
            rows = _sorted_rows(spec, tables[spec.name])
            column_names = tuple(name for name, _sql_type in spec.columns)
            columns_sql = ", ".join(
                f'"{name}" {sql_type}' for name, sql_type in spec.columns
            )
            connection.execute(f'CREATE TABLE "facts" ({columns_sql})')
            if rows:
                placeholders = ", ".join("?" for _ in column_names)
                connection.executemany(
                    f'INSERT INTO "facts" VALUES ({placeholders})',
                    [tuple(row.get(name) for name in column_names) for row in rows],
                )
            path = directory / f"{spec.name}.parquet"
            escaped = str(path.resolve()).replace("'", "''")
            connection.execute(
                f"COPY facts TO '{escaped}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            connection.execute("DROP TABLE facts")
            counts[spec.name] = len(rows)
    finally:
        connection.close()
    return counts


def read_parquet_table(path: Path) -> list[dict[str, Any]]:
    duckdb = _duckdb()
    connection = duckdb.connect(":memory:")
    try:
        cursor = connection.execute(
            "SELECT * FROM read_parquet(?)",
            [str(path.resolve())],
        )
        columns = tuple(item[0] for item in cursor.description)
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()


def _sorted_rows(
    spec: TableSpec,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed = {name for name, _sql_type in spec.columns}
    allowed.discard("run_id")
    if any(set(row) - (allowed | {"run_id"}) for row in rows):
        raise ValueError(f"{spec.name} contains unsupported columns")
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: tuple(_sort_value(row.get(key)) for key in spec.order_by),
    )


def _sort_value(value: Any) -> tuple[int, Any]:
    return (0, 0) if value is None else (1, value)


def _validate_table_inventory(tables: dict[str, list[dict[str, Any]]]) -> None:
    expected = set(TABLE_SPEC_BY_NAME)
    if set(tables) != expected:
        missing = sorted(expected - set(tables))
        extra = sorted(set(tables) - expected)
        raise ValueError(f"canonical table inventory mismatch missing={missing} extra={extra}")


def _duckdb():
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError(
            "DuckDB is required for the research store; install project dependencies"
        ) from error
    return duckdb
