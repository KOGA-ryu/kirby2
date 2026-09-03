"""Strict V1 command, advance, and current-frame wire records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .simulation_contract import (
    SimulationContractIntegrityError,
    _array,
    _enum,
    _exact,
    _freeze,
    _identifier,
    _integer,
    _object,
    _plain,
    _positive_integer,
    _snapshot,
    _text,
    canonical_digest,
)
from .simulation_live_contract import SimulationFrameV1


COMMAND_REQUEST_SCHEMA_ID = "KIRBY2_SIMULATION_COMMAND_REQUEST_V1"
COMMAND_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_COMMAND_RESULT_V1"
ADVANCE_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_ADVANCE_RESULT_V1"
CURRENT_FRAME_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_CURRENT_FRAME_RESULT_V1"
SCHEMA_VERSION = 1

COMMAND_RESULT_UNAVAILABLE_REASONS = frozenset(
    {
        "STALE_ORIGIN",
        "SOURCE_RUN_MISMATCH",
        "RUN_COMPLETE",
        "RUN_FINALIZED",
        "RESET_PENDING",
    }
)
ADVANCE_RESULT_UNAVAILABLE_REASONS = frozenset(
    {
        "STALE_ORIGIN",
        "SOURCE_RUN_MISMATCH",
        "RUN_NOT_RUNNING",
        "TARGET_NOT_AFTER_CURSOR",
        "RUN_COMPLETE",
        "RUN_FINALIZED",
        "RESET_PENDING",
    }
)
CURRENT_FRAME_UNAVAILABLE_REASONS = frozenset(
    {"SOURCE_RUN_MISMATCH", "RUN_ABANDONED"}
)

_RUN_ID = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_FRAME_ID = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_CURSOR_ID = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")
_COMMAND_ID = re.compile(r"simulation-command-[0-9a-f]{24}\Z")
_COMMAND_RESULT_ID = re.compile(r"simulation-command-result-[0-9a-f]{24}\Z")
_ADVANCE_RESULT_ID = re.compile(r"simulation-advance-result-[0-9a-f]{24}\Z")
_SEMANTIC_ACTION_ID = re.compile(r"[A-Z][A-Z0-9_]*\Z")

_COMMAND_REQUEST_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "command_id",
        "source_run_id",
        "origin_frame_id",
        "origin_cursor_id",
        "semantic_action_id",
        "parameters",
    }
)
_COMMAND_OUTCOME_FIELDS = frozenset(
    {
        "action_kind",
        "semantic_action_id",
        "accepted",
        "message",
        "rejection_reason",
        "input_sequence",
        "resulting_order_ids",
    }
)
_COMMAND_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "status",
        "command_id",
        "source_run_id",
        "origin_frame_id",
        "origin_cursor_id",
        "outcome",
        "destination_frame",
        "unavailable_reason",
    }
)
_ADVANCE_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "status",
        "source_run_id",
        "origin_frame_id",
        "origin_cursor_id",
        "target_time_us",
        "destination_frame",
        "unavailable_reason",
    }
)
_CURRENT_FRAME_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "status",
        "source_run_id",
        "current_frame",
        "unavailable_reason",
    }
)


def _schema(root: Mapping[str, object], schema_id: str, label: str) -> None:
    if (
        root["schema_id"] != schema_id
        or type(root["schema_version"]) is not int
        or root["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError(f"{label} schema is unsupported")


def _prefixed_id(value: object, pattern: re.Pattern[str], label: str) -> str:
    result = _text(value, label)
    if pattern.fullmatch(result) is None:
        raise ValueError(f"{label} has an invalid V1 form")
    return result


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_integer(value, label)


def _cursor(frame: SimulationFrameV1) -> Mapping[str, object]:
    cursor = frame.record["cursor"]
    if not isinstance(cursor, Mapping):
        raise SimulationContractIntegrityError("validated frame lost its cursor")
    return cursor


def _assert_destination_continuity(
    origin: SimulationFrameV1,
    destination: SimulationFrameV1,
) -> None:
    if destination.frame_sequence != origin.frame_sequence + 1:
        raise SimulationContractIntegrityError("destination frame sequence is not contiguous")
    if (
        destination.source_run_id != origin.source_run_id
        or destination.run_request_sha256 != origin.run_request_sha256
        or destination.resolved_configuration_sha256
        != origin.resolved_configuration_sha256
        or destination.profile_ref != origin.profile_ref
    ):
        raise SimulationContractIntegrityError("destination frame changed its run identity")
    before = _cursor(origin)
    after = _cursor(destination)
    for field in (
        "simulation_time_us",
        "input_sequence",
        "flow_sequence",
        "exchange_event_sequence",
        "trade_sequence",
    ):
        if int(after[field]) < int(before[field]):
            raise SimulationContractIntegrityError(
                f"destination frame regressed cursor field {field}"
            )


@dataclass(frozen=True, slots=True)
class SimulationCommandRequestV1:
    command_id: str
    source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    semantic_action_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationCommandRequestV1:
        root = _object(_snapshot(payload), "simulation command request")
        _exact(root, _COMMAND_REQUEST_FIELDS, "simulation command request")
        _schema(root, COMMAND_REQUEST_SCHEMA_ID, "simulation command request")
        parameters = _object(root["parameters"], "simulation command request.parameters")
        if parameters:
            raise ValueError("simulation command request parameters must be exactly empty")
        semantic_action_id = _identifier(
            root["semantic_action_id"], "simulation command request.semantic_action_id"
        )
        if _SEMANTIC_ACTION_ID.fullmatch(semantic_action_id) is None:
            raise ValueError("simulation command request semantic action ID is not canonical")
        normalized = {
            "schema_id": COMMAND_REQUEST_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "source_run_id": _prefixed_id(
                root["source_run_id"], _RUN_ID, "simulation command request.source_run_id"
            ),
            "origin_frame_id": _prefixed_id(
                root["origin_frame_id"],
                _FRAME_ID,
                "simulation command request.origin_frame_id",
            ),
            "origin_cursor_id": _prefixed_id(
                root["origin_cursor_id"],
                _CURSOR_ID,
                "simulation command request.origin_cursor_id",
            ),
            "semantic_action_id": semantic_action_id,
            "parameters": {},
        }
        command_id = _prefixed_id(
            root["command_id"], _COMMAND_ID, "simulation command request.command_id"
        )
        expected = f"simulation-command-{canonical_digest(normalized)[:24]}"
        if command_id != expected:
            raise SimulationContractIntegrityError(
                "simulation command ID does not match its content"
            )
        return cls(
            command_id,
            str(normalized["source_run_id"]),
            str(normalized["origin_frame_id"]),
            str(normalized["origin_cursor_id"]),
            semantic_action_id,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": COMMAND_REQUEST_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "command_id": self.command_id,
            "source_run_id": self.source_run_id,
            "origin_frame_id": self.origin_frame_id,
            "origin_cursor_id": self.origin_cursor_id,
            "semantic_action_id": self.semantic_action_id,
            "parameters": {},
        }


@dataclass(frozen=True, slots=True)
class SimulationCommandOutcomeV1:
    action_kind: str
    semantic_action_id: str
    accepted: bool
    message: str
    rejection_reason: str | None
    input_sequence: int | None
    resulting_order_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationCommandOutcomeV1:
        root = _object(_snapshot(payload), "simulation command outcome")
        _exact(root, _COMMAND_OUTCOME_FIELDS, "simulation command outcome")
        action_kind = _enum(
            root["action_kind"],
            frozenset({"PLAYER_ACTION", "LIFECYCLE"}),
            "simulation command outcome.action_kind",
        )
        semantic_action_id = _identifier(
            root["semantic_action_id"], "simulation command outcome.semantic_action_id"
        )
        if _SEMANTIC_ACTION_ID.fullmatch(semantic_action_id) is None:
            raise ValueError("simulation command outcome semantic action ID is not canonical")
        accepted = root["accepted"]
        if type(accepted) is not bool:
            raise ValueError("simulation command outcome.accepted must be boolean")
        rejection = _optional_text(
            root["rejection_reason"], "simulation command outcome.rejection_reason"
        )
        if accepted != (rejection is None):
            raise ValueError("simulation command outcome acceptance fields disagree")
        input_sequence = _optional_positive_integer(
            root["input_sequence"], "simulation command outcome.input_sequence"
        )
        order_ids = tuple(
            _identifier(item, f"simulation command outcome.resulting_order_ids[{index}]")
            for index, item in enumerate(
                _array(
                    root["resulting_order_ids"],
                    "simulation command outcome.resulting_order_ids",
                )
            )
        )
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("simulation command outcome order IDs must be unique")
        if action_kind == "LIFECYCLE":
            if (
                semantic_action_id not in {"SIMULATION_PLAY", "SIMULATION_PAUSE"}
                or input_sequence is not None
                or order_ids
            ):
                raise ValueError("lifecycle command outcome fields are inconsistent")
        elif input_sequence is None or semantic_action_id in {
            "SIMULATION_PLAY",
            "SIMULATION_PAUSE",
        }:
            raise ValueError("player command outcome fields are inconsistent")
        return cls(
            action_kind,
            semantic_action_id,
            accepted,
            _text(root["message"], "simulation command outcome.message"),
            rejection,
            input_sequence,
            order_ids,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "action_kind": self.action_kind,
            "semantic_action_id": self.semantic_action_id,
            "accepted": self.accepted,
            "message": self.message,
            "rejection_reason": self.rejection_reason,
            "input_sequence": self.input_sequence,
            "resulting_order_ids": list(self.resulting_order_ids),
        }


@dataclass(frozen=True, slots=True)
class SimulationCommandResultV1:
    result_id: str
    status: str
    command_id: str
    source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    outcome: SimulationCommandOutcomeV1 | None
    destination_frame: SimulationFrameV1 | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        request: SimulationCommandRequestV1 | None = None,
        origin_frame: SimulationFrameV1 | None = None,
    ) -> SimulationCommandResultV1:
        root = _object(_snapshot(payload), "simulation command result")
        _exact(root, _COMMAND_RESULT_FIELDS, "simulation command result")
        _schema(root, COMMAND_RESULT_SCHEMA_ID, "simulation command result")
        status = _enum(
            root["status"],
            frozenset({"AVAILABLE", "UNAVAILABLE"}),
            "simulation command result.status",
        )
        command_id = _prefixed_id(
            root["command_id"], _COMMAND_ID, "simulation command result.command_id"
        )
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation command result.source_run_id"
        )
        origin_frame_id = _prefixed_id(
            root["origin_frame_id"], _FRAME_ID, "simulation command result.origin_frame_id"
        )
        origin_cursor_id = _prefixed_id(
            root["origin_cursor_id"],
            _CURSOR_ID,
            "simulation command result.origin_cursor_id",
        )
        if request is not None and (
            command_id != request.command_id
            or source_run_id != request.source_run_id
            or origin_frame_id != request.origin_frame_id
            or origin_cursor_id != request.origin_cursor_id
        ):
            raise SimulationContractIntegrityError(
                "simulation command result does not echo its request"
            )
        if origin_frame is not None:
            origin_cursor = _cursor(origin_frame)
            if (
                source_run_id != origin_frame.source_run_id
                or origin_frame_id != origin_frame.frame_id
                or origin_cursor_id != origin_cursor["cursor_id"]
            ):
                raise SimulationContractIntegrityError(
                    "simulation command result does not echo its origin"
                )
        if status == "AVAILABLE":
            if (
                root["outcome"] is None
                or root["destination_frame"] is None
                or root["unavailable_reason"] is not None
            ):
                raise ValueError("available command result nullability is invalid")
            outcome = SimulationCommandOutcomeV1.from_dict(
                _object(root["outcome"], "simulation command result.outcome")
            )
            destination = SimulationFrameV1.from_dict(
                _object(
                    root["destination_frame"],
                    "simulation command result.destination_frame",
                )
            )
            if destination.source_run_id != source_run_id:
                raise SimulationContractIntegrityError(
                    "command destination frame changed its source run"
                )
            if request is not None and outcome.semantic_action_id != request.semantic_action_id:
                raise SimulationContractIntegrityError(
                    "command outcome changed the requested semantic action"
                )
            if origin_frame is not None:
                _assert_destination_continuity(origin_frame, destination)
                before = _cursor(origin_frame)
                after = _cursor(destination)
                if after["simulation_time_us"] != before["simulation_time_us"]:
                    raise SimulationContractIntegrityError(
                        "command unexpectedly advanced simulation time"
                    )
                expected_input = int(before["input_sequence"])
                if outcome.action_kind == "PLAYER_ACTION":
                    expected_input += 1
                if int(after["input_sequence"]) != expected_input:
                    raise SimulationContractIntegrityError(
                        "command destination input sequence is inconsistent"
                    )
            if outcome.action_kind == "PLAYER_ACTION" and (
                outcome.input_sequence != _cursor(destination)["input_sequence"]
            ):
                raise SimulationContractIntegrityError(
                    "player command outcome input sequence differs from its frame"
                )
            unavailable_reason = None
        else:
            if root["outcome"] is not None or root["destination_frame"] is not None:
                raise ValueError("unavailable command result must not carry a frame or outcome")
            outcome = None
            destination = None
            unavailable_reason = _enum(
                root["unavailable_reason"],
                COMMAND_RESULT_UNAVAILABLE_REASONS,
                "simulation command result.unavailable_reason",
            )
        normalized = {
            "schema_id": COMMAND_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "command_id": command_id,
            "source_run_id": source_run_id,
            "origin_frame_id": origin_frame_id,
            "origin_cursor_id": origin_cursor_id,
            "outcome": None if outcome is None else outcome.as_dict(),
            "destination_frame": None if destination is None else destination.as_dict(),
            "unavailable_reason": unavailable_reason,
        }
        result_id = _prefixed_id(
            root["result_id"], _COMMAND_RESULT_ID, "simulation command result.result_id"
        )
        if result_id != f"simulation-command-result-{canonical_digest(normalized)[:24]}":
            raise SimulationContractIntegrityError(
                "simulation command result ID does not match its content"
            )
        record = {**normalized, "result_id": result_id}
        return cls(
            result_id,
            status,
            command_id,
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            outcome,
            destination,
            unavailable_reason,
            _freeze(record),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationAdvanceResultV1:
    result_id: str
    status: str
    source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    target_time_us: int
    destination_frame: SimulationFrameV1 | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        origin_frame: SimulationFrameV1 | None = None,
    ) -> SimulationAdvanceResultV1:
        root = _object(_snapshot(payload), "simulation advance result")
        _exact(root, _ADVANCE_RESULT_FIELDS, "simulation advance result")
        _schema(root, ADVANCE_RESULT_SCHEMA_ID, "simulation advance result")
        status = _enum(
            root["status"],
            frozenset({"AVAILABLE", "UNAVAILABLE"}),
            "simulation advance result.status",
        )
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation advance result.source_run_id"
        )
        origin_frame_id = _prefixed_id(
            root["origin_frame_id"], _FRAME_ID, "simulation advance result.origin_frame_id"
        )
        origin_cursor_id = _prefixed_id(
            root["origin_cursor_id"], _CURSOR_ID, "simulation advance result.origin_cursor_id"
        )
        target_time_us = _integer(
            root["target_time_us"], "simulation advance result.target_time_us", minimum=0
        )
        if origin_frame is not None:
            origin_cursor = _cursor(origin_frame)
            if (
                source_run_id != origin_frame.source_run_id
                or origin_frame_id != origin_frame.frame_id
                or origin_cursor_id != origin_cursor["cursor_id"]
            ):
                raise SimulationContractIntegrityError(
                    "simulation advance result does not echo its origin"
                )
        if status == "AVAILABLE":
            if root["destination_frame"] is None or root["unavailable_reason"] is not None:
                raise ValueError("available advance result nullability is invalid")
            destination = SimulationFrameV1.from_dict(
                _object(
                    root["destination_frame"],
                    "simulation advance result.destination_frame",
                )
            )
            if destination.source_run_id != source_run_id:
                raise SimulationContractIntegrityError(
                    "advance destination frame changed its source run"
                )
            if origin_frame is not None:
                _assert_destination_continuity(origin_frame, destination)
                before = _cursor(origin_frame)
                after = _cursor(destination)
                if (
                    before["run_state"] != "RUNNING"
                    or target_time_us <= int(before["simulation_time_us"])
                ):
                    raise SimulationContractIntegrityError(
                        "available advance did not originate from a running earlier cursor"
                    )
                expected_time = min(target_time_us, int(after["duration_us"]))
                if (
                    int(after["simulation_time_us"]) != expected_time
                    or int(after["input_sequence"]) != int(before["input_sequence"])
                ):
                    raise SimulationContractIntegrityError(
                        "advance destination time or input sequence is inconsistent"
                    )
            unavailable_reason = None
        else:
            if root["destination_frame"] is not None:
                raise ValueError("unavailable advance result must not carry a frame")
            destination = None
            unavailable_reason = _enum(
                root["unavailable_reason"],
                ADVANCE_RESULT_UNAVAILABLE_REASONS,
                "simulation advance result.unavailable_reason",
            )
        normalized = {
            "schema_id": ADVANCE_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "source_run_id": source_run_id,
            "origin_frame_id": origin_frame_id,
            "origin_cursor_id": origin_cursor_id,
            "target_time_us": target_time_us,
            "destination_frame": None if destination is None else destination.as_dict(),
            "unavailable_reason": unavailable_reason,
        }
        result_id = _prefixed_id(
            root["result_id"], _ADVANCE_RESULT_ID, "simulation advance result.result_id"
        )
        if result_id != f"simulation-advance-result-{canonical_digest(normalized)[:24]}":
            raise SimulationContractIntegrityError(
                "simulation advance result ID does not match its content"
            )
        record = {**normalized, "result_id": result_id}
        return cls(
            result_id,
            status,
            source_run_id,
            origin_frame_id,
            origin_cursor_id,
            target_time_us,
            destination,
            unavailable_reason,
            _freeze(record),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationCurrentFrameResultV1:
    status: str
    source_run_id: str
    current_frame: SimulationFrameV1 | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> SimulationCurrentFrameResultV1:
        root = _object(_snapshot(payload), "simulation current-frame result")
        _exact(root, _CURRENT_FRAME_RESULT_FIELDS, "simulation current-frame result")
        _schema(root, CURRENT_FRAME_RESULT_SCHEMA_ID, "simulation current-frame result")
        status = _enum(
            root["status"],
            frozenset({"AVAILABLE", "UNAVAILABLE"}),
            "simulation current-frame result.status",
        )
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation current-frame result.source_run_id"
        )
        if status == "AVAILABLE":
            if root["current_frame"] is None or root["unavailable_reason"] is not None:
                raise ValueError("available current-frame result nullability is invalid")
            frame = SimulationFrameV1.from_dict(
                _object(
                    root["current_frame"],
                    "simulation current-frame result.current_frame",
                )
            )
            if frame.source_run_id != source_run_id:
                raise SimulationContractIntegrityError(
                    "current-frame result changed its source run"
                )
            unavailable_reason = None
        else:
            if root["current_frame"] is not None:
                raise ValueError("unavailable current-frame result must not carry a frame")
            frame = None
            unavailable_reason = _enum(
                root["unavailable_reason"],
                CURRENT_FRAME_UNAVAILABLE_REASONS,
                "simulation current-frame result.unavailable_reason",
            )
        normalized = {
            "schema_id": CURRENT_FRAME_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "source_run_id": source_run_id,
            "current_frame": None if frame is None else frame.as_dict(),
            "unavailable_reason": unavailable_reason,
        }
        return cls(status, source_run_id, frame, unavailable_reason, _freeze(normalized))

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


__all__ = [
    "ADVANCE_RESULT_SCHEMA_ID",
    "COMMAND_REQUEST_SCHEMA_ID",
    "COMMAND_RESULT_SCHEMA_ID",
    "CURRENT_FRAME_RESULT_SCHEMA_ID",
    "SimulationAdvanceResultV1",
    "SimulationCommandOutcomeV1",
    "SimulationCommandRequestV1",
    "SimulationCommandResultV1",
    "SimulationCurrentFrameResultV1",
]
