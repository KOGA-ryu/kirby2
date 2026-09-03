"""Strict public verification receipt for finalized simulation Replay artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .simulation_artifact_contract import ReplayArtifactRefV1
from .simulation_contract import (
    SimulationContractIntegrityError,
    _digest,
    _enum,
    _exact,
    _freeze,
    _object,
    _plain,
    _snapshot,
)


REPLAY_VERIFICATION_RECEIPT_SCHEMA_ID = (
    "KIRBY2_REPLAY_ARTIFACT_VERIFICATION_RECEIPT_V1"
)
REPLAY_VERIFICATION_UNAVAILABLE_REASONS = frozenset(
    {
        "UNKNOWN_STORE",
        "OBJECT_NOT_FOUND",
        "UNSUPPORTED_ARTIFACT_SCHEMA",
        "REFERENCE_MISMATCH",
        "ARTIFACT_DIGEST_MISMATCH",
        "RECORDING_DIGEST_MISMATCH",
        "EVENT_TAPE_DIGEST_MISMATCH",
    }
)

_RECEIPT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "status",
        "artifact_ref",
        "verified_artifact_sha256",
        "verified_recording_sha256",
        "verified_event_tape_sha256",
        "source_run_id",
        "replay_run_id",
        "unavailable_reason",
    }
)


@dataclass(frozen=True, slots=True)
class ReplayArtifactVerificationReceiptV1:
    status: str
    artifact_ref: ReplayArtifactRefV1
    verified_artifact_sha256: str | None
    verified_recording_sha256: str | None
    verified_event_tape_sha256: str | None
    source_run_id: str | None
    replay_run_id: str | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> ReplayArtifactVerificationReceiptV1:
        root = _object(_snapshot(payload), "Replay artifact verification receipt")
        _exact(root, _RECEIPT_FIELDS, "Replay artifact verification receipt")
        if (
            root["schema_id"] != REPLAY_VERIFICATION_RECEIPT_SCHEMA_ID
            or type(root["schema_version"]) is not int
            or root["schema_version"] != 1
        ):
            raise ValueError("Replay artifact verification receipt schema is unsupported")
        status = _enum(
            root["status"],
            frozenset({"AVAILABLE", "UNAVAILABLE"}),
            "Replay artifact verification receipt.status",
        )
        reference = ReplayArtifactRefV1.from_dict(
            _object(root["artifact_ref"], "Replay verification artifact reference")
        )
        if status == "AVAILABLE":
            artifact_sha256 = _digest(
                root["verified_artifact_sha256"],
                "Replay verification artifact digest",
            )
            recording_sha256 = _digest(
                root["verified_recording_sha256"],
                "Replay verification recording digest",
            )
            event_tape_sha256 = _digest(
                root["verified_event_tape_sha256"],
                "Replay verification event-tape digest",
            )
            source_run_id = root["source_run_id"]
            replay_run_id = root["replay_run_id"]
            if (
                type(source_run_id) is not str
                or type(replay_run_id) is not str
                or root["unavailable_reason"] is not None
                or artifact_sha256 != reference.artifact_sha256
                or source_run_id != reference.source_run_id
                or replay_run_id != reference.replay_run_id
            ):
                raise SimulationContractIntegrityError(
                    "available Replay verification receipt differs from its reference"
                )
            unavailable_reason = None
        else:
            if any(
                root[field] is not None
                for field in (
                    "verified_artifact_sha256",
                    "verified_recording_sha256",
                    "verified_event_tape_sha256",
                    "source_run_id",
                    "replay_run_id",
                )
            ):
                raise ValueError(
                    "unavailable Replay verification receipt exposes verified identity"
                )
            artifact_sha256 = None
            recording_sha256 = None
            event_tape_sha256 = None
            source_run_id = None
            replay_run_id = None
            unavailable_reason = _enum(
                root["unavailable_reason"],
                REPLAY_VERIFICATION_UNAVAILABLE_REASONS,
                "Replay artifact verification unavailable reason",
            )
        normalized = {
            "schema_id": REPLAY_VERIFICATION_RECEIPT_SCHEMA_ID,
            "schema_version": 1,
            "status": status,
            "artifact_ref": reference.as_dict(),
            "verified_artifact_sha256": artifact_sha256,
            "verified_recording_sha256": recording_sha256,
            "verified_event_tape_sha256": event_tape_sha256,
            "source_run_id": source_run_id,
            "replay_run_id": replay_run_id,
            "unavailable_reason": unavailable_reason,
        }
        return cls(
            status,
            reference,
            artifact_sha256,
            recording_sha256,
            event_tape_sha256,
            source_run_id,
            replay_run_id,
            unavailable_reason,
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


__all__ = [
    "REPLAY_VERIFICATION_RECEIPT_SCHEMA_ID",
    "REPLAY_VERIFICATION_UNAVAILABLE_REASONS",
    "ReplayArtifactVerificationReceiptV1",
]
