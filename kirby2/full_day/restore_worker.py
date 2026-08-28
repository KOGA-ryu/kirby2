"""Fresh-interpreter stdin/stdout protocol for core-session restoration.

Input is exactly one canonical ``CoreRestoreRequestV1`` JSON object on stdin.
Success writes exactly one canonical JSON result to stdout and nothing to
stderr.  A schema, digest, or invariant refusal writes a deterministic
diagnostic to stderr, writes nothing to stdout, and exits nonzero.  This worker
does not accept a seed or prefix commands and performs no filesystem writes.
"""

from __future__ import annotations

import sys

from kirby2.full_day.models import canonical_json_bytes
from kirby2.full_day.restore import (
    CoreRestoreRequestV1,
    execute_core_restore_request,
)


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        request = CoreRestoreRequestV1.from_json_bytes(raw)
        result = execute_core_restore_request(request)
        output = canonical_json_bytes(result)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        diagnostic = (
            f"CORE_RESTORE_REFUSED {type(error).__name__}: {error}\n"
        ).encode("utf-8", errors="backslashreplace")
        sys.stderr.buffer.write(diagnostic)
        return 2
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
