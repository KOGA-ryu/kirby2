"""Local terminal-trainer and explicit offline-report desktop entrypoint."""

from __future__ import annotations

import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from .headless import RELEASE_BOUNDARIES_V1, run_canonical_cli


RELEASE_DESKTOP_ID_V1 = "DESKTOP_V1"

_HELP = """\
usage:
  kirby2-desktop [TRAINER_OPTIONS...]
  kirby2-desktop trainer [TRAINER_OPTIONS...]
  kirby2-desktop cli KIRBY2_COMMAND [ARGS...]
  kirby2-desktop microscope [MICROSCOPE_OPTIONS...]
  kirby2-desktop open-report REPORT_DIRECTORY

Kirby2 DESKTOP_V1 is a local terminal execution trainer plus explicit local,
offline HTML analysis. It is not a native-widget GUI and starts no web server.

Modes:
  trainer      Run the canonical `kirby2 ui` terminal trainer (the default).
  cli          Run any canonical Kirby2 command, including scenario authoring.
  microscope   Build a portable report through `kirby2 microscope-demo`.
  open-report  Verify and explicitly open one portable local report.

Trainer options are the options reported by `kirby2-desktop trainer --help`.
"""


def _arguments(argv: Sequence[str] | None) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise TypeError("desktop arguments must be a sequence of strings")
    selected = tuple(sys.argv[1:] if argv is None else argv)
    if any(type(item) is not str or "\x00" in item for item in selected):
        raise TypeError("desktop arguments must be NUL-free strings")
    return selected


def _print_help() -> None:
    print(_HELP.rstrip())
    print()
    print("Release boundaries:")
    for boundary in RELEASE_BOUNDARIES_V1:
        print(f"  - {boundary}")


def _open_report(value: str) -> int:
    selected = Path(value).expanduser().resolve(strict=False)
    root = selected.parent if selected.name == "index.html" else selected

    from kirby2.microscope.report import verify_portable_report_bundle

    verification = verify_portable_report_bundle(root)
    index = root / "index.html"
    opened = webbrowser.open(index.as_uri(), new=2, autoraise=True)
    print(
        "KIRBY2_OFFLINE_REPORT "
        f"status={verification['status']} "
        f"bundle_id={verification['bundle_id']} "
        f"path={index}"
    )
    if not opened:
        print(
            "The report verified, but no local browser accepted the open request. "
            "Open the printed index.html path manually.",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one explicit desktop action onto canonical Kirby2 code."""

    arguments = _arguments(argv)
    if arguments and arguments[0] in {"-h", "--help"}:
        _print_help()
        return 0
    if not arguments:
        return run_canonical_cli(("ui",))

    mode, *remainder = arguments
    if mode == "trainer":
        return run_canonical_cli(("ui", *remainder))
    if mode == "cli":
        if not remainder:
            raise SystemExit("kirby2-desktop cli requires a Kirby2 command")
        return run_canonical_cli(tuple(remainder))
    if mode == "microscope":
        return run_canonical_cli(("microscope-demo", *remainder))
    if mode == "open-report":
        if len(remainder) != 1:
            raise SystemExit(
                "kirby2-desktop open-report requires exactly one report directory"
            )
        return _open_report(remainder[0])
    if mode.startswith("-"):
        return run_canonical_cli(("ui", *arguments))
    raise SystemExit(f"unknown Kirby2 desktop mode: {mode!r}")


if __name__ == "__main__":  # pragma: no cover - console entrypoint
    raise SystemExit(main())


__all__ = ["RELEASE_DESKTOP_ID_V1", "main"]
