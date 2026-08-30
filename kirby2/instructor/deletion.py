"""Policy-bound, identity-only profile deletion orchestration.

This module composes the existing consent evaluators with the hardened identity
mapping deletion primitive.  Planning is pure.  Execution re-evaluates the exact
current consent and exact UTC deletion time before delegating the only destructive
operation to :func:`kirby2.instructor.identity.delete_identity_mapping`.

Retained evidence is represented only by closed-kind pseudonymous ID-and-digest
references.  No retained run or evidence payload is opened, rewritten, moved, or
deleted here.  Direct identity, filesystem paths, secrets, and payload bytes have
no field in the durable schemas below.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from enum import Enum

from kirby2.research.paths import DataPaths

from .consent import (
    ConsentDecisionReasonV1,
    ConsentRecordV1,
    ConsentScopeV1,
    ProfileDeletionDecisionV1,
    decide_profile_deletion,
    decide_retention_after_profile_deletion,
)
from .identity import IdentityDeletionReceiptV1, delete_identity_mapping
from .models import require_profile_id


RETAINED_EVIDENCE_REFERENCE_SCHEMA_ID = (
    "KIRBY2_RETAINED_EVIDENCE_REFERENCE_V1"
)
RETAINED_EVIDENCE_REFERENCE_SCHEMA_VERSION = 1
PROFILE_DELETION_PLAN_SCHEMA_ID = "KIRBY2_PROFILE_DELETION_PLAN_V1"
PROFILE_DELETION_PLAN_SCHEMA_VERSION = 1
PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_ID = (
    "KIRBY2_PROFILE_DELETION_RECEIPT_SIDECAR_V1"
)
PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_VERSION = 1
PROFILE_DELETION_RESULT_SCHEMA_ID = "KIRBY2_PROFILE_DELETION_RESULT_V1"
PROFILE_DELETION_RESULT_SCHEMA_VERSION = 1

DIRECT_IDENTITY_ACTION_V1 = "DELETE_SEPARATELY_ERASABLE_IDENTITY_MAPPING_ONLY"
EVIDENCE_BYTES_ACTION_V1 = "NEVER_MUTATE_RETAINED_RUN_OR_EVIDENCE_BYTES"
RETAINED_REFERENCE_POLICY_V1 = (
    "EXACT_PSEUDONYMOUS_ID_DIGEST_ONLY_NO_DIRECT_IDENTITY_LOCAL_PATH_SECRET_OR_PAYLOAD"
)
NO_RETENTION_DISPOSITION_V1 = (
    "NO_RETENTION_REQUESTED_EVIDENCE_BYTES_OUTSIDE_IDENTITY_ONLY_WORKFLOW"
)
AUTHORIZED_RETENTION_DISPOSITION_V1 = "AUTHORIZED_RETAINED_UNCHANGED"
DELETION_EXECUTION_STATUS_V1 = "COMPLETED"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_CONSENT_ID = re.compile(r"consent-[0-9a-f]{24}\Z")
_DECISION_ID = re.compile(r"consent-decision-[0-9a-f]{24}\Z")
_RECEIPT_ID = re.compile(r"identity-deletion-receipt-[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_MAX_CANONICAL_BYTES = 16 * 1024 * 1024

_PLAN_TOKEN = object()
_SIDECAR_TOKEN = object()
_RESULT_TOKEN = object()


class RetainedEvidenceKindV1(str, Enum):
    """Closed immutable planes eligible for post-profile-deletion references."""

    RUN = "RUN"
    EVIDENCE = "EVIDENCE"


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
        raise ValueError("profile deletion record is not strict canonical JSON") from error


def _pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("profile deletion JSON contains a duplicate object key")
        result[key] = value
    return result


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} requires exact bytes")
    if not raw or len(raw) > _MAX_CANONICAL_BYTES:
        raise ValueError(f"{label} byte length is invalid")
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


def _fields(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact object")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return value


def _json_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact JSON array")
    return value


def _text(value: object, label: str, *, maximum_utf8_bytes: int = 1024) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be nonempty text without edge whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use canonical NFC text")
    if len(value.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{label} exceeds its bounded size")
    if any(character == "\x00" or character in "\r\n" for character in value):
        raise ValueError(f"{label} contains a forbidden control character")
    return value


def _semantic_id(value: object, label: str) -> str:
    result = _text(value, label, maximum_utf8_bytes=256)
    if _SEMANTIC_ID.fullmatch(result) is None or "\\" in result or "/" in result:
        raise ValueError(f"{label} must be a non-path semantic identifier")
    return result


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _pattern_id(
    value: object,
    pattern: re.Pattern[str],
    label: str,
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _utc(value: object, label: str) -> str:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise ValueError(f"{label} must use canonical UTC seconds ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a real UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must identify UTC")
    return value


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


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


def _validated_current_consent(current_consent: ConsentRecordV1) -> ConsentRecordV1:
    if type(current_consent) is not ConsentRecordV1:
        raise TypeError("profile deletion requires current ConsentRecordV1")
    rebuilt = ConsentRecordV1.from_json_bytes(current_consent.canonical_bytes())
    if rebuilt != current_consent:
        raise ValueError("current consent differs from its canonical identity")
    return rebuilt


@dataclass(frozen=True, slots=True)
class RetainedEvidenceReferenceV1:
    """Payload-free exact reference to retained pseudonymous evidence."""

    evidence_kind: RetainedEvidenceKindV1
    evidence_id: str
    evidence_sha256: str
    pseudonymous_profile_id: str
    schema_id: str = RETAINED_EVIDENCE_REFERENCE_SCHEMA_ID
    schema_version: int = RETAINED_EVIDENCE_REFERENCE_SCHEMA_VERSION
    reference_policy: str = RETAINED_REFERENCE_POLICY_V1

    def __post_init__(self) -> None:
        if type(self.evidence_kind) is not RetainedEvidenceKindV1:
            raise TypeError("retained evidence kind is invalid")
        _semantic_id(self.evidence_id, "retained evidence ID")
        _sha256(self.evidence_sha256, "retained evidence digest")
        require_profile_id(self.pseudonymous_profile_id)
        if self.reference_policy != RETAINED_REFERENCE_POLICY_V1:
            raise ValueError("retained evidence reference policy changed")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=RETAINED_EVIDENCE_REFERENCE_SCHEMA_ID,
            expected_version=RETAINED_EVIDENCE_REFERENCE_SCHEMA_VERSION,
            label="retained evidence reference",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_kind": self.evidence_kind.value,
            "evidence_sha256": self.evidence_sha256,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "reference_policy": self.reference_policy,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def reference_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> RetainedEvidenceReferenceV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "evidence_id",
                    "evidence_kind",
                    "evidence_sha256",
                    "pseudonymous_profile_id",
                    "reference_policy",
                    "schema_id",
                    "schema_version",
                }
            ),
            "retained evidence reference",
        )
        return cls(
            evidence_kind=RetainedEvidenceKindV1(
                _text(value["evidence_kind"], "retained evidence kind")
            ),
            evidence_id=_semantic_id(value["evidence_id"], "retained evidence ID"),
            evidence_sha256=_sha256(
                value["evidence_sha256"], "retained evidence digest"
            ),
            pseudonymous_profile_id=_text(
                value["pseudonymous_profile_id"], "retained evidence profile ID"
            ),
            reference_policy=_text(
                value["reference_policy"], "retained reference policy"
            ),
            schema_id=_text(value["schema_id"], "retained reference schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "retained reference schema version"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> RetainedEvidenceReferenceV1:
        return _from_json_bytes(cls, raw, "retained evidence reference")


def _canonical_retained_references(
    value: tuple[RetainedEvidenceReferenceV1, ...],
    *,
    allow_empty: bool,
) -> tuple[RetainedEvidenceReferenceV1, ...]:
    if type(value) is not tuple or any(
        type(item) is not RetainedEvidenceReferenceV1 for item in value
    ):
        raise TypeError("retained evidence references must be an immutable typed tuple")
    if not allow_empty and not value:
        raise ValueError("authorized evidence retention requires exact references")
    restored = tuple(
        RetainedEvidenceReferenceV1.from_canonical_bytes(item.canonical_bytes())
        for item in value
    )
    if restored != value:
        raise ValueError(
            "retained evidence reference differs from its canonical identity"
        )
    canonical = tuple(
        sorted(
            set(restored),
            key=lambda item: (
                item.evidence_kind.value,
                item.evidence_id,
                item.evidence_sha256,
                item.pseudonymous_profile_id,
            ),
        )
    )
    if canonical != restored:
        raise ValueError("retained evidence references must be unique and canonical")
    identity_keys = tuple(
        (item.evidence_kind, item.evidence_id) for item in restored
    )
    if len(identity_keys) != len(set(identity_keys)):
        raise ValueError("one retained evidence ID cannot carry conflicting digests")
    return restored


@dataclass(frozen=True, slots=True)
class ProfileDeletionPlanV1:
    """Pure, exact authorization plan produced before any filesystem mutation."""

    pseudonymous_profile_id: str
    consent_id: str
    consent_sha256: str
    consent_revision: int
    required_scope: ConsentScopeV1
    decision_time_utc: str
    planned_deletion_time_utc: str
    requested_pseudonymous_evidence_retention: bool
    deletion_decision_id: str
    deletion_decision_sha256: str
    deletion_decision_reason: ConsentDecisionReasonV1
    retention_at_deletion_decision_id: str | None
    retention_at_deletion_decision_sha256: str | None
    retention_at_deletion_reason: ConsentDecisionReasonV1 | None
    retained_evidence_references: tuple[RetainedEvidenceReferenceV1, ...]
    _construction_token: InitVar[object]
    schema_id: str = PROFILE_DELETION_PLAN_SCHEMA_ID
    schema_version: int = PROFILE_DELETION_PLAN_SCHEMA_VERSION
    direct_identity_action: str = DIRECT_IDENTITY_ACTION_V1
    evidence_bytes_action: str = EVIDENCE_BYTES_ACTION_V1
    retained_reference_policy: str = RETAINED_REFERENCE_POLICY_V1
    plan_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _PLAN_TOKEN:
            raise TypeError("profile deletion plans require the governed planner")
        require_profile_id(self.pseudonymous_profile_id)
        _pattern_id(self.consent_id, _CONSENT_ID, "deletion-plan consent ID")
        _sha256(self.consent_sha256, "deletion-plan consent digest")
        _positive_int(self.consent_revision, "deletion-plan consent revision")
        if type(self.required_scope) is not ConsentScopeV1:
            raise TypeError("deletion-plan consent scope is invalid")
        _utc(self.decision_time_utc, "deletion-plan decision time")
        _utc(self.planned_deletion_time_utc, "planned deletion time")
        if _timestamp(self.planned_deletion_time_utc) < _timestamp(
            self.decision_time_utc
        ):
            raise ValueError("planned deletion time cannot precede authorization")
        requested = _exact_bool(
            self.requested_pseudonymous_evidence_retention,
            "requested pseudonymous evidence retention",
        )
        _pattern_id(
            self.deletion_decision_id,
            _DECISION_ID,
            "profile deletion decision ID",
        )
        _sha256(self.deletion_decision_sha256, "profile deletion decision digest")
        if type(self.deletion_decision_reason) is not ConsentDecisionReasonV1:
            raise TypeError("profile deletion decision reason is invalid")
        retention_binding = (
            self.retention_at_deletion_decision_id,
            self.retention_at_deletion_decision_sha256,
            self.retention_at_deletion_reason,
        )
        _canonical_retained_references(
            self.retained_evidence_references,
            allow_empty=not requested,
        )
        if any(
            item.pseudonymous_profile_id != self.pseudonymous_profile_id
            for item in self.retained_evidence_references
        ):
            raise PermissionError(
                "retained evidence reference belongs to another pseudonymous profile"
            )
        if requested:
            if any(value is None for value in retention_binding):
                raise ValueError(
                    "retaining deletion must bind the deletion-time retention decision"
                )
            _pattern_id(
                self.retention_at_deletion_decision_id,
                _DECISION_ID,
                "deletion-time retention decision ID",
            )
            _sha256(
                self.retention_at_deletion_decision_sha256,
                "deletion-time retention decision digest",
            )
            if self.retention_at_deletion_reason not in {
                ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT,
                ConsentDecisionReasonV1.AUTHORIZED_PRIOR_RETENTION_AFTER_WITHDRAWAL,
            }:
                raise ValueError("deletion-time retention decision is not authorized")
            if self.deletion_decision_reason is not (
                ConsentDecisionReasonV1.PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION
            ):
                raise ValueError("retaining profile deletion reason is not authorized")
        else:
            if any(value is not None for value in retention_binding):
                raise ValueError("non-retaining plan cannot bind retention authorization")
            if self.retained_evidence_references:
                raise ValueError("non-retaining plan cannot claim retained evidence")
            if self.deletion_decision_reason is not (
                ConsentDecisionReasonV1.PROFILE_DELETION_WITHOUT_EVIDENCE_RETENTION
            ):
                raise ValueError("non-retaining profile deletion reason differs")
        if self.direct_identity_action != DIRECT_IDENTITY_ACTION_V1:
            raise ValueError("profile deletion direct-identity action changed")
        if self.evidence_bytes_action != EVIDENCE_BYTES_ACTION_V1:
            raise ValueError("profile deletion evidence-bytes action changed")
        if self.retained_reference_policy != RETAINED_REFERENCE_POLICY_V1:
            raise ValueError("profile deletion retained-reference policy changed")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=PROFILE_DELETION_PLAN_SCHEMA_ID,
            expected_version=PROFILE_DELETION_PLAN_SCHEMA_VERSION,
            label="profile deletion plan",
        )
        object.__setattr__(
            self,
            "plan_id",
            "profile-deletion-plan-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    @property
    def evidence_disposition(self) -> str:
        return (
            AUTHORIZED_RETENTION_DISPOSITION_V1
            if self.requested_pseudonymous_evidence_retention
            else NO_RETENTION_DISPOSITION_V1
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "consent_id": self.consent_id,
            "consent_revision": self.consent_revision,
            "consent_sha256": self.consent_sha256,
            "decision_time_utc": self.decision_time_utc,
            "deletion_decision_id": self.deletion_decision_id,
            "deletion_decision_reason": self.deletion_decision_reason.value,
            "deletion_decision_sha256": self.deletion_decision_sha256,
            "direct_identity_action": self.direct_identity_action,
            "evidence_bytes_action": self.evidence_bytes_action,
            "evidence_disposition": self.evidence_disposition,
            "planned_deletion_time_utc": self.planned_deletion_time_utc,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "requested_pseudonymous_evidence_retention": (
                self.requested_pseudonymous_evidence_retention
            ),
            "required_scope": self.required_scope.value,
            "retained_evidence_references": [
                item.as_dict() for item in self.retained_evidence_references
            ],
            "retained_reference_policy": self.retained_reference_policy,
            "retention_at_deletion_decision_id": (
                self.retention_at_deletion_decision_id
            ),
            "retention_at_deletion_decision_sha256": (
                self.retention_at_deletion_decision_sha256
            ),
            "retention_at_deletion_reason": (
                None
                if self.retention_at_deletion_reason is None
                else self.retention_at_deletion_reason.value
            ),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {"plan_id": self.plan_id, **self.identity_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ProfileDeletionPlanV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "consent_id",
                    "consent_revision",
                    "consent_sha256",
                    "decision_time_utc",
                    "deletion_decision_id",
                    "deletion_decision_reason",
                    "deletion_decision_sha256",
                    "direct_identity_action",
                    "evidence_bytes_action",
                    "evidence_disposition",
                    "plan_id",
                    "planned_deletion_time_utc",
                    "pseudonymous_profile_id",
                    "requested_pseudonymous_evidence_retention",
                    "required_scope",
                    "retained_evidence_references",
                    "retained_reference_policy",
                    "retention_at_deletion_decision_id",
                    "retention_at_deletion_decision_sha256",
                    "retention_at_deletion_reason",
                    "schema_id",
                    "schema_version",
                }
            ),
            "profile deletion plan",
        )
        raw_retention_reason = value["retention_at_deletion_reason"]
        plan = cls(
            pseudonymous_profile_id=_text(
                value["pseudonymous_profile_id"], "deletion-plan profile ID"
            ),
            consent_id=_pattern_id(
                value["consent_id"], _CONSENT_ID, "deletion-plan consent ID"
            ),
            consent_sha256=_sha256(
                value["consent_sha256"], "deletion-plan consent digest"
            ),
            consent_revision=_positive_int(
                value["consent_revision"], "deletion-plan consent revision"
            ),
            required_scope=ConsentScopeV1(
                _text(value["required_scope"], "deletion-plan consent scope")
            ),
            decision_time_utc=_utc(
                value["decision_time_utc"], "deletion-plan decision time"
            ),
            planned_deletion_time_utc=_utc(
                value["planned_deletion_time_utc"], "planned deletion time"
            ),
            requested_pseudonymous_evidence_retention=_exact_bool(
                value["requested_pseudonymous_evidence_retention"],
                "requested pseudonymous evidence retention",
            ),
            deletion_decision_id=_pattern_id(
                value["deletion_decision_id"],
                _DECISION_ID,
                "profile deletion decision ID",
            ),
            deletion_decision_sha256=_sha256(
                value["deletion_decision_sha256"],
                "profile deletion decision digest",
            ),
            deletion_decision_reason=ConsentDecisionReasonV1(
                _text(value["deletion_decision_reason"], "deletion decision reason")
            ),
            retention_at_deletion_decision_id=(
                None
                if value["retention_at_deletion_decision_id"] is None
                else _pattern_id(
                    value["retention_at_deletion_decision_id"],
                    _DECISION_ID,
                    "deletion-time retention decision ID",
                )
            ),
            retention_at_deletion_decision_sha256=(
                None
                if value["retention_at_deletion_decision_sha256"] is None
                else _sha256(
                    value["retention_at_deletion_decision_sha256"],
                    "deletion-time retention decision digest",
                )
            ),
            retention_at_deletion_reason=(
                None
                if raw_retention_reason is None
                else ConsentDecisionReasonV1(
                    _text(raw_retention_reason, "deletion-time retention reason")
                )
            ),
            retained_evidence_references=tuple(
                RetainedEvidenceReferenceV1.from_dict(item)
                for item in _json_array(
                    value["retained_evidence_references"],
                    "retained evidence references",
                )
            ),
            schema_id=_text(value["schema_id"], "deletion-plan schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "deletion-plan schema version"
            ),
            direct_identity_action=_text(
                value["direct_identity_action"], "deletion-plan identity action"
            ),
            evidence_bytes_action=_text(
                value["evidence_bytes_action"], "deletion-plan evidence action"
            ),
            retained_reference_policy=_text(
                value["retained_reference_policy"],
                "deletion-plan retained-reference policy",
            ),
            _construction_token=_PLAN_TOKEN,
        )
        if value["plan_id"] != plan.plan_id:
            raise ValueError("profile deletion plan ID differs")
        if value["evidence_disposition"] != plan.evidence_disposition:
            raise ValueError("profile deletion plan evidence disposition differs")
        return plan

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ProfileDeletionPlanV1:
        return _from_json_bytes(cls, raw, "profile deletion plan")


@dataclass(frozen=True, slots=True)
class ProfileDeletionReceiptSidecarV1:
    """Payload-free retention lineage beside the identity primitive's receipt."""

    pseudonymous_profile_id: str
    plan_id: str
    plan_sha256: str
    identity_receipt_id: str
    identity_receipt_sha256: str
    consent_id: str
    consent_sha256: str
    deletion_decision_id: str
    deletion_decision_sha256: str
    deletion_time_utc: str
    pseudonymous_evidence_retained: bool
    retained_evidence_references: tuple[RetainedEvidenceReferenceV1, ...]
    _construction_token: InitVar[object]
    schema_id: str = PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_ID
    schema_version: int = PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_VERSION
    direct_identity_action: str = DIRECT_IDENTITY_ACTION_V1
    evidence_bytes_action: str = EVIDENCE_BYTES_ACTION_V1
    retained_reference_policy: str = RETAINED_REFERENCE_POLICY_V1
    sidecar_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _SIDECAR_TOKEN:
            raise TypeError("deletion receipt sidecars require governed execution")
        require_profile_id(self.pseudonymous_profile_id)
        _semantic_id(self.plan_id, "deletion sidecar plan ID")
        _sha256(self.plan_sha256, "deletion sidecar plan digest")
        _pattern_id(
            self.identity_receipt_id,
            _RECEIPT_ID,
            "identity deletion receipt ID",
        )
        _sha256(
            self.identity_receipt_sha256,
            "identity deletion receipt digest",
        )
        _pattern_id(self.consent_id, _CONSENT_ID, "deletion sidecar consent ID")
        _sha256(self.consent_sha256, "deletion sidecar consent digest")
        _pattern_id(
            self.deletion_decision_id,
            _DECISION_ID,
            "deletion sidecar decision ID",
        )
        _sha256(
            self.deletion_decision_sha256,
            "deletion sidecar decision digest",
        )
        _utc(self.deletion_time_utc, "deletion sidecar deletion time")
        retained = _exact_bool(
            self.pseudonymous_evidence_retained,
            "sidecar pseudonymous evidence retention",
        )
        _canonical_retained_references(
            self.retained_evidence_references,
            allow_empty=not retained,
        )
        if not retained and self.retained_evidence_references:
            raise ValueError("non-retaining sidecar cannot claim evidence references")
        if any(
            item.pseudonymous_profile_id != self.pseudonymous_profile_id
            for item in self.retained_evidence_references
        ):
            raise PermissionError("deletion sidecar contains another profile's evidence")
        if self.direct_identity_action != DIRECT_IDENTITY_ACTION_V1:
            raise ValueError("deletion sidecar direct-identity action changed")
        if self.evidence_bytes_action != EVIDENCE_BYTES_ACTION_V1:
            raise ValueError("deletion sidecar evidence action changed")
        if self.retained_reference_policy != RETAINED_REFERENCE_POLICY_V1:
            raise ValueError("deletion sidecar retained-reference policy changed")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_ID,
            expected_version=PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_VERSION,
            label="profile deletion receipt sidecar",
        )
        object.__setattr__(
            self,
            "sidecar_id",
            "profile-deletion-sidecar-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    @property
    def evidence_disposition(self) -> str:
        return (
            AUTHORIZED_RETENTION_DISPOSITION_V1
            if self.pseudonymous_evidence_retained
            else NO_RETENTION_DISPOSITION_V1
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "consent_id": self.consent_id,
            "consent_sha256": self.consent_sha256,
            "deletion_decision_id": self.deletion_decision_id,
            "deletion_decision_sha256": self.deletion_decision_sha256,
            "deletion_time_utc": self.deletion_time_utc,
            "direct_identity_action": self.direct_identity_action,
            "evidence_bytes_action": self.evidence_bytes_action,
            "evidence_disposition": self.evidence_disposition,
            "identity_receipt_id": self.identity_receipt_id,
            "identity_receipt_sha256": self.identity_receipt_sha256,
            "plan_id": self.plan_id,
            "plan_sha256": self.plan_sha256,
            "pseudonymous_evidence_retained": self.pseudonymous_evidence_retained,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "retained_evidence_references": [
                item.as_dict() for item in self.retained_evidence_references
            ],
            "retained_reference_policy": self.retained_reference_policy,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {"sidecar_id": self.sidecar_id, **self.identity_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sidecar_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ProfileDeletionReceiptSidecarV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "consent_id",
                    "consent_sha256",
                    "deletion_decision_id",
                    "deletion_decision_sha256",
                    "deletion_time_utc",
                    "direct_identity_action",
                    "evidence_bytes_action",
                    "evidence_disposition",
                    "identity_receipt_id",
                    "identity_receipt_sha256",
                    "plan_id",
                    "plan_sha256",
                    "pseudonymous_evidence_retained",
                    "pseudonymous_profile_id",
                    "retained_evidence_references",
                    "retained_reference_policy",
                    "schema_id",
                    "schema_version",
                    "sidecar_id",
                }
            ),
            "profile deletion receipt sidecar",
        )
        sidecar = cls(
            pseudonymous_profile_id=_text(
                value["pseudonymous_profile_id"], "deletion sidecar profile ID"
            ),
            plan_id=_semantic_id(value["plan_id"], "deletion sidecar plan ID"),
            plan_sha256=_sha256(
                value["plan_sha256"], "deletion sidecar plan digest"
            ),
            identity_receipt_id=_pattern_id(
                value["identity_receipt_id"],
                _RECEIPT_ID,
                "identity deletion receipt ID",
            ),
            identity_receipt_sha256=_sha256(
                value["identity_receipt_sha256"],
                "identity deletion receipt digest",
            ),
            consent_id=_pattern_id(
                value["consent_id"], _CONSENT_ID, "deletion sidecar consent ID"
            ),
            consent_sha256=_sha256(
                value["consent_sha256"], "deletion sidecar consent digest"
            ),
            deletion_decision_id=_pattern_id(
                value["deletion_decision_id"],
                _DECISION_ID,
                "deletion sidecar decision ID",
            ),
            deletion_decision_sha256=_sha256(
                value["deletion_decision_sha256"],
                "deletion sidecar decision digest",
            ),
            deletion_time_utc=_utc(
                value["deletion_time_utc"], "deletion sidecar deletion time"
            ),
            pseudonymous_evidence_retained=_exact_bool(
                value["pseudonymous_evidence_retained"],
                "sidecar pseudonymous evidence retention",
            ),
            retained_evidence_references=tuple(
                RetainedEvidenceReferenceV1.from_dict(item)
                for item in _json_array(
                    value["retained_evidence_references"],
                    "sidecar retained evidence references",
                )
            ),
            schema_id=_text(value["schema_id"], "deletion sidecar schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "deletion sidecar schema version"
            ),
            direct_identity_action=_text(
                value["direct_identity_action"], "deletion sidecar identity action"
            ),
            evidence_bytes_action=_text(
                value["evidence_bytes_action"], "deletion sidecar evidence action"
            ),
            retained_reference_policy=_text(
                value["retained_reference_policy"],
                "deletion sidecar retained-reference policy",
            ),
            _construction_token=_SIDECAR_TOKEN,
        )
        if value["sidecar_id"] != sidecar.sidecar_id:
            raise ValueError("profile deletion sidecar ID differs")
        if value["evidence_disposition"] != sidecar.evidence_disposition:
            raise ValueError("profile deletion sidecar evidence disposition differs")
        return sidecar

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ProfileDeletionReceiptSidecarV1:
        return _from_json_bytes(cls, raw, "profile deletion receipt sidecar")


