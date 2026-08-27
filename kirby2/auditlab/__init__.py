"""Generative correctness, fault injection, minimization, and immutable evidence."""

from .generator import AXES, evidence_coverage_report, generate_configurations
from .kernel import failure_signatures, run_generated_case
from .models import (
    AUDIT_LAB_SCHEMA_VERSION,
    AcceptanceRecord,
    FaultObservation,
    FaultKind,
    GeneratedCaseResult,
    GeneratedConfiguration,
    MinimizedFailure,
    StatisticalCheck,
)
from .projectors import (
    EventLedgerProjector,
    FillLedgerProjector,
    PlayerLedgerProjection,
)
from .runner import AuditLabResult, run_audit_lab
from .store import AuditLabStore, DEFAULT_AUDIT_LAB_STORE, PacketRecord

__all__ = [
    "AUDIT_LAB_SCHEMA_VERSION",
    "AXES",
    "AcceptanceRecord",
    "AuditLabResult",
    "AuditLabStore",
    "DEFAULT_AUDIT_LAB_STORE",
    "FaultObservation",
    "FaultKind",
    "GeneratedCaseResult",
    "GeneratedConfiguration",
    "EventLedgerProjector",
    "FillLedgerProjector",
    "MinimizedFailure",
    "PacketRecord",
    "PlayerLedgerProjection",
    "StatisticalCheck",
    "evidence_coverage_report",
    "failure_signatures",
    "generate_configurations",
    "run_audit_lab",
    "run_generated_case",
]
