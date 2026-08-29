"""Immutable semantic lineage records for future constrained strategy mutation."""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json

from .ast import (
    StateMachineStrategyAstV1,
    StrategyAstV1,
    TrafficLightStrategyAstV1,
)
from .identity import (
    canonical_identity_bytes,
    lineage_payload_sha256,
    strategy_semantic_sha256,
)


STRATEGY_LINEAGE_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_LINEAGE_NODE_V1"
STRATEGY_LINEAGE_SCHEMA_VERSION_V1 = 1
STRATEGY_RNG_SUBSTREAM_POLICY_V1 = "KIRBY2_STRATEGY_RNG_SUBSTREAM_V1"
STRATEGY_RNG_SUBSTREAM_DOMAIN_V1 = b"KIRBY2_STRATEGY_RNG_SUBSTREAM_V1\x00"
_OPERATION_ID = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INVALID_JSON_POINTER_ESCAPE = re.compile(r"~(?![01])")


class SemanticChangeKindV1(str, Enum):
    ADD = "ADD"
    REMOVE = "REMOVE"
    REPLACE = "REPLACE"


@dataclass(frozen=True, slots=True)
class SemanticDiffEntryV1:
    path: str
    change: SemanticChangeKindV1
    before: object = None
    after: object = None

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path.startswith("/"):
            raise ValueError("semantic diff path must be an absolute JSON pointer")
        canonical_identity_bytes(self.path)
        if _INVALID_JSON_POINTER_ESCAPE.search(self.path):
            raise ValueError("semantic diff path contains an invalid JSON pointer escape")
        if not isinstance(self.change, SemanticChangeKindV1):
            raise TypeError("semantic diff change kind is invalid")
        canonical_identity_bytes(self.before)
        canonical_identity_bytes(self.after)
        object.__setattr__(self, "before", freeze_json(self.before))
        object.__setattr__(self, "after", freeze_json(self.after))

    def as_dict(self) -> dict[str, object]:
        return {
            "after": thaw_json(self.after),
            "before": thaw_json(self.before),
            "change": self.change.value,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class StrategyRngSubstreamV1:
    root_seed: int
    label: str

    def __post_init__(self) -> None:
        if (
            type(self.root_seed) is not int
            or self.root_seed < 0
            or self.root_seed > (1 << 64) - 1
        ):
            raise ValueError("strategy RNG root seed must be an unsigned 64-bit integer")
        if type(self.label) is not str or not self.label:
            raise ValueError("strategy RNG substream label must be nonempty text")
        canonical_identity_bytes(self.label)

    @property
    def sha256(self) -> str:
        label_bytes = self.label.encode("utf-8")
        digest = hashlib.sha256()
        digest.update(STRATEGY_RNG_SUBSTREAM_DOMAIN_V1)
        digest.update(struct.pack(">Q", self.root_seed))
        digest.update(struct.pack(">I", len(label_bytes)))
        digest.update(label_bytes)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "policy": STRATEGY_RNG_SUBSTREAM_POLICY_V1,
            "root_seed": self.root_seed,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class StrategyLineageNodeV1:
    parent_semantic_sha256: str
    operation_id: str
    operation_version: int
    parameters: Mapping[str, object]
    rng_substream: StrategyRngSubstreamV1
    child_semantic_sha256: str
    valid: bool
    semantic_diff: tuple[SemanticDiffEntryV1, ...]

    def __post_init__(self) -> None:
        if (
            type(self.parent_semantic_sha256) is not str
            or _SHA256.fullmatch(self.parent_semantic_sha256) is None
            or type(self.child_semantic_sha256) is not str
            or _SHA256.fullmatch(self.child_semantic_sha256) is None
        ):
            raise ValueError("strategy lineage parent and child digests must be SHA-256")
        if type(self.operation_id) is not str or _OPERATION_ID.fullmatch(
            self.operation_id
        ) is None:
            raise ValueError("strategy lineage operation ID is invalid")
        if type(self.operation_version) is not int or self.operation_version <= 0:
            raise ValueError("strategy lineage operation version must be positive")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("strategy lineage parameters must be a mapping")
        detached_parameters = dict(self.parameters)
        canonical_identity_bytes(detached_parameters)
        object.__setattr__(self, "parameters", freeze_json(detached_parameters))
        if not isinstance(self.rng_substream, StrategyRngSubstreamV1):
            raise TypeError("strategy lineage RNG substream is invalid")
        if type(self.valid) is not bool:
            raise TypeError("strategy lineage validity must be boolean")
        if type(self.semantic_diff) is not tuple or any(
            not isinstance(entry, SemanticDiffEntryV1)
            for entry in self.semantic_diff
        ):
            raise TypeError("strategy semantic diff must be a tuple of diff entries")
        ordered_diff = tuple(
            sorted(
                self.semantic_diff,
                key=lambda entry: (
                    entry.path,
                    entry.change.value,
                    canonical_identity_bytes(entry.as_dict()),
                ),
            )
        )
        diff_keys = tuple(
            canonical_identity_bytes(entry.as_dict()) for entry in ordered_diff
        )
        if len(diff_keys) != len(set(diff_keys)):
            raise ValueError("strategy semantic diff entries must be unique")
        object.__setattr__(self, "semantic_diff", ordered_diff)
        same_digest = self.parent_semantic_sha256 == self.child_semantic_sha256
        if same_digest != (not ordered_diff):
            raise ValueError("strategy semantic diff disagrees with parent/child identity")
        if self.valid and same_digest:
            raise ValueError("a valid strategy mutation must change semantic identity")

    @property
    def lineage_sha256(self) -> str:
        return lineage_payload_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "child_semantic_sha256": self.child_semantic_sha256,
            "lineage_schema_id": STRATEGY_LINEAGE_SCHEMA_ID_V1,
            "lineage_schema_version": STRATEGY_LINEAGE_SCHEMA_VERSION_V1,
            "operation_id": self.operation_id,
            "operation_version": self.operation_version,
            "parameters": thaw_json(self.parameters),
            "parent_semantic_sha256": self.parent_semantic_sha256,
            "rng_substream": self.rng_substream.as_dict(),
            "semantic_diff": [entry.as_dict() for entry in self.semantic_diff],
            "valid": self.valid,
        }


