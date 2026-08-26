"""Independent relative-volume and displayed-liquidity dimensions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .distributions import WeightedDiscreteDistribution
from .flow import FlowEventFamily


class VolumePreset(str, Enum):
    X0_25 = "0.25x"
    X0_50 = "0.50x"
    X1_00 = "1.00x"
    X2_00 = "2.00x"
    X5_00 = "5.00x"
    X10_00 = "10.00x"

    @classmethod
    def parse(cls, value: str) -> VolumePreset:
        normalized = value.lower().replace(" ", "")
        aliases = {
            "0.25": cls.X0_25,
            "0.25x": cls.X0_25,
            "0.50": cls.X0_50,
            "0.5": cls.X0_50,
            "0.50x": cls.X0_50,
            "0.5x": cls.X0_50,
            "1": cls.X1_00,
            "1.0": cls.X1_00,
            "1.00x": cls.X1_00,
            "1x": cls.X1_00,
            "2": cls.X2_00,
            "2.0": cls.X2_00,
            "2.00x": cls.X2_00,
            "2x": cls.X2_00,
            "5": cls.X5_00,
            "5.0": cls.X5_00,
            "5.00x": cls.X5_00,
            "5x": cls.X5_00,
            "10": cls.X10_00,
            "10.0": cls.X10_00,
            "10.00x": cls.X10_00,
            "10x": cls.X10_00,
        }
        if normalized not in aliases:
            raise ValueError(f"unknown volume preset: {value}")
        return aliases[normalized]


class LiquidityPreset(str, Enum):
    VERY_THIN = "VERY_THIN"
    THIN = "THIN"
    NORMAL = "NORMAL"
    DEEP = "DEEP"
    VERY_DEEP = "VERY_DEEP"

    @classmethod
    def parse(cls, value: str) -> LiquidityPreset:
        normalized = value.upper().replace("-", "_").replace(" ", "_")
        return cls(normalized)


@dataclass(frozen=True, slots=True)
class VolumeScale:
    relative_volume: float
    event_rate_scale: float
    order_size_scale: float
    displayed_queue_scale: float
    market_frequency_scale: float
    cancellation_activity_scale: float
    replenishment_scale: float


@dataclass(frozen=True, slots=True)
class LiquidityScale:
    initial_depth_scale: float
    queue_size_scale: float
    replenishment_rate_scale: float
    replenishment_size_scale: float
    cancellation_rate_scale: float
    placement_depth_offset: int


_VOLUME_SCALES = {
    VolumePreset.X0_25: VolumeScale(0.25, 0.50, 0.50, 0.70, 0.80, 0.70, 0.80),
    VolumePreset.X0_50: VolumeScale(0.50, 0.72, 0.70, 0.85, 0.90, 0.85, 0.90),
    VolumePreset.X1_00: VolumeScale(1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00),
    VolumePreset.X2_00: VolumeScale(2.00, 1.40, 1.40, 1.20, 1.10, 1.15, 1.10),
    VolumePreset.X5_00: VolumeScale(5.00, 2.25, 2.20, 1.50, 1.25, 1.40, 1.25),
    VolumePreset.X10_00: VolumeScale(10.00, 3.20, 3.10, 1.80, 1.50, 1.70, 1.40),
}


_LIQUIDITY_SCALES = {
    LiquidityPreset.VERY_THIN: LiquidityScale(0.375, 0.25, 0.50, 0.50, 1.80, 2),
    LiquidityPreset.THIN: LiquidityScale(0.625, 0.55, 0.75, 0.75, 1.30, 1),
    LiquidityPreset.NORMAL: LiquidityScale(1.00, 1.00, 1.00, 1.00, 1.00, 0),
    LiquidityPreset.DEEP: LiquidityScale(1.50, 2.00, 1.35, 1.40, 0.75, -1),
    LiquidityPreset.VERY_DEEP: LiquidityScale(2.00, 4.00, 1.75, 2.00, 0.50, -2),
}


@dataclass(frozen=True, slots=True)
class ScenarioDimensions:
    volume: VolumePreset = VolumePreset.X1_00
    liquidity: LiquidityPreset = LiquidityPreset.NORMAL

    @property
    def volume_scale(self) -> VolumeScale:
        return _VOLUME_SCALES[self.volume]

    @property
    def liquidity_scale(self) -> LiquidityScale:
        return _LIQUIDITY_SCALES[self.liquidity]

    @property
    def is_identity(self) -> bool:
        return self.volume is VolumePreset.X1_00 and self.liquidity is LiquidityPreset.NORMAL

    def rate_scale(self, family: FlowEventFamily) -> float:
        volume = self.volume_scale
        liquidity = self.liquidity_scale
        scale = volume.event_rate_scale
        if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
            scale *= volume.replenishment_scale * liquidity.replenishment_rate_scale
        elif family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}:
            scale *= volume.market_frequency_scale
        else:
            scale *= volume.cancellation_activity_scale * liquidity.cancellation_rate_scale
        return scale

    def order_size_scale(self, family: FlowEventFamily) -> float:
        scale = self.volume_scale.order_size_scale
        if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
            scale *= self.liquidity_scale.replenishment_size_scale
        return scale

    def initial_depth(self, base_depth: int) -> int:
        return max(1, round(base_depth * self.liquidity_scale.initial_depth_scale))

    def queue_distribution(
        self,
        base: WeightedDiscreteDistribution,
    ) -> WeightedDiscreteDistribution:
        scale = (
            self.volume_scale.displayed_queue_scale
            * self.liquidity_scale.queue_size_scale
        )
        if scale == 1.0:
            return base
        values = tuple(max(1, round(value * scale)) for value in base.values)
        return WeightedDiscreteDistribution(values=values, weights=base.weights)

    def depth_distribution(
        self,
        base: WeightedDiscreteDistribution,
    ) -> WeightedDiscreteDistribution:
        offset = self.liquidity_scale.placement_depth_offset
        if offset == 0:
            return base
        values = tuple(max(0, value + offset) for value in base.values)
        return WeightedDiscreteDistribution(values=values, weights=base.weights)

    def as_dict(self) -> dict[str, object]:
        volume = self.volume_scale
        liquidity = self.liquidity_scale
        values = {
            "liquidity": self.liquidity.value,
            "liquidity_scale": {
                "cancellation_rate_scale": liquidity.cancellation_rate_scale,
                "initial_depth_scale": liquidity.initial_depth_scale,
                "placement_depth_offset": liquidity.placement_depth_offset,
                "queue_size_scale": liquidity.queue_size_scale,
                "replenishment_rate_scale": liquidity.replenishment_rate_scale,
                "replenishment_size_scale": liquidity.replenishment_size_scale,
            },
            "relative_volume": self.volume.value,
            "volume_scale": {
                "cancellation_activity_scale": volume.cancellation_activity_scale,
                "displayed_queue_scale": volume.displayed_queue_scale,
                "event_rate_scale": volume.event_rate_scale,
                "market_frequency_scale": volume.market_frequency_scale,
                "order_size_scale": volume.order_size_scale,
                "relative_volume": volume.relative_volume,
                "replenishment_scale": volume.replenishment_scale,
            },
        }
        if any(
            not math.isfinite(value)
            for section in (values["liquidity_scale"], values["volume_scale"])
            for value in section.values()  # type: ignore[union-attr]
            if isinstance(value, float)
        ):
            raise ValueError("scenario scale must remain finite")
        return values
