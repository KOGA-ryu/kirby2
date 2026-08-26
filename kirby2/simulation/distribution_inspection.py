"""Deterministic summary statistics for configured integer distributions."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .distribution_framework import DistributionProfile, DistributionPurpose
from .rng import SeededRng


@dataclass(frozen=True, slots=True)
class DistributionInspection:
    profile_id: str
    purpose: DistributionPurpose
    seed: int
    sample_count: int
    mean: float
    median: float
    minimum: int
    maximum: int
    quantiles: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum": self.maximum,
            "mean": self.mean,
            "median": self.median,
            "minimum": self.minimum,
            "profile_id": self.profile_id,
            "purpose": self.purpose.value,
            "quantiles": self.quantiles,
            "sample_count": self.sample_count,
            "seed": self.seed,
        }


def inspect_distribution(
    profile: DistributionProfile,
    purpose: DistributionPurpose,
    seed: int = 42,
    sample_count: int = 10_000,
) -> DistributionInspection:
    if type(seed) is not int:
        raise TypeError("distribution inspection seed must be an integer")
    if type(sample_count) is not int or sample_count <= 0:
        raise ValueError("distribution inspection sample count must be positive")
    rng = SeededRng(seed)
    distribution = profile.distribution(purpose)
    samples = sorted(distribution.draw(rng) for _ in range(sample_count))
    return DistributionInspection(
        profile_id=profile.profile_id,
        purpose=purpose,
        seed=seed,
        sample_count=sample_count,
        mean=round(statistics.fmean(samples), 6),
        median=round(statistics.median(samples), 6),
        minimum=samples[0],
        maximum=samples[-1],
        quantiles={
            "p05": _quantile(samples, 0.05),
            "p25": _quantile(samples, 0.25),
            "p50": _quantile(samples, 0.50),
            "p75": _quantile(samples, 0.75),
            "p95": _quantile(samples, 0.95),
            "p99": _quantile(samples, 0.99),
        },
    )


def _quantile(samples: list[int], probability: float) -> int:
    return samples[round((len(samples) - 1) * probability)]
