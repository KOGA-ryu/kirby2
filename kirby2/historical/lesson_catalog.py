"""Load and validate the packaged historical lesson catalog."""

from __future__ import annotations

import json
from pathlib import Path

from .fixtures import load_historical_fixtures
from .lesson_models import HistoricalLesson
from .models import ExactReplayFixture, HistoricalDataMode


LESSON_DIRECTORY = Path(__file__).with_name("lessons")


def load_historical_lesson(path: Path) -> HistoricalLesson:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("historical lesson file must contain a JSON object")
    return HistoricalLesson.from_dict(payload)


def load_historical_lessons() -> dict[str, HistoricalLesson]:
    lessons = tuple(
        load_historical_lesson(path)
        for path in sorted(LESSON_DIRECTORY.glob("*.json"))
    )
    if len(lessons) < 3:
        raise RuntimeError("historical lesson catalog requires at least three packs")
    by_id = {lesson.lesson_id: lesson for lesson in lessons}
    if len(by_id) != len(lessons):
        raise ValueError("historical lesson IDs must be unique")
    fixtures = load_historical_fixtures()
    for lesson in lessons:
        fixture = fixtures.get(lesson.source.fixture_id)
        if fixture is None:
            raise ValueError(
                f"historical lesson {lesson.lesson_id} references an unknown fixture"
            )
        mode = (
            HistoricalDataMode.EXACT_REPLAY
            if isinstance(fixture, ExactReplayFixture)
            else HistoricalDataMode.RECONSTRUCTION
        )
        duration_us = (
            fixture.duration_us
            if isinstance(fixture, ExactReplayFixture)
            else fixture.constraints.duration_us
        )
        if lesson.mode is not mode:
            raise ValueError(
                f"historical lesson {lesson.lesson_id} mode mismatches its fixture"
            )
        if lesson.source.source_locator != fixture.provenance.source_locator:
            raise ValueError(
                f"historical lesson {lesson.lesson_id} source locator mismatches provenance"
            )
        if (
            lesson.time_window.start_us != 0
            or lesson.time_window.end_us != duration_us
        ):
            raise ValueError(
                f"historical lesson {lesson.lesson_id} must use its complete fixture window"
            )
    return by_id
