"""Simulator latency profiles; names make no real-network performance claim."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .distributions import (
    LatencyComponent,
    LatencyDistributionSpec,
)


class LatencyProfileName(str, Enum):
    ZERO_LATENCY = "ZERO_LATENCY"
    LOW_LATENCY = "LOW_LATENCY"
    NORMAL = "NORMAL"
    STRESSED = "STRESSED"
    UNSTABLE = "UNSTABLE"


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    name: LatencyProfileName
    components: dict[LatencyComponent, LatencyDistributionSpec]
    simulator_only: bool = True

    def __post_init__(self) -> None:
        if set(self.components) != set(LatencyComponent):
            raise ValueError("latency profile must configure every component")
        if self.simulator_only is not True:
            raise ValueError("latency profiles are simulator labels only")

    def distribution(self, component: LatencyComponent) -> LatencyDistributionSpec:
        return self.components[component]

    def as_dict(self) -> dict[str, object]:
        return {
            "components": {
                component.value: self.components[component].as_dict()
                for component in LatencyComponent
            },
            "name": self.name.value,
            "simulator_only": self.simulator_only,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LatencyProfile:
        raw = payload.get("components")
        if not isinstance(raw, dict):
            raise ValueError("latency profile components are missing")
        if payload.get("simulator_only") is not True:
            raise ValueError("latency profile must declare simulator_only=true")
        return cls(
            LatencyProfileName(str(payload["name"])),
            {
                LatencyComponent(str(key)): LatencyDistributionSpec.from_dict(value)
                for key, value in raw.items()
                if isinstance(value, dict)
            },
            True,
        )


def get_latency_profile(name: str | LatencyProfileName) -> LatencyProfile:
    parsed = (
        name
        if isinstance(name, LatencyProfileName)
        else LatencyProfileName(name.upper())
    )
    return _profiles()[parsed]


def _profiles() -> dict[LatencyProfileName, LatencyProfile]:
    fixed = LatencyDistributionSpec.fixed
    uniform = LatencyDistributionSpec.uniform
    lognormal = LatencyDistributionSpec.lognormal
    empirical = LatencyDistributionSpec.empirical
    return {
        LatencyProfileName.ZERO_LATENCY: LatencyProfile(
            LatencyProfileName.ZERO_LATENCY,
            {component: fixed(0) for component in LatencyComponent},
        ),
        LatencyProfileName.LOW_LATENCY: LatencyProfile(
            LatencyProfileName.LOW_LATENCY,
            {
                LatencyComponent.MARKET_DATA_PUBLICATION: fixed(50),
                LatencyComponent.DOWNLINK: uniform(50, 150),
                LatencyComponent.RENDER: fixed(100),
                LatencyComponent.INPUT_PROCESSING: uniform(25, 75),
                LatencyComponent.CLIENT_ROUTING: fixed(50),
                LatencyComponent.UPLINK: uniform(75, 200),
                LatencyComponent.GATEWAY: fixed(75),
                LatencyComponent.VENUE_PROCESSING: fixed(50),
                LatencyComponent.FILL_REPORT: uniform(75, 200),
            },
        ),
        LatencyProfileName.NORMAL: LatencyProfile(
            LatencyProfileName.NORMAL,
            {
                LatencyComponent.MARKET_DATA_PUBLICATION: fixed(250),
                LatencyComponent.DOWNLINK: uniform(300, 700),
                LatencyComponent.RENDER: fixed(250),
                LatencyComponent.INPUT_PROCESSING: fixed(100),
                LatencyComponent.CLIENT_ROUTING: fixed(100),
                LatencyComponent.UPLINK: fixed(1_000),
                LatencyComponent.GATEWAY: fixed(1_000),
                LatencyComponent.VENUE_PROCESSING: fixed(500),
                LatencyComponent.FILL_REPORT: fixed(500),
            },
        ),
        LatencyProfileName.STRESSED: LatencyProfile(
            LatencyProfileName.STRESSED,
            {
                LatencyComponent.MARKET_DATA_PUBLICATION: uniform(500, 2_000),
                LatencyComponent.DOWNLINK: lognormal(
                    500, 8_000, math.log(2_000), 0.65
                ),
                LatencyComponent.RENDER: uniform(500, 3_000),
                LatencyComponent.INPUT_PROCESSING: uniform(250, 1_000),
                LatencyComponent.CLIENT_ROUTING: uniform(300, 1_500),
                LatencyComponent.UPLINK: lognormal(
                    1_000, 12_000, math.log(4_000), 0.75
                ),
                LatencyComponent.GATEWAY: uniform(1_000, 5_000),
                LatencyComponent.VENUE_PROCESSING: uniform(500, 4_000),
                LatencyComponent.FILL_REPORT: lognormal(
                    1_000, 15_000, math.log(5_000), 0.8
                ),
            },
        ),
        LatencyProfileName.UNSTABLE: LatencyProfile(
            LatencyProfileName.UNSTABLE,
            {
                LatencyComponent.MARKET_DATA_PUBLICATION: empirical(
                    (100, 500, 2_000, 10_000)
                ),
                LatencyComponent.DOWNLINK: empirical((200, 400, 5_000, 20_000)),
                LatencyComponent.RENDER: empirical((100, 1_000, 4_000)),
                LatencyComponent.INPUT_PROCESSING: empirical((50, 500, 2_500)),
                LatencyComponent.CLIENT_ROUTING: empirical((100, 1_000, 6_000)),
                LatencyComponent.UPLINK: empirical((250, 2_000, 15_000, 40_000)),
                LatencyComponent.GATEWAY: empirical((100, 3_000, 10_000)),
                LatencyComponent.VENUE_PROCESSING: empirical((100, 2_000, 8_000)),
                LatencyComponent.FILL_REPORT: empirical((200, 5_000, 30_000)),
            },
        ),
    }