@dataclass(frozen=True, slots=True)
class ProfileDeletionResultV1:
    """Completed identity deletion plus its exact payload-free retention sidecar."""

    plan: ProfileDeletionPlanV1
    identity_receipt: IdentityDeletionReceiptV1
    receipt_sidecar: ProfileDeletionReceiptSidecarV1
    _construction_token: InitVar[object]
    schema_id: str = PROFILE_DELETION_RESULT_SCHEMA_ID
    schema_version: int = PROFILE_DELETION_RESULT_SCHEMA_VERSION
    execution_status: str = DELETION_EXECUTION_STATUS_V1
    result_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _RESULT_TOKEN:
            raise TypeError("profile deletion results require governed execution")
        if type(self.plan) is not ProfileDeletionPlanV1:
            raise TypeError("profile deletion result plan is invalid")
        if type(self.identity_receipt) is not IdentityDeletionReceiptV1:
            raise TypeError("profile deletion identity receipt is invalid")
        if type(self.receipt_sidecar) is not ProfileDeletionReceiptSidecarV1:
            raise TypeError("profile deletion receipt sidecar is invalid")
        receipt = self.identity_receipt
        plan = self.plan
        if (
            receipt.pseudonymous_profile_id != plan.pseudonymous_profile_id
            or receipt.deletion_decision_id != plan.deletion_decision_id
            or receipt.deletion_decision_sha256 != plan.deletion_decision_sha256
            or receipt.consent_id != plan.consent_id
            or receipt.consent_sha256 != plan.consent_sha256
            or receipt.pseudonymous_evidence_retained
            is not plan.requested_pseudonymous_evidence_retention
            or receipt.deletion_time_utc != plan.planned_deletion_time_utc
        ):
            raise ValueError("identity deletion receipt differs from its exact plan")
        sidecar = self.receipt_sidecar
        if (
            sidecar.pseudonymous_profile_id != plan.pseudonymous_profile_id
            or sidecar.plan_id != plan.plan_id
            or sidecar.plan_sha256 != plan.plan_sha256
            or sidecar.identity_receipt_id != receipt.receipt_id
            or sidecar.identity_receipt_sha256 != receipt.receipt_sha256
            or sidecar.consent_id != plan.consent_id
            or sidecar.consent_sha256 != plan.consent_sha256
            or sidecar.deletion_decision_id != plan.deletion_decision_id
            or sidecar.deletion_decision_sha256 != plan.deletion_decision_sha256
            or sidecar.deletion_time_utc != plan.planned_deletion_time_utc
            or sidecar.pseudonymous_evidence_retained
            is not plan.requested_pseudonymous_evidence_retention
            or sidecar.retained_evidence_references
            != plan.retained_evidence_references
        ):
            raise ValueError("deletion receipt sidecar differs from plan or receipt")
        if self.execution_status != DELETION_EXECUTION_STATUS_V1:
            raise ValueError("profile deletion execution status changed")
        _schema(
            self.schema_id,
            self.schema_version,
            expected_id=PROFILE_DELETION_RESULT_SCHEMA_ID,
            expected_version=PROFILE_DELETION_RESULT_SCHEMA_VERSION,
            label="profile deletion result",
        )
        object.__setattr__(
            self,
            "result_id",
            "profile-deletion-result-"
            + hashlib.sha256(_canonical_json_bytes(self.identity_dict())).hexdigest(),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "execution_status": self.execution_status,
            "identity_receipt": self.identity_receipt.as_dict(),
            "plan": self.plan.as_dict(),
            "receipt_sidecar": self.receipt_sidecar.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {"result_id": self.result_id, **self.identity_dict()}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def result_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, raw: object) -> ProfileDeletionResultV1:
        value = _fields(
            raw,
            frozenset(
                {
                    "execution_status",
                    "identity_receipt",
                    "plan",
                    "receipt_sidecar",
                    "result_id",
                    "schema_id",
                    "schema_version",
                }
            ),
            "profile deletion result",
        )
        raw_receipt = value["identity_receipt"]
        if type(raw_receipt) is not dict:
            raise TypeError("identity deletion receipt must be an exact object")
        result = cls(
            plan=ProfileDeletionPlanV1.from_dict(value["plan"]),
            identity_receipt=IdentityDeletionReceiptV1.from_json_bytes(
                _canonical_json_bytes(raw_receipt)
            ),
            receipt_sidecar=ProfileDeletionReceiptSidecarV1.from_dict(
                value["receipt_sidecar"]
            ),
            schema_id=_text(value["schema_id"], "deletion-result schema ID"),
            schema_version=_positive_int(
                value["schema_version"], "deletion-result schema version"
            ),
            execution_status=_text(
                value["execution_status"], "deletion-result execution status"
            ),
            _construction_token=_RESULT_TOKEN,
        )
        if value["result_id"] != result.result_id:
            raise ValueError("profile deletion result ID differs")
        return result

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ProfileDeletionResultV1:
        return _from_json_bytes(cls, raw, "profile deletion result")


