"""Deterministic local queries over the instructor console ledger.

This module is the read boundary for WO37-D.  Queries never inspect mutable UI
state: callers name one exact append-only ledger sequence and every result binds
that sequence, its ledger digest, and the immutable source-artifact identities it
was derived from.  Learner-self queries are fail-closed to one pseudonymous
profile.  A view spanning learners is available only through an explicit
cohort-research scope bound to one cohort and one study.

Comparison views intentionally contain source sets and descriptive uncertainty,
not ranks, winners, treatment effects, or learner-difference claims.  The six V1
comparison shapes are governed by a declarative rule table so extending business
routing does not grow an ``if``/``elif`` ladder.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from kirby2.pseudonyms import require_instructor_profile_id, require_learner_profile_id

from .console import (
    ConsoleArtifactKindV1,
    ConsoleArtifactReferenceV1,
    ConsoleCapabilityV1,
    ConsoleSourceIdentityV1,
    InstructorConsoleLedgerV1,
)


INSTRUCTOR_QUERY_SCOPE_SCHEMA_ID = "KIRBY2_INSTRUCTOR_QUERY_SCOPE_V1"
INSTRUCTOR_QUERY_SCOPE_SCHEMA_VERSION = 1
CONSOLE_VIEW_ROW_SCHEMA_ID = "KIRBY2_CONSOLE_VIEW_ROW_V1"
CONSOLE_VIEW_ROW_SCHEMA_VERSION = 1
CONSOLE_VIEW_SCHEMA_ID = "KIRBY2_CONSOLE_VIEW_V1"
CONSOLE_VIEW_SCHEMA_VERSION = 1
COMPARISON_SOURCE_SCHEMA_ID = "KIRBY2_COMPARISON_SOURCE_V1"
COMPARISON_SOURCE_SCHEMA_VERSION = 1
COMPARISON_VIEW_SCHEMA_ID = "KIRBY2_COMPARISON_VIEW_V1"
COMPARISON_VIEW_SCHEMA_VERSION = 1
MICROSCOPE_LINK_SCHEMA_ID = "KIRBY2_INSTRUCTOR_MICROSCOPE_LINK_V1"
MICROSCOPE_LINK_SCHEMA_VERSION = 1
MICROSCOPE_TIMELINE_CURSOR_SCHEMA_ID = "KIRBY2_MICROSCOPE_TIMELINE_CURSOR_V1"
MICROSCOPE_TIMELINE_CURSOR_SCHEMA_VERSION = 1

COMPARISON_INTERPRETATION_V1 = (
    "DESCRIPTIVE_SOURCE_SET_WITH_UNCERTAINTY_ONLY_NO_RANK_OR_DIFFERENCE_CLAIM"
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_ATTEMPT_ID = re.compile(r"assignment-attempt-[0-9a-f]{64}\Z")
_COHORT_ID = re.compile(r"cohort-[0-9a-f]{64}\Z")
_STUDY_ID = re.compile(r"research-study-[0-9a-f]{64}\Z")


class InstructorQueryScopeKindV1(str, Enum):
    """Closed authorization scope for local instructor-console reads."""

    LEARNER_SELF = "LEARNER_SELF"
    INSTRUCTOR_LOCAL = "INSTRUCTOR_LOCAL"
    COHORT_RESEARCH = "COHORT_RESEARCH"


class ComparisonViewKindV1(str, Enum):
    """The six exact comparison shapes exposed by WO37-D."""

    SAME_LEARNER_ACROSS_ATTEMPTS = "SAME_LEARNER_ACROSS_ATTEMPTS"
    SAME_LESSON_ACROSS_LEARNERS = "SAME_LESSON_ACROSS_LEARNERS"
    SAME_SKILL_ACROSS_SCENARIOS = "SAME_SKILL_ACROSS_SCENARIOS"
    SAME_HOTKEY_LAYOUT_ACROSS_SESSIONS = "SAME_HOTKEY_LAYOUT_ACROSS_SESSIONS"
    SAME_STRATEGY_ACROSS_VOLUME_REGIMES = "SAME_STRATEGY_ACROSS_VOLUME_REGIMES"
    MANUAL_EXECUTION_VS_BENCHMARK_ALGORITHM = (
        "MANUAL_EXECUTION_VS_BENCHMARK_ALGORITHM"
    )


class ComparisonExecutionModeV1(str, Enum):
    """Execution provenance for a comparison source."""

    MANUAL = "MANUAL"
    BENCHMARK_ALGORITHM = "BENCHMARK_ALGORITHM"


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ValueError("query record is not strict canonical JSON") from error


def _pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("query JSON contains a duplicate object key")
        value[key] = item
    return value


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if type(value) is not dict or _canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return value


def _exact_object(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    if frozenset(value) != expected:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(expected - frozenset(value))}, "
            f"extra={sorted(frozenset(value) - expected)}"
        )
    return value


def _json_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact JSON array")
    return value


def _text(
    value: object,
    label: str,
    *,
    maximum_utf8_bytes: int = 1024,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use canonical NFC text")
    if len(value.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{label} exceeds its bounded size")
    if any(character == "\x00" or character in "\r\n" for character in value):
        raise ValueError(f"{label} contains a forbidden control character")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _identifier(
    value: object,
    label: str,
    *,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
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


def _from_json_bytes(record_type, raw: bytes, label: str):
    record = record_type.from_dict(_canonical_object(raw, label))
    if record.canonical_bytes() != raw:
        raise ValueError(f"{label} changed during exact restoration")
    return record


def _unique_versions(values) -> tuple[str, ...]:
    """Return a stable exact set of explicit version strings."""

    materialized = tuple(values)
    if any(type(item) is not str for item in materialized):
        raise TypeError("console versions must be exact text")
    return tuple(sorted(set(materialized), key=lambda item: item.encode("utf-8")))


def _version_json(value: object) -> str:
    """Serialize the console's intentionally scalar V1 version identity."""

    return _text(value, "console view version")


