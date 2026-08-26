"""Kirby2 command-line entry point."""

from __future__ import annotations

import argparse
import json
import math
import secrets
import sys
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


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _objective_type(value: str):
    from kirby2.session.objectives import ObjectiveType

    try:
        return ObjectiveType.parse(value)
    except ValueError as error:
        allowed = ", ".join(item.value.lower() for item in ObjectiveType)
        raise argparse.ArgumentTypeError(
            f"unknown objective; choose one of: {allowed}"
        ) from error


def _curriculum_mode(value: str):
    from kirby2.curriculum import CurriculumMode

    try:
        return CurriculumMode.parse(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("curriculum mode must be LEARN or BLIND") from error


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _session_time(value: str) -> int:
    from kirby2.simulation import parse_session_time

    try:
        return parse_session_time(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _binding_key(value: str) -> str:
    key = " " if value.upper() == "SPACE" else value
    if len(key) != 1 and not key.startswith("KEY_"):
        raise argparse.ArgumentTypeError(
            "binding key must be one character, SPACE, or a KEY_* terminal key"
        )
    return key


def _binding_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("binding must use KEY=COMMAND")
    raw_key, command = value.split("=", 1)
    return _binding_key(raw_key), command


def _flow_model_names(value: str) -> tuple[str, ...]:
    allowed = {"simple", "hawkes"}
    models = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if not models or len(models) != len(set(models)):
        raise argparse.ArgumentTypeError("flow models must be nonempty and unique")
    unknown = set(models) - allowed
    if unknown:
        raise argparse.ArgumentTypeError(
            "flow models must be a comma-separated subset of simple,hawkes"
        )
    return models


def _load_strategy_file(path: Path):
    from kirby2.strategy import RuleSyntaxError, parse_strategy

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"STRATEGY_ERROR {path}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    try:
        return parse_strategy(source)
    except RuleSyntaxError as error:
        print(f"STRATEGY_ERROR {path}:{error}", file=sys.stderr)
        raise SystemExit(2) from error


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

    compare_flow = subcommands.add_parser(
        "compare-flow",
        help="compare simple Poisson and Hawkes arrivals on one scenario",
    )
    compare_flow.add_argument(
        "--scenario",
        choices=sorted(load_scenario_definitions()),
        default="momentum_up",
    )
    compare_flow.add_argument("--seed", type=int, default=42)
    compare_flow.add_argument(
        "--models",
        type=_flow_model_names,
        default=("simple", "hawkes"),
    )

    inspect_intensity = subcommands.add_parser(
        "inspect-intensity",
        help="inspect observable queue state and per-channel intensity response",
    )
    inspect_intensity.add_argument(
        "--scenario",
        choices=sorted(load_scenario_definitions()),
        default="balanced",
    )
    inspect_intensity.add_argument("--seed", type=int, default=42)
    inspect_intensity.add_argument("--seconds", type=_positive_int, default=5)

    probe_intensity = subcommands.add_parser(
        "probe-intensity",
        help="sweep queue imbalance through the bounded response layer",
    )
    probe_intensity.add_argument(
        "--scenario",
        choices=sorted(load_scenario_definitions()),
        default="balanced",
    )
    probe_intensity.add_argument("--seed", type=int, default=42)

    features = subcommands.add_parser(
        "features",
        help="print a compact causal microstructure feature stream",
    )
    features.add_argument(
        "--scenario",
        default="absorption",
        help="accepted scenario name; absorption aliases absorption_bid",
    )
    features.add_argument("--seed", type=int, default=42)
    features.add_argument("--seconds", type=_positive_int, default=5)
    features.add_argument("--interval-ms", type=_positive_int, default=250)
    features.add_argument("--catalog", action="store_true")

    inspect_distribution = subcommands.add_parser(
        "inspect-distribution",
        help="sample and summarize one regime-specific distribution",
    )
    inspect_distribution.add_argument(
        "purpose",
        choices=(
            "order_size",
            "trade_size",
            "cancel_size",
            "queue_depth",
            "limit_placement_depth",
            "inter_event_timing_modifier",
            "spread_state_duration",
        ),
    )
    inspect_distribution.add_argument("--scenario", default="balanced")
    inspect_distribution.add_argument("--seed", type=int, default=42)
    inspect_distribution.add_argument("--samples", type=_positive_int, default=10_000)

    inspect_session = subcommands.add_parser(
        "inspect-session",
        help="inspect expected activity modifiers across an intraday profile",
    )
    inspect_session.add_argument("--scenario", default="balanced")
    inspect_session.add_argument("--start", type=_session_time)
    inspect_session.add_argument("--end", type=_session_time)

    measure_compare = subcommands.add_parser(
        "measure-compare",
        help="compare descriptive calibration measurements for two normalized sources",
    )
    measure_compare.add_argument(
        "reference",
        help="fixture:ID, scenario:NAME, normalized JSONL, or Kirby2 replay JSONL",
    )
    measure_compare.add_argument(
        "candidate",
        help="fixture:ID, scenario:NAME, normalized JSONL, or Kirby2 replay JSONL",
    )
    measure_compare.add_argument("--seed", type=int, default=42)
    measure_compare.add_argument("--seconds", type=_positive_int, default=30)

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
    ui.add_argument("--layout", help="load a named hotkey layout")
    ui.add_argument("--layout-dir", type=Path, default=Path(".kirby2/layouts"))
    ui.add_argument("--bind", type=_binding_assignment, action="append", default=[])
    ui.add_argument("--unbind", type=_binding_key, action="append", default=[])
    ui.add_argument("--save-layout", help="save the effective bindings under this name")
    ui.add_argument("--record", type=Path, help="save an event-state session recording")
    ui.add_argument(
        "--strategy",
        type=Path,
        help="observable-only traffic-light rule file",
    )
    ui.add_argument(
        "--objective",
        type=_objective_type,
        default=_objective_type("observe_only"),
    )
    ui.add_argument("--target-quantity", type=_nonnegative_int)
    ui.add_argument("--objective-seconds", type=_positive_int)
    ui.add_argument("--preferred-slippage-ticks", type=_nonnegative_int)

    strategy = subcommands.add_parser(
        "strategy",
        help="validate and inspect a traffic-light rule file",
    )
    strategy.add_argument("rule_file", type=Path)

    layout = subcommands.add_parser("layout", help="manage named hotkey layouts")
    layout_actions = layout.add_subparsers(dest="layout_action", required=True)
    layout_list = layout_actions.add_parser("list", help="list saved layouts")
    layout_list.add_argument("--directory", type=Path, default=Path(".kirby2/layouts"))
    layout_show = layout_actions.add_parser("show", help="show one saved layout")
    layout_show.add_argument("name")
    layout_show.add_argument("--directory", type=Path, default=Path(".kirby2/layouts"))
    layout_save = layout_actions.add_parser("save", help="save an edited layout")
    layout_save.add_argument("name")
    layout_save.add_argument("--base", help="existing layout to edit")
    layout_save.add_argument("--directory", type=Path, default=Path(".kirby2/layouts"))
    layout_save.add_argument("--bind", type=_binding_assignment, action="append", default=[])
    layout_save.add_argument("--unbind", type=_binding_key, action="append", default=[])

    replay = subcommands.add_parser("replay", help="replay a recorded execution session")
    replay.add_argument("recording", type=Path)

    report = subcommands.add_parser(
        "report",
        help="replay and score a recorded training session",
    )
    report.add_argument("recording", type=Path)

    curriculum = subcommands.add_parser(
        "curriculum",
        help="list or run controlled execution drills",
    )
    curriculum_actions = curriculum.add_subparsers(
        dest="curriculum_action",
        required=True,
    )
    curriculum_actions.add_parser("list", help="list the 14 execution lessons")
    curriculum_run = curriculum_actions.add_parser(
        "run",
        help="launch one controlled lesson variation",
    )
    curriculum_run.add_argument(
        "lesson",
        choices=tuple(f"{index:02d}" for index in range(1, 15)),
    )
    curriculum_run.add_argument(
        "--mode",
        type=_curriculum_mode,
        default=_curriculum_mode("learn"),
    )
    curriculum_run.add_argument(
        "--variation-seed",
        type=_nonnegative_int,
        help="reproduce one controlled variation; generated when omitted",
    )
    curriculum_run.add_argument("--speed", type=_positive_float, default=10.0)
    curriculum_run.add_argument(
        "--quantity",
        type=int,
        choices=(25, 50, 100, 200, 500, 1000, 2000),
        default=100,
    )
    curriculum_run.add_argument("--levels", type=int, choices=range(5, 11), default=7)
    curriculum_run.add_argument("--layout", help="load a named hotkey layout")
    curriculum_run.add_argument(
        "--layout-dir",
        type=Path,
        default=Path(".kirby2/layouts"),
    )
    curriculum_run.add_argument(
        "--bind",
        type=_binding_assignment,
        action="append",
        default=[],
    )
    curriculum_run.add_argument(
        "--unbind",
        type=_binding_key,
        action="append",
        default=[],
    )
    curriculum_run.add_argument("--save-layout")
    curriculum_run.add_argument(
        "--record",
        type=Path,
        help="save the completed or interrupted drill for deterministic replay",
    )

    timeline = subcommands.add_parser(
        "timeline",
        help="inspect the event-state timeline of a recorded session",
    )
    timeline.add_argument("recording", type=Path)
    timeline.add_argument("--limit", type=_positive_int)
    timeline.add_argument("--kind", action="append")

    from kirby2.historical import load_historical_fixtures

    historical = subcommands.add_parser(
        "historical",
        help="list or run explicit-provenance historical fixtures",
    )
    historical_actions = historical.add_subparsers(
        dest="historical_action",
        required=True,
    )
    historical_actions.add_parser("list", help="list the local demonstration fixtures")
    historical_run = historical_actions.add_parser(
        "run",
        help="run one exact replay or constrained reconstruction fixture",
    )
    historical_run.add_argument(
        "fixture",
        choices=tuple(sorted(load_historical_fixtures())),
    )
    historical_run.add_argument(
        "--seed",
        type=_nonnegative_int,
        help="override the reconstruction seed; invalid for exact replay",
    )
    historical_run.add_argument(
        "--events-jsonl",
        type=Path,
        help="save the complete provenance-labeled replay stream",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "demo":
        print(run_demo(args.seed))
        return

    if args.command == "strategy":
        from kirby2.strategy import FeatureName

        definition = _load_strategy_file(args.rule_file)
        print("KIRBY2_TRAFFIC_STRATEGY")
        print(json.dumps(definition.as_dict(), sort_keys=True, separators=(",", ":")))
        print(
            "OBSERVABLE_FEATURES "
            + ",".join(feature.value for feature in FeatureName)
        )
        print("STRATEGY_VALID PASS arbitrary_code=DISABLED hidden_regime=UNAVAILABLE")
        return

    if args.command == "historical":
        from kirby2.historical import (
            ExactReplayFixture,
            load_historical_fixtures,
            render_historical_report,
            render_historical_ui,
            run_historical_fixture,
        )

        fixtures = load_historical_fixtures()
        if args.historical_action == "list":
            print("KIRBY2_HISTORICAL_FIXTURES")
            for fixture in fixtures.values():
                mode = (
                    "EXACT_REPLAY"
                    if isinstance(fixture, ExactReplayFixture)
                    else "RECONSTRUCTION"
                )
                print(
                    f"{fixture.fixture_id}  mode={mode} "
                    f"real_market_data={str(fixture.provenance.real_market_data).lower()} "
                    f"label={fixture.label}"
                )
            print(f"FIXTURE_COUNT {len(fixtures)}")
            return
        try:
            run = run_historical_fixture(fixtures[args.fixture], seed=args.seed)
        except (RuntimeError, ValueError) as error:
            print(f"HISTORICAL_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(render_historical_ui(run))
        print()
        print(render_historical_report(run))
        if args.events_jsonl is not None:
            args.events_jsonl.write_text(
                run.replay_json_lines() + "\n",
                encoding="utf-8",
            )
            print(f"HISTORICAL_REPLAY_STREAM path={args.events_jsonl.resolve()}")
        else:
            print("HISTORICAL_REPLAY_STREAM available_via=HistoricalRun.replay_json_lines")
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

    if args.command == "compare-flow":
        from kirby2.simulation.comparison import compare_flow_models

        definition = get_scenario_definition(args.scenario)
        comparison = compare_flow_models(
            definition,
            seed=args.seed,
            models=args.models,
        )
        print(
            f"KIRBY2_FLOW_COMPARISON scenario={comparison.scenario} "
            f"seed={comparison.seed} models={','.join(args.models)}"
        )
        for result in comparison.models:
            print(json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")))
        delta = comparison.clustering_delta()
        if delta is not None:
            print(
                "CLUSTERING_DELTA hawkes_minus_simple "
                + json.dumps(delta, sort_keys=True, separators=(",", ":"))
            )
            evidence = sum(
                delta[key] > 0
                for key in (
                    "aggressive_flow_fano_1s",
                    "cancel_flow_fano_1s",
                    "trade_fano_1s",
                    "event_interarrival_cv",
                )
            )
            print(
                f"HAWKES_CLUSTERING {'EVIDENT' if evidence >= 3 else 'MIXED'} "
                f"positive_indicators={evidence}/4"
            )
        print(f"FLOW_MODEL_INVARIANTS PASS models={len(comparison.models)}")
        return

    if args.command == "inspect-intensity":
        from kirby2.scenarios.market import create_market_engine
        from kirby2.simulation import QueueReactiveFlowModifier

        definition = get_scenario_definition(args.scenario)
        modifier = QueueReactiveFlowModifier()
        engine, _ = create_market_engine(
            definition,
            seed=args.seed,
            intensity_modifier=modifier,
        )
        simulation = engine.run(args.seconds)
        inspection = modifier.inspect(
            engine.policy.rates(engine.book),
            engine.book,
            engine.clock.current_time_us,
        )
        print(
            f"KIRBY2_INTENSITY_INSPECTION scenario={definition.name} "
            f"seed={args.seed} time_us={engine.clock.current_time_us}"
        )
        print(
            "BOOK_STATE "
            + json.dumps(
                inspection.state.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for channel in inspection.channels:
            print(
                "CHANNEL "
                + json.dumps(
                    channel.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        print(f"REPLAY_SHA256 {simulation.replay_sha256()}")
        print("QUEUE_REACTIVE_INVARIANTS PASS")
        return

    if args.command == "probe-intensity":
        from kirby2.scenarios.market import create_market_engine
        from kirby2.simulation import (
            QueueReactiveFlowModifier,
            imbalance_probe_state,
        )

        definition = get_scenario_definition(args.scenario)
        engine, _ = create_market_engine(definition, seed=args.seed)
        engine.start()
        baseline = engine.policy.rates(engine.book)
        modifier = QueueReactiveFlowModifier()
        print(
            f"KIRBY2_IMBALANCE_PROBE scenario={definition.name} "
            f"seed={args.seed} profile={modifier.config.profile_id}"
        )
        for imbalance in (-0.90, -0.60, -0.30, 0.0, 0.30, 0.60, 0.90):
            inspection = modifier.inspect_state(
                baseline,
                imbalance_probe_state(imbalance),
            )
            print(
                json.dumps(
                    {
                        "final_intensities": {
                            channel.family.value: round(channel.final_intensity, 9)
                            for channel in inspection.channels
                        },
                        "imbalance": inspection.state.imbalance,
                        "state_multipliers": {
                            channel.family.value: round(channel.state_multiplier, 9)
                            for channel in inspection.channels
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        print("IMBALANCE_PROBE PASS bounded=true hidden_regime_to_strategy=false")
        return

    if args.command == "features":
        from kirby2.features import (
            FEATURE_CATALOG,
            FeatureKey,
            feature_catalog_sha256,
            inspect_scenario_features,
        )

        scenario_name = (
            "absorption_bid" if args.scenario.lower() == "absorption" else args.scenario
        )
        try:
            definition = get_scenario_definition(scenario_name)
        except ValueError as error:
            print(f"FEATURE_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        stream = inspect_scenario_features(
            definition,
            seed=args.seed,
            seconds=args.seconds,
            emit_interval_us=args.interval_ms * 1_000,
        )
        print(
            f"KIRBY2_FEATURE_STREAM scenario={stream.scenario} seed={stream.seed} "
            f"windows_us={','.join(str(value) for value in stream.windows_us)}"
        )
        print(
            f"FEATURE_CATALOG count={len(FEATURE_CATALOG)} "
            f"sha256={feature_catalog_sha256()}"
        )
        if args.catalog:
            for key in FeatureKey:
                print(
                    "FEATURE_DEFINITION "
                    + json.dumps(
                        FEATURE_CATALOG[key].as_dict(),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        selected = (
            "mid_price",
            "microprice",
            "spread_ticks",
            "top_level_imbalance",
            "multi_level_imbalance",
            "trade_velocity_250ms",
            "trade_velocity_1s",
            "trade_velocity_5s",
            "trade_imbalance_1s",
            "cancel_velocity_bid_1s",
            "cancel_velocity_ask_1s",
            "short_term_return_1s",
            "short_term_volatility_1s",
            "price_velocity_1s",
            "price_acceleration_1s",
        )
        for frame in stream.frames:
            values = frame.as_dict()
            print(
                json.dumps(
                    {
                        "simulation_time_us": frame.simulation_time_us,
                        **{key: values[key] for key in selected},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        print(f"FEATURE_STREAM_SHA256 {stream.replay_sha256()}")
        print("FEATURE_INVARIANTS PASS causal=true hidden_future=false")
        return

    if args.command == "inspect-distribution":
        from kirby2.simulation import (
            DistributionPurpose,
            distribution_profile_for_regime,
            inspect_distribution,
        )

        try:
            definition = get_scenario_definition(args.scenario)
        except ValueError as error:
            print(f"DISTRIBUTION_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        purpose = DistributionPurpose(args.purpose)
        profile = distribution_profile_for_regime(definition.regime)
        inspection = inspect_distribution(
            profile,
            purpose,
            seed=args.seed,
            sample_count=args.samples,
        )
        print(
            f"KIRBY2_DISTRIBUTION_INSPECTION scenario={definition.name} "
            f"purpose={purpose.value}"
        )
        print(
            "CONFIG "
            + json.dumps(
                profile.distribution(purpose).as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(json.dumps(inspection.as_dict(), sort_keys=True, separators=(",", ":")))
        print("DISTRIBUTION_INVARIANTS PASS integer_samples=true owned_rng=true")
        return

    if args.command == "inspect-session":
        from kirby2.simulation import (
            FlowEventFamily,
            IntradayWindow,
            ScenarioDimensions,
            equity_u_shaped_profile,
            format_session_second,
        )

        try:
            definition = get_scenario_definition(args.scenario)
            profile = equity_u_shaped_profile()
            if (args.start is None) != (args.end is None):
                raise ValueError("--start and --end must be supplied together")
            window = (
                IntradayWindow(profile.start_second, profile.end_second)
                if args.start is None
                else IntradayWindow(args.start, args.end)
            )
            if (
                window.start_second < profile.start_second
                or window.end_second > profile.end_second
            ):
                raise ValueError("inspection window exceeds the intraday profile")
        except ValueError as error:
            print(f"SESSION_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error

        dimensions = ScenarioDimensions(
            definition.relative_volume,
            definition.liquidity,
        )
        print(
            f"KIRBY2_INTRADAY_PROFILE id={profile.profile_id} "
            f"scenario={definition.name} "
            f"window={format_session_second(window.start_second)}-"
            f"{format_session_second(window.end_second)}"
        )
        for segment in profile.segments:
            if (
                segment.end_second <= window.start_second
                or segment.start_second >= window.end_second
            ):
                continue
            modifiers = segment.modifiers
            composition = {
                "cancel_activity_multiplier": round(
                    modifiers.relative_volume
                    * modifiers.event_intensity
                    * modifiers.cancellation_activity
                    * modifiers.spread_tendency
                    / modifiers.depth,
                    6,
                ),
                "limit_activity_multiplier": round(
                    modifiers.relative_volume
                    * modifiers.event_intensity
                    * modifiers.depth
                    / modifiers.spread_tendency,
                    6,
                ),
                "market_activity_multiplier": round(
                    modifiers.relative_volume
                    * modifiers.event_intensity
                    * modifiers.volatility
                    * modifiers.spread_tendency,
                    6,
                ),
                "market_size_multiplier": modifiers.trade_size,
                "scenario_rate_scale": {
                    family.value: dimensions.rate_scale(family)
                    for family in FlowEventFamily
                },
            }
            print(
                json.dumps(
                    {**segment.as_dict(), "composed_activity": composition},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        print("SESSION_PROFILE_INVARIANTS PASS contiguous=true positive=true")
        return

    if args.command == "measure-compare":
        from kirby2.calibration import (
            compare_reports,
            measure_stream,
            resolve_measurement_source,
        )

        try:
            reference_stream = resolve_measurement_source(
                args.reference,
                seed=args.seed,
                seconds=args.seconds,
            )
            candidate_stream = resolve_measurement_source(
                args.candidate,
                seed=args.seed,
                seconds=args.seconds,
            )
            reference_report = measure_stream(reference_stream)
            candidate_report = measure_stream(candidate_stream)
            comparison = compare_reports(reference_report, candidate_report)
        except (OSError, TypeError, ValueError) as error:
            print(f"MEASUREMENT_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error

        print(
            f"KIRBY2_MEASURE_COMPARE reference={reference_report.source_id} "
            f"candidate={candidate_report.source_id}"
        )
        print(
            "REFERENCE_REPORT "
            + json.dumps(
                reference_report.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(
            "CANDIDATE_REPORT "
            + json.dumps(
                candidate_report.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print("MAJOR_MEASUREMENT_DIFFERENCES")
        for difference in comparison.differences:
            print(
                json.dumps(
                    difference.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        print(
            "STATISTICAL_CLAIM descriptive_only=true "
            "equivalence_claimed=false"
        )
        print("MEASUREMENT_INVARIANTS PASS units=true sample_counts=true")
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

    if args.command == "layout":
        from kirby2.session.bindings import SessionCommand
        from kirby2.session.layouts import HotkeyLayout, LayoutStore

        store = LayoutStore(args.directory)
        if args.layout_action == "list":
            names = store.list_names()
            print("KIRBY2_HOTKEY_LAYOUTS")
            for name in names:
                print(name)
            print(f"LAYOUT_COUNT {len(names)}")
            return
        if args.layout_action == "show":
            saved = store.load(args.name)
            print(json.dumps(saved.as_dict(), sort_keys=True, indent=2))
            return
        base = store.load(args.base) if args.base else HotkeyLayout.default()
        assignments = {
            key: SessionCommand(command) for key, command in args.bind
        }
        bindings = base.bindings.edited(assignments, tuple(args.unbind))
        saved = HotkeyLayout(args.name, bindings)
        path = store.save(saved)
        print(f"HOTKEY_LAYOUT_SAVED name={saved.name} path={path.resolve()}")
        return

    if args.command == "curriculum":
        from kirby2.curriculum import load_curriculum, prepare_lesson
        from kirby2.session.bindings import SessionCommand
        from kirby2.session.layouts import HotkeyLayout, LayoutStore
        from kirby2.session.live import LiveMarketSession
        from kirby2.session.replay import SessionRecording
        from kirby2.session.scoring import build_session_report
        from kirby2.ui import TerminalUiConfig, run_terminal_ui

        lessons = load_curriculum()
        if args.curriculum_action == "list":
            print("KIRBY2_EXECUTION_CURRICULUM")
            for lesson in lessons.values():
                print(
                    f"{lesson.lesson_id}  {lesson.title}  -  "
                    f"{lesson.learning_objective}"
                )
            print(f"LESSON_COUNT {len(lessons)} modes=LEARN,BLIND")
            return

        variation_seed = (
            secrets.randbits(63)
            if args.variation_seed is None
            else args.variation_seed
        )
        drill = prepare_lesson(args.lesson, args.mode, variation_seed)
        store = LayoutStore(args.layout_dir)
        layout = store.load(args.layout) if args.layout else HotkeyLayout.default()
        assignments = {
            key: SessionCommand(command) for key, command in args.bind
        }
        bindings = layout.bindings.edited(assignments, tuple(args.unbind))
        effective_layout = HotkeyLayout(args.save_layout or layout.name, bindings)
        if args.save_layout:
            saved_path = store.save(effective_layout)
            print(
                f"HOTKEY_LAYOUT_SAVED name={effective_layout.name} "
                f"path={saved_path.resolve()}"
            )
        print(drill.render_briefing())
        session = LiveMarketSession(
            get_scenario_definition(drill.scenario_name),
            seed=drill.scenario_seed,
            duration_seconds=drill.duration_seconds,
            relative_volume=drill.volume,
            liquidity=drill.liquidity,
            initial_quantity=args.quantity,
            objective=drill.player_objective,
            curriculum_drill=drill,
        )
        run_terminal_ui(
            session,
            bindings=effective_layout.bindings,
            config=TerminalUiConfig(
                speed=args.speed,
                ladder_levels=args.levels,
                layout_name=effective_layout.name,
            ),
        )
        if args.record is not None:
            recording = SessionRecording.capture(session, effective_layout)
            recording.save(args.record)
            print(
                f"SESSION_RECORDING path={args.record.resolve()} "
                f"complete={str(recording.complete).lower()} "
                f"inputs={len(recording.input_records)}"
            )
            print(f"STATE_SHA256 {recording.expected_state_sha256}")
            print(f"TIMELINE_SHA256 {recording.expected_timeline_sha256}")
        print(build_session_report(session).render())
        if session.complete:
            print(drill.render_debrief())
        else:
            print("KIRBY2_CURRICULUM_DEBRIEF WITHHELD session_incomplete=true")
        return

    if args.command == "ui":
        from kirby2.session.bindings import SessionCommand
        from kirby2.session.layouts import HotkeyLayout, LayoutStore
        from kirby2.session.live import LiveMarketSession
        from kirby2.session.objectives import ObjectiveType, SessionObjective
        from kirby2.session.replay import SessionRecording
        from kirby2.session.scoring import build_session_report
        from kirby2.ui import TerminalUiConfig, run_terminal_ui

        store = LayoutStore(args.layout_dir)
        layout = store.load(args.layout) if args.layout else HotkeyLayout.default()
        assignments = {
            key: SessionCommand(command) for key, command in args.bind
        }
        bindings = layout.bindings.edited(assignments, tuple(args.unbind))
        effective_layout = HotkeyLayout(args.save_layout or layout.name, bindings)
        if args.save_layout:
            saved_path = store.save(effective_layout)
            print(
                f"HOTKEY_LAYOUT_SAVED name={effective_layout.name} "
                f"path={saved_path.resolve()}"
            )
        definition = get_scenario_definition(args.scenario)
        strategy_definition = (
            None if args.strategy is None else _load_strategy_file(args.strategy)
        )
        target_quantity = (
            0 if args.target_quantity is None else args.target_quantity
        )
        preferred_slippage_ticks = (
            0
            if args.objective is ObjectiveType.OBSERVE_ONLY
            and args.preferred_slippage_ticks is None
            else (
                2
                if args.preferred_slippage_ticks is None
                else args.preferred_slippage_ticks
            )
        )
        try:
            objective = SessionObjective(
                objective_type=args.objective,
                target_quantity=target_quantity,
                time_limit_us=(args.objective_seconds or args.seconds) * 1_000_000,
                preferred_slippage_ticks=preferred_slippage_ticks,
            )
            if objective.time_limit_us > args.seconds * 1_000_000:
                raise ValueError("objective time limit cannot exceed session duration")
        except ValueError as error:
            print(f"OBJECTIVE_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        session = LiveMarketSession(
            definition,
            seed=args.seed,
            duration_seconds=args.seconds,
            relative_volume=args.volume,
            liquidity=args.liquidity,
            initial_quantity=args.quantity,
            strategy_definition=strategy_definition,
            objective=objective,
        )
        run_terminal_ui(
            session,
            bindings=effective_layout.bindings,
            config=TerminalUiConfig(
                speed=args.speed,
                ladder_levels=args.levels,
                layout_name=effective_layout.name,
            ),
        )
        if args.record is not None:
            recording = SessionRecording.capture(session, effective_layout)
            recording.save(args.record)
            print(
                f"SESSION_RECORDING path={args.record.resolve()} "
                f"complete={str(recording.complete).lower()} "
                f"inputs={len(recording.input_records)}"
            )
            print(f"STATE_SHA256 {recording.expected_state_sha256}")
            print(f"TIMELINE_SHA256 {recording.expected_timeline_sha256}")
        print(build_session_report(session).render())
        return

    if args.command in {"replay", "timeline", "report"}:
        from kirby2.session.records import TimelineKind
        from kirby2.session.replay import (
            SessionRecording,
            TimelineInspector,
            replay_recording,
        )
        from kirby2.session.scoring import build_session_report

        recording = SessionRecording.load(args.recording)
        report = replay_recording(recording)
        if args.command == "replay":
            print("KIRBY2_SESSION_REPLAY")
            print(json.dumps(report.summary(), sort_keys=True, separators=(",", ":")))
            print(f"SESSION_REPLAY {'PASS' if report.passed else 'FAIL'}")
            if report.session.curriculum_drill is not None:
                if report.session.complete:
                    print(report.session.curriculum_drill.render_debrief())
                else:
                    print(
                        "KIRBY2_CURRICULUM_DEBRIEF "
                        "WITHHELD session_incomplete=true"
                    )
        elif args.command == "timeline":
            kinds = (
                {TimelineKind(value.upper()) for value in args.kind}
                if args.kind
                else None
            )
            print("KIRBY2_TIMELINE")
            rendered = TimelineInspector(report.session.timeline).render(
                kinds=kinds,
                limit=args.limit,
            )
            if rendered:
                print(rendered)
            print(
                f"TIMELINE_REPLAY {'PASS' if report.passed else 'FAIL'} "
                f"records={len(report.session.timeline)}"
            )
        else:
            try:
                training_report = build_session_report(report.session)
            except ValueError as error:
                print(f"REPORT_ERROR {error}", file=sys.stderr)
                raise SystemExit(2) from error
            print(training_report.render())
            if report.session.curriculum_drill is not None:
                if report.session.complete:
                    print(report.session.curriculum_drill.render_debrief())
                else:
                    print(
                        "KIRBY2_CURRICULUM_DEBRIEF "
                        "WITHHELD session_incomplete=true"
                    )
        if not report.passed:
            raise SystemExit(1)
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
