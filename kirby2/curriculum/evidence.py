"""Immutable learner evidence ledger contracts for WO34-A.

The records in this module are observations and policy-scored evidence.  They do not
contain, update, or claim a learner-mastery projection.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from .errors import (
    AMBIGUITY_ERROR_TYPES_V1,
    LearnerErrorTypeV1,
    mapped_skill_for_error_v1,
)
from .skills import canonical_json_bytes, require_stable_skill_v1, sha256_json


LEARNER_EVIDENCE_SCHEMA_VERSION_V1 = 1
LEARNER_EVIDENCE_LEDGER_ID_V1 = "LEARNER_EVIDENCE_LEDGER_V1"
POLICY_SCALE_V1 = 1_000_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must contain an object")
    try:
        canonical = canonical_json_bytes(payload)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} contains a non-canonical value") from error
    if canonical != raw:
        raise ValueError(f"{label} is not canonical JSON")
    return payload


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    return value


def _exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    return value


def _optional_exact_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _exact_int(value, label)


def _optional_exact_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _exact_text(value, label)


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must be NFC")
    return value


def _identifier(value: object, label: str) -> str:
    selected = _text(value, label)
    if _IDENTIFIER.fullmatch(selected) is None:
        raise ValueError(f"{label} must be an uppercase identifier")
    return selected


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _utc(value: object, label: str) -> str:
    selected = _text(value, label)
    if not selected.endswith("Z"):
        raise ValueError(f"{label} must use explicit UTC Z notation")
    try:
        parsed = datetime.fromisoformat(selected[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be ISO-8601") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be UTC")
    return selected


class EvidenceReferenceKindV1(str, Enum):
    OPPORTUNITY = "OPPORTUNITY"
    ACTION = "ACTION"
    OBSERVABLE_CONTEXT = "OBSERVABLE_CONTEXT"
    SCORING_INPUT = "SCORING_INPUT"
    SOURCE_RECORD = "SOURCE_RECORD"


class EvidenceFamilyV1(str, Enum):
    CORRECT_CLASSIFICATION = "CORRECT_CLASSIFICATION"
    APPROPRIATE_NO_TRADE = "APPROPRIATE_NO_TRADE"
    DISCIPLINE_COMPLIANCE = "DISCIPLINE_COMPLIANCE"
    FILL_QUALITY = "FILL_QUALITY"
    REACTION_TIMING = "REACTION_TIMING"
    CANCEL_MISTAKE = "CANCEL_MISTAKE"
    QUEUE_MISUNDERSTANDING = "QUEUE_MISUNDERSTANDING"
    ROUTING_ERROR = "ROUTING_ERROR"
    ADVERSE_SELECTION = "ADVERSE_SELECTION"
    HOTKEY_ERROR = "HOTKEY_ERROR"


@dataclass(frozen=True, slots=True)
class SupportingEvidenceReferenceV1:
    kind: EvidenceReferenceKindV1
    reference_id: str
    sha256: str
    evidence_family: EvidenceFamilyV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceReferenceKindV1):
            raise TypeError("learner evidence reference kind is invalid")
        if self.evidence_family is not None and not isinstance(
            self.evidence_family,
            EvidenceFamilyV1,
        ):
            raise TypeError("learner evidence family is invalid")
        _text(self.reference_id, "learner evidence reference ID")
        _sha256(self.sha256, "learner evidence reference digest")

    @property
    def sort_key(self) -> tuple[bytes, bytes, bytes, bytes]:
        return (
            self.kind.value.encode("utf-8"),
            (
                b""
                if self.evidence_family is None
                else self.evidence_family.value.encode("utf-8")
            ),
            self.reference_id.encode("utf-8"),
            bytes.fromhex(self.sha256),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_family": (
                None if self.evidence_family is None else self.evidence_family.value
            ),
            "kind": self.kind.value,
            "reference_id": self.reference_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SupportingEvidenceReferenceV1:
        if not isinstance(payload, dict) or set(payload) != {
            "evidence_family",
            "kind",
            "reference_id",
            "sha256",
        }:
            raise ValueError("supporting evidence reference fields differ")
        family = payload["evidence_family"]
        if family is not None and type(family) is not str:
            raise TypeError("supporting evidence family must be text or null")
        return cls(
            EvidenceReferenceKindV1(
                _exact_text(payload["kind"], "supporting evidence kind")
            ),
            _exact_text(payload["reference_id"], "supporting evidence reference ID"),
            _exact_text(payload["sha256"], "supporting evidence digest"),
            None if family is None else EvidenceFamilyV1(family),
        )


def _references(
    values: tuple[SupportingEvidenceReferenceV1, ...],
    label: str,
) -> tuple[SupportingEvidenceReferenceV1, ...]:
    if type(values) is not tuple or not values or any(
        not isinstance(item, SupportingEvidenceReferenceV1) for item in values
    ):
        raise ValueError(f"{label} requires typed supporting references")
    if values != tuple(sorted(values, key=lambda item: item.sort_key)):
        raise ValueError(f"{label} references are not canonically ordered")
    identities = tuple(
        (item.kind, item.evidence_family, item.reference_id, item.sha256)
        for item in values
    )
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} references are duplicated")
    return values


@dataclass(frozen=True, slots=True)
class ScoringPolicyDefinitionV1:
    policy_id: str
    version: int
    score_source: str
    permitted_reference_kinds: tuple[EvidenceReferenceKindV1, ...]

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "scoring policy ID")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("WO34-A scoring policies must use version one")
        _identifier(self.score_source, "scoring policy source")
        if (
            type(self.permitted_reference_kinds) is not tuple
            or not self.permitted_reference_kinds
            or any(
                not isinstance(item, EvidenceReferenceKindV1)
                for item in self.permitted_reference_kinds
            )
            or self.permitted_reference_kinds
            != tuple(sorted(set(self.permitted_reference_kinds), key=lambda item: item.value))
        ):
            raise ValueError("scoring policy evidence kinds are invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "permitted_reference_kinds": [
                item.value for item in self.permitted_reference_kinds
            ],
            "pnl_projection_weight": 0,
            "policy_id": self.policy_id,
            "score_max_ppm": POLICY_SCALE_V1,
            "score_min_ppm": 0,
            "score_source": self.score_source,
            "schema_version": LEARNER_EVIDENCE_SCHEMA_VERSION_V1,
            "version": self.version,
        }

    @property
    def policy_digest(self) -> str:
        return sha256_json(self.as_dict())


_ALL_REFERENCE_KINDS = tuple(
    sorted(EvidenceReferenceKindV1, key=lambda item: item.value)
)
SCORING_POLICY_DEFINITIONS_V1 = (
    ScoringPolicyDefinitionV1(
        "LEGACY_OBJECTIVE_SCORING_V1",
        1,
        "IMMUTABLE_LESSON_OBJECTIVE_POLICY",
        _ALL_REFERENCE_KINDS,
    ),
    ScoringPolicyDefinitionV1(
        "OBSERVE_CLASSIFY_SCORING_V1",
        1,
        "IMMUTABLE_OBSERVE_CLASSIFY_POLICY",
        _ALL_REFERENCE_KINDS,
    ),
)
SCORING_POLICY_REGISTRY_V1 = MappingProxyType(
    {item.policy_id: item for item in SCORING_POLICY_DEFINITIONS_V1}
)


def require_scoring_policy_v1(
    policy_id: str,
    policy_digest: str,
) -> ScoringPolicyDefinitionV1:
    definition = SCORING_POLICY_REGISTRY_V1.get(policy_id)
    if definition is None:
        raise ValueError("unknown learner-evidence scoring policy")
    if policy_digest != definition.policy_digest:
        raise ValueError("learner-evidence scoring policy digest differs")
    return definition


class AttemptModeV1(str, Enum):
    GUIDED = "GUIDED"
    PRACTICE = "PRACTICE"
    ASSESSMENT = "ASSESSMENT"
    REMEDIATION = "REMEDIATION"


class OpportunityStateV1(str, Enum):
    GREEN = "GREEN"
    WAIT = "WAIT"
    RED = "RED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class AttemptOpportunityV1:
    opportunity_id: str
    opportunity_present: bool
    observable: bool
    reaction_time_sufficient: bool
    reference_state: OpportunityStateV1
    activation_us: int | None
    reaction_deadline_us: int | None
    supporting_evidence_references: tuple[SupportingEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        _text(self.opportunity_id, "attempt opportunity ID")
        if type(self.opportunity_present) is not bool or type(self.observable) is not bool:
            raise TypeError("attempt opportunity flags must be booleans")
        if type(self.reaction_time_sufficient) is not bool:
            raise TypeError("reaction-time sufficiency must be a boolean")
        if not isinstance(self.reference_state, OpportunityStateV1):
            raise TypeError("attempt opportunity reference state is invalid")
        if self.opportunity_present:
            if self.reference_state is OpportunityStateV1.NOT_APPLICABLE:
                raise ValueError("present opportunity requires an applicable state")
            if type(self.activation_us) is not int or self.activation_us < 0:
                raise ValueError("present opportunity requires an activation time")
        elif (
            self.reference_state is not OpportunityStateV1.NOT_APPLICABLE
            or self.activation_us is not None
            or self.reaction_deadline_us is not None
        ):
            raise ValueError("absent opportunity requires an inapplicable timeless state")
        if self.reaction_time_sufficient:
            if (
                not self.opportunity_present
                or not self.observable
                or type(self.reaction_deadline_us) is not int
                or self.reaction_deadline_us <= int(self.activation_us)
            ):
                raise ValueError("reaction sufficiency lacks observable reaction time")
        elif self.reaction_deadline_us is not None:
            if (
                type(self.reaction_deadline_us) is not int
                or self.reaction_deadline_us < 0
            ):
                raise ValueError("reaction deadline must be nonnegative or null")
        _references(
            self.supporting_evidence_references,
            "attempt opportunity",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "activation_us": self.activation_us,
            "observable": self.observable,
            "opportunity_id": self.opportunity_id,
            "opportunity_present": self.opportunity_present,
            "reaction_deadline_us": self.reaction_deadline_us,
            "reaction_time_sufficient": self.reaction_time_sufficient,
            "reference_state": self.reference_state.value,
            "supporting_evidence_references": [
                item.as_dict() for item in self.supporting_evidence_references
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> AttemptOpportunityV1:
        expected = {
            "activation_us",
            "observable",
            "opportunity_id",
            "opportunity_present",
            "reaction_deadline_us",
            "reaction_time_sufficient",
            "reference_state",
            "supporting_evidence_references",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("attempt opportunity fields differ")
        raw_refs = payload["supporting_evidence_references"]
        if not isinstance(raw_refs, list):
            raise TypeError("attempt opportunity references are invalid")
        activation = payload["activation_us"]
        deadline = payload["reaction_deadline_us"]
        return cls(
            opportunity_id=_exact_text(
                payload["opportunity_id"],
                "attempt opportunity ID",
            ),
            opportunity_present=payload["opportunity_present"],  # type: ignore[arg-type]
            observable=payload["observable"],  # type: ignore[arg-type]
            reaction_time_sufficient=payload["reaction_time_sufficient"],  # type: ignore[arg-type]
            reference_state=OpportunityStateV1(
                _exact_text(payload["reference_state"], "opportunity reference state")
            ),
            activation_us=_optional_exact_int(activation, "opportunity activation time"),
            reaction_deadline_us=_optional_exact_int(
                deadline,
                "opportunity reaction deadline",
            ),
            supporting_evidence_references=tuple(
                SupportingEvidenceReferenceV1.from_dict(item) for item in raw_refs
            ),
        )


class AttemptActionKindV1(str, Enum):
    NO_ACTION = "NO_ACTION"
    CLASSIFICATION = "CLASSIFICATION"
    ORDER_SUBMISSION = "ORDER_SUBMISSION"
    CANCELLATION = "CANCELLATION"
    REPLACEMENT = "REPLACEMENT"
    ROUTING_DECISION = "ROUTING_DECISION"
    POSITION_EXIT = "POSITION_EXIT"
    MULTI_ACTION = "MULTI_ACTION"


@dataclass(frozen=True, slots=True)
class AttemptActionV1:
    action_id: str
    action_kind: AttemptActionKindV1
    occurred_us: int | None
    action_sha256: str | None
    supporting_evidence_references: tuple[SupportingEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        _text(self.action_id, "attempt action ID")
        if not isinstance(self.action_kind, AttemptActionKindV1):
            raise TypeError("attempt action kind is invalid")
        if self.action_kind is AttemptActionKindV1.NO_ACTION:
            if self.occurred_us is not None or self.action_sha256 is not None:
                raise ValueError("NO_ACTION cannot carry action time or bytes")
        else:
            if type(self.occurred_us) is not int or self.occurred_us < 0:
                raise ValueError("recorded action requires a nonnegative time")
            _sha256(self.action_sha256, "attempt action digest")
        _references(self.supporting_evidence_references, "attempt action")

    def as_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "action_kind": self.action_kind.value,
            "action_sha256": self.action_sha256,
            "occurred_us": self.occurred_us,
            "supporting_evidence_references": [
                item.as_dict() for item in self.supporting_evidence_references
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> AttemptActionV1:
        if not isinstance(payload, dict) or set(payload) != {
            "action_id",
            "action_kind",
            "action_sha256",
            "occurred_us",
            "supporting_evidence_references",
        }:
            raise ValueError("attempt action fields differ")
        raw_refs = payload["supporting_evidence_references"]
        if not isinstance(raw_refs, list):
            raise TypeError("attempt action references are invalid")
        occurred = payload["occurred_us"]
        digest = payload["action_sha256"]
        return cls(
            action_id=_exact_text(payload["action_id"], "attempt action ID"),
            action_kind=AttemptActionKindV1(
                _exact_text(payload["action_kind"], "attempt action kind")
            ),
            occurred_us=_optional_exact_int(occurred, "attempt action time"),
            action_sha256=_optional_exact_text(digest, "attempt action digest"),
            supporting_evidence_references=tuple(
                SupportingEvidenceReferenceV1.from_dict(item) for item in raw_refs
            ),
        )


class EvidenceSourceClassV1(str, Enum):
    SYNTHETIC = "SYNTHETIC"
    HISTORICAL_OR_RECONSTRUCTION = "HISTORICAL_OR_RECONSTRUCTION"


@dataclass(frozen=True, slots=True)
class ObservableAttemptContextV1:
    context_id: str
    scenario_semantic_sha256: str
    volume_multiplier_ppm: int
    liquidity_multiplier_ppm: int
    source_class: EvidenceSourceClassV1
    simulation_time_us: int
    supporting_evidence_references: tuple[SupportingEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        _text(self.context_id, "observable attempt context ID")
        _sha256(self.scenario_semantic_sha256, "scenario semantic digest")
        for value, label in (
            (self.volume_multiplier_ppm, "volume multiplier"),
            (self.liquidity_multiplier_ppm, "liquidity multiplier"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{label} must be a positive integer ppm")
        if not isinstance(self.source_class, EvidenceSourceClassV1):
            raise TypeError("attempt evidence source class is invalid")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("attempt simulation time must be nonnegative")
        _references(self.supporting_evidence_references, "observable attempt context")

    def as_dict(self) -> dict[str, object]:
        return {
            "context_id": self.context_id,
            "liquidity_multiplier_ppm": self.liquidity_multiplier_ppm,
            "scenario_semantic_sha256": self.scenario_semantic_sha256,
            "simulation_time_us": self.simulation_time_us,
            "source_class": self.source_class.value,
            "supporting_evidence_references": [
                item.as_dict() for item in self.supporting_evidence_references
            ],
            "volume_multiplier_ppm": self.volume_multiplier_ppm,
        }

    @classmethod
    def from_dict(cls, payload: object) -> ObservableAttemptContextV1:
        expected = {
            "context_id",
            "liquidity_multiplier_ppm",
            "scenario_semantic_sha256",
            "simulation_time_us",
            "source_class",
            "supporting_evidence_references",
            "volume_multiplier_ppm",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("observable attempt context fields differ")
        raw_refs = payload["supporting_evidence_references"]
        if not isinstance(raw_refs, list):
            raise TypeError("observable context references are invalid")
        return cls(
            context_id=_exact_text(payload["context_id"], "attempt context ID"),
            scenario_semantic_sha256=_exact_text(
                payload["scenario_semantic_sha256"],
                "scenario semantic digest",
            ),
            volume_multiplier_ppm=_exact_int(
                payload["volume_multiplier_ppm"],
                "volume multiplier",
            ),
            liquidity_multiplier_ppm=_exact_int(
                payload["liquidity_multiplier_ppm"],
                "liquidity multiplier",
            ),
            source_class=EvidenceSourceClassV1(
                _exact_text(payload["source_class"], "evidence source class")
            ),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"],
                "attempt simulation time",
            ),
            supporting_evidence_references=tuple(
                SupportingEvidenceReferenceV1.from_dict(item) for item in raw_refs
            ),
        )


@dataclass(frozen=True, slots=True)
class SkillEvidenceV1:
    skill_id: str
    opportunity_present: bool
    observable: bool
    score_ppm: int
    scoring_policy_id: str
    scoring_policy_digest: str
    supporting_evidence_references: tuple[SupportingEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        require_stable_skill_v1(self.skill_id)
        if type(self.opportunity_present) is not bool or type(self.observable) is not bool:
            raise TypeError("skill-evidence opportunity flags must be booleans")
        if type(self.score_ppm) is not int or not 0 <= self.score_ppm <= POLICY_SCALE_V1:
            raise ValueError("skill-evidence score must be an integer in [0,S]")
        definition = require_scoring_policy_v1(
            self.scoring_policy_id,
            self.scoring_policy_digest,
        )
        references = _references(
            self.supporting_evidence_references,
            "skill evidence",
        )
        allowed = set(definition.permitted_reference_kinds)
        if any(item.kind not in allowed for item in references):
            raise ValueError("skill evidence uses a policy-forbidden reference kind")
        if not any(
            item.kind is EvidenceReferenceKindV1.SCORING_INPUT
            for item in references
        ):
            raise ValueError("skill evidence lacks an immutable scoring input")
        if any(
            item.kind is EvidenceReferenceKindV1.SCORING_INPUT
            and item.evidence_family is None
            for item in references
        ):
            raise ValueError("skill scoring input lacks an exact evidence family")

    @property
    def evidence_id(self) -> str:
        return "skill-evidence-" + sha256_json(self.as_dict())

    @property
    def projection_weight_eligible(self) -> bool:
        """Whether WO34-B may consider this row before its other weight rules."""

        return self.opportunity_present and self.observable

    def as_dict(self) -> dict[str, object]:
        return {
            "observable": self.observable,
            "opportunity_present": self.opportunity_present,
            "score_ppm": self.score_ppm,
            "scoring_policy_digest": self.scoring_policy_digest,
            "scoring_policy_id": self.scoring_policy_id,
            "skill_id": self.skill_id,
            "supporting_evidence_references": [
                item.as_dict() for item in self.supporting_evidence_references
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> SkillEvidenceV1:
        expected = {
            "observable",
            "opportunity_present",
            "score_ppm",
            "scoring_policy_digest",
            "scoring_policy_id",
            "skill_id",
            "supporting_evidence_references",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("skill evidence fields differ")
        raw_refs = payload["supporting_evidence_references"]
        if not isinstance(raw_refs, list):
            raise TypeError("skill evidence references are invalid")
        return cls(
            skill_id=_exact_text(payload["skill_id"], "skill-evidence skill ID"),
            opportunity_present=payload["opportunity_present"],  # type: ignore[arg-type]
            observable=payload["observable"],  # type: ignore[arg-type]
            score_ppm=_exact_int(payload["score_ppm"], "skill-evidence score"),
            scoring_policy_id=_exact_text(
                payload["scoring_policy_id"],
                "skill-evidence scoring policy ID",
            ),
            scoring_policy_digest=_exact_text(
                payload["scoring_policy_digest"],
                "skill-evidence scoring policy digest",
            ),
            supporting_evidence_references=tuple(
                SupportingEvidenceReferenceV1.from_dict(item) for item in raw_refs
            ),
        )


@dataclass(frozen=True, slots=True)
class AttemptErrorRecordV1:
    error_type: LearnerErrorTypeV1
    mapped_skill_id: str | None
    supporting_evidence_references: tuple[SupportingEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.error_type, LearnerErrorTypeV1):
            raise TypeError("attempt error type is invalid")
        if self.mapped_skill_id is not None:
            require_stable_skill_v1(self.mapped_skill_id)
        _references(self.supporting_evidence_references, "attempt error")

    def as_dict(self) -> dict[str, object]:
        return {
            "error_type": self.error_type.value,
            "mapped_skill_id": self.mapped_skill_id,
            "supporting_evidence_references": [
                item.as_dict() for item in self.supporting_evidence_references
            ],
        }

    @classmethod
    def from_dict(cls, payload: object) -> AttemptErrorRecordV1:
        if not isinstance(payload, dict) or set(payload) != {
            "error_type",
            "mapped_skill_id",
            "supporting_evidence_references",
        }:
            raise ValueError("attempt error fields differ")
        raw_refs = payload["supporting_evidence_references"]
        if not isinstance(raw_refs, list):
            raise TypeError("attempt error references are invalid")
        mapped = payload["mapped_skill_id"]
        return cls(
            error_type=LearnerErrorTypeV1(
                _exact_text(payload["error_type"], "attempt error type")
            ),
            mapped_skill_id=_optional_exact_text(mapped, "mapped error skill ID"),
            supporting_evidence_references=tuple(
                SupportingEvidenceReferenceV1.from_dict(item) for item in raw_refs
            ),
        )


class AttemptAmbiguityV1(str, Enum):
    NONE = "NONE"
    UNSCORABLE = "UNSCORABLE"
    AMBIGUOUS = "AMBIGUOUS"
    INSUFFICIENT_OBSERVABILITY = "INSUFFICIENT_OBSERVABILITY"


class AttemptEvidenceSufficiencyV1(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class AuxiliaryOutcomeV1:
    outcome_type: str
    value: int
    unit: str
    supporting_evidence_references: tuple[SupportingEvidenceReferenceV1, ...]

    def __post_init__(self) -> None:
        if self.outcome_type != "PNL":
            raise ValueError("WO34-A auxiliary outcomes only admit labeled PNL")
        if type(self.value) is not int:
            raise TypeError("auxiliary PNL value must be an exact integer")
        _identifier(self.unit, "auxiliary PNL unit")
        references = _references(
            self.supporting_evidence_references,
            "auxiliary outcome",
        )
        if any(
            item.kind is not EvidenceReferenceKindV1.SOURCE_RECORD
            for item in references
        ):
            raise ValueError("auxiliary PNL must remain a source-record outcome")
        if any(item.evidence_family is not None for item in references):
            raise ValueError("auxiliary PNL cannot claim a mastery-evidence family")

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome_type": self.outcome_type,
            "projection_weight": 0,
            "supporting_evidence_references": [
                item.as_dict() for item in self.supporting_evidence_references
            ],
            "unit": self.unit,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> AuxiliaryOutcomeV1:
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "outcome_type",
                "projection_weight",
                "supporting_evidence_references",
                "unit",
                "value",
            }
            or payload["projection_weight"] != 0
        ):
            raise ValueError("auxiliary outcome fields differ")
        raw_refs = payload["supporting_evidence_references"]
        if not isinstance(raw_refs, list):
            raise TypeError("auxiliary outcome references are invalid")
        return cls(
            outcome_type=_exact_text(payload["outcome_type"], "auxiliary outcome type"),
            value=_exact_int(payload["value"], "auxiliary outcome value"),
            unit=_exact_text(payload["unit"], "auxiliary outcome unit"),
            supporting_evidence_references=tuple(
                SupportingEvidenceReferenceV1.from_dict(item) for item in raw_refs
            ),
        )


@dataclass(frozen=True, slots=True)
class AttemptAssessmentV1:
    learner_id: str
    attempt_ordinal: int
    lesson_reference_id: str
    lesson_digest: str
    primary_skill_id: str
    supporting_skill_ids: tuple[str, ...]
    mode: AttemptModeV1
    opportunity: AttemptOpportunityV1
    action: AttemptActionV1
    observable_context: ObservableAttemptContextV1
    scoring_policy_id: str
    scoring_policy_digest: str
    skill_evidence: tuple[SkillEvidenceV1, ...]
    errors: tuple[AttemptErrorRecordV1, ...]
    ambiguity: AttemptAmbiguityV1
    evidence_sufficiency: AttemptEvidenceSufficiencyV1
    auxiliary_outcomes: tuple[AuxiliaryOutcomeV1, ...]
    study_timestamp_utc: str
    schema_version: int = LEARNER_EVIDENCE_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _text(self.learner_id, "learner ID")
        if type(self.attempt_ordinal) is not int or self.attempt_ordinal <= 0:
            raise ValueError("attempt ordinal must be a positive integer")
        _text(self.lesson_reference_id, "lesson reference ID")
        _sha256(self.lesson_digest, "lesson digest")
        require_stable_skill_v1(self.primary_skill_id)
        if (
            type(self.supporting_skill_ids) is not tuple
            or any(
                type(item) is not str for item in self.supporting_skill_ids
            )
            or self.supporting_skill_ids
            != tuple(sorted(set(self.supporting_skill_ids)))
            or self.primary_skill_id in self.supporting_skill_ids
        ):
            raise ValueError("attempt supporting skill IDs are invalid")
        for skill_id in self.supporting_skill_ids:
            require_stable_skill_v1(skill_id)
        if not isinstance(self.mode, AttemptModeV1):
            raise TypeError("attempt mode is invalid")
        if not isinstance(self.opportunity, AttemptOpportunityV1):
            raise TypeError("attempt opportunity is invalid")
        if not isinstance(self.action, AttemptActionV1):
            raise TypeError("attempt action is invalid")
        if not isinstance(self.observable_context, ObservableAttemptContextV1):
            raise TypeError("attempt observable context is invalid")
        require_scoring_policy_v1(
            self.scoring_policy_id,
            self.scoring_policy_digest,
        )
        if type(self.skill_evidence) is not tuple or any(
            not isinstance(item, SkillEvidenceV1) for item in self.skill_evidence
        ):
            raise TypeError("attempt skill evidence is invalid")
        if self.skill_evidence != tuple(
            sorted(self.skill_evidence, key=lambda item: item.skill_id.encode("utf-8"))
        ):
            raise ValueError("attempt skill evidence is not canonically ordered")
        expected_skills = {self.primary_skill_id, *self.supporting_skill_ids}
        actual_skills = {item.skill_id for item in self.skill_evidence}
        if actual_skills != expected_skills or len(actual_skills) != len(
            self.skill_evidence
        ):
            raise ValueError("attempt must contain one row for every declared skill")
        if any(
            item.scoring_policy_id != self.scoring_policy_id
            or item.scoring_policy_digest != self.scoring_policy_digest
            or item.opportunity_present != self.opportunity.opportunity_present
            or item.observable != self.opportunity.observable
            for item in self.skill_evidence
        ):
            raise ValueError("attempt and skill-evidence policy/opportunity differ")
        if type(self.errors) is not tuple or any(
            not isinstance(item, AttemptErrorRecordV1) for item in self.errors
        ):
            raise TypeError("attempt error records are invalid")
        if self.errors != tuple(
            sorted(self.errors, key=lambda item: item.error_type.value.encode("utf-8"))
        ) or len({item.error_type for item in self.errors}) != len(self.errors):
            raise ValueError("attempt errors must be unique and canonically ordered")
        for error in self.errors:
            expected_mapping = mapped_skill_for_error_v1(
                error.error_type,
                self.primary_skill_id,
            )
            if error.mapped_skill_id != expected_mapping:
                raise ValueError("attempt error skill mapping differs")
            if expected_mapping is not None and expected_mapping not in actual_skills:
                raise ValueError("attempt error lacks its mapped skill-evidence row")
        self._validate_action_errors()
        if not isinstance(self.ambiguity, AttemptAmbiguityV1) or not isinstance(
            self.evidence_sufficiency,
            AttemptEvidenceSufficiencyV1,
        ):
            raise TypeError("attempt ambiguity or sufficiency is invalid")
        ambiguity_errors = {
            item.error_type for item in self.errors if item.error_type in AMBIGUITY_ERROR_TYPES_V1
        }
        expected_ambiguity = (
            set()
            if self.ambiguity is AttemptAmbiguityV1.NONE
            else {LearnerErrorTypeV1(self.ambiguity.value)}
        )
        if ambiguity_errors != expected_ambiguity:
            raise ValueError("attempt ambiguity differs from its typed error record")
        if (
            self.ambiguity is AttemptAmbiguityV1.NONE
        ) != (
            self.evidence_sufficiency is AttemptEvidenceSufficiencyV1.SUFFICIENT
        ):
            raise ValueError("attempt ambiguity and evidence sufficiency disagree")
        if (
            self.evidence_sufficiency is AttemptEvidenceSufficiencyV1.SUFFICIENT
            and (not self.opportunity.opportunity_present or not self.opportunity.observable)
        ):
            raise ValueError("sufficient assessment lacks observable opportunity evidence")
        if type(self.auxiliary_outcomes) is not tuple or any(
            not isinstance(item, AuxiliaryOutcomeV1)
            for item in self.auxiliary_outcomes
        ) or len({item.outcome_type for item in self.auxiliary_outcomes}) != len(
            self.auxiliary_outcomes
        ):
            raise ValueError("attempt auxiliary outcomes are invalid")
        skill_reference_keys = {
            (reference.kind, reference.reference_id, reference.sha256)
            for evidence in self.skill_evidence
            for reference in evidence.supporting_evidence_references
        }
        auxiliary_reference_keys = {
            (reference.kind, reference.reference_id, reference.sha256)
            for outcome in self.auxiliary_outcomes
            for reference in outcome.supporting_evidence_references
        }
        if skill_reference_keys & auxiliary_reference_keys:
            raise ValueError("auxiliary PNL evidence cannot support a skill score")
        _utc(self.study_timestamp_utc, "attempt study timestamp")
        if (
            type(self.schema_version) is not int
            or self.schema_version != LEARNER_EVIDENCE_SCHEMA_VERSION_V1
        ):
            raise ValueError("attempt evidence schema version is unsupported")

    def _validate_action_errors(self) -> None:
        error_types = {item.error_type for item in self.errors}
        no_action = self.action.action_kind is AttemptActionKindV1.NO_ACTION
        if LearnerErrorTypeV1.FAILED_TO_ACT_DURING_GREEN in error_types and (
            not no_action
            or not self.opportunity.opportunity_present
            or not self.opportunity.observable
            or not self.opportunity.reaction_time_sufficient
            or self.opportunity.reference_state is not OpportunityStateV1.GREEN
        ):
            raise ValueError(
                "failure to act requires a proven green opportunity and reaction time"
            )
        if LearnerErrorTypeV1.ACTED_DURING_RED in error_types and (
            no_action or self.opportunity.reference_state is not OpportunityStateV1.RED
        ):
            raise ValueError("acted-during-red error lacks a recorded red-state action")
        if LearnerErrorTypeV1.CHASED_AFTER_INVALIDATION in error_types and (
            no_action or self.opportunity.reference_state is not OpportunityStateV1.WAIT
        ):
            raise ValueError("invalidation chase lacks a recorded wait-state action")
        if LearnerErrorTypeV1.WRONG_HOTKEY in error_types and no_action:
            raise ValueError("wrong-hotkey error requires a recorded action")

    @property
    def assessment_id(self) -> str:
        return "attempt-assessment-" + sha256_json(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.as_dict(),
            "ambiguity": self.ambiguity.value,
            "attempt_ordinal": self.attempt_ordinal,
            "auxiliary_outcomes": [item.as_dict() for item in self.auxiliary_outcomes],
            "errors": [item.as_dict() for item in self.errors],
            "evidence_sufficiency": self.evidence_sufficiency.value,
            "learner_id": self.learner_id,
            "lesson_digest": self.lesson_digest,
            "lesson_reference_id": self.lesson_reference_id,
            "mode": self.mode.value,
            "observable_context": self.observable_context.as_dict(),
            "opportunity": self.opportunity.as_dict(),
            "primary_skill_id": self.primary_skill_id,
            "projection_state": "NOT_COMPUTED_IN_WO34_A",
            "record_kind": "ATTEMPT_ASSESSMENT_V1",
            "schema_version": self.schema_version,
            "scoring_policy_digest": self.scoring_policy_digest,
            "scoring_policy_id": self.scoring_policy_id,
            "skill_evidence": [item.as_dict() for item in self.skill_evidence],
            "study_timestamp_utc": self.study_timestamp_utc,
            "supporting_skill_ids": list(self.supporting_skill_ids),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def projection_weight_eligible_skill_evidence(
        self,
    ) -> tuple[SkillEvidenceV1, ...]:
        """Rows WO34-B may consider before its mode, error-cap, and decay rules."""

        if (
            self.ambiguity is not AttemptAmbiguityV1.NONE
            or self.evidence_sufficiency is not AttemptEvidenceSufficiencyV1.SUFFICIENT
        ):
            return ()
        return tuple(
            item for item in self.skill_evidence if item.projection_weight_eligible
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> AttemptAssessmentV1:
        assessment = cls.from_dict(_canonical_object(raw, "attempt assessment"))
        if assessment.canonical_bytes() != raw:
            raise ValueError("attempt assessment changed during restoration")
        return assessment

    @classmethod
    def from_dict(cls, payload: object) -> AttemptAssessmentV1:
        expected = {
            "action",
            "ambiguity",
            "attempt_ordinal",
            "auxiliary_outcomes",
            "errors",
            "evidence_sufficiency",
            "learner_id",
            "lesson_digest",
            "lesson_reference_id",
            "mode",
            "observable_context",
            "opportunity",
            "primary_skill_id",
            "projection_state",
            "record_kind",
            "schema_version",
            "scoring_policy_digest",
            "scoring_policy_id",
            "skill_evidence",
            "study_timestamp_utc",
            "supporting_skill_ids",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload["record_kind"] != "ATTEMPT_ASSESSMENT_V1"
            or payload["projection_state"] != "NOT_COMPUTED_IN_WO34_A"
        ):
            raise ValueError("attempt assessment fields differ")
        raw_supporting = payload["supporting_skill_ids"]
        raw_skill_evidence = payload["skill_evidence"]
        raw_errors = payload["errors"]
        raw_auxiliary = payload["auxiliary_outcomes"]
        if any(
            not isinstance(item, list)
            for item in (
                raw_supporting,
                raw_skill_evidence,
                raw_errors,
                raw_auxiliary,
            )
        ):
            raise TypeError("attempt assessment arrays are invalid")
        return cls(
            learner_id=_exact_text(payload["learner_id"], "learner ID"),
            attempt_ordinal=_exact_int(payload["attempt_ordinal"], "attempt ordinal"),
            lesson_reference_id=_exact_text(
                payload["lesson_reference_id"],
                "lesson reference ID",
            ),
            lesson_digest=_exact_text(payload["lesson_digest"], "lesson digest"),
            primary_skill_id=_exact_text(
                payload["primary_skill_id"],
                "primary skill ID",
            ),
            supporting_skill_ids=tuple(
                _exact_text(item, "supporting skill ID") for item in raw_supporting
            ),
            mode=AttemptModeV1(_exact_text(payload["mode"], "attempt mode")),
            opportunity=AttemptOpportunityV1.from_dict(payload["opportunity"]),
            action=AttemptActionV1.from_dict(payload["action"]),
            observable_context=ObservableAttemptContextV1.from_dict(
                payload["observable_context"]
            ),
            scoring_policy_id=_exact_text(
                payload["scoring_policy_id"],
                "assessment scoring policy ID",
            ),
            scoring_policy_digest=_exact_text(
                payload["scoring_policy_digest"],
                "assessment scoring policy digest",
            ),
            skill_evidence=tuple(
                SkillEvidenceV1.from_dict(item) for item in raw_skill_evidence
            ),
            errors=tuple(AttemptErrorRecordV1.from_dict(item) for item in raw_errors),
            ambiguity=AttemptAmbiguityV1(
                _exact_text(payload["ambiguity"], "attempt ambiguity")
            ),
            evidence_sufficiency=AttemptEvidenceSufficiencyV1(
                _exact_text(
                    payload["evidence_sufficiency"],
                    "attempt evidence sufficiency",
                )
            ),
            auxiliary_outcomes=tuple(
                AuxiliaryOutcomeV1.from_dict(item) for item in raw_auxiliary
            ),
            study_timestamp_utc=_exact_text(
                payload["study_timestamp_utc"],
                "attempt study timestamp",
            ),
            schema_version=_exact_int(
                payload["schema_version"],
                "attempt schema version",
            ),
        )


@dataclass(frozen=True, slots=True)
class LearnerEvidenceLedgerV1:
    learner_id: str
    assessments: tuple[AttemptAssessmentV1, ...]
    ledger_id: str = LEARNER_EVIDENCE_LEDGER_ID_V1
    schema_version: int = LEARNER_EVIDENCE_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _text(self.learner_id, "evidence-ledger learner ID")
        if self.ledger_id != LEARNER_EVIDENCE_LEDGER_ID_V1:
            raise ValueError("learner evidence ledger ID differs")
        if (
            type(self.schema_version) is not int
            or self.schema_version != LEARNER_EVIDENCE_SCHEMA_VERSION_V1
        ):
            raise ValueError("learner evidence ledger schema version differs")
        if type(self.assessments) is not tuple or any(
            not isinstance(item, AttemptAssessmentV1) for item in self.assessments
        ):
            raise TypeError("learner evidence ledger assessments are invalid")
        if any(item.learner_id != self.learner_id for item in self.assessments):
            raise ValueError("learner evidence ledger mixes learner identities")
        ordinals = tuple(item.attempt_ordinal for item in self.assessments)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValueError("learner evidence ledger ordinals must strictly increase")
        ids = tuple(item.assessment_id for item in self.assessments)
        if len(ids) != len(set(ids)):
            raise ValueError("learner evidence ledger assessment IDs are duplicated")

    @property
    def ledger_sha256(self) -> str:
        return sha256_json(self.as_dict())

    def append(self, assessment: AttemptAssessmentV1) -> LearnerEvidenceLedgerV1:
        if not isinstance(assessment, AttemptAssessmentV1):
            raise TypeError("learner evidence append requires an assessment")
        return LearnerEvidenceLedgerV1(
            self.learner_id,
            (*self.assessments, assessment),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "assessments": [item.as_dict() for item in self.assessments],
            "learner_id": self.learner_id,
            "ledger_id": self.ledger_id,
            "projection_records": [],
            "record_kind": "IMMUTABLE_LEARNER_EVIDENCE_LEDGER_V1",
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> LearnerEvidenceLedgerV1:
        ledger = cls.from_dict(_canonical_object(raw, "learner evidence ledger"))
        if ledger.canonical_bytes() != raw:
            raise ValueError("learner evidence ledger changed during restoration")
        return ledger

    @classmethod
    def from_dict(cls, payload: object) -> LearnerEvidenceLedgerV1:
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "assessments",
                "learner_id",
                "ledger_id",
                "projection_records",
                "record_kind",
                "schema_version",
            }
            or payload["record_kind"]
            != "IMMUTABLE_LEARNER_EVIDENCE_LEDGER_V1"
            or payload["projection_records"] != []
        ):
            raise ValueError("learner evidence ledger fields differ")
        raw_assessments = payload["assessments"]
        if not isinstance(raw_assessments, list):
            raise TypeError("learner evidence ledger rows are invalid")
        return cls(
            learner_id=_exact_text(payload["learner_id"], "ledger learner ID"),
            assessments=tuple(
                AttemptAssessmentV1.from_dict(item) for item in raw_assessments
            ),
            ledger_id=_exact_text(payload["ledger_id"], "learner evidence ledger ID"),
            schema_version=_exact_int(
                payload["schema_version"],
                "learner evidence ledger schema",
            ),
        )


__all__ = [
    "LEARNER_EVIDENCE_LEDGER_ID_V1",
    "LEARNER_EVIDENCE_SCHEMA_VERSION_V1",
    "SCORING_POLICY_DEFINITIONS_V1",
    "SCORING_POLICY_REGISTRY_V1",
    "AttemptActionKindV1",
    "AttemptActionV1",
    "AttemptAmbiguityV1",
    "AttemptAssessmentV1",
    "AttemptErrorRecordV1",
    "AttemptEvidenceSufficiencyV1",
    "AttemptModeV1",
    "AttemptOpportunityV1",
    "AuxiliaryOutcomeV1",
    "EvidenceFamilyV1",
    "EvidenceReferenceKindV1",
    "EvidenceSourceClassV1",
    "LearnerEvidenceLedgerV1",
    "ObservableAttemptContextV1",
    "OpportunityStateV1",
    "ScoringPolicyDefinitionV1",
    "SkillEvidenceV1",
    "SupportingEvidenceReferenceV1",
    "require_scoring_policy_v1",
]
