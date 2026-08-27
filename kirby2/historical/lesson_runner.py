"""Create and deterministically replay interactive historical lesson sessions."""

from __future__ import annotations

import json

from .fixtures import load_historical_fixtures
from .lesson_models import HistoricalLesson
from .lesson_runtime import (
    HistoricalLessonSession,
    LessonAction,
    LessonSessionInput,
)
from .runner import run_historical_fixture


def run_historical_lesson(lesson: HistoricalLesson) -> HistoricalLessonSession:
    """Create a READY, incomplete lesson session without revealing its outcome."""

    fixtures = load_historical_fixtures()
    fixture = fixtures.get(lesson.source.fixture_id)
    if fixture is None:
        raise ValueError(f"unknown historical lesson fixture: {lesson.source.fixture_id}")
    if lesson.source.source_locator != fixture.provenance.source_locator:
        raise ValueError("historical lesson source locator does not match fixture provenance")
    run = run_historical_fixture(fixture)
    return HistoricalLessonSession(lesson, run)


def replay_historical_lesson_session(
    lesson: HistoricalLesson,
    inputs: tuple[LessonSessionInput, ...],
) -> HistoricalLessonSession:
    if tuple(item.sequence for item in inputs) != tuple(range(1, len(inputs) + 1)):
        raise ValueError("lesson replay inputs must be contiguous")
    session = run_historical_lesson(lesson)
    for item in inputs:
        apply_lesson_input(session, item)
    if tuple(item.as_dict() for item in session.inputs) != tuple(
        item.as_dict() for item in inputs
    ):
        raise RuntimeError("lesson input replay diverged from the recorded commands")
    return session


def replay_historical_lesson_json_lines(
    lesson: HistoricalLesson,
    json_lines: str,
) -> HistoricalLessonSession:
    """Replay and validate a complete canonical session JSONL artifact."""

    try:
        records = tuple(json.loads(line) for line in json_lines.splitlines() if line)
    except json.JSONDecodeError as error:
        raise ValueError("lesson replay stream contains invalid JSON") from error
    if not records or any(not isinstance(record, dict) for record in records):
        raise ValueError("lesson replay stream must contain JSON objects")
    header = records[0]
    if (
        header.get("record_type") != "LESSON_SESSION_HEADER"
        or header.get("lesson_id") != lesson.lesson_id
        or header.get("lesson_sha256") != lesson.sha256
    ):
        raise ValueError("lesson replay header does not match the selected lesson")
    inputs = tuple(
        LessonSessionInput.from_dict(record)
        for record in records
        if record.get("record_type") == "LESSON_INPUT"
    )
    session = replay_historical_lesson_session(lesson, inputs)
    canonical_source = "\n".join(
        json.dumps(record, sort_keys=True, separators=(",", ":"))
        for record in records
    )
    if canonical_source != session.replay_json_lines():
        raise RuntimeError("lesson replay artifact diverged from reproduced session")
    return session


def apply_lesson_input(
    session: HistoricalLessonSession,
    item: LessonSessionInput,
) -> None:
    arguments = item.arguments
    expected_sequence = len(session.inputs) + 1
    if item.sequence != expected_sequence:
        raise ValueError(
            "lesson input sequence mismatch: "
            f"expected {expected_sequence}, got {item.sequence}"
        )
    no_argument_actions = {
        LessonAction.START,
        LessonAction.STEP,
        LessonAction.PAUSE,
        LessonAction.RESUME,
        LessonAction.FREEZE_RESPONSES,
        LessonAction.REVEAL,
        LessonAction.DEBRIEF,
    }
    if item.action in no_argument_actions and arguments:
        raise ValueError(f"{item.action.value} does not accept arguments")
    if item.action is LessonAction.START:
        session.start()
    elif item.action is LessonAction.ADVANCE_TO:
        _require_argument_keys(arguments, {"simulation_time_us"}, item.action)
        simulation_time_us = arguments["simulation_time_us"]
        if type(simulation_time_us) is not int:
            raise TypeError("ADVANCE_TO simulation_time_us must be an integer")
        session.advance_to(simulation_time_us)
    elif item.action is LessonAction.STEP:
        session.step()
    elif item.action is LessonAction.PAUSE:
        session.pause()
    elif item.action is LessonAction.RESUME:
        session.resume()
    elif item.action is LessonAction.ANSWER:
        _require_argument_keys(
            arguments,
            {"question_index", "response"},
            item.action,
        )
        question_index = arguments["question_index"]
        response = arguments["response"]
        if type(question_index) is not int or not isinstance(response, str):
            raise TypeError("ANSWER requires integer question_index and text response")
        session.answer(
            question_index,
            response,
        )
    elif item.action is LessonAction.FREEZE_RESPONSES:
        session.freeze_responses()
    elif item.action is LessonAction.REVEAL:
        session.reveal()
    elif item.action is LessonAction.DEBRIEF:
        session.debrief()
    elif item.action is LessonAction.COUNTERFACTUAL_ORDER:
        _require_argument_keys(
            arguments,
            {"order_type", "price_ticks", "quantity", "side"},
            item.action,
        )
        order_type = arguments["order_type"]
        side = arguments["side"]
        quantity = arguments["quantity"]
        price_ticks = arguments["price_ticks"]
        if not isinstance(order_type, str) or not isinstance(side, str):
            raise TypeError("counterfactual order type and side must be text")
        if type(quantity) is not int:
            raise TypeError("counterfactual quantity must be an integer")
        if price_ticks is not None and type(price_ticks) is not int:
            raise TypeError("counterfactual price_ticks must be an integer or null")
        session.submit_counterfactual(
            order_type=order_type,
            side=side,
            quantity=quantity,
            price_ticks=price_ticks,
        )
    else:
        raise ValueError(f"unsupported lesson input action {item.action.value}")


def _require_argument_keys(
    arguments: dict[str, object],
    expected: set[str],
    action: LessonAction,
) -> None:
    if set(arguments) != expected:
        raise ValueError(
            f"{action.value} arguments must be exactly {sorted(expected)}"
        )
