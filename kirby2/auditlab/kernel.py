"""Single lane-dispatched entry point for generated audit cases."""

from __future__ import annotations

from .models import (
    CheckStatus,
    GeneratedCaseResult,
    GeneratedConfiguration,
)


def run_generated_case(
    configuration: GeneratedConfiguration,
) -> GeneratedCaseResult:
    """Dispatch one configuration to the registered real subsystem executor."""

    if not isinstance(configuration, GeneratedConfiguration):
        raise TypeError("generated audit execution requires GeneratedConfiguration")
    from .executors import EXECUTOR_REGISTRY

    return EXECUTOR_REGISTRY.execute(configuration)


def failure_signatures(result: GeneratedCaseResult) -> tuple[str, ...]:
    """Return stable failure identities without reinterpreting executor evidence."""

    if not isinstance(result, GeneratedCaseResult):
        raise TypeError("failure signatures require GeneratedCaseResult")
    signatures = [
        f"FAILURE:{failure.kind.value}:{failure.code}"
        for failure in result.failures
    ]
    signatures.extend(
        f"CHECK:{result.lane.value}:{check.name}:{check.status.value}"
        for check in result.checks
        if check.status is CheckStatus.FAIL
        or (check.required and check.status is CheckStatus.NOT_EXERCISED)
    )
    return tuple(dict.fromkeys(signatures))
