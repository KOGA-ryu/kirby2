"""Strict public records for a catalog-authored prepared simulation episode."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .simulation_contract import (
    SimulationContractIntegrityError,
    SimulationProfileCatalogV1,
    SimulationProfileRefV1,
    SimulationProfileResolutionV1,
    canonical_digest,
)
from .simulation_live_contract import (
    SimulationFrameV1,
    SimulationStartResultV1,
    SimulationTrainingOptionsV1,
)
from .simulation_facade import list_simulation_profiles


EPISODE_IDENTITY_SCHEMA_ID = "KIRBY2_SIMULATION_EPISODE_IDENTITY_V1"
EPISODE_REQUEST_SCHEMA_ID = "KIRBY2_SIMULATION_EPISODE_PREPARATION_REQUEST_V1"
EPISODE_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_EPISODE_PREPARED_RESULT_V1"
EPISODE_REFUSAL_SCHEMA_ID = "KIRBY2_SIMULATION_EPISODE_REFUSAL_V1"
EPISODE_PROJECTION_SCHEMA_ID = "KIRBY2_SIMULATION_EPISODE_PREFIX_PROJECTION_V1"
EPISODE_VERIFICATION_SCHEMA_ID = "KIRBY2_SIMULATION_EPISODE_VERIFICATION_V1"
FULL_MODEL_PREFIX_PROJECTION_ID = "KIRBY2_SIMULATION_FULL_MODEL_PREFIX_PROJECTION_V1"
EPISODE_PREFIX_TIMING_POLICY = "ACTIONS_AT_T0_THEN_ADVANCE_TO_ANCHOR_V1"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUEST_ID = re.compile(r"simulation-episode-request-[0-9a-f]{24}\Z")
_RESULT_ID = re.compile(r"simulation-episode-prepared-result-[0-9a-f]{24}\Z")
_VERIFICATION_ID = re.compile(r"simulation-episode-verification-[0-9a-f]{24}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ACTIONS = re.compile(r"[A-Z][A-Z0-9_]{1,127}\Z")

_IDENTITY_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "episode_id",
        "episode_version",
        "recipe_sha256",
        "profile_ref",
        "resolved_configuration_sha256",
        "seed",
        "training_options_sha256",
        "prefix_sha256",
        "prefix_timing_policy",
        "anchor_time_us",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "request_id",
        "identity",
        "resolution",
        "training_options",
        "prefix_actions",
        "prefix_timing_policy",
    }
)
_REFUSAL_FIELDS = frozenset({"schema_id", "schema_version", "reason_code", "explanation"})
_VERIFICATION_FIELDS = frozenset(
    {"schema_id", "schema_version", "verification_id", "status", "prepared_result_id", "reason"}
)
_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "result_id",
        "status",
        "request_id",
        "identity",
        "original_start_result",
        "current_frame",
        "prefix_actions",
        "prefix_projection_sha256",
        "full_model_projection_id",
        "full_model_prefix_sha256",
        "resource_ownership",
        "cleanup_disposition",
        "refusal",
    }
)
_REFUSAL_REASONS = frozenset(
    {
        "INVALID_REQUEST",
        "RESOLUTION_NOT_AVAILABLE",
        "START_REFUSED",
        "PREFIX_REJECTED",
        "ANCHOR_INVALID",
        "PREPARATION_FAILED",
        "CLEANUP_UNCONFIRMED",
    }
)
_RESOURCE_OWNERSHIPS = frozenset(
    {"NO_RESOURCE", "CALLER_OWNS_ACTIVE_HANDLE", "CALLER_OWNS_CLEANUP_HANDLE"}
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _exact(value: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 contract")


def _text(value: object, label: str) -> str:
    if type(value) is not str or value != value.strip() or not value:
        raise ValueError(f"{label} must be nonempty normalized text")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{label} is not a V1 identifier")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} is not a SHA-256 digest")
    return value


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _prefix_actions(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("episode prefix actions must be an ordered array")
    actions = tuple(value)
    if not actions:
        raise ValueError("episode prefix actions must not be empty")
    normalized: list[str] = []
    for index, action in enumerate(actions):
        if type(action) is not str or _ACTIONS.fullmatch(action) is None:
            raise ValueError(f"episode prefix action {index} is not a canonical semantic action")
        normalized.append(action)
    return tuple(normalized)


def _resolution(value: object, label: str) -> SimulationProfileResolutionV1:
    return SimulationProfileResolutionV1.from_dict(
        _object(value, label),
        catalog=SimulationProfileCatalogV1.from_dict(list_simulation_profiles()),
    )


@dataclass(frozen=True, slots=True)
class SimulationEpisodeIdentityV1:
    episode_id: str
    episode_version: int
    recipe_sha256: str
    profile_ref: SimulationProfileRefV1
    resolved_configuration_sha256: str
    seed: int
    training_options_sha256: str
    prefix_sha256: str
    prefix_timing_policy: str
    anchor_time_us: int

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationEpisodeIdentityV1:
        root = _object(payload, "simulation episode identity")
        _exact(root, _IDENTITY_FIELDS, "simulation episode identity")
        if root["schema_id"] != EPISODE_IDENTITY_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation episode identity schema is unsupported")
        profile_ref = SimulationProfileRefV1.from_dict(
            _object(root["profile_ref"], "simulation episode identity.profile_ref")
        )
        episode_id = _identifier(root["episode_id"], "simulation episode identity.episode_id")
        episode_version = _positive_integer(
            root["episode_version"], "simulation episode identity.episode_version"
        )
        configuration_sha256 = _digest(
            root["resolved_configuration_sha256"],
            "simulation episode identity.resolved_configuration_sha256",
        )
        seed = _nonnegative_integer(root["seed"], "simulation episode identity.seed")
        training_sha256 = _digest(
            root["training_options_sha256"],
            "simulation episode identity.training_options_sha256",
        )
        prefix_sha256 = _digest(
            root["prefix_sha256"], "simulation episode identity.prefix_sha256"
        )
        prefix_timing_policy = root["prefix_timing_policy"]
        if prefix_timing_policy != EPISODE_PREFIX_TIMING_POLICY:
            raise ValueError("simulation episode identity timing policy is unsupported")
        anchor_time_us = _positive_integer(
            root["anchor_time_us"], "simulation episode identity.anchor_time_us"
        )
        recipe_basis = {
            "schema_id": EPISODE_IDENTITY_SCHEMA_ID,
            "schema_version": 1,
            "episode_id": episode_id,
            "episode_version": episode_version,
            "profile_ref": profile_ref.as_dict(),
            "resolved_configuration_sha256": configuration_sha256,
            "seed": seed,
            "training_options_sha256": training_sha256,
            "prefix_sha256": prefix_sha256,
            "prefix_timing_policy": prefix_timing_policy,
            "anchor_time_us": anchor_time_us,
        }
        recipe_sha256 = _digest(root["recipe_sha256"], "simulation episode identity.recipe_sha256")
        if recipe_sha256 != canonical_digest(recipe_basis):
            raise SimulationContractIntegrityError("episode recipe digest does not match its identity")
        return cls(
            episode_id,
            episode_version,
            recipe_sha256,
            profile_ref,
            configuration_sha256,
            seed,
            training_sha256,
            prefix_sha256,
            prefix_timing_policy,
            anchor_time_us,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": EPISODE_IDENTITY_SCHEMA_ID,
            "schema_version": 1,
            "episode_id": self.episode_id,
            "episode_version": self.episode_version,
            "recipe_sha256": self.recipe_sha256,
            "profile_ref": self.profile_ref.as_dict(),
            "resolved_configuration_sha256": self.resolved_configuration_sha256,
            "seed": self.seed,
            "training_options_sha256": self.training_options_sha256,
            "prefix_sha256": self.prefix_sha256,
            "prefix_timing_policy": self.prefix_timing_policy,
            "anchor_time_us": self.anchor_time_us,
        }


@dataclass(frozen=True, slots=True)
class SimulationEpisodePreparationRequestV1:
    request_id: str
    identity: SimulationEpisodeIdentityV1
    resolution: SimulationProfileResolutionV1
    training_options: SimulationTrainingOptionsV1
    prefix_actions: tuple[str, ...]
    prefix_timing_policy: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationEpisodePreparationRequestV1:
        root = _object(payload, "simulation episode preparation request")
        _exact(root, _REQUEST_FIELDS, "simulation episode preparation request")
        if root["schema_id"] != EPISODE_REQUEST_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation episode preparation request schema is unsupported")
        identity = SimulationEpisodeIdentityV1.from_dict(
            _object(root["identity"], "simulation episode preparation request.identity")
        )
        resolution = _resolution(
            root["resolution"], "simulation episode preparation request.resolution"
        )
        training = SimulationTrainingOptionsV1.from_dict(
            _object(root["training_options"], "simulation episode preparation request.training_options")
        )
        actions = _prefix_actions(root["prefix_actions"])
        timing_policy = root["prefix_timing_policy"]
        if (
            timing_policy != EPISODE_PREFIX_TIMING_POLICY
            or identity.prefix_timing_policy != timing_policy
        ):
            raise ValueError("episode preparation timing policy is unsupported")
        if resolution.status != "AVAILABLE" or resolution.resolved_configuration is None:
            raise ValueError("episode preparation requires an available profile resolution")
        configuration = resolution.resolved_configuration
        if (
            identity.profile_ref != resolution.selection.profile_ref
            or identity.resolved_configuration_sha256
            != resolution.resolved_configuration_sha256
            or identity.seed != configuration.seed
            or identity.training_options_sha256 != canonical_digest(training.as_dict())
            or identity.prefix_sha256 != canonical_digest(list(actions))
            or identity.anchor_time_us >= configuration.duration_us
        ):
            raise SimulationContractIntegrityError(
                "episode preparation identity does not match its request dependencies"
            )
        if training.initial_run_state != "READY":
            raise ValueError("episode preparation requires an ordinary READY Start record")
        basis = {
            "schema_id": EPISODE_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "identity": identity.as_dict(),
            "resolution": resolution.as_dict(),
            "training_options": training.as_dict(),
            "prefix_actions": list(actions),
            "prefix_timing_policy": timing_policy,
        }
        request_id = root["request_id"]
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("simulation episode preparation request ID is invalid")
        if request_id != f"simulation-episode-request-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("episode preparation request ID does not match its content")
        return cls(request_id, identity, resolution, training, actions, timing_policy)

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": EPISODE_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "identity": self.identity.as_dict(),
            "resolution": self.resolution.as_dict(),
            "training_options": self.training_options.as_dict(),
            "prefix_actions": list(self.prefix_actions),
            "prefix_timing_policy": self.prefix_timing_policy,
        }
        return {**basis, "request_id": self.request_id}


@dataclass(frozen=True, slots=True)
class SimulationEpisodeRefusalV1:
    reason_code: str
    explanation: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationEpisodeRefusalV1:
        root = _object(payload, "simulation episode refusal")
        _exact(root, _REFUSAL_FIELDS, "simulation episode refusal")
        if root["schema_id"] != EPISODE_REFUSAL_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation episode refusal schema is unsupported")
        reason = _text(root["reason_code"], "simulation episode refusal.reason_code")
        if reason not in _REFUSAL_REASONS:
            raise ValueError("simulation episode refusal reason is unsupported")
        return cls(reason, _text(root["explanation"], "simulation episode refusal.explanation"))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": EPISODE_REFUSAL_SCHEMA_ID,
            "schema_version": 1,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


def episode_prefix_projection(frame: SimulationFrameV1) -> dict[str, object]:
    """Return the public, source-independent model projection at one prepared cut."""

    record = frame.as_dict()
    cursor = dict(record["cursor"])
    cursor.pop("cursor_id")
    cursor.pop("source_run_id")
    market = dict(record["market_state"])
    market.pop("market_state_id")
    return {
        "schema_id": EPISODE_PROJECTION_SCHEMA_ID,
        "schema_version": 1,
        "profile_ref": record["profile_ref"],
        "resolved_configuration_sha256": record["resolved_configuration_sha256"],
        "run_request_sha256": record["run_request_sha256"],
        "cursor": cursor,
        "market_state": market,
        "instrument": record["instrument"],
        "clock": record["clock"],
        "book": record["book"],
        "recent_trades": record["recent_trades"],
        "working_orders": record["working_orders"],
        "account": record["account"],
        "strategy": record["strategy"],
        "objective": record["objective"],
        "diagnostics": record["diagnostics"],
        "metrics": record["metrics"],
        "status_message": record["status_message"],
        "status_role": record["status_role"],
        "provenance": record["provenance"],
    }


def episode_prefix_projection_sha256(
    frame: SimulationFrameV1,
    prefix_actions: Sequence[str],
) -> str:
    return canonical_digest(
        {"prefix_actions": list(prefix_actions), "model_projection": episode_prefix_projection(frame)}
    )


@dataclass(frozen=True, slots=True)
class SimulationEpisodePreparedResultV1:
    result_id: str
    status: str
    request_id: str | None
    identity: SimulationEpisodeIdentityV1 | None
    original_start_result: SimulationStartResultV1 | None
    current_frame: SimulationFrameV1 | None
    prefix_actions: tuple[str, ...] | None
    prefix_projection_sha256: str | None
    full_model_projection_id: str | None
    full_model_prefix_sha256: str | None
    resource_ownership: str
    cleanup_disposition: str | None
    refusal: SimulationEpisodeRefusalV1 | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationEpisodePreparedResultV1:
        root = _object(payload, "simulation episode prepared result")
        _exact(root, _RESULT_FIELDS, "simulation episode prepared result")
        if root["schema_id"] != EPISODE_RESULT_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation episode prepared result schema is unsupported")
        status = _text(root["status"], "simulation episode prepared result.status")
        if status not in {"AVAILABLE", "REFUSED"}:
            raise ValueError("simulation episode prepared result status is unsupported")
        ownership = _text(root["resource_ownership"], "simulation episode prepared result.resource_ownership")
        if ownership not in _RESOURCE_OWNERSHIPS:
            raise ValueError("simulation episode prepared result ownership is unsupported")
        cleanup = root["cleanup_disposition"]
        if cleanup is not None and cleanup != "USER_ABANDONED":
            raise ValueError("simulation episode cleanup disposition is unsupported")
        request_id = root["request_id"]
        if request_id is not None and (type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None):
            raise ValueError("simulation episode prepared result request ID is invalid")
        identity = (
            None
            if root["identity"] is None
            else SimulationEpisodeIdentityV1.from_dict(_object(root["identity"], "prepared result.identity"))
        )
        start = (
            None
            if root["original_start_result"] is None
            else SimulationStartResultV1.from_dict(
                _object(root["original_start_result"], "prepared result.original_start_result")
            )
        )
        frame = (
            None
            if root["current_frame"] is None
            else SimulationFrameV1.from_dict(_object(root["current_frame"], "prepared result.current_frame"))
        )
        prefix_actions = (
            None if root["prefix_actions"] is None else _prefix_actions(root["prefix_actions"])
        )
        projection_sha256 = (
            None
            if root["prefix_projection_sha256"] is None
            else _digest(root["prefix_projection_sha256"], "prepared result.prefix_projection_sha256")
        )
        full_model_projection_id = root["full_model_projection_id"]
        if full_model_projection_id is not None and full_model_projection_id != FULL_MODEL_PREFIX_PROJECTION_ID:
            raise ValueError("prepared result full-model projection is unsupported")
        full_model_prefix_sha256 = (
            None
            if root["full_model_prefix_sha256"] is None
            else _digest(root["full_model_prefix_sha256"], "prepared result.full_model_prefix_sha256")
        )
        refusal = (
            None
            if root["refusal"] is None
            else SimulationEpisodeRefusalV1.from_dict(_object(root["refusal"], "prepared result.refusal"))
        )
        if status == "AVAILABLE":
            if (
                request_id is None
                or identity is None
                or start is None
                or frame is None
                or prefix_actions is None
                or projection_sha256 is None
                or full_model_projection_id != FULL_MODEL_PREFIX_PROJECTION_ID
                or full_model_prefix_sha256 is None
                or refusal is not None
                or ownership != "CALLER_OWNS_ACTIVE_HANDLE"
                or cleanup is not None
            ):
                raise ValueError("available prepared result nullability is invalid")
            initial = start.initial_frame
            if (
                start.status != "AVAILABLE"
                or initial is None
                or initial.as_dict()["cursor"]["run_state"] != "READY"
                or initial.as_dict()["cursor"]["simulation_time_us"] != 0
                or frame.source_run_id != start.source_run_id
                or frame.as_dict()["cursor"]["run_state"] != "PAUSED"
                or frame.as_dict()["cursor"]["simulation_time_us"] != identity.anchor_time_us
                or frame.profile_ref != identity.profile_ref
                or frame.resolved_configuration_sha256 != identity.resolved_configuration_sha256
                or identity.prefix_sha256 != canonical_digest(list(prefix_actions))
                or projection_sha256 != episode_prefix_projection_sha256(frame, prefix_actions)
            ):
                raise SimulationContractIntegrityError("prepared episode does not preserve its public start/cut identities")
        else:
            if (
                start is not None
                or frame is not None
                or prefix_actions is not None
                or projection_sha256 is not None
                or full_model_projection_id is not None
                or full_model_prefix_sha256 is not None
                or refusal is None
                or (ownership == "NO_RESOURCE" and cleanup not in {None, "USER_ABANDONED"})
                or (ownership != "NO_RESOURCE" and cleanup != "USER_ABANDONED")
            ):
                raise ValueError("refused prepared result nullability is invalid")
        basis = {key: value for key, value in root.items() if key != "result_id"}
        result_id = root["result_id"]
        if type(result_id) is not str or _RESULT_ID.fullmatch(result_id) is None:
            raise ValueError("simulation episode prepared result ID is invalid")
        if result_id != f"simulation-episode-prepared-result-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("prepared result ID does not match its content")
        return cls(
            result_id,
            status,
            request_id,
            identity,
            start,
            frame,
            prefix_actions,
            projection_sha256,
            full_model_projection_id,
            full_model_prefix_sha256,
            ownership,
            cleanup,
            refusal,
        )

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": EPISODE_RESULT_SCHEMA_ID,
            "schema_version": 1,
            "status": self.status,
            "request_id": self.request_id,
            "identity": None if self.identity is None else self.identity.as_dict(),
            "original_start_result": (
                None if self.original_start_result is None else self.original_start_result.as_dict()
            ),
            "current_frame": None if self.current_frame is None else self.current_frame.as_dict(),
            "prefix_actions": None if self.prefix_actions is None else list(self.prefix_actions),
            "prefix_projection_sha256": self.prefix_projection_sha256,
            "full_model_projection_id": self.full_model_projection_id,
            "full_model_prefix_sha256": self.full_model_prefix_sha256,
            "resource_ownership": self.resource_ownership,
            "cleanup_disposition": self.cleanup_disposition,
            "refusal": None if self.refusal is None else self.refusal.as_dict(),
        }
        return {**basis, "result_id": self.result_id}


@dataclass(frozen=True, slots=True)
class SimulationEpisodeVerificationV1:
    verification_id: str
    status: str
    prepared_result_id: str
    reason: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationEpisodeVerificationV1:
        root = _object(payload, "simulation episode verification")
        _exact(root, _VERIFICATION_FIELDS, "simulation episode verification")
        if root["schema_id"] != EPISODE_VERIFICATION_SCHEMA_ID or root["schema_version"] != 1:
            raise ValueError("simulation episode verification schema is unsupported")
        status = _text(root["status"], "simulation episode verification.status")
        if status not in {"MATCH", "MISMATCH"}:
            raise ValueError("simulation episode verification status is unsupported")
        result_id = root["prepared_result_id"]
        if type(result_id) is not str or _RESULT_ID.fullmatch(result_id) is None:
            raise ValueError("simulation episode verification prepared result ID is invalid")
        reason = root["reason"]
        if status == "MATCH":
            if reason is not None:
                raise ValueError("matching episode verification must not carry a reason")
        elif type(reason) is not str or reason not in {
            "CURRENT_FRAME_CHANGED",
            "FULL_MODEL_PREFIX_MISMATCH",
        }:
            raise ValueError("mismatching episode verification reason is unsupported")
        basis = {key: value for key, value in root.items() if key != "verification_id"}
        verification_id = root["verification_id"]
        if type(verification_id) is not str or _VERIFICATION_ID.fullmatch(verification_id) is None:
            raise ValueError("simulation episode verification ID is invalid")
        if verification_id != f"simulation-episode-verification-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("episode verification ID does not match its content")
        return cls(verification_id, status, result_id, reason)

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": EPISODE_VERIFICATION_SCHEMA_ID,
            "schema_version": 1,
            "status": self.status,
            "prepared_result_id": self.prepared_result_id,
            "reason": self.reason,
        }
        return {
            **basis,
            "verification_id": f"simulation-episode-verification-{canonical_digest(basis)[:24]}",
        }


def build_simulation_episode_preparation_request(
    *,
    episode_id: str,
    episode_version: int,
    resolution_payload: Mapping[str, object],
    training_options_payload: Mapping[str, object],
    prefix_actions: Sequence[str],
    anchor_time_us: int,
) -> dict[str, object]:
    """Build one strict request without a caller reimplementing identity recipes."""

    resolution = _resolution(resolution_payload, "episode preparation resolution")
    training = SimulationTrainingOptionsV1.from_dict(training_options_payload)
    actions = _prefix_actions(prefix_actions)
    if resolution.status != "AVAILABLE" or resolution.resolved_configuration is None:
        raise ValueError("episode preparation requires an available profile resolution")
    configuration = resolution.resolved_configuration
    if training.initial_run_state != "READY":
        raise ValueError("episode preparation requires an ordinary READY Start record")
    if type(anchor_time_us) is not int or anchor_time_us <= 0 or anchor_time_us >= configuration.duration_us:
        raise ValueError("episode anchor must be a positive in-duration simulation time")
    profile_ref = resolution.selection.profile_ref
    identity_basis = {
        "schema_id": EPISODE_IDENTITY_SCHEMA_ID,
        "schema_version": 1,
        "episode_id": _identifier(episode_id, "episode ID"),
        "episode_version": _positive_integer(episode_version, "episode version"),
        "profile_ref": profile_ref.as_dict(),
        "resolved_configuration_sha256": resolution.resolved_configuration_sha256,
        "seed": configuration.seed,
        "training_options_sha256": canonical_digest(training.as_dict()),
        "prefix_sha256": canonical_digest(list(actions)),
        "prefix_timing_policy": EPISODE_PREFIX_TIMING_POLICY,
        "anchor_time_us": anchor_time_us,
    }
    identity = {
        **identity_basis,
        "recipe_sha256": canonical_digest(identity_basis),
    }
    request_basis = {
        "schema_id": EPISODE_REQUEST_SCHEMA_ID,
        "schema_version": 1,
        "identity": identity,
        "resolution": resolution.as_dict(),
        "training_options": training.as_dict(),
        "prefix_actions": list(actions),
        "prefix_timing_policy": EPISODE_PREFIX_TIMING_POLICY,
    }
    request = {
        **request_basis,
        "request_id": f"simulation-episode-request-{canonical_digest(request_basis)[:24]}",
    }
    return SimulationEpisodePreparationRequestV1.from_dict(request).as_dict()


__all__ = [
    "EPISODE_IDENTITY_SCHEMA_ID",
    "EPISODE_PROJECTION_SCHEMA_ID",
    "EPISODE_PREFIX_TIMING_POLICY",
    "EPISODE_REQUEST_SCHEMA_ID",
    "EPISODE_RESULT_SCHEMA_ID",
    "EPISODE_VERIFICATION_SCHEMA_ID",
    "FULL_MODEL_PREFIX_PROJECTION_ID",
    "SimulationEpisodeIdentityV1",
    "SimulationEpisodePreparationRequestV1",
    "SimulationEpisodePreparedResultV1",
    "SimulationEpisodeRefusalV1",
    "SimulationEpisodeVerificationV1",
    "build_simulation_episode_preparation_request",
    "episode_prefix_projection",
    "episode_prefix_projection_sha256",
]
