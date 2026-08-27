"""Content-addressed immutable records for execution benchmark runs."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kirby2 import __version__
from kirby2.multivenue import (
    MultiVenueRecording,
    replay_multivenue_recording,
)
from kirby2.multivenue.models import canonical_sha256
from kirby2.research.toml_codec import file_sha256

from .models import (
    ALGORITHM_RECORD_SCHEMA_VERSION,
    AlgorithmDecision,
    AlgorithmParameterManifest,
    ExecutionBenchmarkMetrics,
)


DEFAULT_ALGORITHM_RUN_STORE = Path(".kirby2") / "research" / "algorithm_runs"


@dataclass(frozen=True, slots=True)
class AlgorithmRunArtifacts:
    experiment_id: str
    scenario_name: str
    seed: int
    algorithm_manifest: AlgorithmParameterManifest
    fork_state_sha256: str
    background_path_sha256: str
    decisions: tuple[AlgorithmDecision, ...]
    recording: MultiVenueRecording
    metrics: ExecutionBenchmarkMetrics


@dataclass(frozen=True, slots=True)
class ImmutableAlgorithmRunManifest:
    run_id: str
    experiment_id: str
    scenario_name: str
    seed: int
    algorithm: str
    algorithm_manifest_sha256: str
    fork_state_sha256: str
    background_path_sha256: str
    recording_sha256: str
    decision_trace_sha256: str
    metrics_sha256: str
    result_sha256: str
    artifact_sha256: dict[str, str]
    software_version: str
    record_label: str = "IMMUTABLE_ALGORITHM_RUN_RECORD"
    schema_version: int = ALGORITHM_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ALGORITHM_RECORD_SCHEMA_VERSION:
            raise ValueError("unsupported algorithm run record schema")
        if self.run_id != "run-" + canonical_sha256(self.identity_dict())[:24]:
            raise ValueError("algorithm run ID does not match canonical content identity")

    def identity_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "algorithm_manifest_sha256": self.algorithm_manifest_sha256,
            "artifact_sha256": dict(sorted(self.artifact_sha256.items())),
            "background_path_sha256": self.background_path_sha256,
            "decision_trace_sha256": self.decision_trace_sha256,
            "experiment_id": self.experiment_id,
            "fork_state_sha256": self.fork_state_sha256,
            "metrics_sha256": self.metrics_sha256,
            "record_label": self.record_label,
            "recording_sha256": self.recording_sha256,
            "result_sha256": self.result_sha256,
            "scenario_name": self.scenario_name,
            "schema_version": self.schema_version,
            "seed": self.seed,
            "software_version": self.software_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ImmutableAlgorithmRunManifest:
        expected = {
            "algorithm",
            "algorithm_manifest_sha256",
            "artifact_sha256",
            "background_path_sha256",
            "decision_trace_sha256",
            "experiment_id",
            "fork_state_sha256",
            "metrics_sha256",
            "record_label",
            "recording_sha256",
            "result_sha256",
            "run_id",
            "scenario_name",
            "schema_version",
            "seed",
            "software_version",
        }
        if set(payload) != expected:
            raise ValueError("algorithm run manifest field inventory is invalid")
        raw = payload.get("artifact_sha256")
        if not isinstance(raw, dict):
            raise ValueError("algorithm run artifact digest inventory is missing")
        return cls(
            run_id=str(payload["run_id"]),
            experiment_id=str(payload["experiment_id"]),
            scenario_name=str(payload["scenario_name"]),
            seed=int(payload["seed"]),
            algorithm=str(payload["algorithm"]),
            algorithm_manifest_sha256=str(payload["algorithm_manifest_sha256"]),
            fork_state_sha256=str(payload["fork_state_sha256"]),
            background_path_sha256=str(payload["background_path_sha256"]),
            recording_sha256=str(payload["recording_sha256"]),
            decision_trace_sha256=str(payload["decision_trace_sha256"]),
            metrics_sha256=str(payload["metrics_sha256"]),
            result_sha256=str(payload["result_sha256"]),
            artifact_sha256={str(key): str(value) for key, value in raw.items()},
            software_version=str(payload["software_version"]),
            record_label=str(payload["record_label"]),
            schema_version=int(payload["schema_version"]),
        )


@dataclass(frozen=True, slots=True)
class AlgorithmRunVerification:
    run_id: str
    artifacts_exist: bool
    artifact_digests_match: bool
    content_identity_match: bool
    recording_replay_passed: bool
    result_digest_match: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.artifacts_exist,
                self.artifact_digests_match,
                self.content_identity_match,
                self.recording_replay_passed,
                self.result_digest_match,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests_match": self.artifact_digests_match,
            "artifacts_exist": self.artifacts_exist,
            "content_identity_match": self.content_identity_match,
            "failures": list(self.failures),
            "recording_replay_passed": self.recording_replay_passed,
            "result_digest_match": self.result_digest_match,
            "run_id": self.run_id,
            "status": "PASS" if self.passed else "FAIL",
        }


class AlgorithmRunStore:
    def __init__(self, root: Path = DEFAULT_ALGORITHM_RUN_STORE) -> None:
        self.root = root
        self.runs_directory = root / "runs"
        self.staging_directory = root / ".staging"
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)

    def record(self, artifacts: AlgorithmRunArtifacts) -> ImmutableAlgorithmRunManifest:
        configuration = {
            "algorithm_manifest": artifacts.algorithm_manifest.as_dict(),
            "background_path_sha256": artifacts.background_path_sha256,
            "experiment_id": artifacts.experiment_id,
            "fork_state_sha256": artifacts.fork_state_sha256,
            "scenario_name": artifacts.scenario_name,
            "seed": artifacts.seed,
            "software_version": __version__,
        }
        decisions = [decision.as_dict() for decision in artifacts.decisions]
        recording = artifacts.recording.as_dict()
        metrics = artifacts.metrics.as_dict()
        content = {
            "configuration": configuration,
            "decisions": decisions,
            "metrics": metrics,
            "recording": recording,
        }
        digests = {key: canonical_sha256(value) for key, value in content.items()}
        artifact_sha256 = {
            f"{name}.json": _json_bytes_sha256(payload)
            for name, payload in content.items()
        }
        result_sha256 = canonical_sha256(
            {
                "decision_trace_sha256": digests["decisions"],
                "metrics_sha256": digests["metrics"],
                "recording_sha256": artifacts.recording.sha256(),
            }
        )
        identity = {
            "algorithm": artifacts.algorithm_manifest.algorithm.value,
            "algorithm_manifest_sha256": artifacts.algorithm_manifest.sha256(),
            "artifact_sha256": artifact_sha256,
            "background_path_sha256": artifacts.background_path_sha256,
            "decision_trace_sha256": digests["decisions"],
            "experiment_id": artifacts.experiment_id,
            "fork_state_sha256": artifacts.fork_state_sha256,
            "metrics_sha256": digests["metrics"],
            "record_label": "IMMUTABLE_ALGORITHM_RUN_RECORD",
            "recording_sha256": artifacts.recording.sha256(),
            "result_sha256": result_sha256,
            "scenario_name": artifacts.scenario_name,
            "schema_version": ALGORITHM_RECORD_SCHEMA_VERSION,
            "seed": artifacts.seed,
            "software_version": __version__,
        }
        run_id = "run-" + canonical_sha256(identity)[:24]
        target = self.runs_directory / run_id
        if target.exists():
            report = self.verify_run(run_id)
            if not report.passed:
                raise RuntimeError(
                    "existing immutable algorithm run is invalid and will not be overwritten: "
                    + "; ".join(report.failures)
                )
            return self.load_manifest(run_id)
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix=f"{run_id}-",
        ) as temporary:
            staging = Path(temporary) / run_id
            staging.mkdir()
            paths: dict[str, Path] = {}
            for name, payload in content.items():
                path = staging / f"{name}.json"
                _write_json(path, payload)
                paths[f"{name}.json"] = path
            actual_artifact_sha256 = {
                name: file_sha256(path) for name, path in paths.items()
            }
            if actual_artifact_sha256 != artifact_sha256:
                raise RuntimeError("canonical algorithm artifact bytes changed while writing")
            manifest = ImmutableAlgorithmRunManifest(
                run_id=run_id,
                experiment_id=artifacts.experiment_id,
                scenario_name=artifacts.scenario_name,
                seed=artifacts.seed,
                algorithm=artifacts.algorithm_manifest.algorithm.value,
                algorithm_manifest_sha256=artifacts.algorithm_manifest.sha256(),
                fork_state_sha256=artifacts.fork_state_sha256,
                background_path_sha256=artifacts.background_path_sha256,
                recording_sha256=artifacts.recording.sha256(),
                decision_trace_sha256=digests["decisions"],
                metrics_sha256=digests["metrics"],
                result_sha256=result_sha256,
                artifact_sha256=actual_artifact_sha256,
                software_version=__version__,
            )
            _write_json(staging / "manifest.json", manifest.as_dict())
            staging.rename(target)
        report = self.verify_run(run_id)
        if not report.passed:
            raise RuntimeError(
                "new immutable algorithm run failed verification: "
                + "; ".join(report.failures)
            )
        return self.load_manifest(run_id)

    def load_manifest(self, run_id: str) -> ImmutableAlgorithmRunManifest:
        if not re.fullmatch(r"run-[0-9a-f]{24}", run_id):
            raise ValueError("invalid immutable algorithm run ID")
        path = self.runs_directory / run_id / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("algorithm run manifest is invalid")
        return ImmutableAlgorithmRunManifest.from_dict(payload)

    def load_recording(self, run_id: str) -> MultiVenueRecording:
        verification = self.verify_run(run_id)
        if not verification.passed:
            raise ValueError(
                "immutable algorithm run failed verification: "
                + "; ".join(verification.failures)
            )
        payload = _read_json(self.runs_directory / run_id / "recording.json")
        if not isinstance(payload, dict):
            raise ValueError("algorithm run recording artifact is invalid")
        return MultiVenueRecording.from_dict(payload)

    def verify_run(self, run_id: str) -> AlgorithmRunVerification:
        failures: list[str] = []
        try:
            manifest = self.load_manifest(run_id)
            content_identity_match = manifest.run_id == run_id
        except (OSError, TypeError, ValueError) as error:
            return AlgorithmRunVerification(
                run_id,
                False,
                False,
                False,
                False,
                False,
                (f"manifest invalid: {error}",),
            )
        directory = self.runs_directory / run_id
        paths = {
            name: directory / name for name in manifest.artifact_sha256
        }
        artifacts_exist = set(paths) == {
            "configuration.json",
            "decisions.json",
            "metrics.json",
            "recording.json",
        } and all(path.is_file() for path in paths.values())
        if not artifacts_exist:
            failures.append("algorithm run artifact inventory is incomplete")
        artifact_digests_match = artifacts_exist and all(
            file_sha256(path) == manifest.artifact_sha256[name]
            for name, path in paths.items()
        )
        if not artifact_digests_match:
            failures.append("algorithm run artifact digest differs")
        recording_replay_passed = False
        result_digest_match = False
        if artifacts_exist and artifact_digests_match:
            try:
                configuration = _read_json(paths["configuration.json"])
                decisions = _read_json(paths["decisions.json"])
                metrics = _read_json(paths["metrics.json"])
                recording_payload = _read_json(paths["recording.json"])
                recording = MultiVenueRecording.from_dict(recording_payload)
                replay = replay_multivenue_recording(recording)
                recording_replay_passed = replay.passed
                if not isinstance(decisions, list) or any(
                    not isinstance(item, dict)
                    or canonical_sha256(item.get("observation"))
                    != item.get("observation_sha256")
                    or item.get("manifest_sha256")
                    != manifest.algorithm_manifest_sha256
                    for item in decisions
                ):
                    raise ValueError("algorithm decision evidence digest is invalid")
                if (
                    canonical_sha256(configuration["algorithm_manifest"])
                    != manifest.algorithm_manifest_sha256
                    or recording.sha256() != manifest.recording_sha256
                    or canonical_sha256(decisions) != manifest.decision_trace_sha256
                    or canonical_sha256(metrics) != manifest.metrics_sha256
                ):
                    raise ValueError("algorithm manifest or content digest differs")
                actual_result = canonical_sha256(
                    {
                        "decision_trace_sha256": canonical_sha256(decisions),
                        "metrics_sha256": canonical_sha256(metrics),
                        "recording_sha256": recording.sha256(),
                    }
                )
                result_digest_match = (
                    actual_result == manifest.result_sha256
                    and canonical_sha256(configuration)
                    == canonical_sha256(
                        {
                            "algorithm_manifest": configuration["algorithm_manifest"],
                            "background_path_sha256": manifest.background_path_sha256,
                            "experiment_id": manifest.experiment_id,
                            "fork_state_sha256": manifest.fork_state_sha256,
                            "scenario_name": manifest.scenario_name,
                            "seed": manifest.seed,
                            "software_version": manifest.software_version,
                        }
                    )
                )
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                failures.append(f"algorithm run replay invalid: {error}")
        if not recording_replay_passed:
            failures.append("algorithm run exact replay failed")
        if not result_digest_match:
            failures.append("algorithm run result digest differs")
        if not content_identity_match:
            failures.append("algorithm run content identity differs")
        return AlgorithmRunVerification(
            run_id,
            artifacts_exist,
            artifact_digests_match,
            content_identity_match,
            recording_replay_passed,
            result_digest_match,
            tuple(failures),
        )


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_json_bytes(payload))


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _json_bytes_sha256(payload: object) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()
