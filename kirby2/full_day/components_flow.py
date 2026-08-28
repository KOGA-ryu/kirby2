"""Typed background-flow proposal owners for the authoritative full-day runtime.

The flow owner may observe immutable public book cuts and propose actions.  It
does not own a clock, exchange, book, order allocator, or gateway; the full-day
runtime interprets every proposal and applies it through MarketMechanicsEngine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.simulation.flow import FlowEventFamily
from kirby2.simulation.flow_models import SimpleFlowModel
from kirby2.simulation.rng import SeededRng

from .components import ComponentSnapshotV1, FullDayComponentAdapterV1
from .composition import (
    FLOW_HAWKES_COMPONENT,
    FLOW_QUEUE_REACTIVE_COMPONENT,
    FLOW_SIMPLE_COMPONENT,
    FULL_DAY_RUNTIME_COMPONENT,
    MECHANICS_COMPONENT,
    component_configured_predicate,
)
from .models import (
    FullDayPlanV1,
    VersionedReferenceV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    validate_strict_json,
)


SIMPLE_FLOW_SCHEMA_VERSION = 1
SIMPLE_FLOW_MODEL_ID = "SIMPLE_POISSON_FLOW_V1"
SIMPLE_FLOW_MODEL_VERSION = 1
SIMPLE_FLOW_RNG_LABEL = "full_day/flow/simple/proposal"


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _exact_string(value, field)


@dataclass(frozen=True, slots=True)
class SimpleFlowConfigurationV1:
    """Integer-only identity for the six-channel simple arrival model.

    Intensities are micro-events per second, so 1_000_000 represents one event
    per second.  This keeps the plan/checkpoint identity free of binary floats.
    """

    schema_version: int
    configuration_id: str
    configuration_version: int
    limit_buy_microevents_per_second: int
    limit_sell_microevents_per_second: int
    market_buy_microevents_per_second: int
    market_sell_microevents_per_second: int
    cancel_bid_microevents_per_second: int
    cancel_ask_microevents_per_second: int
    minimum_quantity: int
    maximum_quantity: int
    minimum_placement_depth_ticks: int
    maximum_placement_depth_ticks: int
    account_id: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SIMPLE_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("simple-flow configuration schema version must be 1")
        _exact_string(self.configuration_id, "configuration_id")
        _exact_int(self.configuration_version, "configuration_version", minimum=1)
        rates = self.intensity_microevents_per_second
        if not any(rates.values()):
            raise ValueError("simple-flow configuration requires a positive intensity")
        _exact_int(self.minimum_quantity, "minimum_quantity", minimum=1)
        _exact_int(
            self.maximum_quantity,
            "maximum_quantity",
            minimum=self.minimum_quantity,
        )
        _exact_int(
            self.minimum_placement_depth_ticks,
            "minimum_placement_depth_ticks",
        )
        _exact_int(
            self.maximum_placement_depth_ticks,
            "maximum_placement_depth_ticks",
            minimum=self.minimum_placement_depth_ticks,
        )
        _exact_string(self.account_id, "account_id")

    @property
    def intensity_microevents_per_second(self) -> dict[FlowEventFamily, int]:
        return {
            FlowEventFamily.LIMIT_BUY: _exact_int(
                self.limit_buy_microevents_per_second,
                "limit_buy_microevents_per_second",
            ),
            FlowEventFamily.LIMIT_SELL: _exact_int(
                self.limit_sell_microevents_per_second,
                "limit_sell_microevents_per_second",
            ),
            FlowEventFamily.MARKET_BUY: _exact_int(
                self.market_buy_microevents_per_second,
                "market_buy_microevents_per_second",
            ),
            FlowEventFamily.MARKET_SELL: _exact_int(
                self.market_sell_microevents_per_second,
                "market_sell_microevents_per_second",
            ),
            FlowEventFamily.CANCEL_BID: _exact_int(
                self.cancel_bid_microevents_per_second,
                "cancel_bid_microevents_per_second",
            ),
            FlowEventFamily.CANCEL_ASK: _exact_int(
                self.cancel_ask_microevents_per_second,
                "cancel_ask_microevents_per_second",
            ),
        }

    @property
    def reference(self) -> VersionedReferenceV1:
        return VersionedReferenceV1(
            self.configuration_id,
            self.configuration_version,
            self.sha256,
        )

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "cancel_ask_microevents_per_second": self.cancel_ask_microevents_per_second,
            "cancel_bid_microevents_per_second": self.cancel_bid_microevents_per_second,
            "configuration_id": self.configuration_id,
            "configuration_version": self.configuration_version,
            "limit_buy_microevents_per_second": self.limit_buy_microevents_per_second,
            "limit_sell_microevents_per_second": self.limit_sell_microevents_per_second,
            "market_buy_microevents_per_second": self.market_buy_microevents_per_second,
            "market_sell_microevents_per_second": self.market_sell_microevents_per_second,
            "maximum_placement_depth_ticks": self.maximum_placement_depth_ticks,
            "maximum_quantity": self.maximum_quantity,
            "minimum_placement_depth_ticks": self.minimum_placement_depth_ticks,
            "minimum_quantity": self.minimum_quantity,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimpleFlowConfigurationV1:
        validate_strict_json(payload)
        expected = {
            "account_id",
            "cancel_ask_microevents_per_second",
            "cancel_bid_microevents_per_second",
            "configuration_id",
            "configuration_version",
            "limit_buy_microevents_per_second",
            "limit_sell_microevents_per_second",
            "market_buy_microevents_per_second",
            "market_sell_microevents_per_second",
            "maximum_placement_depth_ticks",
            "maximum_quantity",
            "minimum_placement_depth_ticks",
            "minimum_quantity",
            "schema_version",
        }
        _require_exact_fields(payload, expected, "SimpleFlowConfigurationV1")
        return cls(**{field: payload[field] for field in expected})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FlowObservationCutV1:
    schema_version: int
    simulation_time_us: int
    best_bid_ticks: int | None
    best_ask_ticks: int | None
    reference_price_ticks: int
    cancellable_bid_order_ids: tuple[str, ...]
    cancellable_ask_order_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("flow observation schema version must be 1")
        _exact_int(self.simulation_time_us, "simulation_time_us")
        for value, field in (
            (self.best_bid_ticks, "best_bid_ticks"),
            (self.best_ask_ticks, "best_ask_ticks"),
        ):
            if value is not None:
                _exact_int(value, field, minimum=1)
        _exact_int(self.reference_price_ticks, "reference_price_ticks", minimum=1)
        for values, field in (
            (self.cancellable_bid_order_ids, "cancellable_bid_order_ids"),
            (self.cancellable_ask_order_ids, "cancellable_ask_order_ids"),
        ):
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise ValueError(f"{field} must be a sorted unique tuple")
            for value in values:
                _exact_string(value, field)

    def as_dict(self) -> dict[str, object]:
        return {
            "best_ask_ticks": self.best_ask_ticks,
            "best_bid_ticks": self.best_bid_ticks,
            "cancellable_ask_order_ids": list(self.cancellable_ask_order_ids),
            "cancellable_bid_order_ids": list(self.cancellable_bid_order_ids),
            "reference_price_ticks": self.reference_price_ticks,
            "schema_version": self.schema_version,
            "simulation_time_us": self.simulation_time_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FlowObservationCutV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "best_ask_ticks",
                "best_bid_ticks",
                "cancellable_ask_order_ids",
                "cancellable_bid_order_ids",
                "reference_price_ticks",
                "schema_version",
                "simulation_time_us",
            },
            "FlowObservationCutV1",
        )
        bids = payload["cancellable_bid_order_ids"]
        asks = payload["cancellable_ask_order_ids"]
        if type(bids) is not list or type(asks) is not list:
            raise TypeError("flow observation cancellable IDs must be arrays")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            simulation_time_us=_exact_int(
                payload["simulation_time_us"], "simulation_time_us"
            ),
            best_bid_ticks=(
                None
                if payload["best_bid_ticks"] is None
                else _exact_int(payload["best_bid_ticks"], "best_bid_ticks", minimum=1)
            ),
            best_ask_ticks=(
                None
                if payload["best_ask_ticks"] is None
                else _exact_int(payload["best_ask_ticks"], "best_ask_ticks", minimum=1)
            ),
            reference_price_ticks=_exact_int(
                payload["reference_price_ticks"],
                "reference_price_ticks",
                minimum=1,
            ),
            cancellable_bid_order_ids=tuple(
                _exact_string(value, "cancellable_bid_order_id") for value in bids
            ),
            cancellable_ask_order_ids=tuple(
                _exact_string(value, "cancellable_ask_order_id") for value in asks
            ),
        )


@dataclass(frozen=True, slots=True)
class SimpleFlowProposalV1:
    schema_version: int
    proposal_id: str
    proposal_sequence: int
    scheduled_time_us: int
    observation_cutoff_us: int
    family: FlowEventFamily
    quantity: int | None
    placement_depth_ticks: int | None
    cancel_target_order_id: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("simple-flow proposal schema version must be 1")
        _exact_string(self.proposal_id, "proposal_id")
        _exact_int(self.proposal_sequence, "proposal_sequence", minimum=1)
        _exact_int(self.scheduled_time_us, "scheduled_time_us")
        _exact_int(self.observation_cutoff_us, "observation_cutoff_us")
        if self.scheduled_time_us < self.observation_cutoff_us:
            raise ValueError("flow proposal cannot precede its observation cut")
        if type(self.family) is not FlowEventFamily:
            raise TypeError("flow proposal family must use FlowEventFamily")
        submit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }
        limit = self.family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
        }
        if submit != (self.quantity is not None):
            raise ValueError("only submit proposals carry quantity")
        if self.quantity is not None:
            _exact_int(self.quantity, "quantity", minimum=1)
        if limit != (self.placement_depth_ticks is not None):
            raise ValueError("only limit proposals carry placement depth")
        if self.placement_depth_ticks is not None:
            _exact_int(self.placement_depth_ticks, "placement_depth_ticks")
        if submit and self.cancel_target_order_id is not None:
            raise ValueError("submit proposal cannot carry a cancellation target")
        _optional_string(self.cancel_target_order_id, "cancel_target_order_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "cancel_target_order_id": self.cancel_target_order_id,
            "family": self.family.value,
            "observation_cutoff_us": self.observation_cutoff_us,
            "placement_depth_ticks": self.placement_depth_ticks,
            "proposal_id": self.proposal_id,
            "proposal_sequence": self.proposal_sequence,
            "quantity": self.quantity,
            "scheduled_time_us": self.scheduled_time_us,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimpleFlowProposalV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "cancel_target_order_id",
                "family",
                "observation_cutoff_us",
                "placement_depth_ticks",
                "proposal_id",
                "proposal_sequence",
                "quantity",
                "scheduled_time_us",
                "schema_version",
            },
            "SimpleFlowProposalV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            proposal_id=_exact_string(payload["proposal_id"], "proposal_id"),
            proposal_sequence=_exact_int(
                payload["proposal_sequence"], "proposal_sequence", minimum=1
            ),
            scheduled_time_us=_exact_int(
                payload["scheduled_time_us"], "scheduled_time_us"
            ),
            observation_cutoff_us=_exact_int(
                payload["observation_cutoff_us"], "observation_cutoff_us"
            ),
            family=FlowEventFamily(_exact_string(payload["family"], "family")),
            quantity=(
                None
                if payload["quantity"] is None
                else _exact_int(payload["quantity"], "quantity", minimum=1)
            ),
            placement_depth_ticks=(
                None
                if payload["placement_depth_ticks"] is None
                else _exact_int(
                    payload["placement_depth_ticks"], "placement_depth_ticks"
                )
            ),
            cancel_target_order_id=_optional_string(
                payload["cancel_target_order_id"], "cancel_target_order_id"
            ),
        )


class SimpleFlowOwnerV1:
    """Restorable proposal state with one labeled, component-owned RNG."""

    COMPONENT_ID = FLOW_SIMPLE_COMPONENT

    def __init__(
        self,
        plan: FullDayPlanV1,
        configuration: SimpleFlowConfigurationV1,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("simple-flow owner requires FullDayPlanV1")
        if type(configuration) is not SimpleFlowConfigurationV1:
            raise TypeError("simple-flow owner requires SimpleFlowConfigurationV1")
        self.configuration = configuration
        self.model = SimpleFlowModel()
        self.rng_label = SIMPLE_FLOW_RNG_LABEL
        self.rng = SeededRng(plan.seed_policy.derive(self.rng_label))
        self.last_observation: FlowObservationCutV1 | None = None
        self.pending_proposal: SimpleFlowProposalV1 | None = None
        self.proposal_sequence = 0
        self.diagnostic_draw_sequence: list[dict[str, object]] = []
        self.applied_count = 0
        self.rejected_count = 0
        self.last_rejection_reason: str | None = None
        self._validate_plan_binding(plan)
        self.assert_invariants(plan)

    def _validate_plan_binding(self, plan: FullDayPlanV1) -> None:
        try:
            references = plan.configurations_for_component(self.COMPONENT_ID)
        except KeyError as error:
            raise ValueError("simple-flow configuration is absent from the plan") from error
        if references != (self.configuration.reference,):
            raise ValueError("simple-flow configuration differs from the plan binding")
        if self.rng_label not in {
            row.semantic_path for row in plan.seed_policy.substreams
        }:
            raise ValueError("simple-flow RNG label is undeclared")

    def _baseline_intensities(self) -> dict[FlowEventFamily, float]:
        return {
            family: value / 1_000_000.0
            for family, value in self.configuration.intensity_microevents_per_second.items()
        }

    def plan_next(
        self,
        observation: FlowObservationCutV1,
        *,
        horizon_us: int,
    ) -> SimpleFlowProposalV1 | None:
        if type(observation) is not FlowObservationCutV1:
            raise TypeError("simple flow requires a typed observation cut")
        _exact_int(horizon_us, "horizon_us")
        if self.pending_proposal is not None:
            raise RuntimeError("simple flow already owns a pending proposal")
        if observation.simulation_time_us > horizon_us:
            raise ValueError("flow observation lies beyond the horizon")
        before = self.rng.state_sha256()
        arrival = self.model.schedule_next(
            observation.simulation_time_us,
            self._baseline_intensities(),
            self.rng,
        )
        self.last_observation = observation
        draw: dict[str, object] = {
            "draw_sequence": len(self.diagnostic_draw_sequence) + 1,
            "observation_cutoff_us": observation.simulation_time_us,
            "rng_state_before_sha256": before,
        }
        if arrival is None or arrival.simulation_time_us > horizon_us:
            draw.update(
                {
                    "family": None if arrival is None else arrival.family.value,
                    "outcome": "NO_POSITIVE_INTENSITY" if arrival is None else "OUT_OF_HORIZON",
                    "scheduled_time_us": (
                        None if arrival is None else arrival.simulation_time_us
                    ),
                }
            )
            draw["rng_state_after_sha256"] = self.rng.state_sha256()
            self.diagnostic_draw_sequence.append(draw)
            return None

        family = arrival.family
        quantity: int | None = None
        placement_depth: int | None = None
        cancel_target: str | None = None
        if family in {
            FlowEventFamily.LIMIT_BUY,
            FlowEventFamily.LIMIT_SELL,
            FlowEventFamily.MARKET_BUY,
            FlowEventFamily.MARKET_SELL,
        }:
            quantity = self.rng.integer(
                self.configuration.minimum_quantity,
                self.configuration.maximum_quantity,
            )
        if family in {FlowEventFamily.LIMIT_BUY, FlowEventFamily.LIMIT_SELL}:
            placement_depth = self.rng.integer(
                self.configuration.minimum_placement_depth_ticks,
                self.configuration.maximum_placement_depth_ticks,
            )
        if family is FlowEventFamily.CANCEL_BID:
            candidates = observation.cancellable_bid_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[min(int(selection_draw * len(candidates)), len(candidates) - 1)]
            )
        elif family is FlowEventFamily.CANCEL_ASK:
            candidates = observation.cancellable_ask_order_ids
            selection_draw = self.rng.unit_interval()
            cancel_target = (
                None
                if not candidates
                else candidates[min(int(selection_draw * len(candidates)), len(candidates) - 1)]
            )

        sequence = self.proposal_sequence + 1
        proposal = SimpleFlowProposalV1(
            schema_version=1,
            proposal_id=f"FLOW-SIMPLE-P-{sequence:010d}",
            proposal_sequence=sequence,
            scheduled_time_us=arrival.simulation_time_us,
            observation_cutoff_us=observation.simulation_time_us,
            family=family,
            quantity=quantity,
            placement_depth_ticks=placement_depth,
            cancel_target_order_id=cancel_target,
        )
        self.proposal_sequence = sequence
        self.pending_proposal = proposal
        draw.update(
            {
                "cancel_target_order_id": cancel_target,
                "family": family.value,
                "outcome": "PROPOSAL_CREATED",
                "placement_depth_ticks": placement_depth,
                "proposal_id": proposal.proposal_id,
                "quantity": quantity,
                "scheduled_time_us": proposal.scheduled_time_us,
            }
        )
        draw["rng_state_after_sha256"] = self.rng.state_sha256()
        self.diagnostic_draw_sequence.append(draw)
        return proposal

    def resolve_pending(self, *, applied: bool, rejection_reason: str | None) -> None:
        if type(applied) is not bool:
            raise TypeError("flow proposal applied state must be boolean")
        proposal = self.pending_proposal
        if proposal is None:
            raise RuntimeError("simple flow has no pending proposal to resolve")
        if applied:
            if rejection_reason is not None:
                raise ValueError("applied flow proposal cannot carry a rejection")
            self.applied_count += 1
        else:
            self.rejected_count += 1
            self.last_rejection_reason = _exact_string(
                rejection_reason, "rejection_reason"
            )
        self.model.observe(proposal.family, proposal.scheduled_time_us)
        self.pending_proposal = None

    def checkpoint_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "configuration": self.configuration.as_dict(),
            "diagnostic_draw_sequence": list(self.diagnostic_draw_sequence),
            "intensity_state": {
                family.value: value
                for family, value in self.configuration.intensity_microevents_per_second.items()
            },
            "model_id_version": {
                "model_id": SIMPLE_FLOW_MODEL_ID,
                "model_version": SIMPLE_FLOW_MODEL_VERSION,
                "runtime_state": self.model.runtime_state(),
            },
            "observation_cutoff": (
                None if self.last_observation is None else self.last_observation.as_dict()
            ),
            "pending_proposal": (
                None if self.pending_proposal is None else self.pending_proposal.as_dict()
            ),
            "proposal_sequence": self.proposal_sequence,
            "rejection_state": {
                "applied_count": self.applied_count,
                "last_rejection_reason": self.last_rejection_reason,
                "rejected_count": self.rejected_count,
            },
            "rng_label": self.rng_label,
            "rng_state": self.rng.runtime_state(),
            "schema_version": SIMPLE_FLOW_SCHEMA_VERSION,
        }
        validate_strict_json(state)
        return state

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.checkpoint_state())

    @classmethod
    def from_checkpoint_state(
        cls,
        payload: Mapping[str, object],
        *,
        plan: FullDayPlanV1,
    ) -> SimpleFlowOwnerV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "configuration",
                "diagnostic_draw_sequence",
                "intensity_state",
                "model_id_version",
                "observation_cutoff",
                "pending_proposal",
                "proposal_sequence",
                "rejection_state",
                "rng_label",
                "rng_state",
                "schema_version",
            },
            "SimpleFlowOwnerV1",
        )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != SIMPLE_FLOW_SCHEMA_VERSION
        ):
            raise ValueError("simple-flow owner schema version is unsupported")
        raw_configuration = payload["configuration"]
        raw_model = payload["model_id_version"]
        raw_rejection = payload["rejection_state"]
        raw_rng = payload["rng_state"]
        if not all(
            isinstance(value, Mapping)
            for value in (raw_configuration, raw_model, raw_rejection, raw_rng)
        ):
            raise TypeError("simple-flow owner nested states must be objects")
        configuration = SimpleFlowConfigurationV1.from_dict(raw_configuration)
        owner = cls(plan, configuration)
        _require_exact_fields(
            raw_model,
            {"model_id", "model_version", "runtime_state"},
            "simple-flow model identity",
        )
        if (
            raw_model["model_id"] != SIMPLE_FLOW_MODEL_ID
            or raw_model["model_version"] != SIMPLE_FLOW_MODEL_VERSION
            or not isinstance(raw_model["runtime_state"], Mapping)
        ):
            raise ValueError("simple-flow model identity is unsupported")
        owner.model = SimpleFlowModel.from_runtime_state(raw_model["runtime_state"])
        if payload["rng_label"] != SIMPLE_FLOW_RNG_LABEL:
            raise ValueError("simple-flow RNG label is unsupported")
        owner.rng = SeededRng.from_runtime_state(raw_rng)
        raw_observation = payload["observation_cutoff"]
        raw_pending = payload["pending_proposal"]
        owner.last_observation = (
            None
            if raw_observation is None
            else FlowObservationCutV1.from_dict(raw_observation)  # type: ignore[arg-type]
        )
        owner.pending_proposal = (
            None
            if raw_pending is None
            else SimpleFlowProposalV1.from_dict(raw_pending)  # type: ignore[arg-type]
        )
        owner.proposal_sequence = _exact_int(
            payload["proposal_sequence"], "proposal_sequence"
        )
        diagnostics = payload["diagnostic_draw_sequence"]
        if type(diagnostics) is not list or any(
            not isinstance(row, Mapping) for row in diagnostics
        ):
            raise TypeError("simple-flow diagnostics must be an object array")
        owner.diagnostic_draw_sequence = [dict(row) for row in diagnostics]
        _require_exact_fields(
            raw_rejection,
            {"applied_count", "last_rejection_reason", "rejected_count"},
            "simple-flow rejection state",
        )
        owner.applied_count = _exact_int(raw_rejection["applied_count"], "applied_count")
        owner.rejected_count = _exact_int(
            raw_rejection["rejected_count"], "rejected_count"
        )
        owner.last_rejection_reason = _optional_string(
            raw_rejection["last_rejection_reason"], "last_rejection_reason"
        )
        expected_intensity = {
            family.value: value
            for family, value in configuration.intensity_microevents_per_second.items()
        }
        if payload["intensity_state"] != expected_intensity:
            raise ValueError("simple-flow intensity state differs from configuration")
        owner.assert_invariants(plan)
        if owner.checkpoint_state() != dict(payload):
            raise ValueError("simple-flow checkpoint is not a canonical fixed point")
        return owner

    def assert_invariants(self, plan: FullDayPlanV1) -> None:
        self._validate_plan_binding(plan)
        expected_seed = plan.seed_policy.derive(self.rng_label)
        if self.rng.seed != expected_seed:
            raise RuntimeError("simple-flow RNG differs from the plan substream")
        if self.proposal_sequence < 0:
            raise RuntimeError("simple-flow proposal sequence is invalid")
        resolved = self.applied_count + self.rejected_count
        pending_count = 0 if self.pending_proposal is None else 1
        if resolved + pending_count != self.proposal_sequence:
            raise RuntimeError("simple-flow proposal lifecycle is not conserved")
        if self.pending_proposal is not None:
            if (
                self.pending_proposal.proposal_sequence != self.proposal_sequence
                or self.last_observation is None
                or self.pending_proposal.observation_cutoff_us
                != self.last_observation.simulation_time_us
            ):
                raise RuntimeError("simple-flow pending proposal differs from its cut")
        for sequence, row in enumerate(self.diagnostic_draw_sequence, start=1):
            if row.get("draw_sequence") != sequence:
                raise RuntimeError("simple-flow diagnostic draw sequence has a gap")
        if self.diagnostic_draw_sequence and (
            self.diagnostic_draw_sequence[-1].get("rng_state_after_sha256")
            != self.rng.state_sha256()
        ):
            raise RuntimeError("simple-flow diagnostic tail differs from RNG state")
        validate_strict_json(self.checkpoint_state())


class SimpleFlowComponentAdapterV1(FullDayComponentAdapterV1):
    component_id = FLOW_SIMPLE_COMPONENT
    active_predicate = component_configured_predicate(FLOW_SIMPLE_COMPONENT)
    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_SIMPLE_COMPONENT}_MODEL_STATE",
                f"{FLOW_SIMPLE_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_SIMPLE_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))
    owned_state_ids = (FLOW_SIMPLE_COMPONENT,)

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("simple-flow adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if type(owner) is not SimpleFlowOwnerV1:
            raise TypeError("simple-flow adapter owner has the wrong type")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        self.restore(snapshot, **context)

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        self._validate_snapshot_header(snapshot)
        plan = context.get("plan")
        if type(plan) is not FullDayPlanV1:
            raise ValueError("simple-flow restore requires the exact plan")
        detached = snapshot.as_dict()["state"]
        if not isinstance(detached, Mapping):  # pragma: no cover - snapshot contract
            raise TypeError("simple-flow snapshot state is not an object")
        return SimpleFlowOwnerV1.from_checkpoint_state(detached, plan=plan)


class _ContractOnlyFlowAdapterV1(FullDayComponentAdapterV1):
    """Declaration row for an E2 adapter that is not executable yet."""

    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    borrowed_resource_ids = tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"}))

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("flow adapter requires FullDayPlanV1")
        return cls.component_id in plan.selected_component_ids

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        raise RuntimeError(f"{self.component_id} remains contract-only")

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        raise RuntimeError(f"{self.component_id} remains contract-only")

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        raise RuntimeError(f"{self.component_id} remains contract-only")


class HawkesFlowComponentAdapterV1(_ContractOnlyFlowAdapterV1):
    component_id = FLOW_HAWKES_COMPONENT
    active_predicate = component_configured_predicate(FLOW_HAWKES_COMPONENT)
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_HAWKES_COMPONENT}_MODEL_STATE",
                f"{FLOW_HAWKES_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_HAWKES_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    owned_state_ids = (FLOW_HAWKES_COMPONENT,)


class QueueReactiveFlowComponentAdapterV1(_ContractOnlyFlowAdapterV1):
    component_id = FLOW_QUEUE_REACTIVE_COMPONENT
    active_predicate = component_configured_predicate(FLOW_QUEUE_REACTIVE_COMPONENT)
    owned_resource_ids = tuple(
        sorted(
            {
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_MODEL_STATE",
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_PENDING_PROPOSAL",
                f"{FLOW_QUEUE_REACTIVE_COMPONENT}_RNG_SUBSTREAM",
            }
        )
    )
    owned_state_ids = (FLOW_QUEUE_REACTIVE_COMPONENT,)


__all__ = [
    "FlowObservationCutV1",
    "HawkesFlowComponentAdapterV1",
    "SIMPLE_FLOW_MODEL_ID",
    "SIMPLE_FLOW_MODEL_VERSION",
    "SIMPLE_FLOW_RNG_LABEL",
    "SimpleFlowComponentAdapterV1",
    "SimpleFlowConfigurationV1",
    "SimpleFlowOwnerV1",
    "SimpleFlowProposalV1",
    "QueueReactiveFlowComponentAdapterV1",
]
