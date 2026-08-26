"""Configurable session-time profiles above the exchange clock."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum


MICROSECONDS_PER_SECOND = 1_000_000


class IntradayPhase(str, Enum):
    PREOPEN = "PREOPEN"
    OPENING = "OPENING"
    MORNING = "MORNING"
    MIDDAY = "MIDDAY"
    AFTERNOON = "AFTERNOON"
    CLOSE = "CLOSE"


@dataclass(frozen=True, slots=True)
class IntradayModifiers:
    relative_volume: float
    event_intensity: float
    spread_tendency: float
    depth: float
    volatility: float
    cancellation_activity: float
    trade_size: float

    def __post_init__(self) -> None:
        values = self.as_dict().values()
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("intraday modifiers must be finite and positive")

    def as_dict(self) -> dict[str, float]:
        return {
            "cancellation_activity": self.cancellation_activity,
            "depth": self.depth,
            "event_intensity": self.event_intensity,
            "relative_volume": self.relative_volume,
            "spread_tendency": self.spread_tendency,
            "trade_size": self.trade_size,
            "volatility": self.volatility,
        }


@dataclass(frozen=True, slots=True)
class IntradaySegment:
    phase: IntradayPhase
    start_second: int
    end_second: int
    modifiers: IntradayModifiers

    def __post_init__(self) -> None:
        if (
            type(self.start_second) is not int
            or type(self.end_second) is not int
            or self.start_second < 0
            or self.end_second <= self.start_second
        ):
            raise ValueError("intraday segment bounds are invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "end": format_session_second(self.end_second),
            "end_second": self.end_second,
            "modifiers": self.modifiers.as_dict(),
            "phase": self.phase.value,
            "start": format_session_second(self.start_second),
            "start_second": self.start_second,
        }


@dataclass(frozen=True, slots=True)
class IntradayProfile:
    profile_id: str
    segments: tuple[IntradaySegment, ...]

    def __post_init__(self) -> None:
        if not self.profile_id or not self.segments:
            raise ValueError("intraday profile identity and segments are required")
        if tuple(segment.phase for segment in self.segments) != tuple(IntradayPhase):
            raise ValueError("intraday profile must contain the six phases in order")
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.end_second != current.start_second:
                raise ValueError("intraday segments must be contiguous")

    @property
    def start_second(self) -> int:
        return self.segments[0].start_second

    @property
    def end_second(self) -> int:
        return self.segments[-1].end_second

    def segment_at(self, session_second: int) -> IntradaySegment:
        if session_second == self.end_second:
            return self.segments[-1]
        for segment in self.segments:
            if segment.start_second <= session_second < segment.end_second:
                return segment
        raise ValueError("session time lies outside the intraday profile")

    def modifiers_at(
        self,
        session_second: int,
        observed_relative_volume: float | None = None,
    ) -> IntradayModifiers:
        modifiers = self.segment_at(session_second).modifiers
        if observed_relative_volume is None:
            return modifiers
        if observed_relative_volume <= 0:
            raise ValueError("observed intraday volume must be positive")
        return replace(modifiers, relative_volume=float(observed_relative_volume))

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "segments": [segment.as_dict() for segment in self.segments],
        }


@dataclass(frozen=True, slots=True)
class IntradayWindow:
    start_second: int
    end_second: int

    def __post_init__(self) -> None:
        if self.start_second < 0 or self.end_second <= self.start_second:
            raise ValueError("intraday exercise window is invalid")

    @property
    def duration_us(self) -> int:
        return (self.end_second - self.start_second) * MICROSECONDS_PER_SECOND

    @classmethod
    def parse(cls, start: str, end: str) -> IntradayWindow:
        return cls(parse_session_time(start), parse_session_time(end))

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_us": self.duration_us,
            "end": format_session_second(self.end_second),
            "start": format_session_second(self.start_second),
        }


@dataclass(frozen=True, slots=True)
class ObservedVolumePoint:
    """A normalized observed-volume multiplier beginning at session time."""

    start_second: int
    relative_volume: float

    def __post_init__(self) -> None:
        if type(self.start_second) is not int or self.start_second < 0:
            raise ValueError("observed volume time must be a nonnegative integer")
        if (
            not isinstance(self.relative_volume, (int, float))
            or isinstance(self.relative_volume, bool)
            or not math.isfinite(self.relative_volume)
            or self.relative_volume <= 0
        ):
            raise ValueError("observed relative volume must be finite and positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_volume": float(self.relative_volume),
            "start": format_session_second(self.start_second),
            "start_second": self.start_second,
        }


@dataclass(frozen=True, slots=True)
class ObservedVolumeCurve:
    """Deterministic step curve supplied by normalized historical reconstruction."""

    source_id: str
    points: tuple[ObservedVolumePoint, ...]

    def __post_init__(self) -> None:
        if not self.source_id or not self.points:
            raise ValueError("observed volume source and points are required")
        starts = tuple(point.start_second for point in self.points)
        if starts != tuple(sorted(starts)) or len(starts) != len(set(starts)):
            raise ValueError("observed volume points must be unique and ordered")

    def value_at(self, session_second: int) -> float:
        value: float | None = None
        for point in self.points:
            if point.start_second > session_second:
                break
            value = float(point.relative_volume)
        if value is None:
            raise ValueError("observed volume curve does not cover this session time")
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "points": [point.as_dict() for point in self.points],
            "source_id": self.source_id,
        }


@dataclass(slots=True)
class IntradayClock:
    profile: IntradayProfile
    window: IntradayWindow
    observed_volume: ObservedVolumeCurve | None = None
    simulation_time_us: int = 0

    def __post_init__(self) -> None:
        if (
            self.window.start_second < self.profile.start_second
            or self.window.end_second > self.profile.end_second
        ):
            raise ValueError("exercise window exceeds intraday profile")
        if self.observed_volume is not None:
            self.observed_volume.value_at(self.window.start_second)
        self.advance_to(self.simulation_time_us)

    @property
    def session_second(self) -> int:
        return self.window.start_second + self.simulation_time_us // MICROSECONDS_PER_SECOND

    @property
    def phase(self) -> IntradayPhase:
        return self.profile.segment_at(self.session_second).phase

    @property
    def modifiers(self) -> IntradayModifiers:
        observed = (
            None
            if self.observed_volume is None
            else self.observed_volume.value_at(self.session_second)
        )
        return self.profile.modifiers_at(self.session_second, observed)

    @property
    def next_transition_time_us(self) -> int | None:
        """Next profile or observed-volume boundary inside the exercise window."""

        candidates = [
            segment.end_second
            for segment in self.profile.segments
            if self.session_second < segment.end_second < self.window.end_second
        ]
        if self.observed_volume is not None:
            candidates.extend(
                point.start_second
                for point in self.observed_volume.points
                if self.session_second < point.start_second < self.window.end_second
            )
        if not candidates:
            return None
        return (min(candidates) - self.window.start_second) * MICROSECONDS_PER_SECOND

    def advance_to(self, simulation_time_us: int) -> None:
        if type(simulation_time_us) is not int or not 0 <= simulation_time_us <= self.window.duration_us:
            raise ValueError("intraday simulation time exceeds exercise window")
        if simulation_time_us < self.simulation_time_us:
            raise ValueError("intraday clock cannot move backward")
        self.simulation_time_us = simulation_time_us

    def as_dict(self) -> dict[str, object]:
        result = {
            "phase": self.phase.value,
            "profile_id": self.profile.profile_id,
            "session_time": format_session_second(self.session_second),
            "simulation_time_us": self.simulation_time_us,
            "window": self.window.as_dict(),
        }
        if self.observed_volume is not None:
            result["observed_volume"] = self.observed_volume.as_dict()
        return result


def equity_u_shaped_profile() -> IntradayProfile:
    def modifiers(volume, intensity, spread, depth, volatility, cancels, trade_size):
        return IntradayModifiers(
            float(volume),
            float(intensity),
            float(spread),
            float(depth),
            float(volatility),
            float(cancels),
            float(trade_size),
        )

    return IntradayProfile(
        "equity_u_shaped_v1",
        (
            IntradaySegment(IntradayPhase.PREOPEN, 8 * 3600, 9 * 3600 + 30 * 60, modifiers(0.45, 0.55, 1.8, 0.55, 0.8, 0.7, 0.7)),
            IntradaySegment(IntradayPhase.OPENING, 9 * 3600 + 30 * 60, 10 * 3600, modifiers(2.5, 2.2, 1.5, 0.8, 1.8, 1.6, 1.5)),
            IntradaySegment(IntradayPhase.MORNING, 10 * 3600, 12 * 3600, modifiers(1.2, 1.15, 1.0, 1.1, 1.1, 1.0, 1.05)),
            IntradaySegment(IntradayPhase.MIDDAY, 12 * 3600, 14 * 3600, modifiers(0.65, 0.65, 0.8, 1.3, 0.7, 0.75, 0.8)),
            IntradaySegment(IntradayPhase.AFTERNOON, 14 * 3600, 15 * 3600 + 30 * 60, modifiers(1.0, 1.0, 1.0, 1.1, 1.0, 1.0, 1.0)),
            IntradaySegment(IntradayPhase.CLOSE, 15 * 3600 + 30 * 60, 16 * 3600, modifiers(2.0, 1.9, 1.3, 0.9, 1.6, 1.5, 1.4)),
        ),
    )


def parse_session_time(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("session time must use HH:MM:SS")
    hour, minute, second = (int(part) for part in parts)
    if not 0 <= hour < 24 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise ValueError("session time components are invalid")
    return hour * 3600 + minute * 60 + second


def format_session_second(value: int) -> str:
    if type(value) is not int or not 0 <= value <= 24 * 3600:
        raise ValueError("session second is outside one day")
    hour, remainder = divmod(value, 3600)
    minute, second = divmod(remainder, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"
