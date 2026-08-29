"""Validated manifests for controlled strategy experiments."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kirby2.exchange import Side
from kirby2.strategy import TrafficState, parse_strategy

if TYPE_CHECKING:
    from kirby2.discovery.identity import StrategyIdentityV1


EXPERIMENT_MANIFEST_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ExperimentMode(str, Enum):
    PASSIVE_OBSERVER = "PASSIVE_OBSERVER"
    FORKED_EXECUTION = "FORKED_EXECUTION"


@dataclass(frozen=True, slots=True)
class StrategyVariant:
    name: str
    source: str
    source_path: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError("strategy variant has an invalid name")
        if self.source_path is not None and (
            type(self.source_path) is not str or not self.source_path
        ):
            raise ValueError("strategy variant source path must be nonempty text")
        parse_strategy(self.source)

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    @property
    def strategy_identity(self) -> StrategyIdentityV1:
        """Expose semantic identity beside, never in place of, the legacy hash."""

        from kirby2.discovery.identity import (
            StrategyImportOriginV1,
            strategy_identity_from_source,
        )

        imported_file = self.source_path is not None
        return strategy_identity_from_source(
            self.source,
            import_origin=(
                StrategyImportOriginV1.EXPERIMENT_RULE_FILE
                if imported_file
                else StrategyImportOriginV1.EXPERIMENT_INLINE_SOURCE
            ),
            logical_source=(
                self.source_path
                if imported_file
                else f"experiment-inline:{self.name}"
            ),
        )

    @property
    def semantic_ast_sha256(self) -> str:
        return self.strategy_identity.semantic_ast_sha256

    def discovery_identity_dict(self) -> dict[str, object]:
        """New sidecar projection; legacy ``as_dict`` remains byte-compatible."""

        return self.strategy_identity.as_dict()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: str
    mode: ExperimentMode
    scenario_names: tuple[str, ...]
    seeds: tuple[int, ...]
    strategies: tuple[StrategyVariant, ...]
    duration_us: int
    fork_time_us: int
    decision_interval_us: int
    quantity: int
    entry_side: Side = Side.BUY
    exit_signals: tuple[TrafficState, ...] = (TrafficState.RED,)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.experiment_id):
            raise ValueError("experiment ID is invalid")
        if not isinstance(self.mode, ExperimentMode):
            raise TypeError("experiment mode is invalid")
        if (
            not self.scenario_names
            or len(self.scenario_names) != len(set(self.scenario_names))
            or any(not _IDENTIFIER.fullmatch(name) for name in self.scenario_names)
            or any(name != name.lower() for name in self.scenario_names)
        ):
            raise ValueError(
                "experiment scenarios must be nonempty, unique, lowercase names"
            )
        if len(self.seeds) < 2 or len(self.seeds) != len(set(self.seeds)):
            raise ValueError("controlled experiments require at least two unique seeds")
        if any(type(seed) is not int for seed in self.seeds):
            raise TypeError("experiment seeds must be integers")
        names = tuple(strategy.name for strategy in self.strategies)
        if len(names) < 2 or len(names) != len(set(names)):
            raise ValueError("controlled experiments require two or more named strategies")
        if (
            type(self.duration_us) is not int
            or self.duration_us <= 0
            or type(self.fork_time_us) is not int
            or not 0 <= self.fork_time_us < self.duration_us
            or type(self.decision_interval_us) is not int
            or self.decision_interval_us <= 0
            or type(self.quantity) is not int
            or self.quantity <= 0
        ):
            raise ValueError("experiment timing and quantity values are invalid")
        if self.duration_us % 1_000_000:
            raise ValueError("experiment duration must be a whole number of seconds")
        if not self.exit_signals or len(self.exit_signals) != len(set(self.exit_signals)):
            raise ValueError("exit signals must be nonempty and unique")

    @property
    def comparison_count(self) -> int:
        return len(self.scenario_names) * len(self.seeds)

    @property
    def winner_eligible(self) -> bool:
        return len(self.scenario_names) > 1

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_interval_us": self.decision_interval_us,
            "duration_us": self.duration_us,
            "entry_side": self.entry_side.value,
            "exit_signals": [signal.value for signal in self.exit_signals],
            "experiment_id": self.experiment_id,
            "fork_time_us": self.fork_time_us,
            "mode": self.mode.value,
            "quantity": self.quantity,
            "scenario_names": list(self.scenario_names),
            "schema_version": EXPERIMENT_MANIFEST_SCHEMA_VERSION,
            "seeds": list(self.seeds),
            "strategies": [strategy.as_dict() for strategy in self.strategies],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path: Path) -> ExperimentManifest:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("experiment manifest must contain a JSON object")
        return cls.from_dict(payload, path.parent)

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        base_directory: Path | None = None,
    ) -> ExperimentManifest:
        schema_version = payload.get(
            "schema_version",
            EXPERIMENT_MANIFEST_SCHEMA_VERSION,
        )
        if schema_version != EXPERIMENT_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported experiment manifest schema version")
        raw_strategies = payload.get("strategies")
        raw_scenarios = payload.get("scenario_names", payload.get("scenarios"))
        raw_seeds = payload.get("seeds")
        raw_exit_signals = payload.get("exit_signals", [TrafficState.RED.value])
        if not isinstance(raw_strategies, list) or not all(
            isinstance(item, dict) for item in raw_strategies
        ):
            raise ValueError("manifest strategies must be an array of objects")
        if not isinstance(raw_scenarios, list) or not all(
            isinstance(item, str) for item in raw_scenarios
        ):
            raise ValueError("manifest scenarios must be an array of names")
        if not isinstance(raw_seeds, list):
            raise ValueError("manifest seeds must be an array")
        if not all(type(value) is int for value in raw_seeds):
            raise TypeError("manifest seeds must contain integers")
        if not isinstance(raw_exit_signals, list):
            raise ValueError("manifest exit signals must be an array")
        if not isinstance(payload.get("experiment_id"), str):
            raise ValueError("manifest requires a textual experiment ID")
        if not isinstance(payload.get("mode"), str):
            raise ValueError("manifest requires an experiment mode")
        if type(payload.get("quantity")) is not int:
            raise TypeError("manifest quantity must be an integer")
        if not isinstance(payload.get("entry_side", Side.BUY.value), str):
            raise ValueError("manifest entry side must be text")
        duration_us = _duration_us(payload, "duration")
        fork_time_us = _duration_us(payload, "fork_time", allow_zero=True)
        decision_interval_us = _duration_us(payload, "decision_interval")
        directory = Path(".") if base_directory is None else base_directory
        strategies = tuple(
            _strategy_variant(item, directory)
            for item in raw_strategies
        )
        return cls(
            experiment_id=str(payload["experiment_id"]),
            mode=ExperimentMode(str(payload["mode"]).upper()),
            scenario_names=tuple(str(value) for value in raw_scenarios),
            seeds=tuple(int(value) for value in raw_seeds),
            strategies=strategies,
            duration_us=duration_us,
            fork_time_us=fork_time_us,
            decision_interval_us=decision_interval_us,
            quantity=int(payload["quantity"]),
            entry_side=Side(str(payload.get("entry_side", Side.BUY.value)).lower()),
            exit_signals=tuple(
                TrafficState(str(value).upper()) for value in raw_exit_signals
            ),
        )


def _strategy_variant(
    payload: dict[str, Any],
    base_directory: Path,
) -> StrategyVariant:
    if not isinstance(payload.get("name"), str):
        raise ValueError("strategy variant requires a name")
    source = payload.get("source")
    source_path: str | None = None
    if source is None:
        raw_path = payload.get("rule_file")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("strategy variant requires source or rule_file")
        path = (base_directory / raw_path).resolve()
        source = path.read_text(encoding="utf-8")
        source_path = str(path)
    elif not isinstance(source, str):
        raise ValueError("strategy source must be text")
    else:
        raw_source_path = payload.get("source_path")
        source_path = None if raw_source_path is None else str(raw_source_path)
    variant = StrategyVariant(str(payload["name"]), source, source_path)
    expected_sha256 = payload.get("source_sha256")
    if expected_sha256 is not None and expected_sha256 != variant.source_sha256:
        raise ValueError(f"strategy source hash mismatch for {variant.name}")
    return variant


def _duration_us(
    payload: dict[str, Any],
    prefix: str,
    *,
    allow_zero: bool = False,
) -> int:
    microseconds_key = f"{prefix}_us"
    seconds_key = f"{prefix}_seconds"
    milliseconds_key = f"{prefix}_milliseconds"
    present = [
        key
        for key in (microseconds_key, seconds_key, milliseconds_key)
        if key in payload
    ]
    if len(present) != 1:
        raise ValueError(f"manifest requires exactly one {prefix} time field")
    key = present[0]
    value = payload[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    multiplier = (
        1
        if key == microseconds_key
        else 1_000_000
        if key == seconds_key
        else 1_000
    )
    result = value * multiplier
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{key} is outside its valid range")
    return result
