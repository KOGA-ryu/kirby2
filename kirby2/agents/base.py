"""Causal agent interface: immutable observations in, ordinary order intents out."""

from __future__ import annotations

from abc import ABC, abstractmethod

from kirby2.simulation.rng import SeededRng

from .models import AgentIntent, AgentObservation, AgentSpec, OwnOrderView


class MarketAgent(ABC):
    """Participant boundary with no exchange, queue, future, or truth-log reference."""

    @property
    @abstractmethod
    def spec(self) -> AgentSpec: ...

    @property
    @abstractmethod
    def inventory(self) -> int: ...

    @property
    @abstractmethod
    def remaining_budget(self) -> int: ...

    @property
    @abstractmethod
    def own_orders(self) -> tuple[OwnOrderView, ...]: ...

    @abstractmethod
    def decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]: ...

    @abstractmethod
    def reconcile(
        self,
        inventory: int,
        own_orders: tuple[OwnOrderView, ...],
    ) -> None: ...

    @abstractmethod
    def register_action(self, simulation_time_us: int) -> None: ...

    @abstractmethod
    def can_register_action(self, simulation_time_us: int) -> bool: ...

    @abstractmethod
    def register_accepted_quantity(self, quantity: int) -> None: ...

    @abstractmethod
    def runtime_state(self) -> dict[str, object]: ...

    @abstractmethod
    def assert_invariants(self, simulation_time_us: int) -> None: ...


class BaseMarketAgent(MarketAgent):
    def __init__(self, spec: AgentSpec, seed: int) -> None:
        self._spec = spec
        self._rng = SeededRng(seed)
        self._inventory = 0
        self._accepted_quantity = 0
        self._own_orders: tuple[OwnOrderView, ...] = ()
        self._action_times_us: list[int] = []
        self._decision_count = 0
        self._midpoint_history_x2: list[int] = []
        self._custom_state: dict[str, int | bool | str | None] = {}

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def inventory(self) -> int:
        return self._inventory

    @property
    def remaining_budget(self) -> int:
        return self.spec.bounds.quantity_budget - self._accepted_quantity

    @property
    def own_orders(self) -> tuple[OwnOrderView, ...]:
        return self._own_orders

    @property
    def rng(self) -> SeededRng:
        return self._rng

    @property
    def decision_count(self) -> int:
        return self._decision_count

    @property
    def midpoint_history_x2(self) -> tuple[int, ...]:
        return tuple(self._midpoint_history_x2)

    @property
    def custom_state(self) -> dict[str, int | bool | str | None]:
        return self._custom_state

    def decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]:
        if observation.simulation_time_us < self.spec.bounds.lifetime_start_us:
            raise RuntimeError("agent received an observation before its lifetime")
        if observation.simulation_time_us > self.spec.bounds.lifetime_end_us:
            raise RuntimeError("agent received an observation after its lifetime")
        if observation.own_inventory != self.inventory:
            raise RuntimeError("agent observation inventory disagrees with own state")
        if observation.own_remaining_budget != self.remaining_budget:
            raise RuntimeError("agent observation budget disagrees with own state")
        if observation.own_orders != self.own_orders:
            raise RuntimeError("agent observation working orders disagree with own state")
        if observation.information_boundary != "PUBLIC_MARKET_AND_OWN_STATE_AT_DECISION_TIME":
            raise RuntimeError("agent observation crossed the causal information boundary")
        midpoint = observation.midpoint_x2
        if midpoint is not None:
            self._midpoint_history_x2.append(midpoint)
            self._midpoint_history_x2 = self._midpoint_history_x2[-16:]
        self._decision_count += 1
        intents = self._decide(observation)
        if len(intents) > 1:
            raise RuntimeError("one agent decision may emit at most one ordinary order action")
        return intents

    @abstractmethod
    def _decide(self, observation: AgentObservation) -> tuple[AgentIntent, ...]: ...

    def reconcile(
        self,
        inventory: int,
        own_orders: tuple[OwnOrderView, ...],
    ) -> None:
        self._inventory = inventory
        self._own_orders = tuple(sorted(own_orders, key=lambda item: item.order_id))

    def register_action(self, simulation_time_us: int) -> None:
        if self._action_times_us and simulation_time_us < self._action_times_us[-1]:
            raise RuntimeError("agent action times moved backward")
        self._action_times_us.append(simulation_time_us)

    def can_register_action(self, simulation_time_us: int) -> bool:
        recent = sum(
            simulation_time_us - 1_000_000 < candidate <= simulation_time_us
            for candidate in self._action_times_us
        )
        return recent < self.spec.bounds.max_orders_per_second

    def register_accepted_quantity(self, quantity: int) -> None:
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("accepted agent quantity must be positive")
        self._accepted_quantity += quantity

    def runtime_state(self) -> dict[str, object]:
        return {
            "accepted_quantity": self._accepted_quantity,
            "action_times_us": list(self._action_times_us),
            "custom_state": dict(sorted(self._custom_state.items())),
            "decision_count": self._decision_count,
            "inventory": self.inventory,
            "midpoint_history_x2": list(self._midpoint_history_x2),
            "own_orders": [
                {
                    "auction_only": item.auction_only,
                    "order_id": item.order_id,
                    "price_ticks": item.price_ticks,
                    "remaining_quantity": item.remaining_quantity,
                    "side": item.side.value,
                }
                for item in self.own_orders
            ],
            "remaining_budget": self.remaining_budget,
            "rng": self.rng.runtime_state(),
            "spec": self.spec.identity_dict(),
        }

    def assert_invariants(self, simulation_time_us: int) -> None:
        bounds = self.spec.bounds
        if not 0 <= self.remaining_budget <= bounds.quantity_budget:
            raise RuntimeError(f"agent {self.spec.agent_id} quantity budget is invalid")
        if abs(self.inventory) > bounds.max_abs_inventory:
            raise RuntimeError(f"agent {self.spec.agent_id} inventory limit was exceeded")
        working_quantity = sum(item.remaining_quantity for item in self.own_orders)
        if working_quantity > bounds.max_working_quantity:
            raise RuntimeError(f"agent {self.spec.agent_id} working risk was exceeded")
        if any(item.remaining_quantity <= 0 for item in self.own_orders):
            raise RuntimeError(f"agent {self.spec.agent_id} retained a closed own order")
        for index, time_us in enumerate(self._action_times_us):
            count = sum(
                1
                for candidate in self._action_times_us[: index + 1]
                if time_us - 1_000_000 < candidate <= time_us
            )
            if count > bounds.max_orders_per_second:
                raise RuntimeError(f"agent {self.spec.agent_id} order-rate limit was exceeded")
        if simulation_time_us < 0:
            raise RuntimeError("agent invariant clock is invalid")

    def choose_quantity(self) -> int:
        choices = tuple(
            value
            for value in (
                max(1, self.spec.policy.clip_quantity // 2),
                self.spec.policy.clip_quantity,
            )
            if value <= self.spec.bounds.max_order_quantity
            and value <= self.remaining_budget
        )
        return 0 if not choices else choices[self.rng.index(len(choices))]

    def first_own_order(self, *, side=None) -> OwnOrderView | None:
        return next(
            (
                item
                for item in self.own_orders
                if side is None or item.side is side
            ),
            None,
        )
