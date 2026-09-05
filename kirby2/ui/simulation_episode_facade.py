"""Public preparation facade for deterministic catalog-authored episode cuts."""

from __future__ import annotations

from collections.abc import Mapping

from .simulation_contract import canonical_digest
from .simulation_episode_contract import (
    EPISODE_RESULT_SCHEMA_ID,
    FULL_MODEL_PREFIX_PROJECTION_ID,
    SimulationEpisodeIdentityV1,
    SimulationEpisodePreparationRequestV1,
    SimulationEpisodePreparedResultV1,
    SimulationEpisodeRefusalV1,
    SimulationEpisodeVerificationV1,
    episode_prefix_projection_sha256,
)
from .simulation_interaction_contract import COMMAND_REQUEST_SCHEMA_ID, SimulationCommandResultV1
from .simulation_live_contract import SimulationFrameV1, SimulationStartResultV1
from .simulation_run_facade import (
    advance_simulation_run,
    close_simulation_run,
    dispatch_simulation_command,
    read_current_simulation_frame,
    simulation_run_model_prefix_sha256,
    start_simulation_run,
)


def _cursor(frame: SimulationFrameV1) -> Mapping[str, object]:
    cursor = frame.as_dict()["cursor"]
    if not isinstance(cursor, Mapping):
        raise RuntimeError("validated episode frame has no cursor")
    return cursor


def _command_request(frame: SimulationFrameV1, semantic_action_id: str) -> dict[str, object]:
    cursor = _cursor(frame)
    basis = {
        "schema_id": COMMAND_REQUEST_SCHEMA_ID,
        "schema_version": 1,
        "source_run_id": frame.source_run_id,
        "origin_frame_id": frame.frame_id,
        "origin_cursor_id": cursor["cursor_id"],
        "semantic_action_id": semantic_action_id,
        "parameters": {},
    }
    return {
        **basis,
        "command_id": f"simulation-command-{canonical_digest(basis)[:24]}",
    }


def _result_record(
    *,
    status: str,
    request_id: str | None,
    identity: SimulationEpisodeIdentityV1 | None,
    start: SimulationStartResultV1 | None,
    frame: SimulationFrameV1 | None,
    prefix_actions: tuple[str, ...] | None,
    projection_sha256: str | None,
    full_model_projection_id: str | None,
    full_model_prefix_sha256: str | None,
    ownership: str,
    cleanup_disposition: str | None,
    refusal: SimulationEpisodeRefusalV1 | None,
) -> dict[str, object]:
    basis = {
        "schema_id": EPISODE_RESULT_SCHEMA_ID,
        "schema_version": 1,
        "status": status,
        "request_id": request_id,
        "identity": None if identity is None else identity.as_dict(),
        "original_start_result": None if start is None else start.as_dict(),
        "current_frame": None if frame is None else frame.as_dict(),
        "prefix_actions": None if prefix_actions is None else list(prefix_actions),
        "prefix_projection_sha256": projection_sha256,
        "full_model_projection_id": full_model_projection_id,
        "full_model_prefix_sha256": full_model_prefix_sha256,
        "resource_ownership": ownership,
        "cleanup_disposition": cleanup_disposition,
        "refusal": None if refusal is None else refusal.as_dict(),
    }
    return {
        **basis,
        "result_id": f"simulation-episode-prepared-result-{canonical_digest(basis)[:24]}",
    }


def _refused(
    *,
    reason_code: str,
    explanation: str,
    request: SimulationEpisodePreparationRequestV1 | None,
    ownership: str = "NO_RESOURCE",
    cleanup_disposition: str | None = None,
) -> dict[str, object]:
    refusal = SimulationEpisodeRefusalV1(reason_code, explanation)
    return SimulationEpisodePreparedResultV1.from_dict(
        _result_record(
            status="REFUSED",
            request_id=None if request is None else request.request_id,
            identity=None if request is None else request.identity,
            start=None,
            frame=None,
            prefix_actions=None,
            projection_sha256=None,
            full_model_projection_id=None,
            full_model_prefix_sha256=None,
            ownership=ownership,
            cleanup_disposition=cleanup_disposition,
            refusal=refusal,
        )
    ).as_dict()


def _close_after_failure(
    handle: object,
    request: SimulationEpisodePreparationRequestV1,
    reason_code: str,
    explanation: str,
) -> tuple[object | None, dict[str, object]]:
    """Close a post-allocation failure or return its handle for explicit caller cleanup."""

    try:
        close_record = close_simulation_run(handle, "USER_ABANDONED")
    except Exception as error:  # The opaque handle remains the caller's cleanup responsibility.
        return handle, _refused(
            reason_code="CLEANUP_UNCONFIRMED",
            explanation=f"{explanation}; automatic cleanup raised {type(error).__name__}",
            request=request,
            ownership="CALLER_OWNS_CLEANUP_HANDLE",
            cleanup_disposition="USER_ABANDONED",
        )
    if close_record.get("status") == "CLOSED":
        return None, _refused(
            reason_code=reason_code,
            explanation=explanation,
            request=request,
            cleanup_disposition="USER_ABANDONED",
        )
    return handle, _refused(
        reason_code="CLEANUP_UNCONFIRMED",
        explanation=f"{explanation}; automatic cleanup was not confirmed",
        request=request,
        ownership="CALLER_OWNS_CLEANUP_HANDLE",
        cleanup_disposition="USER_ABANDONED",
    )


