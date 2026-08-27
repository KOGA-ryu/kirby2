"""Immutable per-run artifact store with a rebuildable DuckDB catalog."""

from __future__ import annotations

import tempfile
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kirby2.session.live import LiveMarketSession
from kirby2.session.replay import RECORDING_SCHEMA_VERSION, SessionRecording, replay_recording

from .models import (
    RUN_CONFIGURATION_SCHEMA_VERSION,
    RUN_MANIFEST_SCHEMA_VERSION,
    TABLE_SCHEMA_VERSION,
    ArtifactReference,
    RunManifest,
    RunType,
)
from .runtime import (
    configuration_toml,
    extract_session_tables,
    flow_model_id,
    git_commit,
    load_recording_from_artifacts,
    market_profile_id,
    recording_result_digest,
    software_version,
    strategy_id,
)
from .tables import (
    TABLE_SPECS,
    attach_run_id,
    evidence_digest,
    read_parquet_table,
    write_parquet_tables,
)
from .toml_codec import canonical_digest, file_sha256, load_toml


DEFAULT_RESEARCH_STORE = Path(".kirby2") / "research"
SUPPORTED_SCHEMA_VERSIONS = {
    "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
    "run_configuration": RUN_CONFIGURATION_SCHEMA_VERSION,
    "session_recording": RECORDING_SCHEMA_VERSION,
    **{f"table.{spec.name}": TABLE_SCHEMA_VERSION for spec in TABLE_SPECS},
}


@dataclass(frozen=True, slots=True)
class VerificationReport:
    run_id: str
    manifest_loaded: bool
    references_exist: bool
    artifact_digests_match: bool
    artifact_row_counts_match: bool
    event_sequence_complete: bool
    replay_configuration_available: bool
    replay_passed: bool
    result_digest_match: bool
    evidence_digest_match: bool
    schema_versions_supported: bool
    run_identity_match: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.manifest_loaded,
                self.references_exist,
                self.artifact_digests_match,
                self.artifact_row_counts_match,
                self.event_sequence_complete,
                self.replay_configuration_available,
                self.replay_passed,
                self.result_digest_match,
                self.evidence_digest_match,
                self.schema_versions_supported,
                self.run_identity_match,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests_match": self.artifact_digests_match,
            "artifact_row_counts_match": self.artifact_row_counts_match,
            "event_sequence_complete": self.event_sequence_complete,
            "evidence_digest_match": self.evidence_digest_match,
            "failures": list(self.failures),
            "manifest_loaded": self.manifest_loaded,
            "references_exist": self.references_exist,
            "replay_configuration_available": self.replay_configuration_available,
            "replay_passed": self.replay_passed,
            "result_digest_match": self.result_digest_match,
            "run_id": self.run_id,
            "run_identity_match": self.run_identity_match,
            "schema_versions_supported": self.schema_versions_supported,
            "status": "PASS" if self.passed else "FAIL",
        }

    def render(self) -> str:
        lines = [
            f"KIRBY2_VERIFY_RUN run_id={self.run_id}",
            *(
                f"{key.upper()} {str(value).lower()}"
                for key, value in self.as_dict().items()
                if isinstance(value, bool)
            ),
        ]
        lines.extend(f"FAILURE {failure}" for failure in self.failures)
        lines.append(
            f"VERIFY_RUN {'PASS' if self.passed else 'FAIL'} failures={len(self.failures)}"
        )
        return "\n".join(lines)


