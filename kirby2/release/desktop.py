"""Local terminal-trainer and explicit offline-report desktop entrypoint."""

from __future__ import annotations

import hashlib
import sys
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .headless import RELEASE_BOUNDARIES_V1, run_canonical_cli

if TYPE_CHECKING:
    from kirby2.session.live import SessionSnapshot
    from kirby2.ui.terminal import TerminalFrameFlushSink, TerminalPresentedFrameV2


RELEASE_DESKTOP_ID_V1 = "DESKTOP_V1"


@dataclass(frozen=True, slots=True)
class ReleaseTerminalPresentationTickV2:
    """One cadence tick bound to the latest nonregressing client-visible cut."""

    tick_ordinal: int
    presentation_time_us: int
    causal_source_sequence: int
    source_delivery_time_us: int
    source_market_time_us: int
    snapshot: SessionSnapshot


@dataclass(frozen=True, slots=True)
class ReleaseTerminalPresentedUpdateV2:
    """One production-visible and synchronously flushed full-day terminal update."""

    frame: TerminalPresentedFrameV2
    source_delivery_time_us: int
    source_market_time_us: int

    def as_dict(self) -> dict[str, object]:
        return {
            **self.frame.as_dict(),
            "source_delivery_time_us": self.source_delivery_time_us,
            "source_market_time_us": self.source_market_time_us,
        }


@dataclass(frozen=True, slots=True)
class _ReleaseTerminalMarketStateDeliveryV2:
    """One raw delivered state retained in exact client-delivery order."""

    delivery_time_us: int
    source_sequence: int
    source_market_time_us: int
    market: dict[str, object]


def _presentation_policy_v2(value: object) -> dict[str, object]:
    from kirby2.ui.terminal import RELEASE_TERMINAL_PRESENTATION_POLICY_V2

    if type(value) is not dict or value != RELEASE_TERMINAL_PRESENTATION_POLICY_V2:
        raise ValueError("release terminal presentation policy differs from V2")
    return dict(value)


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _terminal_presentation_inputs_v2(
    delivered_messages: Sequence[Mapping[str, object]],
    *,
    continuous_start_us: int,
    continuous_end_us: int,
    duration_us: int,
    presentation: Mapping[str, object],
) -> tuple[
    dict[str, object],
    int,
    int,
    int,
    tuple[_ReleaseTerminalMarketStateDeliveryV2, ...],
]:
    policy = _presentation_policy_v2(presentation)
    start = _nonnegative_integer(continuous_start_us, "continuous start")
    end = _nonnegative_integer(continuous_end_us, "continuous end")
    duration = _nonnegative_integer(duration_us, "terminal duration")
    if not start < end <= duration:
        raise ValueError("terminal continuous boundary is invalid")
    if not isinstance(delivered_messages, Sequence) or isinstance(
        delivered_messages, (str, bytes)
    ):
        raise TypeError("terminal delivered messages must be a sequence")

    messages: list[_ReleaseTerminalMarketStateDeliveryV2] = []
    last_delivery_time = -1
    message_sequences: set[int] = set()
    for raw in delivered_messages:
        if not isinstance(raw, Mapping) or raw.get("kind") != "MARKET_STATE":
            continue
        sequence = _nonnegative_integer(
            raw.get("message_sequence"), "terminal source message sequence"
        )
        delivery_time = _nonnegative_integer(
            raw.get("delivery_time_us"), "terminal source delivery time"
        )
        client_payload = raw.get("client_payload")
        market = (
            None
            if not isinstance(client_payload, Mapping)
            else client_payload.get("market_state")
        )
        if sequence <= 0 or not isinstance(market, Mapping):
            raise RuntimeError("client-observable terminal update is malformed")
        source_market_time = _nonnegative_integer(
            market.get("simulation_time_us"), "terminal source market time"
        )
        if source_market_time > delivery_time:
            raise RuntimeError("terminal source delivery precedes its market cut")
        if delivery_time < last_delivery_time or sequence in message_sequences:
            raise RuntimeError("terminal source delivery order is not causal")
        last_delivery_time = delivery_time
        message_sequences.add(sequence)
        messages.append(
            _ReleaseTerminalMarketStateDeliveryV2(
                delivery_time_us=delivery_time,
                source_sequence=sequence,
                source_market_time_us=source_market_time,
                market=dict(market),
            )
        )
    if not messages:
        raise RuntimeError("terminal source has no delivered market state")
    return policy, start, end, duration, tuple(messages)


