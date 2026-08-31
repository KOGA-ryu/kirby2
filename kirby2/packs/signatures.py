"""Optional provider-based authenticity for already-verified Kirby2 packs.

Signatures are detached from ``.k2pack`` identity and never participate in archive
activation.  The pack must pass its complete structural, digest, domain, capability,
license, and privacy validation before an authenticity result is useful.  This module
implements no cryptographic primitive and performs no provider discovery; callers
must supply an installed verifier explicitly.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .archive import PackArchivePreflightV1
from .formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_data_identifier,
    require_nfc_text,
    require_sha256,
)
from .models import PackTypeV1


PACK_SIGNATURE_ENVELOPE_SCHEMA_ID_V1 = "KIRBY2_PACK_SIGNATURE_ENVELOPE_V1"
PACK_SIGNATURE_MESSAGE_SCHEMA_ID_V1 = "KIRBY2_PACK_SIGNATURE_MESSAGE_V1"
PACK_SIGNATURE_SCHEMA_VERSION_V1 = 1
PACK_SIGNATURE_SCOPE_V1 = "PACK_ID_MANIFEST_AND_TRANSPORT_V1"
PACK_SIGNATURE_ENCODING_V1 = "BASE64_STANDARD_PADDED_V1"
MAX_PACK_SIGNATURE_FILE_BYTES_V1 = 128 * 1024
MAX_PACK_SIGNATURE_BYTES_V1 = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class PackAuthenticityStatusV1(str, Enum):
    """Signer/authenticity outcome, intentionally separate from pack safety."""

    UNSIGNED = "UNSIGNED"
    VERIFIED = "VERIFIED"
    INVALID = "INVALID"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class PackQualificationStatusV1(str, Enum):
    """Independent status vocabulary for non-authenticity qualification gates."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_ASSESSED = "NOT_ASSESSED"


