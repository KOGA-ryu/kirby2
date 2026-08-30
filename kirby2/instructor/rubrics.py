"""Immutable versioned rubrics and derived assignment-attempt score sidecars.

Rubric definitions are content-addressed revisions.  Scoring never changes an
assignment attempt or a rubric: it creates a separate immutable sidecar bound to
the exact attempt and rubric identities.  A rubric correction is therefore a new
rubric revision plus a new score sidecar that explicitly supersedes the old score.

All score values are integers.  Callers choose the scale (points, basis points,
micropoints, and so on) when defining a rubric, and the committed ``score_unit``
names that scale.  Floating-point values are rejected by exact integer checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass, field

from .models import Rubric, create_rubric_revision


RUBRIC_ITEM_SCHEMA_ID = "KIRBY2_RUBRIC_ITEM_V1"
RUBRIC_ITEM_SCHEMA_VERSION = 1
RUBRIC_CONTENT_SCHEMA_ID = "KIRBY2_RUBRIC_CONTENT_V1"
RUBRIC_CONTENT_SCHEMA_VERSION = 1
RUBRIC_REVISION_SCHEMA_ID = "KIRBY2_RUBRIC_REVISION_V1"
RUBRIC_REVISION_SCHEMA_VERSION = 1
RUBRIC_ITEM_SCORE_SCHEMA_ID = "KIRBY2_RUBRIC_ITEM_SCORE_V1"
RUBRIC_ITEM_SCORE_SCHEMA_VERSION = 1
RUBRIC_SCORE_SIDECAR_SCHEMA_ID = "KIRBY2_RUBRIC_SCORE_SIDECAR_V1"
RUBRIC_SCORE_SIDECAR_SCHEMA_VERSION = 1


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ITEM_ID = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_SCORE_UNIT = re.compile(r"[a-z][a-z0-9._-]{0,31}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_ASSIGNMENT_ATTEMPT_ID = re.compile(r"assignment-attempt-[0-9a-f]{64}\Z")
_RUBRIC_ID = re.compile(r"rubric-[0-9a-f]{64}\Z")
_RUBRIC_LINEAGE_ID = re.compile(r"rubric-lineage-[0-9a-f]{64}\Z")
_RUBRIC_SCORE_ID = re.compile(r"rubric-score-[0-9a-f]{64}\Z")
_SCORE_TOKEN = object()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if type(payload) is not dict or _canonical_json_bytes(payload) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return payload


def _exact_object(
    payload: object,
    keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(payload) is not dict:
        raise TypeError(f"{label} must be an exact object")
    if frozenset(payload) != keys:
        raise ValueError(f"{label} fields changed")
    return payload


def _exact_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    if not allow_empty and not value:
        raise ValueError(f"{label} cannot be empty")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _matching_identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _schema(
    schema_id: object,
    schema_version: object,
    *,
    expected_id: str,
    expected_version: int,
    label: str,
) -> None:
    if type(schema_id) is not str or schema_id != expected_id:
        raise ValueError(f"{label} schema ID changed")
    if type(schema_version) is not int or schema_version != expected_version:
        raise ValueError(f"{label} schema version changed")


def _event_ids(value: tuple[str, ...], label: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    for event_id in value:
        _matching_identifier(event_id, _EVENT_ID, f"{label} member")
    canonical = tuple(sorted(set(value), key=lambda item: item.encode("ascii")))
    if canonical != value:
        raise ValueError(f"{label} must be unique and canonically ordered")
    return value


@dataclass(frozen=True, slots=True)
class RubricItemV1:
    """One exact criterion in a rubric definition."""

    item_id: str
    label: str
    description: str
    maximum_score: int
    evidence_required: bool
    schema_id: str = RUBRIC_ITEM_SCHEMA_ID
    schema_version: int = RUBRIC_ITEM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _matching_identifier(self.item_id, _ITEM_ID, "rubric item ID")
        _exact_text(self.label, "rubric item label")
        _exact_text(self.description, "rubric item description")
        _positive_int(self.maximum_score, "rubric item maximum score")
        _exact_bool(self.evidence_required, "rubric item evidence requirement")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RUBRIC_ITEM_SCHEMA_ID,
            expected_version=RUBRIC_ITEM_SCHEMA_VERSION,
            label="rubric item",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "description": self.description,
            "evidence_required": self.evidence_required,
            "item_id": self.item_id,
            "label": self.label,
            "maximum_score": self.maximum_score,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RubricItemV1:
        value = _exact_object(
            payload,
            frozenset(
                {
                    "description",
                    "evidence_required",
                    "item_id",
                    "label",
                    "maximum_score",
                    "schema_id",
                    "schema_version",
                }
            ),
            "rubric item",
        )
        item = cls(
            item_id=value["item_id"],
            label=value["label"],
            description=value["description"],
            maximum_score=value["maximum_score"],
            evidence_required=value["evidence_required"],
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
        )
        if item.as_dict() != value:
            raise ValueError("rubric item did not round-trip exactly")
        return item


@dataclass(frozen=True, slots=True)
class RubricContentV1:
    """Content committed by one immutable :class:`Rubric` revision."""

    title: str
    description: str
    score_unit: str
    scoring_version: int
    items: tuple[RubricItemV1, ...]
    passing_score: int | None
    schema_id: str = RUBRIC_CONTENT_SCHEMA_ID
    schema_version: int = RUBRIC_CONTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _exact_text(self.title, "rubric title")
        _exact_text(self.description, "rubric description", allow_empty=True)
        _matching_identifier(self.score_unit, _SCORE_UNIT, "rubric score unit")
        _positive_int(self.scoring_version, "rubric scoring version")
        if type(self.items) is not tuple or not self.items:
            raise ValueError("rubric items must be a nonempty immutable tuple")
        if any(type(item) is not RubricItemV1 for item in self.items):
            raise TypeError("rubric items must contain exact RubricItemV1 values")
        item_ids = tuple(item.item_id for item in self.items)
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("rubric item IDs must be unique")
        if self.passing_score is not None:
            _nonnegative_int(self.passing_score, "rubric passing score")
            if self.passing_score > self.maximum_score:
                raise ValueError("rubric passing score exceeds its maximum")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RUBRIC_CONTENT_SCHEMA_ID,
            expected_version=RUBRIC_CONTENT_SCHEMA_VERSION,
            label="rubric content",
        )

    @property
    def maximum_score(self) -> int:
        return sum(item.maximum_score for item in self.items)

    def as_dict(self) -> dict[str, object]:
        return {
            "description": self.description,
            "items": [item.as_dict() for item in self.items],
            "maximum_score": self.maximum_score,
            "passing_score": self.passing_score,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "score_unit": self.score_unit,
            "scoring_version": self.scoring_version,
            "title": self.title,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, payload: object) -> RubricContentV1:
        value = _exact_object(
            payload,
            frozenset(
                {
                    "description",
                    "items",
                    "maximum_score",
                    "passing_score",
                    "schema_id",
                    "schema_version",
                    "score_unit",
                    "scoring_version",
                    "title",
                }
            ),
            "rubric content",
        )
        raw_items = value["items"]
        if type(raw_items) is not list:
            raise TypeError("serialized rubric items must be an exact list")
        serialized_maximum = _positive_int(
            value["maximum_score"],
            "serialized rubric maximum score",
        )
        content = cls(
            title=value["title"],
            description=value["description"],
            score_unit=value["score_unit"],
            scoring_version=value["scoring_version"],
            items=tuple(RubricItemV1.from_dict(item) for item in raw_items),
            passing_score=value["passing_score"],
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
        )
        if serialized_maximum != content.maximum_score:
            raise ValueError("serialized rubric maximum score differs")
        if content.as_dict() != value:
            raise ValueError("rubric content did not round-trip exactly")
        return content

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> RubricContentV1:
        return cls.from_dict(_canonical_object(raw, "rubric content"))


@dataclass(frozen=True, slots=True)
class RubricRevisionV1:
    """A full rubric definition paired with its immutable lineage envelope."""

    rubric: Rubric
    content: RubricContentV1
    lineage_content_sha256: tuple[str, ...]
    schema_id: str = RUBRIC_REVISION_SCHEMA_ID
    schema_version: int = RUBRIC_REVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.rubric) is not Rubric:
            raise TypeError("rubric revision requires an exact Rubric envelope")
        if type(self.content) is not RubricContentV1:
            raise TypeError("rubric revision requires exact rubric content")
        if type(self.lineage_content_sha256) is not tuple or any(
            type(digest) is not str for digest in self.lineage_content_sha256
        ):
            raise TypeError("rubric content lineage must be an immutable digest tuple")
        for digest in self.lineage_content_sha256:
            _digest(digest, "rubric lineage content digest")
        if len(self.lineage_content_sha256) != self.rubric.revision:
            raise ValueError("rubric content lineage length differs from revision")
        if not self.lineage_content_sha256:
            raise ValueError("rubric content lineage cannot be empty")
        if self.lineage_content_sha256[-1] != self.content.sha256:
            raise ValueError("rubric content lineage does not end at current content")
        if self.rubric.content_sha256 != self.content.sha256:
            raise ValueError("rubric envelope does not bind its exact content")
        if self.content.scoring_version != self.rubric.revision:
            raise ValueError("rubric scoring version differs from lineage revision")
        rebuilt: Rubric | None = None
        for content_digest in self.lineage_content_sha256:
            rebuilt = create_rubric_revision(content_digest, predecessor=rebuilt)
        if rebuilt is None or rebuilt.as_dict() != self.rubric.as_dict():
            raise ValueError("rubric envelope differs from its content lineage")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RUBRIC_REVISION_SCHEMA_ID,
            expected_version=RUBRIC_REVISION_SCHEMA_VERSION,
            label="rubric revision",
        )

    @property
    def rubric_id(self) -> str:
        return self.rubric.record_id

    @property
    def revision(self) -> int:
        return self.rubric.revision

    @property
    def scoring_version(self) -> int:
        return self.content.scoring_version

    @property
    def record_sha256(self) -> str:
        """Digest of the exact lineage envelope, used by score bindings."""

        return self.rubric.sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "content": self.content.as_dict(),
            "lineage_content_sha256": list(self.lineage_content_sha256),
            "rubric": self.rubric.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(
        cls,
        payload: object,
        *,
        predecessor: RubricRevisionV1 | None = None,
    ) -> RubricRevisionV1:
        value = _exact_object(
            payload,
            frozenset(
                {
                    "content",
                    "lineage_content_sha256",
                    "rubric",
                    "schema_id",
                    "schema_version",
                }
            ),
            "rubric revision",
        )
        if predecessor is not None and type(predecessor) is not cls:
            raise TypeError("rubric predecessor must be an exact RubricRevisionV1")
        content = RubricContentV1.from_dict(value["content"])
        raw_envelope = _exact_object(
            value["rubric"],
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
            "serialized rubric envelope",
        )
        _digest(raw_envelope["content_sha256"], "serialized rubric content digest")
        _matching_identifier(
            raw_envelope["lineage_id"],
            _RUBRIC_LINEAGE_ID,
            "serialized rubric lineage ID",
        )
        _matching_identifier(
            raw_envelope["record_id"],
            _RUBRIC_ID,
            "serialized rubric record ID",
        )
        _positive_int(raw_envelope["revision"], "serialized rubric revision")
        if type(raw_envelope["record_kind"]) is not str or raw_envelope[
            "record_kind"
        ] != "RUBRIC":
            raise ValueError("serialized rubric record kind changed")
        _schema(
            raw_envelope["schema_id"],
            raw_envelope["schema_version"],
            expected_id="KIRBY2_RUBRIC_V1",
            expected_version=1,
            label="serialized rubric envelope",
        )
        predecessor_id = raw_envelope["predecessor_record_id"]
        predecessor_digest = raw_envelope["predecessor_sha256"]
        if (predecessor_id is None) != (predecessor_digest is None):
            raise ValueError("serialized rubric predecessor fields must travel together")
        if predecessor_id is not None:
            _matching_identifier(
                predecessor_id,
                _RUBRIC_ID,
                "serialized rubric predecessor ID",
            )
            _digest(predecessor_digest, "serialized rubric predecessor digest")
        raw_lineage = value["lineage_content_sha256"]
        if type(raw_lineage) is not list or not raw_lineage:
            raise ValueError("serialized rubric content lineage must be a nonempty list")
        lineage = tuple(raw_lineage)
        envelope: Rubric | None = None
        for content_digest in lineage:
            _digest(content_digest, "serialized rubric lineage content digest")
            envelope = create_rubric_revision(
                content_digest,
                predecessor=envelope,
            )
        if envelope is None:
            raise ValueError("serialized rubric content lineage cannot be empty")
        if predecessor is not None:
            predecessor.rubric.validate_successor(envelope)
        revision = cls(
            rubric=envelope,
            content=content,
            lineage_content_sha256=lineage,
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
        )
        if revision.as_dict() != value:
            raise ValueError(
                "serialized rubric revision does not match its reconstructed lineage"
            )
        return revision

    @classmethod
    def from_json_bytes(
        cls,
        raw: bytes,
        *,
        predecessor: RubricRevisionV1 | None = None,
    ) -> RubricRevisionV1:
        return cls.from_dict(
            _canonical_object(raw, "rubric revision"),
            predecessor=predecessor,
        )


@dataclass(frozen=True, slots=True)
class RubricItemScoreV1:
    """One integer item score and the exact evidence events supporting it."""

    item_id: str
    awarded_score: int
    maximum_score: int
    evidence_event_ids: tuple[str, ...]
    schema_id: str = RUBRIC_ITEM_SCORE_SCHEMA_ID
    schema_version: int = RUBRIC_ITEM_SCORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _matching_identifier(self.item_id, _ITEM_ID, "rubric item score ID")
        _nonnegative_int(self.awarded_score, "rubric awarded score")
        _positive_int(self.maximum_score, "rubric item score maximum")
        if self.awarded_score > self.maximum_score:
            raise ValueError("rubric awarded score exceeds item maximum")
        _event_ids(self.evidence_event_ids, "rubric score evidence event IDs")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RUBRIC_ITEM_SCORE_SCHEMA_ID,
            expected_version=RUBRIC_ITEM_SCORE_SCHEMA_VERSION,
            label="rubric item score",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "awarded_score": self.awarded_score,
            "evidence_event_ids": list(self.evidence_event_ids),
            "item_id": self.item_id,
            "maximum_score": self.maximum_score,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RubricItemScoreV1:
        value = _exact_object(
            payload,
            frozenset(
                {
                    "awarded_score",
                    "evidence_event_ids",
                    "item_id",
                    "maximum_score",
                    "schema_id",
                    "schema_version",
                }
            ),
            "rubric item score",
        )
        raw_event_ids = value["evidence_event_ids"]
        if type(raw_event_ids) is not list:
            raise TypeError("serialized evidence event IDs must be an exact list")
        item = cls(
            item_id=value["item_id"],
            awarded_score=value["awarded_score"],
            maximum_score=value["maximum_score"],
            evidence_event_ids=tuple(raw_event_ids),
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
        )
        if item.as_dict() != value:
            raise ValueError("rubric item score did not round-trip exactly")
        return item


@dataclass(frozen=True, slots=True)
class RubricScoreSidecarV1:
    """Immutable score derived from one exact attempt and rubric revision."""

    assignment_attempt_id: str
    assignment_attempt_sha256: str
    rubric_record_id: str
    rubric_record_sha256: str
    rubric_lineage_id: str
    rubric_revision: int
    rubric_content_sha256: str
    scoring_version: int
    score_unit: str
    item_scores: tuple[RubricItemScoreV1, ...]
    supersedes_score_id: str | None
    supersedes_score_sha256: str | None
    _construction_token: InitVar[object]
    schema_id: str = RUBRIC_SCORE_SIDECAR_SCHEMA_ID
    schema_version: int = RUBRIC_SCORE_SIDECAR_SCHEMA_VERSION
    score_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _SCORE_TOKEN:
            raise TypeError("rubric score sidecars require a rubric scoring builder")
        _matching_identifier(
            self.assignment_attempt_id,
            _ASSIGNMENT_ATTEMPT_ID,
            "assignment attempt ID",
        )
        _digest(self.assignment_attempt_sha256, "assignment attempt digest")
        _matching_identifier(self.rubric_record_id, _RUBRIC_ID, "rubric record ID")
        _digest(self.rubric_record_sha256, "rubric record digest")
        _matching_identifier(
            self.rubric_lineage_id,
            _RUBRIC_LINEAGE_ID,
            "rubric lineage ID",
        )
        _positive_int(self.rubric_revision, "rubric revision")
        _digest(self.rubric_content_sha256, "rubric content digest")
        _positive_int(self.scoring_version, "rubric scoring version")
        _matching_identifier(self.score_unit, _SCORE_UNIT, "rubric score unit")
        if type(self.item_scores) is not tuple or not self.item_scores:
            raise ValueError("rubric score items must be a nonempty immutable tuple")
        if any(type(item) is not RubricItemScoreV1 for item in self.item_scores):
            raise TypeError("rubric score items must be exact RubricItemScoreV1 values")
        item_ids = tuple(item.item_id for item in self.item_scores)
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("rubric score item IDs must be unique")
        if (self.supersedes_score_id is None) != (
            self.supersedes_score_sha256 is None
        ):
            raise ValueError("superseded score ID and digest must travel together")
        if self.supersedes_score_id is not None:
            _matching_identifier(
                self.supersedes_score_id,
                _RUBRIC_SCORE_ID,
                "superseded rubric score ID",
            )
            _digest(self.supersedes_score_sha256, "superseded rubric score digest")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RUBRIC_SCORE_SIDECAR_SCHEMA_ID,
            expected_version=RUBRIC_SCORE_SIDECAR_SCHEMA_VERSION,
            label="rubric score sidecar",
        )
        object.__setattr__(
            self,
            "score_id",
            "rubric-score-" + hashlib.sha256(
                _canonical_json_bytes(self.identity_dict())
            ).hexdigest(),
        )

    @property
    def awarded_score(self) -> int:
        return sum(item.awarded_score for item in self.item_scores)

    @property
    def maximum_score(self) -> int:
        return sum(item.maximum_score for item in self.item_scores)

    def identity_dict(self) -> dict[str, object]:
        return {
            "assignment_attempt_id": self.assignment_attempt_id,
            "assignment_attempt_sha256": self.assignment_attempt_sha256,
            "awarded_score": self.awarded_score,
            "item_scores": [item.as_dict() for item in self.item_scores],
            "maximum_score": self.maximum_score,
            "rubric_content_sha256": self.rubric_content_sha256,
            "rubric_lineage_id": self.rubric_lineage_id,
            "rubric_record_id": self.rubric_record_id,
            "rubric_record_sha256": self.rubric_record_sha256,
            "rubric_revision": self.rubric_revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "score_unit": self.score_unit,
            "scoring_version": self.scoring_version,
            "supersedes_score_id": self.supersedes_score_id,
            "supersedes_score_sha256": self.supersedes_score_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "score_id": self.score_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def validate_rubric(self, rubric: RubricRevisionV1) -> None:
        """Validate every durable binding and item against an exact rubric."""

        if type(rubric) is not RubricRevisionV1:
            raise TypeError("score validation requires exact RubricRevisionV1")
        expected_bindings = (
            rubric.rubric_id,
            rubric.record_sha256,
            rubric.rubric.lineage_id,
            rubric.revision,
            rubric.content.sha256,
            rubric.scoring_version,
            rubric.content.score_unit,
        )
        observed_bindings = (
            self.rubric_record_id,
            self.rubric_record_sha256,
            self.rubric_lineage_id,
            self.rubric_revision,
            self.rubric_content_sha256,
            self.scoring_version,
            self.score_unit,
        )
        if observed_bindings != expected_bindings:
            raise ValueError("score sidecar does not bind the exact rubric revision")
        if tuple(score.item_id for score in self.item_scores) != tuple(
            item.item_id for item in rubric.content.items
        ):
            raise ValueError("score sidecar item order differs from its rubric")
        for definition, score in zip(
            rubric.content.items,
            self.item_scores,
            strict=True,
        ):
            if score.maximum_score != definition.maximum_score:
                raise ValueError("score sidecar item maximum differs from its rubric")
            if definition.evidence_required and not score.evidence_event_ids:
                raise ValueError("score sidecar omitted required rubric evidence")

    def validate_supersedes(self, source: RubricScoreSidecarV1) -> None:
        """Validate an exact append-only score derivation edge."""

        if type(source) is not RubricScoreSidecarV1:
            raise TypeError("superseded score must be exact RubricScoreSidecarV1")
        if self.supersedes_score_id != source.score_id:
            raise ValueError("derived score does not bind its source score ID")
        if self.supersedes_score_sha256 != source.sha256:
            raise ValueError("derived score does not bind its source score digest")
        if self.assignment_attempt_id != source.assignment_attempt_id:
            raise ValueError("derived score changed assignment attempt ID")
        if self.assignment_attempt_sha256 != source.assignment_attempt_sha256:
            raise ValueError("derived score changed assignment attempt digest")
        if self.rubric_lineage_id != source.rubric_lineage_id:
            raise ValueError("derived score changed rubric lineage")
        if self.rubric_revision < source.rubric_revision:
            raise ValueError("derived score regressed to an older rubric revision")

    @classmethod
    def from_dict(cls, payload: object) -> RubricScoreSidecarV1:
        value = _exact_object(
            payload,
            frozenset(
                {
                    "assignment_attempt_id",
                    "assignment_attempt_sha256",
                    "awarded_score",
                    "item_scores",
                    "maximum_score",
                    "rubric_content_sha256",
                    "rubric_lineage_id",
                    "rubric_record_id",
                    "rubric_record_sha256",
                    "rubric_revision",
                    "schema_id",
                    "schema_version",
                    "score_id",
                    "score_unit",
                    "scoring_version",
                    "supersedes_score_id",
                    "supersedes_score_sha256",
                }
            ),
            "rubric score sidecar",
        )
        raw_scores = value["item_scores"]
        if type(raw_scores) is not list:
            raise TypeError("serialized rubric item scores must be an exact list")
        serialized_awarded = _nonnegative_int(
            value["awarded_score"],
            "serialized rubric awarded score",
        )
        serialized_maximum = _positive_int(
            value["maximum_score"],
            "serialized rubric maximum score",
        )
        _matching_identifier(value["score_id"], _RUBRIC_SCORE_ID, "rubric score ID")
        sidecar = cls(
            assignment_attempt_id=value["assignment_attempt_id"],
            assignment_attempt_sha256=value["assignment_attempt_sha256"],
            rubric_record_id=value["rubric_record_id"],
            rubric_record_sha256=value["rubric_record_sha256"],
            rubric_lineage_id=value["rubric_lineage_id"],
            rubric_revision=value["rubric_revision"],
            rubric_content_sha256=value["rubric_content_sha256"],
            scoring_version=value["scoring_version"],
            score_unit=value["score_unit"],
            item_scores=tuple(RubricItemScoreV1.from_dict(item) for item in raw_scores),
            supersedes_score_id=value["supersedes_score_id"],
            supersedes_score_sha256=value["supersedes_score_sha256"],
            _construction_token=_SCORE_TOKEN,
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
        )
        if serialized_awarded != sidecar.awarded_score:
            raise ValueError("serialized awarded score differs from item scores")
        if serialized_maximum != sidecar.maximum_score:
            raise ValueError("serialized maximum score differs from item scores")
        if sidecar.as_dict() != value:
            raise ValueError("rubric score sidecar did not round-trip exactly")
        return sidecar

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> RubricScoreSidecarV1:
        return cls.from_dict(_canonical_object(raw, "rubric score sidecar"))


@dataclass(frozen=True, slots=True)
class RubricCorrectionV1:
    """In-memory result proving a correction derived new immutable artifacts."""

    source_rubric: RubricRevisionV1
    corrected_rubric: RubricRevisionV1
    source_score: RubricScoreSidecarV1
    corrected_score: RubricScoreSidecarV1

    def __post_init__(self) -> None:
        if type(self.source_rubric) is not RubricRevisionV1:
            raise TypeError("rubric correction source is invalid")
        if type(self.corrected_rubric) is not RubricRevisionV1:
            raise TypeError("corrected rubric is invalid")
        if type(self.source_score) is not RubricScoreSidecarV1:
            raise TypeError("rubric correction source score is invalid")
        if type(self.corrected_score) is not RubricScoreSidecarV1:
            raise TypeError("rubric correction derived score is invalid")
        self.source_rubric.rubric.validate_successor(self.corrected_rubric.rubric)
        self.source_score.validate_rubric(self.source_rubric)
        self.corrected_score.validate_rubric(self.corrected_rubric)
        self.corrected_score.validate_supersedes(self.source_score)


def create_rubric(content: RubricContentV1) -> RubricRevisionV1:
    """Create the first immutable revision of one rubric lineage."""

    if type(content) is not RubricContentV1:
        raise TypeError("rubric content must be exact RubricContentV1")
    if content.scoring_version != 1:
        raise ValueError("initial rubric scoring version must be one")
    return RubricRevisionV1(
        rubric=create_rubric_revision(content.sha256),
        content=content,
        lineage_content_sha256=(content.sha256,),
    )


def revise_rubric(
    predecessor: RubricRevisionV1,
    content: RubricContentV1,
) -> RubricRevisionV1:
    """Create an exact contiguous successor without changing its predecessor."""

    if type(predecessor) is not RubricRevisionV1:
        raise TypeError("rubric predecessor must be exact RubricRevisionV1")
    if type(content) is not RubricContentV1:
        raise TypeError("rubric content must be exact RubricContentV1")
    if content.scoring_version != predecessor.scoring_version + 1:
        raise ValueError("rubric scoring version must advance by exactly one")
    successor = RubricRevisionV1(
        rubric=create_rubric_revision(
            content.sha256,
            predecessor=predecessor.rubric,
        ),
        content=content,
        lineage_content_sha256=(
            *predecessor.lineage_content_sha256,
            content.sha256,
        ),
    )
    predecessor.rubric.validate_successor(successor.rubric)
    return successor


def score_attempt(
    *,
    assignment_attempt_id: str,
    assignment_attempt_sha256: str,
    rubric: RubricRevisionV1,
    item_scores: tuple[RubricItemScoreV1, ...],
    supersedes: RubricScoreSidecarV1 | None = None,
) -> RubricScoreSidecarV1:
    """Derive an immutable score sidecar from exact attempt and rubric inputs."""

    _matching_identifier(
        assignment_attempt_id,
        _ASSIGNMENT_ATTEMPT_ID,
        "assignment attempt ID",
    )
    _digest(assignment_attempt_sha256, "assignment attempt digest")
    if type(rubric) is not RubricRevisionV1:
        raise TypeError("rubric score requires an exact RubricRevisionV1")
    if type(item_scores) is not tuple or any(
        type(item) is not RubricItemScoreV1 for item in item_scores
    ):
        raise TypeError("rubric item scores must be an immutable typed tuple")
    expected_items = rubric.content.items
    if tuple(score.item_id for score in item_scores) != tuple(
        item.item_id for item in expected_items
    ):
        raise ValueError("score items must exactly match rubric item order")
    for definition, score in zip(expected_items, item_scores, strict=True):
        if score.maximum_score != definition.maximum_score:
            raise ValueError("score item maximum differs from its rubric definition")
        if definition.evidence_required and not score.evidence_event_ids:
            raise ValueError("rubric item requires at least one evidence event ID")

    if supersedes is not None:
        if type(supersedes) is not RubricScoreSidecarV1:
            raise TypeError("superseded score must be exact RubricScoreSidecarV1")
        if supersedes.assignment_attempt_id != assignment_attempt_id:
            raise ValueError("derived score changed assignment attempt ID")
        if supersedes.assignment_attempt_sha256 != assignment_attempt_sha256:
            raise ValueError("derived score changed assignment attempt digest")
        if supersedes.rubric_lineage_id != rubric.rubric.lineage_id:
            raise ValueError("derived score changed rubric lineage")
        if rubric.revision < supersedes.rubric_revision:
            raise ValueError("derived score cannot use an older rubric revision")

    return RubricScoreSidecarV1(
        assignment_attempt_id=assignment_attempt_id,
        assignment_attempt_sha256=assignment_attempt_sha256,
        rubric_record_id=rubric.rubric_id,
        rubric_record_sha256=rubric.record_sha256,
        rubric_lineage_id=rubric.rubric.lineage_id,
        rubric_revision=rubric.revision,
        rubric_content_sha256=rubric.content.sha256,
        scoring_version=rubric.scoring_version,
        score_unit=rubric.content.score_unit,
        item_scores=item_scores,
        supersedes_score_id=None if supersedes is None else supersedes.score_id,
        supersedes_score_sha256=None if supersedes is None else supersedes.sha256,
        _construction_token=_SCORE_TOKEN,
    )


def correct_rubric(
    predecessor: RubricRevisionV1,
    corrected_content: RubricContentV1,
    *,
    source_score: RubricScoreSidecarV1,
    corrected_item_scores: tuple[RubricItemScoreV1, ...],
) -> RubricCorrectionV1:
    """Create a corrected rubric revision and a superseding derived score.

    The source rubric, attempt, and score are only read.  The returned records bind
    their predecessors by exact IDs and canonical digests.
    """

    if type(predecessor) is not RubricRevisionV1:
        raise TypeError("rubric predecessor must be exact RubricRevisionV1")
    if type(source_score) is not RubricScoreSidecarV1:
        raise TypeError("source score must be exact RubricScoreSidecarV1")
    if source_score.rubric_record_id != predecessor.rubric_id:
        raise ValueError("source score does not bind the rubric being corrected")
    if source_score.rubric_record_sha256 != predecessor.record_sha256:
        raise ValueError("source score rubric digest differs")
    if source_score.rubric_content_sha256 != predecessor.content.sha256:
        raise ValueError("source score rubric content digest differs")
    corrected_rubric = revise_rubric(predecessor, corrected_content)
    corrected_score = score_attempt(
        assignment_attempt_id=source_score.assignment_attempt_id,
        assignment_attempt_sha256=source_score.assignment_attempt_sha256,
        rubric=corrected_rubric,
        item_scores=corrected_item_scores,
        supersedes=source_score,
    )
    return RubricCorrectionV1(
        source_rubric=predecessor,
        corrected_rubric=corrected_rubric,
        source_score=source_score,
        corrected_score=corrected_score,
    )


__all__ = [
    "RUBRIC_CONTENT_SCHEMA_ID",
    "RUBRIC_CONTENT_SCHEMA_VERSION",
    "RUBRIC_ITEM_SCHEMA_ID",
    "RUBRIC_ITEM_SCHEMA_VERSION",
    "RUBRIC_ITEM_SCORE_SCHEMA_ID",
    "RUBRIC_ITEM_SCORE_SCHEMA_VERSION",
    "RUBRIC_REVISION_SCHEMA_ID",
    "RUBRIC_REVISION_SCHEMA_VERSION",
    "RUBRIC_SCORE_SIDECAR_SCHEMA_ID",
    "RUBRIC_SCORE_SIDECAR_SCHEMA_VERSION",
    "RubricContentV1",
    "RubricCorrectionV1",
    "RubricItemScoreV1",
    "RubricItemV1",
    "RubricRevisionV1",
    "RubricScoreSidecarV1",
    "correct_rubric",
    "create_rubric",
    "revise_rubric",
    "score_attempt",
]
