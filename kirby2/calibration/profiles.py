"""Reusable calibrated market profiles and auditable search records."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .models import CalibrationReport


CALIBRATION_SOFTWARE_VERSION = "kirby2-0.1.0+calibration-v1"
MARKET_PROFILE_SCHEMA_VERSION = 1
CALIBRATION_RUN_SCHEMA_VERSION = 1


class DistanceKind(str, Enum):
    SYMMETRIC_SCALAR = "SYMMETRIC_SCALAR"
    QUANTILE_SHAPE = "QUANTILE_SHAPE"
    COMPONENT_VECTOR = "COMPONENT_VECTOR"
    VOLUME_PROFILE = "VOLUME_PROFILE"


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    stage: int
    grid: tuple[float, ...]
    default: float = 1.0

    def __post_init__(self) -> None:
        if not self.name or self.stage not in {1, 2, 3, 4}:
            raise ValueError("parameter identity and calibration stage are invalid")
        if (
            not self.grid
            or tuple(sorted(set(self.grid))) != self.grid
            or any(not math.isfinite(value) for value in self.grid)
            or self.default not in self.grid
        ):
            raise ValueError("parameter grid must be finite, unique, ordered, and include default")

    @property
    def lower(self) -> float:
        return self.grid[0]

    @property
    def upper(self) -> float:
        return self.grid[-1]

    def validate(self, value: float) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or not self.lower <= parsed <= self.upper:
            raise ValueError(
                f"{self.name}={parsed} is outside bounds [{self.lower}, {self.upper}]"
            )
        if self.name == "initial_half_spread_ticks" and not parsed.is_integer():
            raise ValueError("initial_half_spread_ticks must be an integer")
        return parsed

    def as_dict(self, *, fixed: bool = False) -> dict[str, object]:
        return {
            "default": self.default,
            "fitted": not fixed,
            "grid": list(self.grid),
            "lower": self.lower,
            "name": self.name,
            "stage": self.stage,
            "upper": self.upper,
        }


@dataclass(frozen=True, slots=True)
class ObjectiveTerm:
    metric: str
    distance: DistanceKind
    weight: float
    stage: int
    components: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.metric or self.stage not in {1, 2, 3, 4}:
            raise ValueError("objective metric and stage are invalid")
        if not math.isfinite(self.weight) or self.weight <= 0:
            raise ValueError("objective weight must be finite and positive")
        if self.distance is DistanceKind.COMPONENT_VECTOR and not self.components:
            raise ValueError("component-vector distance requires components")

    def as_dict(self) -> dict[str, object]:
        return {
            "components": list(self.components),
            "distance": self.distance.value,
            "metric": self.metric,
            "stage": self.stage,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class CalibrationObjective:
    objective_id: str
    terms: tuple[ObjectiveTerm, ...]

    def __post_init__(self) -> None:
        if not self.objective_id or not self.terms:
            raise ValueError("calibration objective identity and terms are required")
        identities = tuple((term.metric, term.stage) for term in self.terms)
        if len(identities) != len(set(identities)):
            raise ValueError("objective cannot repeat a metric within one stage")

    def through_stage(self, stage: int) -> CalibrationObjective:
        selected = tuple(term for term in self.terms if term.stage <= stage)
        return CalibrationObjective(f"{self.objective_id}:through_stage_{stage}", selected)

    def as_dict(self) -> dict[str, object]:
        return {
            "formula": "sum(weight_i * distance_i) / sum(active_weight_i)",
            "objective_id": self.objective_id,
            "terms": [term.as_dict() for term in self.terms],
        }


@dataclass(frozen=True, slots=True)
class MarketProfile:
    profile_id: str
    scenario_name: str
    regime: str
    parameters: dict[str, float]
    fixed_parameters: tuple[str, ...]
    reference_dataset_id: str
    objective_id: str
    software_config_version: str = CALIBRATION_SOFTWARE_VERSION

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.profile_id,
                self.scenario_name,
                self.regime,
                self.reference_dataset_id,
                self.objective_id,
                self.software_config_version,
            )
        ):
            raise ValueError("market profile identity fields are required")
        if any(
            not name or not math.isfinite(value)
            for name, value in self.parameters.items()
        ):
            raise ValueError("market profile parameters must be named and finite")
        if set(self.fixed_parameters) - set(self.parameters):
            raise ValueError("fixed parameter names must exist in the profile")

    def as_dict(self) -> dict[str, object]:
        return {
            "fixed_parameters": list(self.fixed_parameters),
            "objective_id": self.objective_id,
            "parameters": dict(sorted(self.parameters.items())),
            "profile_id": self.profile_id,
            "reference_dataset_id": self.reference_dataset_id,
            "regime": self.regime,
            "scenario_name": self.scenario_name,
            "schema_version": MARKET_PROFILE_SCHEMA_VERSION,
            "software_config_version": self.software_config_version,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MarketProfile:
        if payload.get("schema_version") != MARKET_PROFILE_SCHEMA_VERSION:
            raise ValueError("unsupported calibrated market-profile schema")
        parameters = payload.get("parameters")
        fixed = payload.get("fixed_parameters", [])
        if not isinstance(parameters, dict) or not isinstance(fixed, list):
            raise ValueError("market-profile parameters are malformed")
        return cls(
            profile_id=str(payload["profile_id"]),
            scenario_name=str(payload["scenario_name"]),
            regime=str(payload["regime"]),
            parameters={str(name): float(value) for name, value in parameters.items()},
            fixed_parameters=tuple(str(value) for value in fixed),
            reference_dataset_id=str(payload["reference_dataset_id"]),
            objective_id=str(payload["objective_id"]),
            software_config_version=str(payload["software_config_version"]),
        )


@dataclass(frozen=True, slots=True)
class CalibrationEvaluation:
    parameters: dict[str, float]
    mean_loss: float
    seed_losses: tuple[tuple[int, float], ...]
    reports: tuple[CalibrationReport, ...]

    def __post_init__(self) -> None:
        if len(self.seed_losses) != len(self.reports) or not self.reports:
            raise ValueError("calibration evaluation seeds and reports must align")
        if not math.isfinite(self.mean_loss) or self.mean_loss < 0:
            raise ValueError("calibration loss must be finite and nonnegative")

    def as_dict(self, *, include_final_metrics: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "mean_loss": self.mean_loss,
            "parameters": dict(sorted(self.parameters.items())),
            "seed_losses": [
                {"loss": loss, "seed": seed} for seed, loss in self.seed_losses
            ],
        }
        if include_final_metrics:
            result["final_metrics"] = {
                str(seed): report.as_dict()
                for (seed, _), report in zip(self.seed_losses, self.reports)
            }
        else:
            result["report_sha256"] = {
                str(seed): report.normalized_stream_sha256
                for (seed, _), report in zip(self.seed_losses, self.reports)
            }
        return result


@dataclass(frozen=True, slots=True)
class CalibrationStageOutcome:
    stage: int
    fitted_parameters: tuple[str, ...]
    candidate_count: int
    starting_loss: float
    best_loss: float

    def as_dict(self) -> dict[str, object]:
        return {
            "best_loss": self.best_loss,
            "candidate_count": self.candidate_count,
            "fitted_parameters": list(self.fitted_parameters),
            "stage": self.stage,
            "starting_loss": self.starting_loss,
        }


@dataclass(frozen=True, slots=True)
class CalibrationRun:
    reference_report: CalibrationReport
    scenario_definition: dict[str, object]
    parameter_specs: tuple[ParameterSpec, ...]
    fixed_parameters: dict[str, float]
    fitting_seeds: tuple[int, ...]
    heldout_seeds: tuple[int, ...]
    search_seed: int
    search_method: str
    objective: CalibrationObjective
    stage_outcomes: tuple[CalibrationStageOutcome, ...]
    initial_fitting: CalibrationEvaluation
    final_fitting: CalibrationEvaluation
    initial_heldout: CalibrationEvaluation
    final_heldout: CalibrationEvaluation
    market_profile: MarketProfile
    software_config_version: str = CALIBRATION_SOFTWARE_VERSION

    @property
    def heldout_improved(self) -> bool:
        return self.final_heldout.mean_loss < self.initial_heldout.mean_loss

    @property
    def heldout_improvement(self) -> float:
        return round(
            self.initial_heldout.mean_loss - self.final_heldout.mean_loss,
            9,
        )

    def as_dict(self) -> dict[str, object]:
        fixed = set(self.fixed_parameters)
        return {
            "best_parameters": dict(sorted(self.market_profile.parameters.items())),
            "final_fitting": self.final_fitting.as_dict(),
            "final_heldout": self.final_heldout.as_dict(include_final_metrics=True),
            "fixed_parameters": dict(sorted(self.fixed_parameters.items())),
            "heldout_improved": self.heldout_improved,
            "heldout_improvement": self.heldout_improvement,
            "initial_fitting": self.initial_fitting.as_dict(),
            "initial_heldout": self.initial_heldout.as_dict(),
            "market_profile": self.market_profile.as_dict(),
            "objective_definition": self.objective.as_dict(),
            "parameter_bounds": [
                spec.as_dict(fixed=spec.name in fixed) for spec in self.parameter_specs
            ],
            "reference_dataset_id": self.reference_report.source_id,
            "reference_report": self.reference_report.as_dict(),
            "schema_version": CALIBRATION_RUN_SCHEMA_VERSION,
            "search": {
                "method": self.search_method,
                "search_seed": self.search_seed,
            },
            "seed_set": {
                "fitting": list(self.fitting_seeds),
                "heldout": list(self.heldout_seeds),
            },
            "software_config_version": self.software_config_version,
            "stage_outcomes": [outcome.as_dict() for outcome in self.stage_outcomes],
            "scenario_definition": self.scenario_definition,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
