"""Verified observed-only artifact ingestion for replay microscope queries.

This module is the backend trust boundary between externally supplied immutable
artifact bytes and :mod:`kirby2.microscope.query`.  It deliberately supports a
small, closed observed-value vocabulary.  New source semantics require a new
record kind and adapter version; arbitrary JSON cannot be labelled as client
evidence merely because it can be represented by ``ObservedValueRecord``.

The manifest pin must come from a governed backend run index independently of the
bundle being opened.  Supplying an adjacent manifest and its freshly calculated hash
does not establish provenance, and this loader cannot prove where its caller obtained
the pin.  The pinning authority must also bind ``source_event_sha256`` to a public,
observed-only identity domain; the ``OBSERVED_ONLY`` manifest label cannot prove that
an issuer did not commit hidden or future outcome bytes into that digest.  This is an
architectural boundary for cooperative first-party code; adversarial code already
executing in this Python interpreter requires a separate process boundary.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from kirby2.full_day.models import (
    canonical_json_bytes,
    parse_canonical_json_object,
)

from .data_age import (
    EvidenceTimestamp,
    EvidenceTiming,
    TimestampAbsenceReason,
    TimestampAvailability,
)
from .query import (
    OBSERVATION_QUERY_SCHEMA_VERSION,
    ObservationQueryRequest,
    ObservationQueryResult,
    ObservedEvidenceSet,
    ObservedValueRecord,
    RecordDisposition,
    query_as_observed,
)


OBSERVED_INGEST_ADAPTER_ID = "KIRBY2_OBSERVED_INGEST_ADAPTER_V1"
OBSERVED_INGEST_ADAPTER_VERSION = 1
OBSERVED_INGEST_MANIFEST_SCHEMA_ID = "KIRBY2_OBSERVED_INGEST_MANIFEST_V1"
OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION = 1

_INGESTION_RECEIPT_SCHEMA_ID = "KIRBY2_OBSERVATION_INGESTION_RECEIPT_V1"
_INGESTION_RECEIPT_SCHEMA_VERSION = 1
_OBSERVED_SOURCE_SCOPE = "OBSERVED_ONLY"
_CLIENT_SOURCE_ARTIFACT_SCHEMA_ID = (
    "KIRBY2_OBSERVED_CLIENT_DELIVERED_SOURCE_ARTIFACT_V1"
)
_DECISION_SOURCE_ARTIFACT_SCHEMA_ID = (
    "KIRBY2_OBSERVED_DECISION_SNAPSHOT_SOURCE_ARTIFACT_V1"
)
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_SOURCE_ARTIFACT_BYTES = 64 * 1024 * 1024

_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*$")


class _ArtifactKind(str, Enum):
    CLIENT_DELIVERED = "CLIENT_DELIVERED"
    DECISION_SNAPSHOT = "DECISION_SNAPSHOT"


class _RecordKind(str, Enum):
    BEST_BID_QUOTE = "BEST_BID_QUOTE"
    ORDER_ACKNOWLEDGEMENT = "ORDER_ACKNOWLEDGEMENT"
    PLAYER_ORDER_STATE = "PLAYER_ORDER_STATE"
    PLAYER_FILL = "PLAYER_FILL"
    IMBALANCE_FEATURE = "IMBALANCE_FEATURE"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    CLIENT_ORDER_INTENTION = "CLIENT_ORDER_INTENTION"


_ARTIFACT_KIND_ORDER = tuple(_ArtifactKind)
_ARTIFACT_SCHEMAS = {
    _ArtifactKind.CLIENT_DELIVERED: _CLIENT_SOURCE_ARTIFACT_SCHEMA_ID,
    _ArtifactKind.DECISION_SNAPSHOT: _DECISION_SOURCE_ARTIFACT_SCHEMA_ID,
}


def _exact_integer(value: object, field_name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an exact integer")


def _nonnegative_integer(value: object, field_name: str) -> None:
    _exact_integer(value, field_name)
    if value < 0:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be nonnegative")


def _bounded_imbalance(value: object, field_name: str) -> None:
    _exact_integer(value, field_name)
    if not -1_000_000 <= value <= 1_000_000:  # type: ignore[operator]
        raise ValueError(f"{field_name} must be within +/-1000000")


def _exact_boolean(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be an exact boolean")


def _closed_text(*allowed: str) -> Callable[[object, str], None]:
    permitted = frozenset(allowed)

    def validate(value: object, field_name: str) -> None:
        if type(value) is not str or value not in permitted:
            raise ValueError(
                f"{field_name} must be one of {tuple(sorted(permitted))!r}"
            )

    return validate


def _exact_series(*allowed: str) -> Callable[[str], bool]:
    permitted = frozenset(allowed)
    return lambda value: value in permitted


def _prefixed_series(
    prefix: str,
    *,
    excluded: frozenset[str] = frozenset(),
) -> Callable[[str], bool]:
    return lambda value: (
        value.startswith(prefix)
        and len(value) > len(prefix)
        and value not in excluded
    )


@dataclass(frozen=True, slots=True)
class _PayloadField:
    name: str
    validator: Callable[[object, str], None]


@dataclass(frozen=True, slots=True)
class _RecordContract:
    artifact_kind: _ArtifactKind
    series_matches: Callable[[str], bool]
    payload_fields: tuple[_PayloadField, ...]


_PLAYER_ORDER_STATES = (
    "ACCEPTED",
    "AUCTION_PENDING",
    "AUCTION_WORKING",
    "CANCELLED",
    "CANCELLED_STP",
    "EXPIRED",
    "FILLED",
    "PARTIALLY_FILLED",
    "PENDING",
    "REJECTED",
    "REPLACED",
    "WORKING",
)

_RECORD_CONTRACTS: Mapping[_RecordKind, _RecordContract] = {
    _RecordKind.BEST_BID_QUOTE: _RecordContract(
        _ArtifactKind.CLIENT_DELIVERED,
        _exact_series("quote.best-bid", "quote.processed-best-bid"),
        (_PayloadField("best_bid_ticks", _exact_integer),),
    ),
    _RecordKind.ORDER_ACKNOWLEDGEMENT: _RecordContract(
        _ArtifactKind.CLIENT_DELIVERED,
        _prefixed_series("ack."),
        (_PayloadField("acknowledged", _exact_boolean),),
    ),
    _RecordKind.PLAYER_ORDER_STATE: _RecordContract(
        _ArtifactKind.CLIENT_DELIVERED,
        _prefixed_series(
            "order.",
            excluded=frozenset({"order.client-intention"}),
        ),
        (_PayloadField("state", _closed_text(*_PLAYER_ORDER_STATES)),),
    ),
    _RecordKind.PLAYER_FILL: _RecordContract(
        _ArtifactKind.CLIENT_DELIVERED,
        _prefixed_series("fill."),
        (_PayloadField("filled_quantity", _nonnegative_integer),),
    ),
    _RecordKind.IMBALANCE_FEATURE: _RecordContract(
        _ArtifactKind.CLIENT_DELIVERED,
        _exact_series("feature.imbalance"),
        (_PayloadField("value_millionths", _bounded_imbalance),),
    ),
    _RecordKind.STRATEGY_SIGNAL: _RecordContract(
        _ArtifactKind.DECISION_SNAPSHOT,
        _exact_series("strategy.signal"),
        (_PayloadField("recorded_signal", _closed_text("GREEN", "RED", "WAIT")),),
    ),
    _RecordKind.CLIENT_ORDER_INTENTION: _RecordContract(
        _ArtifactKind.DECISION_SNAPSHOT,
        _exact_series("order.client-intention"),
        (
            _PayloadField("side", _closed_text("BUY", "SELL")),
            _PayloadField(
                "venue_state",
                _closed_text("NOT_OBSERVED", "RECEIVED"),
            ),
        ),
    ),
}
if frozenset(_RECORD_CONTRACTS) != frozenset(_RecordKind):
    raise RuntimeError("observed ingestion record registry is incomplete")


@dataclass(frozen=True, slots=True)
class ObservedArtifactBytes:
    """One named immutable source artifact supplied to the verified adapter."""

    artifact_id: str
    raw_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, "observed source artifact ID")
        if type(self.raw_bytes) is not bytes:
            raise TypeError("observed source artifact bytes must be exact bytes")
        if len(self.raw_bytes) > _MAX_SOURCE_ARTIFACT_BYTES:
            raise ValueError("observed source artifact exceeds the byte-size ceiling")
        object.__setattr__(self, "raw_bytes", bytes(self.raw_bytes))

    @property
    def sha256(self) -> str:
        return _sha256_bytes(self.raw_bytes)

    @property
    def byte_length(self) -> int:
        return len(self.raw_bytes)


@dataclass(frozen=True, slots=True)
class _ManifestArtifact:
    artifact_id: str
    artifact_kind: _ArtifactKind
    artifact_schema_id: str
    artifact_schema_version: int
    byte_length: int
    normalized_plane_sha256: str
    record_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _IngestManifest:
    source_run_id: str
    source_event_sha256: str
    artifacts: tuple[_ManifestArtifact, ...]


@dataclass(frozen=True, slots=True)
class ObservationIngestionReceipt:
    """Backend-only proof of the exact raw-to-normalized ingestion binding.

    A receipt commits the complete supplied artifacts, including records after an
    arbitrary replay cursor.  It is therefore not cursor-safe UI data and must not be
    exposed through the query facade, screenshots, logs visible to a player, or
    portable replay reports.
    """

    source_run_id: str
    source_event_sha256: str
    manifest_sha256: str
    evidence_sha256: str
    client_delivered_artifact_id: str
    client_delivered_artifact_schema_id: str
    client_delivered_artifact_schema_version: int
    client_delivered_raw_sha256: str
    client_delivered_normalized_plane_sha256: str
    client_delivered_byte_length: int
    client_delivered_record_count: int
    decision_snapshot_artifact_id: str
    decision_snapshot_artifact_schema_id: str
    decision_snapshot_artifact_schema_version: int
    decision_snapshot_raw_sha256: str
    decision_snapshot_normalized_plane_sha256: str
    decision_snapshot_byte_length: int
    decision_snapshot_record_count: int
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _source_identity(self.source_run_id, self.source_event_sha256)
        _sha256(self.manifest_sha256, "ingestion manifest SHA-256")
        _sha256(self.evidence_sha256, "observed evidence SHA-256")
        _validate_receipt_plane(
            _ArtifactKind.CLIENT_DELIVERED,
            self.client_delivered_artifact_id,
            self.client_delivered_artifact_schema_id,
            self.client_delivered_artifact_schema_version,
            self.client_delivered_raw_sha256,
            self.client_delivered_normalized_plane_sha256,
            self.client_delivered_byte_length,
            self.client_delivered_record_count,
        )
        _validate_receipt_plane(
            _ArtifactKind.DECISION_SNAPSHOT,
            self.decision_snapshot_artifact_id,
            self.decision_snapshot_artifact_schema_id,
            self.decision_snapshot_artifact_schema_version,
            self.decision_snapshot_raw_sha256,
            self.decision_snapshot_normalized_plane_sha256,
            self.decision_snapshot_byte_length,
            self.decision_snapshot_record_count,
        )
        if self.client_delivered_artifact_id == self.decision_snapshot_artifact_id:
            raise ValueError("ingestion receipt artifact IDs must be distinct")
        object.__setattr__(
            self,
            "receipt_sha256",
            _sha256_bytes(canonical_json_bytes(self._identity_dict())),
        )

    def _artifact_rows(self) -> list[dict[str, object]]:
        return [
            {
                "artifact_id": self.client_delivered_artifact_id,
                "artifact_kind": _ArtifactKind.CLIENT_DELIVERED.value,
                "artifact_schema_id": self.client_delivered_artifact_schema_id,
                "artifact_schema_version": (
                    self.client_delivered_artifact_schema_version
                ),
                "byte_length": self.client_delivered_byte_length,
                "normalized_plane_sha256": (
                    self.client_delivered_normalized_plane_sha256
                ),
                "record_count": self.client_delivered_record_count,
                "sha256": self.client_delivered_raw_sha256,
            },
            {
                "artifact_id": self.decision_snapshot_artifact_id,
                "artifact_kind": _ArtifactKind.DECISION_SNAPSHOT.value,
                "artifact_schema_id": self.decision_snapshot_artifact_schema_id,
                "artifact_schema_version": (
                    self.decision_snapshot_artifact_schema_version
                ),
                "byte_length": self.decision_snapshot_byte_length,
                "normalized_plane_sha256": (
                    self.decision_snapshot_normalized_plane_sha256
                ),
                "record_count": self.decision_snapshot_record_count,
                "sha256": self.decision_snapshot_raw_sha256,
            },
        ]

    def _identity_dict(self) -> dict[str, object]:
        return {
            "adapter_id": OBSERVED_INGEST_ADAPTER_ID,
            "adapter_version": OBSERVED_INGEST_ADAPTER_VERSION,
            "artifacts": self._artifact_rows(),
            "evidence_sha256": self.evidence_sha256,
            "manifest_schema_id": OBSERVED_INGEST_MANIFEST_SCHEMA_ID,
            "manifest_schema_version": OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION,
            "manifest_sha256": self.manifest_sha256,
            "receipt_schema_id": _INGESTION_RECEIPT_SCHEMA_ID,
            "receipt_schema_version": _INGESTION_RECEIPT_SCHEMA_VERSION,
            "source_event_sha256": self.source_event_sha256,
            "source_run_id": self.source_run_id,
            "source_scope": _OBSERVED_SOURCE_SCOPE,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._identity_dict(), "receipt_sha256": self.receipt_sha256}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


_SERVICE_CONSTRUCTION_TOKEN = object()


class VerifiedObservationSource:
    """Closed AS_OBSERVED service backed by exact pinned source bytes.

    The service retains no reusable ``ObservedEvidenceSet``.  Every query parses and
    verifies the private pinned bytes again before constructing fresh evidence, so
    callers cannot corrupt later queries through a stale mutable adapter object.
    """

    __slots__ = (
        "__artifact_bytes",
        "__manifest_bytes",
        "__manifest_sha256",
        "__receipt",
        "__sealed",
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        del kwargs
        raise TypeError("VerifiedObservationSource is closed to subclassing")

    def __init__(
        self,
        manifest_bytes: bytes,
        manifest_sha256: str,
        artifacts: tuple[ObservedArtifactBytes, ...],
        receipt: ObservationIngestionReceipt,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _SERVICE_CONSTRUCTION_TOKEN:
            raise TypeError(
                "VerifiedObservationSource is constructed only by verified ingestion"
            )
        object.__setattr__(self, "_VerifiedObservationSource__sealed", False)
        object.__setattr__(
            self,
            "_VerifiedObservationSource__manifest_bytes",
            bytes(manifest_bytes),
        )
        object.__setattr__(
            self,
            "_VerifiedObservationSource__manifest_sha256",
            manifest_sha256,
        )
        object.__setattr__(
            self,
            "_VerifiedObservationSource__artifact_bytes",
            tuple((item.artifact_id, bytes(item.raw_bytes)) for item in artifacts),
        )
        object.__setattr__(
            self,
            "_VerifiedObservationSource__receipt",
            receipt,
        )
        object.__setattr__(self, "_VerifiedObservationSource__sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_VerifiedObservationSource__sealed", False):
            raise AttributeError("VerifiedObservationSource is immutable")
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return (
            "VerifiedObservationSource("
            f"source_run_id={self.__receipt.source_run_id!r})"
        )

    def __reduce__(self) -> object:
        raise TypeError("VerifiedObservationSource cannot be serialized")

    def query(self, request: ObservationQueryRequest) -> ObservationQueryResult:
        """Return one policy-enforced AS_OBSERVED result from reverified bytes."""

        if type(request) is not ObservationQueryRequest:
            raise TypeError(
                "verified observation query requires ObservationQueryRequest"
            )
        evidence, receipt = _ingest_verified(
            self.__manifest_bytes,
            self.__manifest_sha256,
            tuple(
                ObservedArtifactBytes(artifact_id, raw)
                for artifact_id, raw in self.__artifact_bytes
            ),
        )
        if receipt != self.__receipt:
            raise RuntimeError(
                "pinned observation source receipt changed during re-ingestion"
            )
        return query_as_observed(evidence, request)

    def result(
        self,
        render_cursor_time_us: int,
        action_time_us: int | None = None,
    ) -> ObservationQueryResult:
        """Build and execute one observed-only cursor request."""

        return self.query(
            ObservationQueryRequest(
                render_cursor_time_us=render_cursor_time_us,
                action_time_us=action_time_us,
            )
        )

    def canonical_bytes(self, request: ObservationQueryRequest) -> bytes:
        """Return canonical bytes for a freshly reverified AS_OBSERVED result."""

        return self.query(request).canonical_bytes()


def load_verified_observation_source(
    manifest_bytes: bytes,
    pinned_manifest_sha256: str,
    artifacts: tuple[ObservedArtifactBytes, ...],
) -> VerifiedObservationSource:
    """Verify exact manifest/artifact bytes and return a closed query service.

    The supplied manifest pin is checked before any JSON parsing takes place.  The
    loader cannot authenticate the caller or establish the pin's origin; the backend
    integration must resolve it from a governed run index, bind the requested run to
    that pin, and require a public observed-only source identity.  Artifact digests
    and lengths are likewise checked before their bytes are parsed.
    """

    evidence, receipt = _validate_ingestion_request(
        manifest_bytes,
        pinned_manifest_sha256,
        artifacts,
    )
    del evidence
    return VerifiedObservationSource(
        bytes(manifest_bytes),
        pinned_manifest_sha256,
        artifacts,
        receipt,
        _token=_SERVICE_CONSTRUCTION_TOKEN,
    )


def verify_observation_ingestion(
    manifest_bytes: bytes,
    pinned_manifest_sha256: str,
    artifacts: tuple[ObservedArtifactBytes, ...],
) -> ObservationIngestionReceipt:
    """Return a backend-only full-run receipt after exact ingestion verification.

    This is deliberately separate from :class:`VerifiedObservationSource`: the
    receipt contains full-artifact commitments and is not safe at a replay cursor.
    First-party UI code must consume only query results from the source facade.
    """

    evidence, receipt = _validate_ingestion_request(
        manifest_bytes,
        pinned_manifest_sha256,
        artifacts,
    )
    del evidence
    return replace(receipt)


def _validate_ingestion_request(
    manifest_bytes: bytes,
    pinned_manifest_sha256: str,
    artifacts: tuple[ObservedArtifactBytes, ...],
) -> tuple[ObservedEvidenceSet, ObservationIngestionReceipt]:
    if type(manifest_bytes) is not bytes:
        raise TypeError("observation ingestion manifest must be exact bytes")
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise ValueError("observation ingestion manifest exceeds the byte-size ceiling")
    _sha256(pinned_manifest_sha256, "pinned ingestion manifest SHA-256")
    actual_manifest_sha256 = _sha256_bytes(manifest_bytes)
    if actual_manifest_sha256 != pinned_manifest_sha256:
        raise ValueError(
            "observation ingestion manifest differs from its supplied pin"
        )
    if type(artifacts) is not tuple or any(
        type(item) is not ObservedArtifactBytes for item in artifacts
    ):
        raise TypeError("observed source artifacts must be a typed immutable tuple")
    if len(artifacts) != len(_ARTIFACT_KIND_ORDER):
        raise ValueError("observed source artifact inventory must contain two planes")
    return _ingest_verified(
        bytes(manifest_bytes),
        pinned_manifest_sha256,
        artifacts,
    )


def _ingest_verified(
    manifest_bytes: bytes,
    pinned_manifest_sha256: str,
    artifacts: tuple[ObservedArtifactBytes, ...],
) -> tuple[ObservedEvidenceSet, ObservationIngestionReceipt]:
    # The caller has already checked this before first parse.  Re-ingestion repeats
    # the check so the internal invariant does not depend on a previous invocation.
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise ValueError("observation ingestion manifest exceeds the byte-size ceiling")
    if _sha256_bytes(manifest_bytes) != pinned_manifest_sha256:
        raise ValueError("pinned observation manifest bytes changed")
    manifest = _parse_manifest(manifest_bytes)
    supplied = {item.artifact_id: item for item in artifacts}
    if len(supplied) != len(artifacts):
        raise ValueError("observed source artifact IDs are duplicated")
    expected_ids = {item.artifact_id for item in manifest.artifacts}
    if set(supplied) != expected_ids:
        raise ValueError("observed source artifact inventory differs from manifest")

    normalized: dict[_ArtifactKind, tuple[ObservedValueRecord, ...]] = {}
    for specification in manifest.artifacts:
        source = supplied[specification.artifact_id]
        if source.byte_length > _MAX_SOURCE_ARTIFACT_BYTES:
            raise ValueError(
                f"observed source artifact exceeds byte-size ceiling: "
                f"{source.artifact_id}"
            )
        if source.byte_length != specification.byte_length:
            raise ValueError(
                f"observed source artifact byte length differs: {source.artifact_id}"
            )
        if source.sha256 != specification.sha256:
            raise ValueError(
                f"observed source artifact digest differs: {source.artifact_id}"
            )
        normalized[specification.artifact_kind] = _parse_source_artifact(
            source.raw_bytes,
            manifest,
            specification,
        )

    evidence = ObservedEvidenceSet(
        source_run_id=manifest.source_run_id,
        source_event_sha256=manifest.source_event_sha256,
        client_delivered=normalized[_ArtifactKind.CLIENT_DELIVERED],
        decision_snapshots=normalized[_ArtifactKind.DECISION_SNAPSHOT],
    )
    normalized_digests = {
        _ArtifactKind.CLIENT_DELIVERED: evidence.client_delivered_artifact_sha256,
        _ArtifactKind.DECISION_SNAPSHOT: evidence.decision_snapshot_artifact_sha256,
    }
    for specification in manifest.artifacts:
        if (
            normalized_digests[specification.artifact_kind]
            != specification.normalized_plane_sha256
        ):
            raise ValueError(
                "normalized observed plane digest differs from manifest: "
                f"{specification.artifact_kind.value}"
            )

    by_kind = {item.artifact_kind: item for item in manifest.artifacts}
    delivered = by_kind[_ArtifactKind.CLIENT_DELIVERED]
    decisions = by_kind[_ArtifactKind.DECISION_SNAPSHOT]
    receipt = ObservationIngestionReceipt(
        source_run_id=manifest.source_run_id,
        source_event_sha256=manifest.source_event_sha256,
        manifest_sha256=pinned_manifest_sha256,
        evidence_sha256=evidence.evidence_sha256,
        client_delivered_artifact_id=delivered.artifact_id,
        client_delivered_artifact_schema_id=delivered.artifact_schema_id,
        client_delivered_artifact_schema_version=delivered.artifact_schema_version,
        client_delivered_raw_sha256=delivered.sha256,
        client_delivered_normalized_plane_sha256=(
            delivered.normalized_plane_sha256
        ),
        client_delivered_byte_length=delivered.byte_length,
        client_delivered_record_count=delivered.record_count,
        decision_snapshot_artifact_id=decisions.artifact_id,
        decision_snapshot_artifact_schema_id=decisions.artifact_schema_id,
        decision_snapshot_artifact_schema_version=decisions.artifact_schema_version,
        decision_snapshot_raw_sha256=decisions.sha256,
        decision_snapshot_normalized_plane_sha256=(
            decisions.normalized_plane_sha256
        ),
        decision_snapshot_byte_length=decisions.byte_length,
        decision_snapshot_record_count=decisions.record_count,
    )
    return evidence, receipt


def _parse_manifest(raw: bytes) -> _IngestManifest:
    payload = parse_canonical_json_object(raw)
    _exact_fields(
        payload,
        {
            "adapter_id",
            "adapter_version",
            "artifacts",
            "schema_id",
            "schema_version",
            "source_event_sha256",
            "source_run_id",
            "source_scope",
        },
        "observation ingestion manifest",
    )
    if _text(payload["adapter_id"], "adapter_id") != OBSERVED_INGEST_ADAPTER_ID:
        raise ValueError("unsupported observation ingestion adapter ID")
    if (
        _integer(payload["adapter_version"], "adapter_version", minimum=1)
        != OBSERVED_INGEST_ADAPTER_VERSION
    ):
        raise ValueError("unsupported observation ingestion adapter version")
    if (
        _text(payload["schema_id"], "schema_id")
        != OBSERVED_INGEST_MANIFEST_SCHEMA_ID
    ):
        raise ValueError("unsupported observation ingestion manifest schema ID")
    if (
        _integer(payload["schema_version"], "schema_version", minimum=1)
        != OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("unsupported observation ingestion manifest schema version")
    if _text(payload["source_scope"], "source_scope") != _OBSERVED_SOURCE_SCOPE:
        raise ValueError("observation ingestion source scope must be OBSERVED_ONLY")
    source_run_id = _text(payload["source_run_id"], "source_run_id")
    source_event_sha256 = _text(
        payload["source_event_sha256"], "source_event_sha256"
    )
    _source_identity(source_run_id, source_event_sha256)
    rows = payload["artifacts"]
    if type(rows) is not list or any(type(item) is not dict for item in rows):
        raise TypeError("ingestion manifest artifacts must be an object array")
    artifacts = tuple(_parse_manifest_artifact(item) for item in rows)
    if tuple(item.artifact_kind for item in artifacts) != _ARTIFACT_KIND_ORDER:
        raise ValueError(
            "ingestion manifest must contain CLIENT_DELIVERED then DECISION_SNAPSHOT"
        )
    artifact_ids = tuple(item.artifact_id for item in artifacts)
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("ingestion manifest artifact IDs must be distinct")
    return _IngestManifest(source_run_id, source_event_sha256, artifacts)


def _parse_manifest_artifact(payload: Mapping[str, object]) -> _ManifestArtifact:
    _exact_fields(
        payload,
        {
            "artifact_id",
            "artifact_kind",
            "artifact_schema_id",
            "artifact_schema_version",
            "byte_length",
            "normalized_plane_sha256",
            "record_count",
            "sha256",
        },
        "observation ingestion artifact",
    )
    artifact_id = _text(payload["artifact_id"], "artifact_id")
    _identifier(artifact_id, "observation ingestion artifact ID")
    try:
        artifact_kind = _ArtifactKind(
            _text(payload["artifact_kind"], "artifact_kind")
        )
    except ValueError as error:
        raise ValueError("unknown observation ingestion artifact kind") from error
    artifact_schema_id = _text(
        payload["artifact_schema_id"], "artifact_schema_id"
    )
    if artifact_schema_id != _ARTIFACT_SCHEMAS[artifact_kind]:
        raise ValueError("source artifact schema ID differs from its artifact kind")
    artifact_schema_version = _integer(
        payload["artifact_schema_version"],
        "artifact_schema_version",
        minimum=1,
    )
    if artifact_schema_version != OBSERVATION_QUERY_SCHEMA_VERSION:
        raise ValueError("unsupported observed source artifact schema version")
    normalized_plane_sha256 = _text(
        payload["normalized_plane_sha256"],
        "normalized_plane_sha256",
    )
    _sha256(normalized_plane_sha256, "normalized observed plane SHA-256")
    sha256 = _text(payload["sha256"], "sha256")
    _sha256(sha256, "observed source artifact SHA-256")
    return _ManifestArtifact(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        artifact_schema_id=artifact_schema_id,
        artifact_schema_version=artifact_schema_version,
        byte_length=_bounded_integer(
            payload["byte_length"],
            "byte_length",
            minimum=1,
            maximum=_MAX_SOURCE_ARTIFACT_BYTES,
        ),
        normalized_plane_sha256=normalized_plane_sha256,
        record_count=_integer(payload["record_count"], "record_count", minimum=0),
        sha256=sha256,
    )


def _parse_source_artifact(
    raw: bytes,
    manifest: _IngestManifest,
    specification: _ManifestArtifact,
) -> tuple[ObservedValueRecord, ...]:
    if len(raw) > _MAX_SOURCE_ARTIFACT_BYTES:
        raise ValueError("observed source artifact exceeds the byte-size ceiling")
    payload = parse_canonical_json_object(raw)
    _exact_fields(
        payload,
        {
            "artifact_schema_id",
            "artifact_schema_version",
            "records",
            "source_event_sha256",
            "source_run_id",
        },
        "observed source artifact",
    )
    artifact_schema_id = _text(
        payload["artifact_schema_id"],
        "artifact_schema_id",
    )
    if artifact_schema_id != specification.artifact_schema_id:
        raise ValueError("source artifact schema ID differs from manifest")
    artifact_schema_version = _integer(
        payload["artifact_schema_version"],
        "artifact_schema_version",
        minimum=1,
    )
    if artifact_schema_version != specification.artifact_schema_version:
        raise ValueError("source artifact schema version differs from manifest")
    source_run_id = _text(payload["source_run_id"], "source_run_id")
    if source_run_id != manifest.source_run_id:
        raise ValueError("source artifact run ID differs from manifest")
    source_event_sha256 = _text(
        payload["source_event_sha256"],
        "source_event_sha256",
    )
    if source_event_sha256 != manifest.source_event_sha256:
        raise ValueError("source artifact event digest differs from manifest")
    rows = payload["records"]
    if type(rows) is not list or any(type(item) is not dict for item in rows):
        raise TypeError("observed source records must be an object array")
    if len(rows) != specification.record_count:
        raise ValueError("observed source artifact record count differs from manifest")
    records = tuple(
        _parse_observed_record(item, specification.artifact_kind) for item in rows
    )
    sequences = tuple(item.sequence for item in records)
    if any(right <= left for left, right in zip(sequences, sequences[1:])):
        raise ValueError("raw observed source sequences must be strictly increasing")
    return records


def _parse_observed_record(
    payload: Mapping[str, object],
    artifact_kind: _ArtifactKind,
) -> ObservedValueRecord:
    _exact_fields(
        payload,
        {
            "disposition",
            "event_id",
            "payload",
            "payload_sha256",
            "record_kind",
            "sequence",
            "series_id",
            "timing",
        },
        "observed source record",
    )
    try:
        record_kind = _RecordKind(_text(payload["record_kind"], "record_kind"))
    except ValueError as error:
        raise ValueError("unknown observed source record kind") from error
    contract = _RECORD_CONTRACTS[record_kind]
    if contract.artifact_kind is not artifact_kind:
        raise ValueError("observed record kind is stored in the wrong artifact plane")
    series_id = _text(payload["series_id"], "series_id")
    if not contract.series_matches(series_id):
        raise ValueError("observed record series is invalid for its record kind")
    value_payload = payload["payload"]
    if not isinstance(value_payload, Mapping):
        raise TypeError("observed source record payload must be an object")
    _validate_record_payload(value_payload, contract)
    supplied_payload_sha256 = _text(
        payload["payload_sha256"], "payload_sha256"
    )
    _sha256(supplied_payload_sha256, "observed source payload SHA-256")
    if _sha256_bytes(canonical_json_bytes(value_payload)) != supplied_payload_sha256:
        raise ValueError("observed source payload digest differs")
    try:
        disposition = RecordDisposition(
            _text(payload["disposition"], "disposition")
        )
    except ValueError as error:
        raise ValueError("unknown observed record disposition") from error
    if disposition is not RecordDisposition.VALUE:
        raise ValueError(
            "current observed source record kinds require VALUE disposition"
        )
    timing_payload = payload["timing"]
    if not isinstance(timing_payload, Mapping):
        raise TypeError("observed source record timing must be an object")
    timing = _parse_timing(timing_payload)
    _validate_record_timing(record_kind, value_payload, timing)
    record = ObservedValueRecord(
        series_id=series_id,
        event_id=_text(payload["event_id"], "event_id"),
        sequence=_integer(payload["sequence"], "sequence", minimum=1),
        timing=timing,
        payload=dict(value_payload),
        disposition=disposition,
    )
    expected = {**record.as_dict(), "record_kind": record_kind.value}
    if expected != dict(payload):
        raise ValueError("observed source record is not an exact typed round trip")
    return record


def _validate_record_payload(
    payload: Mapping[str, object],
    contract: _RecordContract,
) -> None:
    fields = {item.name for item in contract.payload_fields}
    _exact_fields(payload, fields, "observed record payload")
    for item in contract.payload_fields:
        item.validator(payload[item.name], item.name)


def _validate_record_timing(
    record_kind: _RecordKind,
    payload: Mapping[str, object],
    timing: EvidenceTiming,
) -> None:
    if _RECORD_CONTRACTS[record_kind].artifact_kind is _ArtifactKind.CLIENT_DELIVERED:
        _validate_client_delivered_timing(timing)
        return
    if record_kind is _RecordKind.STRATEGY_SIGNAL:
        _require_absent_timestamp(
            timing.venue_receipt,
            TimestampAvailability.NOT_APPLICABLE,
            TimestampAbsenceReason.CLIENT_DECISION,
            "strategy signal venue receipt",
        )
        _require_absent_timestamp(
            timing.client_receive,
            TimestampAvailability.NOT_APPLICABLE,
            TimestampAbsenceReason.RECORDED_SNAPSHOT,
            "strategy signal client receipt",
        )
        _require_recorded_timestamp(
            timing.client_knowledge,
            "strategy signal client knowledge",
        )
        return
    if record_kind is not _RecordKind.CLIENT_ORDER_INTENTION:
        return
    _require_absent_timestamp(
        timing.client_receive,
        TimestampAvailability.NOT_APPLICABLE,
        TimestampAbsenceReason.OUTBOUND_CLIENT_INTENTION,
        "client order intention client receipt",
    )
    _require_recorded_timestamp(
        timing.client_knowledge,
        "client order intention client knowledge",
    )
    if payload["venue_state"] == "NOT_OBSERVED":
        _require_absent_timestamp(
            timing.venue_receipt,
            TimestampAvailability.UNAVAILABLE,
            TimestampAbsenceReason.NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE,
            "unobserved client order intention venue receipt",
        )
    else:
        _require_recorded_timestamp(
            timing.venue_receipt,
            "received client order intention venue receipt",
        )


def _validate_client_delivered_timing(timing: EvidenceTiming) -> None:
    _require_recorded_timestamp(
        timing.client_receive,
        "client-delivered client receipt",
    )
    _require_recorded_timestamp(
        timing.client_knowledge,
        "client-delivered client knowledge",
    )
    venue_receipt = timing.venue_receipt
    if venue_receipt.availability is TimestampAvailability.RECORDED:
        _require_recorded_timestamp(
            venue_receipt,
            "client-delivered venue receipt",
        )
        if (
            venue_receipt.time_us is None
            or timing.client_receive.time_us is None
        ):  # pragma: no cover - guarded by exact timestamp validation
            raise RuntimeError("recorded inbound timing lost its timestamp")
        if venue_receipt.time_us > timing.client_receive.time_us:
            raise ValueError(
                "client-delivered venue receipt follows the client receipt"
            )
        return
    if venue_receipt.availability is TimestampAvailability.UNAVAILABLE:
        _require_absent_timestamp(
            venue_receipt,
            TimestampAvailability.UNAVAILABLE,
            TimestampAbsenceReason.NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE,
            "client-delivered venue receipt",
        )
        return
    _require_absent_timestamp(
        venue_receipt,
        TimestampAvailability.NOT_APPLICABLE,
        TimestampAbsenceReason.NO_VENUE_HOP,
        "client-delivered venue receipt",
    )


def _require_absent_timestamp(
    timestamp: EvidenceTimestamp,
    availability: TimestampAvailability,
    reason: TimestampAbsenceReason,
    context: str,
) -> None:
    if timestamp.availability is not availability or timestamp.reason is not reason:
        raise ValueError(f"{context} has incompatible absence semantics")


def _require_recorded_timestamp(
    timestamp: EvidenceTimestamp,
    context: str,
) -> None:
    if timestamp.availability is not TimestampAvailability.RECORDED:
        raise ValueError(f"{context} must be recorded")


def _parse_timing(payload: Mapping[str, object]) -> EvidenceTiming:
    _exact_fields(
        payload,
        {
            "client_knowledge",
            "client_receive",
            "source_event_time_us",
            "venue_receipt",
        },
        "observed evidence timing",
    )
    timestamps: dict[str, EvidenceTimestamp] = {}
    for field_name in ("venue_receipt", "client_receive", "client_knowledge"):
        row = payload[field_name]
        if not isinstance(row, Mapping):
            raise TypeError(f"{field_name} must be an evidence timestamp object")
        timestamps[field_name] = _parse_timestamp(row)
    timing = EvidenceTiming(
        source_event_time_us=_integer(
            payload["source_event_time_us"],
            "source_event_time_us",
            minimum=0,
        ),
        venue_receipt=timestamps["venue_receipt"],
        client_receive=timestamps["client_receive"],
        client_knowledge=timestamps["client_knowledge"],
    )
    if timing.as_dict() != dict(payload):
        raise ValueError("observed evidence timing is not an exact typed round trip")
    return timing


def _parse_timestamp(payload: Mapping[str, object]) -> EvidenceTimestamp:
    _exact_fields(
        payload,
        {"availability", "reason", "time_us"},
        "evidence timestamp",
    )
    try:
        availability = TimestampAvailability(
            _text(payload["availability"], "availability")
        )
    except ValueError as error:
        raise ValueError("unknown evidence timestamp availability") from error
    raw_reason = payload["reason"]
    if raw_reason is None:
        reason = None
    else:
        try:
            reason = TimestampAbsenceReason(_text(raw_reason, "reason"))
        except ValueError as error:
            raise ValueError("unknown evidence timestamp absence reason") from error
    raw_time = payload["time_us"]
    time_us = (
        None
        if raw_time is None
        else _integer(raw_time, "time_us", minimum=0)
    )
    timestamp = EvidenceTimestamp(availability, time_us, reason)
    if timestamp.as_dict() != dict(payload):
        raise ValueError("evidence timestamp is not an exact typed round trip")
    return timestamp


def _validate_receipt_plane(
    kind: _ArtifactKind,
    artifact_id: str,
    artifact_schema_id: str,
    artifact_schema_version: int,
    raw_sha256: str,
    normalized_sha256: str,
    byte_length: int,
    record_count: int,
) -> None:
    _identifier(artifact_id, "ingestion receipt artifact ID")
    if artifact_schema_id != _ARTIFACT_SCHEMAS[kind]:
        raise ValueError("ingestion receipt artifact schema differs from its kind")
    if (
        type(artifact_schema_version) is not int
        or artifact_schema_version != OBSERVATION_QUERY_SCHEMA_VERSION
    ):
        raise ValueError("ingestion receipt artifact schema version is unsupported")
    _sha256(raw_sha256, "ingestion receipt raw artifact SHA-256")
    _sha256(normalized_sha256, "ingestion receipt normalized plane SHA-256")
    if type(byte_length) is not int or byte_length <= 0:
        raise ValueError("ingestion receipt artifact byte length must be positive")
    if type(record_count) is not int or record_count < 0:
        raise ValueError("ingestion receipt record count must be nonnegative")


def _exact_fields(
    payload: Mapping[str, object],
    expected: set[str],
    context: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{context} must be an object")
    actual = set(payload)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise ValueError(
            f"{context} fields are not exact: missing={missing} unknown={unknown}"
        )


def _integer(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _bounded_integer(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    parsed = _integer(value, field_name, minimum=minimum)
    if parsed > maximum:
        raise ValueError(f"{field_name} must be an integer <= {maximum}")
    return parsed


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be nonempty text")
    return value


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _source_identity(source_run_id: str, source_event_sha256: str) -> None:
    if _RUN_ID.fullmatch(source_run_id) is None:
        raise ValueError("observation ingestion source run ID is invalid")
    _sha256(source_event_sha256, "observation ingestion source event SHA-256")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# Raw evidence constructors and record-kind registries remain backend-internal.
__all__ = [
    "OBSERVED_INGEST_ADAPTER_ID",
    "OBSERVED_INGEST_ADAPTER_VERSION",
    "OBSERVED_INGEST_MANIFEST_SCHEMA_ID",
    "OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION",
    "ObservedArtifactBytes",
    "ObservationIngestionReceipt",
    "VerifiedObservationSource",
    "load_verified_observation_source",
    "verify_observation_ingestion",
]
