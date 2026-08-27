"""Deterministic simulator-only execution algorithm baselines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from kirby2.exchange.models import Side
from kirby2.multivenue import RoutePolicy, RouteStyle
from kirby2.multivenue.models import canonical_sha256

from .models import (
    AlgorithmAction,
    AlgorithmActionType,
    AlgorithmName,
    AlgorithmObservation,
    AlgorithmParameterManifest,
    ExecutionObjective,
)


class ExecutionAlgorithm(ABC):
    """Algorithms consume one client observation and return one declarative action."""

    def __init__(self, manifest: AlgorithmParameterManifest) -> None:
        self.manifest = manifest
        self.objective: ExecutionObjective | None = None

    def reset(self, objective: ExecutionObjective) -> None:
        self.objective = objective

    @abstractmethod
    def decide(self, observation: AlgorithmObservation) -> AlgorithmAction:
        raise NotImplementedError

    def _terminal(self, observation: AlgorithmObservation) -> AlgorithmAction | None:
        if observation.remaining_quantity == 0:
            return _finish("objective completed")
        return None


class _TimedPassiveAlgorithm(ExecutionAlgorithm):
    def __init__(self, manifest: AlgorithmParameterManifest) -> None:
        super().__init__(manifest)
        self.last_submit_time_us: int | None = None

    def reset(self, objective: ExecutionObjective) -> None:
        super().reset(objective)
        self.last_submit_time_us = None

    def _timeout(self, observation: AlgorithmObservation) -> bool:
        timeout = _int(self.manifest, "passive_timeout_us")
        return (
            self.last_submit_time_us is not None
            and observation.simulation_time_us - self.last_submit_time_us >= timeout
        )

    def _near_deadline(self, observation: AlgorithmObservation) -> bool:
        timeout = _int(self.manifest, "passive_timeout_us")
        return observation.simulation_time_us >= observation.objective.deadline_us - timeout


class ImmediateMarketAlgorithm(ExecutionAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.available_to_submit <= 0 or observation.latency_state.pending_route_count:
            return _wait("an order is already working or in flight")
        quantity = _slice(self.manifest, observation.available_to_submit)
        return _submit(
            quantity,
            RoutePolicy.BEST_DISPLAYED_PRICE,
            RouteStyle.AGGRESSIVE,
            "submit immediately to the best displayed venue",
            price_limit=_optional_int(self.manifest, "price_limit_ticks"),
        )


class JoinBestAlgorithm(_TimedPassiveAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.latency_state.pending_route_count:
            return _wait("awaiting venue response")
        _venue_id, join_price = _join_price(observation)
        if observation.working_orders:
            if self._near_deadline(observation):
                self.last_submit_time_us = observation.simulation_time_us
                return _replace_market(observation, self.manifest, "deadline requires aggressive completion")
            if self._timeout(observation):
                if join_price is None or not _price_within_manifest_limit(
                    observation,
                    self.manifest,
                    join_price,
                ):
                    return _wait("rejoin price is absent or outside the price limit")
                self.last_submit_time_us = observation.simulation_time_us
                return _replace_passive(
                    observation,
                    self.manifest,
                    join_price,
                    "passive timeout expired; rejoin best",
                )
            return _wait("joined order remains working")
        if observation.available_to_submit <= 0:
            return _wait("no uncommitted quantity is available")
        if (
            join_price is None
            or not _spread_allowed(observation, self.manifest)
            or not _price_within_manifest_limit(
                observation,
                self.manifest,
                join_price,
            )
        ):
            return _wait("join price is absent or outside spread/price limits")
        self.last_submit_time_us = observation.simulation_time_us
        return _submit(
            _slice(self.manifest, observation.available_to_submit),
            RoutePolicy.PASSIVE_QUEUE,
            RouteStyle.PASSIVE,
            "join the observable best price",
            price_limit=join_price,
        )


class ImproveOneTickAlgorithm(_TimedPassiveAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.latency_state.pending_route_count:
            return _wait("awaiting venue response")
        venue_id, price = _improved_price(observation)
        if observation.working_orders:
            if self._near_deadline(observation):
                return _replace_market(observation, self.manifest, "deadline overrides passive improvement")
            if self._timeout(observation):
                if (
                    price is None
                    or venue_id is None
                    or not _price_within_manifest_limit(
                        observation,
                        self.manifest,
                        price,
                    )
                ):
                    return _wait("refresh price is absent or outside the price limit")
                self.last_submit_time_us = observation.simulation_time_us
                return _replace_passive(
                    observation,
                    self.manifest,
                    price,
                    "passive timeout expired; refresh one-tick improvement",
                    venue_id=venue_id,
                )
            return _wait("improved passive order remains working")
        if (
            price is None
            or venue_id is None
            or not _spread_allowed(observation, self.manifest)
            or not _price_within_manifest_limit(observation, self.manifest, price)
        ):
            return _wait("an uncrossed one-tick improvement is unavailable")
        self.last_submit_time_us = observation.simulation_time_us
        return _submit(
            _slice(self.manifest, observation.available_to_submit),
            RoutePolicy.DIRECT,
            RouteStyle.PASSIVE,
            "improve the observable best by one tick without crossing",
            direct_venue=venue_id,
            price_limit=price,
        )


class PassivePegAlgorithm(_TimedPassiveAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.latency_state.pending_route_count:
            return _wait("awaiting venue response")
        venue_id, price = _join_price(observation)
        if observation.working_orders:
            current = observation.working_orders[0]
            if self._near_deadline(observation):
                return _replace_market(observation, self.manifest, "deadline overrides passive peg")
            if current.price_ticks != price or current.venue_id != venue_id or self._timeout(observation):
                if (
                    price is None
                    or venue_id is None
                    or not _price_within_manifest_limit(
                        observation,
                        self.manifest,
                        price,
                    )
                ):
                    return _wait("peg refresh is absent or outside the price limit")
                self.last_submit_time_us = observation.simulation_time_us
                return _replace_passive(
                    observation,
                    self.manifest,
                    price,
                    "observable best moved or peg timeout expired",
                    venue_id=venue_id,
                )
            return _wait("passive peg remains at the observable best")
        if (
            price is None
            or venue_id is None
            or not _spread_allowed(observation, self.manifest)
            or not _price_within_manifest_limit(observation, self.manifest, price)
        ):
            return _wait("passive peg has no eligible displayed quote")
        self.last_submit_time_us = observation.simulation_time_us
        return _submit(
            _slice(self.manifest, observation.available_to_submit),
            RoutePolicy.DIRECT,
            RouteStyle.PASSIVE,
            "peg to the observable best on the selected venue",
            direct_venue=venue_id,
            price_limit=price,
        )


class TwapAlgorithm(ExecutionAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.available_to_submit <= 0 or observation.latency_state.pending_route_count:
            return _wait("TWAP child is working or in flight")
        elapsed = max(0, observation.simulation_time_us - observation.objective.start_time_us)
        duration = observation.objective.deadline_us - observation.objective.start_time_us
        slices = _int(self.manifest, "slice_count")
        completed_slices = min(slices, elapsed * slices // duration)
        desired = observation.objective.target_quantity * completed_slices // slices
        deficit = desired - observation.filled_quantity - observation.working_quantity
        if observation.simulation_time_us >= observation.objective.deadline_us - duration // slices:
            deficit = observation.available_to_submit
        if deficit <= 0:
            return _wait("TWAP schedule is not behind target")
        return _submit(
            _slice(self.manifest, min(deficit, observation.available_to_submit)),
            RoutePolicy.BEST_DISPLAYED_PRICE,
            RouteStyle.AGGRESSIVE,
            "submit the current equal-time TWAP schedule deficit",
            price_limit=_optional_int(self.manifest, "price_limit_ticks"),
        )


class VwapProfileAlgorithm(ExecutionAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.available_to_submit <= 0 or observation.latency_state.pending_route_count:
            return _wait("VWAP child is working or in flight")
        profile = _int_tuple(self.manifest, "volume_profile_bps")
        elapsed = max(0, observation.simulation_time_us - observation.objective.start_time_us)
        duration = observation.objective.deadline_us - observation.objective.start_time_us
        bucket = min(len(profile), elapsed * len(profile) // duration)
        desired_bps = sum(profile[:bucket])
        if bucket == len(profile) or observation.simulation_time_us >= observation.objective.deadline_us - duration // len(profile):
            desired_bps = 10_000
        desired = observation.objective.target_quantity * desired_bps // 10_000
        deficit = desired - observation.filled_quantity - observation.working_quantity
        if deficit <= 0:
            return _wait("VWAP profile is not behind target")
        return _submit(
            _slice(self.manifest, min(deficit, observation.available_to_submit)),
            RoutePolicy.LOWEST_EXPECTED_COST,
            RouteStyle.AGGRESSIVE,
            "submit the observable volume-profile schedule deficit",
            price_limit=_optional_int(self.manifest, "price_limit_ticks"),
        )


class PovAlgorithm(ExecutionAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.available_to_submit <= 0 or observation.latency_state.pending_route_count:
            return _wait("POV child is working or in flight")
        participation = _int(self.manifest, "participation_rate_bps")
        desired_total = (
            observation.observable_market_features.cumulative_observed_volume
            * participation
            // 10_000
        )
        desired = (
            desired_total
            - observation.filled_quantity
            - observation.working_quantity
        )
        if observation.simulation_time_us >= (
            observation.objective.deadline_us
            - _int(self.manifest, "deadline_buffer_us")
        ):
            desired = observation.available_to_submit
        elif desired < _int(self.manifest, "minimum_slice"):
            return _wait("observable cumulative volume does not support a minimum child")
        return _submit(
            _slice(self.manifest, min(desired, observation.available_to_submit)),
            RoutePolicy.LATENCY_AWARE,
            RouteStyle.AGGRESSIVE,
            "participate in only volume already observed by the client",
            price_limit=_optional_int(self.manifest, "price_limit_ticks"),
        )


class SweepAlgorithm(ExecutionAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.available_to_submit <= 0 or observation.latency_state.pending_route_count:
            return _wait("sweep child is working or in flight")
        return _submit(
            _slice(self.manifest, observation.available_to_submit),
            RoutePolicy.SWEEP,
            RouteStyle.AGGRESSIVE,
            "sweep displayed venues in observed price order",
            price_limit=_optional_int(self.manifest, "price_limit_ticks"),
            maximum_venues=_int(self.manifest, "maximum_venues"),
        )


class ImplementationShortfallAdaptiveAlgorithm(_TimedPassiveAlgorithm):
    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        if observation.latency_state.pending_route_count:
            return _wait("adaptive child is in flight")
        elapsed = max(0, observation.simulation_time_us - observation.objective.start_time_us)
        duration = observation.objective.deadline_us - observation.objective.start_time_us
        curve = _int_tuple(self.manifest, "deadline_curve_bps")
        bucket = min(len(curve) - 1, elapsed * (len(curve) - 1) // duration)
        target_bps = curve[bucket]
        desired = observation.objective.target_quantity * target_bps // 10_000
        schedule_shortfall = max(0, desired - observation.filled_quantity)
        urgency = _int(self.manifest, "urgency_bps") + elapsed * 10_000 // duration
        aggressive = (
            urgency >= 10_000
            or schedule_shortfall >= _int(self.manifest, "maximum_slice")
            or self._near_deadline(observation)
        )
        if observation.working_orders:
            if aggressive:
                return _replace_market(
                    observation,
                    self.manifest,
                    "schedule shortfall or deadline increased urgency",
                )
            if self._timeout(observation):
                return AlgorithmAction(
                    AlgorithmActionType.CANCEL,
                    "passive adaptive child timed out before urgency threshold",
                    target_order_ids=tuple(order.order_id for order in observation.working_orders),
                )
            return _wait("adaptive passive child remains within timeout")
        if observation.available_to_submit <= 0:
            return _wait("adaptive policy has no uncommitted quantity")
        quantity = _slice(
            self.manifest,
            max(_int(self.manifest, "minimum_slice"), schedule_shortfall),
        )
        quantity = min(quantity, observation.available_to_submit)
        if aggressive:
            return _submit(
                quantity,
                RoutePolicy.SWEEP,
                RouteStyle.AGGRESSIVE,
                "observable schedule shortfall selected aggressive execution",
                price_limit=_optional_int(self.manifest, "price_limit_ticks"),
                maximum_venues=3,
            )
        if not _spread_allowed(observation, self.manifest):
            return _wait("spread is too wide for passive adaptive entry")
        _venue_id, passive_price = _join_price(observation)
        if passive_price is None or not _price_within_manifest_limit(
            observation,
            self.manifest,
            passive_price,
        ):
            return _wait("observable passive price is absent or outside the price limit")
        self.last_submit_time_us = observation.simulation_time_us
        return _submit(
            quantity,
            RoutePolicy.PASSIVE_QUEUE,
            RouteStyle.PASSIVE,
            "observable schedule permits a passive child",
            price_limit=passive_price,
        )


class ManualReplayAlgorithm(ExecutionAlgorithm):
    def __init__(self, manifest: AlgorithmParameterManifest) -> None:
        super().__init__(manifest)
        self.index = 0

    def reset(self, objective: ExecutionObjective) -> None:
        super().reset(objective)
        self.index = 0

    def decide(self, observation):
        terminal = self._terminal(observation)
        if terminal is not None:
            return terminal
        raw = self.manifest.parameters.get("replay_actions")
        if not isinstance(raw, (list, tuple)):
            raise ValueError("manual replay manifest requires replay_actions")
        if self.index >= len(raw):
            return _wait("manual replay has no action at this observation")
        item = raw[self.index]
        if not isinstance(item, Mapping):
            raise ValueError("manual replay action must be an object")
        elapsed_time_us = observation.simulation_time_us - observation.objective.start_time_us
        if elapsed_time_us < int(item["elapsed_time_us"]):
            return _wait("next recorded player action has not occurred yet")
        self.index += 1
        action = AlgorithmActionType(str(item["action_type"]).upper())
        if action is AlgorithmActionType.CANCEL:
            return AlgorithmAction(action, "replayed player cancellation")
        if action is AlgorithmActionType.FINISH:
            return _finish("replayed player finish")
        if action is AlgorithmActionType.WAIT:
            return _wait("replayed player wait")
        available = (
            observation.remaining_quantity
            if action is AlgorithmActionType.REPLACE
            else observation.available_to_submit
        )
        quantity = min(int(item["quantity"]), available)
        if quantity <= 0:
            return _wait("replayed player submission has no uncommitted quantity")
        return _submit(
            quantity,
            RoutePolicy(str(item.get("route_policy", "SWEEP")).upper()),
            RouteStyle(str(item.get("route_style", "AGGRESSIVE")).upper()),
            "replayed player submission",
            direct_venue=item.get("direct_venue_id"),
            price_limit=item.get("limit_price_ticks"),
            maximum_venues=int(item.get("maximum_venues", 3)),
            action_type=action,
        )


def create_algorithm(manifest: AlgorithmParameterManifest) -> ExecutionAlgorithm:
    classes = {
        AlgorithmName.IMMEDIATE_MARKET: ImmediateMarketAlgorithm,
        AlgorithmName.JOIN_BEST: JoinBestAlgorithm,
        AlgorithmName.IMPROVE_ONE_TICK: ImproveOneTickAlgorithm,
        AlgorithmName.PASSIVE_PEG: PassivePegAlgorithm,
        AlgorithmName.TWAP: TwapAlgorithm,
        AlgorithmName.VWAP_PROFILE: VwapProfileAlgorithm,
        AlgorithmName.POV: PovAlgorithm,
        AlgorithmName.SWEEP: SweepAlgorithm,
        AlgorithmName.IMPLEMENTATION_SHORTFALL_ADAPTIVE: ImplementationShortfallAdaptiveAlgorithm,
        AlgorithmName.MANUAL_REPLAY: ManualReplayAlgorithm,
    }
    return classes[manifest.algorithm](manifest)


def default_algorithm_manifest(name: AlgorithmName | str) -> AlgorithmParameterManifest:
    algorithm = name if isinstance(name, AlgorithmName) else AlgorithmName.parse(name)
    common: dict[str, object] = {
        "maximum_slice": 200,
        "minimum_slice": 25,
        "price_limit_ticks": None,
    }
    specific: dict[AlgorithmName, dict[str, object]] = {
        AlgorithmName.IMMEDIATE_MARKET: {},
        AlgorithmName.JOIN_BEST: {
            "maximum_spread_ticks": 4,
            "passive_timeout_us": 750_000,
        },
        AlgorithmName.IMPROVE_ONE_TICK: {
            "maximum_spread_ticks": 4,
            "passive_timeout_us": 750_000,
        },
        AlgorithmName.PASSIVE_PEG: {
            "maximum_spread_ticks": 5,
            "passive_timeout_us": 500_000,
        },
        AlgorithmName.TWAP: {"slice_count": 5},
        AlgorithmName.VWAP_PROFILE: {
            "volume_profile_bps": [1_500, 2_000, 3_000, 2_000, 1_500],
        },
        AlgorithmName.POV: {
            "deadline_buffer_us": 500_000,
            "participation_rate_bps": 2_500,
        },
        AlgorithmName.SWEEP: {"maximum_venues": 3},
        AlgorithmName.IMPLEMENTATION_SHORTFALL_ADAPTIVE: {
            "deadline_curve_bps": [0, 1_000, 3_000, 6_000, 8_500, 10_000],
            "maximum_spread_ticks": 5,
            "passive_timeout_us": 500_000,
            "urgency_bps": 2_000,
        },
        AlgorithmName.MANUAL_REPLAY: {},
    }
    if algorithm is AlgorithmName.MANUAL_REPLAY:
        actions: list[dict[str, object]] = [
            {
                "action_type": "SUBMIT",
                "elapsed_time_us": 1_000_000,
                "maximum_venues": 3,
                "quantity": 500,
                "route_policy": "SWEEP",
                "route_style": "AGGRESSIVE",
            }
        ]
        return AlgorithmParameterManifest(
            algorithm,
            {
                "replay_actions": actions,
                "replay_provenance": {
                    "source_sha256": canonical_sha256(actions),
                    "source_type": "BUILTIN_DEMONSTRATION_ACTION_SCHEDULE",
                    "source_verification": "BUILTIN_CANONICAL",
                    "translation_version": 1,
                },
            },
        )
    parameters = specific[algorithm]
    parameters = {**common, **parameters}
    return AlgorithmParameterManifest(algorithm, parameters)


def _submit(
    quantity: int,
    policy: RoutePolicy,
    style: RouteStyle,
    reason: str,
    *,
    direct_venue: str | None = None,
    price_limit: int | None = None,
    maximum_venues: int = 1,
    action_type: AlgorithmActionType = AlgorithmActionType.SUBMIT,
) -> AlgorithmAction:
    return AlgorithmAction(
        action_type,
        reason,
        quantity,
        policy,
        style,
        direct_venue,
        price_limit,
        maximum_venues,
    )


def _replace_market(
    observation: AlgorithmObservation,
    manifest: AlgorithmParameterManifest,
    reason: str,
) -> AlgorithmAction:
    return _submit(
        min(_int(manifest, "maximum_slice"), observation.remaining_quantity),
        RoutePolicy.SWEEP,
        RouteStyle.AGGRESSIVE,
        reason,
        price_limit=_optional_int(manifest, "price_limit_ticks"),
        maximum_venues=3,
        action_type=AlgorithmActionType.REPLACE,
    )


def _replace_passive(
    observation: AlgorithmObservation,
    manifest: AlgorithmParameterManifest,
    price: int | None,
    reason: str,
    *,
    venue_id: str | None = None,
) -> AlgorithmAction:
    policy = RoutePolicy.DIRECT if venue_id is not None else RoutePolicy.PASSIVE_QUEUE
    return _submit(
        min(_int(manifest, "maximum_slice"), observation.remaining_quantity),
        policy,
        RouteStyle.PASSIVE,
        reason,
        direct_venue=venue_id,
        price_limit=price,
        action_type=AlgorithmActionType.REPLACE,
    )


def _wait(reason: str) -> AlgorithmAction:
    return AlgorithmAction(AlgorithmActionType.WAIT, reason)


def _finish(reason: str) -> AlgorithmAction:
    return AlgorithmAction(AlgorithmActionType.FINISH, reason)


def _slice(manifest: AlgorithmParameterManifest, desired: int) -> int:
    minimum = _int(manifest, "minimum_slice")
    maximum = _int(manifest, "maximum_slice")
    if desired <= 0:
        raise ValueError("algorithm slice demand must be positive")
    if desired < minimum:
        return desired
    return min(maximum, max(minimum, desired))


def _spread_allowed(
    observation: AlgorithmObservation,
    manifest: AlgorithmParameterManifest,
) -> bool:
    spread = observation.observable_market_features.spread_ticks
    return spread is not None and spread <= _int(manifest, "maximum_spread_ticks")


def _price_within_manifest_limit(
    observation: AlgorithmObservation,
    manifest: AlgorithmParameterManifest,
    price_ticks: int,
) -> bool:
    limit = _optional_int(manifest, "price_limit_ticks")
    if limit is None:
        return True
    if observation.objective.side is Side.BUY:
        return price_ticks <= limit
    return price_ticks >= limit


def _join_price(observation: AlgorithmObservation) -> tuple[str | None, int | None]:
    side = observation.objective.side
    price = (
        observation.observable_market_features.best_bid_ticks
        if side is Side.BUY
        else observation.observable_market_features.best_ask_ticks
    )
    candidates = (
        observation.consolidated_feed.best_bid_venues
        if side is Side.BUY
        else observation.consolidated_feed.best_ask_venues
    )
    return (None if not candidates else candidates[0]), price


def _improved_price(observation: AlgorithmObservation) -> tuple[str | None, int | None]:
    venue, join = _join_price(observation)
    features = observation.observable_market_features
    if venue is None or join is None or features.spread_ticks is None:
        return None, None
    if features.spread_ticks <= 1:
        return venue, join
    return venue, join + (1 if observation.objective.side is Side.BUY else -1)


def _int(manifest: AlgorithmParameterManifest, key: str) -> int:
    value = manifest.parameters.get(key)
    if type(value) is not int:
        raise ValueError(f"algorithm parameter {key} must be an integer")
    return value


def _optional_int(manifest: AlgorithmParameterManifest, key: str) -> int | None:
    value = manifest.parameters.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"algorithm parameter {key} must be an integer or null")
    return value


def _int_tuple(manifest: AlgorithmParameterManifest, key: str) -> tuple[int, ...]:
    value = manifest.parameters.get(key)
    if not isinstance(value, (list, tuple)) or any(
        type(item) is not int for item in value
    ):
        raise ValueError(f"algorithm parameter {key} must be an integer array")
    return tuple(value)
