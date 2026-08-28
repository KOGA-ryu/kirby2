"""Checkpoint inventory, quiescent-cut, and pilot-limit contracts."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files

from .models import (
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)


CHECKPOINT_CONTRACT_SCHEMA_VERSION = 1
CHECKPOINT_INVENTORY_ID = "FULL_DAY_CHECKPOINT_INVENTORY_V1"
PILOT_LIMITS_ID = "WO31_F_PILOT_LIMITS_V1"

_PRESENCE_VALUES = frozenset({"ALWAYS", "CONDITIONAL"})
_PREDICATES = frozenset(
    {
        "ALWAYS",
        "COMPONENT.DELIVERY_ASYNC_V1.ACTIVE",
        "COMPONENT.EXECUTION_ALGORITHM_V1.ACTIVE",
        "COMPONENT.FEATURE_STRATEGY_PLAYER_V1.ACTIVE",
        "COMPONENT.FLOW_HAWKES_V1.ACTIVE",
        "COMPONENT.FLOW_QUEUE_REACTIVE_V1.ACTIVE",
        "COMPONENT.FLOW_SIMPLE_V1.ACTIVE",
        "COMPONENT.HISTORICAL_REPLAY_V1.ACTIVE",
        "COMPONENT.REGIME_ORDER_FLOW_V1.ACTIVE",
        "COMPONENT.VENUE_MULTIVENUE_HIDDEN_V1.ACTIVE",
        "PLAN.PARTICIPANT_SCHEDULE_NONEMPTY_OR_ANY_INITIAL_ACTIVE",
    }
)


class CheckpointCaptureScopeV1(str, Enum):
    FULL_DAY_RUNTIME = "FULL_DAY_RUNTIME"
    RESTORABLE_COMPONENT_ONLY = "RESTORABLE_COMPONENT_ONLY"


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise TypeError(f"{field} must be an array of nonempty strings")
    result = tuple(value)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError(f"{field} must be sorted and unique")
    return result


def _valid_sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class CheckpointInventoryItemV1:
    schema_version: int
    component_id: str
    state_owner_id: str
    state_schema_version: int
    owned_state_fields: tuple[str, ...]
    presence: str
    active_predicate: str
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != CHECKPOINT_CONTRACT_SCHEMA_VERSION
        ):
            raise ValueError("CheckpointInventoryItemV1 schema_version must be 1")
        _exact_string(self.component_id, "component_id")
        _exact_string(self.state_owner_id, "state_owner_id")
        _exact_int(self.state_schema_version, "state_schema_version", minimum=1)
        if type(self.owned_state_fields) is not tuple or not self.owned_state_fields:
            raise ValueError("checkpoint item requires owned_state_fields")
        if any(type(item) is not str or not item for item in self.owned_state_fields):
            raise TypeError("owned_state_fields must contain nonempty strings")
        validate_strict_json(self.owned_state_fields)
        if self.owned_state_fields != tuple(sorted(set(self.owned_state_fields))):
            raise ValueError("owned_state_fields must be sorted and unique")
        if self.presence not in _PRESENCE_VALUES:
            raise ValueError("checkpoint item presence must be ALWAYS or CONDITIONAL")
        if self.active_predicate not in _PREDICATES:
            raise ValueError("checkpoint item active_predicate is unsupported")
        if self.presence == "ALWAYS" and self.active_predicate != "ALWAYS":
            raise ValueError("always-present state must use the ALWAYS predicate")
        if self.presence == "CONDITIONAL" and self.active_predicate == "ALWAYS":
            raise ValueError("conditional state requires an explicit active predicate")
        if type(self.dependencies) is not tuple:
            raise TypeError("checkpoint dependencies must be an immutable tuple")
        if any(type(item) is not str or not item for item in self.dependencies):
            raise TypeError("checkpoint dependencies must be nonempty strings")
        validate_strict_json(self.dependencies)
        if self.dependencies != tuple(sorted(self.dependencies)) or len(
            self.dependencies
        ) != len(set(self.dependencies)):
            raise ValueError("checkpoint dependencies must be sorted and unique")
        if self.component_id in self.dependencies:
            raise ValueError("checkpoint item cannot depend on itself")

    def as_dict(self) -> dict[str, object]:
        return {
            "active_predicate": self.active_predicate,
            "component_id": self.component_id,
            "dependencies": list(self.dependencies),
            "owned_state_fields": list(self.owned_state_fields),
            "presence": self.presence,
            "schema_version": self.schema_version,
            "state_owner_id": self.state_owner_id,
            "state_schema_version": self.state_schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CheckpointInventoryItemV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "active_predicate",
                "component_id",
                "dependencies",
                "owned_state_fields",
                "presence",
                "schema_version",
                "state_owner_id",
                "state_schema_version",
            },
            "CheckpointInventoryItemV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            component_id=_exact_string(payload["component_id"], "component_id"),
            state_owner_id=_exact_string(payload["state_owner_id"], "state_owner_id"),
            state_schema_version=_exact_int(
                payload["state_schema_version"], "state_schema_version", minimum=1
            ),
            owned_state_fields=_string_tuple(
                payload["owned_state_fields"], "owned_state_fields"
            ),
            presence=_exact_string(payload["presence"], "presence"),
            active_predicate=_exact_string(
                payload["active_predicate"], "active_predicate"
            ),
            dependencies=_string_tuple(payload["dependencies"], "dependencies"),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CheckpointInventoryItemV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def _state_fields(*values: str) -> tuple[str, ...]:
    fields = tuple(sorted(values))
    if not fields or len(fields) != len(set(fields)):
        raise RuntimeError("checkpoint owned-state field declarations must be nonempty/unique")
    return fields


_STATE_FIELDS_BY_ID = {
    "AGENT_SCHEDULER_METAORDERS_V1": _state_fields(
        "agent.activation_state",
        "agent.definition_id_versions",
        "agent.event_cursors",
        "agent.inventories",
        "agent.next_scheduled_decisions",
        "agent.order_bindings",
        "agent.pending_decisions",
        "agent.policy_states",
        "agent.rng_states",
        "agent.scheduler_allocator_state",
        "agent.working_order_ids",
        "metaorder.states",
        "participant.population_transition_cursor",
    ),
    "ALGORITHMS_DEADLINES_CHILD_ORDERS_V1": _state_fields(
        "algorithm.action_allocator_state",
        "algorithm.benchmark_tracker_state",
        "algorithm.child_fill_records",
        "algorithm.child_order_bindings",
        "algorithm.child_order_records",
        "algorithm.current_time_us",
        "algorithm.information_cutoff",
        "algorithm.next_deadline_sequence",
        "algorithm.objective_parameters",
        "algorithm.observation_sequence",
        "algorithm.outstanding_child_orders",
        "algorithm.pending_action",
        "algorithm.pending_deadline",
        "algorithm.policy_id_version",
        "algorithm.recorded_client_latency_state",
        "algorithm.recorded_venue_state",
        "algorithm.risk_parameters",
        "algorithm.schedule_progress",
    ),
    "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1": _state_fields(
        "auction.imbalance_state",
        "auction.order_priority_allocator_state",
        "auction.order_records",
        "auction.reference_price_ticks",
        "auction.seen_order_ids",
        "auction.trade_allocator_state",
        "venue_truth.auction.player_fill_history",
        "venue_truth.auction.player_position",
    ),
    "CALENDAR_CURSOR_V1": _state_fields(
        "calendar.boundary_operation_index",
        "calendar.current_phase_id",
        "calendar.next_boundary_time_us",
    ),
    "CHECKPOINT_CONTROLLER_V1": _state_fields(
        "checkpoint.capture_policy_state",
        "checkpoint.coincident_request_state",
        "checkpoint.completed_count",
        "checkpoint.next_time_us",
        "checkpoint.pending_request_state",
        "checkpoint.sequence_allocator_state",
    ),
    "COMPONENT_LOCAL_ALLOCATORS_V1": _state_fields(
        "runtime.non_state_component_local_event_sequence_allocators",
        "runtime.order_id_allocator_state",
    ),
    "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1": _state_fields(
        "continuous.active_order_index",
        "continuous.resting_priority_allocator_state",
        "continuous.seen_order_ids",
        "venue_truth.continuous.event_journal",
        "venue_truth.continuous.fill_records",
        "venue_truth.continuous.fifo_price_levels",
        "venue_truth.continuous.order_history",
        "venue_truth.continuous.player_fill_history",
        "venue_truth.continuous.player_position",
        "venue_truth.continuous.trade_records",
    ),
    "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1": _state_fields(
        "state.component_local_sequence",
        "state.current_day",
        "state.current_local",
        "state.day_elapsed_age_us",
        "state.day_entered_time_us",
        "state.day_next_eligible_transition_id",
        "state.day_sampled_deadline_us",
        "state.day_sampled_duration_us",
        "state.day_trigger_memory",
        "state.local_elapsed_age_us",
        "state.local_entered_time_us",
        "state.local_next_eligible_transition_id",
        "state.local_sampled_deadline_us",
        "state.local_sampled_duration_us",
        "state.local_trigger_memory",
    ),
    "FEATURES_V1": _state_fields(
        "features.engine_id_version",
        "features.information_cutoffs",
        "features.initialized",
        "features.last_observation_time_us",
        "features.provenance",
        "features.retained_windows",
    ),
    "FLOW_HAWKES_V1": _state_fields(
        "flow.hawkes.diagnostic_draw_sequence",
        "flow.hawkes.excitation_state",
        "flow.hawkes.intensity_state",
        "flow.hawkes.last_decay_time_us",
        "flow.hawkes.model_id_version",
        "flow.hawkes.observation_cutoff",
        "flow.hawkes.pending_proposal",
        "flow.hawkes.proposal_sequence",
        "flow.hawkes.rejection_state",
        "flow.hawkes.rng_state",
    ),
    "FLOW_QUEUE_REACTIVE_V1": _state_fields(
        "flow.queue_reactive.diagnostic_draw_sequence",
        "flow.queue_reactive.model_id_version",
        "flow.queue_reactive.observation_cutoff",
        "flow.queue_reactive.pending_proposal",
        "flow.queue_reactive.proposal_sequence",
        "flow.queue_reactive.rejection_state",
        "flow.queue_reactive.retained_windows",
        "flow.queue_reactive.rng_state",
    ),
    "FLOW_SIMPLE_V1": _state_fields(
        "flow.simple.diagnostic_draw_sequence",
        "flow.simple.intensity_state",
        "flow.simple.model_id_version",
        "flow.simple.observation_cutoff",
        "flow.simple.pending_proposal",
        "flow.simple.proposal_sequence",
        "flow.simple.rejection_state",
        "flow.simple.rng_state",
    ),
    "GLOBAL_EVENT_ALLOCATOR_V1": _state_fields(
        "global_event_allocator.next_sequence"
    ),
    "HIDDEN_LIQUIDITY_V1": _state_fields(
        "hidden.venue_arrival_sequence_allocators",
        "hidden.venue_clock_states",
        "hidden.venue_completion_states",
        "hidden.venue_observable_event_prefixes",
        "hidden.venue_order_records",
        "hidden.venue_pending_observable_ordinals",
        "hidden.venue_pending_observable_queues",
        "hidden.venue_player_ledgers",
        "hidden.venue_priority_sequence_allocators",
        "hidden.venue_public_tapes",
        "hidden.venue_published_feed_states",
        "hidden.venue_rules",
        "hidden.venue_seen_order_ids",
        "hidden.venue_strategy_event_prefixes",
        "hidden.venue_trade_sequence_allocators",
        "hidden.venue_truth_event_prefixes",
    ),
    "HISTORICAL_REPLAY_CURSOR_V1": _state_fields(
        "historical.cursor",
        "historical.dataset_identity",
        "historical.event_prefix",
    ),
    "LEDGER_PREFIX_V1": _state_fields(
        "ledger.event_prefix_last_global_sequence",
        "ledger.event_prefix_sha256",
    ),
    "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1": _state_fields(
        "mechanics.arrival_allocator_state",
        "mechanics.command_allocator_state",
        "mechanics.event_allocator_state",
        "mechanics.event_prefix",
        "mechanics.instrument_rules",
        "mechanics.last_trade",
        "mechanics.managed_order_records",
        "mechanics.session_schedule_cursor",
        "mechanics.session_state",
    ),
    "MULTIVENUE_V1": _state_fields(
        "multivenue.complete",
        "multivenue.consolidated_feed_state",
        "multivenue.coordinator_event_ledger",
        "multivenue.coordinator_event_sequence",
        "multivenue.current_time_us",
        "multivenue.depth_subscriptions",
        "multivenue.global_player_position",
        "multivenue.observable_cursor",
        "multivenue.pending_route_legs",
        "multivenue.route_records",
        "multivenue.route_sequence_allocator_state",
        "multivenue.schedule_sequence_allocator_state",
        "multivenue.seed",
        "multivenue.truth_cursor",
        "multivenue.venue_configs",
        "multivenue.venue_latency_rng_states",
        "multivenue.venue_routing_states",
        "multivenue.venue_session_states",
        "multivenue.venue_working_order_records",
    ),
    "OBSERVABLE_PUBLICATION_CURSOR_V1": _state_fields(
        "observable.client_publication_cursor",
        "observable.last_published_global_sequence",
        "observable.publication_time_us",
    ),
    "PARTICIPANT_SCHEDULE_RUNTIME_V1": _state_fields(
        "participant_schedule.next_index",
        "participant_schedule.replacement_generation",
        "participant_schedule.spec_version_bindings",
    ),
    "PENDING_EVENT_QUEUES_V1": _state_fields(
        "pending_event.causal_parent_by_work_id",
        "pending_event.payloads_by_work_id",
    ),
    "PENDING_LATENCY_CLIENT_DELIVERY_V1": _state_fields(
        "delivery.client_delivery_work_ids",
        "delivery.client_known_working_orders",
        "delivery.client_timestamps",
        "delivery.delivered_message_cursor",
        "delivery.fill_report_messages",
        "delivery.latency.profile_id_version",
        "delivery.latency.rng_state",
        "delivery.message_allocator_state",
        "delivery.pending_acknowledgements",
        "delivery.pending_cancels",
        "delivery.protocol_order_states",
        "delivery.route_allocator_state",
        "delivery.source_timestamps",
        "delivery.venue_receipt_work_ids",
        "delivery.venue_timestamps",
    ),
    "PLAN_COMPOSITION_IDENTITY_V1": _state_fields(
        "plan.composition_matrix_sha256",
        "plan.composition_profile_id",
        "plan.composition_profile_version",
        "plan.semantic_sha256",
    ),
    "PLAYER_OVERLAY_WORKING_ORDERS_V1": _state_fields(
        "client.decision_information_cutoffs",
        "client.fill_report_consumption_cursor",
        "client.player_action_allocator_state",
        "client.player_action_bindings",
        "client.player_fill_history",
        "client.player_pending_decisions",
        "client.player_position",
        "client.player_working_order_ids",
    ),
    "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1": _state_fields(
        "runtime.derived_seed_registry",
        "runtime.rng_algorithm_codec_version",
        "runtime.rng_states",
        "runtime.root_seed",
        "runtime.substream_policy_version",
    ),
    "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1": _state_fields(
        "halt_reopen.controller_local_sequence",
        "halt_reopen.halt_count",
        "halt_reopen.halt_entered_time_us",
        "halt_reopen.maximum_resume_deadline_us",
        "halt_reopen.minimum_resume_eligible_time_us",
        "halt_reopen.pending_transition_work_id",
        "halt_reopen.reopening_auction_end_time_us",
        "halt_reopen.reopening_auction_state",
        "halt_reopen.trigger_memory",
        "scheduled_event.next_index",
        "scheduled_event.state",
        "shock.accepted_count",
        "shock.candidate_draw_count",
        "shock.last_accepted_time_us",
        "shock.proposal_sequence",
        "shock.rejected_count",
    ),
    "SCHEDULED_WORK_QUEUE_V1": _state_fields(
        "scheduled_work.dequeued_count",
        "scheduled_work.pending_heap",
    ),
    "SIMULATION_CLOCK_V1": _state_fields("full_day.current_time_us"),
    "STRATEGIES_V1": _state_fields(
        "strategy.action_allocator_state",
        "strategy.causal_windows",
        "strategy.decision_allocator_state",
        "strategy.definition_sha256",
        "strategy.event_cursor",
        "strategy.id_version",
        "strategy.pending_actions",
        "strategy.state_machine_state",
        "strategy.timers",
        "strategy.traffic_light_state",
    ),
}

_SEMANTIC_FIELD_FAMILY_ALIASES = {
    "active_rng_states": "rng_state",
    "agent.activation_state": "participant_activation_state",
    "agent.rng_states": "rng_state",
    "algorithm.action_allocator_state": "action_allocator_state",
    "algorithm.current_time_us": "simulation_clock",
    "client.player_action_allocator_state": "action_allocator_state",
    "client.player_fill_history": "player_fill_history",
    "client.player_position": "player_position",
    "client_observable_cursor": "publication_cursor",
    "delivery.latency.rng_state": "rng_state",
    "delivery.message_allocator_state": "message_allocator_state",
    "delivery.route_allocator_state": "route_allocator_state",
    "flow.hawkes.diagnostic_draw_sequence": "flow_diagnostic_draw_sequence",
    "flow.hawkes.intensity_state": "flow_intensity_state",
    "flow.hawkes.model_id_version": "flow_model_identity",
    "flow.hawkes.observation_cutoff": "flow_observation_cutoff",
    "flow.hawkes.pending_proposal": "flow_pending_proposal",
    "flow.hawkes.rejection_state": "flow_rejection_state",
    "flow.hawkes.rng_state": "rng_state",
    "flow.queue_reactive.diagnostic_draw_sequence": "flow_diagnostic_draw_sequence",
    "flow.queue_reactive.model_id_version": "flow_model_identity",
    "flow.queue_reactive.observation_cutoff": "flow_observation_cutoff",
    "flow.queue_reactive.pending_proposal": "flow_pending_proposal",
    "flow.queue_reactive.rejection_state": "flow_rejection_state",
    "flow.queue_reactive.rng_state": "rng_state",
    "flow.simple.diagnostic_draw_sequence": "flow_diagnostic_draw_sequence",
    "flow.simple.intensity_state": "flow_intensity_state",
    "flow.simple.model_id_version": "flow_model_identity",
    "flow.simple.observation_cutoff": "flow_observation_cutoff",
    "flow.simple.pending_proposal": "flow_pending_proposal",
    "flow.simple.rejection_state": "flow_rejection_state",
    "flow.simple.rng_state": "rng_state",
    "full_day.current_time_us": "simulation_clock",
    "message_allocator_state": "message_allocator_state",
    "multivenue.current_time_us": "simulation_clock",
    "multivenue.global_player_position": "player_position",
    "multivenue.route_sequence_allocator_state": "route_allocator_state",
    "next_component_local_sequences": "component_local_sequence_allocator",
    "observable.client_publication_cursor": "publication_cursor",
    "observable_publication_cursor": "publication_cursor",
    "participant_statuses": "participant_activation_state",
    "pending_proposal": "flow_pending_proposal",
    "pending_work_heap": "scheduled_work_heap",
    "rng_states": "rng_state",
    "route_allocator_state": "route_allocator_state",
    "runtime.component_local_event_sequence_allocators": (
        "component_local_sequence_allocator"
    ),
    "runtime.non_state_component_local_event_sequence_allocators": (
        "component_local_sequence_allocator"
    ),
    "runtime.rng_states": "rng_state",
    "scheduled_work.pending_heap": "scheduled_work_heap",
    "state.completed_transition_count": "component_local_sequence_allocator",
    "state.component_local_sequence": "component_local_sequence_allocator",
    "strategy.action_allocator_state": "action_allocator_state",
    "venue_truth.auction.player_fill_history": "player_fill_history",
    "venue_truth.auction.player_position": "player_position",
    "venue_truth.continuous.player_fill_history": "player_fill_history",
    "venue_truth.continuous.player_position": "player_position",
}


def _owned_field_members(
    *members: tuple[str, str]
) -> frozenset[tuple[str, str]]:
    return frozenset(members)


_INTENTIONALLY_DISTINCT_FIELD_FAMILIES = {
    "action_allocator_state": _owned_field_members(
        ("ALGORITHMS_DEADLINES_CHILD_ORDERS_V1", "algorithm.action_allocator_state"),
        ("PLAYER_OVERLAY_WORKING_ORDERS_V1", "client.player_action_allocator_state"),
        ("STRATEGIES_V1", "strategy.action_allocator_state"),
    ),
    "component_local_sequence_allocator": _owned_field_members(
        (
            "COMPONENT_LOCAL_ALLOCATORS_V1",
            "runtime.non_state_component_local_event_sequence_allocators",
        ),
        (
            "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1",
            "state.component_local_sequence",
        ),
    ),
    "flow_diagnostic_draw_sequence": _owned_field_members(
        ("FLOW_HAWKES_V1", "flow.hawkes.diagnostic_draw_sequence"),
        (
            "FLOW_QUEUE_REACTIVE_V1",
            "flow.queue_reactive.diagnostic_draw_sequence",
        ),
        ("FLOW_SIMPLE_V1", "flow.simple.diagnostic_draw_sequence"),
    ),
    "flow_intensity_state": _owned_field_members(
        ("FLOW_HAWKES_V1", "flow.hawkes.intensity_state"),
        ("FLOW_SIMPLE_V1", "flow.simple.intensity_state"),
    ),
    "flow_model_identity": _owned_field_members(
        ("FLOW_HAWKES_V1", "flow.hawkes.model_id_version"),
        ("FLOW_QUEUE_REACTIVE_V1", "flow.queue_reactive.model_id_version"),
        ("FLOW_SIMPLE_V1", "flow.simple.model_id_version"),
    ),
    "flow_observation_cutoff": _owned_field_members(
        ("FLOW_HAWKES_V1", "flow.hawkes.observation_cutoff"),
        (
            "FLOW_QUEUE_REACTIVE_V1",
            "flow.queue_reactive.observation_cutoff",
        ),
        ("FLOW_SIMPLE_V1", "flow.simple.observation_cutoff"),
    ),
    "flow_pending_proposal": _owned_field_members(
        ("FLOW_HAWKES_V1", "flow.hawkes.pending_proposal"),
        ("FLOW_QUEUE_REACTIVE_V1", "flow.queue_reactive.pending_proposal"),
        ("FLOW_SIMPLE_V1", "flow.simple.pending_proposal"),
    ),
    "flow_rejection_state": _owned_field_members(
        ("FLOW_HAWKES_V1", "flow.hawkes.rejection_state"),
        ("FLOW_QUEUE_REACTIVE_V1", "flow.queue_reactive.rejection_state"),
        ("FLOW_SIMPLE_V1", "flow.simple.rejection_state"),
    ),
    "message_allocator_state": _owned_field_members(
        (
            "PENDING_LATENCY_CLIENT_DELIVERY_V1",
            "delivery.message_allocator_state",
        ),
    ),
    "participant_activation_state": _owned_field_members(
        ("AGENT_SCHEDULER_METAORDERS_V1", "agent.activation_state"),
    ),
    "player_fill_history": _owned_field_members(
        (
            "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
            "venue_truth.auction.player_fill_history",
        ),
        (
            "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
            "venue_truth.continuous.player_fill_history",
        ),
        ("PLAYER_OVERLAY_WORKING_ORDERS_V1", "client.player_fill_history"),
    ),
    "player_position": _owned_field_members(
        (
            "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
            "venue_truth.auction.player_position",
        ),
        (
            "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
            "venue_truth.continuous.player_position",
        ),
        ("MULTIVENUE_V1", "multivenue.global_player_position"),
        ("PLAYER_OVERLAY_WORKING_ORDERS_V1", "client.player_position"),
    ),
    "publication_cursor": _owned_field_members(
        (
            "OBSERVABLE_PUBLICATION_CURSOR_V1",
            "observable.client_publication_cursor",
        ),
    ),
    "rng_state": _owned_field_members(
        ("AGENT_SCHEDULER_METAORDERS_V1", "agent.rng_states"),
        ("FLOW_HAWKES_V1", "flow.hawkes.rng_state"),
        ("FLOW_QUEUE_REACTIVE_V1", "flow.queue_reactive.rng_state"),
        ("FLOW_SIMPLE_V1", "flow.simple.rng_state"),
        (
            "PENDING_LATENCY_CLIENT_DELIVERY_V1",
            "delivery.latency.rng_state",
        ),
        ("ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1", "runtime.rng_states"),
    ),
    "route_allocator_state": _owned_field_members(
        ("MULTIVENUE_V1", "multivenue.route_sequence_allocator_state"),
        (
            "PENDING_LATENCY_CLIENT_DELIVERY_V1",
            "delivery.route_allocator_state",
        ),
    ),
    "scheduled_work_heap": _owned_field_members(
        ("SCHEDULED_WORK_QUEUE_V1", "scheduled_work.pending_heap"),
    ),
    "simulation_clock": _owned_field_members(
        ("ALGORITHMS_DEADLINES_CHILD_ORDERS_V1", "algorithm.current_time_us"),
        ("MULTIVENUE_V1", "multivenue.current_time_us"),
        ("SIMULATION_CLOCK_V1", "full_day.current_time_us"),
    ),
}


def validate_checkpoint_owned_state_semantics(
    items: tuple[CheckpointInventoryItemV1, ...],
) -> None:
    """Refuse duplicate or ambiguously renamed authoritative checkpoint state."""

    if type(items) is not tuple or any(
        type(item) is not CheckpointInventoryItemV1 for item in items
    ):
        raise TypeError(
            "checkpoint ownership validation requires inventory-item tuples"
        )
    exact_owners: dict[str, str] = {}
    family_members: dict[str, set[tuple[str, str]]] = {}
    for item in items:
        for field_id in item.owned_state_fields:
            prior_owner = exact_owners.get(field_id)
            if prior_owner is not None:
                raise ValueError(
                    f"checkpoint state field {field_id} is owned by both "
                    f"{prior_owner} and {item.component_id}"
                )
            exact_owners[field_id] = item.component_id
            family = _SEMANTIC_FIELD_FAMILY_ALIASES.get(field_id, field_id)
            family_members.setdefault(family, set()).add(
                (item.component_id, field_id)
            )
    for family, expected_members in _INTENTIONALLY_DISTINCT_FIELD_FAMILIES.items():
        actual_members = frozenset(family_members.get(family, set()))
        if actual_members != expected_members:
            raise ValueError(
                f"checkpoint semantic family {family} has ambiguous ownership"
            )
    unknown_shared_families = {
        family: members
        for family, members in family_members.items()
        if len(members) > 1
        and family not in _INTENTIONALLY_DISTINCT_FIELD_FAMILIES
    }
    if unknown_shared_families:
        raise ValueError(
            "checkpoint semantic families require an exact distinct-ownership "
            f"allowlist: {tuple(sorted(unknown_shared_families))}"
        )


def _inventory_rows() -> tuple[CheckpointInventoryItemV1, ...]:
    rows = (
        (
            "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
            "ENGINE_MARKET_MECHANICS_V1",
            "ALWAYS",
            "ALWAYS",
            ("MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",),
        ),
        (
            "CALENDAR_CURSOR_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("PLAN_COMPOSITION_IDENTITY_V1", "SIMULATION_CLOCK_V1"),
        ),
        (
            "CHECKPOINT_CONTROLLER_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("LEDGER_PREFIX_V1", "SCHEDULED_WORK_QUEUE_V1"),
        ),
        (
            "COMPONENT_LOCAL_ALLOCATORS_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            (),
        ),
        (
            "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
            "ENGINE_MARKET_MECHANICS_V1",
            "ALWAYS",
            "ALWAYS",
            ("COMPONENT_LOCAL_ALLOCATORS_V1",),
        ),
        (
            "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            (
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SIMULATION_CLOCK_V1",
            ),
        ),
        (
            "GLOBAL_EVENT_ALLOCATOR_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            (),
        ),
        (
            "LEDGER_PREFIX_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("GLOBAL_EVENT_ALLOCATOR_V1",),
        ),
        (
            "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
            "ENGINE_MARKET_MECHANICS_V1",
            "ALWAYS",
            "ALWAYS",
            ("COMPONENT_LOCAL_ALLOCATORS_V1", "SIMULATION_CLOCK_V1"),
        ),
        (
            "OBSERVABLE_PUBLICATION_CURSOR_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("LEDGER_PREFIX_V1",),
        ),
        (
            "PARTICIPANT_SCHEDULE_RUNTIME_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("PLAN_COMPOSITION_IDENTITY_V1", "SIMULATION_CLOCK_V1"),
        ),
        (
            "PENDING_EVENT_QUEUES_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("COMPONENT_LOCAL_ALLOCATORS_V1", "SCHEDULED_WORK_QUEUE_V1"),
        ),
        (
            "PLAN_COMPOSITION_IDENTITY_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            (),
        ),
        (
            "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("PLAN_COMPOSITION_IDENTITY_V1",),
        ),
        (
            "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            (
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SIMULATION_CLOCK_V1",
            ),
        ),
        (
            "SCHEDULED_WORK_QUEUE_V1",
            "FULL_DAY_RUNTIME_V1",
            "ALWAYS",
            "ALWAYS",
            ("COMPONENT_LOCAL_ALLOCATORS_V1", "SIMULATION_CLOCK_V1"),
        ),
        ("SIMULATION_CLOCK_V1", "FULL_DAY_RUNTIME_V1", "ALWAYS", "ALWAYS", ()),
        (
            "AGENT_SCHEDULER_METAORDERS_V1",
            "AGENT_SCHEDULER_V1",
            "CONDITIONAL",
            "PLAN.PARTICIPANT_SCHEDULE_NONEMPTY_OR_ANY_INITIAL_ACTIVE",
            (
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                "PARTICIPANT_SCHEDULE_RUNTIME_V1",
                "PENDING_EVENT_QUEUES_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SCHEDULED_WORK_QUEUE_V1",
            ),
        ),
        (
            "ALGORITHMS_DEADLINES_CHILD_ORDERS_V1",
            "EXECUTION_ALGORITHM_V1",
            "CONDITIONAL",
            "COMPONENT.EXECUTION_ALGORITHM_V1.ACTIVE",
            (),
        ),
        (
            "FEATURES_V1",
            "FEATURE_STRATEGY_PLAYER_V1",
            "CONDITIONAL",
            "COMPONENT.FEATURE_STRATEGY_PLAYER_V1.ACTIVE",
            (
                "OBSERVABLE_PUBLICATION_CURSOR_V1",
                "PENDING_LATENCY_CLIENT_DELIVERY_V1",
            ),
        ),
        (
            "FLOW_HAWKES_V1",
            "FLOW_HAWKES_V1",
            "CONDITIONAL",
            "COMPONENT.FLOW_HAWKES_V1.ACTIVE",
            (
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                "PENDING_EVENT_QUEUES_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SIMULATION_CLOCK_V1",
            ),
        ),
        (
            "FLOW_QUEUE_REACTIVE_V1",
            "FLOW_QUEUE_REACTIVE_V1",
            "CONDITIONAL",
            "COMPONENT.FLOW_QUEUE_REACTIVE_V1.ACTIVE",
            (
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                "PENDING_EVENT_QUEUES_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SIMULATION_CLOCK_V1",
            ),
        ),
        (
            "FLOW_SIMPLE_V1",
            "FLOW_SIMPLE_V1",
            "CONDITIONAL",
            "COMPONENT.FLOW_SIMPLE_V1.ACTIVE",
            (
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                "PENDING_EVENT_QUEUES_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SIMULATION_CLOCK_V1",
            ),
        ),
        (
            "HIDDEN_LIQUIDITY_V1",
            "VENUE_MULTIVENUE_HIDDEN_V1",
            "CONDITIONAL",
            "COMPONENT.VENUE_MULTIVENUE_HIDDEN_V1.ACTIVE",
            ("MULTIVENUE_V1",),
        ),
        (
            "HISTORICAL_REPLAY_CURSOR_V1",
            "HISTORICAL_REPLAY_V1",
            "CONDITIONAL",
            "COMPONENT.HISTORICAL_REPLAY_V1.ACTIVE",
            ("PLAN_COMPOSITION_IDENTITY_V1", "SIMULATION_CLOCK_V1"),
        ),
        (
            "MULTIVENUE_V1",
            "VENUE_MULTIVENUE_HIDDEN_V1",
            "CONDITIONAL",
            "COMPONENT.VENUE_MULTIVENUE_HIDDEN_V1.ACTIVE",
            (),
        ),
        (
            "PENDING_LATENCY_CLIENT_DELIVERY_V1",
            "DELIVERY_ASYNC_V1",
            "CONDITIONAL",
            "COMPONENT.DELIVERY_ASYNC_V1.ACTIVE",
            (
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                "OBSERVABLE_PUBLICATION_CURSOR_V1",
                "PENDING_EVENT_QUEUES_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SIMULATION_CLOCK_V1",
            ),
        ),
        (
            "PLAYER_OVERLAY_WORKING_ORDERS_V1",
            "FEATURE_STRATEGY_PLAYER_V1",
            "CONDITIONAL",
            "COMPONENT.FEATURE_STRATEGY_PLAYER_V1.ACTIVE",
            (
                "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                "OBSERVABLE_PUBLICATION_CURSOR_V1",
                "PENDING_LATENCY_CLIENT_DELIVERY_V1",
            ),
        ),
        (
            "STRATEGIES_V1",
            "FEATURE_STRATEGY_PLAYER_V1",
            "CONDITIONAL",
            "COMPONENT.FEATURE_STRATEGY_PLAYER_V1.ACTIVE",
            ("FEATURES_V1", "PLAYER_OVERLAY_WORKING_ORDERS_V1"),
        ),
    )
    row_ids = {row[0] for row in rows}
    if row_ids != set(_STATE_FIELDS_BY_ID):
        raise RuntimeError("checkpoint owned-state manifest does not match inventory rows")
    items = tuple(
        CheckpointInventoryItemV1(
            schema_version=1,
            component_id=component_id,
            state_owner_id=owner,
            state_schema_version=1,
            owned_state_fields=_STATE_FIELDS_BY_ID[component_id],
            presence=presence,
            active_predicate=predicate,
            dependencies=tuple(sorted(dependencies)),
        )
        for component_id, owner, presence, predicate, dependencies in sorted(rows)
    )
    validate_checkpoint_owned_state_semantics(items)
    return items


@dataclass(frozen=True, slots=True)
class CheckpointInventoryV1:
    schema_version: int
    inventory_id: str
    items: tuple[CheckpointInventoryItemV1, ...]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != CHECKPOINT_CONTRACT_SCHEMA_VERSION
        ):
            raise ValueError("CheckpointInventoryV1 schema_version must be 1")
        if self.inventory_id != CHECKPOINT_INVENTORY_ID:
            raise ValueError("checkpoint inventory ID is not canonical")
        if type(self.items) is not tuple:
            raise TypeError("checkpoint items must be an immutable tuple")
        if any(type(item) is not CheckpointInventoryItemV1 for item in self.items):
            raise TypeError("checkpoint items must use CheckpointInventoryItemV1")
        validate_checkpoint_owned_state_semantics(self.items)
        item_ids = tuple(item.component_id for item in self.items)
        if item_ids != tuple(sorted(item_ids)) or len(item_ids) != len(set(item_ids)):
            raise ValueError("checkpoint item IDs must be sorted and unique")
        expected = _inventory_rows()
        if tuple(item.as_dict() for item in self.items) != tuple(
            item.as_dict() for item in expected
        ):
            raise ValueError("checkpoint inventory is incomplete or changed")
        self._validate_dependency_graph()

    def _validate_dependency_graph(self) -> None:
        by_id = {item.component_id: item for item in self.items}
        for item in self.items:
            missing = set(item.dependencies) - set(by_id)
            if missing:
                raise ValueError(
                    f"checkpoint item {item.component_id} has missing dependencies"
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(component_id: str) -> None:
            if component_id in visiting:
                raise ValueError("checkpoint inventory dependencies contain a cycle")
            if component_id in visited:
                return
            visiting.add(component_id)
            for dependency in by_id[component_id].dependencies:
                visit(dependency)
            visiting.remove(component_id)
            visited.add(component_id)

        for component_id in sorted(by_id):
            visit(component_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "inventory_id": self.inventory_id,
            "items": [item.as_dict() for item in self.items],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CheckpointInventoryV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {"inventory_id", "items", "schema_version"},
            "CheckpointInventoryV1",
        )
        items = payload["items"]
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) for item in items
        ):
            raise TypeError("checkpoint items must be an array of objects")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            inventory_id=_exact_string(payload["inventory_id"], "inventory_id"),
            items=tuple(CheckpointInventoryItemV1.from_dict(item) for item in items),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CheckpointInventoryV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def checkpoint_inventory_v1() -> CheckpointInventoryV1:
    return CheckpointInventoryV1(
        schema_version=1,
        inventory_id=CHECKPOINT_INVENTORY_ID,
        items=_inventory_rows(),
    )


def validate_checkpoint_component_state_keys(
    inventory: CheckpointInventoryV1,
    *,
    component_id: str,
    state: Mapping[str, object],
) -> None:
    """Bind a preserved component's top-level state exactly to its frozen row."""

    if type(inventory) is not CheckpointInventoryV1:
        raise TypeError("inventory must be CheckpointInventoryV1")
    selected_id = _exact_string(component_id, "component_id")
    if not isinstance(state, Mapping):
        raise TypeError("preserved component state must be a mapping")
    validate_strict_json(state)
    by_id = {item.component_id: item for item in inventory.items}
    try:
        expected_fields = by_id[selected_id].owned_state_fields
    except KeyError as error:
        raise ValueError("component state names an unknown inventory row") from error
    actual_fields = tuple(sorted(state))
    if any(type(field) is not str for field in state) or actual_fields != expected_fields:
        raise ValueError(
            "preserved component state keys differ from owned_state_fields"
        )


