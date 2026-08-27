"""Observable-state market regimes implemented as order-flow policies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderType, Side

from .clock import MICROSECONDS_PER_SECOND
from .config import SimulationConfig
from .distributions import WeightedDiscreteDistribution
from .distribution_framework import (
    INTER_EVENT_TIMING_SCALE,
    DistributionDrawRecord,
    DistributionProfile,
    DistributionPurpose,
    IntegerSampleDistribution,
)
from .flow import FlowEvent, FlowEventFamily, SimulationResult, SyntheticOrderFlow
from .flow_models import FlowModel, ScheduledFlowArrival, SimpleFlowModel
from .intraday import (
    IntradayClock,
    IntradayModifiers,
    IntradayProfile,
    IntradayWindow,
    ObservedVolumeCurve,
)
from .queue_reactive import FlowIntensityModifier, IntensityInspection
from .scaling import ScenarioDimensions


class Regime(str, Enum):
    BALANCED = "BALANCED"
    BUY_PRESSURE = "BUY_PRESSURE"
    SELL_PRESSURE = "SELL_PRESSURE"
    MOMENTUM_UP = "MOMENTUM_UP"
    MOMENTUM_DOWN = "MOMENTUM_DOWN"
    ABSORPTION_BID = "ABSORPTION_BID"
    ABSORPTION_ASK = "ABSORPTION_ASK"
    THIN_LIQUIDITY = "THIN_LIQUIDITY"
    LIQUIDITY_VACUUM = "LIQUIDITY_VACUUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    HIGH_CANCELLATION = "HIGH_CANCELLATION"
    PANIC = "PANIC"


class SpreadPlacementState(str, Enum):
    TOUCH_FAVORING = "TOUCH_FAVORING"
    DEPTH_FAVORING = "DEPTH_FAVORING"


_FAMILIES = tuple(FlowEventFamily)


def _distribution(
    values: tuple[int, ...],
    weights: tuple[int, ...],
) -> WeightedDiscreteDistribution:
    return WeightedDiscreteDistribution(values=values, weights=weights)


SMALL_SIZES = _distribution((25, 50, 100, 200, 400), (20, 30, 30, 15, 5))
NORMAL_SIZES = _distribution((50, 100, 200, 400, 800), (10, 25, 30, 25, 10))
LARGE_SIZES = _distribution((100, 200, 400, 800, 1_200), (10, 20, 30, 25, 15))
HEAVY_SIZES = _distribution((200, 400, 800, 1_200, 2_000), (10, 20, 30, 25, 15))

AT_BEST_DEPTH = _distribution((0, 1, 2), (75, 20, 5))
NORMAL_DEPTH = _distribution((0, 1, 2, 3, 4), (40, 30, 15, 10, 5))
DEEP_DEPTH = _distribution((1, 2, 3, 4, 6), (10, 25, 30, 25, 10))


@dataclass(frozen=True, slots=True)
class RegimeProfile:
    regime: Regime
    rate_multipliers: tuple[float, float, float, float, float, float]
    limit_buy_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    limit_sell_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    market_buy_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    market_sell_sizes: WeightedDiscreteDistribution = NORMAL_SIZES
    bid_depth: WeightedDiscreteDistribution = NORMAL_DEPTH
    ask_depth: WeightedDiscreteDistribution = NORMAL_DEPTH
    imbalance_feedback: float = 0.0
    trend_feedback: float = 0.0
    initial_queue_sizes: WeightedDiscreteDistribution = NORMAL_SIZES

    def __post_init__(self) -> None:
        if len(self.rate_multipliers) != len(_FAMILIES):
            raise ValueError("regime must provide one rate multiplier per flow family")
        if any(not math.isfinite(value) or value < 0 for value in self.rate_multipliers):
            raise ValueError("rate multipliers must be finite and nonnegative")


def regime_profiles() -> dict[Regime, RegimeProfile]:
    return {
        Regime.BALANCED: RegimeProfile(
            regime=Regime.BALANCED,
            rate_multipliers=(1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
            imbalance_feedback=0.20,
        ),
        Regime.BUY_PRESSURE: RegimeProfile(
            regime=Regime.BUY_PRESSURE,
            rate_multipliers=(1.35, 1.10, 1.70, 0.80, 0.80, 1.20),
            limit_buy_sizes=LARGE_SIZES,
            limit_sell_sizes=NORMAL_SIZES,
            market_buy_sizes=LARGE_SIZES,
            bid_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.20,
        ),
        Regime.SELL_PRESSURE: RegimeProfile(
            regime=Regime.SELL_PRESSURE,
            rate_multipliers=(1.10, 1.35, 0.80, 1.70, 1.20, 0.80),
            limit_buy_sizes=NORMAL_SIZES,
            limit_sell_sizes=LARGE_SIZES,
            market_sell_sizes=LARGE_SIZES,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.20,
        ),
        Regime.MOMENTUM_UP: RegimeProfile(
            regime=Regime.MOMENTUM_UP,
            rate_multipliers=(1.25, 0.65, 2.80, 0.50, 0.45, 2.00),
            limit_buy_sizes=LARGE_SIZES,
            limit_sell_sizes=SMALL_SIZES,
            market_buy_sizes=HEAVY_SIZES,
            bid_depth=AT_BEST_DEPTH,
            ask_depth=DEEP_DEPTH,
            imbalance_feedback=0.90,
            trend_feedback=0.70,
        ),
        Regime.MOMENTUM_DOWN: RegimeProfile(
            regime=Regime.MOMENTUM_DOWN,
            rate_multipliers=(0.65, 1.25, 0.50, 2.80, 2.00, 0.45),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=LARGE_SIZES,
            market_sell_sizes=HEAVY_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.90,
            trend_feedback=0.70,
        ),
        Regime.ABSORPTION_BID: RegimeProfile(
            regime=Regime.ABSORPTION_BID,
            rate_multipliers=(3.00, 2.00, 0.55, 2.20, 0.25, 0.20),
            limit_buy_sizes=HEAVY_SIZES,
            market_sell_sizes=LARGE_SIZES,
            bid_depth=AT_BEST_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.15,
            initial_queue_sizes=LARGE_SIZES,
        ),
        Regime.ABSORPTION_ASK: RegimeProfile(
            regime=Regime.ABSORPTION_ASK,
            rate_multipliers=(2.00, 3.00, 2.20, 0.55, 0.20, 0.25),
            limit_sell_sizes=HEAVY_SIZES,
            market_buy_sizes=LARGE_SIZES,
            bid_depth=AT_BEST_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.15,
            initial_queue_sizes=LARGE_SIZES,
        ),
        Regime.THIN_LIQUIDITY: RegimeProfile(
            regime=Regime.THIN_LIQUIDITY,
            rate_multipliers=(0.55, 0.55, 1.00, 1.00, 1.40, 1.40),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=SMALL_SIZES,
            market_buy_sizes=LARGE_SIZES,
            market_sell_sizes=LARGE_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=DEEP_DEPTH,
            initial_queue_sizes=SMALL_SIZES,
        ),
        Regime.LIQUIDITY_VACUUM: RegimeProfile(
            regime=Regime.LIQUIDITY_VACUUM,
            rate_multipliers=(0.25, 0.25, 0.90, 0.90, 3.50, 3.50),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=SMALL_SIZES,
            market_buy_sizes=LARGE_SIZES,
            market_sell_sizes=LARGE_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=DEEP_DEPTH,
            initial_queue_sizes=SMALL_SIZES,
        ),
        Regime.MEAN_REVERSION: RegimeProfile(
            regime=Regime.MEAN_REVERSION,
            rate_multipliers=(1.10, 1.10, 1.00, 1.00, 0.90, 0.90),
            bid_depth=AT_BEST_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=-0.15,
            trend_feedback=-1.60,
        ),
        Regime.HIGH_CANCELLATION: RegimeProfile(
            regime=Regime.HIGH_CANCELLATION,
            rate_multipliers=(1.60, 1.60, 0.80, 0.80, 4.00, 4.00),
            bid_depth=NORMAL_DEPTH,
            ask_depth=NORMAL_DEPTH,
        ),
        Regime.PANIC: RegimeProfile(
            regime=Regime.PANIC,
            rate_multipliers=(0.35, 1.40, 0.35, 4.50, 3.00, 0.50),
            limit_buy_sizes=SMALL_SIZES,
            limit_sell_sizes=LARGE_SIZES,
            market_buy_sizes=SMALL_SIZES,
            market_sell_sizes=HEAVY_SIZES,
            bid_depth=DEEP_DEPTH,
            ask_depth=AT_BEST_DEPTH,
            imbalance_feedback=0.80,
            trend_feedback=0.50,
            initial_queue_sizes=SMALL_SIZES,
        ),
    }


def best_level_size(book: OrderBook, side: Side) -> int:
    price = book.best_bid if side is Side.BUY else book.best_ask
    if price is None:
        return 0
    levels = book.bids if side is Side.BUY else book.asks
    return levels[price].total_quantity


def book_imbalance(book: OrderBook) -> float:
    bid_size = best_level_size(book, Side.BUY)
    ask_size = best_level_size(book, Side.SELL)
    denominator = bid_size + ask_size
    if denominator == 0:
        return 0.0
    return (bid_size - ask_size) / denominator


def mid_displacement_ticks(book: OrderBook, initial_mid_ticks: int) -> float:
    if book.best_bid is not None and book.best_ask is not None:
        return ((book.best_bid + book.best_ask) / 2.0) - initial_mid_ticks
    if book.best_bid is not None:
        return float(book.best_bid - initial_mid_ticks)
    if book.best_ask is not None:
        return float(book.best_ask - initial_mid_ticks)
    return 0.0


@dataclass(frozen=True, slots=True)
class BookObservation:
    simulation_time_us: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    best_bid_size: int
    best_ask_size: int
    imbalance: float
    spread_ticks: int | None


class RegimePolicy:
    def __init__(
        self,
        profile: RegimeProfile,
        config: SimulationConfig,
        parameter_overrides: dict[str, Any] | None = None,
        dimensions: ScenarioDimensions | None = None,
        distribution_profile: DistributionProfile | None = None,
        intraday_clock: IntradayClock | None = None,
    ) -> None:
        self.profile = profile
        self.config = config
        self.parameter_overrides = parameter_overrides or {}
        self.dimensions = dimensions or ScenarioDimensions()
        self.distribution_profile = distribution_profile
        self.intraday_clock = intraday_clock

    def rates(self, book: OrderBook) -> dict[FlowEventFamily, float]:
        base_rates = self.config.rates
        base = (
            base_rates.limit_buy_rate,
            base_rates.limit_sell_rate,
            base_rates.market_buy_rate,
            base_rates.market_sell_rate,
            base_rates.cancel_bid_rate,
            base_rates.cancel_ask_rate,
        )
        multiplier_overrides = self.parameter_overrides.get("rate_multipliers", {})
        if not isinstance(multiplier_overrides, dict):
            raise ValueError("rate_multipliers override must be an object")
        imbalance = book_imbalance(book)
        displacement = max(
            -1.0,
            min(1.0, mid_displacement_ticks(book, self.config.initial_mid_ticks) / 5.0),
        )
        imbalance_feedback_scale = float(
            self.parameter_overrides.get("imbalance_feedback_scale", 1.0)
        )
        trend_feedback_scale = float(
            self.parameter_overrides.get("trend_feedback_scale", 1.0)
        )
        if any(
            not math.isfinite(value)
            for value in (imbalance_feedback_scale, trend_feedback_scale)
        ):
            raise ValueError("regime feedback scales must be finite")
        directional_signal = (
            self.profile.imbalance_feedback
            * imbalance_feedback_scale
            * imbalance
            + self.profile.trend_feedback
            * trend_feedback_scale
            * displacement
        )
        direction_weights = (0.40, -0.40, 1.00, -1.00, -0.50, 0.50)
        reversion_trend_weights = (0.80, -0.80, -1.00, 1.00, 0.00, 0.00)
        balance_weights = (-0.50, 0.50, -0.80, 0.80, 0.30, -0.30)
        rates: dict[FlowEventFamily, float] = {}
        for index, family in enumerate(_FAMILIES):
            profile_multiplier = self.profile.rate_multipliers[index]
            override = float(multiplier_overrides.get(family.value, 1.0))
            if self.profile.regime is Regime.MEAN_REVERSION:
                exponent = (
                    reversion_trend_weights[index]
                    * abs(self.profile.trend_feedback)
                    * displacement
                    + balance_weights[index] * 0.60 * imbalance
                )
            else:
                exponent = direction_weights[index] * directional_signal
            exponent = max(-2.0, min(2.0, exponent))
            rate = (
                base[index]
                * self.config.event_intensity
                * profile_multiplier
                * override
                * math.exp(exponent)
                * self.dimensions.rate_scale(family)
            )
            family_scale_key = (
                "limit_rate_scale"
                if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}
                else "market_rate_scale"
                if family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
                else "cancel_rate_scale"
            )
            family_scale = float(
                self.parameter_overrides.get(family_scale_key, 1.0)
            )
            if not math.isfinite(family_scale) or family_scale < 0:
                raise ValueError(f"{family_scale_key} must be finite and nonnegative")
            rate *= family_scale
            modifiers = self.intraday_modifiers
            if modifiers is not None:
                rate *= modifiers.relative_volume * modifiers.event_intensity
                if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
                    rate *= modifiers.depth / modifiers.spread_tendency
                elif family in {
                    FlowEventFamily.MARKET_BUY,
                    FlowEventFamily.MARKET_SELL,
                }:
                    rate *= modifiers.volatility * modifiers.spread_tendency
                else:
                    rate *= (
                        modifiers.cancellation_activity
                        * modifiers.spread_tendency
                        / modifiers.depth
                    )
            rates[family] = min(100.0, max(0.0, rate))
        return rates

    @property
    def intraday_modifiers(self) -> IntradayModifiers | None:
        return None if self.intraday_clock is None else self.intraday_clock.modifiers

    def size_distribution(self, family: FlowEventFamily) -> IntegerSampleDistribution:
        if self.distribution_profile is not None:
            purpose = (
                DistributionPurpose.TRADE_SIZE
                if family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
                else DistributionPurpose.ORDER_SIZE
            )
            return self.distribution_profile.distribution(purpose)
        return {
            FlowEventFamily.LIMIT_BUY: self.profile.limit_buy_sizes,
            FlowEventFamily.LIMIT_SELL: self.profile.limit_sell_sizes,
            FlowEventFamily.MARKET_BUY: self.profile.market_buy_sizes,
            FlowEventFamily.MARKET_SELL: self.profile.market_sell_sizes,
            FlowEventFamily.CANCEL_BID: NORMAL_SIZES,
            FlowEventFamily.CANCEL_ASK: NORMAL_SIZES,
        }[family]

    def depth_distribution(self, family: FlowEventFamily) -> IntegerSampleDistribution:
        if self.distribution_profile is not None:
            if family not in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
                raise ValueError("only limit flow has a placement depth")
            return self.distribution_profile.distribution(
                DistributionPurpose.LIMIT_PLACEMENT_DEPTH
            )
        if family is FlowEventFamily.LIMIT_BUY:
            return self.dimensions.depth_distribution(self.profile.bid_depth)
        if family is FlowEventFamily.LIMIT_SELL:
            return self.dimensions.depth_distribution(self.profile.ask_depth)
        raise ValueError("only limit flow has a placement depth")


class RegimeOrderFlow(SyntheticOrderFlow):
    def __init__(
        self,
        seed: int,
        regime: Regime,
        config: SimulationConfig,
        parameter_overrides: dict[str, Any] | None = None,
        dimensions: ScenarioDimensions | None = None,
        flow_model: FlowModel | None = None,
        intensity_modifier: FlowIntensityModifier | None = None,
        distribution_profile: DistributionProfile | None = None,
        intraday_profile: IntradayProfile | None = None,
        intraday_window: IntradayWindow | None = None,
        observed_intraday_volume: ObservedVolumeCurve | None = None,
    ) -> None:
        super().__init__(seed=seed, config=config)
        if intraday_profile is None and (
            intraday_window is not None or observed_intraday_volume is not None
        ):
            raise ValueError("intraday window and observed volume require a profile")
        if intraday_profile is not None and intraday_window is None:
            intraday_window = IntradayWindow(
                intraday_profile.start_second,
                intraday_profile.end_second,
            )
        self.intraday_clock = (
            None
            if intraday_profile is None or intraday_window is None
            else IntradayClock(
                intraday_profile,
                intraday_window,
                observed_intraday_volume,
            )
        )
        self.regime = regime
        self.profile = regime_profiles()[regime]
        self.dimensions = dimensions or ScenarioDimensions()
        self.policy = RegimePolicy(
            self.profile,
            config,
            parameter_overrides,
            self.dimensions,
            distribution_profile,
            self.intraday_clock,
        )
        self.distribution_profile = distribution_profile
        self.flow_model = flow_model or SimpleFlowModel()
        self.intensity_modifier = intensity_modifier
        self.last_intensity_inspection: IntensityInspection | None = None
        self.observations: list[BookObservation] = []
        self._started = False
        self._flow_events: list[FlowEvent] = []
        self._initial_exchange_event_count = 0
        self._initial_trade_count = 0
        self._pending_arrival_time_us: int | None = None
        self._pending_family: FlowEventFamily | None = None
        self._scheduled_flow_time_us: int | None = None
        self._scheduled_flow_family: FlowEventFamily | None = None
        self._pending_is_intraday_transition = False
        self._pending_is_spread_transition = False
        self._spread_placement_state: SpreadPlacementState | None = None
        self._spread_state_expiry_us: int | None = None
        self._distribution_draws: list[DistributionDrawRecord] = []
        self._exogenous_replay_active = False

    @property
    def flow_events(self) -> tuple[FlowEvent, ...]:
        return tuple(self._flow_events)

    @property
    def distribution_draws(self) -> tuple[DistributionDrawRecord, ...]:
        return tuple(self._distribution_draws)

    @property
    def spread_placement_state(self) -> SpreadPlacementState | None:
        return self._spread_placement_state

    @property
    def next_scheduled_time_us(self) -> int | None:
        """Next flow, intraday, or latent spread-state simulation boundary."""

        return self._pending_arrival_time_us

    @property
    def initial_exchange_event_count(self) -> int:
        return self._initial_exchange_event_count

    @property
    def initial_trade_count(self) -> int:
        return self._initial_trade_count

    def runtime_state(self) -> dict[str, object]:
        """Complete active simulation state needed to verify a branch point."""

        flow_model_state = self.flow_model.runtime_state()
        modifier_state = (
            None
            if self.intensity_modifier is None
            else self.intensity_modifier.runtime_state()
        )
        return {
            "book_state_sha256": self.book.state_sha256(),
            "clock_us": self.clock.current_time_us,
            "distribution_draws": [item.as_dict() for item in self._distribution_draws],
            "exogenous_replay_active": self._exogenous_replay_active,
            "flow_events": [item.as_dict() for item in self._flow_events],
            "flow_model": flow_model_state,
            "initial_exchange_event_count": self._initial_exchange_event_count,
            "initial_trade_count": self._initial_trade_count,
            "intensity_modifier": modifier_state,
            "intraday_clock": (
                None if self.intraday_clock is None else self.intraday_clock.as_dict()
            ),
            "last_intensity_inspection": (
                None
                if self.last_intensity_inspection is None
                else self.last_intensity_inspection.as_dict()
            ),
            "observations": [
                {
                    "best_ask_size": item.best_ask_size,
                    "best_ask_ticks": item.best_ask_ticks,
                    "best_bid_size": item.best_bid_size,
                    "best_bid_ticks": item.best_bid_ticks,
                    "imbalance": item.imbalance,
                    "simulation_time_us": item.simulation_time_us,
                    "spread_ticks": item.spread_ticks,
                }
                for item in self.observations
            ],
            "pending": {
                "arrival_time_us": self._pending_arrival_time_us,
                "family": None if self._pending_family is None else self._pending_family.value,
                "is_intraday_transition": self._pending_is_intraday_transition,
                "is_spread_transition": self._pending_is_spread_transition,
                "scheduled_flow_family": (
                    None
                    if self._scheduled_flow_family is None
                    else self._scheduled_flow_family.value
                ),
                "scheduled_flow_time_us": self._scheduled_flow_time_us,
            },
            "regime": self.regime.value,
            "rng": self.rng.runtime_state(),
            "spread_placement_state": (
                None
                if self._spread_placement_state is None
                else self._spread_placement_state.value
            ),
            "spread_state_expiry_us": self._spread_state_expiry_us,
            "started": self._started,
        }

    def advance_exogenous_clock_to(self, simulation_time_us: int) -> None:
        """Move only owned clocks; never generate an endogenous arrival."""

        if type(simulation_time_us) is not int:
            raise TypeError("simulation time must be integer microseconds")
        if simulation_time_us < self.clock.current_time_us:
            raise ValueError("simulation clock cannot move backward")
        self._activate_exogenous_replay()
        self.clock.advance_to(simulation_time_us)
        if self.intraday_clock is not None:
            self.intraday_clock.advance_to(simulation_time_us)

    def apply_exogenous_event(
        self,
        reference: FlowEvent,
    ) -> tuple[FlowEvent, tuple[object, ...]]:
        """Apply a fixed external command tape without scheduling new flow."""

        if reference.sequence != len(self._flow_events) + 1:
            raise ValueError("exogenous flow sequence is not contiguous with the fork")
        self.advance_exogenous_clock_to(reference.simulation_time_us)
        event_start = len(self.book.journal.events)
        actual_applied = False
        command = reference.command
        if reference.applied and command is not None:
            order_type = OrderType(str(command["order_type"]))
            if order_type is OrderType.LIMIT:
                self.book.process(
                    Order.limit(
                        str(command["order_id"]),
                        Side(str(command["side"])),
                        int(command["quantity"]),
                        int(command["price_ticks"]),
                    )
                )
                actual_applied = True
            elif order_type is OrderType.MARKET:
                self.book.process(
                    Order.market(
                        str(command["order_id"]),
                        Side(str(command["side"])),
                        int(command["quantity"]),
                    )
                )
                actual_applied = True
            else:
                cancellations = command.get("affected_orders")
                if isinstance(cancellations, list):
                    targets = tuple(
                        (str(item["command_id"]), str(item["target_order_id"]))
                        for item in cancellations
                        if isinstance(item, dict)
                    )
                else:
                    targets = (
                        (
                            str(command["command_id"]),
                            str(command["target_order_id"]),
                        ),
                    )
                for command_id, target_id in targets:
                    was_active = target_id in self.book.active_orders
                    self.book.cancel(target_id, command_id)
                    actual_applied = actual_applied or was_active
        exchange_events = self.book.journal.events[event_start:]
        realized = FlowEvent(
            sequence=reference.sequence,
            simulation_time_us=reference.simulation_time_us,
            family=reference.family,
            applied=actual_applied,
            command=reference.command,
            reason=(
                reference.reason
                if actual_applied or not reference.applied
                else "fixed_external_command_had_no_active_target_in_branch"
            ),
            exchange_event_start=(event_start + 1 if exchange_events else None),
            exchange_event_end=(
                len(self.book.journal.events) if exchange_events else None
            ),
            diagnostic=reference.diagnostic,
        )
        self._after_flow_event(realized)
        if self.intensity_modifier is not None:
            self.intensity_modifier.observe(
                realized,
                self.book,
                reference.simulation_time_us,
            )
        self.flow_model.observe(reference.family, reference.simulation_time_us)
        self._flow_events.append(realized)
        self.book.assert_invariants()
        return realized, tuple(exchange_events)

    def _activate_exogenous_replay(self) -> None:
        if self._exogenous_replay_active:
            return
        self._exogenous_replay_active = True
        self._pending_arrival_time_us = None
        self._pending_family = None
        self._scheduled_flow_time_us = None
        self._scheduled_flow_family = None
        self._pending_is_intraday_transition = False
        self._pending_is_spread_transition = False
        self._spread_state_expiry_us = None

    def start(self) -> None:
        if self._started:
            return
        self._initialize_book()
        self._initial_exchange_event_count = len(self.book.journal.events)
        self._initial_trade_count = len(self.book.trades)
        if self.intensity_modifier is not None:
            self.intensity_modifier.initialize(self.book, self.clock.current_time_us)
        if self.distribution_profile is not None:
            self._spread_placement_state = SpreadPlacementState.TOUCH_FAVORING
            self._schedule_spread_state_expiry()
        self._started = True
        self._schedule_next_arrival()

    def advance_to(
        self,
        simulation_time_us: int,
        on_event: Callable[[FlowEvent], None] | None = None,
    ) -> tuple[FlowEvent, ...]:
        if type(simulation_time_us) is not int:
            raise TypeError("simulation time must be integer microseconds")
        if simulation_time_us < self.clock.current_time_us:
            raise ValueError("simulation clock cannot move backward")
        if (
            self.intraday_clock is not None
            and simulation_time_us > self.intraday_clock.window.duration_us
        ):
            raise ValueError("simulation time exceeds the intraday exercise window")
        self.start()
        first_new_event = len(self._flow_events)

        while (
            self._pending_arrival_time_us is not None
            and self._pending_arrival_time_us <= simulation_time_us
        ):
            arrival_time_us = self._pending_arrival_time_us
            family = self._pending_family
            if self._pending_is_intraday_transition:
                self.clock.advance_to(arrival_time_us)
                if self.intraday_clock is None:
                    raise RuntimeError("intraday transition lacks an intraday clock")
                self.intraday_clock.advance_to(arrival_time_us)
                self._pending_is_intraday_transition = False
                self._schedule_next_arrival()
                continue
            if self._pending_is_spread_transition:
                self.clock.advance_to(arrival_time_us)
                if self.intraday_clock is not None:
                    self.intraday_clock.advance_to(arrival_time_us)
                self._advance_spread_placement_state()
                self._pending_is_spread_transition = False
                self._schedule_next_arrival(preserve_flow=True)
                continue
            if family is None:
                raise RuntimeError("scheduled regime arrival lacks an event family")
            self.clock.advance_to(arrival_time_us)
            if self.intraday_clock is not None:
                self.intraday_clock.advance_to(arrival_time_us)
            self._scheduled_flow_time_us = None
            self._scheduled_flow_family = None
            event = self._apply_arrival(len(self._flow_events) + 1, family)
            self._flow_events.append(event)
            if self.intensity_modifier is not None:
                self.intensity_modifier.observe(event, self.book, arrival_time_us)
            self.flow_model.observe(family, arrival_time_us)
            if on_event is not None:
                on_event(event)
            self._schedule_next_arrival()

        self.clock.advance_to(simulation_time_us)
        if self.intraday_clock is not None:
            self.intraday_clock.advance_to(simulation_time_us)
        self.book.assert_invariants()
        return tuple(self._flow_events[first_new_event:])

    def advance_by(self, delta_us: int) -> tuple[FlowEvent, ...]:
        if type(delta_us) is not int or delta_us < 0:
            raise ValueError("simulation delta must be a nonnegative integer")
        return self.advance_to(self.clock.current_time_us + delta_us)

    def run(self, seconds: int) -> SimulationResult:
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        if self._started:
            raise RuntimeError("regime flow has already started")
        end_time_us = seconds * MICROSECONDS_PER_SECOND
        self.advance_to(end_time_us)
        return SimulationResult(
            seed=self.seed,
            seconds=seconds,
            config=self.config,
            clock=self.clock,
            book=self.book,
            flow_events=self.flow_events,
            initial_exchange_event_count=self._initial_exchange_event_count,
            initial_trade_count=self._initial_trade_count,
            flow_model_config=self.flow_model.replay_config(),
            intensity_modifier_config=(
                None
                if self.intensity_modifier is None
                else self.intensity_modifier.replay_config()
            ),
            distribution_profile_config=(
                None
                if self.distribution_profile is None
                else self.distribution_profile.as_dict()
            ),
            intraday_profile_config=self._intraday_replay_config(),
            distribution_draws=self.distribution_draws,
        )

    def _schedule_next_arrival(self, *, preserve_flow: bool = False) -> None:
        if not preserve_flow:
            rates = self.policy.rates(self.book)
            if self.intensity_modifier is not None:
                self.last_intensity_inspection = self.intensity_modifier.inspect(
                    rates,
                    self.book,
                    self.clock.current_time_us,
                )
                rates = self.last_intensity_inspection.final_intensities
            arrival = self.flow_model.schedule_next(
                self.clock.current_time_us,
                rates,
                self.rng,
            )
            if arrival is not None and self.distribution_profile is not None:
                base_interval_us = (
                    arrival.simulation_time_us - self.clock.current_time_us
                )
                if base_interval_us <= 0:
                    raise ValueError("flow model must schedule a future arrival")
                timing_modifier = self._draw_distribution(
                    DistributionPurpose.INTER_EVENT_TIMING_MODIFIER,
                    "flow_interarrival_duration",
                )
                adjusted_interval_us = max(
                    1,
                    (
                        base_interval_us * timing_modifier
                        + INTER_EVENT_TIMING_SCALE // 2
                    )
                    // INTER_EVENT_TIMING_SCALE,
                )
                arrival = ScheduledFlowArrival(
                    self.clock.current_time_us + adjusted_interval_us,
                    arrival.family,
                )
            self._scheduled_flow_time_us = (
                None if arrival is None else arrival.simulation_time_us
            )
            self._scheduled_flow_family = None if arrival is None else arrival.family
        transition_time_us = (
            None
            if self.intraday_clock is None
            else self.intraday_clock.next_transition_time_us
        )
        candidates: list[tuple[int, int, str, FlowEventFamily | None]] = []
        if transition_time_us is not None:
            candidates.append((transition_time_us, 0, "intraday", None))
        if self._spread_state_expiry_us is not None:
            candidates.append((self._spread_state_expiry_us, 1, "spread", None))
        if self._scheduled_flow_time_us is not None:
            candidates.append(
                (
                    self._scheduled_flow_time_us,
                    2,
                    "flow",
                    self._scheduled_flow_family,
                )
            )
        if not candidates:
            self._pending_arrival_time_us = None
            self._pending_family = None
            self._pending_is_intraday_transition = False
            self._pending_is_spread_transition = False
            return
        pending_time, _, pending_kind, pending_family = min(candidates)
        self._pending_arrival_time_us = pending_time
        self._pending_family = pending_family
        self._pending_is_intraday_transition = pending_kind == "intraday"
        self._pending_is_spread_transition = pending_kind == "spread"
        if pending_kind == "intraday":
            self._pending_family = None
            return
        if pending_kind == "spread":
            self._pending_family = None
            return

    def _draw_order_size(self, family: FlowEventFamily) -> int:
        purpose = (
            DistributionPurpose.TRADE_SIZE
            if family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
            else DistributionPurpose.ORDER_SIZE
        )
        size = (
            self._draw_distribution(purpose, f"{family.value}_quantity")
            if self.distribution_profile is not None
            else self.policy.size_distribution(family).draw(self.rng)
        )
        scale = float(self.policy.parameter_overrides.get("order_size_scale", 1.0))
        scale *= self.dimensions.order_size_scale(family)
        family_scale_key = (
            "market_size_scale"
            if family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
            else "limit_size_scale"
        )
        scale *= float(self.policy.parameter_overrides.get(family_scale_key, 1.0))
        modifiers = self.policy.intraday_modifiers
        if modifiers is not None:
            scale *= (
                modifiers.trade_size
                if family in {FlowEventFamily.MARKET_BUY, FlowEventFamily.MARKET_SELL}
                else modifiers.depth
            )
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("order_size_scale must be finite and positive")
        return max(1, round(size * scale))

    def _draw_depth(self, family: FlowEventFamily) -> int:
        depth = (
            self._draw_distribution(
                DistributionPurpose.LIMIT_PLACEMENT_DEPTH,
                f"{family.value}_placement",
            )
            if self.distribution_profile is not None
            else self.policy.depth_distribution(family).draw(self.rng)
        )
        modifiers = self.policy.intraday_modifiers
        spread_scale = 1.0 if modifiers is None else modifiers.spread_tendency
        placement_scale = float(
            self.policy.parameter_overrides.get("placement_depth_scale", 1.0)
        )
        placement_offset = int(
            self.policy.parameter_overrides.get("placement_depth_offset", 0)
        )
        if not math.isfinite(placement_scale) or placement_scale < 0:
            raise ValueError("placement_depth_scale must be finite and nonnegative")
        adjusted_depth = max(
            0,
            round((depth + 1) * spread_scale * placement_scale)
            - 1
            + placement_offset,
        )
        if self._spread_placement_state is SpreadPlacementState.TOUCH_FAVORING:
            return max(0, adjusted_depth - 1)
        if self._spread_placement_state is SpreadPlacementState.DEPTH_FAVORING:
            return adjusted_depth + 1
        return adjusted_depth

    def _draw_initial_queue_size(self) -> int:
        size = (
            self._draw_distribution(
                DistributionPurpose.QUEUE_DEPTH,
                "initial_resting_queue_quantity",
            )
            if self.distribution_profile is not None
            else super()._draw_initial_queue_size()
        )
        modifiers = self.policy.intraday_modifiers
        return size if modifiers is None else max(1, round(size * modifiers.depth))

    def _submit_cancel(
        self,
        sequence: int,
        side: Side,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if self.distribution_profile is None:
            return super()._submit_cancel(sequence, side)
        budget = self._draw_distribution(
            DistributionPurpose.CANCEL_SIZE,
            f"cancel_{side.value}_volume_budget",
        )
        candidates = sorted(
            (
                order
                for order in self.book.active_orders.values()
                if order.side is side
                and order.order_type is OrderType.LIMIT
                and order.owner is OrderOwner.SIMULATED
            ),
            key=lambda order: (order.resting_sequence, order.order_id),
        )
        if not candidates:
            command_id = self._command_id(
                sequence,
                "CB-BUDGET" if side is Side.BUY else "CA-BUDGET",
            )
            return {
                "affected_order_ids": [],
                "affected_orders": [],
                "actual_cancelled_quantity": 0,
                "cancelled_quantity": 0,
                "command_id": command_id,
                "command_ids": [],
                "order_type": OrderType.CANCEL.value,
                "overshoot_quantity": 0,
                "price_ticks": None,
                "requested_cancel_quantity": budget,
                "side": side.value,
                "target_order_id": None,
                "unfulfilled_quantity": budget,
            }, "no_active_liquidity"
        start = self.rng.index(len(candidates))
        ordered_candidates = candidates[start:] + candidates[:start]
        affected_orders: list[dict[str, Any]] = []
        actual_quantity = 0
        suffix = "CB" if side is Side.BUY else "CA"
        for target in ordered_candidates:
            if actual_quantity >= budget:
                break
            quantity = target.remaining_quantity
            command_id = self._command_id(
                sequence,
                f"{suffix}-{len(affected_orders) + 1:03d}",
            )
            affected_orders.append(
                {
                    "cancelled_quantity": quantity,
                    "command_id": command_id,
                    "price_ticks": target.price_ticks,
                    "target_order_id": target.order_id,
                }
            )
            self.book.cancel(target.order_id, command_id)
            actual_quantity += quantity
        first = affected_orders[0]
        return {
            "affected_order_ids": [
                item["target_order_id"] for item in affected_orders
            ],
            "affected_orders": affected_orders,
            "actual_cancelled_quantity": actual_quantity,
            "cancelled_quantity": actual_quantity,
            "command_id": first["command_id"],
            "command_ids": [item["command_id"] for item in affected_orders],
            "order_type": OrderType.CANCEL.value,
            "overshoot_quantity": max(0, actual_quantity - budget),
            "price_ticks": first["price_ticks"],
            "requested_cancel_quantity": budget,
            "side": side.value,
            "target_order_id": first["target_order_id"],
            "unfulfilled_quantity": max(0, budget - actual_quantity),
        }, None

    def _draw_distribution(
        self,
        purpose: DistributionPurpose,
        consumer: str,
    ) -> int:
        if self.distribution_profile is None:
            raise RuntimeError("distribution draw requires an active profile")
        value = self.distribution_profile.distribution(purpose).draw(self.rng)
        self._distribution_draws.append(
            DistributionDrawRecord(
                sequence=len(self._distribution_draws) + 1,
                profile_id=self.distribution_profile.profile_id,
                purpose=purpose,
                sampled_value=value,
                simulation_time_us=self.clock.current_time_us,
                consumer=consumer,
            )
        )
        return value

    def _schedule_spread_state_expiry(self) -> None:
        if self._spread_placement_state is None:
            raise RuntimeError("spread state duration requires an active state")
        duration_us = self._draw_distribution(
            DistributionPurpose.SPREAD_STATE_DURATION,
            f"spread_state:{self._spread_placement_state.value.lower()}",
        )
        self._spread_state_expiry_us = self.clock.current_time_us + duration_us

    def _advance_spread_placement_state(self) -> None:
        if self._spread_state_expiry_us != self.clock.current_time_us:
            raise RuntimeError("spread state advanced outside its scheduled boundary")
        if self._spread_placement_state is SpreadPlacementState.TOUCH_FAVORING:
            self._spread_placement_state = SpreadPlacementState.DEPTH_FAVORING
        elif self._spread_placement_state is SpreadPlacementState.DEPTH_FAVORING:
            self._spread_placement_state = SpreadPlacementState.TOUCH_FAVORING
        else:
            raise RuntimeError("spread transition lacks an active state")
        self._schedule_spread_state_expiry()

    def _intraday_replay_config(self) -> dict[str, object] | None:
        if self.intraday_clock is None:
            return None
        result: dict[str, object] = {
            "profile": self.intraday_clock.profile.as_dict(),
            "window": self.intraday_clock.window.as_dict(),
        }
        if self.intraday_clock.observed_volume is not None:
            result["observed_volume"] = self.intraday_clock.observed_volume.as_dict()
        return result

    def _after_flow_event(self, event: FlowEvent) -> None:
        bid_size = best_level_size(self.book, Side.BUY)
        ask_size = best_level_size(self.book, Side.SELL)
        spread = (
            self.book.best_ask - self.book.best_bid
            if self.book.best_bid is not None and self.book.best_ask is not None
            else None
        )
        self.observations.append(
            BookObservation(
                simulation_time_us=event.simulation_time_us,
                best_bid_ticks=self.book.best_bid,
                best_ask_ticks=self.book.best_ask,
                best_bid_size=bid_size,
                best_ask_size=ask_size,
                imbalance=book_imbalance(self.book),
                spread_ticks=spread,
            )
        )
