"""Reusable historical-lesson contracts with explicit evidence boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from kirby2.immutable import freeze_json, thaw_json

from .models import HistoricalDataMode


HISTORICAL_LESSON_SCHEMA_VERSION = 1
MINED_LESSON_SOURCE_SCHEMA_VERSION = 1
RECORDED_CLIENT_FEED_POLICY_V1 = "RECORDED_CLIENT_FEED_EXACT_V1"
MINED_HIDDEN_STATE_REVEAL_POLICY_V1 = "SEPARATE_AUTHORIZED_REVEAL_V1"
_LESSON_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")


class RevealPolicy(str, Enum):
    BLIND_UNTIL_COMPLETION = "BLIND_UNTIL_COMPLETION"
    REVEALED_FROM_START = "REVEALED_FROM_START"


class EvidenceCategory(str, Enum):
    KNOWN_HISTORICAL_FACT = "KNOWN_HISTORICAL_FACT"
    MEASURED_SOURCE_DATA = "MEASURED_SOURCE_DATA"
    SYNTHETIC_RECONSTRUCTION = "SYNTHETIC_RECONSTRUCTION"
    LOCAL_FIXTURE_METADATA = "LOCAL_FIXTURE_METADATA"
    DERIVED_FROM_SOURCE = "DERIVED_FROM_SOURCE"


@dataclass(frozen=True, slots=True)
class EvidenceStatement:
    category: EvidenceCategory
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, EvidenceCategory) or not self.text.strip():
            raise ValueError("evidence statement requires a category and text")

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category.value, "text": self.text}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceStatement:
        if not isinstance(payload.get("category"), str) or not isinstance(
            payload.get("text"),
            str,
        ):
            raise ValueError("evidence category and text must be strings")
        return cls(
            EvidenceCategory(str(payload["category"]).upper()),
            str(payload["text"]),
        )


@dataclass(frozen=True, slots=True)
class LessonTimeWindow:
    start_us: int
    end_us: int

    def __post_init__(self) -> None:
        if (
            type(self.start_us) is not int
            or self.start_us < 0
            or type(self.end_us) is not int
            or self.end_us <= self.start_us
        ):
            raise ValueError("lesson time window is invalid")

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def as_dict(self) -> dict[str, int]:
        return {"end_us": self.end_us, "start_us": self.start_us}


@dataclass(frozen=True, slots=True)
class LessonSource:
    fixture_id: str
    source_locator: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.fixture_id, str)
            or not isinstance(self.source_locator, str)
            or not self.fixture_id
            or not self.source_locator
        ):
            raise ValueError("lesson source fixture and locator are required")

    def as_dict(self) -> dict[str, str]:
        return {
            "fixture_id": self.fixture_id,
            "source_locator": self.source_locator,
        }


def _require_mined_digest(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be one lowercase SHA-256 digest")
    return value


def _require_mined_id(value: object, label: str) -> str:
    if type(value) is not str or _STABLE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be one stable identifier")
    return value


@dataclass(frozen=True, slots=True)
class MinedSourceRunReferenceV1:
    """Content-addressed authoritative run used to extract a mined lesson."""

    source_kind: str
    source_id: str
    source_sha256: str
    source_replay_sha256: str

    def __post_init__(self) -> None:
        if self.source_kind not in {"RUN", "DATASET", "RECONSTRUCTION"}:
            raise ValueError("mined source kind is outside the closed V1 vocabulary")
        _require_mined_id(self.source_id, "mined source ID")
        _require_mined_digest(self.source_sha256, "mined source digest")
        _require_mined_digest(
            self.source_replay_sha256,
            "mined source replay digest",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "source_replay_sha256": self.source_replay_sha256,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class MinedSourceTimeBoundsV1:
    """The complete six-bound half-open candidate window."""

    source_start_us: int
    source_end_us: int
    warmup_start_us: int
    active_start_us: int
    active_end_us: int
    post_end_us: int

    def __post_init__(self) -> None:
        values = (
            self.source_start_us,
            self.source_end_us,
            self.warmup_start_us,
            self.active_start_us,
            self.active_end_us,
            self.post_end_us,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("mined lesson bounds must be nonnegative integers")
        if not (
            self.source_start_us
            <= self.warmup_start_us
            <= self.active_start_us
            < self.active_end_us
            <= self.post_end_us
            <= self.source_end_us
        ):
            raise ValueError("mined lesson half-open bounds are inconsistent")

    @property
    def activation_us(self) -> int:
        return self.active_end_us - 1

    def as_dict(self) -> dict[str, int]:
        return {
            "active_end_us": self.active_end_us,
            "active_start_us": self.active_start_us,
            "post_end_us": self.post_end_us,
            "source_end_us": self.source_end_us,
            "source_start_us": self.source_start_us,
            "warmup_start_us": self.warmup_start_us,
        }


@dataclass(frozen=True, slots=True)
class MinedCheckpointReferenceV1:
    checkpoint_id: str
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        _require_mined_id(self.checkpoint_id, "mined checkpoint ID")
        _require_mined_digest(self.checkpoint_sha256, "mined checkpoint digest")

    def as_dict(self) -> dict[str, str]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_sha256": self.checkpoint_sha256,
        }


@dataclass(frozen=True, slots=True)
class MinedDetectorReferenceV1:
    detector_id: str
    detector_version: int
    threshold_sha256: str

    def __post_init__(self) -> None:
        _require_mined_id(self.detector_id, "mined detector ID")
        if type(self.detector_version) is not int or self.detector_version <= 0:
            raise ValueError("mined detector version must be positive")
        _require_mined_digest(self.threshold_sha256, "detector threshold digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "threshold_sha256": self.threshold_sha256,
        }


@dataclass(frozen=True, slots=True)
class MinedLessonSourceRecordV1:
    """Seven-field immutable lineage record for one playable mined lesson.

    This record is intentionally not a blind-presentation model.  A client receives
    only its digest during assessment; the full record is separately disclosed in
    the authorized reveal/debrief payload.
    """

    source_run_reference: MinedSourceRunReferenceV1
    source_time_bounds: MinedSourceTimeBoundsV1
    checkpoint_reference: MinedCheckpointReferenceV1 | None
    observable_feed_policy: str
    hidden_state_reveal_policy: str
    historical_provenance: Mapping[str, object]
    detector: MinedDetectorReferenceV1

    def __post_init__(self) -> None:
        if not isinstance(self.source_run_reference, MinedSourceRunReferenceV1):
            raise TypeError("mined lesson source-run reference is invalid")
        if not isinstance(self.source_time_bounds, MinedSourceTimeBoundsV1):
            raise TypeError("mined lesson source bounds are invalid")
        if self.checkpoint_reference is not None and not isinstance(
            self.checkpoint_reference,
            MinedCheckpointReferenceV1,
        ):
            raise TypeError("mined lesson checkpoint reference is invalid")
        if self.observable_feed_policy != RECORDED_CLIENT_FEED_POLICY_V1:
            raise ValueError("mined lesson observable-feed policy is unsupported")
        if self.hidden_state_reveal_policy != MINED_HIDDEN_STATE_REVEAL_POLICY_V1:
            raise ValueError("mined lesson hidden-state reveal policy is unsupported")
        if not isinstance(self.detector, MinedDetectorReferenceV1):
            raise TypeError("mined lesson detector reference is invalid")
        frozen = freeze_json(self.historical_provenance)
        if not isinstance(frozen, Mapping) or not frozen:
            raise ValueError("mined lesson historical provenance must be a record")
        object.__setattr__(self, "historical_provenance", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_reference": (
                None
                if self.checkpoint_reference is None
                else self.checkpoint_reference.as_dict()
            ),
            "detector": self.detector.as_dict(),
            "hidden_state_reveal_policy": self.hidden_state_reveal_policy,
            "historical_provenance": thaw_json(self.historical_provenance),
            "observable_feed_policy": self.observable_feed_policy,
            "source_run_reference": self.source_run_reference.as_dict(),
            "source_time_bounds": self.source_time_bounds.as_dict(),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalDebrief:
    event: EvidenceStatement
    market_context: EvidenceStatement
    what_happened_next: EvidenceStatement
    why_session_matters: EvidenceStatement

    def __post_init__(self) -> None:
        if any(
            not isinstance(statement, EvidenceStatement)
            for statement in self.statements()
        ):
            raise TypeError("historical debrief fields must be evidence statements")

    def statements(self) -> tuple[EvidenceStatement, ...]:
        return (
            self.event,
            self.market_context,
            self.what_happened_next,
            self.why_session_matters,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "event": self.event.as_dict(),
            "market_context": self.market_context.as_dict(),
            "what_happened_next": self.what_happened_next.as_dict(),
            "why_session_matters": self.why_session_matters.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HistoricalDebrief:
        fields = (
            "event",
            "market_context",
            "what_happened_next",
            "why_session_matters",
        )
        if any(not isinstance(payload.get(field), dict) for field in fields):
            raise ValueError("historical debrief fields must be evidence objects")
        return cls(
            EvidenceStatement.from_dict(payload["event"]),
            EvidenceStatement.from_dict(payload["market_context"]),
            EvidenceStatement.from_dict(payload["what_happened_next"]),
            EvidenceStatement.from_dict(payload["why_session_matters"]),
        )


@dataclass(frozen=True, slots=True)
class HistoricalLesson:
    lesson_id: str
    title: str
    date: str
    instrument: str
    market: str
    mode: HistoricalDataMode
    data_provenance: tuple[EvidenceStatement, ...]
    time_window: LessonTimeWindow
    historical_context: tuple[EvidenceStatement, ...]
    learning_objectives: tuple[str, ...]
    source: LessonSource
    reveal_policy: RevealPolicy
    post_session_explanation: HistoricalDebrief
    known_limitations: tuple[str, ...]
    training_questions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _LESSON_ID.fullmatch(self.lesson_id):
            raise ValueError("historical lesson ID is invalid")
        text_fields = (self.title, self.instrument, self.market)
        if any(not value.strip() for value in text_fields):
            raise ValueError("historical lesson identity fields must not be empty")
        try:
            date.fromisoformat(self.date)
        except ValueError as error:
            raise ValueError("historical lesson date must use ISO YYYY-MM-DD") from error
        if not isinstance(self.mode, HistoricalDataMode):
            raise TypeError("historical lesson data mode is invalid")
        if not isinstance(self.source, LessonSource):
            raise TypeError("historical lesson source is invalid")
        if not isinstance(self.time_window, LessonTimeWindow):
            raise TypeError("historical lesson time window is invalid")
        if not isinstance(self.reveal_policy, RevealPolicy):
            raise TypeError("historical lesson reveal policy is invalid")
        if not isinstance(self.post_session_explanation, HistoricalDebrief):
            raise TypeError("historical lesson debrief is invalid")
        if not self.data_provenance or not self.historical_context:
            raise ValueError("lesson provenance and historical context are required")
        if any(
            not isinstance(item, EvidenceStatement)
            for item in (*self.data_provenance, *self.historical_context)
        ):
            raise TypeError("lesson provenance and context must be evidence statements")
        _validate_text_collection("learning objectives", self.learning_objectives)
        _validate_text_collection("known limitations", self.known_limitations)
        _validate_text_collection("training questions", self.training_questions)
        evidence = self.evidence_statements()
        if self.mode is HistoricalDataMode.EXACT_REPLAY and any(
            item.category is EvidenceCategory.SYNTHETIC_RECONSTRUCTION
            for item in evidence
        ):
            raise ValueError("exact replay lesson cannot label content as reconstruction")
        if self.mode is HistoricalDataMode.RECONSTRUCTION:
            if not any(
                item.category is EvidenceCategory.SYNTHETIC_RECONSTRUCTION
                for item in evidence
            ):
                raise ValueError("reconstruction lesson must disclose synthetic evidence")
            limitations = " ".join(self.known_limitations).lower()
            if "level 2" not in limitations or "missing" not in limitations:
                raise ValueError(
                    "reconstruction lesson must disclose missing historical Level 2"
                )

    def evidence_statements(self) -> tuple[EvidenceStatement, ...]:
        return (
            *self.data_provenance,
            *self.historical_context,
            *self.post_session_explanation.statements(),
        )

    def evidence_inventory(self) -> dict[str, int]:
        statements = self.evidence_statements()
        return {
            category.value: sum(item.category is category for item in statements)
            for category in EvidenceCategory
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "data_provenance": [item.as_dict() for item in self.data_provenance],
            "date": self.date,
            "historical_context": [
                item.as_dict() for item in self.historical_context
            ],
            "instrument": self.instrument,
            "known_limitations": list(self.known_limitations),
            "learning_objectives": list(self.learning_objectives),
            "lesson_id": self.lesson_id,
            "market": self.market,
            "mode": self.mode.value,
            "post_session_explanation": self.post_session_explanation.as_dict(),
            "reveal_policy": self.reveal_policy.value,
            "schema_version": HISTORICAL_LESSON_SCHEMA_VERSION,
            "source": self.source.as_dict(),
            "time_window": self.time_window.as_dict(),
            "title": self.title,
            "training_questions": list(self.training_questions),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HistoricalLesson:
        if payload.get("schema_version") != HISTORICAL_LESSON_SCHEMA_VERSION:
            raise ValueError("unsupported historical lesson schema version")
        data_provenance = _evidence_array(payload, "data_provenance")
        historical_context = _evidence_array(payload, "historical_context")
        raw_window = payload.get("time_window")
        raw_source = payload.get("source")
        raw_debrief = payload.get("post_session_explanation")
        if not isinstance(raw_window, dict):
            raise ValueError("lesson time window must be an object")
        if not isinstance(raw_source, dict):
            raise ValueError("lesson source must be an object")
        if not isinstance(raw_debrief, dict):
            raise ValueError("post-session explanation must be an object")
        identity_fields = (
            "lesson_id",
            "title",
            "date",
            "instrument",
            "market",
            "mode",
            "reveal_policy",
        )
        if any(not isinstance(payload.get(field), str) for field in identity_fields):
            raise ValueError("historical lesson identity fields must be strings")
        if type(raw_window.get("start_us")) is not int or type(
            raw_window.get("end_us")
        ) is not int:
            raise TypeError("historical lesson window bounds must be integers")
        if not isinstance(raw_source.get("fixture_id"), str) or not isinstance(
            raw_source.get("source_locator"),
            str,
        ):
            raise ValueError("historical lesson source fields must be strings")
        return cls(
            lesson_id=str(payload["lesson_id"]),
            title=str(payload["title"]),
            date=str(payload["date"]),
            instrument=str(payload["instrument"]),
            market=str(payload["market"]),
            mode=HistoricalDataMode.parse(str(payload["mode"])),
            data_provenance=data_provenance,
            time_window=LessonTimeWindow(
                int(raw_window["start_us"]),
                int(raw_window["end_us"]),
            ),
            historical_context=historical_context,
            learning_objectives=_text_array(payload, "learning_objectives"),
            source=LessonSource(
                str(raw_source["fixture_id"]),
                str(raw_source["source_locator"]),
            ),
            reveal_policy=RevealPolicy(str(payload["reveal_policy"]).upper()),
            post_session_explanation=HistoricalDebrief.from_dict(raw_debrief),
            known_limitations=_text_array(payload, "known_limitations"),
            training_questions=_text_array(payload, "training_questions"),
        )


def _validate_text_collection(label: str, values: tuple[str, ...]) -> None:
    if not values or any(not value.strip() for value in values):
        raise ValueError(f"historical lesson {label} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"historical lesson {label} must be unique")


def _text_array(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = payload.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"historical lesson {key} must be a text array")
    return tuple(raw)


def _evidence_array(
    payload: dict[str, Any],
    key: str,
) -> tuple[EvidenceStatement, ...]:
    raw = payload.get(key)
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError(f"historical lesson {key} must be an evidence array")
    return tuple(EvidenceStatement.from_dict(item) for item in raw)
