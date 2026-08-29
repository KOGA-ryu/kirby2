"""Deterministic curriculum contracts and disclosure-safe drill records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from kirby2.session.objectives import ObjectiveType, SessionObjective
from kirby2.simulation import LiquidityPreset, SeededRng, VolumePreset


MINED_CURRICULUM_LINEAGE_SCHEMA_VERSION = 1
_MINED_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MINED_CANDIDATE_ID = re.compile(r"^lesson-candidate-[0-9a-f]{64}$")
_MINED_SKILL_ID = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")


def _require_mined_sha256(value: object, label: str) -> str:
    if type(value) is not str or _MINED_SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class MinedCurriculumLineageV1:
    """Curriculum binding for a mined lesson without its withheld answer key."""

    candidate_id: str
    candidate_digest: str
    source_record_sha256: str
    source_envelope_sha256: str
    primary_skill_id: str
    supporting_skill_ids: tuple[str, ...]
    objective_kind: str = "OBSERVE_CLASSIFY_V1"
    schema_version: int = MINED_CURRICULUM_LINEAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.candidate_id) is not str
            or _MINED_CANDIDATE_ID.fullmatch(self.candidate_id) is None
        ):
            raise ValueError("mined curriculum candidate ID is invalid")
        digest = _require_mined_sha256(
            self.candidate_digest,
            "mined curriculum candidate digest",
        )
        if self.candidate_id != f"lesson-candidate-{digest}":
            raise ValueError("mined curriculum candidate ID and digest disagree")
        _require_mined_sha256(
            self.source_record_sha256,
            "mined curriculum source-record digest",
        )
        _require_mined_sha256(
            self.source_envelope_sha256,
            "mined curriculum source-envelope digest",
        )
        if (
            type(self.primary_skill_id) is not str
            or _MINED_SKILL_ID.fullmatch(self.primary_skill_id) is None
        ):
            raise ValueError("mined curriculum primary skill ID is invalid")
        if type(self.supporting_skill_ids) is not tuple or any(
            type(value) is not str or _MINED_SKILL_ID.fullmatch(value) is None
            for value in self.supporting_skill_ids
        ):
            raise ValueError("mined curriculum supporting skill IDs are invalid")
        if self.supporting_skill_ids != tuple(sorted(set(self.supporting_skill_ids))):
            raise ValueError("mined curriculum supporting skills must be sorted and unique")
        if self.primary_skill_id in self.supporting_skill_ids:
            raise ValueError("mined curriculum primary skill cannot be supporting")
        if self.objective_kind != "OBSERVE_CLASSIFY_V1":
            raise ValueError("mined curriculum objective kind is unsupported")
        if self.schema_version != MINED_CURRICULUM_LINEAGE_SCHEMA_VERSION:
            raise ValueError("mined curriculum lineage schema version is unsupported")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_digest": self.candidate_digest,
            "candidate_id": self.candidate_id,
            "objective_kind": self.objective_kind,
            "primary_skill_id": self.primary_skill_id,
            "schema_version": self.schema_version,
            "source_envelope_sha256": self.source_envelope_sha256,
            "source_record_sha256": self.source_record_sha256,
            "supporting_skill_ids": list(self.supporting_skill_ids),
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.as_dict(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


class CurriculumMode(str, Enum):
    LEARN = "LEARN"
    BLIND = "BLIND"

    @classmethod
    def parse(cls, value: str) -> CurriculumMode:
        return cls(value.upper())


@dataclass(frozen=True, slots=True)
class LessonObjectiveTemplate:
    objective_type: ObjectiveType
    target_quantities: tuple[int, ...]
    preferred_slippage_ticks: int

    def __post_init__(self) -> None:
        if not isinstance(self.objective_type, ObjectiveType):
            raise TypeError("lesson objective type must be an ObjectiveType")
        if not self.target_quantities:
            raise ValueError("lesson objective requires at least one target quantity")
        if any(type(value) is not int or value < 0 for value in self.target_quantities):
            raise ValueError("lesson target quantities must be nonnegative integers")
        if len(set(self.target_quantities)) != len(self.target_quantities):
            raise ValueError("lesson target quantities must be unique")
        if self.objective_type is ObjectiveType.OBSERVE_ONLY:
            if self.target_quantities != (0,):
                raise ValueError("OBSERVE_ONLY lesson target must be exactly zero")
        elif any(value <= 0 for value in self.target_quantities):
            raise ValueError("trading lesson targets must be positive")
        if (
            type(self.preferred_slippage_ticks) is not int
            or self.preferred_slippage_ticks < 0
        ):
            raise ValueError("lesson preferred slippage must be nonnegative")

    def select(
        self,
        rng: SeededRng,
        duration_seconds: int,
    ) -> SessionObjective:
        target = self.target_quantities[rng.index(len(self.target_quantities))]
        return SessionObjective(
            objective_type=self.objective_type,
            target_quantity=target,
            time_limit_us=duration_seconds * 1_000_000,
            preferred_slippage_ticks=self.preferred_slippage_ticks,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "objective_type": self.objective_type.value,
            "preferred_slippage_ticks": self.preferred_slippage_ticks,
            "target_quantities": list(self.target_quantities),
        }


@dataclass(frozen=True, slots=True)
class CurriculumLesson:
    lesson_id: str
    title: str
    learning_objective: str
    scenario_name: str
    volumes: tuple[VolumePreset, ...]
    liquidities: tuple[LiquidityPreset, ...]
    seed_pool: tuple[int, ...]
    duration_seconds: tuple[int, ...]
    player_objective: LessonObjectiveTemplate
    post_session_explanation: str

    def __post_init__(self) -> None:
        if len(self.lesson_id) != 2 or not self.lesson_id.isdigit():
            raise ValueError("lesson ID must contain exactly two digits")
        if not self.title or not self.learning_objective or not self.scenario_name:
            raise ValueError("lesson title, objective, and scenario must not be empty")
        if not self.post_session_explanation:
            raise ValueError("lesson requires a post-session explanation")
        if not self.volumes or not self.liquidities:
            raise ValueError("lesson volume and liquidity bounds must not be empty")
        if len(set(self.volumes)) != len(self.volumes):
            raise ValueError("lesson volume bounds must be unique")
        if len(set(self.liquidities)) != len(self.liquidities):
            raise ValueError("lesson liquidity bounds must be unique")
        if len(self.seed_pool) < 2 or len(set(self.seed_pool)) != len(self.seed_pool):
            raise ValueError("lesson seed pool requires at least two unique seeds")
        if any(type(seed) is not int or seed < 0 for seed in self.seed_pool):
            raise ValueError("lesson seeds must be nonnegative integers")
        if not self.duration_seconds or any(
            type(seconds) is not int or seconds <= 0
            for seconds in self.duration_seconds
        ):
            raise ValueError("lesson durations must be positive integers")
        if len(set(self.duration_seconds)) != len(self.duration_seconds):
            raise ValueError("lesson durations must be unique")

    def prepare(
        self,
        mode: CurriculumMode,
        variation_seed: int,
    ) -> CurriculumDrill:
        if not isinstance(mode, CurriculumMode):
            raise TypeError("curriculum mode must be LEARN or BLIND")
        if type(variation_seed) is not int or variation_seed < 0:
            raise ValueError("variation seed must be a nonnegative integer")
        rng = SeededRng(variation_seed)
        scenario_seed = self.seed_pool[rng.index(len(self.seed_pool))]
        volume = self.volumes[rng.index(len(self.volumes))]
        liquidity = self.liquidities[rng.index(len(self.liquidities))]
        duration_seconds = self.duration_seconds[
            rng.index(len(self.duration_seconds))
        ]
        objective = self.player_objective.select(rng, duration_seconds)
        selected = {
            "duration_seconds": duration_seconds,
            "lesson_id": self.lesson_id,
            "liquidity": liquidity.value,
            "objective": objective.as_dict(),
            "scenario_name": self.scenario_name,
            "scenario_seed": scenario_seed,
            "volume": volume.value,
        }
        variation_id = _variation_id(selected)
        return CurriculumDrill(
            lesson_id=self.lesson_id,
            title=self.title,
            mode=mode,
            learning_objective=self.learning_objective,
            scenario_name=self.scenario_name,
            scenario_seed=scenario_seed,
            volume=volume,
            liquidity=liquidity,
            duration_seconds=duration_seconds,
            variation_seed=variation_seed,
            variation_id=variation_id,
            player_objective=objective,
            post_session_explanation=self.post_session_explanation,
        )

    def catalog_dict(self) -> dict[str, object]:
        return {
            "learning_objective": self.learning_objective,
            "lesson_id": self.lesson_id,
            "player_objective": self.player_objective.as_dict(),
            "title": self.title,
            "variation_dimensions": {
                "duration_count": len(self.duration_seconds),
                "liquidity_count": len(self.liquidities),
                "seed_count": len(self.seed_pool),
                "target_count": len(self.player_objective.target_quantities),
                "volume_count": len(self.volumes),
            },
        }

    def assert_contains(self, drill: CurriculumDrill) -> None:
        if (
            drill.lesson_id != self.lesson_id
            or drill.title != self.title
            or drill.learning_objective != self.learning_objective
            or drill.scenario_name != self.scenario_name
            or drill.post_session_explanation != self.post_session_explanation
        ):
            raise ValueError("curriculum drill does not match its canonical lesson")
        if drill.scenario_seed not in self.seed_pool:
            raise ValueError("curriculum drill seed is outside its lesson pool")
        if drill.volume not in self.volumes:
            raise ValueError("curriculum drill volume is outside lesson bounds")
        if drill.liquidity not in self.liquidities:
            raise ValueError("curriculum drill liquidity is outside lesson bounds")
        if drill.duration_seconds not in self.duration_seconds:
            raise ValueError("curriculum drill duration is outside lesson bounds")
        objective = drill.player_objective
        template = self.player_objective
        if (
            objective.objective_type is not template.objective_type
            or objective.target_quantity not in template.target_quantities
            or objective.preferred_slippage_ticks
            != template.preferred_slippage_ticks
            or objective.time_limit_us != drill.duration_seconds * 1_000_000
        ):
            raise ValueError("curriculum player objective is outside lesson bounds")
        if drill != self.prepare(drill.mode, drill.variation_seed):
            raise ValueError("curriculum variation seed does not reproduce the drill")


@dataclass(frozen=True, slots=True)
class CurriculumDrill:
    """Portable selected drill; hidden fields are disclosed only after completion."""

    lesson_id: str
    title: str
    mode: CurriculumMode
    learning_objective: str
    scenario_name: str
    scenario_seed: int
    volume: VolumePreset
    liquidity: LiquidityPreset
    duration_seconds: int
    variation_seed: int
    variation_id: str
    player_objective: SessionObjective
    post_session_explanation: str

    def __post_init__(self) -> None:
        if len(self.lesson_id) != 2 or not self.lesson_id.isdigit():
            raise ValueError("curriculum drill lesson ID must contain two digits")
        if not isinstance(self.mode, CurriculumMode):
            raise TypeError("curriculum drill mode must be LEARN or BLIND")
        if any(
            not value
            for value in (
                self.title,
                self.learning_objective,
                self.scenario_name,
                self.variation_id,
                self.post_session_explanation,
            )
        ):
            raise ValueError("curriculum drill text fields must not be empty")
        if type(self.scenario_seed) is not int or self.scenario_seed < 0:
            raise ValueError("curriculum scenario seed must be nonnegative")
        if type(self.variation_seed) is not int or self.variation_seed < 0:
            raise ValueError("curriculum variation seed must be nonnegative")
        if type(self.duration_seconds) is not int or self.duration_seconds <= 0:
            raise ValueError("curriculum drill duration must be positive")
        if self.player_objective.time_limit_us > self.duration_seconds * 1_000_000:
            raise ValueError("curriculum objective exceeds drill duration")

    @property
    def live_scenario_label(self) -> str:
        if self.mode is CurriculumMode.LEARN:
            return f"lesson_{self.lesson_id}"
        return "blind_drill"

    @property
    def live_regime_label(self) -> str:
        return "HIDDEN"

    @property
    def live_dimension_label(self) -> str:
        return "HIDDEN"

    def render_briefing(self) -> str:
        if self.mode is CurriculumMode.LEARN:
            drill = f"{self.lesson_id} {self.title}"
            learning = self.learning_objective
        else:
            drill = "BLIND DRILL"
            learning = "WITHHELD UNTIL COMPLETION"
        return "\n".join(
            (
                "KIRBY2_CURRICULUM_BRIEFING",
                f"MODE {self.mode.value}",
                f"DRILL {drill}",
                f"LEARNING_OBJECTIVE {learning}",
                f"PLAYER_OBJECTIVE {self.player_objective.describe()}",
                f"VARIATION_ID {self.variation_id}",
                "HIDDEN_CONFIGURATION concealed_until_completion=true",
            )
        )

    def render_debrief(self) -> str:
        return "\n".join(
            (
                "KIRBY2_CURRICULUM_DEBRIEF",
                f"LESSON {self.lesson_id} {self.title}",
                f"MODE {self.mode.value}",
                f"LEARNING_OBJECTIVE {self.learning_objective}",
                (
                    f"CONFIG scenario={self.scenario_name} seed={self.scenario_seed} "
                    f"volume={self.volume.value} liquidity={self.liquidity.value} "
                    f"duration_seconds={self.duration_seconds}"
                ),
                f"PLAYER_OBJECTIVE {self.player_objective.describe()}",
                f"EXPLANATION {self.post_session_explanation}",
                f"VARIATION_ID {self.variation_id} variation_seed={self.variation_seed}",
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "duration_seconds": self.duration_seconds,
            "learning_objective": self.learning_objective,
            "lesson_id": self.lesson_id,
            "liquidity": self.liquidity.value,
            "mode": self.mode.value,
            "player_objective": self.player_objective.as_dict(),
            "post_session_explanation": self.post_session_explanation,
            "scenario_name": self.scenario_name,
            "scenario_seed": self.scenario_seed,
            "title": self.title,
            "variation_id": self.variation_id,
            "variation_seed": self.variation_seed,
            "volume": self.volume.value,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CurriculumDrill:
        objective = payload.get("player_objective")
        if not isinstance(objective, dict):
            raise ValueError("curriculum drill player objective must be an object")
        drill = cls(
            lesson_id=str(payload["lesson_id"]),
            title=str(payload["title"]),
            mode=CurriculumMode.parse(str(payload["mode"])),
            learning_objective=str(payload["learning_objective"]),
            scenario_name=str(payload["scenario_name"]),
            scenario_seed=int(payload["scenario_seed"]),
            volume=VolumePreset.parse(str(payload["volume"])),
            liquidity=LiquidityPreset.parse(str(payload["liquidity"])),
            duration_seconds=int(payload["duration_seconds"]),
            variation_seed=int(payload["variation_seed"]),
            variation_id=str(payload["variation_id"]),
            player_objective=SessionObjective.from_dict(objective),
            post_session_explanation=str(payload["post_session_explanation"]),
        )
        selected = {
            "duration_seconds": drill.duration_seconds,
            "lesson_id": drill.lesson_id,
            "liquidity": drill.liquidity.value,
            "objective": drill.player_objective.as_dict(),
            "scenario_name": drill.scenario_name,
            "scenario_seed": drill.scenario_seed,
            "volume": drill.volume.value,
        }
        if drill.variation_id != _variation_id(selected):
            raise ValueError("curriculum variation ID does not match selected bounds")
        return drill


def _variation_id(selected: dict[str, object]) -> str:
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
