"""Real generated-case executors, registered lane by lane as implemented."""

from .base import (
    CAPABILITY_MATRIX,
    AuditCaseExecutor,
    ExecutorRegistry,
    LaneCapabilitySpec,
)
from .core_flow import CORE_FLOW_RECORDING_TYPE, CoreFlowExecutor
from .fragmented import FRAGMENTED_RECORDING_TYPE, FragmentedExecutor
from .latency import LATENCY_RECORDING_TYPE, LatencyExecutor
from .mechanics import MECHANICS_RECORDING_TYPE, MechanicsExecutor


EXECUTOR_REGISTRY = ExecutorRegistry()
EXECUTOR_REGISTRY.register(CoreFlowExecutor())
EXECUTOR_REGISTRY.register(MechanicsExecutor())
EXECUTOR_REGISTRY.register(LatencyExecutor())
EXECUTOR_REGISTRY.register(FragmentedExecutor())


__all__ = [
    "CAPABILITY_MATRIX",
    "CORE_FLOW_RECORDING_TYPE",
    "EXECUTOR_REGISTRY",
    "FRAGMENTED_RECORDING_TYPE",
    "LATENCY_RECORDING_TYPE",
    "MECHANICS_RECORDING_TYPE",
    "AuditCaseExecutor",
    "CoreFlowExecutor",
    "ExecutorRegistry",
    "FragmentedExecutor",
    "LaneCapabilitySpec",
    "LatencyExecutor",
    "MechanicsExecutor",
]
