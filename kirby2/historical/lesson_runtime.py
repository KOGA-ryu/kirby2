"""Deterministic command-driven runtime for blind historical lessons."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from kirby2.exchange import Order, OrderBook, OrderOwner, OrderType, Side

from .features import apply_historical_command
from .lesson_models import EvidenceStatement, HistoricalLesson
from .models import HistoricalDataMode, HistoricalRun


class LessonPhase(str, Enum):
    READY = "READY"
    BLIND_RUNNING = "BLIND_RUNNING"
    QUESTIONS = "QUESTIONS"
    COMPLETE = "COMPLETE"
    REVEALED = "REVEALED"
    DEBRIEFED = "DEBRIEFED"


class LessonAction(str, Enum):
    START = "START"
    ADVANCE_TO = "ADVANCE_TO"
    STEP = "STEP"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    ANSWER = "ANSWER"
    FREEZE_RESPONSES = "FREEZE_RESPONSES"
    REVEAL = "REVEAL"
    DEBRIEF = "DEBRIEF"
    COUNTERFACTUAL_ORDER = "COUNTERFACTUAL_ORDER"


@dataclass(frozen=True, slots=True)
class LessonSessionInput:
    sequence: int
    action: LessonAction
    arguments: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("lesson input sequence must be positive")
        if not isinstance(self.action, LessonAction):
            raise TypeError("lesson input action is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "arguments": self.arguments,
            "input_sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LessonSessionInput:
        action = payload.get("action")
        arguments = payload.get("arguments")
        sequence = payload.get("input_sequence")
        if not isinstance(action, str) or not isinstance(arguments, dict):
            raise ValueError("lesson input record is malformed")
        if type(sequence) is not int:
            raise ValueError("lesson input sequence must be an integer")
        try:
            parsed_action = LessonAction(action)
        except ValueError as error:
            raise ValueError(f"unknown lesson input action: {action}") from error
        return cls(sequence, parsed_action, arguments)


@dataclass(frozen=True, slots=True)
class LessonSessionEvent:
    sequence: int
    simulation_time_us: int
    action: LessonAction
    phase_before: LessonPhase
    phase_after: LessonPhase
    payload: dict[str, object]
    observable_context: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("lesson event sequence must be positive")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("lesson event time must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "lesson_event_sequence": self.sequence,
            "observable_context": self.observable_context,
            "payload": self.payload,
            "phase_after": self.phase_after.value,
            "phase_before": self.phase_before.value,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class LessonQuestionResponse:
    question_index: int
    question: str
    response: str
    simulation_time_us: int
    observable_context: dict[str, object]

    def __post_init__(self) -> None:
        if self.question_index <= 0 or not self.question or not self.response.strip():
            raise ValueError("lesson response requires a question and nonempty answer")

    def as_dict(self) -> dict[str, object]:
        return {
            "observable_context": self.observable_context,
            "question": self.question,
            "question_index": self.question_index,
            "response": self.response,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class HistoricalResponseComparison:
    question_index: int
    question: str
    response: str
    response_context: dict[str, object]
    evidence: EvidenceStatement
    shared_terms: tuple[str, ...]
    assessment: str

    def as_dict(self) -> dict[str, object]:
        return {
            "assessment": self.assessment,
            "evidence": self.evidence.as_dict(),
            "question": self.question,
            "question_index": self.question_index,
            "response": self.response,
            "response_context": self.response_context,
            "shared_terms": list(self.shared_terms),
        }


@dataclass(frozen=True, slots=True)
class CounterfactualExecution:
    sequence: int
    simulation_time_us: int
    provenance: str
    command: dict[str, object]
    decision_context: dict[str, object]
    exchange_events: tuple[dict[str, object], ...]
    fills: tuple[dict[str, object], ...]
    trades: tuple[dict[str, object], ...]
    source_replay_sha256: str
    overlay_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "counterfactual_sequence": self.sequence,
            "decision_context": self.decision_context,
            "exchange_events": list(self.exchange_events),
            "fills": list(self.fills),
            "overlay_sha256": self.overlay_sha256,
            "provenance": self.provenance,
            "simulation_time_us": self.simulation_time_us,
            "source_replay_sha256": self.source_replay_sha256,
            "trades": list(self.trades),
        }


class HistoricalLessonSession:
    """Stateful runtime whose source run remains immutable and authoritative."""

    def __init__(self, lesson: HistoricalLesson, run: HistoricalRun) -> None:
        if lesson.mode is not run.mode:
            raise ValueError("historical lesson mode does not match its run")
        if lesson.source.fixture_id != run.fixture_id:
            raise ValueError("historical lesson source does not match its run")
        if (
            lesson.time_window.start_us != 0
            or lesson.time_window.end_us != run.duration_us
        ):
            raise ValueError("historical lesson must use its complete source window")
        run.book.assert_invariants()
        self.lesson = lesson
        self._run = run
        self.phase = LessonPhase.READY
        self.current_time_us = lesson.time_window.start_us
        self.paused = False
        self.responses_frozen = False
        self._source_replay_sha256 = run.replay_sha256()
        self._source_command_index = 0
        self._view_book = OrderBook()
        self._inputs: list[LessonSessionInput] = []
        self._events: list[LessonSessionEvent] = []
        self._responses: dict[int, LessonQuestionResponse] = {}
        self._counterfactuals: list[CounterfactualExecution] = []

    @property
    def run(self) -> HistoricalRun:
        """Authoritative final source run; blind presentation models never retain it."""

        return self._run

    @property
    def complete(self) -> bool:
        return self.phase in {
            LessonPhase.COMPLETE,
            LessonPhase.REVEALED,
            LessonPhase.DEBRIEFED,
        }

    @property
    def identity_revealed(self) -> bool:
        return self.phase in {LessonPhase.REVEALED, LessonPhase.DEBRIEFED}

    @property
    def debrief_available(self) -> bool:
        return self.phase is LessonPhase.DEBRIEFED

    @property
    def inputs(self) -> tuple[LessonSessionInput, ...]:
        return tuple(self._inputs)

    @property
    def session_events(self) -> tuple[LessonSessionEvent, ...]:
        return tuple(self._events)

    @property
    def responses(self) -> tuple[LessonQuestionResponse, ...]:
        return tuple(self._responses[index] for index in sorted(self._responses))

    @property
    def counterfactuals(self) -> tuple[CounterfactualExecution, ...]:
        return tuple(self._counterfactuals)

    @property
    def source_command_index(self) -> int:
        return self._source_command_index

    @property
    def source_command_count(self) -> int:
        return len(self._run.commands)

    @property
    def queue_evidence_label(self) -> str:
        if self.lesson.mode is HistoricalDataMode.RECONSTRUCTION:
            return "SYNTHETIC_RECONSTRUCTION"
        if self._run.provenance.provides_book_events:
            return "OBSERVED"
        if self._run.provenance.provides_order_events:
            return "DERIVED_FROM_SOURCE"
        return "UNAVAILABLE"

    @property
    def trade_evidence_label(self) -> str:
        if self.lesson.mode is HistoricalDataMode.RECONSTRUCTION:
            return "SYNTHETIC_RECONSTRUCTION"
        if self._run.provenance.provides_trade_events:
            return "DERIVED_FROM_SOURCE"
        return "UNAVAILABLE"

    def start(self) -> None:
        self._require_phase(LessonPhase.READY)
        before = self.phase
        self.phase = LessonPhase.BLIND_RUNNING
        consumed = 0
        while (
            self._source_command_index < len(self._run.commands)
            and self._run.commands[self._source_command_index].simulation_time_us
            == self.current_time_us
        ):
            self._apply_next_source_command()
            consumed += 1
        self._record(
            LessonAction.START,
            {},
            before,
            {"source_commands_consumed": consumed},
        )

    def advance_to(self, simulation_time_us: int) -> None:
        self._require_running()
        if type(simulation_time_us) is not int:
            raise TypeError("lesson advance target must be integer microseconds")
        if not self.current_time_us <= simulation_time_us <= self._run.duration_us:
            raise ValueError("lesson advance target is outside the remaining source window")
        before = self.phase
        starting_index = self._source_command_index
        while (
            self._source_command_index < len(self._run.commands)
            and self._run.commands[self._source_command_index].simulation_time_us
            <= simulation_time_us
        ):
            self._apply_next_source_command()
        self.current_time_us = simulation_time_us
        self._enter_questions_if_finished()
        self._record(
            LessonAction.ADVANCE_TO,
            {"simulation_time_us": simulation_time_us},
            before,
            {
                "source_commands_consumed": (
                    self._source_command_index - starting_index
                )
            },
        )

    def advance_by(self, delta_us: int) -> None:
        if type(delta_us) is not int or delta_us < 0:
            raise ValueError("lesson advance delta must be a nonnegative integer")
        self.advance_to(min(self._run.duration_us, self.current_time_us + delta_us))

    def step(self) -> None:
        self._require_running()
        before = self.phase
        if self._source_command_index < len(self._run.commands):
            command = self._run.commands[self._source_command_index]
            self.current_time_us = command.simulation_time_us
            self._apply_next_source_command()
            consumed = 1
        else:
            self.current_time_us = self._run.duration_us
            consumed = 0
        self._enter_questions_if_finished()
        self._record(
            LessonAction.STEP,
            {},
            before,
            {"source_commands_consumed": consumed},
        )

    def pause(self) -> None:
        if self.phase is not LessonPhase.BLIND_RUNNING or self.paused:
            raise RuntimeError("only an active blind lesson can be paused")
        before = self.phase
        self.paused = True
        self._record(LessonAction.PAUSE, {}, before, {})

    def resume(self) -> None:
        if self.phase is not LessonPhase.BLIND_RUNNING or not self.paused:
            raise RuntimeError("lesson is not paused")
        before = self.phase
        self.paused = False
        self._record(LessonAction.RESUME, {}, before, {})

    def answer(self, question_index: int, response: str) -> None:
        self._require_phase(LessonPhase.QUESTIONS)
        if self.responses_frozen:
            raise RuntimeError("lesson responses are frozen")
        if type(question_index) is not int or not 1 <= question_index <= len(
            self.lesson.training_questions
        ):
            raise ValueError("lesson question index is out of range")
        if not isinstance(response, str) or not response.strip():
            raise ValueError("lesson response must be nonempty text")
        before = self.phase
        question = self.lesson.training_questions[question_index - 1]
        context = self._observable_context()
        self._responses[question_index] = LessonQuestionResponse(
            question_index,
            question,
            response.strip(),
            self.current_time_us,
            context,
        )
        self._record(
            LessonAction.ANSWER,
            {"question_index": question_index, "response": response.strip()},
            before,
            {"answered_question_index": question_index},
        )

    def freeze_responses(self) -> None:
        self._require_phase(LessonPhase.QUESTIONS)
        expected = set(range(1, len(self.lesson.training_questions) + 1))
        if set(self._responses) != expected:
            missing = sorted(expected - set(self._responses))
            raise RuntimeError(f"cannot freeze; unanswered questions: {missing}")
        before = self.phase
        self.responses_frozen = True
        self.phase = LessonPhase.COMPLETE
        self._record(
            LessonAction.FREEZE_RESPONSES,
            {},
            before,
            {"response_count": len(self._responses)},
        )

    def reveal(self) -> None:
        self._require_phase(LessonPhase.COMPLETE)
        if not self.responses_frozen:
            raise RuntimeError("responses must be frozen before reveal")
        before = self.phase
        self.phase = LessonPhase.REVEALED
        self._record(LessonAction.REVEAL, {}, before, {})

    def debrief(self) -> None:
        self._require_phase(LessonPhase.REVEALED)
        before = self.phase
        self.phase = LessonPhase.DEBRIEFED
        self._record(
            LessonAction.DEBRIEF,
            {},
            before,
            {"comparison_count": len(self.response_comparisons())},
        )

    def submit_counterfactual(
        self,
        order_type: str,
        side: str,
        quantity: int,
        price_ticks: int | None = None,
    ) -> CounterfactualExecution:
        if self.phase not in {LessonPhase.BLIND_RUNNING, LessonPhase.QUESTIONS}:
            raise RuntimeError("counterfactual execution requires a blind decision phase")
        if self.paused:
            raise RuntimeError("resume the lesson before submitting a counterfactual")
        try:
            parsed_type = OrderType(order_type.lower())
            parsed_side = Side(side.lower())
        except ValueError as error:
            raise ValueError("counterfactual order requires limit/market and buy/sell") from error
        if parsed_type not in {OrderType.LIMIT, OrderType.MARKET}:
            raise ValueError("counterfactual order must be limit or market")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("counterfactual quantity must be positive")
        if parsed_type is OrderType.LIMIT:
            if type(price_ticks) is not int or price_ticks <= 0:
                raise ValueError("counterfactual limit requires positive integer ticks")
        elif price_ticks is not None:
            raise ValueError("counterfactual market order cannot carry a price")
        before = self.phase
        source_digest_before = self._run.replay_sha256()
        overlay = self._source_book_prefix()
        baseline_events = len(overlay.journal.events)
        baseline_trades = len(overlay.trades)
        baseline_fills = len(overlay.fills)
        sequence = len(self._counterfactuals) + 1
        order_id = f"LESSON-CF-{sequence:04d}"
        order = (
            Order.limit(
                order_id,
                parsed_side,
                quantity,
                price_ticks,  # type: ignore[arg-type]
                OrderOwner.PLAYER,
            )
            if parsed_type is OrderType.LIMIT
            else Order.market(order_id, parsed_side, quantity, OrderOwner.PLAYER)
        )
        overlay.process(order)
        new_events = overlay.journal.events[baseline_events:]
        new_trades = overlay.trades[baseline_trades:]
        new_fills = overlay.fills[baseline_fills:]
        command = {
            "order_id": order_id,
            "order_type": parsed_type.value,
            "price_ticks": price_ticks,
            "quantity": quantity,
            "side": parsed_side.value,
        }
        overlay_payload = {
            "command": command,
            "events": [event.as_dict() for event in new_events],
            "fills": [_fill_dict(fill) for fill in new_fills],
            "trades": [_trade_dict(trade) for trade in new_trades],
        }
        overlay_sha256 = hashlib.sha256(
            json.dumps(
                overlay_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if self.lesson.mode is HistoricalDataMode.RECONSTRUCTION:
            provenance = "COUNTERFACTUAL_ON_SYNTHETIC_RECONSTRUCTION"
        elif (
            self._run.provenance.provides_book_events
            or self._run.provenance.provides_order_events
        ):
            provenance = "COUNTERFACTUAL_ON_SOURCE_DERIVED_BOOK"
        else:
            provenance = "COUNTERFACTUAL_SYNTHETIC_FROM_UNSUPPORTED_BOOK_STATE"
        result = CounterfactualExecution(
            sequence=sequence,
            simulation_time_us=self.current_time_us,
            provenance=provenance,
            command=command,
            decision_context=self._observable_context(),
            exchange_events=tuple(event.as_dict() for event in new_events),
            fills=tuple(_fill_dict(fill) for fill in new_fills),
            trades=tuple(_trade_dict(trade) for trade in new_trades),
            source_replay_sha256=source_digest_before,
            overlay_sha256=overlay_sha256,
        )
        self._counterfactuals.append(result)
        self._record(
            LessonAction.COUNTERFACTUAL_ORDER,
            {
                "order_type": parsed_type.value,
                "price_ticks": price_ticks,
                "quantity": quantity,
                "side": parsed_side.value,
            },
            before,
            {
                "counterfactual_sequence": sequence,
                "overlay_sha256": overlay_sha256,
                "provenance": provenance,
            },
        )
        if self._run.replay_sha256() != source_digest_before:
            raise RuntimeError("counterfactual overlay mutated authoritative source history")
        return result

    def response_comparisons(self) -> tuple[HistoricalResponseComparison, ...]:
        if self.phase is not LessonPhase.DEBRIEFED:
            raise RuntimeError("response comparisons require the DEBRIEFED phase")
        evidence = (
            self.lesson.post_session_explanation.market_context,
            self.lesson.post_session_explanation.why_session_matters,
            self.lesson.post_session_explanation.what_happened_next,
        )
        comparisons: list[HistoricalResponseComparison] = []
        for response in self.responses:
            statement = evidence[(response.question_index - 1) % len(evidence)]
            shared = tuple(
                sorted(_terms(response.response) & _terms(statement.text))
            )
            comparisons.append(
                HistoricalResponseComparison(
                    question_index=response.question_index,
                    question=response.question,
                    response=response.response,
                    response_context=response.observable_context,
                    evidence=statement,
                    shared_terms=shared,
                    assessment=(
                        "EVIDENCE_ALIGNED_TERMS_PRESENT"
                        if shared
                        else "NO_AUTOMATIC_CORRECTNESS_CLAIM"
                    ),
                )
            )
        return tuple(comparisons)

    def visible_book(self, levels: int = 4) -> dict[str, object]:
        if type(levels) is not int or levels <= 0:
            raise ValueError("visible lesson levels must be positive")
        if self.queue_evidence_label == "UNAVAILABLE":
            return {
                "asks": [],
                "best_ask_ticks": None,
                "best_bid_ticks": None,
                "bids": [],
                "provenance": "UNAVAILABLE",
            }
        return {
            "asks": [
                {
                    "price_ticks": price,
                    "quantity": self._view_book.asks[price].total_quantity,
                }
                for price in self._view_book.ask_prices[:levels]
            ],
            "best_ask_ticks": self._view_book.best_ask,
            "best_bid_ticks": self._view_book.best_bid,
            "bids": [
                {
                    "price_ticks": price,
                    "quantity": self._view_book.bids[price].total_quantity,
                }
                for price in self._view_book.bid_prices[:levels]
            ],
            "provenance": self.queue_evidence_label,
        }

    def visible_trades(self, limit: int = 6) -> tuple[dict[str, object], ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("visible lesson trade limit must be positive")
        if self.trade_evidence_label == "UNAVAILABLE":
            return ()
        show_side = (
            self.lesson.mode is HistoricalDataMode.RECONSTRUCTION
            or self._run.provenance.provides_trade_aggressor_side
        )
        return tuple(
            {
                "price_ticks": trade.price_ticks,
                "provenance": self.trade_evidence_label,
                "quantity": trade.quantity,
                "taker_side": trade.taker_side.value if show_side else None,
                "trade_id": trade.trade_id,
            }
            for trade in self._view_book.trades[-limit:]
        )

    def result_sha256(self) -> str:
        canonical = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def replay_json_lines(self) -> str:
        """Return the complete canonical session stream needed for replay."""

        records: list[dict[str, object]] = [
            {
                "lesson_id": self.lesson.lesson_id,
                "lesson_sha256": self.lesson.sha256,
                "record_type": "LESSON_SESSION_HEADER",
                "source_replay_sha256": self._source_replay_sha256,
            }
        ]
        records.extend(
            {"record_type": "LESSON_INPUT", **item.as_dict()}
            for item in self.inputs
        )
        records.extend(
            {"record_type": "LESSON_EVENT", **item.as_dict()}
            for item in self.session_events
        )
        records.extend(
            {"record_type": "LESSON_RESPONSE", **item.as_dict()}
            for item in self.responses
        )
        records.extend(
            {"record_type": "COUNTERFACTUAL_OVERLAY", **item.as_dict()}
            for item in self.counterfactuals
        )
        records.append(
            {
                "phase": self.phase.value,
                "record_type": "LESSON_SESSION_RESULT",
                "result_sha256": self.result_sha256(),
            }
        )
        return "\n".join(
            json.dumps(record, sort_keys=True, separators=(",", ":"))
            for record in records
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "counterfactuals": [item.as_dict() for item in self.counterfactuals],
            "current_time_us": self.current_time_us,
            "debrief_available": self.debrief_available,
            "identity_revealed": self.identity_revealed,
            "inputs": [item.as_dict() for item in self.inputs],
            "lesson_id": self.lesson.lesson_id,
            "lesson_sha256": self.lesson.sha256,
            "paused": self.paused,
            "phase": self.phase.value,
            "responses": [item.as_dict() for item in self.responses],
            "responses_frozen": self.responses_frozen,
            "session_events": [item.as_dict() for item in self.session_events],
            "source_command_index": self._source_command_index,
            "source_replay_sha256": self._source_replay_sha256,
            "visible_book": self.visible_book(),
            "visible_trades": list(self.visible_trades(limit=1_000_000)),
        }

    def _require_phase(self, expected: LessonPhase) -> None:
        if self.phase is not expected:
            raise RuntimeError(
                f"lesson action requires {expected.value}; current phase={self.phase.value}"
            )

    def _require_running(self) -> None:
        self._require_phase(LessonPhase.BLIND_RUNNING)
        if self.paused:
            raise RuntimeError("lesson is paused")

    def _apply_next_source_command(self) -> None:
        command = self._run.commands[self._source_command_index]
        apply_historical_command(command, self._view_book, self._run)
        self._source_command_index += 1
        self._view_book.assert_invariants()

    def _enter_questions_if_finished(self) -> None:
        if (
            self.current_time_us == self._run.duration_us
            and self._source_command_index == len(self._run.commands)
        ):
            self.phase = LessonPhase.QUESTIONS
            if self._view_book.journal.canonical_json_lines() != (
                self._run.book.journal.canonical_json_lines()
            ):
                raise RuntimeError("lesson source cursor diverged at completion")

    def _observable_context(self) -> dict[str, object]:
        book = self.visible_book(levels=1)
        return {
            "best_ask_ticks": book["best_ask_ticks"],
            "best_bid_ticks": book["best_bid_ticks"],
            "counterfactual_count": len(self._counterfactuals),
            "current_time_us": self.current_time_us,
            "paused": self.paused,
            "phase": self.phase.value,
            "queue_evidence": self.queue_evidence_label,
            "source_command_count": len(self._run.commands),
            "source_command_index": self._source_command_index,
            "trade_evidence": self.trade_evidence_label,
            "visible_trade_count": len(self.visible_trades(limit=1_000_000)),
        }

    def _record(
        self,
        action: LessonAction,
        arguments: dict[str, object],
        phase_before: LessonPhase,
        payload: dict[str, object],
    ) -> None:
        lesson_input = LessonSessionInput(
            len(self._inputs) + 1,
            action,
            arguments,
        )
        self._inputs.append(lesson_input)
        self._events.append(
            LessonSessionEvent(
                sequence=len(self._events) + 1,
                simulation_time_us=self.current_time_us,
                action=action,
                phase_before=phase_before,
                phase_after=self.phase,
                payload=payload,
                observable_context=self._observable_context(),
            )
        )
        self._assert_source_unchanged()

    def _assert_source_unchanged(self) -> None:
        if self._run.replay_sha256() != self._source_replay_sha256:
            raise RuntimeError("lesson runtime mutated authoritative source replay")
        source_prefix = self._run.exchange_events[: len(self._view_book.journal.events)]
        if tuple(event.as_dict() for event in self._view_book.journal.events) != tuple(
            event.as_dict() for event in source_prefix
        ):
            raise RuntimeError("lesson source cursor no longer matches authoritative prefix")

    def _source_book_prefix(self) -> OrderBook:
        book = OrderBook()
        for command in self._run.commands[: self._source_command_index]:
            apply_historical_command(command, book, self._run)
        if book.journal.canonical_json_lines() != self._view_book.journal.canonical_json_lines():
            raise RuntimeError("counterfactual source clone diverged from visible prefix")
        return book


def _fill_dict(fill) -> dict[str, object]:
    return {
        "liquidity": fill.liquidity,
        "order_id": fill.order_id,
        "owner": fill.owner.value,
        "price_ticks": fill.price_ticks,
        "quantity": fill.quantity,
        "side": fill.side.value,
        "trade_id": fill.trade_id,
    }


def _trade_dict(trade) -> dict[str, object]:
    return {
        "maker_order_id": trade.maker_order_id,
        "price_ticks": trade.price_ticks,
        "quantity": trade.quantity,
        "taker_order_id": trade.taker_order_id,
        "taker_side": trade.taker_side.value,
        "trade_id": trade.trade_id,
    }


def _terms(text: str) -> set[str]:
    stop = {"a", "an", "and", "at", "by", "for", "in", "is", "of", "on", "the", "to", "was"}
    return {
        value
        for value in re.findall(r"[a-z0-9]+", text.lower())
        if len(value) > 2 and value not in stop
    }
