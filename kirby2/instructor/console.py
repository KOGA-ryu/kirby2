"""Immutable local instructor-console artifact ledger.

The instructor console is an index over already-immutable source artifacts.  It
does not copy mutable domain objects into a second authority and it does not add
accounts, a service boundary, telemetry, synchronization, or publication.  Each
entry instead commits to the exact source artifact digest and to the metadata a
query must display when that artifact is viewed.

All mutations are functional: appending returns a new ledger value.  Sequence
numbers and predecessor digests form a strict hash chain, and ``as_of`` returns a
verified prefix at an explicit ledger point.  Every durable type supports strict
canonical JSON reload without relying on process-local state.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar


CONSOLE_SOURCE_IDENTITY_SCHEMA_ID = "KIRBY2_CONSOLE_SOURCE_IDENTITY_V1"
CONSOLE_SOURCE_IDENTITY_SCHEMA_VERSION = 1
CONSOLE_ARTIFACT_REFERENCE_SCHEMA_ID = "KIRBY2_CONSOLE_ARTIFACT_REFERENCE_V1"
CONSOLE_ARTIFACT_REFERENCE_SCHEMA_VERSION = 1
CONSOLE_LEDGER_ENTRY_SCHEMA_ID = "KIRBY2_CONSOLE_LEDGER_ENTRY_V1"
CONSOLE_LEDGER_ENTRY_SCHEMA_VERSION = 1
INSTRUCTOR_CONSOLE_LEDGER_SCHEMA_ID = "KIRBY2_INSTRUCTOR_CONSOLE_LEDGER_V1"
INSTRUCTOR_CONSOLE_LEDGER_SCHEMA_VERSION = 1

DEFAULT_INSTRUCTOR_CONSOLE_ID = "instructor-console-local-v1"
NOT_APPLICABLE_VERSION = "NOT_APPLICABLE"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
_MAX_CANONICAL_BYTES = 16 * 1024 * 1024


class ConsoleArtifactKindV1(str, Enum):
    """Closed set of immutable source artifacts visible in the console."""

    PROFILE = "PROFILE"
    ASSIGNMENT = "ASSIGNMENT"
    ASSIGNMENT_ATTEMPT = "ASSIGNMENT_ATTEMPT"
    RUBRIC = "RUBRIC"
    REVIEW = "REVIEW"
    COHORT = "COHORT"
    STUDY = "STUDY"
    AMENDMENT = "AMENDMENT"
    COMPARISON = "COMPARISON"
    MICROSCOPE_LINK = "MICROSCOPE_LINK"


class ConsoleCapabilityV1(str, Enum):
    """Strongest interpretation the referenced artifact supports."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    DESCRIPTIVE = "DESCRIPTIVE"
    CAUSAL = "CAUSAL"


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
        raise ValueError("console value is not strict canonical JSON") from error


def _pairs_without_duplicates(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("console JSON contains a duplicate object key")
        result[key] = value
    return result


def _canonical_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} decoder requires exact bytes")
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


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical identifier")
    return value


