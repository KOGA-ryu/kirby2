"""Immutable per-run artifact store with a rebuildable DuckDB catalog."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kirby2.session.live import LiveMarketSession
from kirby2.session.replay import RECORDING_SCHEMA_VERSION, SessionRecording, replay_recording
from kirby2.pseudonyms import require_learner_profile_id

from .models import (
    RUN_CONFIGURATION_SCHEMA_VERSION,
    RUN_MANIFEST_SCHEMA_VERSION,
    TABLE_SCHEMA_VERSION,
    ArtifactReference,
    ArtifactType,
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
    RUN_ARTIFACT_REGISTRY_COLUMNS,
    TABLE_SPECS,
    artifact_registry_rows,
    attach_run_id,
    evidence_digest,
    learner_artifact_registry_rows,
    lesson_mining_artifact_registry_rows,
    read_parquet_table,
    qualification_artifact_registry_rows,
    strategy_discovery_artifact_registry_rows,
    write_parquet_tables,
)
from .toml_codec import canonical_digest, canonical_toml, file_sha256, load_toml

if TYPE_CHECKING:
    from kirby2.discovery.access import PartitionAccessRecordV1
    from kirby2.discovery.experiment import StrategyDiscoveryExperimentV1
    from kirby2.discovery.partitions import PartitionManifestV1


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
        self.strategy_discovery_directory = self.root / "strategy-discovery"
        self.strategy_partition_directory = (
            self.strategy_discovery_directory / "partitions"
        )
        self.strategy_experiment_state_directory = (
            self.strategy_discovery_directory / "states"
        )
        self.strategy_access_directory = (
            self.strategy_discovery_directory / "access"
        )
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)
        self.strategy_partition_directory.mkdir(parents=True, exist_ok=True)
        self.strategy_experiment_state_directory.mkdir(parents=True, exist_ok=True)
        self.strategy_access_directory.mkdir(parents=True, exist_ok=True)

    def record_strategy_partition_manifest(
        self,
        manifest: PartitionManifestV1,
    ) -> ArtifactReference:
        """Persist one canonical sealed-partition manifest without overwriting."""

        with self._strategy_discovery_lock():
            return self._record_strategy_partition_manifest_unlocked(manifest)

    def _record_strategy_partition_manifest_unlocked(
        self,
        manifest: PartitionManifestV1,
    ) -> ArtifactReference:

        from kirby2.discovery.partitions import PartitionManifestV1

        if not isinstance(manifest, PartitionManifestV1):
            raise TypeError("strategy partition artifact requires PartitionManifestV1")
        raw = manifest.canonical_bytes()
        path = self.strategy_partition_directory / f"{manifest.manifest_sha256}.json"
        for existing_path in sorted(self.strategy_partition_directory.glob("*.json")):
            existing = self.load_strategy_partition_manifest(existing_path.stem)
            if (
                existing.experiment_id == manifest.experiment_id
                and existing.experiment_version == manifest.experiment_version
                and existing.manifest_sha256 != manifest.manifest_sha256
            ):
                raise RuntimeError(
                    "sealed partition manifest identity is immutable per experiment version"
                )
        self._record_strategy_discovery_bytes(path, raw)
        return self._strategy_discovery_reference(
            path,
            raw,
            name=f"strategy-partitions-{manifest.manifest_sha256[:16]}",
            artifact_type=ArtifactType.STRATEGY_PARTITION_MANIFEST,
            schema_version=manifest.schema_version,
            row_count=len(manifest.members),
        )

    def load_strategy_partition_manifest(
        self,
        manifest_sha256: str,
    ) -> PartitionManifestV1:
        from kirby2.discovery.partitions import PartitionManifestV1

        _require_strategy_discovery_digest(manifest_sha256, "partition manifest")
        path = self.strategy_partition_directory / f"{manifest_sha256}.json"
        raw = self._read_strategy_discovery_bytes(path)
        manifest = PartitionManifestV1.from_json_bytes(raw)
        if manifest.manifest_sha256 != manifest_sha256:
            raise RuntimeError("stored partition manifest identity does not match its path")
        return manifest

    def record_strategy_access_record(
        self,
        record: PartitionAccessRecordV1,
    ) -> ArtifactReference:
        """Append one grant or refusal to an experiment's single access chain."""

        with self._strategy_discovery_lock():
            return self._record_strategy_access_record_unlocked(record)

    def _record_strategy_access_record_unlocked(
        self,
        record: PartitionAccessRecordV1,
    ) -> ArtifactReference:

        from kirby2.discovery.access import (
            PartitionAccessRecordV1,
            request_partition_access,
        )

        if not isinstance(record, PartitionAccessRecordV1):
            raise TypeError("strategy access artifact requires PartitionAccessRecordV1")
        manifest = self.load_strategy_partition_manifest(
            record.partition_manifest_sha256
        )
        path = self.strategy_access_directory / f"{record.access_sha256}.json"
        raw = record.canonical_bytes()
        before = self.load_strategy_experiment_state(record.state_before_sha256)
        if (
            before.experiment_id != record.experiment_id
            or before.experiment_version != record.experiment_version
            or before.partition_manifest_sha256 != record.partition_manifest_sha256
            or before.phase is not record.phase_before
        ):
            raise RuntimeError("strategy access record does not match its stored prior state")
        expected = request_partition_access(
            manifest,
            before,
            partition=record.partition,
            purpose=record.purpose,
            member_ids=record.requested_member_ids,
            validation_schedule_id=record.validation_schedule_id,
        )
        if expected.record != record:
            raise RuntimeError("strategy access record differs from the enforced decision")
        if path.exists() or path.is_symlink():
            existing = self._read_strategy_discovery_bytes(path)
            if existing != raw:
                raise RuntimeError("immutable strategy access artifact differs")
            return self._strategy_discovery_reference(
                path,
                raw,
                name=f"strategy-access-{record.access_sha256[:16]}",
                artifact_type=ArtifactType.STRATEGY_ACCESS_RECORD,
                schema_version=record.schema_version,
                row_count=1,
            )
        records = self.query_strategy_access_records(
            record.experiment_id,
            record.experiment_version,
        )
        expected_previous = None if not records else records[-1].access_sha256
        if (
            record.access_ordinal != len(records) + 1
            or record.previous_access_sha256 != expected_previous
            or before.access_record_sha256
            != tuple(item.access_sha256 for item in records)
        ):
            raise RuntimeError("strategy access record would fork or skip the access chain")
        self._record_strategy_discovery_bytes(path, raw)
        return self._strategy_discovery_reference(
            path,
            raw,
            name=f"strategy-access-{record.access_sha256[:16]}",
            artifact_type=ArtifactType.STRATEGY_ACCESS_RECORD,
            schema_version=record.schema_version,
            row_count=1,
        )

    def load_strategy_access_record(
        self,
        access_sha256: str,
    ) -> PartitionAccessRecordV1:
        from kirby2.discovery.access import PartitionAccessRecordV1

        _require_strategy_discovery_digest(access_sha256, "partition access")
        path = self.strategy_access_directory / f"{access_sha256}.json"
        raw = self._read_strategy_discovery_bytes(path)
        record = PartitionAccessRecordV1.from_json_bytes(raw)
        if record.access_sha256 != access_sha256:
            raise RuntimeError("stored strategy access identity does not match its path")
        return record

    def query_strategy_access_records(
        self,
        experiment_id: str,
        experiment_version: int,
    ) -> tuple[PartitionAccessRecordV1, ...]:
        if type(experiment_id) is not str or not experiment_id:
            raise ValueError("strategy access query experiment ID must be nonempty")
        if type(experiment_version) is not int or experiment_version <= 0:
            raise ValueError("strategy access query experiment version must be positive")
        records = []
        for path in sorted(self.strategy_access_directory.glob("*.json")):
            record = self.load_strategy_access_record(path.stem)
            if (
                record.experiment_id == experiment_id
                and record.experiment_version == experiment_version
            ):
                records.append(record)
        records.sort(key=lambda item: (item.access_ordinal, item.access_sha256))
        previous = None
        for expected_ordinal, record in enumerate(records, start=1):
            if (
                record.access_ordinal != expected_ordinal
                or record.previous_access_sha256 != previous
            ):
                raise RuntimeError("stored strategy access chain is forked or incomplete")
            previous = record.access_sha256
        return tuple(records)

    def record_strategy_experiment_state(
        self,
        state: StrategyDiscoveryExperimentV1,
    ) -> ArtifactReference:
        """Persist a lifecycle snapshot only when it matches the access ledger."""

        with self._strategy_discovery_lock():
            return self._record_strategy_experiment_state_unlocked(state)

    def _record_strategy_experiment_state_unlocked(
        self,
        state: StrategyDiscoveryExperimentV1,
    ) -> ArtifactReference:

        from kirby2.discovery.experiment import StrategyDiscoveryExperimentV1

        if not isinstance(state, StrategyDiscoveryExperimentV1):
            raise TypeError(
                "strategy experiment artifact requires StrategyDiscoveryExperimentV1"
            )
        manifest = self.load_strategy_partition_manifest(
            state.partition_manifest_sha256
        )
        if (
            manifest.experiment_id != state.experiment_id
            or manifest.experiment_version != state.experiment_version
        ):
            raise RuntimeError("strategy experiment state is bound to another manifest")
        path = self.strategy_experiment_state_directory / f"{state.state_sha256}.json"
        raw = state.canonical_bytes()
        if path.exists() or path.is_symlink():
            existing = self._read_strategy_discovery_bytes(path)
            if existing != raw:
                raise RuntimeError("immutable strategy experiment state differs")
            return self._strategy_discovery_reference(
                path,
                raw,
                name=f"strategy-state-{state.state_sha256[:16]}",
                artifact_type=ArtifactType.STRATEGY_EXPERIMENT_STATE,
                schema_version=state.schema_version,
                row_count=1,
            )
        records = self.query_strategy_access_records(
            state.experiment_id,
            state.experiment_version,
        )
        record_digests = tuple(item.access_sha256 for item in records)
        if state.access_record_sha256 != record_digests:
            raise RuntimeError("strategy experiment state omits or forks access history")
        if records and records[-1].phase_after is not state.phase:
            candidate_freeze_transition = (
                records[-1].phase_after.value == "SEARCH_OPEN"
                and state.phase.value == "CANDIDATES_FROZEN"
                and state.candidate_freeze is not None
            )
            if not candidate_freeze_transition:
                raise RuntimeError(
                    "strategy experiment phase differs from its access ledger"
                )
        expected_train_count = sum(
            item.decision.value == "GRANTED" and item.purpose.value == "SEARCH_TRAIN"
            for item in records
        )
        expected_validation_counts: dict[str, int] = {}
        for item in records:
            if (
                item.decision.value == "GRANTED"
                and item.purpose.value == "SEARCH_VALIDATION"
                and item.validation_schedule_id is not None
            ):
                expected_validation_counts[item.validation_schedule_id] = (
                    expected_validation_counts.get(item.validation_schedule_id, 0) + 1
                )
        if state.train_access_count != expected_train_count or {
            item.schedule_id: item.count for item in state.validation_access_counts
        } != expected_validation_counts:
            raise RuntimeError("strategy experiment access counters are not ledger-derived")
        revealed = tuple(
            item.access_sha256
            for item in records
            if item.decision.value == "GRANTED"
            and item.purpose.value == "HOLDOUT_REVEAL"
        )
        if len(revealed) > 1 or state.reveal_access_sha256 != (
            None if not revealed else revealed[0]
        ):
            raise RuntimeError("strategy experiment reveal identity is not ledger-derived")
        for existing_path in sorted(
            self.strategy_experiment_state_directory.glob("*.json")
        ):
            existing_state = self.load_strategy_experiment_state(existing_path.stem)
            if (
                existing_state.experiment_id != state.experiment_id
                or existing_state.experiment_version != state.experiment_version
            ):
                continue
            if (
                existing_state.candidate_freeze_sha256 is not None
                and state.candidate_freeze_sha256 is not None
                and existing_state.candidate_freeze_sha256
                != state.candidate_freeze_sha256
            ):
                raise RuntimeError("stored strategy candidate freeze is immutable")
            if (
                existing_state.reveal_access_sha256 is not None
                and state.reveal_access_sha256 is not None
                and existing_state.reveal_access_sha256
                != state.reveal_access_sha256
            ):
                raise RuntimeError("stored strategy reveal identity is immutable")
            if (
                existing_state.terminal_outcome is not None
                and existing_state.terminal_outcome.value in {"PASSED", "FAILED"}
                and state.terminal_outcome != existing_state.terminal_outcome
            ):
                raise RuntimeError("stored terminal evaluation outcome is immutable")
        self._record_strategy_discovery_bytes(path, raw)
        return self._strategy_discovery_reference(
            path,
            raw,
            name=f"strategy-state-{state.state_sha256[:16]}",
            artifact_type=ArtifactType.STRATEGY_EXPERIMENT_STATE,
            schema_version=state.schema_version,
            row_count=1,
        )

    def load_strategy_experiment_state(
        self,
        state_sha256: str,
    ) -> StrategyDiscoveryExperimentV1:
        from kirby2.discovery.experiment import StrategyDiscoveryExperimentV1

        _require_strategy_discovery_digest(state_sha256, "strategy experiment state")
        path = self.strategy_experiment_state_directory / f"{state_sha256}.json"
        raw = self._read_strategy_discovery_bytes(path)
        state = StrategyDiscoveryExperimentV1.from_json_bytes(raw)
        if state.state_sha256 != state_sha256:
            raise RuntimeError("stored strategy state identity does not match its path")
        return state

    def _strategy_discovery_reference(
        self,
        path: Path,
        raw: bytes,
        *,
        name: str,
        artifact_type: ArtifactType,
        schema_version: int,
        row_count: int | None,
    ) -> ArtifactReference:
        return ArtifactReference(
            name=name,
            relative_path=path.relative_to(self.root).as_posix(),
            sha256=hashlib.sha256(raw).hexdigest(),
            schema_version=schema_version,
            row_count=row_count,
            media_type="application/json",
            artifact_type=artifact_type,
        )

    def _record_strategy_discovery_bytes(self, path: Path, raw: bytes) -> None:
        self._assert_strategy_discovery_path(path)
        if type(raw) is not bytes:
            raise TypeError("strategy discovery artifacts must be exact bytes")
        if path.exists() or path.is_symlink():
            existing = self._read_strategy_discovery_bytes(path)
            if existing != raw:
                raise RuntimeError("immutable strategy discovery artifact differs")
            return
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                if self._read_strategy_discovery_bytes(path) != raw:
                    raise RuntimeError("concurrent immutable strategy artifact differs")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _read_strategy_discovery_bytes(self, path: Path) -> bytes:
        self._assert_strategy_discovery_path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unknown or unsafe strategy discovery artifact: {path.name}")
        return path.read_bytes()

    def _assert_strategy_discovery_path(self, path: Path) -> None:
        allowed_directories = {
            self.strategy_partition_directory,
            self.strategy_experiment_state_directory,
            self.strategy_access_directory,
        }
        if path.parent not in allowed_directories:
            raise ValueError("strategy discovery artifact path is outside its store")
        if any(
            candidate.is_symlink()
            for candidate in (
                self.strategy_discovery_directory,
                path.parent,
            )
        ):
            raise ValueError("strategy discovery artifact directories cannot be symlinks")

    @contextmanager
    def _strategy_discovery_lock(self):
        import fcntl

        lock_path = self.root / "strategy-discovery.lock"
        with lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

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
        if manifest.run_type is RunType.FULL_DAY:
            from kirby2.full_day.store import FullDayStore

            report = FullDayStore(self.root.resolve()).verify_day(run_id)
            return VerificationReport(
                run_id=run_id,
                manifest_loaded=report.manifest_valid,
                references_exist=report.artifact_inventory_valid,
                artifact_digests_match=report.artifact_digests_valid,
                artifact_row_counts_match=report.canonical_payloads_valid,
                event_sequence_complete=report.replay_valid,
                replay_configuration_available=report.checkpoints_valid,
                replay_passed=report.replay_valid,
                result_digest_match=report.replay_valid,
                evidence_digest_match=report.summary_valid,
                schema_versions_supported=report.canonical_payloads_valid,
                run_identity_match=manifest.run_id == run_id,
                failures=report.failures,
            )
        if manifest.run_type is RunType.FULL_DAY_QUALIFICATION:
            from kirby2.full_day.qualification import QualificationEvidenceStore

            report = QualificationEvidenceStore(self.root.resolve()).verify(run_id)
            return VerificationReport(
                run_id=run_id,
                manifest_loaded=report.manifest_valid,
                references_exist=report.artifact_inventory_valid,
                artifact_digests_match=report.artifact_digests_valid,
                artifact_row_counts_match=report.canonical_payloads_valid,
                event_sequence_complete=report.replay_verification_valid,
                replay_configuration_available=report.reveal_token_valid,
                replay_passed=report.replay_verification_valid,
                result_digest_match=report.result_digest_valid,
                evidence_digest_match=report.evidence_digest_valid,
                schema_versions_supported=report.schema_inventory_valid,
                run_identity_match=manifest.run_id == run_id,
                failures=report.failures,
            )
        if manifest.run_type in {
            RunType.LESSON_MINING,
            RunType.LESSON_REVIEW,
            RunType.LESSON_BUILD,
        }:
            return LessonMiningStore(self.root).verify_run(run_id)
        if manifest.run_type is RunType.LEARNER_UPDATE:
            return LearnerArtifactStore(self.root).verify_run(run_id)
        directory = self.run_directory(run_id)
        expected_artifact_schemas = {
            "configuration": RUN_CONFIGURATION_SCHEMA_VERSION,
            **{spec.name: spec.schema_version for spec in TABLE_SPECS},
        }
        actual_artifact_schemas = {
            item.name: item.schema_version for item in manifest.artifacts
        }
        expected_schema_versions = {
            **SUPPORTED_SCHEMA_VERSIONS,
            "run_manifest": manifest.schema_version,
        }
        schema_versions_supported = (
            manifest.schema_versions == expected_schema_versions
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
        from kirby2.marketdata.models import DatasetManifest

        dataset_manifests = [
            (DatasetManifest.from_dict(load_toml(path)), file_sha256(path))
            for path in sorted(
                (self.root / "datasets").glob("dataset-*/dataset_manifest.toml")
            )
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
                connection.execute("DROP TABLE IF EXISTS run_artifact_registry")
                artifact_columns_sql = ", ".join(
                    f'"{name}" {sql_type}'
                    for name, sql_type in RUN_ARTIFACT_REGISTRY_COLUMNS
                )
                connection.execute(
                    f"CREATE TABLE run_artifact_registry ({artifact_columns_sql})"
                )
                artifact_rows = artifact_registry_rows(
                    [item for item, _manifest_sha256 in manifests]
                )
                if artifact_rows:
                    placeholders = ", ".join(
                        "?" for _ in RUN_ARTIFACT_REGISTRY_COLUMNS
                    )
                    connection.executemany(
                        f"INSERT INTO run_artifact_registry VALUES ({placeholders})",
                        artifact_rows,
                    )
                connection.execute("DROP TABLE IF EXISTS dataset_registry")
                connection.execute(
                    """
                    CREATE TABLE dataset_registry (
                        dataset_id VARCHAR PRIMARY KEY,
                        adapter VARCHAR NOT NULL,
                        source_locator VARCHAR NOT NULL,
                        source_name VARCHAR NOT NULL,
                        license_note VARCHAR NOT NULL,
                        real_market_data BOOLEAN NOT NULL,
                        capability VARCHAR NOT NULL,
                        tick_size VARCHAR NOT NULL,
                        source_digest VARCHAR NOT NULL,
                        records_digest VARCHAR NOT NULL,
                        quality_digest VARCHAR NOT NULL,
                        replay_mode VARCHAR NOT NULL,
                        exact_replay_allowed BOOLEAN NOT NULL,
                        time_start_ns BIGINT,
                        time_end_ns BIGINT,
                        symbols_toml VARCHAR NOT NULL,
                        session_count BIGINT NOT NULL,
                        creation_timestamp_utc VARCHAR NOT NULL,
                        manifest_sha256 VARCHAR NOT NULL
                    )
                    """
                )
                if dataset_manifests:
                    connection.executemany(
                        "INSERT INTO dataset_registry VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                item.dataset_id,
                                item.adapter,
                                item.source_locator,
                                item.source_name,
                                item.license_note,
                                item.real_market_data,
                                item.capability.value,
                                item.tick_size,
                                item.source_digest,
                                item.records_digest,
                                item.quality_digest,
                                item.replay_mode.value,
                                item.exact_replay_allowed,
                                item.time_start_ns,
                                item.time_end_ns,
                                canonical_toml({"symbols": list(item.symbols)}),
                                item.session_count,
                                item.creation_timestamp_utc,
                                manifest_sha256,
                            )
                            for item, manifest_sha256 in dataset_manifests
                        ],
                    )
                _create_fact_views(connection, self.runs_directory)
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
        expected_datasets = {
            path.parent.name: file_sha256(path)
            for path in (self.root / "datasets").glob(
                "dataset-*/dataset_manifest.toml"
            )
        }
        expected_artifacts = tuple(
            artifact_registry_rows(
                [
                    RunManifest.from_dict(load_toml(path))
                    for path in sorted(
                        self.runs_directory.glob("run-*/manifest.toml")
                    )
                ]
            )
        )
        expected_qualification_artifacts = tuple(
            qualification_artifact_registry_rows(
                [
                    RunManifest.from_dict(load_toml(path))
                    for path in sorted(
                        self.runs_directory.glob("run-*/manifest.toml")
                    )
                ]
            )
        )
        expected_lesson_artifacts = tuple(
            lesson_mining_artifact_registry_rows(
                [
                    RunManifest.from_dict(load_toml(path))
                    for path in sorted(
                        self.runs_directory.glob("run-*/manifest.toml")
                    )
                ]
            )
        )
        expected_learner_artifacts = tuple(
            learner_artifact_registry_rows(
                [
                    RunManifest.from_dict(load_toml(path))
                    for path in sorted(
                        self.runs_directory.glob("run-*/manifest.toml")
                    )
                ]
            )
        )
        expected_strategy_discovery_artifacts = tuple(
            strategy_discovery_artifact_registry_rows(
                [
                    RunManifest.from_dict(load_toml(path))
                    for path in sorted(
                        self.runs_directory.glob("run-*/manifest.toml")
                    )
                ]
            )
        )
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
                actual_datasets = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT dataset_id, manifest_sha256 FROM dataset_registry"
                    ).fetchall()
                }
                actual_artifacts = tuple(
                    connection.execute(
                        "SELECT run_id, artifact_type, artifact_name, relative_path, "
                        "sha256, schema_version, row_count, media_type "
                        "FROM run_artifact_registry "
                        "ORDER BY run_id, artifact_type, artifact_name"
                    ).fetchall()
                )
                actual_qualification_artifacts = tuple(
                    connection.execute(
                        "SELECT run_id, artifact_type, artifact_name, relative_path, "
                        "sha256, schema_version, row_count, media_type "
                        "FROM full_day_qualification_artifacts "
                        "ORDER BY run_id, artifact_type, artifact_name"
                    ).fetchall()
                )
                actual_lesson_artifacts = tuple(
                    connection.execute(
                        "SELECT run_id, artifact_type, artifact_name, relative_path, "
                        "sha256, schema_version, row_count, media_type "
                        "FROM lesson_mining_artifacts "
                        "ORDER BY run_id, artifact_type, artifact_name"
                    ).fetchall()
                )
                actual_learner_artifacts = tuple(
                    connection.execute(
                        "SELECT run_id, artifact_type, artifact_name, relative_path, "
                        "sha256, schema_version, row_count, media_type "
                        "FROM learner_artifacts "
                        "ORDER BY run_id, artifact_type, artifact_name"
                    ).fetchall()
                )
                actual_strategy_discovery_artifacts = tuple(
                    connection.execute(
                        "SELECT run_id, artifact_type, artifact_name, relative_path, "
                        "sha256, schema_version, row_count, media_type "
                        "FROM strategy_discovery_artifacts "
                        "ORDER BY run_id, artifact_type, artifact_name"
                    ).fetchall()
                )
            finally:
                connection.close()
        except Exception:
            return False
        return (
            actual == expected
            and actual_datasets == expected_datasets
            and actual_artifacts == expected_artifacts
            and actual_qualification_artifacts
            == expected_qualification_artifacts
            and actual_lesson_artifacts == expected_lesson_artifacts
            and actual_learner_artifacts == expected_learner_artifacts
            and actual_strategy_discovery_artifacts
            == expected_strategy_discovery_artifacts
        )

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

    def query_qualification_artifacts(
        self, run_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return only typed full-day qualification artifacts from the catalog."""

        self._ensure_catalog()
        duckdb = _duckdb()
        connection = duckdb.connect(str(self.catalog_path), read_only=True)
        try:
            if run_id is None:
                cursor = connection.execute(
                    "SELECT * FROM full_day_qualification_artifacts "
                    "ORDER BY run_id, artifact_type, artifact_name"
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM full_day_qualification_artifacts "
                    "WHERE run_id = ? ORDER BY artifact_type, artifact_name",
                    [run_id],
                )
            columns = tuple(item[0] for item in cursor.description)
            return tuple(
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            )
        finally:
            connection.close()

    def query_lesson_mining_artifacts(
        self, run_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return typed mining, review-sidecar, and lesson-build artifacts."""

        self._ensure_catalog()
        duckdb = _duckdb()
        connection = duckdb.connect(str(self.catalog_path), read_only=True)
        try:
            if run_id is None:
                cursor = connection.execute(
                    "SELECT * FROM lesson_mining_artifacts "
                    "ORDER BY run_id, artifact_type, artifact_name"
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM lesson_mining_artifacts "
                    "WHERE run_id = ? ORDER BY artifact_type, artifact_name",
                    [run_id],
                )
            columns = tuple(item[0] for item in cursor.description)
            return tuple(
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            )
        finally:
            connection.close()

    def query_learner_artifacts(
        self, run_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return typed learner evidence-update and projection artifacts."""

        self._ensure_catalog()
        duckdb = _duckdb()
        connection = duckdb.connect(str(self.catalog_path), read_only=True)
        try:
            if run_id is None:
                cursor = connection.execute(
                    "SELECT * FROM learner_artifacts "
                    "ORDER BY run_id, artifact_type, artifact_name"
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM learner_artifacts "
                    "WHERE run_id = ? ORDER BY artifact_type, artifact_name",
                    [run_id],
                )
            columns = tuple(item[0] for item in cursor.description)
            return tuple(
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()
            )
        finally:
            connection.close()

    def query_strategy_discovery_artifacts(
        self, run_id: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return typed immutable strategy-discovery lineage artifacts."""

        self._ensure_catalog()
        duckdb = _duckdb()
        connection = duckdb.connect(str(self.catalog_path), read_only=True)
        try:
            if run_id is None:
                cursor = connection.execute(
                    "SELECT * FROM strategy_discovery_artifacts "
                    "ORDER BY run_id, artifact_type, artifact_name"
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM strategy_discovery_artifacts "
                    "WHERE run_id = ? ORDER BY artifact_type, artifact_name",
                    [run_id],
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


_LEARNER_ARTIFACT_SPECS = (
    (
        "learner_evidence_update",
        "learner-update.json",
        ArtifactType.LEARNER_EVIDENCE_UPDATE,
        "application/json",
    ),
    (
        "learner_state_projection",
        "learner-projection.json",
        ArtifactType.LEARNER_STATE_PROJECTION,
        "application/json",
    ),
)


class LearnerArtifactStore:
    """Persist one immutable evidence update with its rebuildable projection."""

    def __init__(self, root: Path = DEFAULT_RESEARCH_STORE) -> None:
        self.root = root
        self.runs_directory = root / "runs"
        self.staging_directory = root / ".staging"
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)

    def run_directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"run-[0-9a-f]{24}", run_id):
            raise ValueError("invalid run ID")
        return self.runs_directory / run_id

    def load_manifest(self, run_id: str) -> RunManifest:
        path = self.run_directory(run_id) / "manifest.toml"
        if not path.is_file():
            raise ValueError(f"unknown run ID: {run_id}")
        manifest = RunManifest.from_dict(load_toml(path))
        if manifest.run_type is not RunType.LEARNER_UPDATE:
            raise ValueError("run is not a learner update/projection artifact")
        return manifest

    def record_update(
        self,
        ledger,
        projection,
        *,
        seed: int | None = None,
        parent_run_id: str | None = None,
        repository: Path | None = None,
    ) -> RunManifest:
        from kirby2.curriculum.evidence import LearnerEvidenceLedgerV1
        from kirby2.curriculum.learner import build_learner_projection_v1
        from kirby2.curriculum.projections import LearnerProjectionV1

        if not isinstance(ledger, LearnerEvidenceLedgerV1):
            raise TypeError("learner update persistence requires a typed ledger")
        if not isinstance(projection, LearnerProjectionV1):
            raise TypeError("learner update persistence requires a typed projection")
        require_learner_profile_id(ledger.learner_id)
        require_learner_profile_id(projection.learner_id)
        if seed is not None and type(seed) is not int:
            raise TypeError("learner update seed must be an integer or absent")
        if parent_run_id is not None and not re.fullmatch(
            r"run-[0-9a-f]{24}", parent_run_id
        ):
            raise ValueError("learner update parent run ID is invalid")
        final_ordinal = (
            0 if not ledger.assessments else ledger.assessments[-1].attempt_ordinal
        )
        if projection.learner_id != ledger.learner_id:
            raise ValueError("learner update ledger and projection identities differ")
        if projection.as_of_attempt_ordinal != final_ordinal:
            raise ValueError("learner projection must cover the complete update")
        rebuilt = build_learner_projection_v1(
            ledger,
            as_of_attempt_ordinal=final_ordinal,
        )
        if rebuilt.canonical_bytes() != projection.canonical_bytes():
            raise ValueError("learner projection is not rebuilt from the update")

        payloads = {
            "learner-update.json": ledger.canonical_bytes(),
            "learner-projection.json": projection.canonical_bytes(),
        }
        references = _learner_artifact_references(payloads, ledger, projection)
        configuration_digest, evidence_digest_value, result_digest = (
            _learner_artifact_digests(ledger, projection)
        )
        simulation_times = tuple(
            item.observable_context.simulation_time_us for item in ledger.assessments
        )
        repository_path = repository or Path(__file__).resolve().parents[2]
        manifest = RunManifest.create(
            parent_run_id=parent_run_id,
            run_type=RunType.LEARNER_UPDATE,
            scenario_id=None,
            lesson_id=None,
            seed=seed,
            flow_model=projection.model_id,
            market_profile="LEARNER_EVIDENCE_V1",
            strategy_id="NONE",
            hotkey_layout_id="NONE",
            session_objective="PERSIST_IMMUTABLE_LEARNER_UPDATE_AND_PROJECTION",
            simulation_start_us=min(simulation_times, default=0),
            simulation_end_us=max(simulation_times, default=0),
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions=_learner_schema_versions(),
            input_dataset_references=(
                f"learner-evidence:{ledger.learner_id}:{ledger.ledger_sha256}",
            ),
            configuration_digest=configuration_digest,
            evidence_digest=evidence_digest_value,
            result_digest=result_digest,
            creation_timestamp_utc=(
                "1970-01-01T00:00:00Z"
                if not ledger.assessments
                else ledger.assessments[-1].study_timestamp_utc
            ),
            artifacts=references,
        )
        return self._persist(manifest, payloads)

    def load_update(self, run_id: str):
        from kirby2.curriculum.evidence import LearnerEvidenceLedgerV1
        from kirby2.curriculum.projections import LearnerProjectionV1

        manifest = self.load_manifest(run_id)
        directory = self.run_directory(run_id)
        ledger = LearnerEvidenceLedgerV1.from_json_bytes(
            (directory / "learner-update.json").read_bytes()
        )
        projection = LearnerProjectionV1.from_json_bytes(
            (directory / "learner-projection.json").read_bytes()
        )
        require_learner_profile_id(ledger.learner_id)
        require_learner_profile_id(projection.learner_id)
        if projection.learner_id != ledger.learner_id:
            raise ValueError("learner update ledger and projection identities differ")
        if manifest.input_dataset_references != (
            f"learner-evidence:{ledger.learner_id}:{ledger.ledger_sha256}",
        ):
            raise ValueError("learner artifact manifest identity binding differs")
        if tuple(item.relative_path for item in manifest.artifacts) != tuple(
            item[1] for item in _LEARNER_ARTIFACT_SPECS
        ):
            raise ValueError("learner artifact manifest inventory differs")
        return ledger, projection

    def verify_run(self, run_id: str) -> VerificationReport:
        failures: list[str] = []
        flags = {
            "manifest_loaded": False,
            "references_exist": False,
            "artifact_digests_match": False,
            "artifact_row_counts_match": False,
            "event_sequence_complete": False,
            "replay_configuration_available": False,
            "replay_passed": False,
            "result_digest_match": False,
            "evidence_digest_match": False,
            "schema_versions_supported": False,
            "run_identity_match": False,
        }
        try:
            manifest = self.load_manifest(run_id)
            flags["manifest_loaded"] = True
            flags["run_identity_match"] = manifest.run_id == run_id
            if not flags["run_identity_match"]:
                raise ValueError("learner manifest run ID differs from directory")
        except (OSError, TypeError, ValueError) as error:
            failures.append(f"manifest invalid: {error}")
            return VerificationReport(run_id=run_id, failures=tuple(failures), **flags)

        expected_inventory = tuple(
            (name, path, artifact_type, media_type)
            for name, path, artifact_type, media_type in _LEARNER_ARTIFACT_SPECS
        )
        actual_inventory = tuple(
            (item.name, item.relative_path, item.artifact_type, item.media_type)
            for item in manifest.artifacts
        )
        flags["schema_versions_supported"] = (
            manifest.schema_versions == _learner_schema_versions()
            and actual_inventory == expected_inventory
            and all(item.schema_version == 1 for item in manifest.artifacts)
        )
        if not flags["schema_versions_supported"]:
            failures.append("learner typed artifact or schema inventory differs")

        directory = self.run_directory(run_id)
        payloads: dict[str, bytes] = {}
        try:
            for reference in manifest.artifacts:
                path = directory / reference.relative_path
                if path.is_symlink() or not path.is_file():
                    raise ValueError(
                        f"unsafe or missing learner artifact: {reference.relative_path}"
                    )
                payloads[reference.relative_path] = path.read_bytes()
            extras = {
                path.name for path in directory.iterdir() if path.name != "manifest.toml"
            } - {Path(name).name for name in payloads}
            if extras:
                raise ValueError("learner run contains unregistered artifact files")
            flags["references_exist"] = len(payloads) == len(_LEARNER_ARTIFACT_SPECS)
            flags["artifact_digests_match"] = all(
                hashlib.sha256(payloads[item.relative_path]).hexdigest() == item.sha256
                for item in manifest.artifacts
            )
            if not flags["artifact_digests_match"]:
                failures.append("one or more learner artifact digests differ")
        except (OSError, ValueError) as error:
            failures.append(f"learner artifact inventory invalid: {error}")

        try:
            ledger, projection = self.load_update(run_id)
            from kirby2.curriculum.learner import build_learner_projection_v1

            final_ordinal = (
                0 if not ledger.assessments else ledger.assessments[-1].attempt_ordinal
            )
            rebuilt = build_learner_projection_v1(
                ledger,
                as_of_attempt_ordinal=final_ordinal,
            )
            flags["artifact_row_counts_match"] = tuple(
                item.row_count for item in manifest.artifacts
            ) == (len(ledger.assessments), len(projection.skill_projections))
            flags["event_sequence_complete"] = (
                projection.as_of_attempt_ordinal == final_ordinal
                and projection.input_assessment_count == len(ledger.assessments)
            )
            flags["replay_configuration_available"] = True
            flags["replay_passed"] = (
                rebuilt.canonical_bytes() == projection.canonical_bytes()
            )
            configuration_digest, evidence_digest_value, result_digest = (
                _learner_artifact_digests(ledger, projection)
            )
            flags["evidence_digest_match"] = (
                manifest.evidence_digest == evidence_digest_value
            )
            flags["result_digest_match"] = manifest.result_digest == result_digest
            if manifest.configuration_digest != configuration_digest:
                failures.append("learner projection configuration digest differs")
            if not flags["artifact_row_counts_match"]:
                failures.append("learner artifact row counts differ")
            if not flags["event_sequence_complete"]:
                failures.append("learner projection does not cover the complete update")
            if not flags["replay_passed"]:
                failures.append("learner projection rebuild differs")
            if not flags["evidence_digest_match"]:
                failures.append("learner evidence digest differs")
            if not flags["result_digest_match"]:
                failures.append("learner projection result digest differs")
        except Exception as error:
            failures.append(f"learner artifact replay invalid: {error}")
        return VerificationReport(run_id=run_id, failures=tuple(failures), **flags)

    def _persist(
        self,
        manifest: RunManifest,
        payloads: dict[str, bytes],
    ) -> RunManifest:
        target = self.run_directory(manifest.run_id)
        if target.exists():
            existing = self.load_manifest(manifest.run_id)
            if existing.identity_dict() != manifest.identity_dict():
                raise RuntimeError("content-derived learner run ID collision")
            report = self.verify_run(manifest.run_id)
            if not report.passed:
                raise RuntimeError(
                    "existing immutable learner run is invalid: "
                    + "; ".join(report.failures)
                )
            return existing
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix=f"{manifest.run_id}-",
        ) as temporary:
            staging = Path(temporary) / manifest.run_id
            staging.mkdir()
            for relative_path, raw in payloads.items():
                (staging / relative_path).write_bytes(raw)
            (staging / "manifest.toml").write_text(
                manifest.to_toml(), encoding="utf-8"
            )
            staging.rename(target)
        report = self.verify_run(manifest.run_id)
        if not report.passed:
            raise RuntimeError(
                "new immutable learner run failed verification: "
                + "; ".join(report.failures)
            )
        RunStore(self.root).refresh_catalog()
        return self.load_manifest(manifest.run_id)


