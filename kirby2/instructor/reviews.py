"""Immutable instructor review sidecars for exact simulation evidence.

Reviews never edit an assignment attempt, replay, causal trace, or rubric result.
They append a content-addressed ``ReviewAnnotation`` revision whose payload binds
those source artifacts by exact identifier and canonical SHA-256 digest.

The public operation functions are deliberately functional: every call returns a
new ``ReviewRevisionV1`` and leaves the supplied revision unchanged.  No operation
consults a wall clock or infers evidence that was not explicitly supplied.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import ClassVar, Iterable, Mapping

from .models import (
    ReviewAnnotation,
    create_review_annotation_revision,
    require_instructor_profile_id,
)


REVIEW_SIDECAR_SCHEMA_ID = "KIRBY2_INSTRUCTOR_REVIEW_SIDECAR_V1"
REVIEW_SIDECAR_SCHEMA_VERSION = 1


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class ReviewOperationKindV1(str, Enum):
    """Closed vocabulary for every durable instructor-review operation."""

    OPEN_ATTEMPT = "OPEN_ATTEMPT"
    REPLAY_ATTEMPT = "REPLAY_ATTEMPT"
    INSPECT_CAUSAL_TRACE = "INSPECT_CAUSAL_TRACE"
    COMPARE_ATTEMPTS = "COMPARE_ATTEMPTS"
    ANNOTATE_TIMELINE = "ANNOTATE_TIMELINE"
    TAG_ERRORS = "TAG_ERRORS"
    WRITE_FEEDBACK = "WRITE_FEEDBACK"
    MARK_COMPLETE = "MARK_COMPLETE"
    ATTACH_RUBRIC_RESULT = "ATTACH_RUBRIC_RESULT"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"{label} must be an exact string-keyed object")
    return value


def _exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} keys differ: missing={missing}, extra={extra}")


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one nonempty canonical identifier")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _typed_tuple(value: object, item_type: type, label: str) -> tuple:
    if type(value) is not tuple or any(type(item) is not item_type for item in value):
        raise TypeError(f"{label} must be an immutable typed tuple")
    return value


def _canonical_unique_tuple(value: tuple, label: str) -> tuple:
    canonical = tuple(sorted(set(value), key=lambda item: item.canonical_bytes()))
    if canonical != value:
        raise ValueError(f"{label} must be unique and canonically ordered")
    return value


def _merge_canonical(current: tuple, additions: Iterable[object]) -> tuple:
    return tuple(
        sorted(
            set((*current, *tuple(additions))),
            key=lambda item: item.canonical_bytes(),
        )
    )


def _json_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a JSON array")
    return value


@dataclass(frozen=True, slots=True)
class AttemptReviewBindingV1:
    """Exact immutable source binding for an attempt, replay, and causal trace."""

    attempt_id: str
    attempt_sha256: str
    replay_id: str
    replay_sha256: str
    causal_trace_id: str
    causal_trace_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "assignment attempt ID")
        _sha256(self.attempt_sha256, "assignment attempt digest")
        _identifier(self.replay_id, "replay ID")
        _sha256(self.replay_sha256, "replay digest")
        _identifier(self.causal_trace_id, "causal trace ID")
        _sha256(self.causal_trace_sha256, "causal trace digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_sha256": self.attempt_sha256,
            "causal_trace_id": self.causal_trace_id,
            "causal_trace_sha256": self.causal_trace_sha256,
            "replay_id": self.replay_id,
            "replay_sha256": self.replay_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> AttemptReviewBindingV1:
        value = _mapping(raw, "attempt review binding")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "attempt review binding")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RubricItemReferenceV1:
    """One rubric item pinned to an exact rubric revision digest."""

    rubric_id: str
    rubric_sha256: str
    item_id: str

    def __post_init__(self) -> None:
        _identifier(self.rubric_id, "rubric ID")
        _sha256(self.rubric_sha256, "rubric digest")
        _identifier(self.item_id, "rubric item ID")

    def as_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "rubric_id": self.rubric_id,
            "rubric_sha256": self.rubric_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> RubricItemReferenceV1:
        value = _mapping(raw, "rubric item reference")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "rubric item reference")
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class EvidenceEventReferenceV1:
    """Exact evidence artifact plus the event IDs cited within that artifact."""

    evidence_id: str
    evidence_sha256: str
    event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "evidence ID")
        _sha256(self.evidence_sha256, "evidence digest")
        if type(self.event_ids) is not tuple or not self.event_ids:
            raise TypeError("evidence event IDs must be a nonempty immutable tuple")
        for event_id in self.event_ids:
            _identifier(event_id, "evidence event ID")
        if tuple(sorted(set(self.event_ids))) != self.event_ids:
            raise ValueError("evidence event IDs must be unique and canonically ordered")

    def as_dict(self) -> dict[str, object]:
        return {
            "event_ids": list(self.event_ids),
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> EvidenceEventReferenceV1:
        value = _mapping(raw, "evidence event reference")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "evidence event reference")
        return cls(
            evidence_id=value["evidence_id"],  # type: ignore[arg-type]
            evidence_sha256=value["evidence_sha256"],  # type: ignore[arg-type]
            event_ids=tuple(_json_list(value["event_ids"], "evidence event IDs")),
        )


def _parse_rubric_items(raw: object, label: str) -> tuple[RubricItemReferenceV1, ...]:
    return tuple(RubricItemReferenceV1.from_dict(item) for item in _json_list(raw, label))


def _parse_evidence(raw: object, label: str) -> tuple[EvidenceEventReferenceV1, ...]:
    return tuple(EvidenceEventReferenceV1.from_dict(item) for item in _json_list(raw, label))


@dataclass(frozen=True, slots=True)
class TimelineAnnotationV1:
    """Instructor text anchored to an exact replay time and cited evidence."""

    replay_time_us: int
    body: str
    rubric_items: tuple[RubricItemReferenceV1, ...] = ()
    evidence: tuple[EvidenceEventReferenceV1, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative_int(self.replay_time_us, "timeline replay time")
        _text(self.body, "timeline annotation body")
        _canonical_unique_tuple(
            _typed_tuple(self.rubric_items, RubricItemReferenceV1, "timeline rubric items"),
            "timeline rubric items",
        )
        _canonical_unique_tuple(
            _typed_tuple(self.evidence, EvidenceEventReferenceV1, "timeline evidence"),
            "timeline evidence",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "body": self.body,
            "evidence": [item.as_dict() for item in self.evidence],
            "replay_time_us": self.replay_time_us,
            "rubric_items": [item.as_dict() for item in self.rubric_items],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def annotation_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> TimelineAnnotationV1:
        value = _mapping(raw, "timeline annotation")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "timeline annotation")
        return cls(
            replay_time_us=value["replay_time_us"],  # type: ignore[arg-type]
            body=value["body"],  # type: ignore[arg-type]
            rubric_items=_parse_rubric_items(value["rubric_items"], "timeline rubric items"),
            evidence=_parse_evidence(value["evidence"], "timeline evidence"),
        )


@dataclass(frozen=True, slots=True)
class ErrorTagV1:
    """One instructor-assigned error tag with explicit evidence provenance."""

    tag: str
    detail: str
    rubric_items: tuple[RubricItemReferenceV1, ...] = ()
    evidence: tuple[EvidenceEventReferenceV1, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.tag, "error tag")
        _text(self.detail, "error tag detail")
        _canonical_unique_tuple(
            _typed_tuple(self.rubric_items, RubricItemReferenceV1, "error tag rubric items"),
            "error tag rubric items",
        )
        _canonical_unique_tuple(
            _typed_tuple(self.evidence, EvidenceEventReferenceV1, "error tag evidence"),
            "error tag evidence",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": [item.as_dict() for item in self.evidence],
            "rubric_items": [item.as_dict() for item in self.rubric_items],
            "tag": self.tag,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> ErrorTagV1:
        value = _mapping(raw, "error tag")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "error tag")
        return cls(
            tag=value["tag"],  # type: ignore[arg-type]
            detail=value["detail"],  # type: ignore[arg-type]
            rubric_items=_parse_rubric_items(value["rubric_items"], "error tag rubric items"),
            evidence=_parse_evidence(value["evidence"], "error tag evidence"),
        )


@dataclass(frozen=True, slots=True)
class FeedbackEntryV1:
    """Immutable instructor feedback with exact rubric and evidence citations."""

    feedback_id: str
    body: str
    rubric_items: tuple[RubricItemReferenceV1, ...] = ()
    evidence: tuple[EvidenceEventReferenceV1, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.feedback_id, "feedback ID")
        _text(self.body, "feedback body")
        _canonical_unique_tuple(
            _typed_tuple(self.rubric_items, RubricItemReferenceV1, "feedback rubric items"),
            "feedback rubric items",
        )
        _canonical_unique_tuple(
            _typed_tuple(self.evidence, EvidenceEventReferenceV1, "feedback evidence"),
            "feedback evidence",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "body": self.body,
            "evidence": [item.as_dict() for item in self.evidence],
            "feedback_id": self.feedback_id,
            "rubric_items": [item.as_dict() for item in self.rubric_items],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> FeedbackEntryV1:
        value = _mapping(raw, "feedback entry")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "feedback entry")
        return cls(
            feedback_id=value["feedback_id"],  # type: ignore[arg-type]
            body=value["body"],  # type: ignore[arg-type]
            rubric_items=_parse_rubric_items(value["rubric_items"], "feedback rubric items"),
            evidence=_parse_evidence(value["evidence"], "feedback evidence"),
        )


@dataclass(frozen=True, slots=True)
class RubricResultBindingV1:
    """A derived score sidecar pinned to the exact rubric version it used."""

    result_id: str
    result_sha256: str
    assignment_attempt_id: str
    assignment_attempt_sha256: str
    rubric_id: str
    rubric_sha256: str
    rubric_items: tuple[RubricItemReferenceV1, ...]

    def __post_init__(self) -> None:
        _identifier(self.result_id, "rubric result ID")
        _sha256(self.result_sha256, "rubric result digest")
        _identifier(self.assignment_attempt_id, "rubric result attempt ID")
        _sha256(
            self.assignment_attempt_sha256,
            "rubric result attempt digest",
        )
        _identifier(self.rubric_id, "rubric result rubric ID")
        _sha256(self.rubric_sha256, "rubric result rubric digest")
        if type(self.rubric_items) is not tuple or not self.rubric_items:
            raise TypeError("rubric result items must be a nonempty immutable tuple")
        _canonical_unique_tuple(
            _typed_tuple(self.rubric_items, RubricItemReferenceV1, "rubric result items"),
            "rubric result items",
        )
        if any(
            item.rubric_id != self.rubric_id
            or item.rubric_sha256 != self.rubric_sha256
            for item in self.rubric_items
        ):
            raise ValueError("rubric result items changed the bound rubric version")

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_attempt_id": self.assignment_attempt_id,
            "assignment_attempt_sha256": self.assignment_attempt_sha256,
            "result_id": self.result_id,
            "result_sha256": self.result_sha256,
            "rubric_id": self.rubric_id,
            "rubric_items": [item.as_dict() for item in self.rubric_items],
            "rubric_sha256": self.rubric_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> RubricResultBindingV1:
        value = _mapping(raw, "rubric result binding")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "rubric result binding")
        return cls(
            result_id=value["result_id"],  # type: ignore[arg-type]
            result_sha256=value["result_sha256"],  # type: ignore[arg-type]
            assignment_attempt_id=value["assignment_attempt_id"],  # type: ignore[arg-type]
            assignment_attempt_sha256=value["assignment_attempt_sha256"],  # type: ignore[arg-type]
            rubric_id=value["rubric_id"],  # type: ignore[arg-type]
            rubric_sha256=value["rubric_sha256"],  # type: ignore[arg-type]
            rubric_items=_parse_rubric_items(value["rubric_items"], "rubric result items"),
        )


@dataclass(frozen=True, slots=True)
class ReviewOperationReceiptV1:
    """Ordered receipt committing one semantic review operation's payload."""

    sequence: int
    operation: ReviewOperationKindV1
    payload_sha256: str

    def __post_init__(self) -> None:
        _positive_int(self.sequence, "review operation sequence")
        if type(self.operation) is not ReviewOperationKindV1:
            raise TypeError("review operation must use ReviewOperationKindV1")
        _sha256(self.payload_sha256, "review operation payload digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "payload_sha256": self.payload_sha256,
            "sequence": self.sequence,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, raw: object) -> ReviewOperationReceiptV1:
        value = _mapping(raw, "review operation receipt")
        _exact_keys(value, frozenset(cls.__dataclass_fields__), "review operation receipt")
        try:
            operation = ReviewOperationKindV1(value["operation"])
        except (TypeError, ValueError) as error:
            raise ValueError("review operation kind is invalid") from error
        return cls(
            sequence=value["sequence"],  # type: ignore[arg-type]
            operation=operation,
            payload_sha256=value["payload_sha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ReviewSidecarV1:
    """Complete immutable state of one instructor review revision."""

    reviewer_profile_id: str
    attempt: AttemptReviewBindingV1
    compared_attempts: tuple[AttemptReviewBindingV1, ...] = ()
    rubric_item_references: tuple[RubricItemReferenceV1, ...] = ()
    evidence_event_references: tuple[EvidenceEventReferenceV1, ...] = ()
    timeline_annotations: tuple[TimelineAnnotationV1, ...] = ()
    error_tags: tuple[ErrorTagV1, ...] = ()
    feedback: tuple[FeedbackEntryV1, ...] = ()
    rubric_result: RubricResultBindingV1 | None = None
    operations: tuple[ReviewOperationReceiptV1, ...] = ()
    completed: bool = False

    _OPERATION_COUNTS: ClassVar[dict[ReviewOperationKindV1, str]] = {
        ReviewOperationKindV1.COMPARE_ATTEMPTS: "compared_attempts",
        ReviewOperationKindV1.ANNOTATE_TIMELINE: "timeline_annotations",
        ReviewOperationKindV1.TAG_ERRORS: "error_tags",
        ReviewOperationKindV1.WRITE_FEEDBACK: "feedback",
    }

    def __post_init__(self) -> None:
        require_instructor_profile_id(self.reviewer_profile_id)
        if type(self.attempt) is not AttemptReviewBindingV1:
            raise TypeError("review attempt must use AttemptReviewBindingV1")
        _canonical_unique_tuple(
            _typed_tuple(self.compared_attempts, AttemptReviewBindingV1, "compared attempts"),
            "compared attempts",
        )
        if any(item.attempt_id == self.attempt.attempt_id for item in self.compared_attempts):
            raise ValueError("a review cannot compare its subject attempt to itself")
        compared_ids = tuple(item.attempt_id for item in self.compared_attempts)
        if len(compared_ids) != len(set(compared_ids)):
            raise ValueError("comparison attempt IDs must be unique")
        _canonical_unique_tuple(
            _typed_tuple(
                self.rubric_item_references,
                RubricItemReferenceV1,
                "review rubric item references",
            ),
            "review rubric item references",
        )
        rubric_item_keys = tuple(
            (item.rubric_id, item.item_id) for item in self.rubric_item_references
        )
        if len(rubric_item_keys) != len(set(rubric_item_keys)):
            raise ValueError(
                "one rubric record/item pair cannot carry conflicting digests"
            )
        _canonical_unique_tuple(
            _typed_tuple(
                self.evidence_event_references,
                EvidenceEventReferenceV1,
                "review evidence references",
            ),
            "review evidence references",
        )
        evidence_ids = tuple(
            item.evidence_id for item in self.evidence_event_references
        )
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("one evidence ID cannot carry conflicting references")
        _typed_tuple(self.timeline_annotations, TimelineAnnotationV1, "timeline annotations")
        _typed_tuple(self.error_tags, ErrorTagV1, "error tags")
        _typed_tuple(self.feedback, FeedbackEntryV1, "feedback entries")
        if len(self.timeline_annotations) != len(set(self.timeline_annotations)):
            raise ValueError("timeline annotations must be unique")
        if len(self.error_tags) != len(set(self.error_tags)):
            raise ValueError("error tags must be unique")
        feedback_ids = tuple(item.feedback_id for item in self.feedback)
        if len(feedback_ids) != len(set(feedback_ids)):
            raise ValueError("feedback IDs must be unique")
        if self.rubric_result is not None and type(self.rubric_result) is not RubricResultBindingV1:
            raise TypeError("rubric result must use RubricResultBindingV1")
        if self.rubric_result is not None and (
            self.rubric_result.assignment_attempt_id != self.attempt.attempt_id
            or self.rubric_result.assignment_attempt_sha256
            != self.attempt.attempt_sha256
        ):
            raise ValueError("rubric result changed the reviewed attempt binding")
        _typed_tuple(self.operations, ReviewOperationReceiptV1, "review operations")
        if type(self.completed) is not bool:
            raise TypeError("review completion must be an exact boolean")
        self._validate_operation_log()
        self._validate_reference_index()

    def _validate_operation_log(self) -> None:
        if any(item.sequence != index for index, item in enumerate(self.operations, start=1)):
            raise ValueError("review operation sequence must be contiguous from one")
        kinds = tuple(item.operation for item in self.operations)
        open_count = kinds.count(ReviewOperationKindV1.OPEN_ATTEMPT)
        if self.operations:
            if kinds[0] is not ReviewOperationKindV1.OPEN_ATTEMPT or open_count != 1:
                raise ValueError("an active review must open its attempt exactly once first")
        elif self.completed:
            raise ValueError("an unopened review cannot be complete")
        complete_count = kinds.count(ReviewOperationKindV1.MARK_COMPLETE)
        if self.completed:
            if complete_count != 1 or kinds[-1] is not ReviewOperationKindV1.MARK_COMPLETE:
                raise ValueError("a complete review must end with one completion operation")
        elif complete_count:
            raise ValueError("an incomplete review cannot contain a completion operation")
        for operation, field_name in self._OPERATION_COUNTS.items():
            if kinds.count(operation) != len(getattr(self, field_name)):
                raise ValueError(f"{operation.value} receipts do not match review content")
        attachment_count = kinds.count(ReviewOperationKindV1.ATTACH_RUBRIC_RESULT)
        if self.rubric_result is None and attachment_count:
            raise ValueError("rubric-result receipts require a current result binding")
        if self.rubric_result is not None and not attachment_count:
            raise ValueError("a rubric result requires an attachment receipt")

    def _validate_reference_index(self) -> None:
        rubric_index = set(self.rubric_item_references)
        evidence_index = set(self.evidence_event_references)
        referenced: list[object] = [
            *self.timeline_annotations,
            *self.error_tags,
            *self.feedback,
        ]
        if self.rubric_result is not None:
            referenced.append(self.rubric_result)
        for item in referenced:
            if not set(item.rubric_items).issubset(rubric_index):
                raise ValueError("review content cites an unindexed rubric item")
            nested_evidence = getattr(item, "evidence", ())
            if not set(nested_evidence).issubset(evidence_index):
                raise ValueError("review content cites unindexed evidence")

    @property
    def opened(self) -> bool:
        return bool(self.operations)

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt.as_dict(),
            "compared_attempts": [item.as_dict() for item in self.compared_attempts],
            "completed": self.completed,
            "error_tags": [item.as_dict() for item in self.error_tags],
            "evidence_event_references": [
                item.as_dict() for item in self.evidence_event_references
            ],
            "feedback": [item.as_dict() for item in self.feedback],
            "operations": [item.as_dict() for item in self.operations],
            "reviewer_profile_id": self.reviewer_profile_id,
            "rubric_item_references": [
                item.as_dict() for item in self.rubric_item_references
            ],
            "rubric_result": (
                None if self.rubric_result is None else self.rubric_result.as_dict()
            ),
            "timeline_annotations": [
                item.as_dict() for item in self.timeline_annotations
            ],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ReviewSidecarV1:
        value = _mapping(raw, "review sidecar")
        expected = frozenset(field for field in cls.__dataclass_fields__ if not field.startswith("_"))
        _exact_keys(value, expected, "review sidecar")
        raw_result = value["rubric_result"]
        if raw_result is not None and not isinstance(raw_result, Mapping):
            raise TypeError("rubric result must be null or an object")
        if type(value["completed"]) is not bool:
            raise TypeError("review completion must be an exact boolean")
        return cls(
            reviewer_profile_id=value["reviewer_profile_id"],  # type: ignore[arg-type]
            attempt=AttemptReviewBindingV1.from_dict(value["attempt"]),
            compared_attempts=tuple(
                AttemptReviewBindingV1.from_dict(item)
                for item in _json_list(value["compared_attempts"], "compared attempts")
            ),
            rubric_item_references=_parse_rubric_items(
                value["rubric_item_references"], "review rubric item references"
            ),
            evidence_event_references=_parse_evidence(
                value["evidence_event_references"], "review evidence references"
            ),
            timeline_annotations=tuple(
                TimelineAnnotationV1.from_dict(item)
                for item in _json_list(value["timeline_annotations"], "timeline annotations")
            ),
            error_tags=tuple(
                ErrorTagV1.from_dict(item)
                for item in _json_list(value["error_tags"], "error tags")
            ),
            feedback=tuple(
                FeedbackEntryV1.from_dict(item)
                for item in _json_list(value["feedback"], "feedback entries")
            ),
            rubric_result=(
                None if raw_result is None else RubricResultBindingV1.from_dict(raw_result)
            ),
            operations=tuple(
                ReviewOperationReceiptV1.from_dict(item)
                for item in _json_list(value["operations"], "review operations")
            ),
            completed=value["completed"],
        )


@dataclass(frozen=True, slots=True)
class ReviewRevisionV1:
    """Standalone, exactly reloadable review payload plus envelope lineage."""

    sidecar: ReviewSidecarV1
    lineage: tuple[ReviewAnnotation, ...]
    schema_id: str = REVIEW_SIDECAR_SCHEMA_ID
    schema_version: int = REVIEW_SIDECAR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.sidecar) is not ReviewSidecarV1:
            raise TypeError("review revision sidecar must use ReviewSidecarV1")
        if type(self.lineage) is not tuple or not self.lineage or any(
            type(item) is not ReviewAnnotation for item in self.lineage
        ):
            raise TypeError("review lineage must be a nonempty ReviewAnnotation tuple")
        if type(self.schema_id) is not str or self.schema_id != REVIEW_SIDECAR_SCHEMA_ID:
            raise ValueError("review sidecar schema ID changed")
        if type(self.schema_version) is not int or self.schema_version != REVIEW_SIDECAR_SCHEMA_VERSION:
            raise ValueError("review sidecar schema version changed")
        if self.lineage[-1].revision != len(self.lineage):
            raise ValueError("review lineage length differs from current revision")
        for predecessor, successor in zip(self.lineage, self.lineage[1:]):
            predecessor.validate_successor(successor)
        if self.envelope.content_sha256 != self.sidecar.sha256:
            raise ValueError("review envelope does not commit to its sidecar")

    @property
    def envelope(self) -> ReviewAnnotation:
        return self.lineage[-1]

    @property
    def review_annotation(self) -> ReviewAnnotation:
        return self.envelope

    @property
    def annotation(self) -> ReviewAnnotation:
        return self.envelope

    @property
    def review_id(self) -> str:
        return self.envelope.review_annotation_id

    @property
    def review_annotation_id(self) -> str:
        return self.envelope.review_annotation_id

    @property
    def lineage_id(self) -> str:
        return self.envelope.lineage_id

    @property
    def revision(self) -> int:
        return self.envelope.revision

    @property
    def content_sha256(self) -> str:
        return self.envelope.content_sha256

    @property
    def attempt_id(self) -> str:
        return self.sidecar.attempt.attempt_id

    @property
    def reviewer_profile_id(self) -> str:
        return self.sidecar.reviewer_profile_id

    def as_dict(self) -> dict[str, object]:
        return {
            "lineage": [item.as_dict() for item in self.lineage],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sidecar": self.sidecar.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ReviewRevisionV1:
        value = _mapping(raw, "review revision")
        _exact_keys(
            value,
            frozenset({"lineage", "schema_id", "schema_version", "sidecar"}),
            "review revision",
        )
        raw_lineage = _json_list(value["lineage"], "review lineage")
        if not raw_lineage:
            raise ValueError("review lineage cannot be empty")
        rebuilt: list[ReviewAnnotation] = []
        predecessor: ReviewAnnotation | None = None
        for index, raw_envelope in enumerate(raw_lineage, start=1):
            envelope_dict = _mapping(raw_envelope, f"review lineage envelope {index}")
            _exact_keys(
                envelope_dict,
                frozenset(
                    {
                        "content_sha256",
                        "lineage_id",
                        "predecessor_record_id",
                        "predecessor_sha256",
                        "record_id",
                        "record_kind",
                        "revision",
                        "schema_id",
                        "schema_version",
                    }
                ),
                f"review lineage envelope {index}",
            )
            content_sha256 = _sha256(
                envelope_dict["content_sha256"],
                f"review lineage envelope {index} content digest",
            )
            current = create_review_annotation_revision(
                content_sha256,
                predecessor=predecessor,
            )
            if current.as_dict() != dict(envelope_dict):
                raise ValueError(f"review lineage envelope {index} is not canonical")
            rebuilt.append(current)
            predecessor = current
        return cls(
            sidecar=ReviewSidecarV1.from_dict(value["sidecar"]),
            lineage=tuple(rebuilt),
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ReviewRevisionV1:
        if type(raw) is not bytes:
            raise TypeError("review revision decoder requires exact bytes")
        try:
            value = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("review revision must be canonical ASCII JSON") from error
        if not isinstance(value, dict) or _canonical_json_bytes(value) != raw:
            raise ValueError("review revision must be one canonical JSON object")
        revision = cls.from_dict(value)
        if revision.canonical_bytes() != raw:
            raise ValueError("review revision bytes differ after exact reconstruction")
        return revision


def _receipt(
    sidecar: ReviewSidecarV1,
    operation: ReviewOperationKindV1,
    payload: object,
) -> ReviewOperationReceiptV1:
    return ReviewOperationReceiptV1(
        sequence=len(sidecar.operations) + 1,
        operation=operation,
        payload_sha256=_canonical_sha256(payload),
    )


def _successor(review: ReviewRevisionV1, sidecar: ReviewSidecarV1) -> ReviewRevisionV1:
    if type(review) is not ReviewRevisionV1:
        raise TypeError("review operation requires ReviewRevisionV1")
    if type(sidecar) is not ReviewSidecarV1:
        raise TypeError("review successor sidecar must use ReviewSidecarV1")
    envelope = create_review_annotation_revision(
        sidecar.sha256,
        predecessor=review.envelope,
    )
    return ReviewRevisionV1(sidecar=sidecar, lineage=(*review.lineage, envelope))


def _require_open_active(review: ReviewRevisionV1) -> None:
    if type(review) is not ReviewRevisionV1:
        raise TypeError("review operation requires ReviewRevisionV1")
    if not review.sidecar.opened:
        raise ValueError("review attempt must be opened before this operation")
    if review.sidecar.completed:
        raise ValueError("a completed review cannot accept more operations")


def create_review(
    *,
    reviewer_profile_id: str,
    attempt: AttemptReviewBindingV1,
) -> ReviewRevisionV1:
    """Create an unopened review sidecar without touching its source attempt."""

    sidecar = ReviewSidecarV1(
        reviewer_profile_id=reviewer_profile_id,
        attempt=attempt,
    )
    envelope = create_review_annotation_revision(sidecar.sha256)
    return ReviewRevisionV1(sidecar=sidecar, lineage=(envelope,))


def open_attempt(review: ReviewRevisionV1) -> ReviewRevisionV1:
    """Record that the reviewer opened the exact bound attempt."""

    if type(review) is not ReviewRevisionV1:
        raise TypeError("open attempt requires ReviewRevisionV1")
    if review.sidecar.opened:
        raise ValueError("review attempt is already open")
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.OPEN_ATTEMPT,
        {
            "attempt_id": review.sidecar.attempt.attempt_id,
            "attempt_sha256": review.sidecar.attempt.attempt_sha256,
        },
    )
    return _successor(
        review,
        replace(review.sidecar, operations=(receipt,)),
    )


def replay_attempt(review: ReviewRevisionV1) -> ReviewRevisionV1:
    """Record replay inspection against the review's exact replay binding."""

    _require_open_active(review)
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.REPLAY_ATTEMPT,
        {
            "replay_id": review.sidecar.attempt.replay_id,
            "replay_sha256": review.sidecar.attempt.replay_sha256,
        },
    )
    return _successor(
        review,
        replace(review.sidecar, operations=(*review.sidecar.operations, receipt)),
    )


def inspect_causal_trace(review: ReviewRevisionV1) -> ReviewRevisionV1:
    """Record inspection of the exact causal trace bound at review creation."""

    _require_open_active(review)
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.INSPECT_CAUSAL_TRACE,
        {
            "causal_trace_id": review.sidecar.attempt.causal_trace_id,
            "causal_trace_sha256": review.sidecar.attempt.causal_trace_sha256,
        },
    )
    return _successor(
        review,
        replace(review.sidecar, operations=(*review.sidecar.operations, receipt)),
    )


