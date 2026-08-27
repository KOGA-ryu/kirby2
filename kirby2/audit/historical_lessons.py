"""Executable audit for interactive blind historical lesson sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from kirby2.historical import (
    HistoricalLessonSession,
    LessonAction,
    LessonPhase,
    LessonSessionInput,
    apply_lesson_input,
    blind_lesson_presentation,
    historical_lesson_debrief,
    load_historical_lessons,
    replay_historical_lesson_session,
    replay_historical_lesson_json_lines,
    revealed_lesson_presentation,
    run_historical_lesson,
)


@dataclass(frozen=True, slots=True)
class HistoricalLessonAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_historical_lessons() -> tuple[HistoricalLessonAuditCase, ...]:
    lessons = load_historical_lessons()
    exact = lessons["exact_queue_priority"]
    reconstruction = lessons["reconstruction_liquidity_decision"]
    return (
        _ready_blind_boundary_case(exact),
        _lifecycle_and_guard_case(exact),
        _deterministic_input_replay_case(exact),
        _source_authority_and_overlay_case(exact),
        _capability_truth_case(exact, reconstruction),
        _packaged_lessons_case(lessons),
    )


def _ready_blind_boundary_case(lesson) -> HistoricalLessonAuditCase:
    session = run_historical_lesson(lesson)
    model = blind_lesson_presentation(session)
    payload = json.dumps(model.as_dict(), sort_keys=True)
    protected = (
        lesson.title,
        lesson.date,
        lesson.instrument,
        lesson.market,
        lesson.post_session_explanation.event.text,
    )
    failures: list[str] = []
    if session.phase is not LessonPhase.READY or session.complete:
        failures.append("new lesson did not start incomplete in READY")
    if any(value in payload for value in protected):
        failures.append("blind presentation leaked protected identity or event text")
    forbidden_fields = {
        "date",
        "event",
        "instrument",
        "market",
        "outcome",
        "run",
        "title",
    }
    if forbidden_fields & set(model.__dataclass_fields__):
        failures.append("blind presentation type exposes a reveal-only field")
    reveal_guard = _raises(lambda: revealed_lesson_presentation(session))
    debrief_guard = _raises(lambda: historical_lesson_debrief(session))
    if not reveal_guard or not debrief_guard:
        failures.append("reveal or debrief was accessible from READY")
    return HistoricalLessonAuditCase(
        "ready_is_incomplete_and_blind_safe",
        {
            "blind_fields": sorted(model.__dataclass_fields__),
            "debrief_guarded": debrief_guard,
            "phase": session.phase.value,
            "protected_values_absent": not any(value in payload for value in protected),
            "reveal_guarded": reveal_guard,
        },
        tuple(failures),
    )


def _lifecycle_and_guard_case(lesson) -> HistoricalLessonAuditCase:
    session = run_historical_lesson(lesson)
    failures: list[str] = []
    sequence_guard = _raises(
        lambda: apply_lesson_input(
            run_historical_lesson(lesson),
            LessonSessionInput(2, LessonAction.START, {}),
        )
    )
    argument_type_guard = _raises(
        lambda: apply_lesson_input(
            run_historical_lesson(lesson),
            LessonSessionInput(
                1,
                LessonAction.ADVANCE_TO,
                {"simulation_time_us": True},
            ),
        )
    )
    session.start()
    session.pause()
    paused_step_guard = _raises(session.step)
    session.resume()
    session.step()
    session.advance_to(session.run.duration_us)
    premature_reveal_guard = _raises(session.reveal)
    missing_answers_guard = _raises(session.freeze_responses)
    for index, question in enumerate(lesson.training_questions, start=1):
        session.answer(index, f"Decision-time response {index}: {question}")
    session.freeze_responses()
    pre_reveal_debrief_guard = _raises(session.debrief)
    session.reveal()
    session.debrief()
    phases = tuple(
        [session.session_events[0].phase_before.value]
        + [event.phase_after.value for event in session.session_events]
    )
    expected = {
        phase.value
        for phase in (
            LessonPhase.READY,
            LessonPhase.BLIND_RUNNING,
            LessonPhase.QUESTIONS,
            LessonPhase.COMPLETE,
            LessonPhase.REVEALED,
            LessonPhase.DEBRIEFED,
        )
    }
    if not paused_step_guard:
        failures.append("paused playback accepted a step")
    if not premature_reveal_guard or not pre_reveal_debrief_guard:
        failures.append("reveal/debrief phase guard failed")
    if not missing_answers_guard:
        failures.append("responses froze before every question was answered")
    if not sequence_guard or not argument_type_guard:
        failures.append("malformed or out-of-sequence input was accepted")
    if set(phases) != expected or session.phase is not LessonPhase.DEBRIEFED:
        failures.append("lesson did not traverse every required phase")
    if any(response.observable_context["phase"] != "QUESTIONS" for response in session.responses):
        failures.append("response omitted question-time observable context")
    return HistoricalLessonAuditCase(
        "step_pause_questions_freeze_reveal_debrief",
        {
            "event_count": len(session.session_events),
            "input_count": len(session.inputs),
            "malformed_input_guarded": argument_type_guard,
            "phases": list(phases),
            "response_count": len(session.responses),
            "result_sha256": session.result_sha256(),
            "sequence_guarded": sequence_guard,
        },
        tuple(failures),
    )


def _deterministic_input_replay_case(lesson) -> HistoricalLessonAuditCase:
    first, first_states = _drive_script(lesson)
    second, second_states = _drive_script(lesson)
    replayed = replay_historical_lesson_session(lesson, first.inputs)
    replayed_jsonl = replay_historical_lesson_json_lines(
        lesson,
        first.replay_json_lines(),
    )
    tampered_records = [
        json.loads(line) for line in first.replay_json_lines().splitlines()
    ]
    tampered_records[-1]["result_sha256"] = "0" * 64
    tamper_guard = _raises(
        lambda: replay_historical_lesson_json_lines(
            lesson,
            "\n".join(json.dumps(record) for record in tampered_records),
        )
    )
    failures: list[str] = []
    if first.inputs != second.inputs:
        failures.append("identical script produced different recorded inputs")
    if first.session_events != second.session_events:
        failures.append("identical script produced different session events")
    if first.responses != second.responses:
        failures.append("identical script produced different responses")
    if first_states != second_states:
        failures.append("identical script produced different presentation states")
    if first.as_dict() != replayed.as_dict():
        failures.append("recorded lesson inputs did not reproduce canonical state")
    if first.result_sha256() != second.result_sha256() or first.result_sha256() != replayed.result_sha256():
        failures.append("lesson replay result digest diverged")
    if first.replay_json_lines() != replayed.replay_json_lines():
        failures.append("lesson replay stream was not byte-identical")
    if replayed_jsonl.result_sha256() != first.result_sha256():
        failures.append("canonical JSONL artifact did not reproduce its result")
    if not tamper_guard:
        failures.append("tampered canonical session artifact was accepted")
    return HistoricalLessonAuditCase(
        "same_inputs_same_events_presentations_and_digest",
        {
            "input_count": len(first.inputs),
            "presentation_state_count": len(first_states),
            "replay_bytes": len(first.replay_json_lines().encode("utf-8")),
            "result_sha256": first.result_sha256(),
            "tamper_guarded": tamper_guard,
        },
        tuple(failures),
    )


def _source_authority_and_overlay_case(lesson) -> HistoricalLessonAuditCase:
    session = run_historical_lesson(lesson)
    source_before = session.run.replay_sha256()
    source_events_before = session.run.book.journal.canonical_json_lines()
    session.start()
    overlay = session.submit_counterfactual("market", "buy", 50)
    source_after = session.run.replay_sha256()
    failures: list[str] = []
    if source_before != source_after:
        failures.append("counterfactual changed the authoritative source digest")
    if source_events_before != session.run.book.journal.canonical_json_lines():
        failures.append("counterfactual entered the authoritative source journal")
    if overlay.source_replay_sha256 != source_before:
        failures.append("counterfactual omitted its authoritative parent digest")
    if overlay.provenance != "COUNTERFACTUAL_ON_SOURCE_DERIVED_BOOK":
        failures.append("exact counterfactual provenance was mislabeled")
    if not overlay.fills or not overlay.trades:
        failures.append("executable counterfactual did not expose its separate fills")
    return HistoricalLessonAuditCase(
        "counterfactual_overlay_preserves_source",
        {
            "counterfactual_fill_count": len(overlay.fills),
            "counterfactual_trade_count": len(overlay.trades),
            "overlay_sha256": overlay.overlay_sha256,
            "provenance": overlay.provenance,
            "source_replay_sha256": source_after,
        },
        tuple(failures),
    )


def _capability_truth_case(exact_lesson, reconstruction_lesson) -> HistoricalLessonAuditCase:
    exact = run_historical_lesson(exact_lesson)
    exact.start()
    exact.advance_to(2_000_000)
    partial_side_run = replace(
        exact.run,
        provenance=replace(
            exact.run.provenance,
            provides_trade_aggressor_side=False,
        ),
    )
    partial_side = HistoricalLessonSession(exact_lesson, partial_side_run)
    partial_side.start()
    partial_side.advance_to(2_000_000)
    observation_run = replace(
        exact.run,
        provenance=replace(
            exact.run.provenance,
            provides_order_events=False,
            provides_book_events=False,
        ),
    )
    observation = HistoricalLessonSession(exact_lesson, observation_run)
    observation.start()
    unsupported_overlay = observation.submit_counterfactual("market", "buy", 10)
    reconstruction = run_historical_lesson(reconstruction_lesson)
    reconstruction.start()
    synthetic_overlay = reconstruction.submit_counterfactual("market", "buy", 10)
    failures: list[str] = []
    exact_trades = exact.visible_trades(limit=100)
    partial_trades = partial_side.visible_trades(limit=100)
    if exact.queue_evidence_label != "DERIVED_FROM_SOURCE" or not exact.visible_book()["bids"]:
        failures.append("exact order source did not expose source-derived queues")
    if not exact_trades or any(item["taker_side"] is None for item in exact_trades):
        failures.append("supported exact aggressor side was hidden")
    if not partial_trades or any(item["taker_side"] is not None for item in partial_trades):
        failures.append("unsupported aggressor side was exposed or represented as data")
    if observation.visible_book()["provenance"] != "UNAVAILABLE":
        failures.append("orderless observation source claimed genuine queue evidence")
    if (
        unsupported_overlay.provenance
        != "COUNTERFACTUAL_SYNTHETIC_FROM_UNSUPPORTED_BOOK_STATE"
    ):
        failures.append("unsupported hidden book counterfactual claimed source queue truth")
    if reconstruction.queue_evidence_label != "SYNTHETIC_RECONSTRUCTION":
        failures.append("reconstructed queue was not explicitly synthetic")
    if synthetic_overlay.provenance != "COUNTERFACTUAL_ON_SYNTHETIC_RECONSTRUCTION":
        failures.append("reconstruction counterfactual lost its synthetic boundary")
    return HistoricalLessonAuditCase(
        "exact_partial_and_reconstruction_capability_truth",
        {
            "exact_queue_evidence": exact.queue_evidence_label,
            "observation_queue_evidence": observation.queue_evidence_label,
            "partial_trade_side": partial_trades[-1]["taker_side"],
            "reconstruction_queue_evidence": reconstruction.queue_evidence_label,
            "reconstruction_overlay_provenance": synthetic_overlay.provenance,
            "unsupported_overlay_provenance": unsupported_overlay.provenance,
        },
        tuple(failures),
    )


def _packaged_lessons_case(lessons) -> HistoricalLessonAuditCase:
    digests: dict[str, str] = {}
    failures: list[str] = []
    for lesson_id in sorted(lessons):
        first, _ = _drive_script(lessons[lesson_id], with_counterfactual=False)
        second, _ = _drive_script(lessons[lesson_id], with_counterfactual=False)
        first.run.book.assert_invariants()
        if first.phase is not LessonPhase.DEBRIEFED:
            failures.append(f"{lesson_id} did not reach DEBRIEFED")
        if first.result_sha256() != second.result_sha256():
            failures.append(f"{lesson_id} was nondeterministic")
        digests[lesson_id] = first.result_sha256()
    return HistoricalLessonAuditCase(
        "all_packaged_lessons_complete_deterministically",
        {"lesson_count": len(lessons), "result_sha256": digests},
        tuple(failures),
    )


def _drive_script(lesson, *, with_counterfactual: bool = True):
    session = run_historical_lesson(lesson)
    actions: list[tuple[LessonAction, dict[str, object]]] = [
        (LessonAction.START, {}),
        (LessonAction.PAUSE, {}),
        (LessonAction.RESUME, {}),
        (LessonAction.STEP, {}),
    ]
    if with_counterfactual:
        actions.append(
            (
                LessonAction.COUNTERFACTUAL_ORDER,
                {
                    "order_type": "market",
                    "price_ticks": None,
                    "quantity": 25,
                    "side": "buy",
                },
            )
        )
    actions.append(
        (
            LessonAction.ADVANCE_TO,
            {"simulation_time_us": lesson.time_window.end_us},
        )
    )
    actions.extend(
        (
            LessonAction.ANSWER,
            {
                "question_index": index,
                "response": f"Recorded decision response {index}: {question}",
            },
        )
        for index, question in enumerate(lesson.training_questions, start=1)
    )
    actions.extend(
        (
            (LessonAction.FREEZE_RESPONSES, {}),
            (LessonAction.REVEAL, {}),
            (LessonAction.DEBRIEF, {}),
        )
    )
    presentation_states: list[str] = []
    for sequence, (action, arguments) in enumerate(actions, start=1):
        apply_lesson_input(session, LessonSessionInput(sequence, action, arguments))
        state: dict[str, object] = {
            "blind": blind_lesson_presentation(session).as_dict(),
        }
        if session.identity_revealed:
            state["revealed"] = revealed_lesson_presentation(session).as_dict()
        if session.debrief_available:
            state["debrief"] = historical_lesson_debrief(session)
        presentation_states.append(
            json.dumps(state, sort_keys=True, separators=(",", ":"))
        )
    return session, tuple(presentation_states)


def _raises(operation) -> bool:
    try:
        operation()
    except (RuntimeError, TypeError, ValueError):
        return True
    return False
