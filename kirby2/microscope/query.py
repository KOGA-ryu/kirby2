"""Deterministic, policy-enforced replay microscope queries."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json

from .data_age import (
    DataAge,
    EvidenceTiming,
    NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE,
    TimestampAvailability,
    build_data_age,
)
from .policy import (
    ObservationMode,
    ObservationPolicy,
    ObservedEvidenceKind,
    ReplaySourceCapabilityManifest,
    RevealAuthorization,
    RevealAvailability,
    RevealCapability,
    RevealUnavailableReason,
    SourceCapabilityAvailability,
)


OBSERVED_EVIDENCE_SCHEMA_ID = "KIRBY2_MICROSCOPE_OBSERVED_EVIDENCE_V1"
CLIENT_DELIVERED_ARTIFACT_SCHEMA_ID = "KIRBY2_CLIENT_DELIVERED_ARTIFACT_V1"
DECISION_SNAPSHOT_ARTIFACT_SCHEMA_ID = "KIRBY2_DECISION_SNAPSHOT_ARTIFACT_V1"
REVEAL_EVIDENCE_SCHEMA_ID = "KIRBY2_MICROSCOPE_REVEAL_EVIDENCE_V1"
OBSERVATION_QUERY_SCHEMA_ID = "KIRBY2_MICROSCOPE_OBSERVATION_QUERY_V1"
OBSERVATION_QUERY_SCHEMA_VERSION = 1

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")


class SelectionKind(str, Enum):
    EXACT_RECORDED = "EXACT_RECORDED"
    HELD_LAST_KNOWN = "HELD_LAST_KNOWN"


class RecordDisposition(str, Enum):
    VALUE = "VALUE"
    TOMBSTONE = "TOMBSTONE"


class EvidenceSourceKind(str, Enum):
    CLIENT_DELIVERED = ObservedEvidenceKind.CLIENT_DELIVERED.value
    RECORDED_DECISION_SNAPSHOT = ObservedEvidenceKind.RECORDED_DECISION_SNAPSHOT.value
    REVEALED_GROUND_TRUTH = "REVEALED_GROUND_TRUTH"
    REVEALED_HIDDEN_STATE = "REVEALED_HIDDEN_STATE"


_OBSERVED_SOURCE_KINDS = frozenset(
    {
        EvidenceSourceKind.CLIENT_DELIVERED,
        EvidenceSourceKind.RECORDED_DECISION_SNAPSHOT,
    }
)
_REVEALED_SOURCE_CAPABILITIES = {
    EvidenceSourceKind.REVEALED_GROUND_TRUTH: RevealCapability.GROUND_TRUTH,
    EvidenceSourceKind.REVEALED_HIDDEN_STATE: RevealCapability.HIDDEN_STATE,
}


@dataclass(frozen=True, slots=True)
class ObservedValueRecord:
    """One whole immutable value from an observed replay source."""

    series_id: str
    event_id: str
    sequence: int
    timing: EvidenceTiming
    payload: object
    disposition: RecordDisposition = RecordDisposition.VALUE
    payload_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_identifier(self.series_id, "observed series ID")
        _validate_identifier(self.event_id, "observed event ID")
        _validate_sequence(self.sequence)
        if type(self.timing) is not EvidenceTiming:
            raise TypeError("observed value timing is invalid")
        if type(self.disposition) is not RecordDisposition:
            raise TypeError("observed value disposition is invalid")
        if self.disposition is RecordDisposition.TOMBSTONE and self.payload is not None:
            raise ValueError("observed tombstone payload must be null")
        if self.disposition is RecordDisposition.VALUE and self.payload is None:
            raise ValueError("observed null payload requires TOMBSTONE disposition")
        frozen = freeze_json(self.payload)
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "payload_sha256", _canonical_sha256(thaw_json(frozen)))

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "payload": thaw_json(self.payload),
            "payload_sha256": self.payload_sha256,
            "disposition": self.disposition.value,
            "sequence": self.sequence,
            "series_id": self.series_id,
            "timing": self.timing.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RevealValueRecord:
    """One whole immutable value kept exclusively in a reveal evidence set."""

    series_id: str
    event_id: str
    sequence: int
    timing: EvidenceTiming
    required_capability: RevealCapability
    payload: object
    payload_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_identifier(self.series_id, "reveal series ID")
        _validate_identifier(self.event_id, "reveal event ID")
        _validate_sequence(self.sequence)
        if type(self.timing) is not EvidenceTiming:
            raise TypeError("reveal value timing is invalid")
        if type(self.required_capability) is not RevealCapability:
            raise TypeError("reveal value capability is invalid")
        if self.payload is None:
            raise ValueError("reveal value payload cannot be null")
        frozen = freeze_json(self.payload)
        object.__setattr__(self, "payload", frozen)
        object.__setattr__(self, "payload_sha256", _canonical_sha256(thaw_json(frozen)))

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "payload": thaw_json(self.payload),
            "payload_sha256": self.payload_sha256,
            "required_capability": self.required_capability.value,
            "sequence": self.sequence,
            "series_id": self.series_id,
            "timing": self.timing.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ObservedEvidenceSet:
    """Physically closed observed inputs produced by trusted source adapters.

    The two artifact planes bind exact immutable bytes and never accept reveal record
    types. Constructing a record is not proof that arbitrary JSON was delivered;
    ingestion adapters own that external provenance boundary, not UI/query callers.
    """

    source_run_id: str
    source_event_sha256: str
    client_delivered: tuple[ObservedValueRecord, ...] = ()
    decision_snapshots: tuple[ObservedValueRecord, ...] = ()
    schema_id: str = OBSERVED_EVIDENCE_SCHEMA_ID
    schema_version: int = OBSERVATION_QUERY_SCHEMA_VERSION
    client_delivered_artifact_sha256: str = field(init=False)
    decision_snapshot_artifact_sha256: str = field(init=False)
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_source_identity(self.source_run_id, self.source_event_sha256)
        if (
            self.schema_id != OBSERVED_EVIDENCE_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != OBSERVATION_QUERY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported observed evidence schema")
        delivered = _canonical_observed_records(self.client_delivered)
        decisions = _canonical_observed_records(self.decision_snapshots)
        for record in delivered:
            if (
                record.timing.client_receive.availability
                is not TimestampAvailability.RECORDED
                or record.timing.client_knowledge.availability
                is not TimestampAvailability.RECORDED
            ):
                raise ValueError(
                    "client-delivered evidence requires recorded receive and knowledge times"
                )
        if any(
            record.timing.client_knowledge.availability
            is not TimestampAvailability.RECORDED
            for record in decisions
        ):
            raise ValueError("decision snapshots require recorded client knowledge")
        for record in (*delivered, *decisions):
            _validate_observed_snapshot_timing(record)
        delivered_series = {item.series_id for item in delivered}
        decision_series = {item.series_id for item in decisions}
        if delivered_series & decision_series:
            raise ValueError("observed series IDs must belong to exactly one source plane")
        _validate_record_identity((*delivered, *decisions))
        object.__setattr__(self, "client_delivered", delivered)
        object.__setattr__(self, "decision_snapshots", decisions)
        object.__setattr__(
            self,
            "client_delivered_artifact_sha256",
            _canonical_sha256(
                self._artifact_dict(
                    CLIENT_DELIVERED_ARTIFACT_SCHEMA_ID,
                    delivered,
                )
            ),
        )
        object.__setattr__(
            self,
            "decision_snapshot_artifact_sha256",
            _canonical_sha256(
                self._artifact_dict(
                    DECISION_SNAPSHOT_ARTIFACT_SCHEMA_ID,
                    decisions,
                )
            ),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _canonical_sha256(self._evidence_dict()),
        )

    def _evidence_dict(self) -> dict[str, object]:
        return {
            "client_delivered_artifact_sha256": (
                self.client_delivered_artifact_sha256
            ),
            "client_delivered": [item.as_dict() for item in self.client_delivered],
            "decision_snapshot_artifact_sha256": (
                self.decision_snapshot_artifact_sha256
            ),
            "decision_snapshots": [item.as_dict() for item in self.decision_snapshots],
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
        }

    def _artifact_dict(
        self,
        artifact_schema_id: str,
        records: tuple[ObservedValueRecord, ...],
    ) -> dict[str, object]:
        return {
            "artifact_schema_id": artifact_schema_id,
            "artifact_schema_version": 1,
            "records": [item.as_dict() for item in records],
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._evidence_dict(), "evidence_sha256": self.evidence_sha256}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class RevealEvidenceSet:
    """Truth/reveal bytes that are never accepted by the observed query entry point."""

    source: ReplaySourceCapabilityManifest
    values: tuple[RevealValueRecord, ...] = field(default=(), repr=False)
    schema_id: str = REVEAL_EVIDENCE_SCHEMA_ID
    schema_version: int = OBSERVATION_QUERY_SCHEMA_VERSION
    evidence_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.source) is not ReplaySourceCapabilityManifest:
            raise TypeError("reveal evidence requires a source capability manifest")
        if (
            self.schema_id != REVEAL_EVIDENCE_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != OBSERVATION_QUERY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported reveal evidence schema")
        values = _canonical_reveal_records(self.values)
        _validate_record_identity(values)
        if any(
            item.timing.client_receive.availability
            is TimestampAvailability.RECORDED
            or item.timing.client_knowledge.availability
            is TimestampAvailability.RECORDED
            for item in values
        ):
            raise ValueError(
                "reveal-only evidence cannot claim client delivery or knowledge"
            )
        series_capabilities: dict[str, RevealCapability] = {}
        for item in values:
            prior = series_capabilities.setdefault(
                item.series_id,
                item.required_capability,
            )
            if prior is not item.required_capability:
                raise ValueError("one reveal series cannot span capability planes")
        for capability_evidence in self.source.capability_evidence:
            records = tuple(
                item
                for item in values
                if item.required_capability is capability_evidence.capability
            )
            if (
                capability_evidence.availability
                is SourceCapabilityAvailability.UNAVAILABLE
            ):
                if records:
                    raise ValueError("reveal value lacks evidenced source capability")
                continue
            if not records:
                raise ValueError("available source capability lacks reveal artifact records")
            artifact_sha256 = reveal_artifact_sha256(records)
            if artifact_sha256 != capability_evidence.source_artifact_sha256:
                raise ValueError("reveal records differ from source capability artifact")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "evidence_sha256", _canonical_sha256(self._evidence_dict()))

    def _evidence_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source": self.source.as_dict(),
            "values": [item.as_dict() for item in self.values],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._evidence_dict(), "evidence_sha256": self.evidence_sha256}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class ObservationQueryRequest:
    render_cursor_time_us: int
    action_time_us: int | None = None
    requested_reveal_capabilities: tuple[RevealCapability, ...] = ()

    def __post_init__(self) -> None:
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("query render cursor must be nonnegative microseconds")
        if self.action_time_us is not None and (
            type(self.action_time_us) is not int or self.action_time_us < 0
        ):
            raise ValueError("query action time must be nonnegative microseconds or None")
        if (
            self.action_time_us is not None
            and self.action_time_us > self.render_cursor_time_us
        ):
            raise ValueError("query action time cannot exceed the render cursor")
        capabilities = _canonical_capabilities(self.requested_reveal_capabilities)
        object.__setattr__(self, "requested_reveal_capabilities", capabilities)

    def as_dict(self) -> dict[str, object]:
        return {
            "action_time_us": self.action_time_us,
            "render_cursor_time_us": self.render_cursor_time_us,
            "requested_reveal_capabilities": [
                item.value for item in self.requested_reveal_capabilities
            ],
        }


@dataclass(frozen=True, slots=True)
class RevealDecision:
    availability: RevealAvailability
    requested_capabilities: tuple[RevealCapability, ...]
    unavailable_reason: RevealUnavailableReason | None = None
    authorization_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.availability) is not RevealAvailability:
            raise TypeError("reveal decision availability is invalid")
        capabilities = _canonical_capabilities(self.requested_capabilities)
        object.__setattr__(self, "requested_capabilities", capabilities)
        if self.availability is RevealAvailability.NOT_REQUESTED:
            if capabilities or self.unavailable_reason is not None or self.authorization_id:
                raise ValueError("not-requested reveal decision carries reveal state")
        elif self.availability is RevealAvailability.AVAILABLE:
            if not capabilities or self.unavailable_reason is not None:
                raise ValueError("available reveal decision is inconsistent")
            if not isinstance(self.authorization_id, str) or not self.authorization_id:
                raise ValueError("available reveal decision requires authorization identity")
        else:
            if (
                not capabilities
                or type(self.unavailable_reason) is not RevealUnavailableReason
            ):
                raise ValueError("unavailable reveal decision requires a typed reason")
            if self.authorization_id is not None:
                raise ValueError("unavailable reveal decision cannot expose grant identity")

    def as_dict(self) -> dict[str, object]:
        return {
            "authorization_id": self.authorization_id,
            "availability": self.availability.value,
            "requested_capabilities": [
                item.value for item in self.requested_capabilities
            ],
            "unavailable_reason": (
                None
                if self.unavailable_reason is None
                else self.unavailable_reason.value
            ),
        }


@dataclass(frozen=True, slots=True)
class QueriedValue:
    series_id: str
    event_id: str
    sequence: int
    source_kind: EvidenceSourceKind
    source_evidence_sha256: str
    selection: SelectionKind
    payload: object
    payload_sha256: str
    disposition: RecordDisposition
    data_age: DataAge
    observation_mode: ObservationMode
    policy_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.series_id, "queried series ID")
        _validate_identifier(self.event_id, "queried event ID")
        _validate_sequence(self.sequence)
        if type(self.source_kind) is not EvidenceSourceKind:
            raise TypeError("queried source kind is invalid")
        if not _SHA256.fullmatch(self.source_evidence_sha256):
            raise ValueError("queried source evidence digest is invalid")
        if type(self.selection) is not SelectionKind:
            raise TypeError("queried selection kind is invalid")
        if not _SHA256.fullmatch(self.payload_sha256):
            raise ValueError("queried payload digest is invalid")
        if type(self.disposition) is not RecordDisposition:
            raise TypeError("queried value disposition is invalid")
        if self.disposition is RecordDisposition.TOMBSTONE and self.payload is not None:
            raise ValueError("queried tombstone payload must be null")
        if self.disposition is RecordDisposition.VALUE and self.payload is None:
            raise ValueError("queried null payload requires TOMBSTONE disposition")
        frozen = freeze_json(self.payload)
        if _canonical_sha256(thaw_json(frozen)) != self.payload_sha256:
            raise ValueError("queried payload digest differs")
        object.__setattr__(self, "payload", frozen)
        if type(self.data_age) is not DataAge:
            raise TypeError("queried value data age is invalid")
        if type(self.observation_mode) is not ObservationMode:
            raise TypeError("queried value observation mode is invalid")
        if self.policy_id != ObservationPolicy(self.observation_mode).policy_id:
            raise ValueError("queried value policy label differs from its mode")

    def as_dict(self) -> dict[str, object]:
        return {
            "data_age": self.data_age.as_dict(),
            "disposition": self.disposition.value,
            "event_id": self.event_id,
            "observation_mode": self.observation_mode.value,
            "payload": thaw_json(self.payload),
            "payload_sha256": self.payload_sha256,
            "policy_id": self.policy_id,
            "selection": self.selection.value,
            "sequence": self.sequence,
            "series_id": self.series_id,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_kind": self.source_kind.value,
        }


@dataclass(frozen=True, slots=True)
class ObservationQueryResult:
    policy: ObservationPolicy
    source_run_id: str
    source_event_sha256: str
    observed_projection_sha256: str
    request: ObservationQueryRequest
    values: tuple[QueriedValue, ...]
    reveal: RevealDecision
    reveal_evidence_sha256: str | None = None
    schema_id: str = OBSERVATION_QUERY_SCHEMA_ID
    schema_version: int = OBSERVATION_QUERY_SCHEMA_VERSION
    query_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.policy) is not ObservationPolicy:
            raise TypeError("query result policy is invalid")
        _validate_source_identity(self.source_run_id, self.source_event_sha256)
        if not _SHA256.fullmatch(self.observed_projection_sha256):
            raise ValueError("observed projection digest is invalid")
        if type(self.request) is not ObservationQueryRequest:
            raise TypeError("query result request is invalid")
        if not isinstance(self.values, tuple) or any(
            type(item) is not QueriedValue for item in self.values
        ):
            raise TypeError("query result values are invalid")
        values = tuple(
            sorted(
                self.values,
                key=lambda item: (
                    item.series_id,
                    item.source_kind.value,
                    item.event_id,
                ),
            )
        )
        object.__setattr__(self, "values", values)
        identities = tuple(item.series_id for item in values)
        if len(identities) != len(set(identities)):
            raise ValueError("query result contains duplicate selected series IDs")
        if any(
            item.observation_mode is not self.policy.mode
            or item.policy_id != self.policy.policy_id
            for item in values
        ):
            raise ValueError("query value mode labels differ from the query root")
        if any(
            item.data_age.render_cursor_time_us != self.request.render_cursor_time_us
            or item.data_age.action_time_us != self.request.action_time_us
            for item in values
        ):
            raise ValueError("query value timing differs from the query request")
        if any(
            item.selection
            is not (
                SelectionKind.EXACT_RECORDED
                if item.data_age.policy_visible_at_time_us
                == self.request.render_cursor_time_us
                else SelectionKind.HELD_LAST_KNOWN
            )
            for item in values
        ):
            raise ValueError("query value selection differs from policy visibility")
        if type(self.reveal) is not RevealDecision:
            raise TypeError("query result reveal decision is invalid")
        if (
            self.reveal.requested_capabilities
            != self.request.requested_reveal_capabilities
        ):
            raise ValueError("query request and reveal decision scopes differ")
        if self.policy.mode is ObservationMode.AS_OBSERVED:
            if self.request.requested_reveal_capabilities:
                raise ValueError("as-observed result carries a reveal request")
            if (
                self.reveal.availability is not RevealAvailability.NOT_REQUESTED
                or self.reveal_evidence_sha256 is not None
            ):
                raise ValueError("as-observed result contains reveal state")
            if any(item.source_kind not in _OBSERVED_SOURCE_KINDS for item in values):
                raise ValueError("as-observed result contains a reveal source")
        elif not self.request.requested_reveal_capabilities:
            raise ValueError("postmortem result lacks an explicit reveal request")
        elif self.reveal.availability is RevealAvailability.NOT_REQUESTED:
            raise ValueError("postmortem result lacks a reveal decision")
        elif self.reveal.availability is RevealAvailability.AVAILABLE:
            if (
                self.reveal_evidence_sha256 is None
                or not _SHA256.fullmatch(self.reveal_evidence_sha256)
            ):
                raise ValueError("authorized reveal result lacks its evidence digest")
        elif self.reveal_evidence_sha256 is not None:
            raise ValueError("unavailable reveal result exposes a protected digest")
        if (
            self.reveal.availability is not RevealAvailability.AVAILABLE
            and any(item.source_kind not in _OBSERVED_SOURCE_KINDS for item in values)
        ):
            raise ValueError("query result exposes reveal values without authorization")
        for item in values:
            if item.source_kind in _OBSERVED_SOURCE_KINDS:
                if item.source_evidence_sha256 != self.observed_projection_sha256:
                    raise ValueError("observed value has the wrong evidence identity")
                if (
                    item.data_age.client_knowledge.availability
                    is not TimestampAvailability.RECORDED
                    or item.data_age.client_knowledge.time_us
                    != item.data_age.policy_visible_at_time_us
                ):
                    raise ValueError("observed value lacks exact client knowledge timing")
                if (
                    item.source_kind is EvidenceSourceKind.CLIENT_DELIVERED
                    and item.data_age.client_receive.availability
                    is not TimestampAvailability.RECORDED
                ):
                    raise ValueError("client-delivered value lacks exact receipt timing")
                continue
            capability = _REVEALED_SOURCE_CAPABILITIES[item.source_kind]
            if capability not in self.reveal.requested_capabilities:
                raise ValueError("revealed value exceeds the requested capability scope")
            if item.disposition is not RecordDisposition.VALUE:
                raise ValueError("reveal values cannot carry observed tombstones")
            if (
                item.data_age.policy_visible_at_time_us
                != item.data_age.source_event_time_us
            ):
                raise ValueError("reveal visibility differs from its source event time")
            if item.source_evidence_sha256 != self.reveal_evidence_sha256:
                raise ValueError("revealed value has the wrong evidence identity")
            if (
                item.data_age.client_receive.availability
                is TimestampAvailability.RECORDED
                or item.data_age.client_knowledge.availability
                is TimestampAvailability.RECORDED
            ):
                raise ValueError(
                    "revealed value retroactively claims client receipt or knowledge"
                )
        if (
            self.schema_id != OBSERVATION_QUERY_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != OBSERVATION_QUERY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported observation query schema")
        object.__setattr__(
            self,
            "query_id",
            "observation-query-" + _canonical_sha256(self.identity_dict())[:24],
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "observed_projection_sha256": self.observed_projection_sha256,
            "policy": self.policy.as_dict(),
            "request": self.request.as_dict(),
            "reveal": self.reveal.as_dict(),
            "reveal_evidence_sha256": self.reveal_evidence_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "values": [item.as_dict() for item in self.values],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "query_id": self.query_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    def export_payload(self) -> dict[str, object]:
        return self.as_dict()

    def export_metadata(self) -> dict[str, str]:
        return self._portable_metadata()

    def screenshot_metadata(self) -> dict[str, str]:
        return self._portable_metadata()

    def portable_report_metadata(self) -> dict[str, str]:
        return self._portable_metadata()

    def _portable_metadata(self) -> dict[str, str]:
        reason = self.reveal.unavailable_reason
        return {
            **self.policy.metadata(),
            "query_id": self.query_id,
            "render_cursor_time_us": str(self.request.render_cursor_time_us),
            "reveal_availability": self.reveal.availability.value,
            "reveal_unavailable_reason": "NONE" if reason is None else reason.value,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
        }


def query_as_observed(
    evidence: ObservedEvidenceSet,
    request: ObservationQueryRequest,
) -> ObservationQueryResult:
    """Query client-visible evidence without accepting any reveal source parameter."""

    if type(evidence) is not ObservedEvidenceSet:
        raise TypeError("as-observed query requires ObservedEvidenceSet")
    if type(request) is not ObservationQueryRequest:
        raise TypeError("as-observed query request is invalid")
    if request.requested_reveal_capabilities:
        raise ValueError("as-observed query cannot request reveal capabilities")
    if (
        request.action_time_us is not None
        and request.action_time_us > request.render_cursor_time_us
    ):
        raise ValueError("as-observed query cannot expose a future action time")
    policy = ObservationPolicy(ObservationMode.AS_OBSERVED)
    observed_projection_sha256 = _observed_projection_sha256(evidence, request)
    values = _query_observed_values(
        evidence,
        request,
        policy,
        observed_projection_sha256,
    )
    return ObservationQueryResult(
        policy=policy,
        source_run_id=evidence.source_run_id,
        source_event_sha256=evidence.source_event_sha256,
        observed_projection_sha256=observed_projection_sha256,
        request=request,
        values=values,
        reveal=RevealDecision(RevealAvailability.NOT_REQUESTED, ()),
    )


def query_postmortem(
    observed: ObservedEvidenceSet,
    reveal: RevealEvidenceSet | None,
    authorization: RevealAuthorization | None,
    request: ObservationQueryRequest,
) -> ObservationQueryResult:
    """Add reveal values only after capability, binding, scope, and time checks."""

    if type(observed) is not ObservedEvidenceSet:
        raise TypeError("postmortem query requires ObservedEvidenceSet")
    if reveal is not None and type(reveal) is not RevealEvidenceSet:
        raise TypeError("postmortem reveal source is invalid")
    if authorization is not None and type(authorization) is not RevealAuthorization:
        raise TypeError("postmortem reveal authorization is invalid")
    if type(request) is not ObservationQueryRequest:
        raise TypeError("postmortem query request is invalid")
    requested = request.requested_reveal_capabilities
    if not requested:
        raise ValueError("postmortem query requires an explicit reveal capability request")
    if (
        request.action_time_us is not None
        and request.action_time_us > request.render_cursor_time_us
    ):
        raise ValueError("postmortem query cannot expose a future action time")

    policy = ObservationPolicy(ObservationMode.POSTMORTEM)
    observed_projection_sha256 = _observed_projection_sha256(observed, request)
    observed_values = _query_observed_values(
        observed,
        request,
        policy,
        observed_projection_sha256,
    )
    denial = _reveal_denial(observed, reveal, authorization, request)
    if denial is not None:
        return ObservationQueryResult(
            policy=policy,
            source_run_id=observed.source_run_id,
            source_event_sha256=observed.source_event_sha256,
            observed_projection_sha256=observed_projection_sha256,
            request=request,
            values=observed_values,
            reveal=RevealDecision(
                RevealAvailability.UNAVAILABLE,
                requested,
                denial,
            ),
        )

    if reveal is None or authorization is None:  # pragma: no cover - denial is exhaustive
        raise RuntimeError("authorized reveal query lacks evidence or authorization")
    _validate_authorized_series_namespace(observed, reveal, requested)
    reveal_values = _query_reveal_values(
        reveal,
        request,
        policy,
    )
    return ObservationQueryResult(
        policy=policy,
        source_run_id=observed.source_run_id,
        source_event_sha256=observed.source_event_sha256,
        observed_projection_sha256=observed_projection_sha256,
        request=request,
        values=(*observed_values, *reveal_values),
        reveal=RevealDecision(
            RevealAvailability.AVAILABLE,
            requested,
            authorization_id=authorization.authorization_id,
        ),
        reveal_evidence_sha256=reveal.evidence_sha256,
    )


def _reveal_denial(
    observed: ObservedEvidenceSet,
    reveal: RevealEvidenceSet | None,
    authorization: RevealAuthorization | None,
    request: ObservationQueryRequest,
) -> RevealUnavailableReason | None:
    requested = request.requested_reveal_capabilities
    if reveal is None:
        return RevealUnavailableReason.SOURCE_CAPABILITY_UNAVAILABLE
    available_capabilities = {
        item.capability
        for item in reveal.source.capability_evidence
        if item.availability is SourceCapabilityAvailability.AVAILABLE
    }
    if not set(requested).issubset(available_capabilities):
        return RevealUnavailableReason.SOURCE_CAPABILITY_UNAVAILABLE
    if (
        reveal.source.source_run_id != observed.source_run_id
        or reveal.source.source_event_sha256 != observed.source_event_sha256
    ):
        return RevealUnavailableReason.REVEAL_SOURCE_MISMATCH
    if authorization is None:
        return RevealUnavailableReason.AUTHORIZATION_REQUIRED
    if (
        authorization.source_run_id != observed.source_run_id
        or authorization.source_event_sha256 != observed.source_event_sha256
        or authorization.observed_evidence_sha256 != observed.evidence_sha256
        or authorization.source_capability_manifest_sha256
        != reveal.source.manifest_sha256
        or authorization.reveal_evidence_sha256 != reveal.evidence_sha256
    ):
        return RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH
    if not set(requested).issubset(authorization.capabilities):
        return RevealUnavailableReason.AUTHORIZATION_SCOPE_MISMATCH
    return None


def _query_observed_values(
    evidence: ObservedEvidenceSet,
    request: ObservationQueryRequest,
    policy: ObservationPolicy,
    observed_projection_sha256: str,
) -> tuple[QueriedValue, ...]:
    delivered = _select_records(
        evidence.client_delivered,
        request,
        policy,
        source_kind=EvidenceSourceKind.CLIENT_DELIVERED,
        source_evidence_sha256=observed_projection_sha256,
    )
    decisions = _select_records(
        evidence.decision_snapshots,
        request,
        policy,
        source_kind=EvidenceSourceKind.RECORDED_DECISION_SNAPSHOT,
        source_evidence_sha256=observed_projection_sha256,
    )
    return tuple((*delivered, *decisions))


def _validate_authorized_series_namespace(
    observed: ObservedEvidenceSet,
    reveal: RevealEvidenceSet,
    requested: tuple[RevealCapability, ...],
) -> None:
    observed_series = {
        item.series_id
        for item in (*observed.client_delivered, *observed.decision_snapshots)
    }
    revealed_series = {
        item.series_id
        for item in reveal.values
        if item.required_capability in requested
    }
    overlap = observed_series & revealed_series
    if overlap:
        raise ValueError("authorized observed and reveal series namespaces overlap")


def _observed_projection_sha256(
    evidence: ObservedEvidenceSet,
    request: ObservationQueryRequest,
) -> str:
    def visible(records: tuple[ObservedValueRecord, ...]) -> list[dict[str, object]]:
        return [
            item.as_dict()
            for item in records
            if item.timing.client_knowledge_time_us is not None
            and item.timing.client_knowledge_time_us
            <= request.render_cursor_time_us
        ]

    return _canonical_sha256(
        {
            "client_delivered": visible(evidence.client_delivered),
            "decision_snapshots": visible(evidence.decision_snapshots),
            "render_cursor_time_us": request.render_cursor_time_us,
            "schema_id": OBSERVED_EVIDENCE_SCHEMA_ID,
            "schema_version": OBSERVATION_QUERY_SCHEMA_VERSION,
            "source_event_sha256": evidence.source_event_sha256,
            "source_run_id": evidence.source_run_id,
        }
    )


def _query_reveal_values(
    evidence: RevealEvidenceSet,
    request: ObservationQueryRequest,
    policy: ObservationPolicy,
) -> tuple[QueriedValue, ...]:
    values: list[QueriedValue] = []
    for capability in request.requested_reveal_capabilities:
        records = tuple(
            item for item in evidence.values if item.required_capability is capability
        )
        values.extend(
            _select_records(
                records,
                request,
                policy,
                source_kind=EvidenceSourceKind[f"REVEALED_{capability.value}"],
                source_evidence_sha256=evidence.evidence_sha256,
            )
        )
    return tuple(values)


def _select_records(
    records: tuple[ObservedValueRecord, ...] | tuple[RevealValueRecord, ...],
    request: ObservationQueryRequest,
    policy: ObservationPolicy,
    *,
    source_kind: EvidenceSourceKind,
    source_evidence_sha256: str,
) -> tuple[QueriedValue, ...]:
    selected: dict[str, ObservedValueRecord | RevealValueRecord] = {}
    selected_knowledge: dict[str, int] = {}
    for record in records:
        if source_kind in _OBSERVED_SOURCE_KINDS:
            visibility_time = record.timing.client_knowledge_time_us
            if visibility_time is None:
                raise RuntimeError("observed record lacks client knowledge time")
        else:
            visibility_time = record.timing.source_event_time_us
        if visibility_time > request.render_cursor_time_us:
            continue
        current = selected.get(record.series_id)
        current_knowledge = selected_knowledge.get(record.series_id, -1)
        if current is None or (
            visibility_time,
            record.sequence,
            record.event_id,
        ) > (
            current_knowledge,
            current.sequence,
            current.event_id,
        ):
            selected[record.series_id] = record
            selected_knowledge[record.series_id] = visibility_time

    values = []
    for series_id in sorted(selected):
        record = selected[series_id]
        knowledge_time = selected_knowledge[series_id]
        data_age = build_data_age(
            record.timing,
            request.render_cursor_time_us,
            policy_visible_at_time_us=knowledge_time,
            action_time_us=request.action_time_us,
        )
        values.append(
            QueriedValue(
                series_id=record.series_id,
                event_id=record.event_id,
                sequence=record.sequence,
                source_kind=source_kind,
                source_evidence_sha256=source_evidence_sha256,
                selection=(
                    SelectionKind.EXACT_RECORDED
                    if knowledge_time == request.render_cursor_time_us
                    else SelectionKind.HELD_LAST_KNOWN
                ),
                payload=record.payload,
                payload_sha256=record.payload_sha256,
                disposition=(
                    record.disposition
                    if type(record) is ObservedValueRecord
                    else RecordDisposition.VALUE
                ),
                data_age=data_age,
                observation_mode=policy.mode,
                policy_id=policy.policy_id,
            )
        )
    return tuple(values)


def _canonical_observed_records(
    values: tuple[ObservedValueRecord, ...],
) -> tuple[ObservedValueRecord, ...]:
    if not isinstance(values, tuple) or any(
        type(item) is not ObservedValueRecord for item in values
    ):
        raise TypeError("observed evidence records are invalid")
    return tuple(sorted(values, key=_record_sort_key))


def reveal_artifact_sha256(
    values: tuple[RevealValueRecord, ...],
) -> str:
    """Hash one capability's canonical reveal records as a source artifact."""

    records = _canonical_reveal_records(values)
    if not records:
        raise ValueError("reveal artifact requires at least one record")
    capabilities = {item.required_capability for item in records}
    if len(capabilities) != 1:
        raise ValueError("reveal artifact records must share one capability")
    return _canonical_sha256([item.as_dict() for item in records])