def compare_attempt(
    review: ReviewRevisionV1,
    compared_attempt: AttemptReviewBindingV1,
) -> ReviewRevisionV1:
    """Attach one exact comparison attempt without altering either source run."""

    _require_open_active(review)
    if type(compared_attempt) is not AttemptReviewBindingV1:
        raise TypeError("comparison requires AttemptReviewBindingV1")
    if compared_attempt.attempt_id == review.sidecar.attempt.attempt_id:
        raise ValueError("a review cannot compare its subject attempt to itself")
    if compared_attempt in review.sidecar.compared_attempts:
        raise ValueError("comparison attempt is already attached")
    comparisons = _merge_canonical(review.sidecar.compared_attempts, (compared_attempt,))
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.COMPARE_ATTEMPTS,
        compared_attempt.as_dict(),
    )
    return _successor(
        review,
        replace(
            review.sidecar,
            compared_attempts=comparisons,
            operations=(*review.sidecar.operations, receipt),
        ),
    )


def annotate_timeline(
    review: ReviewRevisionV1,
    annotation: TimelineAnnotationV1,
) -> ReviewRevisionV1:
    """Append a replay-time annotation and index all of its exact citations."""

    _require_open_active(review)
    if type(annotation) is not TimelineAnnotationV1:
        raise TypeError("timeline annotation requires TimelineAnnotationV1")
    if annotation in review.sidecar.timeline_annotations:
        raise ValueError("timeline annotation is already attached")
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.ANNOTATE_TIMELINE,
        annotation.as_dict(),
    )
    return _successor(
        review,
        replace(
            review.sidecar,
            rubric_item_references=_merge_canonical(
                review.sidecar.rubric_item_references,
                annotation.rubric_items,
            ),
            evidence_event_references=_merge_canonical(
                review.sidecar.evidence_event_references,
                annotation.evidence,
            ),
            timeline_annotations=(*review.sidecar.timeline_annotations, annotation),
            operations=(*review.sidecar.operations, receipt),
        ),
    )


