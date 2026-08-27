"""Serializable bounded integer distributions for market-flow inputs."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .rng import SeededRng


class DistributionPurpose(str, Enum):
    ORDER_SIZE = "order_size"
    TRADE_SIZE = "trade_size"
    CANCEL_SIZE = "cancel_size"
    QUEUE_DEPTH = "queue_depth"
    LIMIT_PLACEMENT_DEPTH = "limit_placement_depth"
    INTER_EVENT_TIMING_MODIFIER = "inter_event_timing_modifier"
    SPREAD_STATE_DURATION = "spread_state_duration"

    @property
    def unit(self) -> str:
        return _PURPOSE_UNITS[self]


INTER_EVENT_TIMING_SCALE = 1_000
"""Per-mille scale for timing modifiers; 1,000 means an unchanged interval."""


_PURPOSE_UNITS = {
    DistributionPurpose.ORDER_SIZE: "shares",
    DistributionPurpose.TRADE_SIZE: "shares",
    DistributionPurpose.CANCEL_SIZE: "shares",
    DistributionPurpose.QUEUE_DEPTH: "shares",
    DistributionPurpose.LIMIT_PLACEMENT_DEPTH: "ticks_behind_best",
    DistributionPurpose.INTER_EVENT_TIMING_MODIFIER: "per_mille_1000_equals_1x",
    DistributionPurpose.SPREAD_STATE_DURATION: "simulation_microseconds",
}


@dataclass(frozen=True, slots=True)
class DistributionDrawRecord:
    sequence: int
    profile_id: str
    purpose: DistributionPurpose
    sampled_value: int
    simulation_time_us: int
    consumer: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("distribution draw sequence must be positive")
        if not self.profile_id:
            raise ValueError("distribution draw profile ID must not be empty")
        if type(self.sampled_value) is not int:
            raise TypeError("distribution sampled value must be an integer")
        if self.purpose is DistributionPurpose.LIMIT_PLACEMENT_DEPTH:
            if self.sampled_value < 0:
                raise ValueError("placement-depth draw must be nonnegative")
        elif self.sampled_value <= 0:
            raise ValueError(f"{self.purpose.value} draw must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("distribution draw simulation time must be nonnegative")
        if not self.consumer:
            raise ValueError("distribution draw consumer must not be empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "consumer": self.consumer,
            "draw_sequence": self.sequence,
            "profile_id": self.profile_id,
            "purpose": self.purpose.value,
            "sampled_value": self.sampled_value,
            "simulation_time_us": self.simulation_time_us,
            "unit": self.purpose.unit,
        }


@runtime_checkable
class IntegerSampleDistribution(Protocol):
    @property
    def values(self) -> tuple[int, ...]: ...

    def draw(self, rng: SeededRng) -> int: ...

    def as_dict(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class FixedDistribution:
    value: int

    def __post_init__(self) -> None:
        _integer_bound(self.value, "fixed value")

    @property
    def values(self) -> tuple[int, ...]:
        return (self.value,)

    def draw(self, rng: SeededRng) -> int:
        return self.value

    def as_dict(self) -> dict[str, object]:
        return {"kind": "fixed", "value": self.value}


@dataclass(frozen=True, slots=True)
class UniformIntegerDistribution:
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)

    @property
    def values(self) -> tuple[int, ...]:
        return (self.minimum, self.maximum)

    def draw(self, rng: SeededRng) -> int:
        return rng.integer(self.minimum, self.maximum)

    def as_dict(self) -> dict[str, object]:
        return {"kind": "uniform", "maximum": self.maximum, "minimum": self.minimum}


@dataclass(frozen=True, slots=True)
class LognormalIntegerDistribution:
    mu: float
    sigma: float
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)
        if not math.isfinite(self.mu) or not math.isfinite(self.sigma) or self.sigma < 0:
            raise ValueError("lognormal parameters must be finite with nonnegative sigma")

    @property
    def values(self) -> tuple[int, ...]:
        return (self.minimum, self.maximum)

    def draw(self, rng: SeededRng) -> int:
        return _bounded_round(rng.lognormal(self.mu, self.sigma), self.minimum, self.maximum)

    def as_dict(self) -> dict[str, object]:
        return {"kind": "lognormal", "maximum": self.maximum, "minimum": self.minimum, "mu": self.mu, "sigma": self.sigma}


@dataclass(frozen=True, slots=True)
class GammaIntegerDistribution:
    shape: float
    scale: float
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)
        if any(not math.isfinite(value) or value <= 0 for value in (self.shape, self.scale)):
            raise ValueError("gamma shape and scale must be finite and positive")

    @property
    def values(self) -> tuple[int, ...]:
        return (self.minimum, self.maximum)

    def draw(self, rng: SeededRng) -> int:
        return _bounded_round(rng.gamma(self.shape, self.scale), self.minimum, self.maximum)

    def as_dict(self) -> dict[str, object]:
        return {"kind": "gamma", "maximum": self.maximum, "minimum": self.minimum, "scale": self.scale, "shape": self.shape}


@dataclass(frozen=True, slots=True)
class GeometricIntegerDistribution:
    probability: float
    minimum: int = 0
    maximum: int = 100

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)
        if not math.isfinite(self.probability) or not 0 < self.probability <= 1:
            raise ValueError("geometric probability must lie in (0, 1]")

    @property
    def values(self) -> tuple[int, ...]:
        return (self.minimum, self.maximum)

    def draw(self, rng: SeededRng) -> int:
        if self.probability == 1:
            return self.minimum
        failures = math.floor(
            math.log1p(-rng.unit_interval()) / math.log1p(-self.probability)
        )
        return min(self.maximum, self.minimum + failures)

    def as_dict(self) -> dict[str, object]:
        return {"kind": "geometric", "maximum": self.maximum, "minimum": self.minimum, "probability": self.probability}


@dataclass(frozen=True, slots=True)
class CategoricalIntegerDistribution:
    values: tuple[int, ...]
    weights: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.values or len(self.values) != len(self.weights):
            raise ValueError("categorical values and weights must align")
        if any(type(value) is not int or value < 0 for value in self.values):
            raise ValueError("categorical values must be nonnegative integers")
        if any(type(weight) is not int or weight <= 0 for weight in self.weights):
            raise ValueError("categorical weights must be positive integers")

    def draw(self, rng: SeededRng) -> int:
        return self.values[rng.weighted_index(self.weights)]

    def as_dict(self) -> dict[str, object]:
        return {"kind": "categorical", "values": list(self.values), "weights": list(self.weights)}


@dataclass(frozen=True, slots=True)
class EmpiricalIntegerDistribution:
    observations: tuple[int, ...]
    weights: tuple[int, ...] | None = None
    source_id: str = "inline"
    minimum: int = 0
    maximum: int = 1_000_000

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)
        if not self.observations or any(type(value) is not int for value in self.observations):
            raise ValueError("empirical observations must be nonempty integers")
        if self.weights is not None and (
            len(self.weights) != len(self.observations)
            or any(type(weight) is not int or weight <= 0 for weight in self.weights)
        ):
            raise ValueError("empirical weights must align and be positive")
        if not self.source_id:
            raise ValueError("empirical source ID must not be empty")

    @property
    def values(self) -> tuple[int, ...]:
        return tuple(_clamp(value, self.minimum, self.maximum) for value in self.observations)

    def draw(self, rng: SeededRng) -> int:
        index = (
            rng.index(len(self.observations))
            if self.weights is None
            else rng.weighted_index(self.weights)
        )
        return _clamp(self.observations[index], self.minimum, self.maximum)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "empirical",
            "maximum": self.maximum,
            "minimum": self.minimum,
            "observations": list(self.observations),
            "source_id": self.source_id,
            "weights": None if self.weights is None else list(self.weights),
        }

    @classmethod
    def from_normalized_file(cls, path: Path) -> EmpiricalIntegerDistribution:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported normalized empirical distribution schema")
        observations = payload.get("observations")
        weights = payload.get("weights")
        if not isinstance(observations, list):
            raise ValueError("normalized empirical observations must be an array")
        if weights is not None and not isinstance(weights, list):
            raise ValueError("normalized empirical weights must be an array or null")
        return cls(
            observations=tuple(int(value) for value in observations),
            weights=None if weights is None else tuple(int(value) for value in weights),
            source_id=str(payload["dataset_id"]),
            minimum=int(payload.get("minimum", 0)),
            maximum=int(payload.get("maximum", 1_000_000)),
        )


@dataclass(frozen=True, slots=True)
class DistributionProfile:
    profile_id: str
    distributions: dict[DistributionPurpose, IntegerSampleDistribution]

    def __post_init__(self) -> None:
        if any(type(purpose) is not DistributionPurpose for purpose in self.distributions):
            raise ValueError("distribution profile contains an unsupported purpose")
        if not self.profile_id or set(self.distributions) != set(DistributionPurpose):
            raise ValueError("distribution profile must identify and cover every purpose")
        if any(not isinstance(value, IntegerSampleDistribution) for value in self.distributions.values()):
            raise TypeError("distribution profile values must implement IntegerSampleDistribution")
        positive_purposes = set(DistributionPurpose) - {
            DistributionPurpose.LIMIT_PLACEMENT_DEPTH
        }
        for purpose in positive_purposes:
            if any(value <= 0 for value in self.distributions[purpose].values):
                raise ValueError(f"{purpose.value} distribution must remain positive")

    def distribution(self, purpose: DistributionPurpose) -> IntegerSampleDistribution:
        return self.distributions[purpose]

    def as_dict(self) -> dict[str, object]:
        return {
            "distributions": {
                purpose.value: {
                    **self.distributions[purpose].as_dict(),
                    "unit": purpose.unit,
                }
                for purpose in DistributionPurpose
            },
            "profile_id": self.profile_id,
            "timing_modifier_scale": INTER_EVENT_TIMING_SCALE,
        }


def _integer_bound(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _bounds(minimum: int, maximum: int) -> None:
    _integer_bound(minimum, "minimum")
    _integer_bound(maximum, "maximum")
    if maximum < minimum:
        raise ValueError("distribution maximum must not be below minimum")


def _bounded_round(value: float, minimum: int, maximum: int) -> int:
    if not math.isfinite(value):
        raise ValueError("distribution produced non-finite value")
    return _clamp(round(value), minimum, maximum)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))