def _learner_schema_versions() -> dict[str, int]:
    return {
        "learner_evidence": 1,
        "learner_projection": 1,
        "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
    }


def _learner_artifact_references(
    payloads: dict[str, bytes],
    ledger,
    projection,
) -> tuple[ArtifactReference, ...]:
    expected_paths = {item[1] for item in _LEARNER_ARTIFACT_SPECS}
    if set(payloads) != expected_paths:
        raise ValueError("learner artifact payload inventory differs")
    row_counts = {
        "learner-update.json": len(ledger.assessments),
        "learner-projection.json": len(projection.skill_projections),
    }
    return tuple(
        ArtifactReference(
            name=name,
            relative_path=relative_path,
            sha256=hashlib.sha256(payloads[relative_path]).hexdigest(),
            schema_version=1,
            row_count=row_counts[relative_path],
            media_type=media_type,
            artifact_type=artifact_type,
        )
        for name, relative_path, artifact_type, media_type in _LEARNER_ARTIFACT_SPECS
    )


def _learner_artifact_digests(ledger, projection) -> tuple[str, str, str]:
    return (
        canonical_digest(
            {
                "model_id": projection.model_id,
                "model_policy_digest": projection.model_policy_digest,
                "policy": "LEARNER_UPDATE_PROJECTION_V1",
                "schema_version": 1,
            }
        ),
        hashlib.sha256(ledger.canonical_bytes()).hexdigest(),
        hashlib.sha256(projection.canonical_bytes()).hexdigest(),
    )