def tag_error(review: ReviewRevisionV1, error_tag: ErrorTagV1) -> ReviewRevisionV1:
    """Append an error tag and index its exact rubric/evidence citations."""

    _require_open_active(review)
    if type(error_tag) is not ErrorTagV1:
        raise TypeError("error tagging requires ErrorTagV1")
    if error_tag in review.sidecar.error_tags:
        raise ValueError("error tag is already attached")
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.TAG_ERRORS,
        error_tag.as_dict(),
    )
    return _successor(
        review,
        replace(
            review.sidecar,
            rubric_item_references=_merge_canonical(
                review.sidecar.rubric_item_references,
                error_tag.rubric_items,
            ),
            evidence_event_references=_merge_canonical(
                review.sidecar.evidence_event_references,
                error_tag.evidence,
            ),
            error_tags=(*review.sidecar.error_tags, error_tag),
            operations=(*review.sidecar.operations, receipt),
        ),
    )


def write_feedback(
    review: ReviewRevisionV1,
    feedback: FeedbackEntryV1,
) -> ReviewRevisionV1:
    """Append written feedback and index its exact rubric/evidence citations."""

    _require_open_active(review)
    if type(feedback) is not FeedbackEntryV1:
        raise TypeError("write feedback requires FeedbackEntryV1")
    if feedback in review.sidecar.feedback:
        raise ValueError("feedback entry is already attached")
    if any(item.feedback_id == feedback.feedback_id for item in review.sidecar.feedback):
        raise ValueError("feedback ID is already used by this review")
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.WRITE_FEEDBACK,
        feedback.as_dict(),
    )
    return _successor(
        review,
        replace(
            review.sidecar,
            rubric_item_references=_merge_canonical(
                review.sidecar.rubric_item_references,
                feedback.rubric_items,
            ),
            evidence_event_references=_merge_canonical(
                review.sidecar.evidence_event_references,
                feedback.evidence,
            ),
            feedback=(*review.sidecar.feedback, feedback),
            operations=(*review.sidecar.operations, receipt),
        ),
    )


