"""Keyboard-first terminal interface driven only by session snapshots."""

from __future__ import annotations

import curses
import hashlib
import math
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from kirby2.session.bindings import BindingMap, SessionCommand
from kirby2.session.live import LevelView, LiveMarketSession, SessionSnapshot

if TYPE_CHECKING:
    from kirby2.release.recovery import RecoveryActionV1, RecoveryOfferV1
    from kirby2.research.paths import DataPaths
    from kirby2.session.layouts import HotkeyLayout


MINIMUM_WIDTH = 116
MINIMUM_HEIGHT = 34

# Production-owned identity for the deterministic noninteractive presentation
# stream used by the installed desktop playback seam.  Release qualification
# transcribes this exact object into its input identity; production does not import
# benchmark configuration in order to decide how a terminal frame is presented.
RELEASE_TERMINAL_PRESENTATION_POLICY_V2 = {
    "boundary_policy_id": "CONTINUOUS_INCLUDE_START_EXCLUDE_END_V1",
    "causal_source_policy_id": (
        "LATEST_NONREGRESSING_CLIENT_VISIBLE_MARKET_STATE_AT_OR_BEFORE_TICK_V1"
    ),
    "clock_source_id": "TIME_PERF_COUNTER_NS_V1",
    "duplicate_source_policy_id": "ALLOW_REUSE_AND_ASYNC_SEQUENCE_REORDER_V1",
    "frame_milliseconds": 50,
    "latency_boundary_policy_id": "RENDER_HASH_WRITE_AND_DRAIN_V1",
    "policy_id": "RELEASE_TERMINAL_PRESENTATION_V2",
    "schema_version": 2,
    "simulation_speed_milli": 10_000,
    "simulation_step_us": 500_000,
    "update_ordinal_policy_id": "CONTIGUOUS_VISIBLE_UPDATES_FROM_ZERO_V1",
    "visible_change_policy_id": "ADJACENT_RENDERED_FRAME_SHA256_DIFFERS_V1",
}


_TERMINAL_FRAME_DIGEST_CHAIN_DOMAIN_V1 = (
    b"KIRBY2_TERMINAL_FRAME_DIGEST_CHAIN_V1\x00"
)


