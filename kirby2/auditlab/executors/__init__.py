"""Real generated-case executors, registered lane by lane as implemented."""

from .base import (
    CAPABILITY_MATRIX,
    AuditCaseExecutor,
    ExecutorRegistry,
    LaneCapabilitySpec,
)


EXECUTOR_REGISTRY = ExecutorRegistry()


__all__ = [
    "CAPABILITY_MATRIX",
    "EXECUTOR_REGISTRY",
    "AuditCaseExecutor",
    "ExecutorRegistry",
    "LaneCapabilitySpec",
]
