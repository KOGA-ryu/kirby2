"""Replaceable integer-distribution boundary for flow quantities and depth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .rng import SeededRng


class IntegerDistribution(Protocol):
    @property
    def values(self) -> tuple[int, ...]: ...

    def draw(self, rng: SeededRng) -> int: ...

    def as_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class WeightedDiscreteDistribution:
    values: tuple[int, ...]
    weights: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.values or len(self.values) != len(self.weights):
            raise ValueError("values and weights must be nonempty and have equal length")
        if any(type(value) is not int or value < 0 for value in self.values):
            raise ValueError("distribution values must be nonnegative integers")
        if any(type(weight) is not int or weight <= 0 for weight in self.weights):
            raise ValueError("distribution weights must be positive integers")

    def draw(self, rng: SeededRng) -> int:
        return self.values[rng.weighted_index(self.weights)]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "weighted_discrete",
            "values": list(self.values),
            "weights": list(self.weights),
        }

