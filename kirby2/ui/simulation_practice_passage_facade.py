"""Backend-owned immutable observation passages for paced focused practice."""

from __future__ import annotations

import copy
from collections.abc import Mapping

from kirby2.curriculum.practice_episodes import (
    PracticeEpisodeDefinitionV1,
    list_practice_episodes_v1,
)

from .simulation_contract import canonical_digest
from .simulation_episode_contract import (
    FULL_MODEL_PREFIX_PROJECTION_ID,
    SimulationEpisodePreparationRequestV1,
    build_simulation_episode_preparation_request,
    episode_prefix_projection,
    episode_prefix_projection_sha256,
)
from .simulation_episode_facade import _episode_binding
from .simulation_interaction_contract import SimulationCommandResultV1
from .simulation_lifecycle_contract import SimulationCloseResultV1
from .simulation_live_contract import SimulationFrameV1, SimulationStartResultV1
from .simulation_practice_facade import (
    _ATTEMPTS,
    _command_request,
    _current,
    _resolution,
    _training_options,
)
from .simulation_practice_passage_contract import (
    PRACTICE_PASSAGE_CAPABILITY_SCHEMA_ID,
    PRACTICE_PASSAGE_SAMPLING_POLICY,
    PRACTICE_PASSAGE_TIE_ORDER,
    PracticeObservationPassageRequestV1,
    PracticeObservationPassageResultV1,
    PracticePassageCapabilityCatalogV1,
    build_practice_passage_observation,
    build_practice_passage_result,
)
from .simulation_run_facade import (
    _bind_prepared_episode_identity,
    _practice_attempt_matches,
    _prepared_episode_model_sha256,
    advance_simulation_run,
    close_simulation_run,
    dispatch_simulation_command,
    read_current_simulation_frame,
    start_simulation_run,
)


_F1_UNSUPPORTED_REASON = "ANCHOR_TOO_SHORT_FOR_MEANINGFUL_PACE"
_UNVERIFIED_RECIPE_REASON = "RECIPE_NOT_VERIFIED_FOR_PASSAGE"
_SUPPORTED_RECIPES = {
    "practice.f2.public-pressure.v1": (
        "62ef9ec704b92bd45b9aaf09a61a7d21703da09b7c960c465b86e6693e228122",
        1_000_000,
    ),
    "practice.f2.replenishment.v1": (
        "8b8b748e2156ef187dc5e490ccb09e8823e5e8aaac668ab0c26e3085cc7a3d21",
        1_000_000,
    ),
    "practice.f3.cancel-partial-residual.v1": (
        "a29b223fb7ad3342a1142a70132d41a46e4bc3dc80ffa6824995f4424fdb7474",
        1_000_000,
    ),
    "practice.f3.cancel-volume-variation.v1": (
        "bbea819721cd659794c94672b6649e941d46629b972c42e2fbabbdc31827b9e6",
        1_000_000,
    ),
}


def _passage_support(recipe: PracticeEpisodeDefinitionV1) -> tuple[bool, str | None]:
    expected = _SUPPORTED_RECIPES.get(recipe.episode_id)
    if expected == (recipe.recipe_sha256, recipe.anchor_time_us):
        return True, None
    if recipe.anchor_time_us < 1_000_000:
        return False, _F1_UNSUPPORTED_REASON
    return False, _UNVERIFIED_RECIPE_REASON


def _capability_entry(recipe: PracticeEpisodeDefinitionV1) -> dict[str, object]:
    supported, reason = _passage_support(recipe)
    return {
        "episode_id": recipe.episode_id,
        "episode_recipe_sha256": recipe.recipe_sha256,
        "family": recipe.family,
        "support": "AVAILABLE" if supported else "UNSUPPORTED",
        "passage_duration_us": recipe.anchor_time_us if supported else None,
        "unavailable_reason": reason,
    }


