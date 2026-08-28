"""Forked deterministic execution benchmark runtime and metric aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kirby2.exchange.models import Side
from kirby2.latency import LatencyComponent
from kirby2.multivenue import (
    MarketCoordinator,
    MultiVenueCommand,
    MultiVenueRecording,
    RouteStyle,
    RoutingRequest,
    VenueOrderStatus,
    recording_json_round_trip,
    replay_multivenue_recording,
)
from kirby2.multivenue.models import canonical_sha256
from kirby2.observability import ObservableEventType

from .models import (
    AlgorithmAction,
    AlgorithmActionType,
    AlgorithmDecision,
    AlgorithmName,
    AlgorithmObservation,
    AlgorithmParameterManifest,
    BenchmarkManifest,
    BenchmarkRunResult,
    ClientFill,
    ClientLatencyState,
    ClientVenueState,
    ClientWorkingOrder,
    ExecutionBenchmarkMetrics,
    ExecutionBenchmarkResult,
    ExecutionObjective,
    ObservableMarketFeatures,
    RiskLimits,
)
from .policies import create_algorithm
from .scenarios import BackgroundMarketEvent, get_benchmark_scenario
from .store import AlgorithmRunArtifacts, AlgorithmRunStore


CLIENT_TRACKER_CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExecutionCellResult:
    """One non-persisted algorithm run on a deterministic benchmark cell."""

    scenario_name: str
    seed: int
    manifest: AlgorithmParameterManifest
    objective: ExecutionObjective
    fork_state_sha256: str
    background_path_sha256: str
    control_final_state_sha256: str
    decisions: tuple[AlgorithmDecision, ...]
    client_fills: tuple[ClientFill, ...]
    recording: MultiVenueRecording
    metrics: ExecutionBenchmarkMetrics
    observe_only: bool

    @property
    def final_signed_position(self) -> int:
        return sum(fill.side.sign * fill.quantity for fill in self.client_fills)

    def as_dict(self) -> dict[str, object]:
        return {
            "background_path_sha256": self.background_path_sha256,
            "client_fills": [item.as_dict() for item in self.client_fills],
            "control_final_state_sha256": self.control_final_state_sha256,
            "decisions": [item.as_dict() for item in self.decisions],
            "final_signed_position": self.final_signed_position,
            "fork_state_sha256": self.fork_state_sha256,
            "manifest": self.manifest.as_dict(),
            "metrics": self.metrics.as_dict(),
            "native_recording": self.recording.as_dict(),
            "objective": self.objective.as_dict(),
            "observe_only": self.observe_only,
            "scenario_name": self.scenario_name,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class _BackgroundControl:
    final_midpoint_x2: int
    fork_state_sha256: str
    final_state_sha256: str


class _RecordedCoordinator:
    def __init__(self, scenario, seed: int) -> None:
        self.coordinator = MarketCoordinator(
            scenario.venue_configs,
            seed=seed,
            depth_subscriptions=frozenset(
                config.venue_id for config in scenario.venue_configs
            ),
        )
        self.commands: list[MultiVenueCommand] = []
        self.route_ids: list[str] = []
        self.cancelled_order_count = 0
        for venue_id, request in scenario.initial_orders():
            self.coordinator.add_resting_order(venue_id, request)
            self._record(0, "ADD", {"request": request.as_dict(), "venue_id": venue_id})

    def advance(self, time_us: int) -> None:
        self.coordinator.advance_to(time_us)
        self._record(time_us, "ADVANCE", {})

    def background(self, event: BackgroundMarketEvent) -> None:
        self.coordinator.advance_to(event.simulation_time_us)
        self.coordinator.execute_simulated_market(
            event.venue_id,
            event.order_id,
            event.side,
            event.quantity,
        )
        self._record(
            event.simulation_time_us,
            "SIM_MARKET",
            {
                "order_id": event.order_id,
                "quantity": event.quantity,
                "side": event.side.value,
                "venue_id": event.venue_id,
            },
        )

    def route(self, request: RoutingRequest) -> str:
        time_us = self.coordinator.clock.current_time_us
        route_id = self.coordinator.submit_route(request)
        self.route_ids.append(route_id)
        self._record(time_us, "ROUTE", {"request": request.as_dict()})
        return route_id

    def cancel_all(self) -> None:
        time_us = self.coordinator.clock.current_time_us
        responses = self.coordinator.cancel_all()
        self.cancelled_order_count += sum(
            response.status is VenueOrderStatus.CANCELLED for response in responses
        )
        self._record(time_us, "CANCEL_ALL", {})

    def complete(self) -> MultiVenueRecording:
        time_us = self.coordinator.clock.current_time_us
        self.coordinator.complete_session()
        self._record(time_us, "COMPLETE", {})
        return recording_json_round_trip(
            MultiVenueRecording.capture(
                self.coordinator,
                tuple(self.commands),
                tuple(self.route_ids),
            )
        )

    def _record(self, time_us: int, command_type: str, parameters: dict[str, object]) -> None:
        self.commands.append(
            MultiVenueCommand(
                len(self.commands) + 1,
                time_us,
                command_type,
                parameters,
            )
        )


class _ClientTracker:
    def __init__(self) -> None:
        self.fills: list[ClientFill] = []
        self._seen_events: set[tuple[str, int]] = set()
        self._order_sides: dict[str, Side] = {}
        self._order_decision_midpoints_x2: dict[str, int | None] = {}
        self._route_send_time: dict[str, int] = {}
        self._route_quantities: dict[str, int] = {}
        self._route_decision_midpoints_x2: dict[str, int | None] = {}
        self._route_ids: list[str] = []
        self._route_order_allocations: dict[str, dict[str, int]] = {}
        self._route_order_fills: dict[str, dict[str, int]] = {}
        self._last_observation_time_us = -1
        self._last_midpoint_x2: int | None = None
        self._sequence = 0

    def sent_route(
        self,
        route_id: str,
        time_us: int,
        observed_midpoint_x2: int | None,
        quantity: int,
    ) -> None:
        if (
            type(route_id) is not str
            or not route_id
            or route_id in self._route_send_time
            or type(time_us) is not int
            or time_us < 0
            or type(quantity) is not int
            or quantity <= 0
            or (
                observed_midpoint_x2 is not None
                and (
                    type(observed_midpoint_x2) is not int
                    or observed_midpoint_x2 <= 0
                )
            )
        ):
            raise ValueError("client tracker route state is invalid")
        self._route_ids.append(route_id)
        self._route_send_time[route_id] = time_us
        self._route_quantities[route_id] = quantity
        self._route_decision_midpoints_x2[route_id] = observed_midpoint_x2

    @property
    def pending_route_quantity(self) -> int:
        return sum(
            max(
                0,
                self._route_quantities[route_id]
                - self._observed_route_quantity(route_id),
            )
            for route_id in self._route_ids
        )

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def last_observation_time_us(self) -> int:
        return self._last_observation_time_us

    @property
    def last_midpoint_x2(self) -> int | None:
        return self._last_midpoint_x2

    def checkpoint_state(self) -> dict[str, object]:
        self.assert_invariants()
        return {
            "fills": [fill.as_dict() for fill in self.fills],
            "last_midpoint_x2": self._last_midpoint_x2,
            "last_observation_time_us": self._last_observation_time_us,
            "order_decision_midpoints_x2": dict(
                sorted(self._order_decision_midpoints_x2.items())
            ),
            "order_sides": {
                order_id: side.value
                for order_id, side in sorted(self._order_sides.items())
            },
            "route_decision_midpoints_x2": dict(
                sorted(self._route_decision_midpoints_x2.items())
            ),
            "route_ids": list(self._route_ids),
            "route_order_allocations": {
                route_id: dict(sorted(rows.items()))
                for route_id, rows in sorted(self._route_order_allocations.items())
            },
            "route_order_fills": {
                route_id: dict(sorted(rows.items()))
                for route_id, rows in sorted(self._route_order_fills.items())
            },
            "route_quantities": dict(sorted(self._route_quantities.items())),
            "route_send_time_us": dict(sorted(self._route_send_time.items())),
            "schema_version": CLIENT_TRACKER_CHECKPOINT_SCHEMA_VERSION,
            "seen_events": [
                {"sequence": sequence, "venue_id": venue_id}
                for venue_id, sequence in sorted(self._seen_events)
            ],
            "sequence": self._sequence,
        }

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> _ClientTracker:
        _require_exact_tracker_fields(
            payload,
            {
                "fills",
                "last_midpoint_x2",
                "last_observation_time_us",
                "order_decision_midpoints_x2",
                "order_sides",
                "route_decision_midpoints_x2",
                "route_ids",
                "route_order_allocations",
                "route_order_fills",
                "route_quantities",
                "route_send_time_us",
                "schema_version",
                "seen_events",
                "sequence",
            },
            "client tracker checkpoint",
        )
        if payload["schema_version"] != CLIENT_TRACKER_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported client tracker checkpoint schema")
        tracker = cls()
        raw_fills = _tracker_array(payload["fills"], "client tracker fills")
        tracker.fills = [
            ClientFill.from_dict(_tracker_object(row, "client tracker fill"))
            for row in raw_fills
        ]
        raw_seen = _tracker_array(payload["seen_events"], "client tracker seen events")
        seen: set[tuple[str, int]] = set()
        for raw in raw_seen:
            row = _tracker_object(raw, "client tracker seen event")
            _require_exact_tracker_fields(
                row,
                {"sequence", "venue_id"},
                "client tracker seen event",
            )
            seen.add(
                (
                    _tracker_string(row["venue_id"], "client tracker event venue"),
                    _tracker_int(
                        row["sequence"],
                        "client tracker event sequence",
                        minimum=1,
                    ),
                )
            )
        if len(seen) != len(raw_seen):
            raise ValueError("client tracker seen events are duplicated")
        tracker._seen_events = seen
        tracker._order_sides = {
            _tracker_string(key, "client tracker order ID"): Side(
                _tracker_string(value, "client tracker order side")
            )
            for key, value in _tracker_object(
                payload["order_sides"], "client tracker order sides"
            ).items()
        }
        tracker._order_decision_midpoints_x2 = _tracker_optional_int_map(
            payload["order_decision_midpoints_x2"],
            "client tracker order decision midpoints",
            minimum=1,
        )
        tracker._route_send_time = _tracker_int_map(
            payload["route_send_time_us"],
            "client tracker route send times",
            minimum=0,
        )
        tracker._route_quantities = _tracker_int_map(
            payload["route_quantities"],
            "client tracker route quantities",
            minimum=1,
        )
        tracker._route_decision_midpoints_x2 = _tracker_optional_int_map(
            payload["route_decision_midpoints_x2"],
            "client tracker route decision midpoints",
            minimum=1,
        )
        tracker._route_ids = [
            _tracker_string(value, "client tracker route ID")
            for value in _tracker_array(payload["route_ids"], "client tracker route IDs")
        ]
        tracker._route_order_allocations = _tracker_nested_int_map(
            payload["route_order_allocations"],
            "client tracker route allocations",
            minimum=1,
        )
        tracker._route_order_fills = _tracker_nested_int_map(
            payload["route_order_fills"],
            "client tracker route fills",
            minimum=1,
        )
        tracker._last_observation_time_us = _tracker_int(
            payload["last_observation_time_us"],
            "client tracker last observation time",
            minimum=-1,
        )
        tracker._last_midpoint_x2 = _tracker_optional_int(
            payload["last_midpoint_x2"],
            "client tracker last midpoint",
            minimum=1,
        )
        tracker._sequence = _tracker_int(
            payload["sequence"], "client tracker sequence", minimum=0
        )
        tracker.assert_invariants()
        if tracker.checkpoint_state() != dict(payload):
            raise ValueError("client tracker checkpoint is not a fixed point")
        return tracker

    def assert_invariants(self) -> None:
        if type(self._sequence) is not int or self._sequence < 0:
            raise RuntimeError("client tracker sequence is invalid")
        if type(self._last_observation_time_us) is not int or (
            self._last_observation_time_us < -1
        ):
            raise RuntimeError("client tracker observation time is invalid")
        if (self._sequence == 0) != (self._last_observation_time_us == -1):
            raise RuntimeError("client tracker sequence and observation time disagree")
        if self._last_midpoint_x2 is not None and (
            type(self._last_midpoint_x2) is not int or self._last_midpoint_x2 <= 0
        ):
            raise RuntimeError("client tracker midpoint is invalid")
        if self.fills != sorted(
            self.fills,
            key=lambda item: (
                item.received_time_us,
                item.venue_id,
                item.trade_id,
                item.order_id,
            ),
        ):
            raise RuntimeError("client tracker fills are not canonical")
        fill_keys = tuple(
            (fill.venue_id, fill.trade_id, fill.order_id) for fill in self.fills
        )
        if len(fill_keys) != len(set(fill_keys)):
            raise RuntimeError("client tracker fills are duplicated")
        if len(self._route_ids) != len(set(self._route_ids)) or any(
            type(route_id) is not str or not route_id for route_id in self._route_ids
        ):
            raise RuntimeError("client tracker route IDs are invalid")
        route_set = set(self._route_ids)
        if any(
            set(mapping) != route_set
            for mapping in (
                self._route_send_time,
                self._route_quantities,
                self._route_decision_midpoints_x2,
            )
        ):
            raise RuntimeError("client tracker route authority is incomplete")
        if set(self._route_order_allocations) - route_set or set(
            self._route_order_fills
        ) - route_set:
            raise RuntimeError("client tracker child rows cite an unknown route")
        if any(
            type(value) is not int or value < 0
            for value in self._route_send_time.values()
        ) or any(
            type(value) is not int or value <= 0
            for value in self._route_quantities.values()
        ):
            raise RuntimeError("client tracker route timing or quantity is invalid")
        if any(
            value is not None and (type(value) is not int or value <= 0)
            for value in self._route_decision_midpoints_x2.values()
        ):
            raise RuntimeError("client tracker route midpoint is invalid")
        allocated_order_ids: set[str] = set()
        for route_id in self._route_ids:
            allocations = self._route_order_allocations.get(route_id, {})
            fills = self._route_order_fills.get(route_id, {})
            if any(
                type(order_id) is not str
                or not order_id
                or type(quantity) is not int
                or quantity <= 0
                for rows in (allocations, fills)
                for order_id, quantity in rows.items()
            ):
                raise RuntimeError("client tracker child quantities are invalid")
            local_ids = set(allocations) | set(fills)
            if allocated_order_ids & local_ids:
                raise RuntimeError("client tracker child order belongs to multiple routes")
            allocated_order_ids.update(local_ids)
            if self._observed_route_quantity(route_id) > self._route_quantities[route_id]:
                raise RuntimeError("client tracker route quantity does not conserve")
        if any(
            type(order_id) is not str
            or not order_id
            or not isinstance(side, Side)
            for order_id, side in self._order_sides.items()
        ):
            raise RuntimeError("client tracker order-side map is invalid")
        if any(
            order_id not in self._order_sides
            for order_id in self._order_decision_midpoints_x2
        ) or any(fill.order_id not in self._order_sides for fill in self.fills):
            raise RuntimeError("client tracker order metadata is orphaned")
        if set(self._order_sides) != allocated_order_ids or set(
            self._order_decision_midpoints_x2
        ) != allocated_order_ids:
            raise RuntimeError("client tracker child-order metadata inventory differs")
        if any(
            value is not None and (type(value) is not int or value <= 0)
            for value in self._order_decision_midpoints_x2.values()
        ):
            raise RuntimeError("client tracker order midpoint is invalid")

    def observation(
        self,
        coordinator: MarketCoordinator,
        objective: ExecutionObjective,
        volume_profile_bps: tuple[int, ...],
        risk_limits,
    ) -> AlgorithmObservation:
        feed = coordinator.consolidated_feed()
        midpoint_x2 = _midpoint_x2(feed)
        self._refresh_orders_and_fills(coordinator, objective)
        working = self._working_orders(coordinator)
        filled = sum(item.quantity for item in self.fills if item.side is objective.side)
        pending = [
            route_id
            for route_id in self._route_ids
            if self._observed_route_quantity(route_id)
            < self._route_quantities[route_id]
        ]
        current_time = coordinator.clock.current_time_us
        interval_volume = sum(
            trade.quantity
            for trade in feed.trades
            if self._last_observation_time_us < trade.received_time_us <= current_time
        )
        cumulative_volume = sum(trade.quantity for trade in feed.trades)
        midpoint_change = (
            0
            if midpoint_x2 is None or self._last_midpoint_x2 is None
            else midpoint_x2 - self._last_midpoint_x2
        )
        quotes = feed.quotes
        features = ObservableMarketFeatures(
            feed.best_bid_ticks,
            feed.best_ask_ticks,
            midpoint_x2,
            (
                None
                if feed.best_bid_ticks is None or feed.best_ask_ticks is None
                else feed.best_ask_ticks - feed.best_bid_ticks
            ),
            sum(quote.best_bid_quantity for quote in quotes),
            sum(quote.best_ask_quantity for quote in quotes),
            interval_volume,
            cumulative_volume,
            midpoint_change,
            volume_profile_bps,
        )
        latency = ClientLatencyState(
            len(pending),
            max(
                (
                    current_time - self._route_send_time[route_id]
                    for route_id in pending
                ),
                default=0,
            ),
            max((quote.quote_age_us for quote in quotes), default=0),
            tuple(
                (quote.venue_id, quote.expected_routing_latency_us)
                for quote in quotes
            ),
        )
        venues = tuple(
            ClientVenueState(
                quote.venue_id,
                quote.best_bid_ticks,
                quote.best_bid_quantity,
                quote.best_ask_ticks,
                quote.best_ask_quantity,
                quote.quote_age_us,
                quote.session_state.value,
                quote.expected_fill_probability_bps,
                quote.taker_fee_micros_per_share,
                quote.maker_rebate_micros_per_share,
            )
            for quote in quotes
        )
        self._sequence += 1
        observation = AlgorithmObservation(
            self._sequence,
            current_time,
            objective,
            max(0, objective.target_quantity - filled),
            features,
            working,
            tuple(self.fills),
            latency,
            venues,
            risk_limits,
            feed,
        )
        self._last_observation_time_us = current_time
        self._last_midpoint_x2 = midpoint_x2
        return observation

    def refresh_after_completion(
        self,
        coordinator: MarketCoordinator,
        objective: ExecutionObjective,
    ) -> None:
        self._refresh_orders_and_fills(coordinator, objective)

    def refresh_client_state(
        self,
        coordinator: MarketCoordinator,
        objective: ExecutionObjective,
    ) -> None:
        """Consume currently delivered own-order/fill state without a policy read."""

        self._refresh_orders_and_fills(coordinator, objective)

    def current_working_orders(
        self,
        coordinator: MarketCoordinator,
    ) -> tuple[ClientWorkingOrder, ...]:
        """Return the currently delivered client-visible working inventory."""

        return self._working_orders(coordinator)

    def _refresh_orders_and_fills(
        self,
        coordinator: MarketCoordinator,
        objective: ExecutionObjective,
    ) -> None:
        for venue_id, venue in sorted(coordinator.venues.items()):
            feed = venue.observable_feed()
            for own in feed.own_orders:
                self._order_sides[own.order_id] = own.side
                route_id = self._route_id_for_order(own.order_id)
                if route_id is not None:
                    self._route_order_allocations.setdefault(route_id, {})[
                        own.order_id
                    ] = own.original_quantity
                    self._order_decision_midpoints_x2[own.order_id] = (
                        self._route_decision_midpoints_x2[route_id]
                    )
            for event in feed.events:
                key = (venue_id, event.sequence)
                if key in self._seen_events or event.event_type is not ObservableEventType.OWN_ORDER_FILL:
                    continue
                self._seen_events.add(key)
                raw_own = event.data.get("own_order")
                own_order_id = (
                    str(raw_own["order_id"])
                    if isinstance(raw_own, Mapping)
                    else str(event.data["order_id"])
                )
                route_id = self._route_id_for_order(own_order_id)
                if route_id is not None:
                    route_fills = self._route_order_fills.setdefault(route_id, {})
                    route_fills[own_order_id] = (
                        route_fills.get(own_order_id, 0)
                        + int(event.data["fill_quantity"])
                    )
                    self._order_decision_midpoints_x2[own_order_id] = (
                        self._route_decision_midpoints_x2[route_id]
                    )
                side = self._order_sides.get(own_order_id, objective.side)
                self._order_sides[own_order_id] = side
                self.fills.append(
                    ClientFill(
                        venue_id,
                        str(event.data["trade_id"]),
                        own_order_id,
                        side,
                        int(event.data["price_x2"]),
                        int(event.data["fill_quantity"]),
                        event.received_time_us,
                        self._order_decision_midpoints_x2.get(own_order_id),
                    )
                )
        self.fills.sort(
            key=lambda item: (
                item.received_time_us,
                item.venue_id,
                item.trade_id,
                item.order_id,
            )
        )

    def _route_id_for_order(self, order_id: str) -> str | None:
        return next(
            (
                route_id
                for route_id in self._route_ids
                if f"-{route_id}-" in order_id
            ),
            None,
        )

    def _observed_route_quantity(self, route_id: str) -> int:
        allocations = self._route_order_allocations.get(route_id, {})
        fills = self._route_order_fills.get(route_id, {})
        return sum(
            max(allocations.get(order_id, 0), fills.get(order_id, 0))
            for order_id in allocations.keys() | fills.keys()
        )

    def _working_orders(self, coordinator: MarketCoordinator) -> tuple[ClientWorkingOrder, ...]:
        result: list[ClientWorkingOrder] = []
        for venue_id, venue in sorted(coordinator.venues.items()):
            for own in venue.observable_feed().own_orders:
                if own.remaining_quantity <= 0 or own.status not in {
                    "WORKING",
                    "PARTIALLY_FILLED",
                }:
                    continue
                result.append(
                    ClientWorkingOrder(
                        venue_id,
                        own.order_id,
                        own.side,
                        own.price_ticks,
                        own.original_quantity,
                        own.filled_quantity,
                        own.remaining_quantity,
                        own.status,
                    )
                )
        return tuple(result)


def run_execution_benchmark(
    manifest: BenchmarkManifest,
    *,
    store_root: Path,
) -> ExecutionBenchmarkResult:
    store = AlgorithmRunStore(store_root)
    run_results: list[BenchmarkRunResult] = []
    for scenario_name in manifest.scenario_names:
        for seed in manifest.seeds:
            cell_forks: set[str] = set()
            for algorithm_manifest in manifest.algorithm_manifests:
                completed = run_execution_cell(
                    scenario_name=scenario_name,
                    seed=seed,
                    algorithm_manifest=algorithm_manifest,
                    side=manifest.side,
                    target_quantity=manifest.quantity,
                    duration_us=manifest.duration_us,
                    decision_interval_us=manifest.decision_interval_us,
                    risk_limits=manifest.risk_limits,
                )
                cell_forks.add(completed.fork_state_sha256)
                immutable = store.record(
                    AlgorithmRunArtifacts(
                        manifest.experiment_id,
                        scenario_name,
                        seed,
                        algorithm_manifest,
                        completed.fork_state_sha256,
                        completed.background_path_sha256,
                        completed.decisions,
                        completed.recording,
                        completed.metrics,
                    )
                )
                verification = store.verify_run(immutable.run_id)
                if not verification.passed:
                    raise RuntimeError("immutable algorithm run failed verification")
                run_results.append(
                    BenchmarkRunResult(
                        immutable.run_id,
                        scenario_name,
                        seed,
                        algorithm_manifest.algorithm,
                        algorithm_manifest.sha256(),
                        completed.fork_state_sha256,
                        completed.background_path_sha256,
                        completed.recording.sha256(),
                        canonical_sha256(
                            [decision.as_dict() for decision in completed.decisions]
                        ),
                        True,
                        completed.metrics,
                    )
                )
            if len(cell_forks) != 1:
                raise RuntimeError("forked algorithm runs did not share one starting state")
    captured = tuple(run_results)
    aggregate = _aggregate(manifest, captured)
    result_sha256 = canonical_sha256(
        {
            "aggregate_by_algorithm": list(aggregate),
            "manifest": manifest.as_dict(),
            "runs": [run.as_dict() for run in captured],
            "winner": "NOT_DECLARED",
        }
    )
    return ExecutionBenchmarkResult(
        manifest,
        captured,
        aggregate,
        str(store.root.resolve()),
        result_sha256,
    )


def run_execution_cell(
    *,
    scenario_name: str,
    seed: int,
    algorithm_manifest: AlgorithmParameterManifest,
    side: Side,
    target_quantity: int,
    duration_us: int,
    decision_interval_us: int,
    risk_limits: RiskLimits,
    observe_only: bool = False,
) -> ExecutionCellResult:
    """Run one production benchmark cell without writing immutable artifacts."""

    if type(seed) is not int or seed < 0:
        raise ValueError("execution-cell seed must be a nonnegative integer")
    if not isinstance(algorithm_manifest, AlgorithmParameterManifest):
        raise TypeError("execution cell requires an algorithm manifest")
    if not isinstance(side, Side):
        raise TypeError("execution-cell side must use Side")
    if type(target_quantity) is not int or target_quantity <= 0:
        raise ValueError("execution-cell target quantity must be positive")
    if type(observe_only) is not bool:
        raise TypeError("execution-cell observe-only flag must be boolean")
    if not isinstance(risk_limits, RiskLimits):
        raise TypeError("execution cell requires canonical risk limits")
    scenario = get_benchmark_scenario(
        scenario_name,
        duration_us=duration_us,
        decision_interval_us=decision_interval_us,
    )
    _assert_timing_supports_synchronous_cancels(
        decision_interval_us,
        (algorithm_manifest,),
        scenario,
    )
    control = _run_background_control(scenario, seed)
    completed = _run_algorithm(
        scenario,
        seed,
        algorithm_manifest,
        side=side,
        target_quantity=target_quantity,
        risk_limits=risk_limits,
        control=control,
        observe_only=observe_only,
    )
    if completed.fork_state_sha256 != control.fork_state_sha256:
        raise RuntimeError("algorithm did not start from the control fork state")
    if observe_only and (
        completed.recording.expected_state_sha256 != control.final_state_sha256
    ):
        raise RuntimeError("observe-only cell diverged from its background control")
    return completed


def _run_background_control(scenario, seed: int) -> _BackgroundControl:
    recorder = _RecordedCoordinator(scenario, seed)
    recorder.advance(scenario.start_time_us)
    fork = recorder.coordinator.state_sha256()
    for event in scenario.background_events(seed):
        recorder.background(event)
    if recorder.coordinator.clock.current_time_us < scenario.deadline_us:
        recorder.advance(scenario.deadline_us)
    recording = recorder.complete()
    replay = replay_multivenue_recording(recording)
    if not replay.passed:
        raise RuntimeError("background control recording failed exact replay")
    midpoint = _midpoint_x2(recorder.coordinator.consolidated_feed())
    if midpoint is None:
        raise RuntimeError("background control ended without a two-sided market")
    return _BackgroundControl(
        midpoint,
        fork,
        recorder.coordinator.state_sha256(),
    )


def _assert_timing_supports_synchronous_cancels(
    decision_interval_us: int,
    algorithm_manifests: tuple[AlgorithmParameterManifest, ...],
    scenario,
) -> None:
    cancel_capable = {
        AlgorithmName.IMPLEMENTATION_SHORTFALL_ADAPTIVE,
        AlgorithmName.IMPROVE_ONE_TICK,
        AlgorithmName.JOIN_BEST,
        AlgorithmName.PASSIVE_PEG,
    }
    requires_cancel = any(
        item.algorithm in cancel_capable
        or (
            item.algorithm is AlgorithmName.MANUAL_REPLAY
            and _manual_manifest_can_cancel(item)
        )
        for item in algorithm_manifests
    )
    if not requires_cancel:
        return
    components = (
        LatencyComponent.CLIENT_ROUTING,
        LatencyComponent.UPLINK,
        LatencyComponent.GATEWAY,
        LatencyComponent.VENUE_PROCESSING,
    )
    worst_case_us = sum(
        sum(
            config.latency_profile.distribution(component).upper_us
            for component in components
        )
        for config in scenario.venue_configs
    )
    if decision_interval_us < worst_case_us:
        raise ValueError(
            "decision interval is shorter than the bounded synchronous cancel-all "
            f"latency ({decision_interval_us} < {worst_case_us} microseconds)"
        )


def _manual_manifest_can_cancel(manifest: AlgorithmParameterManifest) -> bool:
    actions = manifest.parameters.get("replay_actions")
    return isinstance(actions, (list, tuple)) and any(
        isinstance(action, Mapping)
        and action.get("action_type") in {"CANCEL", "REPLACE"}
        for action in actions
    )


def _run_algorithm(
    scenario,
    seed: int,
    algorithm_manifest: AlgorithmParameterManifest,
    *,
    side: Side,
    target_quantity: int,
    risk_limits: RiskLimits,
    control: _BackgroundControl,
    observe_only: bool,
) -> ExecutionCellResult:
    recorder = _RecordedCoordinator(scenario, seed)
    recorder.advance(scenario.start_time_us)
    fork_state = recorder.coordinator.state_sha256()
    starting_feed = recorder.coordinator.consolidated_feed()
    arrival_midpoint = _midpoint_x2(starting_feed)
    if arrival_midpoint is None:
        raise RuntimeError("execution benchmark fork lacks a two-sided market")
    objective = ExecutionObjective(
        side,
        target_quantity,
        scenario.start_time_us,
        scenario.deadline_us,
        arrival_midpoint,
    )
    algorithm = None if observe_only else create_algorithm(algorithm_manifest)
    if algorithm is not None:
        algorithm.reset(objective)
    tracker = _ClientTracker()
    decisions: list[AlgorithmDecision] = []
    events_by_time: dict[int, list[BackgroundMarketEvent]] = {}
    for event in scenario.background_events(seed):
        events_by_time.setdefault(event.simulation_time_us, []).append(event)
    finished = False
    for time_us in range(
        scenario.start_time_us,
        scenario.deadline_us + 1,
        scenario.decision_interval_us,
    ):
        if time_us > recorder.coordinator.clock.current_time_us:
            recorder.advance(time_us)
        for event in events_by_time.get(time_us, []):
            recorder.background(event)
        observation = tracker.observation(
            recorder.coordinator,
            objective,
            scenario.volume_profile_bps,
            risk_limits,
        )
        action = (
            _finish_action()
            if finished
            else _observe_only_action()
            if algorithm is None
            else algorithm.decide(observation)
        )
        accepted, rejection, route_id = _apply_action(
            recorder,
            tracker,
            observation,
            action,
        )
        decisions.append(
            AlgorithmDecision(
                len(decisions) + 1,
                observation.simulation_time_us,
                observation.sha256(),
                observation.as_dict(),
                algorithm_manifest.sha256(),
                action,
                accepted,
                rejection,
                route_id,
            )
        )
        if action.action_type is AlgorithmActionType.FINISH:
            finished = True
    recording = recorder.complete()
    replay = replay_multivenue_recording(recording)
    if not replay.passed:
        raise RuntimeError("algorithm coordinator recording failed exact replay")
    tracker.refresh_after_completion(recorder.coordinator, objective)
    metrics = _metrics(
        objective,
        tracker.fills,
        recorder,
        tuple(decisions),
        control.final_midpoint_x2,
    )
    return ExecutionCellResult(
        scenario_name=scenario.name,
        seed=seed,
        manifest=algorithm_manifest,
        objective=objective,
        fork_state_sha256=fork_state,
        background_path_sha256=scenario.background_sha256(seed),
        control_final_state_sha256=control.final_state_sha256,
        decisions=tuple(decisions),
        client_fills=tuple(tracker.fills),
        recording=recording,
        metrics=metrics,
        observe_only=observe_only,
    )


def _apply_action(
    recorder: _RecordedCoordinator,
    tracker: _ClientTracker,
    observation: AlgorithmObservation,
    action: AlgorithmAction,
    *,
    request_order_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    if request_order_id is not None and (
        type(request_order_id) is not str or not request_order_id
    ):
        raise TypeError("algorithm request order ID must be null or nonempty")
    rejection = _risk_rejection(observation, action)
    if rejection is not None:
        return False, rejection, None
    if action.action_type in {
        AlgorithmActionType.SUBMIT,
        AlgorithmActionType.REPLACE,
    }:
        client_available = (
            observation.remaining_quantity
            if action.action_type is AlgorithmActionType.REPLACE
            else observation.available_to_submit
        )
        client_available = max(
            0,
            client_available - tracker.pending_route_quantity,
        )
        if action.quantity > client_available:
            return False, "QUANTITY_EXCEEDS_CLIENT_KNOWN_CAPACITY", None
    if action.action_type in {AlgorithmActionType.WAIT, AlgorithmActionType.FINISH}:
        return True, None, None
    try:
        if action.action_type in {AlgorithmActionType.CANCEL, AlgorithmActionType.REPLACE}:
            recorder.cancel_all()
        if action.action_type is AlgorithmActionType.REPLACE:
            tracker.refresh_after_completion(
                recorder.coordinator,
                observation.objective,
            )
            refreshed_filled = sum(
                fill.quantity
                for fill in tracker.fills
                if fill.side is observation.objective.side
            )
            refreshed_working = sum(
                order.remaining_quantity
                for order in tracker._working_orders(recorder.coordinator)
                if order.side is observation.objective.side
            )
            refreshed_available = max(
                0,
                observation.objective.target_quantity
                - refreshed_filled
                - refreshed_working,
            )
            if action.quantity > refreshed_available:
                return (
                    False,
                    "REPLACE_QUANTITY_EXCEEDS_REFRESHED_OBJECTIVE",
                    None,
                )
        if action.action_type in {AlgorithmActionType.SUBMIT, AlgorithmActionType.REPLACE}:
            effective_limit = action.limit_price_ticks
            if (
                action.route_style is RouteStyle.AGGRESSIVE
                and effective_limit is None
            ):
                effective_limit = observation.risk_limits.price_limit_ticks
            request = RoutingRequest(
                order_id=(
                    f"ALG-{observation.sequence:04d}"
                    if request_order_id is None
                    else request_order_id
                ),
                side=observation.objective.side,
                quantity=action.quantity,
                policy=action.route_policy,
                style=action.route_style,
                direct_venue_id=action.direct_venue_id,
                limit_price_ticks=effective_limit,
                max_venues=action.maximum_venues,
            )
            route_id = recorder.route(request)
            tracker.sent_route(
                route_id,
                recorder.coordinator.clock.current_time_us,
                observation.observable_market_features.midpoint_x2,
                action.quantity,
            )
            return True, None, route_id
    except (TypeError, ValueError, RuntimeError) as error:
        return False, f"ACTION_REJECTED:{error}", None
    return True, None, None


# Public names used by the standalone WO31-E6 owner.  The benchmark keeps its
# original private implementation names so the inherited runtime path is unchanged.
ClientTrackerV1 = _ClientTracker


def apply_algorithm_action(
    recorder: object,
    tracker: ClientTrackerV1,
    observation: AlgorithmObservation,
    action: AlgorithmAction,
    *,
    request_order_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Apply one action through the existing venue/client stages."""

    return _apply_action(
        recorder,  # type: ignore[arg-type]
        tracker,
        observation,
        action,
        request_order_id=request_order_id,
    )


