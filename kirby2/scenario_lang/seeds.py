"""Versioned deterministic seed selection and substream derivation."""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata

from .models import (
    SCENARIO_SEED_DERIVATION_POLICY_VERSION,
    SCENARIO_SEED_POLICY_SCHEMA_VERSION,
    ScenarioCompiledSeedPolicyV1,
    ScenarioRecordV1,
    ScenarioSubstreamSeedV1,
    ScenarioValueKindV1,
)


SCENARIO_SUBSTREAM_SEED_DOMAIN_V1 = b"KIRBY2_SCENARIO_SUBSTREAM_SEED_V1\x00"
SCENARIO_RUN_IDENTITY_DOMAIN_V1 = b"KIRBY2_SCENARIO_RUN_IDENTITY_V1\x00"
_PATH_SEGMENT = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def derive_scenario_substream_seed(
    root_seed: int,
    policy_version: int,
    semantic_path: str,
) -> int:
    """Derive a stable signed-safe 63-bit seed from one semantic path."""

    _validate_seed(root_seed, "scenario root seed")
    if (
        type(policy_version) is not int
        or policy_version != SCENARIO_SEED_DERIVATION_POLICY_VERSION
    ):
        raise ValueError("scenario seed derivation policy version must be exactly 1")
    path = _validate_substream_path(semantic_path)
    encoded = path.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(SCENARIO_SUBSTREAM_SEED_DOMAIN_V1)
    digest.update(struct.pack(">Q", root_seed))
    digest.update(struct.pack(">Q", policy_version))
    digest.update(struct.pack(">Q", len(encoded)))
    digest.update(encoded)
    return int.from_bytes(digest.digest()[:8], "big") & ((1 << 63) - 1)


def build_compiled_seed_policy(
    record: ScenarioRecordV1,
    *,
    cli_seed_override: int | None = None,
) -> ScenarioCompiledSeedPolicyV1:
    if type(record) is not ScenarioRecordV1:
        raise TypeError("compiled seed policy requires a typed scenario record")
    fields = {field.name: field for field in record.fields}
    expected = {
        "allow_cli_override": ScenarioValueKindV1.FLAG,
        "policy_version": ScenarioValueKindV1.VERSION,
        "root_seed": ScenarioValueKindV1.SEED,
        "substreams": ScenarioValueKindV1.IDENTIFIERS,
    }
    if set(fields) != set(expected):
        raise ValueError("materialized scenario seed policy fields are incomplete")
    for name, value_kind in expected.items():
        if fields[name].value_kind is not value_kind:
            raise ValueError(f"materialized seed field {name!r} uses the wrong tag")
    source_root_seed = fields["root_seed"].value
    policy_version = fields["policy_version"].value
    override_allowed = fields["allow_cli_override"].value
    raw_paths = fields["substreams"].value
    if type(source_root_seed) is not int:
        raise TypeError("materialized scenario root seed must be an integer")
    if type(policy_version) is not int:
        raise TypeError("materialized scenario policy version must be an integer")
    if type(override_allowed) is not bool:
        raise TypeError("materialized scenario override permission must be a bool")
    if type(raw_paths) is not tuple or not raw_paths:
        raise ValueError("materialized scenario seed policy requires substreams")
    _validate_seed(source_root_seed, "scenario source root seed")
    if policy_version != SCENARIO_SEED_DERIVATION_POLICY_VERSION:
        raise ValueError("scenario seed derivation policy version must be exactly 1")
    override_applied = cli_seed_override is not None
    if override_applied:
        if not override_allowed:
            raise ValueError("scenario source does not permit a CLI seed override")
        _validate_seed(cli_seed_override, "scenario CLI seed override")
        selected_root_seed = cli_seed_override
    else:
        selected_root_seed = source_root_seed
    paths = tuple(sorted(_validate_substream_path(path) for path in raw_paths))
    if len(paths) != len(set(paths)):
        raise ValueError("scenario seed substream paths must be unique")
    result = ScenarioCompiledSeedPolicyV1(
        schema_version=SCENARIO_SEED_POLICY_SCHEMA_VERSION,
        policy_version=policy_version,
        source_root_seed=source_root_seed,
        selected_root_seed=selected_root_seed,
        cli_override_allowed=override_allowed,
        cli_override_applied=override_applied,
        substreams=tuple(
            ScenarioSubstreamSeedV1(
                path,
                derive_scenario_substream_seed(
                    selected_root_seed,
                    policy_version,
                    path,
                ),
            )
            for path in paths
        ),
    )
    validate_compiled_seed_policy(result)
    return result


def validate_compiled_seed_policy(policy: ScenarioCompiledSeedPolicyV1) -> None:
    if type(policy) is not ScenarioCompiledSeedPolicyV1:
        raise TypeError("scenario seed validation requires the compiled V1 policy")
    for substream in policy.substreams:
        expected = derive_scenario_substream_seed(
            policy.selected_root_seed,
            policy.policy_version,
            substream.semantic_path,
        )
        if substream.derived_seed != expected:
            raise ValueError("compiled scenario substream seed does not match derivation")


def scenario_run_identity_digest(
    native_plan_digest: str,
    policy: ScenarioCompiledSeedPolicyV1,
) -> str:
    """Bind selected seed and policy version into deterministic run identity."""

    if type(native_plan_digest) is not str or _SHA256.fullmatch(native_plan_digest) is None:
        raise ValueError("scenario run identity requires a native plan digest")
    validate_compiled_seed_policy(policy)
    from .identity import canonical_semantic_plan_bytes

    payload = canonical_semantic_plan_bytes(
        {
            "native_plan_digest": native_plan_digest,
            "seed_policy": policy.as_dict(),
        }
    )
    digest = hashlib.sha256()
    digest.update(SCENARIO_RUN_IDENTITY_DOMAIN_V1)
    digest.update(struct.pack(">Q", len(payload)))
    digest.update(payload)
    return digest.hexdigest()


def _validate_seed(value: object, context: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"{context} must lie in [0, 2**63-1]")
    return value


def _validate_substream_path(value: object) -> str:
    if type(value) is not str or unicodedata.normalize("NFC", value) != value:
        raise ValueError("scenario substream path must be NFC text")
    parts = value.split("/")
    if len(parts) < 3 or parts[0] != "scenario":
        raise ValueError("scenario substream path must begin with 'scenario/'")
    if any(_PATH_SEGMENT.fullmatch(part) is None for part in parts):
        raise ValueError("scenario substream path contains a noncanonical segment")
    return value


__all__ = [
    "SCENARIO_RUN_IDENTITY_DOMAIN_V1",
    "SCENARIO_SUBSTREAM_SEED_DOMAIN_V1",
    "build_compiled_seed_policy",
    "derive_scenario_substream_seed",
    "scenario_run_identity_digest",
    "validate_compiled_seed_policy",
]
