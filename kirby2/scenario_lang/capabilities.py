"""Closed target capability contracts for scenario validation V1."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .identity import canonical_semantic_plan_bytes
from .models import (
    CompiledScenarioArtifactV1,
    SCENARIO_TARGET_CONTRACTS_V1,
    ScenarioCapabilityDecisionStatusV1,
    ScenarioCapabilityDecisionV1,
    ScenarioTargetKindV1,
    ScenarioValidationFindingV1,
    ScenarioValidationSeverityV1,
)


SCENARIO_CAPABILITY_CONTRACT_SCHEMA_VERSION = 1
SCENARIO_CAPABILITY_CONTRACT_DIGEST_DOMAIN_V1 = (
    b"KIRBY2_SCENARIO_CAPABILITY_CONTRACT_V1\x00"
)

TOP_OF_BOOK_V1 = "TOP_OF_BOOK_V1"
MARKET_BY_PRICE_V1 = "MARKET_BY_PRICE_V1"
MARKET_BY_ORDER_V1 = "MARKET_BY_ORDER_V1"
SYNTHETIC_FLOW_V1 = "SYNTHETIC_FLOW_V1"
HAWKES_FLOW_V1 = "HAWKES_FLOW_V1"
AUCTION_HALT_V1 = "AUCTION_HALT_V1"
HIDDEN_LIQUIDITY_V1 = "HIDDEN_LIQUIDITY_V1"
MULTIVENUE_ROUTING_V1 = "MULTIVENUE_ROUTING_V1"
EXACT_REPLAY_V1 = "EXACT_REPLAY_V1"
HISTORICAL_REPLAY_V1 = "HISTORICAL_REPLAY_V1"
HISTORICAL_RECONSTRUCTION_V1 = "HISTORICAL_RECONSTRUCTION_V1"
CHECKPOINT_RESTORE_V1 = "CHECKPOINT_RESTORE_V1"
STRATEGY_OBSERVABILITY_V1 = "STRATEGY_OBSERVABILITY_V1"


@dataclass(frozen=True, slots=True)
class ScenarioCapabilityRequirementV1:
    declaration_id: str
    capability_id: str
    required: bool
    source_location: str

    def __post_init__(self) -> None:
        for value, context in (
            (self.declaration_id, "capability declaration ID"),
            (self.capability_id, "capability ID"),
            (self.source_location, "capability source location"),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"scenario {context} must be nonempty text")
        if type(self.required) is not bool:
            raise TypeError("scenario capability required flag must be a bool")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "declaration_id": self.declaration_id,
            "required": self.required,
            "source_location": self.source_location,
        }


@dataclass(frozen=True, slots=True)
class ScenarioTargetCapabilityContractV1:
    target_kind: ScenarioTargetKindV1
    provided_capabilities: tuple[str, ...]
    observable_features: tuple[str, ...]
    checkpoint_adapters: tuple[str, ...]
    supported_order_instructions: tuple[str, ...]
    source_capabilities: tuple[str, ...]
    persist_supported: bool = True
    replay_supported: bool = True

    def __post_init__(self) -> None:
        if type(self.target_kind) is not ScenarioTargetKindV1:
            raise TypeError("target capability contract requires a closed target kind")
        for name in (
            "provided_capabilities",
            "observable_features",
            "checkpoint_adapters",
            "supported_order_instructions",
            "source_capabilities",
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(
                type(item) is not str or not item for item in values
            ):
                raise TypeError(f"target capability {name} must be a string tuple")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"target capability {name} must be unique and sorted")
        if type(self.persist_supported) is not bool or type(self.replay_supported) is not bool:
            raise TypeError("target persist/replay support flags must be bools")

    def as_dict(self) -> dict[str, object]:
        target = SCENARIO_TARGET_CONTRACTS_V1[self.target_kind]
        return {
            "adapter_id": target.adapter_id,
            "adapter_version": target.adapter_version,
            "checkpoint_adapters": list(self.checkpoint_adapters),
            "observable_features": list(self.observable_features),
            "persist_supported": self.persist_supported,
            "provided_capabilities": list(self.provided_capabilities),
            "replay_supported": self.replay_supported,
            "schema_version": SCENARIO_CAPABILITY_CONTRACT_SCHEMA_VERSION,
            "source_capabilities": list(self.source_capabilities),
            "supported_order_instructions": list(
                self.supported_order_instructions
            ),
            "target_kind": self.target_kind.value,
            "target_version": target.target_version,
        }


_COMMON_OBSERVABLE_FEATURES = (
    "BEST_ASK",
    "BEST_BID",
    "DISPLAYED_DEPTH",
    "MIDPOINT",
    "OWN_ACKNOWLEDGED_ORDERS",
    "OWN_RECEIVED_FILLS",
    "SPREAD",
    "TRADE_PRINTS",
)
_COMMON_ORDERS = (
    "CANCEL_REPLACE",
    "DAY",
    "FOK",
    "GOOD_UNTIL_TIME",
    "GTC",
    "IOC",
    "LIMIT",
    "MARKET",
    "MARKETABLE_LIMIT",
    "POST_ONLY",
    "SESSION",
)
_ALL_SOURCE_CAPABILITIES = (
    "BARS_ONLY",
    "LEVEL2_DELTAS",
    "LEVEL2_SNAPSHOTS",
    "MARKET_BY_ORDER",
    "TRADES",
    "TRADES_AND_QUOTES",
)


def _sorted(*values: str) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


SCENARIO_TARGET_CAPABILITIES_V1: Mapping[
    ScenarioTargetKindV1,
    ScenarioTargetCapabilityContractV1,
] = MappingProxyType(
    {
        ScenarioTargetKindV1.FULL_DAY_PLAN_V1: ScenarioTargetCapabilityContractV1(
            target_kind=ScenarioTargetKindV1.FULL_DAY_PLAN_V1,
            provided_capabilities=_sorted(
                AUCTION_HALT_V1,
                CHECKPOINT_RESTORE_V1,
                EXACT_REPLAY_V1,
                HAWKES_FLOW_V1,
                MARKET_BY_PRICE_V1,
                STRATEGY_OBSERVABILITY_V1,
                SYNTHETIC_FLOW_V1,
                TOP_OF_BOOK_V1,
            ),
            observable_features=_sorted(*_COMMON_OBSERVABLE_FEATURES),
            checkpoint_adapters=_sorted(
                "AGENT_SCHEDULER_V1",
                "DELIVERY_ASYNC_V1",
                "ENGINE_MARKET_MECHANICS_V1",
                "FEATURE_STRATEGY_PLAYER_V1",
                "FLOW_HAWKES_V1",
                "FLOW_QUEUE_REACTIVE_V1",
                "FLOW_SIMPLE_V1",
                "FULL_DAY_RUNTIME_V1",
            ),
            supported_order_instructions=_sorted(*_COMMON_ORDERS),
            source_capabilities=_sorted(*_ALL_SOURCE_CAPABILITIES),
        ),
        ScenarioTargetKindV1.MARKET_SCENARIO_V1: ScenarioTargetCapabilityContractV1(
            target_kind=ScenarioTargetKindV1.MARKET_SCENARIO_V1,
            provided_capabilities=_sorted(
                HAWKES_FLOW_V1,
                MARKET_BY_PRICE_V1,
                SYNTHETIC_FLOW_V1,
                TOP_OF_BOOK_V1,
            ),
            observable_features=_sorted(*_COMMON_OBSERVABLE_FEATURES),
            checkpoint_adapters=_sorted("MARKET_SCENARIO_REPLAY_V1"),
            supported_order_instructions=_sorted(*_COMMON_ORDERS),
            source_capabilities=_sorted(*_ALL_SOURCE_CAPABILITIES),
        ),
        ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1: (
            ScenarioTargetCapabilityContractV1(
                target_kind=ScenarioTargetKindV1.HIDDEN_LIQUIDITY_RECORDING_V1,
                provided_capabilities=_sorted(
                    EXACT_REPLAY_V1,
                    HIDDEN_LIQUIDITY_V1,
                    MARKET_BY_ORDER_V1,
                    MARKET_BY_PRICE_V1,
                    TOP_OF_BOOK_V1,
                ),
                observable_features=_sorted(*_COMMON_OBSERVABLE_FEATURES),
                checkpoint_adapters=_sorted(
                    "HIDDEN_LIQUIDITY_RECORDING_V1"
                ),
                supported_order_instructions=_sorted(
                    *_COMMON_ORDERS,
                    "HIDDEN_MIDPOINT",
                    "ICEBERG",
                ),
                source_capabilities=_sorted(*_ALL_SOURCE_CAPABILITIES),
            )
        ),
        ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1: (
            ScenarioTargetCapabilityContractV1(
                target_kind=ScenarioTargetKindV1.MULTIVENUE_RECORDING_V1,
                provided_capabilities=_sorted(
                    EXACT_REPLAY_V1,
                    HIDDEN_LIQUIDITY_V1,
                    MARKET_BY_ORDER_V1,
                    MARKET_BY_PRICE_V1,
                    MULTIVENUE_ROUTING_V1,
                    TOP_OF_BOOK_V1,
                ),
                observable_features=_sorted(
                    *_COMMON_OBSERVABLE_FEATURES,
                    "CONSOLIDATED_BEST_ASK",
                    "CONSOLIDATED_BEST_BID",
                    "VENUE_DISPLAYED_DEPTH",
                ),
                checkpoint_adapters=_sorted("MULTIVENUE_RECORDING_V1"),
                supported_order_instructions=_sorted(
                    *_COMMON_ORDERS,
                    "HIDDEN_MIDPOINT",
                    "ICEBERG",
                ),
                source_capabilities=_sorted(*_ALL_SOURCE_CAPABILITIES),
            )
        ),
        ScenarioTargetKindV1.HISTORICAL_LESSON_V1: (
            ScenarioTargetCapabilityContractV1(
                target_kind=ScenarioTargetKindV1.HISTORICAL_LESSON_V1,
                provided_capabilities=_sorted(
                    HISTORICAL_RECONSTRUCTION_V1,
                    HISTORICAL_REPLAY_V1,
                    TOP_OF_BOOK_V1,
                ),
                observable_features=_sorted(
                    *_COMMON_OBSERVABLE_FEATURES,
                    "HISTORICAL_CONTEXT_AVAILABLE_AT_CUTOFF",
                ),
                checkpoint_adapters=_sorted("HISTORICAL_LESSON_SESSION_V1"),
                supported_order_instructions=_sorted(*_COMMON_ORDERS),
                source_capabilities=_sorted(*_ALL_SOURCE_CAPABILITIES),
            )
        ),
    }
)

SCENARIO_KNOWN_CAPABILITIES_V1 = frozenset(
    capability
    for contract in SCENARIO_TARGET_CAPABILITIES_V1.values()
    for capability in contract.provided_capabilities
)


def scenario_capability_requirements_from_artifact(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[ScenarioCapabilityRequirementV1, ...]:
    if type(artifact) is not CompiledScenarioArtifactV1:
        raise TypeError("scenario capability extraction requires a compiled artifact")
    raw = artifact.as_dict()["required_capability_declarations"]
    if type(raw) is not list:
        raise TypeError("compiled capability declarations must be an array")
    requirements: list[ScenarioCapabilityRequirementV1] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TypeError("compiled capability declaration must be an object")
        required = item.get("required")
        if type(required) is not bool:
            raise TypeError("compiled capability required flag must be a bool")
        requirements.append(
            ScenarioCapabilityRequirementV1(
                declaration_id=str(item["declaration_id"]),
                capability_id=str(item["capability_id"]),
                required=required,
                source_location=str(item["source_location"]),
            )
        )
    result = tuple(sorted(requirements, key=lambda item: item.declaration_id))
    if tuple(item.declaration_id for item in result) != tuple(
        sorted(set(item.declaration_id for item in result))
    ):
        raise ValueError("compiled capability declaration IDs are not unique")
    return result


def scenario_capability_contract_digest_v1(
    target_kind: ScenarioTargetKindV1,
    requirements: Iterable[ScenarioCapabilityRequirementV1],
) -> str:
    if type(target_kind) is not ScenarioTargetKindV1:
        raise TypeError("capability digest requires a closed target kind")
    normalized = tuple(sorted(tuple(requirements), key=lambda item: item.declaration_id))
    if any(type(item) is not ScenarioCapabilityRequirementV1 for item in normalized):
        raise TypeError("capability digest requires typed requirements")
    declaration_ids = tuple(item.declaration_id for item in normalized)
    if declaration_ids != tuple(sorted(set(declaration_ids))):
        raise ValueError("capability digest requirement IDs must be unique")
    payload = canonical_semantic_plan_bytes(
        {
            "requirements": [item.as_dict() for item in normalized],
            "schema_version": SCENARIO_CAPABILITY_CONTRACT_SCHEMA_VERSION,
            "target_contract": SCENARIO_TARGET_CAPABILITIES_V1[
                target_kind
            ].as_dict(),
        }
    )
    digest = hashlib.sha256()
    digest.update(SCENARIO_CAPABILITY_CONTRACT_DIGEST_DOMAIN_V1)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
    return digest.hexdigest()


def evaluate_scenario_capabilities(
    artifact: CompiledScenarioArtifactV1,
) -> tuple[
    tuple[ScenarioCapabilityDecisionV1, ...],
    tuple[ScenarioValidationFindingV1, ...],
]:
    requirements = scenario_capability_requirements_from_artifact(artifact)
    contract = SCENARIO_TARGET_CAPABILITIES_V1[artifact.target_kind]
    provided = set(contract.provided_capabilities)
    decisions: list[ScenarioCapabilityDecisionV1] = []
    findings: list[ScenarioValidationFindingV1] = []
    for requirement in requirements:
        if requirement.capability_id not in SCENARIO_KNOWN_CAPABILITIES_V1:
            status = ScenarioCapabilityDecisionStatusV1.UNSUPPORTED
            reason = "UNKNOWN_CAPABILITY"
            code = "CAPABILITY_UNKNOWN"
            message = (
                f"Capability {requirement.capability_id} is not in the closed V1 "
                "capability inventory."
            )
        elif requirement.capability_id not in provided:
            status = ScenarioCapabilityDecisionStatusV1.UNSUPPORTED
            reason = "TARGET_CAPABILITY_UNSUPPORTED"
            code = "CAPABILITY_UNSUPPORTED_BY_TARGET"
            message = (
                f"Target {artifact.target_kind.value} does not provide "
                f"{requirement.capability_id}."
            )
        else:
            status = ScenarioCapabilityDecisionStatusV1.SUPPORTED
            reason = "CAPABILITY_SUPPORTED"
            code = ""
            message = ""
        decisions.append(
            ScenarioCapabilityDecisionV1(
                declaration_id=requirement.declaration_id,
                capability_id=requirement.capability_id,
                required=requirement.required,
                decision=status,
                reason_code=reason,
                source_location=requirement.source_location,
            )
        )
        if status is not ScenarioCapabilityDecisionStatusV1.SUPPORTED:
            findings.append(
                ScenarioValidationFindingV1(
                    family="TARGET_CAPABILITY_CONTRACT",
                    severity=(
                        ScenarioValidationSeverityV1.ERROR
                        if requirement.required
                        else ScenarioValidationSeverityV1.WARNING
                    ),
                    code=code,
                    source_location=requirement.source_location,
                    message=message,
                    suggested_correction=(
                        "Select a target that explicitly provides this capability "
                        "or remove the requirement."
                    ),
                    required=requirement.required,
                )
            )

    expected_digest = scenario_capability_contract_digest_v1(
        artifact.target_kind,
        requirements,
    )
    if artifact.plan_envelope.capability_digest != expected_digest:
        findings.append(
            ScenarioValidationFindingV1(
                family="TARGET_CAPABILITY_CONTRACT",
                severity=ScenarioValidationSeverityV1.ERROR,
                code="CAPABILITY_DIGEST_MISMATCH",
                source_location="root_source.metadata.capability_digest",
                message=(
                    "The declared capability digest does not bind the selected "
                    "target contract and requirement inventory."
                ),
                suggested_correction=(
                    "Regenerate the capability digest from the closed V1 target "
                    "contract and declared requirements."
                ),
            )
        )
    return (
        tuple(sorted(decisions, key=lambda item: item.declaration_id)),
        tuple(sorted(findings, key=lambda item: item.sort_key())),
    )


__all__ = [
    "AUCTION_HALT_V1",
    "CHECKPOINT_RESTORE_V1",
    "EXACT_REPLAY_V1",
    "HAWKES_FLOW_V1",
    "HIDDEN_LIQUIDITY_V1",
    "HISTORICAL_RECONSTRUCTION_V1",
    "HISTORICAL_REPLAY_V1",
    "MARKET_BY_ORDER_V1",
    "MARKET_BY_PRICE_V1",
    "MULTIVENUE_ROUTING_V1",
    "SCENARIO_CAPABILITY_CONTRACT_SCHEMA_VERSION",
    "SCENARIO_KNOWN_CAPABILITIES_V1",
    "SCENARIO_TARGET_CAPABILITIES_V1",
    "STRATEGY_OBSERVABILITY_V1",
    "SYNTHETIC_FLOW_V1",
    "ScenarioCapabilityRequirementV1",
    "ScenarioTargetCapabilityContractV1",
    "TOP_OF_BOOK_V1",
    "evaluate_scenario_capabilities",
    "scenario_capability_contract_digest_v1",
    "scenario_capability_requirements_from_artifact",
]