def _risk_rejection(
    observation: AlgorithmObservation,
    action: AlgorithmAction,
) -> str | None:
    limits = observation.risk_limits
    if action.action_type in {AlgorithmActionType.WAIT, AlgorithmActionType.FINISH}:
        return None
    if action.action_type is AlgorithmActionType.CANCEL:
        if not observation.working_orders:
            return "NO_OBSERVABLE_WORKING_ORDER"
        if action.target_order_ids and set(action.target_order_ids) != {
            order.order_id for order in observation.working_orders
        }:
            return "PARTIAL_OR_UNKNOWN_CANCEL_TARGETS_UNSUPPORTED"
        return None
    replacing = action.action_type is AlgorithmActionType.REPLACE
    if replacing and not observation.working_orders:
        return "NO_OBSERVABLE_WORKING_ORDER_TO_REPLACE"
    if replacing and action.target_order_ids and set(action.target_order_ids) != {
        order.order_id for order in observation.working_orders
    }:
        return "PARTIAL_OR_UNKNOWN_REPLACE_TARGETS_UNSUPPORTED"
    available = observation.remaining_quantity if replacing else observation.available_to_submit
    if action.quantity > available:
        return "QUANTITY_EXCEEDS_UNCOMMITTED_OBJECTIVE"
    if action.quantity > limits.maximum_child_quantity:
        return "MAXIMUM_CHILD_QUANTITY"
    post_working = action.quantity + (0 if replacing else observation.working_quantity)
    if post_working > limits.maximum_working_quantity:
        return "MAXIMUM_WORKING_QUANTITY"
    if observation.filled_quantity + post_working > limits.maximum_position:
        return "MAXIMUM_POSITION"
    spread = observation.observable_market_features.spread_ticks
    if spread is not None and spread > limits.maximum_spread_ticks:
        return "MAXIMUM_SPREAD"
    if action.direct_venue_id is not None and action.direct_venue_id not in {
        venue.venue_id for venue in observation.venue_state
    }:
        return "UNKNOWN_CLIENT_VENUE"
    if action.maximum_venues > len(observation.venue_state):
        return "MAXIMUM_VENUES_EXCEEDS_VISIBLE_VENUES"
    price_limit = limits.price_limit_ticks
    displayed = (
        observation.observable_market_features.best_ask_ticks
        if observation.objective.side is Side.BUY
        else observation.observable_market_features.best_bid_ticks
    )
    if price_limit is not None and displayed is not None:
        if observation.objective.side is Side.BUY and displayed > price_limit:
            return "OBSERVED_PRICE_LIMIT"
        if observation.objective.side is Side.SELL and displayed < price_limit:
            return "OBSERVED_PRICE_LIMIT"
    if price_limit is not None and action.limit_price_ticks is not None:
        if (
            observation.objective.side is Side.BUY
            and action.limit_price_ticks > price_limit
        ):
            return "ACTION_PRICE_LIMIT_EXCEEDS_RISK_LIMIT"
        if (
            observation.objective.side is Side.SELL
            and action.limit_price_ticks < price_limit
        ):
            return "ACTION_PRICE_LIMIT_EXCEEDS_RISK_LIMIT"
    return None