def advance_terminal_frame_digest_chain_v1(
    previous_chain_sha256: str | None,
    frame_sha256: str,
) -> str:
    """Hash domain, prior raw digest (zero for first), and current frame digest."""

    def digest_bytes(value: str, label: str) -> bytes:
        if (
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return bytes.fromhex(value)

    previous = (
        bytes(32)
        if previous_chain_sha256 is None
        else digest_bytes(previous_chain_sha256, "previous terminal frame chain")
    )
    frame = digest_bytes(frame_sha256, "terminal frame")
    return hashlib.sha256(
        _TERMINAL_FRAME_DIGEST_CHAIN_DOMAIN_V1 + previous + frame
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class TerminalUiConfig:
    speed: float = 10.0
    frame_milliseconds: int = 50
    ladder_levels: int = 7
    tape_rows: int = 12
    working_order_rows: int = 5
    layout_name: str = "layout_default"
    recovery_checkpoint_frames: int = 20

    def __post_init__(self) -> None:
        if not math.isfinite(self.speed) or self.speed <= 0:
            raise ValueError("UI simulation speed must be finite and positive")
        if type(self.frame_milliseconds) is not int or self.frame_milliseconds < 20:
            raise ValueError("UI frame interval must be at least 20 milliseconds")
        if not 5 <= self.ladder_levels <= 10:
            raise ValueError("Level 2 ladder must show between 5 and 10 levels per side")
        if type(self.tape_rows) is not int or self.tape_rows <= 0:
            raise ValueError("Time & Sales row count must be positive")
        if type(self.working_order_rows) is not int or self.working_order_rows <= 0:
            raise ValueError("working-order row count must be positive")
        if not self.layout_name:
            raise ValueError("hotkey layout name must not be empty")
        if (
            type(self.recovery_checkpoint_frames) is not int
            or self.recovery_checkpoint_frames <= 0
        ):
            raise ValueError("recovery checkpoint cadence must be positive")

    @property
    def simulation_step_us(self) -> int:
        return max(1, round(self.frame_milliseconds * 1_000 * self.speed))


class TerminalFrameFlushSink(Protocol):
    """Production sink contract for one complete visible terminal frame."""

    def write_and_flush(self, lines: tuple[str, ...]) -> int:
        """Write all frame bytes and return only after they are client-visible."""


@dataclass(frozen=True, slots=True)
class TerminalPresentedFrameV2:
    """One genuinely changed frame after the production flush boundary."""

    policy_id: str
    tick_ordinal: int
    update_ordinal: int
    presentation_time_us: int
    causal_source_sequence: int
    market_state_id: str
    frame_sha256: str
    frame_digest_chain_sha256: str
    frame_bytes: int
    latency_ns: int
    schema_version: int = 2

    def as_dict(self) -> dict[str, object]:
        return {
            "causal_source_sequence": self.causal_source_sequence,
            "frame_bytes": self.frame_bytes,
            "frame_digest_chain_sha256": self.frame_digest_chain_sha256,
            "frame_sha256": self.frame_sha256,
            "latency_ns": self.latency_ns,
            "market_state_id": self.market_state_id,
            "policy_id": self.policy_id,
            "presentation_time_us": self.presentation_time_us,
            "schema_version": self.schema_version,
            "tick_ordinal": self.tick_ordinal,
            "update_ordinal": self.update_ordinal,
        }


class TerminalFramePresenterV2:
    """Render, visible-change filter, and synchronously flush terminal frames.

    Tick ordinals describe the frozen presentation cadence.  Update ordinals are
    assigned only after the rendered bytes differ from the immediately preceding
    visible frame, so an unchanged duplicate can never become a sample merely by
    receiving a fresh ordinal.  Asynchronous delivery may expose creation-sequence
    IDs out of order; the desktop playback owner separately proves nonregressing
    delivery time and market-state cut time.  Changed-frame latency begins immediately
    before rendering and ends only after the sink's synchronous write-and-drain return;
    evidence-object and digest-chain assembly occurs outside that measured boundary.
    """

    def __init__(
        self,
        bindings: BindingMap,
        config: TerminalUiConfig,
        sink: TerminalFrameFlushSink,
        *,
        width: int,
        policy_id: str,
        visible_change_policy_id: str,
        clock_source_id: str,
        latency_boundary_policy_id: str,
    ) -> None:
        if type(bindings) is not BindingMap or type(config) is not TerminalUiConfig:
            raise TypeError("terminal presenter requires exact bindings and UI config")
        if not callable(getattr(sink, "write_and_flush", None)):
            raise TypeError("terminal presenter sink omits write_and_flush")
        if type(width) is not int or width <= 0:
            raise ValueError("terminal presenter width must be positive")
        if type(policy_id) is not str or not policy_id:
            raise ValueError("terminal presenter policy ID is invalid")
        if (
            visible_change_policy_id
            != "ADJACENT_RENDERED_FRAME_SHA256_DIFFERS_V1"
            or clock_source_id != "TIME_PERF_COUNTER_NS_V1"
            or latency_boundary_policy_id != "RENDER_HASH_WRITE_AND_DRAIN_V1"
        ):
            raise ValueError("terminal presenter policy contract differs")
        self._bindings = bindings
        self._config = config
        self._sink = sink
        self._width = width
        self._policy_id = policy_id
        self._next_tick_ordinal = 0
        self._next_update_ordinal = 0
        self._last_presentation_time_us: int | None = None
        self._last_causal_source_sequence: int | None = None
        self._last_visible_sha256: str | None = None
        self._last_frame_digest_chain_sha256: str | None = None

    @property
    def tick_count(self) -> int:
        return self._next_tick_ordinal

    @property
    def visible_update_count(self) -> int:
        return self._next_update_ordinal

    def present(
        self,
        snapshot: SessionSnapshot,
        *,
        tick_ordinal: int,
        presentation_time_us: int,
        causal_source_sequence: int,
    ) -> TerminalPresentedFrameV2 | None:
        """Present one cadence tick, returning ``None`` for unchanged bytes."""

        if type(snapshot) is not SessionSnapshot:
            raise TypeError("terminal presentation requires SessionSnapshot")
        if type(tick_ordinal) is not int or tick_ordinal != self._next_tick_ordinal:
            raise ValueError("terminal presentation tick ordinals must be contiguous")
        if (
            type(presentation_time_us) is not int
            or presentation_time_us < 0
            or snapshot.simulation_time_us != presentation_time_us
        ):
            raise ValueError("terminal presentation time differs from its snapshot")
        if (
            self._last_presentation_time_us is not None
            and presentation_time_us <= self._last_presentation_time_us
        ):
            raise ValueError("terminal presentation times must increase")
        if (
            type(causal_source_sequence) is not int
            or causal_source_sequence <= 0
            or snapshot.exchange_event_sequence != causal_source_sequence
        ):
            raise ValueError("terminal causal source sequence differs from its snapshot")
        started_ns = time.perf_counter_ns()
        lines = render_terminal_frame(
            snapshot,
            self._bindings,
            self._config,
            width=self._width,
        )
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        frame_sha256 = hashlib.sha256(payload).hexdigest()
        if frame_sha256 == self._last_visible_sha256:
            self._next_tick_ordinal += 1
            self._last_presentation_time_us = presentation_time_us
            self._last_causal_source_sequence = causal_source_sequence
            return None

        frame_bytes = self._sink.write_and_flush(lines)
        latency_ns = time.perf_counter_ns() - started_ns
        if frame_bytes != len(payload):
            raise RuntimeError("terminal sink did not flush the complete frame")
        frame_digest_chain_sha256 = advance_terminal_frame_digest_chain_v1(
            self._last_frame_digest_chain_sha256,
            frame_sha256,
        )
        presented = TerminalPresentedFrameV2(
            policy_id=self._policy_id,
            tick_ordinal=tick_ordinal,
            update_ordinal=self._next_update_ordinal,
            presentation_time_us=presentation_time_us,
            causal_source_sequence=causal_source_sequence,
            market_state_id=snapshot.market_state_id,
            frame_sha256=frame_sha256,
            frame_digest_chain_sha256=frame_digest_chain_sha256,
            frame_bytes=frame_bytes,
            latency_ns=latency_ns,
        )
        self._next_tick_ordinal += 1
        self._next_update_ordinal += 1
        self._last_presentation_time_us = presentation_time_us
        self._last_causal_source_sequence = causal_source_sequence
        self._last_visible_sha256 = frame_sha256
        self._last_frame_digest_chain_sha256 = frame_digest_chain_sha256
        return presented


def render_terminal_frame(
    snapshot: SessionSnapshot,
    bindings: BindingMap,
    config: TerminalUiConfig,
    width: int = 140,
) -> tuple[str, ...]:
    run_state = "RUN" if snapshot.running else "PAUSE"
    if snapshot.complete:
        run_state = "COMPLETE"
    header = (
        f"KIRBY2  {snapshot.scenario_name}/{snapshot.regime}  "
        f"seed={snapshot.seed if snapshot.seed is not None else 'HIDDEN'}  "
        f"SIM {_market_time(snapshot.simulation_time_us)} / "
        f"{_elapsed_time(snapshot.duration_us)}  {run_state} {config.speed:g}x"
    )
    configuration = (
        f"VOLUME {snapshot.relative_volume}  LIQUIDITY {snapshot.liquidity}  "
        f"LAYOUT {config.layout_name}"
    )
    traffic = (
        f"TRAFFIC LIGHT [ {snapshot.traffic_light:^12} ]  "
        f"SETUP {snapshot.traffic_setup or '-'}"
    )
    if snapshot.strategy_state is not None:
        traffic += (
            f"  STATE {snapshot.strategy_state}  "
            f"ENTRY {snapshot.strategy_entry_permission}  "
            f"EXIT {snapshot.strategy_exit_permission}"
        )
    traffic_reason = f"TRAFFIC WHY  {snapshot.traffic_reason}"
    objective = (
        "OBJECTIVE none"
        if snapshot.objective_type is None
        else (
            f"OBJECTIVE {snapshot.objective_type}  "
            f"{snapshot.objective_completed_quantity}/"
            f"{snapshot.objective_target_quantity} "
            f"({snapshot.objective_completion_percentage}%)  "
            f"LIMIT {_elapsed_time(snapshot.objective_time_limit_us or 0)}"
        )
    )
    account = (
        f"POSITION {snapshot.position:+d}  BOUGHT {snapshot.bought_quantity}  "
        f"SOLD {snapshot.sold_quantity}  ORDER QTY {snapshot.selected_quantity}  "
        f"WORKING {len(snapshot.working_orders)}"
    )

    left_width = 64
    right_width = max(42, width - left_width - 3)
    ladder = _ladder_lines(snapshot, config.ladder_levels, left_width)
    tape = _tape_lines(snapshot, config.tape_rows, right_width)
    body_rows = max(len(ladder), len(tape))
    lines = [
        header[:width],
        configuration[:width],
        traffic[:width],
        traffic_reason[:width],
        objective[:width],
        account[:width],
        "",
    ]
    for index in range(body_rows):
        left = ladder[index] if index < len(ladder) else ""
        right = tape[index] if index < len(tape) else ""
        lines.append(f"{left:<{left_width}} | {right}"[:width])

    lines.append("")
    lines.extend(_working_order_lines(snapshot, config.working_order_rows, width))
    lines.append(f"STATUS  {snapshot.status_message}"[:width])
    lines.extend(_legend_lines(bindings, width))
    return tuple(lines)


def run_terminal_ui(
    session: LiveMarketSession,
    bindings: BindingMap | None = None,
    config: TerminalUiConfig | None = None,
    *,
    recovery_paths: DataPaths | None = None,
    recovery_action: RecoveryActionV1 | None = None,
) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Kirby2 UI requires an interactive terminal")
    actual_bindings = bindings or BindingMap.default()
    actual_config = config or TerminalUiConfig()

    from kirby2.release.platform_paths import platform_data_paths
    from kirby2.release.recovery import InteractiveRecoveryCoordinatorV1
    from kirby2.session.journal import LiveSessionSourceV1
    from kirby2.session.layouts import HotkeyLayout

    actual_paths = recovery_paths or platform_data_paths()
    layout = HotkeyLayout(actual_config.layout_name, actual_bindings)
    source = LiveSessionSourceV1.from_session(
        session,
        actual_bindings,
        layout_name=actual_config.layout_name,
    )
    recovery = InteractiveRecoveryCoordinatorV1(actual_paths)
    offer = recovery.inspect(source)

    def wrapped(screen: curses.window) -> None:
        selected = recovery_action or _choose_recovery_action(screen, offer)
        if selected not in offer.actions:
            raise RuntimeError("selected recovery action was not offered")
        if selected.value == "ABANDON":
            recovery.abandon(
                source=source,
                simulation_time_us=session.simulation_time_us,
            )
            return
        if selected.value == "CONTINUE_EXACT":
            recovery.continue_exact(
                session=session,
                source=source,
                bindings=actual_bindings,
            )
        elif selected.value == "REPLAY_SAFE":
            recovery.select_safe_replay(
                session=session,
                source=source,
                layout=layout,
            )
        else:
            recovery.start_new(
                session=session,
                source=source,
                layout=layout,
            )
        _run(
            screen,
            session,
            actual_bindings,
            actual_config,
            layout=layout,
        )

    curses.wrapper(wrapped)


def _run(
    screen: curses.window,
    session: LiveMarketSession,
    bindings: BindingMap,
    config: TerminalUiConfig,
    *,
    layout: HotkeyLayout,
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    frame_seconds = config.frame_milliseconds / 1_000.0
    next_tick = time.monotonic() + frame_seconds
    should_quit = False
    frames_since_checkpoint = 0

    while not should_quit:
        now = time.monotonic()
        if now >= next_tick:
            elapsed_ticks = int((now - next_tick) // frame_seconds) + 1
            advanced_frames = 0
            for _ in range(elapsed_ticks):
                before_time_us = session.simulation_time_us
                session.advance_by(config.simulation_step_us)
                if session.simulation_time_us != before_time_us:
                    advanced_frames += 1
            next_tick += elapsed_ticks * frame_seconds
            frames_since_checkpoint += advanced_frames
            if frames_since_checkpoint >= config.recovery_checkpoint_frames:
                session.commit_recovery_checkpoint(layout, auto_start=True)
                frames_since_checkpoint = 0

        snapshot = session.snapshot()
        client_transaction_id = session.begin_recovery_client_message(snapshot)
        _draw(screen, snapshot, bindings, config)
        session.acknowledge_recovery_client_message(client_transaction_id)
        timeout_ms = max(0, round((next_tick - time.monotonic()) * 1_000))
        screen.timeout(min(config.frame_milliseconds, timeout_ms))
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        input_key = (
            key
            if isinstance(key, str)
            else curses.keyname(key).decode("ascii", errors="replace")
        )
        record = session.handle_input(input_key, bindings)
        session.commit_recovery_checkpoint(layout, auto_start=True)
        frames_since_checkpoint = 0
        if record.resolved_command == SessionCommand.QUIT.value:
            should_quit = True
    session.close_recovery_journal()


def _choose_recovery_action(
    screen: curses.window,
    offer: RecoveryOfferV1,
) -> RecoveryActionV1:
    from kirby2.release.recovery import (
        RecoveryActionV1,
        RecoveryDispositionV1,
    )

    if offer.disposition is RecoveryDispositionV1.NO_RECOVERY:
        return RecoveryActionV1.START_NEW
    screen.erase()
    height, width = screen.getmaxyx()
    lines = [
        "KIRBY2 RECOVERY",
        offer.detail,
        f"Reason: {offer.reason_code.value}",
        "",
    ]
    choices: dict[str, RecoveryActionV1] = {}
    if RecoveryActionV1.CONTINUE_EXACT in offer.actions:
        lines.append("[E] Continue exactly from the verified durable cut")
        choices["e"] = RecoveryActionV1.CONTINUE_EXACT
    if RecoveryActionV1.REPLAY_SAFE in offer.actions:
        lines.append("[R] Start a safe replay from the beginning")
        choices["r"] = RecoveryActionV1.REPLAY_SAFE
    if RecoveryActionV1.ABANDON in offer.actions:
        lines.append("[A] Abandon this unfinished session and exit")
        choices["a"] = RecoveryActionV1.ABANDON
    lines.append("")
    lines.append("Kirby2 will not infer whether an unacknowledged action was applied.")
    for row, line in enumerate(lines[:height]):
        _safe_write(screen, row, 0, line, width)
    screen.refresh()
    screen.timeout(-1)
    while True:
        key = screen.get_wch()
        if isinstance(key, str) and key.casefold() in choices:
            return choices[key.casefold()]


def _draw(
    screen: curses.window,
    snapshot: SessionSnapshot,
    bindings: BindingMap,
    config: TerminalUiConfig,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    if width < MINIMUM_WIDTH or height < MINIMUM_HEIGHT:
        message = (
            f"Kirby2 needs at least {MINIMUM_WIDTH}x{MINIMUM_HEIGHT}; "
            f"current terminal is {width}x{height}. Resize or press q to quit."
        )
        _safe_write(screen, 0, 0, message, width)
        screen.refresh()
        return
    lines = render_terminal_frame(snapshot, bindings, config, width)
    for row, line in enumerate(lines[:height]):
        _safe_write(screen, row, 0, line, width)
    screen.refresh()


def _safe_write(
    screen: curses.window,
    row: int,
    column: int,
    value: str,
    width: int,
) -> None:
    if row < 0 or column >= width:
        return
    try:
        screen.addnstr(row, column, value, max(0, width - column - 1))
    except curses.error:
        pass


def _ladder_lines(
    snapshot: SessionSnapshot,
    levels: int,
    width: int,
) -> list[str]:
    lines = ["LEVEL 2", "SIDE       PRICE       AGG    PLAYER   Q-AHEAD"]
    selected_asks = tuple(reversed(snapshot.asks[:levels]))
    selected_bids = snapshot.bids[:levels]
    for level in selected_asks:
        lines.append(_level_line("ASK", level))
    for _ in range(levels - len(selected_asks)):
        lines.append("ASK   (no displayed liquidity)")
    lines.append("-------------------------- SPREAD --------------------------"[:width])
    for level in selected_bids:
        lines.append(_level_line("BID", level))
    for _ in range(levels - len(selected_bids)):
        lines.append("BID   (no displayed liquidity)")
    return lines


def _level_line(side: str, level: LevelView) -> str:
    player = "-" if level.player_quantity == 0 else str(level.player_quantity)
    queue_ahead = (
        "-" if level.queue_ahead_quantity is None else str(level.queue_ahead_quantity)
    )
    return (
        f"{side:<4} {level.price:>11} {level.aggregate_quantity:>9} "
        f"{player:>9} {queue_ahead:>9}"
    )


def _tape_lines(
    snapshot: SessionSnapshot,
    rows: int,
    width: int,
) -> list[str]:
    lines = ["TIME & SALES", "TIME          PRICE       QTY  AGGR"]
    recent = tuple(reversed(snapshot.tape[-rows:]))
    for trade in recent:
        lines.append(
            f"{_market_time(trade.simulation_time_us)}  {trade.price:>11} "
            f"{trade.quantity:>8}  {trade.aggressor_side.value.upper()}"[:width]
        )
    while len(lines) < rows + 2:
        lines.append("")
    return lines


def _working_order_lines(
    snapshot: SessionSnapshot,
    rows: int,
    width: int,
) -> list[str]:
    lines = ["CURRENT WORKING ORDERS", "ID                 SIDE       PRICE       REM    FILLED   Q-AHEAD"]
    for order in snapshot.working_orders[:rows]:
        lines.append(
            f"{order.order_id:<18} {order.side.value.upper():<5} "
            f"{order.price:>11} {order.remaining_quantity:>9} "
            f"{order.filled_quantity:>9} {order.queue_ahead_quantity:>9}"[:width]
        )
    if not snapshot.working_orders:
        lines.append("(none)")
    elif len(snapshot.working_orders) > rows:
        lines.append(f"... {len(snapshot.working_orders) - rows} more")
    return lines


def _legend_lines(bindings: BindingMap, width: int) -> list[str]:
    prefix = "KEYBOARD  "
    continuation = " " * len(prefix)
    lines: list[str] = []
    current = prefix
    for binding in bindings.bindings:
        item = f"[{binding.display_key}] {binding.label}"
        separator = "" if current in {prefix, continuation} else "   "
        if len(current) + len(separator) + len(item) > width:
            lines.append(current.rstrip())
            current = continuation + item
        else:
            current += separator + item
    lines.append(current.rstrip())
    return lines


def _market_time(simulation_time_us: int) -> str:
    total_milliseconds = 9 * 60 * 60 * 1_000 + 30 * 60 * 1_000
    total_milliseconds += simulation_time_us // 1_000
    hours, remainder = divmod(total_milliseconds, 60 * 60 * 1_000)
    minutes, remainder = divmod(remainder, 60 * 1_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours % 24:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _elapsed_time(duration_us: int) -> str:
    total_seconds = duration_us // 1_000_000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"
