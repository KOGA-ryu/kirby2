"""Governed content-addressed storage for orchestration transfer and results.

WO38-C deliberately keeps filesystem locations out of coordinator/worker messages.
Transfer contracts contain content identities, byte counts, and canonical manifests;
the local installation receipt may retain only WO39's validated registry-relative
content-addressed object locator.  Every store-owned on-disk name is derived privately
from a validated SHA-256 digest beneath one of the existing :class:`DataPaths` areas.

There are three distinct trust transitions:

* source ``.k2pack`` transports are fully preflighted before immutable registration
  beneath the governed cache area and are served only by exact descriptor;
* a receiver repeats hostile-archive preflight, redistribution, compatibility,
  private staging, and atomic pack installation beneath its own governed root;
* worker output bytes live in a private per-attempt stage until a coordinator-built
  result manifest is verified.  Artifact objects are moved into the immutable runs
  CAS and the canonical result manifest is published last as the registration point.

An interrupted result registration may leave an unreferenced immutable object, but
never a registered manifest pointing to absent or unverified bytes.  This card
intentionally exposes no deletion API for registered transports, result objects, or
result manifests.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, ClassVar, NoReturn

from kirby2.packs.archive import (
    PackArchivePreflightV1,
    preflight_pack_archive_bytes,
)
from kirby2.packs.dependencies import PackRuntimeEnvironmentV1
from kirby2.packs.formats import canonical_json_bytes
from kirby2.packs.install import PackInstallReceiptV1, install_pack
from kirby2.packs.staging import (
    ActivationEligiblePackStageV1,
    discard_pack_stage,
    stage_preflighted_pack,
)
from kirby2.packs.validation import (
    DEFAULT_PACK_VALIDATION_LIMITS_V1,
    PackValidationLimitsV1,
)
from kirby2.research.paths import DataAreaId, DataPaths

from .artifacts import (
    ContentRequestV1,
    PackTransferBundleV1,
    PackTransferDescriptorV1,
    ResultArtifactDescriptorV1,
    ResultBundleManifestV1,
)
from .compatibility import (
    ConditionalTransferAuthorizationV1,
    PackCapabilityIdentityBindingV1,
    PackSchemaIdentityBindingV1,
    pack_redistribution_decision_identity,
    validate_pack_receiver_compatibility,
    validate_pack_transfer_completeness,
    validate_required_content_references,
    validate_worker_compatibility,
)
from .models import LogicalWorkUnit
from .protocol import InlineArtifactV1, WorkerCompatibilityV1

if TYPE_CHECKING:
    from .coordinator import VerifiedWorkResultV1


ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION = 1
STORED_PACK_TRANSPORT_SCHEMA_ID = "KIRBY2_STORED_PACK_TRANSPORT_V1"
RECEIVED_PACK_INSTALLATION_SCHEMA_ID = "KIRBY2_RECEIVED_PACK_INSTALLATION_V1"
RESULT_ATTEMPT_STAGE_SCHEMA_ID = "KIRBY2_RESULT_ATTEMPT_STAGE_V1"
REGISTERED_RESULT_BUNDLE_SCHEMA_ID = "KIRBY2_REGISTERED_RESULT_BUNDLE_V1"

_STORE_DIRECTORY = "orchestration-content-v1"
_TRANSPORT_DIRECTORY = "transports"
_RESULT_DIRECTORY = "results"
_ATTEMPT_DIRECTORY = "attempts"
_OBJECT_DIRECTORY = "objects"
_MANIFEST_DIRECTORY = "manifests"
_DIGEST_ALGORITHM_DIRECTORY = "sha256"
_LOCK_FILENAME = ".content-store.lock"
_TEMP_PREFIX = ".content-store-tmp-"
_READ_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class ContentStoreOperationV1(str, Enum):
    """Closed operation vocabulary for stable local refusal records."""

    REGISTER_TRANSPORT = "REGISTER_TRANSPORT"
    SERVE_TRANSPORT = "SERVE_TRANSPORT"
    RECEIVE_PACK = "RECEIVE_PACK"
    BEGIN_RESULT_ATTEMPT = "BEGIN_RESULT_ATTEMPT"
    STAGE_RESULT_ARTIFACT = "STAGE_RESULT_ARTIFACT"
    REGISTER_RESULT_BUNDLE = "REGISTER_RESULT_BUNDLE"
    READ_RESULT_ARTIFACT = "READ_RESULT_ARTIFACT"
    READ_RESULT_MANIFEST = "READ_RESULT_MANIFEST"
    DISCARD_RESULT_ATTEMPT = "DISCARD_RESULT_ATTEMPT"


class ContentStoreRefusalCodeV1(str, Enum):
    """Machine-stable reasons emitted at the content-store boundary."""

    DATA_PATHS_UNSAFE = "DATA_PATHS_UNSAFE"
    STORE_LAYOUT_UNSAFE = "STORE_LAYOUT_UNSAFE"
    TRANSPORT_DESCRIPTOR_MISMATCH = "TRANSPORT_DESCRIPTOR_MISMATCH"
    CONTENT_REQUEST_MISMATCH = "CONTENT_REQUEST_MISMATCH"
    REDISTRIBUTION_DECISION_MISMATCH = "REDISTRIBUTION_DECISION_MISMATCH"
    TRANSPORT_NOT_REGISTERED = "TRANSPORT_NOT_REGISTERED"
    TRANSPORT_OBJECT_INVALID = "TRANSPORT_OBJECT_INVALID"
    PACK_RECEIVER_REFUSED = "PACK_RECEIVER_REFUSED"
    PACK_STAGE_CLEANUP_FAILED = "PACK_STAGE_CLEANUP_FAILED"
    ATTEMPT_ALREADY_EXISTS = "ATTEMPT_ALREADY_EXISTS"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    ATTEMPT_CAPABILITY_MISMATCH = "ATTEMPT_CAPABILITY_MISMATCH"
    ARTIFACT_DESCRIPTOR_MISMATCH = "ARTIFACT_DESCRIPTOR_MISMATCH"
    ARTIFACT_ALREADY_STAGED = "ARTIFACT_ALREADY_STAGED"
    ARTIFACT_INVENTORY_MISMATCH = "ARTIFACT_INVENTORY_MISMATCH"
    RESULT_MANIFEST_MISMATCH = "RESULT_MANIFEST_MISMATCH"
    COORDINATOR_VERIFICATION_MISMATCH = "COORDINATOR_VERIFICATION_MISMATCH"
    REGISTERED_OBJECT_INVALID = "REGISTERED_OBJECT_INVALID"
    REGISTERED_MANIFEST_INVALID = "REGISTERED_MANIFEST_INVALID"
    REGISTERED_CONTENT_IMMUTABLE = "REGISTERED_CONTENT_IMMUTABLE"
    IO_FAILED = "IO_FAILED"


@dataclass(frozen=True, slots=True)
class ContentStoreRefusalV1:
    """One bounded path-free refusal suitable for structured diagnostics."""

    code: ContentStoreRefusalCodeV1
    operation: ContentStoreOperationV1
    detail: str
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not ContentStoreRefusalCodeV1:
            raise TypeError("content-store refusal code is invalid")
        if type(self.operation) is not ContentStoreOperationV1:
            raise TypeError("content-store refusal operation is invalid")
        if (
            type(self.detail) is not str
            or not self.detail
            or len(self.detail.encode("utf-8")) > 4096
        ):
            raise ValueError("content-store refusal detail must be bounded text")
        if self.content_sha256 is not None:
            _require_sha256(self.content_sha256, "content-store refusal digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "content_sha256": self.content_sha256,
            "detail": self.detail,
            "operation": self.operation.value,
            "schema_id": "KIRBY2_CONTENT_STORE_REFUSAL_V1",
            "schema_version": ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION,
        }


class ContentStoreRefused(RuntimeError):
    """A governed content operation failed without exposing a local path."""

    def __init__(self, refusal: ContentStoreRefusalV1) -> None:
        if type(refusal) is not ContentStoreRefusalV1:
            raise TypeError("content-store exception requires ContentStoreRefusalV1")
        self.refusal = refusal
        super().__init__(
            f"{refusal.operation.value}:{refusal.code.value}: {refusal.detail}"
        )


@dataclass(frozen=True, slots=True)
class StoredPackTransportV1:
    """Path-free receipt for one exact immutable source transport."""

    descriptor: PackTransferDescriptorV1
    already_registered: bool

    schema_id: ClassVar[str] = STORED_PACK_TRANSPORT_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.descriptor) is not PackTransferDescriptorV1:
            raise TypeError("stored transport descriptor is invalid")
        if type(self.already_registered) is not bool:
            raise TypeError("stored transport registration state must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "already_registered": self.already_registered,
            "descriptor": self.descriptor.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ReceivedPackInstallationV1:
    """Proof that received bytes installed at a validated registry-relative object."""

    descriptor: PackTransferDescriptorV1
    receipt: PackInstallReceiptV1

    schema_id: ClassVar[str] = RECEIVED_PACK_INSTALLATION_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.descriptor) is not PackTransferDescriptorV1:
            raise TypeError("received transport descriptor is invalid")
        if type(self.receipt) is not PackInstallReceiptV1:
            raise TypeError("received pack installation receipt is invalid")
        if not hmac.compare_digest(self.descriptor.pack_id, self.receipt.pack_id):
            raise ValueError("installation receipt differs from received logical pack")

    def as_dict(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor.as_dict(),
            "receipt": self.receipt.as_dict(),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ResultAttemptStageV1:
    """Opaque local capability for one not-yet-registered result attempt.

    The attempt ID is operational metadata.  ``stage_key_sha256`` is the sole
    directory leaf derived from it, and neither value enters a scientific result
    identity or a worker wire record.
    """

    attempt_id: str
    stage_key_sha256: str
    work_request_id: str
    logical_work_unit_id: str
    capability_nonce: str

    schema_id: ClassVar[str] = RESULT_ATTEMPT_STAGE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.attempt_id) is not str or _ATTEMPT_ID.fullmatch(self.attempt_id) is None:
            raise ValueError("result attempt ID must be canonical operational text")
        _require_sha256(self.stage_key_sha256, "result attempt stage key")
        _require_sha256(self.work_request_id, "result attempt work-request ID")
        _require_sha256(
            self.logical_work_unit_id,
            "result attempt logical work-unit ID",
        )
        if (
            type(self.capability_nonce) is not str
            or len(self.capability_nonce) != 32
            or any(character not in "0123456789abcdef" for character in self.capability_nonce)
        ):
            raise ValueError("result attempt capability nonce must be 128-bit hex")
        expected = _attempt_stage_key(
            self.attempt_id,
            self.work_request_id,
            self.logical_work_unit_id,
            self.capability_nonce,
        )
        if not hmac.compare_digest(self.stage_key_sha256, expected):
            raise ValueError("result attempt stage key differs from its capability")

    def as_dict(self) -> dict[str, object]:
        """Return diagnostic metadata, not a wire-serializable filesystem handle."""

        return {
            "attempt_id": self.attempt_id,
            "logical_work_unit_id": self.logical_work_unit_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "stage_key_sha256": self.stage_key_sha256,
            "work_request_id": self.work_request_id,
        }


@dataclass(frozen=True, slots=True)
class RegisteredResultBundleV1:
    """Coordinator-side immutable registration receipt with no local paths."""

    manifest: ResultBundleManifestV1
    manifest_sha256: str
    artifact_count: int

    schema_id: ClassVar[str] = REGISTERED_RESULT_BUNDLE_SCHEMA_ID
    schema_version: ClassVar[int] = ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.manifest) is not ResultBundleManifestV1:
            raise TypeError("registered result manifest is invalid")
        _require_sha256(self.manifest_sha256, "registered result-manifest digest")
        expected = hashlib.sha256(self.manifest.canonical_bytes()).hexdigest()
        if not hmac.compare_digest(self.manifest_sha256, expected):
            raise ValueError("registered result-manifest digest differs from bytes")
        if type(self.artifact_count) is not int or self.artifact_count <= 0:
            raise ValueError("registered result artifact count must be positive")
        if self.artifact_count != len(self.manifest.artifacts):
            raise ValueError("registered result artifact count differs from manifest")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_count": self.artifact_count,
            "manifest": self.manifest.as_dict(),
            "manifest_sha256": self.manifest_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }


class OrchestrationContentStoreV1:
    """One confined transport/result CAS rooted in an explicit ``DataPaths`` map."""

    __slots__ = ("_paths", "_limits")

    def __init__(
        self,
        *,
        paths: DataPaths,
        limits: PackValidationLimitsV1 = DEFAULT_PACK_VALIDATION_LIMITS_V1,
    ) -> None:
        if type(paths) is not DataPaths:
            raise TypeError("orchestration content store requires exact DataPaths")
        if type(limits) is not PackValidationLimitsV1:
            raise TypeError("content-store limits must be PackValidationLimitsV1")
        paths.validate()
        self._paths = paths
        self._limits = limits

    @property
    def paths(self) -> DataPaths:
        return self._paths

    @property
    def limits(self) -> PackValidationLimitsV1:
        return self._limits

    def register_source_transport(
        self,
        bundle: PackTransferBundleV1,
    ) -> StoredPackTransportV1:
        """Preflight and immutably register exact source archive bytes.

        Local registration is not transfer authorization.  Restricted historical
        bytes may be retained locally, while :meth:`serve_source_transport` still
        refuses to send them through the compatibility authorization boundary.
        """

        operation = ContentStoreOperationV1.REGISTER_TRANSPORT
        try:
            preflight = self._preflight_bundle(bundle)
            self._require_descriptor_preflight(
                bundle.descriptor,
                preflight,
                operation,
            )
            cache_descriptor = self._ensure_area(DataAreaId.CACHE, operation)
            try:
                leaf_parent = _open_digest_parent(
                    cache_descriptor,
                    (_STORE_DIRECTORY, _TRANSPORT_DIRECTORY),
                    bundle.descriptor.transport_sha256,
                    create=True,
                )
                try:
                    with _exclusive_store_lock(cache_descriptor):
                        already_registered = _register_immutable_bytes(
                            leaf_parent,
                            bundle.descriptor.transport_sha256,
                            bundle.archive_bytes,
                            maximum_bytes=self._limits.maximum_archive_bytes,
                        )
                finally:
                    os.close(leaf_parent)
            finally:
                os.close(cache_descriptor)
            return StoredPackTransportV1(
                descriptor=bundle.descriptor,
                already_registered=already_registered,
            )
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.IO_FAILED,
                operation,
                "source transport could not be registered safely",
                content_sha256=_descriptor_transport_sha256(bundle),
                cause=error,
            )

    def serve_source_transport(
        self,
        request: ContentRequestV1,
        descriptor: PackTransferDescriptorV1,
        *,
        logical_work_unit: LogicalWorkUnit,
        authorization: ConditionalTransferAuthorizationV1 | None = None,
    ) -> PackTransferBundleV1:
        """Serve one requested registered bundle after fresh policy verification."""

        operation = ContentStoreOperationV1.SERVE_TRANSPORT
        if type(request) is not ContentRequestV1:
            raise TypeError("transport serving requires ContentRequestV1")
        if type(descriptor) is not PackTransferDescriptorV1:
            raise TypeError("transport serving requires PackTransferDescriptorV1")
        if type(logical_work_unit) is not LogicalWorkUnit:
            raise TypeError("transport serving requires LogicalWorkUnit")
        try:
            validate_required_content_references(
                logical_work_unit,
                request.content_references,
            )
            _require_requested_pack(request, descriptor, operation)
            cache_descriptor = self._ensure_area(
                DataAreaId.CACHE,
                operation,
                create=False,
            )
            try:
                leaf_parent = _open_digest_parent(
                    cache_descriptor,
                    (_STORE_DIRECTORY, _TRANSPORT_DIRECTORY),
                    descriptor.transport_sha256,
                    create=False,
                )
                try:
                    raw = _read_immutable_bytes(
                        leaf_parent,
                        descriptor.transport_sha256,
                        expected_sha256=descriptor.transport_sha256,
                        expected_byte_count=descriptor.byte_count,
                        maximum_bytes=self._limits.maximum_archive_bytes,
                    )
                finally:
                    os.close(leaf_parent)
            finally:
                os.close(cache_descriptor)
            bundle = PackTransferBundleV1(
                descriptor=descriptor,
                archive_bytes=raw,
            )
            preflight = self._preflight_bundle(bundle)
            self._require_descriptor_preflight(descriptor, preflight, operation)
            validate_pack_transfer_completeness(preflight.manifest)
            decision_identity = pack_redistribution_decision_identity(
                preflight.manifest,
                authorization=authorization,
            )
            _require_redistribution_decision(
                descriptor,
                decision_identity,
                operation,
            )
            return bundle
        except FileNotFoundError as error:
            _refuse(
                ContentStoreRefusalCodeV1.TRANSPORT_NOT_REGISTERED,
                operation,
                "requested transport digest is not registered",
                content_sha256=descriptor.transport_sha256,
                cause=error,
            )
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.TRANSPORT_OBJECT_INVALID,
                operation,
                "registered transport failed exact serving verification",
                content_sha256=descriptor.transport_sha256,
                cause=error,
            )

    def receive_and_install_pack(
        self,
        request: ContentRequestV1,
        bundle: PackTransferBundleV1,
        *,
        logical_work_unit: LogicalWorkUnit,
        environment: PackRuntimeEnvironmentV1,
        worker_compatibility: WorkerCompatibilityV1,
        schema_bindings: tuple[PackSchemaIdentityBindingV1, ...],
        capability_bindings: tuple[PackCapabilityIdentityBindingV1, ...],
        authorization: ConditionalTransferAuthorizationV1 | None = None,
    ) -> ReceivedPackInstallationV1:
        """Repeat every receiver-side check, then privately stage and install.

        Compatibility and transfer authorization are checked before the first pack
        staging write.  ``install_pack`` then repeats stage verification and performs
        its existing dependency, registry, read-only object, and atomic activation
        checks.  A failed pre-install stage is discarded only through the pinned
        staging capability.
        """

        operation = ContentStoreOperationV1.RECEIVE_PACK
        if type(request) is not ContentRequestV1:
            raise TypeError("pack receiver requires ContentRequestV1")
        if type(logical_work_unit) is not LogicalWorkUnit:
            raise TypeError("pack receiver requires LogicalWorkUnit")
        if type(environment) is not PackRuntimeEnvironmentV1:
            raise TypeError("pack receiver requires PackRuntimeEnvironmentV1")
        if type(worker_compatibility) is not WorkerCompatibilityV1:
            raise TypeError("pack receiver requires WorkerCompatibilityV1")
        stage: ActivationEligiblePackStageV1 | None = None
        installed = False
        try:
            validate_required_content_references(
                logical_work_unit,
                request.content_references,
            )
            validate_worker_compatibility(
                logical_work_unit,
                worker_compatibility,
            )
            _require_requested_pack(request, bundle.descriptor, operation)
            preflight = self._preflight_bundle(bundle)
            self._require_descriptor_preflight(
                bundle.descriptor,
                preflight,
                operation,
            )
            validate_pack_transfer_completeness(preflight.manifest)
            decision_identity = pack_redistribution_decision_identity(
                preflight.manifest,
                authorization=authorization,
            )
            _require_redistribution_decision(
                bundle.descriptor,
                decision_identity,
                operation,
            )
            validate_pack_receiver_compatibility(
                preflight.manifest,
                environment,
                worker_compatibility,
                schema_bindings=schema_bindings,
                capability_bindings=capability_bindings,
            )
            self._paths.ensure_pack_installation_areas()
            self._paths.validate((DataAreaId.PACKS, DataAreaId.STAGING))
            stage = stage_preflighted_pack(
                bundle.archive_bytes,
                preflight,
                self._paths.staging,
                limits=self._limits,
            )
            receipt = install_pack(
                stage,
                paths=self._paths,
                environment=environment,
                limits=self._limits,
            )
            installed = True
            return ReceivedPackInstallationV1(
                descriptor=bundle.descriptor,
                receipt=receipt,
            )
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.PACK_RECEIVER_REFUSED,
                operation,
                "received pack failed transfer, compatibility, staging, or install checks",
                content_sha256=_descriptor_transport_sha256(bundle),
                cause=error,
            )
        finally:
            if stage is not None and not installed:
                try:
                    discard_pack_stage(stage, limits=self._limits)
                except Exception as cleanup_error:
                    if not _exception_is_active():
                        _refuse(
                            ContentStoreRefusalCodeV1.PACK_STAGE_CLEANUP_FAILED,
                            operation,
                            "failed received stage could not be discarded safely",
                            content_sha256=stage.transport_sha256,
                            cause=cleanup_error,
                        )

    def begin_result_attempt(
        self,
        *,
        attempt_id: str,
        work_request_id: str,
        logical_work_unit_id: str,
    ) -> ResultAttemptStageV1:
        """Create one private empty attempt stage beneath governed staging."""

        operation = ContentStoreOperationV1.BEGIN_RESULT_ATTEMPT
        if type(attempt_id) is not str or _ATTEMPT_ID.fullmatch(attempt_id) is None:
            raise ValueError("result attempt ID must be canonical operational text")
        _require_sha256(work_request_id, "result attempt work-request ID")
        _require_sha256(logical_work_unit_id, "result attempt logical work-unit ID")
        nonce = secrets.token_hex(16)
        capability = ResultAttemptStageV1(
            attempt_id=attempt_id,
            stage_key_sha256=_attempt_stage_key(
                attempt_id,
                work_request_id,
                logical_work_unit_id,
                nonce,
            ),
            work_request_id=work_request_id,
            logical_work_unit_id=logical_work_unit_id,
            capability_nonce=nonce,
        )
        try:
            staging_descriptor = self._ensure_area(DataAreaId.STAGING, operation)
            try:
                attempts_descriptor = _open_directory_chain(
                    staging_descriptor,
                    (_STORE_DIRECTORY, _RESULT_DIRECTORY, _ATTEMPT_DIRECTORY),
                    create=True,
                )
                try:
                    try:
                        os.mkdir(
                            capability.stage_key_sha256,
                            mode=0o700,
                            dir_fd=attempts_descriptor,
                        )
                    except FileExistsError as error:
                        _refuse(
                            ContentStoreRefusalCodeV1.ATTEMPT_ALREADY_EXISTS,
                            operation,
                            "result attempt stage identity already exists",
                            content_sha256=capability.stage_key_sha256,
                            cause=error,
                        )
                    stage_descriptor = _open_directory_at(
                        attempts_descriptor,
                        capability.stage_key_sha256,
                    )
                    try:
                        _require_private_directory(os.fstat(stage_descriptor))
                        _open_directory_chain(
                            stage_descriptor,
                            (_OBJECT_DIRECTORY,),
                            create=True,
                            close_result=True,
                        )
                        os.fsync(stage_descriptor)
                        os.fsync(attempts_descriptor)
                    finally:
                        os.close(stage_descriptor)
                finally:
                    os.close(attempts_descriptor)
            finally:
                os.close(staging_descriptor)
            return capability
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.IO_FAILED,
                operation,
                "result attempt stage could not be created safely",
                content_sha256=capability.stage_key_sha256,
                cause=error,
            )

    def stage_result_artifact(
        self,
        attempt: ResultAttemptStageV1,
        descriptor: ResultArtifactDescriptorV1,
        payload_bytes: bytes,
    ) -> ResultArtifactDescriptorV1:
        """Validate and write one exact result artifact into a private attempt."""

        operation = ContentStoreOperationV1.STAGE_RESULT_ARTIFACT
        _require_attempt(attempt)
        _require_result_artifact_bytes(descriptor, payload_bytes)
        if descriptor.byte_count > self._limits.maximum_file_expanded_bytes:
            _refuse(
                ContentStoreRefusalCodeV1.ARTIFACT_DESCRIPTOR_MISMATCH,
                operation,
                "result artifact exceeds the configured bounded object limit",
                content_sha256=descriptor.sha256,
            )
        # Reuse the WO38-B canonical JSON/JSONL parser and wall-clock screening.
        InlineArtifactV1(
            artifact_id=descriptor.artifact_id,
            media_type=descriptor.media_type,
            payload_bytes=payload_bytes,
        )
        try:
            staging_descriptor, attempts_descriptor, stage_descriptor = (
                self._open_attempt(attempt, operation)
            )
            try:
                objects_descriptor = _open_directory_at(
                    stage_descriptor,
                    _OBJECT_DIRECTORY,
                )
                try:
                    with _exclusive_store_lock(staging_descriptor):
                        if _entry_exists(objects_descriptor, descriptor.sha256):
                            _refuse(
                                ContentStoreRefusalCodeV1.ARTIFACT_ALREADY_STAGED,
                                operation,
                                "result artifact digest is already staged for the attempt",
                                content_sha256=descriptor.sha256,
                            )
                        _write_new_immutable_file(
                            objects_descriptor,
                            descriptor.sha256,
                            payload_bytes,
                            mode=0o600,
                        )
                        os.fsync(objects_descriptor)
                finally:
                    os.close(objects_descriptor)
            finally:
                os.close(stage_descriptor)
                os.close(attempts_descriptor)
                os.close(staging_descriptor)
            return descriptor
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.IO_FAILED,
                operation,
                "result artifact could not be staged safely",
                content_sha256=descriptor.sha256,
                cause=error,
            )

    def register_result_bundle(
        self,
        attempt: ResultAttemptStageV1,
        manifest: ResultBundleManifestV1,
        *,
        logical_work_unit: LogicalWorkUnit,
        coordinator_verification: VerifiedWorkResultV1,
    ) -> RegisteredResultBundleV1:
        """Atomically publish a coordinator-verified result manifest last.

        Every staged artifact is parsed and hashed again under a no-follow handle.
        Artifact objects are moved into the immutable runs CAS first.  The attempt
        directory is removed before the canonical manifest is renamed into the
        manifest CAS; therefore that final rename is the sole registration point.
        """

        operation = ContentStoreOperationV1.REGISTER_RESULT_BUNDLE
        _require_attempt(attempt)
        if type(manifest) is not ResultBundleManifestV1:
            raise TypeError("result registration requires ResultBundleManifestV1")
        if type(logical_work_unit) is not LogicalWorkUnit:
            raise TypeError("result registration requires LogicalWorkUnit")
        _require_verified_result_manifest(
            manifest,
            logical_work_unit,
            coordinator_verification,
            operation,
        )
        if (
            manifest.work_request_id != attempt.work_request_id
            or manifest.logical_work_unit_id != attempt.logical_work_unit_id
        ):
            _refuse(
                ContentStoreRefusalCodeV1.RESULT_MANIFEST_MISMATCH,
                operation,
                "result manifest belongs to a different attempt or logical work unit",
            )
        manifest_raw = manifest.canonical_bytes()
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        if len(manifest_raw) > self._limits.maximum_manifest_bytes:
            _refuse(
                ContentStoreRefusalCodeV1.RESULT_MANIFEST_MISMATCH,
                operation,
                "result manifest exceeds the configured canonical byte limit",
                content_sha256=manifest_sha256,
            )
        try:
            self._paths.ensure((DataAreaId.STAGING, DataAreaId.RUNS))
            self._paths.validate((DataAreaId.STAGING, DataAreaId.RUNS))
            staging_descriptor, attempts_descriptor, stage_descriptor = (
                self._open_attempt(attempt, operation)
            )
            runs_descriptor = self._ensure_area(DataAreaId.RUNS, operation)
            try:
                if os.fstat(staging_descriptor).st_dev != os.fstat(runs_descriptor).st_dev:
                    _refuse(
                        ContentStoreRefusalCodeV1.STORE_LAYOUT_UNSAFE,
                        operation,
                        "attempt staging and immutable result areas require one filesystem",
                    )
                staged_objects = _open_directory_at(
                    stage_descriptor,
                    _OBJECT_DIRECTORY,
                )
                result_root = _open_directory_chain(
                    runs_descriptor,
                    (_STORE_DIRECTORY, _RESULT_DIRECTORY),
                    create=True,
                )
                try:
                    with _exclusive_store_lock(runs_descriptor):
                        self._verify_attempt_inventory(
                            staged_objects,
                            manifest.artifacts,
                            operation,
                        )
                        manifest_parent = _open_digest_parent(
                            result_root,
                            (_MANIFEST_DIRECTORY,),
                            manifest_sha256,
                            create=True,
                        )
                        try:
                            if _entry_exists(manifest_parent, manifest_sha256):
                                _refuse(
                                    ContentStoreRefusalCodeV1.REGISTERED_CONTENT_IMMUTABLE,
                                    operation,
                                    "result manifest is already registered",
                                    content_sha256=manifest_sha256,
                                )
                            pending_manifest = _write_temp_file(
                                manifest_parent,
                                manifest_raw,
                                mode=0o400,
                            )
                            try:
                                for descriptor in manifest.artifacts:
                                    destination_parent = _open_digest_parent(
                                        result_root,
                                        (_OBJECT_DIRECTORY,),
                                        descriptor.sha256,
                                        create=True,
                                    )
                                    try:
                                        self._move_staged_artifact(
                                            staged_objects,
                                            destination_parent,
                                            descriptor,
                                            operation,
                                        )
                                    finally:
                                        os.close(destination_parent)
                                os.rmdir(_OBJECT_DIRECTORY, dir_fd=stage_descriptor)
                                os.close(stage_descriptor)
                                stage_descriptor = -1
                                os.rmdir(
                                    attempt.stage_key_sha256,
                                    dir_fd=attempts_descriptor,
                                )
                                os.fsync(attempts_descriptor)
                                os.rename(
                                    pending_manifest,
                                    manifest_sha256,
                                    src_dir_fd=manifest_parent,
                                    dst_dir_fd=manifest_parent,
                                )
                                pending_manifest = ""
                                os.fsync(manifest_parent)
                                os.fsync(result_root)
                            finally:
                                if pending_manifest:
                                    _unlink_if_present(manifest_parent, pending_manifest)
                        finally:
                            os.close(manifest_parent)
                finally:
                    os.close(result_root)
                    os.close(staged_objects)
            finally:
                if stage_descriptor >= 0:
                    os.close(stage_descriptor)
                os.close(attempts_descriptor)
                os.close(staging_descriptor)
                os.close(runs_descriptor)
            return RegisteredResultBundleV1(
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                artifact_count=len(manifest.artifacts),
            )
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.IO_FAILED,
                operation,
                "coordinator-verified result bundle could not be registered safely",
                content_sha256=manifest_sha256,
                cause=error,
            )

    def read_result_artifact(
        self,
        manifest_sha256: str,
        descriptor: ResultArtifactDescriptorV1,
    ) -> bytes:
        """Read one manifest-referenced artifact after fresh exact verification."""

        operation = ContentStoreOperationV1.READ_RESULT_ARTIFACT
        manifest_digest = _require_sha256(
            manifest_sha256,
            "registered result-manifest digest",
        )
        if type(descriptor) is not ResultArtifactDescriptorV1:
            raise TypeError("result read requires ResultArtifactDescriptorV1")
        try:
            manifest = self.read_result_manifest(manifest_digest)
            if descriptor not in manifest.artifacts:
                _refuse(
                    ContentStoreRefusalCodeV1.REGISTERED_OBJECT_INVALID,
                    operation,
                    "result artifact is not referenced by the registered manifest",
                    content_sha256=descriptor.sha256,
                )
            runs_descriptor = self._ensure_area(
                DataAreaId.RUNS,
                operation,
                create=False,
            )
            try:
                result_root = _open_directory_chain(
                    runs_descriptor,
                    (_STORE_DIRECTORY, _RESULT_DIRECTORY),
                    create=False,
                )
                try:
                    parent = _open_digest_parent(
                        result_root,
                        (_OBJECT_DIRECTORY,),
                        descriptor.sha256,
                        create=False,
                    )
                    try:
                        raw = _read_immutable_bytes(
                            parent,
                            descriptor.sha256,
                            expected_sha256=descriptor.sha256,
                            expected_byte_count=descriptor.byte_count,
                            maximum_bytes=self._limits.maximum_file_expanded_bytes,
                        )
                    finally:
                        os.close(parent)
                finally:
                    os.close(result_root)
            finally:
                os.close(runs_descriptor)
            _require_result_artifact_bytes(descriptor, raw)
            InlineArtifactV1(
                artifact_id=descriptor.artifact_id,
                media_type=descriptor.media_type,
                payload_bytes=raw,
            )
            return raw
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.REGISTERED_OBJECT_INVALID,
                operation,
                "registered result artifact failed exact read verification",
                content_sha256=descriptor.sha256,
                cause=error,
            )

    def read_result_manifest(self, manifest_sha256: str) -> ResultBundleManifestV1:
        """Read one canonical registered manifest by digest, without a path input."""

        operation = ContentStoreOperationV1.READ_RESULT_MANIFEST
        digest = _require_sha256(manifest_sha256, "registered result-manifest digest")
        try:
            runs_descriptor = self._ensure_area(
                DataAreaId.RUNS,
                operation,
                create=False,
            )
            try:
                result_root = _open_directory_chain(
                    runs_descriptor,
                    (_STORE_DIRECTORY, _RESULT_DIRECTORY),
                    create=False,
                )
                try:
                    parent = _open_digest_parent(
                        result_root,
                        (_MANIFEST_DIRECTORY,),
                        digest,
                        create=False,
                    )
                    try:
                        raw = _read_immutable_bytes(
                            parent,
                            digest,
                            expected_sha256=digest,
                            expected_byte_count=None,
                            maximum_bytes=self._limits.maximum_manifest_bytes,
                        )
                    finally:
                        os.close(parent)
                finally:
                    os.close(result_root)
            finally:
                os.close(runs_descriptor)
            manifest = ResultBundleManifestV1.from_canonical_bytes(raw)
            if hashlib.sha256(manifest.canonical_bytes()).hexdigest() != digest:
                raise ValueError("registered result manifest changed during parsing")
            return manifest
        except ContentStoreRefused:
            raise
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.REGISTERED_MANIFEST_INVALID,
                operation,
                "registered result manifest failed canonical verification",
                content_sha256=digest,
                cause=error,
            )

    def discard_result_attempt(self, attempt: ResultAttemptStageV1) -> None:
        """Remove only one exact unregistered private attempt stage.

        Registered results have no attempt directory, and this API never accepts a
        result or manifest digest.  It therefore cannot remove registered content.
        """

        operation = ContentStoreOperationV1.DISCARD_RESULT_ATTEMPT
        _require_attempt(attempt)
        try:
            staging_descriptor, attempts_descriptor, stage_descriptor = (
                self._open_attempt(attempt, operation)
            )
            try:
                objects_descriptor = _open_directory_at(
                    stage_descriptor,
                    _OBJECT_DIRECTORY,
                )
                try:
                    names = _directory_entry_names(objects_descriptor)
                    for name in names:
                        _require_sha256(name, "staged result object name")
                        _unlink_regular_file(objects_descriptor, name)
                    os.fsync(objects_descriptor)
                finally:
                    os.close(objects_descriptor)
                os.rmdir(_OBJECT_DIRECTORY, dir_fd=stage_descriptor)
                os.close(stage_descriptor)
                stage_descriptor = -1
                os.rmdir(attempt.stage_key_sha256, dir_fd=attempts_descriptor)
                os.fsync(attempts_descriptor)
            finally:
                if stage_descriptor >= 0:
                    os.close(stage_descriptor)
                os.close(attempts_descriptor)
                os.close(staging_descriptor)
        except ContentStoreRefused:
            raise
        except FileNotFoundError as error:
            _refuse(
                ContentStoreRefusalCodeV1.ATTEMPT_NOT_FOUND,
                operation,
                "unregistered result attempt does not exist",
                content_sha256=attempt.stage_key_sha256,
                cause=error,
            )
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.IO_FAILED,
                operation,
                "unregistered result attempt could not be discarded safely",
                content_sha256=attempt.stage_key_sha256,
                cause=error,
            )

    def _preflight_bundle(
        self,
        bundle: PackTransferBundleV1,
    ) -> PackArchivePreflightV1:
        if type(bundle) is not PackTransferBundleV1:
            raise TypeError("pack transfer requires PackTransferBundleV1")
        return preflight_pack_archive_bytes(
            bundle.archive_bytes,
            limits=self._limits,
            expected_pack_id=bundle.descriptor.pack_id,
            expected_transport_sha256=bundle.descriptor.transport_sha256,
        )

    @staticmethod
    def _require_descriptor_preflight(
        descriptor: PackTransferDescriptorV1,
        preflight: PackArchivePreflightV1,
        operation: ContentStoreOperationV1,
    ) -> None:
        if (
            descriptor.pack_id != preflight.pack_id
            or descriptor.transport_sha256 != preflight.transport_sha256
            or descriptor.byte_count != preflight.archive_byte_count
            or descriptor.manifest_sha256 != preflight.manifest_sha256
            or descriptor.inventory_sha256 != preflight.inventory_sha256
            or descriptor.validation_policy_id != preflight.validation_policy_id
        ):
            _refuse(
                ContentStoreRefusalCodeV1.TRANSPORT_DESCRIPTOR_MISMATCH,
                operation,
                "transfer descriptor differs from fresh full-archive preflight",
                content_sha256=preflight.transport_sha256,
            )

    def _ensure_area(
        self,
        area_id: DataAreaId,
        operation: ContentStoreOperationV1,
        *,
        create: bool = True,
    ) -> int:
        try:
            if create:
                self._paths.ensure(area_id)
            else:
                self._paths.validate(area_id)
            return _open_area_descriptor(self._paths, area_id)
        except Exception as error:
            _refuse(
                ContentStoreRefusalCodeV1.DATA_PATHS_UNSAFE,
                operation,
                f"governed {area_id.value} area is unavailable or unsafe",
                cause=error,
            )

    def _open_attempt(
        self,
        attempt: ResultAttemptStageV1,
        operation: ContentStoreOperationV1,
    ) -> tuple[int, int, int]:
        staging_descriptor = self._ensure_area(
            DataAreaId.STAGING,
            operation,
            create=False,
        )
        attempts_descriptor: int | None = None
        stage_descriptor: int | None = None
        try:
            attempts_descriptor = _open_directory_chain(
                staging_descriptor,
                (_STORE_DIRECTORY, _RESULT_DIRECTORY, _ATTEMPT_DIRECTORY),
                create=False,
            )
            stage_descriptor = _open_directory_at(
                attempts_descriptor,
                attempt.stage_key_sha256,
            )
            _require_private_directory(os.fstat(stage_descriptor))
            return staging_descriptor, attempts_descriptor, stage_descriptor
        except Exception:
            if stage_descriptor is not None:
                os.close(stage_descriptor)
            if attempts_descriptor is not None:
                os.close(attempts_descriptor)
            os.close(staging_descriptor)
            raise

    def _verify_attempt_inventory(
        self,
        staged_objects: int,
        descriptors: tuple[ResultArtifactDescriptorV1, ...],
        operation: ContentStoreOperationV1,
    ) -> None:
        expected = tuple(item.sha256 for item in descriptors)
        if len(expected) != len(set(expected)):
            _refuse(
                ContentStoreRefusalCodeV1.ARTIFACT_INVENTORY_MISMATCH,
                operation,
                "result manifest maps multiple artifacts to one staged object digest",
            )
        actual = _directory_entry_names(staged_objects)
        if tuple(sorted(actual)) != tuple(sorted(expected)):
            _refuse(
                ContentStoreRefusalCodeV1.ARTIFACT_INVENTORY_MISMATCH,
                operation,
                "staged artifact inventory differs from result manifest",
            )
        for descriptor in descriptors:
            raw = _read_immutable_bytes(
                staged_objects,
                descriptor.sha256,
                expected_sha256=descriptor.sha256,
                expected_byte_count=descriptor.byte_count,
                maximum_bytes=self._limits.maximum_file_expanded_bytes,
                require_read_only=False,
            )
            _require_result_artifact_bytes(descriptor, raw)
            InlineArtifactV1(
                artifact_id=descriptor.artifact_id,
                media_type=descriptor.media_type,
                payload_bytes=raw,
            )

    @staticmethod
    def _move_staged_artifact(
        staged_objects: int,
        destination_parent: int,
        descriptor: ResultArtifactDescriptorV1,
        operation: ContentStoreOperationV1,
    ) -> None:
        if _entry_exists(destination_parent, descriptor.sha256):
            existing = _read_immutable_bytes(
                destination_parent,
                descriptor.sha256,
                expected_sha256=descriptor.sha256,
                expected_byte_count=descriptor.byte_count,
                maximum_bytes=descriptor.byte_count,
            )
            staged = _read_immutable_bytes(
                staged_objects,
                descriptor.sha256,
                expected_sha256=descriptor.sha256,
                expected_byte_count=descriptor.byte_count,
                maximum_bytes=descriptor.byte_count,
                require_read_only=False,
            )
            if not hmac.compare_digest(existing, staged):
                _refuse(
                    ContentStoreRefusalCodeV1.REGISTERED_CONTENT_IMMUTABLE,
                    operation,
                    "existing result object differs from the staged exact bytes",
                    content_sha256=descriptor.sha256,
                )
            _unlink_regular_file(staged_objects, descriptor.sha256)
            return
        descriptor_fd = _open_regular_at(staged_objects, descriptor.sha256, writable=True)
        try:
            os.fchmod(descriptor_fd, 0o400)
            os.fsync(descriptor_fd)
        finally:
            os.close(descriptor_fd)
        os.rename(
            descriptor.sha256,
            descriptor.sha256,
            src_dir_fd=staged_objects,
            dst_dir_fd=destination_parent,
        )
        os.fsync(destination_parent)


def _attempt_stage_key(
    attempt_id: str,
    work_request_id: str,
    logical_work_unit_id: str,
    nonce: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "attempt_id": attempt_id,
                "capability_nonce": nonce,
                "logical_work_unit_id": logical_work_unit_id,
                "schema_id": RESULT_ATTEMPT_STAGE_SCHEMA_ID,
                "schema_version": ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION,
                "work_request_id": work_request_id,
            }
        )
    ).hexdigest()


def _require_attempt(value: object) -> ResultAttemptStageV1:
    if type(value) is not ResultAttemptStageV1:
        raise TypeError("result operation requires ResultAttemptStageV1")
    return value


def _require_verified_result_manifest(
    manifest: ResultBundleManifestV1,
    logical_work_unit: LogicalWorkUnit,
    coordinator_verification: object,
    operation: ContentStoreOperationV1,
) -> None:
    # Local import keeps the storage substrate independent while still requiring the
    # exact coordinator-produced verification contract at its registration boundary.
    from .coordinator import VerifiedWorkResultV1

    if type(coordinator_verification) is not VerifiedWorkResultV1:
        raise TypeError(
            "result registration requires exact VerifiedWorkResultV1 evidence"
        )
    verified = coordinator_verification
    expected_outputs = logical_work_unit.expected_outputs
    expected_names = tuple(item.name for item in expected_outputs)
    verified_names = tuple(item.artifact_id for item in verified.artifacts)
    if (
        logical_work_unit.logical_work_unit_id != verified.logical_work_unit_id
        or verified_names != expected_names
    ):
        _refuse(
            ContentStoreRefusalCodeV1.COORDINATOR_VERIFICATION_MISMATCH,
            operation,
            "coordinator verification differs from the logical output contract",
            content_sha256=verified.scientific_result_sha256,
        )
    schemas_by_name = {item.name: item for item in expected_outputs}
    expected_artifacts = tuple(
        ResultArtifactDescriptorV1(
            artifact_id=artifact.artifact_id,
            media_type=artifact.media_type,
            schema_identity=schemas_by_name[artifact.artifact_id],
            byte_count=artifact.byte_count,
            sha256=artifact.sha256,
        )
        for artifact in verified.artifacts
    )
    expected_manifest = ResultBundleManifestV1(
        work_request_id=verified.work_request_id,
        logical_work_unit_id=verified.logical_work_unit_id,
        worker_compatibility_sha256=verified.worker_compatibility_sha256,
        coordinator_verification_sha256=verified.scientific_result_sha256,
        artifacts=expected_artifacts,
        runtime_audit_results=tuple(
            item.result_reference for item in verified.runtime_audit_results
        ),
    )
    if manifest != expected_manifest:
        _refuse(
            ContentStoreRefusalCodeV1.COORDINATOR_VERIFICATION_MISMATCH,
            operation,
            "result manifest differs from independently verified coordinator bytes",
            content_sha256=verified.scientific_result_sha256,
        )


def _require_result_artifact_bytes(
    descriptor: object,
    raw: object,
) -> bytes:
    if type(descriptor) is not ResultArtifactDescriptorV1:
        raise TypeError("result artifact descriptor is invalid")
    if type(raw) is not bytes or not raw:
        raise ValueError("result artifact payload must be nonempty exact bytes")
    if len(raw) != descriptor.byte_count:
        raise ValueError("result artifact byte count differs from descriptor")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, descriptor.sha256):
        raise ValueError("result artifact bytes differ from descriptor digest")
    return raw


def _descriptor_transport_sha256(value: object) -> str | None:
    descriptor = getattr(value, "descriptor", None)
    digest = getattr(descriptor, "transport_sha256", None)
    if type(digest) is str and _SHA256.fullmatch(digest) is not None:
        return digest
    return None


def _require_requested_pack(
    request: ContentRequestV1,
    descriptor: PackTransferDescriptorV1,
    operation: ContentStoreOperationV1,
) -> None:
    if type(request) is not ContentRequestV1:
        raise TypeError("requested-pack check requires ContentRequestV1")
    if type(descriptor) is not PackTransferDescriptorV1:
        raise TypeError("requested-pack check requires PackTransferDescriptorV1")
    matches = tuple(
        reference
        for reference in request.content_references
        if hmac.compare_digest(reference.sha256, descriptor.pack_id)
    )
    if len(matches) != 1:
        _refuse(
            ContentStoreRefusalCodeV1.CONTENT_REQUEST_MISMATCH,
            operation,
            "pack logical digest is not present exactly once in the content request",
            content_sha256=descriptor.pack_id,
        )


def _require_redistribution_decision(
    descriptor: PackTransferDescriptorV1,
    actual: object,
    operation: ContentStoreOperationV1,
) -> None:
    from .models import DigestReferenceV1

    if type(actual) is not DigestReferenceV1:
        raise TypeError("redistribution decision must be DigestReferenceV1")
    declared = descriptor.redistribution_decision_identity
    if declared.name != actual.name or not hmac.compare_digest(
        declared.sha256,
        actual.sha256,
    ):
        _refuse(
            ContentStoreRefusalCodeV1.REDISTRIBUTION_DECISION_MISMATCH,
            operation,
            "transfer descriptor differs from the fresh redistribution decision",
            content_sha256=descriptor.pack_id,
        )


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _open_area_descriptor(paths: DataPaths, area_id: DataAreaId) -> int:
    root_descriptor = _open_absolute_directory(paths.root)
    try:
        parts = PurePosixPath(paths.area_children[area_id]).parts
        current = os.dup(root_descriptor)
        try:
            for part in parts:
                child = _open_directory_at(current, part)
                os.close(current)
                current = child
            return current
        except Exception:
            os.close(current)
            raise
    finally:
        os.close(root_descriptor)


def _open_absolute_directory(path: object) -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("content store requires no-follow directory handles")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    _require_safe_directory(os.fstat(descriptor))
    return descriptor


def _directory_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("content store requires no-follow directory handles")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_directory_at(parent: int, name: str) -> int:
    _require_private_component(name)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    _require_safe_directory(os.fstat(descriptor))
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    pinned = os.fstat(descriptor)
    if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
        os.close(descriptor)
        raise RuntimeError("content-store directory name was rebound")
    return descriptor


def _open_directory_chain(
    root: int,
    parts: tuple[str, ...],
    *,
    create: bool,
    close_result: bool = False,
) -> int:
    current = os.dup(root)
    try:
        for part in parts:
            _require_private_component(part)
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
            child = _open_directory_at(current, part)
            os.close(current)
            current = child
        if close_result:
            os.close(current)
            return -1
        return current
    except Exception:
        if current >= 0:
            os.close(current)
        raise


def _open_digest_parent(
    root: int,
    prefix: tuple[str, ...],
    digest: str,
    *,
    create: bool,
) -> int:
    canonical = _require_sha256(digest, "content-store object digest")
    return _open_directory_chain(
        root,
        (*prefix, _DIGEST_ALGORITHM_DIRECTORY, canonical[:2]),
        create=create,
    )


def _require_private_component(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("content-store internal path component is invalid")
    return value


def _require_safe_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("content-store node is not a real directory")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError("content-store directory has a foreign owner")
    if metadata.st_mode & 0o022:
        raise PermissionError("content-store directory is group/world writable")


def _require_private_directory(metadata: os.stat_result) -> None:
    _require_safe_directory(metadata)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("attempt stage must not grant group/world access")


class _exclusive_store_lock:
    def __init__(self, area_descriptor: int) -> None:
        self._area_descriptor = area_descriptor
        self._descriptor: int | None = None

    def __enter__(self) -> _exclusive_store_lock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(_LOCK_FILENAME, flags, 0o600, dir_fd=self._area_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise ValueError("content-store lock is not one regular file")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            os.close(descriptor)
            raise PermissionError("content-store lock has a foreign owner")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _register_immutable_bytes(
    parent: int,
    leaf: str,
    raw: bytes,
    *,
    maximum_bytes: int,
) -> bool:
    _require_sha256(leaf, "immutable object name")
    if type(raw) is not bytes or not raw or len(raw) > maximum_bytes:
        raise ValueError("immutable object bytes are empty or exceed their limit")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, leaf):
        raise ValueError("immutable object name differs from exact bytes")
    if _entry_exists(parent, leaf):
        existing = _read_immutable_bytes(
            parent,
            leaf,
            expected_sha256=leaf,
            expected_byte_count=len(raw),
            maximum_bytes=maximum_bytes,
        )
        if not hmac.compare_digest(existing, raw):
            raise RuntimeError("immutable object address already binds different bytes")
        return True
    temporary = _write_temp_file(parent, raw, mode=0o400)
    try:
        os.rename(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = ""
        os.fsync(parent)
    finally:
        if temporary:
            _unlink_if_present(parent, temporary)
    return False


def _write_temp_file(parent: int, raw: bytes, *, mode: int) -> str:
    for _attempt in range(32):
        name = _TEMP_PREFIX + secrets.token_hex(16)
        try:
            _write_new_immutable_file(parent, name, raw, mode=mode)
            return name
        except FileExistsError:
            continue
    raise RuntimeError("content store could not allocate a private temporary file")


def _write_new_immutable_file(parent: int, name: str, raw: bytes, *, mode: int) -> None:
    _require_private_component(name)
    if type(raw) is not bytes or not raw:
        raise ValueError("content-store file must contain nonempty exact bytes")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, mode, dir_fd=parent)
    try:
        view = memoryview(raw)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset : offset + _READ_CHUNK_BYTES])
            if written <= 0:
                raise OSError("content-store write did not make progress")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != len(raw)
        ):
            raise RuntimeError("content-store file changed while being written")
    except Exception:
        os.close(descriptor)
        _unlink_if_present(parent, name)
        raise
    else:
        os.close(descriptor)


def _read_immutable_bytes(
    parent: int,
    leaf: str,
    *,
    expected_sha256: str,
    expected_byte_count: int | None,
    maximum_bytes: int,
    require_read_only: bool = True,
) -> bytes:
    _require_private_component(leaf)
    digest = _require_sha256(expected_sha256, "expected immutable object digest")
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("immutable read limit must be positive")
    descriptor = _open_regular_at(parent, leaf, writable=False)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise ValueError("immutable object metadata is invalid")
        if require_read_only and stat.S_IMODE(before.st_mode) & 0o222:
            raise PermissionError("registered immutable object remains writable")
        if expected_byte_count is not None and before.st_size != expected_byte_count:
            raise ValueError("immutable object size differs from descriptor")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError("immutable object exceeds bounded read limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("immutable object changed during read")
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise RuntimeError("immutable object read was incomplete")
        actual = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual, digest):
            raise ValueError("immutable object digest differs from exact bytes")
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError("immutable object name was rebound during read")
        return raw
    finally:
        os.close(descriptor)


def _open_regular_at(parent: int, name: str, *, writable: bool) -> int:
    _require_private_component(name)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("content-store object is not one regular file")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise PermissionError("content-store object has a foreign owner")
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise RuntimeError("content-store object name was rebound")
    return descriptor


def _entry_exists(parent: int, name: str) -> bool:
    _require_private_component(name)
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _directory_entry_names(descriptor: int) -> tuple[str, ...]:
    with os.scandir(descriptor) as entries:
        names = tuple(sorted(entry.name for entry in entries))
    if any(type(name) is not str for name in names):
        raise TypeError("content-store directory contains a non-text name")
    return names


def _unlink_regular_file(parent: int, name: str) -> None:
    descriptor = _open_regular_at(parent, name, writable=False)
    try:
        pinned = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise RuntimeError("content-store file was rebound before unlink")
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(descriptor)


def _unlink_if_present(parent: int, name: str) -> None:
    try:
        _unlink_regular_file(parent, name)
    except FileNotFoundError:
        pass


def _exception_is_active() -> bool:
    # Avoid importing ``sys`` merely for a single cleanup branch at module scope.
    import sys

    return sys.exc_info()[0] is not None


def _refuse(
    code: ContentStoreRefusalCodeV1,
    operation: ContentStoreOperationV1,
    detail: str,
    *,
    content_sha256: str | None = None,
    cause: BaseException | None = None,
) -> NoReturn:
    refusal = ContentStoreRefusalV1(
        code=code,
        operation=operation,
        detail=detail,
        content_sha256=content_sha256,
    )
    if cause is None:
        raise ContentStoreRefused(refusal)
    raise ContentStoreRefused(refusal) from cause


__all__ = [
    "ORCHESTRATION_CONTENT_STORE_SCHEMA_VERSION",
    "RECEIVED_PACK_INSTALLATION_SCHEMA_ID",
    "REGISTERED_RESULT_BUNDLE_SCHEMA_ID",
    "RESULT_ATTEMPT_STAGE_SCHEMA_ID",
    "STORED_PACK_TRANSPORT_SCHEMA_ID",
    "ContentStoreOperationV1",
    "ContentStoreRefusalCodeV1",
    "ContentStoreRefusalV1",
    "ContentStoreRefused",
    "OrchestrationContentStoreV1",
    "ReceivedPackInstallationV1",
    "RegisteredResultBundleV1",
    "ResultAttemptStageV1",
    "StoredPackTransportV1",
]
