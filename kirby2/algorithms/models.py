"""Contracts for observable-only execution algorithms and benchmark evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from kirby2.exchange.models import Side
from kirby2.multivenue import ConsolidatedFeed, RoutePolicy, RouteStyle
from kirby2.multivenue.models import canonical_sha256


ALGORITHM_RECORD_SCHEMA_VERSION = 1
BENCHMARK_RESULT_SCHEMA_VERSION = 1


class AlgorithmName(str, Enum):
    IMMEDIATE_MARKET = "IMMEDIATE_MARKET"
    JOIN_BEST = "JOIN_BEST"
    IMPROVE_ONE_TICK = "IMPROVE_ONE_TICK"
    PASSIVE_PEG = "PASSIVE_PEG"
    TWAP = "TWAP"
    VWAP_PROFILE = "VWAP_PROFILE"
    POV = "POV"
    SWEEP = "SWEEP"
    IMPLEMENTATION_SHORTFALL_ADAPTIVE = "IMPLEMENTATION_SHORTFALL_ADAPTIVE"
    MANUAL_REPLAY = "MANUAL_REPLAY"

    @classmethod
    def parse(cls, value: str) -> AlgorithmName:
        aliases = {
            "adaptive": cls.IMPLEMENTATION_SHORTFALL_ADAPTIVE,
            "implementation_shortfall": cls.IMPLEMENTATION_SHORTFALL_ADAPTIVE,
            "immediate": cls.IMMEDIATE_MARKET,
            "manual": cls.MANUAL_REPLAY,
            "peg": cls.PASSIVE_PEG,
            "vwap": cls.VWAP_PROFILE,
        }
        normalized = value.strip().lower().replace("-", "_")
        if normalized in aliases:
            return aliases[normalized]
        return cls(normalized.upper())


class AlgorithmActionType(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"
    WAIT = "WAIT"
    FINISH = "FINISH"


@dataclass(frozen=True, slots=True)
class ExecutionObjective:
    side: Side
    target_quantity: int
    start_time_us: int
    deadline_us: int
    arrival_midpoint_x2: int

    def __post_init__(self) -> None:
        if not isinstance(self.side, Side):
            raise TypeError("execution objective side must use Side")
        if self.target_quantity <= 0 or self.start_time_us < 0:
            raise ValueError("execution objective quantity or start time is invalid")
        if self.deadline_us <= self.start_time_us or self.arrival_midpoint_x2 <= 0:
            raise ValueError("execution objective deadline or arrival price is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "arrival_midpoint_x2": self.arrival_midpoint_x2,
            "deadline_us": self.deadline_us,
            "side": self.side.value,
            "start_time_us": self.start_time_us,
            "target_quantity": self.target_quantity,
        }


@dataclass(frozen=True, slots=True)
class RiskLimits:
    maximum_child_quantity: int
    maximum_working_quantity: int
    maximum_position: int
    maximum_spread_ticks: int
    price_limit_ticks: int | None = None

    def __post_init__(self) -> None:
        integers = (
            self.maximum_child_quantity,
            self.maximum_working_quantity,
            self.maximum_position,
            self.maximum_spread_ticks,
        )
        if any(type(value) is not int or value <= 0 for value in integers):
            raise ValueError("risk limits must be positive integers")
        if self.price_limit_ticks is not None and (
            type(self.price_limit_ticks) is not int or self.price_limit_ticks <= 0
        ):
            raise ValueError("risk price limit must be positive integer ticks")

    def as_dict(self) -> dict[str, object]:
        return {
            "maximum_child_quantity": self.maximum_child_quantity,
            "maximum_position": self.maximum_position,
            "maximum_spread_ticks": self.maximum_spread_ticks,
            "maximum_working_quantity": self.maximum_working_quantity,
            "price_limit_ticks": self.price_limit_ticks,
        }


@dataclass(frozen=True, slots=True)
class ObservableMarketFeatures:
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    midpoint_x2: int | None
    spread_ticks: int | None
    displayed_bid_quantity: int
    displayed_ask_quantity: int
    observed_interval_volume: int
    cumulative_observed_volume: int
    midpoint_change_x2: int
    expected_volume_profile_bps: tuple[int, ...]

    def __post_init__(self) -> None:
        if min(
            self.displayed_bid_quantity,
            self.displayed_ask_quantity,
            self.observed_interval_volume,
            self.cumulative_observed_volume,
        ) < 0:
            raise ValueError("observable market feature quantities cannot be negative")
        if sum(self.expected_volume_profile_bps) != 10_000:
            raise ValueError("expected volume profile must sum to 10000 basis points")

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_ticks": self.best_bid_ticks,
            "cumulative_observed_volume": self.cumulative_observed_volume,
            "displayed_ask_quantity": self.displayed_ask_quantity,
            "displayed_bid_quantity": self.displayed_bid_quantity,
            "expected_volume_profile_bps": list(self.expected_volume_profile_bps),
            "midpoint_change_x2": self.midpoint_change_x2,
            "midpoint_x2": self.midpoint_x2,
            "observed_interval_volume": self.observed_interval_volume,
            "spread_ticks": self.spread_ticks,
        }


@dataclass(frozen=True, slots=True)
class ClientWorkingOrder:
    venue_id: str
    order_id: str
    side: Side
    price_ticks: int | None
    original_quantity: int
    filled_quantity: int
    remaining_quantity: int
    status: str

    def __post_init__(self) -> None:
        if not self.venue_id or not self.order_id or not isinstance(self.side, Side):
            raise ValueError("client working order identity is invalid")
        if (
            self.original_quantity <= 0
            or self.filled_quantity < 0
            or self.remaining_quantity <= 0
            or self.filled_quantity + self.remaining_quantity
            != self.original_quantity
        ):
            raise ValueError("client working order quantities do not reconcile")
        if self.price_ticks is None or self.price_ticks <= 0:
            raise ValueError("client working order requires a positive tick price")
        if self.status not in {"WORKING", "PARTIALLY_FILLED"}:
            raise ValueError("client working order status is not active")

    def as_dict(self) -> dict[str, object]:
        return {
            "filled_quantity": self.filled_quantity,
            "order_id": self.order_id,
            "original_quantity": self.original_quantity,
            "price_ticks": self.price_ticks,
            "remaining_quantity": self.remaining_quantity,
            "side": self.side.value,
            "status": self.status,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class ClientFill:
    venue_id: str
    trade_id: str
    order_id: str
    side: Side
    price_x2: int
    quantity: int
    received_time_us: int
    observed_midpoint_x2_at_decision: int | None

    def __post_init__(self) -> None:
        if (
            not self.venue_id
            or not self.trade_id
            or not self.order_id
            or not isinstance(self.side, Side)
        ):
            raise ValueError("client fill identity is invalid")
        if self.price_x2 <= 0 or self.quantity <= 0 or self.received_time_us < 0:
            raise ValueError("client fill price, quantity, or time is invalid")
        if (
            self.observed_midpoint_x2_at_decision is not None
            and self.observed_midpoint_x2_at_decision <= 0
        ):
            raise ValueError("client fill decision midpoint is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "observed_midpoint_x2_at_decision": self.observed_midpoint_x2_at_decision,
            "price_x2": self.price_x2,
            "quantity": self.quantity,
            "received_time_us": self.received_time_us,
            "side": self.side.value,
            "trade_id": self.trade_id,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class ClientLatencyState:
    pending_route_count: int
    oldest_pending_age_us: int
    maximum_quote_age_us: int
    expected_route_latency_by_venue_us: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if min(
            self.pending_route_count,
            self.oldest_pending_age_us,
            self.maximum_quote_age_us,
        ) < 0:
            raise ValueError("client latency values cannot be negative")
        venue_ids = tuple(item[0] for item in self.expected_route_latency_by_venue_us)
        if len(venue_ids) != len(set(venue_ids)) or any(
            not venue_id or latency_us < 0
            for venue_id, latency_us in self.expected_route_latency_by_venue_us
        ):
            raise ValueError("client venue-latency inventory is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_route_latency_by_venue_us": [
                {"expected_latency_us": latency, "venue_id": venue}
                for venue, latency in self.expected_route_latency_by_venue_us
            ],
            "maximum_quote_age_us": self.maximum_quote_age_us,
            "oldest_pending_age_us": self.oldest_pending_age_us,
            "pending_route_count": self.pending_route_count,
        }


@dataclass(frozen=True, slots=True)
class ClientVenueState:
    venue_id: str
    best_bid_ticks: int | None
    best_bid_quantity: int
    best_ask_ticks: int | None
    best_ask_quantity: int
    quote_age_us: int
    session_state: str
    expected_fill_probability_bps: int
    taker_fee_micros_per_share: int
    maker_rebate_micros_per_share: int

    def __post_init__(self) -> None:
        if not self.venue_id or not self.session_state:
            raise ValueError("client venue identity or state is invalid")
        if min(
            self.best_bid_quantity,
            self.best_ask_quantity,
            self.quote_age_us,
            self.taker_fee_micros_per_share,
            self.maker_rebate_micros_per_share,
        ) < 0:
            raise ValueError("client venue quantities, latency, or fees cannot be negative")
        if not 0 <= self.expected_fill_probability_bps <= 10_000:
            raise ValueError("client venue fill probability is outside basis-point bounds")
        if (
            self.best_bid_ticks is not None
            and self.best_ask_ticks is not None
            and self.best_bid_ticks >= self.best_ask_ticks
        ):
            raise ValueError("individual client venue quote is locked or crossed")

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_quantity": self.best_ask_quantity,
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_quantity": self.best_bid_quantity,
            "best_bid_ticks": self.best_bid_ticks,
            "expected_fill_probability_bps": self.expected_fill_probability_bps,
            "maker_rebate_micros_per_share": self.maker_rebate_micros_per_share,
            "quote_age_us": self.quote_age_us,
            "session_state": self.session_state,
            "taker_fee_micros_per_share": self.taker_fee_micros_per_share,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class AlgorithmObservation:
    sequence: int
    simulation_time_us: int
    objective: ExecutionObjective
    remaining_quantity: int
    observable_market_features: ObservableMarketFeatures
    working_orders: tuple[ClientWorkingOrder, ...]
    fills: tuple[ClientFill, ...]
    latency_state: ClientLatencyState
    venue_state: tuple[ClientVenueState, ...]
    risk_limits: RiskLimits
    consolidated_feed: ConsolidatedFeed

    def __post_init__(self) -> None:
        if self.sequence <= 0 or self.simulation_time_us < 0:
            raise ValueError("algorithm observation identity is invalid")
        if not 0 <= self.remaining_quantity <= self.objective.target_quantity:
            raise ValueError("algorithm remaining quantity is invalid")
        if self.simulation_time_us < self.objective.start_time_us:
            raise ValueError("algorithm observation precedes its objective")
        if len({order.order_id for order in self.working_orders}) != len(
            self.working_orders
        ):
            raise ValueError("algorithm observation has duplicate working order IDs")
        if any(order.side is not self.objective.side for order in self.working_orders):
            raise ValueError("algorithm working order side differs from its objective")
        if any(fill.side is not self.objective.side for fill in self.fills):
            raise ValueError("algorithm fill side differs from its objective")
        observed_fill_quantity = sum(fill.quantity for fill in self.fills)
        if self.remaining_quantity != max(
            0,
            self.objective.target_quantity - observed_fill_quantity,
        ):
            raise ValueError("algorithm remaining quantity does not reconcile to fills")
        if self.working_quantity > self.remaining_quantity:
            raise ValueError("algorithm working quantity exceeds its remaining objective")
        venue_ids = tuple(venue.venue_id for venue in self.venue_state)
        if len(venue_ids) != len(set(venue_ids)):
            raise ValueError("algorithm observation has duplicate venue IDs")

    @property
    def filled_quantity(self) -> int:
        return self.objective.target_quantity - self.remaining_quantity

    @property
    def working_quantity(self) -> int:
        return sum(order.remaining_quantity for order in self.working_orders)

    @property
    def available_to_submit(self) -> int:
        return max(0, self.remaining_quantity - self.working_quantity)

    def as_dict(self) -> dict[str, object]:
        return {
            "consolidated_feed": self.consolidated_feed.as_dict(),
            "fills": [fill.as_dict() for fill in self.fills],
            "latency_state": self.latency_state.as_dict(),
            "objective": self.objective.as_dict(),
            "observable_market_features": self.observable_market_features.as_dict(),
            "remaining_quantity": self.remaining_quantity,
            "representation": "ALGORITHM_CLIENT_OBSERVATION",
            "risk_limits": self.risk_limits.as_dict(),
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
            "venue_state": [venue.as_dict() for venue in self.venue_state],
            "working_orders": [order.as_dict() for order in self.working_orders],
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class AlgorithmAction:
    action_type: AlgorithmActionType
    reason: str
    quantity: int = 0
    route_policy: RoutePolicy | None = None
    route_style: RouteStyle | None = None
    direct_venue_id: str | None = None
    limit_price_ticks: int | None = None
    maximum_venues: int = 1
    target_order_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, AlgorithmActionType) or not self.reason:
            raise ValueError("algorithm action type and reason are required")
        if self.action_type in {AlgorithmActionType.SUBMIT, AlgorithmActionType.REPLACE}:
            if self.quantity <= 0 or self.route_policy is None or self.route_style is None:
                raise ValueError("submit/replace action requires quantity and routing")
        elif self.quantity != 0 or self.route_policy is not None or self.route_style is not None:
            raise ValueError("non-submit algorithm action cannot carry a new route")
        if self.limit_price_ticks is not None and self.limit_price_ticks <= 0:
            raise ValueError("algorithm price limit must use positive integer ticks")
        if self.maximum_venues <= 0:
            raise ValueError("algorithm maximum venue count must be positive")
        if len(self.target_order_ids) != len(set(self.target_order_ids)) or any(
            not isinstance(order_id, str) or not order_id
            for order_id in self.target_order_ids
        ):
            raise ValueError("algorithm target order IDs must be unique nonempty strings")
        if self.target_order_ids and self.action_type not in {
            AlgorithmActionType.CANCEL,
            AlgorithmActionType.REPLACE,
        }:
            raise ValueError("only cancel/replace actions may carry target order IDs")

    def as_dict(self) -> dict[str, object]:
        return {
            "action_type": self.action_type.value,
            "direct_venue_id": self.direct_venue_id,
            "limit_price_ticks": self.limit_price_ticks,
            "maximum_venues": self.maximum_venues,
            "quantity": self.quantity,
            "reason": self.reason,
            "route_policy": None if self.route_policy is None else self.route_policy.value,
            "route_style": None if self.route_style is None else self.route_style.value,
            "target_order_ids": list(self.target_order_ids),
        }


@dataclass(frozen=True, slots=True)
class AlgorithmParameterManifest:
    algorithm: AlgorithmName
    parameters: Mapping[str, object]
    simulator_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.algorithm, AlgorithmName) or not self.parameters:
            raise ValueError("algorithm manifest requires a name and parameters")
        if self.simulator_only is not True:
            raise ValueError("benchmark algorithm manifests are simulator-only")
        mutable_parameters = _copy_json(self.parameters)
        if not isinstance(mutable_parameters, dict):
            raise ValueError("algorithm parameters must be a JSON object")
        try:
            json.dumps(
                mutable_parameters,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("algorithm parameters must be canonical JSON values") from error
        _validate_algorithm_parameters(self.algorithm, mutable_parameters)
        object.__setattr__(self, "parameters", _freeze_json(mutable_parameters))

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm.value,
            "parameters": _copy_json(self.parameters),
            "simulator_only": self.simulator_only,
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class AlgorithmDecision:
    sequence: int
    simulation_time_us: int
    observation_sha256: str
    observation: dict[str, object]
    manifest_sha256: str
    action: AlgorithmAction
    action_accepted: bool
    rejection_reason: str | None
    resulting_route_id: str | None

    def __post_init__(self) -> None:
        if canonical_sha256(self.observation) != self.observation_sha256:
            raise ValueError("algorithm decision observation digest is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.as_dict(),
            "action_accepted": self.action_accepted,
            "manifest_sha256": self.manifest_sha256,
            "observation": self.observation,
            "observation_sha256": self.observation_sha256,
            "rejection_reason": self.rejection_reason,
            "resulting_route_id": self.resulting_route_id,
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class ExecutionBenchmarkMetrics:
    target_quantity: int
    completed_quantity: int
    completion_bps: int
    average_fill_price_numerator_x2: int
    average_fill_price_denominator: int
    implementation_shortfall_x2_tick_shares: int
    spread_paid_x2_tick_shares: int
    fees_micros: int
    rebates_micros: int
    adverse_selection_x2_tick_shares: int
    market_impact_x2_ticks: int
    elapsed_time_us: int
    cancel_count: int
    fill_uncertainty_quantity: int
    deadline_failure: bool
    risk_rejection_count: int

    def __post_init__(self) -> None:
        nonnegative = (
            self.completed_quantity,
            self.completion_bps,
            self.average_fill_price_numerator_x2,
            self.average_fill_price_denominator,
            self.fees_micros,
            self.rebates_micros,
            self.elapsed_time_us,
            self.cancel_count,
            self.fill_uncertainty_quantity,
            self.risk_rejection_count,
        )
        if self.target_quantity <= 0 or min(nonnegative) < 0:
            raise ValueError("execution benchmark metric quantities are invalid")
        if not 0 <= self.completed_quantity <= self.target_quantity:
            raise ValueError("execution completion is outside objective bounds")
        if self.completion_bps != (
            self.completed_quantity * 10_000 // self.target_quantity
        ):
            raise ValueError("execution completion basis points do not reconcile")
        if self.average_fill_price_denominator != self.completed_quantity:
            raise ValueError("average fill denominator does not reconcile to completion")
        if self.fill_uncertainty_quantity != (
            self.target_quantity - self.completed_quantity
        ):
            raise ValueError("fill uncertainty does not reconcile to completion")
        if type(self.deadline_failure) is not bool:
            raise ValueError("deadline failure metric must be boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "adverse_selection_x2_tick_shares": self.adverse_selection_x2_tick_shares,
            "average_fill_price_denominator": self.average_fill_price_denominator,
            "average_fill_price_numerator_x2": self.average_fill_price_numerator_x2,
            "cancel_count": self.cancel_count,
            "completed_quantity": self.completed_quantity,
            "completion_bps": self.completion_bps,
            "deadline_failure": self.deadline_failure,
            "elapsed_time_us": self.elapsed_time_us,
            "fees_micros": self.fees_micros,
            "fill_uncertainty_quantity": self.fill_uncertainty_quantity,
            "implementation_shortfall_x2_tick_shares": self.implementation_shortfall_x2_tick_shares,
            "market_impact_x2_ticks": self.market_impact_x2_ticks,
            "rebates_micros": self.rebates_micros,
            "risk_rejection_count": self.risk_rejection_count,
            "spread_paid_x2_tick_shares": self.spread_paid_x2_tick_shares,
            "target_quantity": self.target_quantity,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    experiment_id: str
    scenario_names: tuple[str, ...]
    algorithm_manifests: tuple[AlgorithmParameterManifest, ...]
    seeds: tuple[int, ...]
    quantity: int
    duration_us: int
    decision_interval_us: int
    side: Side
    risk_limits: RiskLimits

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.scenario_names:
            raise ValueError("benchmark experiment ID and scenarios are required")
        if len(self.scenario_names) != len(set(self.scenario_names)):
            raise ValueError("benchmark scenarios must be unique")
        names = tuple(item.algorithm for item in self.algorithm_manifests)
        if len(names) < 1 or len(names) != len(set(names)):
            raise ValueError("benchmark algorithm manifests must be unique")
        if len(self.seeds) < 2 or len(self.seeds) != len(set(self.seeds)):
            raise ValueError("benchmark requires at least two unique seeds")
        if self.quantity <= 0 or self.duration_us <= 0 or self.decision_interval_us <= 0:
            raise ValueError("benchmark quantity and timing must be positive")
        if self.duration_us % self.decision_interval_us:
            raise ValueError("benchmark duration must divide into decision intervals")

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm_manifests": [item.as_dict() for item in self.algorithm_manifests],
            "decision_interval_us": self.decision_interval_us,
            "duration_us": self.duration_us,
            "experiment_id": self.experiment_id,
            "quantity": self.quantity,
            "risk_limits": self.risk_limits.as_dict(),
            "scenario_names": list(self.scenario_names),
            "seeds": list(self.seeds),
            "side": self.side.value,
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class BenchmarkRunResult:
    run_id: str
    scenario_name: str
    seed: int
    algorithm: AlgorithmName
    algorithm_manifest_sha256: str
    fork_state_sha256: str
    background_path_sha256: str
    recording_sha256: str
    decision_trace_sha256: str
    replay_verified: bool
    metrics: ExecutionBenchmarkMetrics

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm.value,
            "algorithm_manifest_sha256": self.algorithm_manifest_sha256,
            "background_path_sha256": self.background_path_sha256,
            "decision_trace_sha256": self.decision_trace_sha256,
            "fork_state_sha256": self.fork_state_sha256,
            "metrics": self.metrics.as_dict(),
            "recording_sha256": self.recording_sha256,
            "replay_verified": self.replay_verified,
            "run_id": self.run_id,
            "scenario_name": self.scenario_name,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class ExecutionBenchmarkResult:
    manifest: BenchmarkManifest
    runs: tuple[BenchmarkRunResult, ...]
    aggregate_by_algorithm: tuple[dict[str, object], ...]
    immutable_store_root: str
    result_sha256: str

    @property
    def winner_declaration(self) -> dict[str, object]:
        return {
            "status": "NOT_DECLARED",
            "winner": None,
            "reason": "DESCRIPTIVE_ONLY: outcomes vary by scenario, seed, objective, and parameter manifest",
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "aggregate_by_algorithm": list(self.aggregate_by_algorithm),
            "immutable_store_root": self.immutable_store_root,
            "manifest": self.manifest.as_dict(),
            "manifest_sha256": self.manifest.sha256(),
            "per_seed": [run.as_dict() for run in self.runs],
            "result_sha256": self.result_sha256,
            "schema_version": BENCHMARK_RESULT_SCHEMA_VERSION,
            "winner_declaration": self.winner_declaration,
        }


def _validate_algorithm_parameters(
    algorithm: AlgorithmName,
    parameters: dict[str, object],
) -> None:
    common = {"maximum_slice", "minimum_slice", "price_limit_ticks"}
    specific = {
        AlgorithmName.IMMEDIATE_MARKET: set(),
        AlgorithmName.JOIN_BEST: {"maximum_spread_ticks", "passive_timeout_us"},
        AlgorithmName.IMPROVE_ONE_TICK: {
            "maximum_spread_ticks",
            "passive_timeout_us",
        },
        AlgorithmName.PASSIVE_PEG: {
            "maximum_spread_ticks",
            "passive_timeout_us",
        },
        AlgorithmName.TWAP: {"slice_count"},
        AlgorithmName.VWAP_PROFILE: {"volume_profile_bps"},
        AlgorithmName.POV: {"deadline_buffer_us", "participation_rate_bps"},
        AlgorithmName.SWEEP: {"maximum_venues"},
        AlgorithmName.IMPLEMENTATION_SHORTFALL_ADAPTIVE: {
            "deadline_curve_bps",
            "maximum_spread_ticks",
            "passive_timeout_us",
            "urgency_bps",
        },
        AlgorithmName.MANUAL_REPLAY: {"replay_actions", "replay_provenance"},
    }[algorithm]
    expected = specific if algorithm is AlgorithmName.MANUAL_REPLAY else common | specific
    if set(parameters) != expected:
        missing = sorted(expected - set(parameters))
        unknown = sorted(set(parameters) - expected)
        raise ValueError(
            f"algorithm parameter inventory differs; missing={missing} unknown={unknown}"
        )
    if algorithm is AlgorithmName.MANUAL_REPLAY:
        actions = parameters["replay_actions"]
        if not isinstance(actions, list) or not actions or any(
            not isinstance(item, dict) for item in actions
        ):
            raise ValueError("manual replay actions must be a nonempty object array")
        times = [item.get("elapsed_time_us") for item in actions]
        if any(type(value) is not int or value < 0 for value in times):
            raise ValueError("manual replay elapsed times must be nonnegative integers")
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("manual replay action times must be strictly increasing")
        route_keys = {
            "direct_venue_id",
            "limit_price_ticks",
            "maximum_venues",
            "quantity",
            "route_policy",
            "route_style",
        }
        for item in actions:
            try:
                action_type = AlgorithmActionType(str(item["action_type"]).upper())
            except (KeyError, ValueError) as error:
                raise ValueError("manual replay action type is invalid") from error
            expected = {"action_type", "elapsed_time_us"}
            if action_type in {AlgorithmActionType.SUBMIT, AlgorithmActionType.REPLACE}:
                expected |= route_keys
                if type(item.get("quantity")) is not int or int(item["quantity"]) <= 0:
                    raise ValueError("manual submit/replace quantity must be positive")
            if set(item) - expected:
                raise ValueError("manual replay action contains unsupported fields")
            if action_type in {AlgorithmActionType.SUBMIT, AlgorithmActionType.REPLACE}:
                try:
                    RoutePolicy(str(item.get("route_policy", "SWEEP")).upper())
                    RouteStyle(str(item.get("route_style", "AGGRESSIVE")).upper())
                except ValueError as error:
                    raise ValueError("manual replay routing value is invalid") from error
                direct = item.get("direct_venue_id")
                if direct is not None and (not isinstance(direct, str) or not direct):
                    raise ValueError("manual replay direct venue must be nonempty or null")
                price_limit = item.get("limit_price_ticks")
                if price_limit is not None and (
                    type(price_limit) is not int or price_limit <= 0
                ):
                    raise ValueError("manual replay price limit must use positive integer ticks")
                maximum_venues = item.get("maximum_venues", 3)
                if type(maximum_venues) is not int or maximum_venues <= 0:
                    raise ValueError("manual replay maximum venue count must be positive")
        provenance = parameters["replay_provenance"]
        if not isinstance(provenance, dict):
            raise ValueError("manual replay provenance must be an object")
        required_provenance = {
            "source_sha256",
            "source_type",
            "source_verification",
            "translation_version",
        }
        if not required_provenance <= set(provenance):
            raise ValueError("manual replay provenance inventory is incomplete")
        if not isinstance(provenance["source_type"], str) or not provenance["source_type"]:
            raise ValueError("manual replay source type is invalid")
        if provenance["source_verification"] not in {
            "BUILTIN_CANONICAL",
            "EXACT_SESSION_REPLAY",
        }:
            raise ValueError("manual replay source verification is invalid")
        if (
            provenance["source_type"] == "KIRBY2_PLAYER_SESSION_RECORDING"
            and provenance["source_verification"] != "EXACT_SESSION_REPLAY"
        ):
            raise ValueError("player session recording provenance must be exactly replayed")
        source_digest = provenance["source_sha256"]
        if (
            not isinstance(source_digest, str)
            or len(source_digest) != 64
            or any(character not in "0123456789abcdef" for character in source_digest)
        ):
            raise ValueError("manual replay source digest is invalid")
        if type(provenance["translation_version"]) is not int:
            raise ValueError("manual replay translation version is invalid")
        return
    minimum = parameters["minimum_slice"]
    maximum = parameters["maximum_slice"]
    if (
        type(minimum) is not int
        or type(maximum) is not int
        or minimum <= 0
        or maximum < minimum
    ):
        raise ValueError("algorithm slice bounds are invalid")
    price_limit = parameters["price_limit_ticks"]
    if price_limit is not None and (
        type(price_limit) is not int or price_limit <= 0
    ):
        raise ValueError("algorithm price limit must be positive integer ticks or null")
    for key in (
        "deadline_buffer_us",
        "maximum_spread_ticks",
        "maximum_venues",
        "passive_timeout_us",
        "slice_count",
    ):
        if key in parameters and (
            type(parameters[key]) is not int or int(parameters[key]) <= 0
        ):
            raise ValueError(f"algorithm parameter {key} must be positive")
    participation = parameters.get("participation_rate_bps")
    if participation is not None and (
        type(participation) is not int or not 1 <= participation <= 10_000
    ):
        raise ValueError("participation rate must be 1..10000 basis points")
    urgency = parameters.get("urgency_bps")
    if urgency is not None and (
        type(urgency) is not int or not 0 <= urgency <= 10_000
    ):
        raise ValueError("urgency must be 0..10000 basis points")
    for key in ("volume_profile_bps", "deadline_curve_bps"):
        values = parameters.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not values or any(
            type(value) is not int or value < 0 for value in values
        ):
            raise ValueError(f"algorithm parameter {key} must be nonnegative integers")
        if key == "volume_profile_bps" and sum(values) != 10_000:
            raise ValueError("algorithm volume profile must sum to 10000 basis points")
        if key == "deadline_curve_bps" and (
            values[0] != 0
            or values[-1] != 10_000
            or values != sorted(values)
        ):
            raise ValueError("deadline curve must increase from 0 to 10000 basis points")


def _copy_json(value: object) -> object:
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("algorithm parameter object keys must be strings")
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    raise ValueError("algorithm parameters must contain only JSON values")


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value