@dataclass(frozen=True, slots=True)
class InstructorQueryScopeV1:
    """Explicit local read scope; no implicit current-user state is consulted."""

    scope_kind: InstructorQueryScopeKindV1
    principal_profile_id: str
    learner_profile_id: str | None = None
    cohort_id: str | None = None
    study_id: str | None = None
    schema_id: str = INSTRUCTOR_QUERY_SCOPE_SCHEMA_ID
    schema_version: int = INSTRUCTOR_QUERY_SCOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.scope_kind) is not InstructorQueryScopeKindV1:
            raise TypeError("query scope kind must be InstructorQueryScopeKindV1")
        if self.scope_kind is InstructorQueryScopeKindV1.LEARNER_SELF:
            require_learner_profile_id(self.principal_profile_id)
            require_learner_profile_id(self.learner_profile_id)
            if self.learner_profile_id != self.principal_profile_id:
                raise ValueError("learner-self scope must name its principal learner")
            if self.cohort_id is not None or self.study_id is not None:
                raise ValueError("learner-self scope cannot carry cohort or study state")
        elif self.scope_kind is InstructorQueryScopeKindV1.INSTRUCTOR_LOCAL:
            require_instructor_profile_id(self.principal_profile_id)
            if any(
                value is not None
                for value in (self.learner_profile_id, self.cohort_id, self.study_id)
            ):
                raise ValueError("instructor-local scope cannot carry research selection")
        else:
            require_instructor_profile_id(self.principal_profile_id)
            if self.learner_profile_id is not None:
                raise ValueError("cohort-research scope cannot target one learner")
            _identifier(self.cohort_id, "query cohort ID", pattern=_COHORT_ID)
            _identifier(self.study_id, "query study ID", pattern=_STUDY_ID)
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=INSTRUCTOR_QUERY_SCOPE_SCHEMA_ID,
            expected_version=INSTRUCTOR_QUERY_SCOPE_SCHEMA_VERSION,
            label="instructor query scope",
        )

    @property
    def allows_cross_learner(self) -> bool:
        return self.scope_kind is InstructorQueryScopeKindV1.COHORT_RESEARCH

    def as_dict(self) -> dict[str, object]:
        return {
            "cohort_id": self.cohort_id,
            "learner_profile_id": self.learner_profile_id,
            "principal_profile_id": self.principal_profile_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scope_kind": self.scope_kind.value,
            "study_id": self.study_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def scope_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> InstructorQueryScopeV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "cohort_id",
                    "learner_profile_id",
                    "principal_profile_id",
                    "schema_id",
                    "schema_version",
                    "scope_kind",
                    "study_id",
                }
            ),
            "instructor query scope",
        )
        return cls(
            scope_kind=InstructorQueryScopeKindV1(
                _text(value["scope_kind"], "query scope kind")
            ),
            principal_profile_id=_text(
                value["principal_profile_id"], "query principal profile ID"
            ),
            learner_profile_id=_optional_text(
                value["learner_profile_id"], "query learner profile ID"
            ),
            cohort_id=_optional_text(value["cohort_id"], "query cohort ID"),
            study_id=_optional_text(value["study_id"], "query study ID"),
            schema_id=_text(value["schema_id"], "query scope schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "query scope schema version"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> InstructorQueryScopeV1:
        return _from_json_bytes(cls, raw, "instructor query scope")


@dataclass(frozen=True, slots=True)
class ConsoleViewRowV1:
    """One immutable artifact row with all mandatory provenance disclosures."""

    reference: ConsoleArtifactReferenceV1
    ledger_sequence: int
    ledger_entry_sha256: str
    schema_id: str = CONSOLE_VIEW_ROW_SCHEMA_ID
    schema_version: int = CONSOLE_VIEW_ROW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.reference) is not ConsoleArtifactReferenceV1:
            raise TypeError("console view row reference is invalid")
        _positive_int(self.ledger_sequence, "console view row ledger sequence")
        _sha256(self.ledger_entry_sha256, "console view row ledger entry digest")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=CONSOLE_VIEW_ROW_SCHEMA_ID,
            expected_version=CONSOLE_VIEW_ROW_SCHEMA_VERSION,
            label="console view row",
        )

    @property
    def content_version(self):
        return self.reference.content_version

    @property
    def scoring_version(self):
        return self.reference.scoring_version

    @property
    def model_version(self):
        return self.reference.model_version

    @property
    def analysis_version(self):
        return self.reference.analysis_version

    @property
    def sample_count(self) -> int:
        return self.reference.sample_count

    @property
    def uncertainty_sha256(self) -> str | None:
        return self.reference.uncertainty_sha256

    @property
    def capability(self) -> ConsoleCapabilityV1:
        return self.reference.capability

    @property
    def consent_eligible(self) -> bool:
        return self.reference.consent_eligible

    @property
    def export_eligible(self) -> bool:
        return self.reference.export_eligible

    @property
    def source_identities(self) -> tuple[ConsoleSourceIdentityV1, ...]:
        return self.reference.source_identities

    def as_dict(self) -> dict[str, object]:
        return {
            "ledger_entry_sha256": self.ledger_entry_sha256,
            "ledger_sequence": self.ledger_sequence,
            "reference": self.reference.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def row_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ConsoleViewRowV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "ledger_entry_sha256",
                    "ledger_sequence",
                    "reference",
                    "schema_id",
                    "schema_version",
                }
            ),
            "console view row",
        )
        return cls(
            reference=ConsoleArtifactReferenceV1.from_dict(value["reference"]),
            ledger_sequence=_positive_int(
                value["ledger_sequence"], "console view row ledger sequence"
            ),
            ledger_entry_sha256=_sha256(
                value["ledger_entry_sha256"], "console view row ledger entry digest"
            ),
            schema_id=_text(value["schema_id"], "console view row schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "console view row schema version"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ConsoleViewRowV1:
        return _from_json_bytes(cls, raw, "console view row")


@dataclass(frozen=True, slots=True)
class ConsoleViewV1:
    """A deterministic, scope-filtered view at one exact ledger point."""

    scope: InstructorQueryScopeV1
    as_of_sequence: int
    ledger_sha256: str
    rows: tuple[ConsoleViewRowV1, ...]
    schema_id: str = CONSOLE_VIEW_SCHEMA_ID
    schema_version: int = CONSOLE_VIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.scope) is not InstructorQueryScopeV1:
            raise TypeError("console view scope is invalid")
        _nonnegative_int(self.as_of_sequence, "console view as-of sequence")
        _sha256(self.ledger_sha256, "console view ledger digest")
        if type(self.rows) is not tuple or any(
            type(item) is not ConsoleViewRowV1 for item in self.rows
        ):
            raise TypeError("console view rows must be an immutable typed tuple")
        canonical = tuple(
            sorted(
                self.rows,
                key=lambda item: (
                    item.reference.artifact_kind.value,
                    item.reference.artifact_id,
                    item.ledger_sequence,
                    item.ledger_entry_sha256,
                ),
            )
        )
        if canonical != self.rows:
            raise ValueError("console view rows must be canonically ordered")
        if any(item.ledger_sequence > self.as_of_sequence for item in self.rows):
            raise ValueError("console view contains a post-as-of row")
        if any(
            not _reference_visible_in_scope(item.reference, self.scope)
            for item in self.rows
        ):
            raise PermissionError("console view contains an artifact outside its scope")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=CONSOLE_VIEW_SCHEMA_ID,
            expected_version=CONSOLE_VIEW_SCHEMA_VERSION,
            label="console view",
        )

    @property
    def content_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.content_version for item in self.rows)

    @property
    def scoring_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.scoring_version for item in self.rows)

    @property
    def model_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.model_version for item in self.rows)

    @property
    def analysis_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.analysis_version for item in self.rows)

    @property
    def sample_count(self) -> int:
        return sum(item.sample_count for item in self.rows)

    @property
    def uncertainty(self) -> tuple[str | None, ...]:
        return tuple(item.uncertainty_sha256 for item in self.rows)

    @property
    def capabilities(self) -> tuple[ConsoleCapabilityV1, ...]:
        return tuple(
            sorted(
                {item.capability for item in self.rows},
                key=lambda item: item.value,
            )
        )

    @property
    def consent_eligible(self) -> bool:
        return bool(self.rows) and all(item.consent_eligible for item in self.rows)

    @property
    def export_eligible(self) -> bool:
        return bool(self.rows) and all(item.export_eligible for item in self.rows)

    @property
    def source_identities(self) -> tuple[ConsoleSourceIdentityV1, ...]:
        keyed = {
            item.canonical_bytes(): item
            for row in self.rows
            for item in row.source_identities
        }
        return tuple(keyed[key] for key in sorted(keyed))

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_versions": [_version_json(item) for item in self.analysis_versions],
            "as_of_sequence": self.as_of_sequence,
            "capabilities": [item.value for item in self.capabilities],
            "consent_eligible": self.consent_eligible,
            "content_versions": [_version_json(item) for item in self.content_versions],
            "export_eligible": self.export_eligible,
            "ledger_sha256": self.ledger_sha256,
            "model_versions": [_version_json(item) for item in self.model_versions],
            "rows": [item.as_dict() for item in self.rows],
            "sample_count": self.sample_count,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scope": self.scope.as_dict(),
            "scoring_versions": [_version_json(item) for item in self.scoring_versions],
            "source_identities": [item.as_dict() for item in self.source_identities],
            "uncertainty": list(self.uncertainty),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def view_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ConsoleViewV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "analysis_versions",
                    "as_of_sequence",
                    "capabilities",
                    "consent_eligible",
                    "content_versions",
                    "export_eligible",
                    "ledger_sha256",
                    "model_versions",
                    "rows",
                    "sample_count",
                    "schema_id",
                    "schema_version",
                    "scope",
                    "scoring_versions",
                    "source_identities",
                    "uncertainty",
                }
            ),
            "console view",
        )
        view = cls(
            scope=InstructorQueryScopeV1.from_dict(value["scope"]),
            as_of_sequence=_nonnegative_int(
                value["as_of_sequence"], "console view as-of sequence"
            ),
            ledger_sha256=_sha256(value["ledger_sha256"], "console view ledger digest"),
            rows=tuple(
                ConsoleViewRowV1.from_dict(item)
                for item in _json_array(value["rows"], "console view rows")
            ),
            schema_id=_text(value["schema_id"], "console view schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "console view schema version"
            ),
        )
        expected_derived = {
            "analysis_versions": [_version_json(item) for item in view.analysis_versions],
            "capabilities": [item.value for item in view.capabilities],
            "consent_eligible": view.consent_eligible,
            "content_versions": [_version_json(item) for item in view.content_versions],
            "export_eligible": view.export_eligible,
            "model_versions": [_version_json(item) for item in view.model_versions],
            "sample_count": view.sample_count,
            "scoring_versions": [_version_json(item) for item in view.scoring_versions],
            "source_identities": [item.as_dict() for item in view.source_identities],
            "uncertainty": list(view.uncertainty),
        }
        if any(value[key] != item for key, item in expected_derived.items()):
            raise ValueError("serialized console view derived disclosure differs")
        return view

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ConsoleViewV1:
        return _from_json_bytes(cls, raw, "console view")