@dataclass(frozen=True, slots=True)
class QuiescentCutV1:
    schema_version: int
    simulation_time_us: int
    microstep: int
    checkpoint_stage_ordinal: int
    last_global_event_sequence: int
    event_prefix_last_global_sequence: int
    event_prefix_sha256: str
    pending_work_count: int
    next_pending_time_us: int | None
    next_pending_microstep: int | None
    due_work_at_or_before_cut: int
    generated_microsteps_complete: bool
    checkpoint_stage_complete: bool
    boundary_complete_at_cut: bool

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != CHECKPOINT_CONTRACT_SCHEMA_VERSION
        ):
            raise ValueError("QuiescentCutV1 schema_version must be 1")
        _exact_int(self.simulation_time_us, "simulation_time_us")
        _exact_int(self.microstep, "microstep")
        if _exact_int(self.checkpoint_stage_ordinal, "checkpoint_stage_ordinal") != 11:
            raise ValueError("checkpoint_stage_ordinal must be 11")
        _exact_int(self.last_global_event_sequence, "last_global_event_sequence")
        _exact_int(
            self.event_prefix_last_global_sequence,
            "event_prefix_last_global_sequence",
        )
        _valid_sha256(self.event_prefix_sha256, "event_prefix_sha256")
        _exact_int(self.pending_work_count, "pending_work_count")
        _exact_int(self.due_work_at_or_before_cut, "due_work_at_or_before_cut")
        for field in (
            "generated_microsteps_complete",
            "checkpoint_stage_complete",
            "boundary_complete_at_cut",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be a boolean")
        if self.pending_work_count == 0:
            if self.next_pending_time_us is not None or self.next_pending_microstep is not None:
                raise ValueError("empty pending work must have a null next key")
        else:
            if self.next_pending_time_us is None or self.next_pending_microstep is None:
                raise ValueError("nonempty pending work must identify its next key")
            _exact_int(self.next_pending_time_us, "next_pending_time_us")
            _exact_int(self.next_pending_microstep, "next_pending_microstep")
        self.validate_quiescent()

    def validate_quiescent(self) -> None:
        if self.due_work_at_or_before_cut != 0:
            raise ValueError("checkpoint cut still has due work")
        if not self.generated_microsteps_complete or not self.checkpoint_stage_complete:
            raise ValueError("checkpoint cut has incomplete generated microsteps")
        if not self.boundary_complete_at_cut:
            raise ValueError("checkpoint cut must follow all due boundary work")
        if self.last_global_event_sequence != self.event_prefix_last_global_sequence:
            raise ValueError("event prefix does not include the last global event")
        if (
            self.next_pending_time_us is not None
            and self.next_pending_time_us <= self.simulation_time_us
        ):
            raise ValueError("queued work exists at or before the checkpoint cut")

    def as_dict(self) -> dict[str, object]:
        return {
            "boundary_complete_at_cut": self.boundary_complete_at_cut,
            "checkpoint_stage_complete": self.checkpoint_stage_complete,
            "checkpoint_stage_ordinal": self.checkpoint_stage_ordinal,
            "due_work_at_or_before_cut": self.due_work_at_or_before_cut,
            "event_prefix_last_global_sequence": self.event_prefix_last_global_sequence,
            "event_prefix_sha256": self.event_prefix_sha256,
            "generated_microsteps_complete": self.generated_microsteps_complete,
            "last_global_event_sequence": self.last_global_event_sequence,
            "microstep": self.microstep,
            "next_pending_microstep": self.next_pending_microstep,
            "next_pending_time_us": self.next_pending_time_us,
            "pending_work_count": self.pending_work_count,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> QuiescentCutV1:
        validate_strict_json(payload)
        fields = {
            "boundary_complete_at_cut",
            "checkpoint_stage_complete",
            "checkpoint_stage_ordinal",
            "due_work_at_or_before_cut",
            "event_prefix_last_global_sequence",
            "event_prefix_sha256",
            "generated_microsteps_complete",
            "last_global_event_sequence",
            "microstep",
            "next_pending_microstep",
            "next_pending_time_us",
            "pending_work_count",
            "schema_version",
            "simulation_time_us",
        }
        _require_exact_fields(payload, fields, "QuiescentCutV1")
        for field in (
            "boundary_complete_at_cut",
            "checkpoint_stage_complete",
            "generated_microsteps_complete",
        ):
            if type(payload[field]) is not bool:
                raise TypeError(f"{field} must be a boolean")
        next_time = payload["next_pending_time_us"]
        next_microstep = payload["next_pending_microstep"]
        if next_time is not None:
            next_time = _exact_int(next_time, "next_pending_time_us")
        if next_microstep is not None:
            next_microstep = _exact_int(next_microstep, "next_pending_microstep")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"], "simulation_time_us"
            ),
            microstep=_exact_int(payload["microstep"], "microstep"),
            checkpoint_stage_ordinal=_exact_int(
                payload["checkpoint_stage_ordinal"], "checkpoint_stage_ordinal"
            ),
            last_global_event_sequence=_exact_int(
                payload["last_global_event_sequence"], "last_global_event_sequence"
            ),
            event_prefix_last_global_sequence=_exact_int(
                payload["event_prefix_last_global_sequence"],
                "event_prefix_last_global_sequence",
            ),
            event_prefix_sha256=_valid_sha256(
                payload["event_prefix_sha256"], "event_prefix_sha256"
            ),
            pending_work_count=_exact_int(
                payload["pending_work_count"], "pending_work_count"
            ),
            next_pending_time_us=next_time,
            next_pending_microstep=next_microstep,
            due_work_at_or_before_cut=_exact_int(
                payload["due_work_at_or_before_cut"], "due_work_at_or_before_cut"
            ),
            generated_microsteps_complete=payload["generated_microsteps_complete"],
            checkpoint_stage_complete=payload["checkpoint_stage_complete"],
            boundary_complete_at_cut=payload["boundary_complete_at_cut"],
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> QuiescentCutV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def validate_checkpoint_capture(
    inventory: CheckpointInventoryV1,
    *,
    cut: QuiescentCutV1,
    active_component_ids: Iterable[str],
    preserved_component_ids: Iterable[str],
    absent_component_ids: Iterable[str],
    capture_scope: CheckpointCaptureScopeV1 = CheckpointCaptureScopeV1.FULL_DAY_RUNTIME,
) -> None:
    if type(inventory) is not CheckpointInventoryV1:
        raise TypeError("inventory must be CheckpointInventoryV1")
    if type(cut) is not QuiescentCutV1:
        raise TypeError("cut must be QuiescentCutV1")
    if type(capture_scope) is not CheckpointCaptureScopeV1:
        raise TypeError("capture_scope must use CheckpointCaptureScopeV1")
    cut.validate_quiescent()
    all_ids = {item.component_id for item in inventory.items}
    always_ids = {
        item.component_id for item in inventory.items if item.presence == "ALWAYS"
    }

    def exact_set(values: Iterable[str], field: str) -> set[str]:
        rows = tuple(values)
        if any(type(item) is not str or not item for item in rows):
            raise TypeError(f"{field} must contain nonempty strings")
        if len(rows) != len(set(rows)):
            raise ValueError(f"{field} contains duplicates")
        return set(rows)

    active = exact_set(active_component_ids, "active_component_ids")
    preserved = exact_set(preserved_component_ids, "preserved_component_ids")
    absent = exact_set(absent_component_ids, "absent_component_ids")
    if (
        capture_scope is CheckpointCaptureScopeV1.FULL_DAY_RUNTIME
        and not always_ids <= active
    ):
        raise ValueError("always-present checkpoint state was marked inactive")
    if active - all_ids or preserved - all_ids or absent - all_ids:
        raise ValueError("checkpoint capture names an unknown inventory item")
    if preserved & absent:
        raise ValueError("checkpoint item cannot be both PRESERVED and ABSENT")
    if preserved | absent != all_ids:
        raise ValueError("checkpoint capture omitted an inventory item")
    if preserved != active:
        raise ValueError("every active item must be PRESERVED and no inactive item may be")
    if absent != all_ids - active:
        raise ValueError("every inactive item must be explicitly ABSENT")
    by_id = {item.component_id: item for item in inventory.items}
    for component_id in active:
        if not set(by_id[component_id].dependencies) <= active:
            raise ValueError("active checkpoint item has an inactive dependency")


@dataclass(frozen=True, slots=True)
class PilotLimitsV1:
    schema_version: int
    manifest_id: str
    manifest_version: int
    semantic_version: int
    pilot_duration_us: int
    max_outer_events: int
    max_pending_work_items: int
    max_microsteps_per_timestamp: int
    max_events_per_timestamp: int
    max_checkpoint_bytes: int
    max_generation_wall_time_ns: int
    max_peak_rss_bytes: int
    semantic_sha256: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != CHECKPOINT_CONTRACT_SCHEMA_VERSION
        ):
            raise ValueError("PilotLimitsV1 schema_version must be 1")
        manifest_version = _exact_int(
            self.manifest_version, "manifest_version", minimum=1
        )
        semantic_version = _exact_int(
            self.semantic_version, "semantic_version", minimum=1
        )
        if semantic_version > manifest_version:
            raise ValueError(
                "pilot semantic_version cannot exceed manifest_version"
            )
        if self.manifest_id != PILOT_LIMITS_ID:
            raise ValueError("pilot limits identity/version is invalid")
        for field in (
            "pilot_duration_us",
            "max_outer_events",
            "max_pending_work_items",
            "max_microsteps_per_timestamp",
            "max_events_per_timestamp",
            "max_checkpoint_bytes",
            "max_generation_wall_time_ns",
            "max_peak_rss_bytes",
        ):
            _exact_int(getattr(self, field), field, minimum=1)
        _valid_sha256(self.semantic_sha256, "semantic_sha256")
        if self.semantic_sha256 != canonical_sha256(self.semantic_identity_dict()):
            raise ValueError("pilot limits semantic_sha256 does not match its identity")
        _valid_sha256(self.manifest_sha256, "manifest_sha256")
        if self.manifest_sha256 != canonical_sha256(self.manifest_identity_dict()):
            raise ValueError("pilot limits manifest_sha256 does not match its identity")

    def semantic_identity_dict(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "max_checkpoint_bytes": self.max_checkpoint_bytes,
            "max_events_per_timestamp": self.max_events_per_timestamp,
            "max_microsteps_per_timestamp": self.max_microsteps_per_timestamp,
            "max_outer_events": self.max_outer_events,
            "max_pending_work_items": self.max_pending_work_items,
            "pilot_duration_us": self.pilot_duration_us,
            "schema_version": self.schema_version,
            "semantic_version": self.semantic_version,
        }

    def manifest_identity_dict(self) -> dict[str, object]:
        return {
            **self.semantic_identity_dict(),
            "manifest_version": self.manifest_version,
            "max_generation_wall_time_ns": self.max_generation_wall_time_ns,
            "max_peak_rss_bytes": self.max_peak_rss_bytes,
            "semantic_sha256": self.semantic_sha256,
        }

    def identity_dict(self) -> dict[str, object]:
        """Return the complete preregistration identity, including operational limits."""

        return self.manifest_identity_dict()

    def as_dict(self) -> dict[str, object]:
        return {**self.manifest_identity_dict(), "manifest_sha256": self.manifest_sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PilotLimitsV1:
        validate_strict_json(payload)
        fields = {
            "manifest_id",
            "manifest_sha256",
            "manifest_version",
            "max_checkpoint_bytes",
            "max_events_per_timestamp",
            "max_generation_wall_time_ns",
            "max_microsteps_per_timestamp",
            "max_outer_events",
            "max_peak_rss_bytes",
            "max_pending_work_items",
            "pilot_duration_us",
            "schema_version",
            "semantic_sha256",
            "semantic_version",
        }
        _require_exact_fields(payload, fields, "PilotLimitsV1")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            manifest_id=_exact_string(payload["manifest_id"], "manifest_id"),
            manifest_version=_exact_int(
                payload["manifest_version"], "manifest_version", minimum=1
            ),
            semantic_version=_exact_int(
                payload["semantic_version"], "semantic_version", minimum=1
            ),
            pilot_duration_us=_exact_int(
                payload["pilot_duration_us"], "pilot_duration_us", minimum=1
            ),
            max_outer_events=_exact_int(
                payload["max_outer_events"], "max_outer_events", minimum=1
            ),
            max_pending_work_items=_exact_int(
                payload["max_pending_work_items"], "max_pending_work_items", minimum=1
            ),
            max_microsteps_per_timestamp=_exact_int(
                payload["max_microsteps_per_timestamp"],
                "max_microsteps_per_timestamp",
                minimum=1,
            ),
            max_events_per_timestamp=_exact_int(
                payload["max_events_per_timestamp"],
                "max_events_per_timestamp",
                minimum=1,
            ),
            max_checkpoint_bytes=_exact_int(
                payload["max_checkpoint_bytes"], "max_checkpoint_bytes", minimum=1
            ),
            max_generation_wall_time_ns=_exact_int(
                payload["max_generation_wall_time_ns"],
                "max_generation_wall_time_ns",
                minimum=1,
            ),
            max_peak_rss_bytes=_exact_int(
                payload["max_peak_rss_bytes"], "max_peak_rss_bytes", minimum=1
            ),
            semantic_sha256=_valid_sha256(
                payload["semantic_sha256"], "semantic_sha256"
            ),
            manifest_sha256=_valid_sha256(
                payload["manifest_sha256"], "manifest_sha256"
            ),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> PilotLimitsV1:
        return cls.from_dict(parse_canonical_json_object(payload))


def load_pilot_limits() -> PilotLimitsV1:
    resource = files("kirby2.full_day").joinpath("pilot_limits.toml")
    raw = resource.read_bytes()
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("pilot_limits.toml is not strict UTF-8 TOML") from error
    if not isinstance(payload, dict):
        raise ValueError("pilot_limits.toml root must be a table")
    return PilotLimitsV1.from_dict(payload)


__all__ = [
    "CHECKPOINT_CONTRACT_SCHEMA_VERSION",
    "CHECKPOINT_INVENTORY_ID",
    "CheckpointCaptureScopeV1",
    "CheckpointInventoryItemV1",
    "CheckpointInventoryV1",
    "PILOT_LIMITS_ID",
    "PilotLimitsV1",
    "QuiescentCutV1",
    "checkpoint_inventory_v1",
    "load_pilot_limits",
    "validate_checkpoint_capture",
    "validate_checkpoint_component_state_keys",
    "validate_checkpoint_owned_state_semantics",
]
