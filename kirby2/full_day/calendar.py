"""Strict synthetic trading-day calendar contracts for full-day execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kirby2.exchange import SessionState

from .models import (
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)


CALENDAR_SCHEMA_VERSION = 1
PHASE_IDS = (
    "PREOPEN",
    "OPENING_AUCTION",
    "CONTINUOUS",
    "CLOSING_AUCTION",
    "POSTCLOSE",
)
_BOUNDARY_STATES = (
    SessionState.PREOPEN,
    SessionState.OPENING_AUCTION,
    SessionState.CONTINUOUS,
    SessionState.CLOSING_AUCTION,
    SessionState.POSTCLOSE,
    SessionState.CLOSED,
)
_UNCROSS_BEFORE = (False, False, True, False, True, False)


def _exact_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


@dataclass(frozen=True, slots=True)
class LocalBoundaryV1:
    """One explicitly resolved local wall-clock boundary."""

    schema_version: int
    local_date: str
    local_time: str
    timezone_name: str
    fold: int
    utc_offset_seconds: int
    simulation_time_us: int

    def __post_init__(self) -> None:
        if _exact_int(self.schema_version, "schema_version") != CALENDAR_SCHEMA_VERSION:
            raise ValueError("LocalBoundaryV1 schema_version must be 1")
        _exact_string(self.local_date, "local_date")
        _exact_string(self.local_time, "local_time")
        _exact_string(self.timezone_name, "timezone_name")
        if type(self.fold) is not int or self.fold not in (0, 1):
            raise ValueError("fold must be the explicit integer 0 or 1")
        _exact_int(self.utc_offset_seconds, "utc_offset_seconds")
        _exact_int(self.simulation_time_us, "simulation_time_us", minimum=0)
        self._resolved_datetime()

    def _resolved_datetime(self) -> datetime:
        try:
            parsed_date = date.fromisoformat(self.local_date)
        except ValueError as error:
            raise ValueError("local_date must be canonical ISO YYYY-MM-DD") from error
        if parsed_date.isoformat() != self.local_date:
            raise ValueError("local_date must use canonical ISO rendering")
        try:
            parsed_time = time.fromisoformat(self.local_time)
        except ValueError as error:
            raise ValueError(
                "local_time must be canonical HH:MM:SS.ffffff"
            ) from error
        if parsed_time.tzinfo is not None or (
            parsed_time.isoformat(timespec="microseconds") != self.local_time
        ):
            raise ValueError("local_time must be canonical HH:MM:SS.ffffff")
        try:
            zone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone_name must name an installed IANA zone") from error

        naive = datetime.combine(parsed_date, parsed_time)
        candidates: dict[int, datetime] = {}
        for candidate_fold in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=candidate_fold)
            round_trip = candidate.astimezone(UTC).astimezone(zone)
            if (
                round_trip.replace(tzinfo=None) == naive
                and round_trip.fold == candidate_fold
            ):
                candidates[candidate_fold] = candidate
        if not candidates:
            raise ValueError("local boundary is nonexistent in the selected IANA zone")
        if self.fold not in candidates:
            raise ValueError("fold does not resolve this local boundary")
        resolved = candidates[self.fold]
        offset = resolved.utcoffset()
        if offset is None:
            raise ValueError("local boundary has no resolved UTC offset")
        offset_us = _timedelta_microseconds(offset)
        if offset_us % 1_000_000:
            raise ValueError("UTC offset must resolve to whole seconds")
        if offset_us // 1_000_000 != self.utc_offset_seconds:
            raise ValueError("utc_offset_seconds does not match the IANA round trip")
        return resolved

    @property
    def utc_datetime(self) -> datetime:
        return self._resolved_datetime().astimezone(UTC)

    def as_dict(self) -> dict[str, object]:
        return {
            "fold": self.fold,
            "local_date": self.local_date,
            "local_time": self.local_time,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
            "timezone_name": self.timezone_name,
            "utc_offset_seconds": self.utc_offset_seconds,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> LocalBoundaryV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "fold",
                "local_date",
                "local_time",
                "schema_version",
                "simulation_time_us",
                "timezone_name",
                "utc_offset_seconds",
            },
            "LocalBoundaryV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            local_date=_exact_string(payload["local_date"], "local_date"),
            local_time=_exact_string(payload["local_time"], "local_time"),
            timezone_name=_exact_string(payload["timezone_name"], "timezone_name"),
            fold=_exact_int(payload["fold"], "fold"),
            utc_offset_seconds=_exact_int(
                payload["utc_offset_seconds"], "utc_offset_seconds"
            ),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"], "simulation_time_us"
            ),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> LocalBoundaryV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class CalendarPhaseV1:
    schema_version: int
    phase_id: str
    start: LocalBoundaryV1
    end: LocalBoundaryV1

    def __post_init__(self) -> None:
        if _exact_int(self.schema_version, "schema_version") != CALENDAR_SCHEMA_VERSION:
            raise ValueError("CalendarPhaseV1 schema_version must be 1")
        if self.phase_id not in PHASE_IDS:
            raise ValueError("phase_id is not one of the five canonical phases")
        if type(self.start) is not LocalBoundaryV1 or type(self.end) is not LocalBoundaryV1:
            raise TypeError("phase boundaries must be LocalBoundaryV1 records")
        if self.end.simulation_time_us <= self.start.simulation_time_us:
            raise ValueError("calendar phases must be nonempty and forward")
        if self.end.utc_datetime <= self.start.utc_datetime:
            raise ValueError("calendar phase UTC time must move strictly forward")
        elapsed_us = _timedelta_microseconds(
            self.end.utc_datetime - self.start.utc_datetime
        )
        if elapsed_us != self.end.simulation_time_us - self.start.simulation_time_us:
            raise ValueError("simulation duration must equal resolved UTC duration")

    def as_dict(self) -> dict[str, object]:
        return {
            "end": self.end.as_dict(),
            "phase_id": self.phase_id,
            "schema_version": self.schema_version,
            "start": self.start.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CalendarPhaseV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {"end", "phase_id", "schema_version", "start"},
            "CalendarPhaseV1",
        )
        start = payload["start"]
        end = payload["end"]
        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
            raise TypeError("calendar phase start/end must be objects")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            phase_id=_exact_string(payload["phase_id"], "phase_id"),
            start=LocalBoundaryV1.from_dict(start),
            end=LocalBoundaryV1.from_dict(end),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CalendarPhaseV1:
        return cls.from_dict(parse_canonical_json_object(payload))


@dataclass(frozen=True, slots=True)
class BoundaryOperationV1:
    schema_version: int
    boundary: LocalBoundaryV1
    destination_session_state: SessionState
    uncross_before: bool

    def __post_init__(self) -> None:
        if _exact_int(self.schema_version, "schema_version") != CALENDAR_SCHEMA_VERSION:
            raise ValueError("BoundaryOperationV1 schema_version must be 1")
        if type(self.boundary) is not LocalBoundaryV1:
            raise TypeError("boundary operation requires LocalBoundaryV1")
        if type(self.destination_session_state) is not SessionState:
            raise TypeError("destination_session_state must use SessionState")
        if type(self.uncross_before) is not bool:
            raise TypeError("uncross_before must be a canonical JSON boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "boundary": self.boundary.as_dict(),
            "destination_session_state": self.destination_session_state.value,
            "schema_version": self.schema_version,
            "uncross_before": self.uncross_before,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> BoundaryOperationV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "boundary",
                "destination_session_state",
                "schema_version",
                "uncross_before",
            },
            "BoundaryOperationV1",
        )
        boundary = payload["boundary"]
        if not isinstance(boundary, Mapping):
            raise TypeError("boundary operation boundary must be an object")
        if type(payload["uncross_before"]) is not bool:
            raise TypeError("uncross_before must be a boolean")
        try:
            destination = SessionState(
                _exact_string(
                    payload["destination_session_state"],
                    "destination_session_state",
                )
            )
        except ValueError as error:
            raise ValueError("destination_session_state is invalid") from error
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            boundary=LocalBoundaryV1.from_dict(boundary),
            destination_session_state=destination,
            uncross_before=payload["uncross_before"],
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> BoundaryOperationV1:
        return cls.from_dict(parse_canonical_json_object(payload))


@dataclass(frozen=True, slots=True)
class TradingDayCalendarV1:
    """A strict five-phase synthetic calendar for exactly one local date."""

    schema_version: int
    calendar_id: str
    synthetic_local_date: str
    timezone_name: str
    phases: tuple[CalendarPhaseV1, ...]
    boundary_operations: tuple[BoundaryOperationV1, ...]

    def __post_init__(self) -> None:
        if _exact_int(self.schema_version, "schema_version") != CALENDAR_SCHEMA_VERSION:
            raise ValueError("TradingDayCalendarV1 schema_version must be 1")
        _exact_string(self.calendar_id, "calendar_id")
        _exact_string(self.synthetic_local_date, "synthetic_local_date")
        _exact_string(self.timezone_name, "timezone_name")
        if type(self.phases) is not tuple or type(self.boundary_operations) is not tuple:
            raise TypeError("calendar phases and operations must be immutable tuples")
        if any(type(item) is not CalendarPhaseV1 for item in self.phases):
            raise TypeError("phases must contain CalendarPhaseV1 records")
        if tuple(item.phase_id for item in self.phases) != PHASE_IDS:
            raise ValueError("calendar must contain the five phases in canonical order")
        first = self.phases[0].start
        if first.simulation_time_us != 0:
            raise ValueError("t=0 must be the start of preopen")
        if any(
            boundary.local_date != self.synthetic_local_date
            or boundary.timezone_name != self.timezone_name
            for phase in self.phases
            for boundary in (phase.start, phase.end)
        ):
            raise ValueError("every phase boundary must use the calendar date and zone")
        for previous, current in zip(self.phases, self.phases[1:]):
            if previous.end.as_dict() != current.start.as_dict():
                raise ValueError("calendar phases must be contiguous without gaps")

        expected_boundaries = (
            self.phases[0].start,
            *(phase.end for phase in self.phases),
        )
        if len(self.boundary_operations) != len(expected_boundaries):
            raise ValueError("calendar requires one operation at every phase boundary")
        for index, (operation, boundary, state, uncross) in enumerate(
            zip(
                self.boundary_operations,
                expected_boundaries,
                _BOUNDARY_STATES,
                _UNCROSS_BEFORE,
            )
        ):
            if type(operation) is not BoundaryOperationV1:
                raise TypeError("boundary_operations must contain canonical records")
            if operation.boundary.as_dict() != boundary.as_dict():
                raise ValueError(f"boundary operation {index} targets the wrong boundary")
            if operation.destination_session_state is not state:
                raise ValueError(f"boundary operation {index} has the wrong destination")
            if operation.uncross_before is not uncross:
                raise ValueError(f"boundary operation {index} has wrong uncross order")

        origin = first.utc_datetime
        for boundary in expected_boundaries:
            elapsed_us = _timedelta_microseconds(boundary.utc_datetime - origin)
            if elapsed_us < 0 or elapsed_us != boundary.simulation_time_us:
                raise ValueError(
                    "boundary simulation time must be forward UTC elapsed time from t=0"
                )

    @property
    def end_time_us(self) -> int:
        return self.phases[-1].end.simulation_time_us

    def as_dict(self) -> dict[str, object]:
        return {
            "boundary_operations": [item.as_dict() for item in self.boundary_operations],
            "calendar_id": self.calendar_id,
            "phases": [item.as_dict() for item in self.phases],
            "schema_version": self.schema_version,
            "synthetic_local_date": self.synthetic_local_date,
            "timezone_name": self.timezone_name,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TradingDayCalendarV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "boundary_operations",
                "calendar_id",
                "phases",
                "schema_version",
                "synthetic_local_date",
                "timezone_name",
            },
            "TradingDayCalendarV1",
        )
        phases = payload["phases"]
        operations = payload["boundary_operations"]
        if not isinstance(phases, list) or any(
            not isinstance(item, Mapping) for item in phases
        ):
            raise TypeError("phases must be a JSON array of objects")
        if not isinstance(operations, list) or any(
            not isinstance(item, Mapping) for item in operations
        ):
            raise TypeError("boundary_operations must be a JSON array of objects")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            calendar_id=_exact_string(payload["calendar_id"], "calendar_id"),
            synthetic_local_date=_exact_string(
                payload["synthetic_local_date"], "synthetic_local_date"
            ),
            timezone_name=_exact_string(payload["timezone_name"], "timezone_name"),
            phases=tuple(CalendarPhaseV1.from_dict(item) for item in phases),
            boundary_operations=tuple(
                BoundaryOperationV1.from_dict(item) for item in operations
            ),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> TradingDayCalendarV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


__all__ = [
    "BoundaryOperationV1",
    "CALENDAR_SCHEMA_VERSION",
    "CalendarPhaseV1",
    "LocalBoundaryV1",
    "PHASE_IDS",
    "TradingDayCalendarV1",
]
