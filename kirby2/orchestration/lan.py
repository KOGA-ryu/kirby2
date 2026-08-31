"""Explicit authenticated trusted-LAN execution for deterministic work requests.

The coordinator is the TLS server and workers make outbound authenticated
connections.  Every byte after the TLS 1.3 handshake crosses a bounded canonical
envelope; the transport has no plaintext fallback, remote shell, executable job,
dynamic import, or request-controlled filesystem path.  Lease, resource, clock, and
session records are operational state and never enter scientific result identity.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import resource
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes

from .leases import (
    LeaseBookV1,
    LeaseGrantV1,
    LeaseHeartbeatV1,
    LeasePolicyV1,
)
from .protocol import (
    MAX_LAN_ENVELOPE_BYTES_V1,
    LanMessageKindV1,
    LanProtocolEnvelopeV1,
    WorkerCompatibilityV1,
    WorkerResultV1,
    WorkRequestV1,
)
from .resources import (
    ExperimentCancellationV1,
    ResourceAdmissionDecisionV1,
    ResourceAdmissionStatusV1,
    ResourceClaimV1,
    ResourceControllerV1,
    ResourceLimitsV1,
    WorkerResourceAdvertisementV1,
)
from .security import (
    AuthenticatedSessionV1,
    LanPeerRoleV1,
    LanTlsConfigurationV1,
    SecurityRefused,
    SessionHelloV1,
    SessionReplayGuardV1,
    build_client_ssl_context,
    build_server_ssl_context,
    certificate_sha256,
    derive_authenticated_session,
    protocol_sha256,
    verify_negotiated_tls_peer,
)
from .worker import execute_work_request


ORCHESTRATION_LAN_SCHEMA_VERSION = 1
DEFAULT_LAN_PORT_V1 = 43838
MAX_LAN_WORKERS_V1 = 64
MAX_LAN_CONNECTION_TIMEOUT_SECONDS_V1 = 60 * 60
MAX_LAN_ATTEMPT_TEMP_BYTES_V1 = 1 << 50
LAN_SESSION_CLOSE_SCHEMA_ID = "KIRBY2_LAN_SESSION_CLOSE_V1"


class LanRefusalCodeV1(str, Enum):
    FRAME_INVALID = "FRAME_INVALID"
    FRAME_OVERSIZED = "FRAME_OVERSIZED"
    STREAM_LIMIT_EXCEEDED = "STREAM_LIMIT_EXCEEDED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    HANDSHAKE_INVALID = "HANDSHAKE_INVALID"
    MESSAGE_ORDER_INVALID = "MESSAGE_ORDER_INVALID"
    PEER_COMPATIBILITY_MISMATCH = "PEER_COMPATIBILITY_MISMATCH"
    RESOURCE_REFUSED = "RESOURCE_REFUSED"
    LEASE_INVALID = "LEASE_INVALID"
    WORK_RESULT_INVALID = "WORK_RESULT_INVALID"
    ATTEMPT_RESOURCE_ABORTED = "ATTEMPT_RESOURCE_ABORTED"


class LanProtocolRefused(RuntimeError):
    def __init__(self, code: LanRefusalCodeV1, detail: str) -> None:
        if type(code) is not LanRefusalCodeV1:
            raise TypeError("LAN refusal code is invalid")
        if type(detail) is not str or not detail or len(detail.encode("utf-8")) > 4096:
            raise ValueError("LAN refusal detail must be bounded text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}")


@dataclass(frozen=True, slots=True)
class LanPeerConnectionV1:
    channel: FramedTlsChannelV1
    session: AuthenticatedSessionV1
    compatibility: WorkerCompatibilityV1
    resources: WorkerResourceAdvertisementV1

    def __post_init__(self) -> None:
        if not isinstance(self.channel, FramedTlsChannelV1):
            raise TypeError("LAN peer connection requires a framed TLS channel")
        if type(self.session) is not AuthenticatedSessionV1:
            raise TypeError("LAN peer connection requires authenticated session")
        if type(self.compatibility) is not WorkerCompatibilityV1:
            raise TypeError("LAN peer connection requires worker compatibility")
        if type(self.resources) is not WorkerResourceAdvertisementV1:
            raise TypeError("LAN peer connection requires worker resources")
        if self.resources.worker_id != self.session.worker_identity:
            raise ValueError("LAN worker resource identity differs from TLS session")
        if (
            self.resources.worker_compatibility_sha256
            != self.compatibility.compatibility_sha256
        ):
            raise ValueError("LAN worker resource compatibility differs from session")


class FramedTlsChannelV1:
    """Length-prefixed canonical envelopes over an already-authenticated socket."""

    __slots__ = (
        "_bootstrap_received",
        "_bootstrap_sent",
        "_limits",
        "_receive_guard",
        "_received_bytes",
        "_send_lock",
        "_send_sequence",
        "_sent_bytes",
        "_session_id",
        "_socket",
    )

    def __init__(self, connection: ssl.SSLSocket, limits: ResourceLimitsV1) -> None:
        if not isinstance(connection, ssl.SSLSocket):
            raise TypeError("framed LAN channel requires ssl.SSLSocket")
        if type(limits) is not ResourceLimitsV1:
            raise TypeError("framed LAN channel requires ResourceLimitsV1")
        self._socket = connection
        self._limits = limits
        self._session_id: str | None = None
        self._receive_guard: SessionReplayGuardV1 | None = None
        self._send_sequence = 0
        self._sent_bytes = 0
        self._received_bytes = 0
        self._bootstrap_sent = False
        self._bootstrap_received = False
        self._send_lock = threading.Lock()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def limits(self) -> ResourceLimitsV1:
        return self._limits

    def send_hello(self, hello: SessionHelloV1) -> None:
        if type(hello) is not SessionHelloV1:
            raise TypeError("LAN bootstrap requires SessionHelloV1")
        with self._send_lock:
            if self._bootstrap_sent or self._session_id is not None:
                raise LanProtocolRefused(
                    LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                    "session hello can be sent exactly once before session binding",
                )
            envelope = LanProtocolEnvelopeV1(
                message_kind=LanMessageKindV1.SESSION_HELLO,
                session_id=None,
                sequence=0,
                nonce=hello.hello_nonce,
                payload_bytes=hello.canonical_bytes(),
            )
            self._send_frame(envelope.canonical_bytes())
            self._bootstrap_sent = True

    def receive_hello(self) -> SessionHelloV1:
        if self._bootstrap_received or self._session_id is not None:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                "session hello can be received exactly once before session binding",
            )
        envelope = LanProtocolEnvelopeV1.from_canonical_bytes(self._receive_frame())
        if envelope.message_kind is not LanMessageKindV1.SESSION_HELLO:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                "first peer message is not a session hello",
            )
        hello = SessionHelloV1.from_canonical_bytes(envelope.payload_bytes)
        if not hmac.compare_digest(envelope.nonce, hello.hello_nonce):
            raise LanProtocolRefused(
                LanRefusalCodeV1.HANDSHAKE_INVALID,
                "session hello envelope nonce differs from its payload",
            )
        self._bootstrap_received = True
        return hello

    def bind_session(self, session: AuthenticatedSessionV1) -> None:
        if type(session) is not AuthenticatedSessionV1:
            raise TypeError("LAN channel binding requires AuthenticatedSessionV1")
        if not self._bootstrap_sent or not self._bootstrap_received:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                "both authenticated hellos are required before session binding",
            )
        if self._session_id is not None:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                "LAN channel is already bound to a session",
            )
        self._session_id = session.session_id
        self._receive_guard = SessionReplayGuardV1(session.session_id)

    def send(self, kind: LanMessageKindV1, payload_bytes: bytes) -> None:
        if type(kind) is not LanMessageKindV1 or kind is LanMessageKindV1.SESSION_HELLO:
            raise TypeError("bound LAN send requires one non-bootstrap message kind")
        if type(payload_bytes) is not bytes:
            raise TypeError("bound LAN send payload must be immutable bytes")
        with self._send_lock:
            if self._session_id is None:
                raise LanProtocolRefused(
                    LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                    "LAN message cannot precede authenticated session binding",
                )
            self._send_sequence += 1
            envelope = LanProtocolEnvelopeV1(
                message_kind=kind,
                session_id=self._session_id,
                sequence=self._send_sequence,
                nonce=_nonce(),
                payload_bytes=payload_bytes,
            )
            self._send_frame(envelope.canonical_bytes())

    def receive(
        self,
        expected: frozenset[LanMessageKindV1],
    ) -> LanProtocolEnvelopeV1:
        if (
            type(expected) is not frozenset
            or not expected
            or any(
                type(item) is not LanMessageKindV1
                or item is LanMessageKindV1.SESSION_HELLO
                for item in expected
            )
        ):
            raise TypeError("LAN receive requires closed non-bootstrap expected kinds")
        if self._session_id is None or self._receive_guard is None:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                "LAN message cannot precede authenticated session binding",
            )
        envelope = LanProtocolEnvelopeV1.from_canonical_bytes(self._receive_frame())
        if envelope.message_kind not in expected:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                f"unexpected LAN message kind {envelope.message_kind.value}",
            )
        if envelope.session_id is None:
            raise LanProtocolRefused(
                LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                "bound LAN message omitted its session identity",
            )
        self._receive_guard.accept(
            session_id=envelope.session_id,
            sequence=envelope.sequence,
            nonce=envelope.nonce,
        )
        return envelope

    def close(self) -> None:
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except (OSError, ssl.SSLError):
            pass
        self._socket.close()

    def _send_frame(self, raw: bytes) -> None:
        maximum = min(self._limits.maximum_message_bytes, MAX_LAN_ENVELOPE_BYTES_V1)
        if not raw or len(raw) > maximum:
            raise LanProtocolRefused(
                LanRefusalCodeV1.FRAME_OVERSIZED,
                "outbound LAN envelope exceeds the configured message limit",
            )
        frame_bytes = 4 + len(raw)
        if self._sent_bytes + frame_bytes > self._limits.maximum_stream_bytes:
            raise LanProtocolRefused(
                LanRefusalCodeV1.STREAM_LIMIT_EXCEEDED,
                "outbound LAN stream exceeds the configured byte limit",
            )
        try:
            self._socket.sendall(len(raw).to_bytes(4, "big") + raw)
        except (OSError, ssl.SSLError) as error:
            raise LanProtocolRefused(
                LanRefusalCodeV1.CONNECTION_FAILED,
                "authenticated LAN send failed",
            ) from error
        self._sent_bytes += frame_bytes

    def _receive_frame(self) -> bytes:
        header = self._receive_exact(4)
        length = int.from_bytes(header, "big")
        maximum = min(self._limits.maximum_message_bytes, MAX_LAN_ENVELOPE_BYTES_V1)
        if length <= 0 or length > maximum:
            raise LanProtocolRefused(
                LanRefusalCodeV1.FRAME_OVERSIZED,
                "inbound LAN frame length exceeds the configured message limit",
            )
        frame_bytes = 4 + length
        if self._received_bytes + frame_bytes > self._limits.maximum_stream_bytes:
            raise LanProtocolRefused(
                LanRefusalCodeV1.STREAM_LIMIT_EXCEEDED,
                "inbound LAN stream exceeds the configured byte limit",
            )
        raw = self._receive_exact(length)
        self._received_bytes += frame_bytes
        return raw

    def _receive_exact(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                chunk = self._socket.recv(min(remaining, 64 * 1024))
                if not chunk:
                    raise LanProtocolRefused(
                        LanRefusalCodeV1.CONNECTION_FAILED,
                        "authenticated LAN peer closed a partial or expected frame",
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
        except LanProtocolRefused:
            raise
        except (OSError, ssl.SSLError) as error:
            raise LanProtocolRefused(
                LanRefusalCodeV1.CONNECTION_FAILED,
                "authenticated LAN receive failed or timed out",
            ) from error
        return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class LanCoordinatorBackendV1:
    """Execution backend accepting an exact number of outbound LAN workers."""

    configuration: LanTlsConfigurationV1
    compatibility: WorkerCompatibilityV1
    plan_id: str
    worker_count: int
    transport_limits: ResourceLimitsV1
    lease_policy: LeasePolicyV1
    claim_memory_bytes: int
    claim_disk_bytes: int
    claim_elapsed_seconds: int
    connection_timeout_seconds: int = 60
    allow_audit_fixture: bool = False

    def __post_init__(self) -> None:
        if type(self.configuration) is not LanTlsConfigurationV1:
            raise TypeError("LAN backend configuration must be typed")
        if self.configuration.role is not LanPeerRoleV1.COORDINATOR:
            raise ValueError("LAN coordinator backend requires coordinator TLS role")
        if type(self.compatibility) is not WorkerCompatibilityV1:
            raise TypeError("LAN backend compatibility must be typed")
        _sha256(self.plan_id, "LAN backend plan ID")
        if type(self.worker_count) is not int or not 1 <= self.worker_count <= MAX_LAN_WORKERS_V1:
            raise ValueError("LAN worker count is outside the V1 range")
        if len(self.configuration.expected_peer_identities) < self.worker_count:
            raise ValueError("LAN backend requires one expected identity per worker")
        if type(self.transport_limits) is not ResourceLimitsV1:
            raise TypeError("LAN backend transport limits must be typed")
        if type(self.lease_policy) is not LeasePolicyV1:
            raise TypeError("LAN backend lease policy must be typed")
        for value, label, maximum in (
            (
                self.claim_memory_bytes,
                "LAN claim memory bytes",
                self.transport_limits.maximum_memory_bytes_per_run,
            ),
            (
                self.claim_disk_bytes,
                "LAN claim disk bytes",
                self.transport_limits.maximum_disk_bytes_per_run,
            ),
            (
                self.claim_elapsed_seconds,
                "LAN claim elapsed seconds",
                self.transport_limits.maximum_elapsed_seconds_per_run,
            ),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{label} exceeds coordinator resource policy")
        if (
            type(self.connection_timeout_seconds) is not int
            or not 1
            <= self.connection_timeout_seconds
            <= MAX_LAN_CONNECTION_TIMEOUT_SECONDS_V1
        ):
            raise ValueError("LAN connection timeout is outside the V1 range")
        if type(self.allow_audit_fixture) is not bool:
            raise TypeError("LAN audit-fixture flag must be boolean")

    @property
    def backend_id(self) -> str:
        return "authenticated-lan-v1"

    def execute_many(
        self,
        requests: tuple[WorkRequestV1, ...],
    ) -> tuple[WorkerResultV1, ...]:
        supplied = _canonical_requests(requests)
        context = build_server_ssl_context(
            self.configuration,
            allow_audit_fixture=self.allow_audit_fixture,
        )
        lease_book = LeaseBookV1(self.lease_policy)
        peers: list[LanPeerConnectionV1] = []
        try:
            listener = socket.create_server(
                (self.configuration.host, self.configuration.port),
                backlog=self.worker_count,
                reuse_port=False,
            )
        except OSError as error:
            raise LanProtocolRefused(
                LanRefusalCodeV1.CONNECTION_FAILED,
                "coordinator could not bind its explicit LAN endpoint",
            ) from error
        try:
            listener.settimeout(self.connection_timeout_seconds)
            for _index in range(self.worker_count):
                peers.append(
                    _accept_worker(
                        listener=listener,
                        context=context,
                        configuration=self.configuration,
                        compatibility=self.compatibility,
                        transport_limits=self.transport_limits,
                        lease_policy=self.lease_policy,
                        timeout_seconds=self.connection_timeout_seconds,
                    )
                )
        except Exception:
            for peer in peers:
                peer.channel.close()
            raise
        finally:
            listener.close()
        worker_ids = tuple(peer.session.worker_identity for peer in peers)
        if len(worker_ids) != len(set(worker_ids)):
            for peer in peers:
                peer.channel.close()
            raise LanProtocolRefused(
                LanRefusalCodeV1.HANDSHAKE_INVALID,
                "coordinator accepted duplicate worker identities",
            )
        batches = tuple(
            tuple(supplied[index] for index in range(offset, len(supplied), len(peers)))
            for offset in range(len(peers))
        )
        gathered: list[WorkerResultV1] = []
        try:
            with ThreadPoolExecutor(max_workers=len(peers)) as executor:
                futures = tuple(
                    executor.submit(
                        self._execute_peer_batch,
                        peer,
                        batch,
                        lease_book,
                    )
                    for peer, batch in zip(peers, batches, strict=True)
                )
                for future in futures:
                    gathered.extend(future.result())
        finally:
            for peer in peers:
                peer.channel.close()
        return _canonical_results(tuple(gathered), supplied)

    def _execute_peer_batch(
        self,
        peer: LanPeerConnectionV1,
        requests: tuple[WorkRequestV1, ...],
        lease_book: LeaseBookV1,
    ) -> tuple[WorkerResultV1, ...]:
        results: list[WorkerResultV1] = []
        for request in requests:
            claim = ResourceClaimV1(
                experiment_id=request.logical_work_unit.experiment_identity.sha256,
                work_request_id=request.work_request_id,
                resource_class=request.logical_work_unit.resource_class,
                memory_bytes=self.claim_memory_bytes,
                disk_bytes=self.claim_disk_bytes,
                elapsed_seconds=self.claim_elapsed_seconds,
            )
            peer.channel.send(LanMessageKindV1.RESOURCE_CLAIM, claim.canonical_bytes())
            decision = _receive_record(
                peer.channel,
                LanMessageKindV1.RESOURCE_DECISION,
                ResourceAdmissionDecisionV1.from_dict,
                "resource admission decision",
            )
            if (
                decision.claim_id != claim.claim_id
                or decision.status is not ResourceAdmissionStatusV1.ADMITTED
            ):
                raise LanProtocolRefused(
                    LanRefusalCodeV1.RESOURCE_REFUSED,
                    "worker did not admit the exact coordinator resource claim",
                )
            grant = lease_book.grant(
                plan_id=self.plan_id,
                work_request_id=request.work_request_id,
                logical_work_unit_id=request.logical_work_unit.logical_work_unit_id,
                attempt_number=1,
                worker_id=peer.session.worker_identity,
                session_id=peer.session.session_id,
                issued_at_utc=_utc_now(),
            )
            peer.channel.send(LanMessageKindV1.LEASE_GRANT, grant.canonical_bytes())
            peer.channel.send(LanMessageKindV1.WORK_REQUEST, request.canonical_bytes())
            result = self._receive_attempt_result(
                peer=peer,
                request=request,
                claim=claim,
                grant=grant,
                lease_book=lease_book,
            )
            results.append(result)
        peer.channel.send(
            LanMessageKindV1.SESSION_CLOSE,
            _session_close_bytes("COORDINATOR_COMPLETE"),
        )
        return tuple(results)

    def _receive_attempt_result(
        self,
        *,
        peer: LanPeerConnectionV1,
        request: WorkRequestV1,
        claim: ResourceClaimV1,
        grant: LeaseGrantV1,
        lease_book: LeaseBookV1,
    ) -> WorkerResultV1:
        expected = frozenset(
            {
                LanMessageKindV1.LEASE_HEARTBEAT,
                LanMessageKindV1.RESOURCE_DECISION,
                LanMessageKindV1.WORK_RESULT,
            }
        )
        while True:
            envelope = peer.channel.receive(expected)
            if envelope.message_kind is LanMessageKindV1.LEASE_HEARTBEAT:
                heartbeat = LeaseHeartbeatV1.from_dict(
                    load_canonical_json_bytes(
                        envelope.payload_bytes,
                        "LAN lease heartbeat",
                    )
                )
                lease_book.heartbeat(heartbeat)
                continue
            if envelope.message_kind is LanMessageKindV1.RESOURCE_DECISION:
                decision = ResourceAdmissionDecisionV1.from_dict(
                    load_canonical_json_bytes(
                        envelope.payload_bytes,
                        "LAN resource abort decision",
                    )
                )
                if (
                    decision.claim_id != claim.claim_id
                    or decision.status
                    not in {
                        ResourceAdmissionStatusV1.ABORTED,
                        ResourceAdmissionStatusV1.CANCELLED,
                    }
                ):
                    raise LanProtocolRefused(
                        LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
                        "worker sent an invalid in-attempt resource decision",
                    )
                raise LanProtocolRefused(
                    LanRefusalCodeV1.ATTEMPT_RESOURCE_ABORTED,
                    "worker resource policy aborted the operational attempt",
                )
            result = WorkerResultV1.from_dict(
                load_canonical_json_bytes(
                    envelope.payload_bytes,
                    "LAN worker result",
                )
            )
            if (
                result.request != request
                or result.worker_compatibility != peer.compatibility
                or grant.work_request_id != request.work_request_id
            ):
                raise LanProtocolRefused(
                    LanRefusalCodeV1.WORK_RESULT_INVALID,
                    "worker result differs from its request, lease, or compatibility",
                )
            lease_book.complete(grant.lease_id)
            released = _receive_record(
                peer.channel,
                LanMessageKindV1.RESOURCE_DECISION,
                ResourceAdmissionDecisionV1.from_dict,
                "resource release decision",
            )
            if (
                released.claim_id != claim.claim_id
                or released.status is not ResourceAdmissionStatusV1.RELEASED
            ):
                raise LanProtocolRefused(
                    LanRefusalCodeV1.RESOURCE_REFUSED,
                    "worker did not release the completed exact resource claim",
                )
            return result


@dataclass(frozen=True, slots=True)
class LanWorkerServiceV1:
    """Outbound worker service executing only the fixed WorkRequestV1 adapter."""

    configuration: LanTlsConfigurationV1
    compatibility: WorkerCompatibilityV1
    resources: WorkerResourceAdvertisementV1
    connection_timeout_seconds: int = 60
    allow_audit_fixture: bool = False

    def __post_init__(self) -> None:
        if type(self.configuration) is not LanTlsConfigurationV1:
            raise TypeError("LAN worker configuration must be typed")
        if self.configuration.role is not LanPeerRoleV1.WORKER:
            raise ValueError("LAN worker service requires worker TLS role")
        if type(self.compatibility) is not WorkerCompatibilityV1:
            raise TypeError("LAN worker compatibility must be typed")
        if type(self.resources) is not WorkerResourceAdvertisementV1:
            raise TypeError("LAN worker resources must be typed")
        if self.resources.worker_id != self.configuration.local_identity:
            raise ValueError("LAN worker advertisement differs from TLS identity")
        if (
            self.resources.worker_compatibility_sha256
            != self.compatibility.compatibility_sha256
        ):
            raise ValueError("LAN worker advertisement compatibility differs")
        if (
            type(self.connection_timeout_seconds) is not int
            or not 1
            <= self.connection_timeout_seconds
            <= MAX_LAN_CONNECTION_TIMEOUT_SECONDS_V1
        ):
            raise ValueError("LAN worker connection timeout is outside the V1 range")
        if type(self.allow_audit_fixture) is not bool:
            raise TypeError("LAN worker audit-fixture flag must be boolean")

    def run(self) -> tuple[WorkerResultV1, ...]:
        peer, lease_policy = _connect_worker(
            configuration=self.configuration,
            compatibility=self.compatibility,
            resources=self.resources,
            timeout_seconds=self.connection_timeout_seconds,
            allow_audit_fixture=self.allow_audit_fixture,
        )
        controller = ResourceControllerV1(
            limits=self.resources.limits,
            resource_classes=self.resources.resource_classes,
        )
        results: list[WorkerResultV1] = []
        try:
            while True:
                envelope = peer.channel.receive(
                    frozenset(
                        {
                            LanMessageKindV1.EXPERIMENT_CANCELLATION,
                            LanMessageKindV1.RESOURCE_CLAIM,
                            LanMessageKindV1.SESSION_CLOSE,
                        }
                    )
                )
                if envelope.message_kind is LanMessageKindV1.SESSION_CLOSE:
                    _validate_session_close(envelope.payload_bytes)
                    break
                if envelope.message_kind is LanMessageKindV1.EXPERIMENT_CANCELLATION:
                    cancellation = ExperimentCancellationV1.from_dict(
                        load_canonical_json_bytes(
                            envelope.payload_bytes,
                            "LAN experiment cancellation",
                        )
                    )
                    for decision in controller.cancel_experiment(cancellation):
                        peer.channel.send(
                            LanMessageKindV1.RESOURCE_DECISION,
                            decision.canonical_bytes(),
                        )
                    continue
                claim = ResourceClaimV1.from_dict(
                    load_canonical_json_bytes(
                        envelope.payload_bytes,
                        "LAN resource claim",
                    )
                )
                decision = controller.admit(claim)
                peer.channel.send(
                    LanMessageKindV1.RESOURCE_DECISION,
                    decision.canonical_bytes(),
                )
                if decision.status is not ResourceAdmissionStatusV1.ADMITTED:
                    continue
                result = self._execute_admitted_attempt(
                    peer=peer,
                    claim=claim,
                    controller=controller,
                    lease_policy=lease_policy,
                )
                if result is not None:
                    results.append(result)
        finally:
            peer.channel.close()
        return tuple(results)

    def _execute_admitted_attempt(
        self,
        *,
        peer: LanPeerConnectionV1,
        claim: ResourceClaimV1,
        controller: ResourceControllerV1,
        lease_policy: LeasePolicyV1,
    ) -> WorkerResultV1 | None:
        grant = _receive_record(
            peer.channel,
            LanMessageKindV1.LEASE_GRANT,
            LeaseGrantV1.from_dict,
            "lease grant",
        )
        request = _receive_record(
            peer.channel,
            LanMessageKindV1.WORK_REQUEST,
            WorkRequestV1.from_dict,
            "work request",
        )
        if (
            grant.plan_id == ""
            or grant.work_request_id != request.work_request_id
            or grant.logical_work_unit_id
            != request.logical_work_unit.logical_work_unit_id
            or grant.worker_id != peer.session.worker_identity
            or grant.session_id != peer.session.session_id
            or claim.work_request_id != request.work_request_id
            or claim.experiment_id
            != request.logical_work_unit.experiment_identity.sha256
            or claim.resource_class != request.logical_work_unit.resource_class
        ):
            raise LanProtocolRefused(
                LanRefusalCodeV1.LEASE_INVALID,
                "work request differs from its lease, session, or resource claim",
            )
        started_ns = time.monotonic_ns()
        memory_before = _resident_memory_bytes()
        with tempfile.TemporaryDirectory(
            prefix=f"kirby2-lan-attempt-{grant.attempt_id[:12]}-"
        ) as raw_attempt_directory:
            attempt_directory = Path(raw_attempt_directory)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(execute_work_request, request)
                heartbeat_sequence = 0
                while True:
                    try:
                        result = future.result(
                            timeout=lease_policy.heartbeat_interval_seconds
                        )
                        break
                    except FutureTimeout:
                        heartbeat_sequence += 1
                        heartbeat = LeaseHeartbeatV1(
                            lease_id=grant.lease_id,
                            attempt_id=grant.attempt_id,
                            worker_id=peer.session.worker_identity,
                            session_id=peer.session.session_id,
                            heartbeat_sequence=heartbeat_sequence,
                            sent_at_utc=_utc_now(),
                            heartbeat_nonce=_nonce(),
                        )
                        peer.channel.send(
                            LanMessageKindV1.LEASE_HEARTBEAT,
                            heartbeat.canonical_bytes(),
                        )
            elapsed_seconds = max(
                1,
                math.ceil((time.monotonic_ns() - started_ns) / 1_000_000_000),
            )
            memory_bytes = max(memory_before, _resident_memory_bytes())
            disk_bytes = _directory_bytes(attempt_directory)
            decisions = controller.observe_usage(
                claim.claim_id,
                memory_bytes=memory_bytes,
                disk_bytes=disk_bytes,
                elapsed_seconds=elapsed_seconds,
            )
            if decisions:
                for decision in decisions:
                    peer.channel.send(
                        LanMessageKindV1.RESOURCE_DECISION,
                        decision.canonical_bytes(),
                    )
                return None
            peer.channel.send(
                LanMessageKindV1.WORK_RESULT,
                result.canonical_bytes(),
            )
            for decision in controller.release(claim.claim_id):
                peer.channel.send(
                    LanMessageKindV1.RESOURCE_DECISION,
                    decision.canonical_bytes(),
                )
            return result


def _accept_worker(
    *,
    listener: socket.socket,
    context: ssl.SSLContext,
    configuration: LanTlsConfigurationV1,
    compatibility: WorkerCompatibilityV1,
    transport_limits: ResourceLimitsV1,
    lease_policy: LeasePolicyV1,
    timeout_seconds: int,
) -> LanPeerConnectionV1:
    raw: socket.socket | None = None
    connection: ssl.SSLSocket | None = None
    try:
        raw, _address = listener.accept()
        raw.settimeout(timeout_seconds)
        connection = context.wrap_socket(raw, server_side=True)
        raw = None
        peer_certificate_sha256 = verify_negotiated_tls_peer(
            connection,
            configuration,
        )
        channel = FramedTlsChannelV1(connection, transport_limits)
        worker_hello = channel.receive_hello()
        if (
            worker_hello.role is not LanPeerRoleV1.WORKER
            or worker_hello.peer_identity not in configuration.expected_peer_identities
            or not hmac.compare_digest(
                worker_hello.certificate_sha256,
                peer_certificate_sha256,
            )
            or not hmac.compare_digest(worker_hello.protocol_sha256, protocol_sha256())
        ):
            raise LanProtocolRefused(
                LanRefusalCodeV1.HANDSHAKE_INVALID,
                "worker hello differs from its authenticated TLS peer",
            )
        coordinator_hello = SessionHelloV1(
            role=LanPeerRoleV1.COORDINATOR,
            peer_identity=configuration.local_identity,
            certificate_sha256=certificate_sha256(configuration.certificate),
            hello_nonce=_nonce(),
            protocol_sha256=protocol_sha256(),
            compatibility_sha256=None,
            resource_advertisement_sha256=None,
        )
        channel.send_hello(coordinator_hello)
        session = derive_authenticated_session(coordinator_hello, worker_hello)
        channel.bind_session(session)
        measured = _receive_record(
            channel,
            LanMessageKindV1.WORKER_COMPATIBILITY,
            WorkerCompatibilityV1.from_dict,
            "worker compatibility",
        )
        advertisement = _receive_record(
            channel,
            LanMessageKindV1.RESOURCE_ADVERTISEMENT,
            WorkerResourceAdvertisementV1.from_dict,
            "worker resource advertisement",
        )
        if (
            measured != compatibility
            or measured.compatibility_sha256 != worker_hello.compatibility_sha256
            or advertisement.advertisement_sha256
            != worker_hello.resource_advertisement_sha256
            or advertisement.worker_id != worker_hello.peer_identity
            or advertisement.worker_compatibility_sha256
            != measured.compatibility_sha256
            or advertisement.limits.maximum_message_bytes
            < transport_limits.maximum_message_bytes
            or advertisement.limits.maximum_stream_bytes
            < transport_limits.maximum_stream_bytes
        ):
            raise LanProtocolRefused(
                LanRefusalCodeV1.PEER_COMPATIBILITY_MISMATCH,
                "worker compatibility or resource advertisement differs",
            )
        channel.send(LanMessageKindV1.LEASE_POLICY, lease_policy.canonical_bytes())
        return LanPeerConnectionV1(
            channel=channel,
            session=session,
            compatibility=measured,
            resources=advertisement,
        )
    except (LanProtocolRefused, SecurityRefused, OSError, ssl.SSLError):
        if connection is not None:
            connection.close()
        if raw is not None:
            raw.close()
        raise


def _connect_worker(
    *,
    configuration: LanTlsConfigurationV1,
    compatibility: WorkerCompatibilityV1,
    resources: WorkerResourceAdvertisementV1,
    timeout_seconds: int,
    allow_audit_fixture: bool,
) -> tuple[LanPeerConnectionV1, LeasePolicyV1]:
    context = build_client_ssl_context(
        configuration,
        allow_audit_fixture=allow_audit_fixture,
    )
    raw: socket.socket | None = None
    connection: ssl.SSLSocket | None = None
    try:
        raw = socket.create_connection(
            (configuration.host, configuration.port),
            timeout=timeout_seconds,
        )
        connection = context.wrap_socket(
            raw,
            server_hostname=configuration.server_hostname,
        )
        raw = None
        connection.settimeout(timeout_seconds)
        peer_certificate_sha256 = verify_negotiated_tls_peer(
            connection,
            configuration,
        )
        channel = FramedTlsChannelV1(connection, resources.limits)
        worker_hello = SessionHelloV1(
            role=LanPeerRoleV1.WORKER,
            peer_identity=configuration.local_identity,
            certificate_sha256=certificate_sha256(configuration.certificate),
            hello_nonce=_nonce(),
            protocol_sha256=protocol_sha256(),
            compatibility_sha256=compatibility.compatibility_sha256,
            resource_advertisement_sha256=resources.advertisement_sha256,
        )
        channel.send_hello(worker_hello)
        coordinator_hello = channel.receive_hello()
        if (
            coordinator_hello.role is not LanPeerRoleV1.COORDINATOR
            or coordinator_hello.peer_identity
            not in configuration.expected_peer_identities
            or not hmac.compare_digest(
                coordinator_hello.certificate_sha256,
                peer_certificate_sha256,
            )
            or not hmac.compare_digest(
                coordinator_hello.protocol_sha256,
                protocol_sha256(),
            )
        ):
            raise LanProtocolRefused(
                LanRefusalCodeV1.HANDSHAKE_INVALID,
                "coordinator hello differs from its authenticated TLS peer",
            )
        session = derive_authenticated_session(coordinator_hello, worker_hello)
        channel.bind_session(session)
        channel.send(
            LanMessageKindV1.WORKER_COMPATIBILITY,
            compatibility.canonical_bytes(),
        )
        channel.send(
            LanMessageKindV1.RESOURCE_ADVERTISEMENT,
            resources.canonical_bytes(),
        )
        lease_policy = _receive_record(
            channel,
            LanMessageKindV1.LEASE_POLICY,
            LeasePolicyV1.from_dict,
            "lease policy",
        )
        return (
            LanPeerConnectionV1(
                channel=channel,
                session=session,
                compatibility=compatibility,
                resources=resources,
            ),
            lease_policy,
        )
    except (LanProtocolRefused, SecurityRefused, OSError, ssl.SSLError):
        if connection is not None:
            connection.close()
        if raw is not None:
            raw.close()
        raise


def _receive_record(
    channel: FramedTlsChannelV1,
    kind: LanMessageKindV1,
    parser,
    label: str,
):
    envelope = channel.receive(frozenset({kind}))
    payload = load_canonical_json_bytes(envelope.payload_bytes, f"LAN {label}")
    restored = parser(payload)
    canonical = getattr(restored, "canonical_bytes", None)
    if not callable(canonical) or canonical() != envelope.payload_bytes:
        raise LanProtocolRefused(
            LanRefusalCodeV1.FRAME_INVALID,
            f"LAN {label} did not round-trip canonically",
        )
    return restored


def _canonical_requests(
    requests: tuple[WorkRequestV1, ...],
) -> tuple[WorkRequestV1, ...]:
    if type(requests) is not tuple or not requests:
        raise ValueError("LAN backend requires a nonempty immutable request tuple")
    if any(type(item) is not WorkRequestV1 for item in requests):
        raise TypeError("LAN backend requests must contain WorkRequestV1")
    ordered = tuple(
        sorted(
            requests,
            key=lambda item: item.logical_work_unit.logical_work_unit_id,
        )
    )
    logical_ids = tuple(
        item.logical_work_unit.logical_work_unit_id for item in ordered
    )
    request_ids = tuple(item.work_request_id for item in ordered)
    if len(logical_ids) != len(set(logical_ids)):
        raise ValueError("LAN backend cannot receive duplicate logical work")
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("LAN backend cannot receive duplicate work requests")
    return ordered


def _canonical_results(
    results: tuple[WorkerResultV1, ...],
    requests: tuple[WorkRequestV1, ...],
) -> tuple[WorkerResultV1, ...]:
    if type(results) is not tuple or any(
        type(item) is not WorkerResultV1 for item in results
    ):
        raise TypeError("LAN results must contain WorkerResultV1")
    by_request: dict[str, WorkerResultV1] = {}
    for result in results:
        request_id = result.request.work_request_id
        if request_id in by_request:
            raise LanProtocolRefused(
                LanRefusalCodeV1.WORK_RESULT_INVALID,
                "LAN workers returned a duplicate request result",
            )
        by_request[request_id] = result
    expected = tuple(item.work_request_id for item in requests)
    if frozenset(by_request) != frozenset(expected):
        raise LanProtocolRefused(
            LanRefusalCodeV1.WORK_RESULT_INVALID,
            "LAN workers omitted or invented request results",
        )
    return tuple(by_request[request_id] for request_id in expected)


def _session_close_bytes(reason: str) -> bytes:
    if reason != "COORDINATOR_COMPLETE":
        raise ValueError("LAN V1 supports only the canonical complete close reason")
    return canonical_json_bytes(
        {
            "reason": reason,
            "schema_id": LAN_SESSION_CLOSE_SCHEMA_ID,
            "schema_version": ORCHESTRATION_LAN_SCHEMA_VERSION,
        }
    )


def _validate_session_close(raw: bytes) -> None:
    row = load_canonical_json_bytes(raw, "LAN session close")
    expected = {
        "reason": "COORDINATOR_COMPLETE",
        "schema_id": LAN_SESSION_CLOSE_SCHEMA_ID,
        "schema_version": ORCHESTRATION_LAN_SCHEMA_VERSION,
    }
    if row != expected or canonical_json_bytes(row) != raw:
        raise LanProtocolRefused(
            LanRefusalCodeV1.MESSAGE_ORDER_INVALID,
            "LAN session close record differs from the V1 contract",
        )


def _directory_bytes(root: Path) -> int:
    if type(root) is not Path or not root.is_absolute():
        raise TypeError("attempt cleanup root must be one absolute Path")
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(directories)
        if any((Path(current) / name).is_symlink() for name in directories):
            raise LanProtocolRefused(
                LanRefusalCodeV1.ATTEMPT_RESOURCE_ABORTED,
                "attempt temporary area contains a symbolic-link directory",
            )
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink():
                raise LanProtocolRefused(
                    LanRefusalCodeV1.ATTEMPT_RESOURCE_ABORTED,
                    "attempt temporary area contains a symbolic link",
                )
            total += path.stat().st_size
            if total > MAX_LAN_ATTEMPT_TEMP_BYTES_V1:
                raise LanProtocolRefused(
                    LanRefusalCodeV1.ATTEMPT_RESOURCE_ABORTED,
                    "attempt temporary area exceeds the V1 hard limit",
                )
    return total


def _resident_memory_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if type(value) not in {int, float} or value < 0 or not math.isfinite(value):
        raise LanProtocolRefused(
            LanRefusalCodeV1.ATTEMPT_RESOURCE_ABORTED,
            "worker resident-memory measurement is unavailable",
        )
    measured = math.ceil(value)
    if sys.platform != "darwin":
        measured *= 1024
    return measured


def _utc_now() -> str:
    value = datetime.now(timezone.utc)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _nonce() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


__all__ = [
    "DEFAULT_LAN_PORT_V1",
    "LAN_SESSION_CLOSE_SCHEMA_ID",
    "MAX_LAN_ATTEMPT_TEMP_BYTES_V1",
    "MAX_LAN_CONNECTION_TIMEOUT_SECONDS_V1",
    "MAX_LAN_WORKERS_V1",
    "ORCHESTRATION_LAN_SCHEMA_VERSION",
    "FramedTlsChannelV1",
    "LanCoordinatorBackendV1",
    "LanPeerConnectionV1",
    "LanProtocolRefused",
    "LanRefusalCodeV1",
    "LanWorkerServiceV1",
]
