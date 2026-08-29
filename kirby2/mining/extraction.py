"""Exact recorded-client-feed extraction for playable mined lessons (WO33-D)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.historical.lesson_models import (
    MINED_HIDDEN_STATE_REVEAL_POLICY_V1,
    RECORDED_CLIENT_FEED_POLICY_V1,
    MinedCheckpointReferenceV1,
    MinedDetectorReferenceV1,
    MinedLessonSourceRecordV1,
    MinedSourceRunReferenceV1,
    MinedSourceTimeBoundsV1,
)
from kirby2.immutable import freeze_json, thaw_json

from .models import LessonCandidateV1, canonical_json_bytes, sha256_json


LESSON_EXTRACTION_SCHEMA_VERSION_V1 = 1
CLIENT_CONTEXT_SNAPSHOT_KIND_V1 = "CLIENT_STATE_SNAPSHOT"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVENT_KIND = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")

# These names are candidate/replay internals, never fields of a recorded client feed.
# The check is recursive so nesting cannot turn an internal into an assessment field.
ASSESSMENT_FORBIDDEN_FEED_KEYS_V1 = frozenset(
    {
        "detector",
        "detector_id",
        "detector_type",
        "detector_version",
        "difficulty",
        "difficulty_ppm",
        "difficulty_projection",
        "future_schedule",
        "ground_truth",
        "ground_truth_summary",
        "hidden_schedule",
        "hidden_state",
        "post_end_us",
        "post_event_boundary",
        "reveal_material",
        "rng_state",
        "root_seed",
        "selection_reason",
        "source_window_outcome",
    }
)


def _require_sha256(value: object, label: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")


def _forbidden_key_paths(value: object, prefix: str = "payload") -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
            child_path = f"{prefix}.{key}"
            if normalized in ASSESSMENT_FORBIDDEN_FEED_KEYS_V1:
                paths.append(child_path)
            paths.extend(_forbidden_key_paths(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            paths.extend(_forbidden_key_paths(child, f"{prefix}[{index}]"))
    return tuple(paths)


@dataclass(frozen=True, slots=True)
class RecordedClientFeedEventV1:
    """One event exactly as released to the recorded client."""

    source_sequence: int
    client_time_us: int
    client_event_id: str
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.source_sequence) is not int or self.source_sequence <= 0:
            raise ValueError("client-feed source sequence must be positive")
        if type(self.client_time_us) is not int or self.client_time_us < 0:
            raise ValueError("client-feed time must be nonnegative microseconds")
        if (
            type(self.client_event_id) is not str
            or not self.client_event_id
            or unicodedata.normalize("NFC", self.client_event_id)
            != self.client_event_id
        ):
            raise ValueError("client-feed event ID must be nonempty NFC text")
        if type(self.kind) is not str or _EVENT_KIND.fullmatch(self.kind) is None:
            raise ValueError("client-feed event kind must be a stable uppercase ID")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("client-feed payload must be an object")
        forbidden = _forbidden_key_paths(frozen)
        if forbidden:
            raise ValueError(
                "client-feed payload contains reveal-only fields: "
                + ",".join(forbidden)
            )
        object.__setattr__(self, "payload", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "client_event_id": self.client_event_id,
            "client_time_us": self.client_time_us,
            "kind": self.kind,
            "payload": thaw_json(self.payload),
            "source_sequence": self.source_sequence,
        }


@dataclass(frozen=True, slots=True)
class RecordedLessonSourceV1:
    """Authoritative replay material supplied to the lesson extractor.

    Hidden generation state lives here, beside rather than inside the client feed.
    All nested JSON is copied into immutable ownership on construction.
    """

    source_run_reference: MinedSourceRunReferenceV1
    source_start_us: int
    source_end_us: int
    checkpoint_reference: MinedCheckpointReferenceV1 | None
    source_ancestry_sha256: str
    parent_source_ancestry_sha256: str | None
    authoritative_event_prefix_sha256: str | None
    observable_feed: tuple[RecordedClientFeedEventV1, ...]
    rng_state: Mapping[str, object]
    hidden_schedule: tuple[Mapping[str, object], ...]
    capability_labels: tuple[str, ...]
    historical_provenance: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.source_run_reference, MinedSourceRunReferenceV1):
            raise TypeError("recorded source run reference is invalid")
        if (
            type(self.source_start_us) is not int
            or self.source_start_us < 0
            or type(self.source_end_us) is not int
            or self.source_end_us <= self.source_start_us
        ):
            raise ValueError("recorded source bounds must be a nonempty interval")
        if self.checkpoint_reference is not None and not isinstance(
            self.checkpoint_reference,
            MinedCheckpointReferenceV1,
        ):
            raise TypeError("recorded source checkpoint is invalid")
        _require_sha256(self.source_ancestry_sha256, "source-ancestry digest")
        _require_sha256(
            self.parent_source_ancestry_sha256,
            "parent source-ancestry digest",
            optional=True,
        )
        _require_sha256(
            self.authoritative_event_prefix_sha256,
            "authoritative event-prefix digest",
            optional=True,
        )
        if type(self.observable_feed) is not tuple or not self.observable_feed:
            raise ValueError("recorded source requires a nonempty client feed")
        if any(
            not isinstance(event, RecordedClientFeedEventV1)
            for event in self.observable_feed
        ):
            raise TypeError("recorded client feed contains an untyped event")
        sequences = tuple(event.source_sequence for event in self.observable_feed)
        times = tuple(event.client_time_us for event in self.observable_feed)
        ids = tuple(event.client_event_id for event in self.observable_feed)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(set(sequences)):
            raise ValueError("recorded client-feed sequences must be unique and ordered")
        if times != tuple(sorted(times)):
            raise ValueError("recorded client-feed times must be monotonic")
        if len(ids) != len(set(ids)):
            raise ValueError("recorded client-feed event IDs must be unique")
        if any(
            not self.source_start_us <= event.client_time_us < self.source_end_us
            for event in self.observable_feed
        ):
            raise ValueError("recorded client-feed event lies outside source bounds")
        if type(self.capability_labels) is not tuple or not self.capability_labels:
            raise ValueError("recorded source requires capability labels")
        if any(
            type(label) is not str or _EVENT_KIND.fullmatch(label) is None
            for label in self.capability_labels
        ):
            raise ValueError("recorded source capability labels are invalid")
        if self.capability_labels != tuple(sorted(set(self.capability_labels))):
            raise ValueError("recorded source capability labels must be sorted and unique")
        rng = freeze_json(self.rng_state)
        if not isinstance(rng, Mapping) or not rng:
            raise ValueError("recorded source RNG state must be a nonempty record")
        schedule = freeze_json(self.hidden_schedule)
        if not isinstance(schedule, tuple) or any(
            not isinstance(item, Mapping) for item in schedule
        ):
            raise TypeError("recorded source hidden schedule must be an object tuple")
        provenance = freeze_json(self.historical_provenance)
        if not isinstance(provenance, Mapping) or not provenance:
            raise ValueError("recorded source historical provenance must be a record")
        object.__setattr__(self, "rng_state", rng)
        object.__setattr__(self, "hidden_schedule", schedule)
        object.__setattr__(self, "historical_provenance", provenance)

    def as_dict(self) -> dict[str, object]:
        return {
            "authoritative_event_prefix_sha256": (
                self.authoritative_event_prefix_sha256
            ),
            "capability_labels": list(self.capability_labels),
            "checkpoint_reference": (
                None
                if self.checkpoint_reference is None
                else self.checkpoint_reference.as_dict()
            ),
            "hidden_schedule": thaw_json(self.hidden_schedule),
            "historical_provenance": thaw_json(self.historical_provenance),
            "observable_feed": [event.as_dict() for event in self.observable_feed],
            "parent_source_ancestry_sha256": (
                self.parent_source_ancestry_sha256
            ),
            "rng_state": thaw_json(self.rng_state),
            "source_ancestry_sha256": self.source_ancestry_sha256,
            "source_end_us": self.source_end_us,
            "source_run_reference": self.source_run_reference.as_dict(),
            "source_start_us": self.source_start_us,
        }

    @property
    def semantic_sha256(self) -> str:
        return sha256_json(self.as_dict())


def observable_prefix_sha256_v1(
    events: tuple[RecordedClientFeedEventV1, ...],
    warmup_start_us: int,
    post_end_us: int,
) -> str:
    """Digest exact feed bytes plus their half-open extraction boundary."""

    if (
        type(warmup_start_us) is not int
        or type(post_end_us) is not int
        or post_end_us <= warmup_start_us
    ):
        raise ValueError("observable-prefix bounds are invalid")
    if any(
        not warmup_start_us <= event.client_time_us < post_end_us
        for event in events
    ):
        raise ValueError("observable-prefix event lies outside its exact bounds")
    return sha256_json(
        {
            "events": [event.as_dict() for event in events],
            "post_end_us": post_end_us,
            "record_kind": "RECORDED_CLIENT_FEED_PREFIX_V1",
            "warmup_start_us": warmup_start_us,
        }
    )


@dataclass(frozen=True, slots=True)
class MinedLessonSourceEnvelopeV1:
    """Sealed supporting material; never used as an assessment presentation."""

    candidate_id: str
    candidate_digest: str
    source_record_sha256: str
    source_ancestry_sha256: str
    parent_source_ancestry_sha256: str | None
    authoritative_event_prefix_sha256: str | None
    source_observable_prefix_sha256: str
    extracted_observable_prefix_sha256: str
    warmup_start_us: int
    post_end_us: int
    observable_feed: tuple[RecordedClientFeedEventV1, ...]
    rng_state: Mapping[str, object]
    hidden_schedule: tuple[Mapping[str, object], ...]
    capability_labels: tuple[str, ...]
    schema_version: int = LESSON_EXTRACTION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if self.candidate_id != f"lesson-candidate-{self.candidate_digest}":
            raise ValueError("source envelope candidate ID and digest disagree")
        for value, label in (
            (self.candidate_digest, "candidate digest"),
            (self.source_record_sha256, "source-record digest"),
            (self.source_ancestry_sha256, "source-ancestry digest"),
            (self.source_observable_prefix_sha256, "source observable-prefix digest"),
            (
                self.extracted_observable_prefix_sha256,
                "extracted observable-prefix digest",
            ),
        ):
            _require_sha256(value, label)
        _require_sha256(
            self.parent_source_ancestry_sha256,
            "parent source-ancestry digest",
            optional=True,
        )
        _require_sha256(
            self.authoritative_event_prefix_sha256,
            "authoritative event-prefix digest",
            optional=True,
        )
        if self.schema_version != LESSON_EXTRACTION_SCHEMA_VERSION_V1:
            raise ValueError("source envelope schema version is unsupported")
        if self.source_observable_prefix_sha256 != self.extracted_observable_prefix_sha256:
            raise ValueError("source and extracted observable prefixes differ")
        if type(self.observable_feed) is not tuple or not self.observable_feed or any(
            not isinstance(event, RecordedClientFeedEventV1)
            for event in self.observable_feed
        ):
            raise TypeError("source envelope requires a typed nonempty client feed")
        if type(self.capability_labels) is not tuple or not self.capability_labels or (
            self.capability_labels != tuple(sorted(set(self.capability_labels)))
        ):
            raise ValueError("source envelope capability labels must be sorted and unique")
        expected = observable_prefix_sha256_v1(
            self.observable_feed,
            self.warmup_start_us,
            self.post_end_us,
        )
        if self.extracted_observable_prefix_sha256 != expected:
            raise ValueError("source envelope observable feed does not match its digest")
        rng = freeze_json(self.rng_state)
        schedule = freeze_json(self.hidden_schedule)
        if not isinstance(rng, Mapping) or not isinstance(schedule, tuple):
            raise TypeError("source envelope hidden state is invalid")
        object.__setattr__(self, "rng_state", rng)
        object.__setattr__(self, "hidden_schedule", schedule)

    def as_dict(self) -> dict[str, object]:
        return {
            "authoritative_event_prefix_sha256": (
                self.authoritative_event_prefix_sha256
            ),
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "capability_labels": list(self.capability_labels),
            "extracted_observable_prefix_sha256": (
                self.extracted_observable_prefix_sha256
            ),
            "hidden_schedule": thaw_json(self.hidden_schedule),
            "observable_feed": [event.as_dict() for event in self.observable_feed],
            "parent_source_ancestry_sha256": (
                self.parent_source_ancestry_sha256
            ),
            "post_end_us": self.post_end_us,
            "record_kind": "MINED_LESSON_SOURCE_ENVELOPE_V1",
            "rng_state": thaw_json(self.rng_state),
            "schema_version": self.schema_version,
            "source_ancestry_sha256": self.source_ancestry_sha256,
            "source_observable_prefix_sha256": (
                self.source_observable_prefix_sha256
            ),
            "source_record_sha256": self.source_record_sha256,
            "warmup_start_us": self.warmup_start_us,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExtractedLessonSourceV1:
    source_record: MinedLessonSourceRecordV1
    envelope: MinedLessonSourceEnvelopeV1

    def __post_init__(self) -> None:
        if not isinstance(self.source_record, MinedLessonSourceRecordV1):
            raise TypeError("extracted lesson source record is invalid")
        if not isinstance(self.envelope, MinedLessonSourceEnvelopeV1):
            raise TypeError("extracted lesson source envelope is invalid")
        if self.envelope.source_record_sha256 != self.source_record.sha256:
            raise ValueError("source envelope is not bound to its lineage record")
        bounds = self.source_record.source_time_bounds
        if (
            bounds.warmup_start_us != self.envelope.warmup_start_us
            or bounds.post_end_us != self.envelope.post_end_us
        ):
            raise ValueError("source record and envelope extraction bounds differ")

    def identity_projection(self) -> dict[str, str]:
        return {
            "source_envelope_sha256": self.envelope.sha256,
            "source_record_sha256": self.source_record.sha256,
        }


def extract_observable_lesson_source_v1(
    candidate: LessonCandidateV1,
    source: RecordedLessonSourceV1,
) -> ExtractedLessonSourceV1:
    """Extract one exact half-open feed window without mutating source replay."""

    if not isinstance(candidate, LessonCandidateV1):
        raise TypeError("lesson extraction requires a typed candidate")
    if not isinstance(source, RecordedLessonSourceV1):
        raise TypeError("lesson extraction requires a typed recorded source")
    source_before = source.semantic_sha256
    ancestry = candidate.source_ancestry
    if (
        source.source_run_reference.source_kind != ancestry.source_kind.value
        or source.source_run_reference.source_id != ancestry.source_id
        or source.source_run_reference.source_sha256 != ancestry.source_sha256
    ):
        raise ValueError("recorded source run differs from candidate ancestry")
    if source.source_ancestry_sha256 != ancestry.sha256:
        raise ValueError("recorded source ancestry digest differs from candidate")
    if (
        source.parent_source_ancestry_sha256
        != ancestry.parent_source_ancestry_sha256
    ):
        raise ValueError("recorded source parent linkage differs from candidate")
    if source.authoritative_event_prefix_sha256 != ancestry.event_prefix_sha256:
        raise ValueError("recorded authoritative event prefix differs from candidate")
    bounds = candidate.bounds
    if (
        source.source_start_us != bounds.source_start_us
        or source.source_end_us != bounds.source_end_us
    ):
        raise ValueError("recorded source bounds differ from candidate source bounds")
    expected_checkpoint = (
        None
        if candidate.checkpoint is None
        else MinedCheckpointReferenceV1(
            candidate.checkpoint.checkpoint_id,
            candidate.checkpoint.checkpoint_sha256,
        )
    )
    if source.checkpoint_reference != expected_checkpoint:
        raise ValueError("recorded source checkpoint differs from candidate")
    expected_capabilities = tuple(
        row.capability for row in candidate.capability_record.records
    )
    if source.capability_labels != expected_capabilities:
        raise ValueError("recorded source capabilities differ from candidate admission")

    source_events = tuple(
        event
        for event in source.observable_feed
        if bounds.warmup_start_us <= event.client_time_us < bounds.post_end_us
    )
    if not source_events:
        raise ValueError("candidate extraction produced an empty client feed")
    if (
        source_events[0].client_time_us != bounds.warmup_start_us
        or source_events[0].kind != CLIENT_CONTEXT_SNAPSHOT_KIND_V1
    ):
        raise ValueError("candidate warmup must begin with an exact client snapshot")
    delivered_at = {
        event.client_event_id: event.client_time_us for event in source_events
    }
    contributing_ids = (
        candidate.observable_feature_summary.contributing_source_event_ids
    )
    if any(event_id not in delivered_at for event_id in contributing_ids):
        raise ValueError("candidate evidence is absent from the extracted client feed")
    if any(delivered_at[event_id] > bounds.activation_us for event_id in contributing_ids):
        raise ValueError("candidate used evidence before the recorded client received it")

    source_prefix = observable_prefix_sha256_v1(
        source_events,
        bounds.warmup_start_us,
        bounds.post_end_us,
    )
    extracted_events = tuple(source_events)
    extracted_prefix = observable_prefix_sha256_v1(
        extracted_events,
        bounds.warmup_start_us,
        bounds.post_end_us,
    )
    provenance_payload = thaw_json(source.historical_provenance)
    assert isinstance(provenance_payload, dict)
    historical_provenance = {
        "evidence_class": candidate.evidence_class.value,
        "source_kind": ancestry.source_kind.value,
        "source_provenance": provenance_payload,
        "source_provenance_sha256": sha256_json(provenance_payload),
    }
    source_record = MinedLessonSourceRecordV1(
        source_run_reference=source.source_run_reference,
        source_time_bounds=MinedSourceTimeBoundsV1(
            source_start_us=bounds.source_start_us,
            source_end_us=bounds.source_end_us,
            warmup_start_us=bounds.warmup_start_us,
            active_start_us=bounds.active_start_us,
            active_end_us=bounds.active_end_us,
            post_end_us=bounds.post_end_us,
        ),
        checkpoint_reference=expected_checkpoint,
        observable_feed_policy=RECORDED_CLIENT_FEED_POLICY_V1,
        hidden_state_reveal_policy=MINED_HIDDEN_STATE_REVEAL_POLICY_V1,
        historical_provenance=historical_provenance,
        detector=MinedDetectorReferenceV1(
            candidate.detector.detector_id,
            candidate.detector.version,
            candidate.detector.threshold_sha256,
        ),
    )
    envelope = MinedLessonSourceEnvelopeV1(
        candidate_id=candidate.candidate_id,
        candidate_digest=candidate.candidate_digest,
        source_record_sha256=source_record.sha256,
        source_ancestry_sha256=ancestry.sha256,
        parent_source_ancestry_sha256=ancestry.parent_source_ancestry_sha256,
        authoritative_event_prefix_sha256=ancestry.event_prefix_sha256,
        source_observable_prefix_sha256=source_prefix,
        extracted_observable_prefix_sha256=extracted_prefix,
        warmup_start_us=bounds.warmup_start_us,
        post_end_us=bounds.post_end_us,
        observable_feed=extracted_events,
        rng_state=source.rng_state,
        hidden_schedule=source.hidden_schedule,
        capability_labels=source.capability_labels,
    )
    if source.semantic_sha256 != source_before:
        raise RuntimeError("lesson extraction mutated authoritative source replay")
    return ExtractedLessonSourceV1(source_record, envelope)


__all__ = [
    "ASSESSMENT_FORBIDDEN_FEED_KEYS_V1",
    "CLIENT_CONTEXT_SNAPSHOT_KIND_V1",
    "LESSON_EXTRACTION_SCHEMA_VERSION_V1",
    "ExtractedLessonSourceV1",
    "MinedLessonSourceEnvelopeV1",
    "RecordedClientFeedEventV1",
    "RecordedLessonSourceV1",
    "extract_observable_lesson_source_v1",
    "observable_prefix_sha256_v1",
]