def _presentation_ticks_from_deliveries_v2(
    messages: tuple[_ReleaseTerminalMarketStateDeliveryV2, ...],
    *,
    start: int,
    end: int,
    duration: int,
    policy: Mapping[str, object],
) -> tuple[ReleaseTerminalPresentationTickV2, ...]:
    step = _nonnegative_integer(policy["simulation_step_us"], "presentation step")
    if step <= 0:
        raise ValueError("terminal presentation step must be positive")
    output: list[ReleaseTerminalPresentationTickV2] = []
    cursor = 0
    selected: _ReleaseTerminalMarketStateDeliveryV2 | None = None
    for tick_ordinal, presentation_time in enumerate(range(start, end, step)):
        while (
            cursor < len(messages)
            and messages[cursor].delivery_time_us <= presentation_time
        ):
            candidate = messages[cursor]
            cursor += 1
            # DeliveryOwnerV1 consumes every delivery but changes the client-visible
            # cut only when source-market time does not regress.  A late stale cut
            # remains raw delivery evidence and never rolls presentation backward.
            if (
                selected is None
                or candidate.source_market_time_us >= selected.source_market_time_us
            ):
                selected = candidate
        if selected is None:
            raise RuntimeError(
                "terminal source has no causal market state at continuous start"
            )
        output.append(
            ReleaseTerminalPresentationTickV2(
                tick_ordinal=tick_ordinal,
                presentation_time_us=presentation_time,
                causal_source_sequence=selected.source_sequence,
                source_delivery_time_us=selected.delivery_time_us,
                source_market_time_us=selected.source_market_time_us,
                snapshot=_release_terminal_snapshot_v2(
                    selected.market,
                    presentation_time_us=presentation_time,
                    causal_source_sequence=selected.source_sequence,
                    duration_us=duration,
                ),
            )
        )
    return tuple(output)


def _release_terminal_snapshot_v2(
    market: Mapping[str, object],
    *,
    presentation_time_us: int,
    causal_source_sequence: int,
    duration_us: int,
) -> SessionSnapshot:
    """Translate a disclosure-safe full-day cut into the production terminal model."""

    from kirby2.packs.formats import canonical_json_bytes
    from kirby2.session.live import LevelView, SessionSnapshot

    source_market_time_us = _nonnegative_integer(
        market.get("simulation_time_us"), "terminal source market time"
    )

    def levels(name: str) -> tuple[LevelView, ...]:
        raw = market.get(name)
        if type(raw) is not list:
            raise TypeError("terminal market-state levels must be an array")
        output: list[LevelView] = []
        for item in raw[:10]:
            if not isinstance(item, Mapping) or set(item) != {
                "price_ticks",
                "quantity",
            }:
                raise ValueError("terminal market level fields differ")
            price = _nonnegative_integer(
                item["price_ticks"], "terminal market level price"
            )
            quantity = _nonnegative_integer(
                item["quantity"], "terminal market level quantity"
            )
            output.append(
                LevelView(
                    price_ticks=price,
                    price=str(price),
                    aggregate_quantity=quantity,
                    player_quantity=0,
                    queue_ahead_quantity=None,
                )
            )
        return tuple(output)

    bids = levels("bid_levels")
    asks = levels("ask_levels")
    market_state_id = hashlib.sha256(
        canonical_json_bytes(dict(market))
    ).hexdigest()
    session_state = market.get("session_state")
    if type(session_state) is not str or not session_state:
        raise ValueError("terminal source session state is invalid")
    return SessionSnapshot(
        scenario_name="QUIET_RANGE_PRESSURE",
        regime=session_state,
        seed=None,
        relative_volume="PROFILE",
        liquidity="PROFILE",
        simulation_time_us=presentation_time_us,
        duration_us=duration_us,
        running=True,
        complete=False,
        selected_quantity=1,
        position=0,
        bought_quantity=0,
        sold_quantity=0,
        bids=bids,
        asks=asks,
        tape=(),
        working_orders=(),
        traffic_light="RECORDED",
        traffic_setup=None,
        strategy_state=None,
        strategy_entry_permission="RECORDED",
        strategy_exit_permission="RECORDED",
        traffic_reason="Client-observable simulated full-day state",
        objective_type=None,
        objective_target_quantity=0,
        objective_completed_quantity=0,
        objective_completion_percentage="0",
        objective_time_limit_us=None,
        # Do not expose the presentation ordinal here.  A new ordinal alone must
        # never manufacture a visible change; the rendered clock or source state
        # has to change the actual frame bytes.
        status_message=f"OBSERVED SOURCE {causal_source_sequence}",
        exchange_event_sequence=causal_source_sequence,
        market_state_id=market_state_id,
        market_state_time_us=source_market_time_us,
    )