_LESSON_MINING_ARTIFACT_SPECS = (
    (
        "qualification_source_matrix",
        "qualification-sources.toml",
        ArtifactType.LESSON_MINING_SOURCE_MATRIX,
        "application/toml",
    ),
    (
        "source_validation",
        "source-validation.json",
        ArtifactType.LESSON_MINING_SOURCE_VALIDATION,
        "application/json",
    ),
    (
        "lesson_candidates",
        "candidates.json",
        ArtifactType.LESSON_MINING_CANDIDATES,
        "application/json",
    ),
    (
        "review_selection",
        "selection.json",
        ArtifactType.LESSON_MINING_SELECTION,
        "application/json",
    ),
    (
        "technical_review_packet",
        "review-packet.json",
        ArtifactType.LESSON_TECHNICAL_REVIEW_PACKET,
        "application/json",
    ),
)
_LESSON_REVIEW_ARTIFACT_SPECS = (
    (
        "lesson_review_sidecar",
        "review-sidecar.json",
        ArtifactType.LESSON_REVIEW_SIDECAR,
        "application/json",
    ),
)
_LESSON_BUILD_ARTIFACT_SPECS = (
    (
        "lesson_build_proposal",
        "lesson-build.json",
        ArtifactType.LESSON_BUILD_PROPOSAL,
        "application/json",
    ),
)
_LESSON_RUN_TYPES = frozenset(
    {RunType.LESSON_MINING, RunType.LESSON_REVIEW, RunType.LESSON_BUILD}
)


