"""Pseudonymous profile and immutable instructor-record vocabulary.

WO37-A separates the stable identities used by append-only evidence from direct
identity.  The two profile records in this module therefore contain only opaque,
role-scoped pseudonymous IDs.  Names, email addresses, institutional identifiers,
and other direct identity belong exclusively to the separately erasable local
identity mapping.

The remaining seven public types are intentionally small revision envelopes.  They
reserve a versioned, content-addressed lineage for later work orders without
prematurely defining assignment, review, cohort, or study behavior.  A revision can
only be created through its type-specific builder, and a successor commits to the
exact ID and canonical digest of its predecessor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import InitVar, dataclass, field
from enum import Enum
from typing import ClassVar, TypeVar

from kirby2.pseudonyms import (
    OPAQUE_PROFILE_ENTROPY_MAX_BYTES,
    OPAQUE_PROFILE_ENTROPY_MIN_BYTES,
    derive_instructor_profile_id,
    derive_learner_profile_id,
    require_instructor_profile_id,
    require_learner_profile_id,
    require_profile_id,
)


INSTRUCTOR_PROFILE_SCHEMA_ID = "KIRBY2_INSTRUCTOR_PROFILE_V1"
INSTRUCTOR_PROFILE_SCHEMA_VERSION = 1
LEARNER_PROFILE_SCHEMA_ID = "KIRBY2_LEARNER_PROFILE_V1"
LEARNER_PROFILE_SCHEMA_VERSION = 1
ASSIGNMENT_SCHEMA_ID = "KIRBY2_ASSIGNMENT_V1"
ASSIGNMENT_SCHEMA_VERSION = 1
ASSIGNMENT_ATTEMPT_SCHEMA_ID = "KIRBY2_ASSIGNMENT_ATTEMPT_V1"
ASSIGNMENT_ATTEMPT_SCHEMA_VERSION = 1
REVIEW_ANNOTATION_SCHEMA_ID = "KIRBY2_REVIEW_ANNOTATION_V1"
REVIEW_ANNOTATION_SCHEMA_VERSION = 1
RUBRIC_SCHEMA_ID = "KIRBY2_RUBRIC_V1"
RUBRIC_SCHEMA_VERSION = 1
CURRICULUM_PLAN_SCHEMA_ID = "KIRBY2_CURRICULUM_PLAN_V1"
CURRICULUM_PLAN_SCHEMA_VERSION = 1
COHORT_SCHEMA_ID = "KIRBY2_COHORT_V1"
COHORT_SCHEMA_VERSION = 1
RESEARCH_STUDY_SCHEMA_ID = "KIRBY2_RESEARCH_STUDY_V1"
RESEARCH_STUDY_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION_TOKEN = object()
# This is a live-object mutation tripwire, not a durable signature.  Persisted
# envelopes still rely on their content IDs, predecessor digests, and owning store.
_REVISION_INTEGRITY_KEY = secrets.token_bytes(32)


class ProfileKind(str, Enum):
    """Closed role namespace for pseudonymous local profiles."""

    INSTRUCTOR = "INSTRUCTOR"
    LEARNER = "LEARNER"


class InstructorRecordKind(str, Enum):
    """Closed WO37 model vocabulary beyond the two profile types."""

    ASSIGNMENT = "ASSIGNMENT"
    ASSIGNMENT_ATTEMPT = "ASSIGNMENT_ATTEMPT"
    REVIEW_ANNOTATION = "REVIEW_ANNOTATION"
    RUBRIC = "RUBRIC"
    CURRICULUM_PLAN = "CURRICULUM_PLAN"
    COHORT = "COHORT"
    RESEARCH_STUDY = "RESEARCH_STUDY"


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


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _initial_lineage_id(
    *,
    record_prefix: str,
    record_kind: InstructorRecordKind,
    schema_id: str,
    schema_version: int,
    content_sha256: str,
) -> str:
    """Derive the lineage commitment carried by an initial revision."""

    lineage_digest = _canonical_sha256(
        {
            "initial_content_sha256": content_sha256,
            "record_kind": record_kind.value,
            "schema_id": schema_id,
            "schema_version": schema_version,
        }
    )
    return f"{record_prefix}lineage-{lineage_digest}"


def _revision_integrity_commitment(record: _RevisionEnvelope) -> str:
    """Bind one live revision instance to its factory-created field values."""

    return hmac.new(
        _REVISION_INTEGRITY_KEY,
        _canonical_json_bytes(record.as_dict()),
        hashlib.sha256,
    ).hexdigest()


def _require_revision_integrity(record: _RevisionEnvelope) -> None:
    stored = getattr(record, "_integrity_commitment", None)
    if type(stored) is not str or not hmac.compare_digest(
        stored,
        _revision_integrity_commitment(record),
    ):
        raise ValueError("instructor revision live integrity commitment differs")


def profile_kind_for_id(profile_id: object) -> ProfileKind:
    """Resolve the closed role encoded by a pseudonymous profile namespace."""

    value = require_profile_id(profile_id)
    if value.startswith("instructor-profile-"):
        return ProfileKind.INSTRUCTOR
    return ProfileKind.LEARNER


def _opaque_profile_id(kind: ProfileKind, opaque_entropy: bytes) -> str:
    if type(kind) is not ProfileKind:
        raise TypeError("profile kind must be ProfileKind")
    if kind is ProfileKind.INSTRUCTOR:
        return derive_instructor_profile_id(opaque_entropy)
    return derive_learner_profile_id(opaque_entropy)


@dataclass(frozen=True, slots=True)
class InstructorProfile:
    """Immutable pseudonymous instructor identity used by evidence records."""

    profile_id: str
    profile_kind: ProfileKind = ProfileKind.INSTRUCTOR
    schema_id: str = INSTRUCTOR_PROFILE_SCHEMA_ID
    schema_version: int = INSTRUCTOR_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_instructor_profile_id(self.profile_id)
        if type(self.profile_kind) is not ProfileKind:
            raise TypeError("instructor profile kind must be ProfileKind")
        if self.profile_kind is not ProfileKind.INSTRUCTOR:
            raise ValueError("instructor profile kind changed")
        if (
            type(self.schema_id) is not str
            or self.schema_id != INSTRUCTOR_PROFILE_SCHEMA_ID
        ):
            raise ValueError("instructor profile schema ID changed")
        if (
            type(self.schema_version) is not int
            or self.schema_version != INSTRUCTOR_PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("instructor profile schema version changed")

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_kind": self.profile_kind.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def profile_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class LearnerProfile:
    """Immutable pseudonymous learner identity used by evidence records."""

    profile_id: str
    profile_kind: ProfileKind = ProfileKind.LEARNER
    schema_id: str = LEARNER_PROFILE_SCHEMA_ID
    schema_version: int = LEARNER_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_learner_profile_id(self.profile_id)
        if type(self.profile_kind) is not ProfileKind:
            raise TypeError("learner profile kind must be ProfileKind")
        if self.profile_kind is not ProfileKind.LEARNER:
            raise ValueError("learner profile kind changed")
        if (
            type(self.schema_id) is not str
            or self.schema_id != LEARNER_PROFILE_SCHEMA_ID
        ):
            raise ValueError("learner profile schema ID changed")
        if (
            type(self.schema_version) is not int
            or self.schema_version != LEARNER_PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("learner profile schema version changed")

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "profile_kind": self.profile_kind.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def profile_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def create_instructor_profile(opaque_entropy: bytes) -> InstructorProfile:
    """Create an instructor pseudonym from caller-supplied opaque entropy.

    The caller must obtain this entropy independently of direct identity.  The local
    identity service uses system entropy; deterministic audits may inject fixed bytes.
    """

    return InstructorProfile(
        profile_id=_opaque_profile_id(ProfileKind.INSTRUCTOR, opaque_entropy)
    )


def create_learner_profile(opaque_entropy: bytes) -> LearnerProfile:
    """Create a learner pseudonym from caller-supplied opaque entropy.

    The caller must obtain this entropy independently of direct identity.  The local
    identity service uses system entropy; deterministic audits may inject fixed bytes.
    """

    return LearnerProfile(
        profile_id=_opaque_profile_id(ProfileKind.LEARNER, opaque_entropy)
    )


@dataclass(frozen=True, slots=True)
class _RevisionEnvelope:
    """Private mechanics shared by the seven future-facing WO37 records."""

    lineage_id: str
    revision: int
    content_sha256: str
    predecessor_record_id: str | None
    predecessor_sha256: str | None
    _construction_token: InitVar[object]
    record_id: str = field(init=False)
    _integrity_commitment: str = field(init=False, repr=False, compare=False)

    _SCHEMA_ID: ClassVar[str]
    _SCHEMA_VERSION: ClassVar[int] = 1
    _RECORD_KIND: ClassVar[InstructorRecordKind]
    _RECORD_PREFIX: ClassVar[str]

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _REVISION_TOKEN:
            raise TypeError("instructor revisions require a type-specific builder")
        if type(self._RECORD_KIND) is not InstructorRecordKind:
            raise TypeError("instructor revision kind is invalid")
        expected_lineage = re.compile(
            rf"^{re.escape(self._RECORD_PREFIX)}lineage-[0-9a-f]{{64}}$"
        )
        if type(self.lineage_id) is not str or expected_lineage.fullmatch(
            self.lineage_id
        ) is None:
            raise ValueError(f"{self._RECORD_KIND.value} lineage ID is invalid")
        _require_positive_int(self.revision, "instructor revision")
        _require_sha256(self.content_sha256, "instructor revision content digest")
        if (self.predecessor_record_id is None) != (
            self.predecessor_sha256 is None
        ):
            raise ValueError("predecessor ID and digest must travel together")
        if self.revision == 1:
            if self.predecessor_record_id is not None:
                raise ValueError("first instructor revision cannot have a predecessor")
            expected_initial_lineage = _initial_lineage_id(
                record_prefix=self._RECORD_PREFIX,
                record_kind=self._RECORD_KIND,
                schema_id=self._SCHEMA_ID,
                schema_version=self._SCHEMA_VERSION,
                content_sha256=self.content_sha256,
            )
            if self.lineage_id != expected_initial_lineage:
                raise ValueError(
                    "first instructor revision lineage does not commit to its "
                    "initial content"
                )
        else:
            expected_record = re.compile(
                rf"^{re.escape(self._RECORD_PREFIX)}[0-9a-f]{{64}}$"
            )
            if (
                type(self.predecessor_record_id) is not str
                or expected_record.fullmatch(self.predecessor_record_id) is None
            ):
                raise ValueError("predecessor record ID is invalid")
            _require_sha256(
                self.predecessor_sha256,
                "predecessor record canonical digest",
            )
        object.__setattr__(
            self,
            "record_id",
            self._RECORD_PREFIX + _canonical_sha256(self.identity_dict()),
        )
        object.__setattr__(
            self,
            "_integrity_commitment",
            _revision_integrity_commitment(self),
        )

    @property
    def schema_id(self) -> str:
        return self._SCHEMA_ID

    @property
    def schema_version(self) -> int:
        return self._SCHEMA_VERSION

    @property
    def record_kind(self) -> InstructorRecordKind:
        return self._RECORD_KIND

    def identity_dict(self) -> dict[str, object]:
        return {
            "content_sha256": self.content_sha256,
            "lineage_id": self.lineage_id,
            "predecessor_record_id": self.predecessor_record_id,
            "predecessor_sha256": self.predecessor_sha256,
            "record_kind": self.record_kind.value,
            "revision": self.revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "record_id": self.record_id}

    def canonical_bytes(self) -> bytes:
        _require_revision_integrity(self)
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def validate_successor(self, successor: _RevisionEnvelope) -> None:
        """Prove an exact contiguous, content-changing successor relation."""

        _require_revision_integrity(self)
        if type(successor) is not type(self):
            raise TypeError("successor must have the exact predecessor record type")
        _require_revision_integrity(successor)
        if successor.lineage_id != self.lineage_id:
            raise ValueError("successor changed instructor-record lineage")
        if successor.revision != self.revision + 1:
            raise ValueError("successor revision must advance by exactly one")
        if successor.predecessor_record_id != self.record_id:
            raise ValueError("successor does not bind predecessor record ID")
        if successor.predecessor_sha256 != self.sha256:
            raise ValueError("successor does not bind predecessor canonical digest")
        if successor.content_sha256 == self.content_sha256:
            raise ValueError("successor must commit to changed content")


@dataclass(frozen=True, slots=True)
class Assignment(_RevisionEnvelope):
    """WO37-B assignment payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = ASSIGNMENT_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = ASSIGNMENT_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = InstructorRecordKind.ASSIGNMENT
    _RECORD_PREFIX: ClassVar[str] = "assignment-"

    @property
    def assignment_id(self) -> str:
        return self.record_id


