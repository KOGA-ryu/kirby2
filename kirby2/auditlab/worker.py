"""Fresh-process real-executor worker; JSON configuration enters on stdin."""

from __future__ import annotations

import json
import sys

from .kernel import run_generated_case
from .models import GeneratedConfiguration, canonical_json


def main() -> None:
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        raise ValueError("worker input must be a configuration object")
    configuration = GeneratedConfiguration.from_dict(payload)
    result = run_generated_case(configuration)
    sys.stdout.write(canonical_json(result.declared_outputs()) + "\n")


if __name__ == "__main__":
    main()
