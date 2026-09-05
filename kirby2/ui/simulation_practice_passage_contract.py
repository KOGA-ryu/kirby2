"""Strict additive contracts for immutable Chapter 1 observation passages."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .simulation_contract import (
    SimulationComponentRefV1,
    SimulationContractIntegrityError,
    canonical_digest,
)
from .simulation_episode_contract import (
    FULL_MODEL_PREFIX_PROJECTION_ID,
    SimulationEpisodeIdentityV1,
    episode_prefix_projection,
    episode_prefix_projection_sha256,
)
from .simulation_live_contract import SimulationFrameV1
from .simulation_practice_contract import PracticeResultV1


PRACTICE_PASSAGE_CAPABILITY_SCHEMA_ID = (
    "KIRBY2_SIMULATION_PRACTICE_PASSAGE_CAPABILITY_CATALOG_V1"
)
PRACTICE_PASSAGE_REQUEST_SCHEMA_ID = (
    "KIRBY2_SIMULATION_PRACTICE_OBSERVATION_PASSAGE_REQUEST_V1"
)
PRACTICE_PASSAGE_RESULT_SCHEMA_ID = (
    "KIRBY2_SIMULATION_PRACTICE_OBSERVATION_PASSAGE_RESULT_V1"
)
PRACTICE_PASSAGE_OBSERVATION_SCHEMA_ID = (
    "KIRBY2_SIMULATION_PRACTICE_OBSERVATION_V1"
)
PRACTICE_PASSAGE_SAMPLING_POLICY = "VERIFIED_UNIFORM_QUARTERS_V1"
PRACTICE_PASSAGE_TIE_ORDER = "SIMULATION_TIME_THEN_TIE_BREAKER_V1"

_ATTEMPT_ID = re.compile(r"practice-attempt-[0-9a-f]{24}\Z")
_RUN_ID = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_FRAME_ID = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_CURSOR_ID = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")
_EPISODE_ID = re.compile(r"practice\.[a-z0-9.-]{1,118}\.v[0-9]+\Z")
_PREPARED_ID = re.compile(r"simulation-episode-prepared-result-[0-9a-f]{24}\Z")
_REQUEST_ID = re.compile(r"practice-passage-request-[0-9a-f]{24}\Z")
_PASSAGE_ID = re.compile(r"practice-passage-[0-9a-f]{24}\Z")
_OBSERVATION_ID = re.compile(r"practice-observation-[0-9a-f]{24}\Z")
_CATALOG_ID = re.compile(r"practice-passage-capabilities-[0-9a-f]{24}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

_REQUEST_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "request_id",
        "attempt_id",
        "source_run_id",
        "origin_frame_id",
        "origin_cursor_id",
        "episode_id",
        "episode_recipe_sha256",
        "prepared_result_id",
        "prepared_identity",
        "observation_policy_ref",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "observation_id",
        "observation_sequence",
        "simulation_time_us",
        "tie_breaker",
        "frame_projection_sha256",
        "frame",
    }
)
_FINAL_CUT_FIELDS = frozenset(
    {
        "source_run_id",
        "frame_id",
        "cursor_id",
        "simulation_time_us",
        "prepared_prefix_projection_sha256",
        "terminal_frame_projection_sha256",
    }
)
_INVARIANCE_FIELDS = frozenset(
    {
        "projection_id",
        "live_before_sha256",
        "reconstructed_terminal_sha256",
        "live_after_sha256",
        "status",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "passage_id",
        "status",
        "request_id",
        "attempt_id",
        "source_run_id",
        "episode_id",
        "episode_recipe_sha256",
        "prepared_result_id",
        "prepared_identity",
        "observation_policy_ref",
        "sampling_policy",
        "tie_order",
        "passage_start_time_us",
        "passage_end_time_us",
        "passage_duration_us",
        "prefix_actions",
        "transient_source_run_id",
        "observations",
        "final_cut",
        "model_invariance",
        "resource_ownership",
        "cleanup_disposition",
        "unavailable_reason",
    }
)
_CAPABILITY_FIELDS = frozenset(
    {
        "episode_id",
        "episode_recipe_sha256",
        "family",
        "support",
        "passage_duration_us",
        "unavailable_reason",
    }
)
_CAPABILITY_CATALOG_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "catalog_id",
        "sampling_policy",
        "tie_order",
        "episodes",
    }
)
_UNAVAILABLE_REASONS = frozenset(
    {
        "ATTEMPT_IDENTITY_MISMATCH",
        "CURRENT_FRAME_CHANGED",
        "EPISODE_PASSAGE_UNSUPPORTED",
        "RECONSTRUCTION_FAILED",
        "TERMINAL_PROJECTION_MISMATCH",
        "FULL_MODEL_PREFIX_MISMATCH",
        "TRANSIENT_CLEANUP_UNCONFIRMED",
        "RESULT_PUBLICATION_FAILED",
    }
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): _plain(child) for key, child in value.items()}


def _exact(value: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 contract")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    return _identifier(value, _DIGEST, label)


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _schema(root: Mapping[str, object], schema_id: str, label: str) -> None:
    if (
        root.get("schema_id") != schema_id
        or type(root.get("schema_version")) is not int
        or root.get("schema_version") != 1
    ):
        raise ValueError(f"{label} schema is unsupported")


def _cursor(frame: Mapping[str, object]) -> dict[str, object]:
    return _object(frame.get("cursor"), "practice passage frame cursor")


@dataclass(frozen=True, slots=True)
class PracticeObservationPassageRequestV1:
    """Exact request for the immutable lead-in of one active attempt."""

    request_id: str
    attempt_id: str
    source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    episode_id: str
    episode_recipe_sha256: str
    prepared_result_id: str
    prepared_identity: SimulationEpisodeIdentityV1
    observation_policy_ref: SimulationComponentRefV1

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> PracticeObservationPassageRequestV1:
        root = _object(payload, "practice passage request")
        _exact(root, _REQUEST_FIELDS, "practice passage request")
        _schema(root, PRACTICE_PASSAGE_REQUEST_SCHEMA_ID, "practice passage request")
        prepared = SimulationEpisodeIdentityV1.from_dict(
            _object(root["prepared_identity"], "practice passage prepared identity")
        )
        policy = SimulationComponentRefV1.from_dict(
            _object(root["observation_policy_ref"], "practice passage observation policy"),
            expected_kind="OBSERVATION_POLICY",
            label="practice passage observation policy",
        )
        basis = {key: root[key] for key in root if key != "request_id"}
        request_id = _identifier(
            root["request_id"], _REQUEST_ID, "practice passage request ID"
        )
        if request_id != f"practice-passage-request-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError(
                "practice passage request ID does not match its content"
            )
        episode_id = _identifier(
            root["episode_id"], _EPISODE_ID, "practice passage episode ID"
        )
        recipe_sha256 = _digest(
            root["episode_recipe_sha256"], "practice passage recipe digest"
        )
        if prepared.episode_id != episode_id:
            raise SimulationContractIntegrityError(
                "practice passage request does not bind its prepared episode"
            )
        return cls(
            request_id,
            _identifier(root["attempt_id"], _ATTEMPT_ID, "practice passage attempt ID"),
            _identifier(root["source_run_id"], _RUN_ID, "practice passage source run ID"),
            _identifier(root["origin_frame_id"], _FRAME_ID, "practice passage frame ID"),
            _identifier(root["origin_cursor_id"], _CURSOR_ID, "practice passage cursor ID"),
            episode_id,
            recipe_sha256,
            _identifier(
                root["prepared_result_id"],
                _PREPARED_ID,
                "practice passage prepared result ID",
            ),
            prepared,
            policy,
        )

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": PRACTICE_PASSAGE_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "source_run_id": self.source_run_id,
            "origin_frame_id": self.origin_frame_id,
            "origin_cursor_id": self.origin_cursor_id,
            "episode_id": self.episode_id,
            "episode_recipe_sha256": self.episode_recipe_sha256,
            "prepared_result_id": self.prepared_result_id,
            "prepared_identity": self.prepared_identity.as_dict(),
            "observation_policy_ref": self.observation_policy_ref.as_dict(),
        }
        return {**basis, "request_id": self.request_id}


@dataclass(frozen=True, slots=True)
class PracticePassageObservationV1:
    observation_id: str
    observation_sequence: int
    simulation_time_us: int
    tie_breaker: int
    frame_projection_sha256: str
    frame: SimulationFrameV1

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PracticePassageObservationV1:
        root = _object(payload, "practice passage observation")
        _exact(root, _OBSERVATION_FIELDS, "practice passage observation")
        _schema(root, PRACTICE_PASSAGE_OBSERVATION_SCHEMA_ID, "practice passage observation")
        frame = SimulationFrameV1.from_dict(
            _object(root["frame"], "practice passage observation frame")
        )
        time_us = _integer(
            root["simulation_time_us"], "practice passage observation time"
        )
        cursor = _cursor(frame.as_dict())
        if cursor["simulation_time_us"] != time_us:
            raise SimulationContractIntegrityError(
                "practice passage observation time differs from its frame"
            )
        projection_sha256 = _digest(
            root["frame_projection_sha256"],
            "practice passage observation projection digest",
        )
        if projection_sha256 != canonical_digest(episode_prefix_projection(frame)):
            raise SimulationContractIntegrityError(
                "practice passage observation projection digest does not match"
            )
        basis = {key: root[key] for key in root if key != "observation_id"}
        observation_id = _identifier(
            root["observation_id"], _OBSERVATION_ID, "practice observation ID"
        )
        if observation_id != f"practice-observation-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError(
                "practice observation ID does not match its content"
            )
        return cls(
            observation_id,
            _integer(
                root["observation_sequence"],
                "practice passage observation sequence",
                minimum=1,
            ),
            time_us,
            _integer(root["tie_breaker"], "practice passage tie breaker"),
            projection_sha256,
            frame,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_id": PRACTICE_PASSAGE_OBSERVATION_SCHEMA_ID,
            "schema_version": 1,
            "observation_id": self.observation_id,
            "observation_sequence": self.observation_sequence,
            "simulation_time_us": self.simulation_time_us,
            "tie_breaker": self.tie_breaker,
            "frame_projection_sha256": self.frame_projection_sha256,
            "frame": self.frame.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PracticeObservationPassageResultV1:
    passage_id: str
    status: str
    record: Mapping[str, object]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        request: PracticeObservationPassageRequestV1 | None = None,
    ) -> PracticeObservationPassageResultV1:
        root = _object(payload, "practice passage result")
        _exact(root, _RESULT_FIELDS, "practice passage result")
        _schema(root, PRACTICE_PASSAGE_RESULT_SCHEMA_ID, "practice passage result")
        status = root["status"]
        if status not in {"AVAILABLE", "UNAVAILABLE"}:
            raise ValueError("practice passage result status is unsupported")
        prepared = SimulationEpisodeIdentityV1.from_dict(
            _object(root["prepared_identity"], "practice passage result prepared identity")
        )
        policy = SimulationComponentRefV1.from_dict(
            _object(root["observation_policy_ref"], "practice passage result policy"),
            expected_kind="OBSERVATION_POLICY",
            label="practice passage result policy",
        )
        if request is not None:
            expected = request.as_dict()
            for field in (
                "request_id",
                "attempt_id",
                "source_run_id",
                "episode_id",
                "episode_recipe_sha256",
                "prepared_result_id",
                "prepared_identity",
                "observation_policy_ref",
            ):
                if root[field] != expected[field]:
                    raise SimulationContractIntegrityError(
                        f"practice passage result changed request field {field}"
                    )
        if prepared.episode_id != root["episode_id"]:
            raise SimulationContractIntegrityError(
                "practice passage result does not bind its prepared episode"
            )
        _identifier(root["request_id"], _REQUEST_ID, "practice passage request ID")
        _identifier(root["attempt_id"], _ATTEMPT_ID, "practice passage attempt ID")
        _identifier(root["source_run_id"], _RUN_ID, "practice passage source run ID")
        _identifier(root["episode_id"], _EPISODE_ID, "practice passage episode ID")
        _digest(root["episode_recipe_sha256"], "practice passage recipe digest")
        _identifier(
            root["prepared_result_id"],
            _PREPARED_ID,
            "practice passage prepared result ID",
        )
        if root["sampling_policy"] != PRACTICE_PASSAGE_SAMPLING_POLICY:
            raise ValueError("practice passage sampling policy is unsupported")
        if root["tie_order"] != PRACTICE_PASSAGE_TIE_ORDER:
            raise ValueError("practice passage tie order is unsupported")
        observations_value = root["observations"]
        if not isinstance(observations_value, Sequence) or isinstance(
            observations_value, (str, bytes)
        ):
            raise ValueError("practice passage observations must be an array")
        observations = tuple(
            PracticePassageObservationV1.from_dict(
                _object(item, f"practice passage observation {index}")
            )
            for index, item in enumerate(observations_value)
        )
        prefix_actions_value = root["prefix_actions"]
        if prefix_actions_value is not None and (
            not isinstance(prefix_actions_value, Sequence)
            or isinstance(prefix_actions_value, (str, bytes))
            or not prefix_actions_value
            or any(type(item) is not str or not item for item in prefix_actions_value)
        ):
            raise ValueError("practice passage prefix actions are invalid")
        prefix_actions = None if prefix_actions_value is None else tuple(prefix_actions_value)
        if status == "AVAILABLE":
            start = _integer(root["passage_start_time_us"], "practice passage start time")
            end = _integer(root["passage_end_time_us"], "practice passage end time", minimum=1)
            duration = _integer(
                root["passage_duration_us"],
                "practice passage duration",
                minimum=1,
            )
            if start != 0 or end != prepared.anchor_time_us or duration != end - start:
                raise SimulationContractIntegrityError(
                    "practice passage interval does not bind its prepared anchor"
                )
            if (
                prefix_actions is None
                or canonical_digest(list(prefix_actions)) != prepared.prefix_sha256
            ):
                raise SimulationContractIntegrityError(
                    "practice passage prefix actions do not bind its prepared identity"
                )
            if len(observations) < 2:
                raise ValueError("available practice passage needs multiple observations")
            pairs = [(item.simulation_time_us, item.tie_breaker) for item in observations]
            if pairs != sorted(pairs) or len(set(pairs)) != len(pairs):
                raise ValueError("practice passage observation order is unstable")
            if [item.observation_sequence for item in observations] != list(
                range(1, len(observations) + 1)
            ):
                raise ValueError("practice passage observation sequence is not contiguous")
            for time_us, group in _group_ties(pairs):
                if [tie for _, tie in group] != list(range(len(group))):
                    raise ValueError(
                        f"practice passage tie order at {time_us} is not contiguous"
                    )
            if (
                observations[0].simulation_time_us != start
                or observations[-1].simulation_time_us != end
            ):
                raise SimulationContractIntegrityError(
                    "practice passage observations do not span their interval"
                )
            if any(item.simulation_time_us > end for item in observations):
                raise SimulationContractIntegrityError(
                    "practice passage exposes an observation after its prepared cut"
                )
            source_ids = {item.frame.source_run_id for item in observations}
            transient_source = _identifier(
                root["transient_source_run_id"],
                _RUN_ID,
                "practice passage transient source run ID",
            )
            if source_ids != {transient_source}:
                raise SimulationContractIntegrityError(
                    "practice passage observations do not share their transient source"
                )
            if any(
                item.frame.as_dict()["provenance"]["observation_policy_ref"]
                != policy.as_dict()
                for item in observations
            ):
                raise SimulationContractIntegrityError(
                    "practice passage observation policy changed within the passage"
                )
            change_basis = {
                canonical_digest(
                    {
                        "book": item.frame.as_dict()["book"],
                        "recent_trades": item.frame.as_dict()["recent_trades"],
                        "working_orders": item.frame.as_dict()["working_orders"],
                        "account": item.frame.as_dict()["account"],
                    }
                )
                for item in observations
            }
            if len(change_basis) < 2:
                raise ValueError("practice passage has no changing public observation")
            final_cut = _object(root["final_cut"], "practice passage final cut")
            _exact(final_cut, _FINAL_CUT_FIELDS, "practice passage final cut")
            terminal = observations[-1]
            terminal_projection = canonical_digest(episode_prefix_projection(terminal.frame))
            final_source_run_id = _identifier(
                final_cut["source_run_id"],
                _RUN_ID,
                "practice passage final source run ID",
            )
            final_frame_id = _identifier(
                final_cut["frame_id"], _FRAME_ID, "practice passage final frame ID"
            )
            final_cursor_id = _identifier(
                final_cut["cursor_id"], _CURSOR_ID, "practice passage final cursor ID"
            )
            final_time_us = _integer(
                final_cut["simulation_time_us"], "practice passage final time"
            )
            _digest(
                final_cut["prepared_prefix_projection_sha256"],
                "practice passage final prepared projection digest",
            )
            _digest(
                final_cut["terminal_frame_projection_sha256"],
                "practice passage terminal projection digest",
            )
            if (
                final_source_run_id != root["source_run_id"]
                or final_time_us != end
                or final_cut["terminal_frame_projection_sha256"] != terminal_projection
                or final_cut["prepared_prefix_projection_sha256"]
                != episode_prefix_projection_sha256(terminal.frame, prefix_actions)
            ):
                raise SimulationContractIntegrityError(
                    "practice passage terminal observation does not bind the live final cut"
                )
            if request is not None and (
                final_source_run_id != request.source_run_id
                or final_frame_id != request.origin_frame_id
                or final_cursor_id != request.origin_cursor_id
                or final_time_us != request.prepared_identity.anchor_time_us
            ):
                raise SimulationContractIntegrityError(
                    "practice passage final cut changed its requested live authority"
                )
            invariance = _object(root["model_invariance"], "practice passage model invariance")
            _exact(invariance, _INVARIANCE_FIELDS, "practice passage model invariance")
            digests = {
                _digest(invariance[field], f"practice passage {field}")
                for field in (
                    "live_before_sha256",
                    "reconstructed_terminal_sha256",
                    "live_after_sha256",
                )
            }
            if (
                invariance["projection_id"] != FULL_MODEL_PREFIX_PROJECTION_ID
                or invariance["status"] != "MATCH"
                or len(digests) != 1
            ):
                raise SimulationContractIntegrityError(
                    "practice passage full-model commitments do not match"
                )
            if (
                root["resource_ownership"] != "NO_RESOURCE"
                or root["cleanup_disposition"] != "TRANSIENT_RECONSTRUCTION_CLOSED"
                or root["unavailable_reason"] is not None
            ):
                raise ValueError("available practice passage resource state is invalid")
        else:
            if observations or prefix_actions is not None:
                raise ValueError("unavailable practice passage carries observations")
            for field in (
                "passage_start_time_us",
                "passage_end_time_us",
                "passage_duration_us",
                "transient_source_run_id",
                "final_cut",
                "model_invariance",
            ):
                if root[field] is not None:
                    raise ValueError("unavailable practice passage carries available-only fields")
            reason = root["unavailable_reason"]
            if reason not in _UNAVAILABLE_REASONS:
                raise ValueError("practice passage unavailable reason is unsupported")
            ownership = root["resource_ownership"]
            cleanup = root["cleanup_disposition"]
            if reason == "TRANSIENT_CLEANUP_UNCONFIRMED":
                if ownership != "CALLER_OWNS_CLEANUP_HANDLE" or cleanup != "CLEANUP_UNCONFIRMED":
                    raise ValueError("unconfirmed passage cleanup ownership is invalid")
            elif ownership != "NO_RESOURCE" or cleanup not in {
                None,
                "TRANSIENT_RECONSTRUCTION_CLOSED",
            }:
                raise ValueError("unavailable practice passage resource state is invalid")
        basis = {key: root[key] for key in root if key != "passage_id"}
        passage_id = _identifier(root["passage_id"], _PASSAGE_ID, "practice passage ID")
        if passage_id != f"practice-passage-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError(
                "practice passage ID does not match its content"
            )
        return cls(passage_id, status, _freeze(root))

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


def _group_ties(
    pairs: Sequence[tuple[int, int]],
) -> tuple[tuple[int, tuple[tuple[int, int], ...]], ...]:
    grouped: list[tuple[int, tuple[tuple[int, int], ...]]] = []
    for time_us in sorted({time for time, _ in pairs}):
        grouped.append((time_us, tuple(pair for pair in pairs if pair[0] == time_us)))
    return tuple(grouped)


@dataclass(frozen=True, slots=True)
class PracticePassageCapabilityCatalogV1:
    catalog_id: str
    episodes: tuple[Mapping[str, object], ...]

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> PracticePassageCapabilityCatalogV1:
        root = _object(payload, "practice passage capability catalog")
        _exact(root, _CAPABILITY_CATALOG_FIELDS, "practice passage capability catalog")
        _schema(
            root,
            PRACTICE_PASSAGE_CAPABILITY_SCHEMA_ID,
            "practice passage capability catalog",
        )
        if root["sampling_policy"] != PRACTICE_PASSAGE_SAMPLING_POLICY:
            raise ValueError("practice passage capability sampling policy is unsupported")
        if root["tie_order"] != PRACTICE_PASSAGE_TIE_ORDER:
            raise ValueError("practice passage capability tie order is unsupported")
        values = root["episodes"]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
            raise ValueError("practice passage capabilities require episode entries")
        episodes: list[Mapping[str, object]] = []
        for index, value in enumerate(values):
            item = _object(value, f"practice passage capability {index}")
            _exact(item, _CAPABILITY_FIELDS, f"practice passage capability {index}")
            _identifier(item["episode_id"], _EPISODE_ID, "passage capability episode ID")
            _digest(item["episode_recipe_sha256"], "passage capability recipe digest")
            if item["family"] not in {"F1_CONTROL", "F2_READING", "F3_RESIDUAL"}:
                raise ValueError("passage capability family is unsupported")
            if item["support"] == "AVAILABLE":
                _integer(item["passage_duration_us"], "passage capability duration", minimum=1)
                if item["unavailable_reason"] is not None:
                    raise ValueError("available passage capability carries a reason")
            elif item["support"] == "UNSUPPORTED":
                if (
                    item["passage_duration_us"] is not None
                    or item["unavailable_reason"]
                    not in {
                        "ANCHOR_TOO_SHORT_FOR_MEANINGFUL_PACE",
                        "RECIPE_NOT_VERIFIED_FOR_PASSAGE",
                    }
                ):
                    raise ValueError("unsupported passage capability is inconsistent")
            else:
                raise ValueError("passage capability support is invalid")
            episodes.append(_freeze(item))
        if len({item["episode_id"] for item in episodes}) != len(episodes):
            raise ValueError("practice passage capabilities repeat an episode")
        basis = {key: root[key] for key in root if key != "catalog_id"}
        catalog_id = _identifier(
            root["catalog_id"], _CATALOG_ID, "practice passage capability catalog ID"
        )
        if catalog_id != f"practice-passage-capabilities-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError(
                "practice passage capability catalog ID does not match"
            )
        return cls(catalog_id, tuple(episodes))

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": PRACTICE_PASSAGE_CAPABILITY_SCHEMA_ID,
            "schema_version": 1,
            "sampling_policy": PRACTICE_PASSAGE_SAMPLING_POLICY,
            "tie_order": PRACTICE_PASSAGE_TIE_ORDER,
            "episodes": [_plain(item) for item in self.episodes],
        }
        return {**basis, "catalog_id": self.catalog_id}


def build_simulation_practice_observation_passage_request(
    practice_result_payload: Mapping[str, object],
) -> dict[str, object]:
    """Bind a passage request to one exact available practice cut."""

    result = PracticeResultV1.from_dict(practice_result_payload).as_dict()
    attempt = result["attempt"]
    frame = result["current_frame"]
    episode = result["episode"]
    if (
        result["status"] not in {"AVAILABLE", "DUPLICATE"}
        or not isinstance(attempt, Mapping)
        or not isinstance(frame, Mapping)
        or not isinstance(episode, Mapping)
    ):
        raise ValueError("practice passage requires an available practice result")
    cursor = _cursor(frame)
    provenance = _object(frame.get("provenance"), "practice passage frame provenance")
    basis = {
        "schema_id": PRACTICE_PASSAGE_REQUEST_SCHEMA_ID,
        "schema_version": 1,
        "attempt_id": attempt["attempt_id"],
        "source_run_id": result["source_run_id"],
        "origin_frame_id": frame["frame_id"],
        "origin_cursor_id": cursor["cursor_id"],
        "episode_id": episode["episode_id"],
        "episode_recipe_sha256": episode["recipe_sha256"],
        "prepared_result_id": attempt["prepared_result_id"],
        "prepared_identity": attempt["prepared_identity"],
        "observation_policy_ref": provenance["observation_policy_ref"],
    }
    return PracticeObservationPassageRequestV1.from_dict(
        {
            **basis,
            "request_id": f"practice-passage-request-{canonical_digest(basis)[:24]}",
        }
    ).as_dict()


def build_practice_passage_observation(
    frame_payload: Mapping[str, object],
    *,
    observation_sequence: int,
    tie_breaker: int,
) -> dict[str, object]:
    frame = SimulationFrameV1.from_dict(frame_payload)
    time_us = int(_cursor(frame.as_dict())["simulation_time_us"])
    basis = {
        "schema_id": PRACTICE_PASSAGE_OBSERVATION_SCHEMA_ID,
        "schema_version": 1,
        "observation_sequence": observation_sequence,
        "simulation_time_us": time_us,
        "tie_breaker": tie_breaker,
        "frame_projection_sha256": canonical_digest(episode_prefix_projection(frame)),
        "frame": frame.as_dict(),
    }
    return PracticePassageObservationV1.from_dict(
        {
            **basis,
            "observation_id": f"practice-observation-{canonical_digest(basis)[:24]}",
        }
    ).as_dict()


def build_practice_passage_result(**fields: object) -> dict[str, object]:
    basis = {
        "schema_id": PRACTICE_PASSAGE_RESULT_SCHEMA_ID,
        "schema_version": 1,
        **fields,
    }
    if set(basis) != _RESULT_FIELDS - {"passage_id"}:
        raise ValueError("practice passage result fields differ from V1")
    return {
        **basis,
        "passage_id": f"practice-passage-{canonical_digest(basis)[:24]}",
    }


__all__ = [
    "PRACTICE_PASSAGE_CAPABILITY_SCHEMA_ID",
    "PRACTICE_PASSAGE_OBSERVATION_SCHEMA_ID",
    "PRACTICE_PASSAGE_REQUEST_SCHEMA_ID",
    "PRACTICE_PASSAGE_RESULT_SCHEMA_ID",
    "PRACTICE_PASSAGE_SAMPLING_POLICY",
    "PRACTICE_PASSAGE_TIE_ORDER",
    "PracticeObservationPassageRequestV1",
    "PracticeObservationPassageResultV1",
    "PracticePassageCapabilityCatalogV1",
    "PracticePassageObservationV1",
    "build_practice_passage_observation",
    "build_practice_passage_result",
    "build_simulation_practice_observation_passage_request",
]
