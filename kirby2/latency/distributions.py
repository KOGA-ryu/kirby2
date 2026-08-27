"""Owned deterministic latency distributions and draw traces."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum


class LatencyComponent(str, Enum):
    MARKET_DATA_PUBLICATION = "market_data_publication_latency"
    DOWNLINK = "downlink_latency"
    RENDER = "render_latency"
    INPUT_PROCESSING = "input_processing_latency"
    CLIENT_ROUTING = "client_routing_latency"
    UPLINK = "uplink_latency"
    GATEWAY = "gateway_latency"
    VENUE_PROCESSING = "venue_processing_latency"
    FILL_REPORT = "fill_report_latency"


class LatencyDistributionKind(str, Enum):
    FIXED = "FIXED"
    UNIFORM_BOUNDED = "UNIFORM_BOUNDED"
    LOGNORMAL_BOUNDED = "LOGNORMAL_BOUNDED"
    EMPIRICAL_SAMPLES = "EMPIRICAL_SAMPLES"


@dataclass(frozen=True, slots=True)
class LatencyDistributionSpec:
    kind: LatencyDistributionKind
    lower_us: int
    upper_us: int
    parameters: tuple[float, ...] = ()
    samples_us: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.lower_us) is not int
            or type(self.upper_us) is not int
            or self.lower_us < 0
            or self.upper_us < self.lower_us
        ):
            raise ValueError("latency distribution bounds are invalid")
        if self.kind is LatencyDistributionKind.FIXED:
            if self.lower_us != self.upper_us or self.parameters or self.samples_us:
                raise ValueError("fixed latency must have one exact bound")
        elif self.kind is LatencyDistributionKind.UNIFORM_BOUNDED:
            if self.parameters or self.samples_us:
                raise ValueError("uniform latency accepts only integer bounds")
        elif self.kind is LatencyDistributionKind.LOGNORMAL_BOUNDED:
            if (
                len(self.parameters) != 2
                or any(not math.isfinite(value) for value in self.parameters)
                or self.parameters[1] < 0
                or self.samples_us
            ):
                raise ValueError("lognormal latency requires finite mu and sigma")
        elif self.kind is LatencyDistributionKind.EMPIRICAL_SAMPLES:
            if (
                self.parameters
                or not self.samples_us
                or any(
                    type(value) is not int
                    or not self.lower_us <= value <= self.upper_us
                    for value in self.samples_us
                )
            ):
                raise ValueError("empirical latency samples violate their bounds")

    @classmethod
    def fixed(cls, value_us: int) -> LatencyDistributionSpec:
        return cls(LatencyDistributionKind.FIXED, value_us, value_us)

    @classmethod
    def uniform(cls, lower_us: int, upper_us: int) -> LatencyDistributionSpec:
        return cls(LatencyDistributionKind.UNIFORM_BOUNDED, lower_us, upper_us)

    @classmethod
    def lognormal(
        cls,
        lower_us: int,
        upper_us: int,
        mu: float,
        sigma: float,
    ) -> LatencyDistributionSpec:
        return cls(
            LatencyDistributionKind.LOGNORMAL_BOUNDED,
            lower_us,
            upper_us,
            (mu, sigma),
        )

    @classmethod
    def empirical(cls, samples_us: tuple[int, ...]) -> LatencyDistributionSpec:
        if not samples_us:
            raise ValueError("empirical latency requires samples")
        return cls(
            LatencyDistributionKind.EMPIRICAL_SAMPLES,
            min(samples_us),
            max(samples_us),
            samples_us=samples_us,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "lower_us": self.lower_us,
            "parameters": list(self.parameters),
            "samples_us": list(self.samples_us),
            "upper_us": self.upper_us,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LatencyDistributionSpec:
        raw_parameters = payload.get("parameters", [])
        raw_samples = payload.get("samples_us", [])
        if not isinstance(raw_parameters, list) or not isinstance(raw_samples, list):
            raise ValueError("latency distribution arrays are invalid")
        return cls(
            kind=LatencyDistributionKind(str(payload["kind"])),
            lower_us=int(payload["lower_us"]),
            upper_us=int(payload["upper_us"]),
            parameters=tuple(float(value) for value in raw_parameters),
            samples_us=tuple(int(value) for value in raw_samples),
        )


@dataclass(frozen=True, slots=True)
class LatencyDraw:
    sequence: int
    simulation_time_us: int
    component: LatencyComponent
    distribution: LatencyDistributionKind
    sampled_latency_us: int
    purpose: str

    def as_dict(self) -> dict[str, object]:
        return {
            "component": self.component.value,
            "distribution": self.distribution.value,
            "purpose": self.purpose,
            "sampled_latency_us": self.sampled_latency_us,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }


class LatencySampler:
    def __init__(self, seed: int) -> None:
        if type(seed) is not int:
            raise TypeError("latency seed must be an integer")
        self.seed = seed
        self._rng = random.Random(seed)
        self._draws: list[LatencyDraw] = []

    @property
    def draws(self) -> tuple[LatencyDraw, ...]:
        return tuple(self._draws)

    def runtime_state(self) -> dict[str, object]:
        version, internal, gaussian = self._rng.getstate()
        return {
            "draws": [item.as_dict() for item in self._draws],
            "gaussian_cache": gaussian,
            "internal_state": list(internal),
            "random_state_version": version,
            "seed": self.seed,
        }

    def sample(
        self,
        component: LatencyComponent,
        spec: LatencyDistributionSpec,
        simulation_time_us: int,
        purpose: str,
    ) -> int:
        if spec.kind is LatencyDistributionKind.FIXED:
            sampled = spec.lower_us
        elif spec.kind is LatencyDistributionKind.UNIFORM_BOUNDED:
            sampled = self._rng.randint(spec.lower_us, spec.upper_us)
        elif spec.kind is LatencyDistributionKind.LOGNORMAL_BOUNDED:
            raw = self._rng.lognormvariate(*spec.parameters)
            sampled = min(spec.upper_us, max(spec.lower_us, int(round(raw))))
        elif spec.kind is LatencyDistributionKind.EMPIRICAL_SAMPLES:
            sampled = spec.samples_us[self._rng.randrange(len(spec.samples_us))]
        else:  # pragma: no cover - exhaustive enum guard
            raise RuntimeError("unsupported latency distribution")
        self._draws.append(
            LatencyDraw(
                len(self._draws) + 1,
                simulation_time_us,
                component,
                spec.kind,
                sampled,
                purpose,
            )
        )
        return sampled
