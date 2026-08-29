"""Declared, bounded transformations over canonical strategy syntax trees."""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum

from kirby2.immutable import freeze_json, thaw_json
from kirby2.strategy.language import ComparisonOperator, FeatureName, TrafficState
from kirby2.strategy.state_machine import (
    PositionFeature,
    TimeQualifier,
)

from .ast import (
    MAX_EXACT_DECIMAL_DIGITS_V1,
    MAX_EXACT_DECIMAL_SCALE_V1,
    MAX_STRATEGY_DURATION_US_V1,
    ComparisonNodeV1,
    ExactDecimalV1,
    StateMachineStrategyAstV1,
    StateNodeV1,
    StrategyAstKindV1,
    StrategyAstV1,
    TrafficLightStrategyAstV1,
    TransitionNodeV1,
    strategy_ast_round_trip,
)
from .diffs import (
    StrategyComplexityDeltaV1,
    StrategyComplexityV1,
    StrategyMutationDiffV1,
    build_mutation_diff,
    strategy_complexity,
)
from .identity import canonical_identity_bytes, strategy_semantic_sha256
from .lineage import (
    StrategyLineageNodeV1,
    StrategyRngSubstreamV1,
    build_strategy_lineage_node,
)


STRATEGY_MUTATION_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_MUTATION_RECORD_V1"
STRATEGY_MUTATION_SCHEMA_VERSION_V1 = 1
STRATEGY_MUTATION_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_MUTATION_RECORD_V1\x00"
STRATEGY_MUTATION_COMPLEXITY_RULE_V1 = "EXACT_CHILD_MINUS_PARENT_V1"
STRATEGY_MUTATION_SEMANTIC_VALIDATION_V1 = (
    "TYPED_AST_CONSTRUCTION_AND_CANONICAL_RENDER_ROUND_TRIP_V1"
)
MAX_MUTATION_CONDITIONS_V1 = 64
MAX_MUTATION_EVENT_COUNT_V1 = 1_000_000
MAX_MUTATION_REQUEST_BYTES_V1 = 16_384
MAX_MUTATION_STATES_V1 = 32
MAX_MUTATION_TRANSITIONS_V1 = 64
MAX_MUTATION_PARAMETERS_V1 = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_INDEX = r"(?:0|[1-9]\d*)"
_CONDITION_PATH = re.compile(
    rf"^/(green_conditions|wait_conditions)/({_CANONICAL_INDEX})$|"
    rf"^/transitions/({_CANONICAL_INDEX})/conditions/({_CANONICAL_INDEX})$"
)
_COLLECTION_PATH = re.compile(
    rf"^/(green_conditions|wait_conditions)$|"
    rf"^/transitions/({_CANONICAL_INDEX})/conditions$"
)
_FUTURE_FEATURE_PREFIXES = ("future_", "lookahead_", "next_")
_MARKET_FEATURES = frozenset(item.value for item in FeatureName)
_POSITION_FEATURES = frozenset(item.value for item in PositionFeature)
_ALL_FEATURES = _MARKET_FEATURES | _POSITION_FEATURES
_VOLUME_FEATURES = frozenset(
    {
        FeatureName.AGGRESSIVE_BUY_VOLUME.value,
        FeatureName.AGGRESSIVE_SELL_VOLUME.value,
        FeatureName.BUY_SELL_RATIO.value,
        FeatureName.RELATIVE_VOLUME.value,
        FeatureName.TRADE_VELOCITY.value,
    }
)


class MutationOperationIdV1(str, Enum):
    THRESHOLD = "THRESHOLD"
    ROLLING_WINDOW = "ROLLING_WINDOW"
    REQUIRED_DURATION = "REQUIRED_DURATION"
    ADD_CONDITION = "ADD_CONDITION"
    REMOVE_CONDITION = "REMOVE_CONDITION"
    FEATURE_REPLACEMENT = "FEATURE_REPLACEMENT"
    LOGICAL_OPERATOR = "LOGICAL_OPERATOR"
    TRANSITION_CONDITION = "TRANSITION_CONDITION"
    COOLDOWN = "COOLDOWN"
    STATE_TIMEOUT = "STATE_TIMEOUT"
    CONFIRMATION_COUNT = "CONFIRMATION_COUNT"
    INVALIDATION_RULE = "INVALIDATION_RULE"
    POSITION_CONSTRAINT = "POSITION_CONSTRAINT"
    SPREAD_LIMIT = "SPREAD_LIMIT"
    VOLUME_REQUIREMENT = "VOLUME_REQUIREMENT"