def list_simulation_practice_passage_capabilities() -> dict[str, object]:
    """Declare exact per-recipe support without changing the V1 practice catalog."""

    episodes = [_capability_entry(recipe) for recipe in list_practice_episodes_v1()]
    basis = {
        "schema_id": PRACTICE_PASSAGE_CAPABILITY_SCHEMA_ID,
        "schema_version": 1,
        "sampling_policy": PRACTICE_PASSAGE_SAMPLING_POLICY,
        "tie_order": PRACTICE_PASSAGE_TIE_ORDER,
        "episodes": episodes,
    }
    record = {
        **basis,
        "catalog_id": f"practice-passage-capabilities-{canonical_digest(basis)[:24]}",
    }
    return PracticePassageCapabilityCatalogV1.from_dict(record).as_dict()


def _unavailable(
    request: PracticeObservationPassageRequestV1,
    reason: str,
    *,
    ownership: str = "NO_RESOURCE",
    cleanup: str | None = None,
) -> dict[str, object]:
    record = build_practice_passage_result(
        status="UNAVAILABLE",
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        source_run_id=request.source_run_id,
        episode_id=request.episode_id,
        episode_recipe_sha256=request.episode_recipe_sha256,
        prepared_result_id=request.prepared_result_id,
        prepared_identity=request.prepared_identity.as_dict(),
        observation_policy_ref=request.observation_policy_ref.as_dict(),
        sampling_policy=PRACTICE_PASSAGE_SAMPLING_POLICY,
        tie_order=PRACTICE_PASSAGE_TIE_ORDER,
        passage_start_time_us=None,
        passage_end_time_us=None,
        passage_duration_us=None,
        prefix_actions=None,
        transient_source_run_id=None,
        observations=[],
        final_cut=None,
        model_invariance=None,
        resource_ownership=ownership,
        cleanup_disposition=cleanup,
        unavailable_reason=reason,
    )
    return PracticeObservationPassageResultV1.from_dict(record, request=request).as_dict()