def _version(value: object, label: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{label} must be one explicit canonical version")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, label)


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be an exact boolean")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _source_kind(value: object, label: str) -> str:
    result = _identifier(value, label)
    if result != result.upper():
        raise ValueError(f"{label} must use an uppercase source namespace")
    return result


@dataclass(frozen=True, slots=True)
class ConsoleSourceIdentityV1:
    """Exact ID-and-digest identity of one immutable source dependency."""

    source_kind: str
    source_id: str
    source_sha256: str

    schema_id: ClassVar[str] = CONSOLE_SOURCE_IDENTITY_SCHEMA_ID
    schema_version: ClassVar[int] = CONSOLE_SOURCE_IDENTITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _source_kind(self.source_kind, "console source kind")
        _identifier(self.source_id, "console source ID")
        _sha256(self.source_sha256, "console source digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_sha256": self.source_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> ConsoleSourceIdentityV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "schema_id",
                    "schema_version",
                    "source_id",
                    "source_kind",
                    "source_sha256",
                }
            ),
            "console source identity",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("console source identity schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("console source identity schema version differs")
        return cls(
            source_kind=_source_kind(payload["source_kind"], "console source kind"),
            source_id=_identifier(payload["source_id"], "console source ID"),
            source_sha256=_sha256(
                payload["source_sha256"], "console source digest"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ConsoleSourceIdentityV1:
        return cls.from_dict(_canonical_object(raw, "console source identity"))


def create_console_source_identity(
    *,
    source_kind: str,
    source_id: str,
    source_bytes: bytes,
) -> ConsoleSourceIdentityV1:
    """Create an exact source identity without retaining source payload bytes."""

    if type(source_bytes) is not bytes or not source_bytes:
        raise ValueError("console source bytes must be nonempty exact bytes")
    return ConsoleSourceIdentityV1(
        source_kind=source_kind,
        source_id=source_id,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class ConsoleArtifactReferenceV1:
    """Complete, query-visible metadata for one immutable source artifact."""

    artifact_kind: ConsoleArtifactKindV1
    artifact_id: str
    artifact_sha256: str
    source_identities: tuple[ConsoleSourceIdentityV1, ...]
    content_version: str
    scoring_version: str
    model_version: str
    analysis_version: str
    sample_count: int
    uncertainty_sha256: str | None
    capability: ConsoleCapabilityV1
    consent_eligible: bool
    export_eligible: bool

    schema_id: ClassVar[str] = CONSOLE_ARTIFACT_REFERENCE_SCHEMA_ID
    schema_version: ClassVar[int] = CONSOLE_ARTIFACT_REFERENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.artifact_kind) is not ConsoleArtifactKindV1:
            raise TypeError("console artifact kind must be ConsoleArtifactKindV1")
        _identifier(self.artifact_id, "console artifact ID")
        _sha256(self.artifact_sha256, "console artifact digest")
        if type(self.source_identities) is not tuple or not self.source_identities:
            raise ValueError("console artifact must identify at least one exact source")
        if any(
            type(item) is not ConsoleSourceIdentityV1
            for item in self.source_identities
        ):
            raise TypeError("console source identities contain an invalid record")
        canonical_sources = tuple(
            sorted(
                set(self.source_identities),
                key=lambda item: item.canonical_bytes(),
            )
        )
        if canonical_sources != self.source_identities:
            raise ValueError(
                "console source identities must be unique and canonically ordered"
            )
        _version(self.content_version, "console content version")
        _version(self.scoring_version, "console scoring version")
        _version(self.model_version, "console model version")
        _version(self.analysis_version, "console analysis version")
        _nonnegative_int(self.sample_count, "console sample count")
        _optional_sha256(self.uncertainty_sha256, "console uncertainty digest")
        if type(self.capability) is not ConsoleCapabilityV1:
            raise TypeError("console capability must be ConsoleCapabilityV1")
        _exact_bool(self.consent_eligible, "console consent eligibility")
        _exact_bool(self.export_eligible, "console export eligibility")

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_version": self.analysis_version,
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind.value,
            "artifact_sha256": self.artifact_sha256,
            "capability": self.capability.value,
            "consent_eligible": self.consent_eligible,
            "content_version": self.content_version,
            "export_eligible": self.export_eligible,
            "model_version": self.model_version,
            "sample_count": self.sample_count,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "scoring_version": self.scoring_version,
            "source_identities": [
                item.as_dict() for item in self.source_identities
            ],
            "uncertainty_sha256": self.uncertainty_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> ConsoleArtifactReferenceV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "analysis_version",
                    "artifact_id",
                    "artifact_kind",
                    "artifact_sha256",
                    "capability",
                    "consent_eligible",
                    "content_version",
                    "export_eligible",
                    "model_version",
                    "sample_count",
                    "schema_id",
                    "schema_version",
                    "scoring_version",
                    "source_identities",
                    "uncertainty_sha256",
                }
            ),
            "console artifact reference",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("console artifact reference schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("console artifact reference schema version differs")
        raw_sources = payload["source_identities"]
        if type(raw_sources) is not list:
            raise TypeError("console source identities must be a list")
        try:
            artifact_kind = ConsoleArtifactKindV1(payload["artifact_kind"])
        except (TypeError, ValueError) as error:
            raise ValueError("console artifact kind is invalid") from error
        try:
            capability = ConsoleCapabilityV1(payload["capability"])
        except (TypeError, ValueError) as error:
            raise ValueError("console capability is invalid") from error
        return cls(
            artifact_kind=artifact_kind,
            artifact_id=_identifier(payload["artifact_id"], "console artifact ID"),
            artifact_sha256=_sha256(
                payload["artifact_sha256"], "console artifact digest"
            ),
            source_identities=tuple(
                ConsoleSourceIdentityV1.from_dict(item) for item in raw_sources
            ),
            content_version=_version(
                payload["content_version"], "console content version"
            ),
            scoring_version=_version(
                payload["scoring_version"], "console scoring version"
            ),
            model_version=_version(
                payload["model_version"], "console model version"
            ),
            analysis_version=_version(
                payload["analysis_version"], "console analysis version"
            ),
            sample_count=_nonnegative_int(
                payload["sample_count"], "console sample count"
            ),
            uncertainty_sha256=_optional_sha256(
                payload["uncertainty_sha256"], "console uncertainty digest"
            ),
            capability=capability,
            consent_eligible=_exact_bool(
                payload["consent_eligible"], "console consent eligibility"
            ),
            export_eligible=_exact_bool(
                payload["export_eligible"], "console export eligibility"
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ConsoleArtifactReferenceV1:
        return cls.from_dict(_canonical_object(raw, "console artifact reference"))


def create_console_artifact_reference(
    *,
    artifact_kind: ConsoleArtifactKindV1,
    artifact_id: str,
    artifact_bytes: bytes,
    source_identities: tuple[ConsoleSourceIdentityV1, ...],
    content_version: str,
    scoring_version: str,
    model_version: str,
    analysis_version: str,
    sample_count: int,
    uncertainty_sha256: str | None,
    capability: ConsoleCapabilityV1,
    consent_eligible: bool,
    export_eligible: bool,
) -> ConsoleArtifactReferenceV1:
    """Hash source bytes and construct their complete immutable console reference."""

    if type(artifact_bytes) is not bytes or not artifact_bytes:
        raise ValueError("console artifact bytes must be nonempty exact bytes")
    if type(source_identities) is not tuple:
        raise TypeError("console source identities must be an immutable tuple")
    if any(type(item) is not ConsoleSourceIdentityV1 for item in source_identities):
        raise TypeError("console source identities contain an invalid record")
    canonical_sources = tuple(
        sorted(set(source_identities), key=lambda item: item.canonical_bytes())
    )
    return ConsoleArtifactReferenceV1(
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        artifact_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        source_identities=canonical_sources,
        content_version=content_version,
        scoring_version=scoring_version,
        model_version=model_version,
        analysis_version=analysis_version,
        sample_count=sample_count,
        uncertainty_sha256=uncertainty_sha256,
        capability=capability,
        consent_eligible=consent_eligible,
        export_eligible=export_eligible,
    )


@dataclass(frozen=True, slots=True)
class ConsoleLedgerEntryV1:
    """One append in a console ledger's exact predecessor hash chain."""

    ledger_id: str
    sequence_number: int
    predecessor_entry_sha256: str | None
    artifact_reference: ConsoleArtifactReferenceV1

    schema_id: ClassVar[str] = CONSOLE_LEDGER_ENTRY_SCHEMA_ID
    schema_version: ClassVar[int] = CONSOLE_LEDGER_ENTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.ledger_id, "console ledger ID")
        _positive_int(self.sequence_number, "console ledger sequence")
        if self.sequence_number == 1:
            if self.predecessor_entry_sha256 is not None:
                raise ValueError(
                    "first console ledger entry cannot have a predecessor digest"
                )
        else:
            _sha256(
                self.predecessor_entry_sha256,
                "console predecessor entry digest",
            )
        if type(self.artifact_reference) is not ConsoleArtifactReferenceV1:
            raise TypeError(
                "console ledger entry artifact must be ConsoleArtifactReferenceV1"
            )

    @property
    def entry_id(self) -> str:
        return f"console-entry-{self.sha256[:24]}"

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_reference": self.artifact_reference.as_dict(),
            "ledger_id": self.ledger_id,
            "predecessor_entry_sha256": self.predecessor_entry_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence_number": self.sequence_number,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> ConsoleLedgerEntryV1:
        payload = _fields(
            value,
            frozenset(
                {
                    "artifact_reference",
                    "ledger_id",
                    "predecessor_entry_sha256",
                    "schema_id",
                    "schema_version",
                    "sequence_number",
                }
            ),
            "console ledger entry",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("console ledger entry schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("console ledger entry schema version differs")
        return cls(
            ledger_id=_identifier(payload["ledger_id"], "console ledger ID"),
            sequence_number=_positive_int(
                payload["sequence_number"], "console ledger sequence"
            ),
            predecessor_entry_sha256=_optional_sha256(
                payload["predecessor_entry_sha256"],
                "console predecessor entry digest",
            ),
            artifact_reference=ConsoleArtifactReferenceV1.from_dict(
                payload["artifact_reference"]
            ),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ConsoleLedgerEntryV1:
        return cls.from_dict(_canonical_object(raw, "console ledger entry"))


@dataclass(frozen=True, slots=True)
class InstructorConsoleLedgerV1:
    """Verified append-only local console ledger and deterministic query point."""

    ledger_id: str
    entries: tuple[ConsoleLedgerEntryV1, ...]

    schema_id: ClassVar[str] = INSTRUCTOR_CONSOLE_LEDGER_SCHEMA_ID
    schema_version: ClassVar[int] = INSTRUCTOR_CONSOLE_LEDGER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.ledger_id, "console ledger ID")
        if type(self.entries) is not tuple:
            raise TypeError("console ledger entries must be an append-only tuple")
        if any(type(item) is not ConsoleLedgerEntryV1 for item in self.entries):
            raise TypeError("console ledger contains an invalid entry")
        self._validate_chain()

    def _validate_chain(self) -> None:
        predecessor: str | None = None
        seen_exact_artifacts: set[tuple[ConsoleArtifactKindV1, str, str]] = set()
        for expected_sequence, entry in enumerate(self.entries, start=1):
            if entry.ledger_id != self.ledger_id:
                raise ValueError("console ledger entry belongs to another ledger")
            if entry.sequence_number != expected_sequence:
                raise ValueError("console ledger sequence must be contiguous from one")
            if entry.predecessor_entry_sha256 != predecessor:
                raise ValueError(
                    "console ledger entry does not bind its exact predecessor"
                )
            reference = entry.artifact_reference
            identity = (
                reference.artifact_kind,
                reference.artifact_id,
                reference.artifact_sha256,
            )
            if identity in seen_exact_artifacts:
                raise ValueError(
                    "console ledger cannot append the same exact artifact twice"
                )
            seen_exact_artifacts.add(identity)
            predecessor = entry.sha256

    @property
    def head_sequence(self) -> int:
        return len(self.entries)

    @property
    def genesis_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json_bytes(
                {
                    "ledger_id": self.ledger_id,
                    "schema_id": self.schema_id,
                    "schema_version": self.schema_version,
                }
            )
        ).hexdigest()

    @property
    def head_sha256(self) -> str:
        if not self.entries:
            return self.genesis_sha256
        return self.entries[-1].sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [item.as_dict() for item in self.entries],
            "ledger_id": self.ledger_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> InstructorConsoleLedgerV1:
        payload = _fields(
            value,
            frozenset({"entries", "ledger_id", "schema_id", "schema_version"}),
            "instructor console ledger",
        )
        if payload["schema_id"] != cls.schema_id:
            raise ValueError("instructor console ledger schema ID differs")
        if payload["schema_version"] != cls.schema_version:
            raise ValueError("instructor console ledger schema version differs")
        raw_entries = payload["entries"]
        if type(raw_entries) is not list:
            raise TypeError("instructor console ledger entries must be a list")
        return cls(
            ledger_id=_identifier(payload["ledger_id"], "console ledger ID"),
            entries=tuple(ConsoleLedgerEntryV1.from_dict(item) for item in raw_entries),
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> InstructorConsoleLedgerV1:
        ledger = cls.from_dict(_canonical_object(raw, "instructor console ledger"))
        if ledger.canonical_bytes() != raw:
            raise ValueError("instructor console ledger bytes changed after reload")
        return ledger

    def as_of(self, sequence_number: int) -> InstructorConsoleLedgerV1:
        """Return the verified prefix ending at an explicit ledger sequence."""

        if type(sequence_number) is not int:
            raise TypeError("console as_of ledger point must be an exact integer")
        if sequence_number < 0 or sequence_number > self.head_sequence:
            raise ValueError("console as_of ledger point is outside the ledger")
        return InstructorConsoleLedgerV1(
            ledger_id=self.ledger_id,
            entries=self.entries[:sequence_number],
        )


def create_console_ledger(
    ledger_id: str = DEFAULT_INSTRUCTOR_CONSOLE_ID,
) -> InstructorConsoleLedgerV1:
    """Create an empty deterministic local console ledger."""

    return InstructorConsoleLedgerV1(ledger_id=ledger_id, entries=())


def append_console_artifact(
    ledger: InstructorConsoleLedgerV1,
    artifact_reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    """Functionally append one exact source artifact to the local console."""

    if type(ledger) is not InstructorConsoleLedgerV1:
        raise TypeError("console ledger must be InstructorConsoleLedgerV1")
    if type(artifact_reference) is not ConsoleArtifactReferenceV1:
        raise TypeError("console artifact must be ConsoleArtifactReferenceV1")
    entry = ConsoleLedgerEntryV1(
        ledger_id=ledger.ledger_id,
        sequence_number=ledger.head_sequence + 1,
        predecessor_entry_sha256=(
            None if not ledger.entries else ledger.entries[-1].sha256
        ),
        artifact_reference=artifact_reference,
    )
    return InstructorConsoleLedgerV1(
        ledger_id=ledger.ledger_id,
        entries=ledger.entries + (entry,),
    )


def _record_typed_artifact(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
    expected_kind: ConsoleArtifactKindV1,
) -> InstructorConsoleLedgerV1:
    if type(reference) is not ConsoleArtifactReferenceV1:
        raise TypeError("console artifact must be ConsoleArtifactReferenceV1")
    if reference.artifact_kind is not expected_kind:
        raise ValueError(
            f"console operation requires {expected_kind.value}, "
            f"received {reference.artifact_kind.value}"
        )
    return append_console_artifact(ledger, reference)


def record_profile(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.PROFILE)


def record_assignment(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.ASSIGNMENT)


def record_attempt(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(
        ledger,
        reference,
        ConsoleArtifactKindV1.ASSIGNMENT_ATTEMPT,
    )


def record_rubric(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.RUBRIC)


def record_review(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.REVIEW)


def record_cohort(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.COHORT)


def record_study(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.STUDY)


def record_amendment(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.AMENDMENT)


def record_comparison(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(ledger, reference, ConsoleArtifactKindV1.COMPARISON)


def record_microscope_link(
    ledger: InstructorConsoleLedgerV1,
    reference: ConsoleArtifactReferenceV1,
) -> InstructorConsoleLedgerV1:
    return _record_typed_artifact(
        ledger,
        reference,
        ConsoleArtifactKindV1.MICROSCOPE_LINK,
    )


def load_console_source_identity(raw: bytes) -> ConsoleSourceIdentityV1:
    return ConsoleSourceIdentityV1.from_canonical_bytes(raw)


def load_console_artifact_reference(raw: bytes) -> ConsoleArtifactReferenceV1:
    return ConsoleArtifactReferenceV1.from_canonical_bytes(raw)


def load_console_ledger_entry(raw: bytes) -> ConsoleLedgerEntryV1:
    return ConsoleLedgerEntryV1.from_canonical_bytes(raw)


def load_instructor_console_ledger(raw: bytes) -> InstructorConsoleLedgerV1:
    return InstructorConsoleLedgerV1.from_canonical_bytes(raw)


__all__ = [
    "CONSOLE_ARTIFACT_REFERENCE_SCHEMA_ID",
    "CONSOLE_ARTIFACT_REFERENCE_SCHEMA_VERSION",
    "CONSOLE_LEDGER_ENTRY_SCHEMA_ID",
    "CONSOLE_LEDGER_ENTRY_SCHEMA_VERSION",
    "CONSOLE_SOURCE_IDENTITY_SCHEMA_ID",
    "CONSOLE_SOURCE_IDENTITY_SCHEMA_VERSION",
    "DEFAULT_INSTRUCTOR_CONSOLE_ID",
    "INSTRUCTOR_CONSOLE_LEDGER_SCHEMA_ID",
    "INSTRUCTOR_CONSOLE_LEDGER_SCHEMA_VERSION",
    "NOT_APPLICABLE_VERSION",
    "ConsoleArtifactKindV1",
    "ConsoleArtifactReferenceV1",
    "ConsoleCapabilityV1",
    "ConsoleLedgerEntryV1",
    "ConsoleSourceIdentityV1",
    "InstructorConsoleLedgerV1",
    "append_console_artifact",
    "create_console_artifact_reference",
    "create_console_ledger",
    "create_console_source_identity",
    "load_console_artifact_reference",
    "load_console_ledger_entry",
    "load_console_source_identity",
    "load_instructor_console_ledger",
    "record_amendment",
    "record_assignment",
    "record_attempt",
    "record_cohort",
    "record_comparison",
    "record_microscope_link",
    "record_profile",
    "record_review",
    "record_rubric",
    "record_study",
]