def create_retained_evidence_reference(
    *,
    evidence_kind: RetainedEvidenceKindV1,
    evidence_id: str,
    evidence_sha256: str,
    pseudonymous_profile_id: str,
) -> RetainedEvidenceReferenceV1:
    """Bind one immutable artifact without accepting its payload or local path."""

    return RetainedEvidenceReferenceV1(
        evidence_kind=evidence_kind,
        evidence_id=evidence_id,
        evidence_sha256=evidence_sha256,
        pseudonymous_profile_id=pseudonymous_profile_id,
    )


def plan_profile_deletion(
    current_consent: ConsentRecordV1,
    *,
    required_scope: ConsentScopeV1,
    requested_pseudonymous_evidence_retention: bool,
    retained_evidence_references: tuple[RetainedEvidenceReferenceV1, ...] = (),
    decision_time_utc: str,
    planned_deletion_time_utc: str,
) -> ProfileDeletionPlanV1:
    """Purely authorize an exact identity-only deletion at an explicit UTC time.

    Requested retention is evaluated twice: at authorization and again at the
    planned deletion time.  A refused outcome raises before any ``DataPaths`` value
    or filesystem operation enters the workflow.
    """

    consent = _validated_current_consent(current_consent)
    if type(required_scope) is not ConsentScopeV1:
        raise TypeError("profile deletion scope must be ConsentScopeV1")
    requested = _exact_bool(
        requested_pseudonymous_evidence_retention,
        "requested pseudonymous evidence retention",
    )
    decision_time = _utc(decision_time_utc, "profile deletion decision time")
    deletion_time = _utc(
        planned_deletion_time_utc,
        "planned profile deletion time",
    )
    if _timestamp(deletion_time) < _timestamp(decision_time):
        raise ValueError("planned deletion time cannot precede authorization")
    if type(retained_evidence_references) is not tuple or any(
        type(item) is not RetainedEvidenceReferenceV1
        for item in retained_evidence_references
    ):
        raise TypeError("retained evidence references must be an immutable typed tuple")
    if requested and not retained_evidence_references:
        raise ValueError("requested retention requires at least one exact reference")
    if not requested and retained_evidence_references:
        raise ValueError("evidence references require an explicit retention request")
    restored_references = tuple(
        RetainedEvidenceReferenceV1.from_canonical_bytes(item.canonical_bytes())
        for item in retained_evidence_references
    )
    if restored_references != retained_evidence_references:
        raise ValueError(
            "retained evidence reference differs from its canonical identity"
        )
    canonical_references = tuple(
        sorted(
            set(restored_references),
            key=lambda item: (
                item.evidence_kind.value,
                item.evidence_id,
                item.evidence_sha256,
                item.pseudonymous_profile_id,
            ),
        )
    )
    _canonical_retained_references(
        canonical_references,
        allow_empty=not requested,
    )
    if any(
        item.pseudonymous_profile_id != consent.pseudonymous_profile_id
        for item in canonical_references
    ):
        raise PermissionError(
            "retained evidence reference belongs to another pseudonymous profile"
        )

    deletion_decision = decide_profile_deletion(
        consent,
        required_scope=required_scope,
        requested_pseudonymous_evidence_retention=requested,
        decision_time_utc=decision_time,
    )
    if deletion_decision.allowed is not True:
        raise PermissionError(
            "profile deletion or requested evidence retention was refused: "
            f"{deletion_decision.reason.value}"
        )

    retention_at_deletion = None
    if requested:
        retention_at_deletion = decide_retention_after_profile_deletion(
            consent,
            required_scope=required_scope,
            decision_time_utc=deletion_time,
        )
        if retention_at_deletion.allowed is not True:
            raise PermissionError(
                "requested evidence retention is not allowed at deletion time: "
                f"{retention_at_deletion.reason.value}"
            )

    return ProfileDeletionPlanV1(
        pseudonymous_profile_id=consent.pseudonymous_profile_id,
        consent_id=consent.consent_id,
        consent_sha256=consent.consent_sha256,
        consent_revision=consent.revision,
        required_scope=required_scope,
        decision_time_utc=decision_time,
        planned_deletion_time_utc=deletion_time,
        requested_pseudonymous_evidence_retention=requested,
        deletion_decision_id=deletion_decision.decision_id,
        deletion_decision_sha256=deletion_decision.decision_sha256,
        deletion_decision_reason=deletion_decision.reason,
        retention_at_deletion_decision_id=(
            None if retention_at_deletion is None else retention_at_deletion.decision_id
        ),
        retention_at_deletion_decision_sha256=(
            None
            if retention_at_deletion is None
            else retention_at_deletion.decision_sha256
        ),
        retention_at_deletion_reason=(
            None if retention_at_deletion is None else retention_at_deletion.reason
        ),
        retained_evidence_references=canonical_references,
        _construction_token=_PLAN_TOKEN,
    )


