"""Strict public wire records for the process-local curated practice loop."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .simulation_contract import (
    SimulationContractIntegrityError,
    SimulationProfileRefV1,
    _freeze,
    _plain,
    canonical_digest,
)
from .simulation_episode_contract import SimulationEpisodeIdentityV1
from .simulation_live_contract import SimulationFrameV1


PRACTICE_CATALOG_SCHEMA_ID = "KIRBY2_SIMULATION_PRACTICE_CATALOG_V1"
PRACTICE_ATTEMPT_REQUEST_SCHEMA_ID = "KIRBY2_SIMULATION_PRACTICE_ATTEMPT_REQUEST_V1"
PRACTICE_ACTION_REQUEST_SCHEMA_ID = "KIRBY2_SIMULATION_PRACTICE_ACTION_REQUEST_V1"
PRACTICE_RESULT_SCHEMA_ID = "KIRBY2_SIMULATION_PRACTICE_RESULT_V1"

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_EPISODE = re.compile(r"practice\.[a-z0-9.-]+\.v1\Z")
_ATTEMPT = re.compile(r"practice-attempt-[0-9a-f]{24}\Z")
_REQUEST = re.compile(r"practice-(?:attempt|action)-request-[0-9a-f]{24}\Z")
_HOLD = re.compile(r"practice-guided-hold-[0-9a-f]{24}\Z")
_RESULT = re.compile(r"practice-result-[0-9a-f]{24}\Z")
_RUN = re.compile(r"simulation-run-[0-9a-f]{32}\Z")
_FRAME = re.compile(r"simulation-frame-[0-9a-f]{24}\Z")
_CURSOR = re.compile(r"simulation-cursor-[0-9a-f]{24}\Z")
_ACTION = re.compile(r"PLAYER_[A-Z0-9_]+\Z")
_CATALOG = re.compile(r"practice-catalog-[0-9a-f]{24}\Z")
_OPERATION = re.compile(r"practice-operation-[0-9a-f]{24}\Z")
_PRACTICE_SEMANTIC_ACTIONS = frozenset(
    {
        "SIMULATION_PLAY",
        "PLAYER_INCREASE_QUANTITY",
        "PLAYER_DECREASE_QUANTITY",
        "PLAYER_BUY_BID",
        "PLAYER_CANCEL_NEAREST",
        "PLAYER_REPLACE_NEAREST",
    }
)
_F1_ACTION_SEQUENCES = frozenset(
    {
        ("PLAYER_INCREASE_QUANTITY", "PLAYER_BUY_BID", "PLAYER_CANCEL_NEAREST"),
        ("PLAYER_DECREASE_QUANTITY", "PLAYER_BUY_BID", "PLAYER_REPLACE_NEAREST"),
    }
)

_CATALOG_FIELDS = frozenset({"schema_id", "schema_version", "catalog_id", "episodes"})
_ATTEMPT_FIELDS = frozenset(
    {
        "schema_id", "schema_version", "request_id", "episode_id", "operation",
        "operation_id", "mode", "pace_multiplier_ppm", "prior_attempt_id",
    }
)
_ACTION_FIELDS = frozenset(
    {
        "schema_id", "schema_version", "request_id", "attempt_id", "hold_id",
        "source_run_id", "origin_frame_id", "origin_cursor_id", "operation",
        "response_kind", "semantic_action_id", "wall_time",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "schema_id", "schema_version", "result_id", "status", "operation",
        "attempt", "episode", "source_run_id", "current_frame", "hold_id",
        "assistance", "assessment", "debrief", "unavailable_reason",
    }
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _exact(value: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{label} fields differ from V1")


def _schema(root: Mapping[str, object], expected: str, label: str) -> None:
    if (
        root["schema_id"] != expected
        or type(root["schema_version"]) is not int
        or root["schema_version"] != 1
    ):
        raise ValueError(f"{label} schema is unsupported")


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be normalized text")
    return value


def _id(value: object, pattern: re.Pattern[str], label: str) -> str:
    text = _text(value, label)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{label} has invalid V1 form")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} must be SHA-256")
    return value


def _wall_time(value: object) -> dict[str, object]:
    root = _object(value, "practice wall time")
    fields = frozenset({"source", "resolution_us", "elapsed_wall_time_us"})
    _exact(root, fields, "practice wall time")
    if root["source"] not in {"UNAVAILABLE", "MONOTONIC_CALLER"}:
        raise ValueError("practice wall time source is unsupported")
    for field in ("resolution_us", "elapsed_wall_time_us"):
        child = root[field]
        if child is not None and (type(child) is not int or child < 0):
            raise ValueError(f"practice wall time {field} is invalid")
    if root["source"] == "UNAVAILABLE":
        if root["resolution_us"] is not None or root["elapsed_wall_time_us"] is not None:
            raise ValueError("unavailable wall time cannot claim a measurement")
    elif root["resolution_us"] is None or root["resolution_us"] <= 0 or root["elapsed_wall_time_us"] is None:
        raise ValueError("caller wall time requires resolution and elapsed measurement")
    return root


def _episode(value: object) -> dict[str, object]:
    """Decode the immutable public recipe shape without importing runtime state."""

    root = _object(value, "practice episode")
    fields = frozenset({
        "schema_id", "schema_version", "episode_id", "family", "title", "variation_of",
        "profile_ref", "seed", "control_values", "duration_us", "preparation_actions",
        "anchor_time_us", "primary_skill_id", "objective_class", "expected_actions",
        "observation_rule", "recipe_sha256",
    })
    _exact(root, fields, "practice episode")
    if (
        root["schema_id"] != "KIRBY2_PRACTICE_EPISODE_DEFINITION_V1"
        or type(root["schema_version"]) is not int
        or root["schema_version"] != 1
    ):
        raise ValueError("practice episode schema is unsupported")
    _id(root["episode_id"], _EPISODE, "practice episode ID")
    if root["family"] not in {"F1_CONTROL", "F2_READING", "F3_RESIDUAL"}:
        raise ValueError("practice episode family is unsupported")
    _text(root["title"], "practice episode title")
    _text(root["primary_skill_id"], "practice episode primary skill")
    if root["objective_class"] not in {
        "EXACT_MECHANICAL", "DECLARED_RULE", "OPEN_JUDGMENT",
    }:
        raise ValueError("practice episode objective class is unsupported")
    if root["variation_of"] is not None:
        _id(root["variation_of"], _EPISODE, "practice episode variation parent")
    if type(root["seed"]) is not int or root["seed"] < 0:
        raise ValueError("practice episode seed is invalid")
    if type(root["duration_us"]) is not int or type(root["anchor_time_us"]) is not int:
        raise ValueError("practice episode timing is invalid")
    if root["duration_us"] <= root["anchor_time_us"] or root["anchor_time_us"] <= 0:
        raise ValueError("practice episode timing is inconsistent")
    SimulationProfileRefV1.from_dict(
        _object(root["profile_ref"], "practice episode.profile_ref"),
        label="practice episode.profile_ref",
    )
    controls = _object(root["control_values"], "practice episode.control_values")
    _exact(controls, frozenset({"relative_volume", "liquidity", "intensity_scale_ppm"}), "practice episode.control_values")
    if (
        type(controls["relative_volume"]) is not str
        or type(controls["liquidity"]) is not str
        or type(controls["intensity_scale_ppm"]) is not int
        or controls["intensity_scale_ppm"] <= 0
    ):
        raise ValueError("practice episode controls are invalid")
    for field in ("preparation_actions", "expected_actions"):
        if not isinstance(root[field], Sequence) or isinstance(root[field], (str, bytes)):
            raise ValueError(f"practice episode.{field} must be an array")
        if any(type(item) is not str or item not in _PRACTICE_SEMANTIC_ACTIONS for item in root[field]):
            raise ValueError(f"practice episode.{field} has an invalid action")
    prep = tuple(root["preparation_actions"])
    expected = tuple(root["expected_actions"])
    if not prep or prep[0] != "SIMULATION_PLAY":
        raise ValueError("practice episode must begin preparation by playing")
    if root["family"] == "F1_CONTROL":
        if (
            root["objective_class"] != "EXACT_MECHANICAL"
            or prep != ("SIMULATION_PLAY",)
            or expected not in _F1_ACTION_SEQUENCES
        ):
            raise ValueError("F1 recipe actions are invalid")
        rule = _object(root["observation_rule"], "practice F1 rule")
        _exact(rule, frozenset({"instruction", "quantity", "requires_learner_placement"}), "practice F1 rule")
        if type(rule["instruction"]) is not str or type(rule["quantity"]) is not int or rule["quantity"] <= 0 or rule["requires_learner_placement"] is not True:
            raise ValueError("practice F1 rule is invalid")
    elif root["family"] == "F2_READING":
        if root["objective_class"] != "DECLARED_RULE" or prep != ("SIMULATION_PLAY",) or expected:
            raise ValueError("F2 recipe actions are invalid")
        rule = _object(root["observation_rule"], "practice F2 rule")
        _exact(rule, frozenset({
            "rule_id", "permitted_observations", "pressure_minimum_recent_trades",
            "pressure_minimum_bid_minus_ask_quantity", "replenishment_minimum_ask_quantity",
            "answers", "unavailable_answer", "no_opportunity_answer",
        }), "practice F2 rule")
        if (
            rule["rule_id"] != "PUBLIC_PRESSURE_REPLENISHMENT_V1"
            or rule["permitted_observations"] != ["book", "recent_trades"]
            or any(type(rule[key]) is not int or rule[key] < 0 for key in (
                "pressure_minimum_recent_trades", "pressure_minimum_bid_minus_ask_quantity",
                "replenishment_minimum_ask_quantity",
            ))
            or rule["answers"] != ["PRESSURE_PRESENT", "REPLENISHMENT_BLOCKS", "INSUFFICIENT_EVIDENCE", "WAIT", "DECLINE"]
            or rule["unavailable_answer"] != "INSUFFICIENT_EVIDENCE"
            or rule["no_opportunity_answer"] != "WAIT"
        ):
            raise ValueError("practice F2 rule is invalid")
    else:
        if (
            root["objective_class"] != "EXACT_MECHANICAL"
            or prep != ("SIMULATION_PLAY", "PLAYER_BUY_BID")
            or expected != ("PLAYER_CANCEL_NEAREST",)
        ):
            raise ValueError("F3 recipe actions are invalid")
        rule = _object(root["observation_rule"], "practice F3 rule")
        _exact(rule, frozenset({
            "invalidation", "requires_single_order_partial_fill", "minimum_position",
            "minimum_remaining_quantity",
        }), "practice F3 rule")
        if (
            type(rule["invalidation"]) is not str
            or rule["requires_single_order_partial_fill"] is not True
            or type(rule["minimum_position"]) is not int or rule["minimum_position"] <= 0
            or type(rule["minimum_remaining_quantity"]) is not int or rule["minimum_remaining_quantity"] <= 0
        ):
            raise ValueError("practice F3 rule is invalid")
    basis = {key: root[key] for key in root if key != "recipe_sha256"}
    if _digest(root["recipe_sha256"], "practice episode recipe digest") != canonical_digest(basis):
        raise SimulationContractIntegrityError("practice episode recipe digest does not match")
    return root


@dataclass(frozen=True, slots=True)
class PracticeCatalogV1:
    catalog_id: str
    episodes: tuple[Mapping[str, object], ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PracticeCatalogV1:
        root = _object(payload, "practice catalog")
        _exact(root, _CATALOG_FIELDS, "practice catalog")
        _schema(root, PRACTICE_CATALOG_SCHEMA_ID, "practice catalog")
        episodes = root["episodes"]
        if not isinstance(episodes, Sequence) or isinstance(episodes, (str, bytes)):
            raise ValueError("practice catalog episodes must be an array")
        decoded = tuple(_episode(item) for item in episodes)
        if len(decoded) != 6 or len({item["episode_id"] for item in decoded}) != 6:
            raise ValueError("practice catalog must contain exactly six unique recipes")
        by_id = {str(item["episode_id"]): item for item in decoded}
        for item in decoded:
            parent = item["variation_of"]
            if parent is not None and (
                parent not in by_id or parent == item["episode_id"]
                or by_id[parent]["family"] != item["family"]
            ):
                raise ValueError("practice catalog variation parent is invalid")
        basis = {"schema_id": PRACTICE_CATALOG_SCHEMA_ID, "schema_version": 1, "episodes": list(decoded)}
        catalog_id = _id(root["catalog_id"], _CATALOG, "practice catalog ID")
        if catalog_id != f"practice-catalog-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("practice catalog ID does not match")
        return cls(catalog_id, tuple(_freeze(item) for item in decoded))

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": PRACTICE_CATALOG_SCHEMA_ID,
            "schema_version": 1,
            "episodes": [_plain(item) for item in self.episodes],
        }
        return {**basis, "catalog_id": self.catalog_id}


@dataclass(frozen=True, slots=True)
class PracticeAttemptRequestV1:
    request_id: str
    episode_id: str
    operation: str
    operation_id: str
    mode: str
    pace_multiplier_ppm: int
    prior_attempt_id: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PracticeAttemptRequestV1:
        root = _object(payload, "practice attempt request")
        _exact(root, _ATTEMPT_FIELDS, "practice attempt request")
        _schema(root, PRACTICE_ATTEMPT_REQUEST_SCHEMA_ID, "practice attempt request")
        episode_id = _id(root["episode_id"], _EPISODE, "practice episode ID")
        operation = _text(root["operation"], "practice attempt operation")
        if operation not in {"BEGIN", "EXACT_REPEAT", "VARIATION"}:
            raise ValueError("practice attempt operation is unsupported")
        operation_id = _id(root["operation_id"], _OPERATION, "practice attempt operation ID")
        mode = _text(root["mode"], "practice attempt mode")
        if mode not in {"GUIDED", "UNASSISTED"}:
            raise ValueError("practice attempt mode is unsupported")
        pace = root["pace_multiplier_ppm"]
        if type(pace) is not int or pace not in {500_000, 1_000_000}:
            raise ValueError("practice pace is unsupported")
        prior = root["prior_attempt_id"]
        if prior is not None:
            prior = _id(prior, _ATTEMPT, "practice prior attempt ID")
        if (operation == "BEGIN") != (prior is None):
            raise ValueError("practice attempt prior identity is inconsistent")
        basis = {
            "schema_id": PRACTICE_ATTEMPT_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "episode_id": episode_id,
            "operation": operation,
            "operation_id": operation_id,
            "mode": mode,
            "pace_multiplier_ppm": pace,
            "prior_attempt_id": prior,
        }
        request_id = _id(root["request_id"], _REQUEST, "practice attempt request ID")
        if request_id != f"practice-attempt-request-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("practice attempt request ID does not match")
        return cls(request_id, episode_id, operation, operation_id, mode, pace, prior)

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": PRACTICE_ATTEMPT_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "episode_id": self.episode_id,
            "operation": self.operation,
            "operation_id": self.operation_id,
            "mode": self.mode,
            "pace_multiplier_ppm": self.pace_multiplier_ppm,
            "prior_attempt_id": self.prior_attempt_id,
        }
        return {**basis, "request_id": self.request_id}


@dataclass(frozen=True, slots=True)
class PracticeActionRequestV1:
    request_id: str
    attempt_id: str
    hold_id: str | None
    source_run_id: str
    origin_frame_id: str
    origin_cursor_id: str
    operation: str
    response_kind: str
    semantic_action_id: str | None
    wall_time: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PracticeActionRequestV1:
        root = _object(payload, "practice action request")
        _exact(root, _ACTION_FIELDS, "practice action request")
        _schema(root, PRACTICE_ACTION_REQUEST_SCHEMA_ID, "practice action request")
        operation = _text(root["operation"], "practice action operation")
        if operation not in {"STAGE", "CONTINUE", "UNASSISTED"}:
            raise ValueError("practice action operation is unsupported")
        kind = _text(root["response_kind"], "practice response kind")
        if kind not in {
            "SEMANTIC_ACTION", "PRESSURE_PRESENT", "REPLENISHMENT_BLOCKS",
            "INSUFFICIENT_EVIDENCE", "WAIT", "DECLINE",
        }:
            raise ValueError("practice response kind is unsupported")
        action = root["semantic_action_id"]
        if kind == "SEMANTIC_ACTION":
            action = _id(action, _ACTION, "practice semantic action")
        elif action is not None:
            raise ValueError("non-semantic practice responses cannot carry a semantic action")
        hold = root["hold_id"]
        if operation == "UNASSISTED":
            if hold is not None:
                raise ValueError("unassisted action cannot carry a hold")
        else:
            hold = _id(hold, _HOLD, "practice guided hold ID")
        wall_time = _wall_time(root["wall_time"])
        basis = {
            "schema_id": PRACTICE_ACTION_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "attempt_id": _id(root["attempt_id"], _ATTEMPT, "practice attempt ID"),
            "hold_id": hold,
            "source_run_id": _id(root["source_run_id"], _RUN, "practice source run ID"),
            "origin_frame_id": _id(root["origin_frame_id"], _FRAME, "practice origin frame ID"),
            "origin_cursor_id": _id(root["origin_cursor_id"], _CURSOR, "practice origin cursor ID"),
            "operation": operation,
            "response_kind": kind,
            "semantic_action_id": action,
            "wall_time": wall_time,
        }
        request_id = _id(root["request_id"], _REQUEST, "practice action request ID")
        if request_id != f"practice-action-request-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("practice action request ID does not match")
        values = {key: basis[key] for key in basis if key not in {"schema_id", "schema_version"}}
        values["wall_time"] = _freeze(wall_time)
        return cls(request_id, **values)

    def as_dict(self) -> dict[str, object]:
        basis = {
            "schema_id": PRACTICE_ACTION_REQUEST_SCHEMA_ID,
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "hold_id": self.hold_id,
            "source_run_id": self.source_run_id,
            "origin_frame_id": self.origin_frame_id,
            "origin_cursor_id": self.origin_cursor_id,
            "operation": self.operation,
            "response_kind": self.response_kind,
            "semantic_action_id": self.semantic_action_id,
            "wall_time": _plain(self.wall_time),
        }
        return {**basis, "request_id": self.request_id}


def build_practice_attempt_request(
    *, episode_id: str, operation: str = "BEGIN", mode: str = "GUIDED",
    pace_multiplier_ppm: int = 1_000_000, prior_attempt_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, object]:
    if operation_id is None:
        operation_id = f"practice-operation-{secrets.token_hex(12)}"
    basis = {
        "schema_id": PRACTICE_ATTEMPT_REQUEST_SCHEMA_ID,
        "schema_version": 1,
        "episode_id": episode_id,
        "operation": operation,
        "operation_id": operation_id,
        "mode": mode,
        "pace_multiplier_ppm": pace_multiplier_ppm,
        "prior_attempt_id": prior_attempt_id,
    }
    record = {**basis, "request_id": f"practice-attempt-request-{canonical_digest(basis)[:24]}"}
    return PracticeAttemptRequestV1.from_dict(record).as_dict()


def build_practice_action_request(
    *, attempt_id: str, hold_id: str | None, source_run_id: str, origin_frame_id: str,
    origin_cursor_id: str, operation: str, response_kind: str,
    semantic_action_id: str | None = None,
    wall_time: Mapping[str, object] | None = None,
) -> dict[str, object]:
    basis = {
        "schema_id": PRACTICE_ACTION_REQUEST_SCHEMA_ID,
        "schema_version": 1,
        "attempt_id": attempt_id,
        "hold_id": hold_id,
        "source_run_id": source_run_id,
        "origin_frame_id": origin_frame_id,
        "origin_cursor_id": origin_cursor_id,
        "operation": operation,
        "response_kind": response_kind,
        "semantic_action_id": semantic_action_id,
        "wall_time": (
            {"source": "UNAVAILABLE", "resolution_us": None, "elapsed_wall_time_us": None}
            if wall_time is None else dict(wall_time)
        ),
    }
    record = {**basis, "request_id": f"practice-action-request-{canonical_digest(basis)[:24]}"}
    return PracticeActionRequestV1.from_dict(record).as_dict()


_ATTEMPT_RESULT_FIELDS = frozenset({
    "attempt_id", "attempt_request_id", "episode_id", "episode_recipe_sha256", "operation",
    "operation_id", "mode", "pace_multiplier_ppm", "prior_attempt_id", "prepared_result_id", "source_run_id",
    "prepared_identity", "anchor_time_us", "step_index", "step_count",
})
_ASSISTANCE_FIELDS = frozenset({"kind", "simulation_time_us", "wall_time", "message"})
_ASSESSMENT_FIELDS = frozenset({
    "assessment_class", "outcome", "action_index", "expected_semantic_action_id",
    "observed_public_classification", "evidence", "message",
})
_EVIDENCE_FIELDS = frozenset({
    "frame_id", "cursor_id", "semantic_action_id", "order_ids", "recent_trade_count",
    "bid_aggregate_quantity", "ask_aggregate_quantity",
})
_DEBRIEF_FIELDS = frozenset({"source_run_id", "action_request_id", "outcome", "evidence"})


def _attempt_record(value: object) -> dict[str, object]:
    root = _object(value, "practice result attempt")
    _exact(root, _ATTEMPT_RESULT_FIELDS, "practice result attempt")
    _id(root["attempt_id"], _ATTEMPT, "practice result attempt ID")
    _id(root["attempt_request_id"], _REQUEST, "practice result attempt request ID")
    _id(root["episode_id"], _EPISODE, "practice result attempt episode ID")
    _digest(root["episode_recipe_sha256"], "practice result attempt recipe digest")
    if root["operation"] not in {"BEGIN", "EXACT_REPEAT", "VARIATION"}:
        raise ValueError("practice result attempt operation is unsupported")
    _id(root["operation_id"], _OPERATION, "practice result attempt operation ID")
    if root["mode"] not in {"GUIDED", "UNASSISTED"}:
        raise ValueError("practice result attempt mode is unsupported")
    if type(root["pace_multiplier_ppm"]) is not int or root["pace_multiplier_ppm"] not in {500_000, 1_000_000}:
        raise ValueError("practice result attempt pace is invalid")
    if root["prior_attempt_id"] is not None:
        _id(root["prior_attempt_id"], _ATTEMPT, "practice result prior attempt ID")
    if (root["operation"] == "BEGIN") != (root["prior_attempt_id"] is None):
        raise ValueError("practice result attempt prior identity is inconsistent")
    _id(root["prepared_result_id"], re.compile(r"simulation-episode-prepared-result-[0-9a-f]{24}\Z"), "practice prepared result ID")
    SimulationEpisodeIdentityV1.from_dict(
        _object(root["prepared_identity"], "practice prepared episode identity")
    )
    _id(root["source_run_id"], _RUN, "practice result attempt source run ID")
    for field in ("anchor_time_us", "step_index", "step_count"):
        if type(root[field]) is not int or root[field] < 0:
            raise ValueError(f"practice result attempt {field} is invalid")
    if root["anchor_time_us"] <= 0 or root["step_index"] > root["step_count"]:
        raise ValueError("practice result attempt counters are inconsistent")
    return root


def _evidence(value: object) -> dict[str, object]:
    root = _object(value, "practice assessment evidence")
    _exact(root, _EVIDENCE_FIELDS, "practice assessment evidence")
    _id(root["frame_id"], _FRAME, "practice evidence frame ID")
    _id(root["cursor_id"], _CURSOR, "practice evidence cursor ID")
    if root["semantic_action_id"] is not None:
        _id(root["semantic_action_id"], _ACTION, "practice evidence semantic action")
    if not isinstance(root["order_ids"], Sequence) or isinstance(root["order_ids"], (str, bytes)) or any(type(item) is not str or not item for item in root["order_ids"]):
        raise ValueError("practice assessment evidence order IDs are invalid")
    for field in ("recent_trade_count", "bid_aggregate_quantity", "ask_aggregate_quantity"):
        if root[field] is not None and (type(root[field]) is not int or root[field] < 0):
            raise ValueError(f"practice assessment evidence {field} is invalid")
    return root


def _assessment(value: object) -> dict[str, object]:
    root = _object(value, "practice assessment")
    _exact(root, _ASSESSMENT_FIELDS, "practice assessment")
    if root["assessment_class"] not in {"EXACT_MECHANICAL", "DECLARED_RULE", "OPEN_JUDGMENT"}:
        raise ValueError("practice assessment class is unsupported")
    if root["outcome"] not in {
        "PASS", "FAIL", "NO_OPPORTUNITY", "INSUFFICIENT_EVIDENCE", "LEARNER_ABORT", "SYSTEM_FAILURE",
    }:
        raise ValueError("practice assessment outcome is unsupported")
    if type(root["action_index"]) is not int or root["action_index"] < 0:
        raise ValueError("practice assessment action index is invalid")
    if root["expected_semantic_action_id"] is not None:
        _id(root["expected_semantic_action_id"], _ACTION, "practice expected semantic action")
    if root["observed_public_classification"] is not None and root["observed_public_classification"] not in {
        "PRESSURE_PRESENT", "REPLENISHMENT_BLOCKS", "NO_OPPORTUNITY", "INSUFFICIENT_EVIDENCE",
    }:
        raise ValueError("practice observed classification is unsupported")
    _evidence(root["evidence"])
    _text(root["message"], "practice assessment message")
    return root


def _debrief(value: object) -> dict[str, object]:
    root = _object(value, "practice debrief")
    _exact(root, _DEBRIEF_FIELDS, "practice debrief")
    _id(root["source_run_id"], _RUN, "practice debrief source run ID")
    if root["action_request_id"] is not None:
        _id(root["action_request_id"], _REQUEST, "practice debrief action request ID")
    if root["outcome"] not in {
        "PASS", "FAIL", "NO_OPPORTUNITY", "INSUFFICIENT_EVIDENCE", "LEARNER_ABORT", "SYSTEM_FAILURE",
    }:
        raise ValueError("practice debrief outcome is unsupported")
    _evidence(root["evidence"])
    return root


@dataclass(frozen=True, slots=True)
class PracticeResultV1:
    """Strict envelope for every catalog, attempt, and guided-action result."""

    result_id: str
    status: str
    operation: str
    record: Mapping[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PracticeResultV1:
        root = _object(payload, "practice result")
        _exact(root, _RESULT_FIELDS, "practice result")
        _schema(root, PRACTICE_RESULT_SCHEMA_ID, "practice result")
        status = _text(root["status"], "practice result status")
        if status not in {"AVAILABLE", "UNAVAILABLE", "REFUSED", "DUPLICATE"}:
            raise ValueError("practice result status is unsupported")
        operation = _text(root["operation"], "practice result operation")
        if operation not in {"CATALOG", "BEGIN", "STAGE", "CONTINUE", "UNASSISTED", "DUPLICATE"}:
            raise ValueError("practice result operation is unsupported")
        attempt = None if root["attempt"] is None else _attempt_record(root["attempt"])
        if root["episode"] is not None:
            _episode(root["episode"])
        if root["source_run_id"] is not None:
            _id(root["source_run_id"], _RUN, "practice result source run ID")
        if root["current_frame"] is not None:
            SimulationFrameV1.from_dict(_object(root["current_frame"], "practice result frame"))
        if root["hold_id"] is not None:
            _id(root["hold_id"], _HOLD, "practice result hold ID")
        if not isinstance(root["assistance"], Sequence) or isinstance(root["assistance"], (str, bytes)):
            raise ValueError("practice result assistance must be an array")
        for item in root["assistance"]:
            assistance = _object(item, "practice assistance")
            _exact(assistance, _ASSISTANCE_FIELDS, "practice assistance")
            if assistance["kind"] not in {
                "PREPARATION_ATTRIBUTED", "GUIDED_STAGED", "GUIDED_FEEDBACK_NO_LIVE_COMMAND",
                "GUIDED_RELEASED", "UNASSISTED_DISPATCH", "DUPLICATE_REQUEST_FENCED",
            }:
                raise ValueError("practice assistance kind is unsupported")
            if type(assistance["simulation_time_us"]) is not int or assistance["simulation_time_us"] < 0:
                raise ValueError("practice assistance simulation time is invalid")
            _wall_time(assistance["wall_time"])
            _text(assistance["message"], "practice assistance message")
        assessment = None if root["assessment"] is None else _assessment(root["assessment"])
        debrief = None if root["debrief"] is None else _debrief(root["debrief"])
        unavailable = root["unavailable_reason"]
        if status == "AVAILABLE" and unavailable is not None:
            raise ValueError("available practice result cannot carry an unavailable reason")
        if status == "UNAVAILABLE" and type(unavailable) is not str:
            raise ValueError("unavailable practice result requires a reason")
        if status == "REFUSED" and type(unavailable) is not str:
            raise ValueError("refused practice result requires a reason")
        if status == "DUPLICATE" and unavailable != "DUPLICATE_REQUEST_ALREADY_PROGRESSED":
            raise ValueError("duplicate practice result requires its typed reason")
        if status not in {"AVAILABLE", "DUPLICATE"}:
            if root["current_frame"] is not None or root["hold_id"] is not None:
                raise ValueError("unavailable or refused practice result cannot carry a destination")
        if operation == "CATALOG":
            if any(value is not None for value in (attempt, root["episode"], root["source_run_id"], root["current_frame"], root["hold_id"], assessment, debrief)) or root["assistance"]:
                raise ValueError("catalog result must not carry attempt state")
        elif status in {"AVAILABLE", "DUPLICATE"}:
            if attempt is None or root["episode"] is None or root["source_run_id"] is None or root["current_frame"] is None:
                raise ValueError("available practice operation requires public attempt state")
            if operation == "BEGIN" and (assessment is not None or debrief is not None):
                raise ValueError("begin result cannot carry an action assessment")
            if operation == "DUPLICATE" and (status != "DUPLICATE" or assessment is not None or debrief is not None):
                raise ValueError("duplicate result has an invalid status or action payload")
            if operation in {"STAGE", "CONTINUE", "UNASSISTED"} and (assessment is None or debrief is None):
                raise ValueError("practice action result requires assessment and debrief")
            if attempt is not None:
                if (
                    attempt["episode_id"] != root["episode"]["episode_id"]
                    or attempt["episode_recipe_sha256"] != root["episode"]["recipe_sha256"]
                    or attempt["source_run_id"] != root["source_run_id"]
                    or root["current_frame"]["source_run_id"] != root["source_run_id"]
                ):
                    raise SimulationContractIntegrityError("practice result attempt identity does not bind its episode or frame")
                prepared_identity = SimulationEpisodeIdentityV1.from_dict(
                    _object(attempt["prepared_identity"], "practice result prepared identity")
                )
                episode_profile = SimulationProfileRefV1.from_dict(
                    _object(root["episode"]["profile_ref"], "practice result episode profile"),
                    label="practice result episode profile",
                )
                if (
                    prepared_identity.episode_id != attempt["episode_id"]
                    or prepared_identity.anchor_time_us != attempt["anchor_time_us"]
                    or prepared_identity.profile_ref != episode_profile
                    or prepared_identity.profile_ref.as_dict() != root["current_frame"]["profile_ref"]
                    or prepared_identity.resolved_configuration_sha256 != root["current_frame"]["resolved_configuration_sha256"]
                ):
                    raise SimulationContractIntegrityError("practice prepared identity does not bind its episode or current frame")
                cursor = root["current_frame"]["cursor"]
                if operation == "BEGIN":
                    if cursor["simulation_time_us"] != attempt["anchor_time_us"] or cursor["run_state"] != "PAUSED":
                        raise ValueError("practice begin result does not preserve its paused anchor")
                    if (attempt["mode"] == "GUIDED") != (root["hold_id"] is not None):
                        raise ValueError("practice begin hold does not match attempt mode")
                elif operation == "STAGE" and (attempt["mode"] != "GUIDED" or root["hold_id"] is None):
                    raise ValueError("practice stage requires an active guided hold")
                elif operation == "UNASSISTED" and (attempt["mode"] != "UNASSISTED" or root["hold_id"] is not None):
                    raise ValueError("unassisted result hold does not match attempt mode")
                elif operation == "CONTINUE":
                    completed = attempt["step_index"] >= attempt["step_count"]
                    if attempt["mode"] == "UNASSISTED" and root["hold_id"] is not None:
                        raise ValueError("unassisted continuation cannot carry a hold")
                    if attempt["mode"] == "GUIDED" and (
                        (completed and root["hold_id"] is not None)
                        or (not completed and root["hold_id"] is None)
                    ):
                        raise ValueError("guided continuation hold does not match completion")
                elif operation == "DUPLICATE" and attempt["mode"] == "UNASSISTED" and root["hold_id"] is not None:
                    raise ValueError("unassisted duplicate cannot carry a hold")
                if assessment is not None:
                    if debrief is None or debrief["source_run_id"] != root["source_run_id"] or debrief["outcome"] != assessment["outcome"] or debrief["evidence"] != assessment["evidence"] or debrief["action_request_id"] is None:
                        raise SimulationContractIntegrityError("practice debrief does not bind its assessment")
                    if attempt["step_count"] == 0:
                        if assessment["action_index"] != 0:
                            raise ValueError("declared-rule assessment action index is invalid")
                    elif assessment["action_index"] >= attempt["step_count"]:
                        raise ValueError("mechanical assessment action index exceeds the attempt")
        basis = {key: root[key] for key in root if key != "result_id"}
        result_id = _id(root["result_id"], _RESULT, "practice result ID")
        if result_id != f"practice-result-{canonical_digest(basis)[:24]}":
            raise SimulationContractIntegrityError("practice result ID does not match")
        return cls(result_id, status, operation, _freeze(root))

    def as_dict(self) -> dict[str, object]:
        return _plain(self.record)


def build_practice_result(**fields: object) -> dict[str, object]:
    basis = {"schema_id": PRACTICE_RESULT_SCHEMA_ID, "schema_version": 1, **fields}
    required = _RESULT_FIELDS - {"result_id"}
    if set(basis) != required:
        raise ValueError("practice result fields differ from V1")
    record = {**basis, "result_id": f"practice-result-{canonical_digest(basis)[:24]}"}
    return PracticeResultV1.from_dict(record).as_dict()


__all__ = [
    "PRACTICE_ACTION_REQUEST_SCHEMA_ID", "PRACTICE_ATTEMPT_REQUEST_SCHEMA_ID",
    "PRACTICE_CATALOG_SCHEMA_ID", "PRACTICE_RESULT_SCHEMA_ID",
    "PracticeActionRequestV1", "PracticeAttemptRequestV1", "PracticeCatalogV1",
    "PracticeResultV1",
    "build_practice_action_request", "build_practice_attempt_request", "build_practice_result",
]
