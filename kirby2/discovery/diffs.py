"""Exact complexity and semantic-diff accounting for strategy mutations."""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

from .ast import (
    StateMachineStrategyAstV1,
    StrategyAstV1,
    TrafficLightStrategyAstV1,
)
from .identity import canonical_identity_bytes
from .lineage import SemanticDiffEntryV1, semantic_strategy_diff


STRATEGY_COMPLEXITY_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_COMPLEXITY_V1"
STRATEGY_MUTATION_DIFF_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_MUTATION_DIFF_V1"
STRATEGY_MUTATION_DIFF_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_MUTATION_DIFF_V1\x00"
_JSON_POINTER = re.compile(r"^/(?:[^~/]|~[01])*(?:/(?:[^~/]|~[01])*)*$")


@dataclass(frozen=True, slots=True)
class StrategyComplexityV1:
    """The six preregistered strategy-complexity dimensions."""

    conditions: int
    features: int
    states: int
    transitions: int
    rolling_windows: int
    parameters: int

    def __post_init__(self) -> None:
        for name, value in self._values().items():
            if type(value) is not int or value < 0:
                raise ValueError(f"strategy complexity {name} must be nonnegative")

    @property
    def total(self) -> int:
        return sum(self._values().values())

    def _values(self) -> dict[str, int]:
        return {
            "conditions": self.conditions,
            "features": self.features,
            "parameters": self.parameters,
            "rolling_windows": self.rolling_windows,
            "states": self.states,
            "transitions": self.transitions,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self._values(),
            "schema_id": STRATEGY_COMPLEXITY_SCHEMA_ID_V1,
            "schema_version": 1,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class StrategyComplexityDeltaV1:
    conditions: int
    features: int
    states: int
    transitions: int
    rolling_windows: int
    parameters: int

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in self._values().values()):
            raise TypeError("strategy complexity deltas must be integers")

    @classmethod
    def between(
        cls,
        before: StrategyComplexityV1,
        after: StrategyComplexityV1,
    ) -> StrategyComplexityDeltaV1:
        if not isinstance(before, StrategyComplexityV1) or not isinstance(
            after,
            StrategyComplexityV1,
        ):
            raise TypeError("complexity delta requires typed endpoints")
        return cls(
            conditions=after.conditions - before.conditions,
            features=after.features - before.features,
            states=after.states - before.states,
            transitions=after.transitions - before.transitions,
            rolling_windows=after.rolling_windows - before.rolling_windows,
            parameters=after.parameters - before.parameters,
        )

    @property
    def total(self) -> int:
        return sum(self._values().values())

    def _values(self) -> dict[str, int]:
        return {
            "conditions": self.conditions,
            "features": self.features,
            "parameters": self.parameters,
            "rolling_windows": self.rolling_windows,
            "states": self.states,
            "transitions": self.transitions,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._values(), "total": self.total}


@dataclass(frozen=True, slots=True)
class StrategyMutationDiffV1:
    affected_rule_path: str
    human_description: str
    inverse_description: str
    semantic_diff: tuple[SemanticDiffEntryV1, ...]

    def __post_init__(self) -> None:
        _require_json_pointer(self.affected_rule_path)
        for value, context in (
            (self.human_description, "mutation diff description"),
            (self.inverse_description, "mutation inverse description"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{context} must be nonempty text")
            canonical_identity_bytes(value)
        if type(self.semantic_diff) is not tuple or any(
            not isinstance(item, SemanticDiffEntryV1) for item in self.semantic_diff
        ):
            raise TypeError("mutation semantic diff must be a typed tuple")
        ordered = tuple(
            sorted(
                self.semantic_diff,
                key=lambda item: (
                    item.path,
                    item.change.value,
                    canonical_identity_bytes(item.as_dict()),
                ),
            )
        )
        if len(ordered) != len(
            {canonical_identity_bytes(item.as_dict()) for item in ordered}
        ):
            raise ValueError("mutation semantic diff entries must be unique")
        object.__setattr__(self, "semantic_diff", ordered)

    @property
    def diff_sha256(self) -> str:
        raw = self.canonical_bytes()
        digest = hashlib.sha256()
        digest.update(STRATEGY_MUTATION_DIFF_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "affected_rule_path": self.affected_rule_path,
            "human_description": self.human_description,
            "inverse_description": self.inverse_description,
            "schema_id": STRATEGY_MUTATION_DIFF_SCHEMA_ID_V1,
            "schema_version": 1,
            "semantic_diff": [item.as_dict() for item in self.semantic_diff],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())


def strategy_complexity(ast: StrategyAstV1) -> StrategyComplexityV1:
    if isinstance(ast, TrafficLightStrategyAstV1):
        conditions = ast.green_conditions + ast.wait_conditions
        return StrategyComplexityV1(
            conditions=len(conditions),
            features=len({item.feature for item in conditions}),
            states=0,
            transitions=0,
            rolling_windows=1,
            parameters=1 + len(conditions),
        )
    if isinstance(ast, StateMachineStrategyAstV1):
        conditions = tuple(
            condition
            for transition in ast.transitions
            for condition in transition.conditions
        )
        timed_parameters = sum(
            transition.duration_us > 0 for transition in ast.transitions
        )
        counted_parameters = sum(
            transition.event_count > 0 for transition in ast.transitions
        )
        cooldown_parameters = sum(state.cooldown_us > 0 for state in ast.states)
        return StrategyComplexityV1(
            conditions=len(conditions),
            features=len({item.feature for item in conditions}),
            states=len(ast.states),
            transitions=len(ast.transitions),
            rolling_windows=1,
            parameters=(
                1
                + len(conditions)
                + timed_parameters
                + counted_parameters
                + cooldown_parameters
            ),
        )
    raise TypeError("strategy complexity requires a canonical strategy AST")


def build_mutation_diff(
    parent: StrategyAstV1,
    child: StrategyAstV1,
    *,
    affected_rule_path: str,
    human_description: str,
    inverse_description: str,
) -> StrategyMutationDiffV1:
    return StrategyMutationDiffV1(
        affected_rule_path=affected_rule_path,
        human_description=human_description,
        inverse_description=inverse_description,
        semantic_diff=semantic_strategy_diff(parent, child),
    )


def _require_json_pointer(value: object) -> None:
    if type(value) is not str or _JSON_POINTER.fullmatch(value) is None:
        raise ValueError("affected rule path must be a canonical JSON pointer")
    canonical_identity_bytes(value)


__all__ = [
    "STRATEGY_COMPLEXITY_SCHEMA_ID_V1",
    "STRATEGY_MUTATION_DIFF_SCHEMA_ID_V1",
    "StrategyComplexityDeltaV1",
    "StrategyComplexityV1",
    "StrategyMutationDiffV1",
    "build_mutation_diff",
    "strategy_complexity",
]
