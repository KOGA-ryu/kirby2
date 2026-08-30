"""Deterministic, order-independent seed derivation for logical work cells.

This module owns scientific seed identity only.  Worker count, worker identity,
attempt number, lease state, input order, filesystem state, clocks, and ambient
randomness are deliberately absent from every derivation frame.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


ORCHESTRATION_SEED_SCHEMA_VERSION = 1
ORCHESTRATION_SEED_POLICY_VERSION_V1 = "KIRBY2_ORCHESTRATION_CELL_SEED_V1"
ORCHESTRATION_MASTER_SEED_IDENTITY_DOMAIN_V1 = (
    b"KIRBY2_ORCHESTRATION_MASTER_SEED_IDENTITY_V1\x00"
)
ORCHESTRATION_CELL_SEED_DOMAIN_V1 = b"KIRBY2_ORCHESTRATION_CELL_SEED_V1\x00"
ORCHESTRATION_SEED_DERIVATION_DOMAIN_V1 = (
    b"KIRBY2_ORCHESTRATION_SEED_DERIVATION_RECORD_V1\x00"
)
MAX_ORCHESTRATION_SEED = (1 << 63) - 1

_CANONICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SeedDerivationCollisionError(ValueError):
    """Two distinct stable logical cells truncated to the same V1 seed."""


@dataclass(frozen=True, slots=True)
class MasterSeedIdentityV1:
    """One versioned, canonical master seed identity.

    The raw seed is part of scientific identity.  Each derivation record binds this
    typed value so its claimed derived seed can be verified at construction time.
    """

    schema_version: int
    policy_version: str
    master_seed: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != ORCHESTRATION_SEED_SCHEMA_VERSION
        ):
            raise ValueError("master seed schema version must be exactly 1")
        if self.policy_version != ORCHESTRATION_SEED_POLICY_VERSION_V1:
            raise ValueError("master seed policy version is not supported")
        _require_seed(self.master_seed, "master seed")

    def as_dict(self) -> dict[str, object]:
        return {
            "master_seed": self.master_seed,
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def identity_sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(ORCHESTRATION_MASTER_SEED_IDENTITY_DOMAIN_V1)
        _update_named_frame(digest, b"master_seed_identity", self.canonical_bytes())
        return digest.hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> MasterSeedIdentityV1:
        payload = _exact_object(
            value,
            frozenset({"master_seed", "policy_version", "schema_version"}),
            "master seed identity",
        )
        restored = cls(
            schema_version=_exact_integer(payload, "schema_version"),
            policy_version=_exact_text(payload, "policy_version"),
            master_seed=_exact_integer(payload, "master_seed"),
        )
        _require_exact_round_trip(restored, payload, "master seed identity")
        return restored


@dataclass(frozen=True, slots=True)
class StableCellIdentityV1:
    """Stable partition/cell coordinates; never a scheduler position."""

    partition_id: str
    cell_id: str

    def __post_init__(self) -> None:
        _require_canonical_id(self.partition_id, "logical partition ID")
        _require_canonical_id(self.cell_id, "logical cell ID")

    @property
    def canonical_key(self) -> tuple[str, str]:
        return (self.partition_id, self.cell_id)

    def as_dict(self) -> dict[str, object]:
        return {"cell_id": self.cell_id, "partition_id": self.partition_id}

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> StableCellIdentityV1:
        payload = _exact_object(
            value,
            frozenset({"cell_id", "partition_id"}),
            "stable cell identity",
        )
        restored = cls(
            partition_id=_exact_text(payload, "partition_id"),
            cell_id=_exact_text(payload, "cell_id"),
        )
        _require_exact_round_trip(restored, payload, "stable cell identity")
        return restored


@dataclass(frozen=True, slots=True)
class SeedDerivationV1:
    """Immutable evidence for one logical cell's V1 seed derivation."""

    schema_version: int
    policy_version: str
    master_seed_identity: MasterSeedIdentityV1
    experiment_identity_sha256: str
    cell_identity: StableCellIdentityV1
    derived_seed: int

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != ORCHESTRATION_SEED_SCHEMA_VERSION
        ):
            raise ValueError("seed derivation schema version must be exactly 1")
        if self.policy_version != ORCHESTRATION_SEED_POLICY_VERSION_V1:
            raise ValueError("seed derivation policy version is not supported")
        if type(self.master_seed_identity) is not MasterSeedIdentityV1:
            raise TypeError("seed derivation requires MasterSeedIdentityV1")
        _require_sha256(
            self.experiment_identity_sha256,
            "experiment identity digest",
        )
        if type(self.cell_identity) is not StableCellIdentityV1:
            raise TypeError("seed derivation requires StableCellIdentityV1")
        _require_seed(self.derived_seed, "derived logical-cell seed")
        expected = _derive_seed_value(
            self.master_seed_identity,
            self.experiment_identity_sha256,
            self.cell_identity,
        )
        if self.derived_seed != expected:
            raise ValueError("derived logical-cell seed differs from canonical inputs")

    @property
    def master_seed_identity_sha256(self) -> str:
        return self.master_seed_identity.identity_sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "cell_identity": self.cell_identity.as_dict(),
            "derived_seed": self.derived_seed,
            "experiment_identity_sha256": self.experiment_identity_sha256,
            "master_seed_identity": self.master_seed_identity.as_dict(),
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_dict())

    @property
    def derivation_sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(ORCHESTRATION_SEED_DERIVATION_DOMAIN_V1)
        _update_named_frame(digest, b"seed_derivation", self.canonical_bytes())
        return digest.hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> SeedDerivationV1:
        payload = _exact_object(
            value,
            frozenset(
                {
                    "cell_identity",
                    "derived_seed",
                    "experiment_identity_sha256",
                    "master_seed_identity",
                    "policy_version",
                    "schema_version",
                }
            ),
            "seed derivation",
        )
        restored = cls(
            schema_version=_exact_integer(payload, "schema_version"),
            policy_version=_exact_text(payload, "policy_version"),
            master_seed_identity=MasterSeedIdentityV1.from_dict(
                payload["master_seed_identity"]
            ),
            experiment_identity_sha256=_exact_text(
                payload,
                "experiment_identity_sha256",
            ),
            cell_identity=StableCellIdentityV1.from_dict(payload["cell_identity"]),
            derived_seed=_exact_integer(payload, "derived_seed"),
        )
        _require_exact_round_trip(restored, payload, "seed derivation")
        return restored


