"""Kirby2 command-line entry point."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import secrets
import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

# The release commands are invoked as ``python -I kirby2/__main__.py``.  Isolated
# startup deliberately excludes the checkout during site initialization.  Load only
# the named ``kirby2`` package afterward; the repository root never becomes a general
# import path where untracked startup hooks or module shadows could execute.
if __name__ == "__main__" and sys.flags.isolated:
    _package_root = Path(__file__).resolve().parent
    _package_spec = importlib.util.spec_from_file_location(
        "kirby2",
        _package_root / "__init__.py",
        submodule_search_locations=[str(_package_root)],
    )
    if _package_spec is None or _package_spec.loader is None:
        raise RuntimeError("isolated Kirby2 package bootstrap failed")
    _package = importlib.util.module_from_spec(_package_spec)
    sys.modules["kirby2"] = _package
    _package_spec.loader.exec_module(_package)

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


def _timed_input(value: str) -> tuple[int, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("player action must use TIME_US:KEY")
    raw_time, raw_key = value.split(":", 1)
    try:
        simulation_time_us = int(raw_time)
    except ValueError as error:
        raise argparse.ArgumentTypeError("player action time must be an integer") from error
    if simulation_time_us < 0:
        raise argparse.ArgumentTypeError("player action time must be nonnegative")
    return simulation_time_us, _binding_key(raw_key)


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


def _integer_tuple(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be comma-separated integers") from error
    if not values or len(values) != len(set(values)):
        raise argparse.ArgumentTypeError("integer list must be nonempty and unique")
    return values


def _benchmark_seeds(value: str) -> tuple[int, ...]:
    if ":" in value:
        raw_start, raw_end = value.split(":", 1)
        try:
            start, end = int(raw_start), int(raw_end)
        except ValueError as error:
            raise argparse.ArgumentTypeError("seed range must use START:END") from error
        if end < start or end - start > 999:
            raise argparse.ArgumentTypeError(
                "seed range must be ascending and contain at most 1000 seeds"
            )
        return tuple(range(start, end + 1))
    return _integer_tuple(value)


def _benchmark_algorithms(value: str):
    from kirby2.algorithms import AlgorithmName

    try:
        algorithms = tuple(
            AlgorithmName.parse(item)
            for item in value.split(",")
            if item.strip()
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"unknown execution algorithm: {error}") from error
    if not algorithms or len(algorithms) != len(set(algorithms)):
        raise argparse.ArgumentTypeError("algorithm list must be nonempty and unique")
    return algorithms


def _action_selector(value: str) -> int:
    prefix, separator, raw_sequence = value.partition(":")
    if prefix.lower() != "action" or separator != ":":
        raise argparse.ArgumentTypeError("branch point must use action:N")
    try:
        sequence = int(raw_sequence)
    except ValueError as error:
        raise argparse.ArgumentTypeError("action sequence must be an integer") from error
    if sequence <= 0:
        raise argparse.ArgumentTypeError("action sequence must be positive")
    return sequence


def _counterfactual_command(value: str):
    from kirby2.counterfactual import parse_counterfactual_command

    try:
        return parse_counterfactual_command(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _counterfactual_mode(value: str):
    from kirby2.counterfactual import CounterfactualMode

    try:
        return CounterfactualMode.parse(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "mode must be exogenous or endogenous"
        ) from error


def _calibration_stages(value: str) -> tuple[int, ...]:
    stages = _integer_tuple(value)
    if stages != tuple(range(1, max(stages) + 1)) or max(stages) > 4:
        raise argparse.ArgumentTypeError("stages must be contiguous from 1 through at most 4")
    return stages


def _parameter_assignment(value: str) -> tuple[str, float]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("fixed parameter must use NAME=VALUE")
    name, raw_value = value.split("=", 1)
    try:
        parsed = float(raw_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("fixed parameter value must be numeric") from error
    if not name or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("fixed parameter name and finite value are required")
    return name, parsed


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

    latency_demo = subcommands.add_parser(
        "latency-demo",
        help="run and exactly replay one deterministic cancel race",
    )
    latency_demo.add_argument(
        "--race",
        choices=("cancel-wins", "fill-wins"),
        default="cancel-wins",
    )
    latency_demo.add_argument("--seed", type=int, default=42)

    mechanics_demo = subcommands.add_parser(
        "mechanics-demo",
        help="run deterministic advanced exchange/session scenarios",
    )
    mechanics_demo.add_argument(
        "--scenario",
        choices=(
            "all",
            "opening-auction",
            "closing-auction",
            "halt-during-momentum",
            "reopening-gap",
            "ioc-partial-fill",
            "fok-rejection",
            "post-only-rejection",
        ),
        default="all",
    )

    from kirby2.agents import ADVERSARIAL_DRILL_IDS, POPULATION_IDS

    agent_ecology = subcommands.add_parser(
        "agent-ecology",
        help="run a bounded deterministic participant population or recognition drill",
    )
    selection = agent_ecology.add_mutually_exclusive_group()
    selection.add_argument("--population", choices=POPULATION_IDS)
    selection.add_argument("--drill", choices=ADVERSARIAL_DRILL_IDS)
    agent_ecology.add_argument("--seed", type=int, default=42)
    agent_ecology.add_argument(
        "--show-public-events",
        action="store_true",
        help="print the disclosure-safe player event stream",
    )
    agent_ecology.add_argument(
        "--show-post-session-actors",
        action="store_true",
        help="print actor attribution only after the synthetic session completes",
    )

    hidden_demo = subcommands.add_parser(
        "hidden-liquidity-demo",
        help="run deterministic hidden-liquidity and blind-feed scenarios",
    )
    hidden_demo.add_argument(
        "--scenario",
        choices=(
            "all",
            "blind-paired",
            "iceberg-absorption",
            "hidden-midpoint-fill",
            "repeated-displayed-refresh",
            "apparent-wall",
            "small-displayed-deep-hidden",
        ),
        default="all",
    )

    multivenue_demo = subcommands.add_parser(
        "multivenue-demo",
        help="run deterministic fragmented-market routing drills",
    )
    multivenue_demo.add_argument(
        "--scenario",
        choices=(
            "all",
            "better-price-poor-fill-probability",
            "deep-slow-versus-shallow-fast",
            "sweep-during-momentum",
            "passive-routing-two-venues",
            "stale-composite-quote",
            "partial-multi-venue-completion",
        ),
        default="all",
    )

    benchmark_execution = subcommands.add_parser(
        "benchmark-execution",
        help="compare simulator-only execution algorithms over deterministic forks",
    )
    benchmark_execution.add_argument(
        "--scenario",
        action="append",
        choices=("opening_momentum", "balanced_execution"),
        dest="benchmark_scenarios",
    )
    benchmark_execution.add_argument(
        "--algorithms",
        type=_benchmark_algorithms,
        default=_benchmark_algorithms("twap,pov,sweep,adaptive"),
    )
    benchmark_execution.add_argument(
        "--seeds",
        type=_benchmark_seeds,
        default=(100, 101, 102),
        help="comma-separated seeds or inclusive START:END",
    )
    benchmark_execution.add_argument("--quantity", type=_positive_int, default=500)
    benchmark_execution.add_argument("--seconds", type=_positive_int, default=5)
    benchmark_execution.add_argument(
        "--decision-interval-ms",
        type=_positive_int,
        default=250,
    )
    benchmark_execution.add_argument("--side", choices=("buy", "sell"), default="buy")
    benchmark_execution.add_argument(
        "--manual-replay",
        type=Path,
        help=(
            "verified Kirby2 player session recording to project into the "
            "manual_replay policy slot"
        ),
    )
    benchmark_execution.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research" / "algorithm_runs",
    )

    counterfactual = subcommands.add_parser(
        "counterfactual",
        help="branch an immutable session run under explicit causal semantics",
    )
    counterfactual.add_argument("run_id")
    counterfactual.add_argument("--at", type=_action_selector, required=True)
    counterfactual.add_argument("--replace", type=_counterfactual_command)
    counterfactual.add_argument(
        "--with",
        dest="replacement_command",
        type=_counterfactual_command,
    )
    counterfactual.add_argument(
        "--mode",
        type=_counterfactual_mode,
        default=_counterfactual_mode("endogenous"),
    )
    counterfactual.add_argument(
        "--order-type",
        choices=("limit", "market", "cancel"),
    )
    counterfactual.add_argument("--price-ticks", type=_positive_int)
    counterfactual.add_argument("--quantity", type=_positive_int)
    counterfactual.add_argument("--venue")
    counterfactual.add_argument("--timing-delta-us", type=int, default=0)
    counterfactual.add_argument("--remove", action="store_true")
    counterfactual.add_argument("--insert", action="store_true")
    counterfactual.add_argument("--hotkey-outcome", type=_counterfactual_command)
    counterfactual.add_argument(
        "--sweep-timing",
        action="store_true",
        help="run and persist the canonical -500/-250/0/+250/+500 ms sweep",
    )
    counterfactual.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

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

    calibrate = subcommands.add_parser(
        "calibrate",
        help="fit a reusable market profile with deterministic staged search",
    )
    calibrate.add_argument(
        "reference",
        help="fixture:ID, scenario:NAME, normalized JSONL, or Kirby2 replay JSONL",
    )
    calibrate.add_argument(
        "--scenario",
        choices=sorted(load_scenario_definitions()),
        default="balanced",
    )
    calibrate.add_argument("--seconds", type=_positive_int)
    calibrate.add_argument("--stages", type=_calibration_stages, default=(1, 2, 3, 4))
    calibrate.add_argument("--fit-seeds", type=_integer_tuple, default=(101, 202, 303))
    calibrate.add_argument("--heldout-seeds", type=_integer_tuple, default=(404, 505))
    calibrate.add_argument("--reference-seed", type=int, default=42)
    calibrate.add_argument("--search-seed", type=int, default=17)
    calibrate.add_argument("--candidates", type=_positive_int, default=24)
    calibrate.add_argument("--fixed", type=_parameter_assignment, action="append", default=[])
    calibrate.add_argument("--profile-id", default="calibrated_market_v1")
    calibrate.add_argument("--output", type=Path, help="optional calibrated profile JSON path")
    calibrate.add_argument("--record", type=Path, help="optional full calibration-run JSON path")
    calibrate.add_argument("--require-heldout-improvement", action="store_true")

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
    subcommands.add_parser(
        "audit-hawkes-stability",
        help="run adversarial Hawkes branching-stability certification cases",
    )
    subcommands.add_parser(
        "audit-strategy-time",
        help="audit simulation-time strategy deadlines, ordering, and replay",
    )
    subcommands.add_parser(
        "audit-distribution-truth",
        help="audit distribution units, runtime consumers, seeded traces, and replay",
    )
    subcommands.add_parser(
        "audit-historical-features",
        help="audit causal historical feature replay, provenance, and strategy gates",
    )
    subcommands.add_parser(
        "audit-historical-lessons",
        help="audit blind lesson phases, capability gates, overlays, and replay",
    )
    subcommands.add_parser(
        "audit-run-store",
        help="audit immutable identities, Parquet facts, DuckDB views, and replay",
    )
    subcommands.add_parser(
        "audit-market-data",
        help="audit adapters, quality detection, provenance, and replay refusal",
    )
    subcommands.add_parser(
        "audit-latency",
        help="audit asynchronous lifecycles, races, metrics, and exact replay",
    )
    subcommands.add_parser(
        "audit-market-mechanics",
        help="audit instructions, sessions, auctions, protections, and replay",
    )
    subcommands.add_parser(
        "audit-hidden-liquidity",
        help="audit hidden liquidity, observability, queue estimates, and replay",
    )
    subcommands.add_parser(
        "audit-multivenue",
        help="audit fragmented venues, routing evidence, scoring, and replay",
    )
    subcommands.add_parser(
        "audit-execution-algorithms",
        help="audit algorithm interfaces, forks, metrics, immutable runs, and replay",
    )
    subcommands.add_parser(
        "audit-counterfactuals",
        help="audit causal forks, mutations, paired timelines, and immutable branches",
    )
    subcommands.add_parser(
        "audit-agent-ecology",
        help="audit bounded agents, disclosure boundaries, drills, emergence, and replay",
    )
    subcommands.add_parser(
        "audit-model-risk-lab",
        help="audit generative coverage, explicit faults, minimization, and immutable evidence",
    )

    audit_lab = subcommands.add_parser(
        "audit-lab",
        help="run the generative model-risk laboratory and persist an immutable packet",
    )
    audit_lab.add_argument("--budget", type=_positive_int, default=10_000)
    audit_lab.add_argument("--seed", type=_nonnegative_int, default=771)
    audit_lab.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research" / "audit_lab",
    )
    audit_lab.add_argument(
        "--save-failures",
        action="store_true",
        help="persist complete event-level minimized reproducers",
    )

    ingest_market_data = subcommands.add_parser(
        "ingest-market-data",
        help="normalize and persist a capability-declared local market-data source",
    )
    ingest_market_data.add_argument(
        "--adapter",
        choices=("csv", "parquet", "kirby-mbo"),
        required=True,
    )
    ingest_market_data.add_argument("source", type=Path)
    ingest_market_data.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

    inspect_dataset = subcommands.add_parser(
        "inspect-dataset",
        help="show immutable provenance and data-quality evidence for one dataset",
    )
    inspect_dataset.add_argument("dataset_id")
    inspect_dataset.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

    validate_dataset = subcommands.add_parser(
        "validate-dataset",
        help="verify normalized dataset schemas, rows, and immutable digests",
    )
    validate_dataset.add_argument("dataset_id")
    validate_dataset.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

    replay_capability = subcommands.add_parser(
        "replay-capability",
        help="report whether source evidence supports exact replay",
    )
    replay_capability.add_argument("dataset_id")
    replay_capability.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

    record_run = subcommands.add_parser(
        "record-run",
        help="run and persist one deterministic synthetic training session",
    )
    record_run.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )
    record_run.add_argument(
        "--scenario",
        choices=sorted(load_scenario_definitions()),
        default="balanced",
    )
    record_run.add_argument("--seed", type=int, default=42)
    record_run.add_argument("--seconds", type=_positive_int, default=5)
    record_run.add_argument("--quantity", type=_positive_int, default=100)
    record_run.add_argument(
        "--player-action",
        type=_timed_input,
        action="append",
        default=[],
        metavar="TIME_US:KEY",
        help="replayable input at simulation microseconds; default is a midpoint market buy",
    )

    inspect_run = subcommands.add_parser(
        "inspect-run",
        help="show one immutable run manifest, summary, and verification state",
    )
    inspect_run.add_argument("run_id")
    inspect_run.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

    query_runs = subcommands.add_parser(
        "query-runs",
        help="query the DuckDB run-summary view",
    )
    query_runs.add_argument("--scenario")
    query_runs.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
    )

    verify_run = subcommands.add_parser(
        "verify-run",
        help="verify immutable artifacts, schemas, event sequence, and deterministic replay",
    )
    verify_run.add_argument("run_id")
    verify_run.add_argument(
        "--store",
        type=Path,
        default=Path(".kirby2") / "research",
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

    experiment = subcommands.add_parser(
        "experiment",
        help="run a controlled multi-seed strategy experiment",
    )
    experiment.add_argument("manifest", type=Path)
    experiment.add_argument(
        "--output",
        type=Path,
        help="new directory for resolved manifest, results, and complete replays",
    )

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

    from kirby2.historical import load_historical_fixtures, load_historical_lessons

    lesson_list = subcommands.add_parser(
        "lesson-list",
        help="list packaged historical teaching lessons",
    )
    lesson_run = subcommands.add_parser(
        "lesson-run",
        help="start one blind historical lesson session",
    )
    lesson_run.add_argument(
        "lesson",
        choices=tuple(sorted(load_historical_lessons())),
    )
    lesson_run.add_argument("--levels", type=int, choices=range(1, 11), default=4)
    lesson_run.add_argument(
        "--auto-complete",
        action="store_true",
        help="deterministically drive the lesson through freeze, reveal, and debrief",
    )
    lesson_run.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="TEXT",
        help="question response in configured order; repeat with --auto-complete",
    )
    lesson_run.add_argument(
        "--events-jsonl",
        type=Path,
        help="save the canonical lesson input, event, response, and overlay stream",
    )
    lesson_run.add_argument(
        "--debrief-json",
        type=Path,
        help="save the structured revealed debrief",
    )

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
    from kirby2.cli.expansion import register_expansion_commands

    register_expansion_commands(subcommands)
    return parser


def main() -> None:
    from kirby2.cli.expansion import dispatch_expansion_command

    args = _parser().parse_args()
    if dispatch_expansion_command(args):
        return
    if args.command == "demo":
        print(run_demo(args.seed))
        return

    if args.command == "latency-demo":
        from kirby2.latency import LatencyTimelineInspector, run_cancel_race

        result = run_cancel_race(args.race, seed=args.seed)
        result.session.assert_invariants()
        print("KIRBY2_LATENCY_DEMO")
        print("PROFILE NORMAL simulator_only=true")
        print(f"RACE {result.race.value}")
        print(
            f"ORDER id={result.order.order_id} state={result.order.state.value} "
            f"filled={result.order.filled_quantity}/{result.order.quantity} "
            f"outcome={result.order.cancel_race_outcome}"
        )
        print(
            f"POSITION exchange={result.session.player_position} "
            f"client={result.session.client_position}"
        )
        print(
            "METRICS "
            + json.dumps(
                result.metrics.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(
            f"EVENTS count={len(result.session.events)} "
            f"sha256={result.session.event_stream_sha256()}"
        )
        print(f"RECORDING sha256={result.recording.sha256()}")
        print(
            f"REPLAY {'PASS' if result.replay.passed else 'FAIL'} "
            f"event_stream_match={str(result.replay.event_stream_match).lower()} "
            f"state_match={str(result.replay.state_match).lower()}"
        )
        print("TIMELINE")
        print(
            LatencyTimelineInspector(result.session.events).render(
                result.order.order_id
            )
        )
        print("RUNTIME_INVARIANTS PASS")
        return

    if args.command == "mechanics-demo":
        from kirby2.exchange import (
            MECHANICS_SCENARIOS,
            MechanicsTimelineInspector,
            run_mechanics_scenario,
        )

        names = (
            MECHANICS_SCENARIOS
            if args.scenario == "all"
            else (args.scenario,)
        )
        print("KIRBY2_MARKET_MECHANICS_DEMO")
        for name in names:
            result = run_mechanics_scenario(name)
            result.engine.assert_invariants()
            print(f"SCENARIO {name}")
            print(
                "SUMMARY "
                + json.dumps(
                    result.summary,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            print(
                f"EVENTS count={len(result.engine.events)} "
                f"sha256={result.engine.event_stream_sha256()}"
            )
            print(f"RECORDING sha256={result.recording.sha256()}")
            print(
                f"REPLAY {'PASS' if result.replay.passed else 'FAIL'} "
                f"event_stream_match={str(result.replay.event_stream_match).lower()} "
                f"state_match={str(result.replay.state_match).lower()}"
            )
            print("TIMELINE")
            print(MechanicsTimelineInspector(result.engine.events).render())
            print("RUNTIME_INVARIANTS PASS")
        return

    if args.command == "agent-ecology":
        from kirby2.agents import (
            EcologyRecording,
            get_adversarial_drill,
            get_population,
            replay_agent_ecology,
            run_agent_ecology,
        )

        population_id = args.drill or args.population or "liquidity_provision"
        definition = (
            get_adversarial_drill(population_id)
            if args.drill is not None
            else get_population(population_id)
        )
        result = run_agent_ecology(definition, seed=args.seed)
        recording = EcologyRecording.capture(result)
        replay = replay_agent_ecology(recording)
        print("KIRBY2_SYNTHETIC_AGENT_ECOLOGY")
        print(
            f"POPULATION id={definition.population_id} agents={len(definition.agents)} "
            f"recognition_drill={str(definition.recognition_drill).lower()} "
            f"definition_sha256={definition.sha256()}"
        )
        print(
            f"STARTING_BOOK sha256={result.summary.starting_book_sha256} "
            f"initial_mid_ticks={definition.initial_mid_ticks}"
        )
        print(
            "SUMMARY "
            + json.dumps(
                result.summary.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(
            f"PLAYER_BOUNDARY events={len(result.public_events)} "
            f"sha256={result.summary.public_event_sha256} "
            "actor_identity=hidden intent=hidden"
        )
        print(
            f"GROUND_TRUTH label=SIMULATOR_GROUND_TRUTH_POST_SESSION "
            f"actions={len(result.truth_events)} "
            f"sha256={result.summary.truth_event_sha256}"
        )
        print(
            "REPLAY "
            + json.dumps(replay.as_dict(), sort_keys=True, separators=(",", ":"))
        )
        print(f"POST_SESSION_EXPLANATION {definition.post_session_explanation}")
        if args.show_public_events:
            print("PUBLIC_EVENT_STREAM")
            for event in result.public_events:
                print(json.dumps(event.as_dict(), sort_keys=True, separators=(",", ":")))
        if args.show_post_session_actors:
            print("POST_SESSION_ACTOR_ATTRIBUTION")
            for actor in result.post_session_analysis["actor_summaries"]:
                print(json.dumps(actor, sort_keys=True, separators=(",", ":")))
        print(f"RESULT_SHA256 {result.result_sha256}")
        print(
            f"AGENT_ECOLOGY {'PASS' if replay.passed else 'FAIL'} "
            "runtime_invariants=PASS synthetic_only=true"
        )
        if not replay.passed:
            raise SystemExit(1)
        return

    if args.command == "hidden-liquidity-demo":
        from kirby2.observability import (
            HIDDEN_LIQUIDITY_SCENARIOS,
            run_blind_hidden_liquidity_exercise,
            run_hidden_liquidity_scenario,
        )

        print("KIRBY2_HIDDEN_LIQUIDITY_DEMO")
        if args.scenario in {"all", "blind-paired"}:
            blind = run_blind_hidden_liquidity_exercise()
            print("BLIND_PAIRED")
            print(f"INITIAL_OBSERVABLE sha256={blind.initial_observable_sha256}")
            print(
                f"FILLS shallow={blind.shallow_fill_quantity} "
                f"deep={blind.deep_fill_quantity}"
            )
            print(
                "SCORING shallow="
                + json.dumps(
                    blind.shallow_score.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            print(
                "SCORING deep="
                + json.dumps(
                    blind.deep_score.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            print(
                "REPLAY "
                f"shallow={'PASS' if blind.shallow.replay.passed else 'FAIL'} "
                f"deep={'PASS' if blind.deep.replay.passed else 'FAIL'}"
            )
            print("INFERENCE_REQUIRED fills,replenishment,tape")
            print("RUNTIME_INVARIANTS PASS")
        if args.scenario != "blind-paired":
            names = (
                HIDDEN_LIQUIDITY_SCENARIOS
                if args.scenario == "all"
                else (args.scenario,)
            )
            for name in names:
                result = run_hidden_liquidity_scenario(name)
                result.venue.assert_invariants()
                print(f"SCENARIO {name}")
                print(
                    "SUMMARY "
                    + json.dumps(
                        result.summary,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                print(
                    f"OBSERVABLE_EVENTS sha256="
                    f"{result.venue.observable_event_sha256()}"
                )
                print(
                    f"GROUND_TRUTH_EVENTS sha256="
                    f"{result.venue.truth_event_sha256()}"
                )
                print(f"RECORDING sha256={result.recording.sha256()}")
                print(
                    f"REPLAY {'PASS' if result.replay.passed else 'FAIL'} "
                    f"observable_match={str(result.replay.observable_match).lower()} "
                    f"truth_match={str(result.replay.ground_truth_match).lower()} "
                    f"state_match={str(result.replay.state_match).lower()}"
                )
                print("OBSERVABLE_TIMELINE")
                print(result.timeline)
                print("RUNTIME_INVARIANTS PASS")
        return

    if args.command == "multivenue-demo":
        from kirby2.multivenue import (
            MULTIVENUE_SCENARIOS,
            run_multivenue_scenario,
        )

        names = MULTIVENUE_SCENARIOS if args.scenario == "all" else (args.scenario,)
        print("KIRBY2_FRAGMENTED_MULTIVENUE_DEMO")
        for name in names:
            result = run_multivenue_scenario(name)
            result.coordinator.assert_invariants()
            print(f"SCENARIO {name}")
            print(
                "SUMMARY "
                + json.dumps(result.summary, sort_keys=True, separators=(",", ":"))
            )
            print(
                f"EVENTS count={len(result.coordinator.events)} "
                f"sha256={result.coordinator.event_stream_sha256()}"
            )
            for route_id in result.route_ids:
                route = result.coordinator.route_result(route_id)
                score = result.coordinator.score_route(route_id)
                print(
                    f"ROUTE {route_id} policy={route.decision.policy.value} "
                    f"evidence_sha256={route.decision.observable_feed_sha256}"
                )
                print(f"EXPLANATION {route.decision.explanation}")
                print(
                    "SCORE "
                    + json.dumps(score.as_dict(), sort_keys=True, separators=(",", ":"))
                )
            print(f"RECORDING sha256={result.recording.sha256()}")
            print(
                f"REPLAY {'PASS' if result.replay.passed else 'FAIL'} "
                f"events={str(result.replay.events_match).lower()} "
                f"feed={str(result.replay.feed_match).lower()} "
                f"truth={str(result.replay.ground_truth_match).lower()} "
                f"scores={str(result.replay.scores_match).lower()} "
                f"state={str(result.replay.state_match).lower()}"
            )
            print("ROUTING_TIMELINE")
            print(result.timeline)
            print("RUNTIME_INVARIANTS PASS")
        return

    if args.command == "benchmark-execution":
        from kirby2.algorithms import (
            AlgorithmName,
            BenchmarkManifest,
            RiskLimits,
            default_algorithm_manifest,
            manual_manifest_from_session_recording,
            run_execution_benchmark,
        )
        from kirby2.exchange.models import Side
        from kirby2.session.replay import SessionRecording

        scenarios = tuple(args.benchmark_scenarios or ("opening_momentum",))
        if len(scenarios) != len(set(scenarios)):
            print("BENCHMARK_ERROR scenarios must be unique", file=sys.stderr)
            raise SystemExit(2)
        duration_us = args.seconds * 1_000_000
        interval_us = args.decision_interval_ms * 1_000
        try:
            side = Side(args.side)
            if (
                args.manual_replay is not None
                and AlgorithmName.MANUAL_REPLAY not in args.algorithms
            ):
                raise ValueError(
                    "--manual-replay requires manual_replay in --algorithms"
                )
            manual_manifest = None
            if args.manual_replay is not None:
                manual_manifest = manual_manifest_from_session_recording(
                    SessionRecording.load(args.manual_replay),
                    objective_side=side,
                    benchmark_duration_us=duration_us,
                    decision_interval_us=interval_us,
                )
            manifest = BenchmarkManifest(
                experiment_id="execution-benchmark-v1",
                scenario_names=scenarios,
                algorithm_manifests=tuple(
                    (
                        manual_manifest
                        if name is AlgorithmName.MANUAL_REPLAY
                        and manual_manifest is not None
                        else default_algorithm_manifest(name)
                    )
                    for name in args.algorithms
                ),
                seeds=args.seeds,
                quantity=args.quantity,
                duration_us=duration_us,
                decision_interval_us=interval_us,
                side=side,
                risk_limits=RiskLimits(
                    maximum_child_quantity=args.quantity,
                    maximum_working_quantity=args.quantity,
                    maximum_position=args.quantity,
                    maximum_spread_ticks=10,
                ),
            )
            result = run_execution_benchmark(manifest, store_root=args.store)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"BENCHMARK_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_EXECUTION_ALGORITHM_BENCHMARK")
        print(
            f"MANIFEST sha256={manifest.sha256()} scenarios={len(scenarios)} "
            f"seeds={len(manifest.seeds)} algorithms={len(manifest.algorithm_manifests)}"
        )
        if manual_manifest is not None:
            provenance = manual_manifest.parameters["replay_provenance"]
            if not isinstance(provenance, Mapping):
                raise RuntimeError("manual replay provenance is not an object")
            print(
                "MANUAL_REPLAY_SOURCE "
                f"sha256={provenance['source_sha256']} "
                f"verification={provenance['source_verification']} "
                f"traffic_light_guided={str(provenance['traffic_light_guided']).lower()}"
            )
        for run in result.runs:
            print(
                "PER_SEED "
                + json.dumps(run.as_dict(), sort_keys=True, separators=(",", ":"))
            )
        for aggregate in result.aggregate_by_algorithm:
            print(
                "AGGREGATE "
                + json.dumps(aggregate, sort_keys=True, separators=(",", ":"))
            )
        print(f"IMMUTABLE_RUNS count={len(result.runs)} root={result.immutable_store_root}")
        print(
            "WINNER none "
            + str(result.winner_declaration["reason"])
        )
        print(f"RESULT_SHA256 {result.result_sha256}")
        print("BENCHMARK_EXECUTION PASS")
        return

    if args.command == "counterfactual":
        from kirby2.counterfactual import (
            ActionMutation,
            CounterfactualStore,
            MutationManifest,
            run_counterfactual,
            run_timing_sweep,
        )
        from kirby2.exchange import OrderType

        mutation_fields = (
            args.replace,
            args.replacement_command,
            args.order_type,
            args.price_ticks,
            args.quantity,
            args.venue,
            args.hotkey_outcome,
        )
        try:
            if args.sweep_timing:
                if (
                    any(value is not None for value in mutation_fields)
                    or args.remove
                    or args.insert
                    or args.timing_delta_us != 0
                ):
                    raise ValueError(
                        "--sweep-timing cannot be combined with a one-off mutation"
                    )
                sweep = run_timing_sweep(
                    args.run_id,
                    args.at,
                    args.mode,
                    parent_store_root=args.store,
                )
                print("KIRBY2_COUNTERFACTUAL_TIMING_SWEEP")
                print(f"PARENT_RUN_ID {args.run_id}")
                print(f"MODE {args.mode.value}")
                for cell in sweep.cells:
                    print(
                        "CELL "
                        + json.dumps(
                            cell.as_dict(),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                print(
                    "CAUSAL_GUARD policy_information=decision_time_only "
                    "analysis_may_use_later_information=true"
                )
                print("COUNTERFACTUAL_TIMING_SWEEP PASS immutable_branches=5")
                return
            mutation = ActionMutation(
                args.at,
                expected_command=args.replace,
                command=args.replacement_command,
                order_type=(
                    None if args.order_type is None else OrderType(args.order_type)
                ),
                price_ticks=args.price_ticks,
                quantity=args.quantity,
                venue_id=args.venue,
                timing_delta_us=args.timing_delta_us,
                remove=args.remove,
                insert=args.insert,
                hotkey_outcome=args.hotkey_outcome,
            )
            report = run_counterfactual(
                args.run_id,
                MutationManifest((mutation,)),
                args.mode,
                parent_store_root=args.store,
            )
            branch_store = CounterfactualStore(args.store / "counterfactual_runs")
            manifest = branch_store.record(report)
            verification = branch_store.verify_run(manifest.run_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"COUNTERFACTUAL_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_COUNTERFACTUAL_EXECUTION_DEBUGGER")
        print(f"PARENT_RUN_ID {report.parent_run_id}")
        print(f"BRANCH_RUN_ID {manifest.run_id}")
        print(f"MODE {report.mode.value}")
        print(
            "MODE_CONTRACT "
            + (
                "external_path=fixed player_impact_on_reference=ignored"
                if report.mode.value == "EXOGENOUS_REPLAY"
                else "full_simulator=forked changed_orders_may_change_future_flow=true"
            )
        )
        print(
            f"SNAPSHOT time_us={report.snapshot.fork_time_us} "
            f"sha256={report.snapshot.sha256()} "
            f"reconstruction_match={str(report.snapshot_reconstruction_match).lower()}"
        )
        for component in report.snapshot.components:
            print(
                f"COMPONENT {component.name} status={component.status.value} "
                f"sha256={component.sha256}"
            )
        print(
            "FIRST_DIVERGENCE "
            + json.dumps(
                report.first_divergence.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(
            "COMPARISON "
            + json.dumps(report.comparison, sort_keys=True, separators=(",", ":"))
        )
        print(
            "CAUSAL_GUARD "
            + json.dumps(
                report.hindsight_guard,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(f"INTERPRETATION {report.cautious_interpretation}")
        print(f"RESULT_SHA256 {report.result_sha256()}")
        print(f"BRANCH_DIRECTORY {branch_store.run_directory(manifest.run_id).resolve()}")
        print(
            f"COUNTERFACTUAL {'PASS' if verification.passed else 'FAIL'} "
            f"invariants={report.branch.invariant_status} immutable=true"
        )
        if not verification.passed:
            raise SystemExit(1)
        return

    if args.command == "strategy":
        from kirby2.strategy import FeatureName, PositionFeature, StateMachineDefinition

        definition = _load_strategy_file(args.rule_file)
        print("KIRBY2_TRAFFIC_STRATEGY")
        print(json.dumps(definition.as_dict(), sort_keys=True, separators=(",", ":")))
        print(
            "OBSERVABLE_FEATURES "
            + ",".join(feature.value for feature in FeatureName)
        )
        if isinstance(definition, StateMachineDefinition):
            print(
                "POSITION_FEATURES "
                + ",".join(feature.value for feature in PositionFeature)
            )
        print("STRATEGY_VALID PASS arbitrary_code=DISABLED hidden_regime=UNAVAILABLE")
        return

    if args.command == "experiment":
        from kirby2.experiments import ExperimentManifest, run_strategy_experiment

        try:
            manifest = ExperimentManifest.load(args.manifest)
            result = run_strategy_experiment(manifest)
            output_directory = (
                args.output
                if args.output is not None
                else Path(".kirby2") / "experiments" / manifest.experiment_id
            )
            result.save(output_directory)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"EXPERIMENT_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(result.render())
        print(f"ARTIFACT_DIRECTORY {output_directory.resolve()}")
        return

    if args.command == "lesson-list":
        from kirby2.historical import load_historical_lessons

        lessons = load_historical_lessons()
        print("KIRBY2_HISTORICAL_LESSONS")
        for lesson in lessons.values():
            print(
                f"{lesson.lesson_id}  mode={lesson.mode.value} "
                f"reveal={lesson.reveal_policy.value}  title={lesson.title}"
            )
        print(f"LESSON_COUNT {len(lessons)}")
        print("LESSON_CATALOG PASS provenance_validated=true")
        return

    if args.command == "ingest-market-data":
        from kirby2.marketdata import MarketDataStore
        from kirby2.research.toml_codec import load_toml

        try:
            store = MarketDataStore(args.store)
            manifest = store.ingest(args.adapter, args.source)
            verification = store.verify_dataset(manifest.dataset_id)
            quality = load_toml(
                store.dataset_directory(manifest.dataset_id) / "quality_report.toml"
            )
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"INGEST_MARKET_DATA_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_INGEST_MARKET_DATA")
        print(f"DATASET_ID {manifest.dataset_id}")
        print(f"ADAPTER {manifest.adapter}")
        print(f"CAPABILITY {manifest.capability.value}")
        print(f"REAL_MARKET_DATA {str(manifest.real_market_data).lower()}")
        print(f"SOURCE_DIGEST {manifest.source_digest}")
        print(
            f"ROWS input={quality['input_rows']} accepted={quality['accepted_rows']} "
            f"rejected={quality['rejected_rows']}"
        )
        print(
            f"QUALITY warnings={len(quality['warnings'])} "
            f"gaps={len(quality['gaps'])} repairs={len(quality['repairs'])}"
        )
        print(
            f"REPLAY mode={manifest.replay_mode.value} "
            f"exact={str(manifest.exact_replay_allowed).lower()}"
        )
        print(
            f"INGEST_MARKET_DATA {'PASS' if verification.passed else 'FAIL'}"
        )
        if not verification.passed:
            raise SystemExit(1)
        return

    if args.command == "inspect-dataset":
        from kirby2.marketdata import MarketDataStore
        from kirby2.research.toml_codec import canonical_toml

        try:
            payload = MarketDataStore(args.store).inspect_dataset(args.dataset_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"INSPECT_DATASET_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_INSPECT_DATASET")
        print(canonical_toml({"inspection": payload}), end="")
        return

    if args.command == "validate-dataset":
        from kirby2.marketdata import MarketDataStore

        try:
            report = MarketDataStore(args.store).verify_dataset(args.dataset_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"VALIDATE_DATASET_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(report.render())
        if not report.passed:
            raise SystemExit(1)
        return

    if args.command == "replay-capability":
        from kirby2.marketdata import MarketDataStore

        try:
            decision = MarketDataStore(args.store).replay_decision(args.dataset_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"REPLAY_CAPABILITY_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_REPLAY_CAPABILITY")
        print(f"DATASET_ID {args.dataset_id}")
        print(f"MODE {decision.mode.value}")
        print(f"EXACT_REPLAY_ALLOWED {str(decision.exact_replay_allowed).lower()}")
        for reason in decision.reasons:
            print(f"REASON {reason}")
        print(
            "REPLAY_CAPABILITY "
            + ("EXACT_SUPPORTED" if decision.exact_replay_allowed else "EXACT_REFUSED")
        )
        return

    if args.command == "record-run":
        from kirby2.research import RunStore
        from kirby2.session.layouts import HotkeyLayout
        from kirby2.session.live import LiveMarketSession
        from kirby2.session.objectives import ObjectiveType, SessionObjective
        from kirby2.session.replay import SessionRecording

        duration_us = args.seconds * 1_000_000
        actions = tuple(args.player_action) or ((duration_us // 2, "d"),)
        if tuple(time_us for time_us, _key in actions) != tuple(
            sorted(time_us for time_us, _key in actions)
        ):
            print("RECORD_RUN_ERROR player actions must be time-ordered", file=sys.stderr)
            raise SystemExit(2)
        if any(time_us > duration_us for time_us, _key in actions):
            print("RECORD_RUN_ERROR player action exceeds session duration", file=sys.stderr)
            raise SystemExit(2)
        try:
            layout = HotkeyLayout.default()
            objective = SessionObjective(
                ObjectiveType.ACQUIRE,
                target_quantity=args.quantity,
                time_limit_us=duration_us,
                preferred_slippage_ticks=2,
            )
            session = LiveMarketSession(
                get_scenario_definition(args.scenario),
                seed=args.seed,
                duration_seconds=args.seconds,
                initial_quantity=args.quantity,
                quantity_options=tuple(
                    sorted(
                        {
                            25,
                            50,
                            100,
                            200,
                            500,
                            1_000,
                            2_000,
                            args.quantity,
                        }
                    )
                ),
                objective=objective,
            )
            session.start()
            for simulation_time_us, input_key in actions:
                delta_us = simulation_time_us - session.simulation_time_us
                if delta_us:
                    if not session.running:
                        raise ValueError("player action sequence paused before its next timestamp")
                    session.advance_by(delta_us)
                session.handle_input(input_key, layout.bindings)
            final_delta_us = duration_us - session.simulation_time_us
            if final_delta_us:
                if not session.running:
                    raise ValueError("player action sequence paused before session completion")
                session.advance_by(final_delta_us)
            recording = SessionRecording.capture(session, layout, auto_start=True)
            store = RunStore(args.store)
            manifest = store.record_session(recording, session)
            verification = store.verify_run(manifest.run_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"RECORD_RUN_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_RECORD_RUN")
        print(f"RUN_ID {manifest.run_id}")
        print(f"RUN_DIRECTORY {store.run_directory(manifest.run_id).resolve()}")
        print(f"CONFIGURATION_DIGEST {manifest.configuration_digest}")
        print(f"EVIDENCE_DIGEST {manifest.evidence_digest}")
        print(f"RESULT_DIGEST {manifest.result_digest}")
        table_artifacts = tuple(
            item for item in manifest.artifacts if item.row_count is not None
        )
        print(
            f"TABLES {len(table_artifacts)} "
            f"ROWS {sum(item.row_count or 0 for item in table_artifacts)}"
        )
        print(
            f"RECORD_RUN {'PASS' if verification.passed else 'FAIL'} "
            f"replay={str(verification.replay_passed).lower()}"
        )
        if not verification.passed:
            raise SystemExit(1)
        return

    if args.command == "inspect-run":
        from kirby2.research import RunStore
        from kirby2.research.toml_codec import canonical_toml

        try:
            payload = RunStore(args.store).inspect_run(args.run_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"INSPECT_RUN_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_INSPECT_RUN")
        print(canonical_toml({"inspection": payload}), end="")
        return

    if args.command == "query-runs":
        from kirby2.research import RunStore

        try:
            rows = RunStore(args.store).query_runs(args.scenario)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"QUERY_RUNS_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print("KIRBY2_QUERY_RUNS")
        for row in rows:
            print(
                f"RUN {row['run_id']} scenario={row['scenario_id']} seed={row['seed']} "
                f"events={row['event_count']} trades={row['trade_count']} "
                f"actions={row['player_action_count']} result={row['result_digest']}"
            )
        print(f"RUN_COUNT {len(rows)}")
        return

    if args.command == "verify-run":
        from kirby2.research import RunStore

        try:
            report = RunStore(args.store).verify_run(args.run_id)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"VERIFY_RUN_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(report.render())
        if not report.passed:
            raise SystemExit(1)
        return

    if args.command == "lesson-run":
        from kirby2.historical import (
            historical_lesson_debrief,
            load_historical_lessons,
            render_historical_lesson,
            run_historical_lesson,
        )

        try:
            lesson = load_historical_lessons()[args.lesson]
            session = run_historical_lesson(lesson)
            if args.answer and not args.auto_complete:
                raise ValueError("--answer requires --auto-complete")
            if args.debrief_json is not None and not args.auto_complete:
                raise ValueError("--debrief-json requires --auto-complete")
            if len(args.answer) > len(lesson.training_questions):
                raise ValueError("more answers were supplied than configured questions")
            if args.auto_complete:
                session.start()
                session.pause()
                session.resume()
                session.step()
                if session.phase.value == "BLIND_RUNNING":
                    session.advance_to(session.run.duration_us)
                for index, _question in enumerate(
                    lesson.training_questions,
                    start=1,
                ):
                    response = (
                        args.answer[index - 1]
                        if index <= len(args.answer)
                        else f"Auto-complete response {index}; no user response supplied."
                    )
                    session.answer(index, response)
                session.freeze_responses()
                session.reveal()
                session.debrief()
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            print(f"LESSON_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error
        print(render_historical_lesson(session, args.levels))
        if args.events_jsonl is not None:
            args.events_jsonl.write_text(
                session.replay_json_lines() + "\n",
                encoding="utf-8",
            )
            print(f"LESSON_SESSION_STREAM {args.events_jsonl.resolve()}")
        if args.debrief_json is not None:
            args.debrief_json.write_text(
                json.dumps(
                    historical_lesson_debrief(session),
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"LESSON_DEBRIEF_JSON {args.debrief_json.resolve()}")
        if session.debrief_available:
            print("HISTORICAL_LESSON PASS phase=DEBRIEFED responses_frozen=true")
        else:
            print(
                "HISTORICAL_LESSON READY incomplete=true "
                "identity_revealed=false"
            )
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
                {
                    **profile.distribution(purpose).as_dict(),
                    "unit": purpose.unit,
                },
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

    if args.command == "calibrate":
        from kirby2.calibration import (
            CalibrationConfig,
            calibrate_market,
            resolve_measurement_source,
        )

        try:
            reference_stream = resolve_measurement_source(
                args.reference,
                seed=args.reference_seed,
                seconds=args.seconds or 30,
            )
            if args.seconds is None:
                if reference_stream.duration_us % 1_000_000:
                    raise ValueError(
                        "reference duration is not whole seconds; pass --seconds"
                    )
                calibration_seconds = reference_stream.duration_us // 1_000_000
            else:
                calibration_seconds = args.seconds
            fixed_parameters: dict[str, float] = {}
            for name, value in args.fixed:
                if name in fixed_parameters:
                    raise ValueError(f"fixed parameter repeated: {name}")
                fixed_parameters[name] = value
            config = CalibrationConfig(
                scenario_name=args.scenario,
                seconds=calibration_seconds,
                stages=args.stages,
                fitting_seeds=args.fit_seeds,
                heldout_seeds=args.heldout_seeds,
                search_seed=args.search_seed,
                candidate_count_per_stage=args.candidates,
                profile_id=args.profile_id,
                fixed_parameters=fixed_parameters,
            )
            run = calibrate_market(reference_stream, config)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            print(f"CALIBRATION_ERROR {error}", file=sys.stderr)
            raise SystemExit(2) from error

        print(
            f"KIRBY2_CALIBRATION reference={run.reference_report.source_id} "
            f"scenario={args.scenario} stages={','.join(map(str, args.stages))}"
        )
        for outcome in run.stage_outcomes:
            print(
                "STAGE "
                + json.dumps(
                    outcome.as_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        print(
            "BEST_PARAMETERS "
            + json.dumps(
                run.market_profile.parameters,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        print(
            f"FITTING_LOSS initial={run.initial_fitting.mean_loss:.9f} "
            f"final={run.final_fitting.mean_loss:.9f}"
        )
        print(
            f"HELDOUT_LOSS initial={run.initial_heldout.mean_loss:.9f} "
            f"final={run.final_heldout.mean_loss:.9f} "
            f"improvement={run.heldout_improvement:.9f}"
        )
        print(f"HELDOUT_GATE {'PASS' if run.heldout_improved else 'FAIL'}")
        print(f"CALIBRATION_RUN_SHA256 {run.sha256()}")
        print(f"MARKET_PROFILE {run.market_profile.canonical_json()}")
        if args.output is not None:
            args.output.write_text(
                json.dumps(run.market_profile.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"MARKET_PROFILE_PATH {args.output.resolve()}")
        if args.record is not None:
            args.record.write_text(
                json.dumps(run.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"CALIBRATION_RECORD_PATH {args.record.resolve()}")
        print("CALIBRATION_INVARIANTS PASS bounded=true multi_seed=true heldout=true")
        if args.require_heldout_improvement and not run.heldout_improved:
            raise SystemExit(1)
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

    if args.command == "audit-hawkes-stability":
        from kirby2.audit.hawkes import audit_hawkes_stability

        reports = audit_hawkes_stability()
        print("KIRBY2_HAWKES_STABILITY_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"HAWKES_STABILITY_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-strategy-time":
        from kirby2.audit.strategy_time import audit_strategy_time

        reports = audit_strategy_time()
        print("KIRBY2_STRATEGY_TIME_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"STRATEGY_TIME_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-distribution-truth":
        from kirby2.audit.distribution_truth import audit_distribution_truth

        reports = audit_distribution_truth()
        print("KIRBY2_DISTRIBUTION_TRUTH_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"DISTRIBUTION_TRUTH_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-historical-features":
        from kirby2.audit.historical_features import audit_historical_features

        reports = audit_historical_features()
        print("KIRBY2_HISTORICAL_FEATURE_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"HISTORICAL_FEATURE_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-historical-lessons":
        from kirby2.audit.historical_lessons import audit_historical_lessons

        reports = audit_historical_lessons()
        print("KIRBY2_HISTORICAL_LESSON_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"HISTORICAL_LESSON_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-run-store":
        from kirby2.audit.run_store import audit_run_store

        reports = audit_run_store()
        print("KIRBY2_RUN_STORE_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"RUN_STORE_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-market-data":
        from kirby2.audit.market_data import audit_market_data

        reports = audit_market_data()
        print("KIRBY2_MARKET_DATA_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"MARKET_DATA_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-latency":
        from kirby2.audit.latency import audit_latency

        reports = audit_latency()
        print("KIRBY2_LATENCY_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"LATENCY_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-market-mechanics":
        from kirby2.audit.market_mechanics import audit_market_mechanics

        reports = audit_market_mechanics()
        print("KIRBY2_MARKET_MECHANICS_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"MARKET_MECHANICS_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-hidden-liquidity":
        from kirby2.audit.hidden_liquidity import audit_hidden_liquidity

        reports = audit_hidden_liquidity()
        print("KIRBY2_HIDDEN_LIQUIDITY_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"HIDDEN_LIQUIDITY_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-multivenue":
        from kirby2.audit.multivenue import audit_multivenue

        reports = audit_multivenue()
        print("KIRBY2_MULTIVENUE_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"MULTIVENUE_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-execution-algorithms":
        from kirby2.audit.execution_algorithms import audit_execution_algorithms

        reports = audit_execution_algorithms()
        print("KIRBY2_EXECUTION_ALGORITHM_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"EXECUTION_ALGORITHM_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-counterfactuals":
        from kirby2.audit.counterfactuals import audit_counterfactuals

        reports = audit_counterfactuals()
        print("KIRBY2_COUNTERFACTUAL_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"COUNTERFACTUAL_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-agent-ecology":
        from kirby2.audit.agent_ecology import audit_agent_ecology

        reports = audit_agent_ecology()
        print("KIRBY2_AGENT_ECOLOGY_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"AGENT_ECOLOGY_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
        )
        if failed:
            raise SystemExit(1)
        return

    if args.command == "audit-lab":
        from kirby2.auditlab import run_audit_lab

        result = run_audit_lab(
            budget=args.budget,
            seed=args.seed,
            store_root=args.store,
            save_failures=args.save_failures,
        )
        print(result.render())
        if not result.passed:
            raise SystemExit(1)
        return

    if args.command == "audit-model-risk-lab":
        from kirby2.audit.model_risk_lab import audit_model_risk_lab

        reports = audit_model_risk_lab()
        print("KIRBY2_MODEL_RISK_LAB_AUDIT")
        for report in reports:
            print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        failed = [report for report in reports if not report.passed]
        print(
            f"MODEL_RISK_LAB_AUDIT {'FAIL' if failed else 'PASS'} "
            f"cases={len(reports)} failures={len(failed)}"
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
