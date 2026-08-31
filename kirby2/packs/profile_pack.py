"""Exact calibrated-market-profile adapter with preregistration/review status."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from kirby2.calibration.profiles import MarketProfile

from .formats import load_canonical_json_bytes
from .models import PackTypeV1
from .types import (
    DomainPackAdapterContractV1,
    DomainPackIndexV1,
    DomainPackRefusalCodeV1,
    DomainPackRefused,
    PackArtifactRoleV1,
    validate_adapter_inventory,
)


PROFILE_PACK_ADAPTER_ID_V1 = "KIRBY2_MARKET_PROFILE_PACK_ADAPTER_V1"


def _roles(*items: PackArtifactRoleV1) -> tuple[PackArtifactRoleV1, ...]:
    return tuple(sorted(items, key=lambda item: item.value))


PROFILE_PACK_ADAPTER_V1 = DomainPackAdapterContractV1(
    pack_type=PackTypeV1.MARKET_PROFILE,
    adapter_id=PROFILE_PACK_ADAPTER_ID_V1,
    adapter_version=1,
    compiler_component_id="KIRBY2_MARKET_PROFILE_PACK_COMPILER_V1",
    compiler_version="0.1.0",
    required_roles=_roles(
        PackArtifactRoleV1.MARKET_PROFILE,
        PackArtifactRoleV1.PROFILE_PREREGISTRATION,
        PackArtifactRoleV1.PROFILE_REVIEW_STATUS,
    ),
    allowed_roles=_roles(
        PackArtifactRoleV1.MARKET_PROFILE,
        PackArtifactRoleV1.PROFILE_PREREGISTRATION,
        PackArtifactRoleV1.PROFILE_REVIEW_STATUS,
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    multiple_roles=_roles(
        PackArtifactRoleV1.EMBEDDED_RUN,
        PackArtifactRoleV1.EMBEDDED_AUDIT,
    ),
    primary_roles=_roles(PackArtifactRoleV1.MARKET_PROFILE),
    supports_replay_equivalence=False,
)


def validate_profile_pack(
    index: DomainPackIndexV1,
    original_bytes: Mapping[str, bytes],
) -> None:
    """Parse the owning profile schema and require explicit governance status."""

    validate_adapter_inventory(
        PROFILE_PACK_ADAPTER_V1,
        index.pack_type,
        index.primary_artifact_id,
        index.artifacts,
    )
    profile_row = index.artifact(PackArtifactRoleV1.MARKET_PROFILE)
    profile_raw = _artifact_bytes(original_bytes, profile_row.artifact_id)
    try:
        payload = json.loads(profile_raw.decode("utf-8"))
        if type(payload) is not dict:
            raise TypeError("market profile must be an object")
        profile = MarketProfile.from_dict(payload)
    except (KeyError, UnicodeDecodeError, TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            "market profile failed the owning calibration parser",
        ) from error
    if profile.canonical_json().encode("utf-8") != profile_raw:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_FORMAT_INVALID,
            "market profile bytes are not canonical in the owning schema",
        )
    if profile_row.logical_identity_sha256 != hashlib.sha256(profile_raw).hexdigest():
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "market profile logical identity differs from canonical profile bytes",
        )

    preregistration_row = index.artifact(
        PackArtifactRoleV1.PROFILE_PREREGISTRATION
    )
    review_row = index.artifact(PackArtifactRoleV1.PROFILE_REVIEW_STATUS)
    preregistration_raw = _artifact_bytes(
        original_bytes,
        preregistration_row.artifact_id,
    )
    review_raw = _artifact_bytes(original_bytes, review_row.artifact_id)
    preregistration = _status_object(
        preregistration_raw,
        "profile preregistration",
    )
    review = _status_object(
        review_raw,
        "profile review status",
    )
    if not ({"preregistered", "status"} & set(preregistration)):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.PROFILE_STATUS_INVALID,
            "profile preregistration artifact has no explicit status",
        )
    if not ({"decision", "review_status", "status"} & set(review)):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.PROFILE_STATUS_INVALID,
            "profile review artifact has no explicit status",
        )
    if preregistration_row.logical_identity_sha256 != hashlib.sha256(
        preregistration_raw
    ).hexdigest() and preregistration_row.logical_identity_sha256 not in _all_text_values(
        preregistration
    ):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "profile preregistration logical identity is not bound to its record",
        )
    if review_row.logical_identity_sha256 != hashlib.sha256(
        review_raw
    ).hexdigest() and review_row.logical_identity_sha256 not in _all_text_values(review):
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_IDENTITY_MISMATCH,
            "profile review logical identity is not bound to its record",
        )


def _status_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = load_canonical_json_bytes(raw, label)
    except (TypeError, ValueError) as error:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.PROFILE_STATUS_INVALID,
            f"{label} must be canonical JSON",
        ) from error
    if type(value) is not dict or not value:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.PROFILE_STATUS_INVALID,
            f"{label} must be a nonempty object",
        )
    return value


def _artifact_bytes(values: Mapping[str, bytes], artifact_id: str) -> bytes:
    raw = values.get(artifact_id)
    if type(raw) is not bytes:
        raise DomainPackRefused(
            DomainPackRefusalCodeV1.ARTIFACT_INVENTORY_INVALID,
            f"profile artifact bytes are absent: {artifact_id}",
        )
    return raw


def _all_text_values(value: object) -> frozenset[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if type(item) is str:
            found.add(item)
        elif type(item) is dict:
            for child in item.values():
                visit(child)
        elif type(item) is list:
            for child in item:
                visit(child)

    visit(value)
    return frozenset(found)


__all__ = [
    "PROFILE_PACK_ADAPTER_ID_V1",
    "PROFILE_PACK_ADAPTER_V1",
    "validate_profile_pack",
]