class RunStore:
    def __init__(self, root: Path = DEFAULT_RESEARCH_STORE) -> None:
        self.root = root
        self.runs_directory = self.root / "runs"
        self.staging_directory = self.root / ".staging"
        self.catalog_path = self.root / "catalog.duckdb"
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)

    def record_session(
        self,
        recording: SessionRecording,
        session: LiveMarketSession,
        *,
        parent_run_id: str | None = None,
        repository: Path | None = None,
    ) -> RunManifest:
        session.engine.book.assert_invariants()
        if session.state_sha256() != recording.expected_state_sha256:
            raise ValueError("session state does not match captured recording")
        if session.timeline_sha256() != recording.expected_timeline_sha256:
            raise ValueError("session timeline does not match captured recording")
        replay = replay_recording(recording)
        if not replay.passed:
            raise RuntimeError("session recording failed deterministic replay before persistence")
        configuration = configuration_toml(recording, session)
        configuration_digest = _sha256_text(configuration)
        raw_tables = extract_session_tables(recording, session)
        facts_digest = evidence_digest(raw_tables)
        schema_versions = dict(SUPPORTED_SCHEMA_VERSIONS)
        objective = (
            "NONE"
            if recording.objective is None
            else recording.objective.objective_type.value
        )
        scenario_id = str(recording.scenario_definition.get("name", "UNKNOWN"))
        input_references = tuple(
            str(row["dataset_reference"])
            for row in raw_tables["data_provenance"]
        )
        repository_path = repository or Path(__file__).resolve().parents[2]
        probe = RunManifest.create(
            parent_run_id=parent_run_id,
            run_type=RunType.SIMULATION,
            scenario_id=scenario_id,
            lesson_id=(
                None
                if recording.curriculum_drill is None
                else recording.curriculum_drill.lesson_id
            ),
            seed=recording.seed,
            flow_model=flow_model_id(session),
            market_profile=market_profile_id(recording),
            strategy_id=strategy_id(recording.strategy_source),
            hotkey_layout_id=recording.layout.name,
            session_objective=objective,
            simulation_start_us=0,
            simulation_end_us=recording.completed_time_us,
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions=schema_versions,
            input_dataset_references=input_references,
            configuration_digest=configuration_digest,
            evidence_digest=facts_digest,
            result_digest=recording_result_digest(recording),
            creation_timestamp_utc=_utc_now(),
            artifacts=(),
        )
        target = self.run_directory(probe.run_id)
        if target.exists():
            existing = self.load_manifest(probe.run_id)
            if existing.identity_dict() != probe.identity_dict():
                raise RuntimeError(
                    "content-derived run ID collision with different immutable identity"
                )
            verification = self.verify_run(probe.run_id)
            if not verification.passed:
                raise RuntimeError(
                    "existing immutable run is invalid and will not be overwritten: "
                    + "; ".join(verification.failures)
                )
            self.refresh_catalog()
            return existing
        tables = attach_run_id(probe.run_id, raw_tables)
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix=f"{probe.run_id}-",
        ) as temporary:
            staging = Path(temporary) / probe.run_id
            staging.mkdir()
            configuration_path = staging / "configuration.toml"
            configuration_path.write_text(configuration, encoding="utf-8")
            table_counts = write_parquet_tables(staging / "tables", tables)
            artifacts = [
                ArtifactReference(
                    name="configuration",
                    relative_path="configuration.toml",
                    sha256=file_sha256(configuration_path),
                    schema_version=RUN_CONFIGURATION_SCHEMA_VERSION,
                    row_count=None,
                    media_type="application/toml",
                )
            ]
            for spec in TABLE_SPECS:
                relative_path = f"tables/{spec.name}.parquet"
                artifacts.append(
                    ArtifactReference(
                        name=spec.name,
                        relative_path=relative_path,
                        sha256=file_sha256(staging / relative_path),
                        schema_version=spec.schema_version,
                        row_count=table_counts[spec.name],
                        media_type="application/vnd.apache.parquet",
                    )
                )
            manifest = RunManifest.create(
                parent_run_id=probe.parent_run_id,
                run_type=probe.run_type,
                scenario_id=probe.scenario_id,
                lesson_id=probe.lesson_id,
                seed=probe.seed,
                flow_model=probe.flow_model,
                market_profile=probe.market_profile,
                strategy_id=probe.strategy_id,
                hotkey_layout_id=probe.hotkey_layout_id,
                session_objective=probe.session_objective,
                simulation_start_us=probe.simulation_start_us,
                simulation_end_us=probe.simulation_end_us,
                software_version=probe.software_version,
                git_commit=probe.git_commit,
                schema_versions=probe.schema_versions,
                input_dataset_references=probe.input_dataset_references,
                configuration_digest=probe.configuration_digest,
                evidence_digest=probe.evidence_digest,
                result_digest=probe.result_digest,
                creation_timestamp_utc=probe.creation_timestamp_utc,
                artifacts=tuple(artifacts),
            )
            (staging / "manifest.toml").write_text(
                manifest.to_toml(),
                encoding="utf-8",
            )
            staging.rename(target)
        verification = self.verify_run(probe.run_id)
        if not verification.passed:
            raise RuntimeError(
                "new immutable run failed verification: "
                + "; ".join(verification.failures)
            )
        self.refresh_catalog()
        return self.load_manifest(probe.run_id)

    def run_directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"run-[0-9a-f]{24}", run_id):
            raise ValueError("invalid run ID")
        return self.runs_directory / run_id

    def load_manifest(self, run_id: str) -> RunManifest:
        path = self.run_directory(run_id) / "manifest.toml"
        if not path.is_file():
            raise ValueError(f"unknown run ID: {run_id}")
        return RunManifest.from_dict(load_toml(path))

    def load_recording(self, run_id: str) -> SessionRecording:
        directory = self.run_directory(run_id)
        configuration = load_toml(directory / "configuration.toml")
        return load_recording_from_artifacts(configuration, directory / "tables")

    def verify_run(self, run_id: str) -> VerificationReport:
        failures: list[str] = []
        manifest_loaded = False
        references_exist = False
        artifact_digests_match = False
        row_counts_match = False
        event_sequence_complete = False
        replay_configuration_available = False
        replay_passed = False
        result_digest_match = False
        evidence_digest_match = False
        schema_versions_supported = False
        run_identity_match = False
        try:
            manifest = self.load_manifest(run_id)
            manifest_loaded = True
            run_identity_match = manifest.run_id == run_id
            if not run_identity_match:
                failures.append("manifest run ID does not match requested directory")
        except (OSError, TypeError, ValueError) as error:
            failures.append(f"manifest invalid: {error}")
            return VerificationReport(
                run_id,
                manifest_loaded,
                references_exist,
                artifact_digests_match,
                row_counts_match,
                event_sequence_complete,
                replay_configuration_available,
                replay_passed,
                result_digest_match,
                evidence_digest_match,
                schema_versions_supported,
                run_identity_match,
                tuple(failures),
            )
        directory = self.run_directory(run_id)
        expected_artifact_schemas = {
            "configuration": RUN_CONFIGURATION_SCHEMA_VERSION,
            **{spec.name: spec.schema_version for spec in TABLE_SPECS},
        }
        actual_artifact_schemas = {
            item.name: item.schema_version for item in manifest.artifacts
        }
        schema_versions_supported = (
            manifest.schema_versions == SUPPORTED_SCHEMA_VERSIONS
            and actual_artifact_schemas == expected_artifact_schemas
        )
        if not schema_versions_supported:
            failures.append("manifest contains missing, unknown, or unsupported schemas")
        paths = [(item, directory / item.relative_path) for item in manifest.artifacts]
        references_exist = all(path.is_file() for _item, path in paths)
        if not references_exist:
            failures.append("one or more manifest artifact references are missing")
        if references_exist:
            artifact_digests_match = all(
                file_sha256(path) == item.sha256 for item, path in paths
            )
            if not artifact_digests_match:
                failures.append("one or more immutable artifact digests differ")
            try:
                row_counts_match = all(
                    item.row_count is None
                    or len(read_parquet_table(path)) == item.row_count
                    for item, path in paths
                )
            except Exception as error:
                row_counts_match = False
                failures.append(f"artifact row count could not be read: {error}")
            if not row_counts_match and not any(
                value.startswith("artifact row count") for value in failures
            ):
                failures.append("one or more Parquet row counts differ")
        configuration_path = directory / "configuration.toml"
        if configuration_path.is_file():
            if file_sha256(configuration_path) != manifest.configuration_digest:
                failures.append("configuration digest does not match manifest")
        else:
            failures.append("replay configuration is missing")
        table_payload: dict[str, list[dict[str, Any]]] = {}
        try:
            for spec in TABLE_SPECS:
                rows = read_parquet_table(directory / "tables" / f"{spec.name}.parquet")
                if any(row.get("run_id") != run_id for row in rows):
                    raise ValueError(f"{spec.name} contains a foreign run ID")
                table_payload[spec.name] = [
                    {key: value for key, value in row.items() if key != "run_id"}
                    for row in rows
                ]
            evidence_digest_match = evidence_digest(table_payload) == manifest.evidence_digest
            if not evidence_digest_match:
                failures.append("canonical table evidence digest differs")
            event_sequences = [
                int(row["event_sequence"])
                for row in sorted(
                    table_payload["events"],
                    key=lambda item: int(item["event_sequence"]),
                )
            ]
            event_sequence_complete = event_sequences == list(
                range(1, len(event_sequences) + 1)
            )
            if not event_sequence_complete:
                failures.append("event sequence is not complete and contiguous")
        except Exception as error:
            failures.append(f"canonical tables invalid: {error}")
        try:
            recording = self.load_recording(run_id)
            replay_configuration_available = True
            replay = replay_recording(recording)
            replay_passed = replay.passed
            actual_result = canonical_digest(
                {
                    "state_sha256": replay.session.state_sha256(),
                    "timeline_sha256": replay.session.timeline_sha256(),
                }
            )
            result_digest_match = (
                replay.passed
                and actual_result == manifest.result_digest
                and recording_result_digest(recording) == manifest.result_digest
            )
            if not replay_passed:
                failures.append("stored replay did not reproduce recorded state")
            if not result_digest_match:
                failures.append("replayed result digest differs from manifest")
        except Exception as error:
            failures.append(f"replay configuration or execution invalid: {error}")
        return VerificationReport(
            run_id,
            manifest_loaded,
            references_exist,
            artifact_digests_match,
            row_counts_match,
            event_sequence_complete,
            replay_configuration_available,
            replay_passed,
            result_digest_match,
            evidence_digest_match,
            schema_versions_supported,
            run_identity_match,
            tuple(failures),
        )

    def refresh_catalog(self) -> None:
        with self._catalog_lock():
            self._refresh_catalog_unlocked()

    def _refresh_catalog_unlocked(self) -> None:
        duckdb = _duckdb()
        manifests = [
            (RunManifest.from_dict(load_toml(path)), file_sha256(path))
            for path in sorted(self.runs_directory.glob("run-*/manifest.toml"))
        ]
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix="catalog-",
        ) as temporary:
            temporary_catalog = Path(temporary) / "catalog.duckdb"
            connection = duckdb.connect(str(temporary_catalog))
            try:
                _drop_catalog_views(connection)
                connection.execute("DROP TABLE IF EXISTS run_registry")
                connection.execute(
                    """
                    CREATE TABLE run_registry (
                        run_id VARCHAR PRIMARY KEY,
                        parent_run_id VARCHAR,
                        run_type VARCHAR NOT NULL,
                        scenario_id VARCHAR,
                        lesson_id VARCHAR,
                        seed BIGINT,
                        flow_model VARCHAR NOT NULL,
                        market_profile VARCHAR NOT NULL,
                        strategy_id VARCHAR NOT NULL,
                        hotkey_layout_id VARCHAR NOT NULL,
                        session_objective VARCHAR NOT NULL,
                        simulation_start_us BIGINT NOT NULL,
                        simulation_end_us BIGINT NOT NULL,
                        software_version VARCHAR NOT NULL,
                        git_commit VARCHAR NOT NULL,
                        configuration_digest VARCHAR NOT NULL,
                        evidence_digest VARCHAR NOT NULL,
                        result_digest VARCHAR NOT NULL,
                        creation_timestamp_utc VARCHAR NOT NULL,
                        manifest_sha256 VARCHAR NOT NULL
                    )
                    """
                )
                if manifests:
                    connection.executemany(
                        "INSERT INTO run_registry VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                item.run_id,
                                item.parent_run_id,
                                item.run_type.value,
                                item.scenario_id,
                                item.lesson_id,
                                item.seed,
                                item.flow_model,
                                item.market_profile,
                                item.strategy_id,
                                item.hotkey_layout_id,
                                item.session_objective,
                                item.simulation_start_us,
                                item.simulation_end_us,
                                item.software_version,
                                item.git_commit,
                                item.configuration_digest,
                                item.evidence_digest,
                                item.result_digest,
                                item.creation_timestamp_utc,
                                manifest_sha256,
                            )
                            for item, manifest_sha256 in manifests
                        ],
                    )
                _create_fact_views(connection, self.runs_directory, bool(manifests))
                _create_summary_views(connection)
            finally:
                connection.close()
            temporary_catalog.replace(self.catalog_path)

    def _ensure_catalog(self) -> None:
        if self._catalog_is_current():
            return
        with self._catalog_lock():
            if not self._catalog_is_current():
                self._refresh_catalog_unlocked()

    def _catalog_is_current(self) -> bool:
        if not self.catalog_path.is_file():
            return False
        expected = {
            path.parent.name: file_sha256(path)
            for path in self.runs_directory.glob("run-*/manifest.toml")
        }
        duckdb = _duckdb()
        try:
            connection = duckdb.connect(str(self.catalog_path), read_only=True)
            try:
                actual = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT run_id, manifest_sha256 FROM run_registry"
                    ).fetchall()
                }
            finally:
                connection.close()
        except Exception:
            return False
        return actual == expected

    @contextmanager
    def _catalog_lock(self):
        import fcntl

        lock_path = self.root / "catalog.lock"
        with lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def query_runs(self, scenario_id: str | None = None) -> tuple[dict[str, Any], ...]:
        self._ensure_catalog()
        duckdb = _duckdb()
        connection = duckdb.connect(str(self.catalog_path), read_only=True)
        try:
            if scenario_id is None:
                cursor = connection.execute(
                    "SELECT * FROM run_summary ORDER BY creation_timestamp_utc, run_id"
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM run_summary WHERE scenario_id = ? "
                    "ORDER BY creation_timestamp_utc, run_id",
                    [scenario_id],
                )
            columns = tuple(item[0] for item in cursor.description)
            return tuple(
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            )
        finally:
            connection.close()

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        self._ensure_catalog()
        duckdb = _duckdb()
        connection = duckdb.connect(str(self.catalog_path), read_only=True)
        try:
            cursor = connection.execute(
                "SELECT * FROM run_summary WHERE run_id = ?",
                [run_id],
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"unknown run ID: {run_id}")
            columns = tuple(item[0] for item in cursor.description)
            summary = dict(zip(columns, row, strict=True))
        finally:
            connection.close()
        manifest = self.load_manifest(run_id)
        return {
            "manifest": manifest.as_dict(),
            "run_directory": str(self.run_directory(run_id).resolve()),
            "summary": summary,
            "verification": self.verify_run(run_id).as_dict(),
        }


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _duckdb():
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError(
            "DuckDB is required for the research store; install project dependencies"
        ) from error
    return duckdb