def _sample_times(anchor_time_us: int) -> tuple[int, ...]:
    """Return stable quarter points; chunk boundaries do not alter session history."""

    values = {0, anchor_time_us}
    for numerator in (1, 2, 3):
        values.add((anchor_time_us * numerator + 3) // 4)
    result = tuple(sorted(values))
    if len(result) != 5 or result[0] != 0 or result[-1] != anchor_time_us:
        raise RuntimeError("supported practice passage has an invalid sampling interval")
    return result


def _live_context(
    handle: object,
    request: PracticeObservationPassageRequestV1,
) -> tuple[PracticeEpisodeDefinitionV1, dict[str, object]] | None:
    state = _ATTEMPTS.get(request.attempt_id)
    if state is None or not _practice_attempt_matches(handle, state.binding):
        return None
    record = state.record
    recipe = state.episode
    if (
        record["source_run_id"] != request.source_run_id
        or record["episode_id"] != request.episode_id
        or record["episode_recipe_sha256"] != request.episode_recipe_sha256
        or record["prepared_result_id"] != request.prepared_result_id
        or record["prepared_identity"] != request.prepared_identity.as_dict()
        or recipe.recipe_sha256 != request.episode_recipe_sha256
    ):
        return None
    current = _current(handle, request.source_run_id)
    if current is None:
        return None
    cursor = current["cursor"]
    if (
        current["frame_id"] != request.origin_frame_id
        or cursor["cursor_id"] != request.origin_cursor_id
        or cursor["simulation_time_us"] != recipe.anchor_time_us
        or cursor["run_state"] != "PAUSED"
        or current["provenance"]["observation_policy_ref"]
        != request.observation_policy_ref.as_dict()
    ):
        return None
    return recipe, current


def _close_transient(handle: object) -> bool:
    try:
        result = SimulationCloseResultV1.from_dict(
            close_simulation_run(handle, "USER_ABANDONED")
        )
    except Exception:
        return False
    return result.status == "CLOSED"


def acquire_simulation_practice_observation_passage(
    handle: object,
    request_payload: Mapping[str, object],
) -> tuple[object | None, dict[str, object]]:
    """Reconstruct, validate, detach, and close one public pre-answer passage.

    The first tuple member is non-null only when transient cleanup could not be
    confirmed. The learner's live handle is never advanced, finalized, or closed.
    """

    request = PracticeObservationPassageRequestV1.from_dict(request_payload)
    context = _live_context(handle, request)
    if context is None:
        return None, _unavailable(request, "ATTEMPT_IDENTITY_MISMATCH")
    recipe, live_frame_before = context
    supported, _ = _passage_support(recipe)
    if not supported:
        return None, _unavailable(request, "EPISODE_PASSAGE_UNSUPPORTED")

    live_model_before = _prepared_episode_model_sha256(handle)
    transient: object | None = None
    cleanup_confirmed = False
    observations: list[dict[str, object]] = []
    transient_model: str | None = None
    transient_frame: SimulationFrameV1 | None = None
    prefix_actions = tuple(recipe.preparation_actions)
    try:
        resolution = _resolution(recipe)
        training = _training_options()
        preparation = SimulationEpisodePreparationRequestV1.from_dict(
            build_simulation_episode_preparation_request(
                episode_id=recipe.episode_id,
                episode_version=1,
                resolution_payload=resolution,
                training_options_payload=training,
                prefix_actions=prefix_actions,
                anchor_time_us=recipe.anchor_time_us,
            )
        )
        if preparation.identity != request.prepared_identity:
            raise RuntimeError("reconstructed preparation identity changed")
        transient, start_payload = start_simulation_run(resolution, training)
        start = SimulationStartResultV1.from_dict(
            start_payload,
            resolution=preparation.resolution,
            training_options=preparation.training_options,
        )
        if transient is None or start.status != "AVAILABLE" or start.initial_frame is None:
            raise RuntimeError("transient reconstruction did not start")
        transient_frame = start.initial_frame
        for action in prefix_actions:
            command = SimulationCommandResultV1.from_dict(
                dispatch_simulation_command(
                    transient,
                    _command_request(transient_frame.as_dict(), action),
                )
            )
            if (
                command.status != "AVAILABLE"
                or command.outcome is None
                or not command.outcome.accepted
                or command.destination_frame is None
            ):
                raise RuntimeError("transient reconstruction prefix was rejected")
            transient_frame = command.destination_frame
        observations.append(
            build_practice_passage_observation(
                transient_frame.as_dict(), observation_sequence=1, tie_breaker=0
            )
        )
        for target_time_us in _sample_times(recipe.anchor_time_us)[1:]:
            cursor = transient_frame.as_dict()["cursor"]
            advance = advance_simulation_run(
                transient,
                transient_frame.source_run_id,
                transient_frame.frame_id,
                str(cursor["cursor_id"]),
                target_time_us,
            )
            if advance.get("status") != "AVAILABLE" or not isinstance(
                advance.get("destination_frame"), Mapping
            ):
                raise RuntimeError("transient passage advance was unavailable")
            transient_frame = SimulationFrameV1.from_dict(advance["destination_frame"])
            if target_time_us == recipe.anchor_time_us:
                pause = SimulationCommandResultV1.from_dict(
                    dispatch_simulation_command(
                        transient,
                        _command_request(transient_frame.as_dict(), "SIMULATION_PAUSE"),
                    )
                )
                if (
                    pause.status != "AVAILABLE"
                    or pause.outcome is None
                    or not pause.outcome.accepted
                    or pause.destination_frame is None
                ):
                    raise RuntimeError("transient passage terminal pause failed")
                transient_frame = pause.destination_frame
            observations.append(
                build_practice_passage_observation(
                    transient_frame.as_dict(),
                    observation_sequence=len(observations) + 1,
                    tie_breaker=0,
                )
            )
        _bind_prepared_episode_identity(
            transient,
            _episode_binding(
                request_id=preparation.request_id,
                identity=preparation.identity,
                prefix_actions=preparation.prefix_actions,
                prefix_timing_policy=preparation.prefix_timing_policy,
            ),
        )
        transient_model = _prepared_episode_model_sha256(transient)
    except Exception:
        if transient is not None:
            cleanup_confirmed = _close_transient(transient)
            if not cleanup_confirmed:
                return transient, _unavailable(
                    request,
                    "TRANSIENT_CLEANUP_UNCONFIRMED",
                    ownership="CALLER_OWNS_CLEANUP_HANDLE",
                    cleanup="CLEANUP_UNCONFIRMED",
                )
        return None, _unavailable(
            request,
            "RECONSTRUCTION_FAILED",
            cleanup="TRANSIENT_RECONSTRUCTION_CLOSED" if cleanup_confirmed else None,
        )

    assert transient is not None and transient_frame is not None and transient_model is not None
    cleanup_confirmed = _close_transient(transient)
    if not cleanup_confirmed:
        return transient, _unavailable(
            request,
            "TRANSIENT_CLEANUP_UNCONFIRMED",
            ownership="CALLER_OWNS_CLEANUP_HANDLE",
            cleanup="CLEANUP_UNCONFIRMED",
        )

    current_after = read_current_simulation_frame(handle, request.source_run_id)
    live_frame_after = current_after.get("current_frame")
    if (
        current_after.get("status") != "AVAILABLE"
        or not isinstance(live_frame_after, Mapping)
        or dict(live_frame_after) != live_frame_before
    ):
        return None, _unavailable(
            request,
            "CURRENT_FRAME_CHANGED",
            cleanup="TRANSIENT_RECONSTRUCTION_CLOSED",
        )
    live_model_after = _prepared_episode_model_sha256(handle)
    terminal_prefix = episode_prefix_projection_sha256(transient_frame, prefix_actions)
    expected_prefix = episode_prefix_projection_sha256(
        SimulationFrameV1.from_dict(live_frame_before), prefix_actions
    )
    if terminal_prefix != expected_prefix:
        return None, _unavailable(
            request,
            "TERMINAL_PROJECTION_MISMATCH",
            cleanup="TRANSIENT_RECONSTRUCTION_CLOSED",
        )
    if not (live_model_before == transient_model == live_model_after):
        return None, _unavailable(
            request,
            "FULL_MODEL_PREFIX_MISMATCH",
            cleanup="TRANSIENT_RECONSTRUCTION_CLOSED",
        )
    terminal_projection = canonical_digest(episode_prefix_projection(transient_frame))
    try:
        record = build_practice_passage_result(
            status="AVAILABLE",
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            source_run_id=request.source_run_id,
            episode_id=request.episode_id,
            episode_recipe_sha256=request.episode_recipe_sha256,
            prepared_result_id=request.prepared_result_id,
            prepared_identity=request.prepared_identity.as_dict(),
            observation_policy_ref=request.observation_policy_ref.as_dict(),
            sampling_policy=PRACTICE_PASSAGE_SAMPLING_POLICY,
            tie_order=PRACTICE_PASSAGE_TIE_ORDER,
            passage_start_time_us=0,
            passage_end_time_us=recipe.anchor_time_us,
            passage_duration_us=recipe.anchor_time_us,
            prefix_actions=list(prefix_actions),
            transient_source_run_id=transient_frame.source_run_id,
            observations=observations,
            final_cut={
                "source_run_id": request.source_run_id,
                "frame_id": request.origin_frame_id,
                "cursor_id": request.origin_cursor_id,
                "simulation_time_us": recipe.anchor_time_us,
                "prepared_prefix_projection_sha256": terminal_prefix,
                "terminal_frame_projection_sha256": terminal_projection,
            },
            model_invariance={
                "projection_id": FULL_MODEL_PREFIX_PROJECTION_ID,
                "live_before_sha256": live_model_before,
                "reconstructed_terminal_sha256": transient_model,
                "live_after_sha256": live_model_after,
                "status": "MATCH",
            },
            resource_ownership="NO_RESOURCE",
            cleanup_disposition="TRANSIENT_RECONSTRUCTION_CLOSED",
            unavailable_reason=None,
        )
        published = PracticeObservationPassageResultV1.from_dict(
            record, request=request
        ).as_dict()
        state = _ATTEMPTS.get(request.attempt_id)
        if state is not None:
            state.passage = copy.deepcopy(published)
        return None, published
    except Exception:
        return None, _unavailable(
            request,
            "RESULT_PUBLICATION_FAILED",
            cleanup="TRANSIENT_RECONSTRUCTION_CLOSED",
        )


__all__ = [
    "acquire_simulation_practice_observation_passage",
    "list_simulation_practice_passage_capabilities",
]
