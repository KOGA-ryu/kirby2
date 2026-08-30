"""Deterministic indexing and verification for mechanistic replay traces."""

from __future__ import annotations

from dataclasses import dataclass

from .lineage import build_player_action_trace
from .models import MechanisticTraceIndex, TraceSourceRecording


@dataclass(frozen=True, slots=True)
class TraceIndexVerification:
    source_run_match: bool
    source_digest_match: bool
    index_identity_match: bool
    canonical_bytes_match: bool
    deterministic_rebuild: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.source_run_match,
                self.source_digest_match,
                self.index_identity_match,
                self.canonical_bytes_match,
                self.deterministic_rebuild,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "canonical_bytes_match": self.canonical_bytes_match,
            "deterministic_rebuild": self.deterministic_rebuild,
            "failures": list(self.failures),
            "index_identity_match": self.index_identity_match,
            "source_digest_match": self.source_digest_match,
            "source_run_match": self.source_run_match,
            "status": "PASS" if self.passed else "FAIL",
        }


def build_trace_index(source: TraceSourceRecording) -> MechanisticTraceIndex:
    if not isinstance(source, TraceSourceRecording):
        raise TypeError("mechanistic trace indexing requires TraceSourceRecording")
    traces = tuple(
        build_player_action_trace(source, action_id)
        for action_id in source.player_action_ids
    )
    return MechanisticTraceIndex(
        source_run_id=source.run_id,
        source_event_sha256=source.source_event_sha256,
        traces=traces,
    )


def verify_trace_index(
    source: TraceSourceRecording,
    index: MechanisticTraceIndex,
) -> TraceIndexVerification:
    rebuilt = build_trace_index(source)
    source_run_match = index.source_run_id == source.run_id
    source_digest_match = index.source_event_sha256 == source.source_event_sha256
    index_identity_match = index.index_id == rebuilt.index_id
    canonical_bytes_match = index.canonical_bytes() == rebuilt.canonical_bytes()
    deterministic_rebuild = build_trace_index(source).canonical_bytes() == (
        rebuilt.canonical_bytes()
    )
    failures: list[str] = []
    if not source_run_match:
        failures.append("trace index points at another source run")
    if not source_digest_match:
        failures.append("trace index source-event digest differs")
    if not index_identity_match:
        failures.append("trace index identity differs from canonical rebuild")
    if not canonical_bytes_match:
        failures.append("trace index bytes differ from canonical rebuild")
    if not deterministic_rebuild:
        failures.append("repeated trace index rebuild diverged")
    return TraceIndexVerification(
        source_run_match,
        source_digest_match,
        index_identity_match,
        canonical_bytes_match,
        deterministic_rebuild,
        tuple(failures),
    )