class LessonMiningStore:
    """Persist mining, review, and build runs without mutating earlier artifacts."""

    def __init__(self, root: Path = DEFAULT_RESEARCH_STORE) -> None:
        self.root = root
        self.runs_directory = root / "runs"
        self.staging_directory = root / ".staging"
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)

    def run_directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"run-[0-9a-f]{24}", run_id):
            raise ValueError("invalid run ID")
        return self.runs_directory / run_id

    def load_manifest(self, run_id: str) -> RunManifest:
        path = self.run_directory(run_id) / "manifest.toml"
        if not path.is_file():
            raise ValueError(f"unknown run ID: {run_id}")
        manifest = RunManifest.from_dict(load_toml(path))
        if manifest.run_type not in _LESSON_RUN_TYPES:
            raise ValueError("run is not a lesson mining/review/build artifact")
        return manifest

    def record_mining_result(
        self,
        result,
        *,
        parent_run_id: str | None = None,
        repository: Path | None = None,
    ) -> RunManifest:
        from kirby2.mining.reviews import MiningQualificationResultV1

        if not isinstance(result, MiningQualificationResultV1):
            raise TypeError("lesson mining persistence requires a typed result")
        if parent_run_id is not None and not re.fullmatch(
            r"run-[0-9a-f]{24}", parent_run_id
        ):
            raise ValueError("lesson mining parent run ID is invalid")
        payloads = result.artifact_payloads()
        row_counts = {
            "source-validation.json": len(result.source_materializations),
            "candidates.json": result.candidate_count,
            "selection.json": result.selection.selected_count,
            "review-packet.json": len(result.review_packet.rows),
        }
        references = _lesson_artifact_references(
            payloads,
            _LESSON_MINING_ARTIFACT_SPECS,
            row_counts,
        )
        configuration_digest, evidence_digest, result_digest = (
            _lesson_mining_digests(payloads)
        )
        repository_path = repository or Path(__file__).resolve().parents[2]
        source_bounds = tuple(
            result.source_manifest.row(row_id).bounds
            for row_id in result.active_source_rows
        )
        manifest = RunManifest.create(
            parent_run_id=parent_run_id,
            run_type=RunType.LESSON_MINING,
            scenario_id="qualification-sources:" + result.source_manifest.manifest_sha256,
            lesson_id=None,
            seed=result.seed,
            flow_model="LESSON_MINING_V1",
            market_profile="PREREGISTERED_FIVE_SOURCE_QUALIFICATION_V1",
            strategy_id="NONE",
            hotkey_layout_id="NONE",
            session_objective="QUALIFY_REVIEW_READY_LESSON_PROPOSALS",
            simulation_start_us=min(int(item["source_start_us"]) for item in source_bounds),
            simulation_end_us=max(int(item["source_end_us"]) for item in source_bounds),
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions=_lesson_schema_versions(RunType.LESSON_MINING),
            input_dataset_references=tuple(
                f"{item.source_id}:{item.source_sha256}"
                for item in result.source_materializations
            ),
            configuration_digest=configuration_digest,
            evidence_digest=evidence_digest,
            result_digest=result_digest,
            creation_timestamp_utc=_utc_now(),
            artifacts=references,
        )
        return self._persist(manifest, payloads)

    def load_mining_result(self, run_id: str):
        from kirby2.mining.reviews import replay_qualification_artifacts

        manifest = self.load_manifest(run_id)
        if manifest.run_type is not RunType.LESSON_MINING:
            raise ValueError("run is not a lesson mining result")
        directory = self.run_directory(run_id)
        payloads = {
            reference.relative_path: (directory / reference.relative_path).read_bytes()
            for reference in manifest.artifacts
        }
        return replay_qualification_artifacts(payloads)

    def list_candidates(self, run_id: str):
        return self.load_mining_result(run_id).candidates

    def find_candidate(self, candidate_id: str):
        matches = []
        for path in sorted(self.runs_directory.glob("run-*/manifest.toml")):
            manifest = RunManifest.from_dict(load_toml(path))
            if manifest.run_type is not RunType.LESSON_MINING:
                continue
            result = self.load_mining_result(manifest.run_id)
            for candidate in result.candidates:
                if candidate.candidate.candidate_id == candidate_id:
                    matches.append((manifest.run_id, candidate))
        if not matches:
            raise ValueError(f"unknown lesson candidate ID: {candidate_id}")
        snapshots = {
            hashlib.sha256(candidate.candidate.canonical_bytes()).hexdigest()
            for _run_id, candidate in matches
        }
        if len(snapshots) != 1:
            raise RuntimeError("candidate ID resolves to inconsistent immutable snapshots")
        return matches[0]

    def review_history(self, candidate_id: str):
        from kirby2.mining.reviews import LessonReviewSidecarV1

        history = []
        for path in sorted(self.runs_directory.glob("run-*/manifest.toml")):
            manifest = RunManifest.from_dict(load_toml(path))
            if manifest.run_type is not RunType.LESSON_REVIEW:
                continue
            reference = manifest.artifacts[0]
            raw = (path.parent / reference.relative_path).read_bytes()
            payload = json.loads(raw)
            sidecar = LessonReviewSidecarV1.from_dict(payload)
            if sidecar.canonical_bytes() != raw:
                raise ValueError("stored lesson review sidecar is not canonical")
            if sidecar.candidate_id == candidate_id:
                history.append((manifest.run_id, sidecar))
        ordered = tuple(
            sorted(history, key=lambda item: (item[1].created_at_utc, item[0]))
        )
        previous = None
        for _run_id, sidecar in ordered:
            if previous is None:
                if sidecar.superseded_review_id is not None:
                    raise ValueError("first lesson review sidecar supersedes unknown history")
            elif (
                sidecar.superseded_review_id != previous.review_id
                or sidecar.superseded_review_sha256 != previous.sidecar_sha256
            ):
                raise ValueError("lesson review sidecar chain is not contiguous")
            previous = sidecar
        return ordered

    def record_review(
        self,
        candidate_id: str,
        *,
        decision,
        reviewer_id: str,
        reviewer_reference: str,
        reviewer_authority,
        rubric_version: str,
        reasons: tuple[str, ...],
        reason_codes: tuple[str, ...],
        created_at_utc: str,
        repository: Path | None = None,
    ) -> RunManifest:
        from kirby2.mining.reviews import (
            LessonReviewSidecarV1,
            ReviewerAuthorityV1,
        )

        mining_run_id, reviewable = self.find_candidate(candidate_id)
        candidate = reviewable.candidate
        candidate_before = candidate.canonical_bytes()
        history = self.review_history(candidate_id)
        previous = None if not history else history[-1][1]
        if previous is not None:
            if created_at_utc <= previous.created_at_utc:
                raise ValueError("new lesson review timestamp must follow prior history")
            if (
                reviewer_authority is ReviewerAuthorityV1.AUTOMATION
                and previous.reviewer_authority
                is ReviewerAuthorityV1.LOCAL_AUTHENTICATED
            ):
                raise PermissionError("automation cannot supersede a human review")
        sidecar = LessonReviewSidecarV1(
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate.candidate_digest,
            mining_run_id=mining_run_id,
            decision=decision,
            reviewer_id=reviewer_id,
            reviewer_reference=reviewer_reference,
            reviewer_authority=reviewer_authority,
            rubric_version=rubric_version,
            reasons=reasons,
            reason_codes=reason_codes,
            created_at_utc=created_at_utc,
            superseded_review_id=None if previous is None else previous.review_id,
            superseded_review_sha256=(
                None if previous is None else previous.sidecar_sha256
            ),
        )
        payloads = {"review-sidecar.json": sidecar.canonical_bytes()}
        references = _lesson_artifact_references(
            payloads,
            _LESSON_REVIEW_ARTIFACT_SPECS,
            {"review-sidecar.json": 1},
        )
        digests = _lesson_review_digests(sidecar)
        repository_path = repository or Path(__file__).resolve().parents[2]
        manifest = RunManifest.create(
            parent_run_id=mining_run_id,
            run_type=RunType.LESSON_REVIEW,
            scenario_id=None,
            lesson_id=None,
            seed=None,
            flow_model="IMMUTABLE_LESSON_REVIEW_V1",
            market_profile="NOT_APPLICABLE",
            strategy_id="NONE",
            hotkey_layout_id="NONE",
            session_objective="RECORD_SEPARATE_LESSON_REVIEW_DECISION",
            simulation_start_us=candidate.bounds.source_start_us,
            simulation_end_us=candidate.bounds.source_end_us,
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions=_lesson_schema_versions(RunType.LESSON_REVIEW),
            input_dataset_references=(
                f"candidate:{candidate.candidate_id}:{candidate.candidate_digest}",
                *(
                    ()
                    if previous is None
                    else (f"superseded-review:{previous.review_id}",)
                ),
            ),
            configuration_digest=digests[0],
            evidence_digest=digests[1],
            result_digest=digests[2],
            creation_timestamp_utc=sidecar.created_at_utc,
            artifacts=references,
        )
        stored = self._persist(manifest, payloads)
        if candidate.canonical_bytes() != candidate_before:
            raise RuntimeError("recording review mutated immutable lesson candidate")
        return stored

    def build_lesson_proposal(
        self,
        candidate_id: str,
        *,
        created_at_utc: str,
        repository: Path | None = None,
    ) -> RunManifest:
        from kirby2.mining.reviews import (
            LessonBuildProposalV1,
            LessonReviewDecisionV1,
        )

        mining_run_id, reviewable = self.find_candidate(candidate_id)
        history = self.review_history(candidate_id)
        latest = None if not history else history[-1][1]
        if latest is not None and latest.decision in {
            LessonReviewDecisionV1.REJECTED,
            LessonReviewDecisionV1.SUPERSEDED,
        }:
            raise PermissionError("latest human review does not permit a lesson build")
        accepted = bool(
            latest is not None
            and latest.decision is LessonReviewDecisionV1.ACCEPTED
        )
        candidate = reviewable.candidate
        proposal = LessonBuildProposalV1(
            candidate_id=candidate.candidate_id,
            candidate_digest=candidate.candidate_digest,
            mining_run_id=mining_run_id,
            technical_status=reviewable.technical_status,
            human_acceptance_status="ACCEPTED" if accepted else "PENDING",
            source_ancestry_sha256=candidate.source_ancestry.sha256,
            candidate_snapshot_sha256=hashlib.sha256(
                candidate.canonical_bytes()
            ).hexdigest(),
            created_at_utc=created_at_utc,
        )
        payloads = {"lesson-build.json": proposal.canonical_bytes()}
        references = _lesson_artifact_references(
            payloads,
            _LESSON_BUILD_ARTIFACT_SPECS,
            {"lesson-build.json": 1},
        )
        digests = _lesson_build_digests(proposal)
        repository_path = repository or Path(__file__).resolve().parents[2]
        manifest = RunManifest.create(
            parent_run_id=mining_run_id,
            run_type=RunType.LESSON_BUILD,
            scenario_id=None,
            lesson_id=None,
            seed=None,
            flow_model="MINED_LESSON_BUILD_V1",
            market_profile="OUTCOME_CONDITIONED_WINDOW",
            strategy_id="NONE",
            hotkey_layout_id="NONE",
            session_objective="BUILD_TECHNICAL_LESSON_PROPOSAL",
            simulation_start_us=candidate.bounds.warmup_start_us,
            simulation_end_us=candidate.bounds.post_end_us,
            software_version=software_version(),
            git_commit=git_commit(repository_path),
            schema_versions=_lesson_schema_versions(RunType.LESSON_BUILD),
            input_dataset_references=(
                f"candidate:{candidate.candidate_id}:{candidate.candidate_digest}",
            ),
            configuration_digest=digests[0],
            evidence_digest=digests[1],
            result_digest=digests[2],
            creation_timestamp_utc=proposal.created_at_utc,
            artifacts=references,
        )
        return self._persist(manifest, payloads)

    def load_review_sidecar(self, run_id: str):
        from kirby2.mining.reviews import LessonReviewSidecarV1

        manifest = self.load_manifest(run_id)
        if manifest.run_type is not RunType.LESSON_REVIEW:
            raise ValueError("run is not a lesson review")
        raw = (self.run_directory(run_id) / manifest.artifacts[0].relative_path).read_bytes()
        sidecar = LessonReviewSidecarV1.from_dict(json.loads(raw))
        if sidecar.canonical_bytes() != raw:
            raise ValueError("lesson review sidecar is not canonical")
        return sidecar

    def load_build_proposal(self, run_id: str):
        from kirby2.mining.reviews import LessonBuildProposalV1

        manifest = self.load_manifest(run_id)
        if manifest.run_type is not RunType.LESSON_BUILD:
            raise ValueError("run is not a lesson build")
        raw = (self.run_directory(run_id) / manifest.artifacts[0].relative_path).read_bytes()
        proposal = LessonBuildProposalV1.from_dict(json.loads(raw))
        if proposal.canonical_bytes() != raw:
            raise ValueError("lesson build proposal is not canonical")
        return proposal

    def verify_run(self, run_id: str) -> VerificationReport:
        failures: list[str] = []
        flags = {
            "manifest_loaded": False,
            "references_exist": False,
            "artifact_digests_match": False,
            "artifact_row_counts_match": False,
            "event_sequence_complete": False,
            "replay_configuration_available": False,
            "replay_passed": False,
            "result_digest_match": False,
            "evidence_digest_match": False,
            "schema_versions_supported": False,
            "run_identity_match": False,
        }
        try:
            manifest = self.load_manifest(run_id)
            flags["manifest_loaded"] = True
            flags["run_identity_match"] = manifest.run_id == run_id
            if not flags["run_identity_match"]:
                raise ValueError("lesson manifest run ID differs from directory")
        except (OSError, TypeError, ValueError) as error:
            failures.append(f"manifest invalid: {error}")
            return VerificationReport(run_id=run_id, failures=tuple(failures), **flags)
        specs = _lesson_specs(manifest.run_type)
        expected_inventory = tuple(
            (name, path, artifact_type, media_type)
            for name, path, artifact_type, media_type in specs
        )
        actual_inventory = tuple(
            (
                item.name,
                item.relative_path,
                item.artifact_type,
                item.media_type,
            )
            for item in manifest.artifacts
        )
        flags["schema_versions_supported"] = (
            manifest.schema_versions == _lesson_schema_versions(manifest.run_type)
            and actual_inventory == expected_inventory
            and all(item.schema_version == 1 for item in manifest.artifacts)
        )
        if not flags["schema_versions_supported"]:
            failures.append("lesson typed artifact or schema inventory differs")
        directory = self.run_directory(run_id)
        payloads: dict[str, bytes] = {}
        try:
            for reference in manifest.artifacts:
                path = directory / reference.relative_path
                if path.is_symlink() or not path.is_file():
                    raise ValueError(
                        f"unsafe or missing lesson artifact: {reference.relative_path}"
                    )
                payloads[reference.relative_path] = path.read_bytes()
            extras = {
                path.name for path in directory.iterdir() if path.name != "manifest.toml"
            } - {Path(name).name for name in payloads}
            if extras:
                raise ValueError("lesson run contains unregistered artifact files")
            flags["references_exist"] = len(payloads) == len(specs)
            flags["artifact_digests_match"] = all(
                hashlib.sha256(payloads[item.relative_path]).hexdigest() == item.sha256
                for item in manifest.artifacts
            )
            if not flags["artifact_digests_match"]:
                failures.append("one or more lesson artifact digests differ")
        except (OSError, ValueError) as error:
            failures.append(f"lesson artifact inventory invalid: {error}")
        try:
            if manifest.run_type is RunType.LESSON_MINING:
                result = self.load_mining_result(run_id)
                expected_counts = {
                    "qualification-sources.toml": None,
                    "source-validation.json": len(result.source_materializations),
                    "candidates.json": result.candidate_count,
                    "selection.json": result.selection.selected_count,
                    "review-packet.json": len(result.review_packet.rows),
                }
                digests = _lesson_mining_digests(payloads)
                flags["event_sequence_complete"] = all(
                    item.candidate.source_ancestry.source_id
                    == result.source_manifest.row(item.recipe.row_id).identity["source_id"]
                    for item in result.candidates
                )
                flags["replay_configuration_available"] = True
                flags["replay_passed"] = True
            elif manifest.run_type is RunType.LESSON_REVIEW:
                sidecar = self.load_review_sidecar(run_id)
                mining_run_id, candidate = self.find_candidate(sidecar.candidate_id)
                if (
                    mining_run_id != sidecar.mining_run_id
                    or manifest.parent_run_id != mining_run_id
                    or candidate.candidate.candidate_digest != sidecar.candidate_digest
                ):
                    raise ValueError("lesson review sidecar targets foreign candidate")
                expected_counts = {"review-sidecar.json": 1}
                digests = _lesson_review_digests(sidecar)
                flags["event_sequence_complete"] = True
                flags["replay_configuration_available"] = True
                flags["replay_passed"] = True
            else:
                proposal = self.load_build_proposal(run_id)
                mining_run_id, candidate = self.find_candidate(proposal.candidate_id)
                if (
                    mining_run_id != proposal.mining_run_id
                    or manifest.parent_run_id != mining_run_id
                    or candidate.candidate.candidate_digest != proposal.candidate_digest
                ):
                    raise ValueError("lesson build proposal targets foreign candidate")
                expected_counts = {"lesson-build.json": 1}
                digests = _lesson_build_digests(proposal)
                flags["event_sequence_complete"] = True
                flags["replay_configuration_available"] = True
                flags["replay_passed"] = True
            flags["artifact_row_counts_match"] = all(
                reference.row_count == expected_counts[reference.relative_path]
                for reference in manifest.artifacts
            )
            flags["evidence_digest_match"] = manifest.evidence_digest == digests[1]
            flags["result_digest_match"] = manifest.result_digest == digests[2]
            if manifest.configuration_digest != digests[0]:
                failures.append("lesson configuration digest differs")
            if not flags["artifact_row_counts_match"]:
                failures.append("lesson artifact row counts differ")
            if not flags["evidence_digest_match"]:
                failures.append("lesson evidence digest differs")
            if not flags["result_digest_match"]:
                failures.append("lesson result digest differs")
        except Exception as error:
            failures.append(f"lesson artifact replay invalid: {error}")
        return VerificationReport(run_id=run_id, failures=tuple(failures), **flags)

    def _persist(
        self,
        manifest: RunManifest,
        payloads: dict[str, bytes],
    ) -> RunManifest:
        target = self.run_directory(manifest.run_id)
        if target.exists():
            existing = self.load_manifest(manifest.run_id)
            if existing.identity_dict() != manifest.identity_dict():
                raise RuntimeError("content-derived lesson run ID collision")
            report = self.verify_run(manifest.run_id)
            if not report.passed:
                raise RuntimeError(
                    "existing immutable lesson run is invalid: "
                    + "; ".join(report.failures)
                )
            return existing
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix=f"{manifest.run_id}-",
        ) as temporary:
            staging = Path(temporary) / manifest.run_id
            staging.mkdir()
            for relative_path, raw in payloads.items():
                path = staging / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            (staging / "manifest.toml").write_text(
                manifest.to_toml(), encoding="utf-8"
            )
            staging.rename(target)
        report = self.verify_run(manifest.run_id)
        if not report.passed:
            raise RuntimeError(
                "new immutable lesson run failed verification: "
                + "; ".join(report.failures)
            )
        RunStore(self.root).refresh_catalog()
        return self.load_manifest(manifest.run_id)