@dataclass(frozen=True, slots=True)
class AssignmentAttempt(_RevisionEnvelope):
    """WO37-B attempt payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = ASSIGNMENT_ATTEMPT_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = ASSIGNMENT_ATTEMPT_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = (
        InstructorRecordKind.ASSIGNMENT_ATTEMPT
    )
    _RECORD_PREFIX: ClassVar[str] = "assignment-attempt-"

    @property
    def assignment_attempt_id(self) -> str:
        return self.record_id

    @property
    def attempt_id(self) -> str:
        return self.record_id


@dataclass(frozen=True, slots=True)
class ReviewAnnotation(_RevisionEnvelope):
    """WO37-B review payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = REVIEW_ANNOTATION_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = REVIEW_ANNOTATION_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = (
        InstructorRecordKind.REVIEW_ANNOTATION
    )
    _RECORD_PREFIX: ClassVar[str] = "review-annotation-"

    @property
    def review_annotation_id(self) -> str:
        return self.record_id

    @property
    def annotation_id(self) -> str:
        return self.record_id


@dataclass(frozen=True, slots=True)
class Rubric(_RevisionEnvelope):
    """WO37-B rubric payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = RUBRIC_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = RUBRIC_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = InstructorRecordKind.RUBRIC
    _RECORD_PREFIX: ClassVar[str] = "rubric-"

    @property
    def rubric_id(self) -> str:
        return self.record_id


@dataclass(frozen=True, slots=True)
class CurriculumPlan(_RevisionEnvelope):
    """WO37-B curriculum payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = CURRICULUM_PLAN_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = CURRICULUM_PLAN_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = (
        InstructorRecordKind.CURRICULUM_PLAN
    )
    _RECORD_PREFIX: ClassVar[str] = "curriculum-plan-"

    @property
    def curriculum_plan_id(self) -> str:
        return self.record_id

    @property
    def plan_id(self) -> str:
        return self.record_id


