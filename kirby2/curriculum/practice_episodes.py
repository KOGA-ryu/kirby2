"""Small immutable catalog for the process-local Chapter 1 decision repeater."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .skills import require_stable_skill_v1


PRACTICE_EPISODE_CATALOG_ID_V1 = "KIRBY2_PRACTICE_EPISODE_CATALOG_V1"
PRACTICE_EPISODE_SCHEMA_ID_V1 = "KIRBY2_PRACTICE_EPISODE_DEFINITION_V1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PracticeEpisodeDefinitionV1:
    episode_id: str
    family: str
    title: str
    variation_of: str | None
    profile_ref: dict[str, object]
    seed: int
    control_values: dict[str, object]
    duration_us: int
    preparation_actions: tuple[str, ...]
    anchor_time_us: int
    primary_skill_id: str
    objective_class: str
    expected_actions: tuple[str, ...]
    observation_rule: dict[str, object]

    def __post_init__(self) -> None:
        if self.family not in {"F1_CONTROL", "F2_READING", "F3_RESIDUAL"}:
            raise ValueError("practice episode family is unsupported")
        if not self.episode_id.startswith("practice.") or self.episode_id != self.episode_id.strip():
            raise ValueError("practice episode ID is invalid")
        if self.variation_of is not None and not self.variation_of.startswith("practice."):
            raise ValueError("practice variation parent is invalid")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("practice episode seed is invalid")
        if type(self.duration_us) is not int or self.duration_us <= self.anchor_time_us:
            raise ValueError("practice episode duration or anchor is invalid")
        if type(self.anchor_time_us) is not int or self.anchor_time_us <= 0:
            raise ValueError("practice episode anchor is invalid")
        if not self.preparation_actions or any(
            type(action) is not str or not action.startswith(("SIMULATION_", "PLAYER_"))
            for action in self.preparation_actions
        ):
            raise ValueError("practice preparation actions are invalid")
        if any(type(action) is not str or not action.startswith("PLAYER_") for action in self.expected_actions):
            raise ValueError("practice expected actions are invalid")
        if self.objective_class not in {
            "EXACT_MECHANICAL",
            "DECLARED_RULE",
            "OPEN_JUDGMENT",
        }:
            raise ValueError("practice objective class is invalid")
        require_stable_skill_v1(self.primary_skill_id)

    def recipe_basis(self) -> dict[str, object]:
        return {
            "schema_id": PRACTICE_EPISODE_SCHEMA_ID_V1,
            "schema_version": 1,
            "episode_id": self.episode_id,
            "family": self.family,
            "title": self.title,
            "variation_of": self.variation_of,
            "profile_ref": self.profile_ref,
            "seed": self.seed,
            "control_values": self.control_values,
            "duration_us": self.duration_us,
            "preparation_actions": list(self.preparation_actions),
            "anchor_time_us": self.anchor_time_us,
            "primary_skill_id": self.primary_skill_id,
            "objective_class": self.objective_class,
            "expected_actions": list(self.expected_actions),
            "observation_rule": self.observation_rule,
        }

    @property
    def recipe_sha256(self) -> str:
        return _digest(self.recipe_basis())

    def as_dict(self) -> dict[str, object]:
        return {**self.recipe_basis(), "recipe_sha256": self.recipe_sha256}


_BALANCED_REF = {
    "profile_id": "accepted.balanced.simple",
    "profile_version": 1,
    "profile_sha256": "f63a5a6cdf0041e2032af11a8ef86758ec1b2fb0b73ff7000f53ccda56e27b53",
}
_BUY_PRESSURE_REF = {
    "profile_id": "accepted.buy_pressure.simple",
    "profile_version": 1,
    "profile_sha256": "df1b852febe91986dae51cd642d096184cf2036d56b4132227157b2d2d4868bd",
}
_STANDARD_CONTROLS = {
    "relative_volume": "1.00x",
    "liquidity": "NORMAL",
    "intensity_scale_ppm": 1_000_000,
}
_PRESSURE_RULE = {
    "rule_id": "PUBLIC_PRESSURE_REPLENISHMENT_V1",
    "permitted_observations": ["book", "recent_trades"],
    "pressure_minimum_recent_trades": 8,
    "pressure_minimum_bid_minus_ask_quantity": 500,
    "replenishment_minimum_ask_quantity": 1_000,
    "answers": [
        "PRESSURE_PRESENT",
        "REPLENISHMENT_BLOCKS",
        "INSUFFICIENT_EVIDENCE",
        "WAIT",
        "DECLINE",
    ],
    "unavailable_answer": "INSUFFICIENT_EVIDENCE",
    "no_opportunity_answer": "WAIT",
}


PRACTICE_EPISODES_V1 = (
    PracticeEpisodeDefinitionV1(
        "practice.f1.place-and-cancel.v1",
        "F1_CONTROL",
        "Select quantity, place, recognize, and cancel a resting bid",
        None,
        _BALANCED_REF,
        101,
        _STANDARD_CONTROLS,
        90_000_000,
        ("SIMULATION_PLAY",),
        1,
        "CANCEL_TIMING",
        "EXACT_MECHANICAL",
        (
            "PLAYER_INCREASE_QUANTITY",
            "PLAYER_BUY_BID",
            "PLAYER_CANCEL_NEAREST",
        ),
        {
            "instruction": "Increase to 200, place a bid, verify it rests, then cancel it.",
            "quantity": 200,
            "requires_learner_placement": True,
        },
    ),
    PracticeEpisodeDefinitionV1(
        "practice.f1.place-and-replace.v1",
        "F1_CONTROL",
        "Select quantity, place, recognize, and replace a resting bid",
        "practice.f1.place-and-cancel.v1",
        _BALANCED_REF,
        102,
        _STANDARD_CONTROLS,
        90_000_000,
        ("SIMULATION_PLAY",),
        1,
        "REPLACE_TIMING",
        "EXACT_MECHANICAL",
        (
            "PLAYER_DECREASE_QUANTITY",
            "PLAYER_BUY_BID",
            "PLAYER_REPLACE_NEAREST",
        ),
        {
            "instruction": "Decrease to 50, place a bid, verify it rests, then replace it.",
            "quantity": 50,
            "requires_learner_placement": True,
        },
    ),
    PracticeEpisodeDefinitionV1(
        "practice.f2.public-pressure.v1",
        "F2_READING",
        "Classify public pressure without a hidden label",
        None,
        _BUY_PRESSURE_REF,
        202,
        _STANDARD_CONTROLS,
        90_000_000,
        ("SIMULATION_PLAY",),
        1_000_000,
        "TAPE_READING",
        "DECLARED_RULE",
        (),
        _PRESSURE_RULE,
    ),
    PracticeEpisodeDefinitionV1(
        "practice.f2.replenishment.v1",
        "F2_READING",
        "Recognize public replenishment blocking pressure",
        "practice.f2.public-pressure.v1",
        _BALANCED_REF,
        101,
        _STANDARD_CONTROLS,
        90_000_000,
        ("SIMULATION_PLAY",),
        1_000_000,
        "BOOK_READING",
        "DECLARED_RULE",
        (),
        _PRESSURE_RULE,
    ),
    PracticeEpisodeDefinitionV1(
        "practice.f3.cancel-partial-residual.v1",
        "F3_RESIDUAL",
        "Cancel a real residual after a partial fill",
        None,
        _BUY_PRESSURE_REF,
        202,
        _STANDARD_CONTROLS,
        90_000_000,
        ("SIMULATION_PLAY", "PLAYER_BUY_BID"),
        1_000_000,
        "PARTIAL_FILL_MANAGEMENT",
        "EXACT_MECHANICAL",
        ("PLAYER_CANCEL_NEAREST",),
        {
            "invalidation": "The one player bid has already partially filled; cancel its real remainder.",
            "requires_single_order_partial_fill": True,
            "minimum_position": 1,
            "minimum_remaining_quantity": 1,
        },
    ),
    PracticeEpisodeDefinitionV1(
        "practice.f3.cancel-volume-variation.v1",
        "F3_RESIDUAL",
        "Cancel a residual under a distinct volume configuration",
        "practice.f3.cancel-partial-residual.v1",
        _BUY_PRESSURE_REF,
        190,
        {
            "relative_volume": "0.50x",
            "liquidity": "NORMAL",
            "intensity_scale_ppm": 1_000_000,
        },
        90_000_000,
        ("SIMULATION_PLAY", "PLAYER_BUY_BID"),
        1_000_000,
        "PARTIAL_FILL_MANAGEMENT",
        "EXACT_MECHANICAL",
        ("PLAYER_CANCEL_NEAREST",),
        {
            "invalidation": "The one player bid has already partially filled; cancel its real remainder.",
            "requires_single_order_partial_fill": True,
            "minimum_position": 1,
            "minimum_remaining_quantity": 1,
        },
    ),
)


def get_practice_episode_v1(episode_id: str) -> PracticeEpisodeDefinitionV1:
    if type(episode_id) is not str:
        raise ValueError("practice episode ID must be text")
    for episode in PRACTICE_EPISODES_V1:
        if episode.episode_id == episode_id:
            return episode
    raise ValueError("unknown practice episode")


def list_practice_episodes_v1() -> tuple[PracticeEpisodeDefinitionV1, ...]:
    return PRACTICE_EPISODES_V1


if len(PRACTICE_EPISODES_V1) != 6 or len({item.episode_id for item in PRACTICE_EPISODES_V1}) != 6:
    raise RuntimeError("practice episode catalog must contain six unique recipes")