def _lesson_specs(run_type: RunType):
    return {
        RunType.LESSON_MINING: _LESSON_MINING_ARTIFACT_SPECS,
        RunType.LESSON_REVIEW: _LESSON_REVIEW_ARTIFACT_SPECS,
        RunType.LESSON_BUILD: _LESSON_BUILD_ARTIFACT_SPECS,
    }[run_type]


def _lesson_schema_versions(run_type: RunType) -> dict[str, int]:
    key = {
        RunType.LESSON_MINING: "lesson_mining",
        RunType.LESSON_REVIEW: "lesson_review",
        RunType.LESSON_BUILD: "lesson_build",
    }[run_type]
    return {key: 1, "run_manifest": RUN_MANIFEST_SCHEMA_VERSION}


def _lesson_artifact_references(
    payloads: dict[str, bytes],
    specs,
    row_counts: dict[str, int],
) -> tuple[ArtifactReference, ...]:
    expected_paths = {path for _name, path, _type, _media in specs}
    if set(payloads) != expected_paths:
        raise ValueError("lesson artifact payload inventory differs")
    return tuple(
        ArtifactReference(
            name=name,
            relative_path=relative_path,
            sha256=hashlib.sha256(payloads[relative_path]).hexdigest(),
            schema_version=1,
            row_count=row_counts.get(relative_path),
            media_type=media_type,
            artifact_type=artifact_type,
        )
        for name, relative_path, artifact_type, media_type in specs
    )


