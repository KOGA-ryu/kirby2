"""Fail-closed observation and reveal policies for replay microscope queries."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum


OBSERVATION_POLICY_SCHEMA_ID = "KIRBY2_MICROSCOPE_OBSERVATION_POLICY_V1"
OBSERVATION_POLICY_SCHEMA_VERSION = 1
SOURCE_CAPABILITY_MANIFEST_SCHEMA_ID = (
    "KIRBY2_MICROSCOPE_SOURCE_CAPABILITY_MANIFEST_V1"
)
SOURCE_CAPABILITY_MANIFEST_SCHEMA_VERSION = 1
AS_OBSERVED_POLICY_ID = "MICROSCOPE_AS_OBSERVED_V1"
POSTMORTEM_POLICY_ID = "MICROSCOPE_POSTMORTEM_V1"

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")


class ObservationMode(str, Enum):
    AS_OBSERVED = "AS_OBSERVED"
    POSTMORTEM = "POSTMORTEM"


class ObservedEvidenceKind(str, Enum):
    CLIENT_DELIVERED = "CLIENT_DELIVERED"
    RECORDED_DECISION_SNAPSHOT = "RECORDED_DECISION_SNAPSHOT"


class RevealCapability(str, Enum):
    GROUND_TRUTH = "GROUND_TRUTH"
    HIDDEN_STATE = "HIDDEN_STATE"


class SourceCapabilityAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class SourceCapabilityUnavailableReason(str, Enum):
    NOT_RECORDED_BY_SOURCE = "NOT_RECORDED_BY_SOURCE"
    SOURCE_SCHEMA_UNSUPPORTED = "SOURCE_SCHEMA_UNSUPPORTED"


class RevealAvailability(str, Enum):
    NOT_REQUESTED = "NOT_REQUESTED"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class RevealUnavailableReason(str, Enum):
    SOURCE_CAPABILITY_UNAVAILABLE = "SOURCE_CAPABILITY_UNAVAILABLE"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    AUTHORIZATION_BINDING_MISMATCH = "AUTHORIZATION_BINDING_MISMATCH"
    AUTHORIZATION_SCOPE_MISMATCH = "AUTHORIZATION_SCOPE_MISMATCH"
    REVEAL_SOURCE_MISMATCH = "REVEAL_SOURCE_MISMATCH"


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """A mode label whose policy identity is derived, never caller supplied."""

    mode: ObservationMode
    schema_id: str = OBSERVATION_POLICY_SCHEMA_ID
    schema_version: int = OBSERVATION_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.mode) is not ObservationMode:
            raise TypeError("observation mode is invalid")
        if (
            self.schema_id != OBSERVATION_POLICY_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != OBSERVATION_POLICY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported observation policy schema")

    @property
    def policy_id(self) -> str:
        return observation_policy_id(self.mode)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "policy_id": self.policy_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "observation_mode": self.mode.value,
            "observation_policy_id": self.policy_id,
            "observation_policy_schema_id": self.schema_id,
            "observation_policy_schema_version": str(self.schema_version),
        }


@dataclass(frozen=True, slots=True)
class SourceCapabilityEvidence:
    """One capability backed by exact source artifact bytes or typed absence."""

    capability: RevealCapability
    availability: SourceCapabilityAvailability
    source_artifact_id: str | None = None
    source_artifact_sha256: str | None = None
    unavailable_reason: SourceCapabilityUnavailableReason | None = None

    def __post_init__(self) -> None:
        if type(self.capability) is not RevealCapability:
            raise TypeError("source capability evidence capability is invalid")
        if type(self.availability) is not SourceCapabilityAvailability:
            raise TypeError("source capability evidence availability is invalid")
        if self.availability is SourceCapabilityAvailability.AVAILABLE:
            _validate_identifier(self.source_artifact_id, "source capability artifact ID")
            if (
                not isinstance(self.source_artifact_sha256, str)
                or not _SHA256.fullmatch(self.source_artifact_sha256)
            ):
                raise ValueError("source capability artifact digest is invalid")
            if self.unavailable_reason is not None:
                raise ValueError("available source capability carries an absence reason")
            return
        if self.source_artifact_id is not None or self.source_artifact_sha256 is not None:
            raise ValueError("unavailable source capability carries artifact identity")
        if type(self.unavailable_reason) is not SourceCapabilityUnavailableReason:
            raise ValueError("unavailable source capability requires a typed reason")

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "capability": self.capability.value,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "unavailable_reason": (
                None
                if self.unavailable_reason is None
                else self.unavailable_reason.value
            ),
        }


@dataclass(frozen=True, slots=True)
class ReplaySourceCapabilityManifest:
    """Canonical source-evidence manifest consumed at the trusted adapter boundary.

    This object is provenance evidence, not a Python secrecy primitive. A source
    adapter must construct it from verified immutable artifacts. Query enforcement
    then binds exact artifact digests, the manifest digest, and reveal evidence.
    """

    source_run_id: str
    source_event_sha256: str
    source_schema_id: str
    source_schema_version: int
    capability_evidence: tuple[SourceCapabilityEvidence, ...]
    schema_id: str = SOURCE_CAPABILITY_MANIFEST_SCHEMA_ID
    schema_version: int = SOURCE_CAPABILITY_MANIFEST_SCHEMA_VERSION
    manifest_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_source_identity(self.source_run_id, self.source_event_sha256)
        _validate_identifier(self.source_schema_id, "replay source schema ID")
        if type(self.source_schema_version) is not int or self.source_schema_version <= 0:
            raise ValueError("replay source schema version must be positive")
        if (
            self.schema_id != SOURCE_CAPABILITY_MANIFEST_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != SOURCE_CAPABILITY_MANIFEST_SCHEMA_VERSION
        ):
            raise ValueError("unsupported source capability manifest schema")
        evidence = _canonical_capability_evidence(self.capability_evidence)
        inventory = tuple(item.capability for item in evidence)
        if len(inventory) != len(set(inventory)):
            raise ValueError("source capability manifest contains duplicate capabilities")
        if set(inventory) != set(RevealCapability):
            raise ValueError("source capability manifest must account for every capability")
        artifact_digests: dict[str, str] = {}
        for item in evidence:
            if item.availability is not SourceCapabilityAvailability.AVAILABLE:
                continue
            artifact_id = item.source_artifact_id
            artifact_sha256 = item.source_artifact_sha256
            if artifact_id is None or artifact_sha256 is None:  # pragma: no cover
                raise RuntimeError("validated capability artifact identity disappeared")
            previous = artifact_digests.setdefault(artifact_id, artifact_sha256)
            if previous != artifact_sha256:
                raise ValueError("one source artifact ID maps to multiple digests")
        object.__setattr__(self, "capability_evidence", evidence)
        object.__setattr__(self, "manifest_sha256", _canonical_sha256(self._manifest_dict()))

    @property
    def capabilities(self) -> tuple[RevealCapability, ...]:
        return tuple(
            item.capability
            for item in self.capability_evidence
            if item.availability is SourceCapabilityAvailability.AVAILABLE
        )

    def _manifest_dict(self) -> dict[str, object]:
        return {
            "capability_evidence": [item.as_dict() for item in self.capability_evidence],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "source_schema_id": self.source_schema_id,
            "source_schema_version": self.source_schema_version,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._manifest_dict(), "manifest_sha256": self.manifest_sha256}


@dataclass(frozen=True, slots=True)
class RevealAuthorization:
    """An out-of-band trusted bearer grant bound to exact evidence bytes.

    Grant issuance belongs to the session/reveal owner outside this query module.
    Construction validates a supplied grant's closed schema and bindings; it does
    not authenticate arbitrary same-process Python code or mint user consent.
    """

    authorization_id: str
    source_run_id: str
    source_event_sha256: str
    observed_evidence_sha256: str
    source_capability_manifest_sha256: str
    reveal_evidence_sha256: str
    capabilities: tuple[RevealCapability, ...]
    policy_id: str = POSTMORTEM_POLICY_ID
    schema_id: str = OBSERVATION_POLICY_SCHEMA_ID
    schema_version: int = OBSERVATION_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_identifier(self.authorization_id, "reveal authorization ID")
        _validate_source_identity(self.source_run_id, self.source_event_sha256)
        if not _SHA256.fullmatch(self.observed_evidence_sha256):
            raise ValueError("reveal authorization observed evidence digest is invalid")
        if not _SHA256.fullmatch(self.source_capability_manifest_sha256):
            raise ValueError("reveal authorization manifest digest is invalid")
        if not _SHA256.fullmatch(self.reveal_evidence_sha256):
            raise ValueError("reveal authorization evidence digest is invalid")
        canonical = _canonical_capabilities(self.capabilities)
        if not canonical:
            raise ValueError("reveal authorization requires at least one capability")
        object.__setattr__(self, "capabilities", canonical)
        if self.policy_id != POSTMORTEM_POLICY_ID:
            raise ValueError("reveal authorization must bind the postmortem policy")
        if (
            self.schema_id != OBSERVATION_POLICY_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != OBSERVATION_POLICY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported reveal authorization schema")

    def as_dict(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "capabilities": [item.value for item in self.capabilities],
            "observed_evidence_sha256": self.observed_evidence_sha256,
            "policy_id": self.policy_id,
            "reveal_evidence_sha256": self.reveal_evidence_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_capability_manifest_sha256": (
                self.source_capability_manifest_sha256
            ),
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
        }


def observation_policy_id(mode: ObservationMode) -> str:
    if type(mode) is not ObservationMode:
        raise TypeError("observation mode is invalid")
    if mode is ObservationMode.AS_OBSERVED:
        return AS_OBSERVED_POLICY_ID
    return POSTMORTEM_POLICY_ID


def _canonical_capability_evidence(
    values: tuple[SourceCapabilityEvidence, ...],
) -> tuple[SourceCapabilityEvidence, ...]:
    if not isinstance(values, tuple) or any(
        type(item) is not SourceCapabilityEvidence for item in values
    ):
        raise TypeError("source capability evidence inventory is invalid")
    return tuple(sorted(values, key=lambda item: item.capability.value))


def _canonical_capabilities(
    values: tuple[RevealCapability, ...],
) -> tuple[RevealCapability, ...]:
    if not isinstance(values, tuple) or any(
        type(item) is not RevealCapability for item in values
    ):
        raise TypeError("reveal capabilities must be a tuple of RevealCapability values")
    return tuple(sorted(set(values), key=lambda item: item.value))


def _validate_source_identity(source_run_id: str, source_event_sha256: str) -> None:
    if not isinstance(source_run_id, str) or not _RUN_ID.fullmatch(source_run_id):
        raise ValueError("replay source run ID is invalid")
    if (
        not isinstance(source_event_sha256, str)
        or not _SHA256.fullmatch(source_event_sha256)
    ):
        raise ValueError("replay source event digest is invalid")


def _validate_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Manifests and bearer grants are backend/issuer records, not UI construction APIs.
__all__ = [
    "AS_OBSERVED_POLICY_ID",
    "OBSERVATION_POLICY_SCHEMA_ID",
    "OBSERVATION_POLICY_SCHEMA_VERSION",
    "POSTMORTEM_POLICY_ID",
    "SOURCE_CAPABILITY_MANIFEST_SCHEMA_ID",
    "SOURCE_CAPABILITY_MANIFEST_SCHEMA_VERSION",
    "ObservationMode",
    "ObservationPolicy",
    "ObservedEvidenceKind",
    "RevealAvailability",
    "RevealCapability",
    "RevealUnavailableReason",
    "SourceCapabilityAvailability",
    "SourceCapabilityUnavailableReason",
    "observation_policy_id",
]
