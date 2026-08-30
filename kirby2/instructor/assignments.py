"""Immutable assignment plans and lock-enforced attempt manifests.

This module is deliberately a pure contract layer.  It never reads a wall clock,
chooses a lesson, changes simulator settings, or mutates a completed run.  Callers
provide every decision explicitly; the builders bind those decisions to canonical
content and reject an attempt before its manifest exists when any locked runtime
value differs from the assignment.

Consent fields are pseudonymous evidence bindings and local policy inputs.  They
are not a representation of anonymity, legal compliance, or human-subjects
approval.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import ClassVar

from .consent import ConsentDecisionStatusV1, ConsentScopeV1, ConsentStateV1
from .models import (
    Assignment,
    AssignmentAttempt,
    create_assignment_attempt_revision,
    create_assignment_revision,
    require_learner_profile_id,
)


ASSIGNMENT_REVISION_SCHEMA_ID = "KIRBY2_ASSIGNMENT_REVISION_V1"
ASSIGNMENT_REVISION_SCHEMA_VERSION = 1
ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_ID = "KIRBY2_ASSIGNMENT_ATTEMPT_MANIFEST_V1"
ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONSENT_ID = re.compile(r"consent-[0-9a-f]{24}\Z")
_CONSENT_DECISION_ID = re.compile(r"consent-decision-[0-9a-f]{24}\Z")
_MAX_SEED = (1 << 63) - 1


class AssignmentTargetKindV1(str, Enum):
    LESSON = "LESSON"
    LESSON_POOL = "LESSON_POOL"


class AssignmentModeV1(str, Enum):
    GUIDED_PRACTICE = "GUIDED_PRACTICE"
    INDEPENDENT_PRACTICE = "INDEPENDENT_PRACTICE"
    ASSESSMENT = "ASSESSMENT"
    RESEARCH = "RESEARCH"


class SeedPolicyKindV1(str, Enum):
    FIXED = "FIXED"
    ASSIGNMENT_ATTEMPT_DERIVED = "ASSIGNMENT_ATTEMPT_DERIVED"
    INSTRUCTOR_PROVIDED = "INSTRUCTOR_PROVIDED"


class StrategyPolicyV1(str, Enum):
    PRESCRIBED = "PRESCRIBED"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    HIDDEN_UNTIL_REVIEW = "HIDDEN_UNTIL_REVIEW"


class FeedbackTimingV1(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    AFTER_ATTEMPT = "AFTER_ATTEMPT"
    AFTER_REVIEW = "AFTER_REVIEW"
    AFTER_DEADLINE = "AFTER_DEADLINE"
    NEVER = "NEVER"


class HiddenStateRevealPolicyV1(str, Enum):
    NEVER = "NEVER"
    AFTER_ATTEMPT = "AFTER_ATTEMPT"
    AFTER_REVIEW_COMPLETION = "AFTER_REVIEW_COMPLETION"
    INSTRUCTOR_ONLY = "INSTRUCTOR_ONLY"


class DeadlineEnforcementClaimV1(str, Enum):
    NOT_PRESENT = "NOT_PRESENT"
    METADATA_ONLY_NO_ENFORCEMENT = "METADATA_ONLY_NO_ENFORCEMENT"
    AUTHORIZED_RECORDED_CLOCK_POLICY = "AUTHORIZED_RECORDED_CLOCK_POLICY"


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


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if type(value) is not dict or _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return value


def _fields(value: object, expected: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} fields differ")
    return value


def _text(
    value: object,
    label: str,
    *,
    maximum_utf8_bytes: int = 512,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty text without edge whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use NFC Unicode normalization")
    if len(value.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{label} is too long")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _seed(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SEED:
        raise ValueError(f"{label} must be an integer from 0 through {_MAX_SEED}")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _utc(value: object, label: str) -> str:
    text = _text(value, label, maximum_utf8_bytes=20)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        raise ValueError(f"{label} must use canonical UTC seconds ending in Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a valid UTC timestamp") from error
    return text


def _enum_text(value: object, label: str) -> str:
    return _text(value, label, maximum_utf8_bytes=96)


def _text_array(
    value: object,
    label: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    items = tuple(_text(item, f"{label} item") for item in value)
    if not allow_empty and not items:
        raise ValueError(f"{label} cannot be empty")
    if len(items) != len(set(items)):
        raise ValueError(f"{label} cannot contain duplicates")
    return items


@dataclass(frozen=True, slots=True)
class LessonReferenceV1:
    lesson_id: str
    lesson_sha256: str

    def __post_init__(self) -> None:
        _text(self.lesson_id, "lesson ID")
        _sha256(self.lesson_sha256, "lesson digest")

    def as_dict(self) -> dict[str, object]:
        return {"lesson_id": self.lesson_id, "lesson_sha256": self.lesson_sha256}

    @classmethod
    def from_dict(cls, value: object) -> LessonReferenceV1:
        payload = _fields(value, {"lesson_id", "lesson_sha256"}, "lesson reference")
        return cls(
            lesson_id=_text(payload["lesson_id"], "lesson ID"),
            lesson_sha256=_sha256(payload["lesson_sha256"], "lesson digest"),
        )


@dataclass(frozen=True, slots=True)
class AssignmentTargetV1:
    kind: AssignmentTargetKindV1
    lessons: tuple[LessonReferenceV1, ...]

    def __post_init__(self) -> None:
        if type(self.kind) is not AssignmentTargetKindV1:
            raise TypeError("assignment target kind must be AssignmentTargetKindV1")
        if type(self.lessons) is not tuple or not self.lessons:
            raise ValueError("assignment target requires at least one lesson")
        if any(type(item) is not LessonReferenceV1 for item in self.lessons):
            raise TypeError("assignment target lessons must be LessonReferenceV1 values")
        if self.kind is AssignmentTargetKindV1.LESSON and len(self.lessons) != 1:
            raise ValueError("a LESSON target must bind exactly one lesson")
        identities = tuple((item.lesson_id, item.lesson_sha256) for item in self.lessons)
        if len(identities) != len(set(identities)):
            raise ValueError("assignment target cannot contain duplicate lesson references")
        if len({item.lesson_id for item in self.lessons}) != len(self.lessons):
            raise ValueError("one lesson ID cannot bind multiple digests in a target")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "lessons": [item.as_dict() for item in self.lessons],
        }

    @classmethod
    def from_dict(cls, value: object) -> AssignmentTargetV1:
        payload = _fields(value, {"kind", "lessons"}, "assignment target")
        raw_lessons = payload["lessons"]
        if type(raw_lessons) is not list:
            raise TypeError("assignment target lessons must be an array")
        return cls(
            kind=AssignmentTargetKindV1(
                _enum_text(payload["kind"], "assignment target kind")
            ),
            lessons=tuple(LessonReferenceV1.from_dict(item) for item in raw_lessons),
        )

    def require_selected(self, selected: LessonReferenceV1) -> None:
        if type(selected) is not LessonReferenceV1:
            raise TypeError("selected lesson must be LessonReferenceV1")
        if selected not in self.lessons:
            raise ValueError("selected lesson is outside the exact assignment target")


@dataclass(frozen=True, slots=True)
class SeedPolicyV1:
    kind: SeedPolicyKindV1
    fixed_seed: int | None
    derivation_namespace: str | None

    def __post_init__(self) -> None:
        if type(self.kind) is not SeedPolicyKindV1:
            raise TypeError("seed policy kind must be SeedPolicyKindV1")
        if self.kind is SeedPolicyKindV1.FIXED:
            _seed(self.fixed_seed, "fixed seed")
            if self.derivation_namespace is not None:
                raise ValueError("a fixed seed policy cannot have a derivation namespace")
        elif self.kind is SeedPolicyKindV1.ASSIGNMENT_ATTEMPT_DERIVED:
            if self.fixed_seed is not None:
                raise ValueError("a derived seed policy cannot have a fixed seed")
            _text(self.derivation_namespace, "seed derivation namespace")
        elif self.fixed_seed is not None or self.derivation_namespace is not None:
            raise ValueError("an instructor-provided seed policy has no fixed inputs")

    def as_dict(self) -> dict[str, object]:
        return {
            "derivation_namespace": self.derivation_namespace,
            "fixed_seed": self.fixed_seed,
            "kind": self.kind.value,
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> SeedPolicyV1:
        payload = _fields(
            value,
            {"derivation_namespace", "fixed_seed", "kind"},
            "seed policy",
        )
        fixed_seed = payload["fixed_seed"]
        namespace = payload["derivation_namespace"]
        return cls(
            kind=SeedPolicyKindV1(_enum_text(payload["kind"], "seed policy kind")),
            fixed_seed=None if fixed_seed is None else _seed(fixed_seed, "fixed seed"),
            derivation_namespace=(
                None
                if namespace is None
                else _text(namespace, "seed derivation namespace")
            ),
        )

    def expected_seed(
        self,
        *,
        assignment_lineage_id: str,
        assignment_revision: int,
        learner_profile_id: str,
        attempt_number: int,
    ) -> int | None:
        if self.kind is SeedPolicyKindV1.FIXED:
            return self.fixed_seed
        if self.kind is SeedPolicyKindV1.INSTRUCTOR_PROVIDED:
            return None
        digest = hashlib.sha256(
            _canonical_json_bytes(
                {
                    "assignment_lineage_id": _text(
                        assignment_lineage_id,
                        "assignment lineage ID",
                    ),
                    "assignment_revision": _positive_int(
                        assignment_revision,
                        "assignment revision",
                    ),
                    "attempt_number": _positive_int(attempt_number, "attempt number"),
                    "derivation_namespace": self.derivation_namespace,
                    "learner_profile_id": require_learner_profile_id(
                        learner_profile_id
                    ),
                    "seed_derivation": "KIRBY2_ASSIGNMENT_ATTEMPT_SEED_V1",
                }
            )
        ).digest()
        return int.from_bytes(digest[:8], "big") & _MAX_SEED


@dataclass(frozen=True, slots=True)
class HotkeyLayoutBindingV1:
    layout_name: str
    layout_sha256: str

    def __post_init__(self) -> None:
        _text(self.layout_name, "hotkey layout name")
        _sha256(self.layout_sha256, "hotkey layout digest")

    def as_dict(self) -> dict[str, object]:
        return {"layout_name": self.layout_name, "layout_sha256": self.layout_sha256}

    @classmethod
    def from_dict(cls, value: object) -> HotkeyLayoutBindingV1:
        payload = _fields(
            value,
            {"layout_name", "layout_sha256"},
            "hotkey layout binding",
        )
        return cls(
            layout_name=_text(payload["layout_name"], "hotkey layout name"),
            layout_sha256=_sha256(payload["layout_sha256"], "hotkey layout digest"),
        )


@dataclass(frozen=True, slots=True)
class RubricBindingV1:
    rubric_record_id: str
    rubric_sha256: str
    rubric_version: int

    def __post_init__(self) -> None:
        _text(self.rubric_record_id, "rubric record ID")
        _sha256(self.rubric_sha256, "rubric digest")
        _positive_int(self.rubric_version, "rubric version")

    def as_dict(self) -> dict[str, object]:
        return {
            "rubric_record_id": self.rubric_record_id,
            "rubric_sha256": self.rubric_sha256,
            "rubric_version": self.rubric_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> RubricBindingV1:
        payload = _fields(
            value,
            {"rubric_record_id", "rubric_sha256", "rubric_version"},
            "rubric binding",
        )
        return cls(
            rubric_record_id=_text(payload["rubric_record_id"], "rubric record ID"),
            rubric_sha256=_sha256(payload["rubric_sha256"], "rubric digest"),
            rubric_version=_positive_int(payload["rubric_version"], "rubric version"),
        )


@dataclass(frozen=True, slots=True)
class ResearchConsentRequirementV1:
    authorization_policy_id: str
    evidence_purpose: str
    required_scopes: tuple[ConsentScopeV1, ...]

    def __post_init__(self) -> None:
        _text(self.authorization_policy_id, "consent authorization policy ID")
        _text(self.evidence_purpose, "research consent evidence purpose", maximum_utf8_bytes=2048)
        if type(self.required_scopes) is not tuple or not self.required_scopes:
            raise ValueError("research consent requirement needs at least one scope")
        if any(type(item) is not ConsentScopeV1 for item in self.required_scopes):
            raise TypeError("required consent scopes must be ConsentScopeV1 values")
        if len(self.required_scopes) != len(set(self.required_scopes)):
            raise ValueError("required consent scopes cannot contain duplicates")
        if ConsentScopeV1.INSTRUCTIONAL_EVIDENCE not in self.required_scopes:
            raise ValueError("assignment consent must require instructional evidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "authorization_policy_id": self.authorization_policy_id,
            "evidence_purpose": self.evidence_purpose,
            "required_scopes": [item.value for item in self.required_scopes],
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchConsentRequirementV1:
        payload = _fields(
            value,
            {"authorization_policy_id", "evidence_purpose", "required_scopes"},
            "research consent requirement",
        )
        raw_scopes = payload["required_scopes"]
        if type(raw_scopes) is not list:
            raise TypeError("required consent scopes must be an array")
        return cls(
            authorization_policy_id=_text(
                payload["authorization_policy_id"],
                "consent authorization policy ID",
            ),
            evidence_purpose=_text(
                payload["evidence_purpose"],
                "research consent evidence purpose",
                maximum_utf8_bytes=2048,
            ),
            required_scopes=tuple(
                ConsentScopeV1(_enum_text(item, "required consent scope"))
                for item in raw_scopes
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchConsentEvidenceV1:
    learner_profile_id: str
    consent_id: str
    consent_sha256: str
    consent_revision: int
    consent_state: ConsentStateV1
    granted_scopes: tuple[ConsentScopeV1, ...]
    authorization_policy_id: str
    authorization_decision_id: str
    authorization_decision_sha256: str
    authorization_status: ConsentDecisionStatusV1

    def __post_init__(self) -> None:
        require_learner_profile_id(self.learner_profile_id)
        if type(self.consent_id) is not str or _CONSENT_ID.fullmatch(self.consent_id) is None:
            raise ValueError("consent ID is invalid")
        _sha256(self.consent_sha256, "consent digest")
        _positive_int(self.consent_revision, "consent revision")
        if self.consent_state is not ConsentStateV1.GRANTED:
            raise ValueError("attempt consent evidence must bind a granted consent")
        if type(self.granted_scopes) is not tuple or not self.granted_scopes:
            raise ValueError("attempt consent evidence needs granted scopes")
        if any(type(item) is not ConsentScopeV1 for item in self.granted_scopes):
            raise TypeError("granted consent scopes must be ConsentScopeV1 values")
        if len(self.granted_scopes) != len(set(self.granted_scopes)):
            raise ValueError("granted consent scopes cannot contain duplicates")
        _text(self.authorization_policy_id, "consent authorization policy ID")
        if (
            type(self.authorization_decision_id) is not str
            or _CONSENT_DECISION_ID.fullmatch(self.authorization_decision_id) is None
        ):
            raise ValueError("consent authorization decision ID is invalid")
        _sha256(self.authorization_decision_sha256, "consent authorization decision digest")
        if self.authorization_status is not ConsentDecisionStatusV1.AUTHORIZED:
            raise ValueError("attempt consent authorization must be AUTHORIZED")

    def as_dict(self) -> dict[str, object]:
        return {
            "authorization_decision_id": self.authorization_decision_id,
            "authorization_decision_sha256": self.authorization_decision_sha256,
            "authorization_policy_id": self.authorization_policy_id,
            "authorization_status": self.authorization_status.value,
            "consent_id": self.consent_id,
            "consent_revision": self.consent_revision,
            "consent_sha256": self.consent_sha256,
            "consent_state": self.consent_state.value,
            "granted_scopes": [item.value for item in self.granted_scopes],
            "learner_profile_id": self.learner_profile_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResearchConsentEvidenceV1:
        payload = _fields(
            value,
            {
                "authorization_decision_id",
                "authorization_decision_sha256",
                "authorization_policy_id",
                "authorization_status",
                "consent_id",
                "consent_revision",
                "consent_sha256",
                "consent_state",
                "granted_scopes",
                "learner_profile_id",
            },
            "research consent evidence",
        )
        raw_scopes = payload["granted_scopes"]
        if type(raw_scopes) is not list:
            raise TypeError("granted consent scopes must be an array")
        return cls(
            learner_profile_id=require_learner_profile_id(payload["learner_profile_id"]),
            consent_id=_text(payload["consent_id"], "consent ID"),
            consent_sha256=_sha256(payload["consent_sha256"], "consent digest"),
            consent_revision=_positive_int(payload["consent_revision"], "consent revision"),
            consent_state=ConsentStateV1(_enum_text(payload["consent_state"], "consent state")),
            granted_scopes=tuple(
                ConsentScopeV1(_enum_text(item, "granted consent scope"))
                for item in raw_scopes
            ),
            authorization_policy_id=_text(
                payload["authorization_policy_id"],
                "consent authorization policy ID",
            ),
            authorization_decision_id=_text(
                payload["authorization_decision_id"],
                "consent authorization decision ID",
            ),
            authorization_decision_sha256=_sha256(
                payload["authorization_decision_sha256"],
                "consent authorization decision digest",
            ),
            authorization_status=ConsentDecisionStatusV1(
                _enum_text(payload["authorization_status"], "consent authorization status")
            ),
        )


@dataclass(frozen=True, slots=True)
class DeadlineMetadataV1:
    deadline_utc: str
    recorded_clock_id: str | None
    recorded_clock_sha256: str | None
    enforcement_policy_id: str | None
    enforcement_policy_sha256: str | None
    policy_authorized: bool

    def __post_init__(self) -> None:
        _utc(self.deadline_utc, "assignment deadline")
        _exact_bool(self.policy_authorized, "deadline policy authorization")
        values = (
            self.recorded_clock_id,
            self.recorded_clock_sha256,
            self.enforcement_policy_id,
            self.enforcement_policy_sha256,
        )
        if self.policy_authorized:
            if any(item is None for item in values):
                raise ValueError(
                    "deadline enforcement requires explicit recorded-clock and policy bindings"
                )
            _text(self.recorded_clock_id, "recorded clock ID")
            _sha256(self.recorded_clock_sha256, "recorded clock digest")
            _text(self.enforcement_policy_id, "deadline enforcement policy ID")
            _sha256(self.enforcement_policy_sha256, "deadline enforcement policy digest")
        elif any(item is not None for item in values):
            raise ValueError(
                "deadline clock/policy bindings require explicit policy authorization"
            )

    @property
    def enforcement_claim(self) -> DeadlineEnforcementClaimV1:
        if self.policy_authorized:
            return DeadlineEnforcementClaimV1.AUTHORIZED_RECORDED_CLOCK_POLICY
        return DeadlineEnforcementClaimV1.METADATA_ONLY_NO_ENFORCEMENT

    def as_dict(self) -> dict[str, object]:
        return {
            "deadline_utc": self.deadline_utc,
            "enforcement_claim": self.enforcement_claim.value,
            "enforcement_policy_id": self.enforcement_policy_id,
            "enforcement_policy_sha256": self.enforcement_policy_sha256,
            "policy_authorized": self.policy_authorized,
            "recorded_clock_id": self.recorded_clock_id,
            "recorded_clock_sha256": self.recorded_clock_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> DeadlineMetadataV1:
        payload = _fields(
            value,
            {
                "deadline_utc",
                "enforcement_claim",
                "enforcement_policy_id",
                "enforcement_policy_sha256",
                "policy_authorized",
                "recorded_clock_id",
                "recorded_clock_sha256",
            },
            "deadline metadata",
        )
        record = cls(
            deadline_utc=_utc(payload["deadline_utc"], "assignment deadline"),
            recorded_clock_id=(
                None
                if payload["recorded_clock_id"] is None
                else _text(payload["recorded_clock_id"], "recorded clock ID")
            ),
            recorded_clock_sha256=(
                None
                if payload["recorded_clock_sha256"] is None
                else _sha256(payload["recorded_clock_sha256"], "recorded clock digest")
            ),
            enforcement_policy_id=(
                None
                if payload["enforcement_policy_id"] is None
                else _text(payload["enforcement_policy_id"], "deadline enforcement policy ID")
            ),
            enforcement_policy_sha256=(
                None
                if payload["enforcement_policy_sha256"] is None
                else _sha256(
                    payload["enforcement_policy_sha256"],
                    "deadline enforcement policy digest",
                )
            ),
            policy_authorized=_exact_bool(
                payload["policy_authorized"],
                "deadline policy authorization",
            ),
        )
        if record.enforcement_claim is not DeadlineEnforcementClaimV1(
            _enum_text(payload["enforcement_claim"], "deadline enforcement claim")
        ):
            raise ValueError("deadline enforcement claim differs from its authorization")
        return record


@dataclass(frozen=True, slots=True)
class DeadlineClockEvidenceV1:
    observed_at_utc: str
    recorded_clock_id: str
    recorded_clock_sha256: str
    enforcement_policy_id: str
    enforcement_policy_sha256: str

    def __post_init__(self) -> None:
        _utc(self.observed_at_utc, "deadline clock observation")
        _text(self.recorded_clock_id, "recorded clock ID")
        _sha256(self.recorded_clock_sha256, "recorded clock digest")
        _text(self.enforcement_policy_id, "deadline enforcement policy ID")
        _sha256(self.enforcement_policy_sha256, "deadline enforcement policy digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "enforcement_policy_id": self.enforcement_policy_id,
            "enforcement_policy_sha256": self.enforcement_policy_sha256,
            "observed_at_utc": self.observed_at_utc,
            "recorded_clock_id": self.recorded_clock_id,
            "recorded_clock_sha256": self.recorded_clock_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> DeadlineClockEvidenceV1:
        payload = _fields(
            value,
            {
                "enforcement_policy_id",
                "enforcement_policy_sha256",
                "observed_at_utc",
                "recorded_clock_id",
                "recorded_clock_sha256",
            },
            "deadline clock evidence",
        )
        return cls(
            observed_at_utc=_utc(payload["observed_at_utc"], "deadline clock observation"),
            recorded_clock_id=_text(payload["recorded_clock_id"], "recorded clock ID"),
            recorded_clock_sha256=_sha256(
                payload["recorded_clock_sha256"],
                "recorded clock digest",
            ),
            enforcement_policy_id=_text(
                payload["enforcement_policy_id"],
                "deadline enforcement policy ID",
            ),
            enforcement_policy_sha256=_sha256(
                payload["enforcement_policy_sha256"],
                "deadline enforcement policy digest",
            ),
        )


@dataclass(frozen=True, slots=True)
class AssignmentLocksV1:
    latency_sha256: str
    volume_sha256: str
    liquidity_sha256: str
    strategy_sha256: str
    objective: str
    venue_count: int
    hidden_state_reveal_policy: HiddenStateRevealPolicyV1
    seed_policy: SeedPolicyV1

    def __post_init__(self) -> None:
        _sha256(self.latency_sha256, "latency digest")
        _sha256(self.volume_sha256, "volume digest")
        _sha256(self.liquidity_sha256, "liquidity digest")
        _sha256(self.strategy_sha256, "strategy digest")
        _text(self.objective, "assignment objective", maximum_utf8_bytes=8192)
        _positive_int(self.venue_count, "venue count")
        if type(self.hidden_state_reveal_policy) is not HiddenStateRevealPolicyV1:
            raise TypeError("hidden-state reveal policy must be HiddenStateRevealPolicyV1")
        if type(self.seed_policy) is not SeedPolicyV1:
            raise TypeError("seed policy must be SeedPolicyV1")

    def as_dict(self) -> dict[str, object]:
        return {
            "hidden_state_reveal_policy": self.hidden_state_reveal_policy.value,
            "latency_sha256": self.latency_sha256,
            "liquidity_sha256": self.liquidity_sha256,
            "objective": self.objective,
            "seed_policy": self.seed_policy.as_dict(),
            "strategy_sha256": self.strategy_sha256,
            "venue_count": self.venue_count,
            "volume_sha256": self.volume_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> AssignmentLocksV1:
        payload = _fields(
            value,
            {
                "hidden_state_reveal_policy",
                "latency_sha256",
                "liquidity_sha256",
                "objective",
                "seed_policy",
                "strategy_sha256",
                "venue_count",
                "volume_sha256",
            },
            "assignment locks",
        )
        return cls(
            latency_sha256=_sha256(payload["latency_sha256"], "latency digest"),
            volume_sha256=_sha256(payload["volume_sha256"], "volume digest"),
            liquidity_sha256=_sha256(payload["liquidity_sha256"], "liquidity digest"),
            strategy_sha256=_sha256(payload["strategy_sha256"], "strategy digest"),
            objective=_text(payload["objective"], "assignment objective", maximum_utf8_bytes=8192),
            venue_count=_positive_int(payload["venue_count"], "venue count"),
            hidden_state_reveal_policy=HiddenStateRevealPolicyV1(
                _enum_text(
                    payload["hidden_state_reveal_policy"],
                    "hidden-state reveal policy",
                )
            ),
            seed_policy=SeedPolicyV1.from_dict(payload["seed_policy"]),
        )


@dataclass(frozen=True, slots=True)
class AttemptRuntimeParametersV1:
    latency_sha256: str
    volume_sha256: str
    liquidity_sha256: str
    strategy_sha256: str
    objective: str
    venue_count: int
    hidden_state_reveal_policy: HiddenStateRevealPolicyV1
    seed_policy: SeedPolicyV1
    seed: int

    def __post_init__(self) -> None:
        self.lock_snapshot()
        _seed(self.seed, "attempt seed")

    def lock_snapshot(self) -> AssignmentLocksV1:
        return AssignmentLocksV1(
            latency_sha256=self.latency_sha256,
            volume_sha256=self.volume_sha256,
            liquidity_sha256=self.liquidity_sha256,
            strategy_sha256=self.strategy_sha256,
            objective=self.objective,
            venue_count=self.venue_count,
            hidden_state_reveal_policy=self.hidden_state_reveal_policy,
            seed_policy=self.seed_policy,
        )

    def as_dict(self) -> dict[str, object]:
        return {**self.lock_snapshot().as_dict(), "seed": self.seed}

    @classmethod
    def from_dict(cls, value: object) -> AttemptRuntimeParametersV1:
        payload = _fields(
            value,
            {
                "hidden_state_reveal_policy",
                "latency_sha256",
                "liquidity_sha256",
                "objective",
                "seed",
                "seed_policy",
                "strategy_sha256",
                "venue_count",
                "volume_sha256",
            },
            "attempt runtime parameters",
        )
        locks = AssignmentLocksV1.from_dict(
            {key: item for key, item in payload.items() if key != "seed"}
        )
        return cls(
            latency_sha256=locks.latency_sha256,
            volume_sha256=locks.volume_sha256,
            liquidity_sha256=locks.liquidity_sha256,
            strategy_sha256=locks.strategy_sha256,
            objective=locks.objective,
            venue_count=locks.venue_count,
            hidden_state_reveal_policy=locks.hidden_state_reveal_policy,
            seed_policy=locks.seed_policy,
            seed=_seed(payload["seed"], "attempt seed"),
        )


@dataclass(frozen=True, slots=True)
class AssignmentSpecV1:
    target: AssignmentTargetV1
    curriculum_sha256: str
    scenario_sha256: str
    pack_sha256: str
    allowed_scenario_variations: tuple[str, ...]
    locks: AssignmentLocksV1
    mode: AssignmentModeV1
    strategy_policy: StrategyPolicyV1
    hotkey_layout: HotkeyLayoutBindingV1
    attempt_limit: int
    deadline: DeadlineMetadataV1 | None
    feedback_timing: FeedbackTimingV1
    scoring_version: str
    rubric: RubricBindingV1
    research_consent: ResearchConsentRequirementV1

    def __post_init__(self) -> None:
        if type(self.target) is not AssignmentTargetV1:
            raise TypeError("assignment target must be AssignmentTargetV1")
        _sha256(self.curriculum_sha256, "curriculum digest")
        _sha256(self.scenario_sha256, "scenario digest")
        _sha256(self.pack_sha256, "pack digest")
        if type(self.allowed_scenario_variations) is not tuple:
            raise TypeError("allowed scenario variations must be a tuple")
        variations = tuple(
            _text(item, "allowed scenario variation")
            for item in self.allowed_scenario_variations
        )
        if variations != self.allowed_scenario_variations:
            raise ValueError("allowed scenario variations differ after validation")
        if len(variations) != len(set(variations)):
            raise ValueError("allowed scenario variations cannot contain duplicates")
        if type(self.locks) is not AssignmentLocksV1:
            raise TypeError("assignment locks must be AssignmentLocksV1")
        if type(self.mode) is not AssignmentModeV1:
            raise TypeError("assignment mode must be AssignmentModeV1")
        if type(self.strategy_policy) is not StrategyPolicyV1:
            raise TypeError("strategy policy must be StrategyPolicyV1")
        if type(self.hotkey_layout) is not HotkeyLayoutBindingV1:
            raise TypeError("hotkey layout must be HotkeyLayoutBindingV1")
        _positive_int(self.attempt_limit, "attempt limit")
        if self.deadline is not None and type(self.deadline) is not DeadlineMetadataV1:
            raise TypeError("deadline must be DeadlineMetadataV1 or None")
        if type(self.feedback_timing) is not FeedbackTimingV1:
            raise TypeError("feedback timing must be FeedbackTimingV1")
        _text(self.scoring_version, "scoring version")
        if type(self.rubric) is not RubricBindingV1:
            raise TypeError("rubric binding must be RubricBindingV1")
        if type(self.research_consent) is not ResearchConsentRequirementV1:
            raise TypeError("research consent must be ResearchConsentRequirementV1")
        if (
            self.mode is AssignmentModeV1.RESEARCH
            and ConsentScopeV1.LOCAL_RESEARCH_STUDY
            not in self.research_consent.required_scopes
        ):
            raise ValueError("research-mode assignments must require local research consent")

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed_scenario_variations": list(self.allowed_scenario_variations),
            "attempt_limit": self.attempt_limit,
            "curriculum_sha256": self.curriculum_sha256,
            "deadline": None if self.deadline is None else self.deadline.as_dict(),
            "feedback_timing": self.feedback_timing.value,
            "hotkey_layout": self.hotkey_layout.as_dict(),
            "locks": self.locks.as_dict(),
            "mode": self.mode.value,
            "pack_sha256": self.pack_sha256,
            "research_consent": self.research_consent.as_dict(),
            "rubric": self.rubric.as_dict(),
            "scenario_sha256": self.scenario_sha256,
            "scoring_version": self.scoring_version,
            "strategy_policy": self.strategy_policy.value,
            "target": self.target.as_dict(),
        }

    @property
    def content_sha256(self) -> str:
        return _canonical_sha256(self.as_dict())

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> AssignmentSpecV1:
        payload = _fields(
            value,
            {
                "allowed_scenario_variations",
                "attempt_limit",
                "curriculum_sha256",
                "deadline",
                "feedback_timing",
                "hotkey_layout",
                "locks",
                "mode",
                "pack_sha256",
                "research_consent",
                "rubric",
                "scenario_sha256",
                "scoring_version",
                "strategy_policy",
                "target",
            },
            "assignment spec",
        )
        return cls(
            target=AssignmentTargetV1.from_dict(payload["target"]),
            curriculum_sha256=_sha256(payload["curriculum_sha256"], "curriculum digest"),
            scenario_sha256=_sha256(payload["scenario_sha256"], "scenario digest"),
            pack_sha256=_sha256(payload["pack_sha256"], "pack digest"),
            allowed_scenario_variations=_text_array(
                payload["allowed_scenario_variations"],
                "allowed scenario variations",
                allow_empty=True,
            ),
            locks=AssignmentLocksV1.from_dict(payload["locks"]),
            mode=AssignmentModeV1(_enum_text(payload["mode"], "assignment mode")),
            strategy_policy=StrategyPolicyV1(
                _enum_text(payload["strategy_policy"], "strategy policy")
            ),
            hotkey_layout=HotkeyLayoutBindingV1.from_dict(payload["hotkey_layout"]),
            attempt_limit=_positive_int(payload["attempt_limit"], "attempt limit"),
            deadline=(
                None
                if payload["deadline"] is None
                else DeadlineMetadataV1.from_dict(payload["deadline"])
            ),
            feedback_timing=FeedbackTimingV1(
                _enum_text(payload["feedback_timing"], "feedback timing")
            ),
            scoring_version=_text(payload["scoring_version"], "scoring version"),
            rubric=RubricBindingV1.from_dict(payload["rubric"]),
            research_consent=ResearchConsentRequirementV1.from_dict(
                payload["research_consent"]
            ),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> AssignmentSpecV1:
        spec = cls.from_dict(_canonical_object(raw, "assignment spec"))
        if spec.canonical_bytes() != raw:
            raise ValueError("assignment spec changed during restoration")
        return spec


_REVISION_FIELDS = {
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


def _restore_assignment_chain(value: object) -> tuple[Assignment, ...]:
    if type(value) is not list or not value:
        raise ValueError("assignment revision chain must be a non-empty array")
    predecessor: Assignment | None = None
    restored: list[Assignment] = []
    for item in value:
        payload = _fields(item, _REVISION_FIELDS, "assignment revision envelope")
        envelope = create_assignment_revision(
            _sha256(payload["content_sha256"], "assignment content digest"),
            predecessor=predecessor,
        )
        if envelope.as_dict() != payload:
            raise ValueError("assignment revision envelope differs from canonical lineage")
        restored.append(envelope)
        predecessor = envelope
    return tuple(restored)


@dataclass(frozen=True, slots=True)
class AssignmentRevisionV1:
    revision_chain: tuple[Assignment, ...]
    spec: AssignmentSpecV1

    schema_id: ClassVar[str] = ASSIGNMENT_REVISION_SCHEMA_ID
    schema_version: ClassVar[int] = ASSIGNMENT_REVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.revision_chain) is not tuple or not self.revision_chain:
            raise ValueError("assignment revision chain cannot be empty")
        if any(type(item) is not Assignment for item in self.revision_chain):
            raise TypeError("assignment revision chain must contain Assignment values")
        for index, item in enumerate(self.revision_chain):
            item.canonical_bytes()
            if item.revision != index + 1:
                raise ValueError("assignment revision chain must be contiguous from one")
            if index:
                self.revision_chain[index - 1].validate_successor(item)
        if type(self.spec) is not AssignmentSpecV1:
            raise TypeError("assignment spec must be AssignmentSpecV1")
        if self.assignment.content_sha256 != self.spec.content_sha256:
            raise ValueError("assignment envelope does not commit to its exact spec")

    @property
    def assignment(self) -> Assignment:
        return self.revision_chain[-1]

    @property
    def assignment_id(self) -> str:
        return self.assignment.assignment_id

    @property
    def content_sha256(self) -> str:
        return self.spec.content_sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "revision_chain": [item.as_dict() for item in self.revision_chain],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "spec": self.spec.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> AssignmentRevisionV1:
        payload = _fields(
            value,
            {"revision_chain", "schema_id", "schema_version", "spec"},
            "assignment revision",
        )
        if payload["schema_id"] != ASSIGNMENT_REVISION_SCHEMA_ID:
            raise ValueError("assignment revision schema ID differs")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != ASSIGNMENT_REVISION_SCHEMA_VERSION
        ):
            raise ValueError("assignment revision schema version differs")
        return cls(
            revision_chain=_restore_assignment_chain(payload["revision_chain"]),
            spec=AssignmentSpecV1.from_dict(payload["spec"]),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> AssignmentRevisionV1:
        record = cls.from_dict(_canonical_object(raw, "assignment revision"))
        if record.canonical_bytes() != raw:
            raise ValueError("assignment revision changed during restoration")
        return record


def create_assignment(spec: AssignmentSpecV1) -> AssignmentRevisionV1:
    if type(spec) is not AssignmentSpecV1:
        raise TypeError("assignment spec must be AssignmentSpecV1")
    return AssignmentRevisionV1(
        revision_chain=(create_assignment_revision(spec.content_sha256),),
        spec=spec,
    )


def revise_assignment(
    predecessor: AssignmentRevisionV1,
    spec: AssignmentSpecV1,
) -> AssignmentRevisionV1:
    if type(predecessor) is not AssignmentRevisionV1:
        raise TypeError("assignment predecessor must be AssignmentRevisionV1")
    if type(spec) is not AssignmentSpecV1:
        raise TypeError("assignment spec must be AssignmentSpecV1")
    successor = create_assignment_revision(
        spec.content_sha256,
        predecessor=predecessor.assignment,
    )
    return AssignmentRevisionV1(
        revision_chain=predecessor.revision_chain + (successor,),
        spec=spec,
    )


def _deadline_claim(deadline: DeadlineMetadataV1 | None) -> DeadlineEnforcementClaimV1:
    if deadline is None:
        return DeadlineEnforcementClaimV1.NOT_PRESENT
    return deadline.enforcement_claim


def _attempt_content_dict(
    *,
    assignment_revision: AssignmentRevisionV1,
    learner_profile_id: str,
    attempt_number: int,
    run_id: str,
    selected_lesson: LessonReferenceV1,
    selected_scenario_variation: str | None,
    runtime_parameters: AttemptRuntimeParametersV1,
    consent_evidence: ResearchConsentEvidenceV1,
    recorded_at_utc: str,
    deadline_clock_evidence: DeadlineClockEvidenceV1 | None,
) -> dict[str, object]:
    return {
        "assignment_revision": assignment_revision.as_dict(),
        "attempt_number": attempt_number,
        "consent_evidence": consent_evidence.as_dict(),
        "deadline_clock_evidence": (
            None if deadline_clock_evidence is None else deadline_clock_evidence.as_dict()
        ),
        "deadline_enforcement_claim": _deadline_claim(
            assignment_revision.spec.deadline
        ).value,
        "learner_profile_id": learner_profile_id,
        "recorded_at_utc": recorded_at_utc,
        "run_id": run_id,
        "runtime_parameters": runtime_parameters.as_dict(),
        "schema_id": ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_ID,
        "schema_version": ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_VERSION,
        "selected_lesson": selected_lesson.as_dict(),
        "selected_scenario_variation": selected_scenario_variation,
    }


def _validate_attempt_binding(
    *,
    assignment_revision: AssignmentRevisionV1,
    learner_profile_id: str,
    attempt_number: int,
    run_id: str,
    selected_lesson: LessonReferenceV1,
    selected_scenario_variation: str | None,
    runtime_parameters: AttemptRuntimeParametersV1,
    consent_evidence: ResearchConsentEvidenceV1,
    recorded_at_utc: str,
    deadline_clock_evidence: DeadlineClockEvidenceV1 | None,
) -> None:
    """Reapply every assignment constraint at creation and restoration."""

    if type(assignment_revision) is not AssignmentRevisionV1:
        raise TypeError("assignment revision must be AssignmentRevisionV1")
    learner_id = require_learner_profile_id(learner_profile_id)
    ordinal = _positive_int(attempt_number, "attempt number")
    if ordinal > assignment_revision.spec.attempt_limit:
        raise ValueError("attempt number exceeds the assignment attempt limit")
    _text(run_id, "attempt run ID")
    assignment_revision.spec.target.require_selected(selected_lesson)
    if selected_scenario_variation is not None:
        _text(selected_scenario_variation, "selected scenario variation")
        if (
            selected_scenario_variation
            not in assignment_revision.spec.allowed_scenario_variations
        ):
            raise ValueError("selected scenario variation is not allowed")
    if type(runtime_parameters) is not AttemptRuntimeParametersV1:
        raise TypeError("runtime parameters must be AttemptRuntimeParametersV1")
    if runtime_parameters.lock_snapshot() != assignment_revision.spec.locks:
        raise ValueError("attempt runtime parameters differ from assignment locks")
    expected_seed = assignment_revision.spec.locks.seed_policy.expected_seed(
        assignment_lineage_id=assignment_revision.assignment.lineage_id,
        assignment_revision=assignment_revision.assignment.revision,
        learner_profile_id=learner_id,
        attempt_number=ordinal,
    )
    if expected_seed is not None and runtime_parameters.seed != expected_seed:
        raise ValueError("attempt seed differs from the locked seed policy")
    if type(consent_evidence) is not ResearchConsentEvidenceV1:
        raise TypeError("consent evidence must be ResearchConsentEvidenceV1")
    if consent_evidence.learner_profile_id != learner_id:
        raise ValueError("consent evidence belongs to another learner profile")
    requirement = assignment_revision.spec.research_consent
    if consent_evidence.authorization_policy_id != requirement.authorization_policy_id:
        raise ValueError("consent authorization policy differs from assignment requirement")
    if not set(requirement.required_scopes).issubset(consent_evidence.granted_scopes):
        raise ValueError("consent evidence does not grant every assignment-required scope")
    record_time = _utc(recorded_at_utc, "attempt manifest record time")
    if (
        deadline_clock_evidence is not None
        and type(deadline_clock_evidence) is not DeadlineClockEvidenceV1
    ):
        raise TypeError("deadline clock evidence must be DeadlineClockEvidenceV1 or None")
    _enforce_deadline(
        assignment_revision.spec.deadline,
        deadline_clock_evidence,
        record_time,
    )


@dataclass(frozen=True, slots=True)
class AssignmentAttemptManifestV1:
    attempt_revision: AssignmentAttempt
    assignment_revision: AssignmentRevisionV1
    learner_profile_id: str
    attempt_number: int
    run_id: str
    selected_lesson: LessonReferenceV1
    selected_scenario_variation: str | None
    runtime_parameters: AttemptRuntimeParametersV1
    consent_evidence: ResearchConsentEvidenceV1
    recorded_at_utc: str
    deadline_clock_evidence: DeadlineClockEvidenceV1 | None

    schema_id: ClassVar[str] = ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.attempt_revision) is not AssignmentAttempt:
            raise TypeError("attempt revision must be AssignmentAttempt")
        self.attempt_revision.canonical_bytes()
        if self.attempt_revision.revision != 1:
            raise ValueError("an immutable attempt manifest must be an initial revision")
        if type(self.assignment_revision) is not AssignmentRevisionV1:
            raise TypeError("assignment revision must be AssignmentRevisionV1")
        require_learner_profile_id(self.learner_profile_id)
        _positive_int(self.attempt_number, "attempt number")
        _text(self.run_id, "attempt run ID")
        if type(self.selected_lesson) is not LessonReferenceV1:
            raise TypeError("selected lesson must be LessonReferenceV1")
        if self.selected_scenario_variation is not None:
            _text(self.selected_scenario_variation, "selected scenario variation")
        if type(self.runtime_parameters) is not AttemptRuntimeParametersV1:
            raise TypeError("runtime parameters must be AttemptRuntimeParametersV1")
        if type(self.consent_evidence) is not ResearchConsentEvidenceV1:
            raise TypeError("consent evidence must be ResearchConsentEvidenceV1")
        _utc(self.recorded_at_utc, "attempt manifest record time")
        if (
            self.deadline_clock_evidence is not None
            and type(self.deadline_clock_evidence) is not DeadlineClockEvidenceV1
        ):
            raise TypeError("deadline clock evidence must be DeadlineClockEvidenceV1 or None")
        _validate_attempt_binding(
            assignment_revision=self.assignment_revision,
            learner_profile_id=self.learner_profile_id,
            attempt_number=self.attempt_number,
            run_id=self.run_id,
            selected_lesson=self.selected_lesson,
            selected_scenario_variation=self.selected_scenario_variation,
            runtime_parameters=self.runtime_parameters,
            consent_evidence=self.consent_evidence,
            recorded_at_utc=self.recorded_at_utc,
            deadline_clock_evidence=self.deadline_clock_evidence,
        )
        if self.attempt_revision.content_sha256 != _canonical_sha256(self.content_dict()):
            raise ValueError("attempt envelope does not commit to its exact manifest")

    @property
    def attempt_id(self) -> str:
        return self.attempt_revision.attempt_id

    @property
    def content_sha256(self) -> str:
        return self.attempt_revision.content_sha256

    @property
    def deadline_enforcement_claim(self) -> DeadlineEnforcementClaimV1:
        return _deadline_claim(self.assignment_revision.spec.deadline)

    def content_dict(self) -> dict[str, object]:
        return _attempt_content_dict(
            assignment_revision=self.assignment_revision,
            learner_profile_id=self.learner_profile_id,
            attempt_number=self.attempt_number,
            run_id=self.run_id,
            selected_lesson=self.selected_lesson,
            selected_scenario_variation=self.selected_scenario_variation,
            runtime_parameters=self.runtime_parameters,
            consent_evidence=self.consent_evidence,
            recorded_at_utc=self.recorded_at_utc,
            deadline_clock_evidence=self.deadline_clock_evidence,
        )

    def as_dict(self) -> dict[str, object]:
        return {**self.content_dict(), "attempt_revision": self.attempt_revision.as_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> AssignmentAttemptManifestV1:
        payload = _fields(
            value,
            {
                "assignment_revision",
                "attempt_number",
                "attempt_revision",
                "consent_evidence",
                "deadline_clock_evidence",
                "deadline_enforcement_claim",
                "learner_profile_id",
                "recorded_at_utc",
                "run_id",
                "runtime_parameters",
                "schema_id",
                "schema_version",
                "selected_lesson",
                "selected_scenario_variation",
            },
            "assignment attempt manifest",
        )
        if payload["schema_id"] != ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_ID:
            raise ValueError("assignment attempt manifest schema ID differs")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_VERSION
        ):
            raise ValueError("assignment attempt manifest schema version differs")
        assignment_revision = AssignmentRevisionV1.from_dict(
            payload["assignment_revision"]
        )
        selected_variation = payload["selected_scenario_variation"]
        deadline_clock = payload["deadline_clock_evidence"]
        content = _attempt_content_dict(
            assignment_revision=assignment_revision,
            learner_profile_id=require_learner_profile_id(payload["learner_profile_id"]),
            attempt_number=_positive_int(payload["attempt_number"], "attempt number"),
            run_id=_text(payload["run_id"], "attempt run ID"),
            selected_lesson=LessonReferenceV1.from_dict(payload["selected_lesson"]),
            selected_scenario_variation=(
                None
                if selected_variation is None
                else _text(selected_variation, "selected scenario variation")
            ),
            runtime_parameters=AttemptRuntimeParametersV1.from_dict(
                payload["runtime_parameters"]
            ),
            consent_evidence=ResearchConsentEvidenceV1.from_dict(
                payload["consent_evidence"]
            ),
            recorded_at_utc=_utc(payload["recorded_at_utc"], "attempt manifest record time"),
            deadline_clock_evidence=(
                None
                if deadline_clock is None
                else DeadlineClockEvidenceV1.from_dict(deadline_clock)
            ),
        )
        expected_claim = content["deadline_enforcement_claim"]
        supplied_claim = DeadlineEnforcementClaimV1(
            _enum_text(payload["deadline_enforcement_claim"], "deadline enforcement claim")
        ).value
        if supplied_claim != expected_claim:
            raise ValueError("attempt deadline enforcement claim differs")
        attempt_payload = _fields(
            payload["attempt_revision"],
            _REVISION_FIELDS,
            "assignment attempt revision envelope",
        )
        attempt_revision = create_assignment_attempt_revision(
            _canonical_sha256(content)
        )
        if attempt_revision.as_dict() != attempt_payload:
            raise ValueError("assignment attempt envelope differs from canonical content")
        return cls(
            attempt_revision=attempt_revision,
            assignment_revision=assignment_revision,
            learner_profile_id=content["learner_profile_id"],  # type: ignore[arg-type]
            attempt_number=content["attempt_number"],  # type: ignore[arg-type]
            run_id=content["run_id"],  # type: ignore[arg-type]
            selected_lesson=LessonReferenceV1.from_dict(content["selected_lesson"]),
            selected_scenario_variation=content["selected_scenario_variation"],  # type: ignore[arg-type]
            runtime_parameters=AttemptRuntimeParametersV1.from_dict(
                content["runtime_parameters"]
            ),
            consent_evidence=ResearchConsentEvidenceV1.from_dict(
                content["consent_evidence"]
            ),
            recorded_at_utc=content["recorded_at_utc"],  # type: ignore[arg-type]
            deadline_clock_evidence=(
                None
                if content["deadline_clock_evidence"] is None
                else DeadlineClockEvidenceV1.from_dict(content["deadline_clock_evidence"])
            ),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> AssignmentAttemptManifestV1:
        manifest = cls.from_dict(_canonical_object(raw, "assignment attempt manifest"))
        if manifest.canonical_bytes() != raw:
            raise ValueError("assignment attempt manifest changed during restoration")
        return manifest


def _enforce_deadline(
    deadline: DeadlineMetadataV1 | None,
    clock_evidence: DeadlineClockEvidenceV1 | None,
    recorded_at_utc: str,
) -> None:
    if deadline is None or not deadline.policy_authorized:
        if clock_evidence is not None:
            raise ValueError(
                "deadline clock evidence cannot create an enforcement claim without authorization"
            )
        return
    if clock_evidence is None:
        raise ValueError("authorized deadline enforcement requires recorded clock evidence")
    if (
        clock_evidence.recorded_clock_id != deadline.recorded_clock_id
        or clock_evidence.recorded_clock_sha256 != deadline.recorded_clock_sha256
        or clock_evidence.enforcement_policy_id != deadline.enforcement_policy_id
        or clock_evidence.enforcement_policy_sha256
        != deadline.enforcement_policy_sha256
    ):
        raise ValueError("deadline clock/policy evidence differs from assignment authorization")
    if clock_evidence.observed_at_utc != recorded_at_utc:
        raise ValueError("attempt record time differs from authorized clock observation")
    if clock_evidence.observed_at_utc > deadline.deadline_utc:
        raise ValueError("attempt is after the authorized recorded-clock deadline")


def bind_assignment_attempt(
    *,
    assignment_revision: AssignmentRevisionV1,
    learner_profile_id: str,
    attempt_number: int,
    run_id: str,
    selected_lesson: LessonReferenceV1,
    selected_scenario_variation: str | None,
    runtime_parameters: AttemptRuntimeParametersV1,
    consent_evidence: ResearchConsentEvidenceV1,
    recorded_at_utc: str,
    deadline_clock_evidence: DeadlineClockEvidenceV1 | None = None,
) -> AssignmentAttemptManifestV1:
    """Validate every lock and return an immutable, exact attempt manifest."""

    if type(assignment_revision) is not AssignmentRevisionV1:
        raise TypeError("assignment revision must be AssignmentRevisionV1")
    learner_id = require_learner_profile_id(learner_profile_id)
    ordinal = _positive_int(attempt_number, "attempt number")
    if ordinal > assignment_revision.spec.attempt_limit:
        raise ValueError("attempt number exceeds the assignment attempt limit")
    _text(run_id, "attempt run ID")
    assignment_revision.spec.target.require_selected(selected_lesson)
    if selected_scenario_variation is not None:
        selected_scenario_variation = _text(
            selected_scenario_variation,
            "selected scenario variation",
        )
        if (
            selected_scenario_variation
            not in assignment_revision.spec.allowed_scenario_variations
        ):
            raise ValueError("selected scenario variation is not allowed")
    if type(runtime_parameters) is not AttemptRuntimeParametersV1:
        raise TypeError("runtime parameters must be AttemptRuntimeParametersV1")
    if runtime_parameters.lock_snapshot() != assignment_revision.spec.locks:
        raise ValueError("attempt runtime parameters differ from assignment locks")
    expected_seed = assignment_revision.spec.locks.seed_policy.expected_seed(
        assignment_lineage_id=assignment_revision.assignment.lineage_id,
        assignment_revision=assignment_revision.assignment.revision,
        learner_profile_id=learner_id,
        attempt_number=ordinal,
    )
    if expected_seed is not None and runtime_parameters.seed != expected_seed:
        raise ValueError("attempt seed differs from the locked seed policy")
    if type(consent_evidence) is not ResearchConsentEvidenceV1:
        raise TypeError("consent evidence must be ResearchConsentEvidenceV1")
    if consent_evidence.learner_profile_id != learner_id:
        raise ValueError("consent evidence belongs to another learner profile")
    requirement = assignment_revision.spec.research_consent
    if consent_evidence.authorization_policy_id != requirement.authorization_policy_id:
        raise ValueError("consent authorization policy differs from assignment requirement")
    if not set(requirement.required_scopes).issubset(consent_evidence.granted_scopes):
        raise ValueError("consent evidence does not grant every assignment-required scope")
    record_time = _utc(recorded_at_utc, "attempt manifest record time")
    if (
        deadline_clock_evidence is not None
        and type(deadline_clock_evidence) is not DeadlineClockEvidenceV1
    ):
        raise TypeError("deadline clock evidence must be DeadlineClockEvidenceV1 or None")
    _enforce_deadline(
        assignment_revision.spec.deadline,
        deadline_clock_evidence,
        record_time,
    )
    content = _attempt_content_dict(
        assignment_revision=assignment_revision,
        learner_profile_id=learner_id,
        attempt_number=ordinal,
        run_id=run_id,
        selected_lesson=selected_lesson,
        selected_scenario_variation=selected_scenario_variation,
        runtime_parameters=runtime_parameters,
        consent_evidence=consent_evidence,
        recorded_at_utc=record_time,
        deadline_clock_evidence=deadline_clock_evidence,
    )
    return AssignmentAttemptManifestV1(
        attempt_revision=create_assignment_attempt_revision(
            _canonical_sha256(content)
        ),
        assignment_revision=assignment_revision,
        learner_profile_id=learner_id,
        attempt_number=ordinal,
        run_id=run_id,
        selected_lesson=selected_lesson,
        selected_scenario_variation=selected_scenario_variation,
        runtime_parameters=runtime_parameters,
        consent_evidence=consent_evidence,
        recorded_at_utc=record_time,
        deadline_clock_evidence=deadline_clock_evidence,
    )


__all__ = [
    "ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_ID",
    "ASSIGNMENT_ATTEMPT_MANIFEST_SCHEMA_VERSION",
    "ASSIGNMENT_REVISION_SCHEMA_ID",
    "ASSIGNMENT_REVISION_SCHEMA_VERSION",
    "AssignmentAttemptManifestV1",
    "AssignmentLocksV1",
    "AssignmentModeV1",
    "AssignmentRevisionV1",
    "AssignmentSpecV1",
    "AssignmentTargetKindV1",
    "AssignmentTargetV1",
    "AttemptRuntimeParametersV1",
    "DeadlineClockEvidenceV1",
    "DeadlineEnforcementClaimV1",
    "DeadlineMetadataV1",
    "FeedbackTimingV1",
    "HiddenStateRevealPolicyV1",
    "HotkeyLayoutBindingV1",
    "LessonReferenceV1",
    "ResearchConsentEvidenceV1",
    "ResearchConsentRequirementV1",
    "RubricBindingV1",
    "SeedPolicyKindV1",
    "SeedPolicyV1",
    "StrategyPolicyV1",
    "bind_assignment_attempt",
    "create_assignment",
    "revise_assignment",
]
