"""Portable session recordings, deterministic replay, and timeline inspection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kirby2.curriculum.models import CurriculumDrill
from kirby2.scenarios import ScenarioDefinition
from kirby2.simulation import LiquidityPreset, VolumePreset
from kirby2.strategy import parse_strategy

from .layouts import HotkeyLayout
from .live import LiveMarketSession
from .objectives import SessionObjective
from .records import InputRecord, MarketStateRecord, TimelineKind, TimelineRecord


RECORDING_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SessionRecording:
    scenario_definition: dict[str, object]
    seed: int
    duration_seconds: int
    relative_volume: VolumePreset
    liquidity: LiquidityPreset
    initial_quantity: int
    quantity_options: tuple[int, ...]
    layout: HotkeyLayout
    strategy_source: str | None
    objective: SessionObjective | None
    auto_start: bool
    input_records: tuple[InputRecord, ...]
    market_states: tuple[MarketStateRecord, ...]
    completed_time_us: int
    complete: bool
    expected_state_sha256: str
    expected_timeline_sha256: str
    curriculum_drill: CurriculumDrill | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("recording duration must be positive")
        duration_us = self.duration_seconds * 1_000_000
        if not 0 <= self.completed_time_us <= duration_us:
            raise ValueError("recording completion time is outside its duration")
        if self.complete and self.completed_time_us != duration_us:
            raise ValueError("complete recording must end at its configured duration")
        if not self.quantity_options or self.initial_quantity not in self.quantity_options:
            raise ValueError("recording has invalid quantity configuration")
        sequences = [record.sequence for record in self.input_records]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("recorded input sequences must be contiguous")
        timestamps = [record.simulation_time_us for record in self.input_records]
        if timestamps != sorted(timestamps):
            raise ValueError("recorded input timestamps must be monotonic")
        state_ids = [state.state_id for state in self.market_states]
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("recording contains duplicate market-state IDs")
        state_id_set = set(state_ids)
        if any(record.market_state_id not in state_id_set for record in self.input_records):
            raise ValueError("recorded input references a missing market state")
        if self.curriculum_drill is not None:
            drill = self.curriculum_drill
            from kirby2.curriculum.catalog import get_lesson

            get_lesson(drill.lesson_id).assert_contains(drill)
            if str(self.scenario_definition.get("name")) != drill.scenario_name:
                raise ValueError("recording curriculum scenario does not match")
            if self.seed != drill.scenario_seed:
                raise ValueError("recording curriculum seed does not match")
            if self.duration_seconds != drill.duration_seconds:
                raise ValueError("recording curriculum duration does not match")
            if self.relative_volume is not drill.volume:
                raise ValueError("recording curriculum volume does not match")
            if self.liquidity is not drill.liquidity:
                raise ValueError("recording curriculum liquidity does not match")
            if self.objective != drill.player_objective:
                raise ValueError("recording curriculum objective does not match")

    @classmethod
    def capture(
        cls,
        session: LiveMarketSession,
        layout: HotkeyLayout,
        auto_start: bool = True,
    ) -> SessionRecording:
        session.engine.book.assert_invariants()
        return cls(
            scenario_definition=session.definition.as_dict(),
            seed=session.seed,
            duration_seconds=session.duration_us // 1_000_000,
            relative_volume=session.dimensions.volume,
            liquidity=session.dimensions.liquidity,
            initial_quantity=session.initial_quantity,
            quantity_options=session.quantity_options,
            layout=layout,
            strategy_source=(
                None
                if session.strategy_definition is None
                else session.strategy_definition.source
            ),
            objective=session.objective,
            auto_start=auto_start,
            input_records=session.input_records,
            market_states=session.market_states,
            completed_time_us=session.simulation_time_us,
            complete=session.complete,
            expected_state_sha256=session.state_sha256(),
            expected_timeline_sha256=session.timeline_sha256(),
            curriculum_drill=session.curriculum_drill,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "auto_start": self.auto_start,
            "complete": self.complete,
            "completed_time_us": self.completed_time_us,
            "curriculum_drill": (
                None
                if self.curriculum_drill is None
                else self.curriculum_drill.as_dict()
            ),
            "duration_seconds": self.duration_seconds,
            "expected_state_sha256": self.expected_state_sha256,
            "expected_timeline_sha256": self.expected_timeline_sha256,
            "initial_quantity": self.initial_quantity,
            "inputs": [record.as_dict() for record in self.input_records],
            "layout": self.layout.as_dict(),
            "liquidity": self.liquidity.value,
            "market_states": [state.as_dict() for state in self.market_states],
            "objective": None if self.objective is None else self.objective.as_dict(),
            "quantity_options": list(self.quantity_options),
            "record_type": "kirby2_session_recording",
            "relative_volume": self.relative_volume.value,
            "scenario_definition": self.scenario_definition,
            "schema_version": RECORDING_SCHEMA_VERSION,
            "seed": self.seed,
            "strategy_source": self.strategy_source,
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.as_dict(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    @classmethod
    def load(cls, path: Path) -> SessionRecording:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("session recording must contain a JSON object")
        if payload.get("record_type") != "kirby2_session_recording":
            raise ValueError("file is not a Kirby2 session recording")
        if payload.get("schema_version") != RECORDING_SCHEMA_VERSION:
            raise ValueError("unsupported session recording schema version")
        scenario = payload.get("scenario_definition")
        layout = payload.get("layout")
        inputs = payload.get("inputs")
        market_states = payload.get("market_states")
        quantities = payload.get("quantity_options")
        objective = payload.get("objective")
        curriculum_drill = payload.get("curriculum_drill")
        if not isinstance(scenario, dict):
            raise ValueError("recording scenario definition must be an object")
        if not isinstance(layout, dict):
            raise ValueError("recording layout must be an object")
        if not isinstance(inputs, list) or not isinstance(market_states, list):
            raise ValueError("recording inputs and market states must be arrays")
        if any(not isinstance(item, dict) for item in inputs):
            raise ValueError("every recorded input must be an object")
        if any(not isinstance(item, dict) for item in market_states):
            raise ValueError("every recorded market state must be an object")
        if not isinstance(quantities, list):
            raise ValueError("recording quantity options must be an array")
        if objective is not None and not isinstance(objective, dict):
            raise ValueError("recording objective must be an object or null")
        if curriculum_drill is not None and not isinstance(curriculum_drill, dict):
            raise ValueError("recording curriculum drill must be an object or null")
        return cls(
            scenario_definition=dict(scenario),
            seed=int(payload["seed"]),
            duration_seconds=int(payload["duration_seconds"]),
            relative_volume=VolumePreset.parse(str(payload["relative_volume"])),
            liquidity=LiquidityPreset.parse(str(payload["liquidity"])),
            initial_quantity=int(payload["initial_quantity"]),
            quantity_options=tuple(int(value) for value in quantities),
            layout=HotkeyLayout.from_dict(layout),
            strategy_source=(
                None
                if payload.get("strategy_source") is None
                else str(payload["strategy_source"])
            ),
            objective=(
                None
                if objective is None
                else SessionObjective.from_dict(objective)
            ),
            auto_start=bool(payload["auto_start"]),
            input_records=tuple(
                InputRecord.from_dict(item) for item in inputs  # type: ignore[arg-type]
            ),
            market_states=tuple(
                MarketStateRecord.from_dict(item)
                for item in market_states  # type: ignore[arg-type]
            ),
            completed_time_us=int(payload["completed_time_us"]),
            complete=bool(payload["complete"]),
            expected_state_sha256=str(payload["expected_state_sha256"]),
            expected_timeline_sha256=str(payload["expected_timeline_sha256"]),
            curriculum_drill=(
                None
                if curriculum_drill is None
                else CurriculumDrill.from_dict(curriculum_drill)
            ),
        )


@dataclass(slots=True)
class ReplayReport:
    recording: SessionRecording
    session: LiveMarketSession
    input_records_match: bool
    market_states_match: bool
    state_digest_match: bool
    timeline_digest_match: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.input_records_match,
                self.market_states_match,
                self.state_digest_match,
                self.timeline_digest_match,
            )
        )

    def summary(self) -> dict[str, object]:
        return {
            "actual_state_sha256": self.session.state_sha256(),
            "actual_timeline_sha256": self.session.timeline_sha256(),
            "complete": self.session.complete,
            "expected_state_sha256": self.recording.expected_state_sha256,
            "expected_timeline_sha256": self.recording.expected_timeline_sha256,
            "input_count": len(self.recording.input_records),
            "input_records_match": self.input_records_match,
            "market_states_match": self.market_states_match,
            "simulation_time_us": self.session.simulation_time_us,
            "state_digest_match": self.state_digest_match,
            "status": "PASS" if self.passed else "FAIL",
            "timeline_digest_match": self.timeline_digest_match,
            "timeline_record_count": len(self.session.timeline),
        }


def replay_recording(recording: SessionRecording) -> ReplayReport:
    definition = ScenarioDefinition.from_dict(recording.scenario_definition)
    strategy_definition = (
        None
        if recording.strategy_source is None
        else parse_strategy(recording.strategy_source)
    )
    session = LiveMarketSession(
        definition,
        seed=recording.seed,
        duration_seconds=recording.duration_seconds,
        relative_volume=recording.relative_volume,
        liquidity=recording.liquidity,
        initial_quantity=recording.initial_quantity,
        quantity_options=recording.quantity_options,
        strategy_definition=strategy_definition,
        objective=recording.objective,
        curriculum_drill=recording.curriculum_drill,
    )
    if recording.auto_start:
        session.start()

    for expected in recording.input_records:
        if expected.simulation_time_us < session.simulation_time_us:
            raise ValueError("recorded input timestamps must be monotonic")
        delta_us = expected.simulation_time_us - session.simulation_time_us
        if delta_us:
            if not session.running:
                raise ValueError("recording advances simulation time while session is paused")
            session.advance_by(delta_us)
        actual = session.handle_input(expected.input_key, recording.layout.bindings)
        if actual.resolved_command != expected.resolved_command:
            raise ValueError("embedded layout does not resolve to the recorded command")

    if recording.completed_time_us < session.simulation_time_us:
        raise ValueError("recording completion precedes its last input")
    final_delta_us = recording.completed_time_us - session.simulation_time_us
    if final_delta_us:
        if not session.running:
            raise ValueError("recording completion advances time while session is paused")
        session.advance_by(final_delta_us)
    session.engine.book.assert_invariants()

    actual_inputs = tuple(record.as_dict() for record in session.input_records)
    expected_inputs = tuple(record.as_dict() for record in recording.input_records)
    actual_states = tuple(state.as_dict() for state in session.market_states)
    expected_states = tuple(state.as_dict() for state in recording.market_states)
    return ReplayReport(
        recording=recording,
        session=session,
        input_records_match=actual_inputs == expected_inputs,
        market_states_match=actual_states == expected_states,
        state_digest_match=session.state_sha256() == recording.expected_state_sha256,
        timeline_digest_match=(
            session.timeline_sha256() == recording.expected_timeline_sha256
        ),
    )


class TimelineInspector:
    def __init__(self, records: tuple[TimelineRecord, ...]) -> None:
        self.records = records

    def render(
        self,
        kinds: set[TimelineKind] | None = None,
        limit: int | None = None,
    ) -> str:
        selected = [
            record for record in self.records if kinds is None or record.kind in kinds
        ]
        if limit is not None:
            if limit <= 0:
                raise ValueError("timeline limit must be positive")
            selected = selected[:limit]
        return "\n".join(
            f"{_market_time(record.simulation_time_us)}  {record.message}"
            for record in selected
        )


def _market_time(simulation_time_us: int) -> str:
    total_milliseconds = 9 * 60 * 60 * 1_000 + 30 * 60 * 1_000
    total_milliseconds += simulation_time_us // 1_000
    hours, remainder = divmod(total_milliseconds, 60 * 60 * 1_000)
    minutes, remainder = divmod(remainder, 60 * 1_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours % 24:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
