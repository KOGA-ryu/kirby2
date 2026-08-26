"""Historical exact-replay and reconstruction foundation."""

from .fixtures import (
    load_exact_fixture,
    load_historical_fixtures,
    load_reconstruction_fixture,
)
from .lesson_catalog import load_historical_lesson, load_historical_lessons
from .lesson_models import (
    HISTORICAL_LESSON_SCHEMA_VERSION,
    EvidenceCategory,
    EvidenceStatement,
    HistoricalDebrief,
    HistoricalLesson,
    HistoricalLessonSession,
    LessonSource,
    LessonTimeWindow,
    RevealPolicy,
)
from .lesson_presentation import (
    historical_lesson_debrief,
    render_historical_lesson,
    render_historical_lesson_debrief,
    render_historical_lesson_session,
)
from .lesson_runner import run_historical_lesson
from .models import (
    ExactOrderMessage,
    ExactReplayFixture,
    ExpectedTrade,
    HistoricalCommandRecord,
    HistoricalConstraints,
    HistoricalDataMode,
    HistoricalProvenance,
    HistoricalRun,
    ReconstructionFixture,
    SpreadObservation,
    TradePrintObservation,
)
from .presentation import (
    RECONSTRUCTION_DISCLOSURE,
    RECONSTRUCTION_TITLE,
    historical_metrics,
    render_historical_report,
    render_historical_ui,
)
from .runner import run_exact_replay, run_historical_fixture, run_reconstruction

__all__ = [
    "ExactOrderMessage",
    "ExactReplayFixture",
    "ExpectedTrade",
    "HistoricalCommandRecord",
    "HistoricalConstraints",
    "HistoricalDataMode",
    "HistoricalProvenance",
    "HistoricalRun",
    "HISTORICAL_LESSON_SCHEMA_VERSION",
    "EvidenceCategory",
    "EvidenceStatement",
    "HistoricalDebrief",
    "HistoricalLesson",
    "HistoricalLessonSession",
    "LessonSource",
    "LessonTimeWindow",
    "RECONSTRUCTION_DISCLOSURE",
    "RECONSTRUCTION_TITLE",
    "ReconstructionFixture",
    "RevealPolicy",
    "SpreadObservation",
    "TradePrintObservation",
    "historical_metrics",
    "historical_lesson_debrief",
    "load_exact_fixture",
    "load_historical_lesson",
    "load_historical_lessons",
    "load_historical_fixtures",
    "load_reconstruction_fixture",
    "render_historical_report",
    "render_historical_lesson",
    "render_historical_lesson_debrief",
    "render_historical_lesson_session",
    "render_historical_ui",
    "run_exact_replay",
    "run_historical_fixture",
    "run_historical_lesson",
    "run_reconstruction",
]
