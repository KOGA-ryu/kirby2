"""TLS 1.3, session replay, and sealed-stage access policy for WO38-D.

This module composes Python's standard TLS implementation; it defines no cipher,
signature, certificate format, or bespoke message authentication.  Session hashes and
nonces are correlation/replay identities only.  Production credentials must be
operator supplied outside the repository, and known committed audit credentials are
refused by certificate fingerprint even when copied elsewhere.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import ssl
import stat
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

from kirby2.discovery.access import (
    PartitionAccessDecisionV1,
    PartitionAccessPurposeV1,
    PartitionAccessRecordV1,
)
from kirby2.discovery.experiment import ExperimentPhaseV1
from kirby2.discovery.partitions import StrategyPartitionV1
from kirby2.orchestration.artifacts import ContentRequestV1
from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes


ORCHESTRATION_SECURITY_SCHEMA_VERSION = 1
LAN_PROTOCOL_ID_V1 = "kirby2-orchestration/1"
SESSION_HELLO_SCHEMA_ID = "KIRBY2_ORCHESTRATION_SESSION_HELLO_V1"
AUTHENTICATED_SESSION_SCHEMA_ID = "KIRBY2_AUTHENTICATED_LAN_SESSION_V1"
ARTIFACT_ACCESS_SCOPE_SCHEMA_ID = "KIRBY2_ARTIFACT_ACCESS_SCOPE_V1"

DEFAULT_LAN_BIND_HOST_V1 = "127.0.0.1"
MAX_LAN_PORT_V1 = 65535
MAX_SESSION_SEQUENCE_V1 = (1 << 63) - 1
MAX_SESSION_NONCES_V1 = 1_000_000

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_PKI_ROOT_V1 = _PACKAGE_ROOT / "orchestration" / "fixtures" / "test_pki"

# Frozen committed fixture leaf/CA certificate fingerprints. Production checks use
# the bytes, not only their path, so copying a fixture does not bless it.
TEST_PKI_CERTIFICATE_SHA256S_V1 = frozenset(
    {
        "4d051b678b0a33e4f8a3551a103aa7c59e2588a4d8dbbfaf1b3b86a3ddfc2cc5",
        "69cf08cebebc04510d2b0fc79e463f1ab065a586e716201f4ba67fc2992a2b03",
        "cc5c08853f08fa22f8af3e8446b30d01b37ac71dbe3af786d099cc0dfd2e7d3f",
    }
)


class LanPeerRoleV1(str, Enum):
    COORDINATOR = "COORDINATOR"
    WORKER = "WORKER"


class CredentialUseV1(str, Enum):
    OPERATOR_PRODUCTION = "OPERATOR_PRODUCTION"
    AUDIT_LOOPBACK_FIXTURE = "AUDIT_LOOPBACK_FIXTURE"


class SecurityRefusalCodeV1(str, Enum):
    TLS13_UNSUPPORTED = "TLS13_UNSUPPORTED"
    LAN_NOT_EXPLICITLY_ENABLED = "LAN_NOT_EXPLICITLY_ENABLED"
    NON_LOOPBACK_DEFAULT_REFUSED = "NON_LOOPBACK_DEFAULT_REFUSED"
    CREDENTIAL_PATH_UNSAFE = "CREDENTIAL_PATH_UNSAFE"
    TEST_CREDENTIAL_REFUSED = "TEST_CREDENTIAL_REFUSED"
    TLS_CONFIGURATION_INVALID = "TLS_CONFIGURATION_INVALID"
    TLS_NEGOTIATION_INVALID = "TLS_NEGOTIATION_INVALID"
    CERTIFICATE_PIN_MISMATCH = "CERTIFICATE_PIN_MISMATCH"
    CERTIFICATE_IDENTITY_MISMATCH = "CERTIFICATE_IDENTITY_MISMATCH"
    SESSION_BINDING_MISMATCH = "SESSION_BINDING_MISMATCH"
    SESSION_REPLAY = "SESSION_REPLAY"
    SESSION_SEQUENCE_INVALID = "SESSION_SEQUENCE_INVALID"
    SEALED_ARTIFACT_REFUSED = "SEALED_ARTIFACT_REFUSED"


class SecurityRefused(RuntimeError):
    def __init__(self, code: SecurityRefusalCodeV1, detail: str) -> None:
        if type(code) is not SecurityRefusalCodeV1:
            raise TypeError("security refusal code is invalid")
        if type(detail) is not str or not detail or len(detail.encode("utf-8")) > 4096:
            raise ValueError("security refusal detail must be bounded text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class LanTlsConfigurationV1:
    role: LanPeerRoleV1
    enabled: bool
    host: str
    port: int
    ca_certificate: Path
    certificate: Path
    private_key: Path
    local_identity: str
    expected_peer_identities: tuple[str, ...]
    credential_use: CredentialUseV1
    server_hostname: str | None = None
    pinned_coordinator_certificate_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.role) is not LanPeerRoleV1:
            raise TypeError("LAN TLS role is invalid")
        if type(self.enabled) is not bool:
            raise TypeError("LAN enabled flag must be boolean")
        _host(self.host)
        if type(self.port) is not int or not 1 <= self.port <= MAX_LAN_PORT_V1:
            raise ValueError("LAN port must be in [1, 65535]")
        for value, label in (
            (self.ca_certificate, "LAN CA certificate"),
            (self.certificate, "LAN certificate"),
            (self.private_key, "LAN private key"),
        ):
            if type(value) is not Path:
                raise TypeError(f"{label} must be pathlib.Path")
        _identifier(self.local_identity, "LAN local identity")
        _canonical_identities(
            self.expected_peer_identities,
            "LAN expected peer identities",
        )
        if type(self.credential_use) is not CredentialUseV1:
            raise TypeError("LAN credential use is invalid")
        if self.role is LanPeerRoleV1.COORDINATOR:
            if (
                self.server_hostname is not None
                or self.pinned_coordinator_certificate_sha256 is not None
            ):
                raise ValueError("coordinator server cannot pin or name itself as peer")
        else:
            if type(self.server_hostname) is not str or not self.server_hostname:
                raise ValueError("worker client requires coordinator server hostname")
            _host(self.server_hostname)
            if self.pinned_coordinator_certificate_sha256 is None:
                raise ValueError("worker client requires pinned coordinator certificate")
            _sha256(
                self.pinned_coordinator_certificate_sha256,
                "pinned coordinator certificate digest",
            )


@dataclass(frozen=True, slots=True)
class SessionHelloV1:
    role: LanPeerRoleV1
    peer_identity: str
    certificate_sha256: str
    hello_nonce: str
    protocol_sha256: str
    compatibility_sha256: str | None
    resource_advertisement_sha256: str | None

    schema_id: ClassVar[str] = SESSION_HELLO_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_SECURITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.role) is not LanPeerRoleV1:
            raise TypeError("session hello role is invalid")
        _identifier(self.peer_identity, "session hello peer identity")
        _sha256(self.certificate_sha256, "session hello certificate digest")
        _sha256(self.hello_nonce, "session hello nonce")
        _sha256(self.protocol_sha256, "session hello protocol digest")
        for value, label in (
            (self.compatibility_sha256, "session hello compatibility digest"),
            (
                self.resource_advertisement_sha256,
                "session hello resource advertisement digest",
            ),
        ):
            if value is not None:
                _sha256(value, label)
        if self.role is LanPeerRoleV1.COORDINATOR:
            if self.compatibility_sha256 is not None or self.resource_advertisement_sha256 is not None:
                raise ValueError("coordinator hello cannot advertise worker execution state")
        elif self.compatibility_sha256 is None or self.resource_advertisement_sha256 is None:
            raise ValueError("worker hello requires compatibility and resource identities")

    def as_dict(self) -> dict[str, object]:
        return {
            "certificate_sha256": self.certificate_sha256,
            "compatibility_sha256": self.compatibility_sha256,
            "hello_nonce": self.hello_nonce,
            "peer_identity": self.peer_identity,
            "protocol_sha256": self.protocol_sha256,
            "resource_advertisement_sha256": self.resource_advertisement_sha256,
            "role": self.role.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> SessionHelloV1:
        row = _exact(
            value,
            {
                "certificate_sha256",
                "compatibility_sha256",
                "hello_nonce",
                "peer_identity",
                "protocol_sha256",
                "resource_advertisement_sha256",
                "role",
                "schema_id",
                "schema_version",
            },
            "session hello",
        )
        _schema(row, cls.schema_id, "session hello")
        restored = cls(
            role=LanPeerRoleV1(_text(row, "role")),
            peer_identity=_text(row, "peer_identity"),
            certificate_sha256=_text(row, "certificate_sha256"),
            hello_nonce=_text(row, "hello_nonce"),
            protocol_sha256=_text(row, "protocol_sha256"),
            compatibility_sha256=_optional_text(row, "compatibility_sha256"),
            resource_advertisement_sha256=_optional_text(
                row,
                "resource_advertisement_sha256",
            ),
        )
        _round_trip(restored, row, "session hello")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> SessionHelloV1:
        if type(raw) is not bytes or not raw or len(raw) > 64 * 1024:
            raise ValueError("session hello bytes exceed the bounded limit")
        restored = cls.from_dict(load_canonical_json_bytes(raw, "session hello"))
        if restored.canonical_bytes() != raw:
            raise ValueError("session hello bytes are not canonical")
        return restored


@dataclass(frozen=True, slots=True)
class AuthenticatedSessionV1:
    session_id: str
    coordinator_identity: str
    worker_identity: str
    coordinator_certificate_sha256: str
    worker_certificate_sha256: str
    protocol_sha256: str

    schema_id: ClassVar[str] = AUTHENTICATED_SESSION_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_SECURITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.session_id, "authenticated session ID")
        _identifier(self.coordinator_identity, "authenticated coordinator identity")
        _identifier(self.worker_identity, "authenticated worker identity")
        _sha256(
            self.coordinator_certificate_sha256,
            "authenticated coordinator certificate",
        )
        _sha256(self.worker_certificate_sha256, "authenticated worker certificate")
        _sha256(self.protocol_sha256, "authenticated session protocol")

    def as_dict(self) -> dict[str, object]:
        return {
            "coordinator_certificate_sha256": self.coordinator_certificate_sha256,
            "coordinator_identity": self.coordinator_identity,
            "protocol_sha256": self.protocol_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "worker_certificate_sha256": self.worker_certificate_sha256,
            "worker_identity": self.worker_identity,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> AuthenticatedSessionV1:
        row = _exact(
            value,
            {
                "coordinator_certificate_sha256",
                "coordinator_identity",
                "protocol_sha256",
                "schema_id",
                "schema_version",
                "session_id",
                "worker_certificate_sha256",
                "worker_identity",
            },
            "authenticated session",
        )
        _schema(row, cls.schema_id, "authenticated session")
        restored = cls(
            session_id=_text(row, "session_id"),
            coordinator_identity=_text(row, "coordinator_identity"),
            worker_identity=_text(row, "worker_identity"),
            coordinator_certificate_sha256=_text(
                row,
                "coordinator_certificate_sha256",
            ),
            worker_certificate_sha256=_text(row, "worker_certificate_sha256"),
            protocol_sha256=_text(row, "protocol_sha256"),
        )
        _round_trip(restored, row, "authenticated session")
        return restored


@dataclass(frozen=True, slots=True)
class ArtifactAccessScopeV1:
    experiment_id: str
    experiment_version: int
    phase: ExperimentPhaseV1
    purpose: PartitionAccessPurposeV1
    partition: StrategyPartitionV1
    access_record_sha256: str | None

    schema_id: ClassVar[str] = ARTIFACT_ACCESS_SCOPE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_SECURITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.experiment_id, "artifact access experiment ID")
        if type(self.experiment_version) is not int or self.experiment_version <= 0:
            raise ValueError("artifact access experiment version must be positive")
        if type(self.phase) is not ExperimentPhaseV1:
            raise TypeError("artifact access experiment phase is invalid")
        if type(self.purpose) is not PartitionAccessPurposeV1:
            raise TypeError("artifact access purpose is invalid")
        if type(self.partition) is not StrategyPartitionV1:
            raise TypeError("artifact access partition is invalid")
        if self.access_record_sha256 is not None:
            _sha256(self.access_record_sha256, "artifact access record digest")
        sealed = self.partition in {
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        }
        if sealed and self.phase is ExperimentPhaseV1.SEARCH_OPEN:
            if self.access_record_sha256 is not None:
                raise ValueError("search-open scope cannot carry a sealed access record")

    def as_dict(self) -> dict[str, object]:
        return {
            "access_record_sha256": self.access_record_sha256,
            "experiment_id": self.experiment_id,
            "experiment_version": self.experiment_version,
            "partition": self.partition.value,
            "phase": self.phase.value,
            "purpose": self.purpose.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ArtifactAccessScopeV1:
        row = _exact(
            value,
            {
                "access_record_sha256",
                "experiment_id",
                "experiment_version",
                "partition",
                "phase",
                "purpose",
                "schema_id",
                "schema_version",
            },
            "artifact access scope",
        )
        _schema(row, cls.schema_id, "artifact access scope")
        raw_version = row["experiment_version"]
        if type(raw_version) is not int:
            raise TypeError("serialized experiment version must be an exact integer")
        restored = cls(
            experiment_id=_text(row, "experiment_id"),
            experiment_version=raw_version,
            phase=ExperimentPhaseV1(_text(row, "phase")),
            purpose=PartitionAccessPurposeV1(_text(row, "purpose")),
            partition=StrategyPartitionV1(_text(row, "partition")),
            access_record_sha256=_optional_text(row, "access_record_sha256"),
        )
        _round_trip(restored, row, "artifact access scope")
        return restored

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> ArtifactAccessScopeV1:
        if type(raw) is not bytes or not raw or len(raw) > 64 * 1024:
            raise ValueError("artifact access scope bytes exceed the bounded limit")
        restored = cls.from_dict(
            load_canonical_json_bytes(raw, "artifact access scope")
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("artifact access scope bytes are not canonical")
        return restored

    @classmethod
    def from_access_record(
        cls,
        record: PartitionAccessRecordV1,
    ) -> ArtifactAccessScopeV1:
        if type(record) is not PartitionAccessRecordV1:
            raise TypeError("artifact access scope requires PartitionAccessRecordV1")
        if record.decision is not PartitionAccessDecisionV1.GRANTED:
            raise SecurityRefused(
                SecurityRefusalCodeV1.SEALED_ARTIFACT_REFUSED,
                "refused WO35 access record cannot authorize artifact transfer",
            )
        return cls(
            experiment_id=record.experiment_id,
            experiment_version=record.experiment_version,
            phase=record.phase_after,
            purpose=record.purpose,
            partition=record.partition,
            access_record_sha256=record.access_sha256,
        )


class SessionReplayGuardV1:
    """Accept exactly increasing per-session sequences and unique message nonces."""

    __slots__ = ("_lock", "_next", "_nonces", "_session_id")

    def __init__(self, session_id: str) -> None:
        self._session_id = _sha256(session_id, "replay guard session ID")
        self._next = 1
        self._nonces: set[str] = set()
        self._lock = threading.Lock()

    def accept(self, *, session_id: str, sequence: int, nonce: str) -> None:
        session = _sha256(session_id, "message session ID")
        token = _sha256(nonce, "message nonce")
        if type(sequence) is not int or not 1 <= sequence <= MAX_SESSION_SEQUENCE_V1:
            raise SecurityRefused(
                SecurityRefusalCodeV1.SESSION_SEQUENCE_INVALID,
                "message sequence is outside the V1 range",
            )
        with self._lock:
            if not hmac.compare_digest(session, self._session_id):
                raise SecurityRefused(
                    SecurityRefusalCodeV1.SESSION_BINDING_MISMATCH,
                    "message is bound to another authenticated session",
                )
            if token in self._nonces:
                raise SecurityRefused(
                    SecurityRefusalCodeV1.SESSION_REPLAY,
                    "message nonce was already accepted",
                )
            if sequence != self._next:
                raise SecurityRefused(
                    SecurityRefusalCodeV1.SESSION_SEQUENCE_INVALID,
                    "message sequence is not the next exact session value",
                )
            if len(self._nonces) >= MAX_SESSION_NONCES_V1:
                raise SecurityRefused(
                    SecurityRefusalCodeV1.SESSION_SEQUENCE_INVALID,
                    "session message inventory reached the V1 hard limit",
                )
            self._nonces.add(token)
            self._next += 1


def protocol_sha256() -> str:
    from .protocol import (
        LAN_PROTOCOL_ENVELOPE_SCHEMA_ID,
        MAX_LAN_ENVELOPE_BYTES_V1,
        MAX_LAN_PAYLOAD_BYTES_V1,
        MAX_LAN_SESSION_SEQUENCE_V1,
        LanMessageKindV1,
    )

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "alpn": LAN_PROTOCOL_ID_V1,
                "envelope_schema_id": LAN_PROTOCOL_ENVELOPE_SCHEMA_ID,
                "maximum_envelope_bytes": MAX_LAN_ENVELOPE_BYTES_V1,
                "maximum_payload_bytes": MAX_LAN_PAYLOAD_BYTES_V1,
                "maximum_sequence": min(
                    MAX_SESSION_SEQUENCE_V1,
                    MAX_LAN_SESSION_SEQUENCE_V1,
                ),
                "message_kinds": [item.value for item in LanMessageKindV1],
                "schema_id": "KIRBY2_AUTHENTICATED_LAN_PROTOCOL_V1",
                "schema_version": ORCHESTRATION_SECURITY_SCHEMA_VERSION,
                "tls_minimum": "TLSv1.3",
                "tls_maximum": "TLSv1.3",
            }
        )
    ).hexdigest()


def derive_authenticated_session(
    coordinator: SessionHelloV1,
    worker: SessionHelloV1,
) -> AuthenticatedSessionV1:
    if type(coordinator) is not SessionHelloV1 or type(worker) is not SessionHelloV1:
        raise TypeError("authenticated session requires two typed hello records")
    if (
        coordinator.role is not LanPeerRoleV1.COORDINATOR
        or worker.role is not LanPeerRoleV1.WORKER
        or coordinator.protocol_sha256 != protocol_sha256()
        or worker.protocol_sha256 != protocol_sha256()
    ):
        raise SecurityRefused(
            SecurityRefusalCodeV1.SESSION_BINDING_MISMATCH,
            "session hellos do not bind the one supported protocol and role order",
        )
    session_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "coordinator": coordinator.as_dict(),
                "schema_id": "KIRBY2_AUTHENTICATED_LAN_SESSION_ID_V1",
                "schema_version": 1,
                "worker": worker.as_dict(),
            }
        )
    ).hexdigest()
    return AuthenticatedSessionV1(
        session_id=session_id,
        coordinator_identity=coordinator.peer_identity,
        worker_identity=worker.peer_identity,
        coordinator_certificate_sha256=coordinator.certificate_sha256,
        worker_certificate_sha256=worker.certificate_sha256,
        protocol_sha256=coordinator.protocol_sha256,
    )


def build_server_ssl_context(
    configuration: LanTlsConfigurationV1,
    *,
    allow_audit_fixture: bool = False,
) -> ssl.SSLContext:
    if type(configuration) is not LanTlsConfigurationV1:
        raise TypeError("server TLS context requires LanTlsConfigurationV1")
    if configuration.role is not LanPeerRoleV1.COORDINATOR:
        raise ValueError("server TLS context requires coordinator role")
    _validate_tls_configuration(configuration, allow_audit_fixture=allow_audit_fixture)
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        _pin_tls13(context)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = False
        context.load_verify_locations(cafile=str(configuration.ca_certificate))
        context.load_cert_chain(
            certfile=str(configuration.certificate),
            keyfile=str(configuration.private_key),
        )
        context.set_alpn_protocols([LAN_PROTOCOL_ID_V1])
        if hasattr(ssl, "OP_NO_COMPRESSION"):
            context.options |= ssl.OP_NO_COMPRESSION
        return context
    except (OSError, ssl.SSLError, ValueError) as error:
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS_CONFIGURATION_INVALID,
            "coordinator TLS context could not be configured",
        ) from error


def build_client_ssl_context(
    configuration: LanTlsConfigurationV1,
    *,
    allow_audit_fixture: bool = False,
) -> ssl.SSLContext:
    if type(configuration) is not LanTlsConfigurationV1:
        raise TypeError("client TLS context requires LanTlsConfigurationV1")
    if configuration.role is not LanPeerRoleV1.WORKER:
        raise ValueError("client TLS context requires worker role")
    _validate_tls_configuration(configuration, allow_audit_fixture=allow_audit_fixture)
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        _pin_tls13(context)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cafile=str(configuration.ca_certificate))
        context.load_cert_chain(
            certfile=str(configuration.certificate),
            keyfile=str(configuration.private_key),
        )
        context.set_alpn_protocols([LAN_PROTOCOL_ID_V1])
        if hasattr(ssl, "OP_NO_COMPRESSION"):
            context.options |= ssl.OP_NO_COMPRESSION
        return context
    except (OSError, ssl.SSLError, ValueError) as error:
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS_CONFIGURATION_INVALID,
            "worker TLS context could not be configured",
        ) from error


def verify_negotiated_tls_peer(
    connection: ssl.SSLSocket,
    configuration: LanTlsConfigurationV1,
) -> str:
    if not isinstance(connection, ssl.SSLSocket):
        raise TypeError("TLS peer verification requires ssl.SSLSocket")
    if type(configuration) is not LanTlsConfigurationV1:
        raise TypeError("TLS peer verification requires LanTlsConfigurationV1")
    if (
        connection.version() != "TLSv1.3"
        or connection.selected_alpn_protocol() != LAN_PROTOCOL_ID_V1
    ):
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS_NEGOTIATION_INVALID,
            "connection did not negotiate exact TLS 1.3 and Kirby2 ALPN",
        )
    certificate_der = connection.getpeercert(binary_form=True)
    if type(certificate_der) is not bytes or not certificate_der:
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS_NEGOTIATION_INVALID,
            "authenticated peer certificate is unavailable",
        )
    fingerprint = hashlib.sha256(certificate_der).hexdigest()
    if configuration.role is LanPeerRoleV1.WORKER:
        expected = configuration.pinned_coordinator_certificate_sha256
        if expected is None or not hmac.compare_digest(fingerprint, expected):
            raise SecurityRefused(
                SecurityRefusalCodeV1.CERTIFICATE_PIN_MISMATCH,
                "coordinator certificate differs from the operator pin",
            )
    peer_certificate = connection.getpeercert(binary_form=False)
    if type(peer_certificate) is not dict:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CERTIFICATE_IDENTITY_MISMATCH,
            "peer certificate identity fields are unavailable",
        )
    identities = _certificate_identities(peer_certificate)
    if not set(configuration.expected_peer_identities) & identities:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CERTIFICATE_IDENTITY_MISMATCH,
            "peer certificate does not contain an expected exact identity",
        )
    return fingerprint


def validate_artifact_access(
    request: ContentRequestV1,
    scope: ArtifactAccessScopeV1,
    *,
    sealed_content_sha256s: tuple[str, ...],
    access_record: PartitionAccessRecordV1 | None = None,
) -> ContentRequestV1:
    if type(request) is not ContentRequestV1:
        raise TypeError("artifact access validation requires ContentRequestV1")
    if type(scope) is not ArtifactAccessScopeV1:
        raise TypeError("artifact access validation requires ArtifactAccessScopeV1")
    _canonical_digests(sealed_content_sha256s, "sealed content identities")
    requested = {item.sha256 for item in request.content_references}
    touches_sealed = bool(requested & set(sealed_content_sha256s))
    if not touches_sealed:
        return request
    search_purpose = scope.purpose in {
        PartitionAccessPurposeV1.SEARCH_TRAIN,
        PartitionAccessPurposeV1.SEARCH_VALIDATION,
    }
    access_matches = (
        type(access_record) is PartitionAccessRecordV1
        and access_record.decision is PartitionAccessDecisionV1.GRANTED
        and access_record.experiment_id == scope.experiment_id
        and access_record.experiment_version == scope.experiment_version
        and access_record.phase_after is scope.phase
        and access_record.purpose is scope.purpose
        and access_record.partition is scope.partition
        and scope.access_record_sha256 is not None
        and hmac.compare_digest(
            access_record.access_sha256,
            scope.access_record_sha256,
        )
    )
    allowed_terminal = (
        scope.phase is ExperimentPhaseV1.TERMINAL_EVALUATION
        and scope.purpose
        in {
            PartitionAccessPurposeV1.HOLDOUT_REVEAL,
            PartitionAccessPurposeV1.TERMINAL_EVALUATION,
        }
        and scope.partition
        in {
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        }
        and scope.access_record_sha256 is not None
        and access_matches
    )
    if search_purpose or not allowed_terminal:
        raise SecurityRefused(
            SecurityRefusalCodeV1.SEALED_ARTIFACT_REFUSED,
            "active search scope cannot receive sealed terminal content",
        )
    return request


def certificate_sha256(path: Path) -> str:
    resolved = _regular_file(path, "certificate", private=False)
    try:
        pem = resolved.read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, UnicodeError, ValueError, ssl.SSLError) as error:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            "certificate is not one readable PEM certificate",
        ) from error
    if type(der) is not bytes or not der:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            "certificate decoder did not return one DER certificate",
        )
    return hashlib.sha256(der).hexdigest()


def _validate_tls_configuration(
    configuration: LanTlsConfigurationV1,
    *,
    allow_audit_fixture: bool,
) -> None:
    if type(allow_audit_fixture) is not bool:
        raise TypeError("audit-fixture opt-in must be boolean")
    if not configuration.enabled:
        raise SecurityRefused(
            SecurityRefusalCodeV1.LAN_NOT_EXPLICITLY_ENABLED,
            "LAN startup requires an explicit enabled configuration",
        )
    if not getattr(ssl, "HAS_TLSv1_3", False) or not hasattr(ssl.TLSVersion, "TLSv1_3"):
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS13_UNSUPPORTED,
            "platform TLS library does not support TLS 1.3",
        )
    is_loopback = _is_loopback(configuration.host)
    if configuration.credential_use is CredentialUseV1.AUDIT_LOOPBACK_FIXTURE and not is_loopback:
        raise SecurityRefused(
            SecurityRefusalCodeV1.NON_LOOPBACK_DEFAULT_REFUSED,
            "audit fixture credentials are valid only on loopback",
        )
    paths = (
        _regular_file(configuration.ca_certificate, "CA certificate", private=False),
        _regular_file(configuration.certificate, "local certificate", private=False),
        _regular_file(configuration.private_key, "private key", private=False),
    )
    fingerprints = {
        certificate_sha256(paths[0]),
        certificate_sha256(paths[1]),
    }
    known_fixture = bool(fingerprints & TEST_PKI_CERTIFICATE_SHA256S_V1)
    within_fixture = any(_within(path, TEST_PKI_ROOT_V1) for path in paths)
    within_repository = any(_within(path, _REPOSITORY_ROOT) for path in paths)
    if configuration.credential_use is CredentialUseV1.OPERATOR_PRODUCTION:
        if known_fixture or within_fixture or within_repository:
            raise SecurityRefused(
                SecurityRefusalCodeV1.TEST_CREDENTIAL_REFUSED,
                "production LAN requires operator credentials outside the repository",
            )
        _require_private_permissions(paths[2])
    else:
        fixture_paths_exact = all(_within(path, TEST_PKI_ROOT_V1) for path in paths)
        fixture_certificates_exact = fingerprints <= TEST_PKI_CERTIFICATE_SHA256S_V1
        if (
            not allow_audit_fixture
            or not fixture_paths_exact
            or not fixture_certificates_exact
        ):
            raise SecurityRefused(
                SecurityRefusalCodeV1.TEST_CREDENTIAL_REFUSED,
                "test credentials require explicit opt-in and the exact fixture packet",
            )


def _pin_tls13(context: ssl.SSLContext) -> None:
    try:
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
    except (AttributeError, ValueError) as error:
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS13_UNSUPPORTED,
            "TLS context cannot pin exact TLS 1.3",
        ) from error
    if (
        context.minimum_version != ssl.TLSVersion.TLSv1_3
        or context.maximum_version != ssl.TLSVersion.TLSv1_3
    ):
        raise SecurityRefused(
            SecurityRefusalCodeV1.TLS13_UNSUPPORTED,
            "TLS context did not retain exact TLS 1.3 bounds",
        )


def _regular_file(path: Path, label: str, *, private: bool) -> Path:
    if type(path) is not Path or not path.is_absolute():
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            f"{label} path must be explicit and absolute",
        )
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            f"{label} path cannot be resolved",
        ) from error
    if resolved != path or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            f"{label} must be an already-resolved regular file",
        )
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            f"{label} has a foreign owner",
        )
    if private and metadata.st_mode & 0o077:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            "private key grants group or world permissions",
        )
    return resolved


def _require_private_permissions(path: Path) -> None:
    try:
        metadata = path.stat()
    except OSError as error:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            "private key metadata cannot be read",
        ) from error
    if metadata.st_mode & 0o077:
        raise SecurityRefused(
            SecurityRefusalCodeV1.CREDENTIAL_PATH_UNSAFE,
            "private key grants group or world permissions",
        )


def _certificate_identities(certificate: dict[str, object]) -> set[str]:
    raw = certificate.get("subjectAltName")
    if type(raw) is not tuple:
        return set()
    identities: set[str] = set()
    for item in raw:
        if (
            type(item) is tuple
            and len(item) == 2
            and item[0] in {"DNS", "IP Address", "URI"}
            and type(item[1]) is str
        ):
            identities.add(item[1])
    return identities


def _host(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("LAN host must be nonempty canonical text")
    if any(character in value for character in ("/", "\\", "\x00")):
        raise ValueError("LAN host cannot contain a path separator")
    return value


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be one canonical identifier")
    return value


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _canonical_identities(values: object, label: str) -> None:
    if type(values) is not tuple or not values:
        raise ValueError(f"{label} must be one nonempty immutable tuple")
    for value in values:
        _identifier(value, label)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be sorted and unique")


def _canonical_digests(values: object, label: str) -> None:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be an immutable tuple")
    for value in values:
        _sha256(value, label)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be sorted and unique")


def _exact(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} must be one exact object")
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )
    return value


def _schema(row: dict[str, object], schema_id: str, label: str) -> None:
    if (
        row["schema_id"] != schema_id
        or type(row["schema_version"]) is not int
        or row["schema_version"] != ORCHESTRATION_SECURITY_SCHEMA_VERSION
    ):
        raise ValueError(f"serialized {label} schema differs from V1")


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _optional_text(row: dict[str, object], key: str) -> str | None:
    value = row[key]
    if value is not None and type(value) is not str:
        raise TypeError(f"serialized {key} must be text or null")
    return value


def _round_trip(record: object, row: dict[str, object], label: str) -> None:
    as_dict = getattr(record, "as_dict", None)
    if not callable(as_dict) or as_dict() != row:
        raise ValueError(f"serialized {label} did not round-trip exactly")


__all__ = [
    "ARTIFACT_ACCESS_SCOPE_SCHEMA_ID",
    "AUTHENTICATED_SESSION_SCHEMA_ID",
    "DEFAULT_LAN_BIND_HOST_V1",
    "LAN_PROTOCOL_ID_V1",
    "MAX_LAN_PORT_V1",
    "MAX_SESSION_NONCES_V1",
    "MAX_SESSION_SEQUENCE_V1",
    "ORCHESTRATION_SECURITY_SCHEMA_VERSION",
    "SESSION_HELLO_SCHEMA_ID",
    "TEST_PKI_CERTIFICATE_SHA256S_V1",
    "TEST_PKI_ROOT_V1",
    "ArtifactAccessScopeV1",
    "AuthenticatedSessionV1",
    "CredentialUseV1",
    "LanPeerRoleV1",
    "LanTlsConfigurationV1",
    "SecurityRefusalCodeV1",
    "SecurityRefused",
    "SessionHelloV1",
    "SessionReplayGuardV1",
    "build_client_ssl_context",
    "build_server_ssl_context",
    "certificate_sha256",
    "derive_authenticated_session",
    "protocol_sha256",
    "validate_artifact_access",
    "verify_negotiated_tls_peer",
]
