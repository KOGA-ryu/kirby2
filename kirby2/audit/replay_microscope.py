"""Runtime acceptance audit for the mechanistic replay trace index."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from dataclasses import dataclass, replace
from pathlib import Path

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
from kirby2.microscope.data_age import (
    NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE,
    EvidenceTimestamp,
    EvidenceTiming,
    TimestampAbsenceReason,
    TimestampAvailability,
)
from kirby2.microscope.ingestion import (
    OBSERVED_INGEST_ADAPTER_ID,
    OBSERVED_INGEST_ADAPTER_VERSION,
    OBSERVED_INGEST_MANIFEST_SCHEMA_ID,
    OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION,
    ObservationIngestionReceipt,
    ObservedArtifactBytes,
    VerifiedObservationSource,
    load_verified_observation_source,
    verify_observation_ingestion,
)
from kirby2.microscope.policy import (
    AS_OBSERVED_POLICY_ID,
    OBSERVATION_POLICY_SCHEMA_ID,
    POSTMORTEM_POLICY_ID,
    ObservationMode,
    ObservationPolicy,
    ReplaySourceCapabilityManifest,
    RevealAuthorization,
    RevealAvailability,
    RevealCapability,
    RevealUnavailableReason,
    SourceCapabilityAvailability,
    SourceCapabilityEvidence,
    SourceCapabilityUnavailableReason,
)
from kirby2.microscope.query import (
    CLIENT_DELIVERED_ARTIFACT_SCHEMA_ID,
    DECISION_SNAPSHOT_ARTIFACT_SCHEMA_ID,
    OBSERVATION_QUERY_SCHEMA_ID,
    EvidenceSourceKind,
    ObservationQueryRequest,
    ObservationQueryResult,
    ObservedEvidenceSet,
    ObservedValueRecord,
    RecordDisposition,
    RevealDecision,
    RevealEvidenceSet,
    RevealValueRecord,
    SelectionKind,
    query_as_observed,
    query_postmortem,
    reveal_artifact_sha256,
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
WO36B_AUDIT_CASE_COUNT = 6
DEV0006_AUDIT_CASE_COUNT = 4


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


@dataclass(frozen=True, slots=True)
class _ObservedIngressFixture:
    observed: ObservedEvidenceSet
    manifest_bytes: bytes
    manifest_sha256: str
    artifacts: tuple[ObservedArtifactBytes, ...]
    client_artifact_id: str
    decision_artifact_id: str


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


def audit_replay_observation_policies() -> tuple[ReplayMicroscopeAuditCase, ...]:
    """Run the fixed WO36-B observation/reveal policy attack inventory."""

    cases = (
        _observed_source_separation_case(),
        _postmortem_authorization_case(),
        _client_knowledge_cutoff_case(),
        _held_last_known_case(),
        _mode_provenance_case(),
        _historical_hidden_unavailable_case(),
    )
    if len(cases) != WO36B_AUDIT_CASE_COUNT:
        raise RuntimeError("WO36-B audit case inventory changed")
    expected_names = (
        "observed_query_uses_closed_client_sources_only",
        "postmortem_requires_capability_and_bound_authorization",
        "client_knowledge_time_controls_observed_visibility",
        "held_last_known_never_interpolates_evidence",
        "mode_provenance_survives_every_metadata_surface",
        "historical_hidden_state_remains_unavailable",
    )
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO36-B audit case order or identity changed")
    return cases


def audit_replay_observation_ingestion() -> tuple[ReplayMicroscopeAuditCase, ...]:
    """Run the fixed DEV-0006 immutable observed-source attack inventory."""

    cases = (
        _verified_ingress_binding_case(),
        _ingress_tamper_case(),
        _ingress_wire_contract_case(),
        _ingress_query_facade_case(),
    )
    if len(cases) != DEV0006_AUDIT_CASE_COUNT:
        raise RuntimeError("DEV-0006 audit case inventory changed")
    expected_names = (
        "pinned_manifest_binds_raw_and_normalized_observed_planes",
        "manifest_and_artifact_tampering_fails_closed",
        "wire_schema_semantics_and_source_identity_fail_closed",
        "query_facade_revalidates_bytes_and_hides_raw_evidence",
    )
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("DEV-0006 audit case order or identity changed")
    return cases


def _verified_ingress_binding_case() -> ReplayMicroscopeAuditCase:
    fixture = _observed_ingress_fixture()
    source = load_verified_observation_source(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        fixture.artifacts,
    )
    request = ObservationQueryRequest(500, action_time_us=300)
    expected = query_as_observed(fixture.observed, request)
    result = source.query(request)
    repeated = load_verified_observation_source(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        tuple(reversed(fixture.artifacts)),
    )
    receipt = verify_observation_ingestion(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        fixture.artifacts,
    )
    repeated_receipt = verify_observation_ingestion(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        tuple(reversed(fixture.artifacts)),
    )
    empty_fixture = _observed_ingress_fixture(
        ObservedEvidenceSet(
            fixture.observed.source_run_id,
            fixture.observed.source_event_sha256,
            client_delivered=fixture.observed.client_delivered,
        )
    )
    empty_source = load_verified_observation_source(
        empty_fixture.manifest_bytes,
        empty_fixture.manifest_sha256,
        empty_fixture.artifacts,
    )
    empty_receipt = verify_observation_ingestion(
        empty_fixture.manifest_bytes,
        empty_fixture.manifest_sha256,
        empty_fixture.artifacts,
    )
    receipt_binding = (
        type(receipt) is ObservationIngestionReceipt
        and receipt.manifest_sha256 == fixture.manifest_sha256
        and receipt.evidence_sha256 == fixture.observed.evidence_sha256
        and receipt.client_delivered_raw_sha256
        == fixture.artifacts[0].sha256
        and receipt.client_delivered_normalized_plane_sha256
        == fixture.observed.client_delivered_artifact_sha256
        and receipt.decision_snapshot_raw_sha256
        == fixture.artifacts[1].sha256
        and receipt.decision_snapshot_normalized_plane_sha256
        == fixture.observed.decision_snapshot_artifact_sha256
        and receipt.client_delivered_record_count
        == len(fixture.observed.client_delivered)
        and receipt.decision_snapshot_record_count
        == len(fixture.observed.decision_snapshots)
    )
    deterministic = (
        result.canonical_bytes() == expected.canonical_bytes()
        and source.canonical_bytes(request) == repeated.canonical_bytes(request)
        and receipt.canonical_bytes() == repeated_receipt.canonical_bytes()
    )
    recorded_empty_preserved = (
        empty_receipt.decision_snapshot_record_count == 0
        and empty_receipt.decision_snapshot_raw_sha256
        == empty_fixture.artifacts[1].sha256
        and empty_source.query(request).policy.mode is ObservationMode.AS_OBSERVED
    )
    failures: list[str] = []
    if type(source) is not VerifiedObservationSource:
        failures.append("verified ingestion returned an open or substituted service")
    if not receipt_binding:
        failures.append("ingestion receipt lost a raw or normalized source binding")
    if not deterministic:
        failures.append("repeated ingestion/query output differs from canonical evidence")
    if not recorded_empty_preserved:
        failures.append("a recorded empty plane was confused with source omission")
    return ReplayMicroscopeAuditCase(
        "pinned_manifest_binds_raw_and_normalized_observed_planes",
        (
            f"records={receipt.client_delivered_record_count}+"
            f"{receipt.decision_snapshot_record_count} "
            f"receipt={receipt.receipt_sha256[:24]} deterministic={deterministic}"
        ),
        {
            "adapter_id": OBSERVED_INGEST_ADAPTER_ID,
            "adapter_version": OBSERVED_INGEST_ADAPTER_VERSION,
            "deterministic": deterministic,
            "manifest_sha256": fixture.manifest_sha256,
            "pin_origin_authenticated_by_loader": False,
            "receipt": receipt.as_dict(),
            "receipt_binding": receipt_binding,
            "receipt_cursor_safe": False,
            "recorded_empty_plane_preserved": recorded_empty_preserved,
            "result_query_id": result.query_id,
        },
        tuple(failures),
    )


def _ingress_tamper_case() -> ReplayMicroscopeAuditCase:
    fixture = _observed_ingress_fixture()
    rewritten = _rewrite_client_source_artifact(
        fixture,
        _rewrite_first_best_bid,
    )
    client, decisions = fixture.artifacts
    probes = {
        "manifest_bytes": _ingestion_rejected(
            lambda: load_verified_observation_source(
                fixture.manifest_bytes + b"\n",
                fixture.manifest_sha256,
                fixture.artifacts,
            )
        ),
        "artifact_bytes": _ingestion_rejected(
            lambda: load_verified_observation_source(
                fixture.manifest_bytes,
                fixture.manifest_sha256,
                (
                    ObservedArtifactBytes(client.artifact_id, client.raw_bytes + b"\n"),
                    decisions,
                ),
            )
        ),
        "self_consistent_cotamper": _ingestion_rejected(
            lambda: load_verified_observation_source(
                rewritten.manifest_bytes,
                fixture.manifest_sha256,
                rewritten.artifacts,
            )
        ),
        "swapped_planes": _ingestion_rejected(
            lambda: load_verified_observation_source(
                fixture.manifest_bytes,
                fixture.manifest_sha256,
                (
                    ObservedArtifactBytes(client.artifact_id, decisions.raw_bytes),
                    ObservedArtifactBytes(decisions.artifact_id, client.raw_bytes),
                ),
            )
        ),
        "missing_plane": _ingestion_rejected(
            lambda: load_verified_observation_source(
                fixture.manifest_bytes,
                fixture.manifest_sha256,
                (client,),
            )
        ),
        "duplicate_artifact_id": _ingestion_rejected(
            lambda: load_verified_observation_source(
                fixture.manifest_bytes,
                fixture.manifest_sha256,
                (client, ObservedArtifactBytes(client.artifact_id, decisions.raw_bytes)),
            )
        ),
        "extra_artifact": _ingestion_rejected(
            lambda: load_verified_observation_source(
                fixture.manifest_bytes,
                fixture.manifest_sha256,
                (
                    *fixture.artifacts,
                    ObservedArtifactBytes("wo36b.observed.extra.v1", b"{}"),
                ),
            )
        ),
    }
    failures = [
        f"ingestion accepted {name.replace('_', ' ')} tampering"
        for name, rejected in probes.items()
        if not rejected
    ]
    return ReplayMicroscopeAuditCase(
        "manifest_and_artifact_tampering_fails_closed",
        f"attacks={len(probes)} rejected={sum(probes.values())}",
        {
            "independent_manifest_pin": fixture.manifest_sha256,
            "rejections": probes,
            "rewritten_manifest_sha256": rewritten.manifest_sha256,
        },
        tuple(failures),
    )


def _ingress_wire_contract_case() -> ReplayMicroscopeAuditCase:
    fixture = _observed_ingress_fixture()

    def unknown_kind(payload: dict[str, object]) -> None:
        _first_source_record(payload)["record_kind"] = "REVEALED_GROUND_TRUTH"

    def truth_smuggle(payload: dict[str, object]) -> None:
        record = _first_source_record(payload)
        value = _source_payload(record)
        value["reserve_quantity"] = 999
        record["payload_sha256"] = _audit_sha256(value)

    def foreign_run(payload: dict[str, object]) -> None:
        payload["source_run_id"] = "run-111111111111111111111111"

    def boolean_sequence(payload: dict[str, object]) -> None:
        _first_source_record(payload)["sequence"] = True

    def descending_sequence(payload: dict[str, object]) -> None:
        records = payload["records"]
        if type(records) is list:
            records.reverse()

    def missing_receive(payload: dict[str, object]) -> None:
        timing = _source_timing(_first_source_record(payload))
        timing["client_receive"] = {
            "availability": "UNAVAILABLE",
            "reason": "NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE",
            "time_us": None,
        }

    def incompatible_delivered_venue_reason(payload: dict[str, object]) -> None:
        timing = _source_timing(_first_source_record(payload))
        timing["venue_receipt"] = {
            "availability": "NOT_APPLICABLE",
            "reason": "CLIENT_DECISION",
            "time_us": None,
        }

    def inverted_delivered_hops(payload: dict[str, object]) -> None:
        timing = _source_timing(_first_source_record(payload))
        venue_receipt = timing.get("venue_receipt")
        client_knowledge = timing.get("client_knowledge")
        if type(venue_receipt) is not dict or type(client_knowledge) is not dict:
            raise RuntimeError("source timing fixture lacks recorded hops")
        venue_receipt["time_us"] = 225
        client_knowledge["time_us"] = 250

    def neutral_unavailable_venue(payload: dict[str, object]) -> None:
        timing = _source_timing(_first_source_record(payload))
        timing["venue_receipt"] = {
            "availability": "UNAVAILABLE",
            "reason": "NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE",
            "time_us": None,
        }

    def client_local_no_venue_hop(payload: dict[str, object]) -> None:
        record = _source_record_for_series(payload, "feature.imbalance")
        timing = _source_timing(record)
        timing["venue_receipt"] = {
            "availability": "NOT_APPLICABLE",
            "reason": "NO_VENUE_HOP",
            "time_us": None,
        }

    def float_payload(payload: dict[str, object]) -> None:
        record = _first_source_record(payload)
        value = _source_payload(record)
        value["best_bid_ticks"] = 100.5
        record["payload_sha256"] = _audit_sha256(value)

    def wrong_record_plane(payload: dict[str, object]) -> None:
        record = _first_source_record(payload)
        record["record_kind"] = "STRATEGY_SIGNAL"
        record["series_id"] = "strategy.signal"
        record["payload"] = {"recorded_signal": "GREEN"}
        record["payload_sha256"] = _audit_sha256(record["payload"])

    def incompatible_decision_timing(payload: dict[str, object]) -> None:
        timing = _source_timing(_first_source_record(payload))
        timing["client_receive"] = {
            "availability": "NOT_APPLICABLE",
            "reason": "OUTBOUND_CLIENT_INTENTION",
            "time_us": None,
        }

    def duplicate_cross_plane_sequence(payload: dict[str, object]) -> None:
        _first_source_record(payload)["sequence"] = 1

    def foreign_decision_source(payload: dict[str, object]) -> None:
        payload["source_event_sha256"] = "1" * 64

    malformed_sources = {
        "unknown_record_kind": unknown_kind,
        "truth_payload_smuggling": truth_smuggle,
        "foreign_run": foreign_run,
        "boolean_sequence": boolean_sequence,
        "descending_sequence": descending_sequence,
        "missing_client_receive": missing_receive,
        "incompatible_delivered_venue_reason": (
            incompatible_delivered_venue_reason
        ),
        "inverted_delivered_hops": inverted_delivered_hops,
        "binary_float_payload": float_payload,
        "record_kind_in_wrong_plane": wrong_record_plane,
    }
    probes = {
        name: _ingestion_rejected(
            lambda mutation=mutation: _load_ingress_fixture(
                _rewrite_client_source_artifact(fixture, mutation)
            )
        )
        for name, mutation in malformed_sources.items()
    }
    decision_sources = {
        "incompatible_decision_timing": incompatible_decision_timing,
        "duplicate_cross_plane_sequence": duplicate_cross_plane_sequence,
        "foreign_decision_source": foreign_decision_source,
    }
    probes.update(
        {
            name: _ingestion_rejected(
                lambda mutation=mutation: _load_ingress_fixture(
                    _rewrite_decision_source_artifact(fixture, mutation)
                )
            )
            for name, mutation in decision_sources.items()
        }
    )
    valid_venue_semantics = {
        "neutral_unavailable": not _ingestion_rejected(
            lambda: _load_ingress_fixture(
                _rewrite_client_source_artifact(
                    fixture,
                    neutral_unavailable_venue,
                )
            )
        ),
        "client_local_no_venue_hop": not _ingestion_rejected(
            lambda: _load_ingress_fixture(
                _rewrite_client_source_artifact(
                    fixture,
                    client_local_no_venue_hop,
                )
            )
        ),
    }

    unknown_manifest = _rewrite_ingest_manifest(
        fixture,
        lambda payload: payload.__setitem__("unexpected", "field"),
    )
    dynamic_adapter = _rewrite_ingest_manifest(
        fixture,
        lambda payload: payload.__setitem__(
            "adapter_id",
            "python.module:CallerSelectedAdapter",
        ),
    )
    wrong_schema = _rewrite_ingest_manifest(
        fixture,
        lambda payload: payload.__setitem__("schema_version", 2),
    )
    boolean_schema = _rewrite_ingest_manifest(
        fixture,
        lambda payload: payload.__setitem__("schema_version", True),
    )
    foreign_manifest = _rewrite_ingest_manifest(
        fixture,
        lambda payload: payload.__setitem__(
            "source_run_id",
            "run-111111111111111111111111",
        ),
    )

    def duplicate_role(payload: dict[str, object]) -> None:
        artifacts = payload.get("artifacts")
        if type(artifacts) is not list or type(artifacts[1]) is not dict:
            raise RuntimeError("manifest fixture lacks its second artifact")
        artifacts[1]["artifact_kind"] = "CLIENT_DELIVERED"
        artifacts[1]["artifact_schema_id"] = (
            "KIRBY2_OBSERVED_CLIENT_DELIVERED_SOURCE_ARTIFACT_V1"
        )

    duplicate_role_manifest = _rewrite_ingest_manifest(fixture, duplicate_role)

    def omitted_role(payload: dict[str, object]) -> None:
        artifacts = payload.get("artifacts")
        if type(artifacts) is not list:
            raise RuntimeError("manifest fixture lacks its artifact inventory")
        artifacts.pop()

    omitted_role_manifest = _rewrite_ingest_manifest(fixture, omitted_role)
    duplicate_key_manifest = (
        b'{"adapter_id":"duplicate",' + fixture.manifest_bytes[1:]
    )
    bom_manifest = b"\xef\xbb\xbf" + fixture.manifest_bytes
    trailing_manifest = fixture.manifest_bytes + b"{}"
    probes.update(
        {
            "unknown_manifest_field": _ingestion_rejected(
                lambda: _load_ingress_fixture(unknown_manifest)
            ),
            "dynamic_adapter": _ingestion_rejected(
                lambda: _load_ingress_fixture(dynamic_adapter)
            ),
            "unknown_schema": _ingestion_rejected(
                lambda: _load_ingress_fixture(wrong_schema)
            ),
            "boolean_schema_alias": _ingestion_rejected(
                lambda: _load_ingress_fixture(boolean_schema)
            ),
            "foreign_manifest_source": _ingestion_rejected(
                lambda: _load_ingress_fixture(foreign_manifest)
            ),
            "duplicate_artifact_role": _ingestion_rejected(
                lambda: _load_ingress_fixture(duplicate_role_manifest)
            ),
            "omitted_artifact_role": _ingestion_rejected(
                lambda: _load_ingress_fixture(omitted_role_manifest)
            ),
            "duplicate_json_key": _ingestion_rejected(
                lambda: load_verified_observation_source(
                    duplicate_key_manifest,
                    hashlib.sha256(duplicate_key_manifest).hexdigest(),
                    fixture.artifacts,
                )
            ),
            "utf8_bom": _ingestion_rejected(
                lambda: load_verified_observation_source(
                    bom_manifest,
                    hashlib.sha256(bom_manifest).hexdigest(),
                    fixture.artifacts,
                )
            ),
            "trailing_json": _ingestion_rejected(
                lambda: load_verified_observation_source(
                    trailing_manifest,
                    hashlib.sha256(trailing_manifest).hexdigest(),
                    fixture.artifacts,
                )
            ),
        }
    )
    failures = [
        f"wire contract accepted {name.replace('_', ' ')}"
        for name, rejected in probes.items()
        if not rejected
    ]
    failures.extend(
        f"wire contract rejected valid {name.replace('_', ' ')} venue timing"
        for name, accepted in valid_venue_semantics.items()
        if not accepted
    )
    return ReplayMicroscopeAuditCase(
        "wire_schema_semantics_and_source_identity_fail_closed",
        f"attacks={len(probes)} rejected={sum(probes.values())}",
        {
            "closed_adapter_registry": probes["dynamic_adapter"],
            "closed_record_registry": probes["unknown_record_kind"],
            "rejections": probes,
            "source_scope": "OBSERVED_ONLY",
            "valid_venue_semantics": valid_venue_semantics,
        },
        tuple(failures),
    )


def _ingress_query_facade_case() -> ReplayMicroscopeAuditCase:
    fixture = _observed_ingress_fixture()
    source = _load_ingress_fixture(fixture)
    backend_receipt = verify_observation_ingestion(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        fixture.artifacts,
    )
    request = ObservationQueryRequest(200, action_time_us=200)
    baseline = source.canonical_bytes(request)
    detached = source.query(request).export_payload()
    detached["values"] = []
    policy = detached.get("policy")
    if type(policy) is dict:
        policy["mode"] = "POSTMORTEM"
    detached_receipt = verify_observation_ingestion(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        fixture.artifacts,
    )
    object.__setattr__(detached_receipt, "evidence_sha256", "f" * 64)

    direct_record = min(
        fixture.observed.client_delivered,
        key=lambda item: item.sequence,
    )
    smuggled = {
        "future_fill": True,
        "hidden_reserve_quantity": 999,
    }
    object.__setattr__(direct_record, "payload", smuggled)
    object.__setattr__(direct_record, "payload_sha256", _audit_sha256(smuggled))
    direct_smuggle_visible = b"hidden_reserve_quantity" in query_as_observed(
        fixture.observed,
        request,
    ).canonical_bytes()
    service_revalidated = (
        source.canonical_bytes(request) == baseline
        and b"hidden_reserve_quantity" not in baseline
    )
    ui_findings = _ui_raw_evidence_imports()
    ui_guard_sources = {
        "absolute_module_alias": "import kirby2.microscope.query as raw_query\n",
        "parent_submodule_alias": (
            "from kirby2.microscope import query as raw_query\n"
        ),
        "raw_query_entrypoint": (
            "from kirby2.microscope.query import query_as_observed\n"
        ),
        "relative_raw_constructor": (
            "from ..microscope.query import ObservedEvidenceSet\n"
        ),
        "relative_private_ingestion": (
            "from ..microscope.ingestion import _ingest_verified\n"
        ),
        "root_package_alias": "from kirby2 import microscope\n",
        "root_package_import": "import kirby2\n",
        "root_package_import_alias": "import kirby2 as package_root\n",
        "sibling_import_exposes_root": "import kirby2.session.live\n",
        "backend_receipt_api": (
            "from kirby2.microscope.ingestion import "
            "verify_observation_ingestion\n"
        ),
        "unlisted_policy_constructor": (
            "from kirby2.microscope.policy import "
            "ReplaySourceCapabilityManifest\n"
        ),
        "unlisted_query_constructor": (
            "from kirby2.microscope.query import QueriedValue\n"
        ),
        "future_parent_reexport": (
            "from kirby2.microscope import ObservedEvidenceSet\n"
        ),
        "safe_query_dto": (
            "from kirby2.microscope.query import ObservationQueryResult\n"
        ),
        "safe_policy_enum": (
            "from kirby2.microscope.policy import ObservationMode\n"
        ),
    }
    ui_guard_detected = {
        name: bool(_raw_evidence_imports_in_source(source_text, f"{name}.py"))
        for name, source_text in ui_guard_sources.items()
    }
    ui_guard_expected = {
        name: not name.startswith("safe_") for name in ui_guard_sources
    }
    ui_guard_regressions = {
        name: {
            "detected": ui_guard_detected[name],
            "expected": expected,
        }
        for name, expected in ui_guard_expected.items()
        if ui_guard_detected[name] is not expected
    }
    result_signature = tuple(inspect.signature(source.result).parameters)
    query_signature = tuple(inspect.signature(source.query).parameters)
    constructor_closed = _ingestion_rejected(
        lambda: VerifiedObservationSource(
            fixture.manifest_bytes,
            fixture.manifest_sha256,
            fixture.artifacts,
            backend_receipt,
        )
    )
    public_surface = tuple(
        sorted(name for name in dir(source) if not name.startswith("_"))
    )
    safe_repr = (
        "raw_bytes" not in repr(source)
        and "best_bid_ticks" not in repr(source)
        and fixture.manifest_bytes.decode("ascii") not in repr(source)
        and backend_receipt.receipt_sha256 not in repr(source)
        and backend_receipt.evidence_sha256 not in repr(source)
    )
    detached_result = source.canonical_bytes(request) == baseline
    backend_receipt_isolated = (
        backend_receipt.evidence_sha256 != "f" * 64
        and source.canonical_bytes(request) == baseline
    )
    facade_closed = (
        result_signature == ("render_cursor_time_us", "action_time_us")
        and query_signature == ("request",)
        and constructor_closed
        and public_surface == ("canonical_bytes", "query", "result")
    )
    failures: list[str] = []
    if not direct_smuggle_visible:
        failures.append("attack fixture no longer reproduces direct evidence laundering")
    if not service_revalidated:
        failures.append("verified query reused a mutated caller-held evidence object")
    if not detached_result:
        failures.append("mutating a detached result changed the next verified query")
    if not backend_receipt_isolated:
        failures.append("mutating a backend receipt changed the query-only source")
    if not facade_closed:
        failures.append("query facade accepts raw evidence, reveal state, or open construction")
    if not safe_repr:
        failures.append("verified source repr exposed private source bytes")
    if ui_findings:
        failures.append("first-party UI imports raw evidence or ingestion internals")
    if ui_guard_regressions:
        failures.append("first-party UI import guard missed a static bypass")
    return ReplayMicroscopeAuditCase(
        "query_facade_revalidates_bytes_and_hides_raw_evidence",
        (
            f"direct_attack={direct_smuggle_visible} revalidated={service_revalidated} "
            f"ui_findings={len(ui_findings)} "
            f"ui_guard={sum(ui_guard_detected.values())}/"
            f"{sum(ui_guard_expected.values())}"
        ),
        {
            "cooperative_process_boundary": True,
            "constructor_closed": constructor_closed,
            "detached_result": detached_result,
            "backend_receipt_isolated": backend_receipt_isolated,
            "direct_attack_reproduced": direct_smuggle_visible,
            "public_surface": list(public_surface),
            "query_signature": list(query_signature),
            "receipt_exposed_by_facade": "receipt" in public_surface,
            "result_signature": list(result_signature),
            "safe_repr": safe_repr,
            "service_revalidated": service_revalidated,
            "ui_raw_import_findings": ui_findings,
            "ui_import_guard": ui_guard_detected,
            "ui_import_guard_regressions": ui_guard_regressions,
        },
        tuple(failures),
    )


def _observed_source_separation_case() -> ReplayMicroscopeAuditCase:
    observed = _observed_policy_fixture()
    reveal = _reveal_policy_fixture(
        (RevealCapability.GROUND_TRUTH, RevealCapability.HIDDEN_STATE),
        include_hidden=True,
    )
    before = observed.canonical_bytes()
    reveal_record_type_rejected = False
    try:
        ObservedEvidenceSet(
            observed.source_run_id,
            observed.source_event_sha256,
            client_delivered=(reveal.values[0],),  # type: ignore[arg-type]
        )
    except TypeError:
        reveal_record_type_rejected = True
    result = query_as_observed(
        observed,
        ObservationQueryRequest(500, action_time_us=300),
    )
    encoded = result.canonical_bytes()
    signature = tuple(inspect.signature(query_as_observed).parameters)
    source_kinds = {item.source_kind.value for item in result.values}
    forbidden = (
        b"sealed-ground-truth-wo36b",
        b"sealed-hidden-state-wo36b",
        b"truth.best-bid",
        b"hidden.reserve-quantity",
        reveal.evidence_sha256.encode("ascii"),
    )
    failures: list[str] = []
    if signature != ("evidence", "request"):
        failures.append("as-observed entry point accepts something beyond observed evidence")
    if source_kinds != {
        "CLIENT_DELIVERED",
        "RECORDED_DECISION_SNAPSHOT",
    }:
        failures.append("as-observed query returned a non-client source kind")
    if any(marker in encoded for marker in forbidden):
        failures.append("as-observed result exposed reveal payload, identity, or digest")
    if before != observed.canonical_bytes():
        failures.append("as-observed query modified its immutable evidence source")
    if not reveal_record_type_rejected:
        failures.append("a reveal record crossed into a client-delivered artifact plane")
    if result.policy.mode is not ObservationMode.AS_OBSERVED:
        failures.append("as-observed query lost its enforced mode label")
    detail = (
        f"signature={','.join(signature)} values={len(result.values)} "
        f"source_kinds={','.join(sorted(source_kinds))} truth_bytes=ABSENT"
    )
    return ReplayMicroscopeAuditCase(
        "observed_query_uses_closed_client_sources_only",
        detail,
        {
            "observed_evidence_sha256": observed.evidence_sha256,
            "client_delivered_artifact_sha256": (
                observed.client_delivered_artifact_sha256
            ),
            "decision_snapshot_artifact_sha256": (
                observed.decision_snapshot_artifact_sha256
            ),
            "query_id": result.query_id,
            "query_parameters": list(signature),
            "reveal_digest_present": reveal.evidence_sha256.encode("ascii") in encoded,
            "reveal_record_type_rejected": reveal_record_type_rejected,
            "source_kinds": sorted(source_kinds),
            "truth_bytes_present": any(marker in encoded for marker in forbidden[:-1]),
        },
        tuple(failures),
    )


def _postmortem_authorization_case() -> ReplayMicroscopeAuditCase:
    observed = _observed_policy_fixture()
    absent = _reveal_policy_fixture((), include_hidden=False)
    present = _reveal_policy_fixture(
        (RevealCapability.GROUND_TRUTH,),
        include_hidden=False,
    )
    request = ObservationQueryRequest(
        500,
        action_time_us=300,
        requested_reveal_capabilities=(RevealCapability.GROUND_TRUTH,),
    )
    absent_without_grant = query_postmortem(observed, absent, None, request)
    present_without_grant = query_postmortem(observed, present, None, request)
    synthetic_grant = _reveal_authorization(
        absent,
        (RevealCapability.GROUND_TRUTH,),
        observed=observed,
    )
    absent_with_grant = query_postmortem(observed, absent, synthetic_grant, request)
    exact_grant = _reveal_authorization(
        present,
        (RevealCapability.GROUND_TRUTH,),
        observed=observed,
    )
    exact = query_postmortem(observed, present, exact_grant, request)
    overlapping_reveal = _reveal_policy_fixture(
        (RevealCapability.GROUND_TRUTH,),
        include_hidden=False,
        ground_series_id=observed.client_delivered[0].series_id,
    )
    cross_plane_series_rejected = False
    try:
        query_postmortem(
            observed,
            overlapping_reveal,
            _reveal_authorization(
                overlapping_reveal,
                (RevealCapability.GROUND_TRUTH,),
                observed=observed,
            ),
            request,
        )
    except ValueError:
        cross_plane_series_rejected = True
    altered_first = replace(
        observed.client_delivered[0],
        payload={"best_bid_ticks": 9_999, "sentinel": "altered-observed-source"},
    )
    altered_observed = replace(
        observed,
        client_delivered=(altered_first, *observed.client_delivered[1:]),
    )
    wrong_observed = query_postmortem(
        altered_observed,
        present,
        exact_grant,
        request,
    )
    wrong_run = query_postmortem(
        observed,
        present,
        _reveal_authorization(
            present,
            (RevealCapability.GROUND_TRUTH,),
            observed=observed,
            source_run_id="run-111111111111111111111111",
        ),
        request,
    )
    wrong_source = query_postmortem(
        observed,
        present,
        _reveal_authorization(
            present,
            (RevealCapability.GROUND_TRUTH,),
            observed=observed,
            source_event_sha256="1" * 64,
        ),
        request,
    )
    wrong_reveal = query_postmortem(
        observed,
        present,
        _reveal_authorization(
            present,
            (RevealCapability.GROUND_TRUTH,),
            observed=observed,
            reveal_evidence_sha256="2" * 64,
        ),
        request,
    )
    alternate_evidence = tuple(
        replace(
            item,
            source_artifact_id="wo36b.ground-truth.alternate",
        )
        if item.capability is RevealCapability.GROUND_TRUTH
        else item
        for item in present.source.capability_evidence
    )
    alternate_manifest = replace(
        present.source,
        capability_evidence=alternate_evidence,
    )
    alternate_reveal = RevealEvidenceSet(alternate_manifest, present.values)
    wrong_manifest = query_postmortem(
        observed,
        alternate_reveal,
        _reveal_authorization(
            alternate_reveal,
            (RevealCapability.GROUND_TRUTH,),
            observed=observed,
            source_capability_manifest_sha256=present.source.manifest_sha256,
        ),
        request,
    )
    wrong_scope = query_postmortem(
        observed,
        present,
        _reveal_authorization(
            present,
            (RevealCapability.HIDDEN_STATE,),
            observed=observed,
        ),
        request,
    )
    scrubbed = query_postmortem(
        observed,
        present,
        exact_grant,
        replace(request, render_cursor_time_us=300, action_time_us=300),
    )
    forged_policy_rejected = False
    try:
        RevealAuthorization(
            "authorization-forged-policy",
            present.source.source_run_id,
            present.source.source_event_sha256,
            observed.evidence_sha256,
            present.source.manifest_sha256,
            present.evidence_sha256,
            (RevealCapability.GROUND_TRUTH,),
            policy_id=AS_OBSERVED_POLICY_ID,
        )
    except ValueError:
        forged_policy_rejected = True

    artifact_alias_rejected = False
    available_ground = next(
        item
        for item in present.source.capability_evidence
        if item.capability is RevealCapability.GROUND_TRUTH
    )
    try:
        ReplaySourceCapabilityManifest(
            present.source.source_run_id,
            present.source.source_event_sha256,
            present.source.source_schema_id,
            present.source.source_schema_version,
            (
                available_ground,
                SourceCapabilityEvidence(
                    RevealCapability.HIDDEN_STATE,
                    SourceCapabilityAvailability.AVAILABLE,
                    source_artifact_id=available_ground.source_artifact_id,
                    source_artifact_sha256="f" * 64,
                ),
            ),
        )
    except ValueError:
        artifact_alias_rejected = True

    schema_alias_rejections: dict[str, bool] = {}
    schema_alias_probes = (
        (
            "policy",
            lambda: ObservationPolicy(
                ObservationMode.AS_OBSERVED,
                schema_version=True,
            ),
        ),
        ("manifest", lambda: replace(present.source, schema_version=True)),
        ("authorization", lambda: replace(exact_grant, schema_version=True)),
        ("observed_evidence", lambda: replace(observed, schema_version=True)),
        ("reveal_evidence", lambda: replace(present, schema_version=True)),
        ("data_age", lambda: replace(exact.values[0].data_age, schema_version=True)),
        ("query_result", lambda: replace(exact, schema_version=True)),
    )
    for name, probe in schema_alias_probes:
        try:
            probe()
        except ValueError:
            schema_alias_rejections[name] = True
        else:
            schema_alias_rejections[name] = False

    class ForgedAuthorization(RevealAuthorization):
        pass

    subclass_rejected = False
    try:
        query_postmortem(
            observed,
            present,
            ForgedAuthorization(
                "authorization-subclass-forgery",
                present.source.source_run_id,
                present.source.source_event_sha256,
                observed.evidence_sha256,
                present.source.manifest_sha256,
                "f" * 64,
                (RevealCapability.GROUND_TRUTH,),
            ),
            request,
        )
    except TypeError:
        subclass_rejected = True

    future_action_rejected = False
    try:
        query_postmortem(
            observed,
            present,
            exact_grant,
            replace(request, render_cursor_time_us=300, action_time_us=301),
        )
    except ValueError:
        future_action_rejected = True

    null_reveal_rejected = False
    try:
        _reveal_value(
            "truth.null-value",
            "truth-null-event-0001",
            99,
            RevealCapability.GROUND_TRUTH,
            None,  # type: ignore[arg-type]
        )
    except ValueError:
        null_reveal_rejected = True

    expected_reasons = (
        (absent_without_grant, RevealUnavailableReason.SOURCE_CAPABILITY_UNAVAILABLE),
        (present_without_grant, RevealUnavailableReason.AUTHORIZATION_REQUIRED),
        (absent_with_grant, RevealUnavailableReason.SOURCE_CAPABILITY_UNAVAILABLE),
        (wrong_run, RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH),
        (wrong_source, RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH),
        (wrong_reveal, RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH),
        (wrong_manifest, RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH),
        (wrong_observed, RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH),
        (wrong_scope, RevealUnavailableReason.AUTHORIZATION_SCOPE_MISMATCH),
    )
    failures: list[str] = []
    for result, reason in expected_reasons:
        if (
            result.reveal.availability is not RevealAvailability.UNAVAILABLE
            or result.reveal.unavailable_reason is not reason
        ):
            failures.append(f"postmortem denial differs for {reason.value}")
        denied_bytes = result.canonical_bytes()
        if (
            present.evidence_sha256.encode("ascii") in denied_bytes
            or b"sealed-ground-truth-wo36b" in denied_bytes
            or b"truth.best-bid" in denied_bytes
        ):
            failures.append(f"denied {reason.value} result exposed protected reveal bytes")
    revealed = tuple(
        item
        for item in exact.values
        if item.source_kind is EvidenceSourceKind.REVEALED_GROUND_TRUTH
    )
    if (
        exact.reveal.availability is not RevealAvailability.AVAILABLE
        or len(revealed) != 1
        or exact.reveal_evidence_sha256 != present.evidence_sha256
    ):
        failures.append("exact capability and source-bound authorization did not reveal")
    if not forged_policy_rejected:
        failures.append("a reveal grant bound to the observed policy was accepted")
    if not artifact_alias_rejected:
        failures.append("one capability artifact ID mapped to conflicting digests")
    if not all(schema_alias_rejections.values()):
        failures.append("boolean schema-version alias crossed a V1 wire contract")
    if not subclass_rejected:
        failures.append("a subclassed reveal grant crossed the closed policy boundary")
    if not future_action_rejected:
        failures.append("postmortem accepted an action beyond the render cursor")
    if not null_reveal_rejected:
        failures.append("nullable reveal evidence failed late instead of at ingress")
    if not cross_plane_series_rejected:
        failures.append("authorized observed/reveal series namespace collision survived")
    if scrubbed.reveal.availability is not RevealAvailability.AVAILABLE:
        failures.append("out-of-band authorization was incorrectly tied to replay time")
    if revealed and (
        revealed[0].data_age.client_knowledge.availability
        is TimestampAvailability.RECORDED
        or revealed[0].data_age.known_at_action is not False
        or revealed[0].data_age.age_at_action_us is not None
    ):
        failures.append("reveal authorization was misreported as client knowledge")

    relabel_rejected = False
    request_scope_relabel_rejected = False
    contradictory_result_rejected = False
    duplicate_result_rejected = False
    reveal_receive_rejected = False
    reveal_visibility_rejected = False
    delivered_receive_rejected = False
    if revealed:
        relabeled = replace(
            revealed[0],
            observation_mode=ObservationMode.AS_OBSERVED,
            policy_id=AS_OBSERVED_POLICY_ID,
        )
        try:
            ObservationQueryResult(
                policy=ObservationPolicy(ObservationMode.AS_OBSERVED),
                source_run_id=observed.source_run_id,
                source_event_sha256=observed.source_event_sha256,
                observed_projection_sha256=exact.observed_projection_sha256,
                request=ObservationQueryRequest(500, action_time_us=300),
                values=(relabeled,),
                reveal=RevealDecision(RevealAvailability.NOT_REQUESTED, ()),
            )
        except ValueError:
            relabel_rejected = True
        contradictory = replace(
            exact.values[0],
            data_age=replace(
                exact.values[0].data_age,
                render_cursor_time_us=501,
            ),
        )
        try:
            replace(exact, values=(contradictory, *exact.values[1:]))
        except ValueError:
            contradictory_result_rejected = True
        try:
            replace(exact, values=(*exact.values, exact.values[0]))
        except ValueError:
            duplicate_result_rejected = True
        try:
            replace(
                exact,
                request=replace(
                    exact.request,
                    requested_reveal_capabilities=(RevealCapability.HIDDEN_STATE,),
                ),
            )
        except ValueError:
            request_scope_relabel_rejected = True
        receive_claim = replace(
            revealed[0],
            data_age=replace(
                revealed[0].data_age,
                client_receive=EvidenceTimestamp.recorded(350),
            ),
        )
        try:
            replace(
                exact,
                values=tuple(
                    receive_claim if item is revealed[0] else item
                    for item in exact.values
                ),
            )
        except ValueError:
            reveal_receive_rejected = True
        shifted_visibility = replace(
            revealed[0],
            data_age=replace(
                revealed[0].data_age,
                policy_visible_at_time_us=(
                    revealed[0].data_age.source_event_time_us + 1
                ),
            ),
        )
        try:
            replace(
                exact,
                values=tuple(
                    shifted_visibility if item is revealed[0] else item
                    for item in exact.values
                ),
            )
        except ValueError:
            reveal_visibility_rejected = True
        delivered = next(
            item
            for item in exact.values
            if item.source_kind is EvidenceSourceKind.CLIENT_DELIVERED
        )
        missing_receive = replace(
            delivered,
            data_age=replace(
                delivered.data_age,
                client_receive=EvidenceTimestamp.unavailable(
                    NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE
                ),
            ),
        )
        try:
            replace(
                exact,
                values=tuple(
                    missing_receive if item is delivered else item
                    for item in exact.values
                ),
            )
        except ValueError:
            delivered_receive_rejected = True
    if not relabel_rejected:
        failures.append("revealed truth could be relabeled as observed output")
    if not contradictory_result_rejected:
        failures.append("query result accepted contradictory cursor metadata")
    if not duplicate_result_rejected:
        failures.append("query result accepted a duplicate selected source series")
    if not request_scope_relabel_rejected:
        failures.append("query result accepted a request/reveal scope mismatch")
    if not reveal_receive_rejected:
        failures.append("revealed value retroactively claimed client receipt")
    if not reveal_visibility_rejected:
        failures.append("revealed value accepted a delayed policy visibility time")
    if not delivered_receive_rejected:
        failures.append("client-delivered value accepted absent receipt timing")
    detail = (
        f"matrix_cases={len(expected_reasons) + 1} exact_revealed={len(revealed)} "
        f"closed_types={subclass_rejected} relabel_rejected={relabel_rejected}"
    )
    return ReplayMicroscopeAuditCase(
        "postmortem_requires_capability_and_bound_authorization",
        detail,
        {
            "authorization_matrix": {
                reason.value: result.reveal.availability.value
                for result, reason in expected_reasons
            },
            "exact_query_id": exact.query_id,
            "exact_reveal_count": len(revealed),
            "future_action_rejected": future_action_rejected,
            "cross_plane_series_rejected": cross_plane_series_rejected,
            "artifact_alias_rejected": artifact_alias_rejected,
            "schema_alias_rejections": schema_alias_rejections,
            "forged_policy_rejected": forged_policy_rejected,
            "manifest_binding_rejected": (
                wrong_manifest.reveal.unavailable_reason
                is RevealUnavailableReason.AUTHORIZATION_BINDING_MISMATCH
            ),
            "reveal_evidence_sha256": present.evidence_sha256,
            "null_reveal_rejected": null_reveal_rejected,
            "result_invariants": {
                "contradictory_cursor_rejected": contradictory_result_rejected,
                "duplicate_source_series_rejected": duplicate_result_rejected,
                "reveal_relabel_rejected": relabel_rejected,
                "request_scope_relabel_rejected": request_scope_relabel_rejected,
                "reveal_client_receive_rejected": reveal_receive_rejected,
                "reveal_visibility_rejected": reveal_visibility_rejected,
                "client_delivered_receive_rejected": delivered_receive_rejected,
            },
            "scrubbed_authorization_available": (
                scrubbed.reveal.availability is RevealAvailability.AVAILABLE
            ),
            "subclass_rejected": subclass_rejected,
        },
        tuple(dict.fromkeys(failures)),
    )


def _client_knowledge_cutoff_case() -> ReplayMicroscopeAuditCase:
    run_id, source_sha256 = _wo36b_source_identity()
    timing = EvidenceTiming(
        source_event_time_us=100,
        venue_receipt=EvidenceTimestamp.recorded(120),
        client_receive=EvidenceTimestamp.recorded(200),
        client_knowledge=EvidenceTimestamp.recorded(250),
    )
    evidence = ObservedEvidenceSet(
        run_id,
        source_sha256,
        client_delivered=(
            ObservedValueRecord(
                "quote.processed-best-bid",
                "quote-event-0001",
                1,
                timing,
                {"best_bid_ticks": 100},
            ),
        ),
    )
    before = query_as_observed(evidence, ObservationQueryRequest(249))
    cutoff = query_as_observed(
        evidence,
        ObservationQueryRequest(250, action_time_us=250),
    )
    action = query_as_observed(
        evidence,
        ObservationQueryRequest(300, action_time_us=300),
    )
    bool_time_rejected = False
    try:
        EvidenceTiming(
            source_event_time_us=True,
            venue_receipt=EvidenceTimestamp.not_applicable(
                TimestampAbsenceReason.NO_VENUE_HOP
            ),
            client_receive=EvidenceTimestamp.not_applicable(
                TimestampAbsenceReason.DECISION_SNAPSHOT
            ),
            client_knowledge=EvidenceTimestamp.recorded(1),
        )
    except ValueError:
        bool_time_rejected = True
    freeform_absence_rejected = False
    try:
        EvidenceTimestamp.unavailable("TOP_SECRET_REASON_ABC")  # type: ignore[arg-type]
    except ValueError:
        freeform_absence_rejected = True

    initial_outbound = ObservedValueRecord(
        "order.client-intention",
        "outbound-order-event-0001",
        1,
        EvidenceTiming(
            source_event_time_us=100,
            venue_receipt=EvidenceTimestamp.unavailable(
                NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE
            ),
            client_receive=EvidenceTimestamp.not_applicable(
                TimestampAbsenceReason.OUTBOUND_CLIENT_INTENTION
            ),
            client_knowledge=EvidenceTimestamp.recorded(100),
        ),
        {"side": "BUY", "venue_state": "NOT_OBSERVED"},
    )
    received_outbound = ObservedValueRecord(
        "order.client-intention",
        "outbound-order-event-0002",
        2,
        EvidenceTiming(
            source_event_time_us=100,
            venue_receipt=EvidenceTimestamp.recorded(300),
            client_receive=EvidenceTimestamp.not_applicable(
                TimestampAbsenceReason.OUTBOUND_CLIENT_INTENTION
            ),
            client_knowledge=EvidenceTimestamp.recorded(300),
        ),
        {"side": "BUY", "venue_state": "RECEIVED"},
    )
    eventual_receipt = ObservedEvidenceSet(
        run_id,
        source_sha256,
        decision_snapshots=(initial_outbound, received_outbound),
    )
    never_received = ObservedEvidenceSet(
        run_id,
        source_sha256,
        decision_snapshots=(initial_outbound,),
    )
    outbound_before_receipt = query_as_observed(
        eventual_receipt,
        ObservationQueryRequest(200, action_time_us=100),
    )
    never_received_before = query_as_observed(
        never_received,
        ObservationQueryRequest(200, action_time_us=100),
    )
    outbound_on_receipt = query_as_observed(
        eventual_receipt,
        ObservationQueryRequest(300, action_time_us=100),
    )
    outbound_future_receipt_hidden = (
        len(outbound_before_receipt.values) == 1
        and outbound_before_receipt.values[0].data_age.venue_receipt.availability
        is TimestampAvailability.UNAVAILABLE
        and outbound_before_receipt.values[0].data_age.venue_receipt.reason
        == NOT_OBSERVED_AS_OF_CLIENT_KNOWLEDGE
        and outbound_before_receipt.canonical_bytes()
        == never_received_before.canonical_bytes()
    )
    outbound_receipt_visible = (
        len(outbound_on_receipt.values) == 1
        and outbound_on_receipt.values[0].data_age.venue_receipt.time_us == 300
    )

    failures: list[str] = []
    if before.values:
        failures.append("value rendered before client knowledge time")
    if len(cutoff.values) != 1 or cutoff.values[0].selection is not SelectionKind.EXACT_RECORDED:
        failures.append("value was not rendered at the exact client knowledge cutoff")
    if len(action.values) != 1:
        failures.append("client-known decision disappeared after its knowledge time")
        age = None
    else:
        age = action.values[0].data_age
        if age.as_dict() != {
            "action_time_us": 300,
            "age_at_action_us": 200,
            "client_knowledge": {
                "availability": "RECORDED",
                "reason": None,
                "time_us": 250,
            },
            "client_receive": {
                "availability": "RECORDED",
                "reason": None,
                "time_us": 200,
            },
            "event_age_at_render_us": 200,
            "knowledge_age_at_render_us": 50,
            "known_at_action": True,
            "policy_visibility_age_at_render_us": 50,
            "policy_visible_at_time_us": 250,
            "render_cursor_time_us": 300,
            "schema_id": "KIRBY2_MICROSCOPE_DATA_AGE_V1",
            "schema_version": 1,
            "source_event_time_us": 100,
            "venue_receipt": {
                "availability": "RECORDED",
                "reason": None,
                "time_us": 120,
            },
        }:
            failures.append("query data-age projection differs from exact causal times")
    pre_source_projection_rejected = False
    inverted_client_projection_rejected = False
    if age is not None:
        try:
            replace(age, venue_receipt=EvidenceTimestamp.recorded(99))
        except ValueError:
            pre_source_projection_rejected = True
        try:
            replace(
                age,
                client_receive=EvidenceTimestamp.recorded(260),
                client_knowledge=EvidenceTimestamp.recorded(250),
            )
        except ValueError:
            inverted_client_projection_rejected = True
    if not bool_time_rejected:
        failures.append("boolean timestamp was accepted as an integer time")
    if not freeform_absence_rejected:
        failures.append("free-form timestamp absence reason crossed the wire schema")
    if not pre_source_projection_rejected:
        failures.append("data-age projection accepted a pre-source venue receipt")
    if not inverted_client_projection_rejected:
        failures.append("data-age projection accepted knowledge before client receipt")
    if not outbound_future_receipt_hidden:
        failures.append("outbound intention exposed its future venue receipt")
    if not outbound_receipt_visible:
        failures.append("outbound venue receipt did not appear at its exact cursor")
    detail = (
        f"visible_at_249={bool(before.values)} visible_at_250={bool(cutoff.values)} "
        f"age_at_action_us={None if age is None else age.age_at_action_us}"
    )
    return ReplayMicroscopeAuditCase(
        "client_knowledge_time_controls_observed_visibility",
        detail,
        {
            "age": None if age is None else age.as_dict(),
            "bool_time_rejected": bool_time_rejected,
            "data_age_chronology_rejections": {
                "client_inversion": inverted_client_projection_rejected,
                "pre_source_venue": pre_source_projection_rejected,
            },
            "freeform_absence_rejected": freeform_absence_rejected,
            "cutoff_selection": (
                None if not cutoff.values else cutoff.values[0].selection.value
            ),
            "visible_before_knowledge": bool(before.values),
            "visible_on_knowledge_cutoff": bool(cutoff.values),
            "outbound_future_receipt_hidden": outbound_future_receipt_hidden,
            "outbound_receipt_visible_at_300": outbound_receipt_visible,
        },
        tuple(failures),
    )


def _held_last_known_case() -> ReplayMicroscopeAuditCase:
    evidence = _observed_policy_fixture()
    reversed_evidence = _observed_policy_fixture(reverse=True)
    before = query_as_observed(evidence, ObservationQueryRequest(199, action_time_us=199))
    midpoint = query_as_observed(evidence, ObservationQueryRequest(300, action_time_us=300))
    midpoint_reversed = query_as_observed(
        reversed_evidence,
        ObservationQueryRequest(300, action_time_us=300),
    )
    later = query_as_observed(evidence, ObservationQueryRequest(400, action_time_us=400))
    expected_early = {
        item.series_id: item for item in evidence.client_delivered if item.sequence % 2 == 1
    }
    expected_later = {
        item.series_id: item for item in evidence.client_delivered if item.sequence % 2 == 0
    }
    midpoint_delivered = {
        item.series_id: item
        for item in midpoint.values
        if item.source_kind is EvidenceSourceKind.CLIENT_DELIVERED
    }
    later_delivered = {
        item.series_id: item
        for item in later.values
        if item.source_kind is EvidenceSourceKind.CLIENT_DELIVERED
    }
    run_id, source_sha256 = _wo36b_source_identity()
    tie_and_tombstone = ObservedEvidenceSet(
        run_id,
        source_sha256,
        decision_snapshots=(
            ObservedValueRecord(
                "feature.tie-break",
                "tie-break-event-0001",
                30,
                EvidenceTiming(
                    100,
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.CLIENT_DECISION
                    ),
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.RECORDED_SNAPSHOT
                    ),
                    EvidenceTimestamp.recorded(300),
                ),
                {"value": 1},
            ),
            ObservedValueRecord(
                "feature.tie-break",
                "tie-break-event-0002",
                31,
                EvidenceTiming(
                    101,
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.CLIENT_DECISION
                    ),
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.RECORDED_SNAPSHOT
                    ),
                    EvidenceTimestamp.recorded(300),
                ),
                {"value": 2},
            ),
            ObservedValueRecord(
                "feature.cleared",
                "clearable-event-0001",
                40,
                EvidenceTiming(
                    120,
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.CLIENT_DECISION
                    ),
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.RECORDED_SNAPSHOT
                    ),
                    EvidenceTimestamp.recorded(200),
                ),
                {"value": 99},
            ),
            ObservedValueRecord(
                "feature.cleared",
                "clearable-event-0002",
                41,
                EvidenceTiming(
                    320,
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.CLIENT_DECISION
                    ),
                    EvidenceTimestamp.not_applicable(
                        TimestampAbsenceReason.RECORDED_SNAPSHOT
                    ),
                    EvidenceTimestamp.recorded(400),
                ),
                None,
                RecordDisposition.TOMBSTONE,
            ),
        ),
    )
    tie_result = query_as_observed(
        tie_and_tombstone,
        ObservationQueryRequest(300, action_time_us=300),
    )
    cleared_result = query_as_observed(
        tie_and_tombstone,
        ObservationQueryRequest(500, action_time_us=500),
    )
    tie_value = next(
        item for item in tie_result.values if item.series_id == "feature.tie-break"
    )
    cleared_value = next(
        item for item in cleared_result.values if item.series_id == "feature.cleared"
    )
    failures: list[str] = []
    if before.values:
        failures.append("query invented state before the first known record")
    if set(midpoint_delivered) != set(expected_early):
        failures.append("midpoint query series inventory differs")
    for series_id, expected in expected_early.items():
        actual = midpoint_delivered.get(series_id)
        if (
            actual is None
            or actual.event_id != expected.event_id
            or actual.payload_sha256 != expected.payload_sha256
            or actual.selection is not SelectionKind.HELD_LAST_KNOWN
        ):
            failures.append(f"{series_id} was interpolated or field-merged at midpoint")
    for series_id, expected in expected_later.items():
        actual = later_delivered.get(series_id)
        if (
            actual is None
            or actual.event_id != expected.event_id
            or actual.payload_sha256 != expected.payload_sha256
            or actual.selection is not SelectionKind.EXACT_RECORDED
        ):
            failures.append(f"{series_id} did not select the exact later record")
    if midpoint.canonical_bytes() != midpoint_reversed.canonical_bytes():
        failures.append("query result depends on caller evidence order")
    if tie_value.sequence != 31 or tie_value.event_id != "tie-break-event-0002":
        failures.append("same-knowledge tie did not select recorded sequence order")
    if (
        cleared_value.disposition is not RecordDisposition.TOMBSTONE
        or thaw_json(cleared_value.payload) is not None
    ):
        failures.append("typed tombstone did not supersede the prior whole value")
    detail = (
        f"series={len(expected_early)} midpoint=HELD_LAST_KNOWN "
        f"caller_order_independent={midpoint.query_id == midpoint_reversed.query_id}"
    )
    return ReplayMicroscopeAuditCase(
        "held_last_known_never_interpolates_evidence",
        detail,
        {
            "before_first_value_count": len(before.values),
            "caller_order_independent": (
                midpoint.canonical_bytes() == midpoint_reversed.canonical_bytes()
            ),
            "midpoint_event_ids": {
                key: value.event_id for key, value in sorted(midpoint_delivered.items())
            },
            "series": sorted(expected_early),
            "same_time_selected_sequence": tie_value.sequence,
            "tombstone_disposition": cleared_value.disposition.value,
        },
        tuple(dict.fromkeys(failures)),
    )


def _mode_provenance_case() -> ReplayMicroscopeAuditCase:
    observed = _observed_policy_fixture()
    reveal = _reveal_policy_fixture(
        (RevealCapability.GROUND_TRUTH,),
        include_hidden=False,
    )
    as_observed = query_as_observed(
        observed,
        ObservationQueryRequest(500, action_time_us=300),
    )
    postmortem = query_postmortem(
        observed,
        reveal,
        _reveal_authorization(
            reveal,
            (RevealCapability.GROUND_TRUTH,),
            observed=observed,
        ),
        ObservationQueryRequest(
            500,
            action_time_us=300,
            requested_reveal_capabilities=(RevealCapability.GROUND_TRUTH,),
        ),
    )
    expected = (
        (as_observed, "AS_OBSERVED", AS_OBSERVED_POLICY_ID),
        (postmortem, "POSTMORTEM", POSTMORTEM_POLICY_ID),
    )
    failures: list[str] = []
    metadata_equality: dict[str, bool] = {}
    for result, mode, policy_id in expected:
        export = result.export_metadata()
        screenshot = result.screenshot_metadata()
        portable = result.portable_report_metadata()
        metadata_equality[mode] = export == screenshot == portable
        if not metadata_equality[mode]:
            failures.append(f"{mode} metadata surfaces diverged")
        if (
            export["observation_mode"] != mode
            or export["observation_policy_id"] != policy_id
            or result.export_payload()["policy"]["mode"] != mode
            or result.export_payload()["policy"]["policy_id"] != policy_id
        ):
            failures.append(f"{mode} policy label did not survive export")
        if any(
            item.observation_mode.value != mode or item.policy_id != policy_id
            for item in result.values
        ):
            failures.append(f"{mode} sliced values lost their policy label")
    before = as_observed.canonical_bytes()
    detached = as_observed.export_payload()
    policy_payload = detached.get("policy")
    if isinstance(policy_payload, dict):
        policy_payload["mode"] = "TAMPERED"
    detached["values"] = []
    if as_observed.canonical_bytes() != before:
        failures.append("mutating detached export changed the query result")
    detail = (
        f"as_observed={as_observed.query_id} postmortem={postmortem.query_id} "
        f"metadata_equal={all(metadata_equality.values())}"
    )
    return ReplayMicroscopeAuditCase(
        "mode_provenance_survives_every_metadata_surface",
        detail,
        {
            "as_observed_policy_id": as_observed.policy.policy_id,
            "metadata_surface_equality": metadata_equality,
            "postmortem_policy_id": postmortem.policy.policy_id,
            "query_ids": [as_observed.query_id, postmortem.query_id],
        },
        tuple(failures),
    )


def _historical_hidden_unavailable_case() -> ReplayMicroscopeAuditCase:
    observed = _observed_policy_fixture()
    historical_reveal = _reveal_policy_fixture(
        (RevealCapability.GROUND_TRUTH,),
        include_hidden=False,
    )
    synthetic_hidden_grant = _reveal_authorization(
        historical_reveal,
        (RevealCapability.HIDDEN_STATE,),
        observed=observed,
    )
    result = query_postmortem(
        observed,
        historical_reveal,
        synthetic_hidden_grant,
        ObservationQueryRequest(
            500,
            action_time_us=300,
            requested_reveal_capabilities=(RevealCapability.HIDDEN_STATE,),
        ),
    )
    hidden_record_rejected = False
    try:
        RevealEvidenceSet(
            historical_reveal.source,
            (
                _reveal_value(
                    "hidden.reserve-quantity",
                    "historical-hidden-event-0001",
                    1,
                    RevealCapability.HIDDEN_STATE,
                    {"reserve_quantity": 900, "sentinel": "historical-hidden-secret"},
                ),
            ),
        )
    except ValueError:
        hidden_record_rejected = True
    hidden_capability_evidence = next(
        item
        for item in historical_reveal.source.capability_evidence
        if item.capability is RevealCapability.HIDDEN_STATE
    )
    explicit_source_absence = (
        hidden_capability_evidence.availability
        is SourceCapabilityAvailability.UNAVAILABLE
        and hidden_capability_evidence.unavailable_reason
        is SourceCapabilityUnavailableReason.NOT_RECORDED_BY_SOURCE
        and hidden_capability_evidence.source_artifact_id is None
        and hidden_capability_evidence.source_artifact_sha256 is None
    )
    encoded = result.canonical_bytes()
    failures: list[str] = []
    if (
        result.reveal.availability is not RevealAvailability.UNAVAILABLE
        or result.reveal.unavailable_reason
        is not RevealUnavailableReason.SOURCE_CAPABILITY_UNAVAILABLE
    ):
        failures.append("historical source manufactured hidden-state capability")
    if (
        historical_reveal.evidence_sha256.encode("ascii") in encoded
        or b"hidden.reserve-quantity" in encoded
        or b"historical-hidden-secret" in encoded
    ):
        failures.append("unavailable historical result exposed hidden reveal material")
    if not hidden_record_rejected:
        failures.append("historical reveal store accepted an unevidenced hidden record")
    if not explicit_source_absence:
        failures.append("historical manifest omitted typed hidden-state absence evidence")
    if result.policy.mode is not ObservationMode.POSTMORTEM:
        failures.append("unavailable postmortem query silently changed modes")
    detail = (
        f"capability=ABSENT authorization=PRESENT result="
        f"{result.reveal.unavailable_reason.value} payload=ABSENT"
    )
    return ReplayMicroscopeAuditCase(
        "historical_hidden_state_remains_unavailable",
        detail,
        {
            "hidden_record_rejected": hidden_record_rejected,
            "hidden_source_evidence": hidden_capability_evidence.as_dict(),
            "explicit_source_absence": explicit_source_absence,
            "source_capability_manifest_sha256": (
                historical_reveal.source.manifest_sha256
            ),
            "mode": result.policy.mode.value,
            "reveal_availability": result.reveal.availability.value,
            "unavailable_reason": result.reveal.unavailable_reason.value,
        },
        tuple(failures),
    )


def _observed_policy_fixture(*, reverse: bool = False) -> ObservedEvidenceSet:
    run_id, source_sha256 = _wo36b_source_identity()
    payloads = (
        ("quote.best-bid", {"best_bid_ticks": 100}, {"best_bid_ticks": 110}),
        ("ack.client-order-1", {"acknowledged": False}, {"acknowledged": True}),
        ("order.client-order-1", {"state": "PENDING"}, {"state": "WORKING"}),
        ("fill.client-order-1", {"filled_quantity": 0}, {"filled_quantity": 25}),
        (
            "feature.imbalance",
            {"value_millionths": 100_000},
            {"value_millionths": 300_000},
        ),
    )
    records: list[ObservedValueRecord] = []
    sequence = 1
    for offset, (series_id, early, later) in enumerate(payloads):
        event_base = series_id.replace(".", "-")
        records.append(
            ObservedValueRecord(
                series_id,
                f"{event_base}-early",
                sequence,
                _delivered_timing(100 + offset, 120 + offset, 200),
                early,
            )
        )
        sequence += 1
        records.append(
            ObservedValueRecord(
                series_id,
                f"{event_base}-later",
                sequence,
                _delivered_timing(300 + offset, 320 + offset, 400),
                later,
            )
        )
        sequence += 1
    decisions = (
        ObservedValueRecord(
            "strategy.signal",
            "strategy-decision-0001",
            20,
            EvidenceTiming(
                source_event_time_us=180,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(250),
            ),
            {"recorded_signal": "GREEN"},
        ),
    )
    delivered = tuple(reversed(records)) if reverse else tuple(records)
    decision_values = tuple(reversed(decisions)) if reverse else decisions
    return ObservedEvidenceSet(
        run_id,
        source_sha256,
        client_delivered=delivered,
        decision_snapshots=decision_values,
    )


def _reveal_policy_fixture(
    capabilities: tuple[RevealCapability, ...],
    *,
    include_hidden: bool,
    ground_series_id: str = "truth.best-bid",
) -> RevealEvidenceSet:
    run_id, source_sha256 = _wo36b_source_identity()
    values: list[RevealValueRecord] = []
    if RevealCapability.GROUND_TRUTH in capabilities:
        values.append(
            _reveal_value(
                ground_series_id,
                "truth-event-0001",
                1,
                RevealCapability.GROUND_TRUTH,
                {"best_bid_ticks": 101, "sentinel": "sealed-ground-truth-wo36b"},
            )
        )
    if include_hidden and RevealCapability.HIDDEN_STATE in capabilities:
        values.append(
            _reveal_value(
                "hidden.reserve-quantity",
                "hidden-event-0001",
                2,
                RevealCapability.HIDDEN_STATE,
                {
                    "reserve_quantity": 900,
                    "sentinel": "sealed-hidden-state-wo36b",
                },
            )
        )
    value_tuple = tuple(values)
    capability_evidence: list[SourceCapabilityEvidence] = []
    for capability in RevealCapability:
        records = tuple(
            item for item in value_tuple if item.required_capability is capability
        )
        if capability in capabilities:
            if not records:
                raise ValueError("fixture cannot claim a capability without source records")
            capability_evidence.append(
                SourceCapabilityEvidence(
                    capability,
                    SourceCapabilityAvailability.AVAILABLE,
                    source_artifact_id=(
                        "wo36b."
                        + capability.value.lower().replace("_", "-")
                        + ".artifact.v1"
                    ),
                    source_artifact_sha256=reveal_artifact_sha256(records),
                )
            )
        else:
            capability_evidence.append(
                SourceCapabilityEvidence(
                    capability,
                    SourceCapabilityAvailability.UNAVAILABLE,
                    unavailable_reason=(
                        SourceCapabilityUnavailableReason.NOT_RECORDED_BY_SOURCE
                    ),
                )
            )
    source = ReplaySourceCapabilityManifest(
        run_id,
        source_sha256,
        "KIRBY2_WO36B_REPLAY_SOURCE_V1",
        1,
        tuple(capability_evidence),
    )
    return RevealEvidenceSet(source, value_tuple)


def _reveal_value(
    series_id: str,
    event_id: str,
    sequence: int,
    capability: RevealCapability,
    payload: dict[str, object],
) -> RevealValueRecord:
    return RevealValueRecord(
        series_id,
        event_id,
        sequence,
        EvidenceTiming(
            source_event_time_us=300,
            venue_receipt=EvidenceTimestamp.recorded(300),
            client_receive=EvidenceTimestamp.unavailable(
                TimestampAbsenceReason.NEVER_CLIENT_DELIVERED
            ),
            client_knowledge=EvidenceTimestamp.unavailable(
                TimestampAbsenceReason.NEVER_CLIENT_KNOWN_DURING_RUN
            ),
        ),
        capability,
        payload,
    )


def _reveal_authorization(
    reveal: RevealEvidenceSet,
    capabilities: tuple[RevealCapability, ...],
    *,
    observed: ObservedEvidenceSet,
    source_run_id: str | None = None,
    source_event_sha256: str | None = None,
    observed_evidence_sha256: str | None = None,
    source_capability_manifest_sha256: str | None = None,
    reveal_evidence_sha256: str | None = None,
) -> RevealAuthorization:
    suffix = "-".join(item.value.lower().replace("_", "-") for item in capabilities)
    return RevealAuthorization(
        f"authorization-wo36b-{suffix}",
        reveal.source.source_run_id if source_run_id is None else source_run_id,
        (
            reveal.source.source_event_sha256
            if source_event_sha256 is None
            else source_event_sha256
        ),
        (
            observed.evidence_sha256
            if observed_evidence_sha256 is None
            else observed_evidence_sha256
        ),
        (
            reveal.source.manifest_sha256
            if source_capability_manifest_sha256 is None
            else source_capability_manifest_sha256
        ),
        reveal.evidence_sha256 if reveal_evidence_sha256 is None else reveal_evidence_sha256,
        capabilities,
    )


def _delivered_timing(
    source_event_time_us: int,
    venue_receipt_time_us: int,
    client_receive_time_us: int,
) -> EvidenceTiming:
    return EvidenceTiming(
        source_event_time_us,
        EvidenceTimestamp.recorded(venue_receipt_time_us),
        EvidenceTimestamp.recorded(client_receive_time_us),
        EvidenceTimestamp.recorded(client_receive_time_us),
    )


def _wo36b_source_identity() -> tuple[str, str]:
    label = b"wo36-b-observation-policy-source-v1"
    digest = hashlib.sha256(label).hexdigest()
    return "run-" + digest[:24], digest


def _observed_ingress_fixture(
    observed: ObservedEvidenceSet | None = None,
) -> _ObservedIngressFixture:
    source = _observed_policy_fixture() if observed is None else observed
    client_artifact_id = "wo36b.observed.client-delivered.v1"
    decision_artifact_id = "wo36b.observed.decision-snapshots.v1"
    client_schema = "KIRBY2_OBSERVED_CLIENT_DELIVERED_SOURCE_ARTIFACT_V1"
    decision_schema = "KIRBY2_OBSERVED_DECISION_SNAPSHOT_SOURCE_ARTIFACT_V1"

    def raw_records(
        records: tuple[ObservedValueRecord, ...],
    ) -> list[dict[str, object]]:
        return [
            {
                **record.as_dict(),
                "record_kind": _ingress_record_kind(record.series_id),
            }
            for record in sorted(records, key=lambda item: item.sequence)
        ]

    client_payload = {
        "artifact_schema_id": client_schema,
        "artifact_schema_version": 1,
        "records": raw_records(source.client_delivered),
        "source_event_sha256": source.source_event_sha256,
        "source_run_id": source.source_run_id,
    }
    decision_payload = {
        "artifact_schema_id": decision_schema,
        "artifact_schema_version": 1,
        "records": raw_records(source.decision_snapshots),
        "source_event_sha256": source.source_event_sha256,
        "source_run_id": source.source_run_id,
    }
    client_bytes = _audit_canonical_json_bytes(client_payload)
    decision_bytes = _audit_canonical_json_bytes(decision_payload)
    manifest_payload = {
        "adapter_id": OBSERVED_INGEST_ADAPTER_ID,
        "adapter_version": OBSERVED_INGEST_ADAPTER_VERSION,
        "artifacts": [
            {
                "artifact_id": client_artifact_id,
                "artifact_kind": "CLIENT_DELIVERED",
                "artifact_schema_id": client_schema,
                "artifact_schema_version": 1,
                "byte_length": len(client_bytes),
                "normalized_plane_sha256": (
                    source.client_delivered_artifact_sha256
                ),
                "record_count": len(source.client_delivered),
                "sha256": hashlib.sha256(client_bytes).hexdigest(),
            },
            {
                "artifact_id": decision_artifact_id,
                "artifact_kind": "DECISION_SNAPSHOT",
                "artifact_schema_id": decision_schema,
                "artifact_schema_version": 1,
                "byte_length": len(decision_bytes),
                "normalized_plane_sha256": (
                    source.decision_snapshot_artifact_sha256
                ),
                "record_count": len(source.decision_snapshots),
                "sha256": hashlib.sha256(decision_bytes).hexdigest(),
            },
        ],
        "schema_id": OBSERVED_INGEST_MANIFEST_SCHEMA_ID,
        "schema_version": OBSERVED_INGEST_MANIFEST_SCHEMA_VERSION,
        "source_event_sha256": source.source_event_sha256,
        "source_run_id": source.source_run_id,
        "source_scope": "OBSERVED_ONLY",
    }
    manifest_bytes = _audit_canonical_json_bytes(manifest_payload)
    return _ObservedIngressFixture(
        source,
        manifest_bytes,
        hashlib.sha256(manifest_bytes).hexdigest(),
        (
            ObservedArtifactBytes(client_artifact_id, client_bytes),
            ObservedArtifactBytes(decision_artifact_id, decision_bytes),
        ),
        client_artifact_id,
        decision_artifact_id,
    )


def _ingress_record_kind(series_id: str) -> str:
    exact = {
        "feature.imbalance": "IMBALANCE_FEATURE",
        "order.client-intention": "CLIENT_ORDER_INTENTION",
        "quote.best-bid": "BEST_BID_QUOTE",
        "quote.processed-best-bid": "BEST_BID_QUOTE",
        "strategy.signal": "STRATEGY_SIGNAL",
    }
    if series_id in exact:
        return exact[series_id]
    prefixes = (
        ("ack.", "ORDER_ACKNOWLEDGEMENT"),
        ("fill.", "PLAYER_FILL"),
        ("order.", "PLAYER_ORDER_STATE"),
    )
    for prefix, record_kind in prefixes:
        if series_id.startswith(prefix):
            return record_kind
    raise ValueError(f"fixture series lacks an ingestion record kind: {series_id}")


def _rewrite_client_source_artifact(
    fixture: _ObservedIngressFixture,
    mutation: object,
) -> _ObservedIngressFixture:
    client, decisions = fixture.artifacts
    payload = json.loads(client.raw_bytes.decode("ascii"))
    if type(payload) is not dict or not callable(mutation):  # pragma: no cover
        raise RuntimeError("invalid ingestion rewrite fixture")
    mutation(payload)
    client_bytes = _audit_canonical_json_bytes(payload)
    manifest = json.loads(fixture.manifest_bytes.decode("ascii"))
    if type(manifest) is not dict or type(manifest.get("artifacts")) is not list:
        raise RuntimeError("invalid ingestion manifest fixture")
    row = manifest["artifacts"][0]
    if type(row) is not dict:
        raise RuntimeError("invalid client artifact manifest fixture")
    row["byte_length"] = len(client_bytes)
    row["record_count"] = len(payload.get("records", ()))
    row["normalized_plane_sha256"] = _normalized_source_plane_sha256(
        payload,
        CLIENT_DELIVERED_ARTIFACT_SCHEMA_ID,
    )
    row["sha256"] = hashlib.sha256(client_bytes).hexdigest()
    manifest_bytes = _audit_canonical_json_bytes(manifest)
    return _ObservedIngressFixture(
        fixture.observed,
        manifest_bytes,
        hashlib.sha256(manifest_bytes).hexdigest(),
        (
            ObservedArtifactBytes(client.artifact_id, client_bytes),
            decisions,
        ),
        fixture.client_artifact_id,
        fixture.decision_artifact_id,
    )


def _rewrite_decision_source_artifact(
    fixture: _ObservedIngressFixture,
    mutation: object,
) -> _ObservedIngressFixture:
    client, decisions = fixture.artifacts
    payload = json.loads(decisions.raw_bytes.decode("ascii"))
    if type(payload) is not dict or not callable(mutation):  # pragma: no cover
        raise RuntimeError("invalid decision ingestion rewrite fixture")
    mutation(payload)
    decision_bytes = _audit_canonical_json_bytes(payload)
    manifest = json.loads(fixture.manifest_bytes.decode("ascii"))
    if type(manifest) is not dict or type(manifest.get("artifacts")) is not list:
        raise RuntimeError("invalid ingestion manifest fixture")
    row = manifest["artifacts"][1]
    if type(row) is not dict:
        raise RuntimeError("invalid decision artifact manifest fixture")
    row["byte_length"] = len(decision_bytes)
    row["record_count"] = len(payload.get("records", ()))
    row["normalized_plane_sha256"] = _normalized_source_plane_sha256(
        payload,
        DECISION_SNAPSHOT_ARTIFACT_SCHEMA_ID,
    )
    row["sha256"] = hashlib.sha256(decision_bytes).hexdigest()
    manifest_bytes = _audit_canonical_json_bytes(manifest)
    return _ObservedIngressFixture(
        fixture.observed,
        manifest_bytes,
        hashlib.sha256(manifest_bytes).hexdigest(),
        (
            client,
            ObservedArtifactBytes(decisions.artifact_id, decision_bytes),
        ),
        fixture.client_artifact_id,
        fixture.decision_artifact_id,
    )


def _rewrite_ingest_manifest(
    fixture: _ObservedIngressFixture,
    mutation: object,
) -> _ObservedIngressFixture:
    payload = json.loads(fixture.manifest_bytes.decode("ascii"))
    if type(payload) is not dict or not callable(mutation):  # pragma: no cover
        raise RuntimeError("invalid ingestion manifest rewrite")
    mutation(payload)
    manifest_bytes = _audit_canonical_json_bytes(payload)
    return _ObservedIngressFixture(
        fixture.observed,
        manifest_bytes,
        hashlib.sha256(manifest_bytes).hexdigest(),
        fixture.artifacts,
        fixture.client_artifact_id,
        fixture.decision_artifact_id,
    )


def _normalized_source_plane_sha256(
    payload: dict[str, object],
    artifact_schema_id: str,
) -> str:
    records = payload.get("records")
    if type(records) is not list or any(type(item) is not dict for item in records):
        raise RuntimeError("source artifact fixture lacks object records")
    normalized_records: list[dict[str, object]] = []
    for item in records:
        normalized = dict(item)
        normalized.pop("record_kind", None)
        normalized_records.append(normalized)
    normalized_records.sort(key=_normalized_record_sort_key)
    return _audit_sha256(
        {
            "artifact_schema_id": artifact_schema_id,
            "artifact_schema_version": 1,
            "records": normalized_records,
            "source_event_sha256": payload.get("source_event_sha256"),
            "source_run_id": payload.get("source_run_id"),
        }
    )


def _normalized_record_sort_key(
    record: dict[str, object],
) -> tuple[str, int, int, int, str]:
    timing = record.get("timing")
    if type(timing) is not dict:
        raise RuntimeError("source record fixture lacks timing")
    client_knowledge = timing.get("client_knowledge")
    if type(client_knowledge) is not dict:
        raise RuntimeError("source record fixture lacks client knowledge")
    knowledge_time = client_knowledge.get("time_us")
    if knowledge_time is not None and type(knowledge_time) is not int:
        raise RuntimeError("source record fixture has invalid client knowledge time")
    series_id = record.get("series_id")
    source_event_time_us = timing.get("source_event_time_us")
    sequence = record.get("sequence")
    event_id = record.get("event_id")
    if (
        type(series_id) is not str
        or type(source_event_time_us) is not int
        or type(sequence) not in {int, bool}
        or type(event_id) is not str
    ):
        raise RuntimeError("source record fixture lacks a canonical sort key")
    return (
        series_id,
        source_event_time_us,
        -1 if knowledge_time is None else knowledge_time,
        sequence,
        event_id,
    )


def _rewrite_first_best_bid(payload: dict[str, object]) -> None:
    record = _first_source_record(payload)
    value = _source_payload(record)
    value["best_bid_ticks"] = 9_999
    record["payload_sha256"] = _audit_sha256(value)


def _first_source_record(payload: dict[str, object]) -> dict[str, object]:
    records = payload.get("records")
    if type(records) is not list or not records or type(records[0]) is not dict:
        raise RuntimeError("source artifact fixture lacks its first record")
    return records[0]


def _source_record_for_series(
    payload: dict[str, object],
    series_id: str,
) -> dict[str, object]:
    records = payload.get("records")
    if type(records) is not list:
        raise RuntimeError("source artifact fixture lacks records")
    for record in records:
        if type(record) is dict and record.get("series_id") == series_id:
            return record
    raise RuntimeError(f"source artifact fixture lacks series {series_id}")


def _source_payload(record: dict[str, object]) -> dict[str, object]:
    payload = record.get("payload")
    if type(payload) is not dict:
        raise RuntimeError("source record fixture payload is invalid")
    return payload


def _source_timing(record: dict[str, object]) -> dict[str, object]:
    timing = record.get("timing")
    if type(timing) is not dict:
        raise RuntimeError("source record fixture timing is invalid")
    return timing


def _load_ingress_fixture(
    fixture: _ObservedIngressFixture,
) -> VerifiedObservationSource:
    return load_verified_observation_source(
        fixture.manifest_bytes,
        fixture.manifest_sha256,
        fixture.artifacts,
    )


def _ingestion_rejected(operation: object) -> bool:
    if not callable(operation):  # pragma: no cover
        raise RuntimeError("ingestion rejection probe must be callable")
    try:
        operation()
    except (TypeError, ValueError):
        return True
    return False


def _audit_canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _audit_sha256(payload: object) -> str:
    return hashlib.sha256(_audit_canonical_json_bytes(payload)).hexdigest()


def _ui_raw_evidence_imports() -> list[str]:
    ui_root = Path(__file__).resolve().parents[1] / "ui"
    findings: list[str] = []
    for path in sorted(ui_root.rglob("*.py")):
        findings.extend(
            _raw_evidence_imports_in_source(
                path.read_text(encoding="utf-8"),
                str(path.relative_to(ui_root)),
            )
        )
    return findings


def _raw_evidence_imports_in_source(source: str, filename: str) -> list[str]:
    sensitive_modules = {
        "kirby2.microscope.ingestion",
        "kirby2.microscope.policy",
        "kirby2.microscope.query",
    }
    relative_sensitive_modules = {
        "microscope.ingestion",
        "microscope.policy",
        "microscope.query",
    }
    ui_safe_imports = {
        "kirby2.microscope.ingestion": frozenset(),
        "kirby2.microscope.policy": frozenset(
            {
                "ObservationMode",
                "RevealAvailability",
                "RevealUnavailableReason",
            }
        ),
        "kirby2.microscope.query": frozenset(
            {
                "EvidenceSourceKind",
                "ObservationQueryRequest",
                "ObservationQueryResult",
                "RecordDisposition",
                "SelectionKind",
            }
        ),
    }
    findings: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "kirby2" or alias.name.startswith("kirby2."):
                    findings.append(
                        f"{filename}:{node.lineno}:import {alias.name}"
                    )
        elif not isinstance(node, ast.ImportFrom):
            continue
        else:
            module = node.module or ""
            imported = {alias.name for alias in node.names}
            canonical_sensitive_module: str | None = None
            if module in sensitive_modules:
                canonical_sensitive_module = module
            elif node.level > 0 and module in relative_sensitive_modules:
                canonical_sensitive_module = "kirby2." + module
            direct_sensitive = canonical_sensitive_module is not None
            sensitive_parent = module == "kirby2.microscope" or (
                node.level > 0 and module == "microscope"
            )
            root_alias = (
                (module == "kirby2" or (node.level > 0 and module == ""))
                and "microscope" in imported
            )
            blocked: list[str] = []
            if direct_sensitive:
                if canonical_sensitive_module is None:  # pragma: no cover
                    raise RuntimeError("sensitive UI import lost its module")
                blocked.extend(
                    sorted(imported - ui_safe_imports[canonical_sensitive_module])
                )
            if sensitive_parent:
                blocked.extend(sorted(imported))
            if root_alias:
                blocked.append("microscope")
            if blocked:
                findings.append(
                    f"{filename}:{node.lineno}:"
                    f"{'.' * node.level}{module}:"
                    + ",".join(sorted(set(blocked)))
                )
    return findings
