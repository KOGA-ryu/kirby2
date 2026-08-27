"""Immutable contracts for causal counterfactual execution branches."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.exchange import OrderType
from kirby2.immutable import freeze_json, thaw_json
from kirby2.multivenue.models import canonical_sha256
from kirby2.session.bindings import SessionCommand


COUNTERFACTUAL_SCHEMA_VERSION = 1
CAUTIOUS_INTERPRETATION = (
    "This simulated counterfactual is evidence about Kirby2's configured model, "
    "not proof of what the real market would have done."
)


class CounterfactualMode(str, Enum):
    EXOGENOUS_REPLAY = "EXOGENOUS_REPLAY"
    ENDOGENOUS_FORK = "ENDOGENOUS_FORK"

    @classmethod
    def parse(cls, value: str) -> CounterfactualMode:
        aliases = {
            "endogenous": cls.ENDOGENOUS_FORK,
            "exogenous": cls.EXOGENOUS_REPLAY,
        }
        normalized = value.strip().lower().replace("-", "_")
        if normalized in aliases:
            return aliases[normalized]
        return cls(normalized.upper())


class ComponentStatus(str, Enum):
    PRESERVED = "PRESERVED"
    ABSENT = "ABSENT"


@dataclass(frozen=True, slots=True)
class ActionMutation:
    target_action_sequence: int
    expected_command: SessionCommand | None = None
    command: SessionCommand | None = None
    order_type: OrderType | None = None
    price_ticks: int | None = None
    quantity: int | None = None
    venue_id: str | None = None
    timing_delta_us: int = 0
    remove: bool = False
    insert: bool = False
    hotkey_outcome: SessionCommand | None = None

    def __post_init__(self) -> None:
        if type(self.target_action_sequence) is not int or self.target_action_sequence <= 0:
            raise ValueError("mutation target action sequence must be positive")
        if type(self.timing_delta_us) is not int:
            raise TypeError("mutation timing delta must be integer microseconds")
        if self.price_ticks is not None and (
            type(self.price_ticks) is not int or self.price_ticks <= 0
        ):
            raise ValueError("mutation price must be positive integer ticks")
        if self.quantity is not None and (
            type(self.quantity) is not int or self.quantity <= 0
        ):
            raise ValueError("mutation quantity must be a positive integer")
        if self.venue_id is not None and not self.venue_id:
            raise ValueError("mutation venue ID must not be empty")
        if self.remove and (
            self.insert
            or self.command is not None
            or self.order_type is not None
            or self.price_ticks is not None
            or self.quantity is not None
            or self.venue_id is not None
            or self.timing_delta_us != 0
            or self.hotkey_outcome is not None
        ):
            raise ValueError("remove mutation cannot include another mutation field")
        if self.insert and self.expected_command is not None:
            raise ValueError("insert mutation cannot assert an existing command")
        if self.insert and self.command is None and self.hotkey_outcome is None:
            raise ValueError("insert mutation requires a command or hotkey outcome")
        if self.command is not None and self.hotkey_outcome is not None:
            raise ValueError("command and hotkey-outcome mutations are mutually exclusive")
        if not self.remove and not self.insert and all(
            value is None
            for value in (
                self.command,
                self.order_type,
                self.price_ticks,
                self.quantity,
                self.venue_id,
                self.hotkey_outcome,
            )
        ) and self.timing_delta_us == 0:
            raise ValueError("mutation does not change an action")

    def as_dict(self) -> dict[str, object]:
        return {
            "command": None if self.command is None else self.command.value,
            "expected_command": (
                None if self.expected_command is None else self.expected_command.value
            ),
            "hotkey_outcome": (
                None if self.hotkey_outcome is None else self.hotkey_outcome.value
            ),
            "insert": self.insert,
            "order_type": None if self.order_type is None else self.order_type.value,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "remove": self.remove,
            "target_action_sequence": self.target_action_sequence,
            "timing_delta_us": self.timing_delta_us,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class MutationManifest:
    mutations: tuple[ActionMutation, ...]
    information_policy: str = "DECISION_TIME_OBSERVABLES_ONLY"
    schema_version: int = COUNTERFACTUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.mutations:
            raise ValueError("counterfactual branch requires at least one mutation")
        if self.schema_version != COUNTERFACTUAL_SCHEMA_VERSION:
            raise ValueError("unsupported counterfactual mutation schema")
        changed = [
            item.target_action_sequence for item in self.mutations if not item.insert
        ]
        if len(changed) != len(set(changed)):
            raise ValueError("an existing action can have at most one mutation")

    def as_dict(self) -> dict[str, object]:
        return {
            "information_policy": self.information_policy,
            "mutations": [item.as_dict() for item in self.mutations],
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class SnapshotComponent:
    name: str
    status: ComponentStatus
    payload: object | None
    detail: str

    def __post_init__(self) -> None:
        if not self.name or not self.detail:
            raise ValueError("snapshot component name and detail are required")
        if self.status is ComponentStatus.PRESERVED and self.payload is None:
            raise ValueError("preserved snapshot component requires a payload")
        if self.status is ComponentStatus.ABSENT and self.payload is not None:
            raise ValueError("absent snapshot component cannot carry a payload")
        if self.payload is not None:
            object.__setattr__(self, "payload", freeze_json(self.payload))

    @property
    def sha256(self) -> str | None:
        if self.payload is None:
            return None
        return canonical_sha256({"payload": thaw_json(self.payload)})

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "name": self.name,
            "payload": None if self.payload is None else thaw_json(self.payload),
            "sha256": self.sha256,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class BranchSnapshot:
    parent_run_id: str
    fork_time_us: int
    components: tuple[SnapshotComponent, ...]
    schema_version: int = COUNTERFACTUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.parent_run_id.startswith("run-") or self.fork_time_us < 0:
            raise ValueError("branch snapshot parent or time is invalid")
        names = tuple(item.name for item in self.components)
        if len(names) != len(set(names)):
            raise ValueError("branch snapshot component names must be unique")
        required = {
            "agent_state",
            "all_venue_states",
            "exchange_state",
            "feature_windows",
            "flow_model_state",
            "hawkes_decay_state",
            "historical_replay_cursor",
            "pending_latency_messages",
            "player_state",
            "rng_state",
            "simulation_clock",
            "strategy_state",
            "working_orders",
        }
        if set(names) != required:
            raise ValueError("branch snapshot component inventory is incomplete")

    def as_dict(self) -> dict[str, object]:
        return {
            "components": [item.as_dict() for item in self.components],
            "fork_time_us": self.fork_time_us,
            "parent_run_id": self.parent_run_id,
            "schema_version": self.schema_version,
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action_id: str
    source_action_sequence: int | None
    simulation_time_us: int
    command: SessionCommand | None
    input_key: str
    quantity: int | None
    price_ticks: int | None
    venue_id: str
    origin: str
    information_cutoff_us: int

    def as_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "command": None if self.command is None else self.command.value,
            "information_cutoff_us": self.information_cutoff_us,
            "input_key": self.input_key,
            "origin": self.origin,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "simulation_time_us": self.simulation_time_us,
            "source_action_sequence": self.source_action_sequence,
            "venue_id": self.venue_id,
        }


@dataclass(frozen=True, slots=True)
class CounterfactualTimelineEntry:
    sequence: int
    simulation_time_us: int
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("counterfactual timeline payload must be a JSON object")
        object.__setattr__(self, "payload", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "payload": thaw_json(self.payload),
            "sequence": self.sequence,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class FirstDivergence:
    index: int | None
    original: Mapping[str, object] | None
    branch: Mapping[str, object] | None
    explanation: str

    def __post_init__(self) -> None:
        for field_name in ("original", "branch"):
            value = getattr(self, field_name)
            if value is None:
                continue
            frozen = freeze_json(value)
            if not isinstance(frozen, Mapping):
                raise TypeError(f"divergence {field_name} must be a JSON object")
            object.__setattr__(self, field_name, frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": None if self.branch is None else thaw_json(self.branch),
            "explanation": self.explanation,
            "index": self.index,
            "original": (
                None if self.original is None else thaw_json(self.original)
            ),
        }


@dataclass(frozen=True, slots=True)
class CounterfactualOutcome:
    state_sha256: str
    timeline_sha256: str
    metrics: Mapping[str, object]
    timeline: tuple[CounterfactualTimelineEntry, ...]
    invariant_status: str

    def __post_init__(self) -> None:
        frozen = freeze_json(self.metrics)
        if not isinstance(frozen, Mapping):
            raise TypeError("counterfactual metrics must be a JSON object")
        object.__setattr__(self, "metrics", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "invariant_status": self.invariant_status,
            "metrics": thaw_json(self.metrics),
            "state_sha256": self.state_sha256,
            "timeline": [item.as_dict() for item in self.timeline],
            "timeline_sha256": self.timeline_sha256,
        }


@dataclass(frozen=True, slots=True)
class CounterfactualReport:
    parent_run_id: str
    mode: CounterfactualMode
    mutation_manifest: MutationManifest
    snapshot: BranchSnapshot
    snapshot_reconstruction_match: bool
    original: CounterfactualOutcome
    branch: CounterfactualOutcome
    first_divergence: FirstDivergence
    comparison: Mapping[str, object]
    exogenous_reference_path_sha256: str | None
    hindsight_guard: Mapping[str, object]
    cautious_interpretation: str = CAUTIOUS_INTERPRETATION
    schema_version: int = COUNTERFACTUAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("comparison", "hindsight_guard"):
            frozen = freeze_json(getattr(self, field_name))
            if not isinstance(frozen, Mapping):
                raise TypeError(f"counterfactual {field_name} must be a JSON object")
            object.__setattr__(self, field_name, frozen)
        if self.parent_run_id != self.snapshot.parent_run_id:
            raise ValueError("counterfactual report parent linkage is inconsistent")
        if not self.snapshot_reconstruction_match:
            raise ValueError("counterfactual fork snapshot did not reconstruct exactly")
        if self.mode is CounterfactualMode.EXOGENOUS_REPLAY:
            if self.exogenous_reference_path_sha256 is None:
                raise ValueError("exogenous report requires a fixed-path digest")
        elif self.exogenous_reference_path_sha256 is not None:
            raise ValueError("endogenous report cannot claim an exogenous path")
        if self.cautious_interpretation != CAUTIOUS_INTERPRETATION:
            raise ValueError("counterfactual interpretation caveat is mandatory")

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch.as_dict(),
            "cautious_interpretation": self.cautious_interpretation,
            "comparison": thaw_json(self.comparison),
            "exogenous_reference_path_sha256": self.exogenous_reference_path_sha256,
            "first_divergence": self.first_divergence.as_dict(),
            "hindsight_guard": thaw_json(self.hindsight_guard),
            "mode": self.mode.value,
            "mutation_manifest": self.mutation_manifest.as_dict(),
            "original": self.original.as_dict(),
            "parent_run_id": self.parent_run_id,
            "schema_version": self.schema_version,
            "snapshot": self.snapshot.as_dict(),
            "snapshot_reconstruction_match": self.snapshot_reconstruction_match,
        }

    def result_sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class TimingSweepCell:
    timing_delta_us: int
    branch_run_id: str
    report_result_sha256: str
    branch_metrics: Mapping[str, object]
    first_divergence_index: int | None

    def __post_init__(self) -> None:
        frozen = freeze_json(self.branch_metrics)
        if not isinstance(frozen, Mapping):
            raise TypeError("timing-sweep metrics must be a JSON object")
        object.__setattr__(self, "branch_metrics", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "branch_run_id": self.branch_run_id,
            "branch_metrics": thaw_json(self.branch_metrics),
            "first_divergence_index": self.first_divergence_index,
            "report_result_sha256": self.report_result_sha256,
            "timing_delta_us": self.timing_delta_us,
        }


@dataclass(frozen=True, slots=True)
class TimingSweepReport:
    parent_run_id: str
    mode: CounterfactualMode
    action_sequence: int
    cells: tuple[TimingSweepCell, ...]

    def __post_init__(self) -> None:
        expected = (-500_000, -250_000, 0, 250_000, 500_000)
        if tuple(item.timing_delta_us for item in self.cells) != expected:
            raise ValueError("timing sweep must use the canonical five offsets")

    def as_dict(self) -> dict[str, object]:
        return {
            "action_sequence": self.action_sequence,
            "cells": [item.as_dict() for item in self.cells],
            "mode": self.mode.value,
            "parent_run_id": self.parent_run_id,
        }
