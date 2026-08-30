"""Immutable, preregistered local-study protocols and execution ledgers.

The objects in this module are pure contracts.  They do not read a clock, select a
participant, randomize an allocation, inspect an attempt, or authorize consent.
Callers provide those facts explicitly.  A study protocol may be revised only before
it is locked; observations are admitted only through a ledger bound to the exact
locked revision.  Later amendments and deviations are append-only sidecars and never
rewrite the source manifest.

The consent, retention, and export fields encode Kirby2's local engineering policy.
They are not legal advice or a claim of regulatory compliance.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import ClassVar

from .assignments import SeedPolicyV1
from .consent import (
    ConsentScopeV1,
    EvidenceExportPermissionV1,
    EvidenceRetentionPolicyV1,
)
from .models import ResearchStudy, create_research_study_revision


STUDY_MANIFEST_SCHEMA_ID = "KIRBY2_STUDY_MANIFEST_V1"
STUDY_MANIFEST_SCHEMA_VERSION = 1
STUDY_REVISION_SCHEMA_ID = "KIRBY2_STUDY_REVISION_V1"
STUDY_REVISION_SCHEMA_VERSION = 1
STUDY_PROTOCOL_LOCK_SCHEMA_ID = "KIRBY2_STUDY_PROTOCOL_LOCK_V1"
STUDY_PROTOCOL_LOCK_SCHEMA_VERSION = 1
STUDY_ATTEMPT_BINDING_SCHEMA_ID = "KIRBY2_STUDY_ATTEMPT_BINDING_V1"
STUDY_ATTEMPT_BINDING_SCHEMA_VERSION = 1
STUDY_AMENDMENT_SCHEMA_ID = "KIRBY2_STUDY_AMENDMENT_V1"
STUDY_AMENDMENT_SCHEMA_VERSION = 1
PROTOCOL_DEVIATION_SCHEMA_ID = "KIRBY2_PROTOCOL_DEVIATION_V1"
PROTOCOL_DEVIATION_SCHEMA_VERSION = 1
STUDY_EXECUTION_LEDGER_SCHEMA_ID = "KIRBY2_STUDY_EXECUTION_LEDGER_V1"
STUDY_EXECUTION_LEDGER_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ASSIGNMENT_ID = re.compile(r"assignment-[0-9a-f]{64}\Z")
_ATTEMPT_ID = re.compile(r"assignment-attempt-[0-9a-f]{64}\Z")
_STUDY_ID = re.compile(r"research-study-[0-9a-f]{64}\Z")
_STUDY_LINEAGE_ID = re.compile(r"research-study-lineage-[0-9a-f]{64}\Z")
_LOCK_ID = re.compile(r"study-protocol-lock-[0-9a-f]{64}\Z")
_AMENDMENT_ID = re.compile(r"study-amendment-[0-9a-f]{64}\Z")
_DEVIATION_ID = re.compile(r"protocol-deviation-[0-9a-f]{64}\Z")


class StudyStatusV1(str, Enum):
    EXPLORATORY = "EXPLORATORY"
    CONFIRMATORY = "CONFIRMATORY"


class DesignCapabilityV1(str, Enum):
    """Strongest claim class the preregistered design may support."""

    DESCRIPTIVE = "DESCRIPTIVE"
    CAUSAL = "CAUSAL"


class StudyDesignKindV1(str, Enum):
    OBSERVATIONAL = "OBSERVATIONAL"
    RANDOMIZED_CONTROLLED = "RANDOMIZED_CONTROLLED"
    QUASI_EXPERIMENTAL = "QUASI_EXPERIMENTAL"


class AllocationMethodV1(str, Enum):
    OBSERVED_NO_ALLOCATION = "OBSERVED_NO_ALLOCATION"
    RANDOMIZED = "RANDOMIZED"
    DETERMINISTIC = "DETERMINISTIC"


class ProtocolDeviationImpactV1(str, Enum):
    NO_KNOWN_ANALYSIS_IMPACT = "NO_KNOWN_ANALYSIS_IMPACT"
    SENSITIVITY_ANALYSIS_REQUIRED = "SENSITIVITY_ANALYSIS_REQUIRED"
    EXCLUDE_BY_PREREGISTERED_RULE = "EXCLUDE_BY_PREREGISTERED_RULE"
    INVALIDATES_CONFIRMATORY_CLAIM = "INVALIDATES_CONFIRMATORY_CLAIM"


class StudyLedgerEntryKindV1(str, Enum):
    INCLUDED_ATTEMPT = "INCLUDED_ATTEMPT"
    AMENDMENT = "AMENDMENT"
    PROTOCOL_DEVIATION = "PROTOCOL_DEVIATION"


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


def _text(value: object, label: str, *, maximum_utf8_bytes: int = 8192) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty text without edge whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use NFC Unicode normalization")
    if len(value.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{label} is too long")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _utc(value: object, label: str) -> str:
    text = _text(value, label, maximum_utf8_bytes=20)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text) is None:
        raise ValueError(f"{label} must use canonical UTC seconds ending in Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a valid UTC timestamp") from error
    return text


def _text_tuple(value: object, label: str, *, allow_empty: bool) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    result = tuple(_text(item, f"{label} item") for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{label} cannot be empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{label} cannot contain duplicates")
    return result


def _enum_text(value: object, label: str) -> str:
    return _text(value, label, maximum_utf8_bytes=96)


def _id(value: object, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


class _CanonicalRecordV1:
    """Shared strict canonical transport for immutable public records."""

    __slots__ = ()

    def as_dict(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_json_bytes(cls, raw: bytes):
        value = cls.from_dict(_canonical_object(raw, cls.__name__))
        if value.canonical_bytes() != raw:
            raise ValueError(f"{cls.__name__} changed during restoration")
        return value


@dataclass(frozen=True, slots=True)
class StudyAssignmentBindingV1(_CanonicalRecordV1):
    assignment_id: str
    assignment_sha256: str

    def __post_init__(self) -> None:
        _id(self.assignment_id, _ASSIGNMENT_ID, "study assignment ID")
        _sha256(self.assignment_sha256, "study assignment digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "assignment_sha256": self.assignment_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyAssignmentBindingV1:
        payload = _fields(
            value,
            {"assignment_id", "assignment_sha256"},
            "study assignment binding",
        )
        return cls(
            assignment_id=_id(
                payload["assignment_id"], _ASSIGNMENT_ID, "study assignment ID"
            ),
            assignment_sha256=_sha256(
                payload["assignment_sha256"], "study assignment digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class StudyDesignV1(_CanonicalRecordV1):
    capability: DesignCapabilityV1
    design_kind: StudyDesignKindV1
    intervention: str | None
    comparator: str | None
    causal_estimand: str | None
    randomization_evidence_sha256: str | None
    identifying_assumptions: tuple[str, ...]
    confounding_adjustment_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.capability) is not DesignCapabilityV1:
            raise TypeError("study capability must be DesignCapabilityV1")
        if type(self.design_kind) is not StudyDesignKindV1:
            raise TypeError("study design kind must be StudyDesignKindV1")
        _optional_text(self.intervention, "study intervention")
        _optional_text(self.comparator, "study comparator")
        _optional_text(self.causal_estimand, "causal estimand")
        if self.randomization_evidence_sha256 is not None:
            _sha256(self.randomization_evidence_sha256, "randomization evidence digest")
        if type(self.identifying_assumptions) is not tuple:
            raise TypeError("identifying assumptions must be a tuple")
        assumptions = tuple(
            _text(item, "identifying assumption") for item in self.identifying_assumptions
        )
        if assumptions != self.identifying_assumptions:
            raise ValueError("identifying assumptions differ after validation")
        if len(assumptions) != len(set(assumptions)):
            raise ValueError("identifying assumptions cannot contain duplicates")
        if self.confounding_adjustment_sha256 is not None:
            _sha256(
                self.confounding_adjustment_sha256,
                "confounding-adjustment digest",
            )
        if self.capability is DesignCapabilityV1.CAUSAL:
            self.require_causal_support()

    @property
    def supports_causal_claim(self) -> bool:
        if self.capability is not DesignCapabilityV1.CAUSAL:
            return False
        common = (
            self.intervention is not None
            and self.comparator is not None
            and self.causal_estimand is not None
        )
        if self.design_kind is StudyDesignKindV1.RANDOMIZED_CONTROLLED:
            return common and self.randomization_evidence_sha256 is not None
        if self.design_kind is StudyDesignKindV1.QUASI_EXPERIMENTAL:
            return (
                common
                and bool(self.identifying_assumptions)
                and self.confounding_adjustment_sha256 is not None
            )
        return False

    def require_causal_support(self) -> None:
        if not self.supports_causal_claim:
            raise ValueError(
                "causal capability requires an intervention, comparator, estimand, "
                "and randomized or quasi-experimental identification evidence"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "causal_estimand": self.causal_estimand,
            "comparator": self.comparator,
            "confounding_adjustment_sha256": self.confounding_adjustment_sha256,
            "design_kind": self.design_kind.value,
            "identifying_assumptions": list(self.identifying_assumptions),
            "intervention": self.intervention,
            "randomization_evidence_sha256": self.randomization_evidence_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyDesignV1:
        payload = _fields(
            value,
            {
                "capability",
                "causal_estimand",
                "comparator",
                "confounding_adjustment_sha256",
                "design_kind",
                "identifying_assumptions",
                "intervention",
                "randomization_evidence_sha256",
            },
            "study design",
        )
        assumptions = payload["identifying_assumptions"]
        return cls(
            capability=DesignCapabilityV1(
                _enum_text(payload["capability"], "study capability")
            ),
            design_kind=StudyDesignKindV1(
                _enum_text(payload["design_kind"], "study design kind")
            ),
            intervention=_optional_text(payload["intervention"], "study intervention"),
            comparator=_optional_text(payload["comparator"], "study comparator"),
            causal_estimand=_optional_text(payload["causal_estimand"], "causal estimand"),
            randomization_evidence_sha256=(
                None
                if payload["randomization_evidence_sha256"] is None
                else _sha256(
                    payload["randomization_evidence_sha256"],
                    "randomization evidence digest",
                )
            ),
            identifying_assumptions=_text_tuple(
                assumptions,
                "identifying assumptions",
                allow_empty=True,
            ),
            confounding_adjustment_sha256=(
                None
                if payload["confounding_adjustment_sha256"] is None
                else _sha256(
                    payload["confounding_adjustment_sha256"],
                    "confounding-adjustment digest",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class AllocationRandomizationV1(_CanonicalRecordV1):
    method: AllocationMethodV1
    allocation_unit: str
    arm_ids: tuple[str, ...]
    allocation_ratio: tuple[int, ...]
    allocation_policy_sha256: str
    randomization_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.method) is not AllocationMethodV1:
            raise TypeError("allocation method must be AllocationMethodV1")
        _text(self.allocation_unit, "allocation unit")
        if type(self.arm_ids) is not tuple or not self.arm_ids:
            raise ValueError("allocation plan requires at least one arm")
        arms = tuple(_text(item, "allocation arm ID") for item in self.arm_ids)
        if arms != self.arm_ids or len(arms) != len(set(arms)):
            raise ValueError("allocation arms must be unique exact text")
        if type(self.allocation_ratio) is not tuple:
            raise TypeError("allocation ratio must be a tuple")
        if self.method is AllocationMethodV1.OBSERVED_NO_ALLOCATION:
            if self.allocation_ratio:
                raise ValueError("an observed design cannot declare an allocation ratio")
            if self.randomization_sha256 is not None:
                raise ValueError("an observed design cannot claim randomization evidence")
        else:
            if len(self.arm_ids) < 2:
                raise ValueError("an allocated design requires at least two arms")
            if len(self.allocation_ratio) != len(self.arm_ids):
                raise ValueError("allocation ratio must have one weight per arm")
            for weight in self.allocation_ratio:
                _positive_int(weight, "allocation ratio weight")
        _sha256(self.allocation_policy_sha256, "allocation policy digest")
        if self.method is AllocationMethodV1.RANDOMIZED:
            _sha256(self.randomization_sha256, "randomization digest")
        elif self.randomization_sha256 is not None:
            raise ValueError("only randomized allocation may bind randomization evidence")

    def as_dict(self) -> dict[str, object]:
        return {
            "allocation_policy_sha256": self.allocation_policy_sha256,
            "allocation_ratio": list(self.allocation_ratio),
            "allocation_unit": self.allocation_unit,
            "arm_ids": list(self.arm_ids),
            "method": self.method.value,
            "randomization_sha256": self.randomization_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> AllocationRandomizationV1:
        payload = _fields(
            value,
            {
                "allocation_policy_sha256",
                "allocation_ratio",
                "allocation_unit",
                "arm_ids",
                "method",
                "randomization_sha256",
            },
            "allocation and randomization plan",
        )
        raw_ratio = payload["allocation_ratio"]
        if type(raw_ratio) is not list:
            raise TypeError("allocation ratio must be an array")
        return cls(
            method=AllocationMethodV1(
                _enum_text(payload["method"], "allocation method")
            ),
            allocation_unit=_text(payload["allocation_unit"], "allocation unit"),
            arm_ids=_text_tuple(payload["arm_ids"], "allocation arms", allow_empty=False),
            allocation_ratio=tuple(
                _positive_int(item, "allocation ratio weight") for item in raw_ratio
            ),
            allocation_policy_sha256=_sha256(
                payload["allocation_policy_sha256"], "allocation policy digest"
            ),
            randomization_sha256=(
                None
                if payload["randomization_sha256"] is None
                else _sha256(payload["randomization_sha256"], "randomization digest")
            ),
        )


@dataclass(frozen=True, slots=True)
class BlindingRevealV1(_CanonicalRecordV1):
    participants_blinded: bool
    instructors_blinded: bool
    outcome_assessors_blinded: bool
    analysts_blinded: bool
    reveal_policy_id: str
    reveal_policy_sha256: str
    reveal_timing: str

    def __post_init__(self) -> None:
        _exact_bool(self.participants_blinded, "participant blinding")
        _exact_bool(self.instructors_blinded, "instructor blinding")
        _exact_bool(self.outcome_assessors_blinded, "outcome-assessor blinding")
        _exact_bool(self.analysts_blinded, "analyst blinding")
        _text(self.reveal_policy_id, "reveal policy ID")
        _sha256(self.reveal_policy_sha256, "reveal policy digest")
        _text(self.reveal_timing, "reveal timing")

    def as_dict(self) -> dict[str, object]:
        return {
            "analysts_blinded": self.analysts_blinded,
            "instructors_blinded": self.instructors_blinded,
            "outcome_assessors_blinded": self.outcome_assessors_blinded,
            "participants_blinded": self.participants_blinded,
            "reveal_policy_id": self.reveal_policy_id,
            "reveal_policy_sha256": self.reveal_policy_sha256,
            "reveal_timing": self.reveal_timing,
        }

    @classmethod
    def from_dict(cls, value: object) -> BlindingRevealV1:
        payload = _fields(
            value,
            {
                "analysts_blinded",
                "instructors_blinded",
                "outcome_assessors_blinded",
                "participants_blinded",
                "reveal_policy_id",
                "reveal_policy_sha256",
                "reveal_timing",
            },
            "blinding and reveal plan",
        )
        return cls(
            participants_blinded=_exact_bool(
                payload["participants_blinded"], "participant blinding"
            ),
            instructors_blinded=_exact_bool(
                payload["instructors_blinded"], "instructor blinding"
            ),
            outcome_assessors_blinded=_exact_bool(
                payload["outcome_assessors_blinded"], "outcome-assessor blinding"
            ),
            analysts_blinded=_exact_bool(
                payload["analysts_blinded"], "analyst blinding"
            ),
            reveal_policy_id=_text(payload["reveal_policy_id"], "reveal policy ID"),
            reveal_policy_sha256=_sha256(
                payload["reveal_policy_sha256"], "reveal policy digest"
            ),
            reveal_timing=_text(payload["reveal_timing"], "reveal timing"),
        )


@dataclass(frozen=True, slots=True)
class ContentLockV1(_CanonicalRecordV1):
    lock_name: str
    content_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        _text(self.lock_name, "content lock name")
        _text(self.content_id, "content lock ID")
        _sha256(self.content_sha256, "content lock digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "content_id": self.content_id,
            "content_sha256": self.content_sha256,
            "lock_name": self.lock_name,
        }

    @classmethod
    def from_dict(cls, value: object) -> ContentLockV1:
        payload = _fields(
            value,
            {"content_id", "content_sha256", "lock_name"},
            "content lock",
        )
        return cls(
            lock_name=_text(payload["lock_name"], "content lock name"),
            content_id=_text(payload["content_id"], "content lock ID"),
            content_sha256=_sha256(payload["content_sha256"], "content lock digest"),
        )


@dataclass(frozen=True, slots=True)
class ParameterLockV1(_CanonicalRecordV1):
    parameter_path: str
    value_sha256: str

    def __post_init__(self) -> None:
        _text(self.parameter_path, "parameter lock path")
        _sha256(self.value_sha256, "parameter value digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "parameter_path": self.parameter_path,
            "value_sha256": self.value_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> ParameterLockV1:
        payload = _fields(
            value,
            {"parameter_path", "value_sha256"},
            "parameter lock",
        )
        return cls(
            parameter_path=_text(payload["parameter_path"], "parameter lock path"),
            value_sha256=_sha256(payload["value_sha256"], "parameter value digest"),
        )


@dataclass(frozen=True, slots=True)
class MetricDeclarationV1(_CanonicalRecordV1):
    metric_id: str
    metric_version: str
    definition_sha256: str
    unit: str

    def __post_init__(self) -> None:
        _text(self.metric_id, "metric ID")
        _text(self.metric_version, "metric version")
        _sha256(self.definition_sha256, "metric definition digest")
        _text(self.unit, "metric unit")

    def as_dict(self) -> dict[str, object]:
        return {
            "definition_sha256": self.definition_sha256,
            "metric_id": self.metric_id,
            "metric_version": self.metric_version,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, value: object) -> MetricDeclarationV1:
        payload = _fields(
            value,
            {"definition_sha256", "metric_id", "metric_version", "unit"},
            "metric declaration",
        )
        return cls(
            metric_id=_text(payload["metric_id"], "metric ID"),
            metric_version=_text(payload["metric_version"], "metric version"),
            definition_sha256=_sha256(
                payload["definition_sha256"], "metric definition digest"
            ),
            unit=_text(payload["unit"], "metric unit"),
        )


@dataclass(frozen=True, slots=True)
class OutcomeDeclarationV1(_CanonicalRecordV1):
    outcome_id: str
    metric_id: str
    estimand: str
    analysis_population: str
    time_window: str

    def __post_init__(self) -> None:
        _text(self.outcome_id, "outcome ID")
        _text(self.metric_id, "outcome metric ID")
        _text(self.estimand, "outcome estimand")
        _text(self.analysis_population, "outcome analysis population")
        _text(self.time_window, "outcome time window")

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_population": self.analysis_population,
            "estimand": self.estimand,
            "metric_id": self.metric_id,
            "outcome_id": self.outcome_id,
            "time_window": self.time_window,
        }

    @classmethod
    def from_dict(cls, value: object) -> OutcomeDeclarationV1:
        payload = _fields(
            value,
            {"analysis_population", "estimand", "metric_id", "outcome_id", "time_window"},
            "outcome declaration",
        )
        return cls(
            outcome_id=_text(payload["outcome_id"], "outcome ID"),
            metric_id=_text(payload["metric_id"], "outcome metric ID"),
            estimand=_text(payload["estimand"], "outcome estimand"),
            analysis_population=_text(
                payload["analysis_population"], "outcome analysis population"
            ),
            time_window=_text(payload["time_window"], "outcome time window"),
        )


@dataclass(frozen=True, slots=True)
class AnalysisPlanV1(_CanonicalRecordV1):
    version: str
    plan_sha256: str
    code_sha256: str
    capability: DesignCapabilityV1

    def __post_init__(self) -> None:
        _text(self.version, "analysis plan version")
        _sha256(self.plan_sha256, "analysis plan digest")
        _sha256(self.code_sha256, "analysis code digest")
        if type(self.capability) is not DesignCapabilityV1:
            raise TypeError("analysis capability must be DesignCapabilityV1")

    @property
    def analysis_capability(self) -> DesignCapabilityV1:
        return self.capability

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "code_sha256": self.code_sha256,
            "plan_sha256": self.plan_sha256,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> AnalysisPlanV1:
        payload = _fields(
            value,
            {"capability", "code_sha256", "plan_sha256", "version"},
            "analysis plan",
        )
        return cls(
            version=_text(payload["version"], "analysis plan version"),
            plan_sha256=_sha256(payload["plan_sha256"], "analysis plan digest"),
            code_sha256=_sha256(payload["code_sha256"], "analysis code digest"),
            capability=DesignCapabilityV1(
                _enum_text(payload["capability"], "analysis capability")
            ),
        )


@dataclass(frozen=True, slots=True)
class StudyConsentPolicyV1(_CanonicalRecordV1):
    authorization_policy_id: str
    required_scopes: tuple[ConsentScopeV1, ...]
    require_current_grant_at_inclusion: bool

    def __post_init__(self) -> None:
        _text(self.authorization_policy_id, "study consent authorization policy ID")
        if type(self.required_scopes) is not tuple or not self.required_scopes:
            raise ValueError("study consent policy requires at least one scope")
        if any(type(item) is not ConsentScopeV1 for item in self.required_scopes):
            raise TypeError("study consent scopes must be ConsentScopeV1 values")
        if len(self.required_scopes) != len(set(self.required_scopes)):
            raise ValueError("study consent scopes cannot contain duplicates")
        if ConsentScopeV1.LOCAL_RESEARCH_STUDY not in self.required_scopes:
            raise ValueError("study consent must require LOCAL_RESEARCH_STUDY")
        if not _exact_bool(
            self.require_current_grant_at_inclusion,
            "current consent requirement",
        ):
            raise ValueError("study inclusion must require a current consent grant")

    def as_dict(self) -> dict[str, object]:
        return {
            "authorization_policy_id": self.authorization_policy_id,
            "require_current_grant_at_inclusion": self.require_current_grant_at_inclusion,
            "required_scopes": [item.value for item in self.required_scopes],
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyConsentPolicyV1:
        payload = _fields(
            value,
            {
                "authorization_policy_id",
                "require_current_grant_at_inclusion",
                "required_scopes",
            },
            "study consent policy",
        )
        raw_scopes = payload["required_scopes"]
        if type(raw_scopes) is not list:
            raise TypeError("study consent scopes must be an array")
        return cls(
            authorization_policy_id=_text(
                payload["authorization_policy_id"],
                "study consent authorization policy ID",
            ),
            required_scopes=tuple(
                ConsentScopeV1(_enum_text(item, "study consent scope"))
                for item in raw_scopes
            ),
            require_current_grant_at_inclusion=_exact_bool(
                payload["require_current_grant_at_inclusion"],
                "current consent requirement",
            ),
        )


@dataclass(frozen=True, slots=True)
class StudyRetentionPolicyV1(_CanonicalRecordV1):
    policy: EvidenceRetentionPolicyV1
    retention_until_utc: str | None
    retain_after_profile_deletion: bool

    def __post_init__(self) -> None:
        if type(self.policy) is not EvidenceRetentionPolicyV1:
            raise TypeError("study retention policy must be EvidenceRetentionPolicyV1")
        _exact_bool(self.retain_after_profile_deletion, "post-deletion retention")
        if self.policy is EvidenceRetentionPolicyV1.RETAIN_UNTIL_UTC:
            _utc(self.retention_until_utc, "study retention end")
            if not self.retain_after_profile_deletion:
                raise ValueError(
                    "bounded study retention requires post-deletion retention"
                )
        elif self.retention_until_utc is not None:
            raise ValueError("only RETAIN_UNTIL_UTC may carry a retention end")
        if self.policy is EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE:
            if self.retain_after_profile_deletion:
                raise ValueError(
                    "DELETE_WITH_PROFILE cannot retain evidence after deletion"
                )
        elif self.policy is EvidenceRetentionPolicyV1.RETAIN_WITHOUT_FIXED_END:
            if not self.retain_after_profile_deletion:
                raise ValueError(
                    "unbounded study retention requires post-deletion retention"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy.value,
            "retain_after_profile_deletion": self.retain_after_profile_deletion,
            "retention_until_utc": self.retention_until_utc,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyRetentionPolicyV1:
        payload = _fields(
            value,
            {"policy", "retain_after_profile_deletion", "retention_until_utc"},
            "study retention policy",
        )
        return cls(
            policy=EvidenceRetentionPolicyV1(
                _enum_text(payload["policy"], "study retention policy")
            ),
            retention_until_utc=(
                None
                if payload["retention_until_utc"] is None
                else _utc(payload["retention_until_utc"], "study retention end")
            ),
            retain_after_profile_deletion=_exact_bool(
                payload["retain_after_profile_deletion"], "post-deletion retention"
            ),
        )


@dataclass(frozen=True, slots=True)
class StudyDataExportPolicyV1(_CanonicalRecordV1):
    permission: EvidenceExportPermissionV1
    redaction_policy_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.permission) is not EvidenceExportPermissionV1:
            raise TypeError("study export permission must be EvidenceExportPermissionV1")
        if self.permission is EvidenceExportPermissionV1.DENIED:
            if self.redaction_policy_sha256 is not None:
                raise ValueError("a denied export cannot claim a redaction policy")
        else:
            _sha256(self.redaction_policy_sha256, "study redaction-policy digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "permission": self.permission.value,
            "redaction_policy_sha256": self.redaction_policy_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyDataExportPolicyV1:
        payload = _fields(
            value,
            {"permission", "redaction_policy_sha256"},
            "study data-export policy",
        )
        return cls(
            permission=EvidenceExportPermissionV1(
                _enum_text(payload["permission"], "study export permission")
            ),
            redaction_policy_sha256=(
                None
                if payload["redaction_policy_sha256"] is None
                else _sha256(
                    payload["redaction_policy_sha256"],
                    "study redaction-policy digest",
                )
            ),
        )


def _record_tuple(
    value: object,
    record_type: type[_CanonicalRecordV1],
    label: str,
) -> tuple[_CanonicalRecordV1, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return tuple(record_type.from_dict(item) for item in value)


@dataclass(frozen=True, slots=True)
class StudyManifestV1(_CanonicalRecordV1):
    question: str
    hypothesis: str
    assignment_set: tuple[StudyAssignmentBindingV1, ...]
    study_status: StudyStatusV1
    preregistration_sha256: str
    preregistered_at_utc: str
    population: str
    design: StudyDesignV1
    allocation_randomization: AllocationRandomizationV1
    blinding_reveal: BlindingRevealV1
    content_locks: tuple[ContentLockV1, ...]
    parameter_locks: tuple[ParameterLockV1, ...]
    declared_metrics: tuple[MetricDeclarationV1, ...]
    primary_outcomes: tuple[OutcomeDeclarationV1, ...]
    secondary_outcomes: tuple[OutcomeDeclarationV1, ...]
    planned_sample_size: int
    sample_rationale: str
    stopping_rule: str
    missing_data_policy: str
    multiplicity_policy: str
    inclusion_criteria: tuple[str, ...]
    exclusion_criteria: tuple[str, ...]
    analysis_plan: AnalysisPlanV1
    seed_policy: SeedPolicyV1
    software_version: str
    consent_policy: StudyConsentPolicyV1
    retention_policy: StudyRetentionPolicyV1
    data_export_policy: StudyDataExportPolicyV1

    schema_id: ClassVar[str] = STUDY_MANIFEST_SCHEMA_ID
    schema_version: ClassVar[int] = STUDY_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _text(self.question, "study question")
        _text(self.hypothesis, "study hypothesis")
        if type(self.assignment_set) is not tuple or not self.assignment_set:
            raise ValueError("study assignment set cannot be empty")
        if any(type(item) is not StudyAssignmentBindingV1 for item in self.assignment_set):
            raise TypeError("study assignment set must contain StudyAssignmentBindingV1")
        assignment_keys = tuple(
            (item.assignment_id, item.assignment_sha256) for item in self.assignment_set
        )
        if assignment_keys != tuple(sorted(assignment_keys)):
            raise ValueError("study assignment set must use canonical ID/digest order")
        if len(assignment_keys) != len(set(assignment_keys)):
            raise ValueError("study assignment set cannot contain duplicates")
        if len({item.assignment_id for item in self.assignment_set}) != len(
            self.assignment_set
        ):
            raise ValueError("one assignment ID cannot bind multiple digests")
        if type(self.study_status) is not StudyStatusV1:
            raise TypeError("study status must be StudyStatusV1")
        _sha256(self.preregistration_sha256, "preregistration digest")
        _utc(self.preregistered_at_utc, "preregistration time")
        _text(self.population, "study population")
        if type(self.design) is not StudyDesignV1:
            raise TypeError("study design must be StudyDesignV1")
        if type(self.allocation_randomization) is not AllocationRandomizationV1:
            raise TypeError("allocation/randomization must be AllocationRandomizationV1")
        if type(self.blinding_reveal) is not BlindingRevealV1:
            raise TypeError("blinding/reveal must be BlindingRevealV1")
        self._validate_named_locks()
        self._validate_metrics_and_outcomes()
        _positive_int(self.planned_sample_size, "planned sample size")
        _text(self.sample_rationale, "sample rationale")
        _text(self.stopping_rule, "stopping rule")
        _text(self.missing_data_policy, "missing-data policy")
        _text(self.multiplicity_policy, "multiplicity policy")
        self._validate_criteria()
        if type(self.analysis_plan) is not AnalysisPlanV1:
            raise TypeError("analysis plan must be AnalysisPlanV1")
        if type(self.seed_policy) is not SeedPolicyV1:
            raise TypeError("study seed policy must be SeedPolicyV1")
        _text(self.software_version, "software version")
        if type(self.consent_policy) is not StudyConsentPolicyV1:
            raise TypeError("consent policy must be StudyConsentPolicyV1")
        if type(self.retention_policy) is not StudyRetentionPolicyV1:
            raise TypeError("retention policy must be StudyRetentionPolicyV1")
        if type(self.data_export_policy) is not StudyDataExportPolicyV1:
            raise TypeError("data-export policy must be StudyDataExportPolicyV1")
        self._validate_design_capability()

    @property
    def status(self) -> StudyStatusV1:
        return self.study_status

    @property
    def exploratory_confirmatory_status(self) -> StudyStatusV1:
        return self.study_status

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def supports_causal_claim(self) -> bool:
        return (
            self.design.supports_causal_claim
            and self.analysis_plan.capability is DesignCapabilityV1.CAUSAL
        )

    def require_causal_support(self) -> None:
        self.design.require_causal_support()
        if self.analysis_plan.capability is not DesignCapabilityV1.CAUSAL:
            raise ValueError("causal claims require a preregistered causal analysis plan")

    def _validate_named_locks(self) -> None:
        for records, record_type, label, key in (
            (self.content_locks, ContentLockV1, "content locks", lambda item: item.lock_name),
            (
                self.parameter_locks,
                ParameterLockV1,
                "parameter locks",
                lambda item: item.parameter_path,
            ),
        ):
            if type(records) is not tuple or not records:
                raise ValueError(f"{label} cannot be empty")
            if any(type(item) is not record_type for item in records):
                raise TypeError(f"{label} contain the wrong record type")
            names = tuple(key(item) for item in records)
            if names != tuple(sorted(names)) or len(names) != len(set(names)):
                raise ValueError(f"{label} must have unique names in canonical order")

    def _validate_metrics_and_outcomes(self) -> None:
        if type(self.declared_metrics) is not tuple or not self.declared_metrics:
            raise ValueError("declared metrics cannot be empty")
        if any(type(item) is not MetricDeclarationV1 for item in self.declared_metrics):
            raise TypeError("declared metrics contain the wrong record type")
        metric_ids = tuple(item.metric_id for item in self.declared_metrics)
        if metric_ids != tuple(sorted(metric_ids)) or len(metric_ids) != len(set(metric_ids)):
            raise ValueError("declared metric IDs must be unique and canonically ordered")
        if type(self.primary_outcomes) is not tuple or not self.primary_outcomes:
            raise ValueError("study requires at least one primary outcome")
        if type(self.secondary_outcomes) is not tuple:
            raise TypeError("secondary outcomes must be a tuple")
        outcomes = self.primary_outcomes + self.secondary_outcomes
        if any(type(item) is not OutcomeDeclarationV1 for item in outcomes):
            raise TypeError("study outcomes contain the wrong record type")
        outcome_ids = tuple(item.outcome_id for item in outcomes)
        if len(outcome_ids) != len(set(outcome_ids)):
            raise ValueError("study outcome IDs cannot repeat")
        unknown_metrics = {item.metric_id for item in outcomes} - set(metric_ids)
        if unknown_metrics:
            raise ValueError("study outcome references an undeclared metric")

    def _validate_criteria(self) -> None:
        for criteria, label in (
            (self.inclusion_criteria, "inclusion criteria"),
            (self.exclusion_criteria, "exclusion criteria"),
        ):
            if type(criteria) is not tuple or not criteria:
                raise ValueError(f"{label} cannot be empty")
            checked = tuple(_text(item, f"{label} item") for item in criteria)
            if checked != criteria or len(checked) != len(set(checked)):
                raise ValueError(f"{label} must be unique exact text")

    def _validate_design_capability(self) -> None:
        if self.design.design_kind is StudyDesignKindV1.RANDOMIZED_CONTROLLED:
            if self.allocation_randomization.method is not AllocationMethodV1.RANDOMIZED:
                raise ValueError("a randomized controlled design requires randomized allocation")
            if (
                self.design.randomization_evidence_sha256
                != self.allocation_randomization.randomization_sha256
            ):
                raise ValueError("design and allocation bind different randomization evidence")
        if self.design.design_kind is StudyDesignKindV1.QUASI_EXPERIMENTAL:
            if self.allocation_randomization.method is AllocationMethodV1.RANDOMIZED:
                raise ValueError("a quasi-experimental design cannot claim randomized allocation")
        if (
            self.analysis_plan.capability is DesignCapabilityV1.CAUSAL
            and self.design.capability is not DesignCapabilityV1.CAUSAL
        ):
            raise ValueError("a causal analysis plan exceeds the declared design capability")

    def as_dict(self) -> dict[str, object]:
        return {
            "allocation_randomization": self.allocation_randomization.as_dict(),
            "analysis_plan": self.analysis_plan.as_dict(),
            "assignment_set": [item.as_dict() for item in self.assignment_set],
            "blinding_reveal": self.blinding_reveal.as_dict(),
            "consent_policy": self.consent_policy.as_dict(),
            "content_locks": [item.as_dict() for item in self.content_locks],
            "data_export_policy": self.data_export_policy.as_dict(),
            "declared_metrics": [item.as_dict() for item in self.declared_metrics],
            "design": self.design.as_dict(),
            "exclusion_criteria": list(self.exclusion_criteria),
            "hypothesis": self.hypothesis,
            "inclusion_criteria": list(self.inclusion_criteria),
            "missing_data_policy": self.missing_data_policy,
            "multiplicity_policy": self.multiplicity_policy,
            "parameter_locks": [item.as_dict() for item in self.parameter_locks],
            "planned_sample_size": self.planned_sample_size,
            "population": self.population,
            "preregistered_at_utc": self.preregistered_at_utc,
            "preregistration_sha256": self.preregistration_sha256,
            "primary_outcomes": [item.as_dict() for item in self.primary_outcomes],
            "question": self.question,
            "retention_policy": self.retention_policy.as_dict(),
            "sample_rationale": self.sample_rationale,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "secondary_outcomes": [item.as_dict() for item in self.secondary_outcomes],
            "seed_policy": self.seed_policy.as_dict(),
            "software_version": self.software_version,
            "stopping_rule": self.stopping_rule,
            "study_status": self.study_status.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyManifestV1:
        expected = {
            "allocation_randomization",
            "analysis_plan",
            "assignment_set",
            "blinding_reveal",
            "consent_policy",
            "content_locks",
            "data_export_policy",
            "declared_metrics",
            "design",
            "exclusion_criteria",
            "hypothesis",
            "inclusion_criteria",
            "missing_data_policy",
            "multiplicity_policy",
            "parameter_locks",
            "planned_sample_size",
            "population",
            "preregistered_at_utc",
            "preregistration_sha256",
            "primary_outcomes",
            "question",
            "retention_policy",
            "sample_rationale",
            "schema_id",
            "schema_version",
            "secondary_outcomes",
            "seed_policy",
            "software_version",
            "stopping_rule",
            "study_status",
        }
        payload = _fields(value, expected, "study manifest")
        if payload["schema_id"] != STUDY_MANIFEST_SCHEMA_ID:
            raise ValueError("study manifest schema ID differs")
        if payload["schema_version"] != STUDY_MANIFEST_SCHEMA_VERSION:
            raise ValueError("study manifest schema version differs")
        return cls(
            question=_text(payload["question"], "study question"),
            hypothesis=_text(payload["hypothesis"], "study hypothesis"),
            assignment_set=tuple(
                _record_tuple(
                    payload["assignment_set"],
                    StudyAssignmentBindingV1,
                    "study assignment set",
                )
            ),
            study_status=StudyStatusV1(
                _enum_text(payload["study_status"], "study status")
            ),
            preregistration_sha256=_sha256(
                payload["preregistration_sha256"], "preregistration digest"
            ),
            preregistered_at_utc=_utc(
                payload["preregistered_at_utc"], "preregistration time"
            ),
            population=_text(payload["population"], "study population"),
            design=StudyDesignV1.from_dict(payload["design"]),
            allocation_randomization=AllocationRandomizationV1.from_dict(
                payload["allocation_randomization"]
            ),
            blinding_reveal=BlindingRevealV1.from_dict(payload["blinding_reveal"]),
            content_locks=tuple(
                _record_tuple(payload["content_locks"], ContentLockV1, "content locks")
            ),
            parameter_locks=tuple(
                _record_tuple(
                    payload["parameter_locks"], ParameterLockV1, "parameter locks"
                )
            ),
            declared_metrics=tuple(
                _record_tuple(
                    payload["declared_metrics"],
                    MetricDeclarationV1,
                    "declared metrics",
                )
            ),
            primary_outcomes=tuple(
                _record_tuple(
                    payload["primary_outcomes"],
                    OutcomeDeclarationV1,
                    "primary outcomes",
                )
            ),
            secondary_outcomes=tuple(
                _record_tuple(
                    payload["secondary_outcomes"],
                    OutcomeDeclarationV1,
                    "secondary outcomes",
                )
            ),
            planned_sample_size=_positive_int(
                payload["planned_sample_size"], "planned sample size"
            ),
            sample_rationale=_text(payload["sample_rationale"], "sample rationale"),
            stopping_rule=_text(payload["stopping_rule"], "stopping rule"),
            missing_data_policy=_text(
                payload["missing_data_policy"], "missing-data policy"
            ),
            multiplicity_policy=_text(
                payload["multiplicity_policy"], "multiplicity policy"
            ),
            inclusion_criteria=_text_tuple(
                payload["inclusion_criteria"], "inclusion criteria", allow_empty=False
            ),
            exclusion_criteria=_text_tuple(
                payload["exclusion_criteria"], "exclusion criteria", allow_empty=False
            ),
            analysis_plan=AnalysisPlanV1.from_dict(payload["analysis_plan"]),
            seed_policy=SeedPolicyV1.from_dict(payload["seed_policy"]),
            software_version=_text(payload["software_version"], "software version"),
            consent_policy=StudyConsentPolicyV1.from_dict(payload["consent_policy"]),
            retention_policy=StudyRetentionPolicyV1.from_dict(
                payload["retention_policy"]
            ),
            data_export_policy=StudyDataExportPolicyV1.from_dict(
                payload["data_export_policy"]
            ),
        )


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


def _restore_study_chain(value: object) -> tuple[ResearchStudy, ...]:
    if type(value) is not list or not value:
        raise ValueError("study revision chain must be a non-empty array")
    predecessor: ResearchStudy | None = None
    restored: list[ResearchStudy] = []
    for item in value:
        payload = _fields(item, _REVISION_FIELDS, "study revision envelope")
        envelope = create_research_study_revision(
            _sha256(payload["content_sha256"], "study content digest"),
            predecessor=predecessor,
        )
        if envelope.as_dict() != payload:
            raise ValueError("study revision envelope differs from canonical lineage")
        restored.append(envelope)
        predecessor = envelope
    return tuple(restored)


@dataclass(frozen=True, slots=True)
class StudyRevisionV1(_CanonicalRecordV1):
    revision_chain: tuple[ResearchStudy, ...]
    manifest: StudyManifestV1

    schema_id: ClassVar[str] = STUDY_REVISION_SCHEMA_ID
    schema_version: ClassVar[int] = STUDY_REVISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.revision_chain) is not tuple or not self.revision_chain:
            raise ValueError("study revision chain cannot be empty")
        if any(type(item) is not ResearchStudy for item in self.revision_chain):
            raise TypeError("study revision chain must contain ResearchStudy values")
        for index, item in enumerate(self.revision_chain):
            item.canonical_bytes()
            if item.revision != index + 1:
                raise ValueError("study revision chain must be contiguous from one")
            if index:
                self.revision_chain[index - 1].validate_successor(item)
        if type(self.manifest) is not StudyManifestV1:
            raise TypeError("study manifest must be StudyManifestV1")
        if self.study.content_sha256 != self.manifest.content_sha256:
            raise ValueError("study envelope does not commit to its exact manifest")

    @property
    def study(self) -> ResearchStudy:
        return self.revision_chain[-1]

    @property
    def study_id(self) -> str:
        return self.study.study_id

    @property
    def content_sha256(self) -> str:
        return self.manifest.content_sha256

    @property
    def design(self) -> StudyDesignV1:
        return self.manifest.design

    def require_causal_support(self) -> None:
        self.manifest.require_causal_support()

    def as_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.as_dict(),
            "revision_chain": [item.as_dict() for item in self.revision_chain],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyRevisionV1:
        payload = _fields(
            value,
            {"manifest", "revision_chain", "schema_id", "schema_version"},
            "study revision",
        )
        if payload["schema_id"] != STUDY_REVISION_SCHEMA_ID:
            raise ValueError("study revision schema ID differs")
        if payload["schema_version"] != STUDY_REVISION_SCHEMA_VERSION:
            raise ValueError("study revision schema version differs")
        return cls(
            revision_chain=_restore_study_chain(payload["revision_chain"]),
            manifest=StudyManifestV1.from_dict(payload["manifest"]),
        )


@dataclass(frozen=True, slots=True)
class StudyProtocolLockV1(_CanonicalRecordV1):
    study_id: str
    study_lineage_id: str
    study_revision_number: int
    study_revision_sha256: str
    manifest_sha256: str
    preregistration_sha256: str
    preregistered_at_utc: str
    locked_at_utc: str
    protocol_lock_id: str = field(init=False)

    schema_id: ClassVar[str] = STUDY_PROTOCOL_LOCK_SCHEMA_ID
    schema_version: ClassVar[int] = STUDY_PROTOCOL_LOCK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _id(self.study_id, _STUDY_ID, "locked study ID")
        _id(self.study_lineage_id, _STUDY_LINEAGE_ID, "locked study lineage ID")
        _positive_int(self.study_revision_number, "locked study revision")
        _sha256(self.study_revision_sha256, "locked study revision digest")
        _sha256(self.manifest_sha256, "locked study manifest digest")
        _sha256(self.preregistration_sha256, "locked preregistration digest")
        _utc(self.preregistered_at_utc, "locked preregistration time")
        _utc(self.locked_at_utc, "protocol lock time")
        if self.locked_at_utc < self.preregistered_at_utc:
            raise ValueError("study protocol cannot be locked before preregistration")
        object.__setattr__(
            self,
            "protocol_lock_id",
            "study-protocol-lock-" + hashlib.sha256(
                _canonical_json_bytes(self.identity_dict())
            ).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "locked_at_utc": self.locked_at_utc,
            "manifest_sha256": self.manifest_sha256,
            "preregistered_at_utc": self.preregistered_at_utc,
            "preregistration_sha256": self.preregistration_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "study_id": self.study_id,
            "study_lineage_id": self.study_lineage_id,
            "study_revision_number": self.study_revision_number,
            "study_revision_sha256": self.study_revision_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "protocol_lock_id": self.protocol_lock_id}

    @classmethod
    def from_dict(cls, value: object) -> StudyProtocolLockV1:
        payload = _fields(
            value,
            {
                "locked_at_utc",
                "manifest_sha256",
                "preregistered_at_utc",
                "preregistration_sha256",
                "protocol_lock_id",
                "schema_id",
                "schema_version",
                "study_id",
                "study_lineage_id",
                "study_revision_number",
                "study_revision_sha256",
            },
            "study protocol lock",
        )
        if payload["schema_id"] != STUDY_PROTOCOL_LOCK_SCHEMA_ID:
            raise ValueError("study protocol lock schema ID differs")
        if payload["schema_version"] != STUDY_PROTOCOL_LOCK_SCHEMA_VERSION:
            raise ValueError("study protocol lock schema version differs")
        restored = cls(
            study_id=_id(payload["study_id"], _STUDY_ID, "locked study ID"),
            study_lineage_id=_id(
                payload["study_lineage_id"],
                _STUDY_LINEAGE_ID,
                "locked study lineage ID",
            ),
            study_revision_number=_positive_int(
                payload["study_revision_number"], "locked study revision"
            ),
            study_revision_sha256=_sha256(
                payload["study_revision_sha256"], "locked study revision digest"
            ),
            manifest_sha256=_sha256(
                payload["manifest_sha256"], "locked study manifest digest"
            ),
            preregistration_sha256=_sha256(
                payload["preregistration_sha256"], "locked preregistration digest"
            ),
            preregistered_at_utc=_utc(
                payload["preregistered_at_utc"], "locked preregistration time"
            ),
            locked_at_utc=_utc(payload["locked_at_utc"], "protocol lock time"),
        )
        if restored.protocol_lock_id != _id(
            payload["protocol_lock_id"], _LOCK_ID, "protocol lock ID"
        ):
            raise ValueError("protocol lock ID differs from locked protocol")
        return restored


@dataclass(frozen=True, slots=True)
class StudyAttemptBindingV1(_CanonicalRecordV1):
    sequence_number: int
    predecessor_entry_sha256: str
    protocol_lock_id: str
    protocol_lock_sha256: str
    study_id: str
    study_revision_sha256: str
    assignment_id: str
    assignment_sha256: str
    attempt_id: str
    attempt_sha256: str
    observed_at_utc: str
    included_at_utc: str

    schema_id: ClassVar[str] = STUDY_ATTEMPT_BINDING_SCHEMA_ID
    schema_version: ClassVar[int] = STUDY_ATTEMPT_BINDING_SCHEMA_VERSION
    entry_kind: ClassVar[StudyLedgerEntryKindV1] = StudyLedgerEntryKindV1.INCLUDED_ATTEMPT

    def __post_init__(self) -> None:
        _positive_int(self.sequence_number, "study ledger sequence")
        _sha256(self.predecessor_entry_sha256, "predecessor ledger-entry digest")
        _id(self.protocol_lock_id, _LOCK_ID, "attempt protocol lock ID")
        _sha256(self.protocol_lock_sha256, "attempt protocol lock digest")
        _id(self.study_id, _STUDY_ID, "attempt study ID")
        _sha256(self.study_revision_sha256, "attempt study revision digest")
        _id(self.assignment_id, _ASSIGNMENT_ID, "included assignment ID")
        _sha256(self.assignment_sha256, "included assignment digest")
        _id(self.attempt_id, _ATTEMPT_ID, "included attempt ID")
        _sha256(self.attempt_sha256, "included attempt digest")
        _utc(self.observed_at_utc, "attempt observation time")
        _utc(self.included_at_utc, "attempt inclusion time")
        if self.included_at_utc < self.observed_at_utc:
            raise ValueError("attempt cannot be included before it is observed")

    def as_dict(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "assignment_sha256": self.assignment_sha256,
            "attempt_id": self.attempt_id,
            "attempt_sha256": self.attempt_sha256,
            "entry_kind": self.entry_kind.value,
            "included_at_utc": self.included_at_utc,
            "observed_at_utc": self.observed_at_utc,
            "predecessor_entry_sha256": self.predecessor_entry_sha256,
            "protocol_lock_id": self.protocol_lock_id,
            "protocol_lock_sha256": self.protocol_lock_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence_number": self.sequence_number,
            "study_id": self.study_id,
            "study_revision_sha256": self.study_revision_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyAttemptBindingV1:
        expected = {
            "assignment_id",
            "assignment_sha256",
            "attempt_id",
            "attempt_sha256",
            "entry_kind",
            "included_at_utc",
            "observed_at_utc",
            "predecessor_entry_sha256",
            "protocol_lock_id",
            "protocol_lock_sha256",
            "schema_id",
            "schema_version",
            "sequence_number",
            "study_id",
            "study_revision_sha256",
        }
        payload = _fields(value, expected, "study attempt binding")
        if payload["schema_id"] != STUDY_ATTEMPT_BINDING_SCHEMA_ID:
            raise ValueError("study attempt binding schema ID differs")
        if payload["schema_version"] != STUDY_ATTEMPT_BINDING_SCHEMA_VERSION:
            raise ValueError("study attempt binding schema version differs")
        if payload["entry_kind"] != StudyLedgerEntryKindV1.INCLUDED_ATTEMPT.value:
            raise ValueError("study attempt binding entry kind differs")
        return cls(
            sequence_number=_positive_int(payload["sequence_number"], "study ledger sequence"),
            predecessor_entry_sha256=_sha256(
                payload["predecessor_entry_sha256"], "predecessor ledger-entry digest"
            ),
            protocol_lock_id=_id(
                payload["protocol_lock_id"], _LOCK_ID, "attempt protocol lock ID"
            ),
            protocol_lock_sha256=_sha256(
                payload["protocol_lock_sha256"], "attempt protocol lock digest"
            ),
            study_id=_id(payload["study_id"], _STUDY_ID, "attempt study ID"),
            study_revision_sha256=_sha256(
                payload["study_revision_sha256"], "attempt study revision digest"
            ),
            assignment_id=_id(
                payload["assignment_id"], _ASSIGNMENT_ID, "included assignment ID"
            ),
            assignment_sha256=_sha256(
                payload["assignment_sha256"], "included assignment digest"
            ),
            attempt_id=_id(payload["attempt_id"], _ATTEMPT_ID, "included attempt ID"),
            attempt_sha256=_sha256(payload["attempt_sha256"], "included attempt digest"),
            observed_at_utc=_utc(payload["observed_at_utc"], "attempt observation time"),
            included_at_utc=_utc(payload["included_at_utc"], "attempt inclusion time"),
        )


@dataclass(frozen=True, slots=True)
class StudyAmendmentV1(_CanonicalRecordV1):
    sequence_number: int
    predecessor_entry_sha256: str
    protocol_lock_id: str
    protocol_lock_sha256: str
    study_id: str
    study_revision_sha256: str
    amendment_number: int
    predecessor_amendment_id: str | None
    predecessor_amendment_sha256: str | None
    amended_at_utc: str
    rationale: str
    changed_fields: tuple[str, ...]
    replacement_protocol_sha256: str
    prospective_only: bool
    amendment_id: str = field(init=False)

    schema_id: ClassVar[str] = STUDY_AMENDMENT_SCHEMA_ID
    schema_version: ClassVar[int] = STUDY_AMENDMENT_SCHEMA_VERSION
    entry_kind: ClassVar[StudyLedgerEntryKindV1] = StudyLedgerEntryKindV1.AMENDMENT

    def __post_init__(self) -> None:
        _positive_int(self.sequence_number, "study ledger sequence")
        _sha256(self.predecessor_entry_sha256, "predecessor ledger-entry digest")
        _id(self.protocol_lock_id, _LOCK_ID, "amendment protocol lock ID")
        _sha256(self.protocol_lock_sha256, "amendment protocol lock digest")
        _id(self.study_id, _STUDY_ID, "amendment study ID")
        _sha256(self.study_revision_sha256, "amendment study revision digest")
        _positive_int(self.amendment_number, "study amendment number")
        if (self.predecessor_amendment_id is None) != (
            self.predecessor_amendment_sha256 is None
        ):
            raise ValueError("amendment predecessor ID and digest must travel together")
        if self.amendment_number == 1:
            if self.predecessor_amendment_id is not None:
                raise ValueError("first amendment cannot have an amendment predecessor")
        else:
            _id(
                self.predecessor_amendment_id,
                _AMENDMENT_ID,
                "predecessor amendment ID",
            )
            _sha256(
                self.predecessor_amendment_sha256,
                "predecessor amendment digest",
            )
        _utc(self.amended_at_utc, "study amendment time")
        _text(self.rationale, "study amendment rationale")
        if type(self.changed_fields) is not tuple or not self.changed_fields:
            raise ValueError("study amendment changed fields cannot be empty")
        changed = tuple(_text(item, "changed study field") for item in self.changed_fields)
        if changed != tuple(sorted(changed)) or len(changed) != len(set(changed)):
            raise ValueError("changed study fields must be unique and canonically ordered")
        _sha256(self.replacement_protocol_sha256, "replacement protocol digest")
        _exact_bool(self.prospective_only, "prospective-only amendment flag")
        object.__setattr__(
            self,
            "amendment_id",
            "study-amendment-" + hashlib.sha256(
                _canonical_json_bytes(self.identity_dict())
            ).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "amended_at_utc": self.amended_at_utc,
            "amendment_number": self.amendment_number,
            "changed_fields": list(self.changed_fields),
            "entry_kind": self.entry_kind.value,
            "predecessor_amendment_id": self.predecessor_amendment_id,
            "predecessor_amendment_sha256": self.predecessor_amendment_sha256,
            "predecessor_entry_sha256": self.predecessor_entry_sha256,
            "prospective_only": self.prospective_only,
            "protocol_lock_id": self.protocol_lock_id,
            "protocol_lock_sha256": self.protocol_lock_sha256,
            "rationale": self.rationale,
            "replacement_protocol_sha256": self.replacement_protocol_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence_number": self.sequence_number,
            "study_id": self.study_id,
            "study_revision_sha256": self.study_revision_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "amendment_id": self.amendment_id}

    @classmethod
    def from_dict(cls, value: object) -> StudyAmendmentV1:
        expected = {
            "amended_at_utc",
            "amendment_id",
            "amendment_number",
            "changed_fields",
            "entry_kind",
            "predecessor_amendment_id",
            "predecessor_amendment_sha256",
            "predecessor_entry_sha256",
            "prospective_only",
            "protocol_lock_id",
            "protocol_lock_sha256",
            "rationale",
            "replacement_protocol_sha256",
            "schema_id",
            "schema_version",
            "sequence_number",
            "study_id",
            "study_revision_sha256",
        }
        payload = _fields(value, expected, "study amendment")
        if payload["schema_id"] != STUDY_AMENDMENT_SCHEMA_ID:
            raise ValueError("study amendment schema ID differs")
        if payload["schema_version"] != STUDY_AMENDMENT_SCHEMA_VERSION:
            raise ValueError("study amendment schema version differs")
        if payload["entry_kind"] != StudyLedgerEntryKindV1.AMENDMENT.value:
            raise ValueError("study amendment entry kind differs")
        restored = cls(
            sequence_number=_positive_int(payload["sequence_number"], "study ledger sequence"),
            predecessor_entry_sha256=_sha256(
                payload["predecessor_entry_sha256"], "predecessor ledger-entry digest"
            ),
            protocol_lock_id=_id(
                payload["protocol_lock_id"], _LOCK_ID, "amendment protocol lock ID"
            ),
            protocol_lock_sha256=_sha256(
                payload["protocol_lock_sha256"], "amendment protocol lock digest"
            ),
            study_id=_id(payload["study_id"], _STUDY_ID, "amendment study ID"),
            study_revision_sha256=_sha256(
                payload["study_revision_sha256"], "amendment study revision digest"
            ),
            amendment_number=_positive_int(
                payload["amendment_number"], "study amendment number"
            ),
            predecessor_amendment_id=(
                None
                if payload["predecessor_amendment_id"] is None
                else _id(
                    payload["predecessor_amendment_id"],
                    _AMENDMENT_ID,
                    "predecessor amendment ID",
                )
            ),
            predecessor_amendment_sha256=(
                None
                if payload["predecessor_amendment_sha256"] is None
                else _sha256(
                    payload["predecessor_amendment_sha256"],
                    "predecessor amendment digest",
                )
            ),
            amended_at_utc=_utc(payload["amended_at_utc"], "study amendment time"),
            rationale=_text(payload["rationale"], "study amendment rationale"),
            changed_fields=_text_tuple(
                payload["changed_fields"], "changed study fields", allow_empty=False
            ),
            replacement_protocol_sha256=_sha256(
                payload["replacement_protocol_sha256"], "replacement protocol digest"
            ),
            prospective_only=_exact_bool(
                payload["prospective_only"], "prospective-only amendment flag"
            ),
        )
        if restored.amendment_id != _id(
            payload["amendment_id"], _AMENDMENT_ID, "study amendment ID"
        ):
            raise ValueError("study amendment ID differs from amendment content")
        return restored


@dataclass(frozen=True, slots=True)
class ProtocolDeviationV1(_CanonicalRecordV1):
    sequence_number: int
    predecessor_entry_sha256: str
    protocol_lock_id: str
    protocol_lock_sha256: str
    study_id: str
    study_revision_sha256: str
    deviation_number: int
    predecessor_deviation_id: str | None
    predecessor_deviation_sha256: str | None
    attempt_id: str
    attempt_sha256: str
    occurred_at_utc: str
    recorded_at_utc: str
    description: str
    impact: ProtocolDeviationImpactV1
    disposition: str
    deviation_id: str = field(init=False)

    schema_id: ClassVar[str] = PROTOCOL_DEVIATION_SCHEMA_ID
    schema_version: ClassVar[int] = PROTOCOL_DEVIATION_SCHEMA_VERSION
    entry_kind: ClassVar[StudyLedgerEntryKindV1] = StudyLedgerEntryKindV1.PROTOCOL_DEVIATION

    def __post_init__(self) -> None:
        _positive_int(self.sequence_number, "study ledger sequence")
        _sha256(self.predecessor_entry_sha256, "predecessor ledger-entry digest")
        _id(self.protocol_lock_id, _LOCK_ID, "deviation protocol lock ID")
        _sha256(self.protocol_lock_sha256, "deviation protocol lock digest")
        _id(self.study_id, _STUDY_ID, "deviation study ID")
        _sha256(self.study_revision_sha256, "deviation study revision digest")
        _positive_int(self.deviation_number, "protocol deviation number")
        if (self.predecessor_deviation_id is None) != (
            self.predecessor_deviation_sha256 is None
        ):
            raise ValueError("deviation predecessor ID and digest must travel together")
        if self.deviation_number == 1:
            if self.predecessor_deviation_id is not None:
                raise ValueError("first deviation cannot have a deviation predecessor")
        else:
            _id(
                self.predecessor_deviation_id,
                _DEVIATION_ID,
                "predecessor deviation ID",
            )
            _sha256(
                self.predecessor_deviation_sha256,
                "predecessor deviation digest",
            )
        _id(self.attempt_id, _ATTEMPT_ID, "deviation attempt ID")
        _sha256(self.attempt_sha256, "deviation attempt digest")
        _utc(self.occurred_at_utc, "protocol deviation occurrence time")
        _utc(self.recorded_at_utc, "protocol deviation record time")
        if self.recorded_at_utc < self.occurred_at_utc:
            raise ValueError("a protocol deviation cannot be recorded before it occurred")
        _text(self.description, "protocol deviation description")
        if type(self.impact) is not ProtocolDeviationImpactV1:
            raise TypeError("protocol deviation impact must be ProtocolDeviationImpactV1")
        _text(self.disposition, "protocol deviation disposition")
        object.__setattr__(
            self,
            "deviation_id",
            "protocol-deviation-" + hashlib.sha256(
                _canonical_json_bytes(self.identity_dict())
            ).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_sha256": self.attempt_sha256,
            "description": self.description,
            "deviation_number": self.deviation_number,
            "disposition": self.disposition,
            "entry_kind": self.entry_kind.value,
            "impact": self.impact.value,
            "occurred_at_utc": self.occurred_at_utc,
            "predecessor_deviation_id": self.predecessor_deviation_id,
            "predecessor_deviation_sha256": self.predecessor_deviation_sha256,
            "predecessor_entry_sha256": self.predecessor_entry_sha256,
            "protocol_lock_id": self.protocol_lock_id,
            "protocol_lock_sha256": self.protocol_lock_sha256,
            "recorded_at_utc": self.recorded_at_utc,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence_number": self.sequence_number,
            "study_id": self.study_id,
            "study_revision_sha256": self.study_revision_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "deviation_id": self.deviation_id}

    @classmethod
    def from_dict(cls, value: object) -> ProtocolDeviationV1:
        expected = {
            "attempt_id",
            "attempt_sha256",
            "description",
            "deviation_id",
            "deviation_number",
            "disposition",
            "entry_kind",
            "impact",
            "occurred_at_utc",
            "predecessor_deviation_id",
            "predecessor_deviation_sha256",
            "predecessor_entry_sha256",
            "protocol_lock_id",
            "protocol_lock_sha256",
            "recorded_at_utc",
            "schema_id",
            "schema_version",
            "sequence_number",
            "study_id",
            "study_revision_sha256",
        }
        payload = _fields(value, expected, "protocol deviation")
        if payload["schema_id"] != PROTOCOL_DEVIATION_SCHEMA_ID:
            raise ValueError("protocol deviation schema ID differs")
        if payload["schema_version"] != PROTOCOL_DEVIATION_SCHEMA_VERSION:
            raise ValueError("protocol deviation schema version differs")
        if payload["entry_kind"] != StudyLedgerEntryKindV1.PROTOCOL_DEVIATION.value:
            raise ValueError("protocol deviation entry kind differs")
        restored = cls(
            sequence_number=_positive_int(payload["sequence_number"], "study ledger sequence"),
            predecessor_entry_sha256=_sha256(
                payload["predecessor_entry_sha256"], "predecessor ledger-entry digest"
            ),
            protocol_lock_id=_id(
                payload["protocol_lock_id"], _LOCK_ID, "deviation protocol lock ID"
            ),
            protocol_lock_sha256=_sha256(
                payload["protocol_lock_sha256"], "deviation protocol lock digest"
            ),
            study_id=_id(payload["study_id"], _STUDY_ID, "deviation study ID"),
            study_revision_sha256=_sha256(
                payload["study_revision_sha256"], "deviation study revision digest"
            ),
            deviation_number=_positive_int(
                payload["deviation_number"], "protocol deviation number"
            ),
            predecessor_deviation_id=(
                None
                if payload["predecessor_deviation_id"] is None
                else _id(
                    payload["predecessor_deviation_id"],
                    _DEVIATION_ID,
                    "predecessor deviation ID",
                )
            ),
            predecessor_deviation_sha256=(
                None
                if payload["predecessor_deviation_sha256"] is None
                else _sha256(
                    payload["predecessor_deviation_sha256"],
                    "predecessor deviation digest",
                )
            ),
            attempt_id=_id(payload["attempt_id"], _ATTEMPT_ID, "deviation attempt ID"),
            attempt_sha256=_sha256(payload["attempt_sha256"], "deviation attempt digest"),
            occurred_at_utc=_utc(
                payload["occurred_at_utc"], "protocol deviation occurrence time"
            ),
            recorded_at_utc=_utc(
                payload["recorded_at_utc"], "protocol deviation record time"
            ),
            description=_text(payload["description"], "protocol deviation description"),
            impact=ProtocolDeviationImpactV1(
                _enum_text(payload["impact"], "protocol deviation impact")
            ),
            disposition=_text(payload["disposition"], "protocol deviation disposition"),
        )
        if restored.deviation_id != _id(
            payload["deviation_id"], _DEVIATION_ID, "protocol deviation ID"
        ):
            raise ValueError("protocol deviation ID differs from deviation content")
        return restored


LedgerEntryV1 = StudyAttemptBindingV1 | StudyAmendmentV1 | ProtocolDeviationV1


@dataclass(frozen=True, slots=True)
class StudyExecutionLedgerV1(_CanonicalRecordV1):
    study_revision: StudyRevisionV1
    protocol_lock: StudyProtocolLockV1
    included_attempts: tuple[StudyAttemptBindingV1, ...]
    amendments: tuple[StudyAmendmentV1, ...]
    protocol_deviations: tuple[ProtocolDeviationV1, ...]

    schema_id: ClassVar[str] = STUDY_EXECUTION_LEDGER_SCHEMA_ID
    schema_version: ClassVar[int] = STUDY_EXECUTION_LEDGER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.study_revision) is not StudyRevisionV1:
            raise TypeError("execution ledger study must be StudyRevisionV1")
        if type(self.protocol_lock) is not StudyProtocolLockV1:
            raise TypeError("execution ledger lock must be StudyProtocolLockV1")
        self._validate_lock()
        for records, record_type, label in (
            (self.included_attempts, StudyAttemptBindingV1, "included attempts"),
            (self.amendments, StudyAmendmentV1, "study amendments"),
            (self.protocol_deviations, ProtocolDeviationV1, "protocol deviations"),
        ):
            if type(records) is not tuple:
                raise TypeError(f"{label} must be an append-only tuple")
            if any(type(item) is not record_type for item in records):
                raise TypeError(f"{label} contain the wrong record type")
        self._validate_entries()

    @property
    def entries(self) -> tuple[LedgerEntryV1, ...]:
        return tuple(
            sorted(
                self.included_attempts + self.amendments + self.protocol_deviations,
                key=lambda item: item.sequence_number,
            )
        )

    @property
    def observation_started(self) -> bool:
        return bool(self.included_attempts)

    def _validate_lock(self) -> None:
        study = self.study_revision
        lock = self.protocol_lock
        if (
            lock.study_id != study.study_id
            or lock.study_lineage_id != study.study.lineage_id
            or lock.study_revision_number != study.study.revision
            or lock.study_revision_sha256 != study.sha256
            or lock.manifest_sha256 != study.manifest.sha256
            or lock.preregistration_sha256 != study.manifest.preregistration_sha256
            or lock.preregistered_at_utc != study.manifest.preregistered_at_utc
        ):
            raise ValueError("execution ledger lock differs from the exact study revision")

    def _validate_entries(self) -> None:
        entries = self.entries
        if tuple(item.sequence_number for item in entries) != tuple(
            range(1, len(entries) + 1)
        ):
            raise ValueError("study ledger sequence must be contiguous from one")
        predecessor_sha256 = self.protocol_lock.sha256
        assignment_set = {
            (item.assignment_id, item.assignment_sha256)
            for item in self.study_revision.manifest.assignment_set
        }
        seen_attempt_ids: set[str] = set()
        last_amendment: StudyAmendmentV1 | None = None
        last_deviation: ProtocolDeviationV1 | None = None
        for item in entries:
            if item.predecessor_entry_sha256 != predecessor_sha256:
                raise ValueError("study ledger entry does not bind its exact predecessor")
            if (
                item.protocol_lock_id != self.protocol_lock.protocol_lock_id
                or item.protocol_lock_sha256 != self.protocol_lock.sha256
                or item.study_id != self.study_revision.study_id
                or item.study_revision_sha256 != self.study_revision.sha256
            ):
                raise ValueError("study ledger entry differs from the locked protocol")
            if type(item) is StudyAttemptBindingV1:
                if (item.assignment_id, item.assignment_sha256) not in assignment_set:
                    raise ValueError("included attempt is outside the exact assignment set")
                if item.attempt_id in seen_attempt_ids:
                    raise ValueError("one assignment attempt cannot be included twice")
                if item.observed_at_utc <= self.protocol_lock.locked_at_utc:
                    raise ValueError("included attempt must be observed after protocol lock")
                seen_attempt_ids.add(item.attempt_id)
            elif type(item) is StudyAmendmentV1:
                expected_number = 1 if last_amendment is None else last_amendment.amendment_number + 1
                if item.amendment_number != expected_number:
                    raise ValueError("study amendments must be contiguous from one")
                expected_id = None if last_amendment is None else last_amendment.amendment_id
                expected_sha = None if last_amendment is None else last_amendment.sha256
                if (
                    item.predecessor_amendment_id != expected_id
                    or item.predecessor_amendment_sha256 != expected_sha
                ):
                    raise ValueError("study amendment does not bind its exact predecessor")
                if item.amended_at_utc < self.protocol_lock.locked_at_utc:
                    raise ValueError("study amendment predates the protocol lock")
                if item.replacement_protocol_sha256 == self.protocol_lock.manifest_sha256:
                    raise ValueError("study amendment must bind changed protocol content")
                last_amendment = item
            else:
                expected_number = 1 if last_deviation is None else last_deviation.deviation_number + 1
                if item.deviation_number != expected_number:
                    raise ValueError("protocol deviations must be contiguous from one")
                expected_id = None if last_deviation is None else last_deviation.deviation_id
                expected_sha = None if last_deviation is None else last_deviation.sha256
                if (
                    item.predecessor_deviation_id != expected_id
                    or item.predecessor_deviation_sha256 != expected_sha
                ):
                    raise ValueError("protocol deviation does not bind its exact predecessor")
                matching_attempt = next(
                    (
                        attempt
                        for attempt in self.included_attempts
                        if attempt.attempt_id == item.attempt_id
                        and attempt.attempt_sha256 == item.attempt_sha256
                        and attempt.sequence_number < item.sequence_number
                    ),
                    None,
                )
                if matching_attempt is None:
                    raise ValueError("protocol deviation must follow its exact included attempt")
                if item.occurred_at_utc <= self.protocol_lock.locked_at_utc:
                    raise ValueError("protocol deviation cannot predate the protocol lock")
                last_deviation = item
            predecessor_sha256 = item.sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "amendments": [item.as_dict() for item in self.amendments],
            "included_attempts": [item.as_dict() for item in self.included_attempts],
            "protocol_deviations": [item.as_dict() for item in self.protocol_deviations],
            "protocol_lock": self.protocol_lock.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "study_revision": self.study_revision.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> StudyExecutionLedgerV1:
        payload = _fields(
            value,
            {
                "amendments",
                "included_attempts",
                "protocol_deviations",
                "protocol_lock",
                "schema_id",
                "schema_version",
                "study_revision",
            },
            "study execution ledger",
        )
        if payload["schema_id"] != STUDY_EXECUTION_LEDGER_SCHEMA_ID:
            raise ValueError("study execution ledger schema ID differs")
        if payload["schema_version"] != STUDY_EXECUTION_LEDGER_SCHEMA_VERSION:
            raise ValueError("study execution ledger schema version differs")
        return cls(
            study_revision=StudyRevisionV1.from_dict(payload["study_revision"]),
            protocol_lock=StudyProtocolLockV1.from_dict(payload["protocol_lock"]),
            included_attempts=tuple(
                _record_tuple(
                    payload["included_attempts"],
                    StudyAttemptBindingV1,
                    "included attempts",
                )
            ),
            amendments=tuple(
                _record_tuple(payload["amendments"], StudyAmendmentV1, "study amendments")
            ),
            protocol_deviations=tuple(
                _record_tuple(
                    payload["protocol_deviations"],
                    ProtocolDeviationV1,
                    "protocol deviations",
                )
            ),
        )


def create_study(manifest: StudyManifestV1) -> StudyRevisionV1:
    if type(manifest) is not StudyManifestV1:
        raise TypeError("study manifest must be StudyManifestV1")
    return StudyRevisionV1(
        revision_chain=(create_research_study_revision(manifest.content_sha256),),
        manifest=manifest,
    )


def revise_study(
    predecessor: StudyRevisionV1,
    manifest: StudyManifestV1,
    *,
    protocol_lock: StudyProtocolLockV1 | None = None,
    execution_ledger: StudyExecutionLedgerV1 | None = None,
) -> StudyRevisionV1:
    """Create a pre-lock successor and refuse any locked/executed protocol."""

    if type(predecessor) is not StudyRevisionV1:
        raise TypeError("study predecessor must be StudyRevisionV1")
    if type(manifest) is not StudyManifestV1:
        raise TypeError("study manifest must be StudyManifestV1")
    if protocol_lock is not None:
        if type(protocol_lock) is not StudyProtocolLockV1:
            raise TypeError("protocol lock must be StudyProtocolLockV1 or None")
        if protocol_lock.study_id != predecessor.study_id:
            raise ValueError("protocol lock belongs to a different study")
        raise ValueError(
            "locked study protocols cannot be revised; record an immutable amendment"
        )
    if execution_ledger is not None:
        if type(execution_ledger) is not StudyExecutionLedgerV1:
            raise TypeError("execution ledger must be StudyExecutionLedgerV1 or None")
        if execution_ledger.study_revision.sha256 != predecessor.sha256:
            raise ValueError("execution ledger belongs to a different study revision")
        raise ValueError(
            "executed study protocols cannot be revised; record an immutable amendment"
        )
    successor = create_research_study_revision(
        manifest.content_sha256,
        predecessor=predecessor.study,
    )
    return StudyRevisionV1(
        revision_chain=predecessor.revision_chain + (successor,),
        manifest=manifest,
    )


def lock_study_protocol(
    study_revision: StudyRevisionV1,
    *,
    locked_at_utc: str,
) -> StudyProtocolLockV1:
    if type(study_revision) is not StudyRevisionV1:
        raise TypeError("study revision must be StudyRevisionV1")
    return StudyProtocolLockV1(
        study_id=study_revision.study_id,
        study_lineage_id=study_revision.study.lineage_id,
        study_revision_number=study_revision.study.revision,
        study_revision_sha256=study_revision.sha256,
        manifest_sha256=study_revision.manifest.sha256,
        preregistration_sha256=study_revision.manifest.preregistration_sha256,
        preregistered_at_utc=study_revision.manifest.preregistered_at_utc,
        locked_at_utc=_utc(locked_at_utc, "protocol lock time"),
    )


def create_study_execution_ledger(
    study_revision: StudyRevisionV1,
    *,
    locked_at_utc: str | None = None,
    protocol_lock: StudyProtocolLockV1 | None = None,
) -> StudyExecutionLedgerV1:
    if type(study_revision) is not StudyRevisionV1:
        raise TypeError("study revision must be StudyRevisionV1")
    if (locked_at_utc is None) == (protocol_lock is None):
        raise ValueError("provide exactly one of locked_at_utc or protocol_lock")
    lock = (
        lock_study_protocol(study_revision, locked_at_utc=locked_at_utc)
        if protocol_lock is None
        else protocol_lock
    )
    if type(lock) is not StudyProtocolLockV1:
        raise TypeError("protocol lock must be StudyProtocolLockV1")
    return StudyExecutionLedgerV1(
        study_revision=study_revision,
        protocol_lock=lock,
        included_attempts=(),
        amendments=(),
        protocol_deviations=(),
    )


def _next_ledger_position(ledger: StudyExecutionLedgerV1) -> tuple[int, str]:
    if type(ledger) is not StudyExecutionLedgerV1:
        raise TypeError("study execution ledger must be StudyExecutionLedgerV1")
    entries = ledger.entries
    return (
        len(entries) + 1,
        ledger.protocol_lock.sha256 if not entries else entries[-1].sha256,
    )


def include_study_attempt(
    ledger: StudyExecutionLedgerV1,
    *,
    assignment_id: str,
    assignment_sha256: str,
    attempt_id: str,
    attempt_sha256: str,
    observed_at_utc: str,
    included_at_utc: str,
) -> StudyExecutionLedgerV1:
    sequence_number, predecessor_sha256 = _next_ledger_position(ledger)
    binding = StudyAttemptBindingV1(
        sequence_number=sequence_number,
        predecessor_entry_sha256=predecessor_sha256,
        protocol_lock_id=ledger.protocol_lock.protocol_lock_id,
        protocol_lock_sha256=ledger.protocol_lock.sha256,
        study_id=ledger.study_revision.study_id,
        study_revision_sha256=ledger.study_revision.sha256,
        assignment_id=assignment_id,
        assignment_sha256=assignment_sha256,
        attempt_id=attempt_id,
        attempt_sha256=attempt_sha256,
        observed_at_utc=observed_at_utc,
        included_at_utc=included_at_utc,
    )
    return StudyExecutionLedgerV1(
        study_revision=ledger.study_revision,
        protocol_lock=ledger.protocol_lock,
        included_attempts=ledger.included_attempts + (binding,),
        amendments=ledger.amendments,
        protocol_deviations=ledger.protocol_deviations,
    )


def append_study_amendment(
    ledger: StudyExecutionLedgerV1,
    *,
    amended_at_utc: str,
    rationale: str,
    changed_fields: tuple[str, ...],
    replacement_protocol_sha256: str,
    prospective_only: bool,
) -> StudyExecutionLedgerV1:
    sequence_number, predecessor_entry_sha256 = _next_ledger_position(ledger)
    predecessor = ledger.amendments[-1] if ledger.amendments else None
    amendment = StudyAmendmentV1(
        sequence_number=sequence_number,
        predecessor_entry_sha256=predecessor_entry_sha256,
        protocol_lock_id=ledger.protocol_lock.protocol_lock_id,
        protocol_lock_sha256=ledger.protocol_lock.sha256,
        study_id=ledger.study_revision.study_id,
        study_revision_sha256=ledger.study_revision.sha256,
        amendment_number=len(ledger.amendments) + 1,
        predecessor_amendment_id=(
            None if predecessor is None else predecessor.amendment_id
        ),
        predecessor_amendment_sha256=(None if predecessor is None else predecessor.sha256),
        amended_at_utc=amended_at_utc,
        rationale=rationale,
        changed_fields=changed_fields,
        replacement_protocol_sha256=replacement_protocol_sha256,
        prospective_only=prospective_only,
    )
    return StudyExecutionLedgerV1(
        study_revision=ledger.study_revision,
        protocol_lock=ledger.protocol_lock,
        included_attempts=ledger.included_attempts,
        amendments=ledger.amendments + (amendment,),
        protocol_deviations=ledger.protocol_deviations,
    )


def append_protocol_deviation(
    ledger: StudyExecutionLedgerV1,
    *,
    attempt_id: str,
    attempt_sha256: str,
    occurred_at_utc: str,
    recorded_at_utc: str,
    description: str,
    impact: ProtocolDeviationImpactV1,
    disposition: str,
) -> StudyExecutionLedgerV1:
    sequence_number, predecessor_entry_sha256 = _next_ledger_position(ledger)
    predecessor = ledger.protocol_deviations[-1] if ledger.protocol_deviations else None
    deviation = ProtocolDeviationV1(
        sequence_number=sequence_number,
        predecessor_entry_sha256=predecessor_entry_sha256,
        protocol_lock_id=ledger.protocol_lock.protocol_lock_id,
        protocol_lock_sha256=ledger.protocol_lock.sha256,
        study_id=ledger.study_revision.study_id,
        study_revision_sha256=ledger.study_revision.sha256,
        deviation_number=len(ledger.protocol_deviations) + 1,
        predecessor_deviation_id=(None if predecessor is None else predecessor.deviation_id),
        predecessor_deviation_sha256=(None if predecessor is None else predecessor.sha256),
        attempt_id=attempt_id,
        attempt_sha256=attempt_sha256,
        occurred_at_utc=occurred_at_utc,
        recorded_at_utc=recorded_at_utc,
        description=description,
        impact=impact,
        disposition=disposition,
    )
    return StudyExecutionLedgerV1(
        study_revision=ledger.study_revision,
        protocol_lock=ledger.protocol_lock,
        included_attempts=ledger.included_attempts,
        amendments=ledger.amendments,
        protocol_deviations=ledger.protocol_deviations + (deviation,),
    )


amend_study_protocol = append_study_amendment
record_protocol_deviation = append_protocol_deviation


__all__ = [
    "PROTOCOL_DEVIATION_SCHEMA_ID",
    "PROTOCOL_DEVIATION_SCHEMA_VERSION",
    "STUDY_AMENDMENT_SCHEMA_ID",
    "STUDY_AMENDMENT_SCHEMA_VERSION",
    "STUDY_ATTEMPT_BINDING_SCHEMA_ID",
    "STUDY_ATTEMPT_BINDING_SCHEMA_VERSION",
    "STUDY_EXECUTION_LEDGER_SCHEMA_ID",
    "STUDY_EXECUTION_LEDGER_SCHEMA_VERSION",
    "STUDY_MANIFEST_SCHEMA_ID",
    "STUDY_MANIFEST_SCHEMA_VERSION",
    "STUDY_PROTOCOL_LOCK_SCHEMA_ID",
    "STUDY_PROTOCOL_LOCK_SCHEMA_VERSION",
    "STUDY_REVISION_SCHEMA_ID",
    "STUDY_REVISION_SCHEMA_VERSION",
    "AllocationMethodV1",
    "AllocationRandomizationV1",
    "AnalysisPlanV1",
    "BlindingRevealV1",
    "ContentLockV1",
    "DesignCapabilityV1",
    "MetricDeclarationV1",
    "OutcomeDeclarationV1",
    "ParameterLockV1",
    "ProtocolDeviationImpactV1",
    "ProtocolDeviationV1",
    "StudyAmendmentV1",
    "StudyAssignmentBindingV1",
    "StudyAttemptBindingV1",
    "StudyConsentPolicyV1",
    "StudyDataExportPolicyV1",
    "StudyDesignKindV1",
    "StudyDesignV1",
    "StudyExecutionLedgerV1",
    "StudyLedgerEntryKindV1",
    "StudyManifestV1",
    "StudyProtocolLockV1",
    "StudyRetentionPolicyV1",
    "StudyRevisionV1",
    "StudyStatusV1",
    "amend_study_protocol",
    "append_protocol_deviation",
    "append_study_amendment",
    "create_study",
    "create_study_execution_ledger",
    "include_study_attempt",
    "lock_study_protocol",
    "record_protocol_deviation",
    "revise_study",
]
