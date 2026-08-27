"""Real generated-case executors, registered lane by lane as implemented."""

from .base import (
    CAPABILITY_MATRIX,
    AuditCaseExecutor,
    ExecutorRegistry,
    LaneCapabilitySpec,
)
from .core_flow import CORE_FLOW_RECORDING_TYPE, CoreFlowExecutor


EXECUTOR_REGISTRY = ExecutorRegistry()
EXECUTOR_REGISTRY.register(CoreFlowExecutor())


__all__ = [
    "CAPABILITY_MATRIX",
    "CORE_FLOW_RECORDING_TYPE",
    "EXECUTOR_REGISTRY",
    "AuditCaseExecutor",
    "CoreFlowExecutor",
    "ExecutorRegistry",
    "LaneCapabilitySpec",
]
