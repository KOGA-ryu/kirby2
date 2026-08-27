"""Content-addressed immutable storage for counterfactual branch evidence."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kirby2 import __version__
from kirby2.multivenue.models import canonical_sha256
from kirby2.research.toml_codec import canonical_digest, canonical_toml, file_sha256, load_toml

from .models import COUNTERFACTUAL_SCHEMA_VERSION, CounterfactualReport


DEFAULT_COUNTERFACTUAL_STORE = Path(".kirby2") / "research" / "counterfactual_runs"
_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")


@dataclass(frozen=True, slots=True)
class ImmutableCounterfactualManifest:
    run_id: str
    parent_run_id: str
    mode: str
    mutation_manifest_sha256: str
    branch_snapshot_sha256: str
    report_artifact_sha256: str
    result_sha256: str
    artifact_sha256: dict[str, str]
    software_version: str
    record_label: str = "IMMUTABLE_COUNTERFACTUAL_BRANCH_RECORD"
    schema_version: int = COUNTERFACTUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _RUN_ID.fullmatch(self.run_id) or not _RUN_ID.fullmatch(self.parent_run_id):
            raise ValueError("counterfactual run or parent ID is invalid")
        if self.schema_version != COUNTERFACTUAL_SCHEMA_VERSION:
            raise ValueError("unsupported counterfactual record schema")
        if self.run_id != "run-" + canonical_sha256(self.identity_dict())[:24]:
            raise ValueError("counterfactual run ID does not match content identity")

    def identity_dict(self) -> dict[str, object]:
        return {
            "artifact_sha256": dict(sorted(self.artifact_sha256.items())),
            "branch_snapshot_sha256": self.branch_snapshot_sha256,
            "mode": self.mode,
            "mutation_manifest_sha256": self.mutation_manifest_sha256,
            "parent_run_id": self.parent_run_id,
            "record_label": self.record_label,
            "report_artifact_sha256": self.report_artifact_sha256,
            "result_sha256": self.result_sha256,
            "schema_version": self.schema_version,
            "software_version": self.software_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "run_id": self.run_id}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ImmutableCounterfactualManifest:
        expected = {
            "artifact_sha256",
            "branch_snapshot_sha256",
            "mode",
            "mutation_manifest_sha256",
            "parent_run_id",
            "record_label",
            "report_artifact_sha256",
            "result_sha256",
            "run_id",
            "schema_version",
            "software_version",
        }
        if set(payload) != expected:
            raise ValueError("counterfactual manifest field inventory is invalid")
        artifacts = payload.get("artifact_sha256")
        if not isinstance(artifacts, dict):
            raise ValueError("counterfactual artifact digest inventory is invalid")
        return cls(
            run_id=str(payload["run_id"]),
            parent_run_id=str(payload["parent_run_id"]),
            mode=str(payload["mode"]),
            mutation_manifest_sha256=str(payload["mutation_manifest_sha256"]),
            branch_snapshot_sha256=str(payload["branch_snapshot_sha256"]),
            report_artifact_sha256=str(payload["report_artifact_sha256"]),
            result_sha256=str(payload["result_sha256"]),
            artifact_sha256={str(key): str(value) for key, value in artifacts.items()},
            software_version=str(payload["software_version"]),
            record_label=str(payload["record_label"]),
            schema_version=int(payload["schema_version"]),
        )


@dataclass(frozen=True, slots=True)
class CounterfactualVerification:
    run_id: str
    artifacts_exist: bool
    artifact_digests_match: bool
    content_identity_match: bool
    parent_link_present: bool
    snapshot_and_mutation_match: bool
    result_digest_match: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.artifacts_exist,
                self.artifact_digests_match,
                self.content_identity_match,
                self.parent_link_present,
                self.snapshot_and_mutation_match,
                self.result_digest_match,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests_match": self.artifact_digests_match,
            "artifacts_exist": self.artifacts_exist,
            "content_identity_match": self.content_identity_match,
            "failures": list(self.failures),
            "parent_link_present": self.parent_link_present,
            "result_digest_match": self.result_digest_match,
            "run_id": self.run_id,
            "snapshot_and_mutation_match": self.snapshot_and_mutation_match,
            "status": "PASS" if self.passed else "FAIL",
        }


class CounterfactualStore:
    def __init__(self, root: Path = DEFAULT_COUNTERFACTUAL_STORE) -> None:
        self.root = root
        self.runs_directory = root / "runs"
        self.staging_directory = root / ".staging"
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.staging_directory.mkdir(parents=True, exist_ok=True)

    def record(self, report: CounterfactualReport) -> ImmutableCounterfactualManifest:
        artifacts_payload = {
            "mutation_manifest.toml": {
                "mutation_manifest": report.mutation_manifest.as_dict()
            },
            "branch_snapshot.toml": {"branch_snapshot": report.snapshot.as_dict()},
            "report.toml": {"report": report.as_dict()},
        }
        artifact_text = {
            name: canonical_toml(payload)
            for name, payload in artifacts_payload.items()
        }
        artifact_sha = {
            name: _text_sha256(text) for name, text in artifact_text.items()
        }
        report_artifact_sha = canonical_digest(artifacts_payload["report.toml"])
        identity = {
            "artifact_sha256": artifact_sha,
            "branch_snapshot_sha256": report.snapshot.sha256(),
            "mode": report.mode.value,
            "mutation_manifest_sha256": report.mutation_manifest.sha256(),
            "parent_run_id": report.parent_run_id,
            "record_label": "IMMUTABLE_COUNTERFACTUAL_BRANCH_RECORD",
            "report_artifact_sha256": report_artifact_sha,
            "result_sha256": report.result_sha256(),
            "schema_version": COUNTERFACTUAL_SCHEMA_VERSION,
            "software_version": __version__,
        }
        run_id = "run-" + canonical_sha256(identity)[:24]
        target = self.run_directory(run_id)
        if target.exists():
            verification = self.verify_run(run_id)
            if not verification.passed:
                raise RuntimeError(
                    "existing immutable counterfactual run is invalid and will not be overwritten: "
                    + "; ".join(verification.failures)
                )
            return self.load_manifest(run_id)
        with tempfile.TemporaryDirectory(
            dir=self.staging_directory,
            prefix=f"{run_id}-",
        ) as temporary:
            staging = Path(temporary) / run_id
            staging.mkdir()
            for name, text in artifact_text.items():
                (staging / name).write_text(text, encoding="utf-8")
            actual_sha = {
                name: file_sha256(staging / name) for name in artifact_text
            }
            if actual_sha != artifact_sha:
                raise RuntimeError("counterfactual artifact bytes changed while writing")
            manifest = ImmutableCounterfactualManifest(
                run_id,
                report.parent_run_id,
                report.mode.value,
                report.mutation_manifest.sha256(),
                report.snapshot.sha256(),
                report_artifact_sha,
                report.result_sha256(),
                actual_sha,
                __version__,
            )
            (staging / "manifest.toml").write_text(
                canonical_toml({"manifest": manifest.as_dict()}),
                encoding="utf-8",
            )
            staging.rename(target)
        verification = self.verify_run(run_id)
        if not verification.passed:
            raise RuntimeError(
                "new immutable counterfactual run failed verification: "
                + "; ".join(verification.failures)
            )
        return self.load_manifest(run_id)

    def run_directory(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid counterfactual run ID")
        return self.runs_directory / run_id

    def load_manifest(self, run_id: str) -> ImmutableCounterfactualManifest:
        path = self.run_directory(run_id) / "manifest.toml"
        if not path.is_file():
            raise ValueError(f"unknown counterfactual run ID: {run_id}")
        payload = load_toml(path).get("manifest")
        if not isinstance(payload, dict):
            raise ValueError("counterfactual manifest artifact is invalid")
        return ImmutableCounterfactualManifest.from_dict(payload)

    def load_report_payload(self, run_id: str) -> dict[str, object]:
        payload = load_toml(self.run_directory(run_id) / "report.toml").get("report")
        if not isinstance(payload, dict):
            raise ValueError("counterfactual report artifact is invalid")
        return payload

    def verify_run(self, run_id: str) -> CounterfactualVerification:
        failures: list[str] = []
        try:
            manifest = self.load_manifest(run_id)
        except (OSError, TypeError, ValueError) as error:
            return CounterfactualVerification(
                run_id,
                False,
                False,
                False,
                False,
                False,
                False,
                (str(error),),
            )
        directory = self.run_directory(run_id)
        artifacts_exist = all(
            (directory / name).is_file() for name in manifest.artifact_sha256
        )
        if not artifacts_exist:
            failures.append("one or more counterfactual artifacts are missing")
        actual_sha = (
            {
                name: file_sha256(directory / name)
                for name in manifest.artifact_sha256
            }
            if artifacts_exist
            else {}
        )
        artifact_digests_match = actual_sha == manifest.artifact_sha256
        if not artifact_digests_match:
            failures.append("counterfactual artifact digest mismatch")
        manifest_path = directory / "manifest.toml"
        manifest_bytes_canonical = manifest_path.read_text(
            encoding="utf-8"
        ) == canonical_toml({"manifest": manifest.as_dict()})
        content_identity_match = (
            manifest.run_id == run_id
            and manifest.run_id
            == "run-" + canonical_sha256(manifest.identity_dict())[:24]
            and manifest_bytes_canonical
        )
        if not content_identity_match:
            failures.append("counterfactual content identity mismatch")
        parent_link_present = _RUN_ID.fullmatch(manifest.parent_run_id) is not None
        if not parent_link_present:
            failures.append("counterfactual parent linkage is missing")
        snapshot_and_mutation_match = False
        result_digest_match = False
        if artifacts_exist:
            try:
                mutation = load_toml(directory / "mutation_manifest.toml").get(
                    "mutation_manifest"
                )
                snapshot = load_toml(directory / "branch_snapshot.toml").get(
                    "branch_snapshot"
                )
                report = self.load_report_payload(run_id)
                snapshot_and_mutation_match = (
                    isinstance(mutation, dict)
                    and isinstance(snapshot, dict)
                    and canonical_sha256(mutation) == manifest.mutation_manifest_sha256
                    and canonical_sha256(snapshot) == manifest.branch_snapshot_sha256
                    and report.get("parent_run_id") == manifest.parent_run_id
                    and report.get("mode") == manifest.mode
                    and report.get("mutation_manifest") == mutation
                    and report.get("snapshot") == snapshot
                )
                result_digest_match = (
                    canonical_sha256(report) == manifest.result_sha256
                    and canonical_digest({"report": report})
                    == manifest.report_artifact_sha256
                )
            except (OSError, TypeError, ValueError):
                snapshot_and_mutation_match = False
                result_digest_match = False
        if not snapshot_and_mutation_match:
            failures.append("snapshot or mutation manifest linkage mismatch")
        if not result_digest_match:
            failures.append("counterfactual result digest mismatch")
        return CounterfactualVerification(
            run_id,
            artifacts_exist,
            artifact_digests_match,
            content_identity_match,
            parent_link_present,
            snapshot_and_mutation_match,
            result_digest_match,
            tuple(failures),
        )


def _text_sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
