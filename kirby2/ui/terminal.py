"""Keyboard-first terminal interface driven only by session snapshots."""

from __future__ import annotations

import curses
import math
import sys
import time
from dataclasses import dataclass

from kirby2.session.bindings import BindingMap, SessionCommand
from kirby2.session.live import LevelView, LiveMarketSession, SessionSnapshot


MINIMUM_WIDTH = 116
MINIMUM_HEIGHT = 34


@dataclass(frozen=True, slots=True)
class TerminalUiConfig:
    speed: float = 10.0
    frame_milliseconds: int = 50
    ladder_levels: int = 7
    tape_rows: int = 12
    working_order_rows: int = 5
    layout_name: str = "layout_default"

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

    @property
    def simulation_step_us(self) -> int:
        return max(1, round(self.frame_milliseconds * 1_000 * self.speed))


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
        f"KIRBY2  {snapshot.scenario_name}/{snapshot.regime}  seed={snapshot.seed}  "
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
) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("Kirby2 UI requires an interactive terminal")
    actual_bindings = bindings or BindingMap.default()
    actual_config = config or TerminalUiConfig()

    def wrapped(screen: curses.window) -> None:
        _run(screen, session, actual_bindings, actual_config)

    curses.wrapper(wrapped)


def _run(
    screen: curses.window,
    session: LiveMarketSession,
    bindings: BindingMap,
    config: TerminalUiConfig,
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    session.start()
    frame_seconds = config.frame_milliseconds / 1_000.0
    next_tick = time.monotonic() + frame_seconds
    should_quit = False

    while not should_quit:
        now = time.monotonic()
        if now >= next_tick:
            elapsed_ticks = int((now - next_tick) // frame_seconds) + 1
            for _ in range(elapsed_ticks):
                session.advance_by(config.simulation_step_us)
            next_tick += elapsed_ticks * frame_seconds

        _draw(screen, session.snapshot(), bindings, config)
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
        if record.resolved_command == SessionCommand.QUIT.value:
            should_quit = True


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
