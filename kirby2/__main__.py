"""Kirby2 command-line entry point."""

from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from kirby2.scenarios import (
    get_scenario_definition,
    load_scenario_definitions,
    run_demo,
    run_market_scenario,
    run_scenario_matrix,
)
from kirby2.simulation import (
    EventRates,
    LiquidityPreset,
    SimulationConfig,
    VolumePreset,
    run_simulation,
)


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("must be a decimal number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _volume_preset(value: str) -> VolumePreset:
    try:
        return VolumePreset.parse(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _liquidity_preset(value: str) -> LiquidityPreset:
    try:
        return LiquidityPreset.parse(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kirby2")
    subcommands = parser.add_subparsers(dest="command", required=True)
    demo = subcommands.add_parser("demo", help="run the deterministic exchange demonstration")
    demo.add_argument("--seed", type=int, default=42, help="explicit simulation RNG seed")

    defaults = EventRates()
    simulate = subcommands.add_parser("simulate", help="run seeded Poisson-style order flow")
    simulate.add_argument("--seed", type=int, default=42, help="explicit simulation RNG seed")
    simulate.add_argument("--seconds", type=int, default=60, help="simulated duration")
    simulate.add_argument("--tick-size", type=_decimal, default=Decimal("0.01"))
    simulate.add_argument("--initial-mid-ticks", type=int, default=10_000)
    simulate.add_argument("--initial-depth", type=int, default=8)
    simulate.add_argument("--intensity", type=float, default=1.0)
    simulate.add_argument("--limit-buy-rate", type=float, default=defaults.limit_buy_rate)
    simulate.add_argument("--limit-sell-rate", type=float, default=defaults.limit_sell_rate)
    simulate.add_argument("--market-buy-rate", type=float, default=defaults.market_buy_rate)
    simulate.add_argument("--market-sell-rate", type=float, default=defaults.market_sell_rate)
    simulate.add_argument("--cancel-bid-rate", type=float, default=defaults.cancel_bid_rate)
    simulate.add_argument("--cancel-ask-rate", type=float, default=defaults.cancel_ask_rate)
    simulate.add_argument(
        "--events-jsonl",
        type=Path,
        help="optional path for the complete replay stream",
    )

    scenario = subcommands.add_parser("scenario", help="run a deterministic market regime")
    scenario.add_argument(
        "name",
        choices=sorted(load_scenario_definitions()),
        help="accepted scenario definition",
    )
    scenario.add_argument("--seed", type=int, help="override the accepted scenario seed")
    scenario.add_argument("--seconds", type=int, help="override the scenario duration")
    scenario.add_argument(
        "--volume",
        type=_volume_preset,
        help="override the scenario relative-volume preset",
    )
    scenario.add_argument(
        "--liquidity",
        type=_liquidity_preset,
        help="override the scenario displayed-liquidity preset",
    )
    scenario.add_argument(
        "--events-jsonl",
        type=Path,
        help="optional path for the raw replay stream; regime labels are omitted",
    )

    subcommands.add_parser(
        "audit-scenarios",
        help="rerun accepted regime seeds, digests, invariants, and behavioral envelopes",
    )

    matrix = subcommands.add_parser(
        "matrix",
        help="inspect all volume and liquidity combinations for one regime scenario",
    )
    matrix.add_argument("name", choices=sorted(load_scenario_definitions()))
    matrix.add_argument("--seed", type=int, help="shared comparison seed")
    matrix.add_argument("--seconds", type=int, default=30, help="duration of each matrix cell")

    ui = subcommands.add_parser("ui", help="launch the minimal execution interface")
    ui.add_argument(
        "--scenario",
        choices=sorted(load_scenario_definitions()),
        default="balanced",
    )
    ui.add_argument("--seed", type=int, help="override the scenario seed")
    ui.add_argument("--seconds", type=_positive_int, default=300)
    ui.add_argument("--speed", type=_positive_float, default=10.0)
    ui.add_argument("--volume", type=_volume_preset)
    ui.add_argument("--liquidity", type=_liquidity_preset)
    ui.add_argument(
        "--quantity",
        type=int,
        choices=(25, 50, 100, 200, 500, 1000, 2000),
        default=100,
    )
    ui.add_argument("--levels", type=int, choices=range(5, 11), default=7)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "demo":
        print(run_demo(args.seed))
        return

    if args.command == "scenario":
        definition = get_scenario_definition(args.name)
        result = run_market_scenario(
            definition,
            seed=args.seed,
            seconds=args.seconds,
            relative_volume=args.volume,
            liquidity=args.liquidity,
        )
        print(
            f"KIRBY2_SCENARIO name={definition.name} "
            f"seed={result.seed} seconds={result.duration_seconds}"
        )
        print("SUMMARY")
        print(json.dumps(result.metrics(), sort_keys=True, separators=(",", ":")))
        if args.events_jsonl is not None:
            args.events_jsonl.write_text(result.replay_json_lines() + "\n", encoding="utf-8")
            print(f"RAW_REPLAY_STREAM path={args.events_jsonl.resolve()} regime_label=OMITTED")
        else:
            print("RAW_REPLAY_STREAM available_via=ScenarioRun.replay_json_lines regime_label=OMITTED")
        print("RUNTIME_INVARIANTS PASS")
        return

    if args.command == "matrix":
        definition = get_scenario_definition(args.name)
        matrix = run_scenario_matrix(
            definition,
            seed=args.seed,
            seconds=args.seconds,
        )
        print(
            f"KIRBY2_MATRIX name={definition.name} "
            f"seed={matrix.seed} seconds_per_cell={matrix.seconds}"
        )
        print(matrix.render())
        failures = [
            cell
            for cell in matrix.cells
            if cell.run.metrics()["invariant_status"] != "PASS"
        ]
        print(
            f"MATRIX_INVARIANTS {'FAIL' if failures else 'PASS'} "
            f"cells={len(matrix.cells)} failures={len(failures)}"
        )
        if failures:
            raise SystemExit(1)
        return

    if args.command == "ui":
        from kirby2.session.bindings import BindingMap
        from kirby2.session.live import LiveMarketSession
        from kirby2.ui import TerminalUiConfig, run_terminal_ui

        definition = get_scenario_definition(args.scenario)
        session = LiveMarketSession(
            definition,
            seed=args.seed,
            duration_seconds=args.seconds,
            relative_volume=args.volume,
            liquidity=args.liquidity,
            initial_quantity=args.quantity,
        )
        run_terminal_ui(
            session,
            bindings=BindingMap.default(),
            config=TerminalUiConfig(
                speed=args.speed,
                ladder_levels=args.levels,
            ),
        )
        return

    if args.command == "audit-scenarios":
        from kirby2.audit.scenarios import audit_accepted_scenarios

        reports = audit_accepted_scenarios()
        print("KIRBY2_SCENARIO_AUDIT")
        for report in reports:
            status = "PASS" if report.passed else "FAIL"
            payload = {
                "digest": report.digest,
                "failures": list(report.failures),
                "name": report.name,
                "status": status,
            }
            print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"SCENARIO_AUDIT {'FAIL' if failed else 'PASS'} "
            f"accepted={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    rates = EventRates(
        limit_buy_rate=args.limit_buy_rate,
        limit_sell_rate=args.limit_sell_rate,
        market_buy_rate=args.market_buy_rate,
        market_sell_rate=args.market_sell_rate,
        cancel_bid_rate=args.cancel_bid_rate,
        cancel_ask_rate=args.cancel_ask_rate,
    )
    config = SimulationConfig(
        tick_size=args.tick_size,
        initial_mid_ticks=args.initial_mid_ticks,
        initial_depth=args.initial_depth,
        event_intensity=args.intensity,
        rates=rates,
    )
    result = run_simulation(seed=args.seed, seconds=args.seconds, config=config)
    summary = result.summary()
    print(f"KIRBY2_SIMULATION seed={args.seed} seconds={args.seconds}")
    print("SUMMARY")
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    if args.events_jsonl is not None:
        args.events_jsonl.write_text(result.replay_json_lines() + "\n", encoding="utf-8")
        print(f"REPLAY_STREAM path={args.events_jsonl.resolve()}")
    else:
        print("REPLAY_STREAM available_via=SimulationResult.replay_json_lines")
    print("RUNTIME_INVARIANTS PASS")


if __name__ == "__main__":
    main()