class PackSignatureProviderV1(Protocol):
    """Caller-supplied verifier backed by an established signature implementation.

    A provider is local installed code, never content loaded from the pack being
    verified.  Implementations may use platform facilities or an optional dependency,
    but this interface deliberately defines no cryptography of its own.
    """

    provider_id: str
    algorithm_id: str

    def sign(
        self,
        *,
        key_id: str,
        message: bytes,
    ) -> bytes: ...

    def verify(
        self,
        *,
        key_id: str,
        message: bytes,
        signature: bytes,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class PackSignatureEnvelopeV1:
    """Canonical detached signature binding for one exact pack transport."""

    provider_id: str
    algorithm_id: str
    key_id: str
    pack_id: str
    manifest_sha256: str
    transport_sha256: str
    signature: bytes

    schema_id = PACK_SIGNATURE_ENVELOPE_SCHEMA_ID_V1
    schema_version = PACK_SIGNATURE_SCHEMA_VERSION_V1
    signature_scope = PACK_SIGNATURE_SCOPE_V1
    signature_encoding = PACK_SIGNATURE_ENCODING_V1

    def __post_init__(self) -> None:
        require_data_identifier(self.provider_id, "pack signature provider ID")
        require_data_identifier(self.algorithm_id, "pack signature algorithm ID")
        require_data_identifier(self.key_id, "pack signature key ID")
        require_sha256(self.pack_id, "signed pack ID")
        require_sha256(self.manifest_sha256, "signed pack manifest digest")
        require_sha256(self.transport_sha256, "signed pack transport digest")
        if (
            type(self.signature) is not bytes
            or not self.signature
            or len(self.signature) > MAX_PACK_SIGNATURE_BYTES_V1
        ):
            raise ValueError("pack signature bytes are empty or exceed the V1 bound")

    def message_dict(self) -> dict[str, object]:
        return _signature_message_dict(
            provider_id=self.provider_id,
            algorithm_id=self.algorithm_id,
            key_id=self.key_id,
            pack_id=self.pack_id,
            manifest_sha256=self.manifest_sha256,
            transport_sha256=self.transport_sha256,
        )

    def message_bytes(self) -> bytes:
        return canonical_json_bytes(self.message_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            **self.message_dict(),
            "schema_id": self.schema_id,
            "signature_base64": base64.b64encode(self.signature).decode("ascii"),
            "signature_encoding": self.signature_encoding,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> PackSignatureEnvelopeV1:
        value = load_canonical_json_bytes(raw, "pack signature envelope")
        expected = {
            "algorithm_id",
            "key_id",
            "manifest_sha256",
            "pack_id",
            "provider_id",
            "schema_id",
            "schema_version",
            "signature_base64",
            "signature_encoding",
            "signature_scope",
            "transport_sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("pack signature envelope fields differ")
        if (
            value["schema_id"] != cls.schema_id
            or value["schema_version"] != cls.schema_version
            or value["signature_scope"] != cls.signature_scope
            or value["signature_encoding"] != cls.signature_encoding
        ):
            raise ValueError("pack signature envelope contract differs")
        encoded = _text(value, "signature_base64")
        try:
            signature = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise ValueError("pack signature is not canonical base64") from error
        if base64.b64encode(signature).decode("ascii") != encoded:
            raise ValueError("pack signature base64 is not canonical")
        restored = cls(
            provider_id=_text(value, "provider_id"),
            algorithm_id=_text(value, "algorithm_id"),
            key_id=_text(value, "key_id"),
            pack_id=_text(value, "pack_id"),
            manifest_sha256=_text(value, "manifest_sha256"),
            transport_sha256=_text(value, "transport_sha256"),
            signature=signature,
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("pack signature envelope changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class PackAuthenticityVerificationV1:
    """One detached authenticity outcome that grants no structural authority."""

    status: PackAuthenticityStatusV1
    provider_id: str | None
    algorithm_id: str | None
    key_id: str | None
    envelope_sha256: str | None
    detail: str

    def __post_init__(self) -> None:
        if type(self.status) is not PackAuthenticityStatusV1:
            raise TypeError("pack authenticity status is invalid")
        optional_identifiers = (
            (self.provider_id, "authenticity provider ID"),
            (self.algorithm_id, "authenticity algorithm ID"),
            (self.key_id, "authenticity key ID"),
        )
        for value, label in optional_identifiers:
            if value is not None:
                require_data_identifier(value, label)
        if self.envelope_sha256 is not None:
            require_sha256(self.envelope_sha256, "signature envelope digest")
        require_nfc_text(self.detail, "pack authenticity detail", maximum_bytes=1024)
        if not self.detail:
            raise ValueError("pack authenticity detail must not be empty")
        if self.status is PackAuthenticityStatusV1.UNSIGNED and any(
            item is not None
            for item in (
                self.provider_id,
                self.algorithm_id,
                self.key_id,
                self.envelope_sha256,
            )
        ):
            raise ValueError("unsigned authenticity cannot name a signature claim")
        claim_fields = (
            self.provider_id,
            self.algorithm_id,
            self.key_id,
            self.envelope_sha256,
        )
        if self.status in {
            PackAuthenticityStatusV1.VERIFIED,
            PackAuthenticityStatusV1.BINDING_MISMATCH,
            PackAuthenticityStatusV1.PROVIDER_UNAVAILABLE,
            PackAuthenticityStatusV1.PROVIDER_ERROR,
        } and any(item is None for item in claim_fields):
            raise ValueError("signature claim status requires complete provider identity")
        if self.status is PackAuthenticityStatusV1.INVALID and not (
            all(item is None for item in claim_fields[:3])
            or all(item is not None for item in claim_fields)
        ):
            raise ValueError("invalid signature status has a partial provider identity")

    @property
    def authenticated(self) -> bool:
        return self.status is PackAuthenticityStatusV1.VERIFIED

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm_id": self.algorithm_id,
            "authenticated": self.authenticated,
            "detail": self.detail,
            "envelope_sha256": self.envelope_sha256,
            "key_id": self.key_id,
            "provider_id": self.provider_id,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class PackQualificationReportV1:
    """Separate successful qualification dimensions for one verified archive."""

    pack_id: str
    transport_sha256: str
    structural_safety: PackQualificationStatusV1
    digest_integrity: PackQualificationStatusV1
    signer_authenticity: PackAuthenticityVerificationV1
    compatibility: PackQualificationStatusV1
    capability: PackQualificationStatusV1
    provenance: PackQualificationStatusV1
    privacy: PackQualificationStatusV1
    scientific_status: PackQualificationStatusV1

    def __post_init__(self) -> None:
        require_sha256(self.pack_id, "qualified pack ID")
        require_sha256(self.transport_sha256, "qualified transport digest")
        for name in (
            "structural_safety",
            "digest_integrity",
            "compatibility",
            "capability",
            "provenance",
            "privacy",
            "scientific_status",
        ):
            if type(getattr(self, name)) is not PackQualificationStatusV1:
                raise TypeError(f"pack qualification {name} status is invalid")
        if type(self.signer_authenticity) is not PackAuthenticityVerificationV1:
            raise TypeError("pack qualification authenticity result is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "compatibility": self.compatibility.value,
            "digest_integrity": self.digest_integrity.value,
            "pack_id": self.pack_id,
            "privacy": self.privacy.value,
            "provenance": self.provenance.value,
            "scientific_status": self.scientific_status.value,
            "signer_authenticity": self.signer_authenticity.as_dict(),
            "structural_safety": self.structural_safety.value,
            "transport_sha256": self.transport_sha256,
        }


def verify_pack_signature(
    preflight: PackArchivePreflightV1,
    signature_bytes: bytes | None,
    providers: Mapping[str, PackSignatureProviderV1],
) -> PackAuthenticityVerificationV1:
    """Evaluate an optional detached claim after exact archive preflight.

    Signature verification can produce an authenticity result only.  It cannot
    change or suppress any pack validation result.
    """

    if type(preflight) is not PackArchivePreflightV1:
        raise TypeError("pack authenticity verification requires archive preflight")
    if not isinstance(providers, Mapping) or any(
        type(key) is not str for key in providers
    ):
        raise TypeError("pack signature providers must be an explicit text-key mapping")
    if signature_bytes is None:
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.UNSIGNED,
            provider_id=None,
            algorithm_id=None,
            key_id=None,
            envelope_sha256=None,
            detail="No detached signature was supplied; structural validation is unchanged.",
        )
    if type(signature_bytes) is not bytes:
        raise TypeError("detached pack signature must be exact bytes or absent")
    try:
        envelope = PackSignatureEnvelopeV1.from_canonical_bytes(signature_bytes)
    except (TypeError, ValueError):
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.INVALID,
            provider_id=None,
            algorithm_id=None,
            key_id=None,
            envelope_sha256=hashlib.sha256(signature_bytes).hexdigest(),
            detail="Detached signature envelope is not valid canonical V1 data.",
        )
    common = {
        "provider_id": envelope.provider_id,
        "algorithm_id": envelope.algorithm_id,
        "key_id": envelope.key_id,
        "envelope_sha256": envelope.sha256,
    }
    if (
        envelope.pack_id != preflight.pack_id
        or envelope.manifest_sha256 != preflight.manifest_sha256
        or envelope.transport_sha256 != preflight.transport_sha256
    ):
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.BINDING_MISMATCH,
            detail="Detached signature names a different pack, manifest, or transport.",
            **common,
        )
    provider = providers.get(envelope.provider_id)
    if provider is None:
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.PROVIDER_UNAVAILABLE,
            detail="No explicitly supplied local provider can verify this signature.",
            **common,
        )
    if (
        getattr(provider, "provider_id", None) != envelope.provider_id
        or getattr(provider, "algorithm_id", None) != envelope.algorithm_id
    ):
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.PROVIDER_ERROR,
            detail="Supplied provider identity or algorithm differs from the envelope.",
            **common,
        )
    try:
        accepted = provider.verify(
            key_id=envelope.key_id,
            message=envelope.message_bytes(),
            signature=envelope.signature,
        )
    except Exception:
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.PROVIDER_ERROR,
            detail="Signature provider failed without changing pack safety.",
            **common,
        )
    if type(accepted) is not bool:
        return PackAuthenticityVerificationV1(
            status=PackAuthenticityStatusV1.PROVIDER_ERROR,
            detail="Signature provider returned a non-boolean result.",
            **common,
        )
    return PackAuthenticityVerificationV1(
        status=(
            PackAuthenticityStatusV1.VERIFIED
            if accepted
            else PackAuthenticityStatusV1.INVALID
        ),
        detail=(
            "Detached signature was verified by the explicitly supplied provider."
            if accepted
            else "Detached signature was rejected by the explicitly supplied provider."
        ),
        **common,
    )


