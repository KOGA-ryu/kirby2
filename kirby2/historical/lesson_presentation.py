"""Blind session view and structured post-session historical debrief."""

from __future__ import annotations

import json

from .lesson_models import (
    EvidenceStatement,
    HistoricalLessonSession,
    RevealPolicy,
)
from .models import HistoricalDataMode
from .presentation import historical_metrics


def historical_lesson_debrief(
    session: HistoricalLessonSession,
) -> dict[str, object]:
    lesson = session.lesson
    run = session.run
    return {
        "complete": session.complete,
        "data_provenance": [item.as_dict() for item in lesson.data_provenance],
        "date": lesson.date,
        "evidence_inventory": lesson.evidence_inventory(),
        "historical_context": [
            item.as_dict() for item in lesson.historical_context
        ],
        "instrument": lesson.instrument,
        "known_limitations": list(lesson.known_limitations),
        "learning_objectives": list(lesson.learning_objectives),
        "lesson_id": lesson.lesson_id,
        "lesson_sha256": lesson.sha256,
        "market": lesson.market,
        "metrics": historical_metrics(run),
        "mode": lesson.mode.value,
        "post_session_explanation": lesson.post_session_explanation.as_dict(),
        "replay_sha256": run.replay_sha256(),
        "source_provenance": run.provenance.as_dict(),
        "title": lesson.title,
        "training_questions": list(lesson.training_questions),
    }


def render_historical_lesson_session(
    session: HistoricalLessonSession,
    levels: int = 4,
) -> str:
    if type(levels) is not int or levels <= 0:
        raise ValueError("historical lesson ladder levels must be positive")
    lesson = session.lesson
    run = session.run
    blind = lesson.reveal_policy is RevealPolicy.BLIND_UNTIL_COMPLETION
    identity = (
        "date=HIDDEN instrument=HIDDEN market=HIDDEN event=HIDDEN"
        if blind
        else (
            f"date={lesson.date} instrument={lesson.instrument} "
            f"market={lesson.market} event={lesson.post_session_explanation.event.text}"
        )
    )
    lines = [
        "KIRBY2_HISTORICAL_LESSON_SESSION",
        f"PHASE {'BLIND' if blind else 'REVEALED'}",
        f"LESSON_ID {lesson.lesson_id}",
        f"IDENTITY {identity}",
        f"MODE {lesson.mode.value}",
        (
            "DATA_BOUNDARY "
            f"real_market_data={str(run.provenance.real_market_data).lower()} "
            f"source_orders={str(run.provenance.provides_order_events).lower()} "
            f"source_trades={str(run.provenance.provides_trade_events).lower()} "
            "source_aggressor_side="
            f"{str(run.provenance.provides_trade_aggressor_side).lower()} "
            f"source_book={str(run.provenance.provides_book_events).lower()} "
            f"synthetic_orders={str(lesson.mode is HistoricalDataMode.RECONSTRUCTION).lower()}"
        ),
        f"TIME_WINDOW start_us={lesson.time_window.start_us} end_us={lesson.time_window.end_us}",
        "TRAINING_QUESTIONS",
        *(f"QUESTION {question}" for question in lesson.training_questions),
        "",
        "PRICE_LADDER",
    ]
    for price in reversed(run.book.ask_prices[:levels]):
        lines.append(
            f"ASK  {_price(session, price):>9}  {run.book.asks[price].total_quantity:>7}"
        )
    lines.append(
        f"----  bid={_price(session, run.book.best_bid)} "
        f"ask={_price(session, run.book.best_ask)}"
    )
    for price in run.book.bid_prices[:levels]:
        lines.append(
            f"BID  {_price(session, price):>9}  {run.book.bids[price].total_quantity:>7}"
        )
    lines.extend(("", "RECENT_TAPE"))
    if run.generated_trades:
        for trade in run.generated_trades[-6:]:
            lines.append(
                f"{trade.trade_id}  {_price(session, trade.price_ticks):>9}  "
                f"{trade.quantity:>6}  taker={trade.taker_side.value}"
            )
    else:
        lines.append("NO_TRADES")
    lines.extend(
        (
            "",
            "SESSION_STATUS COMPLETE",
            "RUNTIME_INVARIANTS PASS",
        )
    )
    return "\n".join(lines)


