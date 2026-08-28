"""Standalone checkpoint owner for observable execution-algorithm state.

The component deliberately owns its own ``MarketCoordinator`` and client tracker.
It is a restorable component boundary, not an executable full-day composition.
In particular, a policy decision is captured as a pending action before it is
applied so restore cannot silently recompute it from a later observation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kirby2.algorithms.benchmark import ClientTrackerV1, apply_algorithm_action
from kirby2.algorithms.models import (
    AlgorithmAction,
    AlgorithmActionType,
    AlgorithmDecision,
    AlgorithmObservation,
    ClientFill,
    ClientLatencyState,
    ClientVenueState,
    ClientWorkingOrder,
    ExecutionObjective,
    ObservableMarketFeatures,
    RiskLimits,
)
from kirby2.algorithms.policies import (
    ExecutionAlgorithm,
    restore_algorithm_from_checkpoint_state,
)
from kirby2.exchange.models import Side
from kirby2.full_day.models import (
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)
from kirby2.immutable import freeze_json, thaw_json
from kirby2.multivenue import MarketCoordinator, RoutingRequest, VenueOrderStatus


EXECUTION_ALGORITHM_COMPONENT_ID = "EXECUTION_ALGORITHM_V1"
EXECUTION_ALGORITHM_COMPONENT_SCHEMA_VERSION = 1
EXECUTION_ALGORITHM_IMPLEMENTATION_VERSION = 1
EXECUTION_ALGORITHM_STATE_ID = "EXECUTION_ALGORITHM_V1"
EXECUTION_ALGORITHM_SUFFIX_COMMAND_SCHEMA_VERSION = 1

_OWNED_STATE_IDS = [EXECUTION_ALGORITHM_STATE_ID]
_COMMAND_TYPES = {"ADVANCE", "APPLY", "COMPLETE", "DECIDE", "SIM_MARKET"}


@dataclass(frozen=True, slots=True)
class ExecutionAlgorithmSuffixCommandV1:
    sequence: int
    simulation_time_us: int
    command_type: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("algorithm suffix sequence must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("algorithm suffix time must be nonnegative")
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("algorithm suffix command type is unknown")
        if type(self.parameters) is not dict:
            raise TypeError("algorithm suffix parameters must be an object")
        parameters = dict(self.parameters)
        if self.command_type == "SIM_MARKET":
            _require_exact_fields(
                parameters,
                {"order_id", "quantity", "side", "venue_id"},
                "algorithm simulated-market command",
            )
            _string(parameters["venue_id"], "algorithm simulated-market venue")
            _string(parameters["order_id"], "algorithm simulated-market order")
            Side(_string(parameters["side"], "algorithm simulated-market side"))
            _integer(
                parameters["quantity"],
                "algorithm simulated-market quantity",
                minimum=1,
            )
        elif parameters:
            raise ValueError("algorithm control command cannot carry parameters")
        validate_strict_json(parameters)
        object.__setattr__(self, "parameters", freeze_json(parameters))

    def as_dict(self) -> dict[str, object]:
        return {
            "command_type": self.command_type,
            "parameters": thaw_json(self.parameters),
            "schema_version": EXECUTION_ALGORITHM_SUFFIX_COMMAND_SCHEMA_VERSION,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> ExecutionAlgorithmSuffixCommandV1:
        _require_exact_fields(
            payload,
            {
                "command_type",
                "parameters",
                "schema_version",
                "sequence",
                "simulation_time_us",
            },
            "algorithm suffix command",
        )
        if (
            payload["schema_version"]
            != EXECUTION_ALGORITHM_SUFFIX_COMMAND_SCHEMA_VERSION
        ):
            raise ValueError("unsupported algorithm suffix command schema")
        command = cls(
            _integer(payload["sequence"], "algorithm suffix sequence", minimum=1),
            _integer(
                payload["simulation_time_us"],
                "algorithm suffix time",
                minimum=0,
            ),
            _string(payload["command_type"], "algorithm suffix command type"),
            _object(payload["parameters"], "algorithm suffix parameters"),
        )
        if command.as_dict() != dict(payload):
            raise ValueError("algorithm suffix command is not a fixed point")
        return command


class ExecutionAlgorithmOwnerV1:
    """Exact owner of one policy, schedule, tracker, and standalone coordinator."""

    def __init__(
        self,
        coordinator: MarketCoordinator,
        algorithm: ExecutionAlgorithm,
        objective: ExecutionObjective,
        risk_limits: RiskLimits,
        volume_profile_bps: tuple[int, ...],
        decision_interval_us: int,
        *,
        next_decision_time_us: int | None,
        tracker: ClientTrackerV1 | None = None,
        decisions: Sequence[AlgorithmDecision] = (),
        last_observation: Mapping[str, object] | None = None,
        pending_action: Mapping[str, object] | None = None,
        child_request_sequence: int = 0,
        route_ids: Sequence[str] = (),
        cancelled_order_count: int = 0,
        algorithm_finished: bool = False,
    ) -> None:
        if type(coordinator) is not MarketCoordinator:
            raise TypeError("algorithm owner requires the exact MarketCoordinator")
        if not isinstance(algorithm, ExecutionAlgorithm):
            raise TypeError("algorithm owner requires an execution policy")
        if not isinstance(objective, ExecutionObjective):
            raise TypeError("algorithm owner requires an execution objective")
        if not isinstance(risk_limits, RiskLimits):
            raise TypeError("algorithm owner requires canonical risk limits")
        if type(volume_profile_bps) is not tuple or any(
            type(value) is not int or value < 0 for value in volume_profile_bps
        ):
            raise TypeError("algorithm owner volume profile must be an integer tuple")
        if not volume_profile_bps or sum(volume_profile_bps) != 10_000:
            raise ValueError("algorithm owner volume profile must sum to 10000")
        if type(decision_interval_us) is not int or decision_interval_us <= 0:
            raise ValueError("algorithm decision interval must be positive")
        if (
            objective.deadline_us - objective.start_time_us
        ) % decision_interval_us:
            raise ValueError("algorithm decision interval must divide its objective")
        self.coordinator = coordinator
        self.algorithm = algorithm
        self.objective = objective
        self.risk_limits = risk_limits
        self.volume_profile_bps = volume_profile_bps
        self.decision_interval_us = decision_interval_us
        self.next_decision_time_us = next_decision_time_us
        self.tracker = ClientTrackerV1() if tracker is None else tracker
        self.decisions = [
            AlgorithmDecision.from_dict(decision.as_dict()) for decision in decisions
        ]
        self.last_observation = (
            None
            if last_observation is None
            else parse_canonical_json_object(canonical_json_bytes(last_observation))
        )
        self.pending_action = (
            None
            if pending_action is None
            else parse_canonical_json_object(canonical_json_bytes(pending_action))
        )
        self.child_request_sequence = child_request_sequence
        self.route_ids = list(route_ids)
        self.cancelled_order_count = cancelled_order_count
        self.algorithm_finished = algorithm_finished
        self.assert_invariants()

    @classmethod
    def create(
        cls,
        coordinator: MarketCoordinator,
        algorithm: ExecutionAlgorithm,
        objective: ExecutionObjective,
        risk_limits: RiskLimits,
        volume_profile_bps: tuple[int, ...],
        decision_interval_us: int,
    ) -> ExecutionAlgorithmOwnerV1:
        if coordinator.clock.current_time_us != objective.start_time_us:
            raise ValueError("algorithm coordinator must be at the objective start")
        algorithm.reset(objective)
        return cls(
            coordinator,
            algorithm,
            objective,
            risk_limits,
            volume_profile_bps,
            decision_interval_us,
            next_decision_time_us=objective.start_time_us,
        )

    def advance_to(self, target_time_us: int) -> None:
        if self.pending_action is not None:
            raise RuntimeError("algorithm cannot advance while an action is pending")
        if type(target_time_us) is not int:
            raise TypeError("algorithm advance target must be an integer")
        if target_time_us < self.coordinator.clock.current_time_us:
            raise ValueError("algorithm time cannot move backward")
        if (
            self.next_decision_time_us is not None
            and target_time_us > self.next_decision_time_us
        ):
            raise ValueError("algorithm advance cannot skip a decision deadline")
        self.coordinator.advance_to(target_time_us)
        self.tracker.refresh_client_state(self.coordinator, self.objective)
        self.assert_invariants()

    def execute_simulated_market(
        self,
        simulation_time_us: int,
        venue_id: str,
        order_id: str,
        side: Side,
        quantity: int,
    ) -> None:
        self.advance_to(simulation_time_us)
        self.coordinator.execute_simulated_market(
            venue_id,
            order_id,
            side,
            quantity,
        )
        self.tracker.refresh_client_state(self.coordinator, self.objective)
        self.assert_invariants()

    def capture_decision(self) -> AlgorithmAction:
        if self.pending_action is not None:
            raise RuntimeError("algorithm already has a pending action")
        if self.algorithm_finished or self.next_decision_time_us is None:
            raise RuntimeError("algorithm decision schedule is finished")
        current_time = self.coordinator.clock.current_time_us
        if current_time != self.next_decision_time_us:
            raise ValueError("algorithm decision must occur at its exact deadline")
        observation = self.tracker.observation(
            self.coordinator,
            self.objective,
            self.volume_profile_bps,
            self.risk_limits,
        )
        action = self.algorithm.decide(observation)
        self.last_observation = observation.as_dict()
        self.pending_action = {
            "action": action.as_dict(),
            "decision_sequence": len(self.decisions) + 1,
            "decision_time_us": current_time,
            "information_cutoff_time_us": current_time,
            "manifest_sha256": self.algorithm.manifest.sha256(),
            "observation": observation.as_dict(),
            "observation_sha256": observation.sha256(),
        }
        self.assert_invariants()
        return action

    def apply_pending_action(self) -> AlgorithmDecision:
        if self.pending_action is None:
            raise RuntimeError("algorithm has no pending action")
        pending = self.pending_action
        current_time = self.coordinator.clock.current_time_us
        if current_time != pending["information_cutoff_time_us"]:
            raise RuntimeError("pending action moved beyond its information cutoff")
        observation = self._pending_observation()
        action = AlgorithmAction.from_dict(
            _object(pending["action"], "pending algorithm action")
        )
        request_order_id = (
            None
            if action.action_type
            not in {AlgorithmActionType.SUBMIT, AlgorithmActionType.REPLACE}
            else f"ALG-E6-{self.child_request_sequence + 1:06d}"
        )
        accepted, rejection, route_id = apply_algorithm_action(
            self,
            self.tracker,
            observation,
            action,
            request_order_id=request_order_id,
        )
        decision = AlgorithmDecision(
            _integer(
                pending["decision_sequence"],
                "pending decision sequence",
                minimum=1,
            ),
            _integer(
                pending["decision_time_us"],
                "pending decision time",
                minimum=0,
            ),
            _string(
                pending["observation_sha256"],
                "pending observation SHA-256",
            ),
            _object(pending["observation"], "pending observation"),
            _string(pending["manifest_sha256"], "pending manifest SHA-256"),
            action,
            accepted,
            rejection,
            route_id,
        )
        self.decisions.append(decision)
        self.pending_action = None
        if action.action_type is AlgorithmActionType.FINISH:
            self.algorithm_finished = True
            self.next_decision_time_us = None
        else:
            next_time = decision.simulation_time_us + self.decision_interval_us
            self.next_decision_time_us = (
                None if next_time > self.objective.deadline_us else next_time
            )
        self.assert_invariants()
        return decision

    def route(self, request: RoutingRequest) -> str:
        expected = f"ALG-E6-{self.child_request_sequence + 1:06d}"
        if request.order_id != expected:
            raise ValueError("algorithm child-order allocator moved or was bypassed")
        route_id = self.coordinator.submit_route(request)
        self.child_request_sequence += 1
        self.route_ids.append(route_id)
        return route_id

    def cancel_all(self) -> None:
        responses = self.coordinator.cancel_all()
        self.tracker.refresh_client_state(self.coordinator, self.objective)
        self.cancelled_order_count += sum(
            response.status is VenueOrderStatus.CANCELLED for response in responses
        )

    def complete_session(self) -> None:
        if self.pending_action is not None:
            raise RuntimeError("algorithm cannot complete with a pending action")
        if self.next_decision_time_us is not None:
            raise RuntimeError("algorithm cannot complete before its decision schedule")
        if self.coordinator.clock.current_time_us < self.objective.deadline_us:
            raise RuntimeError("algorithm cannot complete before its objective deadline")
        self.coordinator.complete_session()
        self.tracker.refresh_after_completion(self.coordinator, self.objective)
        self.algorithm_finished = True
        self.assert_invariants()

    def checkpoint_state(self) -> dict[str, object]:
        self.assert_invariants()
        state: dict[str, object] = {
            "algorithm_finished": self.algorithm_finished,
            "allocators": {
                "cancelled_order_count": self.cancelled_order_count,
                "child_request_sequence": self.child_request_sequence,
                "route_ids": list(self.route_ids),
            },
            "component_id": EXECUTION_ALGORITHM_COMPONENT_ID,
            "coordinator": self.coordinator.checkpoint_state(),
            "decisions": [decision.as_dict() for decision in self.decisions],
            "implementation_version": EXECUTION_ALGORITHM_IMPLEMENTATION_VERSION,
            "last_observation": self.last_observation,
            "objective": self.objective.as_dict(),
            "owned_state_ids": list(_OWNED_STATE_IDS),
            "pending_action": self.pending_action,
            "policy": self.algorithm.checkpoint_state(),
            "risk_limits": self.risk_limits.as_dict(),
            "schedule": {
                "decision_interval_us": self.decision_interval_us,
                "next_decision_time_us": self.next_decision_time_us,
            },
            "schema_version": EXECUTION_ALGORITHM_COMPONENT_SCHEMA_VERSION,
            "tracker": self.tracker.checkpoint_state(),
            "tracker_metrics": self._tracker_metrics(),
            "volume_profile_bps": list(self.volume_profile_bps),
        }
        validate_strict_json(state)
        return parse_canonical_json_object(canonical_json_bytes(state))

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
    ) -> ExecutionAlgorithmOwnerV1:
        _require_exact_fields(
            payload,
            {
                "algorithm_finished",
                "allocators",
                "component_id",
                "coordinator",
                "decisions",
                "implementation_version",
                "last_observation",
                "objective",
                "owned_state_ids",
                "pending_action",
                "policy",
                "risk_limits",
                "schedule",
                "schema_version",
                "tracker",
                "tracker_metrics",
                "volume_profile_bps",
            },
            "execution-algorithm component checkpoint",
        )
        validate_strict_json(payload)
        if payload["schema_version"] != EXECUTION_ALGORITHM_COMPONENT_SCHEMA_VERSION:
            raise ValueError("unsupported execution-algorithm checkpoint schema")
        if (
            payload["implementation_version"]
            != EXECUTION_ALGORITHM_IMPLEMENTATION_VERSION
        ):
            raise ValueError("unsupported execution-algorithm implementation version")
        if payload["component_id"] != EXECUTION_ALGORITHM_COMPONENT_ID:
            raise ValueError("execution-algorithm checkpoint has the wrong owner")
        if payload["owned_state_ids"] != _OWNED_STATE_IDS:
            raise ValueError("execution-algorithm owned-state inventory differs")
        schedule = _object(payload["schedule"], "algorithm schedule")
        _require_exact_fields(
            schedule,
            {"decision_interval_us", "next_decision_time_us"},
            "algorithm schedule",
        )
        allocators = _object(payload["allocators"], "algorithm allocators")
        _require_exact_fields(
            allocators,
            {"cancelled_order_count", "child_request_sequence", "route_ids"},
            "algorithm allocators",
        )
        raw_last = payload["last_observation"]
        raw_pending = payload["pending_action"]
        owner = cls(
            MarketCoordinator.from_checkpoint_state(
                _object(payload["coordinator"], "algorithm coordinator")
            ),
            restore_algorithm_from_checkpoint_state(
                _object(payload["policy"], "algorithm policy")
            ),
            ExecutionObjective.from_dict(
                _object(payload["objective"], "algorithm objective")
            ),
            RiskLimits.from_dict(
                _object(payload["risk_limits"], "algorithm risk limits")
            ),
            tuple(
                _integer(value, "algorithm volume profile", minimum=0)
                for value in _array(
                    payload["volume_profile_bps"], "algorithm volume profile"
                )
            ),
            _integer(
                schedule["decision_interval_us"],
                "algorithm decision interval",
                minimum=1,
            ),
            next_decision_time_us=_optional_integer(
                schedule["next_decision_time_us"],
                "algorithm next decision time",
                minimum=0,
            ),
            tracker=ClientTrackerV1.from_checkpoint_state(
                _object(payload["tracker"], "algorithm client tracker")
            ),
            decisions=tuple(
                AlgorithmDecision.from_dict(
                    _object(value, "algorithm decision record")
                )
                for value in _array(payload["decisions"], "algorithm decisions")
            ),
            last_observation=(
                None
                if raw_last is None
                else _object(raw_last, "algorithm last observation")
            ),
            pending_action=(
                None
                if raw_pending is None
                else _object(raw_pending, "algorithm pending action")
            ),
            child_request_sequence=_integer(
                allocators["child_request_sequence"],
                "algorithm child allocator",
                minimum=0,
            ),
            route_ids=tuple(
                _string(value, "algorithm route ID")
                for value in _array(allocators["route_ids"], "algorithm route IDs")
            ),
            cancelled_order_count=_integer(
                allocators["cancelled_order_count"],
                "algorithm cancelled-order count",
                minimum=0,
            ),
            algorithm_finished=_boolean(
                payload["algorithm_finished"], "algorithm finished flag"
            ),
        )
        if owner._tracker_metrics() != _object(
            payload["tracker_metrics"], "algorithm tracker metrics"
        ):
            raise ValueError("algorithm tracker metrics differ from owned state")
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("execution-algorithm checkpoint is not a fixed point")
        return owner

    @classmethod
    def from_canonical_state_bytes(
        cls,
        payload: bytes,
    ) -> ExecutionAlgorithmOwnerV1:
        return cls.from_checkpoint_state(parse_canonical_json_object(payload))

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    def state_sha256(self) -> str:
        return canonical_sha256(self.checkpoint_state())

    def public_projection(self) -> dict[str, object]:
        projection: dict[str, object] = {
            "algorithm_finished": self.algorithm_finished,
            "cancelled_order_count": self.cancelled_order_count,
            "component_id": EXECUTION_ALGORITHM_COMPONENT_ID,
            "consolidated_feed": self.coordinator.consolidated_feed().as_dict(),
            "coordinator_state_sha256": canonical_sha256(
                self.coordinator.checkpoint_state()
            ),
            "decisions": [decision.as_dict() for decision in self.decisions],
            "next_decision_time_us": self.next_decision_time_us,
            "objective": self.objective.as_dict(),
            "pending_action": self.pending_action,
            "policy_manifest": self.algorithm.manifest.as_dict(),
            "risk_limits": self.risk_limits.as_dict(),
            "route_ids": list(self.route_ids),
            "simulation_time_us": self.coordinator.clock.current_time_us,
            "tracker_metrics": self._tracker_metrics(),
        }
        validate_strict_json(projection)
        return parse_canonical_json_object(canonical_json_bytes(projection))

    def assert_invariants(self) -> None:
        self.coordinator.assert_invariants()
        self.tracker.assert_invariants()
        if self.algorithm.objective != self.objective:
            raise RuntimeError("algorithm policy objective differs from its owner")
        if self.coordinator.clock.current_time_us < self.objective.start_time_us:
            raise RuntimeError("algorithm coordinator precedes its objective")
        if type(self.algorithm_finished) is not bool:
            raise RuntimeError("algorithm finished state is invalid")
        if type(self.child_request_sequence) is not int or self.child_request_sequence < 0:
            raise RuntimeError("algorithm child allocator is invalid")
        if type(self.cancelled_order_count) is not int or self.cancelled_order_count < 0:
            raise RuntimeError("algorithm cancel count is invalid")
        if self.child_request_sequence != len(self.route_ids):
            raise RuntimeError("algorithm child allocator differs from route inventory")
        if len(self.route_ids) != len(set(self.route_ids)):
            raise RuntimeError("algorithm route IDs are duplicated")
        coordinator_state = self.coordinator.checkpoint_state()
        coordinator_routes = coordinator_state["routes"]
        if type(coordinator_routes) is not dict or list(coordinator_routes) != self.route_ids:
            raise RuntimeError("algorithm coordinator route ownership differs")
        for index, route_id in enumerate(self.route_ids, start=1):
            route = _object(coordinator_routes[route_id], "algorithm coordinator route")
            request = _object(route["request"], "algorithm coordinator request")
            if request.get("order_id") != f"ALG-E6-{index:06d}":
                raise RuntimeError("algorithm child-order allocator identity differs")
        tracker_state = self.tracker.checkpoint_state()
        tracker_routes = tracker_state["route_ids"]
        if tracker_routes != self.route_ids:
            raise RuntimeError("algorithm tracker route ownership differs")
        tracker_quantities = _object(
            tracker_state["route_quantities"], "algorithm tracker route quantities"
        )
        tracker_send_times = _object(
            tracker_state["route_send_time_us"], "algorithm tracker route send times"
        )
        tracker_allocations = _object(
            tracker_state["route_order_allocations"],
            "algorithm tracker route allocations",
        )
        tracker_fills = _object(
            tracker_state["route_order_fills"], "algorithm tracker route fills"
        )
        authoritative_child_ids: set[str] = set()
        routed_quantity = 0
        for route_id in self.route_ids:
            route = _object(coordinator_routes[route_id], "algorithm coordinator route")
            request = _object(route["request"], "algorithm coordinator request")
            if tracker_quantities.get(route_id) != request.get("quantity"):
                raise RuntimeError("algorithm tracker route quantity differs from routing")
            if request.get("side") != self.objective.side.value:
                raise RuntimeError("algorithm routed child side differs from its objective")
            routed_quantity += _integer(
                request["quantity"], "algorithm routed child quantity", minimum=1
            )
            decision = _object(route["decision"], "algorithm coordinator decision")
            if tracker_send_times.get(route_id) != decision.get("decision_time_us"):
                raise RuntimeError("algorithm tracker route time differs from routing")
            executions = {
                _string(row["order_id"], "algorithm route execution order ID"): row
                for row in (
                    _object(raw, "algorithm route execution")
                    for raw in _array(route["executions"], "algorithm route executions")
                )
            }
            pending_child_ids = {
                _string(row["order_id"], "algorithm pending child order ID")
                for row in (
                    _object(raw, "algorithm pending route leg")
                    for raw in _array(
                        coordinator_state["pending_route_legs"],
                        "algorithm pending route legs",
                    )
                )
                if row.get("route_id") == route_id
            }
            authoritative_child_ids.update(executions)
            authoritative_child_ids.update(pending_child_ids)
            child_ids = set(
                _object(
                    tracker_allocations.get(route_id, {}),
                    "algorithm tracker route allocations",
                )
            ) | set(
                _object(
                    tracker_fills.get(route_id, {}),
                    "algorithm tracker route fills",
                )
            )
            if child_ids - (set(executions) | pending_child_ids) or any(
                f"-{route_id}-" not in order_id for order_id in child_ids
            ):
                raise RuntimeError("algorithm tracker has an orphan child order")
            allocations = _object(
                tracker_allocations.get(route_id, {}),
                "algorithm tracker route allocations",
            )
            fills = _object(
                tracker_fills.get(route_id, {}), "algorithm tracker route fills"
            )
            if any(
                order_id in executions
                and quantity != executions[order_id].get("requested_quantity")
                for order_id, quantity in allocations.items()
            ) or any(
                order_id in executions
                and (
                    type(quantity) is not int
                    or quantity > executions[order_id].get("filled_quantity", -1)
                )
                for order_id, quantity in fills.items()
            ):
                raise RuntimeError("algorithm tracker child quantities differ from routing")
        order_sides = _object(
            tracker_state["order_sides"], "algorithm tracker order sides"
        )
        if any(side != self.objective.side.value for side in order_sides.values()):
            raise RuntimeError("algorithm tracker child side differs from its objective")
        observable_own_ids = {
            order.order_id
            for venue in self.coordinator.venues.values()
            for order in venue.observable_feed().own_orders
        }
        if observable_own_ids - authoritative_child_ids:
            raise RuntimeError("algorithm coordinator has an unowned child order")
        signed_position = self.coordinator.global_player_position * self.objective.side.sign
        if not 0 <= signed_position <= routed_quantity:
            raise RuntimeError("algorithm coordinator position differs from child fills")
        expected_times = self._expected_decision_times(len(self.decisions))
        if tuple(decision.sequence for decision in self.decisions) != tuple(
            range(1, len(self.decisions) + 1)
        ):
            raise RuntimeError("algorithm decision sequence is not contiguous")
        if tuple(decision.simulation_time_us for decision in self.decisions) != expected_times:
            raise RuntimeError("algorithm decisions moved off their schedule")
        manifest_sha256 = self.algorithm.manifest.sha256()
        for decision in self.decisions:
            if decision.manifest_sha256 != manifest_sha256:
                raise RuntimeError("algorithm decision cites a different policy manifest")
            self._assert_observation_record(decision.observation, decision.sequence)
        resulting_routes = [
            decision.resulting_route_id
            for decision in self.decisions
            if decision.resulting_route_id is not None
        ]
        if resulting_routes != self.route_ids:
            raise RuntimeError("algorithm decision routes differ from allocator routes")
        expected_tracker_sequence = len(self.decisions) + (
            1 if self.pending_action is not None else 0
        )
        if self.tracker.sequence != expected_tracker_sequence:
            raise RuntimeError("algorithm tracker and decision sequences differ")
        if expected_tracker_sequence == 0:
            if self.last_observation is not None:
                raise RuntimeError("algorithm has an observation before its first decision")
        else:
            expected_last = (
                _object(self.pending_action["observation"], "pending observation")
                if self.pending_action is not None
                else self.decisions[-1].observation
            )
            if self.last_observation != expected_last:
                raise RuntimeError("algorithm last observation is not authoritative")
            self._assert_observation_record(
                _object(self.last_observation, "algorithm last observation"),
                expected_tracker_sequence,
            )
            if self.tracker.last_observation_time_us != self.last_observation.get(
                "simulation_time_us"
            ):
                raise RuntimeError("algorithm tracker observation time differs")
            features = _object(
                self.last_observation["observable_market_features"],
                "algorithm last observable features",
            )
            if self.tracker.last_midpoint_x2 != features.get("midpoint_x2"):
                raise RuntimeError("algorithm tracker midpoint differs from observation")
            observed_fills = _array(
                self.last_observation["fills"], "algorithm last observed fills"
            )
            tracker_fill_rows = [fill.as_dict() for fill in self.tracker.fills]
            if tracker_fill_rows[: len(observed_fills)] != observed_fills:
                raise RuntimeError("algorithm tracker fills do not extend its observation")
        if self.pending_action is not None:
            self._assert_pending_action()
        expected_next = self._scheduled_time_after(len(self.decisions))
        if self.algorithm_finished:
            if self.next_decision_time_us is not None:
                raise RuntimeError("finished algorithm retained a decision deadline")
        elif self.next_decision_time_us != expected_next:
            raise RuntimeError("algorithm next-decision deadline differs from schedule")
        if self.pending_action is not None and self.algorithm_finished:
            raise RuntimeError("finished algorithm retained a pending action")
        finish_indexes = tuple(
            index
            for index, decision in enumerate(self.decisions)
            if decision.action.action_type is AlgorithmActionType.FINISH
        )
        if finish_indexes and finish_indexes != (len(self.decisions) - 1,):
            raise RuntimeError("algorithm decision history continued after FINISH")
        if self.algorithm_finished and not self.coordinator.complete and not finish_indexes:
            raise RuntimeError("algorithm was marked finished without a FINISH decision")
        if finish_indexes and not self.algorithm_finished:
            raise RuntimeError("algorithm FINISH decision did not finish its schedule")
        if self.next_decision_time_us is not None:
            if self.coordinator.clock.current_time_us > self.next_decision_time_us:
                raise RuntimeError("algorithm clock passed its pending deadline")
        elif not self.algorithm_finished and self.decisions:
            if self.decisions[-1].simulation_time_us != self.objective.deadline_us:
                raise RuntimeError("algorithm schedule ended before the objective deadline")
        if self.coordinator.complete and not self.algorithm_finished:
            raise RuntimeError("completed coordinator retained an unfinished algorithm")
        if self.coordinator.complete and (
            self.coordinator.clock.current_time_us < self.objective.deadline_us
            or self.next_decision_time_us is not None
        ):
            raise RuntimeError("completed algorithm coordinator has an unfinished schedule")
        self._assert_policy_progress()
        filled = sum(fill.quantity for fill in self.tracker.fills)
        working = sum(
            order.remaining_quantity
            for order in self.tracker.current_working_orders(self.coordinator)
        )
        if filled + working + self.tracker.pending_route_quantity > self.objective.target_quantity:
            raise RuntimeError("algorithm quantity state does not conserve")

    def _assert_pending_action(self) -> None:
        pending = _object(self.pending_action, "algorithm pending action")
        _require_exact_fields(
            pending,
            {
                "action",
                "decision_sequence",
                "decision_time_us",
                "information_cutoff_time_us",
                "manifest_sha256",
                "observation",
                "observation_sha256",
            },
            "algorithm pending action",
        )
        sequence = _integer(
            pending["decision_sequence"], "pending decision sequence", minimum=1
        )
        decision_time = _integer(
            pending["decision_time_us"], "pending decision time", minimum=0
        )
        cutoff = _integer(
            pending["information_cutoff_time_us"],
            "pending information cutoff",
            minimum=0,
        )
        if sequence != len(self.decisions) + 1:
            raise RuntimeError("pending action decision sequence differs")
        if decision_time != self.next_decision_time_us or cutoff != decision_time:
            raise RuntimeError("pending action deadline and information cutoff differ")
        if self.coordinator.clock.current_time_us != cutoff:
            raise RuntimeError("pending action no longer sits at its information cutoff")
        observation = _object(pending["observation"], "pending observation")
        self._assert_observation_record(observation, sequence)
        if observation.get("simulation_time_us") != cutoff:
            raise RuntimeError("pending action observation has the wrong cutoff")
        if canonical_sha256(observation) != pending["observation_sha256"]:
            raise RuntimeError("pending action observation digest differs")
        if pending["manifest_sha256"] != self.algorithm.manifest.sha256():
            raise RuntimeError("pending action cites a different policy manifest")
        AlgorithmAction.from_dict(_object(pending["action"], "pending action body"))
        if self.coordinator.consolidated_feed().as_dict() != observation.get(
            "consolidated_feed"
        ):
            raise RuntimeError("pending action feed differs from its information cutoff")

    def _assert_observation_record(
        self,
        payload: Mapping[str, object],
        sequence: int,
    ) -> None:
        _require_exact_fields(
            payload,
            {
                "consolidated_feed",
                "fills",
                "latency_state",
                "objective",
                "observable_market_features",
                "remaining_quantity",
                "representation",
                "risk_limits",
                "sequence",
                "simulation_time_us",
                "venue_state",
                "working_orders",
            },
            "algorithm observation record",
        )
        if payload["representation"] != "ALGORITHM_CLIENT_OBSERVATION":
            raise RuntimeError("algorithm observation representation differs")
        if payload["sequence"] != sequence:
            raise RuntimeError("algorithm observation sequence differs")
        if payload["objective"] != self.objective.as_dict():
            raise RuntimeError("algorithm observation objective differs")
        if payload["risk_limits"] != self.risk_limits.as_dict():
            raise RuntimeError("algorithm observation risk limits differ")
        features = ObservableMarketFeatures.from_dict(
            _object(payload["observable_market_features"], "observable features")
        )
        if features.expected_volume_profile_bps != self.volume_profile_bps:
            raise RuntimeError("algorithm observation volume profile differs")
        ClientLatencyState.from_dict(
            _object(payload["latency_state"], "algorithm latency state")
        )
        for row in _array(payload["venue_state"], "algorithm venue state"):
            ClientVenueState.from_dict(_object(row, "algorithm venue state row"))
        working = tuple(
            ClientWorkingOrder.from_dict(_object(row, "algorithm working-order row"))
            for row in _array(payload["working_orders"], "algorithm working orders")
        )
        fills = tuple(
            ClientFill.from_dict(_object(row, "algorithm fill row"))
            for row in _array(payload["fills"], "algorithm fills")
        )
        if len({row.order_id for row in working}) != len(working):
            raise RuntimeError("algorithm observation has duplicate working orders")
        venue_rows = _array(payload["venue_state"], "algorithm venue state")
        if len({row["venue_id"] for row in venue_rows}) != len(venue_rows):
            raise RuntimeError("algorithm observation has duplicate venue rows")
        if any(row.side is not self.objective.side for row in (*working, *fills)):
            raise RuntimeError("algorithm observation side differs from its objective")
        remaining = _integer(
            payload["remaining_quantity"],
            "algorithm observation remaining quantity",
            minimum=0,
        )
        if remaining != max(
            0,
            self.objective.target_quantity - sum(fill.quantity for fill in fills),
        ):
            raise RuntimeError("algorithm observation remaining quantity differs")
        if sum(row.remaining_quantity for row in working) > remaining:
            raise RuntimeError("algorithm observation working quantity does not conserve")
        _integer(
            payload["simulation_time_us"],
            "algorithm observation time",
            minimum=0,
        )
        _object(payload["consolidated_feed"], "algorithm consolidated feed")

    def _assert_policy_progress(self) -> None:
        mutable = _object(
            self.algorithm.checkpoint_state()["mutable_state"],
            "algorithm policy mutable state",
        )
        decision_times = {
            decision.simulation_time_us for decision in self.decisions
        }
        if self.pending_action is not None:
            decision_times.add(
                _integer(
                    self.pending_action["decision_time_us"],
                    "pending policy decision time",
                    minimum=0,
                )
            )
        if "last_submit_time_us" in mutable:
            last_submit = mutable["last_submit_time_us"]
            if last_submit is not None and last_submit not in decision_times:
                raise RuntimeError("algorithm policy submit time is not a decision time")
            if last_submit is not None and last_submit > self.coordinator.clock.current_time_us:
                raise RuntimeError("algorithm policy submit time is in the future")
        if "replay_index" in mutable:
            replayed = sum(
                decision.action.reason.startswith("replayed player")
                for decision in self.decisions
            )
            if self.pending_action is not None:
                pending_action = AlgorithmAction.from_dict(
                    _object(self.pending_action["action"], "pending policy action")
                )
                replayed += pending_action.reason.startswith("replayed player")
            if mutable["replay_index"] != replayed:
                raise RuntimeError("manual replay index differs from decision history")

    def _pending_observation(self) -> AlgorithmObservation:
        pending = _object(self.pending_action, "algorithm pending action")
        payload = _object(pending["observation"], "pending algorithm observation")
        feed = self.coordinator.consolidated_feed()
        if feed.as_dict() != payload["consolidated_feed"]:
            raise RuntimeError("pending action cannot use a later consolidated feed")
        observation = AlgorithmObservation(
            _integer(payload["sequence"], "pending observation sequence", minimum=1),
            _integer(
                payload["simulation_time_us"],
                "pending observation time",
                minimum=0,
            ),
            ExecutionObjective.from_dict(
                _object(payload["objective"], "pending observation objective")
            ),
            _integer(
                payload["remaining_quantity"],
                "pending observation remaining quantity",
                minimum=0,
            ),
            ObservableMarketFeatures.from_dict(
                _object(
                    payload["observable_market_features"],
                    "pending observable features",
                )
            ),
            tuple(
                ClientWorkingOrder.from_dict(
                    _object(row, "pending working-order row")
                )
                for row in _array(payload["working_orders"], "pending working orders")
            ),
            tuple(
                ClientFill.from_dict(_object(row, "pending fill row"))
                for row in _array(payload["fills"], "pending fills")
            ),
            ClientLatencyState.from_dict(
                _object(payload["latency_state"], "pending latency state")
            ),
            tuple(
                ClientVenueState.from_dict(_object(row, "pending venue row"))
                for row in _array(payload["venue_state"], "pending venue state")
            ),
            RiskLimits.from_dict(
                _object(payload["risk_limits"], "pending observation risk limits")
            ),
            feed,
        )
        if observation.as_dict() != payload:
            raise RuntimeError("pending observation is not a canonical fixed point")
        return observation

    def _expected_decision_times(self, count: int) -> tuple[int, ...]:
        times: list[int] = []
        value = self.objective.start_time_us
        for _ in range(count):
            if value > self.objective.deadline_us:
                raise RuntimeError("algorithm has more decisions than scheduled deadlines")
            times.append(value)
            value += self.decision_interval_us
        return tuple(times)

    def _scheduled_time_after(self, count: int) -> int | None:
        value = self.objective.start_time_us + count * self.decision_interval_us
        return None if value > self.objective.deadline_us else value

    def _tracker_metrics(self) -> dict[str, object]:
        fills = sum(fill.quantity for fill in self.tracker.fills)
        working = sum(
            order.remaining_quantity
            for order in self.tracker.current_working_orders(self.coordinator)
        )
        return {
            "client_observation_sequence": self.tracker.sequence,
            "decision_count": len(self.decisions),
            "observed_fill_quantity": fills,
            "pending_route_quantity": self.tracker.pending_route_quantity,
            "working_quantity": working,
        }


def apply_execution_algorithm_suffix(
    owner: ExecutionAlgorithmOwnerV1,
    commands: Sequence[ExecutionAlgorithmSuffixCommandV1],
    *,
    completed_time_us: int,
) -> None:
    """Apply a strict suffix without replaying or inspecting the captured prefix."""

    if type(owner) is not ExecutionAlgorithmOwnerV1:
        raise TypeError("algorithm suffix requires ExecutionAlgorithmOwnerV1")
    if not isinstance(commands, Sequence) or isinstance(
        commands, (str, bytes, bytearray)
    ):
        raise TypeError("algorithm suffix commands must be a sequence")
    command_tuple = tuple(commands)
    if any(type(command) is not ExecutionAlgorithmSuffixCommandV1 for command in command_tuple):
        raise TypeError("algorithm suffix contains a noncanonical command")
    if tuple(command.sequence for command in command_tuple) != tuple(
        range(1, len(command_tuple) + 1)
    ):
        raise ValueError("algorithm suffix command sequence must start at one")
    times = tuple(command.simulation_time_us for command in command_tuple)
    if times != tuple(sorted(times)):
        raise ValueError("algorithm suffix command time moved backward")
    if times and times[0] < owner.coordinator.clock.current_time_us:
        raise ValueError("algorithm suffix precedes its checkpoint")
    for command in command_tuple:
        if command.command_type == "ADVANCE":
            owner.advance_to(command.simulation_time_us)
        elif command.command_type == "SIM_MARKET":
            owner.execute_simulated_market(
                command.simulation_time_us,
                _string(command.parameters["venue_id"], "simulated-market venue"),
                _string(command.parameters["order_id"], "simulated-market order"),
                Side(_string(command.parameters["side"], "simulated-market side")),
                _integer(
                    command.parameters["quantity"],
                    "simulated-market quantity",
                    minimum=1,
                ),
            )
        elif command.command_type == "DECIDE":
            owner.advance_to(command.simulation_time_us)
            owner.capture_decision()
        elif command.command_type == "APPLY":
            if command.simulation_time_us != owner.coordinator.clock.current_time_us:
                raise ValueError("pending action must apply at its information cutoff")
            owner.apply_pending_action()
        elif command.command_type == "COMPLETE":
            owner.advance_to(command.simulation_time_us)
            owner.complete_session()
        else:  # pragma: no cover - constructor fixes this inventory
            raise RuntimeError("unreachable algorithm suffix command")
    if type(completed_time_us) is not int or completed_time_us < 0:
        raise ValueError("algorithm suffix completion time must be nonnegative")
    if completed_time_us < owner.coordinator.clock.current_time_us:
        raise ValueError("algorithm suffix completion precedes resulting state")
    if completed_time_us > owner.coordinator.clock.current_time_us:
        owner.advance_to(completed_time_us)
    owner.assert_invariants()


def _require_exact_fields(
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


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an array")
    return value


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _optional_integer(value: object, label: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, label, minimum=minimum)


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be boolean")
    return value


__all__ = [
    "EXECUTION_ALGORITHM_COMPONENT_ID",
    "EXECUTION_ALGORITHM_COMPONENT_SCHEMA_VERSION",
    "EXECUTION_ALGORITHM_IMPLEMENTATION_VERSION",
    "EXECUTION_ALGORITHM_STATE_ID",
    "EXECUTION_ALGORITHM_SUFFIX_COMMAND_SCHEMA_VERSION",
    "ExecutionAlgorithmOwnerV1",
    "ExecutionAlgorithmSuffixCommandV1",
    "apply_execution_algorithm_suffix",
]
