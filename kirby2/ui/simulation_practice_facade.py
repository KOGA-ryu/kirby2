"""Process-local public facade for six deterministic Chapter 1 practice recipes."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.curriculum.practice_assessment import (
    assess_dispatched_action,
    assess_response,
    debrief_from_assessment,
)
from kirby2.curriculum.practice_episodes import (
    PracticeEpisodeDefinitionV1,
    get_practice_episode_v1,
    list_practice_episodes_v1,
)

from .simulation_contract import PROFILE_SELECTION_SCHEMA_ID, canonical_digest
from .simulation_episode_contract import build_simulation_episode_preparation_request
from .simulation_episode_facade import prepare_simulation_episode, release_simulation_episode
from .simulation_interaction_contract import SimulationCommandResultV1
from .simulation_live_contract import SimulationFrameV1
from .simulation_practice_contract import (
    PRACTICE_CATALOG_SCHEMA_ID,
    PracticeActionRequestV1,
    PracticeAttemptRequestV1,
    PracticeCatalogV1,
    build_practice_result,
)
from .simulation_run_facade import (
    _activate_guided_hold,
    _bind_practice_attempt,
    _practice_attempt_matches,
    _peek_staged_practice_response,
    _release_guided_hold,
    _stage_practice_response,
    _take_staged_practice_response,
    dispatch_simulation_command,
    read_current_simulation_frame,
)
from .simulation_facade import list_simulation_training_resources, resolve_simulation_profile


_UNAVAILABLE_WALL_TIME = {
    "source": "UNAVAILABLE", "resolution_us": None, "elapsed_wall_time_us": None,
}


@dataclass(slots=True)
class _AttemptState:
    episode: PracticeEpisodeDefinitionV1
    binding: Mapping[str, object]
    record: dict[str, object]
    step_index: int = 0
    hold_id: str | None = None
    initial_begin_active: bool = True


@dataclass(slots=True)
class _RequestOutcome:
    handle: object
    result: dict[str, object]


_ATTEMPTS: dict[str, _AttemptState] = {}
_REQUEST_OUTCOMES: dict[str, _RequestOutcome] = {}
_SETTLED_REQUESTS: dict[str, str] = {}


def _cursor(frame: Mapping[str, object]) -> dict[str, object]:
    cursor = frame.get("cursor")
    if not isinstance(cursor, Mapping):
        raise ValueError("practice frame has no public cursor")
    return dict(cursor)


def _command_request(frame: Mapping[str, object], action: str) -> dict[str, object]:
    cursor = _cursor(frame)
    basis = {
        "schema_id": "KIRBY2_SIMULATION_COMMAND_REQUEST_V1",
        "schema_version": 1,
        "source_run_id": frame["source_run_id"],
        "origin_frame_id": frame["frame_id"],
        "origin_cursor_id": cursor["cursor_id"],
        "semantic_action_id": action,
        "parameters": {},
    }
    return {**basis, "command_id": f"simulation-command-{canonical_digest(basis)[:24]}"}


def _training_options() -> dict[str, object]:
    resources = list_simulation_training_resources()
    defaults = resources.get("defaults")
    if not isinstance(defaults, Mapping):
        raise RuntimeError("simulation training resources omit V1 defaults")
    return {
        "schema_id": "KIRBY2_SIMULATION_TRAINING_OPTIONS_V1",
        "schema_version": 1,
        "quantity_options": list(defaults["quantity_options"]),
        "initial_quantity": defaults["initial_quantity"],
        "layout_ref": dict(defaults["layout_ref"]),
        "strategy_ref": None,
        "objective": None,
        "curriculum_drill_ref": None,
        "initial_run_state": "READY",
        "observation_policy_ref": dict(defaults["observation_policy_ref"]),
    }


def _resolution(recipe: PracticeEpisodeDefinitionV1) -> dict[str, object]:
    return resolve_simulation_profile(
        {
            "schema_id": PROFILE_SELECTION_SCHEMA_ID,
            "schema_version": 1,
            "profile_ref": dict(recipe.profile_ref),
            "seed": recipe.seed,
            "duration_us": recipe.duration_us,
            "control_values": dict(recipe.control_values),
        }
    )


def _hold_id(attempt_id: str, frame: Mapping[str, object]) -> str:
    cursor = _cursor(frame)
    basis = {
        "attempt_id": attempt_id,
        "source_run_id": frame["source_run_id"],
        "frame_id": frame["frame_id"],
        "cursor_id": cursor["cursor_id"],
    }
    return f"practice-guided-hold-{canonical_digest(basis)[:24]}"


def _attempt_public(state: _AttemptState) -> dict[str, object]:
    return {**state.record, "step_index": state.step_index}


def _assistance(kind: str, frame: Mapping[str, object], wall_time: Mapping[str, object], message: str) -> dict[str, object]:
    return {
        "kind": kind,
        "simulation_time_us": _cursor(frame)["simulation_time_us"],
        "wall_time": dict(wall_time),
        "message": message,
    }


def _result(
    *, status: str, operation: str, attempt: Mapping[str, object] | None = None,
    episode: Mapping[str, object] | None = None, source_run_id: str | None = None,
    current_frame: Mapping[str, object] | None = None, hold_id: str | None = None,
    assistance: list[dict[str, object]] | None = None, assessment: Mapping[str, object] | None = None,
    debrief: Mapping[str, object] | None = None, unavailable_reason: str | None = None,
) -> dict[str, object]:
    return build_practice_result(
        status=status,
        operation=operation,
        attempt=None if attempt is None else dict(attempt),
        episode=None if episode is None else dict(episode),
        source_run_id=source_run_id,
        current_frame=None if current_frame is None else dict(current_frame),
        hold_id=hold_id,
        assistance=[] if assistance is None else assistance,
        assessment=None if assessment is None else dict(assessment),
        debrief=None if debrief is None else dict(debrief),
        unavailable_reason=unavailable_reason,
    )


def list_simulation_practice_episodes() -> dict[str, object]:
    """Publish a detached strict catalog of the six immutable practice recipes."""

    episodes = [episode.as_dict() for episode in list_practice_episodes_v1()]
    basis = {"schema_id": PRACTICE_CATALOG_SCHEMA_ID, "schema_version": 1, "episodes": episodes}
    record = {**basis, "catalog_id": f"practice-catalog-{canonical_digest(basis)[:24]}"}
    return PracticeCatalogV1.from_dict(record).as_dict()


def _repeat_is_valid(request: PracticeAttemptRequestV1, recipe: PracticeEpisodeDefinitionV1) -> bool:
    if request.operation == "BEGIN":
        return True
    prior = _ATTEMPTS.get(str(request.prior_attempt_id))
    if prior is None:
        return False
    if request.operation == "EXACT_REPEAT":
        return recipe.episode_id == prior.episode.episode_id
    return recipe.variation_of == prior.episode.episode_id


def _begin_refusal(handle: object | None, reason: str) -> tuple[object | None, dict[str, object]]:
    """Close an acquired source or explicitly return its opaque cleanup owner."""

    if handle is None:
        return None, _result(status="REFUSED", operation="BEGIN", unavailable_reason=reason)
    try:
        closed = release_simulation_episode(handle)
    except Exception:
        closed = {"status": "UNAVAILABLE"}
    if closed.get("status") == "CLOSED":
        return None, _result(status="REFUSED", operation="BEGIN", unavailable_reason=reason)
    return handle, _result(status="REFUSED", operation="BEGIN", unavailable_reason="CLEANUP_UNCONFIRMED")


def _duplicate_request(request: PracticeAttemptRequestV1) -> tuple[object | None, dict[str, object]] | None:
    """Return the original active ownership, never allocate a second source for retry."""

    existing = _REQUEST_OUTCOMES.get(request.request_id)
    if existing is None:
        return None
    attempt = existing.result.get("attempt")
    if not isinstance(attempt, Mapping):
        _REQUEST_OUTCOMES.pop(request.request_id, None)
        return None
    current = _current(existing.handle, str(attempt["source_run_id"]))
    if current is None:
        _REQUEST_OUTCOMES.pop(request.request_id, None)
        _SETTLED_REQUESTS[request.request_id] = str(attempt["attempt_id"])
        return None, _result(
            status="REFUSED", operation="BEGIN",
            unavailable_reason="DUPLICATE_REQUEST_SETTLED",
        )
    state = _ATTEMPTS.get(str(attempt["attempt_id"]))
    original_frame = existing.result.get("current_frame")
    if (
        state is not None
        and state.initial_begin_active
        and isinstance(original_frame, Mapping)
        and current["frame_id"] == original_frame.get("frame_id")
        and state.hold_id == existing.result.get("hold_id")
    ):
        return existing.handle, copy.deepcopy(existing.result)
    if state is None:
        _REQUEST_OUTCOMES.pop(request.request_id, None)
        return None
    return existing.handle, _result(
        status="DUPLICATE", operation="DUPLICATE", attempt=_attempt_public(state),
        episode=state.episode.as_dict(), source_run_id=str(attempt["source_run_id"]),
        current_frame=current, hold_id=state.hold_id,
        assistance=[_assistance("DUPLICATE_REQUEST_FENCED", current, _UNAVAILABLE_WALL_TIME, "The request already owns this progressed process-local attempt; no second source was allocated.")],
        unavailable_reason="DUPLICATE_REQUEST_ALREADY_PROGRESSED",
    )


def begin_simulation_practice_attempt(
    request_payload: Mapping[str, object],
) -> tuple[object | None, dict[str, object]]:
    """Prepare one fresh ordinary source run and optionally place its guided hold."""

    request = PracticeAttemptRequestV1.from_dict(request_payload)
    if request.request_id in _SETTLED_REQUESTS:
        return None, _result(
            status="REFUSED", operation="BEGIN",
            unavailable_reason="DUPLICATE_REQUEST_SETTLED",
        )
    duplicate = _duplicate_request(request)
    if duplicate is not None:
        return duplicate
    recipe = get_practice_episode_v1(request.episode_id)
    if not _repeat_is_valid(request, recipe):
        return None, _result(status="REFUSED", operation="BEGIN", unavailable_reason="INVALID_REPEAT_LINEAGE")
    resolution = _resolution(recipe)
    if resolution.get("status") != "AVAILABLE":
        return None, _result(status="REFUSED", operation="BEGIN", unavailable_reason="PROFILE_RESOLUTION_REFUSED")
    preparation = build_simulation_episode_preparation_request(
        episode_id=recipe.episode_id,
        episode_version=1,
        resolution_payload=resolution,
        training_options_payload=_training_options(),
        prefix_actions=recipe.preparation_actions,
        anchor_time_us=recipe.anchor_time_us,
    )
    handle, prepared = prepare_simulation_episode(preparation)
    if handle is None or prepared.get("status") != "AVAILABLE" or not isinstance(prepared.get("current_frame"), Mapping):
        return _begin_refusal(handle, "EPISODE_PREPARATION_REFUSED")
    frame = dict(prepared["current_frame"])
    source_run_id = str(frame["source_run_id"])
    attempt_basis = {
        "attempt_request_id": request.request_id,
        "episode_recipe_sha256": recipe.recipe_sha256,
        "prepared_result_id": prepared["result_id"],
        "source_run_id": source_run_id,
    }
    attempt_id = f"practice-attempt-{canonical_digest(attempt_basis)[:24]}"
    binding = {
        "attempt_id": attempt_id, **attempt_basis, "episode_id": recipe.episode_id,
        "operation_id": request.operation_id,
    }
    state: _AttemptState | None = None
    try:
        _bind_practice_attempt(handle, binding)
        record = {
            "attempt_id": attempt_id,
            "attempt_request_id": request.request_id,
            "episode_id": recipe.episode_id,
            "episode_recipe_sha256": recipe.recipe_sha256,
            "operation": request.operation,
            "operation_id": request.operation_id,
            "mode": request.mode,
            "pace_multiplier_ppm": request.pace_multiplier_ppm,
            "prior_attempt_id": request.prior_attempt_id,
            "prepared_result_id": prepared["result_id"],
            "prepared_identity": dict(prepared["identity"]),
            "source_run_id": source_run_id,
            "anchor_time_us": recipe.anchor_time_us,
            "step_index": 0,
            "step_count": len(recipe.expected_actions),
        }
        hold_id = None
        state = _AttemptState(recipe, binding, record, hold_id=hold_id)
        _ATTEMPTS[attempt_id] = state
        if request.mode == "GUIDED":
            hold_id = _hold_id(attempt_id, frame)
            cursor = _cursor(frame)
            _activate_guided_hold(
                handle,
                attempt_id=attempt_id,
                hold_id=hold_id,
                source_run_id=source_run_id,
                frame_id=str(frame["frame_id"]),
                cursor_id=str(cursor["cursor_id"]),
            )
            state.hold_id = hold_id
        result = _result(
            status="AVAILABLE", operation="BEGIN", attempt=_attempt_public(state), episode=recipe.as_dict(),
            source_run_id=source_run_id, current_frame=frame, hold_id=hold_id,
            assistance=[_assistance("PREPARATION_ATTRIBUTED", frame, _UNAVAILABLE_WALL_TIME, "Preparation actions are attributed to the recipe, not the learner.")],
        )
    except Exception:
        _ATTEMPTS.pop(attempt_id, None)
        return _begin_refusal(handle, "PRACTICE_RESULT_PUBLICATION_FAILED")
    _REQUEST_OUTCOMES[request.request_id] = _RequestOutcome(handle, copy.deepcopy(result))
    return handle, result


def _current(handle: object, source_run_id: str) -> dict[str, object] | None:
    record = read_current_simulation_frame(handle, source_run_id)
    frame = record.get("current_frame")
    return dict(frame) if record.get("status") == "AVAILABLE" and isinstance(frame, Mapping) else None


def _state_for_request(handle: object, request: PracticeActionRequestV1) -> _AttemptState | None:
    state = _ATTEMPTS.get(request.attempt_id)
    if state is None or not _practice_attempt_matches(handle, state.binding):
        return None
    if state.record["source_run_id"] != request.source_run_id:
        return None
    return state


def _origin_matches(frame: Mapping[str, object], request: PracticeActionRequestV1) -> bool:
    cursor = _cursor(frame)
    return frame["frame_id"] == request.origin_frame_id and cursor["cursor_id"] == request.origin_cursor_id


def _unavailable_action(request: PracticeActionRequestV1, reason: str) -> dict[str, object]:
    state = _ATTEMPTS.get(request.attempt_id)
    return _result(
        status="UNAVAILABLE", operation=request.operation,
        attempt=None if state is None else _attempt_public(state),
        episode=None if state is None else state.episode.as_dict(),
        source_run_id=request.source_run_id, unavailable_reason=reason,
    )


def _valid_to_stage(state: _AttemptState, assessment: Mapping[str, object]) -> bool:
    outcome = assessment["outcome"]
    if state.episode.family == "F2_READING":
        return outcome in {"PASS", "NO_OPPORTUNITY", "INSUFFICIENT_EVIDENCE"}
    return outcome in {"PASS", "LEARNER_ABORT"}


def _advance_step_or_rehold(
    handle: object,
    state: _AttemptState,
    frame: Mapping[str, object],
    *,
    successful_semantic_action: bool,
) -> str | None:
    if successful_semantic_action:
        state.step_index += 1
    if state.step_index >= len(state.episode.expected_actions) or state.record["mode"] != "GUIDED":
        return None
    hold_id = _hold_id(str(state.record["attempt_id"]), frame)
    cursor = _cursor(frame)
    _activate_guided_hold(
        handle,
        attempt_id=str(state.record["attempt_id"]), hold_id=hold_id,
        source_run_id=str(state.record["source_run_id"]), frame_id=str(frame["frame_id"]),
        cursor_id=str(cursor["cursor_id"]),
    )
    return hold_id


def _system_failure_assessment(
    episode: PracticeEpisodeDefinitionV1,
    frame: Mapping[str, object],
    *,
    response_kind: str,
    semantic_action_id: str | None,
    action_index: int,
    causal_assessment: Mapping[str, object],
) -> dict[str, object]:
    assessment = assess_response(
        episode, frame, response_kind=response_kind,
        semantic_action_id=semantic_action_id, action_index=action_index,
    )
    evidence = causal_assessment.get("evidence")
    if isinstance(evidence, Mapping):
        assessment["evidence"] = dict(evidence)
    assessment["outcome"] = "SYSTEM_FAILURE"
    assessment["message"] = "The ordinary command boundary did not confirm the staged action; the attempt remains recoverable."
    return assessment


def submit_simulation_practice_action(
    handle: object,
    request_payload: Mapping[str, object],
) -> dict[str, object]:
    """Stage, release, or directly execute one learner action under public fences."""

    request = PracticeActionRequestV1.from_dict(request_payload)
    state = _state_for_request(handle, request)
    if state is None:
        return _unavailable_action(request, "ATTEMPT_IDENTITY_MISMATCH")
    frame = _current(handle, request.source_run_id)
    if frame is None:
        return _unavailable_action(request, "CURRENT_FRAME_UNAVAILABLE")
    if not _origin_matches(frame, request):
        return _unavailable_action(request, "STALE_ORIGIN")
    episode = state.episode
    if request.operation == "STAGE":
        if state.record["mode"] != "GUIDED" or request.hold_id is None:
            return _unavailable_action(request, "GUIDED_HOLD_REQUIRED")
        assessment = assess_response(
            episode, frame, response_kind=request.response_kind,
            semantic_action_id=request.semantic_action_id, action_index=state.step_index,
        )
        debrief = debrief_from_assessment(assessment, source_run_id=request.source_run_id, action_request_id=request.request_id)
        if not _valid_to_stage(state, assessment):
            return _result(
                status="AVAILABLE", operation="STAGE", attempt=_attempt_public(state), episode=episode.as_dict(),
                source_run_id=request.source_run_id, current_frame=frame, hold_id=request.hold_id,
                assistance=[_assistance("GUIDED_FEEDBACK_NO_LIVE_COMMAND", frame, request.wall_time, "The staged response is not valid; no simulation command was sent.")],
                assessment=assessment, debrief=debrief,
            )
        staged = {
            "request_id": request.request_id, "response_kind": request.response_kind,
            "semantic_action_id": request.semantic_action_id, "assessment": assessment,
            "wall_time": dict(request.wall_time), "action_index": state.step_index,
        }
        cursor = _cursor(frame)
        if not _stage_practice_response(
            handle, state.binding, attempt_id=request.attempt_id, hold_id=request.hold_id,
            source_run_id=request.source_run_id, frame_id=request.origin_frame_id,
            cursor_id=request.origin_cursor_id, response=staged,
        ):
            return _unavailable_action(request, "GUIDED_HOLD_MISMATCH")
        state.initial_begin_active = False
        return _result(
            status="AVAILABLE", operation="STAGE", attempt=_attempt_public(state), episode=episode.as_dict(),
            source_run_id=request.source_run_id, current_frame=frame, hold_id=request.hold_id,
            assistance=[_assistance("GUIDED_STAGED", frame, request.wall_time, "The response is staged; explicit Continue is required before release.")],
            assessment=assessment, debrief=debrief,
        )
    if request.operation == "CONTINUE":
        if state.record["mode"] != "GUIDED" or request.hold_id is None:
            return _unavailable_action(request, "GUIDED_HOLD_REQUIRED")
        staged = _peek_staged_practice_response(handle, state.binding)
        if not isinstance(staged, Mapping) or (
            staged.get("response_kind") != request.response_kind
            or staged.get("semantic_action_id") != request.semantic_action_id
        ):
            return _unavailable_action(request, "NO_MATCHING_STAGED_RESPONSE")
        if not _release_guided_hold(
            handle, attempt_id=request.attempt_id, hold_id=request.hold_id,
            source_run_id=request.source_run_id, frame_id=request.origin_frame_id,
            cursor_id=request.origin_cursor_id,
        ):
            return _unavailable_action(request, "GUIDED_HOLD_MISMATCH")
        assessment = dict(staged["assessment"])
        if request.response_kind != "SEMANTIC_ACTION":
            staged = _take_staged_practice_response(handle, state.binding)
            if not isinstance(staged, Mapping):
                raise RuntimeError("guided non-command response disappeared after its exact release")
            state.hold_id = None
            debrief = debrief_from_assessment(assessment, source_run_id=request.source_run_id, action_request_id=request.request_id)
            return _result(
                status="AVAILABLE", operation="CONTINUE", attempt=_attempt_public(state), episode=episode.as_dict(),
                source_run_id=request.source_run_id, current_frame=frame, hold_id=None,
                assistance=[_assistance("GUIDED_RELEASED", frame, request.wall_time, "The staged non-command response was released without a simulation command.")],
                assessment=assessment, debrief=debrief,
            )
        try:
            command = SimulationCommandResultV1.from_dict(
                dispatch_simulation_command(handle, _command_request(frame, request.semantic_action_id or ""))
            )
        except Exception:
            _activate_guided_hold(
                handle, attempt_id=request.attempt_id, hold_id=request.hold_id,
                source_run_id=request.source_run_id, frame_id=request.origin_frame_id,
                cursor_id=request.origin_cursor_id,
            )
            state.hold_id = request.hold_id
            failure = _system_failure_assessment(
                episode, frame, response_kind=request.response_kind,
                semantic_action_id=request.semantic_action_id, action_index=state.step_index,
                causal_assessment=assessment,
            )
            debrief = debrief_from_assessment(failure, source_run_id=request.source_run_id, action_request_id=request.request_id)
            return _result(
                status="AVAILABLE", operation="CONTINUE", attempt=_attempt_public(state), episode=episode.as_dict(),
                source_run_id=request.source_run_id, current_frame=frame, hold_id=request.hold_id,
                assistance=[_assistance("GUIDED_FEEDBACK_NO_LIVE_COMMAND", frame, request.wall_time, "Command dispatch failed before confirmation; the original guided hold was restored.")],
                assessment=failure, debrief=debrief,
            )
        if command.status != "AVAILABLE" or command.outcome is None or command.destination_frame is None or not command.outcome.accepted:
            destination = frame if command.destination_frame is None else command.destination_frame.as_dict()
            _take_staged_practice_response(handle, state.binding)
            next_hold = _advance_step_or_rehold(handle, state, destination, successful_semantic_action=False)
            state.hold_id = next_hold
            failure = _system_failure_assessment(
                episode, destination, response_kind=request.response_kind,
                semantic_action_id=request.semantic_action_id, action_index=state.step_index,
                causal_assessment=assessment,
            )
            debrief = debrief_from_assessment(failure, source_run_id=request.source_run_id, action_request_id=request.request_id)
            return _result(
                status="AVAILABLE", operation="CONTINUE", attempt=_attempt_public(state), episode=episode.as_dict(),
                source_run_id=request.source_run_id, current_frame=destination, hold_id=next_hold,
                assistance=[_assistance("GUIDED_FEEDBACK_NO_LIVE_COMMAND", destination, request.wall_time, "The command did not confirm; a fresh guided hold was retained for recovery.")],
                assessment=failure, debrief=debrief,
            )
        staged = _take_staged_practice_response(handle, state.binding)
        if not isinstance(staged, Mapping):
            raise RuntimeError("guided response disappeared after confirmed command dispatch")
        destination = command.destination_frame.as_dict()
        assessment = assess_dispatched_action(
            episode, destination, action_index=state.step_index, command_outcome=command.outcome.as_dict(),
            causal_evidence=assessment["evidence"],
        )
        next_hold = _advance_step_or_rehold(
            handle, state, destination, successful_semantic_action=assessment["outcome"] == "PASS",
        )
        state.hold_id = next_hold
        debrief = debrief_from_assessment(assessment, source_run_id=request.source_run_id, action_request_id=request.request_id)
        return _result(
            status="AVAILABLE", operation="CONTINUE", attempt=_attempt_public(state), episode=episode.as_dict(),
            source_run_id=request.source_run_id, current_frame=destination, hold_id=next_hold,
            assistance=[_assistance("GUIDED_RELEASED", destination, request.wall_time, "The matching staged semantic action was released exactly once.")],
            assessment=assessment, debrief=debrief,
        )
    if request.operation != "UNASSISTED":
        return _unavailable_action(request, "UNSUPPORTED_ACTION_OPERATION")
    if state.record["mode"] != "UNASSISTED":
        return _unavailable_action(request, "UNASSISTED_MODE_REQUIRED")
    assessment = assess_response(
        episode, frame, response_kind=request.response_kind,
        semantic_action_id=request.semantic_action_id, action_index=state.step_index,
    )
    if request.response_kind == "SEMANTIC_ACTION":
        command = SimulationCommandResultV1.from_dict(
            dispatch_simulation_command(handle, _command_request(frame, request.semantic_action_id or ""))
        )
        if command.status != "AVAILABLE" or command.outcome is None or command.destination_frame is None:
            return _unavailable_action(request, "UNASSISTED_COMMAND_UNAVAILABLE")
        frame = command.destination_frame.as_dict()
        assessment = assess_dispatched_action(
            episode, frame, action_index=state.step_index, command_outcome=command.outcome.as_dict(),
            causal_evidence=assessment["evidence"],
        )
        if assessment["outcome"] == "PASS":
            state.step_index += 1
    debrief = debrief_from_assessment(assessment, source_run_id=request.source_run_id, action_request_id=request.request_id)
    return _result(
        status="AVAILABLE", operation="UNASSISTED", attempt=_attempt_public(state), episode=episode.as_dict(),
        source_run_id=request.source_run_id, current_frame=frame, hold_id=None,
        assistance=[_assistance("UNASSISTED_DISPATCH", frame, request.wall_time, "Unassisted semantic actions are sent through the ordinary command boundary.")],
        assessment=assessment, debrief=debrief,
    )


__all__ = [
    "begin_simulation_practice_attempt", "list_simulation_practice_episodes",
    "submit_simulation_practice_action",
]
