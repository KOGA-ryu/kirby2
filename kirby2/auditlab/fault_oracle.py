"""Independent expectations for production fault observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .models import FaultKind, FaultObservation, canonical_sha256


_EXPECTED_CODES: Mapping[FaultKind, tuple[str, ...]] = MappingProxyType(
    {
        FaultKind.DUPLICATE_MESSAGE: ("DUPLICATE_RECORD",),
        FaultKind.DROPPED_MARKET_DATA: ("MISSING_SEQUENCE",),
        FaultKind.DELAYED_ACKNOWLEDGEMENT: (
            "ACK_LATENCY_BUDGET_EXCEEDED",
        ),
        FaultKind.OUT_OF_ORDER_DELIVERY: ("OUT_OF_ORDER_RECORD",),
        FaultKind.SNAPSHOT_GAP: ("SNAPSHOT_GAP",),
        FaultKind.CORRUPTED_DATASET_ROW: ("INVALID_QUANTITY",),
        FaultKind.VENUE_REJECTION: ("UNSUPPORTED_MARKET_INSTRUCTION",),
        FaultKind.HALT_DURING_PENDING_ORDER: ("PENDING_ORDER_HALTED",),
        FaultKind.CANCEL_FILL_RACE: ("TERMINAL_RACE_CLASSIFIED",),
        FaultKind.SCHEMA_MISMATCH: ("UNSUPPORTED_SCHEMA_VERSION",),
    }
)

if set(_EXPECTED_CODES) != set(FaultKind):
    raise RuntimeError("fault oracle does not cover the complete fault inventory")


@dataclass(frozen=True, slots=True)
class FaultEvaluation:
    """Oracle comparison made only after a production observation exists."""

    fault: FaultKind
    expected_codes: tuple[str, ...]
    observed_code: str | None
    observation_sha256: str

    @property
    def detected(self) -> bool:
        return self.observed_code in self.expected_codes

    @property
    def outcome(self) -> str:
        return "EXPECTED_DETECTION" if self.detected else "FAULT_MISS"

    def as_dict(self) -> dict[str, object]:
        return {
            "detected": self.detected,
            "expected_codes": list(self.expected_codes),
            "fault": self.fault.value,
            "observation_sha256": self.observation_sha256,
            "observed_code": self.observed_code,
            "outcome": self.outcome,
        }


def expected_fault_codes(fault: FaultKind) -> tuple[str, ...]:
    if not isinstance(fault, FaultKind):
        raise TypeError("fault oracle requires FaultKind")
    return _EXPECTED_CODES[fault]


def evaluate_fault_observation(observation: FaultObservation) -> FaultEvaluation:
    if not isinstance(observation, FaultObservation):
        raise TypeError("fault oracle requires FaultObservation")
    return FaultEvaluation(
        fault=observation.fault,
        expected_codes=expected_fault_codes(observation.fault),
        observed_code=observation.observed_code,
        observation_sha256=canonical_sha256(observation.as_dict()),
    )