def _metrics(
    objective: ExecutionObjective,
    fills: list[ClientFill],
    recorder: _RecordedCoordinator,
    decisions: tuple[AlgorithmDecision, ...],
    control_midpoint_x2: int,
) -> ExecutionBenchmarkMetrics:
    relevant = [fill for fill in fills if fill.side is objective.side]
    completed = min(objective.target_quantity, sum(fill.quantity for fill in relevant))
    price_numerator = sum(fill.price_x2 * fill.quantity for fill in relevant)
    implementation_shortfall = sum(
        objective.side.sign
        * (fill.price_x2 - objective.arrival_midpoint_x2)
        * fill.quantity
        for fill in relevant
    )
    spread_paid = sum(
        objective.side.sign
        * (
            fill.price_x2
            - (
                objective.arrival_midpoint_x2
                if fill.observed_midpoint_x2_at_decision is None
                else fill.observed_midpoint_x2_at_decision
            )
        )
        * fill.quantity
        for fill in relevant
    )
    final_midpoint = _midpoint_x2(recorder.coordinator.consolidated_feed())
    if final_midpoint is None:
        final_midpoint = objective.arrival_midpoint_x2
    adverse = sum(
        objective.side.sign * (fill.price_x2 - final_midpoint) * fill.quantity
        for fill in relevant
    )
    fees = 0
    rebates = 0
    for route_id in recorder.route_ids:
        score = recorder.coordinator.score_route(route_id)
        fees += score.fees_micros
        rebates += score.rebates_micros
    completion_by_deadline = sum(
        fill.quantity for fill in relevant if fill.received_time_us <= objective.deadline_us
    )
    last_fill_time = max(
        (fill.received_time_us for fill in relevant),
        default=objective.deadline_us,
    )
    risk_rejections = sum(
        not decision.action_accepted
        and decision.rejection_reason is not None
        and not decision.rejection_reason.startswith("ACTION_REJECTED:")
        for decision in decisions
    )
    return ExecutionBenchmarkMetrics(
        target_quantity=objective.target_quantity,
        completed_quantity=completed,
        completion_bps=completed * 10_000 // objective.target_quantity,
        average_fill_price_numerator_x2=price_numerator,
        average_fill_price_denominator=sum(fill.quantity for fill in relevant),
        implementation_shortfall_x2_tick_shares=implementation_shortfall,
        spread_paid_x2_tick_shares=spread_paid,
        fees_micros=fees,
        rebates_micros=rebates,
        adverse_selection_x2_tick_shares=adverse,
        market_impact_x2_ticks=objective.side.sign * (final_midpoint - control_midpoint_x2),
        elapsed_time_us=max(0, last_fill_time - objective.start_time_us),
        cancel_count=recorder.cancelled_order_count,
        fill_uncertainty_quantity=max(0, objective.target_quantity - completed),
        deadline_failure=completion_by_deadline < objective.target_quantity,
        risk_rejection_count=risk_rejections,
    )


