"""Immutable manual curriculum plans for WO34-C adaptive selection.

A plan is a sidecar: it does not mutate evidence, projections, lessons, or the
catalog.  Applicability is deliberately narrow (learner plus inclusive selection
ordinal).  Catalog, assignment, mode, prerequisite, consent, capability, and
assessment-policy bindings are validated by the selector after a sole applicable
plan has been identified, so an invalid plan is refused rather than silently
ignored in favor of adaptive ranking.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import CurriculumMode
from .skills import canonical_json_bytes, sha256_json


CURRICULUM_PLAN_SCHEMA_VERSION_V1 = 1
CURRICULUM_PLAN_POLICY_ID_V1 = "IMMUTABLE_CURRICULUM_PLAN_V1"
NOT_APPLICABLE_V1 = "NOT_APPLICABLE"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ADAPTIVE_MODES_V1 = frozenset(
    {
        CurriculumMode.GUIDED,
        CurriculumMode.PRACTICE,
        CurriculumMode.ASSESSMENT,
        CurriculumMode.REMEDIATION,
    }
)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be nonempty exact text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _digest_or_not_applicable(value: object, label: str) -> str:
    if value == NOT_APPLICABLE_V1:
        return NOT_APPLICABLE_V1
    return _digest(value, label)


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
        raise ValueError(f"{label} is not a canonical JSON object")
    return payload


@dataclass(frozen=True, slots=True)
class CurriculumPlanEntryV1:
    """One exact plan position, bound to lesson semantics rather than a title."""

    selection_ordinal: int
    lesson_digest: str
    mode: CurriculumMode

    def __post_init__(self) -> None:
        _integer(self.selection_ordinal, "plan selection ordinal", minimum=1)
        _digest(self.lesson_digest, "plan lesson digest")
        if self.mode not in _ADAPTIVE_MODES_V1:
            raise ValueError("plan entry mode must be an adaptive WO34-C mode")

    def as_dict(self) -> dict[str, object]:
        return {
            "lesson_digest": self.lesson_digest,
            "mode": self.mode.value,
            "selection_ordinal": self.selection_ordinal,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CurriculumPlanEntryV1:
        if not isinstance(payload, dict) or set(payload) != {
            "lesson_digest",
            "mode",
            "selection_ordinal",
        }:
            raise ValueError("curriculum plan entry fields differ")
        return cls(
            selection_ordinal=_integer(
                payload["selection_ordinal"],
                "plan selection ordinal",
                minimum=1,
            ),
            lesson_digest=_digest(payload["lesson_digest"], "plan lesson digest"),
            mode=CurriculumMode.parse(_text(payload["mode"], "plan mode")),
        )


@dataclass(frozen=True, slots=True)
class CurriculumPlanV1:
    """Canonical, immutable manual sequence over one learner and ordinal range."""

    plan_scope_id: str
    learner_id: str
    start_selection_ordinal: int
    end_selection_ordinal: int
    catalog_digest: str
    assignment_digest: str
    entries: tuple[CurriculumPlanEntryV1, ...]
    policy_id: str = CURRICULUM_PLAN_POLICY_ID_V1
    schema_version: int = CURRICULUM_PLAN_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _text(self.plan_scope_id, "curriculum plan scope ID")
        _text(self.learner_id, "curriculum plan learner ID")
        start = _integer(
            self.start_selection_ordinal,
            "plan start selection ordinal",
            minimum=1,
        )
        end = _integer(
            self.end_selection_ordinal,
            "plan end selection ordinal",
            minimum=1,
        )
        if end < start:
            raise ValueError("curriculum plan interval is reversed")
        _digest(self.catalog_digest, "curriculum plan catalog digest")
        _digest_or_not_applicable(
            self.assignment_digest,
            "curriculum plan assignment digest",
        )
        if (
            type(self.entries) is not tuple
            or any(not isinstance(item, CurriculumPlanEntryV1) for item in self.entries)
        ):
            raise TypeError("curriculum plan entries must be typed and immutable")
        expected_ordinals = tuple(range(start, end + 1))
        if tuple(item.selection_ordinal for item in self.entries) != expected_ordinals:
            raise ValueError(
                "curriculum plan entries must cover the inclusive interval exactly"
            )
        if self.policy_id != CURRICULUM_PLAN_POLICY_ID_V1:
            raise ValueError("curriculum plan policy differs")
        if (
            type(self.schema_version) is not int
            or self.schema_version != CURRICULUM_PLAN_SCHEMA_VERSION_V1
        ):
            raise ValueError("curriculum plan schema version differs")

    @property
    def plan_digest(self) -> str:
        return sha256_json(self.as_dict())

    @property
    def plan_id(self) -> str:
        return "curriculum-plan-" + self.plan_digest

    def applies_to(self, learner_id: str, selection_ordinal: int) -> bool:
        """Return scope applicability only; policy bindings are checked later."""

        _text(learner_id, "selection learner ID")
        _integer(selection_ordinal, "selection ordinal", minimum=1)
        return (
            learner_id == self.learner_id
            and self.start_selection_ordinal
            <= selection_ordinal
            <= self.end_selection_ordinal
        )

    def entry_for(self, selection_ordinal: int) -> CurriculumPlanEntryV1:
        _integer(selection_ordinal, "selection ordinal", minimum=1)
        if not (
            self.start_selection_ordinal
            <= selection_ordinal
            <= self.end_selection_ordinal
        ):
            raise ValueError("selection ordinal is outside the curriculum plan")
        return self.entries[selection_ordinal - self.start_selection_ordinal]

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_digest": self.assignment_digest,
            "catalog_digest": self.catalog_digest,
            "end_selection_ordinal": self.end_selection_ordinal,
            "entries": [item.as_dict() for item in self.entries],
            "learner_id": self.learner_id,
            "plan_scope_id": self.plan_scope_id,
            "policy_id": self.policy_id,
            "record_kind": "IMMUTABLE_CURRICULUM_PLAN_V1",
            "schema_version": self.schema_version,
            "start_selection_ordinal": self.start_selection_ordinal,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CurriculumPlanV1:
        plan = cls.from_dict(_canonical_object(raw, "curriculum plan"))
        if plan.canonical_bytes() != raw:
            raise ValueError("curriculum plan changed during restoration")
        return plan

    @classmethod
    def from_dict(cls, payload: object) -> CurriculumPlanV1:
        expected = {
            "assignment_digest",
            "catalog_digest",
            "end_selection_ordinal",
            "entries",
            "learner_id",
            "plan_scope_id",
            "policy_id",
            "record_kind",
            "schema_version",
            "start_selection_ordinal",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload["record_kind"] != "IMMUTABLE_CURRICULUM_PLAN_V1"
        ):
            raise ValueError("curriculum plan fields differ")
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, list):
            raise TypeError("curriculum plan entries must be an array")
        return cls(
            plan_scope_id=_text(payload["plan_scope_id"], "plan scope ID"),
            learner_id=_text(payload["learner_id"], "plan learner ID"),
            start_selection_ordinal=_integer(
                payload["start_selection_ordinal"],
                "plan start selection ordinal",
                minimum=1,
            ),
            end_selection_ordinal=_integer(
                payload["end_selection_ordinal"],
                "plan end selection ordinal",
                minimum=1,
            ),
            catalog_digest=_digest(payload["catalog_digest"], "plan catalog digest"),
            assignment_digest=_digest_or_not_applicable(
                payload["assignment_digest"],
                "plan assignment digest",
            ),
            entries=tuple(CurriculumPlanEntryV1.from_dict(item) for item in raw_entries),
            policy_id=_text(payload["policy_id"], "plan policy ID"),
            schema_version=_integer(
                payload["schema_version"],
                "plan schema version",
                minimum=1,
            ),
        )


__all__ = [
    "CURRICULUM_PLAN_POLICY_ID_V1",
    "CURRICULUM_PLAN_SCHEMA_VERSION_V1",
    "NOT_APPLICABLE_V1",
    "CurriculumPlanEntryV1",
    "CurriculumPlanV1",
]
