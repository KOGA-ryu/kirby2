"""Executable audit for immutable run-ledger and research-store behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from kirby2.research import RunStore
from kirby2.research.tables import TABLE_SPECS, read_parquet_table
from kirby2.research.toml_codec import file_sha256
from kirby2.scenarios import get_scenario_definition
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.objectives import ObjectiveType, SessionObjective
from kirby2.session.replay import SessionRecording, replay_recording


@dataclass(frozen=True, slots=True)
class RunStoreAuditCase:
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


def audit_run_store() -> tuple[RunStoreAuditCase, ...]:
    return (
        _restart_replay_case(),
        _content_identity_case(),
        _immutable_tamper_case(),
        _event_sequence_tamper_case(),
        _schema_rejection_case(),
        _tables_and_views_case(),
    )


def _restart_replay_case() -> RunStoreAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        session, recording = _completed_session()
        manifest = RunStore(root).record_session(recording, session)
        restarted = RunStore(root)
        loaded = restarted.load_recording(manifest.run_id)
        replay = replay_recording(loaded)
        verification = restarted.verify_run(manifest.run_id)
        summary = restarted.inspect_run(manifest.run_id)["summary"]
        if not verification.passed or not replay.passed:
            failures.append("restarted store did not verify and replay exactly")
        if replay.session.state_sha256() != recording.expected_state_sha256:
            failures.append("restarted replay state digest diverged")
        if summary["result_digest"] != manifest.result_digest:
            failures.append("restarted DuckDB summary lost result linkage")
        evidence = {
            "event_count": summary["event_count"],
            "player_action_count": summary["player_action_count"],
            "replay_status": "PASS" if replay.passed else "FAIL",
            "result_digest": manifest.result_digest,
            "run_id": manifest.run_id,
            "verification_status": "PASS" if verification.passed else "FAIL",
        }
    return RunStoreAuditCase(
        "persist_close_load_verify_and_replay",
        evidence,
        tuple(failures),
    )


def _content_identity_case() -> RunStoreAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = RunStore(root)
        first_session, first_recording = _completed_session()
        first = store.record_session(first_recording, first_session)
        manifest_bytes = (store.run_directory(first.run_id) / "manifest.toml").read_bytes()
        second_session, second_recording = _completed_session()
        second = store.record_session(second_recording, second_session)
        third_session, third_recording = _completed_session(seed=43)
        third = store.record_session(third_recording, third_session)
        run_directories = tuple(root.glob("runs/run-*"))
        if first.run_id != second.run_id:
            failures.append("identical completed identity produced a new run ID")
        if first.run_id == third.run_id:
            failures.append("different seeded result reused an existing run ID")
        if (store.run_directory(first.run_id) / "manifest.toml").read_bytes() != manifest_bytes:
            failures.append("idempotent record attempt overwrote immutable manifest")
        if len(run_directories) != 2:
            failures.append("idempotent record attempt created duplicate directories")
        evidence = {
            "different_seed_run_id": third.run_id,
            "identical_run_id": first.run_id,
            "manifest_unchanged": (
                store.run_directory(first.run_id) / "manifest.toml"
            ).read_bytes()
            == manifest_bytes,
            "run_directory_count": len(run_directories),
        }
    return RunStoreAuditCase(
        "content_identity_is_predictable_and_idempotent",
        evidence,
        tuple(failures),
    )


def _immutable_tamper_case() -> RunStoreAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        session, recording = _completed_session()
        store = RunStore(root)
        manifest = store.record_session(recording, session)
        configuration = store.run_directory(manifest.run_id) / "configuration.toml"
        configuration.write_text(
            configuration.read_text(encoding="utf-8") + "# tampered\n",
            encoding="utf-8",
        )
        tampered_sha = file_sha256(configuration)
        verification = store.verify_run(manifest.run_id)
        overwrite_rejected = False
        try:
            store.record_session(recording, session)
        except RuntimeError:
            overwrite_rejected = True
        if verification.passed or verification.artifact_digests_match:
            failures.append("configuration tamper was not detected")
        if not overwrite_rejected:
            failures.append("invalid existing run was silently overwritten")
        if file_sha256(configuration) != tampered_sha:
            failures.append("failed overwrite attempt changed tampered evidence")
        evidence = {
            "artifact_digest_match": verification.artifact_digests_match,
            "configuration_digest_match": tampered_sha == manifest.configuration_digest,
            "overwrite_rejected": overwrite_rejected,
            "verification_status": "PASS" if verification.passed else "FAIL_EXPECTED",
        }
    return RunStoreAuditCase(
        "tamper_detected_and_overwrite_refused",
        evidence,
        tuple(failures),
    )


def _event_sequence_tamper_case() -> RunStoreAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        session, recording = _completed_session()
        store = RunStore(root)
        manifest = store.record_session(recording, session)
        events_path = store.run_directory(manifest.run_id) / "tables" / "events.parquet"
        replacement = events_path.with_suffix(".replacement.parquet")
        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            source_sql = str(events_path).replace("'", "''")
            replacement_sql = str(replacement).replace("'", "''")
            connection.execute(
                f"COPY (SELECT * FROM read_parquet('{source_sql}') "
                "WHERE event_sequence <> 2) "
                f"TO '{replacement_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            connection.close()
        replacement.replace(events_path)
        verification = store.verify_run(manifest.run_id)
        if verification.event_sequence_complete:
            failures.append("missing event sequence was accepted as complete")
        if verification.artifact_row_counts_match:
            failures.append("missing event row did not violate manifest row count")
        evidence = {
            "artifact_digest_match": verification.artifact_digests_match,
            "event_sequence_complete": verification.event_sequence_complete,
            "row_count_match": verification.artifact_row_counts_match,
        }
    return RunStoreAuditCase(
        "missing_event_sequence_fails_closed",
        evidence,
        tuple(failures),
    )


def _schema_rejection_case() -> RunStoreAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        session, recording = _completed_session()
        store = RunStore(root)
        manifest = store.record_session(recording, session)
        manifest_path = store.run_directory(manifest.run_id) / "manifest.toml"
        text = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(
            text.replace('"schema_version" = 1', '"schema_version" = 999', 1),
            encoding="utf-8",
        )
        verification = store.verify_run(manifest.run_id)
        if verification.schema_versions_supported or verification.passed:
            failures.append("unsupported manifest schema was silently interpreted")
        evidence = {
            "failure": verification.failures[0] if verification.failures else None,
            "manifest_loaded": verification.manifest_loaded,
            "schema_versions_supported": verification.schema_versions_supported,
            "verification_status": "PASS" if verification.passed else "FAIL_EXPECTED",
        }
    return RunStoreAuditCase(
        "unsupported_schema_is_detectable",
        evidence,
        tuple(failures),
    )


def _tables_and_views_case() -> RunStoreAuditCase:
    failures: list[str] = []
    with TemporaryDirectory() as directory:
        root = Path(directory)
        session, recording = _completed_session()
        store = RunStore(root)
        manifest = store.record_session(recording, session)
        table_artifacts = {
            item.name: item for item in manifest.artifacts if item.row_count is not None
        }
        required_tables = {spec.name for spec in TABLE_SPECS}
        if set(table_artifacts) != required_tables:
            failures.append("canonical Parquet table inventory is incomplete")
        if any(store.run_directory(manifest.run_id).rglob("*.json")):
            failures.append("run directory used JSON for an internal artifact")
        events = read_parquet_table(
            store.run_directory(manifest.run_id) / "tables" / "events.parquet"
        )
        actions = read_parquet_table(
            store.run_directory(manifest.run_id) / "tables" / "player_actions.parquet"
        )
        import duckdb

        connection = duckdb.connect(str(store.catalog_path), read_only=True)
        view_names = (
            "run_summary",
            "execution_summary",
            "strategy_summary",
            "scenario_summary",
            "historical_lesson_summary",
            "experiment_comparison",
            "invariant_violations",
        )
        try:
            view_counts = {
                name: int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in view_names
            }
        finally:
            connection.close()
        if any(name not in view_counts for name in view_names):
            failures.append("one or more required DuckDB views are missing")
        if not events or len(actions) != 1:
            failures.append("canonical event or player-action facts were not persisted")
        store.catalog_path.unlink()
        rebuilt_rows = store.query_runs("balanced")
        if len(rebuilt_rows) != 1 or not store.catalog_path.is_file():
            failures.append("deleted derived catalog was not rebuilt from immutable runs")
        evidence = {
            "catalog_rebuilt": len(rebuilt_rows) == 1,
            "event_rows": len(events),
            "json_artifact_count": len(
                tuple(store.run_directory(manifest.run_id).rglob("*.json"))
            ),
            "player_action_rows": len(actions),
            "table_count": len(table_artifacts),
            "view_counts": view_counts,
        }
    return RunStoreAuditCase(
        "canonical_parquet_layout_and_duckdb_views",
        evidence,
        tuple(failures),
    )


def _completed_session(
    *,
    seed: int = 42,
) -> tuple[LiveMarketSession, SessionRecording]:
    layout = HotkeyLayout.default()
    objective = SessionObjective(
        ObjectiveType.ACQUIRE,
        target_quantity=100,
        time_limit_us=1_000_000,
        preferred_slippage_ticks=2,
    )
    session = LiveMarketSession(
        get_scenario_definition("balanced"),
        seed=seed,
        duration_seconds=1,
        initial_quantity=100,
        objective=objective,
    )
    session.start()
    session.advance_by(500_000)
    session.handle_input("d", layout.bindings)
    session.advance_by(500_000)
    recording = SessionRecording.capture(session, layout, auto_start=True)
    return session, recording