_CATALOG_VIEWS = (
    "invariant_violations",
    "experiment_comparison",
    "historical_lesson_summary",
    "scenario_summary",
    "strategy_summary",
    "execution_summary",
    "run_summary",
    *(f"all_{spec.name}" for spec in TABLE_SPECS),
)


def _drop_catalog_views(connection) -> None:
    for name in _CATALOG_VIEWS:
        connection.execute(f'DROP VIEW IF EXISTS "{name}"')


def _create_fact_views(connection, runs_directory: Path, has_runs: bool) -> None:
    for spec in TABLE_SPECS:
        view_name = f"all_{spec.name}"
        if has_runs:
            pattern = str(
                (runs_directory / "*" / "tables" / f"{spec.name}.parquet").resolve()
            ).replace("'", "''")
            connection.execute(
                f'CREATE VIEW "{view_name}" AS '
                f"SELECT * FROM read_parquet('{pattern}', union_by_name = true)"
            )
        else:
            expressions = ", ".join(
                f'CAST(NULL AS {sql_type}) AS "{name}"'
                for name, sql_type in spec.columns
            )
            connection.execute(
                f'CREATE VIEW "{view_name}" AS SELECT {expressions} WHERE false'
            )


def _create_summary_views(connection) -> None:
    connection.execute(
        """
        CREATE VIEW run_summary AS
        SELECT r.*,
               COALESCE(e.event_count, 0) AS event_count,
               COALESCE(t.trade_count, 0) AS trade_count,
               COALESCE(t.traded_volume, 0) AS traded_volume,
               COALESCE(a.player_action_count, 0) AS player_action_count
        FROM run_registry r
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS event_count FROM all_events GROUP BY run_id
        ) e USING (run_id)
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS trade_count,
                   COALESCE(SUM(quantity), 0) AS traded_volume
            FROM all_trades GROUP BY run_id
        ) t USING (run_id)
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS player_action_count
            FROM all_player_actions GROUP BY run_id
        ) a USING (run_id)
        """
    )
    connection.execute(
        """
        CREATE VIEW execution_summary AS
        SELECT r.run_id, r.scenario_id, r.seed, r.session_objective,
               r.trade_count, r.traded_volume, r.player_action_count,
               COALESCE(f.player_fill_count, 0) AS player_fill_count,
               COALESCE(f.player_fill_quantity, 0) AS player_fill_quantity,
               COALESCE(s.score_rows, 0) AS score_rows
        FROM run_summary r
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS player_fill_count,
                   COALESCE(SUM(quantity), 0) AS player_fill_quantity
            FROM all_fills WHERE owner = 'player' GROUP BY run_id
        ) f USING (run_id)
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS score_rows FROM all_scores GROUP BY run_id
        ) s USING (run_id)
        """
    )
    connection.execute(
        """
        CREATE VIEW strategy_summary AS
        SELECT r.run_id, r.strategy_id,
               COALESCE(st.state_count, 0) AS state_count,
               COALESCE(tr.transition_count, 0) AS transition_count
        FROM run_registry r
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS state_count
            FROM all_strategy_states GROUP BY run_id
        ) st USING (run_id)
        LEFT JOIN (
            SELECT run_id, COUNT(*) AS transition_count
            FROM all_traffic_light_transitions GROUP BY run_id
        ) tr USING (run_id)
        """
    )
    connection.execute(
        """
        CREATE VIEW scenario_summary AS
        SELECT scenario_id, COUNT(*) AS run_count,
               SUM(event_count) AS event_count,
               SUM(trade_count) AS trade_count,
               SUM(traded_volume) AS traded_volume
        FROM run_summary
        WHERE scenario_id IS NOT NULL
        GROUP BY scenario_id
        """
    )
    connection.execute(
        """
        CREATE VIEW historical_lesson_summary AS
        SELECT run_id, lesson_id, scenario_id, seed, result_digest,
               event_count, trade_count, traded_volume
        FROM run_summary WHERE lesson_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE VIEW experiment_comparison AS
        SELECT e.experiment_id, e.variant_id, e.comparison_group,
               e.run_id, s.score_name, s.score_value, s.status
        FROM all_experiment_membership e
        LEFT JOIN all_scores s USING (run_id)
        """
    )
    connection.execute(
        """
        CREATE VIEW invariant_violations AS
        SELECT run_id, event_sequence, simulation_time_us,
               event_type, payload_toml
        FROM all_events
        WHERE event_type = 'INVARIANT_VIOLATION'
        """
    )