def _canonical_reveal_records(
    values: tuple[RevealValueRecord, ...],
) -> tuple[RevealValueRecord, ...]:
    if not isinstance(values, tuple) or any(
        type(item) is not RevealValueRecord for item in values
    ):
        raise TypeError("reveal evidence records are invalid")
    return tuple(sorted(values, key=_record_sort_key))


def _record_sort_key(
    item: ObservedValueRecord | RevealValueRecord,
) -> tuple[str, int, int, int, str]:
    knowledge_time = item.timing.client_knowledge_time_us
    return (
        item.series_id,
        item.timing.source_event_time_us,
        -1 if knowledge_time is None else knowledge_time,
        item.sequence,
        item.event_id,
    )


def _validate_record_identity(
    records: tuple[ObservedValueRecord | RevealValueRecord, ...],
) -> None:
    event_ids = tuple(item.event_id for item in records)
    sequences = tuple(item.sequence for item in records)
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("evidence event IDs must be unique")
    if len(sequences) != len(set(sequences)):
        raise ValueError("evidence sequences must be unique")


def _validate_observed_snapshot_timing(record: ObservedValueRecord) -> None:
    knowledge_time = record.timing.client_knowledge_time_us
    if knowledge_time is None:  # pragma: no cover - caller validated by plane
        raise RuntimeError("observed snapshot lost its client knowledge time")
    for timestamp in (
        record.timing.venue_receipt,
        record.timing.client_receive,
    ):
        if timestamp.time_us is not None and timestamp.time_us > knowledge_time:
            raise ValueError(
                "observed snapshots cannot contain later-enriched timing evidence"
            )
        if (
            timestamp.availability is TimestampAvailability.UNAVAILABLE
            and timestamp.reason != NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE
        ):
            raise ValueError(
                "observed unavailable timing must be neutral at its knowledge cutoff"
            )


