"""Regime-specific distribution shapes independent from exchange semantics."""

from __future__ import annotations

from pathlib import Path

from .distribution_framework import (
    CategoricalIntegerDistribution,
    DistributionProfile,
    DistributionPurpose,
    EmpiricalIntegerDistribution,
    GammaIntegerDistribution,
    GeometricIntegerDistribution,
    LognormalIntegerDistribution,
)
from .regimes import Regime


EMPIRICAL_DIRECTORY = Path(__file__).with_name("empirical")


def balanced_distribution_profile() -> DistributionProfile:
    empirical_trades = EmpiricalIntegerDistribution.from_normalized_file(
        EMPIRICAL_DIRECTORY / "fixture_trade_sizes.json"
    )
    return DistributionProfile(
        profile_id="balanced_empirical_v1",
        distributions={
            DistributionPurpose.ORDER_SIZE: GammaIntegerDistribution(2.2, 140.0, 1, 3_000),
            DistributionPurpose.TRADE_SIZE: empirical_trades,
            DistributionPurpose.CANCEL_SIZE: LognormalIntegerDistribution(5.2, 0.8, 1, 5_000),
            DistributionPurpose.QUEUE_DEPTH: GammaIntegerDistribution(2.5, 220.0, 1, 5_000),
            DistributionPurpose.LIMIT_PLACEMENT_DEPTH: GeometricIntegerDistribution(0.48, 0, 8),
            DistributionPurpose.INTER_EVENT_TIMING_MODIFIER: LognormalIntegerDistribution(6.9, 0.22, 500, 2_000),
            DistributionPurpose.SPREAD_STATE_DURATION: GammaIntegerDistribution(2.0, 500_000.0, 1_000, 10_000_000),
        },
    )


def panic_distribution_profile() -> DistributionProfile:
    return DistributionProfile(
        profile_id="panic_heavy_tail_v1",
        distributions={
            DistributionPurpose.ORDER_SIZE: GammaIntegerDistribution(1.5, 180.0, 1, 4_000),
            DistributionPurpose.TRADE_SIZE: EmpiricalIntegerDistribution(
                observations=(100, 200, 400, 800, 1_600, 3_200, 6_400),
                weights=(8, 12, 18, 20, 18, 14, 10),
                source_id="kirby2_panic_heavy_tail_fixture_v1",
                minimum=1,
                maximum=10_000,
            ),
            DistributionPurpose.CANCEL_SIZE: GammaIntegerDistribution(1.4, 800.0, 1, 10_000),
            DistributionPurpose.QUEUE_DEPTH: CategoricalIntegerDistribution(
                values=(25, 50, 100, 200, 400),
                weights=(25, 30, 25, 15, 5),
            ),
            DistributionPurpose.LIMIT_PLACEMENT_DEPTH: GeometricIntegerDistribution(0.25, 1, 12),
            DistributionPurpose.INTER_EVENT_TIMING_MODIFIER: LognormalIntegerDistribution(6.5, 0.55, 200, 3_000),
            DistributionPurpose.SPREAD_STATE_DURATION: GammaIntegerDistribution(1.5, 900_000.0, 1_000, 20_000_000),
        },
    )


def absorption_distribution_profile() -> DistributionProfile:
    profile = balanced_distribution_profile()
    distributions = dict(profile.distributions)
    distributions[DistributionPurpose.ORDER_SIZE] = GammaIntegerDistribution(
        3.0,
        260.0,
        1,
        6_000,
    )
    distributions[DistributionPurpose.QUEUE_DEPTH] = GammaIntegerDistribution(
        3.5,
        350.0,
        1,
        8_000,
    )
    return DistributionProfile("absorption_replenishment_v1", distributions)


def distribution_profile_for_regime(regime: Regime) -> DistributionProfile:
    if regime is Regime.PANIC:
        return panic_distribution_profile()
    if regime in {Regime.ABSORPTION_BID, Regime.ABSORPTION_ASK}:
        return absorption_distribution_profile()
    return balanced_distribution_profile()