def attach_rubric_result(
    review: ReviewRevisionV1,
    result: RubricResultBindingV1,
) -> ReviewRevisionV1:
    """Attach or supersede a derived score binding without editing either score."""

    _require_open_active(review)
    if type(result) is not RubricResultBindingV1:
        raise TypeError("rubric attachment requires RubricResultBindingV1")
    if (
        result.assignment_attempt_id != review.sidecar.attempt.attempt_id
        or result.assignment_attempt_sha256
        != review.sidecar.attempt.attempt_sha256
    ):
        raise ValueError("rubric result is bound to a different assignment attempt")
    if result == review.sidecar.rubric_result:
        raise ValueError("rubric result is already attached")
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.ATTACH_RUBRIC_RESULT,
        result.as_dict(),
    )
    return _successor(
        review,
        replace(
            review.sidecar,
            rubric_item_references=_merge_canonical(
                review.sidecar.rubric_item_references,
                result.rubric_items,
            ),
            rubric_result=result,
            operations=(*review.sidecar.operations, receipt),
        ),
    )


def mark_complete(review: ReviewRevisionV1) -> ReviewRevisionV1:
    """Seal a review against further operations while preserving all source runs."""

    _require_open_active(review)
    receipt = _receipt(
        review.sidecar,
        ReviewOperationKindV1.MARK_COMPLETE,
        {
            "attempt_id": review.sidecar.attempt.attempt_id,
            "review_content_sha256_before_completion": review.sidecar.sha256,
        },
    )
    return _successor(
        review,
        replace(
            review.sidecar,
            operations=(*review.sidecar.operations, receipt),
            completed=True,
        ),
    )


