"""Exact timing and data-age contracts for replay microscope values."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


DATA_AGE_SCHEMA_ID = "KIRBY2_MICROSCOPE_DATA_AGE_V1"
DATA_AGE_SCHEMA_VERSION = 1


class TimestampAvailability(str, Enum):
    """Whether one causal timestamp was recorded for an evidence value."""

    RECORDED = "RECORDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNAVAILABLE = "UNAVAILABLE"


class TimestampAbsenceReason(str, Enum):
    CLIENT_DECISION = "CLIENT_DECISION"
    DECISION_SNAPSHOT = "DECISION_SNAPSHOT"
    NEVER_CLIENT_DELIVERED = "NEVER_CLIENT_DELIVERED"
    NEVER_CLIENT_KNOWN_DURING_RUN = "NEVER_CLIENT_KNOWN_DURING_RUN"
    NOT_KNOWN_AT_RENDER_CURSOR = "NOT_KNOWN_AT_RENDER_CURSOR"
    NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE = (
        "NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE"
    )
    NO_VENUE_HOP = "NO_VENUE_HOP"
    OUTBOUND_CLIENT_INTENTION = "OUTBOUND_CLIENT_INTENTION"
    RECORDED_SNAPSHOT = "RECORDED_SNAPSHOT"


NOT_KNOWN_AT_RENDER_CURSOR = TimestampAbsenceReason.NOT_KNOWN_AT_RENDER_CURSOR
NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE = (
    TimestampAbsenceReason.NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE
)


@dataclass(frozen=True, slots=True)
class EvidenceTimestamp:
    """A recorded timestamp or an explicit typed absence."""

    availability: TimestampAvailability
    time_us: int | None
    reason: TimestampAbsenceReason | None = None

    def __post_init__(self) -> None:
        if type(self.availability) is not TimestampAvailability:
            raise TypeError("evidence timestamp availability is invalid")
        if self.availability is TimestampAvailability.RECORDED:
            if type(self.time_us) is not int or self.time_us < 0:
                raise ValueError("recorded evidence timestamp must be nonnegative")
            if self.reason is not None:
                raise ValueError("recorded evidence timestamp cannot carry an absence reason")
            return
        if self.time_us is not None:
            raise ValueError("absent evidence timestamp cannot carry a time")
        if type(self.reason) is not TimestampAbsenceReason:
            raise ValueError("absent evidence timestamp requires a typed reason")

    @classmethod
    def recorded(cls, time_us: int) -> EvidenceTimestamp:
        return cls(TimestampAvailability.RECORDED, time_us)

    @classmethod
    def not_applicable(cls, reason: TimestampAbsenceReason) -> EvidenceTimestamp:
        return cls(TimestampAvailability.NOT_APPLICABLE, None, reason)

    @classmethod
    def unavailable(cls, reason: TimestampAbsenceReason) -> EvidenceTimestamp:
        return cls(TimestampAvailability.UNAVAILABLE, None, reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "reason": None if self.reason is None else self.reason.value,
            "time_us": self.time_us,
        }


@dataclass(frozen=True, slots=True)
class EvidenceTiming:
    """Immutable causal timestamps recorded before any replay query runs.

    Venue and client chronology is intentionally directional. Inbound data can be
    received before later client processing/knowledge, while an outbound intention
    can be client-known before its later venue receipt. Every recorded hop must be
    after the source event, but no universal venue-versus-client ordering is imposed.
    """

    source_event_time_us: int
    venue_receipt: EvidenceTimestamp
    client_receive: EvidenceTimestamp
    client_knowledge: EvidenceTimestamp

    def __post_init__(self) -> None:
        if type(self.source_event_time_us) is not int or self.source_event_time_us < 0:
            raise ValueError("source event time must be nonnegative microseconds")
        if type(self.venue_receipt) is not EvidenceTimestamp:
            raise TypeError("venue receipt time must use EvidenceTimestamp")
        if type(self.client_receive) is not EvidenceTimestamp:
            raise TypeError("client receive time must use EvidenceTimestamp")
        if type(self.client_knowledge) is not EvidenceTimestamp:
            raise TypeError("client knowledge time must use EvidenceTimestamp")
        for label, timestamp in (
            ("venue receipt", self.venue_receipt),
            ("client receive", self.client_receive),
            ("client knowledge", self.client_knowledge),
        ):
            if (
                timestamp.time_us is not None
                and timestamp.time_us < self.source_event_time_us
            ):
                raise ValueError(f"{label} time precedes the source event")
        if (
            self.client_receive.time_us is not None
            and self.client_knowledge.time_us is not None
            and self.client_knowledge.time_us < self.client_receive.time_us
        ):
            raise ValueError("client knowledge time precedes client receipt")

    @property
    def client_knowledge_time_us(self) -> int | None:
        return self.client_knowledge.time_us

    def as_dict(self) -> dict[str, object]:
        return {
            "client_knowledge": self.client_knowledge.as_dict(),
            "client_receive": self.client_receive.as_dict(),
            "source_event_time_us": self.source_event_time_us,
            "venue_receipt": self.venue_receipt.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DataAge:
    """Cursor-safe timing projection derived from immutable causal timestamps."""

    source_event_time_us: int
    venue_receipt: EvidenceTimestamp
    client_receive: EvidenceTimestamp
    client_knowledge: EvidenceTimestamp
    policy_visible_at_time_us: int
    render_cursor_time_us: int
    action_time_us: int | None = None
    event_age_at_render_us: int = field(init=False)
    policy_visibility_age_at_render_us: int = field(init=False)
    knowledge_age_at_render_us: int | None = field(init=False)
    age_at_action_us: int | None = field(init=False)
    known_at_action: bool | None = field(init=False)
    schema_id: str = DATA_AGE_SCHEMA_ID
    schema_version: int = DATA_AGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        EvidenceTiming(
            source_event_time_us=self.source_event_time_us,
            venue_receipt=self.venue_receipt,
            client_receive=self.client_receive,
            client_knowledge=self.client_knowledge,
        )
        if type(self.source_event_time_us) is not int or self.source_event_time_us < 0:
            raise ValueError("data-age source event time is invalid")
        if type(self.venue_receipt) is not EvidenceTimestamp:
            raise TypeError("data-age venue receipt is invalid")
        if type(self.client_receive) is not EvidenceTimestamp:
            raise TypeError("data-age client receipt is invalid")
        if type(self.client_knowledge) is not EvidenceTimestamp:
            raise TypeError("data-age client knowledge is invalid")
        if (
            type(self.policy_visible_at_time_us) is not int
            or self.policy_visible_at_time_us < self.source_event_time_us
        ):
            raise ValueError("policy visibility time precedes the source event")
        if type(self.render_cursor_time_us) is not int or self.render_cursor_time_us < 0:
            raise ValueError("render cursor time must be nonnegative microseconds")
        if self.policy_visible_at_time_us > self.render_cursor_time_us:
            raise ValueError("evidence is not policy-visible at the render cursor")
        if self.action_time_us is not None and (
            type(self.action_time_us) is not int or self.action_time_us < 0
        ):
            raise ValueError("action time must be nonnegative microseconds or None")
        if (
            self.schema_id != DATA_AGE_SCHEMA_ID
            or type(self.schema_version) is not int
            or self.schema_version != DATA_AGE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported data-age schema")
        for timestamp in (
            self.venue_receipt,
            self.client_receive,
            self.client_knowledge,
        ):
            if timestamp.time_us is not None and timestamp.time_us > self.render_cursor_time_us:
                raise ValueError("data-age projection exposes a future timestamp")

        object.__setattr__(
            self,
            "event_age_at_render_us",
            self.render_cursor_time_us - self.source_event_time_us,
        )
        object.__setattr__(
            self,
            "policy_visibility_age_at_render_us",
            self.render_cursor_time_us - self.policy_visible_at_time_us,
        )
        knowledge_time = self.client_knowledge.time_us
        object.__setattr__(
            self,
            "knowledge_age_at_render_us",
            (
                None
                if knowledge_time is None
                else self.render_cursor_time_us - knowledge_time
            ),
        )
        if self.action_time_us is None:
            object.__setattr__(self, "known_at_action", None)
            object.__setattr__(self, "age_at_action_us", None)
            return
        known_at_action = knowledge_time is not None and knowledge_time <= self.action_time_us
        object.__setattr__(self, "known_at_action", known_at_action)
        object.__setattr__(
            self,
            "age_at_action_us",
            self.action_time_us - self.source_event_time_us if known_at_action else None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "action_time_us": self.action_time_us,
            "age_at_action_us": self.age_at_action_us,
            "client_knowledge": self.client_knowledge.as_dict(),
            "client_receive": self.client_receive.as_dict(),
            "event_age_at_render_us": self.event_age_at_render_us,
            "knowledge_age_at_render_us": self.knowledge_age_at_render_us,
            "known_at_action": self.known_at_action,
            "policy_visibility_age_at_render_us": (
                self.policy_visibility_age_at_render_us
            ),
            "policy_visible_at_time_us": self.policy_visible_at_time_us,
            "render_cursor_time_us": self.render_cursor_time_us,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "source_event_time_us": self.source_event_time_us,
            "venue_receipt": self.venue_receipt.as_dict(),
        }


def build_data_age(
    timing: EvidenceTiming,
    render_cursor_time_us: int,
    *,
    policy_visible_at_time_us: int,
    action_time_us: int | None = None,
) -> DataAge:
    """Derive cursor-safe ages without exposing later recorded timestamps."""

    if type(timing) is not EvidenceTiming:
        raise TypeError("data age requires EvidenceTiming")
    if type(render_cursor_time_us) is not int or render_cursor_time_us < 0:
        raise ValueError("render cursor time must be nonnegative microseconds")
    return DataAge(
        source_event_time_us=timing.source_event_time_us,
        venue_receipt=_project_timestamp(timing.venue_receipt, render_cursor_time_us),
        client_receive=_project_timestamp(timing.client_receive, render_cursor_time_us),
        client_knowledge=_project_timestamp(
            timing.client_knowledge,
            render_cursor_time_us,
        ),
        policy_visible_at_time_us=policy_visible_at_time_us,
        render_cursor_time_us=render_cursor_time_us,
        action_time_us=action_time_us,
    )


def _project_timestamp(
    timestamp: EvidenceTimestamp,
    render_cursor_time_us: int,
) -> EvidenceTimestamp:
    if type(timestamp) is not EvidenceTimestamp:
        raise TypeError("projected timing value is invalid")
    if timestamp.time_us is not None and timestamp.time_us > render_cursor_time_us:
        return EvidenceTimestamp.unavailable(NOT_KNOWN_AT_RENDER_CURSOR)
    return timestamp


__all__ = [
    "DATA_AGE_SCHEMA_ID",
    "DATA_AGE_SCHEMA_VERSION",
    "NOT_KNOWN_AT_RENDER_CURSOR",
    "NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE",
    "DataAge",
    "EvidenceTimestamp",
    "EvidenceTiming",
    "TimestampAvailability",
    "TimestampAbsenceReason",
    "build_data_age",
]