@dataclass(frozen=True, slots=True)
class ComparisonSourceV1:
    """One attempt source and its declared comparison dimensions."""

    reference: ConsoleArtifactReferenceV1
    learner_profile_id: str
    lesson_id: str | None
    skill_id: str | None
    scenario_id: str | None
    hotkey_layout_id: str | None
    session_id: str | None
    strategy_id: str | None
    volume_regime_id: str | None
    execution_mode: ComparisonExecutionModeV1
    schema_id: str = COMPARISON_SOURCE_SCHEMA_ID
    schema_version: int = COMPARISON_SOURCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.reference) is not ConsoleArtifactReferenceV1:
            raise TypeError("comparison source reference is invalid")
        if self.reference.artifact_kind.value not in {
            "ATTEMPT",
            "ASSIGNMENT_ATTEMPT",
        }:
            raise ValueError("comparison sources must reference assignment attempts")
        _identifier(
            self.reference.artifact_id,
            "comparison attempt ID",
            pattern=_ATTEMPT_ID,
        )
        require_learner_profile_id(self.learner_profile_id)
        for value, label in (
            (self.lesson_id, "comparison lesson ID"),
            (self.skill_id, "comparison skill ID"),
            (self.scenario_id, "comparison scenario ID"),
            (self.hotkey_layout_id, "comparison hotkey layout ID"),
            (self.session_id, "comparison session ID"),
            (self.strategy_id, "comparison strategy ID"),
            (self.volume_regime_id, "comparison volume regime ID"),
        ):
            if value is not None:
                _identifier(value, label)
        if type(self.execution_mode) is not ComparisonExecutionModeV1:
            raise TypeError("comparison execution mode is invalid")
        if self.reference.sample_count <= 0:
            raise ValueError("comparison sources require at least one sample")
        if self.reference.uncertainty_sha256 is None:
            raise ValueError("comparison sources require declared uncertainty")
        if self.reference.capability is ConsoleCapabilityV1.NOT_APPLICABLE:
            raise ValueError("comparison sources require descriptive capability")
        if not self.reference.consent_eligible:
            raise ValueError("comparison source is not consent eligible")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COMPARISON_SOURCE_SCHEMA_ID,
            expected_version=COMPARISON_SOURCE_SCHEMA_VERSION,
            label="comparison source",
        )

    @property
    def attempt_id(self) -> str:
        return self.reference.artifact_id

    @property
    def attempt_sha256(self) -> str:
        return self.reference.artifact_sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "execution_mode": self.execution_mode.value,
            "hotkey_layout_id": self.hotkey_layout_id,
            "learner_profile_id": self.learner_profile_id,
            "lesson_id": self.lesson_id,
            "reference": self.reference.as_dict(),
            "scenario_id": self.scenario_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "skill_id": self.skill_id,
            "strategy_id": self.strategy_id,
            "volume_regime_id": self.volume_regime_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ComparisonSourceV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "execution_mode",
                    "hotkey_layout_id",
                    "learner_profile_id",
                    "lesson_id",
                    "reference",
                    "scenario_id",
                    "schema_id",
                    "schema_version",
                    "session_id",
                    "skill_id",
                    "strategy_id",
                    "volume_regime_id",
                }
            ),
            "comparison source",
        )
        return cls(
            reference=ConsoleArtifactReferenceV1.from_dict(value["reference"]),
            learner_profile_id=_text(
                value["learner_profile_id"], "comparison learner profile ID"
            ),
            lesson_id=_optional_text(value["lesson_id"], "comparison lesson ID"),
            skill_id=_optional_text(value["skill_id"], "comparison skill ID"),
            scenario_id=_optional_text(value["scenario_id"], "comparison scenario ID"),
            hotkey_layout_id=_optional_text(
                value["hotkey_layout_id"], "comparison hotkey layout ID"
            ),
            session_id=_optional_text(value["session_id"], "comparison session ID"),
            strategy_id=_optional_text(value["strategy_id"], "comparison strategy ID"),
            volume_regime_id=_optional_text(
                value["volume_regime_id"], "comparison volume regime ID"
            ),
            execution_mode=ComparisonExecutionModeV1(
                _text(value["execution_mode"], "comparison execution mode")
            ),
            schema_id=_text(value["schema_id"], "comparison source schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "comparison source schema version"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ComparisonSourceV1:
        return _from_json_bytes(cls, raw, "comparison source")


@dataclass(frozen=True, slots=True)
class _ComparisonRuleV1:
    constant_fields: tuple[str, ...]
    varied_field: str
    requires_cohort_scope: bool = False
    exact_execution_modes: frozenset[ComparisonExecutionModeV1] | None = None


_COMPARISON_RULES = MappingProxyType(
    {
        ComparisonViewKindV1.SAME_LEARNER_ACROSS_ATTEMPTS: _ComparisonRuleV1(
            constant_fields=("learner_profile_id",),
            varied_field="attempt_id",
        ),
        ComparisonViewKindV1.SAME_LESSON_ACROSS_LEARNERS: _ComparisonRuleV1(
            constant_fields=("lesson_id",),
            varied_field="learner_profile_id",
            requires_cohort_scope=True,
        ),
        ComparisonViewKindV1.SAME_SKILL_ACROSS_SCENARIOS: _ComparisonRuleV1(
            constant_fields=("skill_id",),
            varied_field="scenario_id",
        ),
        ComparisonViewKindV1.SAME_HOTKEY_LAYOUT_ACROSS_SESSIONS: _ComparisonRuleV1(
            constant_fields=("hotkey_layout_id",),
            varied_field="session_id",
        ),
        ComparisonViewKindV1.SAME_STRATEGY_ACROSS_VOLUME_REGIMES: _ComparisonRuleV1(
            constant_fields=("strategy_id",),
            varied_field="volume_regime_id",
        ),
        ComparisonViewKindV1.MANUAL_EXECUTION_VS_BENCHMARK_ALGORITHM: (
            _ComparisonRuleV1(
                constant_fields=(),
                varied_field="execution_mode",
                exact_execution_modes=frozenset(
                    {
                        ComparisonExecutionModeV1.MANUAL,
                        ComparisonExecutionModeV1.BENCHMARK_ALGORITHM,
                    }
                ),
            )
        ),
    }
)

_LEARNER_SELF_SHARED_KINDS = frozenset(
    {
        ConsoleArtifactKindV1.ASSIGNMENT,
        ConsoleArtifactKindV1.RUBRIC,
    }
)


@dataclass(frozen=True, slots=True)
class ComparisonViewV1:
    """A version-disclosed descriptive source set, never a ranking."""

    view_kind: ComparisonViewKindV1
    scope: InstructorQueryScopeV1
    as_of_sequence: int
    ledger_sha256: str
    sources: tuple[ComparisonSourceV1, ...]
    scope_source_identities: tuple[ConsoleSourceIdentityV1, ...] = ()
    interpretation: str = COMPARISON_INTERPRETATION_V1
    capability: ConsoleCapabilityV1 = ConsoleCapabilityV1.DESCRIPTIVE
    schema_id: str = COMPARISON_VIEW_SCHEMA_ID
    schema_version: int = COMPARISON_VIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.view_kind) is not ComparisonViewKindV1:
            raise TypeError("comparison view kind is invalid")
        if type(self.scope) is not InstructorQueryScopeV1:
            raise TypeError("comparison query scope is invalid")
        _nonnegative_int(self.as_of_sequence, "comparison as-of sequence")
        _sha256(self.ledger_sha256, "comparison ledger digest")
        if type(self.sources) is not tuple or any(
            type(item) is not ComparisonSourceV1 for item in self.sources
        ):
            raise TypeError("comparison sources must be an immutable typed tuple")
        canonical = tuple(
            sorted(
                self.sources,
                key=lambda item: (
                    item.attempt_id,
                    item.attempt_sha256,
                    item.source_sha256,
                ),
            )
        )
        if canonical != self.sources:
            raise ValueError("comparison sources must be canonically ordered")
        if len(self.sources) < 2:
            raise ValueError("comparison requires at least two source attempts")
        if len({item.attempt_id for item in self.sources}) < 2:
            raise ValueError("comparison cannot rank one attempt against itself")
        if len({item.learner_profile_id for item in self.sources}) > 1 and (
            not self.scope.allows_cross_learner
        ):
            raise PermissionError(
                "cross-learner comparison requires explicit cohort-research scope"
            )
        if self.scope.scope_kind is InstructorQueryScopeKindV1.LEARNER_SELF and any(
            item.learner_profile_id != self.scope.learner_profile_id
            for item in self.sources
        ):
            raise PermissionError("learner-self comparison leaks another profile")
        if type(self.scope_source_identities) is not tuple or any(
            type(item) is not ConsoleSourceIdentityV1
            for item in self.scope_source_identities
        ):
            raise TypeError("comparison scope sources must be an immutable typed tuple")
        canonical_scope_sources = tuple(
            sorted(
                set(self.scope_source_identities),
                key=lambda item: item.canonical_bytes(),
            )
        )
        if canonical_scope_sources != self.scope_source_identities:
            raise ValueError("comparison scope sources must be unique and canonical")
        if self.scope.scope_kind is InstructorQueryScopeKindV1.COHORT_RESEARCH:
            if len(self.scope_source_identities) != 2 or {
                (item.source_kind, item.source_id)
                for item in self.scope_source_identities
            } != {
                ("COHORT", self.scope.cohort_id),
                ("STUDY", self.scope.study_id),
            }:
                raise ValueError(
                    "cohort comparison must expose exact cohort and study sources"
                )
        elif self.scope_source_identities:
            raise ValueError("non-research comparison cannot carry research sources")
        _validate_comparison_rule(self.view_kind, self.scope, self.sources)
        if self.interpretation != COMPARISON_INTERPRETATION_V1:
            raise ValueError("comparison interpretation exceeds descriptive source set")
        if self.capability is not ConsoleCapabilityV1.DESCRIPTIVE:
            raise ValueError("comparison view capability must remain descriptive")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COMPARISON_VIEW_SCHEMA_ID,
            expected_version=COMPARISON_VIEW_SCHEMA_VERSION,
            label="comparison view",
        )

    @property
    def content_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.reference.content_version for item in self.sources)

    @property
    def scoring_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.reference.scoring_version for item in self.sources)

    @property
    def model_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.reference.model_version for item in self.sources)

    @property
    def analysis_versions(self) -> tuple[str, ...]:
        return _unique_versions(item.reference.analysis_version for item in self.sources)

    @property
    def sample_count(self) -> int:
        return sum(item.reference.sample_count for item in self.sources)

    @property
    def uncertainty(self) -> tuple[str, ...]:
        return tuple(
            item.reference.uncertainty_sha256  # type: ignore[misc]
            for item in self.sources
        )

    @property
    def consent_eligible(self) -> bool:
        return all(item.reference.consent_eligible for item in self.sources)

    @property
    def export_eligible(self) -> bool:
        return all(item.reference.export_eligible for item in self.sources)

    @property
    def source_identities(self) -> tuple[ConsoleSourceIdentityV1, ...]:
        keyed = {
            item.canonical_bytes(): item
            for item in (
                self.scope_source_identities
                + tuple(
                    identity
                    for source in self.sources
                    for identity in source.reference.source_identities
                )
            )
        }
        return tuple(keyed[key] for key in sorted(keyed))

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_versions": list(self.analysis_versions),
            "as_of_sequence": self.as_of_sequence,
            "capability": self.capability.value,
            "consent_eligible": self.consent_eligible,
            "content_versions": list(self.content_versions),
            "export_eligible": self.export_eligible,
            "interpretation": self.interpretation,
            "ledger_sha256": self.ledger_sha256,
            "model_versions": list(self.model_versions),
            "sample_count": self.sample_count,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scope": self.scope.as_dict(),
            "scope_source_identities": [
                item.as_dict() for item in self.scope_source_identities
            ],
            "scoring_versions": list(self.scoring_versions),
            "source_identities": [item.as_dict() for item in self.source_identities],
            "sources": [item.as_dict() for item in self.sources],
            "uncertainty": list(self.uncertainty),
            "view_kind": self.view_kind.value,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def comparison_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def comparison_id(self) -> str:
        return f"instructor-comparison-{self.comparison_sha256}"

    @classmethod
    def from_dict(cls, raw: object) -> ComparisonViewV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "analysis_versions",
                    "as_of_sequence",
                    "capability",
                    "consent_eligible",
                    "content_versions",
                    "export_eligible",
                    "interpretation",
                    "ledger_sha256",
                    "model_versions",
                    "sample_count",
                    "schema_id",
                    "schema_version",
                    "scope",
                    "scope_source_identities",
                    "scoring_versions",
                    "source_identities",
                    "sources",
                    "uncertainty",
                    "view_kind",
                }
            ),
            "comparison view",
        )
        view = cls(
            view_kind=ComparisonViewKindV1(
                _text(value["view_kind"], "comparison view kind")
            ),
            scope=InstructorQueryScopeV1.from_dict(value["scope"]),
            as_of_sequence=_nonnegative_int(
                value["as_of_sequence"], "comparison as-of sequence"
            ),
            ledger_sha256=_sha256(
                value["ledger_sha256"], "comparison ledger digest"
            ),
            sources=tuple(
                ComparisonSourceV1.from_dict(item)
                for item in _json_array(value["sources"], "comparison sources")
            ),
            scope_source_identities=tuple(
                ConsoleSourceIdentityV1.from_dict(item)
                for item in _json_array(
                    value["scope_source_identities"],
                    "comparison scope source identities",
                )
            ),
            interpretation=_text(
                value["interpretation"], "comparison interpretation"
            ),
            capability=ConsoleCapabilityV1(
                _text(value["capability"], "comparison capability")
            ),
            schema_id=_text(value["schema_id"], "comparison view schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "comparison view schema version"
            ),
        )
        expected_derived = {
            "analysis_versions": list(view.analysis_versions),
            "consent_eligible": view.consent_eligible,
            "content_versions": list(view.content_versions),
            "export_eligible": view.export_eligible,
            "model_versions": list(view.model_versions),
            "sample_count": view.sample_count,
            "scoring_versions": list(view.scoring_versions),
            "source_identities": [item.as_dict() for item in view.source_identities],
            "uncertainty": list(view.uncertainty),
        }
        if any(value[key] != item for key, item in expected_derived.items()):
            raise ValueError("serialized comparison derived disclosure differs")
        return view

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ComparisonViewV1:
        return _from_json_bytes(cls, raw, "comparison view")