def compare_attempts(
    review: ReviewRevisionV1,
    compared_attempt: AttemptReviewBindingV1,
) -> ReviewRevisionV1:
    """Plural spelling matching the durable ``COMPARE_ATTEMPTS`` operation."""

    return compare_attempt(review, compared_attempt)


def tag_errors(review: ReviewRevisionV1, error_tag: ErrorTagV1) -> ReviewRevisionV1:
    """Plural spelling matching the durable ``TAG_ERRORS`` operation."""

    return tag_error(review, error_tag)


__all__ = [
    "REVIEW_SIDECAR_SCHEMA_ID",
    "REVIEW_SIDECAR_SCHEMA_VERSION",
    "AttemptReviewBindingV1",
    "ErrorTagV1",
    "EvidenceEventReferenceV1",
    "FeedbackEntryV1",
    "ReviewOperationKindV1",
    "ReviewOperationReceiptV1",
    "ReviewRevisionV1",
    "ReviewSidecarV1",
    "RubricItemReferenceV1",
    "RubricResultBindingV1",
    "TimelineAnnotationV1",
    "annotate_timeline",
    "attach_rubric_result",
    "compare_attempt",
    "compare_attempts",
    "create_review",
    "inspect_causal_trace",
    "mark_complete",
    "open_attempt",
    "replay_attempt",
    "tag_error",
    "tag_errors",
    "write_feedback",
]