def _revalidate_plan_authorization(
    current_consent: ConsentRecordV1,
    plan: ProfileDeletionPlanV1,
) -> tuple[ConsentRecordV1, ProfileDeletionDecisionV1]:
    consent = _validated_current_consent(current_consent)
    if consent.pseudonymous_profile_id != plan.pseudonymous_profile_id:
        raise PermissionError("current consent belongs to another profile")
    expected_plan = plan_profile_deletion(
        consent,
        required_scope=plan.required_scope,
        requested_pseudonymous_evidence_retention=(
            plan.requested_pseudonymous_evidence_retention
        ),
        retained_evidence_references=plan.retained_evidence_references,
        decision_time_utc=plan.decision_time_utc,
        planned_deletion_time_utc=plan.planned_deletion_time_utc,
    )
    if expected_plan != plan:
        raise PermissionError(
            "profile deletion plan differs from the current consent authorization"
        )
    deletion_decision = decide_profile_deletion(
        consent,
        required_scope=plan.required_scope,
        requested_pseudonymous_evidence_retention=(
            plan.requested_pseudonymous_evidence_retention
        ),
        decision_time_utc=plan.decision_time_utc,
    )
    if (
        deletion_decision.allowed is not True
        or deletion_decision.decision_id != plan.deletion_decision_id
        or deletion_decision.decision_sha256 != plan.deletion_decision_sha256
    ):
        raise PermissionError("profile deletion authorization is stale or refused")
    return consent, deletion_decision


