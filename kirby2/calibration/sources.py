"""Resolve local reference, synthetic, and normalized-file measurement sources."""

from __future__ import annotations

import json
from pathlib import Path

from kirby2.historical import (
    ExactReplayFixture,
    load_historical_fixtures,
)
from kirby2.scenarios import (
    get_scenario_definition,
    load_scenario_definitions,
    run_market_scenario,
)

from .models import NormalizedMarketStream
from .normalization import (
    normalize_exact_fixture,
    normalize_kirby_replay,
    normalize_reconstruction_fixture,
    normalize_simulation,
)


def resolve_measurement_source(
    locator: str,
    *,
    seed: int = 42,
    seconds: int = 30,
) -> NormalizedMarketStream:
    path = Path(locator)
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        first = next((line for line in text.splitlines() if line.strip()), None)
        if first is None:
            raise ValueError(f"measurement source is empty: {path}")
        header = json.loads(first)
        if header.get("record_type") == "normalized_market_stream":
            return NormalizedMarketStream.from_json_lines(text)
        if header.get("record_type") == "simulation_config":
            return normalize_kirby_replay(text, source_id=f"file:{path.resolve()}")
        raise ValueError("file is neither a normalized stream nor Kirby2 replay JSONL")

    fixtures = load_historical_fixtures()
    scenarios = load_scenario_definitions()
    if locator.startswith("fixture:"):
        fixture_id = locator.split(":", 1)[1]
        if fixture_id not in fixtures:
            raise ValueError(f"unknown historical fixture: {fixture_id}")
        fixture = fixtures[fixture_id]
        return (
            normalize_exact_fixture(fixture)
            if isinstance(fixture, ExactReplayFixture)
            else normalize_reconstruction_fixture(fixture)
        )
    if locator.startswith("scenario:"):
        scenario_name = locator.split(":", 1)[1]
        if scenario_name not in scenarios:
            raise ValueError(f"unknown synthetic scenario: {scenario_name}")
        return _scenario_stream(scenario_name, seed, seconds)
    if locator in fixtures:
        fixture = fixtures[locator]
        return (
            normalize_exact_fixture(fixture)
            if isinstance(fixture, ExactReplayFixture)
            else normalize_reconstruction_fixture(fixture)
        )
    if locator in scenarios:
        return _scenario_stream(locator, seed, seconds)
    raise ValueError(
        "unknown measurement source; use fixture:ID, scenario:NAME, "
        "a local normalized JSONL file, or a Kirby2 replay JSONL file"
    )


def _scenario_stream(name: str, seed: int, seconds: int) -> NormalizedMarketStream:
    if type(seed) is not int:
        raise TypeError("measurement seed must be an integer")
    if type(seconds) is not int or seconds <= 0:
        raise ValueError("measurement duration must be a positive integer")
    run = run_market_scenario(
        get_scenario_definition(name),
        seed=seed,
        seconds=seconds,
    )
    return normalize_simulation(
        run.simulation,
        source_id=f"kirby2:{name}:seed={seed}:seconds={seconds}",
    )
