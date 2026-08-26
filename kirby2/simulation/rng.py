"""Explicit ownership wrapper for seeded pseudo-randomness."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


class SeededRng:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._random = random.Random(seed)

    def integer(self, minimum: int, maximum: int) -> int:
        return self._random.randint(minimum, maximum)

    def index(self, stop: int) -> int:
        if stop <= 0:
            raise ValueError("stop must be positive")
        return self._random.randrange(stop)

    def unit_interval(self) -> float:
        """Draw from [0, 1) using only this explicitly owned RNG."""
        return self._random.random()

    def uniform(self, minimum: float, maximum: float) -> float:
        return self._random.uniform(minimum, maximum)

    def lognormal(self, mu: float, sigma: float) -> float:
        return self._random.lognormvariate(mu, sigma)

    def gamma(self, shape: float, scale: float) -> float:
        return self._random.gammavariate(shape, scale)

    def weighted_index(self, weights: Sequence[int]) -> int:
        if not weights or any(type(weight) is not int or weight <= 0 for weight in weights):
            raise ValueError("weights must be positive integers")
        draw = self._random.randrange(sum(weights))
        cumulative = 0
        for index, weight in enumerate(weights):
            cumulative += weight
            if draw < cumulative:
                return index
        raise RuntimeError("weighted draw exceeded cumulative weight")

    def weighted_float_index(self, weights: Sequence[float]) -> int:
        if not weights or any(not math.isfinite(weight) or weight < 0 for weight in weights):
            raise ValueError("weights must be finite and nonnegative")
        total = sum(weights)
        if total <= 0:
            raise ValueError("at least one weight must be positive")
        draw = self._random.random() * total
        cumulative = 0.0
        for index, weight in enumerate(weights):
            cumulative += weight
            if draw < cumulative:
                return index
        return len(weights) - 1

    def exponential_interval_microseconds(self, rate_per_second: float) -> int:
        if not math.isfinite(rate_per_second) or rate_per_second <= 0:
            raise ValueError("event rate must be finite and positive")
        open_unit_interval = 1.0 - self._random.random()
        seconds = -math.log(open_unit_interval) / rate_per_second
        return max(1, math.ceil(seconds * 1_000_000))