def create_pack_signature(
    preflight: PackArchivePreflightV1,
    provider: PackSignatureProviderV1,
    *,
    key_id: str,
) -> PackSignatureEnvelopeV1:
    """Ask one explicit established provider to sign the canonical V1 message."""

    if type(preflight) is not PackArchivePreflightV1:
        raise TypeError("pack signing requires archive preflight")
    provider_id = getattr(provider, "provider_id", None)
    algorithm_id = getattr(provider, "algorithm_id", None)
    require_data_identifier(provider_id, "pack signing provider ID")
    require_data_identifier(algorithm_id, "pack signing algorithm ID")
    require_data_identifier(key_id, "pack signing key ID")
    message = canonical_json_bytes(
        _signature_message_dict(
            provider_id=provider_id,
            algorithm_id=algorithm_id,
            key_id=key_id,
            pack_id=preflight.pack_id,
            manifest_sha256=preflight.manifest_sha256,
            transport_sha256=preflight.transport_sha256,
        )
    )
    signature = provider.sign(key_id=key_id, message=message)
    envelope = PackSignatureEnvelopeV1(
        provider_id=provider_id,
        algorithm_id=algorithm_id,
        key_id=key_id,
        pack_id=preflight.pack_id,
        manifest_sha256=preflight.manifest_sha256,
        transport_sha256=preflight.transport_sha256,
        signature=signature,
    )
    verified = provider.verify(
        key_id=key_id,
        message=envelope.message_bytes(),
        signature=envelope.signature,
    )
    if type(verified) is not bool or not verified:
        raise ValueError("pack signature provider did not verify its emitted signature")
    return envelope


