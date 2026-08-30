"""Fresh-process real-executor adapter on the shared data-only worker seam."""

from __future__ import annotations

from kirby2.orchestration.worker import run_data_only_stdio_worker

from .kernel import run_generated_case
from .models import GeneratedConfiguration


def execute_generated_configuration(payload: dict[str, object]) -> dict[str, object]:
    """Preserve the audit worker's raw request and response object shapes."""

    configuration = GeneratedConfiguration.from_dict(payload)
    result = run_generated_case(configuration)
    return result.declared_outputs()


def main() -> None:
    run_data_only_stdio_worker(execute_generated_configuration)


if __name__ == "__main__":
    main()
