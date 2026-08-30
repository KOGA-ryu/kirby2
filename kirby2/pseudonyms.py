"""Strict role-scoped pseudonyms for evidence and research artifacts.

These identifiers are pseudonymous, not anonymous.  Callers must supply entropy
that was generated independently of names, email addresses, institutional IDs,
or other direct identity.  The role is included in the hash domain so the same
entropy cannot create the same instructor and learner identifier.
"""

from __future__ import annotations

import hashlib
import re


OPAQUE_PROFILE_ENTROPY_MIN_BYTES = 32
OPAQUE_PROFILE_ENTROPY_MAX_BYTES = 4096
OPAQUE_PROFILE_DERIVATION_ID = "KIRBY2_OPAQUE_PROFILE_V1"
PSEUDONYMOUS_PROFILE_ID_POLICY = "STRICT_ROLE_SCOPED_OPAQUE_SHA256_V1"

_PROFILE_DOMAIN = OPAQUE_PROFILE_DERIVATION_ID.encode("ascii") + b"\x00"
_INSTRUCTOR_PROFILE_ID = re.compile(r"instructor-profile-[0-9a-f]{64}")
_LEARNER_PROFILE_ID = re.compile(r"learner-profile-[0-9a-f]{64}")


def require_instructor_profile_id(value: object) -> str:
    """Return *value* only when it is one strict opaque instructor ID."""

    if type(value) is not str or _INSTRUCTOR_PROFILE_ID.fullmatch(value) is None:
        raise ValueError("instructor profile ID is invalid")
    return value


def require_learner_profile_id(value: object) -> str:
    """Return *value* only when it is one strict opaque learner ID."""

    if type(value) is not str or _LEARNER_PROFILE_ID.fullmatch(value) is None:
        raise ValueError("learner profile ID is invalid")
    return value


def require_profile_id(value: object) -> str:
    """Return *value* only when it is a strict role-scoped opaque profile ID."""

    if type(value) is not str:
        raise ValueError("profile ID is invalid")
    if (
        _INSTRUCTOR_PROFILE_ID.fullmatch(value) is None
        and _LEARNER_PROFILE_ID.fullmatch(value) is None
    ):
        raise ValueError("profile ID is invalid")
    return value


def _derive_profile_id(role: str, namespace: str, opaque_entropy: bytes) -> str:
    if type(opaque_entropy) is not bytes:
        raise TypeError("opaque profile entropy must be exact bytes")
    if not (
        OPAQUE_PROFILE_ENTROPY_MIN_BYTES
        <= len(opaque_entropy)
        <= OPAQUE_PROFILE_ENTROPY_MAX_BYTES
    ):
        raise ValueError("opaque profile entropy must contain 32 to 4096 bytes")
    digest = hashlib.sha256(
        _PROFILE_DOMAIN + role.encode("ascii") + b"\x00" + opaque_entropy
    ).hexdigest()
    return f"{namespace}-profile-{digest}"


def derive_instructor_profile_id(opaque_entropy: bytes) -> str:
    """Derive an opaque instructor ID from direct-identity-independent entropy."""

    return _derive_profile_id("INSTRUCTOR", "instructor", opaque_entropy)


def derive_learner_profile_id(opaque_entropy: bytes) -> str:
    """Derive an opaque learner ID from direct-identity-independent entropy."""

    return _derive_profile_id("LEARNER", "learner", opaque_entropy)


__all__ = [
    "OPAQUE_PROFILE_DERIVATION_ID",
    "OPAQUE_PROFILE_ENTROPY_MAX_BYTES",
    "OPAQUE_PROFILE_ENTROPY_MIN_BYTES",
    "PSEUDONYMOUS_PROFILE_ID_POLICY",
    "derive_instructor_profile_id",
    "derive_learner_profile_id",
    "require_instructor_profile_id",
    "require_learner_profile_id",
    "require_profile_id",
]