def qualification_report_for_verified_pack(
    verification: object,
    authenticity: PackAuthenticityVerificationV1,
) -> PackQualificationReportV1:
    """Describe successful non-scientific gates without conflating authenticity."""

    from .builders import DomainPackVerificationV1

    if type(verification) is not DomainPackVerificationV1:
        raise TypeError("pack qualification requires DomainPackVerificationV1")
    if type(authenticity) is not PackAuthenticityVerificationV1:
        raise TypeError("pack qualification requires exact authenticity status")
    return PackQualificationReportV1(
        pack_id=verification.pack_id,
        transport_sha256=verification.preflight.transport_sha256,
        structural_safety=PackQualificationStatusV1.PASS,
        digest_integrity=PackQualificationStatusV1.PASS,
        signer_authenticity=authenticity,
        compatibility=PackQualificationStatusV1.PASS,
        capability=PackQualificationStatusV1.PASS,
        provenance=PackQualificationStatusV1.PASS,
        privacy=(
            PackQualificationStatusV1.PASS
            if verification.manifest.pack_type is PackTypeV1.RESEARCH
            else PackQualificationStatusV1.NOT_APPLICABLE
        ),
        scientific_status=PackQualificationStatusV1.NOT_ASSESSED,
    )


def read_pack_signature_bytes(source: Path) -> bytes:
    """Read one bounded, stable, no-follow detached signature file."""

    if not isinstance(source, Path):
        raise TypeError("pack signature source must be a pathlib.Path")
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("pack signature capture requires no-follow file opens")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_PACK_SIGNATURE_FILE_BYTES_V1
        ):
            raise ValueError("pack signature must be one bounded non-linked regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PACK_SIGNATURE_FILE_BYTES_V1:
                raise ValueError("pack signature exceeds the V1 byte bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or total != after.st_size
        ):
            raise ValueError("pack signature changed during capture")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _text(value: dict[str, object], key: str) -> str:
    item = value[key]
    if type(item) is not str:
        raise TypeError(f"{key} must be text")
    return item


def _signature_message_dict(
    *,
    provider_id: str,
    algorithm_id: str,
    key_id: str,
    pack_id: str,
    manifest_sha256: str,
    transport_sha256: str,
) -> dict[str, object]:
    require_data_identifier(provider_id, "pack signature provider ID")
    require_data_identifier(algorithm_id, "pack signature algorithm ID")
    require_data_identifier(key_id, "pack signature key ID")
    require_sha256(pack_id, "signed pack ID")
    require_sha256(manifest_sha256, "signed pack manifest digest")
    require_sha256(transport_sha256, "signed pack transport digest")
    return {
        "algorithm_id": algorithm_id,
        "key_id": key_id,
        "manifest_sha256": manifest_sha256,
        "pack_id": pack_id,
        "provider_id": provider_id,
        "schema_id": PACK_SIGNATURE_MESSAGE_SCHEMA_ID_V1,
        "schema_version": PACK_SIGNATURE_SCHEMA_VERSION_V1,
        "signature_scope": PACK_SIGNATURE_SCOPE_V1,
        "transport_sha256": transport_sha256,
    }


__all__ = [
    "MAX_PACK_SIGNATURE_BYTES_V1",
    "MAX_PACK_SIGNATURE_FILE_BYTES_V1",
    "PACK_SIGNATURE_ENCODING_V1",
    "PACK_SIGNATURE_ENVELOPE_SCHEMA_ID_V1",
    "PACK_SIGNATURE_MESSAGE_SCHEMA_ID_V1",
    "PACK_SIGNATURE_SCHEMA_VERSION_V1",
    "PACK_SIGNATURE_SCOPE_V1",
    "PackAuthenticityStatusV1",
    "PackAuthenticityVerificationV1",
    "PackQualificationReportV1",
    "PackQualificationStatusV1",
    "PackSignatureEnvelopeV1",
    "PackSignatureProviderV1",
    "create_pack_signature",
    "qualification_report_for_verified_pack",
    "read_pack_signature_bytes",
    "verify_pack_signature",
]
