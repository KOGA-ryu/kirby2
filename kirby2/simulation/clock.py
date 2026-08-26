"""Monotonic simulation time independent from the wall clock."""

from __future__ import annotations

from dataclasses import dataclass


MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(slots=True)
class SimulationClock:
    current_time_us: int = 0

    def __post_init__(self) -> None:
        if type(self.current_time_us) is not int or self.current_time_us < 0:
            raise ValueError("simulation time must be a nonnegative integer number of microseconds")

    def advance_to(self, time_us: int) -> None:
        if type(time_us) is not int:
            raise TypeError("simulation time must be integer microseconds")
        if time_us < self.current_time_us:
            raise ValueError("simulation clock cannot move backward")
        self.current_time_us = time_us

    def advance_by(self, delta_us: int) -> None:
        if type(delta_us) is not int or delta_us < 0:
            raise ValueError("simulation delta must be a nonnegative integer")
        self.advance_to(self.current_time_us + delta_us)