def prepare_simulation_episode(
    request_payload: Mapping[str, object],
) -> tuple[object | None, dict[str, object]]:
    """Prepare an opaque paused run at an audited deterministic episode cut."""

    try:
        request = SimulationEpisodePreparationRequestV1.from_dict(request_payload)
    except Exception as error:
        return None, _refused(
            reason_code="INVALID_REQUEST",
            explanation=str(error),
            request=None,
        )

    handle, start_payload = start_simulation_run(
        request.resolution.as_dict(), request.training_options.as_dict()
    )
    try:
        start = SimulationStartResultV1.from_dict(
            start_payload,
            resolution=request.resolution,
            training_options=request.training_options,
        )
    except Exception as error:
        if handle is None:
            return None, _refused(
                reason_code="START_REFUSED",
                explanation=f"start result was invalid: {error}",
                request=request,
            )
        return _close_after_failure(
            handle,
            request,
            "START_REFUSED",
            f"start result was invalid: {error}",
        )
    if handle is None or start.status != "AVAILABLE" or start.initial_frame is None:
        return None, _refused(
            reason_code="START_REFUSED",
            explanation="the pinned profile could not start an ordinary READY run",
            request=request,
        )

    current = start.initial_frame
    try:
        for action in request.prefix_actions:
            command = SimulationCommandResultV1.from_dict(
                dispatch_simulation_command(handle, _command_request(current, action))
            )
            if (
                command.status != "AVAILABLE"
                or command.outcome is None
                or not command.outcome.accepted
                or command.destination_frame is None
            ):
                return _close_after_failure(
                    handle,
                    request,
                    "PREFIX_REJECTED",
                    f"prefix action {action} was not accepted",
                )
            current = command.destination_frame
        cursor = _cursor(current)
        advance = advance_simulation_run(
            handle,
            current.source_run_id,
            current.frame_id,
            str(cursor["cursor_id"]),
            request.identity.anchor_time_us,
        )
        if advance.get("status") != "AVAILABLE" or advance.get("destination_frame") is None:
            return _close_after_failure(
                handle,
                request,
                "ANCHOR_INVALID",
                "the pinned prefix could not advance to its declared anchor",
            )
        current = SimulationFrameV1.from_dict(advance["destination_frame"])
        pause = SimulationCommandResultV1.from_dict(
            dispatch_simulation_command(handle, _command_request(current, "SIMULATION_PAUSE"))
        )
        if (
            pause.status != "AVAILABLE"
            or pause.outcome is None
            or not pause.outcome.accepted
            or pause.destination_frame is None
        ):
            return _close_after_failure(
                handle,
                request,
                "PREPARATION_FAILED",
                "the prepared cut could not be paused at its declared anchor",
            )
        current = pause.destination_frame
        result = SimulationEpisodePreparedResultV1.from_dict(
            _result_record(
                status="AVAILABLE",
                request_id=request.request_id,
                identity=request.identity,
                start=start,
                frame=current,
                prefix_actions=request.prefix_actions,
                projection_sha256=episode_prefix_projection_sha256(
                    current, request.prefix_actions
                ),
                full_model_projection_id=FULL_MODEL_PREFIX_PROJECTION_ID,
                full_model_prefix_sha256=simulation_run_model_prefix_sha256(
                    handle, request.prefix_actions
                ),
                ownership="CALLER_OWNS_ACTIVE_HANDLE",
                cleanup_disposition=None,
                refusal=None,
            )
        )
        return handle, result.as_dict()
    except Exception as error:
        return _close_after_failure(
            handle,
            request,
            "PREPARATION_FAILED",
            f"episode preparation raised {type(error).__name__}: {error}",
        )


def release_simulation_episode(handle_value: object) -> dict[str, object]:
    """Release an unfinalized prepared episode through the public close boundary."""

    return close_simulation_run(handle_value, "USER_ABANDONED")


def verify_prepared_simulation_episode(
    handle_value: object,
    prepared_result_payload: Mapping[str, object],
) -> dict[str, object]:
    """Compare an opaque active run with its prepared full-model commitment."""

    prepared = SimulationEpisodePreparedResultV1.from_dict(prepared_result_payload)
    if (
        prepared.status != "AVAILABLE"
        or prepared.current_frame is None
        or prepared.prefix_actions is None
        or prepared.full_model_prefix_sha256 is None
    ):
        raise ValueError("episode verification requires an available prepared result")
    current = read_current_simulation_frame(handle_value, prepared.current_frame.source_run_id)
    if (
        current.get("status") != "AVAILABLE"
        or current.get("current_frame", {}).get("frame_id") != prepared.current_frame.frame_id
    ):
        status, reason = "MISMATCH", "CURRENT_FRAME_CHANGED"
    elif (
        simulation_run_model_prefix_sha256(handle_value, prepared.prefix_actions)
        != prepared.full_model_prefix_sha256
    ):
        status, reason = "MISMATCH", "FULL_MODEL_PREFIX_MISMATCH"
    else:
        status, reason = "MATCH", None
    return SimulationEpisodeVerificationV1(
        verification_id="", status=status, prepared_result_id=prepared.result_id, reason=reason
    ).as_dict()


__all__ = [
    "prepare_simulation_episode",
    "release_simulation_episode",
    "verify_prepared_simulation_episode",
]
