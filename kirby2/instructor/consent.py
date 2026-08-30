"""Immutable local consent records and fail-closed WO37-A policy decisions.

These contracts describe Kirby2's local engineering policy.  They are not legal
advice, a representation of anonymity, or a claim of compliance with any privacy or
human-subjects regime.  Every permission is an explicit input.  Record and decision
times are explicit canonical UTC values; this module never consults a wall clock.

Direct identity is deliberately absent.  Consent binds only an opaque pseudonymous
profile ID, while separately erasable direct-identity mappings remain owned by
``kirby2.instructor.identity``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from enum import Enum

from .models import require_profile_id


CONSENT_RECORD_SCHEMA_ID = "KIRBY2_CONSENT_RECORD_V1"
CONSENT_RECORD_SCHEMA_VERSION = 1
RETENTION_AFTER_DELETION_DECISION_SCHEMA_ID = (
    "KIRBY2_RETENTION_AFTER_PROFILE_DELETION_DECISION_V1"
)
RETENTION_AFTER_DELETION_DECISION_SCHEMA_VERSION = 1
EVIDENCE_EXPORT_DECISION_SCHEMA_ID = "KIRBY2_EVIDENCE_EXPORT_DECISION_V1"
EVIDENCE_EXPORT_DECISION_SCHEMA_VERSION = 1
PROFILE_DELETION_DECISION_SCHEMA_ID = "KIRBY2_PROFILE_DELETION_DECISION_V1"
PROFILE_DELETION_DECISION_SCHEMA_VERSION = 1
CONSENT_RECORD_KIND = "IMMUTABLE_PSEUDONYMOUS_CONSENT_RECORD_V1"
CONSENT_ID_PREFIX = "consent-"
CONSENT_DECISION_ID_PREFIX = "consent-decision-"
PSEUDONYMIZATION_CLAIM = "PSEUDONYMOUS_NOT_ANONYMOUS"
DIRECT_IDENTITY_POLICY = "DIRECT_IDENTITY_EXCLUDED_FROM_CONSENT_EVIDENCE"
CURRENT_CONSENT_AUTHORITY_POLICY = "CALLER_ASSERTED_CURRENT_CONSENT_REQUIRED"


_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONSENT_ID = re.compile(r"consent-[0-9a-f]{24}\Z")
_CONSENT_TOKEN = object()
_RETENTION_DECISION_TOKEN = object()
_EXPORT_DECISION_TOKEN = object()
_DELETION_DECISION_TOKEN = object()
# This is a live-object mutation tripwire, not authentication for arbitrary saved
# bytes.  A storage boundary must independently pin a restored record's digest.
_CONSENT_INTEGRITY_KEY = secrets.token_bytes(32)


class ConsentScopeV1(str, Enum):
    """Closed purposes for which pseudonymous evidence may be used."""

    INSTRUCTIONAL_EVIDENCE = "INSTRUCTIONAL_EVIDENCE"
    INSTRUCTOR_REVIEW = "INSTRUCTOR_REVIEW"
    LOCAL_COHORT_ANALYSIS = "LOCAL_COHORT_ANALYSIS"
    LOCAL_RESEARCH_STUDY = "LOCAL_RESEARCH_STUDY"


class ConsentStateV1(str, Enum):
    GRANTED = "GRANTED"
    WITHDRAWN = "WITHDRAWN"


class EvidenceRetentionPolicyV1(str, Enum):
    """Explicit retention disposition for pseudonymous evidence."""

    DELETE_WITH_PROFILE = "DELETE_WITH_PROFILE"
    RETAIN_UNTIL_UTC = "RETAIN_UNTIL_UTC"
    RETAIN_WITHOUT_FIXED_END = "RETAIN_WITHOUT_FIXED_END"


class EvidenceExportPermissionV1(str, Enum):
    DENIED = "DENIED"
    PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY = (
        "PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY"
    )


class EvidenceExportClassV1(str, Enum):
    """Requested export classes, including classes that can never be authorized."""

    PSEUDONYMOUS_REDACTED_EVIDENCE = "PSEUDONYMOUS_REDACTED_EVIDENCE"
    DIRECT_IDENTITY = "DIRECT_IDENTITY"
    IDENTITY_MAPPING = "IDENTITY_MAPPING"
    UNREDACTED_EVIDENCE = "UNREDACTED_EVIDENCE"


class WithdrawalPolicyV1(str, Enum):
    """Effect of withdrawal on already-created pseudonymous evidence."""

    REVOKE_FUTURE_RETENTION_AND_EXPORT = (
        "REVOKE_FUTURE_RETENTION_AND_EXPORT"
    )
    PRESERVE_PREVIOUSLY_AUTHORIZED_RETENTION_REVOKE_EXPORT = (
        "PRESERVE_PREVIOUSLY_AUTHORIZED_RETENTION_REVOKE_EXPORT"
    )


class ConsentDecisionStatusV1(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    REFUSED = "REFUSED"


class ConsentDecisionReasonV1(str, Enum):
    AUTHORIZED_BY_ACTIVE_CONSENT = "AUTHORIZED_BY_ACTIVE_CONSENT"
    AUTHORIZED_PRIOR_RETENTION_AFTER_WITHDRAWAL = (
        "AUTHORIZED_PRIOR_RETENTION_AFTER_WITHDRAWAL"
    )
    PROFILE_DELETION_WITHOUT_EVIDENCE_RETENTION = (
        "PROFILE_DELETION_WITHOUT_EVIDENCE_RETENTION"
    )
    PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION = (
        "PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION"
    )
    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    SCOPE_NOT_GRANTED = "SCOPE_NOT_GRANTED"
    RETENTION_AFTER_DELETION_NOT_GRANTED = (
        "RETENTION_AFTER_DELETION_NOT_GRANTED"
    )
    RETENTION_POLICY_PROHIBITS = "RETENTION_POLICY_PROHIBITS"
    RETENTION_EXPIRED = "RETENTION_EXPIRED"
    EXPORT_NOT_GRANTED = "EXPORT_NOT_GRANTED"
    DIRECT_IDENTITY_EXPORT_PROHIBITED = "DIRECT_IDENTITY_EXPORT_PROHIBITED"
    IDENTITY_MAPPING_EXPORT_PROHIBITED = "IDENTITY_MAPPING_EXPORT_PROHIBITED"
    UNREDACTED_EXPORT_PROHIBITED = "UNREDACTED_EXPORT_PROHIBITED"


_AUTHORIZED_REASONS = frozenset(
    {
        ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT,
        ConsentDecisionReasonV1.AUTHORIZED_PRIOR_RETENTION_AFTER_WITHDRAWAL,
        ConsentDecisionReasonV1.PROFILE_DELETION_WITHOUT_EVIDENCE_RETENTION,
        ConsentDecisionReasonV1.PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION,
    }
)


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
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be canonical ASCII JSON") from error
    if not isinstance(payload, dict) or _canonical_json_bytes(payload) != raw:
        raise ValueError(f"{label} must be one canonical JSON object")
    return payload


def _utc(value: object, label: str) -> str:
    if type(value) is not str or _UTC.fullmatch(value) is None:
        raise ValueError(f"{label} must use canonical UTC seconds ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is not a valid UTC timestamp") from error
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _consent_id(value: object, label: str) -> str:
    if type(value) is not str or _CONSENT_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be exact text")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _canonical_scopes(scopes: tuple[ConsentScopeV1, ...]) -> tuple[ConsentScopeV1, ...]:
    if type(scopes) is not tuple or not scopes or any(
        type(scope) is not ConsentScopeV1 for scope in scopes
    ):
        raise TypeError("consent scopes must be a nonempty immutable typed tuple")
    canonical = tuple(sorted(set(scopes), key=lambda item: item.value.encode("ascii")))
    if canonical != scopes:
        raise ValueError("consent scopes must be unique and canonically ordered")
    return scopes


def _validate_retention_contract(
    *,
    state: ConsentStateV1,
    recorded_at_utc: str,
    retention_policy: EvidenceRetentionPolicyV1,
    retention_until_utc: str | None,
    retain_after_deletion: bool,
) -> None:
    if type(retention_policy) is not EvidenceRetentionPolicyV1:
        raise TypeError("consent retention policy is invalid")
    _exact_bool(
        retain_after_deletion,
        "pseudonymous evidence retention after profile deletion",
    )
    if retention_until_utc is not None:
        _utc(retention_until_utc, "consent retention end")

    if retention_policy is EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE:
        if retain_after_deletion or retention_until_utc is not None:
            raise ValueError(
                "delete-with-profile retention cannot retain evidence or have an end"
            )
        return
    if retention_policy is EvidenceRetentionPolicyV1.RETAIN_UNTIL_UTC:
        if not retain_after_deletion or retention_until_utc is None:
            raise ValueError(
                "bounded retention requires post-deletion retention and an end"
            )
        if state is ConsentStateV1.GRANTED and retention_until_utc <= recorded_at_utc:
            raise ValueError("active bounded retention must end after consent time")
        return
    if retention_policy is EvidenceRetentionPolicyV1.RETAIN_WITHOUT_FIXED_END:
        if not retain_after_deletion or retention_until_utc is not None:
            raise ValueError(
                "unbounded retention requires post-deletion retention without an end"
            )
        return
    raise TypeError("consent retention policy is invalid")


def _validate_decision_outcome(
    allowed: bool,
    status: ConsentDecisionStatusV1,
    reason: ConsentDecisionReasonV1,
) -> None:
    _exact_bool(allowed, "consent decision allowed")
    if type(status) is not ConsentDecisionStatusV1:
        raise TypeError("consent decision status is invalid")
    if type(reason) is not ConsentDecisionReasonV1:
        raise TypeError("consent decision reason is invalid")
    if (status is ConsentDecisionStatusV1.AUTHORIZED) != allowed:
        raise ValueError("consent decision status and allowed flag disagree")
    if (reason in _AUTHORIZED_REASONS) != allowed:
        raise ValueError("consent decision reason and allowed flag disagree")


def _consent_integrity_commitment(record: ConsentRecordV1) -> str:
    """Bind one live record instance to the exact fields set by its factory."""

    return hmac.new(
        _CONSENT_INTEGRITY_KEY,
        _canonical_json_bytes(record.as_dict()),
        hashlib.sha256,
    ).hexdigest()


def _require_consent_integrity(record: ConsentRecordV1) -> None:
    stored = getattr(record, "_integrity_commitment", None)
    if type(stored) is not str or not hmac.compare_digest(
        stored,
        _consent_integrity_commitment(record),
    ):
        raise ValueError("consent record live integrity commitment differs")


@dataclass(frozen=True, slots=True)
class ConsentRecordV1:
    """One immutable consent revision over an opaque pseudonymous profile."""

    pseudonymous_profile_id: str
    scopes: tuple[ConsentScopeV1, ...]
    recorded_at_utc: str
    state: ConsentStateV1
    retention_policy: EvidenceRetentionPolicyV1
    retention_until_utc: str | None
    retain_pseudonymous_evidence_after_profile_deletion: bool
    export_permission: EvidenceExportPermissionV1
    withdrawal_policy: WithdrawalPolicyV1
    revision: int
    predecessor_consent_id: str | None
    predecessor_sha256: str | None
    _construction_token: InitVar[object]
    schema_id: str = CONSENT_RECORD_SCHEMA_ID
    schema_version: int = CONSENT_RECORD_SCHEMA_VERSION
    record_kind: str = CONSENT_RECORD_KIND
    pseudonymization_claim: str = PSEUDONYMIZATION_CLAIM
    direct_identity_policy: str = DIRECT_IDENTITY_POLICY
    consent_id: str = field(init=False)
    _integrity_commitment: str = field(init=False, repr=False, compare=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _CONSENT_TOKEN:
            raise TypeError("consent records require a governed consent factory")
        require_profile_id(self.pseudonymous_profile_id)
        _canonical_scopes(self.scopes)
        _utc(self.recorded_at_utc, "consent record time")
        if type(self.state) is not ConsentStateV1:
            raise TypeError("consent state is invalid")
        _validate_retention_contract(
            state=self.state,
            recorded_at_utc=self.recorded_at_utc,
            retention_policy=self.retention_policy,
            retention_until_utc=self.retention_until_utc,
            retain_after_deletion=(
                self.retain_pseudonymous_evidence_after_profile_deletion
            ),
        )
        if type(self.export_permission) is not EvidenceExportPermissionV1:
            raise TypeError("consent export permission is invalid")
        if type(self.withdrawal_policy) is not WithdrawalPolicyV1:
            raise TypeError("consent withdrawal policy is invalid")
        _positive_int(self.revision, "consent revision")
        if (self.predecessor_consent_id is None) != (
            self.predecessor_sha256 is None
        ):
            raise ValueError("consent predecessor ID and digest must travel together")
        if self.revision == 1:
            if self.predecessor_consent_id is not None:
                raise ValueError("first consent revision cannot have a predecessor")
            if self.state is ConsentStateV1.WITHDRAWN:
                raise ValueError("withdrawal requires a prior granted consent revision")
        else:
            _consent_id(self.predecessor_consent_id, "predecessor consent ID")
            _sha256(self.predecessor_sha256, "predecessor consent digest")
        if (
            self.schema_id != CONSENT_RECORD_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != CONSENT_RECORD_SCHEMA_VERSION
            or self.record_kind != CONSENT_RECORD_KIND
            or self.pseudonymization_claim != PSEUDONYMIZATION_CLAIM
            or self.direct_identity_policy != DIRECT_IDENTITY_POLICY
        ):
            raise ValueError("consent schema or privacy claim differs")
        object.__setattr__(
            self,
            "consent_id",
            CONSENT_ID_PREFIX + _canonical_sha256(self.identity_dict())[:24],
        )
        object.__setattr__(
            self,
            "_integrity_commitment",
            _consent_integrity_commitment(self),
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "direct_identity_policy": self.direct_identity_policy,
            "export_permission": self.export_permission.value,
            "predecessor_consent_id": self.predecessor_consent_id,
            "predecessor_sha256": self.predecessor_sha256,
            "pseudonymization_claim": self.pseudonymization_claim,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "record_kind": self.record_kind,
            "recorded_at_utc": self.recorded_at_utc,
            "retain_pseudonymous_evidence_after_profile_deletion": (
                self.retain_pseudonymous_evidence_after_profile_deletion
            ),
            "retention_policy": self.retention_policy.value,
            "retention_until_utc": self.retention_until_utc,
            "revision": self.revision,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scopes": [scope.value for scope in self.scopes],
            "state": self.state.value,
            "withdrawal_policy": self.withdrawal_policy.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "consent_id": self.consent_id}

    def canonical_bytes(self) -> bytes:
        _require_consent_integrity(self)
        return _canonical_json_bytes(self.as_dict())

    @property
    def consent_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, payload: object) -> ConsentRecordV1:
        expected = {
            "consent_id",
            "direct_identity_policy",
            "export_permission",
            "predecessor_consent_id",
            "predecessor_sha256",
            "pseudonymization_claim",
            "pseudonymous_profile_id",
            "record_kind",
            "recorded_at_utc",
            "retain_pseudonymous_evidence_after_profile_deletion",
            "retention_policy",
            "retention_until_utc",
            "revision",
            "schema_id",
            "schema_version",
            "scopes",
            "state",
            "withdrawal_policy",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("consent record fields differ")
        raw_scopes = payload["scopes"]
        if not isinstance(raw_scopes, list):
            raise TypeError("consent scopes must be an array")
        record = cls(
            pseudonymous_profile_id=_exact_text(
                payload["pseudonymous_profile_id"],
                "consent profile ID",
            ),
            scopes=tuple(ConsentScopeV1(item) for item in raw_scopes),
            recorded_at_utc=_exact_text(
                payload["recorded_at_utc"],
                "consent record time",
            ),
            state=ConsentStateV1(payload["state"]),
            retention_policy=EvidenceRetentionPolicyV1(
                payload["retention_policy"]
            ),
            retention_until_utc=(
                None
                if payload["retention_until_utc"] is None
                else _exact_text(
                    payload["retention_until_utc"],
                    "consent retention end",
                )
            ),
            retain_pseudonymous_evidence_after_profile_deletion=payload[
                "retain_pseudonymous_evidence_after_profile_deletion"
            ],  # type: ignore[arg-type]
            export_permission=EvidenceExportPermissionV1(
                payload["export_permission"]
            ),
            withdrawal_policy=WithdrawalPolicyV1(payload["withdrawal_policy"]),
            revision=payload["revision"],  # type: ignore[arg-type]
            predecessor_consent_id=(
                None
                if payload["predecessor_consent_id"] is None
                else _exact_text(
                    payload["predecessor_consent_id"],
                    "predecessor consent ID",
                )
            ),
            predecessor_sha256=(
                None
                if payload["predecessor_sha256"] is None
                else _exact_text(
                    payload["predecessor_sha256"],
                    "predecessor consent digest",
                )
            ),
            schema_id=_exact_text(payload["schema_id"], "consent schema ID"),
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            record_kind=_exact_text(payload["record_kind"], "consent record kind"),
            pseudonymization_claim=_exact_text(
                payload["pseudonymization_claim"],
                "consent pseudonymization claim",
            ),
            direct_identity_policy=_exact_text(
                payload["direct_identity_policy"],
                "consent direct-identity policy",
            ),
            _construction_token=_CONSENT_TOKEN,
        )
        if record.consent_id != payload["consent_id"]:
            raise ValueError("consent ID differs from canonical content")
        return record

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ConsentRecordV1:
        record = cls.from_dict(_canonical_object(raw, "consent record"))
        if record.canonical_bytes() != raw:
            raise ValueError("consent record changed during restoration")
        return record


def create_consent_record(
    *,
    pseudonymous_profile_id: str,
    scopes: tuple[ConsentScopeV1, ...],
    recorded_at_utc: str,
    retention_policy: EvidenceRetentionPolicyV1,
    retention_until_utc: str | None,
    retain_pseudonymous_evidence_after_profile_deletion: bool,
    export_permission: EvidenceExportPermissionV1,
    withdrawal_policy: WithdrawalPolicyV1,
) -> ConsentRecordV1:
    """Create an explicit first grant; no scope or permission has a default."""

    return ConsentRecordV1(
        pseudonymous_profile_id=pseudonymous_profile_id,
        scopes=scopes,
        recorded_at_utc=recorded_at_utc,
        state=ConsentStateV1.GRANTED,
        retention_policy=retention_policy,
        retention_until_utc=retention_until_utc,
        retain_pseudonymous_evidence_after_profile_deletion=(
            retain_pseudonymous_evidence_after_profile_deletion
        ),
        export_permission=export_permission,
        withdrawal_policy=withdrawal_policy,
        revision=1,
        predecessor_consent_id=None,
        predecessor_sha256=None,
        _construction_token=_CONSENT_TOKEN,
    )


def revise_consent_record(
    predecessor: ConsentRecordV1,
    *,
    scopes: tuple[ConsentScopeV1, ...],
    recorded_at_utc: str,
    retention_policy: EvidenceRetentionPolicyV1,
    retention_until_utc: str | None,
    retain_pseudonymous_evidence_after_profile_deletion: bool,
    export_permission: EvidenceExportPermissionV1,
    withdrawal_policy: WithdrawalPolicyV1,
) -> ConsentRecordV1:
    """Create an exact granted successor; the predecessor remains immutable."""

    if type(predecessor) is not ConsentRecordV1:
        raise TypeError("consent revision requires a ConsentRecordV1 predecessor")
    rebuilt_predecessor = ConsentRecordV1.from_json_bytes(
        predecessor.canonical_bytes()
    )
    if rebuilt_predecessor != predecessor:
        raise ValueError("consent predecessor differs from its canonical identity")
    predecessor = rebuilt_predecessor
    _utc(recorded_at_utc, "consent revision time")
    if recorded_at_utc <= predecessor.recorded_at_utc:
        raise ValueError("consent successor time must advance")
    return ConsentRecordV1(
        pseudonymous_profile_id=predecessor.pseudonymous_profile_id,
        scopes=scopes,
        recorded_at_utc=recorded_at_utc,
        state=ConsentStateV1.GRANTED,
        retention_policy=retention_policy,
        retention_until_utc=retention_until_utc,
        retain_pseudonymous_evidence_after_profile_deletion=(
            retain_pseudonymous_evidence_after_profile_deletion
        ),
        export_permission=export_permission,
        withdrawal_policy=withdrawal_policy,
        revision=predecessor.revision + 1,
        predecessor_consent_id=predecessor.consent_id,
        predecessor_sha256=predecessor.consent_sha256,
        _construction_token=_CONSENT_TOKEN,
    )


def withdraw_consent(
    predecessor: ConsentRecordV1,
    *,
    recorded_at_utc: str,
) -> ConsentRecordV1:
    """Record withdrawal as a successor without editing the earlier grant."""

    if type(predecessor) is not ConsentRecordV1:
        raise TypeError("consent withdrawal requires a ConsentRecordV1 predecessor")
    rebuilt_predecessor = ConsentRecordV1.from_json_bytes(
        predecessor.canonical_bytes()
    )
    if rebuilt_predecessor != predecessor:
        raise ValueError("consent predecessor differs from its canonical identity")
    predecessor = rebuilt_predecessor
    if predecessor.state is ConsentStateV1.WITHDRAWN:
        raise ValueError("consent is already withdrawn")
    _utc(recorded_at_utc, "consent withdrawal time")
    if recorded_at_utc <= predecessor.recorded_at_utc:
        raise ValueError("consent withdrawal time must advance")
    return ConsentRecordV1(
        pseudonymous_profile_id=predecessor.pseudonymous_profile_id,
        scopes=predecessor.scopes,
        recorded_at_utc=recorded_at_utc,
        state=ConsentStateV1.WITHDRAWN,
        retention_policy=predecessor.retention_policy,
        retention_until_utc=predecessor.retention_until_utc,
        retain_pseudonymous_evidence_after_profile_deletion=(
            predecessor.retain_pseudonymous_evidence_after_profile_deletion
        ),
        export_permission=predecessor.export_permission,
        withdrawal_policy=predecessor.withdrawal_policy,
        revision=predecessor.revision + 1,
        predecessor_consent_id=predecessor.consent_id,
        predecessor_sha256=predecessor.consent_sha256,
        _construction_token=_CONSENT_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class RetentionAfterDeletionDecisionV1:
    pseudonymous_profile_id: str
    consent_id: str
    consent_sha256: str
    required_scope: ConsentScopeV1
    decision_time_utc: str
    allowed: bool
    status: ConsentDecisionStatusV1
    reason: ConsentDecisionReasonV1
    _construction_token: InitVar[object]
    schema_id: str = RETENTION_AFTER_DELETION_DECISION_SCHEMA_ID
    schema_version: int = RETENTION_AFTER_DELETION_DECISION_SCHEMA_VERSION
    decision_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _RETENTION_DECISION_TOKEN:
            raise TypeError("retention decisions require the governed evaluator")
        require_profile_id(self.pseudonymous_profile_id)
        _consent_id(self.consent_id, "retention decision consent ID")
        _sha256(self.consent_sha256, "retention decision consent digest")
        if type(self.required_scope) is not ConsentScopeV1:
            raise TypeError("retention decision scope is invalid")
        _utc(self.decision_time_utc, "retention decision time")
        _validate_decision_outcome(self.allowed, self.status, self.reason)
        if self.allowed and self.reason not in {
            ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT,
            ConsentDecisionReasonV1.AUTHORIZED_PRIOR_RETENTION_AFTER_WITHDRAWAL,
        }:
            raise ValueError("retention authorization reason differs")
        if (
            self.schema_id != RETENTION_AFTER_DELETION_DECISION_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version
            != RETENTION_AFTER_DELETION_DECISION_SCHEMA_VERSION
        ):
            raise ValueError("retention decision schema differs")
        object.__setattr__(
            self,
            "decision_id",
            CONSENT_DECISION_ID_PREFIX + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "consent_id": self.consent_id,
            "consent_sha256": self.consent_sha256,
            "decision_kind": "RETENTION_AFTER_PROFILE_DELETION",
            "decision_time_utc": self.decision_time_utc,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "reason": self.reason.value,
            "required_scope": self.required_scope.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "decision_id": self.decision_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def decision_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceExportDecisionV1:
    pseudonymous_profile_id: str
    consent_id: str
    consent_sha256: str
    required_scope: ConsentScopeV1
    requested_export: EvidenceExportClassV1
    decision_time_utc: str
    allowed: bool
    status: ConsentDecisionStatusV1
    reason: ConsentDecisionReasonV1
    _construction_token: InitVar[object]
    schema_id: str = EVIDENCE_EXPORT_DECISION_SCHEMA_ID
    schema_version: int = EVIDENCE_EXPORT_DECISION_SCHEMA_VERSION
    decision_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _EXPORT_DECISION_TOKEN:
            raise TypeError("export decisions require the governed evaluator")
        require_profile_id(self.pseudonymous_profile_id)
        _consent_id(self.consent_id, "export decision consent ID")
        _sha256(self.consent_sha256, "export decision consent digest")
        if type(self.required_scope) is not ConsentScopeV1:
            raise TypeError("export decision scope is invalid")
        if type(self.requested_export) is not EvidenceExportClassV1:
            raise TypeError("requested export class is invalid")
        _utc(self.decision_time_utc, "export decision time")
        _validate_decision_outcome(self.allowed, self.status, self.reason)
        if self.allowed and (
            self.requested_export
            is not EvidenceExportClassV1.PSEUDONYMOUS_REDACTED_EVIDENCE
            or self.reason
            is not ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT
        ):
            raise ValueError("export authorization exceeds the redacted consent scope")
        if (
            self.schema_id != EVIDENCE_EXPORT_DECISION_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != EVIDENCE_EXPORT_DECISION_SCHEMA_VERSION
        ):
            raise ValueError("export decision schema differs")
        object.__setattr__(
            self,
            "decision_id",
            CONSENT_DECISION_ID_PREFIX + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "consent_id": self.consent_id,
            "consent_sha256": self.consent_sha256,
            "decision_kind": "EVIDENCE_EXPORT",
            "decision_time_utc": self.decision_time_utc,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "reason": self.reason.value,
            "requested_export": self.requested_export.value,
            "required_scope": self.required_scope.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "decision_id": self.decision_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def decision_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProfileDeletionDecisionV1:
    """Exact authorization consumed by the local identity-mapping deletion boundary."""

    pseudonymous_profile_id: str
    consent_id: str
    consent_sha256: str
    required_scope: ConsentScopeV1
    requested_pseudonymous_evidence_retention: bool
    retention_decision_id: str | None
    retention_decision_sha256: str | None
    retention_decision_allowed: bool | None
    retention_decision_reason: ConsentDecisionReasonV1 | None
    decision_time_utc: str
    allowed: bool
    status: ConsentDecisionStatusV1
    reason: ConsentDecisionReasonV1
    _construction_token: InitVar[object]
    schema_id: str = PROFILE_DELETION_DECISION_SCHEMA_ID
    schema_version: int = PROFILE_DELETION_DECISION_SCHEMA_VERSION
    decision_id: str = field(init=False)

    def __post_init__(self, _construction_token: object) -> None:
        if _construction_token is not _DELETION_DECISION_TOKEN:
            raise TypeError("profile deletion decisions require the governed evaluator")
        require_profile_id(self.pseudonymous_profile_id)
        _consent_id(self.consent_id, "profile deletion consent ID")
        _sha256(self.consent_sha256, "profile deletion consent digest")
        if type(self.required_scope) is not ConsentScopeV1:
            raise TypeError("profile deletion decision scope is invalid")
        requested = _exact_bool(
            self.requested_pseudonymous_evidence_retention,
            "requested pseudonymous evidence retention",
        )
        retention_binding = (
            self.retention_decision_id,
            self.retention_decision_sha256,
            self.retention_decision_allowed,
            self.retention_decision_reason,
        )
        bound_retention: RetentionAfterDeletionDecisionV1 | None = None
        if requested:
            if any(value is None for value in retention_binding):
                raise ValueError(
                    "retaining evidence requires a complete bound retention decision"
                )
            if (
                type(self.retention_decision_id) is not str
                or not self.retention_decision_id.startswith(CONSENT_DECISION_ID_PREFIX)
            ):
                raise ValueError("bound retention decision ID is invalid")
            _sha256(
                self.retention_decision_sha256,
                "bound retention decision digest",
            )
            bound_allowed = _exact_bool(
                self.retention_decision_allowed,
                "bound retention decision allowed",
            )
            if type(self.retention_decision_reason) is not ConsentDecisionReasonV1:
                raise TypeError("bound retention decision reason is invalid")
            bound_retention = RetentionAfterDeletionDecisionV1(
                pseudonymous_profile_id=self.pseudonymous_profile_id,
                consent_id=self.consent_id,
                consent_sha256=self.consent_sha256,
                required_scope=self.required_scope,
                decision_time_utc=self.decision_time_utc,
                allowed=bound_allowed,
                status=(
                    ConsentDecisionStatusV1.AUTHORIZED
                    if bound_allowed
                    else ConsentDecisionStatusV1.REFUSED
                ),
                reason=self.retention_decision_reason,
                _construction_token=_RETENTION_DECISION_TOKEN,
            )
            if (
                bound_retention.decision_id != self.retention_decision_id
                or bound_retention.decision_sha256
                != self.retention_decision_sha256
            ):
                raise ValueError(
                    "bound retention decision identity or outcome differs"
                )
        elif any(value is not None for value in retention_binding):
            raise ValueError("non-retaining deletion cannot bind a retention decision")
        _utc(self.decision_time_utc, "profile deletion decision time")
        _validate_decision_outcome(self.allowed, self.status, self.reason)
        if not requested and (
            not self.allowed
            or self.reason
            is not ConsentDecisionReasonV1.PROFILE_DELETION_WITHOUT_EVIDENCE_RETENTION
        ):
            raise ValueError("non-retaining profile deletion must be authorized")
        if requested:
            if bound_retention is None:  # pragma: no cover - guarded above
                raise ValueError("retaining deletion lacks its semantic binding")
            if self.allowed != bound_retention.allowed:
                raise ValueError(
                    "retaining deletion and bound retention outcomes disagree"
                )
            if self.allowed:
                if self.reason is not (
                    ConsentDecisionReasonV1.PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION
                ):
                    raise ValueError("retaining deletion authorization reason differs")
            elif self.reason is not bound_retention.reason:
                raise ValueError(
                    "retaining deletion refusal reason differs from bound retention"
                )
        if (
            self.schema_id != PROFILE_DELETION_DECISION_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != PROFILE_DELETION_DECISION_SCHEMA_VERSION
        ):
            raise ValueError("profile deletion decision schema differs")
        object.__setattr__(
            self,
            "decision_id",
            CONSENT_DECISION_ID_PREFIX + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "consent_id": self.consent_id,
            "consent_sha256": self.consent_sha256,
            "decision_kind": "PROFILE_DELETION",
            "decision_time_utc": self.decision_time_utc,
            "pseudonymous_profile_id": self.pseudonymous_profile_id,
            "reason": self.reason.value,
            "requested_pseudonymous_evidence_retention": (
                self.requested_pseudonymous_evidence_retention
            ),
            "required_scope": self.required_scope.value,
            "retention_decision_id": self.retention_decision_id,
            "retention_decision_allowed": self.retention_decision_allowed,
            "retention_decision_reason": (
                None
                if self.retention_decision_reason is None
                else self.retention_decision_reason.value
            ),
            "retention_decision_sha256": self.retention_decision_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "decision_id": self.decision_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def decision_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _decision_status(
    allowed: bool,
) -> ConsentDecisionStatusV1:
    return (
        ConsentDecisionStatusV1.AUTHORIZED
        if allowed
        else ConsentDecisionStatusV1.REFUSED
    )


def _validate_decision_request(
    consent: ConsentRecordV1,
    required_scope: ConsentScopeV1,
    decision_time_utc: str,
) -> ConsentRecordV1:
    if type(consent) is not ConsentRecordV1:
        raise TypeError("consent policy evaluation requires ConsentRecordV1")
    validated = ConsentRecordV1.from_json_bytes(consent.canonical_bytes())
    if validated != consent:
        raise ValueError("consent record differs from its canonical identity")
    if type(required_scope) is not ConsentScopeV1:
        raise TypeError("consent policy scope is invalid")
    _utc(decision_time_utc, "consent policy decision time")
    if decision_time_utc < validated.recorded_at_utc:
        raise ValueError("consent policy decision cannot precede its consent revision")
    return validated


def _retention_reason(
    consent: ConsentRecordV1,
    *,
    required_scope: ConsentScopeV1,
    decision_time_utc: str,
) -> ConsentDecisionReasonV1:
    if (
        consent.state is ConsentStateV1.WITHDRAWN
        and consent.withdrawal_policy
        is WithdrawalPolicyV1.REVOKE_FUTURE_RETENTION_AND_EXPORT
    ):
        return ConsentDecisionReasonV1.CONSENT_WITHDRAWN
    if required_scope not in consent.scopes:
        return ConsentDecisionReasonV1.SCOPE_NOT_GRANTED
    if not consent.retain_pseudonymous_evidence_after_profile_deletion:
        return ConsentDecisionReasonV1.RETENTION_AFTER_DELETION_NOT_GRANTED
    if consent.retention_policy is EvidenceRetentionPolicyV1.DELETE_WITH_PROFILE:
        return ConsentDecisionReasonV1.RETENTION_POLICY_PROHIBITS
    if (
        consent.retention_policy is EvidenceRetentionPolicyV1.RETAIN_UNTIL_UTC
        and consent.retention_until_utc is not None
        and decision_time_utc > consent.retention_until_utc
    ):
        return ConsentDecisionReasonV1.RETENTION_EXPIRED
    if consent.state is ConsentStateV1.WITHDRAWN:
        return ConsentDecisionReasonV1.AUTHORIZED_PRIOR_RETENTION_AFTER_WITHDRAWAL
    return ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT


def decide_retention_after_profile_deletion(
    current_consent: ConsentRecordV1,
    *,
    required_scope: ConsentScopeV1,
    decision_time_utc: str,
) -> RetentionAfterDeletionDecisionV1:
    """Evaluate retention against the caller-asserted current consent revision.

    This pure policy function does not discover later revisions.  Its caller must
    resolve the authoritative current head before evaluation.
    """

    consent = _validate_decision_request(
        current_consent,
        required_scope,
        decision_time_utc,
    )
    reason = _retention_reason(
        consent,
        required_scope=required_scope,
        decision_time_utc=decision_time_utc,
    )
    allowed = reason in _AUTHORIZED_REASONS
    return RetentionAfterDeletionDecisionV1(
        pseudonymous_profile_id=consent.pseudonymous_profile_id,
        consent_id=consent.consent_id,
        consent_sha256=consent.consent_sha256,
        required_scope=required_scope,
        decision_time_utc=decision_time_utc,
        allowed=allowed,
        status=_decision_status(allowed),
        reason=reason,
        _construction_token=_RETENTION_DECISION_TOKEN,
    )


def decide_evidence_export(
    current_consent: ConsentRecordV1,
    *,
    required_scope: ConsentScopeV1,
    requested_export: EvidenceExportClassV1,
    decision_time_utc: str,
) -> EvidenceExportDecisionV1:
    """Evaluate consent eligibility for one explicit export request.

    The supplied record must be the caller-asserted current consent head; this pure
    evaluator does not search for later revisions.  This decision is not an export
    artifact and does not prove that redaction,
    source-evidence availability, or any later deletion-receipt condition passed.
    WO37-E must bind those independent checks before writing bytes.  Identity material
    is categorically ineligible here.
    """

    consent = _validate_decision_request(
        current_consent,
        required_scope,
        decision_time_utc,
    )
    if type(requested_export) is not EvidenceExportClassV1:
        raise TypeError("requested export class is invalid")
    if requested_export is EvidenceExportClassV1.DIRECT_IDENTITY:
        reason = ConsentDecisionReasonV1.DIRECT_IDENTITY_EXPORT_PROHIBITED
    elif requested_export is EvidenceExportClassV1.IDENTITY_MAPPING:
        reason = ConsentDecisionReasonV1.IDENTITY_MAPPING_EXPORT_PROHIBITED
    elif requested_export is EvidenceExportClassV1.UNREDACTED_EVIDENCE:
        reason = ConsentDecisionReasonV1.UNREDACTED_EXPORT_PROHIBITED
    elif consent.state is ConsentStateV1.WITHDRAWN:
        reason = ConsentDecisionReasonV1.CONSENT_WITHDRAWN
    elif required_scope not in consent.scopes:
        reason = ConsentDecisionReasonV1.SCOPE_NOT_GRANTED
    elif (
        consent.retention_policy is EvidenceRetentionPolicyV1.RETAIN_UNTIL_UTC
        and consent.retention_until_utc is not None
        and decision_time_utc > consent.retention_until_utc
    ):
        reason = ConsentDecisionReasonV1.RETENTION_EXPIRED
    elif (
        consent.export_permission
        is not EvidenceExportPermissionV1.PSEUDONYMOUS_REDACTED_EVIDENCE_ONLY
    ):
        reason = ConsentDecisionReasonV1.EXPORT_NOT_GRANTED
    else:
        reason = ConsentDecisionReasonV1.AUTHORIZED_BY_ACTIVE_CONSENT
    allowed = reason in _AUTHORIZED_REASONS
    return EvidenceExportDecisionV1(
        pseudonymous_profile_id=consent.pseudonymous_profile_id,
        consent_id=consent.consent_id,
        consent_sha256=consent.consent_sha256,
        required_scope=required_scope,
        requested_export=requested_export,
        decision_time_utc=decision_time_utc,
        allowed=allowed,
        status=_decision_status(allowed),
        reason=reason,
        _construction_token=_EXPORT_DECISION_TOKEN,
    )


def decide_profile_deletion(
    current_consent: ConsentRecordV1,
    *,
    required_scope: ConsentScopeV1,
    requested_pseudonymous_evidence_retention: bool,
    decision_time_utc: str,
) -> ProfileDeletionDecisionV1:
    """Evaluate deletion against the caller-asserted current consent revision."""

    consent = _validate_decision_request(
        current_consent,
        required_scope,
        decision_time_utc,
    )
    requested = _exact_bool(
        requested_pseudonymous_evidence_retention,
        "requested pseudonymous evidence retention",
    )
    retention_decision: RetentionAfterDeletionDecisionV1 | None = None
    if requested:
        retention_decision = decide_retention_after_profile_deletion(
            consent,
            required_scope=required_scope,
            decision_time_utc=decision_time_utc,
        )
        allowed = retention_decision.allowed
        reason = (
            ConsentDecisionReasonV1.PROFILE_DELETION_WITH_AUTHORIZED_PSEUDONYMOUS_RETENTION
            if allowed
            else retention_decision.reason
        )
    else:
        allowed = True
        reason = (
            ConsentDecisionReasonV1.PROFILE_DELETION_WITHOUT_EVIDENCE_RETENTION
        )
    return ProfileDeletionDecisionV1(
        pseudonymous_profile_id=consent.pseudonymous_profile_id,
        consent_id=consent.consent_id,
        consent_sha256=consent.consent_sha256,
        required_scope=required_scope,
        requested_pseudonymous_evidence_retention=requested,
        retention_decision_id=(
            None if retention_decision is None else retention_decision.decision_id
        ),
        retention_decision_sha256=(
            None if retention_decision is None else retention_decision.decision_sha256
        ),
        retention_decision_allowed=(
            None if retention_decision is None else retention_decision.allowed
        ),
        retention_decision_reason=(
            None if retention_decision is None else retention_decision.reason
        ),
        decision_time_utc=decision_time_utc,
        allowed=allowed,
        status=_decision_status(allowed),
        reason=reason,
        _construction_token=_DELETION_DECISION_TOKEN,
    )


def validate_profile_deletion_decision(
    decision: ProfileDeletionDecisionV1,
) -> ProfileDeletionDecisionV1:
    """Return a rebuilt exact decision, rejecting stale or force-mutated state."""

    if type(decision) is not ProfileDeletionDecisionV1:
        raise TypeError("profile deletion validation requires its exact decision type")
    validated = ProfileDeletionDecisionV1(
        pseudonymous_profile_id=decision.pseudonymous_profile_id,
        consent_id=decision.consent_id,
        consent_sha256=decision.consent_sha256,
        required_scope=decision.required_scope,
        requested_pseudonymous_evidence_retention=(
            decision.requested_pseudonymous_evidence_retention
        ),
        retention_decision_id=decision.retention_decision_id,
        retention_decision_sha256=decision.retention_decision_sha256,
        retention_decision_allowed=decision.retention_decision_allowed,
        retention_decision_reason=decision.retention_decision_reason,
        decision_time_utc=decision.decision_time_utc,
        allowed=decision.allowed,
        status=decision.status,
        reason=decision.reason,
        schema_id=decision.schema_id,
        schema_version=decision.schema_version,
        _construction_token=_DELETION_DECISION_TOKEN,
    )
    if validated != decision:
        raise ValueError(
            "profile deletion decision differs from its canonical identity"
        )
    return validated


__all__ = [
    "CONSENT_DECISION_ID_PREFIX",
    "CONSENT_ID_PREFIX",
    "CONSENT_RECORD_KIND",
    "CONSENT_RECORD_SCHEMA_ID",
    "CONSENT_RECORD_SCHEMA_VERSION",
    "CURRENT_CONSENT_AUTHORITY_POLICY",
    "DIRECT_IDENTITY_POLICY",
    "EVIDENCE_EXPORT_DECISION_SCHEMA_ID",
    "EVIDENCE_EXPORT_DECISION_SCHEMA_VERSION",
    "PROFILE_DELETION_DECISION_SCHEMA_ID",
    "PROFILE_DELETION_DECISION_SCHEMA_VERSION",
    "PSEUDONYMIZATION_CLAIM",
    "RETENTION_AFTER_DELETION_DECISION_SCHEMA_ID",
    "RETENTION_AFTER_DELETION_DECISION_SCHEMA_VERSION",
    "ConsentDecisionReasonV1",
    "ConsentDecisionStatusV1",
    "ConsentRecordV1",
    "ConsentScopeV1",
    "ConsentStateV1",
    "EvidenceExportClassV1",
    "EvidenceExportDecisionV1",
    "EvidenceExportPermissionV1",
    "EvidenceRetentionPolicyV1",
    "ProfileDeletionDecisionV1",
    "RetentionAfterDeletionDecisionV1",
    "WithdrawalPolicyV1",
    "create_consent_record",
    "decide_evidence_export",
    "decide_profile_deletion",
    "decide_retention_after_profile_deletion",
    "revise_consent_record",
    "validate_profile_deletion_decision",
    "withdraw_consent",
]