def _lesson_mining_digests(payloads: dict[str, bytes]) -> tuple[str, str, str]:
    source_sha = hashlib.sha256(payloads["qualification-sources.toml"]).hexdigest()
    validation_sha = hashlib.sha256(payloads["source-validation.json"]).hexdigest()
    candidates_sha = hashlib.sha256(payloads["candidates.json"]).hexdigest()
    selection_sha = hashlib.sha256(payloads["selection.json"]).hexdigest()
    packet_sha = hashlib.sha256(payloads["review-packet.json"]).hexdigest()
    return (
        canonical_digest(
            {"policy": "LESSON_MINING_V1", "source_matrix_sha256": source_sha}
        ),
        canonical_digest(
            {
                "candidates_sha256": candidates_sha,
                "source_validation_sha256": validation_sha,
            }
        ),
        canonical_digest(
            {
                "review_packet_sha256": packet_sha,
                "selection_sha256": selection_sha,
            }
        ),
    )


def _lesson_review_digests(sidecar) -> tuple[str, str, str]:
    return (
        canonical_digest(
            {
                "reviewer_authority": sidecar.reviewer_authority.value,
                "rubric_version": sidecar.rubric_version,
            }
        ),
        sidecar.candidate_digest,
        sidecar.sidecar_sha256,
    )


