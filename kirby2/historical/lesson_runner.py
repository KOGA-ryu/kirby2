"""Execute one packaged lesson through the existing historical driver."""

from __future__ import annotations

from .fixtures import load_historical_fixtures
from .lesson_models import HistoricalLesson, HistoricalLessonSession
from .runner import run_historical_fixture


def run_historical_lesson(lesson: HistoricalLesson) -> HistoricalLessonSession:
    fixtures = load_historical_fixtures()
    fixture = fixtures.get(lesson.source.fixture_id)
    if fixture is None:
        raise ValueError(f"unknown historical lesson fixture: {lesson.source.fixture_id}")
    if lesson.source.source_locator != fixture.provenance.source_locator:
        raise ValueError("historical lesson source locator does not match fixture provenance")
    run = run_historical_fixture(fixture)
    return HistoricalLessonSession(lesson, run)