@dataclass(frozen=True, slots=True)
class Cohort(_RevisionEnvelope):
    """WO37-C cohort payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = COHORT_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = COHORT_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = InstructorRecordKind.COHORT
    _RECORD_PREFIX: ClassVar[str] = "cohort-"

    @property
    def cohort_id(self) -> str:
        return self.record_id


@dataclass(frozen=True, slots=True)
class ResearchStudy(_RevisionEnvelope):
    """WO37-C study payload commitment and immutable lineage envelope."""

    _SCHEMA_ID: ClassVar[str] = RESEARCH_STUDY_SCHEMA_ID
    _SCHEMA_VERSION: ClassVar[int] = RESEARCH_STUDY_SCHEMA_VERSION
    _RECORD_KIND: ClassVar[InstructorRecordKind] = InstructorRecordKind.RESEARCH_STUDY
    _RECORD_PREFIX: ClassVar[str] = "research-study-"

    @property
    def research_study_id(self) -> str:
        return self.record_id

    @property
    def study_id(self) -> str:
        return self.record_id


INSTRUCTOR_RECORD_TYPES = (
    InstructorProfile,
    LearnerProfile,
    Assignment,
    AssignmentAttempt,
    ReviewAnnotation,
    Rubric,
    CurriculumPlan,
    Cohort,
    ResearchStudy,
)


_RevisionT = TypeVar("_RevisionT", bound=_RevisionEnvelope)


def _create_revision(
    record_type: type[_RevisionT],
    content_sha256: str,
    *,
    predecessor: _RevisionT | None,
) -> _RevisionT:
    content_digest = _require_sha256(
        content_sha256,
        f"{record_type._RECORD_KIND.value} content digest",
    )
    if predecessor is None:
        lineage_id = _initial_lineage_id(
            record_prefix=record_type._RECORD_PREFIX,
            record_kind=record_type._RECORD_KIND,
            schema_id=record_type._SCHEMA_ID,
            schema_version=record_type._SCHEMA_VERSION,
            content_sha256=content_digest,
        )
        return record_type(
            lineage_id=lineage_id,
            revision=1,
            content_sha256=content_digest,
            predecessor_record_id=None,
            predecessor_sha256=None,
            _construction_token=_REVISION_TOKEN,
        )
    if type(predecessor) is not record_type:
        raise TypeError("revision predecessor has the wrong exact record type")
    _require_revision_integrity(predecessor)
    rebuilt_predecessor = record_type(
        lineage_id=predecessor.lineage_id,
        revision=predecessor.revision,
        content_sha256=predecessor.content_sha256,
        predecessor_record_id=predecessor.predecessor_record_id,
        predecessor_sha256=predecessor.predecessor_sha256,
        _construction_token=_REVISION_TOKEN,
    )
    if (
        rebuilt_predecessor != predecessor
        or rebuilt_predecessor.canonical_bytes() != predecessor.canonical_bytes()
    ):
        raise ValueError("revision predecessor differs from its canonical identity")
    predecessor = rebuilt_predecessor
    if content_digest == predecessor.content_sha256:
        raise ValueError("successor must commit to changed content")
    successor = record_type(
        lineage_id=predecessor.lineage_id,
        revision=predecessor.revision + 1,
        content_sha256=content_digest,
        predecessor_record_id=predecessor.record_id,
        predecessor_sha256=predecessor.sha256,
        _construction_token=_REVISION_TOKEN,
    )
    predecessor.validate_successor(successor)
    return successor


def create_assignment_revision(
    content_sha256: str,
    *,
    predecessor: Assignment | None = None,
) -> Assignment:
    return _create_revision(Assignment, content_sha256, predecessor=predecessor)


def create_assignment_attempt_revision(
    content_sha256: str,
    *,
    predecessor: AssignmentAttempt | None = None,
) -> AssignmentAttempt:
    return _create_revision(
        AssignmentAttempt,
        content_sha256,
        predecessor=predecessor,
    )


def create_review_annotation_revision(
    content_sha256: str,
    *,
    predecessor: ReviewAnnotation | None = None,
) -> ReviewAnnotation:
    return _create_revision(
        ReviewAnnotation,
        content_sha256,
        predecessor=predecessor,
    )


def create_rubric_revision(
    content_sha256: str,
    *,
    predecessor: Rubric | None = None,
) -> Rubric:
    return _create_revision(Rubric, content_sha256, predecessor=predecessor)


def create_curriculum_plan_revision(
    content_sha256: str,
    *,
    predecessor: CurriculumPlan | None = None,
) -> CurriculumPlan:
    return _create_revision(CurriculumPlan, content_sha256, predecessor=predecessor)


def create_cohort_revision(
    content_sha256: str,
    *,
    predecessor: Cohort | None = None,
) -> Cohort:
    return _create_revision(Cohort, content_sha256, predecessor=predecessor)


def create_research_study_revision(
    content_sha256: str,
    *,
    predecessor: ResearchStudy | None = None,
) -> ResearchStudy:
    return _create_revision(ResearchStudy, content_sha256, predecessor=predecessor)


__all__ = [
    "ASSIGNMENT_ATTEMPT_SCHEMA_ID",
    "ASSIGNMENT_ATTEMPT_SCHEMA_VERSION",
    "ASSIGNMENT_SCHEMA_ID",
    "ASSIGNMENT_SCHEMA_VERSION",
    "COHORT_SCHEMA_ID",
    "COHORT_SCHEMA_VERSION",
    "CURRICULUM_PLAN_SCHEMA_ID",
    "CURRICULUM_PLAN_SCHEMA_VERSION",
    "INSTRUCTOR_PROFILE_SCHEMA_ID",
    "INSTRUCTOR_PROFILE_SCHEMA_VERSION",
    "INSTRUCTOR_RECORD_TYPES",
    "LEARNER_PROFILE_SCHEMA_ID",
    "LEARNER_PROFILE_SCHEMA_VERSION",
    "OPAQUE_PROFILE_ENTROPY_MAX_BYTES",
    "OPAQUE_PROFILE_ENTROPY_MIN_BYTES",
    "RESEARCH_STUDY_SCHEMA_ID",
    "RESEARCH_STUDY_SCHEMA_VERSION",
    "REVIEW_ANNOTATION_SCHEMA_ID",
    "REVIEW_ANNOTATION_SCHEMA_VERSION",
    "RUBRIC_SCHEMA_ID",
    "RUBRIC_SCHEMA_VERSION",
    "Assignment",
    "AssignmentAttempt",
    "Cohort",
    "CurriculumPlan",
    "InstructorProfile",
    "InstructorRecordKind",
    "LearnerProfile",
    "ProfileKind",
    "ResearchStudy",
    "ReviewAnnotation",
    "Rubric",
    "create_assignment_attempt_revision",
    "create_assignment_revision",
    "create_cohort_revision",
    "create_curriculum_plan_revision",
    "create_instructor_profile",
    "create_learner_profile",
    "create_research_study_revision",
    "create_review_annotation_revision",
    "create_rubric_revision",
    "profile_kind_for_id",
    "require_instructor_profile_id",
    "require_learner_profile_id",
    "require_profile_id",
]