def _lesson_build_digests(proposal) -> tuple[str, str, str]:
    return (
        proposal.candidate_snapshot_sha256,
        proposal.source_ancestry_sha256,
        proposal.proposal_sha256,
    )


def _require_strategy_discovery_digest(value: object, context: str) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{context} identity must be lowercase SHA-256")


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
    "full_day_qualification_artifacts",
    "lesson_mining_artifacts",
    "learner_artifacts",
    "strategy_discovery_artifacts",
    "dataset_provenance",
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


def _create_fact_views(connection, runs_directory: Path) -> None:
    for spec in TABLE_SPECS:
        view_name = f"all_{spec.name}"
        paths = tuple(
            sorted(runs_directory.glob(f"run-*/tables/{spec.name}.parquet"))
        )
        if paths:
            path_rows = ",".join(
                "'" + str(path.resolve()).replace("'", "''") + "'"
                for path in paths
            )
            connection.execute(
                f'CREATE VIEW "{view_name}" AS '
                f"SELECT * FROM read_parquet([{path_rows}], union_by_name = true)"
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
        CREATE VIEW full_day_qualification_artifacts AS
        SELECT * FROM run_artifact_registry
        WHERE artifact_type IN (
            'FULL_DAY_PROFILE_QUALIFICATION',
            'FULL_DAY_QUALIFICATION_RUN_PROOFS',
            'FULL_DAY_REVIEW_SOURCE',
            'FULL_DAY_REVIEW_SELECTION',
            'FULL_DAY_REVIEW_PACKET',
            'FULL_DAY_PERFORMANCE_EVIDENCE',
            'FULL_DAY_QUALIFICATION_LEDGER',
            'FULL_DAY_REVEAL_TOKEN',
            'FULL_DAY_REVIEWER_SIDECAR'
        )
        """
    )
    connection.execute(
        """
        CREATE VIEW lesson_mining_artifacts AS
        SELECT * FROM run_artifact_registry
        WHERE artifact_type IN (
            'LESSON_MINING_SOURCE_MATRIX',
            'LESSON_MINING_SOURCE_VALIDATION',
            'LESSON_MINING_CANDIDATES',
            'LESSON_MINING_SELECTION',
            'LESSON_TECHNICAL_REVIEW_PACKET',
            'LESSON_REVIEW_SIDECAR',
            'LESSON_BUILD_PROPOSAL'
        )
        """
    )
    connection.execute(
        """
        CREATE VIEW learner_artifacts AS
        SELECT * FROM run_artifact_registry
        WHERE artifact_type IN (
            'LEARNER_EVIDENCE_UPDATE',
            'LEARNER_STATE_PROJECTION'
        )
        """
    )
    connection.execute(
        """
        CREATE VIEW strategy_discovery_artifacts AS
        SELECT * FROM run_artifact_registry
        WHERE artifact_type IN (
            'STRATEGY_DISCOVERY_BINDING',
            'STRATEGY_DISCOVERY_RECORD',
            'STRATEGY_LINEAGE_REPORT',
            'STRATEGY_REVEAL_TOKEN',
            'STRATEGY_SCIENTIFIC_OUTCOME'
        )
        """
    )
    connection.execute(
        """
        CREATE VIEW dataset_provenance AS
        SELECT dataset_id, adapter, source_locator, source_name, license_note,
               real_market_data, capability, tick_size,
               source_digest, records_digest, quality_digest, replay_mode,
               exact_replay_allowed, time_start_ns, time_end_ns,
               symbols_toml, session_count, creation_timestamp_utc
        FROM dataset_registry
        """
    )
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
