"""Governed resolution and deep verification of simulation Replay artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.packs.formats import load_canonical_json_bytes
from kirby2.session.live import LiveMarketSession, SessionSnapshot
from kirby2.session.replay import SessionRecording

from .simulation_artifact_contract import (
    ARTIFACT_SCHEMA_ID,
    RECORDING_ENCODING,
    RECORDING_MEDIA_TYPE,
    RECORDING_SCHEMA_VERSION,
    EmbeddedSessionRecordingV1,
    ReplayArtifactRefV1,
    SimulationReplayArtifactV1,
)
from .simulation_artifact_store import (
    SIMULATION_REPLAY_STORE_ID,
    _read_simulation_replay_artifact,
)
from .simulation_contract import (
    PROFILE_RESOLUTION_SCHEMA_ID,
    SimulationContractDecodeError,
    SimulationContractIntegrityError,
    SimulationProfileResolutionV1,
    SimulationResolutionRefusal,
    canonical_digest,
)
from .simulation_live_contract import (
    SimulationStartRefusal,
    SimulationTrainingOptionsV1,
)
from .simulation_replay_contract import (
    REPLAY_VERIFICATION_RECEIPT_SCHEMA_ID,
    ReplayArtifactVerificationReceiptV1,
)
from .simulation_facade import _catalog_state
from .simulation_run_facade import _frame, _materialize_session


class _VerificationFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _VerifiedReplayReconstruction:
    resolution: SimulationProfileResolutionV1
    training_options: SimulationTrainingOptionsV1
    snapshots: tuple[SessionSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _VerifiedSimulationReplaySource:
    artifact_ref: ReplayArtifactRefV1
    artifact: SimulationReplayArtifactV1
    artifact_bytes: bytes
    recording: SessionRecording
    reconstruction: _VerifiedReplayReconstruction


def _resolution_from_artifact(
    artifact: SimulationReplayArtifactV1,
) -> tuple[SimulationProfileResolutionV1, SimulationTrainingOptionsV1]:
    root = artifact.as_dict()
    selection = root["selection"]
    configuration = root["resolved_configuration"]
    training = root["training_options"]
    if (
        type(selection) is not dict
        or type(configuration) is not dict
        or type(training) is not dict
    ):
        raise _VerificationFailure("REFERENCE_MISMATCH")
    resolution_record = {
        "schema_id": PROFILE_RESOLUTION_SCHEMA_ID,
        "schema_version": 1,
        "status": "AVAILABLE",
        "selection": selection,
        "selection_sha256": configuration["selection_sha256"],
        "resolved_configuration_sha256": root["resolved_configuration_sha256"],
        "resolved_configuration": configuration,
        "refusal": None,
    }
    try:
        resolution = SimulationProfileResolutionV1.from_dict(
            resolution_record,
            catalog=_catalog_state().profiles,
        )
        options = SimulationTrainingOptionsV1.from_dict(training)
    except (KeyError, TypeError, ValueError, SimulationResolutionRefusal) as error:
        raise _VerificationFailure("REFERENCE_MISMATCH") from error
    return resolution, options


def _lifecycle_event(value: Mapping[str, object]) -> bool:
    data = value.get("data")
    return isinstance(data, Mapping) and data.get("action_kind") == "LIFECYCLE"


def _apply_lifecycle_event(
    session: LiveMarketSession,
    event: Mapping[str, object],
) -> None:
    data = event["data"]
    if not isinstance(data, Mapping) or set(data) != {
        "accepted",
        "action_kind",
        "semantic_action_id",
    }:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
    accepted = data["accepted"]
    action = data["semantic_action_id"]
    if type(accepted) is not bool or action not in {
        "SIMULATION_PLAY",
        "SIMULATION_PAUSE",
    }:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
    running = session.running
    complete = session.complete
    can_accept = (
        (action == "SIMULATION_PLAY" and not running and not complete)
        or (action == "SIMULATION_PAUSE" and running and not complete)
    )
    if accepted != can_accept:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
    expected_kind = "COMMAND" if accepted else "REJECTED"
    if event.get("kind") != expected_kind:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
    if not accepted:
        expected_message = (
            "SIMULATION_PLAY rejected: run already running"
            if action == "SIMULATION_PLAY"
            else "SIMULATION_PAUSE rejected: run is not running"
        )
        if event.get("message") != expected_message:
            raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
        session.status_message = expected_message
        return
    if action == "SIMULATION_PLAY":
        session.start()
    else:
        session.pause()
    if event.get("message") != session.status_message:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")


def _session_tape(session: LiveMarketSession) -> list[dict[str, object]]:
    timeline = session.timeline
    result: list[dict[str, object]] = []
    for item in timeline:
        record = item.as_dict()
        result.append(
            {
                "simulation_time_us": record["simulation_timestamp"],
                "kind": record["kind"],
                "message": record["message"],
                "data": record["data"],
            }
        )
    return result


def _reconstruct_artifact(
    artifact: SimulationReplayArtifactV1,
    recording: SessionRecording,
) -> _VerifiedReplayReconstruction:
    resolution, training = _resolution_from_artifact(artifact)
    try:
        session, disclosure, _layout, _semantic_keys = _materialize_session(
            resolution,
            training,
        )
    except SimulationStartRefusal as error:
        raise _VerificationFailure("REFERENCE_MISMATCH") from error
    artifact_root = artifact.as_dict()
    raw_events = artifact_root["event_tape"]
    if type(raw_events) is not list:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
    snapshots_by_time: dict[int, SessionSnapshot] = {0: session.snapshot()}
    inputs = recording.input_records
    input_index = 0
    event_index = 0
    while event_index < len(raw_events):
        first = raw_events[event_index]
        if type(first) is not dict or type(first.get("simulation_time_us")) is not int:
            raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
        group_time_us = first["simulation_time_us"]
        if group_time_us < session.simulation_time_us:
            raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
        if group_time_us > session.simulation_time_us:
            if not session.running:
                raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
            session.advance_by(group_time_us - session.simulation_time_us)
        group: list[dict[str, object]] = []
        while event_index < len(raw_events):
            candidate = raw_events[event_index]
            if type(candidate) is not dict:
                raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
            if candidate.get("simulation_time_us") != group_time_us:
                break
            group.append(candidate)
            event_index += 1
        for event in group:
            if _lifecycle_event(event):
                _apply_lifecycle_event(session, event)
                continue
            if event.get("kind") != "INPUT":
                continue
            if input_index >= len(inputs):
                raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
            expected = inputs[input_index]
            data = event.get("data")
            if not isinstance(data, Mapping) or (
                data.get("input_key") != expected.input_key
                or data.get("market_state_id") != expected.market_state_id
                or data.get("resolved_command") != expected.resolved_command
                or expected.simulation_time_us != group_time_us
            ):
                raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
            actual = session.handle_input(expected.input_key, recording.layout.bindings)
            if actual.as_dict() != expected.as_dict():
                raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
            input_index += 1
        snapshots_by_time[group_time_us] = session.snapshot()
    if input_index != len(inputs):
        raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
    if recording.completed_time_us < session.simulation_time_us:
        raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
    if recording.completed_time_us > session.simulation_time_us:
        if not session.running:
            raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
        session.advance_by(recording.completed_time_us - session.simulation_time_us)
    snapshots_by_time[recording.completed_time_us] = session.snapshot()
    actual_inputs = tuple(item.as_dict() for item in session.input_records)
    expected_inputs = tuple(item.as_dict() for item in recording.input_records)
    actual_states = tuple(item.as_dict() for item in session.market_states)
    expected_states = tuple(item.as_dict() for item in recording.market_states)
    if (
        actual_inputs != expected_inputs
        or actual_states != expected_states
        or session.state_sha256() != recording.expected_state_sha256
        or session.timeline_sha256() != recording.expected_timeline_sha256
        or session.simulation_time_us != recording.completed_time_us
        or session.complete != recording.complete
    ):
        raise _VerificationFailure("RECORDING_DIGEST_MISMATCH")
    expected_event_tape = [
        {
            "simulation_time_us": event["simulation_time_us"],
            "kind": event["kind"],
            "message": event["message"],
            "data": event["data"],
        }
        for event in raw_events
        if not _lifecycle_event(event)
    ]
    if _session_tape(session) != expected_event_tape:
        raise _VerificationFailure("EVENT_TAPE_DIGEST_MISMATCH")
    final_frame = artifact.final_frame
    cursor = final_frame.as_dict()["cursor"]
    if type(cursor) is not dict:
        raise _VerificationFailure("REFERENCE_MISMATCH")
    reconstructed_frame = _frame(
        (
            artifact.source_run_id,
            str(artifact_root["run_request_sha256"]),
            str(artifact_root["resolved_configuration_sha256"]),
        ),
        resolution,
        training,
        session,
        disclosure,
        final_frame.frame_sequence,
        str(cursor["run_state"]),
    )
    if reconstructed_frame.as_dict() != final_frame.as_dict():
        raise _VerificationFailure("REFERENCE_MISMATCH")
    return _VerifiedReplayReconstruction(
        resolution,
        training,
        tuple(snapshots_by_time[time_us] for time_us in sorted(snapshots_by_time)),
    )


def _receipt(
    reference: ReplayArtifactRefV1,
    *,
    status: str,
    recording_sha256: str | None = None,
    event_tape_sha256: str | None = None,
    unavailable_reason: str | None = None,
) -> ReplayArtifactVerificationReceiptV1:
    available = status == "AVAILABLE"
    record = {
        "schema_id": REPLAY_VERIFICATION_RECEIPT_SCHEMA_ID,
        "schema_version": 1,
        "status": status,
        "artifact_ref": reference.as_dict(),
        "verified_artifact_sha256": (
            reference.artifact_sha256 if available else None
        ),
        "verified_recording_sha256": recording_sha256 if available else None,
        "verified_event_tape_sha256": event_tape_sha256 if available else None,
        "source_run_id": reference.source_run_id if available else None,
        "replay_run_id": reference.replay_run_id if available else None,
        "unavailable_reason": unavailable_reason,
    }
    try:
        return ReplayArtifactVerificationReceiptV1.from_dict(record)
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractIntegrityError(
            f"backend constructed an invalid Replay verification receipt: {error}"
        ) from error


def _unavailable(
    reference: ReplayArtifactRefV1,
    reason: str,
) -> tuple[None, dict[str, object]]:
    return None, _receipt(
        reference,
        status="UNAVAILABLE",
        unavailable_reason=reason,
    ).as_dict()


def _supported_recording_wrapper(value: object) -> bool:
    return type(value) is dict and (
        set(value)
        == {
            "media_type",
            "recording_schema_version",
            "encoding",
            "bytes_base64",
            "bytes_sha256",
        }
        and value["media_type"] == RECORDING_MEDIA_TYPE
        and type(value["recording_schema_version"]) is int
        and value["recording_schema_version"] == RECORDING_SCHEMA_VERSION
        and value["encoding"] == RECORDING_ENCODING
    )


def resolve_replay_artifact(
    reference_payload: Mapping[str, object],
) -> tuple[object | None, dict[str, object]]:
    """Resolve, hash, decode, and internally reconcile one immutable artifact."""

    try:
        reference = ReplayArtifactRefV1.from_dict(reference_payload)
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractDecodeError(str(error)) from error
    if reference.store_id != SIMULATION_REPLAY_STORE_ID:
        return _unavailable(reference, "UNKNOWN_STORE")
    artifact_bytes = _read_simulation_replay_artifact(
        reference.store_id,
        reference.object_key,
    )
    if artifact_bytes is None:
        return _unavailable(reference, "OBJECT_NOT_FOUND")
    if hashlib.sha256(artifact_bytes).hexdigest() != reference.artifact_sha256:
        return _unavailable(reference, "ARTIFACT_DIGEST_MISMATCH")
    try:
        root = load_canonical_json_bytes(
            artifact_bytes,
            "simulation Replay artifact",
        )
    except (TypeError, ValueError):
        return _unavailable(reference, "UNSUPPORTED_ARTIFACT_SCHEMA")
    if (
        type(root) is not dict
        or root.get("schema_id") != ARTIFACT_SCHEMA_ID
        or type(root.get("schema_version")) is not int
        or root["schema_version"] != 1
    ):
        return _unavailable(reference, "UNSUPPORTED_ARTIFACT_SCHEMA")
    recording_wrapper = root.get("session_recording")
    if not _supported_recording_wrapper(recording_wrapper):
        return _unavailable(reference, "UNSUPPORTED_ARTIFACT_SCHEMA")
    try:
        embedded_recording = EmbeddedSessionRecordingV1.from_dict(recording_wrapper)
        recording_payload = json.loads(embedded_recording.recording_bytes.decode("utf-8"))
        recording = SessionRecording.from_dict(recording_payload)
    except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _unavailable(reference, "RECORDING_DIGEST_MISMATCH")
    event_tape = root.get("event_tape")
    event_tape_sha256 = root.get("event_tape_sha256")
    try:
        actual_event_tape_sha256 = canonical_digest(event_tape)
    except (TypeError, ValueError):
        return _unavailable(reference, "EVENT_TAPE_DIGEST_MISMATCH")
    if (
        type(event_tape) is not list
        or type(event_tape_sha256) is not str
        or actual_event_tape_sha256 != event_tape_sha256
    ):
        return _unavailable(reference, "EVENT_TAPE_DIGEST_MISMATCH")
    try:
        artifact = SimulationReplayArtifactV1.from_dict(root)
    except (KeyError, TypeError, ValueError):
        return _unavailable(reference, "REFERENCE_MISMATCH")
    if (
        artifact.source_run_id != reference.source_run_id
        or artifact.replay_run_id != reference.replay_run_id
        or artifact.event_tape_sha256 != actual_event_tape_sha256
        or embedded_recording.bytes_sha256
        != artifact.session_recording.bytes_sha256
    ):
        return _unavailable(reference, "REFERENCE_MISMATCH")
    try:
        reconstruction = _reconstruct_artifact(artifact, recording)
    except _VerificationFailure as failure:
        return _unavailable(reference, failure.reason)
    source = _VerifiedSimulationReplaySource(
        reference,
        artifact,
        bytes(artifact_bytes),
        recording,
        reconstruction,
    )
    receipt = _receipt(
        reference,
        status="AVAILABLE",
        recording_sha256=embedded_recording.bytes_sha256,
        event_tape_sha256=actual_event_tape_sha256,
    )
    return source, receipt.as_dict()


__all__ = ["resolve_replay_artifact"]