def _aggregate(
    manifest: BenchmarkManifest,
    runs: tuple[BenchmarkRunResult, ...],
) -> tuple[dict[str, object], ...]:
    metric_names = (
        "completion_bps",
        "implementation_shortfall_x2_tick_shares",
        "spread_paid_x2_tick_shares",
        "fees_micros",
        "rebates_micros",
        "adverse_selection_x2_tick_shares",
        "market_impact_x2_ticks",
        "elapsed_time_us",
        "cancel_count",
        "fill_uncertainty_quantity",
        "risk_rejection_count",
    )
    result: list[dict[str, object]] = []
    for algorithm_manifest in manifest.algorithm_manifests:
        group = tuple(
            run for run in runs if run.algorithm is algorithm_manifest.algorithm
        )
        item: dict[str, object] = {
            "aggregate_average_fill_price": {
                "denominator": sum(
                    run.metrics.average_fill_price_denominator for run in group
                ),
                "numerator_x2": sum(
                    run.metrics.average_fill_price_numerator_x2 for run in group
                ),
            },
            "algorithm": algorithm_manifest.algorithm.value,
            "deadline_failure_count": sum(run.metrics.deadline_failure for run in group),
            "run_count": len(group),
        }
        for name in metric_names:
            item[f"mean_{name}"] = {
                "denominator": len(group),
                "numerator": sum(int(getattr(run.metrics, name)) for run in group),
            }
        result.append(item)
    return tuple(result)


