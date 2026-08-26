"""Apply reusable calibration parameters to the existing market simulator."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

from kirby2.exchange import OrderBook
from kirby2.scenarios import get_scenario_definition, run_market_scenario
from kirby2.simulation import (
    ChannelIntensity,
    FlowEvent,
    FlowEventFamily,
    HawkesFlowModel,
    IntensityInspection,
    QueueReactiveFlowModifier,
    Regime,
    SimpleFlowModel,
    accepted_hawkes_profile_for_regime,
    load_accepted_hawkes_configs,
)

from .measurements import measure_stream
from .models import CalibrationReport, NormalizedMarketStream
from .normalization import normalize_simulation
from .profiles import MarketProfile


SPECIAL_FLOW_PARAMETERS = {
    "hawkes_excitation_scale",
    "queue_response_scale",
}


class ScaledQueueReactiveModifier:
    """Blend the accepted observable-state response toward or away from identity."""

    def __init__(self, response_scale: float) -> None:
        if not math.isfinite(response_scale) or response_scale <= 0:
            raise ValueError("queue response scale must be finite and positive")
        self.response_scale = response_scale
        self.base = QueueReactiveFlowModifier()

    def initialize(self, book: OrderBook, simulation_time_us: int = 0) -> None:
        self.base.initialize(book, simulation_time_us)

    def observe(
        self,
        event: FlowEvent,
        book: OrderBook,
        simulation_time_us: int,
    ) -> None:
        self.base.observe(event, book, simulation_time_us)

    def inspect(
        self,
        baseline_intensities: Mapping[FlowEventFamily, float],
        book: OrderBook,
        simulation_time_us: int,
    ) -> IntensityInspection:
        raw = self.base.inspect(baseline_intensities, book, simulation_time_us)
        channels = []
        for channel in raw.channels:
            scaled_multiplier = channel.state_multiplier ** self.response_scale
            final = min(
                self.base.config.maximum_intensity,
                channel.base_intensity * scaled_multiplier,
            )
            channels.append(
                ChannelIntensity(
                    family=channel.family,
                    base_intensity=channel.base_intensity,
                    state_multiplier=scaled_multiplier,
                    final_intensity=final,
                    term_results=(
                        *channel.term_results,
                        {
                            "calibration_response_scale": self.response_scale,
                            "raw_state_multiplier": round(
                                channel.state_multiplier,
                                9,
                            ),
                        },
                    ),
                )
            )
        return IntensityInspection(raw.state, tuple(channels))

    def replay_config(self) -> dict[str, object]:
        return {
            "base": self.base.replay_config(),
            "kind": "scaled_queue_reactive",
            "response_scale": self.response_scale,
        }


def run_parameterized_market(
    scenario_name: str,
    parameters: Mapping[str, float],
    *,
    seed: int,
    seconds: int,
) -> tuple[NormalizedMarketStream, CalibrationReport]:
    definition = get_scenario_definition(scenario_name)
    flow_model, intensity_modifier = _flow_components(
        definition.regime,
        parameters,
    )
    policy_parameters = {
        name: value
        for name, value in parameters.items()
        if name not in SPECIAL_FLOW_PARAMETERS
    }
    run = run_market_scenario(
        definition,
        seed=seed,
        seconds=seconds,
        flow_model=flow_model,
        intensity_modifier=intensity_modifier,
        parameter_overrides=policy_parameters,
    )
    source_id = (
        f"calibration:{scenario_name}:seed={seed}:seconds={seconds}:"
        + ",".join(f"{name}={parameters[name]}" for name in sorted(parameters))
    )
    stream = normalize_simulation(run.simulation, source_id)
    return stream, measure_stream(stream)


def run_market_profile(
    profile: MarketProfile,
    *,
    seed: int,
    seconds: int,
) -> tuple[NormalizedMarketStream, CalibrationReport]:
    definition = get_scenario_definition(profile.scenario_name)
    if definition.regime.value != profile.regime:
        raise ValueError("market profile regime no longer matches its scenario")
    return run_parameterized_market(
        profile.scenario_name,
        profile.parameters,
        seed=seed,
        seconds=seconds,
    )


def _flow_components(regime: Regime, parameters: Mapping[str, float]):
    hawkes_scale = float(parameters.get("hawkes_excitation_scale", 0.0))
    queue_scale = float(parameters.get("queue_response_scale", 0.0))
    if not math.isfinite(hawkes_scale) or hawkes_scale < 0:
        raise ValueError("Hawkes excitation scale must be finite and nonnegative")
    if not math.isfinite(queue_scale) or queue_scale < 0:
        raise ValueError("queue response scale must be finite and nonnegative")
    if hawkes_scale == 0:
        flow_model = SimpleFlowModel()
    else:
        profile_id = accepted_hawkes_profile_for_regime(regime)
        base = load_accepted_hawkes_configs()[profile_id]
        config = replace(
            base,
            profile_id=f"{profile_id}:calibrated:{hawkes_scale:g}x",
            alpha=tuple(
                tuple(value * hawkes_scale for value in row)
                for row in base.alpha
            ),
        )
        flow_model = HawkesFlowModel(config, use_runtime_baseline=True)
    modifier = (
        None if queue_scale == 0 else ScaledQueueReactiveModifier(queue_scale)
    )
    return flow_model, modifier