def _validate_comparison_rule(
    view_kind: ComparisonViewKindV1,
    scope: InstructorQueryScopeV1,
    sources: tuple[ComparisonSourceV1, ...],
) -> None:
    rule = _COMPARISON_RULES[view_kind]
    if rule.requires_cohort_scope and (
        scope.scope_kind is not InstructorQueryScopeKindV1.COHORT_RESEARCH
    ):
        raise PermissionError(f"{view_kind.value} requires cohort-research scope")
    for field_name in rule.constant_fields:
        values = {getattr(item, field_name) for item in sources}
        if None in values:
            raise ValueError(
                f"{view_kind.value} requires declared {field_name} on every source"
            )
        if len(values) != 1:
            raise ValueError(f"{view_kind.value} must hold {field_name} constant")
    varied_values = {getattr(item, rule.varied_field) for item in sources}
    if None in varied_values:
        raise ValueError(
            f"{view_kind.value} requires declared {rule.varied_field} on every source"
        )
    if len(varied_values) < 2:
        raise ValueError(
            f"{view_kind.value} requires at least two distinct {rule.varied_field} values"
        )
    if rule.exact_execution_modes is not None and (
        varied_values != rule.exact_execution_modes
    ):
        raise ValueError(
            "manual-versus-benchmark comparison requires both and only those modes"
        )


