"""Installed headless entrypoint over Kirby2's one canonical command surface."""

from __future__ import annotations

import sys
from collections.abc import Sequence


RELEASE_BOUNDARIES_V1 = (
    "Kirby2 is a simulation and training environment.",
    "Kirby2 is not a broker.",
    "Kirby2 is not a live market connector.",
    "Kirby2 provides no performance guarantee.",
    "A reconstruction is not proof of historical market state.",
)


def run_canonical_cli(argv: Sequence[str]) -> int:
    """Run the existing Kirby2 parser and handlers with an explicit argument vector.

    Desktop and headless release surfaces delegate here so they cannot acquire a
    second simulation plan, runtime, store, or command router.
    """

    if isinstance(argv, (str, bytes)):
        raise TypeError("release CLI arguments must be a sequence of strings")
    arguments = tuple(argv)
    if any(type(item) is not str or "\x00" in item for item in arguments):
        raise TypeError("release CLI arguments must be NUL-free strings")

    from kirby2.__main__ import main as canonical_main

    previous = sys.argv
    program = previous[0] if previous else "kirby2-headless"
    sys.argv = [program, *arguments]
    try:
        canonical_main()
    finally:
        sys.argv = previous
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the complete installed offline CLI without introducing another router."""

    return run_canonical_cli(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover - console entrypoint
    raise SystemExit(main())


__all__ = ["RELEASE_BOUNDARIES_V1", "main", "run_canonical_cli"]