def build_strategy_lineage_node(
    parent: StrategyAstV1,
    child: StrategyAstV1,
    *,
    operation_id: str,
    operation_version: int,
    parameters: Mapping[str, object],
    rng_substream: StrategyRngSubstreamV1,
    valid: bool,
) -> StrategyLineageNodeV1:
    """Bind an already parsed parent/child pair; raw source is never accepted here."""

    _require_ast(parent, "parent")
    _require_ast(child, "child")
    return StrategyLineageNodeV1(
        parent_semantic_sha256=strategy_semantic_sha256(parent),
        operation_id=operation_id,
        operation_version=operation_version,
        parameters=parameters,
        rng_substream=rng_substream,
        child_semantic_sha256=strategy_semantic_sha256(child),
        valid=valid,
        semantic_diff=semantic_strategy_diff(parent, child),
    )


def semantic_strategy_diff(
    parent: StrategyAstV1,
    child: StrategyAstV1,
) -> tuple[SemanticDiffEntryV1, ...]:
    _require_ast(parent, "parent")
    _require_ast(child, "child")
    rows: list[SemanticDiffEntryV1] = []
    _diff_value(
        parent.semantic_projection(),
        child.semantic_projection(),
        "",
        rows,
    )
    return tuple(rows)


def _require_ast(value: object, context: str) -> None:
    if not isinstance(
        value,
        (TrafficLightStrategyAstV1, StateMachineStrategyAstV1),
    ):
        raise TypeError(
            f"strategy lineage {context} must be parsed as a canonical AST first"
        )


def _diff_value(
    before: object,
    after: object,
    path: str,
    rows: list[SemanticDiffEntryV1],
) -> None:
    if before == after:
        return
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys):
            rows.append(
                SemanticDiffEntryV1(
                    _child_path(path, key),
                    SemanticChangeKindV1.REMOVE,
                    before[key],
                    None,
                )
            )
        for key in sorted(after_keys - before_keys):
            rows.append(
                SemanticDiffEntryV1(
                    _child_path(path, key),
                    SemanticChangeKindV1.ADD,
                    None,
                    after[key],
                )
            )
        for key in sorted(before_keys & after_keys):
            _diff_value(before[key], after[key], _child_path(path, key), rows)
        return
    if type(before) in {list, tuple} and type(after) in {list, tuple}:
        shared = min(len(before), len(after))
        for index in range(shared):
            _diff_value(
                before[index],
                after[index],
                _child_path(path, str(index)),
                rows,
            )
        for index in range(shared, len(before)):
            rows.append(
                SemanticDiffEntryV1(
                    _child_path(path, str(index)),
                    SemanticChangeKindV1.REMOVE,
                    before[index],
                    None,
                )
            )
        for index in range(shared, len(after)):
            rows.append(
                SemanticDiffEntryV1(
                    _child_path(path, str(index)),
                    SemanticChangeKindV1.ADD,
                    None,
                    after[index],
                )
            )
        return
    rows.append(
        SemanticDiffEntryV1(
            path or "/",
            SemanticChangeKindV1.REPLACE,
            before,
            after,
        )
    )


def _child_path(parent: str, key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


__all__ = [
    "STRATEGY_LINEAGE_SCHEMA_ID_V1",
    "STRATEGY_LINEAGE_SCHEMA_VERSION_V1",
    "STRATEGY_RNG_SUBSTREAM_DOMAIN_V1",
    "STRATEGY_RNG_SUBSTREAM_POLICY_V1",
    "SemanticChangeKindV1",
    "SemanticDiffEntryV1",
    "StrategyLineageNodeV1",
    "StrategyRngSubstreamV1",
    "build_strategy_lineage_node",
    "semantic_strategy_diff",
]
