"""Phase-gated blind, reveal, and debrief presentation models."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .lesson_models import EvidenceStatement
from .lesson_runtime import HistoricalLessonSession, LessonPhase
from .models import HistoricalDataMode
from .presentation import historical_metrics


@dataclass(frozen=True, slots=True)
class BlindLessonPresentation:
    lesson_id: str
    phase: LessonPhase
    historical_mode: HistoricalDataMode
    simulation_time_us: int
    duration_us: int
    paused: bool
    source_command_index: int
    source_command_count: int
    data_boundary: dict[str, object]
    book: dict[str, object]
    trades: tuple[dict[str, object], ...]
    questions: tuple[dict[str, object], ...]
    counterfactuals: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "book": self.book,
            "counterfactuals": list(self.counterfactuals),
            "data_boundary": self.data_boundary,
            "duration_us": self.duration_us,
            "historical_mode": self.historical_mode.value,
            "lesson_id": self.lesson_id,
            "paused": self.paused,
            "phase": self.phase.value,
            "questions": list(self.questions),
            "simulation_time_us": self.simulation_time_us,
            "source_command_count": self.source_command_count,
            "source_command_index": self.source_command_index,
            "trades": list(self.trades),
        }


@dataclass(frozen=True, slots=True)
class RevealedLessonPresentation:
    lesson_id: str
    phase: LessonPhase
    title: str
    date: str
    instrument: str
    market: str
    event: dict[str, str]
    source_provenance: dict[str, object]
    outcome: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "date": self.date,
            "event": self.event,
            "instrument": self.instrument,
            "lesson_id": self.lesson_id,
            "market": self.market,
            "outcome": self.outcome,
            "phase": self.phase.value,
            "source_provenance": self.source_provenance,
            "title": self.title,
        }


def blind_lesson_presentation(
    session: HistoricalLessonSession,
    levels: int = 4,
) -> BlindLessonPresentation:
    if type(levels) is not int or levels <= 0:
        raise ValueError("historical lesson ladder levels must be positive")
    run = session.run
    answers = {item.question_index: item for item in session.responses}
    return BlindLessonPresentation(
        lesson_id=session.lesson.lesson_id,
        phase=session.phase,
        historical_mode=session.lesson.mode,
        simulation_time_us=session.current_time_us,
        duration_us=run.duration_us,
        paused=session.paused,
        source_command_index=session.source_command_index,
        source_command_count=session.source_command_count,
        data_boundary={
            "book_evidence": session.queue_evidence_label,
            "real_market_data": run.provenance.real_market_data,
            "source_aggressor_side": run.provenance.provides_trade_aggressor_side,
            "source_book": run.provenance.provides_book_events,
            "source_orders": run.provenance.provides_order_events,
            "source_trades": run.provenance.provides_trade_events,
            "trade_evidence": session.trade_evidence_label,
        },
        book=session.visible_book(levels),
        trades=session.visible_trades(),
        questions=tuple(
            {
                "answered": index in answers,
                "question": question,
                "question_index": index,
                "response_frozen": session.responses_frozen,
            }
            for index, question in enumerate(
                session.lesson.training_questions,
                start=1,
            )
        ),
        counterfactuals=tuple(
            {
                "command": item.command,
                "counterfactual_sequence": item.sequence,
                "fill_count": len(item.fills),
                "overlay_sha256": item.overlay_sha256,
                "provenance": item.provenance,
                "trade_count": len(item.trades),
            }
            for item in session.counterfactuals
        ),
    )


def revealed_lesson_presentation(
    session: HistoricalLessonSession,
) -> RevealedLessonPresentation:
    if not session.identity_revealed:
        raise RuntimeError("lesson identity remains protected until REVEALED")
    lesson = session.lesson
    metrics = historical_metrics(session.run)
    return RevealedLessonPresentation(
        lesson_id=lesson.lesson_id,
        phase=session.phase,
        title=lesson.title,
        date=lesson.date,
        instrument=lesson.instrument,
        market=lesson.market,
        event=lesson.post_session_explanation.event.as_dict(),
        source_provenance=session.run.provenance.as_dict(),
        outcome={
            key: metrics[key]
            for key in (
                "ending_best_ask_ticks",
                "ending_best_bid_ticks",
                "exchange_event_count",
                "replay_sha256",
                "trade_count",
                "traded_volume",
            )
        },
    )


def historical_lesson_debrief(
    session: HistoricalLessonSession,
) -> dict[str, object]:
    if not session.debrief_available:
        raise RuntimeError("historical debrief requires the DEBRIEFED phase")
    lesson = session.lesson
    run = session.run
    return {
        "complete": session.complete,
        "counterfactuals": [item.as_dict() for item in session.counterfactuals],
        "data_provenance": [item.as_dict() for item in lesson.data_provenance],
        "date": lesson.date,
        "evidence_inventory": lesson.evidence_inventory(),
        "historical_context": [item.as_dict() for item in lesson.historical_context],
        "instrument": lesson.instrument,
        "known_limitations": list(lesson.known_limitations),
        "learning_objectives": list(lesson.learning_objectives),
        "lesson_id": lesson.lesson_id,
        "lesson_sha256": lesson.sha256,
        "market": lesson.market,
        "metrics": historical_metrics(run),
        "mode": lesson.mode.value,
        "phase": session.phase.value,
        "post_session_explanation": lesson.post_session_explanation.as_dict(),
        "response_comparisons": [
            item.as_dict() for item in session.response_comparisons()
        ],
        "responses": [item.as_dict() for item in session.responses],
        "result_sha256": session.result_sha256(),
        "source_provenance": run.provenance.as_dict(),
        "source_replay_sha256": run.replay_sha256(),
        "title": lesson.title,
        "training_questions": list(lesson.training_questions),
    }


def render_historical_lesson_session(
    session: HistoricalLessonSession,
    levels: int = 4,
) -> str:
    model = blind_lesson_presentation(session, levels)
    book = model.book
    lines = [
        "KIRBY2_HISTORICAL_LESSON_SESSION",
        f"PHASE {model.phase.value}",
        f"LESSON_ID {model.lesson_id}",
        "IDENTITY date=HIDDEN instrument=HIDDEN market=HIDDEN event=HIDDEN",
        f"MODE {model.historical_mode.value}",
        "DATA_BOUNDARY "
        + " ".join(
            f"{key}={str(value).lower() if isinstance(value, bool) else value}"
            for key, value in sorted(model.data_boundary.items())
        ),
        (
            f"SOURCE_PROGRESS time_us={model.simulation_time_us}/{model.duration_us} "
            f"commands={model.source_command_index}/{model.source_command_count}"
        ),
        f"PLAYBACK {'PAUSED' if model.paused else 'ACTIVE'}",
        f"BOOK_EVIDENCE {book['provenance']}",
        "",
        "PRICE_LADDER",
    ]
    if book["provenance"] == "UNAVAILABLE":
        lines.append("UNAVAILABLE source has no order-level or book-state evidence")
    else:
        for level in reversed(book["asks"]):
            lines.append(
                f"ASK  {_price(session, int(level['price_ticks'])):>9}  "
                f"{int(level['quantity']):>7}"
            )
        lines.append(
            f"----  bid={_price(session, book['best_bid_ticks'])} "
            f"ask={_price(session, book['best_ask_ticks'])}"
        )
        for level in book["bids"]:
            lines.append(
                f"BID  {_price(session, int(level['price_ticks'])):>9}  "
                f"{int(level['quantity']):>7}"
            )
    lines.extend(("", "VISIBLE_TAPE"))
    if model.trades:
        for trade in model.trades:
            side = (
                " unavailable"
                if trade["taker_side"] is None
                else f" taker={trade['taker_side']}"
            )
            lines.append(
                f"{trade['trade_id']}  "
                f"{_price(session, int(trade['price_ticks'])):>9}  "
                f"{int(trade['quantity']):>6}{side}  "
                f"[{trade['provenance']}]"
            )
    else:
        lines.append("NO_SUPPORTED_VISIBLE_TRADES")
    lines.extend(("", "TRAINING_QUESTIONS"))
    for question in model.questions:
        lines.append(
            f"QUESTION {question['question_index']} "
            f"answered={str(question['answered']).lower()} "
            f"frozen={str(question['response_frozen']).lower()} "
            f"{question['question']}"
        )
    if model.counterfactuals:
        lines.append("COUNTERFACTUAL_OVERLAYS")
        for item in model.counterfactuals:
            lines.append(
                "COUNTERFACTUAL "
                + json.dumps(item, sort_keys=True, separators=(",", ":"))
            )
    lines.extend(
        (
            "",
            f"SESSION_STATUS {'COMPLETE' if session.complete else 'INCOMPLETE'}",
            f"RESPONSES_FROZEN {str(session.responses_frozen).lower()}",
            "RUNTIME_INVARIANTS PASS source_authoritative=true",
        )
    )
    return "\n".join(lines)


def render_revealed_lesson(session: HistoricalLessonSession) -> str:
    model = revealed_lesson_presentation(session)
    return "\n".join(
        (
            "KIRBY2_HISTORICAL_LESSON_REVEAL",
            f"PHASE {model.phase.value}",
            f"LESSON {model.lesson_id} | {model.title}",
            f"DATE {model.date}",
            f"INSTRUMENT {model.instrument}",
            f"MARKET {model.market}",
            f"EVENT [{model.event['category']}] {model.event['text']}",
            "OUTCOME "
            + json.dumps(model.outcome, sort_keys=True, separators=(",", ":")),
        )
    )


def render_historical_lesson_debrief(session: HistoricalLessonSession) -> str:
    payload = historical_lesson_debrief(session)
    lesson = session.lesson
    run = session.run
    lines = [
        "KIRBY2_STRUCTURED_HISTORICAL_DEBRIEF",
        f"PHASE {session.phase.value}",
        "SESSION_STATUS COMPLETE",
        f"LESSON {lesson.lesson_id} | {lesson.title}",
        f"DATE {lesson.date}",
        f"INSTRUMENT {lesson.instrument}",
        f"MARKET {lesson.market}",
        f"MODE {lesson.mode.value}",
        _evidence_line("EVENT", lesson.post_session_explanation.event),
        _evidence_line("MARKET_CONTEXT", lesson.post_session_explanation.market_context),
        _evidence_line("WHAT_HAPPENED_NEXT", lesson.post_session_explanation.what_happened_next),
        _evidence_line("WHY_SESSION_MATTERS", lesson.post_session_explanation.why_session_matters),
        "RESPONSE_COMPARISONS",
        *(
            "COMPARISON "
            + json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in payload["response_comparisons"]
        ),
        "EVIDENCE_INVENTORY "
        + json.dumps(lesson.evidence_inventory(), sort_keys=True, separators=(",", ":")),
        "DATA_PROVENANCE",
        *(_evidence_line("PROVENANCE", item) for item in lesson.data_provenance),
        "SOURCE_PROVENANCE "
        + json.dumps(run.provenance.as_dict(), sort_keys=True, separators=(",", ":")),
    ]
    if lesson.mode is HistoricalDataMode.RECONSTRUCTION:
        lines.extend(
            (
                "HISTORICAL_LEVEL2 MISSING",
                "SYNTHETIC_RECONSTRUCTION generated orders are scenario output, not historical observations",
            )
        )
    lines.extend(
        (
            f"LESSON_SHA256 {lesson.sha256}",
            f"SOURCE_REPLAY_SHA256 {run.replay_sha256()}",
            f"RESULT_SHA256 {session.result_sha256()}",
            "DEBRIEF PASS responses_frozen=true provenance_transparent=true identity_revealed=true",
        )
    )
    return "\n".join(lines)


def render_historical_lesson(
    session: HistoricalLessonSession,
    levels: int = 4,
) -> str:
    sections = [render_historical_lesson_session(session, levels)]
    if session.identity_revealed:
        sections.append(render_revealed_lesson(session))
    if session.debrief_available:
        sections.append(render_historical_lesson_debrief(session))
    return "\n\n".join(sections)


def _price(session: HistoricalLessonSession, price_ticks: object) -> str:
    if price_ticks is None:
        return "EMPTY"
    return format(session.run.tick_size * int(price_ticks), "f")


def _evidence_line(label: str, statement: EvidenceStatement) -> str:
    return f"{label} [{statement.category.value}] {statement.text}"
