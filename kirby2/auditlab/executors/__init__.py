"""Real generated-case executors, registered lane by lane as implemented."""

from .base import (
    CAPABILITY_MATRIX,
    AuditCaseExecutor,
    ExecutorRegistry,
    LaneCapabilitySpec,
)
from .algorithms import ALGORITHM_RECORDING_TYPE, AlgorithmExecutor
from .core_flow import CORE_FLOW_RECORDING_TYPE, CoreFlowExecutor
from .ecology import ECOLOGY_RECORDING_TYPE, EcologyExecutor
from .fault import FAULT_RECORDING_TYPE, FaultExecutor
from .fragmented import FRAGMENTED_RECORDING_TYPE, FragmentedExecutor
from .latency import LATENCY_RECORDING_TYPE, LatencyExecutor
from .mechanics import MECHANICS_RECORDING_TYPE, MechanicsExecutor


EXECUTOR_REGISTRY = ExecutorRegistry()
EXECUTOR_REGISTRY.register(CoreFlowExecutor())
EXECUTOR_REGISTRY.register(MechanicsExecutor())
EXECUTOR_REGISTRY.register(LatencyExecutor())
EXECUTOR_REGISTRY.register(FragmentedExecutor())
EXECUTOR_REGISTRY.register(EcologyExecutor())
EXECUTOR_REGISTRY.register(AlgorithmExecutor())
EXECUTOR_REGISTRY.register(FaultExecutor())


__all__ = [
    "CAPABILITY_MATRIX",
    "ALGORITHM_RECORDING_TYPE",
    "CORE_FLOW_RECORDING_TYPE",
    "ECOLOGY_RECORDING_TYPE",
    "EXECUTOR_REGISTRY",
    "FRAGMENTED_RECORDING_TYPE",
    "FAULT_RECORDING_TYPE",
    "LATENCY_RECORDING_TYPE",
    "MECHANICS_RECORDING_TYPE",
    "AuditCaseExecutor",
    "AlgorithmExecutor",
    "CoreFlowExecutor",
    "EcologyExecutor",
    "ExecutorRegistry",
    "FragmentedExecutor",
    "FaultExecutor",
    "LaneCapabilitySpec",
    "LatencyExecutor",
    "MechanicsExecutor",
]