def iter_release_terminal_presentation_ticks_v2(
    delivered_messages: Sequence[Mapping[str, object]],
    *,
    continuous_start_us: int,
    continuous_end_us: int,
    duration_us: int,
    presentation: Mapping[str, object],
) -> tuple[ReleaseTerminalPresentationTickV2, ...]:
    """Build the exact nonregressing presentation stream for one full-day source."""

    policy, start, end, duration, messages = _terminal_presentation_inputs_v2(
        delivered_messages,
        continuous_start_us=continuous_start_us,
        continuous_end_us=continuous_end_us,
        duration_us=duration_us,
        presentation=presentation,
    )
    return _presentation_ticks_from_deliveries_v2(
        messages,
        start=start,
        end=end,
        duration=duration,
        policy=policy,
    )


def present_release_terminal_updates_v2(
    delivered_messages: Sequence[Mapping[str, object]],
    *,
    continuous_start_us: int,
    continuous_end_us: int,
    duration_us: int,
    presentation: Mapping[str, object],
    sink: TerminalFrameFlushSink,
    required_update_count: int,
    width: int,
) -> tuple[
    tuple[ReleaseTerminalPresentedUpdateV2, ...],
    dict[str, object],
]:
    """Run the canonical noninteractive production terminal playback seam.

    This public desktop entrypoint owns causal source selection, the presentation
    clock, production rendering, visible-change eligibility, and synchronous flush.
    The installed release auxiliary invokes this seam instead of calling the renderer
    directly.  Interactive curses input remains a separate client because a replay
    source has no keyboard/session mutation loop.
    """

    from kirby2.session.bindings import BindingMap
    from kirby2.ui.terminal import TerminalFramePresenterV2, TerminalUiConfig

    policy, start, end, duration, delivered_states = _terminal_presentation_inputs_v2(
        delivered_messages,
        continuous_start_us=continuous_start_us,
        continuous_end_us=continuous_end_us,
        duration_us=duration_us,
        presentation=presentation,
    )
    required = _nonnegative_integer(
        required_update_count, "required terminal update count"
    )
    if required <= 0:
        raise ValueError("required terminal update count must be positive")
    config = TerminalUiConfig(
        speed=policy["simulation_speed_milli"] / 1_000,  # type: ignore[operator]
        frame_milliseconds=policy["frame_milliseconds"],  # type: ignore[arg-type]
    )
    if config.simulation_step_us != policy["simulation_step_us"]:
        raise RuntimeError("production terminal cadence differs from V2")
    ticks = _presentation_ticks_from_deliveries_v2(
        delivered_states,
        start=start,
        end=end,
        duration=duration,
        policy=policy,
    )
    if len(ticks) < required:
        raise RuntimeError("terminal presentation cadence cannot supply 5100 updates")
    presenter = TerminalFramePresenterV2(
        BindingMap.default(),
        config,
        sink,
        width=width,
        policy_id=policy["policy_id"],  # type: ignore[arg-type]
        visible_change_policy_id=policy[  # type: ignore[arg-type]
            "visible_change_policy_id"
        ],
        clock_source_id=policy["clock_source_id"],  # type: ignore[arg-type]
        latency_boundary_policy_id=policy[  # type: ignore[arg-type]
            "latency_boundary_policy_id"
        ],
    )
    updates: list[ReleaseTerminalPresentedUpdateV2] = []
    skipped_unchanged = 0
    for tick in ticks:
        frame = presenter.present(
            tick.snapshot,
            tick_ordinal=tick.tick_ordinal,
            presentation_time_us=tick.presentation_time_us,
            causal_source_sequence=tick.causal_source_sequence,
        )
        if frame is None:
            skipped_unchanged += 1
            continue
        updates.append(
            ReleaseTerminalPresentedUpdateV2(
                frame=frame,
                source_delivery_time_us=tick.source_delivery_time_us,
                source_market_time_us=tick.source_market_time_us,
            )
        )
        if len(updates) == required:
            break
    if len(updates) != required:
        raise RuntimeError("terminal presentation lacks 5100 visible changed frames")
    sequences = tuple(item.frame.causal_source_sequence for item in updates)
    delivered_sequences = tuple(item.source_sequence for item in delivered_states)
    source_delivery_times = tuple(item.source_delivery_time_us for item in updates)
    source_market_times = tuple(item.source_market_time_us for item in updates)
    frame_digests = tuple(item.frame.frame_sha256 for item in updates)
    if (
        source_delivery_times != tuple(sorted(source_delivery_times))
        or source_market_times != tuple(sorted(source_market_times))
        or any(left == right for left, right in zip(frame_digests, frame_digests[1:]))
    ):
        raise RuntimeError(
            "terminal presentation causality or visible-change proof failed"
        )
    feasibility = {
        "available_tick_count": len(ticks),
        "boundary_policy_id": policy["boundary_policy_id"],
        "causal_source_policy_id": policy["causal_source_policy_id"],
        "clock_source_id": policy["clock_source_id"],
        "consumed_tick_count": presenter.tick_count,
        "continuous_end_us": continuous_end_us,
        "continuous_start_us": continuous_start_us,
        "delivered_source_sequence_count": len(delivered_sequences),
        "delivered_source_sequence_reorder_count": sum(
            right < left
            for left, right in zip(delivered_sequences, delivered_sequences[1:])
        ),
        "delivered_source_sequences": list(delivered_sequences),
        "duplicate_source_policy_id": policy["duplicate_source_policy_id"],
        "eligible_visible_update_count": len(updates),
        "first_causal_source_sequence": sequences[0],
        "first_presentation_time_us": updates[0].frame.presentation_time_us,
        "frame_milliseconds": policy["frame_milliseconds"],
        "last_causal_source_sequence": sequences[-1],
        "last_presentation_time_us": updates[-1].frame.presentation_time_us,
        "latency_boundary_policy_id": policy["latency_boundary_policy_id"],
        "policy_id": policy["policy_id"],
        "presented_source_sequence_reorder_count": sum(
            right < left for left, right in zip(sequences, sequences[1:])
        ),
        "presented_source_sequence_reuse_count": sum(
            left == right for left, right in zip(sequences, sequences[1:])
        ),
        "required_update_count": required,
        "schema_version": 2,
        "simulation_speed_milli": policy["simulation_speed_milli"],
        "simulation_step_us": policy["simulation_step_us"],
        "skipped_unchanged_tick_count": skipped_unchanged,
        "status": "PASS",
        "update_ordinal_policy_id": policy["update_ordinal_policy_id"],
        "visible_change_policy_id": policy["visible_change_policy_id"],
    }
    return tuple(updates), feasibility

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


__all__ = [
    "RELEASE_DESKTOP_ID_V1",
    "ReleaseTerminalPresentationTickV2",
    "ReleaseTerminalPresentedUpdateV2",
    "iter_release_terminal_presentation_ticks_v2",
    "main",
    "present_release_terminal_updates_v2",
]
