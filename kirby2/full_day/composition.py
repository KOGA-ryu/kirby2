"""Append-only full-day composition and ownership contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .models import (
    RNG_LABEL_PREFIXES_BY_COMPONENT_V1,
    _require_exact_fields,
    canonical_json_bytes,
    canonical_sha256,
    parse_canonical_json_object,
    validate_strict_json,
)


COMPOSITION_SCHEMA_VERSION = 1
INITIAL_PROFILE_ID = "SINGLE_VENUE_AGENT_MECHANICS_V1"
FLOW_PROFILE_ID = "SINGLE_VENUE_AGENT_FLOW_V1"
DELIVERY_PROFILE_ID = "SINGLE_VENUE_AGENT_FLOW_DELIVERY_V1"
RESEARCH_PROFILE_ID = "SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1"
MULTIVENUE_HIDDEN_PROFILE_ID = "MULTIVENUE_HIDDEN_RESEARCH_V1"
EXECUTION_ALGORITHM_PROFILE_ID = "STANDALONE_EXECUTION_ALGORITHM_V1"
INITIAL_MATRIX_ID = "COMPOSITION_MATRIX_V1"

FULL_DAY_RUNTIME_COMPONENT = "FULL_DAY_RUNTIME_V1"
MECHANICS_COMPONENT = "ENGINE_MARKET_MECHANICS_V1"
AGENT_SCHEDULER_COMPONENT = "AGENT_SCHEDULER_V1"
FLOW_SIMPLE_COMPONENT = "FLOW_SIMPLE_V1"
FLOW_HAWKES_COMPONENT = "FLOW_HAWKES_V1"
FLOW_QUEUE_REACTIVE_COMPONENT = "FLOW_QUEUE_REACTIVE_V1"
DELIVERY_ASYNC_COMPONENT = "DELIVERY_ASYNC_V1"
FEATURE_STRATEGY_PLAYER_COMPONENT = "FEATURE_STRATEGY_PLAYER_V1"
MULTIVENUE_HIDDEN_COMPONENT = "VENUE_MULTIVENUE_HIDDEN_V1"
EXECUTION_ALGORITHM_COMPONENT = "EXECUTION_ALGORITHM_V1"

FLOW_COMPONENT_IDS = (
    FLOW_HAWKES_COMPONENT,
    FLOW_QUEUE_REACTIVE_COMPONENT,
    FLOW_SIMPLE_COMPONENT,
)

_INITIAL_COMPONENT_IDS = (
    AGENT_SCHEDULER_COMPONENT,
    MECHANICS_COMPONENT,
    FULL_DAY_RUNTIME_COMPONENT,
)
_INITIAL_COMPONENT_IDS_SORTED = tuple(sorted(_INITIAL_COMPONENT_IDS))
_INITIAL_REFUSED_COMPONENT_IDS = tuple(
    sorted(
        (
            "AGENT_ECOLOGY_COMPATIBILITY_WRAPPER",
            "ALGORITHMS",
            "ASYNCHRONOUS_EXECUTION_SESSION",
            "FEATURES",
            "FLOW_HAWKES",
            "FLOW_QUEUE_REACTIVE",
            "FLOW_SIMPLE",
            "HIDDEN_LIQUIDITY",
            "HISTORICAL_REPLAY",
            "MULTIVENUE_ROUTING",
            "PLAYER_OVERLAY",
            "REGIME_ORDER_FLOW",
            "STRATEGIES",
        )
    )
)

_REQUIRED_RUNTIME_RESOURCES = frozenset(
    {
        "GLOBAL_EVENT_ALLOCATOR",
        "MARKET_MECHANICS_ENGINE",
        "ORDER_ALLOCATOR",
        "ORDER_GATEWAY",
        "ORDER_BOOK",
        "AUCTION_BOOK",
        "RNG_SUBSTREAM_NAMESPACE",
        "SCHEDULING_HEAP",
        "SESSION_CALENDAR",
        "SIMULATION_CLOCK",
        "QUIESCENT_CUT_CONTROLLER",
    }
)
_COMPONENT_CONFIGURED_PREDICATE_PREFIX = "PLAN.COMPONENT_CONFIGURED/"
_ALLOWED_STATUSES = frozenset(
    {"CONTRACT_ONLY", "EXECUTABLE", "RESTORABLE_COMPONENT_ONLY", "REFUSED"}
)
_IMPLEMENTATION_STATUS_REASON_CODES = {
    "CONTRACT_ONLY": "IMPLEMENTATION_STATUS_CONTRACT_ONLY",
    "EXECUTABLE": "IMPLEMENTATION_STATUS_EXECUTABLE",
    "RESTORABLE_COMPONENT_ONLY": "IMPLEMENTATION_STATUS_RESTORABLE_COMPONENT_ONLY",
    "REFUSED": "IMPLEMENTATION_STATUS_REFUSED",
}

AGENT_SCHEDULER_ACTIVE_PREDICATE = (
    "PLAN.PARTICIPANT_SCHEDULE_NONEMPTY_OR_ANY_INITIAL_ACTIVE"
)
ABSENT_REASON_COMPONENT_INACTIVE = "COMPOSITION_ACTIVE_PREDICATE_FALSE"
ABSENT_REASON_COMPONENT_REFUSED = "COMPOSITION_PROFILE_REFUSES_COMPONENT"
ABSENT_REASON_SYNTHETIC_NO_HISTORICAL_CURSOR = (
    "SYNTHETIC_PLAN_HAS_NO_HISTORICAL_CURSOR"
)


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _exact_string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    validate_strict_json(value)
    return value


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field} must be a boolean")
    return value


def agent_scheduler_is_active(
    *,
    participant_schedule_nonempty: bool,
    any_participant_initially_active: bool,
) -> bool:
    """Evaluate the frozen scheduler predicate without truthy-value coercion."""

    schedule_nonempty = _exact_bool(
        participant_schedule_nonempty, "participant_schedule_nonempty"
    )
    initially_active = _exact_bool(
        any_participant_initially_active, "any_participant_initially_active"
    )
    return schedule_nonempty or initially_active


def component_configured_predicate(component_id: str) -> str:
    """Return the sole supported plan-binding predicate for a component."""

    component_id = _exact_string(component_id, "component_id")
    if (
        "/" in component_id
        or component_id in {".", ".."}
        or any(token in component_id for token in ("*", "EVERYTHING", "ENABLE_ALL"))
    ):
        raise ValueError("component_id is not valid in a plan-binding predicate")
    return _COMPONENT_CONFIGURED_PREDICATE_PREFIX + component_id


def _validate_active_predicate(value: object) -> str:
    predicate = _exact_string(value, "active_predicate")
    if predicate in {"ALWAYS", AGENT_SCHEDULER_ACTIVE_PREDICATE}:
        return predicate
    if predicate.startswith(_COMPONENT_CONFIGURED_PREDICATE_PREFIX):
        component_id = predicate.removeprefix(
            _COMPONENT_CONFIGURED_PREDICATE_PREFIX
        )
        if component_configured_predicate(component_id) == predicate:
            return predicate
    raise ValueError("component active_predicate is unsupported")


def _refused_component_reason_code(component_id: str) -> str:
    if component_id == "HISTORICAL_REPLAY":
        return ABSENT_REASON_SYNTHETIC_NO_HISTORICAL_CURSOR
    return ABSENT_REASON_COMPONENT_REFUSED


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise TypeError(f"{field} must be an array of nonempty strings")
    return tuple(value)


def _string_tuple_groups(
    value: object, field: str
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array of string arrays")
    return tuple(
        _string_tuple(group, f"{field}[{index}]")
        for index, group in enumerate(value)
    )


def _assert_sorted_unique(values: tuple[str, ...], field: str) -> None:
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{field} must be sorted and unique")


@dataclass(frozen=True, slots=True)
class ComponentSpecV1:
    schema_version: int
    component_id: str
    component_version: int
    implementation_status: str
    active_predicate: str
    dependencies: tuple[str, ...]
    owned_resources: tuple[str, ...]
    borrowed_resources: tuple[str, ...]
    rng_label_prefixes: tuple[str, ...]
    checkpoint_state_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != COMPOSITION_SCHEMA_VERSION
        ):
            raise ValueError("ComponentSpecV1 schema_version must be 1")
        _exact_string(self.component_id, "component_id")
        _exact_int(self.component_version, "component_version", minimum=1)
        if self.implementation_status not in _ALLOWED_STATUSES:
            raise ValueError("component implementation_status is invalid")
        _validate_active_predicate(self.active_predicate)
        if (
            self.component_id == AGENT_SCHEDULER_COMPONENT
            and self.active_predicate != AGENT_SCHEDULER_ACTIVE_PREDICATE
        ) or (
            self.component_id != AGENT_SCHEDULER_COMPONENT
            and self.active_predicate == AGENT_SCHEDULER_ACTIVE_PREDICATE
        ):
            raise ValueError(
                "the scheduler activation predicate belongs only to AgentScheduler"
            )
        if self.active_predicate.startswith(
            _COMPONENT_CONFIGURED_PREDICATE_PREFIX
        ) and self.active_predicate != component_configured_predicate(
            self.component_id
        ):
            raise ValueError(
                "a plan-binding predicate must name its own component ID"
            )
        if any(token in self.active_predicate for token in ("*", "EVERYTHING", "ENABLE_ALL")):
            raise ValueError("generic enable-everything predicates are forbidden")
        tuple_fields = (
            ("component dependencies", self.dependencies),
            ("component owned_resources", self.owned_resources),
            ("component borrowed_resources", self.borrowed_resources),
            ("component rng_label_prefixes", self.rng_label_prefixes),
            ("component checkpoint_state_ids", self.checkpoint_state_ids),
        )
        for field, values in tuple_fields:
            if type(values) is not tuple:
                raise TypeError(f"{field} must be an immutable tuple")
            if any(type(item) is not str or not item for item in values):
                raise TypeError(f"{field} must be nonempty strings")
            validate_strict_json(values)
            _assert_sorted_unique(values, field)
        if set(self.owned_resources) & set(self.borrowed_resources):
            raise ValueError("a component cannot own and borrow the same resource")
        for prefix in self.rng_label_prefixes:
            if (
                prefix.startswith("/")
                or prefix.endswith("/")
                or "//" in prefix
                or any(part in {"", ".", ".."} for part in prefix.split("/"))
            ):
                raise ValueError("RNG label prefixes must be stable semantic paths")
        expected_rng_prefixes = RNG_LABEL_PREFIXES_BY_COMPONENT_V1.get(
            self.component_id, ()
        )
        if self.rng_label_prefixes != expected_rng_prefixes:
            raise ValueError(
                "component RNG prefixes differ from the frozen ownership registry"
            )
        if self.component_id in self.dependencies:
            raise ValueError("component cannot depend on itself")

    def as_dict(self) -> dict[str, object]:
        return {
            "active_predicate": self.active_predicate,
            "borrowed_resources": list(self.borrowed_resources),
            "checkpoint_state_ids": list(self.checkpoint_state_ids),
            "component_id": self.component_id,
            "component_version": self.component_version,
            "dependencies": list(self.dependencies),
            "implementation_status": self.implementation_status,
            "owned_resources": list(self.owned_resources),
            "rng_label_prefixes": list(self.rng_label_prefixes),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ComponentSpecV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "active_predicate",
                "borrowed_resources",
                "checkpoint_state_ids",
                "component_id",
                "component_version",
                "dependencies",
                "implementation_status",
                "owned_resources",
                "rng_label_prefixes",
                "schema_version",
            },
            "ComponentSpecV1",
        )
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            component_id=_exact_string(payload["component_id"], "component_id"),
            component_version=_exact_int(
                payload["component_version"], "component_version", minimum=1
            ),
            implementation_status=_exact_string(
                payload["implementation_status"], "implementation_status"
            ),
            active_predicate=_exact_string(
                payload["active_predicate"], "active_predicate"
            ),
            dependencies=_string_tuple(payload["dependencies"], "dependencies"),
            owned_resources=_string_tuple(
                payload["owned_resources"], "owned_resources"
            ),
            borrowed_resources=_string_tuple(
                payload["borrowed_resources"], "borrowed_resources"
            ),
            rng_label_prefixes=_string_tuple(
                payload["rng_label_prefixes"], "rng_label_prefixes"
            ),
            checkpoint_state_ids=_string_tuple(
                payload["checkpoint_state_ids"], "checkpoint_state_ids"
            ),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> ComponentSpecV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    @property
    def implementation_reason_code(self) -> str:
        """Return the stable reason code implied by the frozen status enum."""

        return _IMPLEMENTATION_STATUS_REASON_CODES[self.implementation_status]


@dataclass(frozen=True, slots=True)
class CompositionProfileV1:
    schema_version: int
    profile_id: str
    profile_version: int
    implementation_status: str
    runtime_owner_component_id: str
    components: tuple[ComponentSpecV1, ...]
    refused_component_ids: tuple[str, ...]
    exactly_one_component_groups: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != COMPOSITION_SCHEMA_VERSION
        ):
            raise ValueError("CompositionProfileV1 schema_version must be 1")
        _exact_string(self.profile_id, "profile_id")
        _exact_int(self.profile_version, "profile_version", minimum=1)
        if self.implementation_status not in _ALLOWED_STATUSES:
            raise ValueError("profile implementation_status is invalid")
        _exact_string(self.runtime_owner_component_id, "runtime_owner_component_id")
        if any(token in self.profile_id for token in ("*", "EVERYTHING", "ENABLE_ALL")):
            raise ValueError("generic enable-everything profiles are forbidden")
        if (
            type(self.components) is not tuple
            or type(self.refused_component_ids) is not tuple
            or type(self.exactly_one_component_groups) is not tuple
        ):
            raise TypeError(
                "profile components/refusals/selection groups must be immutable tuples"
            )
        if any(type(item) is not ComponentSpecV1 for item in self.components):
            raise TypeError("components must contain ComponentSpecV1 records")
        component_ids = tuple(item.component_id for item in self.components)
        _assert_sorted_unique(component_ids, "profile component IDs")
        if any(type(item) is not str or not item for item in self.refused_component_ids):
            raise TypeError("refused component IDs must be nonempty strings")
        validate_strict_json(self.refused_component_ids)
        _assert_sorted_unique(self.refused_component_ids, "refused component IDs")
        if set(component_ids) & set(self.refused_component_ids):
            raise ValueError("a component cannot be both declared and refused")
        if self.runtime_owner_component_id not in component_ids:
            raise ValueError("runtime owner must name a declared component")

        component_set = set(component_ids)
        self._validate_exactly_one_groups(component_set)
        for component in self.components:
            missing = set(component.dependencies) - component_set
            if missing:
                raise ValueError(
                    f"component {component.component_id} has missing dependencies: "
                    + ",".join(sorted(missing))
                )
        self._validate_dependency_graph()
        self._validate_ownership()
        if self.profile_id == INITIAL_PROFILE_ID and self.profile_version == 1:
            self._validate_initial_profile()

    def _validate_exactly_one_groups(self, component_set: set[str]) -> None:
        groups = self.exactly_one_component_groups
        if any(type(group) is not tuple for group in groups):
            raise TypeError("exactly-one component groups must contain tuples")
        if groups != tuple(sorted(groups)) or len(groups) != len(set(groups)):
            raise ValueError("exactly-one component groups must be sorted and unique")
        grouped_members: set[str] = set()
        by_id = {item.component_id: item for item in self.components}
        for group in groups:
            if not group:
                raise ValueError("an exactly-one component group cannot be empty")
            if any(type(item) is not str or not item for item in group):
                raise TypeError("exactly-one component group IDs must be strings")
            validate_strict_json(group)
            _assert_sorted_unique(group, "exactly-one component group")
            missing = set(group) - component_set
            if missing:
                raise ValueError(
                    "exactly-one component group names undeclared components: "
                    + ",".join(sorted(missing))
                )
            overlap = grouped_members & set(group)
            if overlap:
                raise ValueError(
                    "a component cannot belong to multiple exactly-one groups: "
                    + ",".join(sorted(overlap))
                )
            grouped_members.update(group)
            for component_id in group:
                if by_id[component_id].active_predicate != component_configured_predicate(
                    component_id
                ):
                    raise ValueError(
                        "exactly-one members require their own plan-binding predicate"
                    )

    def _validate_dependency_graph(self) -> None:
        by_id = {item.component_id: item for item in self.components}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(component_id: str) -> None:
            if component_id in visiting:
                raise ValueError("composition dependencies contain a cycle")
            if component_id in visited:
                return
            visiting.add(component_id)
            for dependency in by_id[component_id].dependencies:
                visit(dependency)
            visiting.remove(component_id)
            visited.add(component_id)

        for component_id in sorted(by_id):
            visit(component_id)

    def _validate_ownership(self) -> None:
        owners: dict[str, str] = {}
        checkpoint_owners: dict[str, str] = {}
        rng_prefix_owners: list[tuple[str, str]] = []
        for component in self.components:
            for resource in component.owned_resources:
                previous = owners.get(resource)
                if previous is not None:
                    raise ValueError(
                        f"resource {resource} has duplicate owners {previous} and "
                        f"{component.component_id}"
                    )
                owners[resource] = component.component_id
            for checkpoint_state_id in component.checkpoint_state_ids:
                previous = checkpoint_owners.get(checkpoint_state_id)
                if previous is not None:
                    raise ValueError(
                        f"checkpoint state {checkpoint_state_id} has duplicate owners "
                        f"{previous} and {component.component_id}"
                    )
                checkpoint_owners[checkpoint_state_id] = component.component_id
            for prefix in component.rng_label_prefixes:
                for previous_prefix, previous_owner in rng_prefix_owners:
                    if (
                        prefix == previous_prefix
                        or prefix.startswith(previous_prefix + "/")
                        or previous_prefix.startswith(prefix + "/")
                    ):
                        raise ValueError(
                            f"RNG prefixes {previous_prefix} and {prefix} overlap "
                            f"between {previous_owner} and {component.component_id}"
                        )
                rng_prefix_owners.append((prefix, component.component_id))
        by_id = {item.component_id: item for item in self.components}

        def dependency_closure(component_id: str) -> set[str]:
            reachable: set[str] = set()
            pending = list(by_id[component_id].dependencies)
            while pending:
                dependency = pending.pop()
                if dependency in reachable:
                    continue
                reachable.add(dependency)
                pending.extend(by_id[dependency].dependencies)
            return reachable

        for component in self.components:
            missing = set(component.borrowed_resources) - set(owners)
            if missing:
                raise ValueError(
                    f"component {component.component_id} borrows unowned resources: "
                    + ",".join(sorted(missing))
                )
            dependencies = dependency_closure(component.component_id)
            unrelated_owners = {
                owners[resource]
                for resource in component.borrowed_resources
                if owners[resource] not in dependencies
            }
            if unrelated_owners:
                raise ValueError(
                    f"component {component.component_id} borrows from nondependencies: "
                    + ",".join(sorted(unrelated_owners))
                )

    def _validate_initial_profile(self) -> None:
        if self.implementation_status != "CONTRACT_ONLY":
            raise ValueError("initial profile revision 1 must remain CONTRACT_ONLY")
        if self.exactly_one_component_groups:
            raise ValueError("initial profile revision 1 has no selection groups")
        component_ids = tuple(item.component_id for item in self.components)
        if component_ids != _INITIAL_COMPONENT_IDS_SORTED:
            raise ValueError("initial profile has an unsupported component set")
        if self.refused_component_ids != _INITIAL_REFUSED_COMPONENT_IDS:
            raise ValueError("initial profile refusal set is not exact")
        if self.runtime_owner_component_id != FULL_DAY_RUNTIME_COMPONENT:
            raise ValueError("FullDayRuntime must be the initial runtime owner")
        runtime_components = [
            item for item in self.components if item.component_id == FULL_DAY_RUNTIME_COMPONENT
        ]
        if len(runtime_components) != 1:
            raise ValueError("initial profile requires exactly one FullDayRuntime")
        runtime = runtime_components[0]
        if frozenset(runtime.owned_resources) != _REQUIRED_RUNTIME_RESOURCES:
            raise ValueError("FullDayRuntime ownership is incomplete or excessive")
        resource_owners = {
            resource: component.component_id
            for component in self.components
            for resource in component.owned_resources
        }
        for resource in _REQUIRED_RUNTIME_RESOURCES:
            if resource_owners.get(resource) != FULL_DAY_RUNTIME_COMPONENT:
                raise ValueError(f"{resource} must have exactly one FullDayRuntime owner")

        expected_specs = {
            FULL_DAY_RUNTIME_COMPONENT: {
                "active_predicate": "ALWAYS",
                "borrowed_resources": (),
                "checkpoint_state_ids": tuple(
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
                ),
                "dependencies": (),
                "owned_resources": tuple(sorted(_REQUIRED_RUNTIME_RESOURCES)),
                "rng_label_prefixes": ("full_day/runtime",),
            },
            MECHANICS_COMPONENT: {
                "active_predicate": "ALWAYS",
                "borrowed_resources": tuple(
                    sorted({"AUCTION_BOOK", "MARKET_MECHANICS_ENGINE", "ORDER_BOOK"})
                ),
                "checkpoint_state_ids": tuple(
                    sorted(
                        {
                            "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
                            "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                            "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                        }
                    )
                ),
                "dependencies": (FULL_DAY_RUNTIME_COMPONENT,),
                "owned_resources": (),
                "rng_label_prefixes": (),
            },
            AGENT_SCHEDULER_COMPONENT: {
                "active_predicate": AGENT_SCHEDULER_ACTIVE_PREDICATE,
                "borrowed_resources": tuple(
                    sorted(
                        {
                            "MARKET_MECHANICS_ENGINE",
                            "ORDER_GATEWAY",
                            "SIMULATION_CLOCK",
                        }
                    )
                ),
                "checkpoint_state_ids": ("AGENT_SCHEDULER_METAORDERS_V1",),
                "dependencies": tuple(
                    sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT})
                ),
                "owned_resources": tuple(
                    sorted(
                        {
                            "AGENT_POLICY_STATE",
                            "AGENT_RNG_SUBSTREAMS",
                            "METAORDER_STATE",
                            "PARTICIPANT_ACTIVATION_STATE",
                            "PENDING_AGENT_DECISIONS",
                        }
                    )
                ),
                "rng_label_prefixes": ("full_day/participant",),
            },
        }
        for component in self.components:
            if (
                component.component_version != 1
                or component.implementation_status != "CONTRACT_ONLY"
            ):
                raise ValueError("initial component versions/statuses are immutable")
            expected = expected_specs[component.component_id]
            for field, value in expected.items():
                if getattr(component, field) != value:
                    raise ValueError(
                        f"initial component {component.component_id} changed {field}"
                    )

    def as_dict(self) -> dict[str, object]:
        return {
            "components": [item.as_dict() for item in self.components],
            "exactly_one_component_groups": [
                list(group) for group in self.exactly_one_component_groups
            ],
            "implementation_status": self.implementation_status,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "refused_component_ids": list(self.refused_component_ids),
            "runtime_owner_component_id": self.runtime_owner_component_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CompositionProfileV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "components",
                "exactly_one_component_groups",
                "implementation_status",
                "profile_id",
                "profile_version",
                "refused_component_ids",
                "runtime_owner_component_id",
                "schema_version",
            },
            "CompositionProfileV1",
        )
        components = payload["components"]
        if not isinstance(components, list) or any(
            not isinstance(item, Mapping) for item in components
        ):
            raise TypeError("components must be an array of objects")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            profile_id=_exact_string(payload["profile_id"], "profile_id"),
            profile_version=_exact_int(
                payload["profile_version"], "profile_version", minimum=1
            ),
            implementation_status=_exact_string(
                payload["implementation_status"], "implementation_status"
            ),
            runtime_owner_component_id=_exact_string(
                payload["runtime_owner_component_id"], "runtime_owner_component_id"
            ),
            components=tuple(ComponentSpecV1.from_dict(item) for item in components),
            refused_component_ids=_string_tuple(
                payload["refused_component_ids"], "refused_component_ids"
            ),
            exactly_one_component_groups=_string_tuple_groups(
                payload["exactly_one_component_groups"],
                "exactly_one_component_groups",
            ),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CompositionProfileV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def implementation_reason_code(self) -> str:
        """Return the stable reason code implied by the frozen status enum."""

        return _IMPLEMENTATION_STATUS_REASON_CODES[self.implementation_status]

    def resolve_active_components(
        self, predicate_values: Mapping[str, bool]
    ) -> tuple[str, ...]:
        if not isinstance(predicate_values, Mapping):
            raise TypeError("predicate_values must be a mapping")
        expected_predicates = {
            item.active_predicate
            for item in self.components
            if item.active_predicate != "ALWAYS"
        }
        if set(predicate_values) != expected_predicates or any(
            type(value) is not bool for value in predicate_values.values()
        ):
            raise ValueError("predicate_values must exactly resolve active predicates")
        active = tuple(
            item.component_id
            for item in self.components
            if item.active_predicate == "ALWAYS"
            or predicate_values[item.active_predicate]
        )
        for group in self.exactly_one_component_groups:
            selected = set(group) & set(active)
            if len(selected) != 1:
                raise ValueError(
                    "exactly-one component group must resolve one active member"
                )
        return active

    def predicate_values_for_plan_bindings(
        self,
        configured_component_ids: Iterable[str],
        *,
        participant_schedule_nonempty: bool,
        any_participant_initially_active: bool,
    ) -> dict[str, bool]:
        """Resolve supported predicates from explicit plan configuration bindings."""

        if isinstance(configured_component_ids, (str, bytes)):
            raise TypeError("configured_component_ids must be an iterable of IDs")
        configured = tuple(configured_component_ids)
        if any(type(item) is not str or not item for item in configured):
            raise TypeError("configured component IDs must be nonempty strings")
        if len(configured) != len(set(configured)):
            raise ValueError("configured component IDs must be unique")
        declared = {item.component_id for item in self.components}
        unknown = set(configured) - declared
        if unknown:
            raise ValueError(
                "plan configures components outside the profile: "
                + ",".join(sorted(unknown))
            )
        scheduler_active = agent_scheduler_is_active(
            participant_schedule_nonempty=participant_schedule_nonempty,
            any_participant_initially_active=any_participant_initially_active,
        )
        values: dict[str, bool] = {}
        for component in self.components:
            predicate = component.active_predicate
            if predicate == "ALWAYS":
                continue
            if predicate == AGENT_SCHEDULER_ACTIVE_PREDICATE:
                values[predicate] = scheduler_active
                continue
            configured_id = predicate.removeprefix(
                _COMPONENT_CONFIGURED_PREDICATE_PREFIX
            )
            values[predicate] = configured_id in configured
        # Resolve now so invalid exactly-one selections fail at the plan boundary.
        self.resolve_active_components(values)
        return values

    def component_status_and_reason(self, component_id: str) -> tuple[str, str]:
        """Resolve a declared/refused component to stable status semantics."""

        component_id = _exact_string(component_id, "component_id")
        declared = {
            component.component_id: component for component in self.components
        }
        component = declared.get(component_id)
        if component is not None:
            return component.implementation_status, component.implementation_reason_code
        if component_id in self.refused_component_ids:
            return "REFUSED", _refused_component_reason_code(component_id)
        raise ValueError("component is neither declared nor explicitly refused")

    def absence_reason_code(
        self,
        component_id: str,
        predicate_values: Mapping[str, bool],
    ) -> str:
        """Return the stable composition proof for a checkpoint ABSENT record."""

        component_id = _exact_string(component_id, "component_id")
        active = set(self.resolve_active_components(predicate_values))
        if component_id in self.refused_component_ids:
            return _refused_component_reason_code(component_id)
        declared = {item.component_id for item in self.components}
        if component_id not in declared:
            raise ValueError("component is neither declared nor explicitly refused")
        if component_id in active:
            raise ValueError("an active component cannot be recorded ABSENT")
        return ABSENT_REASON_COMPONENT_INACTIVE

    def validate_activation(
        self,
        active_component_ids: Iterable[str],
        predicate_values: Mapping[str, bool],
    ) -> None:
        active = tuple(sorted(active_component_ids))
        if len(active) != len(set(active)):
            raise ValueError("active component IDs must be unique")
        expected = tuple(sorted(self.resolve_active_components(predicate_values)))
        if active != expected:
            raise ValueError("active component set omits or adds a component")
        refused = set(active) & set(self.refused_component_ids)
        if refused:
            raise ValueError("refused components cannot be activated")


@dataclass(frozen=True, slots=True)
class CompositionMatrixV1:
    schema_version: int
    matrix_id: str
    matrix_version: int
    previous_matrix_sha256: str | None
    profiles: tuple[CompositionProfileV1, ...]

    def __post_init__(self) -> None:
        if (
            _exact_int(self.schema_version, "schema_version")
            != COMPOSITION_SCHEMA_VERSION
        ):
            raise ValueError("CompositionMatrixV1 schema_version must be 1")
        _exact_string(self.matrix_id, "matrix_id")
        _exact_int(self.matrix_version, "matrix_version", minimum=1)
        if self.previous_matrix_sha256 is not None and (
            type(self.previous_matrix_sha256) is not str
            or len(self.previous_matrix_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.previous_matrix_sha256)
        ):
            raise ValueError("previous_matrix_sha256 must be null or lowercase SHA-256")
        if self.matrix_version == 1 and self.previous_matrix_sha256 is not None:
            raise ValueError("matrix version 1 cannot have a predecessor")
        if self.matrix_version > 1 and self.previous_matrix_sha256 is None:
            raise ValueError("later matrix versions must bind their predecessor")
        if type(self.profiles) is not tuple:
            raise TypeError("composition profiles must be an immutable tuple")
        if any(type(item) is not CompositionProfileV1 for item in self.profiles):
            raise TypeError("profiles must contain CompositionProfileV1 records")
        if not self.profiles:
            raise ValueError("composition matrix must contain at least one profile row")
        latest_profile_versions: dict[str, int] = {}
        latest_profile_rows: dict[str, CompositionProfileV1] = {}
        profile_keys: set[tuple[str, int]] = set()
        component_records: dict[tuple[str, int], bytes] = {}
        component_versions: dict[str, set[int]] = {}
        for profile in self.profiles:
            key = (profile.profile_id, profile.profile_version)
            if key in profile_keys:
                raise ValueError("composition profile revision is duplicated")
            profile_keys.add(key)
            expected_version = latest_profile_versions.get(profile.profile_id, 0) + 1
            if profile.profile_version != expected_version:
                raise ValueError(
                    "composition profile revisions must start at 1 and be contiguous"
                )
            previous_profile = latest_profile_rows.get(profile.profile_id)
            if previous_profile is not None:
                if (
                    profile.runtime_owner_component_id
                    != previous_profile.runtime_owner_component_id
                    or tuple(item.component_id for item in profile.components)
                    != tuple(item.component_id for item in previous_profile.components)
                    or profile.refused_component_ids
                    != previous_profile.refused_component_ids
                    or profile.exactly_one_component_groups
                    != previous_profile.exactly_one_component_groups
                ):
                    raise ValueError(
                        "a profile revision must preserve its logical composition"
                    )
            latest_profile_versions[profile.profile_id] = profile.profile_version
            latest_profile_rows[profile.profile_id] = profile
            for component in profile.components:
                component_key = (
                    component.component_id,
                    component.component_version,
                )
                component_bytes = component.canonical_bytes()
                previous_bytes = component_records.get(component_key)
                if previous_bytes is not None and previous_bytes != component_bytes:
                    raise ValueError(
                        "a component ID/version cannot change meaning across profiles"
                    )
                component_records[component_key] = component_bytes
                component_versions.setdefault(component.component_id, set()).add(
                    component.component_version
                )
        for versions in component_versions.values():
            if versions != set(range(1, max(versions) + 1)):
                raise ValueError(
                    "component versions must start at 1 and remain contiguous"
                )
        if (
            self.profiles[0].profile_id != INITIAL_PROFILE_ID
            or self.profiles[0].profile_version != 1
        ):
            raise ValueError("every matrix must retain the initial profile as row zero")
        if self.matrix_version == 1 and len(self.profiles) != 1:
            raise ValueError("initial matrix must contain only the initial profile")

    def as_dict(self) -> dict[str, object]:
        return {
            "matrix_id": self.matrix_id,
            "matrix_version": self.matrix_version,
            "previous_matrix_sha256": self.previous_matrix_sha256,
            "profiles": [item.as_dict() for item in self.profiles],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CompositionMatrixV1:
        validate_strict_json(payload)
        _require_exact_fields(
            payload,
            {
                "matrix_id",
                "matrix_version",
                "previous_matrix_sha256",
                "profiles",
                "schema_version",
            },
            "CompositionMatrixV1",
        )
        profiles = payload["profiles"]
        if not isinstance(profiles, list) or any(
            not isinstance(item, Mapping) for item in profiles
        ):
            raise TypeError("profiles must be an array of objects")
        previous = payload["previous_matrix_sha256"]
        if previous is not None and type(previous) is not str:
            raise TypeError("previous_matrix_sha256 must be null or a string")
        return cls(
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            matrix_id=_exact_string(payload["matrix_id"], "matrix_id"),
            matrix_version=_exact_int(
                payload["matrix_version"], "matrix_version", minimum=1
            ),
            previous_matrix_sha256=previous,
            profiles=tuple(CompositionProfileV1.from_dict(item) for item in profiles),
        )

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> CompositionMatrixV1:
        return cls.from_dict(parse_canonical_json_object(payload))

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def profile(
        self, profile_id: str, profile_version: int | None = None
    ) -> CompositionProfileV1:
        _exact_string(profile_id, "profile_id")
        if profile_version is not None:
            _exact_int(profile_version, "profile_version", minimum=1)
        matches = [item for item in self.profiles if item.profile_id == profile_id]
        if not matches:
            raise ValueError("composition profile is not present")
        if profile_version is None:
            return matches[-1]
        version_matches = [
            item for item in matches if item.profile_version == profile_version
        ]
        if len(version_matches) != 1:
            raise ValueError("composition profile revision is not present exactly once")
        return version_matches[0]

    def validate_append_only_successor(self, successor: CompositionMatrixV1) -> None:
        if type(successor) is not CompositionMatrixV1:
            raise TypeError("successor must be CompositionMatrixV1")
        if successor.matrix_id != self.matrix_id:
            raise ValueError("successor matrix_id changed")
        if successor.matrix_version != self.matrix_version + 1:
            raise ValueError("successor matrix_version must advance by exactly one")
        if successor.previous_matrix_sha256 != self.sha256:
            raise ValueError("successor does not bind this matrix digest")
        if len(successor.profiles) <= len(self.profiles):
            raise ValueError("successor must append at least one profile revision")
        successor_prefix = successor.profiles[: len(self.profiles)]
        for previous, retained in zip(self.profiles, successor_prefix, strict=True):
            if retained.canonical_bytes() != previous.canonical_bytes():
                raise ValueError(
                    "successor must retain every existing profile row byte-identically"
                )
        latest_versions = {
            profile_id: max(
                item.profile_version
                for item in self.profiles
                if item.profile_id == profile_id
            )
            for profile_id in {item.profile_id for item in self.profiles}
        }
        for appended in successor.profiles[len(self.profiles) :]:
            expected_version = latest_versions.get(appended.profile_id, 0) + 1
            if appended.profile_version != expected_version:
                raise ValueError(
                    "successor additions must be new profiles or contiguous revisions"
                )
            latest_versions[appended.profile_id] = appended.profile_version


def initial_composition_matrix() -> CompositionMatrixV1:
    runtime = ComponentSpecV1(
        schema_version=1,
        component_id=FULL_DAY_RUNTIME_COMPONENT,
        component_version=1,
        implementation_status="CONTRACT_ONLY",
        active_predicate="ALWAYS",
        dependencies=(),
        owned_resources=tuple(sorted(_REQUIRED_RUNTIME_RESOURCES)),
        borrowed_resources=(),
        rng_label_prefixes=("full_day/runtime",),
        checkpoint_state_ids=tuple(
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
        ),
    )
    mechanics = ComponentSpecV1(
        schema_version=1,
        component_id=MECHANICS_COMPONENT,
        component_version=1,
        implementation_status="CONTRACT_ONLY",
        active_predicate="ALWAYS",
        dependencies=(FULL_DAY_RUNTIME_COMPONENT,),
        owned_resources=(),
        borrowed_resources=tuple(
            sorted({"AUCTION_BOOK", "MARKET_MECHANICS_ENGINE", "ORDER_BOOK"})
        ),
        rng_label_prefixes=(),
        checkpoint_state_ids=tuple(
            sorted(
                {
                    "AUCTION_ORDERS_COUNTERS_PLAYER_POSITION_V1",
                    "CONTINUOUS_BOOK_FIFO_ORDERS_TRADES_FILLS_JOURNAL_PLAYER_POSITION_V1",
                    "MECHANICS_RULES_SESSION_COUNTERS_MANAGED_ORDERS_LAST_TRADE_V1",
                }
            )
        ),
    )
    scheduler = ComponentSpecV1(
        schema_version=1,
        component_id=AGENT_SCHEDULER_COMPONENT,
        component_version=1,
        implementation_status="CONTRACT_ONLY",
        active_predicate=AGENT_SCHEDULER_ACTIVE_PREDICATE,
        dependencies=tuple(
            sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT})
        ),
        owned_resources=tuple(
            sorted(
                {
                    "AGENT_POLICY_STATE",
                    "AGENT_RNG_SUBSTREAMS",
                    "METAORDER_STATE",
                    "PARTICIPANT_ACTIVATION_STATE",
                    "PENDING_AGENT_DECISIONS",
                }
            )
        ),
        borrowed_resources=tuple(
            sorted({"MARKET_MECHANICS_ENGINE", "ORDER_GATEWAY", "SIMULATION_CLOCK"})
        ),
        rng_label_prefixes=("full_day/participant",),
        checkpoint_state_ids=("AGENT_SCHEDULER_METAORDERS_V1",),
    )
    components = tuple(sorted((runtime, mechanics, scheduler), key=lambda item: item.component_id))
    profile = CompositionProfileV1(
        schema_version=1,
        profile_id=INITIAL_PROFILE_ID,
        profile_version=1,
        implementation_status="CONTRACT_ONLY",
        runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
        components=components,
        refused_component_ids=_INITIAL_REFUSED_COMPONENT_IDS,
        exactly_one_component_groups=(),
    )
    return CompositionMatrixV1(
        schema_version=1,
        matrix_id=INITIAL_MATRIX_ID,
        matrix_version=1,
        previous_matrix_sha256=None,
        profiles=(profile,),
    )


def executable_agent_mechanics_composition_matrix() -> CompositionMatrixV1:
    """Return the append-only WO31-E1 executable profile promotion.

    The immutable revision-1 row remains byte-identical.  Only the mechanics
    engine and injected agent scheduler advance to component version 2 and
    ``EXECUTABLE``; the runtime contract row and every refusal retain their
    prior meaning exactly, as required by the bounded E1 capability claim.
    """

    previous = initial_composition_matrix()
    prior_profile = previous.profile(INITIAL_PROFILE_ID, 1)
    promoted_ids = frozenset(
        {AGENT_SCHEDULER_COMPONENT, MECHANICS_COMPONENT}
    )
    promoted_components: list[ComponentSpecV1] = []
    for component in prior_profile.components:
        if component.component_id not in promoted_ids:
            promoted_components.append(component)
            continue
        payload = component.as_dict()
        payload["component_version"] = component.component_version + 1
        payload["implementation_status"] = "EXECUTABLE"
        promoted_components.append(ComponentSpecV1.from_dict(payload))
    promoted_profile = CompositionProfileV1(
        schema_version=prior_profile.schema_version,
        profile_id=prior_profile.profile_id,
        profile_version=prior_profile.profile_version + 1,
        implementation_status="EXECUTABLE",
        runtime_owner_component_id=prior_profile.runtime_owner_component_id,
        components=tuple(promoted_components),
        refused_component_ids=prior_profile.refused_component_ids,
        exactly_one_component_groups=prior_profile.exactly_one_component_groups,
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, promoted_profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def executable_simple_flow_composition_matrix() -> CompositionMatrixV1:
    """Append the bounded WO31-E2 simple-flow executable profile.

    The E1 rows remain byte-identical.  The new profile declares the complete
    exactly-one flow selection contract while promoting only ``FLOW_SIMPLE_V1``;
    Hawkes and queue-reactive remain explicit contract-only rows until their
    independent executable/restore evidence exists.
    """

    previous = executable_agent_mechanics_composition_matrix()
    e1_profile = previous.profile(INITIAL_PROFILE_ID, 2)
    flow_specs = tuple(
        ComponentSpecV1(
            schema_version=1,
            component_id=component_id,
            component_version=1,
            implementation_status=(
                "EXECUTABLE"
                if component_id == FLOW_SIMPLE_COMPONENT
                else "CONTRACT_ONLY"
            ),
            active_predicate=component_configured_predicate(component_id),
            dependencies=tuple(
                sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT})
            ),
            owned_resources=tuple(
                sorted(
                    {
                        f"{component_id}_MODEL_STATE",
                        f"{component_id}_PENDING_PROPOSAL",
                        f"{component_id}_RNG_SUBSTREAM",
                    }
                )
            ),
            borrowed_resources=tuple(
                sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"})
            ),
            rng_label_prefixes={
                FLOW_HAWKES_COMPONENT: ("full_day/flow/hawkes",),
                FLOW_QUEUE_REACTIVE_COMPONENT: (
                    "full_day/flow/queue_reactive",
                ),
                FLOW_SIMPLE_COMPONENT: ("full_day/flow/simple",),
            }[component_id],
            checkpoint_state_ids=(component_id,),
        )
        for component_id in FLOW_COMPONENT_IDS
    )
    profile = CompositionProfileV1(
        schema_version=1,
        profile_id=FLOW_PROFILE_ID,
        profile_version=1,
        implementation_status="EXECUTABLE",
        runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
        components=tuple(
            sorted(
                (*e1_profile.components, *flow_specs),
                key=lambda item: item.component_id,
            )
        ),
        refused_component_ids=tuple(
            component_id
            for component_id in e1_profile.refused_component_ids
            if component_id
            not in {"FLOW_HAWKES", "FLOW_QUEUE_REACTIVE", "FLOW_SIMPLE"}
        ),
        exactly_one_component_groups=(FLOW_COMPONENT_IDS,),
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def executable_hawkes_flow_composition_matrix() -> CompositionMatrixV1:
    """Append the independently evidenced Hawkes-flow profile revision.

    Matrix/profile revisions already published for E1 and simple flow remain
    byte-identical.  The new flow-profile revision promotes only the Hawkes
    adapter; queue-reactive flow remains contract-only until its own executable
    and restore evidence exists.
    """

    previous = executable_simple_flow_composition_matrix()
    prior_profile = previous.profile(FLOW_PROFILE_ID, 1)
    promoted_components: list[ComponentSpecV1] = []
    for component in prior_profile.components:
        if component.component_id != FLOW_HAWKES_COMPONENT:
            promoted_components.append(component)
            continue
        payload = component.as_dict()
        payload["component_version"] = component.component_version + 1
        payload["implementation_status"] = "EXECUTABLE"
        promoted_components.append(ComponentSpecV1.from_dict(payload))
    promoted_profile = CompositionProfileV1(
        schema_version=prior_profile.schema_version,
        profile_id=prior_profile.profile_id,
        profile_version=prior_profile.profile_version + 1,
        implementation_status="EXECUTABLE",
        runtime_owner_component_id=prior_profile.runtime_owner_component_id,
        components=tuple(promoted_components),
        refused_component_ids=prior_profile.refused_component_ids,
        exactly_one_component_groups=prior_profile.exactly_one_component_groups,
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, promoted_profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def executable_queue_reactive_flow_composition_matrix() -> CompositionMatrixV1:
    """Append the queue-reactive promotion without rewriting prior profiles."""

    previous = executable_hawkes_flow_composition_matrix()
    prior_profile = previous.profile(FLOW_PROFILE_ID, 2)
    promoted_components: list[ComponentSpecV1] = []
    for component in prior_profile.components:
        if component.component_id != FLOW_QUEUE_REACTIVE_COMPONENT:
            promoted_components.append(component)
            continue
        payload = component.as_dict()
        payload["component_version"] = component.component_version + 1
        payload["implementation_status"] = "EXECUTABLE"
        promoted_components.append(ComponentSpecV1.from_dict(payload))
    promoted_profile = CompositionProfileV1(
        schema_version=prior_profile.schema_version,
        profile_id=prior_profile.profile_id,
        profile_version=prior_profile.profile_version + 1,
        implementation_status="EXECUTABLE",
        runtime_owner_component_id=prior_profile.runtime_owner_component_id,
        components=tuple(promoted_components),
        refused_component_ids=prior_profile.refused_component_ids,
        exactly_one_component_groups=prior_profile.exactly_one_component_groups,
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, promoted_profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def executable_delivery_composition_matrix() -> CompositionMatrixV1:
    """Append the bounded passive-delivery profile without changing E1/E2 rows."""

    previous = executable_queue_reactive_flow_composition_matrix()
    flow_profile = previous.profile(FLOW_PROFILE_ID, 3)
    delivery_spec = ComponentSpecV1(
        schema_version=1,
        component_id=DELIVERY_ASYNC_COMPONENT,
        component_version=1,
        implementation_status="EXECUTABLE",
        active_predicate=component_configured_predicate(DELIVERY_ASYNC_COMPONENT),
        dependencies=tuple(
            sorted({FULL_DAY_RUNTIME_COMPONENT, MECHANICS_COMPONENT})
        ),
        owned_resources=tuple(
            sorted(
                {
                    "CLIENT_DELIVERY_QUEUE",
                    "CLIENT_KNOWN_WORKING_ORDER_STATE",
                    "DELIVERY_MESSAGE_ALLOCATOR",
                    "DELIVERY_RNG_SUBSTREAM",
                    "VENUE_RECEIPT_QUEUE",
                }
            )
        ),
        borrowed_resources=tuple(sorted({"ORDER_GATEWAY", "SIMULATION_CLOCK"})),
        rng_label_prefixes=("full_day/delivery",),
        checkpoint_state_ids=("PENDING_LATENCY_CLIENT_DELIVERY_V1",),
    )
    profile = CompositionProfileV1(
        schema_version=1,
        profile_id=DELIVERY_PROFILE_ID,
        profile_version=1,
        implementation_status="EXECUTABLE",
        runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
        components=tuple(
            sorted(
                (*flow_profile.components, delivery_spec),
                key=lambda item: item.component_id,
            )
        ),
        refused_component_ids=flow_profile.refused_component_ids,
        exactly_one_component_groups=flow_profile.exactly_one_component_groups,
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def executable_research_composition_matrix() -> CompositionMatrixV1:
    """Append the passive observable research profile for WO31-E4.

    The component borrows only the authoritative runtime gateway/clock and the
    already-delivered client projection.  It owns no exchange, book, calendar,
    cash ledger, or RNG stream.
    """

    previous = executable_delivery_composition_matrix()
    delivery_profile = previous.profile(DELIVERY_PROFILE_ID, 1)
    research_spec = ComponentSpecV1(
        schema_version=1,
        component_id=FEATURE_STRATEGY_PLAYER_COMPONENT,
        component_version=1,
        implementation_status="EXECUTABLE",
        active_predicate=component_configured_predicate(
            FEATURE_STRATEGY_PLAYER_COMPONENT
        ),
        dependencies=tuple(
            sorted(
                {
                    DELIVERY_ASYNC_COMPONENT,
                    FULL_DAY_RUNTIME_COMPONENT,
                    MECHANICS_COMPONENT,
                }
            )
        ),
        owned_resources=tuple(
            sorted(
                {
                    "FEATURE_WINDOWS",
                    "PLAYER_DECISION_STATE",
                    "PLAYER_POSITION_PROJECTION",
                    "STRATEGY_TIMER_STATE",
                }
            )
        ),
        borrowed_resources=tuple(
            sorted(
                {
                    "CLIENT_KNOWN_WORKING_ORDER_STATE",
                    "ORDER_GATEWAY",
                    "SIMULATION_CLOCK",
                }
            )
        ),
        rng_label_prefixes=(),
        checkpoint_state_ids=tuple(
            sorted(
                {
                    "FEATURES_V1",
                    "PLAYER_OVERLAY_WORKING_ORDERS_V1",
                    "STRATEGIES_V1",
                }
            )
        ),
    )
    profile = CompositionProfileV1(
        schema_version=1,
        profile_id=RESEARCH_PROFILE_ID,
        profile_version=1,
        implementation_status="EXECUTABLE",
        runtime_owner_component_id=FULL_DAY_RUNTIME_COMPONENT,
        components=tuple(
            sorted(
                (*delivery_profile.components, research_spec),
                key=lambda item: item.component_id,
            )
        ),
        refused_component_ids=tuple(
            component_id
            for component_id in delivery_profile.refused_component_ids
            if component_id not in {"FEATURES", "PLAYER_OVERLAY", "STRATEGIES"}
        ),
        exactly_one_component_groups=(FLOW_COMPONENT_IDS,),
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def restorable_multivenue_hidden_composition_matrix() -> CompositionMatrixV1:
    """Append WO31-E5 without claiming a second executable exchange profile.

    The fragmented-market owner can restore independently, so its component status
    is exact ``RESTORABLE_COMPONENT_ONLY``.  The named research composition remains
    ``CONTRACT_ONLY`` because the owner replaces, rather than wraps, the E4
    single-engine owner graph.
    """

    previous = executable_research_composition_matrix()
    multivenue = ComponentSpecV1(
        schema_version=1,
        component_id=MULTIVENUE_HIDDEN_COMPONENT,
        component_version=1,
        implementation_status="RESTORABLE_COMPONENT_ONLY",
        active_predicate="ALWAYS",
        dependencies=(),
        owned_resources=tuple(
            sorted(
                {
                    "CONSOLIDATED_OBSERVABLE_FEED",
                    "HIDDEN_LIQUIDITY_TRUTH",
                    "MARKET_MECHANICS_ENGINE",
                    "MULTIVENUE_COORDINATOR",
                    "MULTIVENUE_ROUTE_STATE",
                    "ORDER_BOOK",
                    "SIMULATION_CLOCK",
                    "VENUE_LATENCY_RNG_SUBSTREAMS",
                }
            )
        ),
        borrowed_resources=(),
        rng_label_prefixes=("full_day/multivenue",),
        checkpoint_state_ids=tuple(
            sorted({"HIDDEN_LIQUIDITY_V1", "MULTIVENUE_V1"})
        ),
    )
    profile = CompositionProfileV1(
        schema_version=1,
        profile_id=MULTIVENUE_HIDDEN_PROFILE_ID,
        profile_version=1,
        implementation_status="CONTRACT_ONLY",
        runtime_owner_component_id=MULTIVENUE_HIDDEN_COMPONENT,
        components=(multivenue,),
        refused_component_ids=tuple(
            sorted(
                {
                    "ALGORITHMS",
                    "ENGINE_MARKET_MECHANICS_V1",
                    "FEATURES",
                    "FULL_DAY_RUNTIME_V1",
                    "HISTORICAL_REPLAY",
                    "PLAYER_OVERLAY",
                    "STRATEGIES",
                }
            )
        ),
        exactly_one_component_groups=(),
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


def restorable_execution_algorithm_composition_matrix() -> CompositionMatrixV1:
    """Append WO31-E6 as a standalone restorable algorithm boundary.

    The policy, client tracker, decision schedule, child allocator, latency view,
    and its private coordinator restore together.  This row intentionally refuses
    both full-day execution and historical replay; it is not a compatibility claim
    with either the E4 or E5 owner graph.
    """

    previous = restorable_multivenue_hidden_composition_matrix()
    algorithm = ComponentSpecV1(
        schema_version=1,
        component_id=EXECUTION_ALGORITHM_COMPONENT,
        component_version=1,
        implementation_status="RESTORABLE_COMPONENT_ONLY",
        active_predicate="ALWAYS",
        dependencies=(),
        owned_resources=tuple(
            sorted(
                {
                    "ALGORITHM_CHILD_ORDER_ALLOCATOR",
                    "ALGORITHM_CLIENT_TRACKER",
                    "ALGORITHM_DECISION_SCHEDULE",
                    "ALGORITHM_POLICY_STATE",
                    "CLIENT_LATENCY_VENUE_STATE",
                    "CONSOLIDATED_OBSERVABLE_FEED",
                    "MULTIVENUE_COORDINATOR",
                    "MULTIVENUE_ROUTE_STATE",
                    "ORDER_GATEWAY",
                    "SIMULATION_CLOCK",
                }
            )
        ),
        borrowed_resources=(),
        rng_label_prefixes=(),
        checkpoint_state_ids=(EXECUTION_ALGORITHM_COMPONENT,),
    )
    profile = CompositionProfileV1(
        schema_version=1,
        profile_id=EXECUTION_ALGORITHM_PROFILE_ID,
        profile_version=1,
        implementation_status="CONTRACT_ONLY",
        runtime_owner_component_id=EXECUTION_ALGORITHM_COMPONENT,
        components=(algorithm,),
        refused_component_ids=tuple(
            sorted(
                {
                    "ENGINE_MARKET_MECHANICS_V1",
                    "FEATURES",
                    "FULL_DAY_RUNTIME_V1",
                    "HISTORICAL_REPLAY",
                    "PLAYER_OVERLAY",
                    "STRATEGIES",
                    "VENUE_MULTIVENUE_HIDDEN_V1",
                }
            )
        ),
        exactly_one_component_groups=(),
    )
    successor = CompositionMatrixV1(
        schema_version=previous.schema_version,
        matrix_id=previous.matrix_id,
        matrix_version=previous.matrix_version + 1,
        previous_matrix_sha256=previous.sha256,
        profiles=(*previous.profiles, profile),
    )
    previous.validate_append_only_successor(successor)
    return successor


__all__ = [
    "ABSENT_REASON_COMPONENT_INACTIVE",
    "ABSENT_REASON_COMPONENT_REFUSED",
    "ABSENT_REASON_SYNTHETIC_NO_HISTORICAL_CURSOR",
    "AGENT_SCHEDULER_ACTIVE_PREDICATE",
    "AGENT_SCHEDULER_COMPONENT",
    "COMPOSITION_SCHEMA_VERSION",
    "DELIVERY_ASYNC_COMPONENT",
    "DELIVERY_PROFILE_ID",
    "EXECUTION_ALGORITHM_COMPONENT",
    "EXECUTION_ALGORITHM_PROFILE_ID",
    "FEATURE_STRATEGY_PLAYER_COMPONENT",
    "ComponentSpecV1",
    "CompositionMatrixV1",
    "CompositionProfileV1",
    "FULL_DAY_RUNTIME_COMPONENT",
    "FLOW_COMPONENT_IDS",
    "FLOW_HAWKES_COMPONENT",
    "FLOW_PROFILE_ID",
    "FLOW_QUEUE_REACTIVE_COMPONENT",
    "FLOW_SIMPLE_COMPONENT",
    "INITIAL_MATRIX_ID",
    "INITIAL_PROFILE_ID",
    "MECHANICS_COMPONENT",
    "MULTIVENUE_HIDDEN_COMPONENT",
    "MULTIVENUE_HIDDEN_PROFILE_ID",
    "RESEARCH_PROFILE_ID",
    "agent_scheduler_is_active",
    "component_configured_predicate",
    "executable_agent_mechanics_composition_matrix",
    "executable_delivery_composition_matrix",
    "executable_hawkes_flow_composition_matrix",
    "executable_queue_reactive_flow_composition_matrix",
    "executable_research_composition_matrix",
    "executable_simple_flow_composition_matrix",
    "initial_composition_matrix",
    "restorable_execution_algorithm_composition_matrix",
    "restorable_multivenue_hidden_composition_matrix",
]
