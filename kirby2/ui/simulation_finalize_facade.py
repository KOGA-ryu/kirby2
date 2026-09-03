"""Atomic finalization of one live simulation into immutable Replay evidence."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Mapping

from kirby2.full_day.models import canonical_json_bytes
from kirby2.session.replay import SessionRecording

from .simulation_artifact_contract import (
    ARTIFACT_KIND,
    ARTIFACT_REFERENCE_SCHEMA_ID,
    ARTIFACT_SCHEMA_ID,
    FINALIZE_MODES,
    FINALIZE_RESULT_SCHEMA_ID,
    RECORDING_ENCODING,
    RECORDING_MEDIA_TYPE,
    RECORDING_SCHEMA_VERSION,
    RUN_RESULT_SCHEMA_ID,
    ReplayArtifactRefV1,
    SimulationFinalizeResultV1,
    SimulationReplayArtifactV1,
    SimulationRunResultV1,
    replay_run_id_for_source,
)
from .simulation_artifact_store import (
    SIMULATION_REPLAY_STORE_ID,
    _store_simulation_replay_artifact,
)
from .simulation_contract import (
    SimulationContractDecodeError,
    SimulationContractIntegrityError,
    _snapshot,
    canonical_digest,
)
from .simulation_live_contract import SimulationFrameV1
from .simulation_run_facade import (
    _CURSOR_ID_PATTERN,
    _FRAME_ID_PATTERN,
    _RUN_ID_PATTERN,
    _SimulationRunHandle,
    _capture_session_timeline,
    _current_cursor,
    _operation_id,
    _run_handle,
)


@dataclass(slots=True)
class _FinalizedSimulationRun:
    artifact: SimulationReplayArtifactV1
    artifact_bytes: bytes
    run_result: SimulationRunResultV1
    results_by_mode: dict[str, SimulationFinalizeResultV1] = field(default_factory=dict)


def _recording_bytes(recording: SessionRecording) -> bytes:
    return (
        json.dumps(
            recording.as_dict(),
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _finalize_result_record(
    *,
    status: str,
    mode: str,
    source_run_id: str,
    origin_frame_id: str,
    origin_cursor_id: str,
    run_result: dict[str, object] | None,
    unavailable_reason: str | None,
) -> dict[str, object]:
    basis = {
        "schema_id": FINALIZE_RESULT_SCHEMA_ID,
        "schema_version": 1,
        "status": status,
        "mode": mode,
        "source_run_id": source_run_id,
        "origin_frame_id": origin_frame_id,
        "origin_cursor_id": origin_cursor_id,
        "run_result": run_result,
        "unavailable_reason": unavailable_reason,
    }
    return {
        **basis,
        "result_id": f"simulation-finalize-result-{canonical_digest(basis)[:24]}",
    }


def _validated_finalize_result(
    record: Mapping[str, object],
    *,
    origin_frame: SimulationFrameV1 | None = None,
    final_frame: SimulationFrameV1 | None = None,
) -> SimulationFinalizeResultV1:
    try:
        return SimulationFinalizeResultV1.from_dict(
            record,
            origin_frame=origin_frame,
            final_frame=final_frame,
        )
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractIntegrityError(
            f"backend constructed an invalid finalize result: {error}"
        ) from error


def _unavailable_finalize(
    *,
    mode: str,
    source_run_id: str,
    origin_frame_id: str,
    origin_cursor_id: str,
    reason: str,
) -> dict[str, object]:
    return _validated_finalize_result(
        _finalize_result_record(
            status="UNAVAILABLE",
            mode=mode,
            source_run_id=source_run_id,
            origin_frame_id=origin_frame_id,
            origin_cursor_id=origin_cursor_id,
            run_result=None,
            unavailable_reason=reason,
        )
    ).as_dict()


def _build_artifact(handle: _SimulationRunHandle) -> SimulationReplayArtifactV1:
    configuration = handle.resolution.resolved_configuration
    if configuration is None:
        raise SimulationContractIntegrityError(
            "active simulation run lost its resolved configuration"
        )
    _capture_session_timeline(handle)
    event_tape = _snapshot(handle.event_tape)
    if type(event_tape) is not list:
        raise SimulationContractIntegrityError("simulation event tape lost its array root")
    event_tape_sha256 = canonical_digest(event_tape)
    recording = SessionRecording.capture(
        handle.session,
        handle.layout,
        auto_start=handle.training_options.initial_run_state == "RUNNING",
    )
    recording_bytes = _recording_bytes(recording)
    final_frame = handle.current_frame.as_dict()
    cursor = final_frame["cursor"]
    if type(cursor) is not dict:
        raise SimulationContractIntegrityError("final simulation frame lost its cursor")
    terminal_status = (
        "COMPLETE" if cursor["run_state"] == "COMPLETE" else "SAVED_PARTIAL"
    )
    record = {
        "schema_id": ARTIFACT_SCHEMA_ID,
        "schema_version": 1,
        "source_run_id": handle.source_run_id,
        "replay_run_id": replay_run_id_for_source(handle.source_run_id),
        "profile_ref": handle.resolution.selection.profile_ref.as_dict(),
        "selection": handle.resolution.selection.as_dict(),
        "resolved_configuration_sha256": handle.resolved_configuration_sha256,
        "resolved_configuration": configuration.as_dict(),
        "training_options": handle.training_options.as_dict(),
        "run_request_sha256": handle.run_request_sha256,
        "component_payloads": [
            {"component_ref": dict(row["component_ref"]), "payload": row["payload"]}
            for row in handle.component_payloads
        ],
        "session_recording": {
            "media_type": RECORDING_MEDIA_TYPE,
            "recording_schema_version": RECORDING_SCHEMA_VERSION,
            "encoding": RECORDING_ENCODING,
            "bytes_base64": base64.b64encode(recording_bytes).decode("ascii"),
            "bytes_sha256": hashlib.sha256(recording_bytes).hexdigest(),
        },
        "event_tape": event_tape,
        "event_tape_sha256": event_tape_sha256,
        "terminal_status": terminal_status,
        "final_frame": final_frame,
        "provenance": final_frame["provenance"],
    }
    try:
        return SimulationReplayArtifactV1.from_dict(record)
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractIntegrityError(
            f"backend constructed an invalid simulation Replay artifact: {error}"
        ) from error


def _artifact_reference(
    artifact: SimulationReplayArtifactV1,
    artifact_sha256: str,
    object_key: str,
) -> ReplayArtifactRefV1:
    record = {
        "schema_id": ARTIFACT_REFERENCE_SCHEMA_ID,
        "schema_version": 1,
        "artifact_id": f"replay-artifact-{artifact_sha256[:24]}",
        "artifact_kind": ARTIFACT_KIND,
        "artifact_schema_id": ARTIFACT_SCHEMA_ID,
        "artifact_schema_version": 1,
        "artifact_sha256": artifact_sha256,
        "source_run_id": artifact.source_run_id,
        "replay_run_id": artifact.replay_run_id,
        "store_id": SIMULATION_REPLAY_STORE_ID,
        "object_key": object_key,
    }
    try:
        return ReplayArtifactRefV1.from_dict(record)
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractIntegrityError(
            f"backend constructed an invalid Replay artifact reference: {error}"
        ) from error


def _run_result(
    handle: _SimulationRunHandle,
    artifact: SimulationReplayArtifactV1,
    reference: ReplayArtifactRefV1,
) -> SimulationRunResultV1:
    final_frame = handle.current_frame
    frame = final_frame.as_dict()
    basis = {
        "schema_id": RUN_RESULT_SCHEMA_ID,
        "schema_version": 1,
        "source_run_id": handle.source_run_id,
        "replay_run_id": artifact.replay_run_id,
        "profile_ref": handle.resolution.selection.profile_ref.as_dict(),
        "selection_sha256": handle.resolution.selection_sha256,
        "resolved_configuration_sha256": handle.resolved_configuration_sha256,
        "run_request_sha256": handle.run_request_sha256,
        "terminal_status": artifact.terminal_status,
        "final_frame_id": final_frame.frame_id,
        "final_cursor": frame["cursor"],
        "final_book_state_sha256": frame["market_state"]["book_state_sha256"],
        "event_tape_sha256": artifact.event_tape_sha256,
        "replay_artifact": reference.as_dict(),
        "metrics": frame["metrics"],
        "diagnostics": frame["diagnostics"],
        "provenance": frame["provenance"],
    }
    record = {
        **basis,
        "result_id": f"simulation-run-result-{canonical_digest(basis)[:24]}",
    }
    try:
        return SimulationRunResultV1.from_dict(record, final_frame=final_frame)
    except SimulationContractIntegrityError:
        raise
    except (TypeError, ValueError) as error:
        raise SimulationContractIntegrityError(
            f"backend constructed an invalid simulation run result: {error}"
        ) from error


def _available_finalize_result(
    handle: _SimulationRunHandle,
    state: _FinalizedSimulationRun,
    mode: str,
    origin_frame_id: str,
    origin_cursor_id: str,
) -> SimulationFinalizeResultV1:
    existing = state.results_by_mode.get(mode)
    if existing is not None:
        return existing
    record = _finalize_result_record(
        status="AVAILABLE",
        mode=mode,
        source_run_id=handle.source_run_id,
        origin_frame_id=origin_frame_id,
        origin_cursor_id=origin_cursor_id,
        run_result=state.run_result.as_dict(),
        unavailable_reason=None,
    )
    result = _validated_finalize_result(
        record,
        origin_frame=handle.current_frame,
        final_frame=handle.current_frame,
    )
    state.results_by_mode[mode] = result
    return result


def finalize_simulation_run(
    handle_value: object,
    source_run_id: str,
    origin_frame_id: str,
    origin_cursor_id: str,
    mode: str,
) -> dict[str, object]:
    """Finalize one exact frame and store its canonical artifact only once."""

    handle = _run_handle(handle_value)
    if type(mode) is not str or mode not in FINALIZE_MODES:
        raise SimulationContractDecodeError(
            "simulation finalize mode is not a supported V1 value"
        )
    source_run_id = _operation_id(
        source_run_id,
        _RUN_ID_PATTERN,
        "simulation finalize source run ID",
    )
    origin_frame_id = _operation_id(
        origin_frame_id,
        _FRAME_ID_PATTERN,
        "simulation finalize origin frame ID",
    )
    origin_cursor_id = _operation_id(
        origin_cursor_id,
        _CURSOR_ID_PATTERN,
        "simulation finalize origin cursor ID",
    )
    if source_run_id != handle.source_run_id:
        return _unavailable_finalize(
            mode=mode,
            source_run_id=source_run_id,
            origin_frame_id=origin_frame_id,
            origin_cursor_id=origin_cursor_id,
            reason="SOURCE_RUN_MISMATCH",
        )
    if handle.reset_pending:
        return _unavailable_finalize(
            mode=mode,
            source_run_id=source_run_id,
            origin_frame_id=origin_frame_id,
            origin_cursor_id=origin_cursor_id,
            reason="RESET_PENDING",
        )
    if handle.lifecycle_disposition not in {None, "FINALIZED"}:
        return _unavailable_finalize(
            mode=mode,
            source_run_id=source_run_id,
            origin_frame_id=origin_frame_id,
            origin_cursor_id=origin_cursor_id,
            reason="ALREADY_ABANDONED",
        )
    cursor = _current_cursor(handle)
    if (
        origin_frame_id != handle.current_frame.frame_id
        or origin_cursor_id != cursor["cursor_id"]
    ):
        return _unavailable_finalize(
            mode=mode,
            source_run_id=source_run_id,
            origin_frame_id=origin_frame_id,
            origin_cursor_id=origin_cursor_id,
            reason="STALE_ORIGIN",
        )
    if mode == "COMPLETE_ONLY" and cursor["run_state"] != "COMPLETE":
        return _unavailable_finalize(
            mode=mode,
            source_run_id=source_run_id,
            origin_frame_id=origin_frame_id,
            origin_cursor_id=origin_cursor_id,
            reason="RUN_NOT_COMPLETE",
        )
    if handle.lifecycle_disposition == "FINALIZED":
        state = handle.finalization_state
        if type(state) is not _FinalizedSimulationRun:
            raise SimulationContractIntegrityError(
                "finalized simulation handle lost its immutable result"
            )
        return _available_finalize_result(
            handle,
            state,
            mode,
            origin_frame_id,
            origin_cursor_id,
        ).as_dict()

    artifact = _build_artifact(handle)
    artifact_bytes = canonical_json_bytes(artifact.as_dict())
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    object_key = f"simulation-replay-artifact-{artifact_sha256}"
    reference = _artifact_reference(artifact, artifact_sha256, object_key)
    run_result = _run_result(handle, artifact, reference)
    state = _FinalizedSimulationRun(artifact, artifact_bytes, run_result)
    result = _available_finalize_result(
        handle,
        state,
        mode,
        origin_frame_id,
        origin_cursor_id,
    )
    stored_id, stored_key, stored_sha256 = _store_simulation_replay_artifact(
        artifact_bytes
    )
    if (
        stored_id != reference.store_id
        or stored_key != reference.object_key
        or stored_sha256 != reference.artifact_sha256
    ):
        raise SimulationContractIntegrityError(
            "simulation Replay store returned a different immutable location"
        )
    handle.finalization_state = state
    handle.lifecycle_disposition = "FINALIZED"
    return result.as_dict()


__all__ = ["finalize_simulation_run"]
