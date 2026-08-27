"""Seeded benchmark market recipes; future paths are never exposed to policies."""

from __future__ import annotations

import random
from dataclasses import dataclass

from kirby2.exchange.models import OrderOwner, Side
from kirby2.latency import LatencyProfileName, get_latency_profile
from kirby2.multivenue import VenueConfig, VenueFeeSchedule
from kirby2.multivenue.models import canonical_sha256
from kirby2.observability import (
    HiddenLiquidityRules,
    HiddenOrderRequest,
    IcebergDefinition,
    IcebergRefreshBehavior,
    LiquidityKind,
    RefreshEventVisibility,
)


BENCHMARK_SCENARIOS = ("opening_momentum", "balanced_execution")


@dataclass(frozen=True, slots=True)
class BackgroundMarketEvent:
    sequence: int
    simulation_time_us: int
    venue_id: str
    order_id: str
    side: Side
    quantity: int

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "quantity": self.quantity,
            "sequence": self.sequence,
            "side": self.side.value,
            "simulation_time_us": self.simulation_time_us,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class ExecutionBenchmarkScenario:
    name: str
    start_time_us: int
    duration_us: int
    decision_interval_us: int
    initial_mid_ticks: int
    buy_flow_probability_bps: int
    volume_profile_bps: tuple[int, ...]
    venue_configs: tuple[VenueConfig, ...]

    def __post_init__(self) -> None:
        if self.name not in BENCHMARK_SCENARIOS:
            raise ValueError("unknown execution benchmark scenario")
        if sum(self.volume_profile_bps) != 10_000:
            raise ValueError("benchmark volume profile must sum to 10000 basis points")

    @property
    def deadline_us(self) -> int:
        return self.start_time_us + self.duration_us

    def initial_orders(self) -> tuple[tuple[str, HiddenOrderRequest], ...]:
        mid = self.initial_mid_ticks
        orders: list[tuple[str, HiddenOrderRequest]] = []
        depths = {
            "DEEP": (800, 1, 2),
            "FAST": (300, 1, 1),
            "REBATE": (400, 1, 1),
        }
        for venue_id, (quantity, bid_distance, ask_distance) in depths.items():
            orders.extend(
                (
                    (
                        venue_id,
                        _displayed(
                            f"{venue_id}-BID-1",
                            Side.BUY,
                            quantity,
                            mid - bid_distance,
                        ),
                    ),
                    (
                        venue_id,
                        _displayed(
                            f"{venue_id}-BID-2",
                            Side.BUY,
                            quantity * 2,
                            mid - bid_distance - 1,
                        ),
                    ),
                    (
                        venue_id,
                        _displayed(
                            f"{venue_id}-ASK-2",
                            Side.SELL,
                            quantity * 2,
                            mid + ask_distance + 1,
                        ),
                    ),
                )
            )
            if venue_id == "REBATE":
                orders.append(
                    (
                        venue_id,
                        HiddenOrderRequest(
                            f"{venue_id}-ASK-1",
                            Side.SELL,
                            LiquidityKind.ICEBERG,
                            OrderOwner.SIMULATED,
                            "BENCHMARK-MARKET",
                            quantity + 500,
                            mid + ask_distance,
                            IcebergDefinition(
                                quantity,
                                500,
                                100,
                                IcebergRefreshBehavior.AUTOMATIC,
                                RefreshEventVisibility.QUOTE_UPDATE_ONLY,
                            ),
                        ),
                    )
                )
            else:
                orders.append(
                    (
                        venue_id,
                        _displayed(
                            f"{venue_id}-ASK-1",
                            Side.SELL,
                            quantity,
                            mid + ask_distance,
                        ),
                    )
                )
        return tuple(orders)

    def background_events(self, seed: int) -> tuple[BackgroundMarketEvent, ...]:
        rng = random.Random(seed ^ 0x27EC0710)
        venues = tuple(config.venue_id for config in self.venue_configs)
        sizes = (20, 25, 40, 50, 75, 100)
        result: list[BackgroundMarketEvent] = []
        sequence = 0
        for time_us in range(
            self.start_time_us + self.decision_interval_us,
            self.deadline_us + 1,
            self.decision_interval_us,
        ):
            event_count = 2 if rng.randrange(10_000) < 3_000 else 1
            for ordinal in range(event_count):
                sequence += 1
                venue_id = venues[rng.randrange(len(venues))]
                side = (
                    Side.BUY
                    if rng.randrange(10_000) < self.buy_flow_probability_bps
                    else Side.SELL
                )
                quantity = sizes[rng.randrange(len(sizes))]
                result.append(
                    BackgroundMarketEvent(
                        sequence,
                        time_us,
                        venue_id,
                        f"BG-{seed}-{sequence:05d}-{ordinal}",
                        side,
                        quantity,
                    )
                )
        return tuple(result)

    def background_sha256(self, seed: int) -> str:
        return canonical_sha256(
            [event.as_dict() for event in self.background_events(seed)]
        )


def get_benchmark_scenario(
    name: str,
    *,
    duration_us: int | None = None,
    decision_interval_us: int | None = None,
) -> ExecutionBenchmarkScenario:
    normalized = name.strip().lower().replace("-", "_")
    if normalized not in BENCHMARK_SCENARIOS:
        raise ValueError(f"unknown execution benchmark scenario: {name}")
    duration = 5_000_000 if duration_us is None else duration_us
    interval = 250_000 if decision_interval_us is None else decision_interval_us
    if duration <= 0 or interval <= 0 or duration % interval:
        raise ValueError("benchmark scenario timing is invalid")
    buy_probability = 7_500 if normalized == "opening_momentum" else 5_000
    profile = (
        (3_000, 2_500, 2_000, 1_500, 1_000)
        if normalized == "opening_momentum"
        else (1_500, 2_000, 3_000, 2_000, 1_500)
    )
    return ExecutionBenchmarkScenario(
        normalized,
        1_000,
        duration,
        interval,
        10_000,
        buy_probability,
        profile,
        _venue_configs(),
    )


def _venue_configs() -> tuple[VenueConfig, ...]:
    return (
        VenueConfig(
            "FAST",
            get_latency_profile(LatencyProfileName.LOW_LATENCY),
            VenueFeeSchedule(50, 5),
            hidden_rules=HiddenLiquidityRules(feed_delay_us=100),
            expected_fill_probability_bps=9_200,
        ),
        VenueConfig(
            "DEEP",
            get_latency_profile(LatencyProfileName.NORMAL),
            VenueFeeSchedule(20, 10),
            hidden_rules=HiddenLiquidityRules(feed_delay_us=400),
            expected_fill_probability_bps=8_500,
        ),
        VenueConfig(
            "REBATE",
            get_latency_profile(LatencyProfileName.LOW_LATENCY),
            VenueFeeSchedule(40, 30),
            hidden_rules=HiddenLiquidityRules(feed_delay_us=200),
            expected_fill_probability_bps=7_500,
        ),
    )


def _displayed(
    order_id: str,
    side: Side,
    quantity: int,
    price_ticks: int,
) -> HiddenOrderRequest:
    return HiddenOrderRequest(
        order_id,
        side,
        LiquidityKind.DISPLAYED_LIMIT,
        OrderOwner.SIMULATED,
        "BENCHMARK-MARKET",
        quantity,
        price_ticks,
    )