def build_master_seed_identity(master_seed: int) -> MasterSeedIdentityV1:
    """Construct the one supported master-seed identity version."""

    return MasterSeedIdentityV1(
        schema_version=ORCHESTRATION_SEED_SCHEMA_VERSION,
        policy_version=ORCHESTRATION_SEED_POLICY_VERSION_V1,
        master_seed=master_seed,
    )


def derive_logical_cell_seed(
    master_seed_identity: MasterSeedIdentityV1,
    experiment_identity: str,
    cell_identity: StableCellIdentityV1,
) -> SeedDerivationV1:
    """Derive one stable signed-safe 63-bit seed from semantic identities only."""

    if type(master_seed_identity) is not MasterSeedIdentityV1:
        raise TypeError("seed derivation requires MasterSeedIdentityV1")
    experiment_sha256 = _require_sha256(
        experiment_identity,
        "experiment identity digest",
    )
    if type(cell_identity) is not StableCellIdentityV1:
        raise TypeError("seed derivation requires StableCellIdentityV1")

    derived_seed = _derive_seed_value(
        master_seed_identity,
        experiment_sha256,
        cell_identity,
    )
    return SeedDerivationV1(
        schema_version=ORCHESTRATION_SEED_SCHEMA_VERSION,
        policy_version=ORCHESTRATION_SEED_POLICY_VERSION_V1,
        master_seed_identity=master_seed_identity,
        experiment_identity_sha256=experiment_sha256,
        cell_identity=cell_identity,
        derived_seed=derived_seed,
    )


def _derive_seed_value(
    master_seed_identity: MasterSeedIdentityV1,
    experiment_identity_sha256: str,
    cell_identity: StableCellIdentityV1,
) -> int:
    digest = hashlib.sha256()
    digest.update(ORCHESTRATION_CELL_SEED_DOMAIN_V1)
    digest.update(struct.pack(">Q", 3))
    _update_named_frame(
        digest,
        b"master_seed_identity",
        master_seed_identity.canonical_bytes(),
    )
    _update_named_frame(
        digest,
        b"experiment_identity_sha256",
        bytes.fromhex(experiment_identity_sha256),
    )
    _update_named_frame(
        digest,
        b"stable_cell_identity",
        cell_identity.canonical_bytes(),
    )
    return int.from_bytes(digest.digest()[:8], "big") & MAX_ORCHESTRATION_SEED


