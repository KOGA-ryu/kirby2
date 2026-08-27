"""Real generated-case executors, registered lane by lane as implemented."""

from .base import (
    CAPABILITY_MATRIX,
    AuditCaseExecutor,
    ExecutorRegistry,
    LaneCapabilitySpec,
)
from .core_flow import CORE_FLOW_RECORDING_TYPE, CoreFlowExecutor
from .mechanics import MECHANICS_RECORDING_TYPE, MechanicsExecutor


EXECUTOR_REGISTRY = ExecutorRegistry()
EXECUTOR_REGISTRY.register(CoreFlowExecutor())
EXECUTOR_REGISTRY.register(MechanicsExecutor())


__all__ = [
    "CAPABILITY_MATRIX",
    "CORE_FLOW_RECORDING_TYPE",
    "EXECUTOR_REGISTRY",
    "MECHANICS_RECORDING_TYPE",
    "AuditCaseExecutor",
    "CoreFlowExecutor",
    "ExecutorRegistry",
    "LaneCapabilitySpec",
    "MechanicsExecutor",
]
