"""Market-regime scenario definitions, execution, and observable summaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kirby2.exchange import Side
from kirby2.simulation import (
    BookObservation,
    LiquidityPreset,
    Regime,
    RegimeOrderFlow,
    ScenarioDimensions,
    SimulationConfig,
    SimulationResult,
    VolumePreset,
)
from kirby2.simulation.regimes import regime_profiles


DEFINITIONS_PATH = Path(__file__).with_name("accepted_scenarios.json")


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    name: str
    regime: Regime
    seed: int
    duration_seconds: int
    initial_mid_ticks: int
    initial_depth: int
    relative_volume: VolumePreset
    liquidity: LiquidityPreset
    parameter_overrides: dict[str, Any]
    accepted_replay_sha256: str
    behavioral_envelope: dict[str, dict[str, float]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioDefinition:
        definition = cls(
            name=str(data["name"]),
            regime=Regime(str(data["regime"]).upper()),
            seed=int(data["seed"]),
            duration_seconds=int(data["duration_seconds"]),
            initial_mid_ticks=int(data["initial_mid_ticks"]),
            initial_depth=int(data["initial_depth"]),
            relative_volume=VolumePreset.parse(
                str(data.get("relative_volume", "1.00x"))
            ),
            liquidity=LiquidityPreset.parse(str(data.get("liquidity", "NORMAL"))),
            parameter_overrides=dict(data.get("parameter_overrides", {})),
            accepted_replay_sha256=str(data["accepted_replay_sha256"]),
            behavioral_envelope={
                str(bound): {str(metric): float(value) for metric, value in values.items()}
                for bound, values in dict(data.get("behavioral_envelope", {})).items()
            },
        )
        if not definition.name or definition.name != definition.name.lower():
            raise ValueError("scenario names must be nonempty lowercase identifiers")
        if definition.duration_seconds <= 0:
            raise ValueError("scenario duration must be positive")
        if definition.initial_mid_ticks <= 0 or definition.initial_depth <= 0:
            raise ValueError("scenario initial mid and depth must be positive")
        if set(definition.behavioral_envelope) - {"minimum", "maximum"}:
            raise ValueError("behavioral envelope supports only minimum and maximum bounds")
        return definition


@dataclass(slots=True)
class ScenarioRun:
    definition: ScenarioDefinition
    seed: int
    duration_seconds: int
    simulation: SimulationResult
    observations: tuple[BookObservation, ...]
    dimensions: ScenarioDimensions

    def replay_json_lines(self) -> str:
        return self.simulation.replay_json_lines()

    def metrics(self) -> dict[str, Any]:
        self.simulation.book.assert_invariants()
        trades = self.simulation.book.trades[self.simulation.initial_trade_count :]
        executed_buy_volume = sum(
            trade.quantity for trade in trades if trade.taker_side is Side.BUY
        )
        executed_sell_volume = sum(
            trade.quantity for trade in trades if trade.taker_side is Side.SELL
        )
        aggressive_buy_volume = sum(
            int(event.command["quantity"])
            for event in self.simulation.flow_events
            if event.applied
            and event.family.value == "market_buy"
            and event.command is not None
        )
        aggressive_sell_volume = sum(
            int(event.command["quantity"])
            for event in self.simulation.flow_events
            if event.applied
            and event.family.value == "market_sell"
            and event.command is not None
        )
        spreads = [
            observation.spread_ticks
            for observation in self.observations
            if observation.spread_ticks is not None
        ]
        average_depth = (
            sum(
                observation.best_bid_size + observation.best_ask_size
                for observation in self.observations
            )
            / len(self.observations)
            if self.observations
            else 0.0
        )
        average_imbalance = (
            sum(observation.imbalance for observation in self.observations)
            / len(self.observations)
            if self.observations
            else 0.0
        )
        midpoint_displacements = [
            ((observation.best_bid_ticks + observation.best_ask_ticks) / 2.0)
            - self.definition.initial_mid_ticks
            for observation in self.observations
            if observation.best_bid_ticks is not None
            and observation.best_ask_ticks is not None
        ]
        cancellation_events = [
            event
            for event in self.simulation.flow_events
            if event.applied and event.family.value.startswith("cancel_")
        ]
        cancel_bid_events = [
            event for event in cancellation_events if event.family.value == "cancel_bid"
        ]
        cancel_ask_events = [
            event for event in cancellation_events if event.family.value == "cancel_ask"
        ]
        cancellation_arrivals = [
            event
            for event in self.simulation.flow_events
            if event.family.value.startswith("cancel_")
        ]
        displacement = (
            float(trades[-1].price_ticks - self.definition.initial_mid_ticks)
            if trades
            else 0.0
        )
        return {
            "aggressive_buy_sell_ratio": self._ratio(
                aggressive_buy_volume,
                aggressive_sell_volume,
            ),
            "aggressive_buy_volume": aggressive_buy_volume,
            "aggressive_sell_buy_ratio": self._ratio(
                aggressive_sell_volume,
                aggressive_buy_volume,
            ),
            "aggressive_sell_volume": aggressive_sell_volume,
            "average_depth": round(average_depth, 3),
            "average_imbalance": round(average_imbalance, 6),
            "average_abs_midpoint_displacement_ticks": round(
                sum(abs(value) for value in midpoint_displacements)
                / len(midpoint_displacements),
                6,
            )
            if midpoint_displacements
            else 0.0,
            "average_spread_ticks": round(sum(spreads) / len(spreads), 6) if spreads else None,
            "cancel_ask_count": len(cancel_ask_events),
            "cancel_bid_count": len(cancel_bid_events),
            "cancellation_count": len(cancellation_events),
            "cancellation_arrival_count": len(cancellation_arrivals),
            "cancellation_quantity": sum(
                int(event.command["cancelled_quantity"])
                for event in cancellation_events
                if event.command is not None
            ),
            "ending_best_ask_ticks": self.simulation.book.best_ask,
            "ending_best_bid_ticks": self.simulation.book.best_bid,
            "event_count": len(self.simulation.flow_events),
            "executed_buy_volume": executed_buy_volume,
            "executed_sell_volume": executed_sell_volume,
            "invariant_status": "PASS",
            "liquidity": self.dimensions.liquidity.value,
            "max_spread_ticks": max(spreads) if spreads else None,
            "midpoint_displacement_ticks": round(midpoint_displacements[-1], 3)
            if midpoint_displacements
            else 0.0,
            "price_displacement_ticks": displacement,
            "replay_sha256": self.simulation.replay_sha256(),
            "relative_volume": self.dimensions.volume.value,
            "seed": self.seed,
            "side_empty_fraction": round(
                sum(
                    observation.best_bid_ticks is None
                    or observation.best_ask_ticks is None
                    for observation in self.observations
                )
                / len(self.observations),
                6,
            )
            if self.observations
            else 0.0,
            "skipped_cancellation_count": sum(
                not event.applied for event in cancellation_arrivals
            ),
            "total_trades": len(trades),
            "total_volume": sum(trade.quantity for trade in trades),
        }

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        if denominator == 0:
            return 999.0 if numerator > 0 else 1.0
        return round(numerator / denominator, 6)


def load_scenario_definitions(path: Path = DEFINITIONS_PATH) -> dict[str, ScenarioDefinition]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    definitions = [ScenarioDefinition.from_dict(item) for item in payload["scenarios"]]
    by_name = {definition.name: definition for definition in definitions}
    if len(by_name) != len(definitions):
        raise ValueError("scenario names must be unique")
    if {definition.regime for definition in definitions} != set(Regime):
        raise ValueError("accepted scenarios must contain exactly one definition per regime")
    return by_name


def get_scenario_definition(name: str) -> ScenarioDefinition:
    normalized = name.lower()
    definitions = load_scenario_definitions()
    if normalized not in definitions:
        available = ", ".join(sorted(definitions))
        raise ValueError(f"unknown scenario {name!r}; available: {available}")
    return definitions[normalized]


def run_market_scenario(
    definition: ScenarioDefinition,
    seed: int | None = None,
    seconds: int | None = None,
    relative_volume: VolumePreset | None = None,
    liquidity: LiquidityPreset | None = None,
) -> ScenarioRun:
    actual_seed = definition.seed if seed is None else seed
    actual_seconds = definition.duration_seconds if seconds is None else seconds
    engine, dimensions = create_market_engine(
        definition,
        seed=actual_seed,
        relative_volume=relative_volume,
        liquidity=liquidity,
    )
    simulation = engine.run(actual_seconds)
    return ScenarioRun(
        definition=definition,
        seed=actual_seed,
        duration_seconds=actual_seconds,
        simulation=simulation,
        observations=tuple(engine.observations),
        dimensions=dimensions,
    )


def create_market_engine(
    definition: ScenarioDefinition,
    seed: int | None = None,
    relative_volume: VolumePreset | None = None,
    liquidity: LiquidityPreset | None = None,
) -> tuple[RegimeOrderFlow, ScenarioDimensions]:
    actual_seed = definition.seed if seed is None else seed
    profile = regime_profiles()[definition.regime]
    dimensions = ScenarioDimensions(
        definition.relative_volume if relative_volume is None else relative_volume,
        definition.liquidity if liquidity is None else liquidity,
    )
    intensity = float(definition.parameter_overrides.get("event_intensity", 1.0))
    config = SimulationConfig(
        initial_mid_ticks=definition.initial_mid_ticks,
        initial_depth=dimensions.initial_depth(definition.initial_depth),
        event_intensity=intensity,
        queue_size_distribution=dimensions.queue_distribution(profile.initial_queue_sizes),
    )
    engine = RegimeOrderFlow(
        seed=actual_seed,
        regime=definition.regime,
        config=config,
        parameter_overrides=definition.parameter_overrides,
        dimensions=dimensions,
    )
    return engine, dimensions


def evaluate_behavioral_envelope(
    definition: ScenarioDefinition,
    metrics: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    for metric, minimum in definition.behavioral_envelope.get("minimum", {}).items():
        actual = metrics.get(metric)
        if not isinstance(actual, (int, float)) or actual < minimum:
            failures.append(f"{metric}={actual!r} below minimum {minimum}")
    for metric, maximum in definition.behavioral_envelope.get("maximum", {}).items():
        actual = metrics.get(metric)
        if not isinstance(actual, (int, float)) or actual > maximum:
            failures.append(f"{metric}={actual!r} above maximum {maximum}")
    return failures
