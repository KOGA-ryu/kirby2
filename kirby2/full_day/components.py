"""Fail-closed component adapters for the authoritative full-day runtime.

The composition contracts describe ownership.  This module turns that static
description into a deliberately small executable adapter interface.  Adapters
never discover dependencies or owners from object graphs: every ID is declared,
validated against the selected composition profile, and restored in one explicit
topological order.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from kirby2.immutable import freeze_json, thaw_json

from .composition import (
    AGENT_SCHEDULER_ACTIVE_PREDICATE,
    AGENT_SCHEDULER_COMPONENT,
    FULL_DAY_RUNTIME_COMPONENT,
    MECHANICS_COMPONENT,
    CompositionProfileV1,
    agent_scheduler_is_active,
)
from .models import (
    FullDayPlanV1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)

if TYPE_CHECKING:
    from kirby2.exchange.mechanics_engine import MarketMechanicsEngine
    from kirby2.simulation.clock import SimulationClock


COMPONENT_SNAPSHOT_SCHEMA_VERSION = 1
COMPONENT_ADAPTER_IMPLEMENTATION_VERSION = 1


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty identifier")
    validate_strict_json(value)
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-" for character in value):
        raise ValueError(f"{field} contains a noncanonical character")
    return value


def _identifier_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field} must be an immutable tuple")
    selected = tuple(_identifier(item, f"{field}[{index}]") for index, item in enumerate(value))
    if selected != tuple(sorted(set(selected))):
        raise ValueError(f"{field} must be sorted and unique")
    return selected


def _wire_identifier_tuple(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"serialized {field} must be an array")
    return _identifier_tuple(tuple(value), field)


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    detached = thaw_json(freeze_json(value))
    if type(detached) is not dict:  # pragma: no cover - Mapping checked by caller
        raise TypeError("component state did not detach to a JSON object")
    return detached


@dataclass(frozen=True, slots=True)
class ComponentSnapshotV1:
    """One canonical adapter-owned state object.

    This is an adapter boundary, not the 29-row portable checkpoint envelope.
    The latter can project this object into its frozen inventory rows without
    asking the component to rediscover another component's state.
    """

    schema_version: int
    component_id: str
    component_schema_version: int
    implementation_version: int
    dependencies: tuple[str, ...]
    owned_state_ids: tuple[str, ...]
    state: Mapping[str, object]
    state_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != COMPONENT_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("component snapshot schema version must be 1")
        _identifier(self.component_id, "component_id")
        for field in ("component_schema_version", "implementation_version"):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field} must be a positive integer")
        _identifier_tuple(self.dependencies, "dependencies")
        if self.component_id in self.dependencies:
            raise ValueError("a component snapshot cannot depend on itself")
        if not _identifier_tuple(self.owned_state_ids, "owned_state_ids"):
            raise ValueError("a component snapshot must own checkpoint state")
        if not isinstance(self.state, Mapping):
            raise TypeError("component snapshot state must be a JSON object")
        validate_strict_json(self.state)
        frozen = freeze_json(self.state)
        if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
            raise TypeError("component snapshot state must remain an object")
        if type(self.state_sha256) is not str or len(self.state_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.state_sha256
        ):
            raise ValueError("component snapshot digest must be lowercase SHA-256")
        if canonical_sha256(frozen) != self.state_sha256:
            raise ValueError("component snapshot digest mismatch")
        object.__setattr__(self, "state", frozen)

    @classmethod
    def create(
        cls,
        *,
        component_id: str,
        component_schema_version: int,
        implementation_version: int,
        dependencies: tuple[str, ...],
        owned_state_ids: tuple[str, ...],
        state: Mapping[str, object],
    ) -> ComponentSnapshotV1:
        validate_strict_json(state)
        return cls(
            schema_version=COMPONENT_SNAPSHOT_SCHEMA_VERSION,
            component_id=component_id,
            component_schema_version=component_schema_version,
            implementation_version=implementation_version,
            dependencies=dependencies,
            owned_state_ids=owned_state_ids,
            state=state,
            state_sha256=canonical_sha256(state),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "component_schema_version": self.component_schema_version,
            "dependencies": list(self.dependencies),
            "implementation_version": self.implementation_version,
            "owned_state_ids": list(self.owned_state_ids),
            "schema_version": self.schema_version,
            "state": _plain_mapping(self.state),
            "state_sha256": self.state_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ComponentSnapshotV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "component_id",
                "component_schema_version",
                "dependencies",
                "implementation_version",
                "owned_state_ids",
                "schema_version",
                "state",
                "state_sha256",
            },
            "ComponentSnapshotV1",
        )
        state = payload["state"]
        if not isinstance(state, Mapping):
            raise TypeError("serialized component snapshot state must be an object")
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            component_id=payload["component_id"],  # type: ignore[arg-type]
            component_schema_version=payload["component_schema_version"],  # type: ignore[arg-type]
            implementation_version=payload["implementation_version"],  # type: ignore[arg-type]
            dependencies=_wire_identifier_tuple(payload["dependencies"], "dependencies"),
            owned_state_ids=_wire_identifier_tuple(payload["owned_state_ids"], "owned_state_ids"),
            state=state,
            state_sha256=payload["state_sha256"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> ComponentSnapshotV1:
        return cls.from_dict(parse_canonical_json_object(payload))


class FullDayComponentAdapterV1(ABC):
    """Narrow state-owner interface used by full-day restore."""

    component_id: ClassVar[str]
    component_schema_version: ClassVar[int] = 1
    implementation_version: ClassVar[int] = COMPONENT_ADAPTER_IMPLEMENTATION_VERSION
    active_predicate: ClassVar[str]
    dependencies: ClassVar[tuple[str, ...]]
    owned_resource_ids: ClassVar[tuple[str, ...]]
    borrowed_resource_ids: ClassVar[tuple[str, ...]]
    owned_state_ids: ClassVar[tuple[str, ...]]

    @classmethod
    def validate_declaration(cls) -> None:
        _identifier(cls.component_id, "adapter component_id")
        if type(cls.component_schema_version) is not int or cls.component_schema_version < 1:
            raise ValueError("adapter schema version must be positive")
        if type(cls.implementation_version) is not int or cls.implementation_version < 1:
            raise ValueError("adapter implementation version must be positive")
        _identifier(cls.active_predicate, "adapter active_predicate")
        _identifier_tuple(cls.dependencies, "adapter dependencies")
        _identifier_tuple(cls.owned_resource_ids, "adapter owned resources")
        _identifier_tuple(cls.borrowed_resource_ids, "adapter borrowed resources")
        _identifier_tuple(cls.owned_state_ids, "adapter owned state IDs")
        if cls.component_id in cls.dependencies:
            raise ValueError("adapter cannot depend on itself")
        if set(cls.owned_resource_ids) & set(cls.borrowed_resource_ids):
            raise ValueError("adapter cannot own and borrow one resource")

    @classmethod
    @abstractmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        """Return the frozen active predicate for *plan*."""

    @abstractmethod
    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        """Capture complete component-owned state without mutating *owner*."""

    @abstractmethod
    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        """Validate state and every borrowed-owner identity before restore."""

    @abstractmethod
    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        """Return one detached restored owner."""

    def canonical_digest(self, owner: object) -> str:
        return self.snapshot(owner).state_sha256

    @classmethod
    def _validate_snapshot_header(cls, snapshot: ComponentSnapshotV1) -> None:
        if type(snapshot) is not ComponentSnapshotV1:
            raise TypeError("component adapter requires ComponentSnapshotV1")
        if (
            snapshot.component_id != cls.component_id
            or snapshot.component_schema_version != cls.component_schema_version
            or snapshot.implementation_version != cls.implementation_version
            or snapshot.dependencies != cls.dependencies
            or snapshot.owned_state_ids != cls.owned_state_ids
        ):
            raise ValueError("component snapshot header differs from adapter declaration")


class FullDayRuntimeComponentAdapterV1(FullDayComponentAdapterV1):
    """Adapter for the sole runtime/calendar/allocator owner."""

    component_id = FULL_DAY_RUNTIME_COMPONENT
    active_predicate = "ALWAYS"
    dependencies = ()
    owned_resource_ids = tuple(
        sorted(
            {
                "AUCTION_BOOK",
                "GLOBAL_EVENT_ALLOCATOR",
                "MARKET_MECHANICS_ENGINE",
                "ORDER_ALLOCATOR",
                "ORDER_BOOK",
                "ORDER_GATEWAY",
                "QUIESCENT_CUT_CONTROLLER",
                "RNG_SUBSTREAM_NAMESPACE",
                "SCHEDULING_HEAP",
                "SESSION_CALENDAR",
                "SIMULATION_CLOCK",
            }
        )
    )
    borrowed_resource_ids = ()
    owned_state_ids = tuple(
        sorted(
            {
                "CALENDAR_CURSOR_V1",
                "CHECKPOINT_CONTROLLER_V1",
                "COMPONENT_LOCAL_ALLOCATORS_V1",
                "CURRENT_DAY_LOCAL_STATE_AGES_DEADLINES_TRIGGER_MEMORY_V1",
                "GLOBAL_EVENT_ALLOCATOR_V1",
                "LEDGER_PREFIX_V1",
                "OBSERVABLE_PUBLICATION_CURSOR_V1",
                "PARTICIPANT_SCHEDULE_RUNTIME_V1",
                "PENDING_EVENT_QUEUES_V1",
                "PLAN_COMPOSITION_IDENTITY_V1",
                "ROOT_SEED_DERIVED_LABEL_REGISTRY_ACTIVE_RNG_V1",
                "SCHEDULED_EVENT_SHOCK_HALT_REOPEN_STATE_V1",
                "SCHEDULED_WORK_QUEUE_V1",
                "SIMULATION_CLOCK_V1",
            }
        )
    )

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("runtime adapter requires FullDayPlanV1")
        return True

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        from .runtime import FullDayRuntime

        if type(owner) is not FullDayRuntime:
            raise TypeError("runtime adapter owner must be FullDayRuntime")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=owner.runtime_owner_checkpoint_state(),
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        from .runtime import FullDayRuntime

        self._validate_snapshot_header(snapshot)
        FullDayRuntime.validate_runtime_owner_checkpoint_state(
            _plain_mapping(snapshot.state)
        )

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        from .runtime import FullDayRuntime

        self._validate_snapshot_header(snapshot)
        return FullDayRuntime.restore_runtime_owner_checkpoint_state(
            _plain_mapping(snapshot.state)
        )


class AgentSchedulerComponentAdapterV1(FullDayComponentAdapterV1):
    """Adapter for the engine-injected participant scheduler."""

    component_id = AGENT_SCHEDULER_COMPONENT
    implementation_version = 2
    active_predicate = AGENT_SCHEDULER_ACTIVE_PREDICATE
    dependencies = tuple(sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT}))
    owned_resource_ids = tuple(
        sorted(
            {
                "AGENT_POLICY_STATE",
                "AGENT_RNG_SUBSTREAMS",
                "METAORDER_STATE",
                "PARTICIPANT_ACTIVATION_STATE",
                "PENDING_AGENT_DECISIONS",
            }
        )
    )
    borrowed_resource_ids = tuple(
        sorted({"MARKET_MECHANICS_ENGINE", "ORDER_GATEWAY", "SIMULATION_CLOCK"})
    )
    owned_state_ids = ("AGENT_SCHEDULER_METAORDERS_V1",)

    @classmethod
    def is_active(cls, plan: FullDayPlanV1) -> bool:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("scheduler adapter requires FullDayPlanV1")
        return agent_scheduler_is_active(
            participant_schedule_nonempty=bool(plan.participant_schedule),
            any_participant_initially_active=any(
                participant.initially_active
                for participant in plan.participant_definitions
            ),
        )

    def snapshot(self, owner: object) -> ComponentSnapshotV1:
        if getattr(owner, "COMPONENT_ID", None) != self.component_id:
            raise TypeError("scheduler adapter owner has the wrong component ID")
        state_method = getattr(owner, "checkpoint_state", None)
        if not callable(state_method):
            raise TypeError("scheduler owner has no checkpoint_state method")
        state = state_method()
        if not isinstance(state, Mapping):
            raise TypeError("scheduler checkpoint state must be an object")
        return ComponentSnapshotV1.create(
            component_id=self.component_id,
            component_schema_version=self.component_schema_version,
            implementation_version=self.implementation_version,
            dependencies=self.dependencies,
            owned_state_ids=self.owned_state_ids,
            state=state,
        )

    def validate(self, snapshot: ComponentSnapshotV1, **context: object) -> None:
        restored = self.restore(snapshot, **context)
        state_method = getattr(restored, "canonical_state_bytes", None)
        if not callable(state_method) or state_method() != canonical_json_bytes(snapshot.state):
            raise ValueError("agent scheduler state is not a canonical fixed point")

    def restore(self, snapshot: ComponentSnapshotV1, **context: object) -> object:
        from kirby2.agents.ecology import AgentScheduler
        from .runtime import RuntimeOrderIdAllocatorV1

        self._validate_snapshot_header(snapshot)
        engine = context.get("engine")
        clock = context.get("clock")
        allocator = context.get("order_id_allocator")
        if engine is None or clock is None or not callable(allocator):
            raise ValueError(
                "scheduler restore requires engine, clock, and runtime order allocator"
            )
        if (
            getattr(allocator, "__self__", None) is None
            or type(getattr(allocator, "__self__", None))
            is not RuntimeOrderIdAllocatorV1
            or getattr(allocator, "__func__", None)
            is not RuntimeOrderIdAllocatorV1.allocate
        ):
            raise ValueError("scheduler restore allocator is not a runtime-owned binding")
        state = _plain_mapping(snapshot.state)
        owned = state.get("state")
        if not isinstance(owned, Mapping) or owned.get("allocator_owner") != "INJECTED_RUNTIME":
            raise ValueError("compatibility-wrapper scheduler state is forbidden")
        restored = AgentScheduler.from_checkpoint_state(
            state,
            engine=engine,
            clock=clock,
            order_id_allocator=allocator,
        )
        plan = context.get("plan")
        if plan is not None:
            if type(plan) is not FullDayPlanV1:
                raise TypeError("scheduler adapter plan context must be FullDayPlanV1")
            plan_ids = tuple(
                participant.participant_id
                for participant in plan.participant_definitions
            )
            restored_ids = tuple(
                sorted(spec.agent_id for spec in restored.definition.agents)
            )
            if restored_ids != plan_ids:
                raise ValueError("restored scheduler differs from plan participants")
        return restored


class ComponentAdapterGraphV1:
    """Validated adapter owner graph with an explicit dependency-first order."""

    def __init__(
        self,
        adapters: tuple[FullDayComponentAdapterV1, ...],
        *,
        plan: FullDayPlanV1,
        profile: CompositionProfileV1 | None = None,
    ) -> None:
        if type(plan) is not FullDayPlanV1:
            raise TypeError("component graph requires FullDayPlanV1")
        if type(adapters) is not tuple or any(
            not isinstance(adapter, FullDayComponentAdapterV1) for adapter in adapters
        ):
            raise TypeError("component graph adapters must be an immutable adapter tuple")
        for adapter in adapters:
            adapter.validate_declaration()
        ordered = tuple(sorted(adapters, key=lambda adapter: adapter.component_id))
        ids = tuple(adapter.component_id for adapter in ordered)
        if len(ids) != len(set(ids)):
            raise ValueError("component graph contains a duplicate adapter owner")
        by_id = {adapter.component_id: adapter for adapter in ordered}
        for adapter in ordered:
            missing = set(adapter.dependencies) - set(by_id)
            if missing:
                raise ValueError(
                    f"component {adapter.component_id} has missing dependencies: "
                    + ",".join(sorted(missing))
                )
        resource_owners: dict[str, str] = {}
        state_owners: dict[str, str] = {}
        for adapter in ordered:
            for resource_id in adapter.owned_resource_ids:
                if resource_id in resource_owners:
                    raise ValueError(
                        f"resource {resource_id} has duplicate adapter owners"
                    )
                resource_owners[resource_id] = adapter.component_id
            for state_id in adapter.owned_state_ids:
                if state_id in state_owners:
                    raise ValueError(
                        f"checkpoint state {state_id} has duplicate adapter owners"
                    )
                state_owners[state_id] = adapter.component_id
        for adapter in ordered:
            missing_resources = set(adapter.borrowed_resource_ids) - set(resource_owners)
            if missing_resources:
                raise ValueError(
                    f"component {adapter.component_id} borrows unowned resources: "
                    + ",".join(sorted(missing_resources))
                )
        active = {
            adapter.component_id for adapter in ordered if adapter.is_active(plan)
        }
        for adapter_id in active:
            inactive_dependencies = set(by_id[adapter_id].dependencies) - active
            if inactive_dependencies:
                raise ValueError("an active adapter depends on an inactive adapter")
        if profile is not None:
            self._validate_profile(profile, ordered)
        self._adapters = ordered
        self._by_id = MappingProxyType(by_id)
        self._active_component_ids = tuple(sorted(active))
        self._resource_owners = MappingProxyType(dict(sorted(resource_owners.items())))
        self._state_owners = MappingProxyType(dict(sorted(state_owners.items())))
        self._restore_order = self._topological_order(by_id)

    @staticmethod
    def _validate_profile(
        profile: CompositionProfileV1,
        adapters: tuple[FullDayComponentAdapterV1, ...],
    ) -> None:
        if type(profile) is not CompositionProfileV1:
            raise TypeError("profile must use CompositionProfileV1")
        declared = {component.component_id: component for component in profile.components}
        if set(declared) != {adapter.component_id for adapter in adapters}:
            raise ValueError("adapter graph differs from the exact composition profile")
        for adapter in adapters:
            component = declared[adapter.component_id]
            if (
                component.component_version != adapter.implementation_version
                or component.active_predicate != adapter.active_predicate
                or component.dependencies != adapter.dependencies
                or component.owned_resources != adapter.owned_resource_ids
                or component.borrowed_resources != adapter.borrowed_resource_ids
                or component.checkpoint_state_ids != adapter.owned_state_ids
            ):
                raise ValueError(
                    f"adapter declaration differs from profile for {adapter.component_id}"
                )

    @staticmethod
    def _topological_order(
        by_id: Mapping[str, FullDayComponentAdapterV1],
    ) -> tuple[str, ...]:
        visiting: set[str] = set()
        visited: set[str] = set()
        result: list[str] = []

        def visit(component_id: str) -> None:
            if component_id in visiting:
                raise ValueError("component adapter dependencies contain a cycle")
            if component_id in visited:
                return
            visiting.add(component_id)
            for dependency in by_id[component_id].dependencies:
                visit(dependency)
            visiting.remove(component_id)
            visited.add(component_id)
            result.append(component_id)

        for component_id in sorted(by_id):
            visit(component_id)
        return tuple(result)

    @property
    def adapters(self) -> tuple[FullDayComponentAdapterV1, ...]:
        return self._adapters

    @property
    def active_component_ids(self) -> tuple[str, ...]:
        return self._active_component_ids

    @property
    def restore_order(self) -> tuple[str, ...]:
        return self._restore_order

    @property
    def resource_owners(self) -> Mapping[str, str]:
        return self._resource_owners

    @property
    def state_owners(self) -> Mapping[str, str]:
        return self._state_owners

    def adapter(self, component_id: str) -> FullDayComponentAdapterV1:
        try:
            return self._by_id[component_id]
        except KeyError as error:
            raise ValueError("unknown component adapter") from error

    def validate_snapshots(
        self,
        snapshots: Mapping[str, ComponentSnapshotV1],
        **context_by_component: Mapping[str, object],
    ) -> None:
        if set(snapshots) != set(self.active_component_ids):
            raise ValueError("component snapshot set differs from active adapters")
        for component_id in self.restore_order:
            if component_id not in snapshots:
                continue
            self.adapter(component_id).validate(
                snapshots[component_id],
                **dict(context_by_component.get(component_id, {})),
            )


__all__ = [
    "AgentSchedulerComponentAdapterV1",
    "COMPONENT_ADAPTER_IMPLEMENTATION_VERSION",
    "COMPONENT_SNAPSHOT_SCHEMA_VERSION",
    "ComponentAdapterGraphV1",
    "ComponentSnapshotV1",
    "FullDayComponentAdapterV1",
    "FullDayRuntimeComponentAdapterV1",
]