@dataclass(frozen=True, slots=True)
class MicroscopeLinkV1:
    """Local content-addressed link from an attempt to an exact replay cursor."""

    attempt_id: str
    attempt_sha256: str
    timeline_id: str
    cursor_id: str
    cursor_sha256: str
    source_run_id: str
    source_event_sha256: str
    render_cursor_time_us: int
    observation_mode: str
    schema_id: str = MICROSCOPE_LINK_SCHEMA_ID
    schema_version: int = MICROSCOPE_LINK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "microscope-link attempt ID", pattern=_ATTEMPT_ID)
        _sha256(self.attempt_sha256, "microscope-link attempt digest")
        _identifier(self.timeline_id, "microscope-link timeline ID")
        _identifier(self.cursor_id, "microscope-link cursor ID")
        _sha256(self.cursor_sha256, "microscope-link cursor digest")
        _identifier(self.source_run_id, "microscope-link run ID", pattern=_RUN_ID)
        _sha256(self.source_event_sha256, "microscope-link source-event digest")
        _nonnegative_int(
            self.render_cursor_time_us,
            "microscope-link render cursor",
        )
        _identifier(self.observation_mode, "microscope-link observation mode")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=MICROSCOPE_LINK_SCHEMA_ID,
            expected_version=MICROSCOPE_LINK_SCHEMA_VERSION,
            label="microscope link",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_sha256": self.attempt_sha256,
            "cursor_id": self.cursor_id,
            "cursor_sha256": self.cursor_sha256,
            "observation_mode": self.observation_mode,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "timeline_id": self.timeline_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def link_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def link_id(self) -> str:
        return f"instructor-microscope-link-{self.link_sha256}"

    @classmethod
    def from_dict(cls, raw: object) -> MicroscopeLinkV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "attempt_id",
                    "attempt_sha256",
                    "cursor_id",
                    "cursor_sha256",
                    "observation_mode",
                    "render_cursor_time_us",
                    "schema_id",
                    "schema_version",
                    "source_event_sha256",
                    "source_run_id",
                    "timeline_id",
                }
            ),
            "microscope link",
        )
        return cls(
            attempt_id=_text(value["attempt_id"], "microscope-link attempt ID"),
            attempt_sha256=_sha256(
                value["attempt_sha256"], "microscope-link attempt digest"
            ),
            timeline_id=_text(value["timeline_id"], "microscope-link timeline ID"),
            cursor_id=_text(value["cursor_id"], "microscope-link cursor ID"),
            cursor_sha256=_sha256(
                value["cursor_sha256"], "microscope-link cursor digest"
            ),
            source_run_id=_text(
                value["source_run_id"], "microscope-link source run ID"
            ),
            source_event_sha256=_sha256(
                value["source_event_sha256"],
                "microscope-link source-event digest",
            ),
            render_cursor_time_us=_nonnegative_int(
                value["render_cursor_time_us"],
                "microscope-link render cursor",
            ),
            observation_mode=_text(
                value["observation_mode"], "microscope-link observation mode"
            ),
            schema_id=_text(value["schema_id"], "microscope-link schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "microscope-link schema version"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> MicroscopeLinkV1:
        return _from_json_bytes(cls, raw, "microscope link")


def create_microscope_link(
    *,
    attempt_reference: ConsoleArtifactReferenceV1,
    cursor: object,
) -> MicroscopeLinkV1:
    """Bind an attempt to a cursor produced by the local replay microscope.

    A structural cursor boundary avoids importing the large presentation stack.
    Exact field validation and the cursor's canonical digest still make the link
    fail closed if an unrelated object is supplied.
    """

    if type(attempt_reference) is not ConsoleArtifactReferenceV1:
        raise TypeError("microscope link requires a console attempt reference")
    if attempt_reference.artifact_kind is not ConsoleArtifactKindV1.ASSIGNMENT_ATTEMPT:
        raise ValueError("microscope link requires an assignment-attempt reference")
    if (
        type(getattr(cursor, "schema_id", None)) is not str
        or getattr(cursor, "schema_id", None) != MICROSCOPE_TIMELINE_CURSOR_SCHEMA_ID
        or type(getattr(cursor, "schema_version", None)) is not int
        or getattr(cursor, "schema_version", None)
        != MICROSCOPE_TIMELINE_CURSOR_SCHEMA_VERSION
    ):
        raise TypeError("microscope link requires a V1 replay-timeline cursor")
    canonical_bytes = getattr(cursor, "canonical_bytes", None)
    if not callable(canonical_bytes):
        raise TypeError("microscope cursor must expose canonical_bytes()")
    raw_cursor = canonical_bytes()
    if type(raw_cursor) is not bytes or not raw_cursor:
        raise ValueError("microscope cursor canonical bytes are invalid")
    observation_mode = getattr(cursor, "observation_mode", None)
    observation_value = getattr(observation_mode, "value", observation_mode)
    return MicroscopeLinkV1(
        attempt_id=attempt_reference.artifact_id,
        attempt_sha256=attempt_reference.artifact_sha256,
        timeline_id=getattr(cursor, "timeline_id", None),
        cursor_id=getattr(cursor, "cursor_id", None),
        cursor_sha256=hashlib.sha256(raw_cursor).hexdigest(),
        source_run_id=getattr(cursor, "source_run_id", None),
        source_event_sha256=getattr(cursor, "source_event_sha256", None),
        render_cursor_time_us=getattr(cursor, "render_cursor_time_us", None),
        observation_mode=observation_value,
    )


def _reference_learner_ids(
    reference: ConsoleArtifactReferenceV1,
) -> frozenset[str]:
    return frozenset(
        item.source_id
        for item in reference.source_identities
        if item.source_id.startswith("learner-profile-")
    )


def _reference_visible_in_scope(
    reference: ConsoleArtifactReferenceV1,
    scope: InstructorQueryScopeV1,
) -> bool:
    learner_ids = _reference_learner_ids(reference)
    if scope.scope_kind is InstructorQueryScopeKindV1.LEARNER_SELF:
        if learner_ids:
            return learner_ids == frozenset({scope.learner_profile_id})
        return reference.artifact_kind in _LEARNER_SELF_SHARED_KINDS
    if scope.scope_kind is InstructorQueryScopeKindV1.INSTRUCTOR_LOCAL:
        return True
    research_ids = frozenset({scope.cohort_id, scope.study_id})
    if reference.artifact_id in research_ids:
        return True
    return any(item.source_id in research_ids for item in reference.source_identities)


def _query_artifact_kinds(
    ledger: InstructorConsoleLedgerV1,
    *,
    scope: InstructorQueryScopeV1,
    as_of: int,
    artifact_kinds: tuple[ConsoleArtifactKindV1, ...],
) -> ConsoleViewV1:
    if type(ledger) is not InstructorConsoleLedgerV1:
        raise TypeError("console query requires InstructorConsoleLedgerV1")
    if type(scope) is not InstructorQueryScopeV1:
        raise TypeError("console query requires InstructorQueryScopeV1")
    if type(artifact_kinds) is not tuple or any(
        type(item) is not ConsoleArtifactKindV1 for item in artifact_kinds
    ):
        raise TypeError("artifact kinds must be an immutable typed tuple")
    selected = frozenset(artifact_kinds)
    snapshot = ledger.as_of(as_of)
    _require_research_scope_bindings(snapshot, scope)
    rows = tuple(
        sorted(
            (
                ConsoleViewRowV1(
                    reference=entry.artifact_reference,
                    ledger_sequence=entry.sequence_number,
                    ledger_entry_sha256=entry.sha256,
                )
                for entry in snapshot.entries
                if (not selected or entry.artifact_reference.artifact_kind in selected)
                and _reference_visible_in_scope(entry.artifact_reference, scope)
            ),
            key=lambda item: (
                item.reference.artifact_kind.value,
                item.reference.artifact_id,
                item.ledger_sequence,
                item.ledger_entry_sha256,
            ),
        )
    )
    return ConsoleViewV1(
        scope=scope,
        as_of_sequence=snapshot.head_sequence,
        ledger_sha256=snapshot.head_sha256,
        rows=rows,
    )


def query_console_artifacts(
    ledger: InstructorConsoleLedgerV1,
    *,
    scope: InstructorQueryScopeV1,
    as_of: int,
    artifact_kinds: tuple[ConsoleArtifactKindV1, ...] = (),
) -> ConsoleViewV1:
    """List visible artifacts at an explicit append-only ledger point."""

    return _query_artifact_kinds(
        ledger,
        scope=scope,
        as_of=as_of,
        artifact_kinds=artifact_kinds,
    )


_LIST_OPERATIONS = MappingProxyType(
    {
        "profiles": (ConsoleArtifactKindV1.PROFILE,),
        "assignments": (ConsoleArtifactKindV1.ASSIGNMENT,),
        "attempts": (ConsoleArtifactKindV1.ASSIGNMENT_ATTEMPT,),
        "rubrics": (ConsoleArtifactKindV1.RUBRIC,),
        "reviews": (ConsoleArtifactKindV1.REVIEW,),
        "cohorts": (ConsoleArtifactKindV1.COHORT,),
        "studies": (ConsoleArtifactKindV1.STUDY,),
        "amendments": (ConsoleArtifactKindV1.AMENDMENT,),
        "comparisons": (ConsoleArtifactKindV1.COMPARISON,),
        "microscope_links": (ConsoleArtifactKindV1.MICROSCOPE_LINK,),
    }
)


def _list_operation(name: str):
    kinds = _LIST_OPERATIONS[name]

    def operation(
        ledger: InstructorConsoleLedgerV1,
        *,
        scope: InstructorQueryScopeV1,
        as_of: int,
    ) -> ConsoleViewV1:
        return _query_artifact_kinds(
            ledger,
            scope=scope,
            as_of=as_of,
            artifact_kinds=kinds,
        )

    return operation


list_profiles = _list_operation("profiles")
list_assignments = _list_operation("assignments")
list_attempts = _list_operation("attempts")
list_rubrics = _list_operation("rubrics")
list_reviews = _list_operation("reviews")
list_cohorts = _list_operation("cohorts")
list_studies = _list_operation("studies")
list_amendments = _list_operation("amendments")
list_comparisons = _list_operation("comparisons")
list_microscope_links = _list_operation("microscope_links")


def _require_research_scope_bindings(
    snapshot: InstructorConsoleLedgerV1,
    scope: InstructorQueryScopeV1,
) -> tuple[ConsoleArtifactReferenceV1, ConsoleArtifactReferenceV1] | None:
    if scope.scope_kind is not InstructorQueryScopeKindV1.COHORT_RESEARCH:
        return None
    bound: dict[ConsoleArtifactKindV1, ConsoleArtifactReferenceV1] = {}
    for entry in snapshot.entries:
        reference = entry.artifact_reference
        expected_id = (
            scope.cohort_id
            if reference.artifact_kind is ConsoleArtifactKindV1.COHORT
            else scope.study_id
        )
        if (
            reference.artifact_kind
            in {ConsoleArtifactKindV1.COHORT, ConsoleArtifactKindV1.STUDY}
            and reference.artifact_id == expected_id
        ):
            bound[reference.artifact_kind] = reference
    if set(bound) != {ConsoleArtifactKindV1.COHORT, ConsoleArtifactKindV1.STUDY}:
        raise PermissionError(
            "cohort-research scope must bind cohort and study artifacts present as_of"
        )
    cohort_reference = bound[ConsoleArtifactKindV1.COHORT]
    study_reference = bound[ConsoleArtifactKindV1.STUDY]
    if not any(
        item.source_id == study_reference.artifact_id
        and item.source_sha256 == study_reference.artifact_sha256
        for item in cohort_reference.source_identities
    ):
        raise PermissionError(
            "cohort artifact does not bind the scoped study ID and digest"
        )
    return cohort_reference, study_reference


def build_comparison_view(
    ledger: InstructorConsoleLedgerV1,
    *,
    view_kind: ComparisonViewKindV1,
    scope: InstructorQueryScopeV1,
    sources: tuple[ComparisonSourceV1, ...],
    as_of: int,
) -> ComparisonViewV1:
    """Build one of six descriptive comparisons from exact as-of attempts."""

    if type(ledger) is not InstructorConsoleLedgerV1:
        raise TypeError("comparison query requires InstructorConsoleLedgerV1")
    if type(view_kind) is not ComparisonViewKindV1:
        raise TypeError("comparison view kind is invalid")
    if type(scope) is not InstructorQueryScopeV1:
        raise TypeError("comparison scope is invalid")
    if type(sources) is not tuple or any(
        type(item) is not ComparisonSourceV1 for item in sources
    ):
        raise TypeError("comparison sources must be an immutable typed tuple")
    snapshot = ledger.as_of(as_of)
    research_bindings = _require_research_scope_bindings(snapshot, scope)
    indexed_references = {
        (
            entry.artifact_reference.artifact_kind,
            entry.artifact_reference.artifact_id,
            entry.artifact_reference.artifact_sha256,
        ): entry.artifact_reference
        for entry in snapshot.entries
    }
    for source in sources:
        identity = (
            source.reference.artifact_kind,
            source.reference.artifact_id,
            source.reference.artifact_sha256,
        )
        if indexed_references.get(identity) != source.reference:
            raise ValueError("comparison source is absent or differs at as_of")
        if source.learner_profile_id not in {
            item.source_id for item in source.reference.source_identities
        }:
            raise ValueError(
                "comparison learner differs from immutable attempt source identity"
            )
        if research_bindings is not None:
            cohort_reference, _study_reference = research_bindings
            if not any(
                item.source_id == source.attempt_id
                and item.source_sha256 == source.attempt_sha256
                for item in cohort_reference.source_identities
            ):
                raise PermissionError(
                    "comparison attempt is not an exact source of the scoped cohort"
                )
    canonical_sources = tuple(
        sorted(
            sources,
            key=lambda item: (
                item.attempt_id,
                item.attempt_sha256,
                item.source_sha256,
            ),
        )
    )
    return ComparisonViewV1(
        view_kind=view_kind,
        scope=scope,
        as_of_sequence=snapshot.head_sequence,
        ledger_sha256=snapshot.head_sha256,
        sources=canonical_sources,
        scope_source_identities=(
            ()
            if research_bindings is None
            else tuple(
                sorted(
                    (
                        ConsoleSourceIdentityV1(
                            source_kind="COHORT",
                            source_id=research_bindings[0].artifact_id,
                            source_sha256=research_bindings[0].artifact_sha256,
                        ),
                        ConsoleSourceIdentityV1(
                            source_kind="STUDY",
                            source_id=research_bindings[1].artifact_id,
                            source_sha256=research_bindings[1].artifact_sha256,
                        ),
                    ),
                    key=lambda item: item.canonical_bytes(),
                )
            )
        ),
    )


def load_instructor_query_scope(raw: bytes) -> InstructorQueryScopeV1:
    return InstructorQueryScopeV1.from_canonical_bytes(raw)


def load_console_view_row(raw: bytes) -> ConsoleViewRowV1:
    return ConsoleViewRowV1.from_canonical_bytes(raw)


def load_console_view(raw: bytes) -> ConsoleViewV1:
    return ConsoleViewV1.from_canonical_bytes(raw)


def load_comparison_source(raw: bytes) -> ComparisonSourceV1:
    return ComparisonSourceV1.from_canonical_bytes(raw)


def load_comparison_view(raw: bytes) -> ComparisonViewV1:
    return ComparisonViewV1.from_canonical_bytes(raw)


def load_microscope_link(raw: bytes) -> MicroscopeLinkV1:
    return MicroscopeLinkV1.from_canonical_bytes(raw)


__all__ = [
    "COMPARISON_INTERPRETATION_V1",
    "COMPARISON_SOURCE_SCHEMA_ID",
    "COMPARISON_SOURCE_SCHEMA_VERSION",
    "COMPARISON_VIEW_SCHEMA_ID",
    "COMPARISON_VIEW_SCHEMA_VERSION",
    "CONSOLE_VIEW_ROW_SCHEMA_ID",
    "CONSOLE_VIEW_ROW_SCHEMA_VERSION",
    "CONSOLE_VIEW_SCHEMA_ID",
    "CONSOLE_VIEW_SCHEMA_VERSION",
    "INSTRUCTOR_QUERY_SCOPE_SCHEMA_ID",
    "INSTRUCTOR_QUERY_SCOPE_SCHEMA_VERSION",
    "MICROSCOPE_LINK_SCHEMA_ID",
    "MICROSCOPE_LINK_SCHEMA_VERSION",
    "ComparisonExecutionModeV1",
    "ComparisonSourceV1",
    "ComparisonViewKindV1",
    "ComparisonViewV1",
    "ConsoleViewRowV1",
    "ConsoleViewV1",
    "InstructorQueryScopeKindV1",
    "InstructorQueryScopeV1",
    "MicroscopeLinkV1",
    "build_comparison_view",
    "create_microscope_link",
    "list_amendments",
    "list_assignments",
    "list_attempts",
    "list_cohorts",
    "list_comparisons",
    "list_microscope_links",
    "list_profiles",
    "list_reviews",
    "list_rubrics",
    "list_studies",
    "load_comparison_source",
    "load_comparison_view",
    "load_console_view",
    "load_console_view_row",
    "load_instructor_query_scope",
    "load_microscope_link",
    "query_console_artifacts",
]
