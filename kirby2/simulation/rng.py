"""Explicit ownership wrapper for seeded pseudo-randomness."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence


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

    def runtime_state(self) -> dict[str, object]:
        """Return the complete portable PRNG state used by deterministic forks."""

        version, internal, gaussian = self._random.getstate()
        return {
            "gaussian_cache": gaussian,
            "internal_state": list(internal),
            "random_state_version": version,
            "seed": self.seed,
        }

    @classmethod
    def from_runtime_state(cls, payload: Mapping[str, object]) -> "SeededRng":
        """Restore one detached RNG from its exact portable state.

        Full-day component restore must not recreate a stream from its seed after
        draws have been consumed.  This constructor therefore requires the native
        PRNG state as well as the identity-bearing seed and refuses coercions or
        extra fields.
        """

        if not isinstance(payload, Mapping):
            raise TypeError("RNG runtime state must be an object")
        expected = {
            "gaussian_cache",
            "internal_state",
            "random_state_version",
            "seed",
        }
        if set(payload) != expected:
            raise ValueError("RNG runtime state fields are not exact")
        seed = payload["seed"]
        version = payload["random_state_version"]
        internal = payload["internal_state"]
        gaussian = payload["gaussian_cache"]
        if type(seed) is not int or seed < 0:
            raise ValueError("RNG seed must be a nonnegative integer")
        if type(version) is not int or version < 1:
            raise ValueError("RNG state version must be a positive integer")
        if type(internal) is not list or not internal or any(
            type(item) is not int for item in internal
        ):
            raise TypeError("RNG internal state must be a nonempty integer array")
        if gaussian is not None and type(gaussian) is not float:
            raise TypeError("RNG Gaussian cache must be null or a float")
        restored = cls(seed)
        try:
            restored._random.setstate((version, tuple(internal), gaussian))
        except (TypeError, ValueError) as error:
            raise ValueError("RNG runtime state is invalid") from error
        if restored.runtime_state() != dict(payload):
            raise ValueError("RNG runtime state is not a canonical fixed point")
        return restored

    def state_sha256(self) -> str:
        canonical = json.dumps(
            self.runtime_state(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
