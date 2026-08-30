"""Runtime acceptance audit for the mechanistic replay trace index."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from kirby2.immutable import thaw_json
from kirby2.microscope import (
    MECHANISTIC_INTERPRETATION,
    TRACE_EDGE_ORDER,
    TRACE_INDEX_SCHEMA_ID,
    TRACE_INDEX_SCHEMA_VERSION,
    TRACE_STAGE_ORDER,
    RecordedTraceEvent,
    TraceAvailability,
    TraceLinkStatus,
    TraceSourceRecording,
    TraceStage,
    TraceUnavailableReason,
    build_trace_index,
    complete_trace_fixture,
    incomplete_legacy_trace_fixture,
    verify_trace_index,
)


WO36A_AUDIT_CASE_COUNT = 5
WO36A_COMPLETE_SOURCE_SHA256 = (
    "92598dc970bb36eee6cfbd2cbc017c4a333bf4d11eedf20a14842d63094beefc"
)
WO36A_COMPLETE_INDEX_SHA256 = (
    "69c1184e29706118fab8c324587677d3170c0f031924c020001eb02e84f65d80"
)
WO36A_LEGACY_SOURCE_SHA256 = (
    "eeb4028d440c198ff39c7b5f2ebd9298631660e4f56b34b29db967358e8136c0"
)
WO36A_LEGACY_INDEX_SHA256 = (
    "422b1893997dfae18876ddb6542b6480274da32da08922bf48c8cb6f109230a2"
)


@dataclass(frozen=True, slots=True)
class ReplayMicroscopeAuditCase:
    name: str
    detail: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "detail": self.detail,
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_replay_microscope() -> tuple[ReplayMicroscopeAuditCase, ...]:
    cases = (
        _complete_fixture_case(),
        _legacy_unavailable_case(),
        _deterministic_rebuild_case(),
        _identity_binding_case(),
        _source_ownership_case(),
    )
    if len(cases) != WO36A_AUDIT_CASE_COUNT:
        raise RuntimeError("WO36-A audit case inventory changed")
    return cases


def _complete_fixture_case() -> ReplayMicroscopeAuditCase:
    source = complete_trace_fixture()
    source_before = source.canonical_bytes()
    index = build_trace_index(source)
    verification = verify_trace_index(source, index)
    source_events = {item.event_id: item for item in source.events}
    failures: list[str] = []
    if source.source_event_sha256 != WO36A_COMPLETE_SOURCE_SHA256:
        failures.append("complete fixture source digest drifted")
    if _sha256(index.canonical_bytes()) != WO36A_COMPLETE_INDEX_SHA256:
        failures.append("complete fixture index digest drifted")
    if index.interpretation != MECHANISTIC_INTERPRETATION:
        failures.append("mechanistic interpretation label changed")
    if len(index.traces) != len(source.player_action_ids) or len(index.traces) != 2:
        failures.append("complete fixture did not index every player action")
    if index.complete_action_count != len(index.traces):
        failures.append("complete fixture contains an unavailable required edge")
    if not verification.passed:
        failures.extend(verification.failures)
    for trace in index.traces:
        if tuple(item.stage for item in trace.nodes) != TRACE_STAGE_ORDER:
            failures.append(f"{trace.action_id} node inventory changed")
        if tuple(item.kind for item in trace.edges) != TRACE_EDGE_ORDER:
            failures.append(f"{trace.action_id} edge inventory changed")
        decision_node = trace.nodes[
            TRACE_STAGE_ORDER.index(TraceStage.STRATEGY_RULE_EVALUATION)
        ]
        decision_event = (
            None
            if decision_node.source_event_id is None
            else source_events[decision_node.source_event_id]
        )
        if (
            decision_node.availability is not TraceAvailability.RECORDED
            or decision_event is None
            or decision_event.artifact_kind.value
            != "RECORDED_STRATEGY_DECISION"
        ):
            failures.append(
                f"{trace.action_id} explanation was not a recorded decision artifact"
            )
        for edge in trace.edges:
            if edge.status is not TraceLinkStatus.LINKED:
                failures.append(f"{trace.action_id} {edge.kind.value} was unavailable")
                continue
            if (
                edge.from_event_id is None
                or edge.to_event_id is None
                or not edge.correlation_ids
                or len(edge.provenance) != 2
            ):
                failures.append(f"{trace.action_id} {edge.kind.value} lacks sources")
                continue
            right = source_events[edge.to_event_id]
            if edge.from_event_id not in right.parent_event_ids:
                failures.append(
                    f"{trace.action_id} {edge.kind.value} lacks explicit parentage"
                )
    if source.canonical_bytes() != source_before:
        failures.append("indexing modified the immutable complete source")
    detail = (
        f"source_sha256={source.source_event_sha256} index_id={index.index_id} "
        f"actions={len(index.traces)} complete={index.complete_action_count} "
        f"nodes={sum(len(item.nodes) for item in index.traces)} "
        f"edges={sum(len(item.edges) for item in index.traces)}"
    )
    return ReplayMicroscopeAuditCase(
        "complete_fixture_links_every_required_action_edge",
        detail,
        {
            "action_count": len(index.traces),
            "complete_action_count": index.complete_action_count,
            "index_id": index.index_id,
            "index_sha256": _sha256(index.canonical_bytes()),
            "interpretation": index.interpretation,
            "source_event_sha256": source.source_event_sha256,
            "verification": verification.as_dict(),
        },
        tuple(failures),
    )


def _legacy_unavailable_case() -> ReplayMicroscopeAuditCase:
    source = incomplete_legacy_trace_fixture()
    source_before = source.canonical_bytes()
    index = build_trace_index(source)
    trace = index.traces[0]
    failures: list[str] = []
    if source.source_event_sha256 != WO36A_LEGACY_SOURCE_SHA256:
        failures.append("legacy fixture source digest drifted")
    if _sha256(index.canonical_bytes()) != WO36A_LEGACY_INDEX_SHA256:
        failures.append("legacy fixture index digest drifted")
    if trace.complete:
        failures.append("incomplete legacy trace was reported complete")
    if trace.unavailable_node_count != 4:
        failures.append("legacy missing-node inventory changed")
    if trace.unavailable_edge_count != 9:
        failures.append("legacy unavailable-edge inventory changed")
    observation_edge = trace.edges[0]
    if (
        observation_edge.status is not TraceLinkStatus.UNAVAILABLE
        or observation_edge.unavailable_reason
        is not TraceUnavailableReason.EXPLICIT_PARENT_LINK_MISSING
    ):
        failures.append("adjacent legacy events were treated as causal")
    decision_node = trace.nodes[
        TRACE_STAGE_ORDER.index(TraceStage.STRATEGY_RULE_EVALUATION)
    ]
    if (
        decision_node.availability is not TraceAvailability.UNAVAILABLE
        or decision_node.unavailable_reason
        is not TraceUnavailableReason.SOURCE_EVENT_MISSING
    ):
        failures.append("missing recorded strategy explanation was recomputed")
    if any(
        item.status is TraceLinkStatus.UNAVAILABLE
        and item.unavailable_reason is None
        for item in trace.edges
    ):
        failures.append("legacy unavailable edge lacks a typed reason")
    if source.canonical_bytes() != source_before:
        failures.append("indexing modified the immutable legacy source")
    detail = (
        f"source_sha256={source.source_event_sha256} index_id={index.index_id} "
        f"unavailable_nodes={trace.unavailable_node_count} "
        f"unavailable_edges={trace.unavailable_edge_count} "
        "timestamp_inference=REFUSED"
    )
    return ReplayMicroscopeAuditCase(
        "legacy_gaps_are_unavailable_without_timestamp_inference",
        detail,
        {
            "index_id": index.index_id,
            "index_sha256": _sha256(index.canonical_bytes()),
            "missing_decision_reason": decision_node.unavailable_reason.value,
            "nearby_event_link_status": observation_edge.status.value,
            "nearby_event_reason": observation_edge.unavailable_reason.value,
            "source_event_sha256": source.source_event_sha256,
            "unavailable_edge_count": trace.unavailable_edge_count,
            "unavailable_node_count": trace.unavailable_node_count,
        },
        tuple(failures),
    )


def _deterministic_rebuild_case() -> ReplayMicroscopeAuditCase:
    source = complete_trace_fixture()
    reordered = TraceSourceRecording(source.run_id, tuple(reversed(source.events)))
    first = build_trace_index(source)
    second = build_trace_index(source)
    reordered_index = build_trace_index(reordered)
    source_equal = source.canonical_bytes() == reordered.canonical_bytes()
    repeated_equal = first.canonical_bytes() == second.canonical_bytes()
    reordered_equal = first.canonical_bytes() == reordered_index.canonical_bytes()
    failures: list[str] = []
    if not source_equal:
        failures.append("source canonicalization depends on caller event order")
    if not repeated_equal:
        failures.append("repeated index rebuild changed canonical bytes")
    if not reordered_equal:
        failures.append("index rebuild depends on caller event order")
    if first.index_id != second.index_id or first.index_id != reordered_index.index_id:
        failures.append("deterministic rebuild changed index identity")
    detail = (
        f"index_id={first.index_id} repeated={'PASS' if repeated_equal else 'FAIL'} "
        f"reordered={'PASS' if reordered_equal else 'FAIL'}"
    )
    return ReplayMicroscopeAuditCase(
        "index_rebuild_is_canonical_and_deterministic",
        detail,
        {
            "caller_order_canonicalized": source_equal,
            "index_id": first.index_id,
            "reordered_equal": reordered_equal,
            "repeated_equal": repeated_equal,
        },
        tuple(failures),
    )


def _identity_binding_case() -> ReplayMicroscopeAuditCase:
    source = complete_trace_fixture()
    original = build_trace_index(source)
    changed_event = _changed_first_event(source.events[0])
    changed_source = TraceSourceRecording(
        source.run_id,
        (changed_event, *source.events[1:]),
    )
    changed = build_trace_index(changed_source)
    other_run_id = "run-" + _sha256(b"wo36-a-other-source-run")[:24]
    retargeted_events = tuple(
        replace(
            item,
            provenance=replace(item.provenance, run_id=other_run_id),
        )
        for item in source.events
    )
    retargeted_source = TraceSourceRecording(other_run_id, retargeted_events)
    retargeted = build_trace_index(retargeted_source)
    identity = original.identity_dict()
    event_digest_bound = (
        original.source_event_sha256 != changed.source_event_sha256
        and original.index_id != changed.index_id
    )
    run_bound = (
        original.source_run_id != retargeted.source_run_id
        and original.index_id != retargeted.index_id
    )
    schema_bound = (
        identity["schema_id"] == TRACE_INDEX_SCHEMA_ID
        and identity["schema_version"] == TRACE_INDEX_SCHEMA_VERSION
        and set(identity)
        == {
            "interpretation",
            "lineage_sha256",
            "schema_id",
            "schema_version",
            "source_event_sha256",
            "source_run_id",
        }
    )
    failures: list[str] = []
    if not event_digest_bound:
        failures.append("index identity does not bind source event bytes")
    if not run_bound:
        failures.append("index identity does not bind source run identity")
    if not schema_bound:
        failures.append("index identity omits its exact schema contract")
    detail = (
        f"original={original.index_id} changed_event={changed.index_id} "
        f"changed_run={retargeted.index_id} event_bound={event_digest_bound} "
        f"run_bound={run_bound} schema_bound={schema_bound}"
    )
    return ReplayMicroscopeAuditCase(
        "index_identity_binds_source_run_events_and_schema",
        detail,
        {
            "changed_event_index_id": changed.index_id,
            "changed_run_index_id": retargeted.index_id,
            "event_digest_bound": event_digest_bound,
            "original_index_id": original.index_id,
            "run_bound": run_bound,
            "schema_bound": schema_bound,
        },
        tuple(failures),
    )


def _source_ownership_case() -> ReplayMicroscopeAuditCase:
    original_source = complete_trace_fixture()
    first = original_source.events[0]
    mutable_payload = thaw_json(first.payload)
    if not isinstance(mutable_payload, dict):
        raise RuntimeError("fixture trace payload is not an object")
    copied_first = replace(first, payload=mutable_payload)
    source = TraceSourceRecording(
        original_source.run_id,
        (copied_first, *original_source.events[1:]),
    )
    before = source.canonical_bytes()
    mutable_payload["best_bid_ticks"] = -1
    exported = source.as_dict()
    exported_events = exported["events"]
    if not isinstance(exported_events, list) or not isinstance(
        exported_events[0],
        dict,
    ):
        raise RuntimeError("trace source export is not detached JSON")
    exported_events[0]["payload"] = {"tampered": True}
    direct_mutation_rejected = False
    try:
        copied_first.payload["tampered"] = True
    except TypeError:
        direct_mutation_rejected = True
    index = build_trace_index(source)
    source_unchanged = before == source.canonical_bytes()
    failures: list[str] = []
    if not source_unchanged:
        failures.append("caller or export mutation changed owned source evidence")
    if not direct_mutation_rejected:
        failures.append("owned source payload accepted direct mutation")
    if index.source_event_sha256 != source.source_event_sha256:
        failures.append("index source digest differs after ownership checks")
    detail = (
        f"source_unchanged={source_unchanged} "
        f"direct_mutation_rejected={direct_mutation_rejected} "
        f"source_sha256={source.source_event_sha256}"
    )
    return ReplayMicroscopeAuditCase(
        "source_evidence_is_owned_detached_and_unmodified",
        detail,
        {
            "direct_mutation_rejected": direct_mutation_rejected,
            "index_source_sha256": index.source_event_sha256,
            "source_sha256": source.source_event_sha256,
            "source_unchanged": source_unchanged,
        },
        tuple(failures),
    )


def _changed_first_event(event: RecordedTraceEvent) -> RecordedTraceEvent:
    payload = thaw_json(event.payload)
    if not isinstance(payload, dict):
        raise RuntimeError("fixture trace payload is not an object")
    payload["best_bid_ticks"] = 9_999
    return replace(event, payload=payload)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
