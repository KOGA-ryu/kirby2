"""Deterministic playable-lesson assembly over sealed extracted sources (WO33-D)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kirby2.curriculum.models import MinedCurriculumLineageV1
from kirby2.immutable import freeze_json, thaw_json

from .extraction import ExtractedLessonSourceV1
from .models import LessonCandidateV1, canonical_json_bytes, sha256_json
from .selection import DiversitySelectionDecisionV1


PLAYABLE_LESSON_SCHEMA_VERSION_V1 = 1
PLAYABLE_ASSESSMENT_POLICY_ID_V1 = "RECORDED_CLIENT_FEED_ASSESSMENT_V1"
PLAYABLE_REVEAL_POLICY_ID_V1 = "COMPLETED_ASSESSMENT_GRANT_V1"
PLAYER_OVERLAY_PROVENANCE_V1 = "PLAYER_ACTION_OVERLAY_NOT_SOURCE_HISTORY"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTION_KIND = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class MinedLessonAssessmentV1:
    """Closed client-facing type: no answer key or future boundary fields."""

    lesson_id: str
    lesson_digest: str
    source_record_sha256: str
    playback_elapsed_us: int
    classification_status: str
    objective_kind: str
    objective_prompt: str
    observable_feed: tuple[Mapping[str, object], ...]
    observable_feed_prefix_sha256: str
    assessment_policy_id: str = PLAYABLE_ASSESSMENT_POLICY_ID_V1
    schema_version: int = PLAYABLE_LESSON_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if self.lesson_id != f"mined-lesson-{self.lesson_digest}":
            raise ValueError("assessment lesson ID and digest disagree")
        _require_sha256(self.lesson_digest, "assessment lesson digest")
        _require_sha256(self.source_record_sha256, "assessment source-record digest")
        _require_sha256(
            self.observable_feed_prefix_sha256,
            "assessment observable-prefix digest",
        )
        if type(self.playback_elapsed_us) is not int or self.playback_elapsed_us < 0:
            raise ValueError("assessment playback time must be nonnegative")
        if self.classification_status not in {"NOT_YET_OPEN", "OPEN", "CLOSED"}:
            raise ValueError("assessment classification status is invalid")
        if self.objective_kind != "OBSERVE_CLASSIFY_V1":
            raise ValueError("assessment objective kind is unsupported")
        if self.objective_prompt != "Classify the observable market-structure pattern.":
            raise ValueError("assessment objective prompt differs from policy")
        feed = freeze_json(self.observable_feed)
        if not isinstance(feed, tuple) or any(
            not isinstance(item, Mapping) for item in feed
        ):
            raise TypeError("assessment observable feed must be an object tuple")
        if self.assessment_policy_id != PLAYABLE_ASSESSMENT_POLICY_ID_V1:
            raise ValueError("assessment policy ID is unsupported")
        if self.schema_version != PLAYABLE_LESSON_SCHEMA_VERSION_V1:
            raise ValueError("assessment schema version is unsupported")
        object.__setattr__(self, "observable_feed", feed)

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment_policy_id": self.assessment_policy_id,
            "classification_status": self.classification_status,
            "lesson_digest": self.lesson_digest,
            "lesson_id": self.lesson_id,
            "objective_kind": self.objective_kind,
            "objective_prompt": self.objective_prompt,
            "observable_feed": thaw_json(self.observable_feed),
            "observable_feed_prefix_sha256": (
                self.observable_feed_prefix_sha256
            ),
            "playback_elapsed_us": self.playback_elapsed_us,
            "record_kind": "MINED_LESSON_ASSESSMENT_V1",
            "schema_version": self.schema_version,
            "source_record_sha256": self.source_record_sha256,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class MinedLessonRevealPayloadV1:
    """Answer key and debrief material kept outside the assessment type."""

    candidate_id: str
    candidate_digest: str
    detector: Mapping[str, object]
    direction: str
    source_window_outcome: str
    post_event_boundary_us: int
    difficulty_projection: Mapping[str, object]
    selection_reason: Mapping[str, object]
    reveal_material: Mapping[str, object]
    protected_ground_truth: Mapping[str, object] | None
    observable_feature_summary: Mapping[str, object]
    source_record: Mapping[str, object]
    source_envelope_sha256: str
    hidden_generation_state: Mapping[str, object]
    debrief: Mapping[str, object]
    reveal_policy_id: str = PLAYABLE_REVEAL_POLICY_ID_V1
    schema_version: int = PLAYABLE_LESSON_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if self.candidate_id != f"lesson-candidate-{self.candidate_digest}":
            raise ValueError("reveal candidate ID and digest disagree")
        _require_sha256(self.candidate_digest, "reveal candidate digest")
        _require_sha256(self.source_envelope_sha256, "reveal source-envelope digest")
        if type(self.direction) is not str or not self.direction:
            raise ValueError("reveal direction is required")
        if type(self.source_window_outcome) is not str or not self.source_window_outcome:
            raise ValueError("reveal source-window outcome is required")
        if type(self.post_event_boundary_us) is not int or self.post_event_boundary_us <= 0:
            raise ValueError("reveal post-event boundary must be positive")
        for field_name in (
            "detector",
            "difficulty_projection",
            "selection_reason",
            "reveal_material",
            "observable_feature_summary",
            "source_record",
            "hidden_generation_state",
            "debrief",
        ):
            frozen = freeze_json(getattr(self, field_name))
            if not isinstance(frozen, Mapping):
                raise TypeError(f"reveal {field_name} must be an object")
            object.__setattr__(self, field_name, frozen)
        if self.protected_ground_truth is not None:
            truth = freeze_json(self.protected_ground_truth)
            if not isinstance(truth, Mapping):
                raise TypeError("reveal protected ground truth must be an object")
            object.__setattr__(self, "protected_ground_truth", truth)
        if self.reveal_policy_id != PLAYABLE_REVEAL_POLICY_ID_V1:
            raise ValueError("reveal policy ID is unsupported")
        if self.schema_version != PLAYABLE_LESSON_SCHEMA_VERSION_V1:
            raise ValueError("reveal schema version is unsupported")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "debrief": thaw_json(self.debrief),
            "detector": thaw_json(self.detector),
            "difficulty_projection": thaw_json(self.difficulty_projection),
            "direction": self.direction,
            "hidden_generation_state": thaw_json(self.hidden_generation_state),
            "observable_feature_summary": thaw_json(
                self.observable_feature_summary
            ),
            "post_event_boundary_us": self.post_event_boundary_us,
            "protected_ground_truth": (
                None
                if self.protected_ground_truth is None
                else thaw_json(self.protected_ground_truth)
            ),
            "record_kind": "MINED_LESSON_REVEAL_V1",
            "reveal_material": thaw_json(self.reveal_material),
            "reveal_policy_id": self.reveal_policy_id,
            "schema_version": self.schema_version,
            "selection_reason": thaw_json(self.selection_reason),
            "source_envelope_sha256": self.source_envelope_sha256,
            "source_record": thaw_json(self.source_record),
            "source_window_outcome": self.source_window_outcome,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True, slots=True)
class MinedLessonRevealGrantV1:
    lesson_id: str
    lesson_digest: str
    reveal_payload_sha256: str
    completion_assessment_sha256: str
    scope: str = "COMPLETED_ASSESSMENT_REVEAL"

    def __post_init__(self) -> None:
        if self.lesson_id != f"mined-lesson-{self.lesson_digest}":
            raise ValueError("reveal grant lesson ID and digest disagree")
        _require_sha256(self.lesson_digest, "reveal grant lesson digest")
        _require_sha256(self.reveal_payload_sha256, "reveal grant payload digest")
        _require_sha256(
            self.completion_assessment_sha256,
            "reveal grant completion digest",
        )
        if self.scope != "COMPLETED_ASSESSMENT_REVEAL":
            raise ValueError("reveal grant scope is invalid")


@dataclass(frozen=True, slots=True)
class MinedPlayableLessonV1:
    curriculum_lineage: MinedCurriculumLineageV1
    source: ExtractedLessonSourceV1 = field(repr=False)
    reveal_payload: MinedLessonRevealPayloadV1 = field(repr=False)
    activation_elapsed_us: int = field(repr=False)
    duration_us: int = field(repr=False)
    schema_version: int = PLAYABLE_LESSON_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if not isinstance(self.curriculum_lineage, MinedCurriculumLineageV1):
            raise TypeError("playable lesson curriculum lineage is invalid")
        if not isinstance(self.source, ExtractedLessonSourceV1):
            raise TypeError("playable lesson extracted source is invalid")
        if not isinstance(self.reveal_payload, MinedLessonRevealPayloadV1):
            raise TypeError("playable lesson reveal payload is invalid")
        if (
            type(self.activation_elapsed_us) is not int
            or self.activation_elapsed_us < 0
            or type(self.duration_us) is not int
            or self.duration_us <= self.activation_elapsed_us
        ):
            raise ValueError("playable lesson relative timing is invalid")
        if (
            self.curriculum_lineage.source_record_sha256
            != self.source.source_record.sha256
            or self.curriculum_lineage.source_envelope_sha256
            != self.source.envelope.sha256
        ):
            raise ValueError("playable lesson curriculum/source lineage differs")
        if self.reveal_payload.source_envelope_sha256 != self.source.envelope.sha256:
            raise ValueError("playable lesson reveal/source lineage differs")
        if self.schema_version != PLAYABLE_LESSON_SCHEMA_VERSION_V1:
            raise ValueError("playable lesson schema version is unsupported")

    def identity_projection(self) -> dict[str, object]:
        return {
            "activation_elapsed_us": self.activation_elapsed_us,
            "assessment_policy_id": PLAYABLE_ASSESSMENT_POLICY_ID_V1,
            "curriculum_lineage_sha256": self.curriculum_lineage.sha256,
            "duration_us": self.duration_us,
            "record_kind": "MINED_PLAYABLE_LESSON_V1",
            "reveal_payload_sha256": self.reveal_payload.sha256,
            "reveal_policy_id": PLAYABLE_REVEAL_POLICY_ID_V1,
            "schema_version": self.schema_version,
            "source_envelope_sha256": self.source.envelope.sha256,
            "source_record_sha256": self.source.source_record.sha256,
        }

    @property
    def lesson_digest(self) -> str:
        return sha256_json(self.identity_projection())

    @property
    def lesson_id(self) -> str:
        return f"mined-lesson-{self.lesson_digest}"

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "curriculum_lineage": self.curriculum_lineage.as_dict(),
                "identity": self.identity_projection(),
                "lesson_digest": self.lesson_digest,
                "lesson_id": self.lesson_id,
            }
        )

    def assessment_at(self, playback_elapsed_us: int) -> MinedLessonAssessmentV1:
        if (
            type(playback_elapsed_us) is not int
            or not 0 <= playback_elapsed_us <= self.duration_us
        ):
            raise ValueError("assessment playback time lies outside the lesson")
        bounds = self.source.source_record.source_time_bounds
        client_cut_us = bounds.warmup_start_us + playback_elapsed_us
        visible = tuple(
            event
            for event in self.source.envelope.observable_feed
            if event.client_time_us <= client_cut_us
        )
        prefix_sha256 = sha256_json(
            {
                "client_cut_us": client_cut_us,
                "events": [event.as_dict() for event in visible],
                "record_kind": "VISIBLE_CLIENT_FEED_PREFIX_V1",
            }
        )
        if playback_elapsed_us < self.activation_elapsed_us:
            status = "NOT_YET_OPEN"
        elif playback_elapsed_us < self.duration_us:
            status = "OPEN"
        else:
            status = "CLOSED"
        return MinedLessonAssessmentV1(
            lesson_id=self.lesson_id,
            lesson_digest=self.lesson_digest,
            source_record_sha256=self.source.source_record.sha256,
            playback_elapsed_us=playback_elapsed_us,
            classification_status=status,
            objective_kind="OBSERVE_CLASSIFY_V1",
            objective_prompt="Classify the observable market-structure pattern.",
            observable_feed=tuple(event.as_dict() for event in visible),
            observable_feed_prefix_sha256=prefix_sha256,
        )

    def authorize_reveal(
        self,
        completed_assessment: MinedLessonAssessmentV1,
    ) -> MinedLessonRevealGrantV1:
        if not isinstance(completed_assessment, MinedLessonAssessmentV1):
            raise PermissionError("reveal requires a typed completed assessment")
        expected = self.assessment_at(self.duration_us)
        if (
            completed_assessment.as_dict() != expected.as_dict()
            or completed_assessment.classification_status != "CLOSED"
        ):
            raise PermissionError("reveal requires this lesson's completed assessment")
        return MinedLessonRevealGrantV1(
            lesson_id=self.lesson_id,
            lesson_digest=self.lesson_digest,
            reveal_payload_sha256=self.reveal_payload.sha256,
            completion_assessment_sha256=completed_assessment.sha256,
        )

    def reveal(
        self,
        grant: MinedLessonRevealGrantV1,
    ) -> MinedLessonRevealPayloadV1:
        expected_completion = self.assessment_at(self.duration_us)
        if not isinstance(grant, MinedLessonRevealGrantV1) or (
            grant.lesson_id != self.lesson_id
            or grant.lesson_digest != self.lesson_digest
            or grant.reveal_payload_sha256 != self.reveal_payload.sha256
            or grant.completion_assessment_sha256 != expected_completion.sha256
        ):
            raise PermissionError("reveal grant is not bound to this completed lesson")
        return self.reveal_payload


def build_playable_lesson_v1(
    candidate: LessonCandidateV1,
    source: ExtractedLessonSourceV1,
    selection_decision: DiversitySelectionDecisionV1,
) -> MinedPlayableLessonV1:
    """Build a content-addressed lesson while keeping withheld fields sealed."""

    if not isinstance(candidate, LessonCandidateV1):
        raise TypeError("playable lesson build requires a typed candidate")
    if not isinstance(source, ExtractedLessonSourceV1):
        raise TypeError("playable lesson build requires a typed extracted source")
    if not isinstance(selection_decision, DiversitySelectionDecisionV1):
        raise TypeError("playable lesson build requires a typed selection decision")
    if selection_decision.candidate_id != candidate.candidate_id:
        raise ValueError("selection decision targets a different candidate")
    if source.envelope.candidate_id != candidate.candidate_id:
        raise ValueError("extracted source targets a different candidate")
    bounds = source.source_record.source_time_bounds
    if bounds.as_dict() != candidate.bounds.as_dict():
        raise ValueError("extracted source bounds differ from candidate")
    curriculum = MinedCurriculumLineageV1(
        candidate_id=candidate.candidate_id,
        candidate_digest=candidate.candidate_digest,
        source_record_sha256=source.source_record.sha256,
        source_envelope_sha256=source.envelope.sha256,
        primary_skill_id=candidate.primary_skill_id,
        supporting_skill_ids=candidate.supporting_skill_ids,
    )
    reveal = MinedLessonRevealPayloadV1(
        candidate_id=candidate.candidate_id,
        candidate_digest=candidate.candidate_digest,
        detector=candidate.detector.as_dict(),
        direction=candidate.candidate_key.direction.value,
        source_window_outcome=candidate.source_window_outcome.value,
        post_event_boundary_us=bounds.post_end_us,
        difficulty_projection=candidate.difficulty_projection.as_dict(),
        selection_reason=selection_decision.as_dict(),
        reveal_material=candidate.reveal_material.as_dict(),
        protected_ground_truth=(
            None
            if candidate.ground_truth_summary is None
            else candidate.ground_truth_summary.as_dict()
        ),
        observable_feature_summary=(
            candidate.observable_feature_summary.as_dict()
        ),
        source_record=source.source_record.as_dict(),
        source_envelope_sha256=source.envelope.sha256,
        hidden_generation_state={
            "hidden_schedule": thaw_json(source.envelope.hidden_schedule),
            "rng_state": thaw_json(source.envelope.rng_state),
        },
        debrief={
            "known_ambiguity": list(candidate.known_ambiguity),
            "objective_outcome_mapping_id": "OBSERVE_CLASSIFY_OUTCOME_V1",
            "primary_skill_id": candidate.primary_skill_id,
            "supporting_skill_ids": list(candidate.supporting_skill_ids),
        },
    )
    return MinedPlayableLessonV1(
        curriculum_lineage=curriculum,
        source=source,
        reveal_payload=reveal,
        activation_elapsed_us=bounds.activation_us - bounds.warmup_start_us,
        duration_us=bounds.post_end_us - bounds.warmup_start_us,
    )


def assessment_replay_sha256_v1(
    lesson: MinedPlayableLessonV1,
    playback_times_us: Sequence[int],
) -> str:
    if not isinstance(lesson, MinedPlayableLessonV1):
        raise TypeError("assessment replay requires a typed playable lesson")
    if not playback_times_us:
        raise ValueError("assessment replay requires at least one playback cut")
    if any(type(value) is not int for value in playback_times_us):
        raise TypeError("assessment replay cuts must be exact integers")
    if tuple(playback_times_us) != tuple(sorted(playback_times_us)):
        raise ValueError("assessment replay cuts must be monotonic")
    return sha256_json(
        {
            "lesson_digest": lesson.lesson_digest,
            "presentations": [
                lesson.assessment_at(value).as_dict() for value in playback_times_us
            ],
            "record_kind": "MINED_LESSON_ASSESSMENT_REPLAY_V1",
        }
    )


@dataclass(frozen=True, slots=True)
class MinedLessonPlayerActionV1:
    sequence: int
    playback_elapsed_us: int
    action_kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("player-overlay action sequence must be positive")
        if type(self.playback_elapsed_us) is not int or self.playback_elapsed_us < 0:
            raise ValueError("player-overlay action time must be nonnegative")
        if type(self.action_kind) is not str or _ACTION_KIND.fullmatch(self.action_kind) is None:
            raise ValueError("player-overlay action kind is invalid")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("player-overlay action payload must be an object")
        object.__setattr__(self, "payload", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "action_kind": self.action_kind,
            "payload": thaw_json(self.payload),
            "playback_elapsed_us": self.playback_elapsed_us,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class MinedLessonPlayerOverlayV1:
    parent_lesson_id: str
    parent_lesson_digest: str
    parent_source_record_sha256: str
    parent_source_envelope_sha256: str
    actions: tuple[MinedLessonPlayerActionV1, ...]
    provenance: str = PLAYER_OVERLAY_PROVENANCE_V1
    source_authoritative: bool = True
    schema_version: int = PLAYABLE_LESSON_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if self.parent_lesson_id != f"mined-lesson-{self.parent_lesson_digest}":
            raise ValueError("player overlay parent lesson ID and digest disagree")
        for value, label in (
            (self.parent_lesson_digest, "player-overlay lesson digest"),
            (self.parent_source_record_sha256, "player-overlay source-record digest"),
            (
                self.parent_source_envelope_sha256,
                "player-overlay source-envelope digest",
            ),
        ):
            _require_sha256(value, label)
        if type(self.actions) is not tuple or not self.actions:
            raise ValueError("player overlay requires at least one action")
        if any(not isinstance(action, MinedLessonPlayerActionV1) for action in self.actions):
            raise TypeError("player overlay contains an untyped action")
        sequences = tuple(action.sequence for action in self.actions)
        times = tuple(action.playback_elapsed_us for action in self.actions)
        if sequences != tuple(range(1, len(self.actions) + 1)):
            raise ValueError("player-overlay actions must have contiguous sequence")
        if times != tuple(sorted(times)):
            raise ValueError("player-overlay action times must be monotonic")
        if self.provenance != PLAYER_OVERLAY_PROVENANCE_V1:
            raise ValueError("player-overlay provenance label is invalid")
        if self.source_authoritative is not True:
            raise ValueError("player overlay cannot replace authoritative source history")
        if self.schema_version != PLAYABLE_LESSON_SCHEMA_VERSION_V1:
            raise ValueError("player-overlay schema version is unsupported")

    def as_dict(self) -> dict[str, object]:
        return {
            "actions": [action.as_dict() for action in self.actions],
            "parent_lesson_digest": self.parent_lesson_digest,
            "parent_lesson_id": self.parent_lesson_id,
            "parent_source_envelope_sha256": (
                self.parent_source_envelope_sha256
            ),
            "parent_source_record_sha256": self.parent_source_record_sha256,
            "provenance": self.provenance,
            "record_kind": "MINED_LESSON_PLAYER_OVERLAY_V1",
            "schema_version": self.schema_version,
            "source_authoritative": self.source_authoritative,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


def build_player_overlay_v1(
    lesson: MinedPlayableLessonV1,
    actions: tuple[MinedLessonPlayerActionV1, ...],
) -> MinedLessonPlayerOverlayV1:
    if not isinstance(lesson, MinedPlayableLessonV1):
        raise TypeError("player overlay requires a typed playable lesson")
    if type(actions) is not tuple or any(
        not isinstance(action, MinedLessonPlayerActionV1) for action in actions
    ):
        raise TypeError("player overlay actions must be a typed tuple")
    source_before = lesson.source.envelope.sha256
    if any(action.playback_elapsed_us >= lesson.duration_us for action in actions):
        raise ValueError("player-overlay action lies outside the half-open lesson")
    overlay = MinedLessonPlayerOverlayV1(
        parent_lesson_id=lesson.lesson_id,
        parent_lesson_digest=lesson.lesson_digest,
        parent_source_record_sha256=lesson.source.source_record.sha256,
        parent_source_envelope_sha256=source_before,
        actions=actions,
    )
    if lesson.source.envelope.sha256 != source_before:
        raise RuntimeError("player overlay mutated authoritative mined source")
    return overlay


def replay_player_overlay_v1(
    lesson: MinedPlayableLessonV1,
    overlay: MinedLessonPlayerOverlayV1,
) -> MinedLessonPlayerOverlayV1:
    if not isinstance(lesson, MinedPlayableLessonV1):
        raise TypeError("player-overlay replay requires a typed playable lesson")
    if not isinstance(overlay, MinedLessonPlayerOverlayV1) or (
        overlay.parent_lesson_id != lesson.lesson_id
        or overlay.parent_lesson_digest != lesson.lesson_digest
        or overlay.parent_source_record_sha256 != lesson.source.source_record.sha256
        or overlay.parent_source_envelope_sha256 != lesson.source.envelope.sha256
    ):
        raise ValueError("player overlay is not bound to this lesson source")
    replayed = build_player_overlay_v1(lesson, overlay.actions)
    if replayed.as_dict() != overlay.as_dict():
        raise RuntimeError("player overlay replay diverged")
    return replayed


__all__ = [
    "PLAYABLE_ASSESSMENT_POLICY_ID_V1",
    "PLAYABLE_LESSON_SCHEMA_VERSION_V1",
    "PLAYABLE_REVEAL_POLICY_ID_V1",
    "PLAYER_OVERLAY_PROVENANCE_V1",
    "MinedLessonAssessmentV1",
    "MinedLessonPlayerActionV1",
    "MinedLessonPlayerOverlayV1",
    "MinedLessonRevealGrantV1",
    "MinedLessonRevealPayloadV1",
    "MinedPlayableLessonV1",
    "assessment_replay_sha256_v1",
    "build_playable_lesson_v1",
    "build_player_overlay_v1",
    "replay_player_overlay_v1",
]
