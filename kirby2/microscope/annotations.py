"""Immutable replay bookmarks, annotations, and timing-lie review sidecars.

The records in this module are analysis sidecars.  They bind exact, already-built
replay outputs and never accept or rewrite a source recording.  Editing a bookmark,
annotation, or human review therefore creates a content-addressed successor whose
predecessor commitment remains visible.

Timing-lie readiness and human authority are deliberately separate.  Software can
build a complete six-search packet with ``READY_FOR_HUMAN_REVIEW`` technical status,
but that packet always reports the human result as ``PENDING``.  WO36-E has no
verified reviewer-authority adapter, so its public API cannot construct a human
reviewer sidecar or manufacture a non-pending judgment.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum
from types import MappingProxyType

from .panes import PaneAvailability, PaneKind, SynchronizedPaneSnapshot
from .policy import ObservationMode, ObservationPolicy
from .timeline import TimelineCursor


REPLAY_SIDECAR_TARGET_SCHEMA_ID = "KIRBY2_REPLAY_SIDECAR_TARGET_V1"
REPLAY_SIDECAR_TARGET_SCHEMA_VERSION = 1
REPLAY_BOOKMARK_SCHEMA_ID = "KIRBY2_REPLAY_BOOKMARK_V1"
REPLAY_BOOKMARK_SCHEMA_VERSION = 1
REPLAY_ANNOTATION_SCHEMA_ID = "KIRBY2_REPLAY_ANNOTATION_V1"
REPLAY_ANNOTATION_SCHEMA_VERSION = 1
TIMING_LIE_REVIEW_PACKET_SCHEMA_ID = "KIRBY2_TIMING_LIE_REVIEW_PACKET_V1"
TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION = 1
TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_ID = (
    "KIRBY2_TIMING_LIE_REVIEWER_SIDECAR_V1"
)
TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_VERSION = 1
TIMING_LIE_REVIEW_RESULT_SCHEMA_ID = "KIRBY2_TIMING_LIE_REVIEW_RESULT_V1"
TIMING_LIE_REVIEW_RESULT_SCHEMA_VERSION = 1
TIMING_LIE_RUBRIC_VERSION = "KIRBY2_TIMING_LIE_RUBRIC_V1"
SOURCE_MUTATION_POLICY = "SOURCE_RUN_IMMUTABLE_SIDECAR_ONLY"
HUMAN_REVIEW_AUTHORITY = "SEPARATE_LOCAL_AUTHENTICATED_REVIEWER_SIDECAR"


_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_TARGET_TOKEN = object()
_BOOKMARK_TOKEN = object()
_ANNOTATION_TOKEN = object()
_RUBRIC_SEARCH_TOKEN = object()
_REVIEW_PACKET_TOKEN = object()
_RUBRIC_JUDGMENT_TOKEN = object()
_REVIEWER_SIDECAR_TOKEN = object()
_REVIEW_RESULT_TOKEN = object()


class ReplayAnnotationKind(str, Enum):
    ANALYSIS_NOTE = "ANALYSIS_NOTE"
    QUESTION = "QUESTION"
    TIMING_LIE_CANDIDATE = "TIMING_LIE_CANDIDATE"
    CAUSAL_LANGUAGE_NOTE = "CAUSAL_LANGUAGE_NOTE"


class TimingLieRubricSearch(str, Enum):
    ORDER_BEFORE_ACKNOWLEDGEMENT = "ORDER_BEFORE_ACKNOWLEDGEMENT"
    FILL_BEFORE_CLIENT_REPORT = "FILL_BEFORE_CLIENT_REPORT"
    FUTURE_QUOTE_OR_FEATURE_INTERPOLATION = (
        "FUTURE_QUOTE_OR_FEATURE_INTERPOLATION"
    )
    HIDDEN_FIELD_LEAKAGE = "HIDDEN_FIELD_LEAKAGE"
    RECOMPUTED_EXPLANATIONS = "RECOMPUTED_EXPLANATIONS"
    MISLEADING_CAUSAL_WORDING = "MISLEADING_CAUSAL_WORDING"


TIMING_LIE_RUBRIC_ORDER = tuple(TimingLieRubricSearch)


TIMING_LIE_RUBRIC_PROMPTS: Mapping[TimingLieRubricSearch, str] = MappingProxyType({
    TimingLieRubricSearch.ORDER_BEFORE_ACKNOWLEDGEMENT: (
        "Search for an order shown before its recorded client acknowledgement."
    ),
    TimingLieRubricSearch.FILL_BEFORE_CLIENT_REPORT: (
        "Search for a fill shown before its recorded client report."
    ),
    TimingLieRubricSearch.FUTURE_QUOTE_OR_FEATURE_INTERPOLATION: (
        "Search for future quote or feature values interpolated into an earlier view."
    ),
    TimingLieRubricSearch.HIDDEN_FIELD_LEAKAGE: (
        "Search for hidden or unauthorized fields leaked into the active view."
    ),
    TimingLieRubricSearch.RECOMPUTED_EXPLANATIONS: (
        "Search for explanations recomputed from later state instead of "
        "recorded evidence."
    ),
    TimingLieRubricSearch.MISLEADING_CAUSAL_WORDING: (
        "Search for causal wording that overstates the linked recorded evidence."
    ),
})


class TimingLieTechnicalStatus(str, Enum):
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"


class TimingLieHumanResult(str, Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class TimingLieRubricOutcome(str, Enum):
    NO_TIMING_LIE_FOUND = "NO_TIMING_LIE_FOUND"
    TIMING_LIE_FOUND = "TIMING_LIE_FOUND"
    INCONCLUSIVE = "INCONCLUSIVE"


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


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _run_id(value: object, label: str) -> str:
    if type(value) is not str or _RUN_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _text(
    value: object,
    label: str,
    *,
    maximum: int,
    empty: bool = False,
) -> str:
    if type(value) is not str or (not empty and not value):
        qualifier = "text" if empty else "nonempty text"
        raise ValueError(f"{label} must be {qualifier}")
    if len(value) > maximum or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be bounded NFC text")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _canonical_tags(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError("sidecar tags must be an immutable tuple")
    for value in values:
        _identifier(value, "sidecar tag")
    canonical = tuple(sorted(set(values), key=lambda item: item.encode("utf-8")))
    if len(canonical) != len(values):
        raise ValueError("sidecar tags must be unique")
    return canonical


@dataclass(frozen=True, slots=True)
class ReplaySidecarTargetV1:
    """Exact immutable pane/cursor/snapshot target for an analysis sidecar."""

    source_run_id: str
    source_event_sha256: str
    timeline_id: str
    cursor_id: str
    query_id: str
    observed_projection_sha256: str
    observation_mode: ObservationMode
    policy_id: str
    render_cursor_time_us: int
    snapshot_id: str
    pane_kind: PaneKind
    pane_availability: PaneAvailability
    pane_sha256: str
    _construction_token: InitVar[object]
    schema_id: str = REPLAY_SIDECAR_TARGET_SCHEMA_ID
    schema_version: int = REPLAY_SIDECAR_TARGET_SCHEMA_VERSION
    source_mutation_policy: str = SOURCE_MUTATION_POLICY
    target_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _TARGET_TOKEN:
            raise TypeError("replay sidecar targets require the governed binder")
        _run_id(self.source_run_id, "sidecar target source run ID")
        _sha256(self.source_event_sha256, "sidecar target source event digest")
        _identifier(self.timeline_id, "sidecar target timeline ID")
        _identifier(self.cursor_id, "sidecar target cursor ID")
        _identifier(self.query_id, "sidecar target query ID")
        _sha256(
            self.observed_projection_sha256,
            "sidecar target observed projection digest",
        )
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("sidecar target observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("sidecar target mode and policy differ")
        _nonnegative_int(self.render_cursor_time_us, "sidecar target render cursor")
        _identifier(self.snapshot_id, "sidecar target snapshot ID")
        if type(self.pane_kind) is not PaneKind:
            raise TypeError("sidecar target pane kind is invalid")
        if type(self.pane_availability) is not PaneAvailability:
            raise TypeError("sidecar target pane availability is invalid")
        _sha256(self.pane_sha256, "sidecar target pane digest")
        if (
            self.schema_id != REPLAY_SIDECAR_TARGET_SCHEMA_ID
            or self.schema_version != REPLAY_SIDECAR_TARGET_SCHEMA_VERSION
            or self.source_mutation_policy != SOURCE_MUTATION_POLICY
        ):
            raise ValueError("sidecar target schema or source policy changed")
        object.__setattr__(
            self,
            "target_id",
            "replay-sidecar-target-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "cursor_id": self.cursor_id,
            "observation_mode": self.observation_mode.value,
            "observed_projection_sha256": self.observed_projection_sha256,
            "pane_availability": self.pane_availability.value,
            "pane_kind": self.pane_kind.value,
            "pane_sha256": self.pane_sha256,
            "policy_id": self.policy_id,
            "query_id": self.query_id,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_event_sha256": self.source_event_sha256,
            "source_mutation_policy": self.source_mutation_policy,
            "source_run_id": self.source_run_id,
            "timeline_id": self.timeline_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "target_id": self.target_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def target_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def bind_replay_sidecar_target(
    cursor: TimelineCursor,
    snapshot: SynchronizedPaneSnapshot,
    pane_kind: PaneKind,
) -> ReplaySidecarTargetV1:
    """Bind one governed cursor and synchronized pane without source mutation."""

    if type(cursor) is not TimelineCursor:
        raise TypeError("sidecar target binder requires TimelineCursor")
    if type(snapshot) is not SynchronizedPaneSnapshot:
        raise TypeError("sidecar target binder requires SynchronizedPaneSnapshot")
    if type(pane_kind) is not PaneKind:
        raise TypeError("sidecar target binder requires PaneKind")
    if (
        cursor.source_run_id != snapshot.source_run_id
        or cursor.source_event_sha256 != snapshot.source_event_sha256
        or cursor.observation_mode is not snapshot.observation_mode
        or cursor.policy_id != snapshot.policy_id
        or cursor.render_cursor_time_us != snapshot.render_cursor_time_us
    ):
        raise ValueError("sidecar cursor and pane snapshot are split-brain")
    pane = snapshot.pane(pane_kind)
    return ReplaySidecarTargetV1(
        source_run_id=snapshot.source_run_id,
        source_event_sha256=snapshot.source_event_sha256,
        timeline_id=cursor.timeline_id,
        cursor_id=cursor.cursor_id,
        query_id=snapshot.query_id,
        observed_projection_sha256=snapshot.observed_projection_sha256,
        observation_mode=snapshot.observation_mode,
        policy_id=snapshot.policy_id,
        render_cursor_time_us=snapshot.render_cursor_time_us,
        snapshot_id=snapshot.snapshot_id,
        pane_kind=pane_kind,
        pane_availability=pane.availability,
        pane_sha256=_canonical_sha256(pane.as_dict()),
        _construction_token=_TARGET_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class ReplayBookmarkV1:
    target: ReplaySidecarTargetV1
    label: str
    author_id: str
    tags: tuple[str, ...]
    revision: int
    predecessor_bookmark_id: str | None
    predecessor_sha256: str | None
    _construction_token: InitVar[object]
    schema_id: str = REPLAY_BOOKMARK_SCHEMA_ID
    schema_version: int = REPLAY_BOOKMARK_SCHEMA_VERSION
    source_mutation_policy: str = SOURCE_MUTATION_POLICY
    bookmark_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _BOOKMARK_TOKEN:
            raise TypeError("replay bookmarks require the governed builder")
        if type(self.target) is not ReplaySidecarTargetV1:
            raise TypeError("bookmark target is invalid")
        _text(self.label, "bookmark label", maximum=256)
        _identifier(self.author_id, "bookmark author ID")
        object.__setattr__(self, "tags", _canonical_tags(self.tags))
        _positive_int(self.revision, "bookmark revision")
        if (self.predecessor_bookmark_id is None) != (
            self.predecessor_sha256 is None
        ):
            raise ValueError("bookmark predecessor ID and digest must travel together")
        if self.revision == 1:
            if self.predecessor_bookmark_id is not None:
                raise ValueError("first bookmark revision cannot have a predecessor")
        else:
            _identifier(self.predecessor_bookmark_id, "predecessor bookmark ID")
            _sha256(self.predecessor_sha256, "predecessor bookmark digest")
        if (
            self.schema_id != REPLAY_BOOKMARK_SCHEMA_ID
            or self.schema_version != REPLAY_BOOKMARK_SCHEMA_VERSION
            or self.source_mutation_policy != SOURCE_MUTATION_POLICY
        ):
            raise ValueError("bookmark schema or source policy changed")
        object.__setattr__(
            self,
            "bookmark_id",
            "replay-bookmark-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "author_id": self.author_id,
            "label": self.label,
            "predecessor_bookmark_id": self.predecessor_bookmark_id,
            "predecessor_sha256": self.predecessor_sha256,
            "revision": self.revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_mutation_policy": self.source_mutation_policy,
            "tags": list(self.tags),
            "target": self.target.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "bookmark_id": self.bookmark_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sidecar_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def source_run_id(self) -> str:
        return self.target.source_run_id

    @property
    def source_event_sha256(self) -> str:
        return self.target.source_event_sha256

    @property
    def snapshot_id(self) -> str:
        return self.target.snapshot_id

    @property
    def render_cursor_time_us(self) -> int:
        return self.target.render_cursor_time_us

    @property
    def pane_kind(self) -> PaneKind:
        return self.target.pane_kind


def create_replay_bookmark(
    target: ReplaySidecarTargetV1,
    *,
    label: str,
    author_id: str,
    tags: tuple[str, ...] = (),
    predecessor: ReplayBookmarkV1 | None = None,
) -> ReplayBookmarkV1:
    """Create the first bookmark revision or a successor without mutation."""

    if type(target) is not ReplaySidecarTargetV1:
        raise TypeError("bookmark builder requires a governed target")
    if predecessor is not None:
        if type(predecessor) is not ReplayBookmarkV1:
            raise TypeError("bookmark predecessor is invalid")
        if predecessor.target.target_id != target.target_id:
            raise ValueError("bookmark revisions cannot move to another replay target")
    return ReplayBookmarkV1(
        target=target,
        label=label,
        author_id=author_id,
        tags=tags,
        revision=1 if predecessor is None else predecessor.revision + 1,
        predecessor_bookmark_id=(
            None if predecessor is None else predecessor.bookmark_id
        ),
        predecessor_sha256=(
            None if predecessor is None else predecessor.sidecar_sha256
        ),
        _construction_token=_BOOKMARK_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class ReplayAnnotationV1:
    target: ReplaySidecarTargetV1
    kind: ReplayAnnotationKind
    body: str
    author_id: str
    tags: tuple[str, ...]
    revision: int
    predecessor_annotation_id: str | None
    predecessor_sha256: str | None
    _construction_token: InitVar[object]
    schema_id: str = REPLAY_ANNOTATION_SCHEMA_ID
    schema_version: int = REPLAY_ANNOTATION_SCHEMA_VERSION
    source_mutation_policy: str = SOURCE_MUTATION_POLICY
    annotation_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _ANNOTATION_TOKEN:
            raise TypeError("replay annotations require the governed builder")
        if type(self.target) is not ReplaySidecarTargetV1:
            raise TypeError("annotation target is invalid")
        if type(self.kind) is not ReplayAnnotationKind:
            raise TypeError("annotation kind is invalid")
        _text(self.body, "annotation body", maximum=8192)
        _identifier(self.author_id, "annotation author ID")
        object.__setattr__(self, "tags", _canonical_tags(self.tags))
        _positive_int(self.revision, "annotation revision")
        if (self.predecessor_annotation_id is None) != (
            self.predecessor_sha256 is None
        ):
            raise ValueError(
                "annotation predecessor ID and digest must travel together"
            )
        if self.revision == 1:
            if self.predecessor_annotation_id is not None:
                raise ValueError("first annotation revision cannot have a predecessor")
        else:
            _identifier(self.predecessor_annotation_id, "predecessor annotation ID")
            _sha256(self.predecessor_sha256, "predecessor annotation digest")
        if (
            self.schema_id != REPLAY_ANNOTATION_SCHEMA_ID
            or self.schema_version != REPLAY_ANNOTATION_SCHEMA_VERSION
            or self.source_mutation_policy != SOURCE_MUTATION_POLICY
        ):
            raise ValueError("annotation schema or source policy changed")
        object.__setattr__(
            self,
            "annotation_id",
            "replay-annotation-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "author_id": self.author_id,
            "body": self.body,
            "kind": self.kind.value,
            "predecessor_annotation_id": self.predecessor_annotation_id,
            "predecessor_sha256": self.predecessor_sha256,
            "revision": self.revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_mutation_policy": self.source_mutation_policy,
            "tags": list(self.tags),
            "target": self.target.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "annotation_id": self.annotation_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sidecar_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def source_run_id(self) -> str:
        return self.target.source_run_id

    @property
    def source_event_sha256(self) -> str:
        return self.target.source_event_sha256

    @property
    def snapshot_id(self) -> str:
        return self.target.snapshot_id

    @property
    def render_cursor_time_us(self) -> int:
        return self.target.render_cursor_time_us

    @property
    def pane_kind(self) -> PaneKind:
        return self.target.pane_kind


def create_replay_annotation(
    target: ReplaySidecarTargetV1,
    *,
    kind: ReplayAnnotationKind,
    body: str,
    author_id: str,
    tags: tuple[str, ...] = (),
    predecessor: ReplayAnnotationV1 | None = None,
) -> ReplayAnnotationV1:
    """Create the first annotation revision or an immutable successor."""

    if type(target) is not ReplaySidecarTargetV1:
        raise TypeError("annotation builder requires a governed target")
    if predecessor is not None:
        if type(predecessor) is not ReplayAnnotationV1:
            raise TypeError("annotation predecessor is invalid")
        if predecessor.target.target_id != target.target_id:
            raise ValueError(
                "annotation revisions cannot move to another replay target"
            )
    return ReplayAnnotationV1(
        target=target,
        kind=kind,
        body=body,
        author_id=author_id,
        tags=tags,
        revision=1 if predecessor is None else predecessor.revision + 1,
        predecessor_annotation_id=(
            None if predecessor is None else predecessor.annotation_id
        ),
        predecessor_sha256=(
            None if predecessor is None else predecessor.sidecar_sha256
        ),
        _construction_token=_ANNOTATION_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class TimingLieRubricSearchV1:
    search: TimingLieRubricSearch
    prompt: str
    targets: tuple[ReplaySidecarTargetV1, ...]
    _construction_token: InitVar[object]
    technical_status: TimingLieTechnicalStatus = (
        TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW
    )
    human_result: TimingLieHumanResult = TimingLieHumanResult.PENDING
    search_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _RUBRIC_SEARCH_TOKEN:
            raise TypeError("timing-lie searches require the governed packet builder")
        if type(self.search) is not TimingLieRubricSearch:
            raise TypeError("timing-lie rubric search is invalid")
        if self.prompt != TIMING_LIE_RUBRIC_PROMPTS[self.search]:
            raise ValueError("timing-lie rubric prompt changed")
        if type(self.targets) is not tuple or not self.targets or any(
            type(item) is not ReplaySidecarTargetV1 for item in self.targets
        ):
            raise ValueError("timing-lie rubric search requires exact targets")
        canonical = tuple(sorted(self.targets, key=lambda item: item.target_id))
        if len({item.target_id for item in canonical}) != len(canonical):
            raise ValueError("timing-lie rubric search contains duplicate targets")
        object.__setattr__(self, "targets", canonical)
        if (
            self.technical_status
            is not TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW
            or self.human_result is not TimingLieHumanResult.PENDING
        ):
            raise ValueError("software rubric searches cannot claim human judgment")
        object.__setattr__(
            self,
            "search_id",
            "timing-lie-search-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "human_result": self.human_result.value,
            "prompt": self.prompt,
            "search": self.search.value,
            "targets": [item.as_dict() for item in self.targets],
            "technical_status": self.technical_status.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "search_id": self.search_id}


@dataclass(frozen=True, slots=True)
class TimingLieReviewPacketV1:
    searches: tuple[TimingLieRubricSearchV1, ...]
    _construction_token: InitVar[object]
    rubric_version: str = TIMING_LIE_RUBRIC_VERSION
    technical_status: TimingLieTechnicalStatus = (
        TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW
    )
    human_result: TimingLieHumanResult = TimingLieHumanResult.PENDING
    schema_id: str = TIMING_LIE_REVIEW_PACKET_SCHEMA_ID
    schema_version: int = TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION
    source_mutation_policy: str = SOURCE_MUTATION_POLICY
    human_review_authority: str = HUMAN_REVIEW_AUTHORITY
    packet_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _REVIEW_PACKET_TOKEN:
            raise TypeError("timing-lie review packets require the governed builder")
        if type(self.searches) is not tuple or tuple(
            item.search for item in self.searches
        ) != TIMING_LIE_RUBRIC_ORDER:
            raise ValueError("timing-lie review packet must contain all six searches")
        if any(type(item) is not TimingLieRubricSearchV1 for item in self.searches):
            raise TypeError("timing-lie review packet search inventory is invalid")
        targets = tuple(target for item in self.searches for target in item.targets)
        authorities = {
            (
                target.source_run_id,
                target.source_event_sha256,
                target.timeline_id,
                target.observation_mode,
                target.policy_id,
            )
            for target in targets
        }
        if len(authorities) != 1:
            raise ValueError(
                "timing-lie review targets belong to different authorities"
            )
        if (
            self.rubric_version != TIMING_LIE_RUBRIC_VERSION
            or self.technical_status
            is not TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW
            or self.human_result is not TimingLieHumanResult.PENDING
            or self.schema_id != TIMING_LIE_REVIEW_PACKET_SCHEMA_ID
            or self.schema_version != TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION
            or self.source_mutation_policy != SOURCE_MUTATION_POLICY
            or self.human_review_authority != HUMAN_REVIEW_AUTHORITY
        ):
            raise ValueError("timing-lie review packet authority or schema changed")
        object.__setattr__(
            self,
            "packet_id",
            "timing-lie-review-packet-"
            + _canonical_sha256(self.identity_dict())[:24],
        )

    @property
    def source_run_id(self) -> str:
        return self.searches[0].targets[0].source_run_id

    @property
    def source_event_sha256(self) -> str:
        return self.searches[0].targets[0].source_event_sha256

    @property
    def timeline_id(self) -> str:
        return self.searches[0].targets[0].timeline_id

    @property
    def observation_mode(self) -> ObservationMode:
        return self.searches[0].targets[0].observation_mode

    @property
    def policy_id(self) -> str:
        return self.searches[0].targets[0].policy_id

    def identity_dict(self) -> dict[str, object]:
        return {
            "human_result": self.human_result.value,
            "human_review_authority": self.human_review_authority,
            "observation_mode": self.observation_mode.value,
            "policy_id": self.policy_id,
            "rubric_version": self.rubric_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "searches": [item.as_dict() for item in self.searches],
            "source_event_sha256": self.source_event_sha256,
            "source_mutation_policy": self.source_mutation_policy,
            "source_run_id": self.source_run_id,
            "technical_status": self.technical_status.value,
            "timeline_id": self.timeline_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "packet_id": self.packet_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def packet_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def build_timing_lie_review_packet(
    targets_by_search: Mapping[
        TimingLieRubricSearch,
        tuple[ReplaySidecarTargetV1, ...],
    ],
) -> TimingLieReviewPacketV1:
    """Build the exact six-search packet; human status remains ``PENDING``."""

    if not isinstance(targets_by_search, Mapping):
        raise TypeError("timing-lie review targets must be a mapping")
    if set(targets_by_search) != set(TIMING_LIE_RUBRIC_ORDER):
        raise ValueError("timing-lie review requires exactly the six rubric searches")
    searches: list[TimingLieRubricSearchV1] = []
    for search in TIMING_LIE_RUBRIC_ORDER:
        targets = targets_by_search[search]
        if type(targets) is not tuple:
            raise TypeError("timing-lie rubric targets must be immutable tuples")
        searches.append(
            TimingLieRubricSearchV1(
                search=search,
                prompt=TIMING_LIE_RUBRIC_PROMPTS[search],
                targets=targets,
                _construction_token=_RUBRIC_SEARCH_TOKEN,
            )
        )
    return TimingLieReviewPacketV1(
        searches=tuple(searches),
        _construction_token=_REVIEW_PACKET_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class TimingLieRubricJudgmentV1:
    search: TimingLieRubricSearch
    outcome: TimingLieRubricOutcome
    note: str
    _construction_token: InitVar[object]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _RUBRIC_JUDGMENT_TOKEN:
            raise TypeError("timing-lie judgments require the reviewer builder")
        if type(self.search) is not TimingLieRubricSearch:
            raise TypeError("timing-lie judgment search is invalid")
        if type(self.outcome) is not TimingLieRubricOutcome:
            raise TypeError("timing-lie judgment outcome is invalid")
        _text(self.note, "timing-lie reviewer note", maximum=8192)

    def as_dict(self) -> dict[str, object]:
        return {
            "note": self.note,
            "outcome": self.outcome.value,
            "search": self.search.value,
        }


@dataclass(frozen=True, slots=True)
class TimingLieReviewerSidecarV1:
    packet_id: str
    packet_sha256: str
    source_run_id: str
    source_event_sha256: str
    rubric_version: str
    reviewer_id: str
    reviewer_reference: str
    judgments: tuple[TimingLieRubricJudgmentV1, ...]
    revision: int
    predecessor_sidecar_id: str | None
    predecessor_sha256: str | None
    _construction_token: InitVar[object]
    schema_id: str = TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_ID
    schema_version: int = TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_VERSION
    source_mutation_policy: str = SOURCE_MUTATION_POLICY
    sidecar_id: str = field(init=False)
    human_result: TimingLieHumanResult = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _REVIEWER_SIDECAR_TOKEN:
            raise TypeError("timing-lie reviewer sidecars require the governed builder")
        _identifier(self.packet_id, "timing-lie packet ID")
        _sha256(self.packet_sha256, "timing-lie packet digest")
        _run_id(self.source_run_id, "timing-lie reviewer source run ID")
        _sha256(
            self.source_event_sha256,
            "timing-lie reviewer source event digest",
        )
        if self.rubric_version != TIMING_LIE_RUBRIC_VERSION:
            raise ValueError("timing-lie reviewer rubric version changed")
        _identifier(self.reviewer_id, "timing-lie reviewer ID")
        _text(
            self.reviewer_reference,
            "timing-lie reviewer reference",
            maximum=512,
        )
        if not self.reviewer_reference.startswith(("local:", "auth:")):
            raise PermissionError(
                "timing-lie human judgments require local: or auth: reviewer authority"
            )
        if type(self.judgments) is not tuple or tuple(
            item.search for item in self.judgments
        ) != TIMING_LIE_RUBRIC_ORDER:
            raise ValueError("timing-lie reviewer must judge all six searches")
        if any(type(item) is not TimingLieRubricJudgmentV1 for item in self.judgments):
            raise TypeError("timing-lie reviewer judgments are invalid")
        _positive_int(self.revision, "timing-lie reviewer revision")
        if (self.predecessor_sidecar_id is None) != (
            self.predecessor_sha256 is None
        ):
            raise ValueError("reviewer predecessor ID and digest must travel together")
        if self.revision == 1:
            if self.predecessor_sidecar_id is not None:
                raise ValueError("first reviewer revision cannot have a predecessor")
        else:
            _identifier(self.predecessor_sidecar_id, "predecessor reviewer sidecar ID")
            _sha256(self.predecessor_sha256, "predecessor reviewer sidecar digest")
        if (
            self.schema_id != TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_ID
            or self.schema_version != TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_VERSION
            or self.source_mutation_policy != SOURCE_MUTATION_POLICY
        ):
            raise ValueError("reviewer sidecar schema or source policy changed")
        outcomes = {item.outcome for item in self.judgments}
        if TimingLieRubricOutcome.TIMING_LIE_FOUND in outcomes:
            human_result = TimingLieHumanResult.FAIL
        elif TimingLieRubricOutcome.INCONCLUSIVE in outcomes:
            human_result = TimingLieHumanResult.INCONCLUSIVE
        else:
            human_result = TimingLieHumanResult.PASS
        object.__setattr__(self, "human_result", human_result)
        object.__setattr__(
            self,
            "sidecar_id",
            "timing-lie-reviewer-sidecar-"
            + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "human_result": self.human_result.value,
            "judgments": [item.as_dict() for item in self.judgments],
            "packet_id": self.packet_id,
            "packet_sha256": self.packet_sha256,
            "predecessor_sha256": self.predecessor_sha256,
            "predecessor_sidecar_id": self.predecessor_sidecar_id,
            "reviewer_id": self.reviewer_id,
            "reviewer_reference": self.reviewer_reference,
            "revision": self.revision,
            "rubric_version": self.rubric_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_mutation_policy": self.source_mutation_policy,
            "source_run_id": self.source_run_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "sidecar_id": self.sidecar_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sidecar_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def create_timing_lie_reviewer_sidecar(
    packet: TimingLieReviewPacketV1,
    *,
    reviewer_id: str,
    reviewer_reference: str,
    outcomes: Mapping[TimingLieRubricSearch, TimingLieRubricOutcome],
    notes: Mapping[TimingLieRubricSearch, str],
    predecessor: TimingLieReviewerSidecarV1 | None = None,
) -> TimingLieReviewerSidecarV1:
    """Refuse caller-created human authority until a verified adapter exists.

    Reviewer identity and ``local:``/``auth:`` labels are caller-controlled strings;
    accepting either would let ordinary software manufacture a human ``PASS``.  A
    future work order may replace this refusal with a factory that consumes a
    cryptographically or platform-verified reviewer authority receipt.  The latent
    sidecar/result schemas remain defined so that change can be versioned without
    weakening WO36-E.
    """

    if type(packet) is not TimingLieReviewPacketV1:
        raise TypeError("reviewer sidecar requires TimingLieReviewPacketV1")
    del reviewer_id, reviewer_reference, outcomes, notes, predecessor
    raise PermissionError(
        "human timing-lie review requires a verified reviewer-authority adapter; "
        "WO36-E keeps the human result PENDING"
    )


@dataclass(frozen=True, slots=True)
class TimingLieReviewResultV1:
    packet_id: str
    packet_sha256: str
    source_run_id: str
    source_event_sha256: str
    technical_status: TimingLieTechnicalStatus
    human_result: TimingLieHumanResult
    reviewer_sidecar_id: str | None
    reviewer_sidecar_sha256: str | None
    _construction_token: InitVar[object]
    schema_id: str = TIMING_LIE_REVIEW_RESULT_SCHEMA_ID
    schema_version: int = TIMING_LIE_REVIEW_RESULT_SCHEMA_VERSION
    source_mutation_policy: str = SOURCE_MUTATION_POLICY
    result_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _REVIEW_RESULT_TOKEN:
            raise TypeError("timing-lie review results require the governed resolver")
        _identifier(self.packet_id, "timing-lie result packet ID")
        _sha256(self.packet_sha256, "timing-lie result packet digest")
        _run_id(self.source_run_id, "timing-lie result source run ID")
        _sha256(self.source_event_sha256, "timing-lie result source event digest")
        if (
            self.technical_status
            is not TimingLieTechnicalStatus.READY_FOR_HUMAN_REVIEW
        ):
            raise ValueError("timing-lie technical result is not review-ready")
        if type(self.human_result) is not TimingLieHumanResult:
            raise TypeError("timing-lie human result is invalid")
        if (self.reviewer_sidecar_id is None) != (
            self.reviewer_sidecar_sha256 is None
        ):
            raise ValueError("reviewer result ID and digest must travel together")
        if self.reviewer_sidecar_id is None:
            if self.human_result is not TimingLieHumanResult.PENDING:
                raise ValueError("human result requires a separate reviewer sidecar")
        else:
            _identifier(self.reviewer_sidecar_id, "timing-lie reviewer sidecar ID")
            _sha256(
                self.reviewer_sidecar_sha256,
                "timing-lie reviewer sidecar digest",
            )
            if self.human_result is TimingLieHumanResult.PENDING:
                raise ValueError("attached reviewer sidecar cannot remain pending")
        if (
            self.schema_id != TIMING_LIE_REVIEW_RESULT_SCHEMA_ID
            or self.schema_version != TIMING_LIE_REVIEW_RESULT_SCHEMA_VERSION
            or self.source_mutation_policy != SOURCE_MUTATION_POLICY
        ):
            raise ValueError("timing-lie result schema or source policy changed")
        object.__setattr__(
            self,
            "result_id",
            "timing-lie-review-result-"
            + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "human_result": self.human_result.value,
            "packet_id": self.packet_id,
            "packet_sha256": self.packet_sha256,
            "reviewer_sidecar_id": self.reviewer_sidecar_id,
            "reviewer_sidecar_sha256": self.reviewer_sidecar_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_mutation_policy": self.source_mutation_policy,
            "source_run_id": self.source_run_id,
            "technical_status": self.technical_status.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "result_id": self.result_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


def resolve_timing_lie_review(
    packet: TimingLieReviewPacketV1,
    reviewer_sidecar: TimingLieReviewerSidecarV1 | None = None,
) -> TimingLieReviewResultV1:
    """Resolve review state without allowing software to invent human authority."""

    if type(packet) is not TimingLieReviewPacketV1:
        raise TypeError("timing-lie resolver requires TimingLieReviewPacketV1")
    if reviewer_sidecar is not None:
        raise PermissionError(
            "human timing-lie resolution requires a verified reviewer-authority "
            "adapter; WO36-E rejects every caller-supplied reviewer sidecar"
        )
    return TimingLieReviewResultV1(
        packet_id=packet.packet_id,
        packet_sha256=packet.packet_sha256,
        source_run_id=packet.source_run_id,
        source_event_sha256=packet.source_event_sha256,
        technical_status=packet.technical_status,
        human_result=TimingLieHumanResult.PENDING,
        reviewer_sidecar_id=None,
        reviewer_sidecar_sha256=None,
        _construction_token=_REVIEW_RESULT_TOKEN,
    )


__all__ = [
    "HUMAN_REVIEW_AUTHORITY",
    "REPLAY_ANNOTATION_SCHEMA_ID",
    "REPLAY_ANNOTATION_SCHEMA_VERSION",
    "REPLAY_BOOKMARK_SCHEMA_ID",
    "REPLAY_BOOKMARK_SCHEMA_VERSION",
    "REPLAY_SIDECAR_TARGET_SCHEMA_ID",
    "REPLAY_SIDECAR_TARGET_SCHEMA_VERSION",
    "SOURCE_MUTATION_POLICY",
    "TIMING_LIE_REVIEW_PACKET_SCHEMA_ID",
    "TIMING_LIE_REVIEW_PACKET_SCHEMA_VERSION",
    "TIMING_LIE_REVIEW_RESULT_SCHEMA_ID",
    "TIMING_LIE_REVIEW_RESULT_SCHEMA_VERSION",
    "TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_ID",
    "TIMING_LIE_REVIEWER_SIDECAR_SCHEMA_VERSION",
    "TIMING_LIE_RUBRIC_ORDER",
    "TIMING_LIE_RUBRIC_PROMPTS",
    "TIMING_LIE_RUBRIC_VERSION",
    "ReplayAnnotationKind",
    "ReplayAnnotationV1",
    "ReplayBookmarkV1",
    "ReplaySidecarTargetV1",
    "TimingLieHumanResult",
    "TimingLieReviewPacketV1",
    "TimingLieReviewResultV1",
    "TimingLieRubricOutcome",
    "TimingLieRubricSearch",
    "TimingLieRubricSearchV1",
    "TimingLieTechnicalStatus",
    "bind_replay_sidecar_target",
    "build_timing_lie_review_packet",
    "create_replay_annotation",
    "create_replay_bookmark",
    "resolve_timing_lie_review",
]