class MutationStatusV1(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class MutationRejectionReasonV1(str, Enum):
    NONE = "NONE"
    INPUT_KIND_UNSUPPORTED = "INPUT_KIND_UNSUPPORTED"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    INVALID_CHILD = "INVALID_CHILD"
    NO_OP = "NO_OP"
    DUPLICATE = "DUPLICATE"
    FUTURE_DEPENDENT = "FUTURE_DEPENDENT"
    UNAVAILABLE_FEATURE = "UNAVAILABLE_FEATURE"
    RESOURCE_EXCESSIVE = "RESOURCE_EXCESSIVE"
    PERMISSION_WIDENING = "PERMISSION_WIDENING"
    UNSUPPORTED_OPERATION_VERSION = "UNSUPPORTED_OPERATION_VERSION"


@dataclass(frozen=True, slots=True)
class MutationResourceLimitsV1:
    max_conditions: int = MAX_MUTATION_CONDITIONS_V1
    max_features: int = len(_ALL_FEATURES)
    max_states: int = MAX_MUTATION_STATES_V1
    max_transitions: int = MAX_MUTATION_TRANSITIONS_V1
    max_rolling_windows: int = 1
    max_parameters: int = MAX_MUTATION_PARAMETERS_V1

    def __post_init__(self) -> None:
        values = self.as_dict()
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise ValueError("mutation resource limits must be positive integers")
        hard_bounds = {
            "max_conditions": MAX_MUTATION_CONDITIONS_V1,
            "max_features": len(_ALL_FEATURES),
            "max_parameters": MAX_MUTATION_PARAMETERS_V1,
            "max_rolling_windows": 1,
            "max_states": MAX_MUTATION_STATES_V1,
            "max_transitions": MAX_MUTATION_TRANSITIONS_V1,
        }
        if any(values[name] > bound for name, bound in hard_bounds.items()):
            raise ValueError("mutation resource limits cannot widen the fixed bounds")

    def admits(self, complexity: StrategyComplexityV1) -> bool:
        if not isinstance(complexity, StrategyComplexityV1):
            raise TypeError("mutation resource check requires typed complexity")
        return (
            complexity.conditions <= self.max_conditions
            and complexity.features <= self.max_features
            and complexity.states <= self.max_states
            and complexity.transitions <= self.max_transitions
            and complexity.rolling_windows <= self.max_rolling_windows
            and complexity.parameters <= self.max_parameters
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "max_conditions": self.max_conditions,
            "max_features": self.max_features,
            "max_parameters": self.max_parameters,
            "max_rolling_windows": self.max_rolling_windows,
            "max_states": self.max_states,
            "max_transitions": self.max_transitions,
        }


@dataclass(frozen=True, slots=True)
class MutationOperatorSpecV1:
    operation_id: MutationOperationIdV1
    operation_version: int
    input_node_kinds: tuple[StrategyAstKindV1, ...]
    parameter_domain: Mapping[str, object]
    observability_requirements: tuple[str, ...]
    semantic_validation: str
    machine_reason: str
    human_reason: str
    inverse_description: str
    diff_description: str
    complexity_delta_rule: str = STRATEGY_MUTATION_COMPLEXITY_RULE_V1

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, MutationOperationIdV1):
            raise TypeError("mutation operation ID is invalid")
        if type(self.operation_version) is not int or self.operation_version <= 0:
            raise ValueError("mutation operation version must be positive")
        if type(self.input_node_kinds) is not tuple or not self.input_node_kinds or any(
            not isinstance(item, StrategyAstKindV1) for item in self.input_node_kinds
        ):
            raise TypeError("mutation input node kinds must be a nonempty typed tuple")
        kinds = tuple(sorted(set(self.input_node_kinds), key=lambda item: item.value))
        object.__setattr__(self, "input_node_kinds", kinds)
        if not isinstance(self.parameter_domain, Mapping):
            raise TypeError("mutation parameter domain must be a mapping")
        domain = thaw_json(self.parameter_domain)
        canonical_identity_bytes(domain)
        object.__setattr__(self, "parameter_domain", freeze_json(domain))
        if type(self.observability_requirements) is not tuple or any(
            type(item) is not str or item not in _ALL_FEATURES
            for item in self.observability_requirements
        ):
            raise ValueError("mutation observability requirements are invalid")
        requirements = tuple(sorted(set(self.observability_requirements)))
        object.__setattr__(self, "observability_requirements", requirements)
        for value, context in (
            (self.machine_reason, "mutation machine reason"),
            (self.human_reason, "mutation human reason"),
            (self.inverse_description, "mutation inverse description"),
            (self.diff_description, "mutation diff description"),
            (self.semantic_validation, "mutation semantic validation"),
            (self.complexity_delta_rule, "mutation complexity-delta rule"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{context} must be nonempty text")
            canonical_identity_bytes(value)

    def as_dict(self) -> dict[str, object]:
        return {
            "complexity_delta_rule": self.complexity_delta_rule,
            "diff_description": self.diff_description,
            "human_reason": self.human_reason,
            "input_node_kinds": [item.value for item in self.input_node_kinds],
            "inverse_description": self.inverse_description,
            "machine_reason": self.machine_reason,
            "observability_requirements": list(self.observability_requirements),
            "operation_id": self.operation_id.value,
            "operation_version": self.operation_version,
            "parameter_domain": thaw_json(self.parameter_domain),
            "semantic_validation": self.semantic_validation,
        }


@dataclass(frozen=True, slots=True)
class MutationRequestV1:
    operation_id: MutationOperationIdV1
    operation_version: int
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, MutationOperationIdV1):
            raise TypeError("mutation request operation ID is invalid")
        if type(self.operation_version) is not int or self.operation_version <= 0:
            raise ValueError("mutation request operation version must be positive")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("mutation request parameters must be a mapping")
        parameters = thaw_json(self.parameters)
        if len(canonical_identity_bytes(parameters)) > MAX_MUTATION_REQUEST_BYTES_V1:
            raise ValueError("mutation request exceeds the canonical byte limit")
        object.__setattr__(self, "parameters", freeze_json(parameters))

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id.value,
            "operation_version": self.operation_version,
            "parameters": thaw_json(self.parameters),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class StrategyMutationRecordV1:
    parent_strategy_id: str
    parent_semantic_sha256: str
    child_strategy_id: str
    child_semantic_sha256: str
    operation_id: MutationOperationIdV1
    operation_version: int
    parameters: Mapping[str, object]
    status: MutationStatusV1
    rejection_reason: MutationRejectionReasonV1
    evaluation_eligible: bool
    machine_reason: str
    human_reason: str
    rng_substream: StrategyRngSubstreamV1
    mutation_diff: StrategyMutationDiffV1
    complexity_before: StrategyComplexityV1
    complexity_after: StrategyComplexityV1
    complexity_delta: StrategyComplexityDeltaV1
    lineage: StrategyLineageNodeV1
    schema_version: int = STRATEGY_MUTATION_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _require_strategy_id(
            self.parent_strategy_id,
            self.parent_semantic_sha256,
            "parent",
        )
        _require_strategy_id(
            self.child_strategy_id,
            self.child_semantic_sha256,
            "child",
        )
        if not isinstance(self.operation_id, MutationOperationIdV1):
            raise TypeError("mutation record operation ID is invalid")
        if type(self.operation_version) is not int or self.operation_version <= 0:
            raise ValueError("mutation record operation version must be positive")
        if (
            type(self.schema_version) is not int
            or self.schema_version != STRATEGY_MUTATION_SCHEMA_VERSION_V1
        ):
            raise ValueError("unsupported strategy mutation record schema")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("mutation record parameters must be a mapping")
        parameters = thaw_json(self.parameters)
        if len(canonical_identity_bytes(parameters)) > MAX_MUTATION_REQUEST_BYTES_V1:
            raise ValueError("mutation record parameters exceed the canonical byte limit")
        object.__setattr__(self, "parameters", freeze_json(parameters))
        if not isinstance(self.status, MutationStatusV1) or not isinstance(
            self.rejection_reason,
            MutationRejectionReasonV1,
        ):
            raise TypeError("mutation record status or reason is invalid")
        if type(self.evaluation_eligible) is not bool:
            raise TypeError("mutation evaluation eligibility must be boolean")
        for value, context in (
            (self.machine_reason, "mutation record machine reason"),
            (self.human_reason, "mutation record human reason"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{context} must be nonempty text")
            canonical_identity_bytes(value)
        if not isinstance(self.rng_substream, StrategyRngSubstreamV1):
            raise TypeError("mutation record RNG substream is invalid")
        if not isinstance(self.mutation_diff, StrategyMutationDiffV1):
            raise TypeError("mutation record semantic diff is invalid")
        if not isinstance(self.complexity_before, StrategyComplexityV1) or not isinstance(
            self.complexity_after,
            StrategyComplexityV1,
        ):
            raise TypeError("mutation record complexity endpoints are invalid")
        if not isinstance(self.complexity_delta, StrategyComplexityDeltaV1):
            raise TypeError("mutation record complexity delta is invalid")
        if self.complexity_delta != StrategyComplexityDeltaV1.between(
            self.complexity_before,
            self.complexity_after,
        ):
            raise ValueError("mutation record complexity delta is not exact")
        if not isinstance(self.lineage, StrategyLineageNodeV1):
            raise TypeError("mutation record lineage is invalid")
        if (
            self.lineage.parent_semantic_sha256 != self.parent_semantic_sha256
            or self.lineage.child_semantic_sha256 != self.child_semantic_sha256
            or self.lineage.operation_id != self.operation_id.value
            or self.lineage.operation_version != self.operation_version
            or thaw_json(self.lineage.parameters) != thaw_json(self.parameters)
            or self.lineage.rng_substream != self.rng_substream
            or self.lineage.semantic_diff != self.mutation_diff.semantic_diff
        ):
            raise ValueError("mutation record and lineage disagree")
        accepted = self.status is MutationStatusV1.ACCEPTED
        if accepted != self.evaluation_eligible or accepted != self.lineage.valid:
            raise ValueError("mutation acceptance and evaluation eligibility disagree")
        if accepted != (self.rejection_reason is MutationRejectionReasonV1.NONE):
            raise ValueError("mutation status and rejection reason disagree")
        if accepted and not self.mutation_diff.semantic_diff:
            raise ValueError("accepted mutation must carry a semantic diff")

    @property
    def record_sha256(self) -> str:
        raw = self.canonical_bytes()
        digest = hashlib.sha256()
        digest.update(STRATEGY_MUTATION_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "child_semantic_sha256": self.child_semantic_sha256,
            "child_strategy_id": self.child_strategy_id,
            "complexity_after": self.complexity_after.as_dict(),
            "complexity_before": self.complexity_before.as_dict(),
            "complexity_delta": self.complexity_delta.as_dict(),
            "evaluation_eligible": self.evaluation_eligible,
            "human_reason": self.human_reason,
            "lineage": self.lineage.as_dict(),
            "machine_reason": self.machine_reason,
            "mutation_diff": self.mutation_diff.as_dict(),
            "operation_id": self.operation_id.value,
            "operation_version": self.operation_version,
            "parameters": thaw_json(self.parameters),
            "parent_semantic_sha256": self.parent_semantic_sha256,
            "parent_strategy_id": self.parent_strategy_id,
            "rejection_reason": self.rejection_reason.value,
            "rng_substream": self.rng_substream.as_dict(),
            "schema_id": STRATEGY_MUTATION_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class StrategyMutationResultV1:
    child: StrategyAstV1
    record: StrategyMutationRecordV1

    def __post_init__(self) -> None:
        _require_ast(self.child)
        if not isinstance(self.record, StrategyMutationRecordV1):
            raise TypeError("strategy mutation result record is invalid")
        if strategy_semantic_sha256(self.child) != self.record.child_semantic_sha256:
            raise ValueError("strategy mutation result child and record disagree")


def strategy_id(ast: StrategyAstV1) -> str:
    _require_ast(ast)
    return "strategy-" + strategy_semantic_sha256(ast)


def mutation_operator_spec(
    operation_id: MutationOperationIdV1,
    operation_version: int = 1,
) -> MutationOperatorSpecV1:
    if not isinstance(operation_id, MutationOperationIdV1):
        raise TypeError("mutation operator lookup requires MutationOperationIdV1")
    for spec in REQUIRED_MUTATION_OPERATORS_V1:
        if (
            spec.operation_id is operation_id
            and spec.operation_version == operation_version
        ):
            return spec
    raise KeyError((operation_id.value, operation_version))


def apply_strategy_mutation(
    parent: StrategyAstV1,
    request: MutationRequestV1,
    *,
    rng_substream: StrategyRngSubstreamV1,
    available_features: tuple[str, ...],
    resource_limits: MutationResourceLimitsV1 = MutationResourceLimitsV1(),
    known_semantic_sha256: tuple[str, ...] = (),
) -> StrategyMutationResultV1:
    _require_ast(parent)
    if not isinstance(request, MutationRequestV1):
        raise TypeError("strategy mutation requires a typed request")
    if not isinstance(rng_substream, StrategyRngSubstreamV1):
        raise TypeError("strategy mutation requires a labeled RNG substream")
    if type(available_features) is not tuple or any(
        type(item) is not str or item not in _ALL_FEATURES
        for item in available_features
    ):
        raise ValueError("available mutation features are invalid")
    available = tuple(sorted(set(available_features)))
    if len(available) != len(available_features):
        raise ValueError("available mutation features must be unique")
    if not isinstance(resource_limits, MutationResourceLimitsV1):
        raise TypeError("strategy mutation resource limits are invalid")
    _require_digest_tuple(known_semantic_sha256, "known strategy digests")
    parent_complexity = strategy_complexity(parent)
    try:
        spec = mutation_operator_spec(request.operation_id, request.operation_version)
    except KeyError:
        return _rejected_result(
            parent,
            parent,
            request,
            rng_substream,
            parent_complexity,
            "/",
            MutationRejectionReasonV1.UNSUPPORTED_OPERATION_VERSION,
            "The requested mutation operation version is not registered.",
            "No inverse exists because no mutation was applied.",
        )
    if parent.kind not in spec.input_node_kinds:
        return _rejected_result(
            parent,
            parent,
            request,
            rng_substream,
            parent_complexity,
            "/",
            MutationRejectionReasonV1.INPUT_KIND_UNSUPPORTED,
            "The operation does not accept this canonical AST node kind.",
            spec.inverse_description,
        )
    requested_features = _requested_features(thaw_json(request.parameters))
    if any(
        feature.startswith(_FUTURE_FEATURE_PREFIXES)
        for feature in requested_features
    ):
        return _rejected_result(
            parent,
            parent,
            request,
            rng_substream,
            parent_complexity,
            "/",
            MutationRejectionReasonV1.FUTURE_DEPENDENT,
            "The mutation requested a future-dependent feature.",
            spec.inverse_description,
        )
    try:
        child, affected_path = _transform(parent, request)
    except _MutationRefusal as refusal:
        return _rejected_result(
            parent,
            parent,
            request,
            rng_substream,
            parent_complexity,
            refusal.path,
            refusal.reason,
            refusal.human_reason,
            spec.inverse_description,
        )
    except (IndexError, KeyError, TypeError, ValueError) as error:
        return _rejected_result(
            parent,
            parent,
            request,
            rng_substream,
            parent_complexity,
            "/",
            MutationRejectionReasonV1.INVALID_PARAMETER,
            f"The mutation parameters are invalid: {type(error).__name__}.",
            spec.inverse_description,
        )
    try:
        if strategy_ast_round_trip(child) != child:
            raise ValueError("canonical AST render round trip changed the child")
    except (TypeError, ValueError):
        return _rejected_result(
            parent,
            parent,
            request,
            rng_substream,
            parent_complexity,
            affected_path,
            MutationRejectionReasonV1.INVALID_CHILD,
            "The typed child failed canonical semantic validation.",
            spec.inverse_description,
        )
    if _permission_projection(parent) != _permission_projection(child):
        return _rejected_result(
            parent,
            child,
            request,
            rng_substream,
            parent_complexity,
            affected_path,
            MutationRejectionReasonV1.PERMISSION_WIDENING,
            "The mutation attempted to widen strategy permissions.",
            spec.inverse_description,
        )
    required_features = _strategy_features(child) | set(
        spec.observability_requirements
    )
    if not required_features <= set(available):
        return _rejected_result(
            parent,
            child,
            request,
            rng_substream,
            parent_complexity,
            affected_path,
            MutationRejectionReasonV1.UNAVAILABLE_FEATURE,
            "The mutation requires a feature outside the declared observation surface.",
            spec.inverse_description,
        )
    parent_digest = strategy_semantic_sha256(parent)
    child_digest = strategy_semantic_sha256(child)
    if child_digest == parent_digest:
        return _rejected_result(
            parent,
            child,
            request,
            rng_substream,
            parent_complexity,
            affected_path,
            MutationRejectionReasonV1.NO_OP,
            "Canonicalization collapsed the mutation to a semantic no-op.",
            spec.inverse_description,
        )
    child_complexity = strategy_complexity(child)
    if not resource_limits.admits(child_complexity):
        return _rejected_result(
            parent,
            child,
            request,
            rng_substream,
            parent_complexity,
            affected_path,
            MutationRejectionReasonV1.RESOURCE_EXCESSIVE,
            "The mutation exceeds the declared deterministic resource limits.",
            spec.inverse_description,
        )
    if child_digest in set(known_semantic_sha256):
        return _rejected_result(
            parent,
            child,
            request,
            rng_substream,
            parent_complexity,
            affected_path,
            MutationRejectionReasonV1.DUPLICATE,
            "The canonical child already exists in the candidate inventory.",
            spec.inverse_description,
        )
    return _result(
        parent,
        child,
        request,
        rng_substream,
        affected_path,
        MutationStatusV1.ACCEPTED,
        MutationRejectionReasonV1.NONE,
        spec.machine_reason,
        spec.human_reason,
        spec.diff_description,
        spec.inverse_description,
    )


def _result(
    parent: StrategyAstV1,
    child: StrategyAstV1,
    request: MutationRequestV1,
    rng_substream: StrategyRngSubstreamV1,
    affected_path: str,
    status: MutationStatusV1,
    rejection_reason: MutationRejectionReasonV1,
    machine_reason: str,
    human_reason: str,
    diff_description: str,
    inverse_description: str,
) -> StrategyMutationResultV1:
    before = strategy_complexity(parent)
    after = strategy_complexity(child)
    mutation_diff = build_mutation_diff(
        parent,
        child,
        affected_rule_path=affected_path,
        human_description=diff_description,
        inverse_description=inverse_description,
    )
    accepted = status is MutationStatusV1.ACCEPTED
    lineage = build_strategy_lineage_node(
        parent,
        child,
        operation_id=request.operation_id.value,
        operation_version=request.operation_version,
        parameters=thaw_json(request.parameters),
        rng_substream=rng_substream,
        valid=accepted,
    )
    record = StrategyMutationRecordV1(
        parent_strategy_id=strategy_id(parent),
        parent_semantic_sha256=strategy_semantic_sha256(parent),
        child_strategy_id=strategy_id(child),
        child_semantic_sha256=strategy_semantic_sha256(child),
        operation_id=request.operation_id,
        operation_version=request.operation_version,
        parameters=thaw_json(request.parameters),
        status=status,
        rejection_reason=rejection_reason,
        evaluation_eligible=accepted,
        machine_reason=machine_reason,
        human_reason=human_reason,
        rng_substream=rng_substream,
        mutation_diff=mutation_diff,
        complexity_before=before,
        complexity_after=after,
        complexity_delta=StrategyComplexityDeltaV1.between(before, after),
        lineage=lineage,
    )
    return StrategyMutationResultV1(child, record)


def _rejected_result(
    parent: StrategyAstV1,
    child: StrategyAstV1,
    request: MutationRequestV1,
    rng_substream: StrategyRngSubstreamV1,
    parent_complexity: StrategyComplexityV1,
    affected_path: str,
    reason: MutationRejectionReasonV1,
    human_reason: str,
    inverse_description: str,
) -> StrategyMutationResultV1:
    if strategy_complexity(parent) != parent_complexity:
        raise AssertionError("parent complexity changed during mutation")
    return _result(
        parent,
        child,
        request,
        rng_substream,
        affected_path,
        MutationStatusV1.REJECTED,
        reason,
        reason.value,
        human_reason,
        "No evaluation-eligible semantic change was produced.",
        inverse_description,
    )


class _MutationRefusal(ValueError):
    def __init__(
        self,
        reason: MutationRejectionReasonV1,
        path: str,
        human_reason: str,
    ) -> None:
        self.reason = reason
        self.path = path
        self.human_reason = human_reason
        super().__init__(human_reason)


def _transform(
    parent: StrategyAstV1,
    request: MutationRequestV1,
) -> tuple[StrategyAstV1, str]:
    parameters = thaw_json(request.parameters)
    operation = request.operation_id
    if operation is MutationOperationIdV1.THRESHOLD:
        _exact_parameters(parameters, {"path", "threshold"})
        path = _text_parameter(parameters, "path")
        condition = _condition_at(parent, path)
        child = _replace_condition(
            parent,
            path,
            replace(condition, threshold=_decimal_parameter(parameters["threshold"])),
        )
        return child, f"{path}/threshold"
    if operation is MutationOperationIdV1.ROLLING_WINDOW:
        _exact_parameters(parameters, {"window_us"})
        child = replace(
            parent,
            window_us=_positive_duration(parameters, "window_us"),
        )
        return child, "/window_us"
    if operation is MutationOperationIdV1.REQUIRED_DURATION:
        _exact_parameters(parameters, {"duration_us", "transition_index"})
        machine = _machine(parent)
        index = _nonnegative_integer(parameters, "transition_index")
        transition = _transition(machine, index)
        if transition.qualifier not in {
            TimeQualifier.TRUE_FOR,
            TimeQualifier.OCCURRED_WITHIN,
            TimeQualifier.EVENTS_WITHIN,
        }:
            raise _MutationRefusal(
                MutationRejectionReasonV1.INVALID_PARAMETER,
                f"/transitions/{index}",
                "Required duration applies only to a duration-qualified transition.",
            )
        child = _replace_transition(
            machine,
            index,
            replace(
                transition,
                duration_us=_positive_duration(parameters, "duration_us"),
            ),
        )
        return child, f"/transitions/{index}/duration_us"
    if operation is MutationOperationIdV1.ADD_CONDITION:
        _exact_parameters(parameters, {"collection_path", "condition"})
        path = _text_parameter(parameters, "collection_path")
        condition = _condition_parameter(parameters["condition"])
        return _add_condition(parent, path, condition), path
    if operation is MutationOperationIdV1.REMOVE_CONDITION:
        _exact_parameters(parameters, {"path"})
        path = _text_parameter(parameters, "path")
        return _remove_condition(parent, path), path
    if operation is MutationOperationIdV1.FEATURE_REPLACEMENT:
        _exact_parameters(parameters, {"feature", "path"})
        path = _text_parameter(parameters, "path")
        feature = _text_parameter(parameters, "feature")
        condition = _condition_at(parent, path)
        child = _replace_condition(parent, path, replace(condition, feature=feature))
        return child, f"{path}/feature"
    if operation is MutationOperationIdV1.LOGICAL_OPERATOR:
        _exact_parameters(parameters, {"operator", "path"})
        path = _text_parameter(parameters, "path")
        operator = ComparisonOperator(_text_parameter(parameters, "operator"))
        condition = _condition_at(parent, path)
        child = _replace_condition(parent, path, replace(condition, operator=operator))
        return child, f"{path}/operator"
    if operation is MutationOperationIdV1.TRANSITION_CONDITION:
        _exact_parameters(parameters, {"condition", "path"})
        path = _text_parameter(parameters, "path")
        if not path.startswith("/transitions/"):
            raise ValueError("transition-condition path must address a transition")
        condition = _condition_parameter(parameters["condition"])
        return _replace_condition(parent, path, condition), path
    if operation is MutationOperationIdV1.COOLDOWN:
        _exact_parameters(parameters, {"cooldown_us", "state_name"})
        machine = _machine(parent)
        state_name = _text_parameter(parameters, "state_name")
        state_index, _ = _state(machine, state_name)
        child = replace(
            machine,
            states=tuple(
                replace(item, cooldown_us=_nonnegative_duration(parameters, "cooldown_us"))
                if item.name == state_name
                else item
                for item in machine.states
            ),
        )
        return child, f"/states/{state_index}/cooldown_us"
    if operation is MutationOperationIdV1.STATE_TIMEOUT:
        _exact_parameters(parameters, {"timeout_us", "transition_index"})
        machine = _machine(parent)
        index = _nonnegative_integer(parameters, "transition_index")
        transition = _transition(machine, index)
        if transition.qualifier is not TimeQualifier.AFTER_ENTRY:
            raise _MutationRefusal(
                MutationRejectionReasonV1.INVALID_PARAMETER,
                f"/transitions/{index}",
                "State timeout requires an existing after-entry transition.",
            )
        timeout_condition = ComparisonNodeV1(
            PositionFeature.WORKING_ORDER_COUNT.value,
            ComparisonOperator.GREATER_EQUAL,
            ExactDecimalV1(0, 0),
        )
        timeout = replace(
            transition,
            qualifier=TimeQualifier.TRUE_FOR,
            conditions=(timeout_condition,),
            duration_us=_positive_duration(parameters, "timeout_us"),
        )
        return _replace_transition(machine, index, timeout), f"/transitions/{index}"
    if operation is MutationOperationIdV1.CONFIRMATION_COUNT:
        _exact_parameters(parameters, {"event_count", "transition_index"})
        machine = _machine(parent)
        index = _nonnegative_integer(parameters, "transition_index")
        transition = _transition(machine, index)
        if transition.qualifier is not TimeQualifier.EVENTS_WITHIN:
            raise _MutationRefusal(
                MutationRejectionReasonV1.INVALID_PARAMETER,
                f"/transitions/{index}",
                "Confirmation count requires an events-within transition.",
            )
        child = _replace_transition(
            machine,
            index,
            replace(
                transition,
                event_count=_positive_integer(parameters, "event_count"),
            ),
        )
        return child, f"/transitions/{index}/event_count"
    if operation is MutationOperationIdV1.INVALIDATION_RULE:
        _exact_parameters(parameters, {"condition", "transition_index"})
        machine = _machine(parent)
        index = _nonnegative_integer(parameters, "transition_index")
        transition = _transition(machine, index)
        target_names = [item.name for item in machine.states]
        target = machine.states[target_names.index(transition.target_state)]
        if (
            target.signal is not TrafficState.RED
            or transition.qualifier is TimeQualifier.AFTER_ENTRY
        ):
            raise _MutationRefusal(
                MutationRejectionReasonV1.INVALID_PARAMETER,
                f"/transitions/{index}",
                "Invalidation rules require a condition-based transition to a RED state.",
            )
        path = f"/transitions/{index}/conditions"
        condition = _condition_parameter(parameters["condition"])
        return _add_condition(machine, path, condition), path
    if operation is MutationOperationIdV1.POSITION_CONSTRAINT:
        _exact_parameters(
            parameters,
            {"feature", "operator", "threshold", "transition_index"},
        )
        feature = _text_parameter(parameters, "feature")
        if feature not in _POSITION_FEATURES:
            raise ValueError("position constraint feature is outside the position surface")
        condition = ComparisonNodeV1(
            feature,
            ComparisonOperator(_text_parameter(parameters, "operator")),
            _decimal_parameter(parameters["threshold"]),
        )
        index = _nonnegative_integer(parameters, "transition_index")
        path = f"/transitions/{index}/conditions"
        return _add_condition(_machine(parent), path, condition), path
    if operation is MutationOperationIdV1.SPREAD_LIMIT:
        _exact_parameters(parameters, {"collection_path", "max_spread_ticks"})
        path = _text_parameter(parameters, "collection_path")
        condition = ComparisonNodeV1(
            FeatureName.SPREAD_TICKS.value,
            ComparisonOperator.LESS_EQUAL,
            _nonnegative_decimal_parameter(parameters["max_spread_ticks"]),
        )
        return _add_condition(parent, path, condition), path
    if operation is MutationOperationIdV1.VOLUME_REQUIREMENT:
        _exact_parameters(parameters, {"collection_path", "feature", "minimum"})
        path = _text_parameter(parameters, "collection_path")
        feature = _text_parameter(parameters, "feature")
        if feature not in _VOLUME_FEATURES:
            raise ValueError("volume requirement feature is outside the volume surface")
        condition = ComparisonNodeV1(
            feature,
            ComparisonOperator.GREATER_EQUAL,
            _nonnegative_decimal_parameter(parameters["minimum"]),
        )
        return _add_condition(parent, path, condition), path
    raise AssertionError(f"unhandled mutation operation: {operation.value}")


def _condition_at(ast: StrategyAstV1, path: str) -> ComparisonNodeV1:
    match = _CONDITION_PATH.fullmatch(path)
    if match is None:
        raise ValueError("condition path is outside the canonical strategy AST")
    traffic_collection, traffic_index, transition_index, condition_index = match.groups()
    if traffic_collection is not None:
        if not isinstance(ast, TrafficLightStrategyAstV1):
            raise ValueError("traffic-light condition path used for a state machine")
        values = getattr(ast, traffic_collection)
        return values[int(traffic_index)]
    machine = _machine(ast)
    return _transition(machine, int(transition_index)).conditions[int(condition_index)]


def _replace_condition(
    ast: StrategyAstV1,
    path: str,
    condition: ComparisonNodeV1,
) -> StrategyAstV1:
    match = _CONDITION_PATH.fullmatch(path)
    if match is None:
        raise ValueError("condition path is outside the canonical strategy AST")
    traffic_collection, traffic_index, transition_index, condition_index = match.groups()
    if traffic_collection is not None:
        if not isinstance(ast, TrafficLightStrategyAstV1):
            raise ValueError("traffic-light condition path used for a state machine")
        values = list(getattr(ast, traffic_collection))
        values[int(traffic_index)] = condition
        return replace(ast, **{traffic_collection: tuple(values)})
    machine = _machine(ast)
    transition = _transition(machine, int(transition_index))
    conditions = list(transition.conditions)
    conditions[int(condition_index)] = condition
    return _replace_transition(
        machine,
        int(transition_index),
        replace(transition, conditions=tuple(conditions)),
    )


def _add_condition(
    ast: StrategyAstV1,
    collection_path: str,
    condition: ComparisonNodeV1,
) -> StrategyAstV1:
    match = _COLLECTION_PATH.fullmatch(collection_path)
    if match is None:
        raise ValueError("condition collection path is outside the canonical AST")
    traffic_collection, transition_index = match.groups()
    if traffic_collection is not None:
        if not isinstance(ast, TrafficLightStrategyAstV1):
            raise ValueError("traffic-light condition collection used for a state machine")
        values = (*getattr(ast, traffic_collection), condition)
        return replace(ast, **{traffic_collection: values})
    machine = _machine(ast)
    index = int(transition_index)
    transition = _transition(machine, index)
    if transition.qualifier is TimeQualifier.AFTER_ENTRY:
        raise ValueError("after-entry transition cannot carry conditions")
    return _replace_transition(
        machine,
        index,
        replace(transition, conditions=(*transition.conditions, condition)),
    )


def _remove_condition(ast: StrategyAstV1, path: str) -> StrategyAstV1:
    match = _CONDITION_PATH.fullmatch(path)
    if match is None:
        raise ValueError("condition path is outside the canonical strategy AST")
    traffic_collection, traffic_index, transition_index, condition_index = match.groups()
    if traffic_collection is not None:
        if not isinstance(ast, TrafficLightStrategyAstV1):
            raise ValueError("traffic-light condition path used for a state machine")
        values = list(getattr(ast, traffic_collection))
        target_index = int(traffic_index)
        _ = values[target_index]
        if len(values) == 1:
            raise _MutationRefusal(
                MutationRejectionReasonV1.INVALID_CHILD,
                path,
                "Removing the condition would leave a required rule empty.",
            )
        del values[target_index]
        return replace(ast, **{traffic_collection: tuple(values)})
    machine = _machine(ast)
    index = int(transition_index)
    transition = _transition(machine, index)
    conditions = list(transition.conditions)
    target_index = int(condition_index)
    _ = conditions[target_index]
    if len(conditions) == 1:
        raise _MutationRefusal(
            MutationRejectionReasonV1.INVALID_CHILD,
            path,
            "Removing the condition would leave a required transition rule empty.",
        )
    del conditions[target_index]
    return _replace_transition(
        machine,
        index,
        replace(transition, conditions=tuple(conditions)),
    )


def _machine(ast: StrategyAstV1) -> StateMachineStrategyAstV1:
    if not isinstance(ast, StateMachineStrategyAstV1):
        raise ValueError("mutation operation requires a state-machine AST")
    return ast


def _transition(
    machine: StateMachineStrategyAstV1,
    index: int,
) -> TransitionNodeV1:
    if type(index) is not int or index < 0:
        raise ValueError("transition index must be nonnegative")
    return machine.transitions[index]


def _replace_transition(
    machine: StateMachineStrategyAstV1,
    index: int,
    transition: TransitionNodeV1,
) -> StateMachineStrategyAstV1:
    values = list(machine.transitions)
    values[index] = transition
    return replace(machine, transitions=tuple(values))


def _state(
    machine: StateMachineStrategyAstV1,
    state_name: str,
) -> tuple[int, StateNodeV1]:
    for index, state in enumerate(machine.states):
        if state.name == state_name:
            return index, state
    raise KeyError(state_name)


def _condition_parameter(value: object) -> ComparisonNodeV1:
    if type(value) is not dict or set(value) != {"feature", "operator", "threshold"}:
        raise ValueError("condition parameter fields are not exact")
    feature = value["feature"]
    operator = value["operator"]
    if type(feature) is not str or type(operator) is not str:
        raise TypeError("condition feature and operator must be text")
    return ComparisonNodeV1(
        feature,
        ComparisonOperator(operator),
        _decimal_parameter(value["threshold"]),
    )


def _decimal_parameter(value: object) -> ExactDecimalV1:
    if type(value) is not dict or set(value) != {"coefficient", "scale"}:
        raise ValueError("exact-decimal parameter fields are not exact")
    coefficient = value["coefficient"]
    scale = value["scale"]
    if type(coefficient) is not int or type(scale) is not int:
        raise TypeError("exact-decimal parameters must be integers")
    return ExactDecimalV1(coefficient, scale)


def _nonnegative_decimal_parameter(value: object) -> ExactDecimalV1:
    decimal = _decimal_parameter(value)
    if decimal.coefficient < 0:
        raise ValueError("mutation parameter requires a nonnegative exact decimal")
    return decimal


def _exact_parameters(parameters: object, expected: set[str]) -> None:
    if type(parameters) is not dict:
        raise TypeError("mutation parameters must be an object")
    if set(parameters) != expected:
        raise ValueError("mutation parameter fields are not exact")


def _text_parameter(parameters: dict[str, object], key: str) -> str:
    value = parameters[key]
    if type(value) is not str or not value:
        raise ValueError(f"mutation parameter {key} must be nonempty text")
    return value


def _nonnegative_integer(parameters: dict[str, object], key: str) -> int:
    value = parameters[key]
    if type(value) is not int or value < 0:
        raise ValueError(f"mutation parameter {key} must be nonnegative")
    return value


def _positive_integer(parameters: dict[str, object], key: str) -> int:
    value = _nonnegative_integer(parameters, key)
    if value == 0:
        raise ValueError(f"mutation parameter {key} must be positive")
    if value > MAX_MUTATION_EVENT_COUNT_V1:
        raise ValueError(f"mutation parameter {key} exceeds the bounded domain")
    return value


def _nonnegative_duration(parameters: dict[str, object], key: str) -> int:
    value = _nonnegative_integer(parameters, key)
    if value % 1_000:
        raise ValueError(f"mutation duration {key} must be renderable in milliseconds")
    return value


def _positive_duration(parameters: dict[str, object], key: str) -> int:
    value = _nonnegative_duration(parameters, key)
    if value == 0:
        raise ValueError(f"mutation duration {key} must be positive")
    return value


def _strategy_features(ast: StrategyAstV1) -> set[str]:
    if isinstance(ast, TrafficLightStrategyAstV1):
        return {
            item.feature for item in ast.green_conditions + ast.wait_conditions
        }
    return {
        item.feature
        for transition in ast.transitions
        for item in transition.conditions
    }


def _requested_features(value: object) -> tuple[str, ...]:
    found: list[str] = []

    def visit(item: object, key: str | None = None) -> None:
        if type(item) is dict:
            for child_key in sorted(item):
                visit(item[child_key], child_key)
        elif type(item) is list:
            for child in item:
                visit(child, key)
        elif key == "feature" and type(item) is str:
            found.append(item)

    visit(value)
    return tuple(sorted(found))


def _permission_projection(ast: StrategyAstV1) -> object:
    if isinstance(ast, TrafficLightStrategyAstV1):
        return {
            "kind": ast.kind.value,
            "unavailable_policy": ast.unavailable_policy.value,
        }
    return {
        "initial_state": ast.initial_state,
        "kind": ast.kind.value,
        "states": [
            {
                "entry_permission": state.entry_permission.value,
                "exit_permission": state.exit_permission.value,
                "name": state.name,
            }
            for state in ast.states
        ],
        "unavailable_policy": ast.unavailable_policy.value,
    }


def _require_ast(value: object) -> None:
    if not isinstance(
        value,
        (TrafficLightStrategyAstV1, StateMachineStrategyAstV1),
    ):
        raise TypeError("strategy mutation requires a canonical strategy AST")


def _require_strategy_id(strategy_identifier: str, digest: str, context: str) -> None:
    if type(digest) is not str or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"mutation {context} digest must be lowercase SHA-256")
    if strategy_identifier != "strategy-" + digest:
        raise ValueError(f"mutation {context} strategy ID does not match its digest")


def _require_digest_tuple(values: object, context: str) -> None:
    if type(values) is not tuple or any(
        type(item) is not str or _SHA256.fullmatch(item) is None for item in values
    ):
        raise ValueError(f"{context} must be a tuple of lowercase SHA-256 values")
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must be unique")


def _spec(
    operation_id: MutationOperationIdV1,
    kinds: tuple[StrategyAstKindV1, ...],
    parameter_domain: dict[str, object],
    human_reason: str,
    inverse_description: str,
    diff_description: str,
    observability_requirements: tuple[str, ...] = (),
    semantic_validation: str = STRATEGY_MUTATION_SEMANTIC_VALIDATION_V1,
) -> MutationOperatorSpecV1:
    return MutationOperatorSpecV1(
        operation_id=operation_id,
        operation_version=1,
        input_node_kinds=kinds,
        parameter_domain=parameter_domain,
        observability_requirements=observability_requirements,
        semantic_validation=semantic_validation,
        machine_reason=f"MUTATION_{operation_id.value}_V1",
        human_reason=human_reason,
        inverse_description=inverse_description,
        diff_description=diff_description,
    )


_BOTH_KINDS = (
    StrategyAstKindV1.TRAFFIC_LIGHT,
    StrategyAstKindV1.STATE_MACHINE,
)
_MACHINE_KIND = (StrategyAstKindV1.STATE_MACHINE,)
_EXACT_DECIMAL_DOMAIN_V1 = {
    "coefficient_decimal_digits_maximum": MAX_EXACT_DECIMAL_DIGITS_V1,
    "scale_maximum": MAX_EXACT_DECIMAL_SCALE_V1,
    "scale_minimum": 0,
    "type": "EXACT_DECIMAL_V1",
}
_COMPARISON_DOMAIN_V1 = {
    "feature": sorted(_ALL_FEATURES),
    "operator": [item.value for item in ComparisonOperator],
    "threshold": _EXACT_DECIMAL_DOMAIN_V1,
    "type": "COMPARISON_V1",
}
_NONNEGATIVE_EXACT_DECIMAL_DOMAIN_V1 = {
    **_EXACT_DECIMAL_DOMAIN_V1,
    "minimum": {"coefficient": 0, "scale": 0},
}
_POSITIVE_DURATION_DOMAIN_V1 = {
    "maximum": MAX_STRATEGY_DURATION_US_V1,
    "minimum": 1_000,
    "multiple_of": 1_000,
    "unit": "MICROSECONDS",
}
_NONNEGATIVE_DURATION_DOMAIN_V1 = {
    "maximum": MAX_STRATEGY_DURATION_US_V1,
    "minimum": 0,
    "multiple_of": 1_000,
    "unit": "MICROSECONDS",
}
_CONDITION_PATH_DOMAIN_V1 = {
    "existing_node": "COMPARISON_V1",
    "maximum_conditions": MAX_MUTATION_CONDITIONS_V1,
    "type": "CANONICAL_JSON_POINTER",
}
_CONDITION_COLLECTION_DOMAIN_V1 = {
    "existing_collection": "COMPARISON_CONJUNCTION_V1",
    "maximum_conditions": MAX_MUTATION_CONDITIONS_V1,
    "type": "CANONICAL_JSON_POINTER",
}
_TRANSITION_INDEX_DOMAIN_V1 = {
    "maximum_exclusive": MAX_MUTATION_TRANSITIONS_V1,
    "minimum": 0,
    "references": "EXISTING_TRANSITION",
}
REQUIRED_MUTATION_OPERATORS_V1 = (
    _spec(
        MutationOperationIdV1.THRESHOLD,
        _BOTH_KINDS,
        {
            "path": _CONDITION_PATH_DOMAIN_V1,
            "threshold": _EXACT_DECIMAL_DOMAIN_V1,
        },
        "Adjust one exact comparison threshold.",
        "Restore the previous exact threshold at the affected rule path.",
        "Replace one canonical comparison threshold.",
    ),
    _spec(
        MutationOperationIdV1.ROLLING_WINDOW,
        _BOTH_KINDS,
        {"window_us": _POSITIVE_DURATION_DOMAIN_V1},
        "Adjust the strategy rolling observation window.",
        "Restore the previous rolling-window duration.",
        "Replace the canonical rolling-window duration.",
    ),
    _spec(
        MutationOperationIdV1.REQUIRED_DURATION,
        _MACHINE_KIND,
        {
            "duration_us": _POSITIVE_DURATION_DOMAIN_V1,
            "transition_index": _TRANSITION_INDEX_DOMAIN_V1,
        },
        "Adjust the required duration of a qualified transition.",
        "Restore the previous qualified-transition duration.",
        "Replace one transition duration requirement.",
    ),
    _spec(
        MutationOperationIdV1.ADD_CONDITION,
        _BOTH_KINDS,
        {
            "collection_path": _CONDITION_COLLECTION_DOMAIN_V1,
            "condition": _COMPARISON_DOMAIN_V1,
        },
        "Add one observable comparison to a declared conjunction.",
        "Remove the newly added comparison from the same condition collection.",
        "Add one canonical comparison condition.",
    ),
    _spec(
        MutationOperationIdV1.REMOVE_CONDITION,
        _BOTH_KINDS,
        {"path": _CONDITION_PATH_DOMAIN_V1},
        "Remove one comparison while preserving a valid rule.",
        "Reinsert the removed comparison at the affected condition collection.",
        "Remove one canonical comparison condition.",
    ),
    _spec(
        MutationOperationIdV1.FEATURE_REPLACEMENT,
        _BOTH_KINDS,
        {"feature": sorted(_ALL_FEATURES), "path": _CONDITION_PATH_DOMAIN_V1},
        "Replace one condition feature inside the declared observation surface.",
        "Restore the previous feature at the affected comparison.",
        "Replace one canonical comparison feature.",
    ),
    _spec(
        MutationOperationIdV1.LOGICAL_OPERATOR,
        _BOTH_KINDS,
        {
            "operator": [item.value for item in ComparisonOperator],
            "path": _CONDITION_PATH_DOMAIN_V1,
        },
        "Replace one comparison operator without adding arbitrary logic.",
        "Restore the previous comparison operator.",
        "Replace one canonical comparison operator.",
    ),
    _spec(
        MutationOperationIdV1.TRANSITION_CONDITION,
        _MACHINE_KIND,
        {
            "condition": _COMPARISON_DOMAIN_V1,
            "path": _CONDITION_PATH_DOMAIN_V1,
        },
        "Replace one state-transition comparison.",
        "Restore the prior comparison on the same transition.",
        "Replace one canonical transition condition.",
    ),
    _spec(
        MutationOperationIdV1.COOLDOWN,
        _MACHINE_KIND,
        {
            "cooldown_us": _NONNEGATIVE_DURATION_DOMAIN_V1,
            "state_name": {
                "maximum_inventory": MAX_MUTATION_STATES_V1,
                "references": "EXISTING_STATE",
            },
        },
        "Adjust one state's transition cooldown.",
        "Restore the state's previous cooldown duration.",
        "Replace one canonical state cooldown.",
    ),
    _spec(
        MutationOperationIdV1.STATE_TIMEOUT,
        _MACHINE_KIND,
        {
            "timeout_us": _POSITIVE_DURATION_DOMAIN_V1,
            "transition_index": {
                **_TRANSITION_INDEX_DOMAIN_V1,
                "qualifier": "AFTER_ENTRY",
            },
        },
        "Compile an after-entry edge into an observable bounded state timeout.",
        "Restore the original immediate after-entry transition.",
        "Replace an immediate after-entry edge with a bounded TRUE_FOR timeout.",
        (PositionFeature.WORKING_ORDER_COUNT.value,),
    ),
    _spec(
        MutationOperationIdV1.CONFIRMATION_COUNT,
        _MACHINE_KIND,
        {
            "event_count": {
                "maximum": MAX_MUTATION_EVENT_COUNT_V1,
                "minimum": 1,
            },
            "transition_index": {
                **_TRANSITION_INDEX_DOMAIN_V1,
                "qualifier": "EVENTS_WITHIN",
            },
        },
        "Adjust the event confirmation count for an events-within transition.",
        "Restore the prior event confirmation count.",
        "Replace one canonical event confirmation count.",
    ),
    _spec(
        MutationOperationIdV1.INVALIDATION_RULE,
        _MACHINE_KIND,
        {
            "condition": _COMPARISON_DOMAIN_V1,
            "transition_index": {
                **_TRANSITION_INDEX_DOMAIN_V1,
                "target_signal": "RED",
            },
        },
        "Add one observable invalidation condition to a RED-target transition.",
        "Remove the newly added invalidation condition.",
        "Add one condition to a canonical RED-target invalidation rule.",
    ),
    _spec(
        MutationOperationIdV1.POSITION_CONSTRAINT,
        _MACHINE_KIND,
        {
            "feature": sorted(_POSITION_FEATURES),
            "operator": [item.value for item in ComparisonOperator],
            "threshold": _EXACT_DECIMAL_DOMAIN_V1,
            "transition_index": _TRANSITION_INDEX_DOMAIN_V1,
        },
        "Add a client-observable position constraint to one transition.",
        "Remove the newly added position constraint.",
        "Add one canonical position-state comparison.",
    ),
    _spec(
        MutationOperationIdV1.SPREAD_LIMIT,
        _BOTH_KINDS,
        {
            "collection_path": _CONDITION_COLLECTION_DOMAIN_V1,
            "max_spread_ticks": _NONNEGATIVE_EXACT_DECIMAL_DOMAIN_V1,
        },
        "Add an observable maximum-spread constraint.",
        "Remove the newly added spread constraint.",
        "Add one spread_ticks upper-bound comparison.",
        (FeatureName.SPREAD_TICKS.value,),
    ),
    _spec(
        MutationOperationIdV1.VOLUME_REQUIREMENT,
        _BOTH_KINDS,
        {
            "collection_path": _CONDITION_COLLECTION_DOMAIN_V1,
            "feature": sorted(_VOLUME_FEATURES),
            "minimum": _NONNEGATIVE_EXACT_DECIMAL_DOMAIN_V1,
        },
        "Add an observable minimum-volume requirement.",
        "Remove the newly added volume requirement.",
        "Add one canonical volume lower-bound comparison.",
    ),
)

if tuple(item.operation_id for item in REQUIRED_MUTATION_OPERATORS_V1) != tuple(
    MutationOperationIdV1
):
    raise RuntimeError("required mutation operator inventory is incomplete or reordered")


__all__ = [
    "MAX_MUTATION_CONDITIONS_V1",
    "MAX_MUTATION_EVENT_COUNT_V1",
    "MAX_MUTATION_PARAMETERS_V1",
    "MAX_MUTATION_REQUEST_BYTES_V1",
    "MAX_MUTATION_STATES_V1",
    "MAX_MUTATION_TRANSITIONS_V1",
    "REQUIRED_MUTATION_OPERATORS_V1",
    "STRATEGY_MUTATION_SCHEMA_ID_V1",
    "STRATEGY_MUTATION_SEMANTIC_VALIDATION_V1",
    "MutationOperationIdV1",
    "MutationOperatorSpecV1",
    "MutationRejectionReasonV1",
    "MutationRequestV1",
    "MutationResourceLimitsV1",
    "MutationStatusV1",
    "StrategyMutationRecordV1",
    "StrategyMutationResultV1",
    "apply_strategy_mutation",
    "mutation_operator_spec",
    "strategy_id",
]
