"""Strict V1 reset and close lifecycle wire records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .simulation_contract import (
    SimulationContractIntegrityError,
    _digest,
    _enum,
    _exact,
    _freeze,
    _object,
    _plain,
    _snapshot,
    _text,
    canonical_digest,
)
from .simulation_live_contract import SimulationFrameV1


RESET_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_RESET_RESULT_V1"
RESET_COMMIT_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_RESET_COMMIT_RESULT_V1"
CLOSE_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_CLOSE_RESULT_V1"
SCHEMA_VERSION = 1

RESET_UNAVAILABLE_REASONS = frozenset(
    {"STALE_ORIGIN", "SOURCE_RUN_MISMATCH", "RUN_FINALIZED", "RESET_PENDING"}
)
RESET_COMMIT_UNAVAILABLE_REASONS = frozenset(
    {"UNKNOWN_RESET_TOKEN", "RESET_TOKEN_MISMATCH"}
)
CLOSE_DISPOSITIONS = frozenset(
    {
        "UNPUBLISHED_START_REJECTED_BY_UI",
        "INTEGRITY_LOCKED",
        "USER_ABANDONED",
    }
)
CLOSE_UNAVAILABLE_REASONS = frozenset(
    {"RUN_FINALIZED", "DISPOSITION_MISMATCH"}
)

_RUN_ID = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_FRAME_ID = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_CURSOR_ID = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")
_RESET_TOKEN_ID = re.compile(r"simulation-reset-token-[0-9a-f]{32}\Z")
_RESET_RESULT_ID = re.compile(r"simulation-reset-result-[0-9a-f]{24}\Z")

_RESET_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "status",
        "previous_source_run_id",
        "origin_frame_id",
        "origin_cursor_id",
        "reset_token_id",
        "previous_run_disposition_on_commit",
        "new_source_run_id",
        "run_request_sha256",
        "initial_frame",
        "unavailable_reason",
    }
)
_RESET_COMMIT_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "status",
        "reset_token_id",
        "previous_source_run_id",
        "new_source_run_id",
        "initial_frame_id",
        "previous_run_disposition",
        "unavailable_reason",
    }
)
_CLOSE_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "status",
        "source_run_id",
        "disposition",
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


def _optional_prefixed_id(
    value: object,
    pattern: re.Pattern[str],
    label: str,
) -> str | None:
    if value is None:
        return None
    return _prefixed_id(value, pattern, label)


def _optional_digest(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _digest(value, label)


@dataclass(frozen=True, slots=True)
class SimulationResetResultV1:
    result_id: str
    status: str
    previous_source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    reset_token_id: str | None
    previous_run_disposition_on_commit: str | None
    new_source_run_id: str | None
    run_request_sha256: str | None
    initial_frame: SimulationFrameV1 | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        origin_frame: SimulationFrameV1 | None = None,
    ) -> SimulationResetResultV1:
        root = _object(_snapshot(payload), "simulation reset result")
        _exact(root, _RESET_RESULT_FIELDS, "simulation reset result")
        _schema(root, RESET_RESULT_SCHEMA_ID, "simulation reset result")
        status = _enum(
            root["status"],
            frozenset({"AVAILABLE", "UNAVAILABLE"}),
            "simulation reset result.status",
        )
        previous_source_run_id = _prefixed_id(
            root["previous_source_run_id"],
            _RUN_ID,
            "simulation reset result.previous_source_run_id",
        )
        origin_frame_id = _prefixed_id(
            root["origin_frame_id"],
            _FRAME_ID,
            "simulation reset result.origin_frame_id",
        )
        origin_cursor_id = _prefixed_id(
            root["origin_cursor_id"],
            _CURSOR_ID,
            "simulation reset result.origin_cursor_id",
        )
        if origin_frame is not None:
            cursor = origin_frame.as_dict()["cursor"]
            if (
                previous_source_run_id != origin_frame.source_run_id
                or origin_frame_id != origin_frame.frame_id
                or origin_cursor_id != cursor["cursor_id"]
            ):
                raise SimulationContractIntegrityError(
                    "simulation reset result does not echo its origin"
                )
        reset_token_id = _optional_prefixed_id(
            root["reset_token_id"],
            _RESET_TOKEN_ID,
            "simulation reset result.reset_token_id",
        )
        new_source_run_id = _optional_prefixed_id(
            root["new_source_run_id"],
            _RUN_ID,
            "simulation reset result.new_source_run_id",
        )
        run_request_sha256 = _optional_digest(
            root["run_request_sha256"],
            "simulation reset result.run_request_sha256",
        )
        disposition = root["previous_run_disposition_on_commit"]
        if status == "AVAILABLE":
            if (
                reset_token_id is None
                or new_source_run_id is None
                or run_request_sha256 is None
                or root["initial_frame"] is None
                or disposition != "ABANDONED_BY_RESET"
                or root["unavailable_reason"] is not None
            ):
                raise ValueError("available simulation reset result nullability is invalid")
            if new_source_run_id == previous_source_run_id:
                raise SimulationContractIntegrityError(
                    "simulation reset reused the previous source run ID"
                )
            initial_frame = SimulationFrameV1.from_dict(
                _object(root["initial_frame"], "simulation reset result.initial_frame")
            )
            cursor = initial_frame.as_dict()["cursor"]
            if (
                initial_frame.source_run_id != new_source_run_id
                or initial_frame.run_request_sha256 != run_request_sha256
                or initial_frame.frame_sequence != 1
                or cursor["simulation_time_us"] != 0
                or cursor["run_state"] not in {"READY", "RUNNING"}
                or any(
                    cursor[field] != 0
                    for field in (
                        "input_sequence",
                        "flow_sequence",
                        "trade_sequence",
                    )
                )
            ):
                raise SimulationContractIntegrityError(
                    "simulation reset initial frame is not a fresh replacement"
                )
            if origin_frame is not None and (
                initial_frame.run_request_sha256 != origin_frame.run_request_sha256
                or initial_frame.resolved_configuration_sha256
                != origin_frame.resolved_configuration_sha256
                or initial_frame.profile_ref != origin_frame.profile_ref
            ):
                raise SimulationContractIntegrityError(
                    "simulation reset changed the pinned run identity"
                )
            unavailable_reason = None
        else:
            if (
                reset_token_id is not None
                or disposition is not None
                or new_source_run_id is not None
                or run_request_sha256 is not None
                or root["initial_frame"] is not None
            ):
                raise ValueError("unavailable simulation reset result carries replacement state")
            initial_frame = None
            unavailable_reason = _enum(
                root["unavailable_reason"],
                RESET_UNAVAILABLE_REASONS,
                "simulation reset result.unavailable_reason",
            )
        normalized = {
            "schema_id": RESET_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "previous_source_run_id": previous_source_run_id,
            "origin_frame_id": origin_frame_id,
            "origin_cursor_id": origin_cursor_id,
            "reset_token_id": reset_token_id,
            "previous_run_disposition_on_commit": (
                "ABANDONED_BY_RESET" if status == "AVAILABLE" else None
            ),
            "new_source_run_id": new_source_run_id,
            "run_request_sha256": run_request_sha256,
            "initial_frame": None if initial_frame is None else initial_frame.as_dict(),
            "unavailable_reason": unavailable_reason,
        }
        result_id = _prefixed_id(
            root["result_id"], _RESET_RESULT_ID, "simulation reset result.result_id"
        )
        if result_id != f"simulation-reset-result-{canonical_digest(normalized)[:24]}":
            raise SimulationContractIntegrityError(
                "simulation reset result ID does not match its content"
            )
        record = {**normalized, "result_id": result_id}
        return cls(
            result_id,
            status,
            previous_source_run_id,
            origin_frame_id,
            origin_cursor_id,
            reset_token_id,
            normalized["previous_run_disposition_on_commit"],
            new_source_run_id,
            run_request_sha256,
            initial_frame,
            unavailable_reason,
            _freeze(record),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationResetCommitResultV1:
    status: str
    reset_token_id: str
    previous_source_run_id: str
    new_source_run_id: str | None
    initial_frame_id: str | None
    previous_run_disposition: str | None
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> SimulationResetCommitResultV1:
        root = _object(_snapshot(payload), "simulation reset commit result")
        _exact(root, _RESET_COMMIT_RESULT_FIELDS, "simulation reset commit result")
        _schema(root, RESET_COMMIT_RESULT_SCHEMA_ID, "simulation reset commit result")
        status = _enum(
            root["status"],
            frozenset({"COMMITTED", "UNAVAILABLE"}),
            "simulation reset commit result.status",
        )
        reset_token_id = _prefixed_id(
            root["reset_token_id"],
            _RESET_TOKEN_ID,
            "simulation reset commit result.reset_token_id",
        )
        previous_source_run_id = _prefixed_id(
            root["previous_source_run_id"],
            _RUN_ID,
            "simulation reset commit result.previous_source_run_id",
        )
        new_source_run_id = _optional_prefixed_id(
            root["new_source_run_id"],
            _RUN_ID,
            "simulation reset commit result.new_source_run_id",
        )
        initial_frame_id = _optional_prefixed_id(
            root["initial_frame_id"],
            _FRAME_ID,
            "simulation reset commit result.initial_frame_id",
        )
        disposition = root["previous_run_disposition"]
        if status == "COMMITTED":
            if (
                new_source_run_id is None
                or initial_frame_id is None
                or disposition != "ABANDONED_BY_RESET"
                or root["unavailable_reason"] is not None
                or new_source_run_id == previous_source_run_id
            ):
                raise ValueError("committed simulation reset fields are invalid")
            unavailable_reason = None
        else:
            if (
                new_source_run_id is not None
                or initial_frame_id is not None
                or disposition is not None
            ):
                raise ValueError("unavailable reset commit carries destination state")
            unavailable_reason = _enum(
                root["unavailable_reason"],
                RESET_COMMIT_UNAVAILABLE_REASONS,
                "simulation reset commit result.unavailable_reason",
            )
        normalized = {
            "schema_id": RESET_COMMIT_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "reset_token_id": reset_token_id,
            "previous_source_run_id": previous_source_run_id,
            "new_source_run_id": new_source_run_id,
            "initial_frame_id": initial_frame_id,
            "previous_run_disposition": (
                "ABANDONED_BY_RESET" if status == "COMMITTED" else None
            ),
            "unavailable_reason": unavailable_reason,
        }
        return cls(
            status,
            reset_token_id,
            previous_source_run_id,
            new_source_run_id,
            initial_frame_id,
            normalized["previous_run_disposition"],
            unavailable_reason,
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


@dataclass(frozen=True, slots=True)
class SimulationCloseResultV1:
    status: str
    source_run_id: str
    disposition: str
    unavailable_reason: str | None
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationCloseResultV1:
        root = _object(_snapshot(payload), "simulation close result")
        _exact(root, _CLOSE_RESULT_FIELDS, "simulation close result")
        _schema(root, CLOSE_RESULT_SCHEMA_ID, "simulation close result")
        status = _enum(
            root["status"],
            frozenset({"CLOSED", "UNAVAILABLE"}),
            "simulation close result.status",
        )
        source_run_id = _prefixed_id(
            root["source_run_id"], _RUN_ID, "simulation close result.source_run_id"
        )
        disposition = _enum(
            root["disposition"], CLOSE_DISPOSITIONS, "simulation close result.disposition"
        )
        if status == "CLOSED":
            if root["unavailable_reason"] is not None:
                raise ValueError("closed simulation result must not have an unavailable reason")
            unavailable_reason = None
        else:
            unavailable_reason = _enum(
                root["unavailable_reason"],
                CLOSE_UNAVAILABLE_REASONS,
                "simulation close result.unavailable_reason",
            )
        normalized = {
            "schema_id": CLOSE_RESULT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "source_run_id": source_run_id,
            "disposition": disposition,
            "unavailable_reason": unavailable_reason,
        }
        return cls(
            status,
            source_run_id,
            disposition,
            unavailable_reason,
            _freeze(normalized),
        )

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


__all__ = [
    "CLOSE_DISPOSITIONS",
    "CLOSE_RESULT_SCHEMA_ID",
    "RESET_COMMIT_RESULT_SCHEMA_ID",
    "RESET_RESULT_SCHEMA_ID",
    "SimulationCloseResultV1",
    "SimulationResetCommitResultV1",
    "SimulationResetResultV1",
]