def execute_profile_deletion(
    paths: DataPaths,
    current_consent: ConsentRecordV1,
    plan: ProfileDeletionPlanV1,
    *,
    deletion_time_utc: str,
) -> ProfileDeletionResultV1:
    """Execute one exact plan through the existing identity deletion primitive.

    The wrapper never writes retained run/evidence bytes.  The delegated primitive
    removes only the separately erasable identity mapping and persists its existing
    payload-free identity-deletion receipt.
    """

    if type(paths) is not DataPaths:
        raise TypeError("profile deletion execution requires exact DataPaths")
    if type(plan) is not ProfileDeletionPlanV1:
        raise TypeError("profile deletion execution requires ProfileDeletionPlanV1")
    restored_plan = ProfileDeletionPlanV1.from_canonical_bytes(plan.canonical_bytes())
    if restored_plan != plan:
        raise ValueError("profile deletion plan differs from its canonical identity")
    deletion_time = _utc(deletion_time_utc, "profile deletion execution time")
    if deletion_time != plan.planned_deletion_time_utc:
        raise ValueError("execution time differs from the exact planned deletion time")

    # Complete every authorization and retained-reference check before invoking the
    # only filesystem-mutating primitive in this module.
    consent, deletion_decision = _revalidate_plan_authorization(
        current_consent,
        restored_plan,
    )
    identity_receipt = delete_identity_mapping(
        paths,
        restored_plan.pseudonymous_profile_id,
        deletion_decision,
        consent,
        deletion_time_utc=deletion_time,
    )
    sidecar = ProfileDeletionReceiptSidecarV1(
        pseudonymous_profile_id=restored_plan.pseudonymous_profile_id,
        plan_id=restored_plan.plan_id,
        plan_sha256=restored_plan.plan_sha256,
        identity_receipt_id=identity_receipt.receipt_id,
        identity_receipt_sha256=identity_receipt.receipt_sha256,
        consent_id=restored_plan.consent_id,
        consent_sha256=restored_plan.consent_sha256,
        deletion_decision_id=restored_plan.deletion_decision_id,
        deletion_decision_sha256=restored_plan.deletion_decision_sha256,
        deletion_time_utc=deletion_time,
        pseudonymous_evidence_retained=(
            restored_plan.requested_pseudonymous_evidence_retention
        ),
        retained_evidence_references=restored_plan.retained_evidence_references,
        _construction_token=_SIDECAR_TOKEN,
    )
    return ProfileDeletionResultV1(
        plan=restored_plan,
        identity_receipt=identity_receipt,
        receipt_sidecar=sidecar,
        _construction_token=_RESULT_TOKEN,
    )