def derive_logical_cell_seed_batch(
    master_seed_identity: MasterSeedIdentityV1,
    experiment_identity: str,
    cell_identities: Iterable[StableCellIdentityV1],
) -> tuple[SeedDerivationV1, ...]:
    """Derive a canonically ordered, collision-free complete cell seed batch.

    A collision is a closed failure.  V1 never probes, increments, salts, or otherwise
    changes a seed based on input order to escape a truncated-seed collision.
    """

    if type(master_seed_identity) is not MasterSeedIdentityV1:
        raise TypeError("seed batch derivation requires MasterSeedIdentityV1")
    experiment_sha256 = _require_sha256(
        experiment_identity,
        "experiment identity digest",
    )
    if isinstance(cell_identities, (str, bytes, bytearray, Mapping)):
        raise TypeError("seed batch cells must be an iterable of stable identities")
    try:
        supplied = tuple(cell_identities)
    except TypeError as error:
        raise TypeError(
            "seed batch cells must be an iterable of StableCellIdentityV1"
        ) from error
    if not supplied:
        raise ValueError("seed batch requires at least one stable cell identity")
    if any(type(cell) is not StableCellIdentityV1 for cell in supplied):
        raise TypeError("seed batch cells must use StableCellIdentityV1")

    ordered = tuple(sorted(supplied, key=lambda cell: cell.canonical_key))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.canonical_key == current.canonical_key:
            raise ValueError(
                "seed batch contains duplicate stable cell identity "
                f"{current.partition_id!r}/{current.cell_id!r}"
            )

    derivations = tuple(
        derive_logical_cell_seed(
            master_seed_identity,
            experiment_sha256,
            cell,
        )
        for cell in ordered
    )
    seed_owner: dict[int, StableCellIdentityV1] = {}
    for derivation in derivations:
        incumbent = seed_owner.setdefault(
            derivation.derived_seed,
            derivation.cell_identity,
        )
        if incumbent != derivation.cell_identity:
            raise SeedDerivationCollisionError(
                "distinct stable cells collide after V1 63-bit seed truncation: "
                f"{incumbent.partition_id!r}/{incumbent.cell_id!r} and "
                f"{derivation.cell_identity.partition_id!r}/"
                f"{derivation.cell_identity.cell_id!r}"
            )
    return derivations


def _update_named_frame(digest: Any, name: bytes, value: bytes) -> None:
    if type(name) is not bytes or not name:
        raise TypeError("derivation frame name must be nonempty bytes")
    if type(value) is not bytes:
        raise TypeError("derivation frame payload must be bytes")
    digest.update(struct.pack(">Q", len(name)))
    digest.update(name)
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _require_seed(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ORCHESTRATION_SEED:
        raise ValueError(f"{label} must lie in [0, 2**63-1]")
    return value


def _require_canonical_id(value: object, label: str) -> str:
    if type(value) is not str or _CANONICAL_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be one bounded canonical identifier")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _exact_object(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"serialized {label} must be an exact object")
    if any(type(key) is not str for key in value):
        raise TypeError(f"serialized {label} field names must be exact text")
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"serialized {label} fields differ: "
            f"missing={sorted(expected - actual)} unknown={sorted(actual - expected)}"
        )
    return value


def _exact_text(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise TypeError(f"serialized {key} must be exact text")
    return value


def _exact_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"serialized {key} must be an exact integer")
    return value


def _require_exact_round_trip(
    record: MasterSeedIdentityV1 | StableCellIdentityV1 | SeedDerivationV1,
    payload: Mapping[str, object],
    label: str,
) -> None:
    if record.as_dict() != payload:
        raise ValueError(f"serialized {label} did not round-trip exactly")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


__all__ = [
    "MAX_ORCHESTRATION_SEED",
    "ORCHESTRATION_CELL_SEED_DOMAIN_V1",
    "ORCHESTRATION_MASTER_SEED_IDENTITY_DOMAIN_V1",
    "ORCHESTRATION_SEED_DERIVATION_DOMAIN_V1",
    "ORCHESTRATION_SEED_POLICY_VERSION_V1",
    "ORCHESTRATION_SEED_SCHEMA_VERSION",
    "MasterSeedIdentityV1",
    "SeedDerivationCollisionError",
    "SeedDerivationV1",
    "StableCellIdentityV1",
    "build_master_seed_identity",
    "derive_logical_cell_seed",
    "derive_logical_cell_seed_batch",
]