def _canonical_capabilities(
    values: tuple[RevealCapability, ...],
) -> tuple[RevealCapability, ...]:
    if not isinstance(values, tuple) or any(
        type(item) is not RevealCapability for item in values
    ):
        raise TypeError("requested reveal capabilities are invalid")
    return tuple(sorted(set(values), key=lambda item: item.value))


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _validate_sequence(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("evidence sequence must be a positive integer")


def _validate_source_identity(source_run_id: str, source_event_sha256: str) -> None:
    if not isinstance(source_run_id, str) or not _RUN_ID.fullmatch(source_run_id):
        raise ValueError("query source run ID is invalid")
    if (
        not isinstance(source_event_sha256, str)
        or not _SHA256.fullmatch(source_event_sha256)
    ):
        raise ValueError("query source event digest is invalid")


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    if isinstance(payload, Mapping):
        payload = dict(payload)
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


# Raw evidence/result-value constructors are intentionally omitted: verified source
# adapters own them. UI/integration callers consume requests, results, and query APIs.
__all__ = [
    "OBSERVATION_QUERY_SCHEMA_ID",
    "OBSERVATION_QUERY_SCHEMA_VERSION",
    "OBSERVED_EVIDENCE_SCHEMA_ID",
    "CLIENT_DELIVERED_ARTIFACT_SCHEMA_ID",
    "DECISION_SNAPSHOT_ARTIFACT_SCHEMA_ID",
    "REVEAL_EVIDENCE_SCHEMA_ID",
    "EvidenceSourceKind",
    "ObservationQueryRequest",
    "ObservationQueryResult",
    "RecordDisposition",
    "SelectionKind",
    "query_as_observed",
    "query_postmortem",
]
