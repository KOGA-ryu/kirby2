"""Generative correctness, fault injection, minimization, and immutable evidence."""

from .generator import AXES, coverage_report, generate_configurations
from .kernel import run_kernel, violation_signatures
from .models import (
    AUDIT_LAB_SCHEMA_VERSION,
    AcceptanceRecord,
    FaultEvidence,
    FaultKind,
    GeneratedConfiguration,
    KernelResult,
    MinimizedFailure,
    StatisticalCheck,
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
    "FaultEvidence",
    "FaultKind",
    "GeneratedConfiguration",
    "KernelResult",
    "MinimizedFailure",
    "PacketRecord",
    "StatisticalCheck",
    "coverage_report",
    "generate_configurations",
    "run_audit_lab",
    "run_kernel",
    "violation_signatures",
]
