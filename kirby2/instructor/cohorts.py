"""Immutable cohort definitions and version-safe descriptive summaries.

A cohort is a protocol-bound selection, not a mutable list in an instructor UI.
The definition in this module commits to one exact research-study revision and
protocol lock before source attempts are summarized.  Membership is either an
explicit, canonically ordered tuple of pseudonymous learner profile IDs or an
exact ID-and-digest binding to a membership policy; the two modes cannot be
combined.

Summary construction is deliberately strict.  Every metric observation must map
one-to-one to an immutable source-attempt ID and digest.  Version compatibility
and statistical capability decisions come from :mod:`kirby2.instructor.statistics`.
Mixed version signatures are therefore stratified or refused and are never
silently pooled.  Descriptive language is the default, and a causal view invokes
the shared capability gate for both study design and analysis support.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from .models import Cohort, create_cohort_revision, require_learner_profile_id
from .statistics import (
    AnalysisCapabilityV1,
    CompatibilityActionV1,
    CompatibilityDecisionV1,
    DescriptiveEstimateV1,
    DescriptiveSummaryV1,
    MetricObservationV1,
    MissingReasonCountV1,
    UnsupportedCausalClaimError,
    UncertaintyIntervalV1,
    VersionSignatureV1,
    require_claim_capability,
    summarize_observations,
)


COHORT_MEMBERSHIP_POLICY_SCHEMA_ID = "KIRBY2_COHORT_MEMBERSHIP_POLICY_V1"
COHORT_MEMBERSHIP_POLICY_SCHEMA_VERSION = 1
COHORT_ASSIGNMENT_BINDING_SCHEMA_ID = "KIRBY2_COHORT_ASSIGNMENT_BINDING_V1"
COHORT_ASSIGNMENT_BINDING_SCHEMA_VERSION = 1
COHORT_SOURCE_ATTEMPT_SCHEMA_ID = "KIRBY2_COHORT_SOURCE_ATTEMPT_V1"
COHORT_SOURCE_ATTEMPT_SCHEMA_VERSION = 1
COHORT_DEFINITION_SCHEMA_ID = "KIRBY2_COHORT_DEFINITION_V1"
COHORT_DEFINITION_SCHEMA_VERSION = 1
COHORT_REVISION_SCHEMA_ID = "KIRBY2_COHORT_REVISION_V1"
COHORT_REVISION_SCHEMA_VERSION = 1
COHORT_SUMMARY_SCHEMA_ID = "KIRBY2_COHORT_SUMMARY_V1"
COHORT_SUMMARY_SCHEMA_VERSION = 1


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_ASSIGNMENT_ID = re.compile(r"assignment-[0-9a-f]{64}\Z")
_ATTEMPT_ID = re.compile(r"assignment-attempt-[0-9a-f]{64}\Z")
_STUDY_ID = re.compile(r"research-study-[0-9a-f]{64}\Z")
_COHORT_ID = re.compile(r"cohort-[0-9a-f]{64}\Z")


class CohortMembershipModeV1(str, Enum):
    """Closed choice between explicit pseudonyms and a locked policy."""

    EXPLICIT_PSEUDONYMOUS_MEMBERS = "EXPLICIT_PSEUDONYMOUS_MEMBERS"
    LOCKED_MEMBERSHIP_POLICY = "LOCKED_MEMBERSHIP_POLICY"


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
        raise ValueError("cohort record is not strict canonical JSON") from error


def _pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("cohort JSON contains a duplicate object key")
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
        raise ValueError(f"{label} fields differ from its V1 schema")
    return value


def _json_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact JSON array")
    return value


def _text(
    value: object,
    label: str,
    *,
    maximum_utf8_bytes: int = 4096,
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


def _identifier(
    value: object,
    pattern: re.Pattern[str],
    label: str,
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


def _canonical_text_tuple(
    value: object,
    label: str,
    *,
    allow_empty: bool,
    learner_profile_ids: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise TypeError(f"{label} must be an immutable text tuple")
    if not allow_empty and not value:
        raise ValueError(f"{label} cannot be empty")
    for item in value:
        if learner_profile_ids:
            require_learner_profile_id(item)
        else:
            _text(item, f"{label} member")
    canonical = tuple(sorted(set(value), key=lambda item: item.encode("utf-8")))
    if canonical != value:
        raise ValueError(f"{label} must be unique and canonically ordered")
    return value


def _canonical_typed_tuple(
    value: object,
    item_type: type,
    label: str,
    *,
    allow_empty: bool,
    key,
) -> tuple:
    if type(value) is not tuple or any(type(item) is not item_type for item in value):
        raise TypeError(f"{label} must be an immutable typed tuple")
    if not allow_empty and not value:
        raise ValueError(f"{label} cannot be empty")
    canonical = tuple(sorted(value, key=key))
    if canonical != value:
        raise ValueError(f"{label} must be canonically ordered")
    return value


def _from_json_bytes(record_type, raw: bytes, label: str):
    record = record_type.from_dict(_canonical_object(raw, label))
    if record.canonical_bytes() != raw:
        raise ValueError(f"{label} changed during exact restoration")
    return record


@dataclass(frozen=True, slots=True)
class CohortMembershipPolicyV1:
    """Exact immutable binding for policy-selected cohort membership."""

    policy_id: str
    policy_sha256: str
    schema_id: str = COHORT_MEMBERSHIP_POLICY_SCHEMA_ID
    schema_version: int = COHORT_MEMBERSHIP_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.policy_id, _POLICY_ID, "cohort membership policy ID")
        _sha256(self.policy_sha256, "cohort membership policy digest")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COHORT_MEMBERSHIP_POLICY_SCHEMA_ID,
            expected_version=COHORT_MEMBERSHIP_POLICY_SCHEMA_VERSION,
            label="cohort membership policy",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_sha256": self.policy_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> CohortMembershipPolicyV1:
        value = _exact_object(
            raw,
            frozenset({"policy_id", "policy_sha256", "schema_id", "schema_version"}),
            "cohort membership policy",
        )
        return cls(**value)  # type: ignore[arg-type]

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CohortMembershipPolicyV1:
        return _from_json_bytes(cls, raw, "cohort membership policy")


@dataclass(frozen=True, slots=True)
class CohortAssignmentBindingV1:
    """One assignment revision admitted by the locked cohort definition."""

    assignment_id: str
    assignment_sha256: str
    schema_id: str = COHORT_ASSIGNMENT_BINDING_SCHEMA_ID
    schema_version: int = COHORT_ASSIGNMENT_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.assignment_id, _ASSIGNMENT_ID, "cohort assignment ID")
        _sha256(self.assignment_sha256, "cohort assignment digest")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COHORT_ASSIGNMENT_BINDING_SCHEMA_ID,
            expected_version=COHORT_ASSIGNMENT_BINDING_SCHEMA_VERSION,
            label="cohort assignment binding",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "assignment_sha256": self.assignment_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> CohortAssignmentBindingV1:
        value = _exact_object(
            raw,
            frozenset(
                {"assignment_id", "assignment_sha256", "schema_id", "schema_version"}
            ),
            "cohort assignment binding",
        )
        return cls(**value)  # type: ignore[arg-type]

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CohortAssignmentBindingV1:
        return _from_json_bytes(cls, raw, "cohort assignment binding")


@dataclass(frozen=True, slots=True)
class CohortSourceAttemptV1:
    """Pseudonymous ID-and-digest binding to one immutable source attempt."""

    learner_profile_id: str
    assignment_id: str
    assignment_sha256: str
    attempt_id: str
    attempt_sha256: str
    schema_id: str = COHORT_SOURCE_ATTEMPT_SCHEMA_ID
    schema_version: int = COHORT_SOURCE_ATTEMPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_learner_profile_id(self.learner_profile_id)
        _identifier(self.assignment_id, _ASSIGNMENT_ID, "source assignment ID")
        _sha256(self.assignment_sha256, "source assignment digest")
        _identifier(self.attempt_id, _ATTEMPT_ID, "source attempt ID")
        _sha256(self.attempt_sha256, "source attempt digest")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COHORT_SOURCE_ATTEMPT_SCHEMA_ID,
            expected_version=COHORT_SOURCE_ATTEMPT_SCHEMA_VERSION,
            label="cohort source attempt",
        )

    @property
    def assignment_binding(self) -> tuple[str, str]:
        return (self.assignment_id, self.assignment_sha256)

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "assignment_sha256": self.assignment_sha256,
            "attempt_id": self.attempt_id,
            "attempt_sha256": self.attempt_sha256,
            "learner_profile_id": self.learner_profile_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> CohortSourceAttemptV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "assignment_id",
                    "assignment_sha256",
                    "attempt_id",
                    "attempt_sha256",
                    "learner_profile_id",
                    "schema_id",
                    "schema_version",
                }
            ),
            "cohort source attempt",
        )
        return cls(**value)  # type: ignore[arg-type]

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CohortSourceAttemptV1:
        return _from_json_bytes(cls, raw, "cohort source attempt")


@dataclass(frozen=True, slots=True)
class CohortDefinitionV1:
    """Complete immutable cohort selection committed before observation."""

    study_id: str
    study_sha256: str
    protocol_lock_sha256: str
    population: str
    inclusion_criteria: tuple[str, ...]
    exclusion_criteria: tuple[str, ...]
    member_profile_ids: tuple[str, ...]
    membership_policy: CohortMembershipPolicyV1 | None
    assignment_bindings: tuple[CohortAssignmentBindingV1, ...]
    metric_ids: tuple[str, ...]
    schema_id: str = COHORT_DEFINITION_SCHEMA_ID
    schema_version: int = COHORT_DEFINITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.study_id, _STUDY_ID, "cohort study ID")
        _sha256(self.study_sha256, "cohort study revision digest")
        _sha256(self.protocol_lock_sha256, "cohort protocol lock digest")
        _text(self.population, "cohort population")
        _canonical_text_tuple(
            self.inclusion_criteria,
            "cohort inclusion criteria",
            allow_empty=False,
        )
        _canonical_text_tuple(
            self.exclusion_criteria,
            "cohort exclusion criteria",
            allow_empty=True,
        )
        _canonical_text_tuple(
            self.member_profile_ids,
            "cohort member profile IDs",
            allow_empty=True,
            learner_profile_ids=True,
        )
        if self.membership_policy is not None and type(
            self.membership_policy
        ) is not CohortMembershipPolicyV1:
            raise TypeError(
                "cohort membership policy must be CohortMembershipPolicyV1 or None"
            )
        if bool(self.member_profile_ids) == (self.membership_policy is not None):
            raise ValueError(
                "cohort membership requires exactly one of explicit pseudonymous "
                "members or a locked membership policy"
            )
        _canonical_typed_tuple(
            self.assignment_bindings,
            CohortAssignmentBindingV1,
            "cohort assignment bindings",
            allow_empty=False,
            key=lambda item: (item.assignment_id, item.assignment_sha256),
        )
        assignment_ids = tuple(item.assignment_id for item in self.assignment_bindings)
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("cohort assignment IDs must be unique")
        _canonical_text_tuple(
            self.metric_ids,
            "cohort metric IDs",
            allow_empty=False,
        )
        for metric_id in self.metric_ids:
            _identifier(metric_id, _SEMANTIC_ID, "cohort metric ID")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COHORT_DEFINITION_SCHEMA_ID,
            expected_version=COHORT_DEFINITION_SCHEMA_VERSION,
            label="cohort definition",
        )

    @property
    def membership_mode(self) -> CohortMembershipModeV1:
        if self.member_profile_ids:
            return CohortMembershipModeV1.EXPLICIT_PSEUDONYMOUS_MEMBERS
        return CohortMembershipModeV1.LOCKED_MEMBERSHIP_POLICY

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_bindings": [
                item.as_dict() for item in self.assignment_bindings
            ],
            "exclusion_criteria": list(self.exclusion_criteria),
            "inclusion_criteria": list(self.inclusion_criteria),
            "member_profile_ids": list(self.member_profile_ids),
            "membership_mode": self.membership_mode.value,
            "membership_policy": (
                None
                if self.membership_policy is None
                else self.membership_policy.as_dict()
            ),
            "metric_ids": list(self.metric_ids),
            "population": self.population,
            "protocol_lock_sha256": self.protocol_lock_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "study_id": self.study_id,
            "study_sha256": self.study_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def content_sha256(self) -> str:
        return self.sha256

    @classmethod
    def from_dict(cls, raw: object) -> CohortDefinitionV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "assignment_bindings",
                    "exclusion_criteria",
                    "inclusion_criteria",
                    "member_profile_ids",
                    "membership_mode",
                    "membership_policy",
                    "metric_ids",
                    "population",
                    "protocol_lock_sha256",
                    "schema_id",
                    "schema_version",
                    "study_id",
                    "study_sha256",
                }
            ),
            "cohort definition",
        )
        raw_policy = value["membership_policy"]
        if raw_policy is not None and type(raw_policy) is not dict:
            raise TypeError("cohort membership policy must be null or an object")
        mode = CohortMembershipModeV1(
            _text(value["membership_mode"], "cohort membership mode")
        )
        definition = cls(
            study_id=value["study_id"],  # type: ignore[arg-type]
            study_sha256=value["study_sha256"],  # type: ignore[arg-type]
            protocol_lock_sha256=value["protocol_lock_sha256"],  # type: ignore[arg-type]
            population=value["population"],  # type: ignore[arg-type]
            inclusion_criteria=tuple(
                _json_array(value["inclusion_criteria"], "cohort inclusion criteria")
            ),
            exclusion_criteria=tuple(
                _json_array(value["exclusion_criteria"], "cohort exclusion criteria")
            ),
            member_profile_ids=tuple(
                _json_array(value["member_profile_ids"], "cohort member profile IDs")
            ),
            membership_policy=(
                None
                if raw_policy is None
                else CohortMembershipPolicyV1.from_dict(raw_policy)
            ),
            assignment_bindings=tuple(
                CohortAssignmentBindingV1.from_dict(item)
                for item in _json_array(
                    value["assignment_bindings"],
                    "cohort assignment bindings",
                )
            ),
            metric_ids=tuple(
                _json_array(value["metric_ids"], "cohort metric IDs")
            ),
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if definition.membership_mode is not mode:
            raise ValueError("serialized cohort membership mode differs")
        if definition.as_dict() != value:
            raise ValueError("cohort definition did not round-trip exactly")
        return definition

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CohortDefinitionV1:
        return _from_json_bytes(cls, raw, "cohort definition")


_ENVELOPE_FIELDS = frozenset(
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
)


def _restore_cohort_lineage(raw: object) -> tuple[Cohort, ...]:
    items = _json_array(raw, "cohort revision lineage")
    if not items:
        raise ValueError("cohort revision lineage cannot be empty")
    lineage: list[Cohort] = []
    predecessor: Cohort | None = None
    for index, raw_item in enumerate(items, start=1):
        value = _exact_object(
            raw_item,
            _ENVELOPE_FIELDS,
            f"cohort lineage envelope {index}",
        )
        envelope = create_cohort_revision(
            _sha256(
                value["content_sha256"],
                f"cohort lineage envelope {index} content digest",
            ),
            predecessor=predecessor,
        )
        if envelope.as_dict() != value:
            raise ValueError(f"cohort lineage envelope {index} is not canonical")
        lineage.append(envelope)
        predecessor = envelope
    return tuple(lineage)


@dataclass(frozen=True, slots=True)
class CohortRevisionV1:
    """Standalone-reloadable definition paired with its full cohort lineage."""

    revision_chain: tuple[Cohort, ...]
    definition: CohortDefinitionV1
    schema_id: str = COHORT_REVISION_SCHEMA_ID
    schema_version: int = COHORT_REVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.revision_chain) is not tuple or not self.revision_chain:
            raise ValueError("cohort revision chain cannot be empty")
        if any(type(item) is not Cohort for item in self.revision_chain):
            raise TypeError("cohort revision chain must contain exact Cohort envelopes")
        for index, item in enumerate(self.revision_chain, start=1):
            item.canonical_bytes()
            if item.revision != index:
                raise ValueError("cohort revision chain must be contiguous from one")
            if index > 1:
                self.revision_chain[index - 2].validate_successor(item)
        if type(self.definition) is not CohortDefinitionV1:
            raise TypeError("cohort revision definition must be CohortDefinitionV1")
        if self.cohort.content_sha256 != self.definition.sha256:
            raise ValueError("cohort envelope does not commit to its exact definition")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COHORT_REVISION_SCHEMA_ID,
            expected_version=COHORT_REVISION_SCHEMA_VERSION,
            label="cohort revision",
        )

    @property
    def cohort(self) -> Cohort:
        return self.revision_chain[-1]

    @property
    def cohort_id(self) -> str:
        return self.cohort.cohort_id

    @property
    def lineage_id(self) -> str:
        return self.cohort.lineage_id

    @property
    def revision(self) -> int:
        return self.cohort.revision

    @property
    def content_sha256(self) -> str:
        return self.definition.sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "definition": self.definition.as_dict(),
            "revision_chain": [item.as_dict() for item in self.revision_chain],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> CohortRevisionV1:
        value = _exact_object(
            raw,
            frozenset({"definition", "revision_chain", "schema_id", "schema_version"}),
            "cohort revision",
        )
        revision = cls(
            revision_chain=_restore_cohort_lineage(value["revision_chain"]),
            definition=CohortDefinitionV1.from_dict(value["definition"]),
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if revision.as_dict() != value:
            raise ValueError("cohort revision did not round-trip exactly")
        return revision

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CohortRevisionV1:
        return _from_json_bytes(cls, raw, "cohort revision")


def create_cohort(definition: CohortDefinitionV1) -> CohortRevisionV1:
    """Create the initial immutable revision for an exact cohort definition."""

    if type(definition) is not CohortDefinitionV1:
        raise TypeError("cohort definition must be CohortDefinitionV1")
    return CohortRevisionV1(
        revision_chain=(create_cohort_revision(definition.sha256),),
        definition=definition,
    )


def revise_cohort(
    predecessor: CohortRevisionV1,
    definition: CohortDefinitionV1,
) -> CohortRevisionV1:
    """Create a content-changing successor without altering its predecessor."""

    if type(predecessor) is not CohortRevisionV1:
        raise TypeError("cohort predecessor must be CohortRevisionV1")
    if type(definition) is not CohortDefinitionV1:
        raise TypeError("cohort definition must be CohortDefinitionV1")
    successor = create_cohort_revision(
        definition.sha256,
        predecessor=predecessor.cohort,
    )
    return CohortRevisionV1(
        revision_chain=(*predecessor.revision_chain, successor),
        definition=definition,
    )


def _capability(value: object, label: str) -> AnalysisCapabilityV1:
    if type(value) is AnalysisCapabilityV1:
        return value
    value = getattr(value, "design", value)
    raw_value = getattr(value, "capability", value)
    raw_value = getattr(raw_value, "value", raw_value)
    if type(raw_value) is str:
        try:
            return AnalysisCapabilityV1(raw_value)
        except ValueError:
            pass
    raise TypeError(f"{label} must declare DESCRIPTIVE or CAUSAL capability")


def _canonical_source_attempts(
    value: object,
) -> tuple[CohortSourceAttemptV1, ...]:
    items = _canonical_typed_tuple(
        value,
        CohortSourceAttemptV1,
        "cohort summary source attempts",
        allow_empty=False,
        key=lambda item: item.attempt_id,
    )
    attempt_ids = tuple(item.attempt_id for item in items)
    if len(attempt_ids) != len(set(attempt_ids)):
        raise ValueError("cohort source attempt IDs must be unique")
    return items


def _canonical_observations(
    value: object,
) -> tuple[MetricObservationV1, ...]:
    items = _canonical_typed_tuple(
        value,
        MetricObservationV1,
        "cohort metric observations",
        allow_empty=False,
        key=lambda item: item.observation_id,
    )
    observation_ids = tuple(item.observation_id for item in items)
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("cohort metric observation IDs must be unique")
    return items


def _missing_reason_counts(
    observations: tuple[MetricObservationV1, ...],
) -> tuple[MissingReasonCountV1, ...]:
    counts = Counter(
        item.missing_reason for item in observations if not item.present
    )
    if None in counts:
        raise ValueError("missing metric observation lacks a missing reason")
    return tuple(
        MissingReasonCountV1(reason=reason, count=counts[reason])
        for reason in sorted(counts)
    )


def _signature_key(signature: VersionSignatureV1) -> bytes:
    return signature.canonical_bytes()


@dataclass(frozen=True, slots=True)
class CohortSummaryV1:
    """Version-explicit cohort result over immutable source-attempt bindings."""

    cohort_id: str
    cohort_sha256: str
    study_id: str
    study_sha256: str
    protocol_lock_sha256: str
    member_count: int
    eligible_denominator: int
    design_capability: AnalysisCapabilityV1
    observations: tuple[MetricObservationV1, ...]
    source_attempts: tuple[CohortSourceAttemptV1, ...]
    statistics: DescriptiveSummaryV1
    schema_id: str = COHORT_SUMMARY_SCHEMA_ID
    schema_version: int = COHORT_SUMMARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.cohort_id, _COHORT_ID, "cohort summary cohort ID")
        _sha256(self.cohort_sha256, "cohort summary cohort digest")
        _identifier(self.study_id, _STUDY_ID, "cohort summary study ID")
        _sha256(self.study_sha256, "cohort summary study digest")
        _sha256(self.protocol_lock_sha256, "cohort summary protocol lock digest")
        _positive_int(self.member_count, "cohort summary member count")
        _positive_int(
            self.eligible_denominator,
            "cohort summary eligible denominator",
        )
        if type(self.design_capability) is not AnalysisCapabilityV1:
            raise TypeError(
                "cohort summary design capability must be AnalysisCapabilityV1"
            )
        observations = _canonical_observations(self.observations)
        source_attempts = _canonical_source_attempts(self.source_attempts)
        if len(observations) != self.eligible_denominator:
            raise ValueError(
                "cohort summary observations differ from eligible denominator"
            )
        if len(source_attempts) != self.eligible_denominator:
            raise ValueError(
                "cohort summary source attempts differ from eligible denominator"
            )
        if tuple(item.observation_id for item in observations) != tuple(
            item.attempt_id for item in source_attempts
        ):
            raise ValueError(
                "each cohort observation ID must equal its exact source attempt ID"
            )
        if type(self.statistics) is not DescriptiveSummaryV1:
            raise TypeError("cohort statistics must be DescriptiveSummaryV1")
        if any(item.metric_id != self.statistics.metric_id for item in observations):
            raise ValueError("cohort observations changed summary metric ID")
        observed_signatures = tuple(
            sorted(
                {item.version_signature for item in observations},
                key=_signature_key,
            )
        )
        if observed_signatures != self.statistics.compatibility_decision.signatures:
            raise ValueError(
                "cohort compatibility decision differs from observed versions"
            )
        if self.included_count + self.missing_count != self.eligible_denominator:
            raise ValueError("cohort included and missing counts do not cover eligibility")
        if sum(item.count for item in self.missing_reasons) != self.missing_count:
            raise ValueError("cohort missing-reason counts differ from missing count")
        if sum(item.included_count for item in self.estimates) != self.included_count:
            raise ValueError("cohort estimates differ from included observation count")
        if sum(item.missing_count for item in self.estimates) != self.missing_count:
            raise ValueError("cohort estimates differ from missing observation count")
        if len(observed_signatures) > 1:
            if self.compatibility_action is not CompatibilityActionV1.STRATIFY:
                raise ValueError(
                    "mixed cohort versions require explicit stratification; pooling "
                    "and persisted refusal summaries are forbidden"
                )
            estimate_signatures = tuple(
                sorted(
                    (item.version_signature for item in self.estimates),
                    key=_signature_key,
                )
            )
            if estimate_signatures != observed_signatures:
                raise ValueError("stratified estimates do not cover every version")
        if self.requested_capability is AnalysisCapabilityV1.CAUSAL:
            require_claim_capability(
                requested_capability=self.requested_capability,
                design_capability=self.design_capability,
                analysis_capability=self.analysis_capability,
            )
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=COHORT_SUMMARY_SCHEMA_ID,
            expected_version=COHORT_SUMMARY_SCHEMA_VERSION,
            label="cohort summary",
        )

    @property
    def metric_id(self) -> str:
        return self.statistics.metric_id

    @property
    def requested_capability(self) -> AnalysisCapabilityV1:
        return self.statistics.requested_capability

    @property
    def analysis_capability(self) -> AnalysisCapabilityV1:
        return self.statistics.analysis_capability

    @property
    def view_language(self) -> str:
        return self.requested_capability.value

    @property
    def compatibility_decision(self) -> CompatibilityDecisionV1:
        return self.statistics.compatibility_decision

    @property
    def compatibility_action(self) -> CompatibilityActionV1:
        return self.compatibility_decision.action

    @property
    def estimates(self) -> tuple[DescriptiveEstimateV1, ...]:
        return self.statistics.estimates

    @property
    def version_signatures(self) -> tuple[VersionSignatureV1, ...]:
        return self.compatibility_decision.signatures

    @property
    def score_versions(self) -> tuple[int, ...]:
        return tuple(sorted({item.score_version for item in self.version_signatures}))

    @property
    def model_versions(self) -> tuple[int, ...]:
        return tuple(sorted({item.model_version for item in self.version_signatures}))

    @property
    def analysis_versions(self) -> tuple[int, ...]:
        return tuple(
            sorted({item.analysis_version for item in self.version_signatures})
        )

    @property
    def included_count(self) -> int:
        return sum(1 for item in self.observations if item.present)

    @property
    def missing_count(self) -> int:
        return self.eligible_denominator - self.included_count

    @property
    def missing_reasons(self) -> tuple[MissingReasonCountV1, ...]:
        return _missing_reason_counts(self.observations)

    @property
    def uncertainty(self) -> tuple[UncertaintyIntervalV1 | None, ...]:
        return tuple(item.uncertainty for item in self.estimates)

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_versions": list(self.analysis_versions),
            "cohort_id": self.cohort_id,
            "cohort_sha256": self.cohort_sha256,
            "compatibility_decision": self.compatibility_decision.as_dict(),
            "design_capability": self.design_capability.value,
            "eligible_denominator": self.eligible_denominator,
            "included_count": self.included_count,
            "member_count": self.member_count,
            "metric_id": self.metric_id,
            "missing_count": self.missing_count,
            "missing_reasons": [item.as_dict() for item in self.missing_reasons],
            "model_versions": list(self.model_versions),
            "observations": [item.as_dict() for item in self.observations],
            "protocol_lock_sha256": self.protocol_lock_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "score_versions": list(self.score_versions),
            "source_attempts": [item.as_dict() for item in self.source_attempts],
            "statistics": self.statistics.as_dict(),
            "study_id": self.study_id,
            "study_sha256": self.study_sha256,
            "uncertainty": [
                None if item is None else item.as_dict() for item in self.uncertainty
            ],
            "version_signatures": [
                item.as_dict() for item in self.version_signatures
            ],
            "view_language": self.view_language,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> CohortSummaryV1:
        value = _exact_object(
            raw,
            frozenset(
                {
                    "analysis_versions",
                    "cohort_id",
                    "cohort_sha256",
                    "compatibility_decision",
                    "design_capability",
                    "eligible_denominator",
                    "included_count",
                    "member_count",
                    "metric_id",
                    "missing_count",
                    "missing_reasons",
                    "model_versions",
                    "observations",
                    "protocol_lock_sha256",
                    "schema_id",
                    "schema_version",
                    "score_versions",
                    "source_attempts",
                    "statistics",
                    "study_id",
                    "study_sha256",
                    "uncertainty",
                    "version_signatures",
                    "view_language",
                }
            ),
            "cohort summary",
        )
        summary = cls(
            cohort_id=value["cohort_id"],  # type: ignore[arg-type]
            cohort_sha256=value["cohort_sha256"],  # type: ignore[arg-type]
            study_id=value["study_id"],  # type: ignore[arg-type]
            study_sha256=value["study_sha256"],  # type: ignore[arg-type]
            protocol_lock_sha256=value["protocol_lock_sha256"],  # type: ignore[arg-type]
            member_count=_positive_int(value["member_count"], "cohort member count"),
            eligible_denominator=_positive_int(
                value["eligible_denominator"],
                "cohort eligible denominator",
            ),
            design_capability=AnalysisCapabilityV1(
                _text(value["design_capability"], "cohort design capability")
            ),
            observations=tuple(
                MetricObservationV1.from_dict(item)
                for item in _json_array(
                    value["observations"],
                    "cohort metric observations",
                )
            ),
            source_attempts=tuple(
                CohortSourceAttemptV1.from_dict(item)
                for item in _json_array(
                    value["source_attempts"],
                    "cohort source attempts",
                )
            ),
            statistics=DescriptiveSummaryV1.from_dict(value["statistics"]),
            schema_id=value["schema_id"],  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
        if (
            _nonnegative_int(value["included_count"], "cohort included count")
            != summary.included_count
        ):
            raise ValueError("serialized cohort included count differs")
        if (
            _nonnegative_int(value["missing_count"], "cohort missing count")
            != summary.missing_count
        ):
            raise ValueError("serialized cohort missing count differs")
        version_fields = (
            ("score_versions", summary.score_versions),
            ("model_versions", summary.model_versions),
            ("analysis_versions", summary.analysis_versions),
        )
        for label, expected_versions in version_fields:
            raw_versions = _json_array(value[label], f"cohort {label}")
            if any(type(item) is not int or item <= 0 for item in raw_versions):
                raise ValueError(f"cohort {label} must contain positive integers")
            if tuple(raw_versions) != expected_versions:
                raise ValueError(f"serialized cohort {label} differs")
        missing_reasons = tuple(
            MissingReasonCountV1.from_dict(item)
            for item in _json_array(value["missing_reasons"], "cohort missing reasons")
        )
        if missing_reasons != summary.missing_reasons:
            raise ValueError("serialized cohort missing reasons differ")
        uncertainty = tuple(
            None if item is None else UncertaintyIntervalV1.from_dict(item)
            for item in _json_array(value["uncertainty"], "cohort uncertainty")
        )
        if uncertainty != summary.uncertainty:
            raise ValueError("serialized cohort uncertainty differs")
        version_signatures = tuple(
            VersionSignatureV1.from_dict(item)
            for item in _json_array(
                value["version_signatures"],
                "cohort version signatures",
            )
        )
        if version_signatures != summary.version_signatures:
            raise ValueError("serialized cohort version signatures differ")
        decision = CompatibilityDecisionV1.from_dict(
            value["compatibility_decision"]
        )
        if decision != summary.compatibility_decision:
            raise ValueError("serialized cohort compatibility decision differs")
        if (
            _identifier(value["metric_id"], _SEMANTIC_ID, "cohort metric ID")
            != summary.metric_id
        ):
            raise ValueError("serialized cohort metric ID differs")
        view_language = AnalysisCapabilityV1(
            _text(value["view_language"], "cohort view language")
        )
        if view_language is not summary.requested_capability:
            raise ValueError("serialized cohort view language differs")
        if summary.as_dict() != value:
            raise ValueError("cohort summary did not round-trip exactly")
        return summary

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> CohortSummaryV1:
        return _from_json_bytes(cls, raw, "cohort summary")


def build_cohort_summary(
    cohort: CohortRevisionV1,
    metric_id: str,
    observations: tuple[MetricObservationV1, ...],
    source_attempts: tuple[CohortSourceAttemptV1, ...],
    *,
    compatibility_action: CompatibilityActionV1 = CompatibilityActionV1.REFUSE,
    requested_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    analysis_capability: AnalysisCapabilityV1 = AnalysisCapabilityV1.DESCRIPTIVE,
    design_capability: object = AnalysisCapabilityV1.DESCRIPTIVE,
) -> CohortSummaryV1:
    """Summarize one declared cohort metric without mutating source attempts.

    ``MetricObservationV1.observation_id`` must equal the corresponding immutable
    assignment-attempt ID.  Missing observations remain explicit observations with
    ``present=False`` and a reason, so the eligible denominator cannot shrink during
    analysis.  A mixed-version ``POOL`` or ``REFUSE`` request raises the statistics
    layer's explicit compatibility refusal; ``STRATIFY`` returns one estimate per
    exact :class:`VersionSignatureV1`.
    """

    if type(cohort) is not CohortRevisionV1:
        raise TypeError("cohort summary requires CohortRevisionV1")
    metric = _identifier(metric_id, _SEMANTIC_ID, "cohort summary metric ID")
    if metric not in cohort.definition.metric_ids:
        raise ValueError("cohort summary metric is outside the locked definition")
    if type(compatibility_action) is not CompatibilityActionV1:
        raise TypeError("compatibility action must be CompatibilityActionV1")
    if type(requested_capability) is not AnalysisCapabilityV1:
        raise TypeError("requested capability must be AnalysisCapabilityV1")
    if type(analysis_capability) is not AnalysisCapabilityV1:
        raise TypeError("analysis capability must be AnalysisCapabilityV1")
    normalized_design_capability = _capability(
        design_capability,
        "study design capability",
    )
    if requested_capability is AnalysisCapabilityV1.CAUSAL:
        if (
            getattr(design_capability, "study_id", None)
            != cohort.definition.study_id
            or getattr(design_capability, "sha256", None)
            != cohort.definition.study_sha256
        ):
            raise UnsupportedCausalClaimError(
                "causal cohort summaries require the exact study revision bound "
                "by the cohort definition"
            )
        design_validator = getattr(design_capability, "require_causal_support", None)
        if not callable(design_validator):
            raise UnsupportedCausalClaimError(
                "causal cohort summaries require an exact study design or manifest "
                "with causal-support evidence"
            )
        design_validator()
        require_claim_capability(
            requested_capability=requested_capability,
            design_capability=design_capability,
            analysis_capability=analysis_capability,
        )

    raw_observations = _canonical_observations(observations)
    raw_sources = _canonical_source_attempts(source_attempts)
    if tuple(item.observation_id for item in raw_observations) != tuple(
        item.attempt_id for item in raw_sources
    ):
        raise ValueError(
            "cohort observations must map one-to-one to source attempt IDs"
        )
    if any(item.metric_id != metric for item in raw_observations):
        raise ValueError("cohort observations must share the declared metric ID")

    allowed_assignments = {
        (item.assignment_id, item.assignment_sha256)
        for item in cohort.definition.assignment_bindings
    }
    if any(item.assignment_binding not in allowed_assignments for item in raw_sources):
        raise ValueError(
            "cohort source attempt uses an assignment outside the exact definition"
        )
    if cohort.definition.member_profile_ids:
        allowed_members = set(cohort.definition.member_profile_ids)
        if any(item.learner_profile_id not in allowed_members for item in raw_sources):
            raise ValueError(
                "cohort source attempt uses a learner outside explicit membership"
            )

    statistical_summary = summarize_observations(
        raw_observations,
        compatibility_action=compatibility_action,
        requested_capability=requested_capability,
        analysis_capability=analysis_capability,
        design_capability=design_capability,
    )
    if type(statistical_summary) is not DescriptiveSummaryV1:
        raise TypeError("statistics builder returned an unsupported summary type")
    member_count = (
        len(cohort.definition.member_profile_ids)
        if cohort.definition.member_profile_ids
        else len({item.learner_profile_id for item in raw_sources})
    )
    return CohortSummaryV1(
        cohort_id=cohort.cohort_id,
        cohort_sha256=cohort.sha256,
        study_id=cohort.definition.study_id,
        study_sha256=cohort.definition.study_sha256,
        protocol_lock_sha256=cohort.definition.protocol_lock_sha256,
        member_count=member_count,
        eligible_denominator=len(raw_observations),
        design_capability=normalized_design_capability,
        observations=raw_observations,
        source_attempts=raw_sources,
        statistics=statistical_summary,
    )


summarize_cohort = build_cohort_summary


__all__ = [
    "COHORT_ASSIGNMENT_BINDING_SCHEMA_ID",
    "COHORT_ASSIGNMENT_BINDING_SCHEMA_VERSION",
    "COHORT_DEFINITION_SCHEMA_ID",
    "COHORT_DEFINITION_SCHEMA_VERSION",
    "COHORT_MEMBERSHIP_POLICY_SCHEMA_ID",
    "COHORT_MEMBERSHIP_POLICY_SCHEMA_VERSION",
    "COHORT_REVISION_SCHEMA_ID",
    "COHORT_REVISION_SCHEMA_VERSION",
    "COHORT_SOURCE_ATTEMPT_SCHEMA_ID",
    "COHORT_SOURCE_ATTEMPT_SCHEMA_VERSION",
    "COHORT_SUMMARY_SCHEMA_ID",
    "COHORT_SUMMARY_SCHEMA_VERSION",
    "CohortAssignmentBindingV1",
    "CohortDefinitionV1",
    "CohortMembershipModeV1",
    "CohortMembershipPolicyV1",
    "CohortRevisionV1",
    "CohortSourceAttemptV1",
    "CohortSummaryV1",
    "build_cohort_summary",
    "create_cohort",
    "revise_cohort",
    "summarize_cohort",
]
