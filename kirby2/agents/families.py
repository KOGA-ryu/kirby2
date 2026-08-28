"""Bounded synthetic participant families for training-only market ecologies."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from kirby2.exchange import OrderType, SessionState, Side

from .base import BaseMarketAgent, MarketAgent
from .models import (
    AgentFamily,
    AgentIntent,
    AgentIntentType,
    AgentObservation,
    AgentSafetyClass,
    AgentSpec,
    OwnOrderView,
)


_AUCTION_STATES = {
    SessionState.PREOPEN,
    SessionState.OPENING_AUCTION,
    SessionState.REOPENING_AUCTION,
    SessionState.CLOSING_AUCTION,
}


def _cancel(order_id: str, rationale: str) -> tuple[AgentIntent, ...]:
    return (
        AgentIntent(
            AgentIntentType.CANCEL,
            rationale,
            cancel_target_order_id=order_id,
        ),
    )


def _limit(
    side: Side,
    quantity: int,
    price_ticks: int,
    rationale: str,
    *,
    auction_only: bool = False,
) -> tuple[AgentIntent, ...]:
    if quantity <= 0:
        return ()
    return (
        AgentIntent(
            AgentIntentType.SUBMIT,
            rationale,
            order_type=OrderType.LIMIT,
            side=side,
            quantity=quantity,
            price_ticks=price_ticks,
            auction_only=auction_only,
        ),
    )


def _market(
    side: Side,
    quantity: int,
    rationale: str,
) -> tuple[AgentIntent, ...]:
    if quantity <= 0:
        return ()
    return (
        AgentIntent(
            AgentIntentType.SUBMIT,
            rationale,
            order_type=OrderType.MARKET,
            side=side,
            quantity=quantity,
        ),
    )


def _quote_price(
    observation: AgentObservation,
    side: Side,
    offset_ticks: int,
) -> int | None:
    if side is Side.BUY:
        touch = observation.best_bid_ticks
        if touch is None and observation.best_ask_ticks is not None:
            touch = observation.best_ask_ticks - 1
        return None if touch is None else max(1, touch - offset_ticks)
    touch = observation.best_ask_ticks
    if touch is None and observation.best_bid_ticks is not None:
        touch = observation.best_bid_ticks + 1
    return None if touch is None else touch + offset_ticks


class NoiseTrader(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        if self.own_orders and self.rng.index(7) == 0:
            order = self.own_orders[self.rng.index(len(self.own_orders))]
            return _cancel(order.order_id, "NOISE_CANCEL")
        quantity = self.choose_quantity()
        if not quantity:
            return ()
        side = Side.BUY if self.rng.index(2) == 0 else Side.SELL
        if self.rng.index(5) == 0:
            return _market(side, quantity, "NOISE_AGGRESSIVE_FLOW")
        price = _quote_price(observation, side, self.rng.index(3))
        return () if price is None else _limit(side, quantity, price, "NOISE_RESTING_FLOW")


class PassiveMarketMaker(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        preferred = self.spec.policy.preferred_side
        sides = (preferred,) if preferred is not None else (Side.BUY, Side.SELL)
        reserve = self.spec.policy.reserve_price_ticks
        for side in sides:
            desired = (
                reserve
                if reserve is not None and side is preferred
                else _quote_price(observation, side, self.spec.policy.quote_offset_ticks)
            )
            if desired is None:
                continue
            own = self.first_own_order(side=side)
            if own is not None and own.price_ticks != desired:
                return _cancel(own.order_id, "PASSIVE_QUOTE_REFRESH")
            if own is None:
                return _limit(
                    side,
                    min(self.spec.policy.clip_quantity, self.remaining_budget),
                    desired,
                    (
                        "CONTROLLED_RESERVE_REPLENISHMENT"
                        if reserve is not None
                        else "PASSIVE_TWO_SIDED_LIQUIDITY"
                    ),
                )
        return ()


class InventorySensitiveMarketMaker(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        threshold = max(1, self.spec.bounds.max_abs_inventory // 4)
        if self.inventory >= threshold:
            priority = (Side.SELL, Side.BUY)
        elif self.inventory <= -threshold:
            priority = (Side.BUY, Side.SELL)
        else:
            priority = (Side.BUY, Side.SELL)
        for side in priority:
            skew = 0
            if side is Side.BUY and self.inventory > 0:
                skew = 1
            elif side is Side.SELL and self.inventory < 0:
                skew = 1
            desired = _quote_price(
                observation,
                side,
                self.spec.policy.quote_offset_ticks + skew,
            )
            if desired is None:
                continue
            own = self.first_own_order(side=side)
            if own is not None and own.price_ticks != desired:
                return _cancel(own.order_id, "INVENTORY_QUOTE_REPRICE")
            if own is None:
                return _limit(
                    side,
                    min(self.spec.policy.clip_quantity, self.remaining_budget),
                    desired,
                    "INVENTORY_BOUNDED_LIQUIDITY",
                )
        return ()


class MomentumTrader(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        history = self.midpoint_history_x2
        if len(history) < 3:
            return ()
        change_x2 = history[-1] - history[max(0, len(history) - 4)]
        if change_x2 == 0:
            return ()
        side = Side.BUY if change_x2 > 0 else Side.SELL
        return _market(side, self.choose_quantity(), "PUBLIC_MOMENTUM_RESPONSE")


class MeanReversionTrader(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        history = self.midpoint_history_x2
        if len(history) < 4:
            return ()
        average_x2 = sum(history) // len(history)
        deviation_x2 = history[-1] - average_x2
        if abs(deviation_x2) < 2:
            return ()
        side = Side.SELL if deviation_x2 > 0 else Side.BUY
        price = _quote_price(observation, side, 0)
        return (
            ()
            if price is None
            else _limit(
                side,
                self.choose_quantity(),
                price,
                "PUBLIC_MEAN_REVERSION_RESPONSE",
            )
        )


class ScheduledMetaorder(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if (
            observation.session_state is not SessionState.CONTINUOUS
            or observation.simulation_time_us < self.spec.policy.activation_time_us
        ):
            return ()
        side = self.spec.policy.preferred_side
        if side is None:
            raise RuntimeError("scheduled metaorder requires a configured side")
        quantity = min(self.spec.policy.clip_quantity, self.remaining_budget)
        return _market(side, quantity, "BOUNDED_SCHEDULED_PARENT_EXECUTION")


class DistressedLiquidator(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if (
            observation.session_state is not SessionState.CONTINUOUS
            or observation.simulation_time_us < self.spec.policy.activation_time_us
        ):
            return ()
        side = self.spec.policy.preferred_side or Side.SELL
        urgency_multiplier = 2 if self.remaining_budget < self.spec.bounds.quantity_budget // 2 else 1
        quantity = min(
            self.spec.policy.clip_quantity * urgency_multiplier,
            self.spec.bounds.max_order_quantity,
            self.remaining_budget,
        )
        return _market(side, quantity, "BOUNDED_DISTRESSED_LIQUIDATION")


class LiquidityWithdrawer(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        withdrawal = self.spec.policy.withdrawal_time_us
        if withdrawal is None:
            raise RuntimeError("liquidity withdrawer requires a withdrawal time")
        if observation.simulation_time_us >= withdrawal:
            if self.own_orders:
                return _cancel(self.own_orders[0].order_id, "BOUNDED_LIQUIDITY_WITHDRAWAL")
            self.custom_state["withdrawn"] = True
            return ()
        for side in (Side.BUY, Side.SELL):
            if self.first_own_order(side=side) is None:
                price = _quote_price(observation, side, 0)
                return (
                    ()
                    if price is None
                    else _limit(
                        side,
                        min(self.spec.policy.clip_quantity, self.remaining_budget),
                        price,
                        "TEMPORARY_DISPLAYED_LIQUIDITY",
                    )
                )
        return ()


class LatentValueTrader(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if (
            observation.session_state is not SessionState.CONTINUOUS
            or observation.simulation_time_us < self.spec.policy.activation_time_us
            or observation.midpoint_x2 is None
        ):
            return ()
        latent = self.spec.policy.latent_value_ticks
        if latent is None:
            raise RuntimeError("controlled latent-value actor has no latent value")
        difference_x2 = latent * 2 - observation.midpoint_x2
        if difference_x2 == 0:
            return ()
        side = Side.BUY if difference_x2 > 0 else Side.SELL
        quantity = self.choose_quantity()
        if abs(difference_x2) >= 4:
            return _market(side, quantity, "CONTROLLED_LATENT_VALUE_DISLOCATION")
        price = _quote_price(observation, side, 0)
        return (
            ()
            if price is None
            else _limit(side, quantity, price, "CONTROLLED_LATENT_VALUE_QUOTE")
        )


class AuctionParticipant(BaseMarketAgent):
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state not in _AUCTION_STATES or self.own_orders:
            return ()
        side = self.spec.policy.preferred_side
        if side is None:
            side = Side.BUY if self.rng.index(2) == 0 else Side.SELL
        reference_x2 = observation.midpoint_x2
        if reference_x2 is None:
            reference_x2 = (
                (observation.best_bid_ticks or 10_000)
                + (observation.best_ask_ticks or 10_000)
            )
        price = max(1, reference_x2 // 2 + (1 if side is Side.BUY else -1))
        return _limit(
            side,
            min(self.spec.policy.clip_quantity, self.remaining_budget),
            price,
            "BOUNDED_AUCTION_PARTICIPATION",
            auction_only=True,
        )


class DeceptiveDisplayTrainingAgent(BaseMarketAgent):
    """Recognition-drill-only unreliable display; never configurable as a live tactic."""

    def __init__(self, spec: AgentSpec, seed: int) -> None:
        if spec.safety_class is not AgentSafetyClass.RECOGNITION_DRILL_ONLY:
            raise ValueError("deceptive-display training agent requires recognition-only scope")
        super().__init__(spec, seed)
        self.custom_state["display_cycle"] = 0

    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.session_state is not SessionState.CONTINUOUS:
            return ()
        side = self.spec.policy.preferred_side or Side.SELL
        if self.own_orders:
            should_withdraw = (
                self.decision_count % 3 == 0
                or (
                    self.spec.policy.withdrawal_time_us is not None
                    and observation.simulation_time_us
                    >= self.spec.policy.withdrawal_time_us
                )
            )
            if should_withdraw:
                self.custom_state["display_cycle"] = int(
                    self.custom_state["display_cycle"] or 0
                ) + 1
                return _cancel(
                    self.own_orders[0].order_id,
                    "DISPLAY_RELIABILITY_RECOGNITION_DRILL",
                )
            return ()
        if (
            self.custom_state["display_cycle"]
            and not self.spec.policy.repeat_display
        ):
            return ()
        price = _quote_price(
            observation,
            side,
            self.spec.policy.quote_offset_ticks,
        )
        return (
            ()
            if price is None
            else _limit(
                side,
                min(self.spec.policy.clip_quantity, self.remaining_budget),
                price,
                "UNRELIABLE_DISPLAY_RECOGNITION_DRILL",
            )
        )


_AGENT_TYPES: dict[AgentFamily, type[BaseMarketAgent]] = {
    AgentFamily.NOISE_TRADER: NoiseTrader,
    AgentFamily.PASSIVE_MARKET_MAKER: PassiveMarketMaker,
    AgentFamily.INVENTORY_SENSITIVE_MARKET_MAKER: InventorySensitiveMarketMaker,
    AgentFamily.MOMENTUM_TRADER: MomentumTrader,
    AgentFamily.MEAN_REVERSION_TRADER: MeanReversionTrader,
    AgentFamily.SCHEDULED_METAORDER: ScheduledMetaorder,
    AgentFamily.DISTRESSED_LIQUIDATOR: DistressedLiquidator,
    AgentFamily.LIQUIDITY_WITHDRAWER: LiquidityWithdrawer,
    AgentFamily.LATENT_VALUE_TRADER: LatentValueTrader,
    AgentFamily.AUCTION_PARTICIPANT: AuctionParticipant,
    AgentFamily.DECEPTIVE_DISPLAY: DeceptiveDisplayTrainingAgent,
}


def create_agent(spec: AgentSpec, master_seed: int) -> MarketAgent:
    """Create an agent with a stable, explicitly owned per-agent RNG stream."""

    return create_agent_with_seed(spec, derive_agent_seed(spec, master_seed))


def derive_agent_seed(spec: AgentSpec, master_seed: int) -> int:
    """Return the legacy per-agent substream seed without touching global RNG state."""

    if type(master_seed) is not int or master_seed < 0:
        raise ValueError("agent master seed must be a nonnegative integer")
    identity = f"{master_seed}:{spec.agent_id}:{spec.family.value}".encode("utf-8")
    return int(hashlib.sha256(identity).hexdigest()[:16], 16)


def create_agent_with_seed(spec: AgentSpec, agent_seed: int) -> MarketAgent:
    """Construct one family policy from an explicitly labeled scheduler seed."""

    if type(agent_seed) is not int or agent_seed < 0:
        raise ValueError("agent substream seed must be a nonnegative integer")
    return _AGENT_TYPES[spec.family](spec, agent_seed)


def restore_agent_runtime_state(
    spec: AgentSpec,
    agent_seed: int,
    payload: Mapping[str, object],
) -> MarketAgent:
    """Validate and restore every policy-owned field for a fresh scheduler process."""

    expected_fields = {
        "accepted_quantity",
        "action_times_us",
        "custom_state",
        "decision_count",
        "inventory",
        "midpoint_history_x2",
        "own_orders",
        "remaining_budget",
        "rng",
        "spec",
    }
    actual_fields = set(payload)
    if actual_fields != expected_fields:
        raise ValueError(
            "agent runtime-state fields are not exact: "
            f"missing={sorted(expected_fields - actual_fields)} "
            f"unknown={sorted(actual_fields - expected_fields)}"
        )
    if payload["spec"] != spec.identity_dict():
        raise ValueError("agent runtime state is bound to a different specification")

    def exact_int(value: object, label: str, *, minimum: int | None = None) -> int:
        if type(value) is not int or (minimum is not None and value < minimum):
            raise ValueError(f"{label} must be an integer")
        return value

    accepted_quantity = exact_int(
        payload["accepted_quantity"], "accepted quantity", minimum=0
    )
    decision_count = exact_int(
        payload["decision_count"], "decision count", minimum=0
    )
    inventory = exact_int(payload["inventory"], "inventory")
    remaining_budget = exact_int(
        payload["remaining_budget"], "remaining budget", minimum=0
    )
    action_times = payload["action_times_us"]
    midpoint_history = payload["midpoint_history_x2"]
    own_orders = payload["own_orders"]
    custom_state = payload["custom_state"]
    rng_state = payload["rng"]
    if type(action_times) is not list or any(type(item) is not int for item in action_times):
        raise TypeError("agent action times must be an integer array")
    if action_times != sorted(action_times) or any(item < 0 for item in action_times):
        raise ValueError("agent action times must be nonnegative and ordered")
    if type(midpoint_history) is not list or any(
        type(item) is not int for item in midpoint_history
    ):
        raise TypeError("agent midpoint history must be an integer array")
    if len(midpoint_history) > 16:
        raise ValueError("agent midpoint history exceeds its bounded window")
    if type(own_orders) is not list or any(
        not isinstance(item, Mapping) for item in own_orders
    ):
        raise TypeError("agent own-order state must be an object array")
    if not isinstance(custom_state, Mapping) or any(
        type(key) is not str
        or value is not None
        and type(value) not in {bool, int, str}
        for key, value in custom_state.items()
    ):
        raise TypeError("agent custom state must use strict scalar JSON values")
    if not isinstance(rng_state, Mapping):
        raise TypeError("agent RNG state must be an object")
    rng_expected = {
        "gaussian_cache",
        "internal_state",
        "random_state_version",
        "seed",
    }
    if set(rng_state) != rng_expected:
        raise ValueError("agent RNG state fields are not exact")
    if rng_state["gaussian_cache"] is not None:
        raise ValueError("agent RNG Gaussian cache must remain unused")
    version = exact_int(rng_state["random_state_version"], "RNG state version")
    if version != 3:
        raise ValueError("unsupported agent RNG state version")
    if exact_int(rng_state["seed"], "RNG seed", minimum=0) != agent_seed:
        raise ValueError("agent RNG checkpoint is bound to a different substream seed")
    internal = rng_state["internal_state"]
    if type(internal) is not list or any(type(item) is not int for item in internal):
        raise TypeError("agent RNG internal state must be an integer array")

    agent = create_agent_with_seed(spec, agent_seed)
    try:
        agent.rng._random.setstate(  # type: ignore[attr-defined]  # owned RNG adapter
            (version, tuple(internal), None)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("agent RNG internal state is invalid") from error
    agent._accepted_quantity = accepted_quantity  # type: ignore[attr-defined]
    agent._action_times_us = list(action_times)  # type: ignore[attr-defined]
    agent._decision_count = decision_count  # type: ignore[attr-defined]
    agent._midpoint_history_x2 = list(midpoint_history)  # type: ignore[attr-defined]
    agent._custom_state = dict(sorted(custom_state.items()))  # type: ignore[attr-defined]
    agent.reconcile(
        inventory,
        tuple(OwnOrderView.from_dict(item) for item in own_orders),
    )
    if agent.remaining_budget != remaining_budget:
        raise ValueError("agent remaining budget does not reconcile to accepted quantity")
    agent.assert_invariants(max(action_times, default=0))
    if agent.runtime_state() != dict(payload):
        raise ValueError("agent runtime state is not canonical")
    return agent
