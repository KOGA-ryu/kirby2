"""Pure WO38-C content, receiver, and redistribution compatibility policy.

This module performs no filesystem, transport, registry, clock, or network work.  It
turns the exact identities already carried by orchestration and pack contracts into
closed allow/refuse decisions.  Names remain labels: schema and capability support is
accepted only through explicit bindings to exact worker digest identities.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, NoReturn

from kirby2.packs.dependencies import (
    PackRuntimeEnvironmentV1,
    validate_installability,
)
from kirby2.packs.formats import (
    canonical_json_bytes,
    canonical_manifest_bytes,
    load_canonical_json_bytes,
    require_data_identifier,
    require_sha256,
)
from kirby2.packs.models import (
    PackCompatibilityV1,
    PackContentModeV1,
    PackManifestV1,
    PackRedistributionPolicyV1,
)

from .artifacts import ContentRequestV1
from .models import DigestReferenceV1, LogicalWorkUnit
from .protocol import WorkerCompatibilityV1


CONDITIONAL_TRANSFER_AUTHORIZATION_SCHEMA_ID = (
    "KIRBY2_CONDITIONAL_TRANSFER_AUTHORIZATION_V1"
)
CONDITIONAL_TRANSFER_AUTHORIZATION_SCHEMA_VERSION = 1
PACK_REDISTRIBUTION_DECISION_SCHEMA_ID = "KIRBY2_PACK_REDISTRIBUTION_DECISION_V1"
PACK_REDISTRIBUTION_DECISION_SCHEMA_VERSION = 1


class OrchestrationCompatibilityRefusalCodeV1(str, Enum):
    """Closed refusal vocabulary for pure WO38-C policy decisions."""

    REQUIRED_CONTENT_AMBIGUOUS = "REQUIRED_CONTENT_AMBIGUOUS"
    REQUIRED_CONTENT_NONCANONICAL = "REQUIRED_CONTENT_NONCANONICAL"
    REQUIRED_CONTENT_MISSING = "REQUIRED_CONTENT_MISSING"
    REQUIRED_CONTENT_EXTRA = "REQUIRED_CONTENT_EXTRA"
    REQUIRED_CONTENT_INVENTORY_MISMATCH = "REQUIRED_CONTENT_INVENTORY_MISMATCH"
    REQUIRED_CONTENT_DIGEST_MISMATCH = "REQUIRED_CONTENT_DIGEST_MISMATCH"
    WORKER_COMPATIBILITY_MISMATCH = "WORKER_COMPATIBILITY_MISMATCH"
    PACK_INSTALLABILITY_MISMATCH = "PACK_INSTALLABILITY_MISMATCH"
    SCHEMA_BINDING_MISSING = "SCHEMA_BINDING_MISSING"
    SCHEMA_BINDING_EXTRA = "SCHEMA_BINDING_EXTRA"
    SCHEMA_BINDING_VERSION_MISMATCH = "SCHEMA_BINDING_VERSION_MISMATCH"
    SCHEMA_IDENTITY_UNAVAILABLE = "SCHEMA_IDENTITY_UNAVAILABLE"
    CAPABILITY_BINDING_MISSING = "CAPABILITY_BINDING_MISSING"
    CAPABILITY_BINDING_EXTRA = "CAPABILITY_BINDING_EXTRA"
    CAPABILITY_IDENTITY_UNAVAILABLE = "CAPABILITY_IDENTITY_UNAVAILABLE"
    REFERENCE_ONLY_CONTENT_INCOMPLETE = "REFERENCE_ONLY_CONTENT_INCOMPLETE"
    REDISTRIBUTION_PROHIBITED = "REDISTRIBUTION_PROHIBITED"
    REDISTRIBUTION_UNKNOWN = "REDISTRIBUTION_UNKNOWN"
    CONDITIONAL_AUTHORIZATION_REQUIRED = "CONDITIONAL_AUTHORIZATION_REQUIRED"
    CONDITIONAL_AUTHORIZATION_MISMATCH = "CONDITIONAL_AUTHORIZATION_MISMATCH"
    CONDITIONAL_AUTHORIZATION_UNEXPECTED = "CONDITIONAL_AUTHORIZATION_UNEXPECTED"


@dataclass(frozen=True, slots=True)
class OrchestrationCompatibilityRefusalV1:
    """One bounded data-only refusal with a stable machine-readable code."""

    code: OrchestrationCompatibilityRefusalCodeV1
    detail: str

    def __post_init__(self) -> None:
        if type(self.code) is not OrchestrationCompatibilityRefusalCodeV1:
            raise TypeError("orchestration compatibility refusal code is invalid")
        if (
            type(self.detail) is not str
            or not self.detail
            or len(self.detail.encode("utf-8")) > 1024
        ):
            raise ValueError("orchestration compatibility refusal detail is invalid")

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code.value, "detail": self.detail}


class OrchestrationCompatibilityRefused(ValueError):
    """Raised when one closed compatibility or redistribution rule refuses."""

    def __init__(self, refusal: OrchestrationCompatibilityRefusalV1) -> None:
        if type(refusal) is not OrchestrationCompatibilityRefusalV1:
            raise TypeError("compatibility refusal exception requires a typed refusal")
        self.refusal = refusal
        super().__init__(f"{refusal.code.value}: {refusal.detail}")

    @property
    def code(self) -> OrchestrationCompatibilityRefusalCodeV1:
        return self.refusal.code


@dataclass(frozen=True, slots=True)
class ConditionalTransferAuthorizationV1:
    """Explicit evidence binding permission to one exact logical pack manifest.

    The record is authorization *evidence*, not a signature or a legal conclusion.
    The caller remains responsible for supplying it from an authorized policy
    boundary.  Its content-derived digest lets transfer receipts retain exactly which
    authorization decision was applied without embedding ambient paths or clocks.
    """

    authorization_id: str
    policy_id: str
    pack_id: str
    manifest_sha256: str
    authorization_evidence_sha256: str

    schema_id: ClassVar[str] = CONDITIONAL_TRANSFER_AUTHORIZATION_SCHEMA_ID
    schema_version: ClassVar[int] = (
        CONDITIONAL_TRANSFER_AUTHORIZATION_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        require_data_identifier(
            self.authorization_id,
            "conditional transfer authorization ID",
        )
        require_data_identifier(
            self.policy_id,
            "conditional transfer policy ID",
        )
        require_sha256(self.pack_id, "conditional transfer pack ID")
        require_sha256(
            self.manifest_sha256,
            "conditional transfer manifest digest",
        )
        require_sha256(
            self.authorization_evidence_sha256,
            "conditional transfer evidence digest",
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "authorization_evidence_sha256": self.authorization_evidence_sha256,
            "authorization_id": self.authorization_id,
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "policy_id": self.policy_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    @property
    def authorization_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.identity_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_dict(),
            "authorization_sha256": self.authorization_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> ConditionalTransferAuthorizationV1:
        payload = _exact_object(
            value,
            {
                "authorization_evidence_sha256",
                "authorization_id",
                "authorization_sha256",
                "manifest_sha256",
                "pack_id",
                "policy_id",
                "schema_id",
                "schema_version",
            },
            "conditional transfer authorization",
        )
        if type(payload["schema_id"]) is not str or payload["schema_id"] != cls.schema_id:
            raise ValueError("conditional transfer authorization schema ID differs")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != cls.schema_version
        ):
            raise ValueError(
                "conditional transfer authorization schema version differs"
            )
        declared = require_sha256(
            payload["authorization_sha256"],
            "declared conditional transfer authorization digest",
        )
        restored = cls(
            authorization_id=_exact_text(payload, "authorization_id"),
            policy_id=_exact_text(payload, "policy_id"),
            pack_id=_exact_text(payload, "pack_id"),
            manifest_sha256=_exact_text(payload, "manifest_sha256"),
            authorization_evidence_sha256=_exact_text(
                payload,
                "authorization_evidence_sha256",
            ),
        )
        if not hmac.compare_digest(declared, restored.authorization_sha256):
            raise ValueError(
                "conditional transfer authorization digest differs from content"
            )
        if restored.as_dict() != payload:
            raise ValueError(
                "conditional transfer authorization did not round-trip exactly"
            )
        return restored

    @classmethod
    def from_canonical_bytes(
        cls,
        raw: bytes,
    ) -> ConditionalTransferAuthorizationV1:
        restored = cls.from_dict(
            load_canonical_json_bytes(raw, "conditional transfer authorization")
        )
        if restored.canonical_bytes() != raw:
            raise ValueError(
                "conditional transfer authorization bytes are not canonical"
            )
        return restored


@dataclass(frozen=True, slots=True)
class PackRedistributionDecisionV1:
    """One permitted redistribution result recomputable from manifest and evidence."""

    pack_id: str
    manifest_sha256: str
    redistribution_policy: PackRedistributionPolicyV1
    conditional_authorization_sha256: str | None

    schema_id: ClassVar[str] = PACK_REDISTRIBUTION_DECISION_SCHEMA_ID
    schema_version: ClassVar[int] = PACK_REDISTRIBUTION_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_sha256(self.pack_id, "redistribution decision pack ID")
        require_sha256(
            self.manifest_sha256,
            "redistribution decision manifest digest",
        )
        if type(self.redistribution_policy) is not PackRedistributionPolicyV1:
            raise TypeError("redistribution decision policy is invalid")
        if self.conditional_authorization_sha256 is not None:
            require_sha256(
                self.conditional_authorization_sha256,
                "redistribution decision authorization digest",
            )
        if self.redistribution_policy is PackRedistributionPolicyV1.ALLOWED:
            if self.conditional_authorization_sha256 is not None:
                raise ValueError(
                    "unconditional redistribution cannot bind conditional evidence"
                )
            return
        if self.redistribution_policy is PackRedistributionPolicyV1.CONDITIONAL:
            if self.conditional_authorization_sha256 is None:
                raise ValueError(
                    "conditional redistribution must bind authorization evidence"
                )
            return
        raise ValueError("a refused redistribution policy cannot create a decision")

    def identity_dict(self) -> dict[str, object]:
        return {
            "conditional_authorization_sha256": (
                self.conditional_authorization_sha256
            ),
            "manifest_sha256": self.manifest_sha256,
            "pack_id": self.pack_id,
            "redistribution_policy": self.redistribution_policy.value,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.identity_dict())

    @property
    def decision_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def digest_reference(self) -> DigestReferenceV1:
        return DigestReferenceV1(
            name=f"pack-redistribution:{self.pack_id}",
            sha256=self.decision_sha256,
        )


@dataclass(frozen=True, slots=True)
class PackSchemaIdentityBindingV1:
    """Explicit pack schema/version to exact worker schema-identity binding."""

    schema_id: str
    schema_version: int
    worker_schema_identity: DigestReferenceV1

    def __post_init__(self) -> None:
        require_data_identifier(self.schema_id, "bound pack schema ID")
        if type(self.schema_version) is not int or self.schema_version <= 0:
            raise ValueError("bound pack schema version must be a positive integer")
        if type(self.worker_schema_identity) is not DigestReferenceV1:
            raise TypeError("bound worker schema identity must be DigestReferenceV1")

    @property
    def sort_key(self) -> tuple[str, int, str, str]:
        return (
            self.schema_id,
            self.schema_version,
            *self.worker_schema_identity.sort_key,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "worker_schema_identity": self.worker_schema_identity.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PackCapabilityIdentityBindingV1:
    """Explicit pack capability label to exact worker capability identity."""

    capability_label: str
    worker_capability_identity: DigestReferenceV1

    def __post_init__(self) -> None:
        require_data_identifier(
            self.capability_label,
            "bound pack capability label",
        )
        if type(self.worker_capability_identity) is not DigestReferenceV1:
            raise TypeError(
                "bound worker capability identity must be DigestReferenceV1"
            )

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.capability_label, *self.worker_capability_identity.sort_key)

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_label": self.capability_label,
            "worker_capability_identity": self.worker_capability_identity.as_dict(),
        }


def required_content_references(
    logical_work_unit: LogicalWorkUnit,
) -> tuple[DigestReferenceV1, ...]:
    """Return the one canonical input inventory required by a logical work unit.

    Exact duplicate role references are transferred once.  Reusing one name for
    different digests is refused because a name-to-content binding would otherwise
    depend on which scientific role happened to be inspected first.
    """

    if type(logical_work_unit) is not LogicalWorkUnit:
        raise TypeError("required content projection requires LogicalWorkUnit")
    supplied = (
        logical_work_unit.scenario,
        logical_work_unit.market_profile,
        *logical_work_unit.datasets,
        *logical_work_unit.strategies,
        *logical_work_unit.packs,
    )
    by_name: dict[str, DigestReferenceV1] = {}
    for reference in supplied:
        if type(reference) is not DigestReferenceV1:
            raise TypeError("logical work content references must be DigestReferenceV1")
        prior = by_name.get(reference.name)
        if prior is None:
            by_name[reference.name] = reference
        elif prior != reference:
            _refuse(
                OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_AMBIGUOUS,
                "one required content name is bound to multiple digests",
            )
    canonical = tuple(sorted(by_name.values(), key=lambda item: item.sort_key))
    names_by_digest: dict[str, str] = {}
    for reference in canonical:
        prior_name = names_by_digest.get(reference.sha256)
        if prior_name is not None and prior_name != reference.name:
            _refuse(
                OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_AMBIGUOUS,
                "one required content digest is bound to multiple names",
            )
        names_by_digest[reference.sha256] = reference.name
    return canonical


def build_content_request(
    logical_work_unit: LogicalWorkUnit,
) -> ContentRequestV1:
    """Project one logical work unit into its exact path-free content request."""

    return ContentRequestV1(
        content_references=required_content_references(logical_work_unit)
    )


def validate_required_content_references(
    logical_work_unit: LogicalWorkUnit,
    supplied: tuple[DigestReferenceV1, ...],
) -> tuple[DigestReferenceV1, ...]:
    """Require a transfer inventory to contain every required reference and no extra."""

    expected = required_content_references(logical_work_unit)
    _require_unambiguous_references(supplied, "supplied content references")
    expected_by_name = {item.name: item for item in expected}
    supplied_by_name = {item.name: item for item in supplied}
    missing = set(expected_by_name) - set(supplied_by_name)
    extra = set(supplied_by_name) - set(expected_by_name)
    if missing and extra:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_INVENTORY_MISMATCH,
            "content transfer both omits required names and invents extra names",
        )
    if missing:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_MISSING,
            "content transfer omits one or more required references",
        )
    if extra:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_EXTRA,
            "content transfer contains one or more unrequested references",
        )
    if any(
        not hmac.compare_digest(
            expected_by_name[name].sha256,
            supplied_by_name[name].sha256,
        )
        for name in expected_by_name
    ):
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_DIGEST_MISMATCH,
            "content transfer names are correct but one or more digests differ",
        )
    if supplied != expected:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_NONCANONICAL,
            "content transfer inventory differs from canonical required order",
        )
    return expected


def validate_worker_compatibility(
    logical_work_unit: LogicalWorkUnit,
    worker_compatibility: WorkerCompatibilityV1,
) -> WorkerCompatibilityV1:
    """Require exact implementation/runtime/dependency/compiler/schema/capability IDs."""

    if type(logical_work_unit) is not LogicalWorkUnit:
        raise TypeError("worker compatibility validation requires LogicalWorkUnit")
    if type(worker_compatibility) is not WorkerCompatibilityV1:
        raise TypeError(
            "worker compatibility validation requires WorkerCompatibilityV1"
        )
    expected = WorkerCompatibilityV1.from_logical_work_unit(logical_work_unit)
    if (
        worker_compatibility != expected
        or not hmac.compare_digest(
            worker_compatibility.compatibility_sha256,
            expected.compatibility_sha256,
        )
    ):
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.WORKER_COMPATIBILITY_MISMATCH,
            "worker exact compatibility identities differ from logical work",
        )
    return worker_compatibility


def validate_pack_receiver_compatibility(
    manifest: PackManifestV1,
    environment: PackRuntimeEnvironmentV1,
    worker_compatibility: WorkerCompatibilityV1,
    *,
    schema_bindings: tuple[PackSchemaIdentityBindingV1, ...],
    capability_bindings: tuple[PackCapabilityIdentityBindingV1, ...],
) -> PackCompatibilityV1:
    """Validate installability plus explicit exact schema and capability bindings."""

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack receiver compatibility requires PackManifestV1")
    if type(environment) is not PackRuntimeEnvironmentV1:
        raise TypeError(
            "pack receiver compatibility requires PackRuntimeEnvironmentV1"
        )
    if type(worker_compatibility) is not WorkerCompatibilityV1:
        raise TypeError(
            "pack receiver compatibility requires WorkerCompatibilityV1"
        )
    _require_schema_bindings(schema_bindings)
    _require_capability_bindings(capability_bindings)
    try:
        installable = validate_installability(manifest, environment)
    except ValueError as error:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.PACK_INSTALLABILITY_MISMATCH,
            "pack INSTALLABLE engine, compiler, or schema requirements are unmet",
            cause=error,
        )

    expected_schema_ids = {item.schema_id for item in installable.schemas}
    supplied_schema_ids = {item.schema_id for item in schema_bindings}
    missing_schema_ids = expected_schema_ids - supplied_schema_ids
    extra_schema_ids = supplied_schema_ids - expected_schema_ids
    if missing_schema_ids:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.SCHEMA_BINDING_MISSING,
            "one or more installable pack schemas lack an explicit worker binding",
        )
    if extra_schema_ids:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.SCHEMA_BINDING_EXTRA,
            "schema bindings contain an entry not required by the pack",
        )
    schema_by_id = {item.schema_id: item for item in schema_bindings}
    worker_schema_identities = frozenset(worker_compatibility.schemas)
    for requirement in installable.schemas:
        binding = schema_by_id[requirement.schema_id]
        environment_version = environment.schema_version(requirement.schema_id)
        if (
            environment_version is None
            or binding.schema_version != environment_version
            or binding.schema_version not in requirement.supported_versions
        ):
            _refuse(
                OrchestrationCompatibilityRefusalCodeV1.SCHEMA_BINDING_VERSION_MISMATCH,
                "explicit worker schema binding differs from the installable version",
            )
        if binding.worker_schema_identity not in worker_schema_identities:
            _refuse(
                OrchestrationCompatibilityRefusalCodeV1.SCHEMA_IDENTITY_UNAVAILABLE,
                "explicit schema binding names an unavailable worker digest identity",
            )

    expected_capabilities = set(manifest.capability_labels)
    supplied_capabilities = {
        item.capability_label for item in capability_bindings
    }
    missing_capabilities = expected_capabilities - supplied_capabilities
    extra_capabilities = supplied_capabilities - expected_capabilities
    if missing_capabilities:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.CAPABILITY_BINDING_MISSING,
            "one or more pack capability labels lack an explicit worker binding",
        )
    if extra_capabilities:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.CAPABILITY_BINDING_EXTRA,
            "capability bindings contain a label not declared by the pack",
        )
    worker_capability_identities = frozenset(worker_compatibility.capabilities)
    for binding in capability_bindings:
        if binding.worker_capability_identity not in worker_capability_identities:
            _refuse(
                OrchestrationCompatibilityRefusalCodeV1.CAPABILITY_IDENTITY_UNAVAILABLE,
                "explicit capability binding names an unavailable worker identity",
            )
    return installable


def validate_pack_transfer_authorization(
    manifest: PackManifestV1,
    authorization: ConditionalTransferAuthorizationV1 | None = None,
) -> ConditionalTransferAuthorizationV1 | None:
    """Apply the closed redistribution policy for one exact logical pack.

    ``REFERENCE_ONLY`` never widens permission: this function intentionally decides
    only from ``redistribution_policy`` and exact conditional evidence.
    """

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack transfer authorization requires PackManifestV1")
    if authorization is not None and type(authorization) is not (
        ConditionalTransferAuthorizationV1
    ):
        raise TypeError(
            "conditional pack transfer authorization has an unsupported type"
        )
    policy = manifest.license.redistribution_policy
    if policy is PackRedistributionPolicyV1.PROHIBITED:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REDISTRIBUTION_PROHIBITED,
            "pack redistribution policy prohibits transfer",
        )
    if policy is PackRedistributionPolicyV1.UNKNOWN:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REDISTRIBUTION_UNKNOWN,
            "pack redistribution policy is unknown and therefore fails closed",
        )
    if policy is PackRedistributionPolicyV1.ALLOWED:
        if authorization is not None:
            _refuse(
                OrchestrationCompatibilityRefusalCodeV1.CONDITIONAL_AUTHORIZATION_UNEXPECTED,
                "an unconditional pack cannot carry conditional authorization evidence",
            )
        return None
    if policy is not PackRedistributionPolicyV1.CONDITIONAL:
        raise RuntimeError("pack redistribution policy is not exhaustively handled")
    if authorization is None:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.CONDITIONAL_AUTHORIZATION_REQUIRED,
            "conditional redistribution requires explicit digest-bound authorization",
        )
    expected_manifest_sha256 = hashlib.sha256(
        canonical_manifest_bytes(manifest)
    ).hexdigest()
    if (
        not hmac.compare_digest(authorization.pack_id, manifest.pack_id)
        or not hmac.compare_digest(
            authorization.manifest_sha256,
            expected_manifest_sha256,
        )
    ):
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.CONDITIONAL_AUTHORIZATION_MISMATCH,
            "conditional authorization is bound to different logical content",
        )
    return authorization


def pack_redistribution_decision(
    manifest: PackManifestV1,
    authorization: ConditionalTransferAuthorizationV1 | None = None,
) -> PackRedistributionDecisionV1:
    """Recompute the permitted redistribution decision for descriptor binding."""

    validated = validate_pack_transfer_authorization(manifest, authorization)
    return PackRedistributionDecisionV1(
        pack_id=manifest.pack_id,
        manifest_sha256=hashlib.sha256(
            canonical_manifest_bytes(manifest)
        ).hexdigest(),
        redistribution_policy=manifest.license.redistribution_policy,
        conditional_authorization_sha256=(
            None if validated is None else validated.authorization_sha256
        ),
    )


def pack_redistribution_decision_identity(
    manifest: PackManifestV1,
    authorization: ConditionalTransferAuthorizationV1 | None = None,
) -> DigestReferenceV1:
    """Return the exact identity a transfer descriptor must reproduce."""

    return pack_redistribution_decision(manifest, authorization).digest_reference


def validate_pack_transfer_completeness(
    manifest: PackManifestV1,
) -> PackManifestV1:
    """Require transferred bytes to be sufficient for clean-root activation/use.

    This decision is intentionally separate from redistribution permission.  A
    ``REFERENCE_ONLY`` pack may be legal to send yet still cannot reproduce its
    referenced content on a clean worker, so activation/execution transfer fails
    closed without weakening or reinterpreting the license declaration.
    """

    if type(manifest) is not PackManifestV1:
        raise TypeError("pack transfer completeness requires PackManifestV1")
    if manifest.license.content_mode is PackContentModeV1.REFERENCE_ONLY:
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REFERENCE_ONLY_CONTENT_INCOMPLETE,
            "reference-only pack bytes cannot establish clean-root reproduction",
        )
    if manifest.license.content_mode is not PackContentModeV1.SELF_CONTAINED:
        raise RuntimeError("pack content mode is not exhaustively handled")
    return manifest


def _require_unambiguous_references(
    values: object,
    label: str,
) -> None:
    if type(values) is not tuple or any(
        type(item) is not DigestReferenceV1 for item in values
    ):
        raise TypeError(f"{label} must be an immutable DigestReferenceV1 tuple")
    names = tuple(item.name for item in values)
    if len(names) != len(set(names)):
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_AMBIGUOUS,
            f"{label} contains duplicate names",
        )
    digests = tuple(item.sha256 for item in values)
    if len(digests) != len(set(digests)):
        _refuse(
            OrchestrationCompatibilityRefusalCodeV1.REQUIRED_CONTENT_AMBIGUOUS,
            f"{label} contains duplicate digests",
        )


def _require_schema_bindings(value: object) -> None:
    if type(value) is not tuple or any(
        type(item) is not PackSchemaIdentityBindingV1 for item in value
    ):
        raise TypeError("pack schema bindings must be an immutable typed tuple")
    if value != tuple(sorted(value, key=lambda item: item.sort_key)):
        raise ValueError("pack schema bindings must use canonical order")
    identifiers = tuple(item.schema_id for item in value)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("pack schema bindings must use unique schema IDs")


def _require_capability_bindings(value: object) -> None:
    if type(value) is not tuple or any(
        type(item) is not PackCapabilityIdentityBindingV1 for item in value
    ):
        raise TypeError("pack capability bindings must be an immutable typed tuple")
    if value != tuple(sorted(value, key=lambda item: item.sort_key)):
        raise ValueError("pack capability bindings must use canonical order")
    labels = tuple(item.capability_label for item in value)
    if len(labels) != len(set(labels)):
        raise ValueError("pack capability bindings must use unique labels")


def _exact_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} must be one exact object")
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )
    return value


def _exact_text(payload: dict[str, object], field: str) -> str:
    value = payload[field]
    if type(value) is not str:
        raise TypeError(f"serialized {field} must be exact text")
    return value


def _refuse(
    code: OrchestrationCompatibilityRefusalCodeV1,
    detail: str,
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    refusal = OrchestrationCompatibilityRefusalV1(code=code, detail=detail)
    if cause is None:
        raise OrchestrationCompatibilityRefused(refusal)
    raise OrchestrationCompatibilityRefused(refusal) from cause


__all__ = [
    "CONDITIONAL_TRANSFER_AUTHORIZATION_SCHEMA_ID",
    "CONDITIONAL_TRANSFER_AUTHORIZATION_SCHEMA_VERSION",
    "PACK_REDISTRIBUTION_DECISION_SCHEMA_ID",
    "PACK_REDISTRIBUTION_DECISION_SCHEMA_VERSION",
    "ConditionalTransferAuthorizationV1",
    "OrchestrationCompatibilityRefusalCodeV1",
    "OrchestrationCompatibilityRefusalV1",
    "OrchestrationCompatibilityRefused",
    "PackCapabilityIdentityBindingV1",
    "PackRedistributionDecisionV1",
    "PackSchemaIdentityBindingV1",
    "build_content_request",
    "pack_redistribution_decision",
    "pack_redistribution_decision_identity",
    "required_content_references",
    "validate_pack_receiver_compatibility",
    "validate_pack_transfer_authorization",
    "validate_pack_transfer_completeness",
    "validate_required_content_references",
    "validate_worker_compatibility",
]