def _midpoint_x2(feed) -> int | None:
    if feed.best_bid_ticks is None or feed.best_ask_ticks is None:
        return None
    return feed.best_bid_ticks + feed.best_ask_ticks


def _finish_action() -> AlgorithmAction:
    return AlgorithmAction(AlgorithmActionType.FINISH, "algorithm already finished")


def _observe_only_action() -> AlgorithmAction:
    return AlgorithmAction(
        AlgorithmActionType.WAIT,
        "observe-only control emits no child order",
    )


def _require_exact_tracker_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} must be an object")
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )


def _tracker_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


def _tracker_array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _tracker_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a nonempty string")
    return value


def _tracker_int(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _tracker_optional_int(
    value: object, label: str, *, minimum: int
) -> int | None:
    if value is None:
        return None
    return _tracker_int(value, label, minimum=minimum)


def _tracker_int_map(
    value: object, label: str, *, minimum: int
) -> dict[str, int]:
    payload = _tracker_object(value, label)
    return {
        _tracker_string(key, f"{label} key"): _tracker_int(
            item, f"{label} value", minimum=minimum
        )
        for key, item in payload.items()
    }


def _tracker_optional_int_map(
    value: object, label: str, *, minimum: int
) -> dict[str, int | None]:
    payload = _tracker_object(value, label)
    return {
        _tracker_string(key, f"{label} key"): _tracker_optional_int(
            item, f"{label} value", minimum=minimum
        )
        for key, item in payload.items()
    }


def _tracker_nested_int_map(
    value: object, label: str, *, minimum: int
) -> dict[str, dict[str, int]]:
    payload = _tracker_object(value, label)
    return {
        _tracker_string(route_id, f"{label} route ID"): _tracker_int_map(
            rows,
            f"{label} route rows",
            minimum=minimum,
        )
        for route_id, rows in payload.items()
    }
