"""Scenario and historical adapters for the canonical feature interface."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from kirby2.scenarios.market import ScenarioDefinition, create_market_engine

from .engine import MicrostructureFeatureEngine
from .models import FeatureFrame

if TYPE_CHECKING:
    from kirby2.historical import HistoricalRun


@dataclass(frozen=True, slots=True)
class FeatureStream:
    scenario: str
    seed: int
    windows_us: tuple[int, ...]
    frames: tuple[FeatureFrame, ...]

    def replay_sha256(self) -> str:
        canonical = json.dumps(
            {
                "frames": [
                    {
                        "simulation_time_us": frame.simulation_time_us,
                        "values": frame.as_dict(),
                    }
                    for frame in self.frames
                ],
                "scenario": self.scenario,
                "seed": self.seed,
                "windows_us": self.windows_us,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def inspect_scenario_features(
    definition: ScenarioDefinition,
    seed: int,
    seconds: int = 5,
    emit_interval_us: int = 250_000,
    windows_us: tuple[int, ...] = (250_000, 1_000_000, 5_000_000),
) -> FeatureStream:
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("feature inspection seconds must be positive")
    if type(emit_interval_us) is not int or emit_interval_us <= 0:
        raise ValueError("feature emission interval must be positive")
    engine, dimensions = create_market_engine(definition, seed=seed)
    engine.start()
    feature_engine = MicrostructureFeatureEngine(
        windows_us=windows_us,
        relative_volume=Decimal(str(dimensions.volume_scale.relative_volume)),
    )
    frames = [feature_engine.reset(0, engine.book)]
    next_emit_us = emit_interval_us

    def on_event(flow_event) -> None:
        nonlocal next_emit_us
        if (
            flow_event.exchange_event_start is None
            or flow_event.exchange_event_end is None
        ):
            exchange_events = ()
        else:
            exchange_events = engine.book.journal.events[
                flow_event.exchange_event_start - 1 : flow_event.exchange_event_end
            ]
        frame = feature_engine.observe(
            flow_event.simulation_time_us,
            exchange_events,
            engine.book,
        )
        if flow_event.simulation_time_us >= next_emit_us:
            frames.append(frame)
            while next_emit_us <= flow_event.simulation_time_us:
                next_emit_us += emit_interval_us

    end_time_us = seconds * 1_000_000
    engine.advance_to(end_time_us, on_event=on_event)
    final_frame = feature_engine.snapshot(end_time_us, engine.book)
    if frames[-1].simulation_time_us != final_frame.simulation_time_us:
        frames.append(final_frame)
    engine.book.assert_invariants()
    return FeatureStream(
        scenario=definition.name,
        seed=seed,
        windows_us=feature_engine.windows_us,
        frames=tuple(frames),
    )


def historical_feature_frame(
    run: HistoricalRun,
    windows_us: tuple[int, ...] = (250_000, 1_000_000, 5_000_000),
) -> FeatureFrame:
    """Expose historical final state through the same engine; rolling values start empty."""
    engine = MicrostructureFeatureEngine(windows_us=windows_us)
    return engine.reset(run.duration_us, run.book)
