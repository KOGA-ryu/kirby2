"""Immutable contracts for the Kirby2 model-risk laboratory."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


AUDIT_LAB_SCHEMA_VERSION = 1
AUDIT_PACKET_SCHEMA_VERSION = 2
LEGACY_AUDIT_PACKET_SCHEMA_VERSION = 1
_ACCEPTANCE_ID = re.compile(r"^acceptance-[A-Za-z0-9_-]{1,96}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class FaultKind(str, Enum):
    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
    DROPPED_MARKET_DATA = "DROPPED_MARKET_DATA"
    DELAYED_ACKNOWLEDGEMENT = "DELAYED_ACKNOWLEDGEMENT"
    OUT_OF_ORDER_DELIVERY = "OUT_OF_ORDER_DELIVERY"
    SNAPSHOT_GAP = "SNAPSHOT_GAP"
    CORRUPTED_DATASET_ROW = "CORRUPTED_DATASET_ROW"
    VENUE_REJECTION = "VENUE_REJECTION"
    HALT_DURING_PENDING_ORDER = "HALT_DURING_PENDING_ORDER"
    CANCEL_FILL_RACE = "CANCEL_FILL_RACE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"


@dataclass(frozen=True, slots=True)
class GeneratedConfiguration:
    """One fully declared generative case; no value is read from global state."""

    sequence: int
    seed: int
    duration_us: int
    duration_events: int
    agent_count: int
    flow_model: str
    regime: str
    volume: str
    liquidity: str
    latency: str
    session_phase: str
    order_types: str
    hidden_liquidity: str
    venue_count: int
    auction_state: str
    agent_population: str
    strategy: str
    objective: str
    injected_fault: FaultKind | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("configuration sequence must be positive")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("configuration seed must be nonnegative")
        if type(self.duration_events) is not int or self.duration_events <= 0:
            raise ValueError("configuration duration must be positive")
        if type(self.duration_us) is not int or self.duration_us <= 0:
            raise ValueError("configuration duration must be positive microseconds")
        if type(self.agent_count) is not int or not 1 <= self.agent_count <= 12:
            raise ValueError("configuration agent count must be in 1..12")
        if type(self.venue_count) is not int or not 1 <= self.venue_count <= 4:
            raise ValueError("configuration venue count must be in 1..4")
        textual = (
            self.flow_model,
            self.regime,
            self.volume,
            self.liquidity,
            self.latency,
            self.session_phase,
            self.order_types,
            self.hidden_liquidity,
            self.auction_state,
            self.agent_population,
            self.strategy,
            self.objective,
        )
        if any(not value for value in textual):
            raise ValueError("configuration dimensions must be nonempty")
        if self.injected_fault is not None and not isinstance(
            self.injected_fault, FaultKind
        ):
            raise TypeError("injected fault must use FaultKind")

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_count": self.agent_count,
            "agent_population": self.agent_population,
            "auction_state": self.auction_state,
            "duration_events": self.duration_events,
            "duration_us": self.duration_us,
            "flow_model": self.flow_model,
            "hidden_liquidity": self.hidden_liquidity,
            "injected_fault": (
                None if self.injected_fault is None else self.injected_fault.value
            ),
            "latency": self.latency,
            "liquidity": self.liquidity,
            "objective": self.objective,
            "order_types": self.order_types,
            "regime": self.regime,
            "schema_version": AUDIT_LAB_SCHEMA_VERSION,
            "seed": self.seed,
            "sequence": self.sequence,
            "session_phase": self.session_phase,
            "strategy": self.strategy,
            "venue_count": self.venue_count,
            "volume": self.volume,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GeneratedConfiguration:
        if payload.get("schema_version") != AUDIT_LAB_SCHEMA_VERSION:
            raise ValueError("unsupported audit-lab configuration schema")
        raw_fault = payload.get("injected_fault")
        return cls(
            sequence=int(payload["sequence"]),
            seed=int(payload["seed"]),
            duration_us=int(payload["duration_us"]),
            duration_events=int(payload["duration_events"]),
            agent_count=int(payload["agent_count"]),
            flow_model=str(payload["flow_model"]),
            regime=str(payload["regime"]),
            volume=str(payload["volume"]),
            liquidity=str(payload["liquidity"]),
            latency=str(payload["latency"]),
            session_phase=str(payload["session_phase"]),
            order_types=str(payload["order_types"]),
            hidden_liquidity=str(payload["hidden_liquidity"]),
            venue_count=int(payload["venue_count"]),
            auction_state=str(payload["auction_state"]),
            agent_population=str(payload["agent_population"]),
            strategy=str(payload["strategy"]),
            objective=str(payload["objective"]),
            injected_fault=None if raw_fault is None else FaultKind(str(raw_fault)),
        )


@dataclass(frozen=True, slots=True)
class FaultEvidence:
    fault: FaultKind
    detector: str
    expected_code: str
    detected_code: str | None
    injection_event: int
    details: dict[str, object]

    @property
    def detected(self) -> bool:
        return self.detected_code == self.expected_code

    def as_dict(self) -> dict[str, object]:
        return {
            "details": self.details,
            "detected": self.detected,
            "detected_code": self.detected_code,
            "detector": self.detector,
            "expected_code": self.expected_code,
            "fault": self.fault.value,
            "injection_event": self.injection_event,
        }


@dataclass(frozen=True, slots=True)
class KernelResult:
    configuration: GeneratedConfiguration
    event_stream: tuple[dict[str, object], ...]
    venue_states: tuple[dict[str, object], ...]
    observable_layer: dict[str, object]
    metrics: dict[str, int | float | str | None]
    invariant_checks: dict[str, bool]
    violations: tuple[str, ...]
    fault_evidence: FaultEvidence | None

    @property
    def event_digest(self) -> str:
        return canonical_sha256(self.event_stream)

    @property
    def state_digest(self) -> str:
        return canonical_sha256(self.venue_states)

    @property
    def result_digest(self) -> str:
        return canonical_sha256(self.declared_outputs())

    @property
    def passed(self) -> bool:
        return not self.violations and all(self.invariant_checks.values())

    def declared_outputs(self) -> dict[str, object]:
        return {
            "configuration_sha256": self.configuration.sha256,
            "event_digest": self.event_digest,
            "fault_evidence": (
                None if self.fault_evidence is None else self.fault_evidence.as_dict()
            ),
            "invariant_checks": dict(sorted(self.invariant_checks.items())),
            "metrics": dict(sorted(self.metrics.items())),
            "observable_layer_sha256": canonical_sha256(self.observable_layer),
            "state_digest": self.state_digest,
            "violations": list(self.violations),
        }

    def as_dict(self, *, include_events: bool = True) -> dict[str, object]:
        payload = {
            "configuration": self.configuration.as_dict(),
            "declared_outputs": self.declared_outputs(),
            "observable_layer": self.observable_layer,
            "result_digest": self.result_digest,
            "status": "PASS" if self.passed else "FAIL",
            "venue_states": list(self.venue_states),
        }
        if include_events:
            payload["event_stream"] = list(self.event_stream)
        return payload


@dataclass(frozen=True, slots=True)
class MinimizedFailure:
    signature: str
    source_configuration_sha256: str
    minimized_configuration: GeneratedConfiguration
    attempts: int
    preserved: bool
    result_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "minimized_configuration": self.minimized_configuration.as_dict(),
            "preserved": self.preserved,
            "result_digest": self.result_digest,
            "signature": self.signature,
            "source_configuration_sha256": self.source_configuration_sha256,
        }


@dataclass(frozen=True, slots=True)
class StatisticalCheck:
    name: str
    status: str
    evidence: dict[str, object]
    threshold: str

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "name": self.name,
            "status": self.status,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceRecord:
    record_id: str
    scenario_version: int
    seed: int
    reviewer_decision: str
    observed_characteristics: tuple[str, ...]
    known_defects: tuple[str, ...]
    artifact_digests: dict[str, str]
    supersedes_record_id: str | None = None

    def __post_init__(self) -> None:
        if not _ACCEPTANCE_ID.fullmatch(self.record_id):
            raise ValueError("acceptance record ID is invalid")
        if type(self.scenario_version) is not int or self.scenario_version <= 0:
            raise ValueError("acceptance scenario version must be positive")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("acceptance seed must be nonnegative")
        if (
            not self.reviewer_decision
            or not self.reviewer_decision.replace("_", "").isalnum()
            or self.reviewer_decision != self.reviewer_decision.upper()
        ):
            raise ValueError("acceptance reviewer decision must be an uppercase identifier")
        if not self.observed_characteristics or any(
            not item for item in self.observed_characteristics
        ):
            raise ValueError("acceptance record requires observed characteristics")
        if any(not item for item in self.known_defects):
            raise ValueError("known-defect entries must be nonempty")
        if not self.artifact_digests or any(
            not name or not _SHA256.fullmatch(digest)
            for name, digest in self.artifact_digests.items()
        ):
            raise ValueError("acceptance artifact digests are invalid")
        if self.supersedes_record_id is not None:
            if (
                not _ACCEPTANCE_ID.fullmatch(self.supersedes_record_id)
                or self.supersedes_record_id == self.record_id
            ):
                raise ValueError("superseded acceptance record ID is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests": dict(sorted(self.artifact_digests.items())),
            "known_defects": list(self.known_defects),
            "observed_characteristics": list(self.observed_characteristics),
            "record_id": self.record_id,
            "reviewer_decision": self.reviewer_decision,
            "scenario_version": self.scenario_version,
            "seed": self.seed,
            "supersedes_record_id": self.supersedes_record_id,
        }