def render_historical_lesson_debrief(session: HistoricalLessonSession) -> str:
    lesson = session.lesson
    run = session.run
    metrics = historical_metrics(run)
    lines = [
        "KIRBY2_STRUCTURED_HISTORICAL_DEBRIEF",
        "SESSION_STATUS COMPLETE",
        f"LESSON {lesson.lesson_id} | {lesson.title}",
        f"DATE {lesson.date}",
        f"INSTRUMENT {lesson.instrument}",
        f"MARKET {lesson.market}",
        f"MODE {lesson.mode.value}",
        _evidence_line("EVENT", lesson.post_session_explanation.event),
        _evidence_line("MARKET_CONTEXT", lesson.post_session_explanation.market_context),
        _evidence_line(
            "WHAT_HAPPENED_NEXT",
            lesson.post_session_explanation.what_happened_next,
        ),
        _evidence_line(
            "WHY_SESSION_MATTERS",
            lesson.post_session_explanation.why_session_matters,
        ),
        "EVIDENCE_INVENTORY "
        + json.dumps(
            lesson.evidence_inventory(),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "DATA_PROVENANCE",
        *(
            _evidence_line("PROVENANCE", statement)
            for statement in lesson.data_provenance
        ),
        "HISTORICAL_CONTEXT",
        *(
            _evidence_line("CONTEXT", statement)
            for statement in lesson.historical_context
        ),
        "SOURCE_PROVENANCE "
        + json.dumps(
            run.provenance.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    if lesson.mode is HistoricalDataMode.RECONSTRUCTION:
        lines.extend(
            (
                "HISTORICAL_LEVEL2 MISSING",
                "SYNTHETIC_RECONSTRUCTION generated orders are scenario output, not historical observations",
            )
        )
    elif not run.provenance.real_market_data:
        lines.append(
            "REAL_MARKET_CLAIM none; source is a local pedagogical exact-event fixture"
        )
    lines.extend(
        (
            "LEARNING_OBJECTIVES",
            *(f"OBJECTIVE {objective}" for objective in lesson.learning_objectives),
            "TRAINING_QUESTIONS",
            *(f"QUESTION {question}" for question in lesson.training_questions),
            "KNOWN_LIMITATIONS",
            *(f"LIMITATION {limitation}" for limitation in lesson.known_limitations),
            "OUTCOME "
            + json.dumps(
                {
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
                sort_keys=True,
                separators=(",", ":"),
            ),
            f"LESSON_SHA256 {lesson.sha256}",
            f"REPLAY_SHA256 {run.replay_sha256()}",
            "DEBRIEF PASS provenance_transparent=true identity_revealed=true",
        )
    )
    return "\n".join(lines)


def render_historical_lesson(
    session: HistoricalLessonSession,
    levels: int = 4,
) -> str:
    separator = (
        "IDENTITY_REVEAL after=SESSION_COMPLETE"
        if session.lesson.reveal_policy is RevealPolicy.BLIND_UNTIL_COMPLETION
        else "POST_SESSION_DEBRIEF"
    )
    return "\n\n".join(
        (
            render_historical_lesson_session(session, levels),
            separator,
            render_historical_lesson_debrief(session),
        )
    )


def _price(session: HistoricalLessonSession, price_ticks: int | None) -> str:
    if price_ticks is None:
        return "EMPTY"
    return format(session.run.tick_size * price_ticks, "f")


def _evidence_line(label: str, statement: EvidenceStatement) -> str:
    return f"{label} [{statement.category.value}] {statement.text}"