def load_retained_evidence_reference(raw: bytes) -> RetainedEvidenceReferenceV1:
    return RetainedEvidenceReferenceV1.from_canonical_bytes(raw)


def load_profile_deletion_plan(raw: bytes) -> ProfileDeletionPlanV1:
    return ProfileDeletionPlanV1.from_canonical_bytes(raw)


def load_profile_deletion_receipt_sidecar(
    raw: bytes,
) -> ProfileDeletionReceiptSidecarV1:
    return ProfileDeletionReceiptSidecarV1.from_canonical_bytes(raw)


def load_profile_deletion_result(raw: bytes) -> ProfileDeletionResultV1:
    return ProfileDeletionResultV1.from_canonical_bytes(raw)


__all__ = [
    "AUTHORIZED_RETENTION_DISPOSITION_V1",
    "DELETION_EXECUTION_STATUS_V1",
    "DIRECT_IDENTITY_ACTION_V1",
    "EVIDENCE_BYTES_ACTION_V1",
    "NO_RETENTION_DISPOSITION_V1",
    "PROFILE_DELETION_PLAN_SCHEMA_ID",
    "PROFILE_DELETION_PLAN_SCHEMA_VERSION",
    "PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_ID",
    "PROFILE_DELETION_RECEIPT_SIDECAR_SCHEMA_VERSION",
    "PROFILE_DELETION_RESULT_SCHEMA_ID",
    "PROFILE_DELETION_RESULT_SCHEMA_VERSION",
    "RETAINED_EVIDENCE_REFERENCE_SCHEMA_ID",
    "RETAINED_EVIDENCE_REFERENCE_SCHEMA_VERSION",
    "RETAINED_REFERENCE_POLICY_V1",
    "ProfileDeletionPlanV1",
    "ProfileDeletionReceiptSidecarV1",
    "ProfileDeletionResultV1",
    "RetainedEvidenceKindV1",
    "RetainedEvidenceReferenceV1",
    "create_retained_evidence_reference",
    "execute_profile_deletion",
    "load_profile_deletion_plan",
    "load_profile_deletion_receipt_sidecar",
    "load_profile_deletion_result",
    "load_retained_evidence_reference",
    "plan_profile_deletion",
]
