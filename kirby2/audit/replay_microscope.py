"""Runtime acceptance audit for the mechanistic replay trace index."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import shutil
import tempfile
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
from kirby2.microscope.commands import (
    MICROSCOPE_COMMAND_MODULE,
    STALE_PARTIAL_CANCEL_RACE_FIXTURE,
    build_microscope_demo_artifact,
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
from kirby2.microscope.overlays import (
    OVERLAY_KIND_ORDER,
    OVERLAY_SET_SCHEMA_ID,
    OVERLAY_SPECIFICATIONS,
    OverlayAvailability,
    OverlayInputSelection,
    OverlayUnavailableReason,
    build_overlay_set,
    build_overlay_window_projection,
)
from kirby2.microscope.panes import (
    HISTORICAL_PANE_SOURCE_SCHEMA_ID,
    PANE_CAPABILITY_MANIFEST_SCOPE,
    PANE_CAPABILITY_SCHEMA_ID,
    PANE_CAPABILITY_SCHEMA_VERSION,
    PANE_ORDER,
    PANE_SNAPSHOT_SCHEMA_ID,
    PANE_SOURCE_SCHEMA_VERSION,
    SYNTHETIC_PANE_SOURCE_SCHEMA_ID,
    PaneAvailability,
    PaneCapabilityAuthority,
    PaneKind,
    PaneUnavailableReason,
    QueueCapability,
    QueueTruthAvailability,
    bind_pane_capabilities,
    build_synchronized_panes,
    load_verified_pane_capabilities,
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
from kirby2.microscope.report import (
    OVERLAY_FORMATTERS,
    REPORT_ASSET_SHA256,
    REPORT_SECTION_ORDER,
    PortableReportBundle,
    PortableReplayReportV1,
    ReplayPresentationFrameV1,
    ReportSectionAvailability,
    ReportSectionKind,
    build_portable_replay_report,
    verify_portable_report_bundle,
    write_portable_report_bundle,
)
from kirby2.microscope.timeline import (
    TIMELINE_RECEIPT_SCHEMA_ID,
    TIMELINE_SCHEMA_ID,
    TimelineDirection,
    TimelineEventKind,
    TimelineEvidenceEvent,
    TimelineEvidenceSource,
    TimelineJumpTarget,
    TimelineNavigationAvailability,
    TimelineNavigationUnavailableReason,
    TimelinePlaybackState,
    TimelineSidecarRefusalReason,
    build_replay_timeline,
    derive_timeline_event,
    timeline_event_from_query_result,
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
WO36C_AUDIT_CASE_COUNT = 6
WO36D_AUDIT_CASE_COUNT = 6


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


def audit_synchronized_replay_read_models() -> tuple[ReplayMicroscopeAuditCase, ...]:
    """Run the fixed WO36-C synchronized read-model attack inventory."""

    cases = (
        _timeline_cursor_partition_case(),
        _synchronized_pane_inventory_case(),
        _unsupported_pane_explanation_case(),
        _synthetic_queue_truth_case(),
        _overlay_contract_case(),
        _read_model_cross_binding_case(),
    )
    if len(cases) != WO36C_AUDIT_CASE_COUNT:
        raise RuntimeError("WO36-C audit case inventory changed")
    expected_names = (
        "timeline_controls_partition_simultaneous_events_deterministically",
        "all_eighteen_panes_share_one_cursor_policy_and_provenance",
        "unsupported_depth_and_queue_explain_typed_unavailability",
        "queue_truth_requires_pinned_synthetic_postmortem_authority",
        "nine_overlays_preserve_versions_windows_units_and_sources",
        "read_models_reject_cross_query_future_and_ui_backend_inputs",
    )
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO36-C audit case order or identity changed")
    return cases


def audit_portable_replay_reports() -> tuple[ReplayMicroscopeAuditCase, ...]:
    """Run the fixed WO36-D presentation and portable-report attack inventory."""

    cases = (
        _portable_atomic_frame_case(),
        _portable_observation_boundary_case(),
        _portable_presentation_contract_case(),
        _portable_offline_relocation_case(),
        _portable_tamper_refusal_case(),
        _portable_factory_boundary_case(),
    )
    if len(cases) != WO36D_AUDIT_CASE_COUNT:
        raise RuntimeError("WO36-D audit case inventory changed")
    expected_names = (
        "named_demo_builds_three_atomic_frames_deterministically",
        "observation_watermarks_and_reveal_payloads_remain_separate",
        "presentation_preserves_panes_overlays_provenance_and_deferred_sections",
        "portable_bundle_is_relocatable_offline_and_digest_bound",
        "portable_verifier_rejects_repinned_manifest_and_asset_tampering",
        "presentation_and_report_factories_reject_cross_root_and_backend_material",
    )
    if tuple(item.name for item in cases) != expected_names:
        raise RuntimeError("WO36-D audit case order or identity changed")
    return cases


def _portable_atomic_frame_case() -> ReplayMicroscopeAuditCase:
    first = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )
    repeated = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )
    expected_times_us = (59_750_000, 59_830_000, 60_000_000)
    member_names = (
        "assets/report.css",
        "assets/report.js",
        "index.html",
        "manifest.json",
    )
    deterministic = (
        first.report.canonical_bytes() == repeated.report.canonical_bytes()
        and first.report.report_id == repeated.report.report_id
        and first.bundle.bundle_id == repeated.bundle.bundle_id
        and all(
            first.bundle.member_bytes(name) == repeated.bundle.member_bytes(name)
            for name in member_names
        )
    )
    command_inventory = tuple(
        (item.command_id, item.name)
        for item in MICROSCOPE_COMMAND_MODULE.commands
    )
    failures: list[str] = []
    if first.fixture_name != STALE_PARTIAL_CANCEL_RACE_FIXTURE:
        failures.append("named demo returned a different fixture identity")
    if first.mode is not ObservationMode.AS_OBSERVED:
        failures.append("named demo returned a different observation mode")
    if command_inventory != (("MICROSCOPE_DEMO", "microscope-demo"),):
        failures.append("microscope command inventory changed")
    if len(first.report.frames) != 3:
        failures.append("named fixture did not produce exactly three frames")
    if tuple(
        thaw_json(item.identity)["render_cursor_time_us"]
        for item in first.report.frames
    ) != expected_times_us:
        failures.append("named fixture frame cursor inventory changed")
    if not deterministic:
        failures.append("repeated named-fixture construction changed canonical output")

    for frame in first.report.frames:
        payload = frame.as_dict()
        identity = payload["identity"]
        timeline = payload["timeline_root"]
        cursor = payload["cursor"]
        pane_snapshot = payload["pane_snapshot"]
        overlay_set = payload["overlay_set"]
        presentation = payload["presentation"]
        if not all(
            isinstance(item, dict)
            for item in (
                identity,
                timeline,
                cursor,
                pane_snapshot,
                overlay_set,
                presentation,
            )
        ):
            failures.append(f"{frame.frame_id} contains a non-object root")
            continue
        root_keys = (
            "source_run_id",
            "source_event_sha256",
            "observation_mode",
            "policy_id",
            "render_cursor_time_us",
        )
        authority = tuple(identity[key] for key in root_keys)
        if any(
            tuple(root[key] for key in root_keys) != authority
            for root in (cursor, pane_snapshot, overlay_set)
        ):
            failures.append(f"{frame.frame_id} cross-root authority differs")
        if tuple(timeline[key] for key in root_keys[:-1]) != authority[:-1]:
            failures.append(f"{frame.frame_id} timeline authority differs")
        if (
            identity["timeline_id"] != timeline["timeline_id"]
            or identity["timeline_id"] != cursor["timeline_id"]
            or identity["cursor_id"] != cursor["cursor_id"]
            or identity["query_id"] != pane_snapshot["query_id"]
            or identity["query_id"] != overlay_set["query_id"]
            or identity["snapshot_id"] != pane_snapshot["snapshot_id"]
            or identity["overlay_set_id"] != overlay_set["overlay_set_id"]
        ):
            failures.append(f"{frame.frame_id} cross-root content ID differs")
        pane_kinds = tuple(item["pane_kind"] for item in pane_snapshot["panes"])
        overlay_kinds = tuple(item["kind"] for item in overlay_set["overlays"])
        if pane_kinds != tuple(item.value for item in PANE_ORDER):
            failures.append(f"{frame.frame_id} pane inventory changed")
        if overlay_kinds != tuple(item.value for item in OVERLAY_KIND_ORDER):
            failures.append(f"{frame.frame_id} overlay inventory changed")
        if tuple(item["pane_kind"] for item in presentation["panes"]) != pane_kinds:
            failures.append(f"{frame.frame_id} presentation pane order differs")
        if tuple(item["kind"] for item in presentation["overlays"]) != overlay_kinds:
            failures.append(f"{frame.frame_id} presentation overlay order differs")
        if tuple(item["event_id"] for item in presentation["events"]) != tuple(
            item["event_id"] for item in cursor["current_events"]
        ):
            failures.append(f"{frame.frame_id} cursor event presentation differs")
        identity_payload = dict(payload)
        declared_frame_id = identity_payload.pop("frame_id")
        expected_frame_id = (
            "replay-presentation-frame-"
            + _audit_sha256(identity_payload)[:24]
        )
        if declared_frame_id != expected_frame_id:
            failures.append(f"{frame.frame_id} is not content-derived")

    return ReplayMicroscopeAuditCase(
        "named_demo_builds_three_atomic_frames_deterministically",
        (
            f"frames={len(first.report.frames)} "
            f"report={first.report.report_id} deterministic={deterministic}"
        ),
        {
            "bundle_id": first.bundle.bundle_id,
            "command_inventory": [list(item) for item in command_inventory],
            "deterministic": deterministic,
            "frame_ids": [item.frame_id for item in first.report.frames],
            "frame_times_us": list(expected_times_us),
            "report_id": first.report.report_id,
        },
        tuple(failures),
    )


def _portable_observation_boundary_case() -> ReplayMicroscopeAuditCase:
    observed = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )
    postmortem = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.POSTMORTEM,
    )
    expected_capabilities = [item.value for item in RevealCapability]
    observed_frames_valid = True
    postmortem_frames_valid = True
    hidden_reveal_referenced = True
    for frame in observed.report.frames:
        identity = thaw_json(frame.identity)
        presentation = frame.presentation.as_dict()
        agent = next(
            item
            for item in presentation["panes"]
            if item["pane_kind"] == PaneKind.AGENT_ACTIVITY.value
        )
        observed_frames_valid = observed_frames_valid and (
            identity["observation_mode"] == ObservationMode.AS_OBSERVED.value
            and identity["requested_reveal_capabilities"] == []
            and identity["reveal_availability"]
            == RevealAvailability.NOT_REQUESTED.value
            and identity["reveal_evidence_sha256"] is None
            and presentation["watermark"]["semantic_role"] == "AS_OBSERVED"
            and agent["availability"] == PaneAvailability.UNAVAILABLE.value
            and agent["source_references"] == []
        )
    for frame in postmortem.report.frames:
        identity = thaw_json(frame.identity)
        presentation = frame.presentation.as_dict()
        agent = next(
            item
            for item in presentation["panes"]
            if item["pane_kind"] == PaneKind.AGENT_ACTIVITY.value
        )
        postmortem_frames_valid = postmortem_frames_valid and (
            identity["observation_mode"] == ObservationMode.POSTMORTEM.value
            and identity["requested_reveal_capabilities"]
            == expected_capabilities
            and identity["reveal_availability"] == RevealAvailability.AVAILABLE.value
            and isinstance(identity["reveal_evidence_sha256"], str)
            and presentation["watermark"]["semantic_role"] == "POSTMORTEM"
            and agent["availability"] == PaneAvailability.AVAILABLE.value
        )
        hidden_reveal_referenced = hidden_reveal_referenced and any(
            item["source_kind"] == EvidenceSourceKind.REVEALED_HIDDEN_STATE.value
            for item in agent["source_references"]
        )
    observed_payload = observed.report.canonical_bytes()
    leaked_reveal_tokens = tuple(
        token
        for token in (
            EvidenceSourceKind.REVEALED_GROUND_TRUTH.value,
            EvidenceSourceKind.REVEALED_HIDDEN_STATE.value,
            "AUTHORIZED_GROUND_TRUTH",
            "AUTHORIZED_HIDDEN_STATE",
            "authorization_id",
            "reveal_authorization",
        )
        if token.encode("ascii") in observed_payload
    )
    source_roots_match = (
        thaw_json(observed.report.frames[0].identity)["source_run_id"]
        == thaw_json(postmortem.report.frames[0].identity)["source_run_id"]
        and thaw_json(observed.report.frames[0].identity)["source_event_sha256"]
        == thaw_json(postmortem.report.frames[0].identity)["source_event_sha256"]
    )
    failures: list[str] = []
    if not observed_frames_valid:
        failures.append("AS_OBSERVED frames lost their non-reveal policy boundary")
    if not postmortem_frames_valid:
        failures.append("POSTMORTEM frames lost their authorized reveal boundary")
    if not hidden_reveal_referenced:
        failures.append("POSTMORTEM agent presentation lacks hidden-state provenance")
    if leaked_reveal_tokens:
        failures.append("AS_OBSERVED report contains reveal-only material")
    if not source_roots_match:
        failures.append("mode comparison did not use one shared recording root")
    if observed.report.report_id == postmortem.report.report_id:
        failures.append(
            "observation mode and reveal scope did not affect report identity"
        )
    return ReplayMicroscopeAuditCase(
        "observation_watermarks_and_reveal_payloads_remain_separate",
        (
            f"observed={observed.report.report_id} "
            f"postmortem={postmortem.report.report_id} "
            f"reveal_leaks={len(leaked_reveal_tokens)}"
        ),
        {
            "hidden_reveal_referenced": hidden_reveal_referenced,
            "observed_frames_valid": observed_frames_valid,
            "observed_reveal_tokens": list(leaked_reveal_tokens),
            "postmortem_frames_valid": postmortem_frames_valid,
            "report_ids_distinct": (
                observed.report.report_id != postmortem.report.report_id
            ),
            "source_roots_match": source_roots_match,
        },
        tuple(failures),
    )


def _portable_presentation_contract_case() -> ReplayMicroscopeAuditCase:
    artifact = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )
    expected_formatters = (
        ("SPREAD", "SPREAD_TICKS_V1", 1),
        ("MICROPRICE", "MICROPRICE_TICKS_V1", 1_000_000),
        ("IMBALANCE", "IMBALANCE_PERCENT_V1", 10_000),
        ("TRADE_VELOCITY", "TRADE_VELOCITY_V1", 1_000_000),
        ("CANCELLATION_VELOCITY", "CANCEL_VELOCITY_V1", 1_000_000),
        ("REPLENISHMENT", "REPLENISHMENT_V1", 1_000_000),
        ("RELATIVE_VOLUME", "RELATIVE_VOLUME_V1", 1_000_000),
        ("SHORT_TERM_VOLATILITY", "VOLATILITY_BPS_V1", 1_000_000),
        (
            "IMPLEMENTATION_SHORTFALL",
            "SHORTFALL_TICK_SHARES_V1",
            2,
        ),
    )
    formatter_inventory = tuple(
        (item.kind.value, item.formatter_id, item.display_divisor)
        for item in OVERLAY_FORMATTERS
    )
    presentation_inventory_valid = all(
        tuple(item["pane_kind"] for item in frame.presentation.as_dict()["panes"])
        == tuple(item.value for item in PANE_ORDER)
        and tuple(item["kind"] for item in frame.presentation.as_dict()["overlays"])
        == tuple(item.value for item in OVERLAY_KIND_ORDER)
        for frame in artifact.report.frames
    )
    middle_presentation = artifact.report.frames[1].presentation.as_dict()
    consolidated = next(
        item
        for item in middle_presentation["panes"]
        if item["pane_kind"] == PaneKind.CONSOLIDATED_QUOTES.value
    )
    crossed_without_verdict = (
        consolidated["market_classification"] == "CROSSED_COMPOSITE"
        and consolidated["integrity_assessment"] == "NOT_ASSESSED"
    )
    first_overlays = {
        item["kind"]: item
        for item in artifact.report.frames[0].presentation.as_dict()["overlays"]
    }
    final_overlays = {
        item["kind"]: item
        for item in artifact.report.frames[-1].presentation.as_dict()["overlays"]
    }
    expected_final_values = {
        "SPREAD": ("2", "2 ticks"),
        "MICROPRICE": ("102500000", "102.5 ticks"),
        "IMBALANCE": ("500000", "+50.00%"),
        "RELATIVE_VOLUME": ("600000", "0.6×"),
        "IMPLEMENTATION_SHORTFALL": ("40", "+20.0 tick-shares"),
    }
    display_values_valid = all(
        (
            final_overlays[kind]["raw_value_decimal"],
            final_overlays[kind]["display_value"],
        )
        == expected
        for kind, expected in expected_final_values.items()
    )
    early_relative_volume_unavailable = (
        first_overlays["RELATIVE_VOLUME"]["availability"]
        == OverlayAvailability.UNAVAILABLE.value
        and first_overlays["RELATIVE_VOLUME"]["display_value"] is None
        and first_overlays["RELATIVE_VOLUME"]["raw_value_decimal"] is None
    )
    source_references: list[dict[str, object]] = []
    for frame in artifact.report.frames:
        presentation = frame.presentation.as_dict()
        for item in (*presentation["panes"], *presentation["overlays"]):
            source_references.extend(item["source_references"])
    provenance_observations_valid = bool(source_references)
    for reference in source_references:
        observations = reference.get("source_observations")
        if not isinstance(observations, list) or not observations:
            provenance_observations_valid = False
            continue
        for observation in observations:
            data_age = observation.get("data_age")
            if (
                not isinstance(data_age, dict)
                or type(observation.get("query_id")) is not str
                or type(observation.get("query_render_cursor_time_us")) is not int
                or observation.get("selection_kind")
                not in {item.value for item in SelectionKind}
                or type(observation.get("is_current")) is not bool
                or observation["is_current"]
                != (observation["selection_kind"] == SelectionKind.EXACT_RECORDED.value)
                or data_age.get("render_cursor_time_us")
                != observation["query_render_cursor_time_us"]
            ):
                provenance_observations_valid = False
    authority_valid = all(
        frame.presentation.as_dict().get("metadata_authority")
        == {
            "authority": "SOURCE_BOUND_DISPLAY_DECLARATION",
            "evidence_classification": "PRESENTATION_ONLY_NOT_MARKET_EVIDENCE",
            "source_event_sha256": thaw_json(frame.identity)[
                "source_event_sha256"
            ],
            "source_run_id": thaw_json(frame.identity)["source_run_id"],
        }
        for frame in artifact.report.frames
    )
    section_inventory_valid = (
        tuple(item.kind for item in artifact.report.sections)
        == REPORT_SECTION_ORDER
    )
    deferred_kinds = (
        ReportSectionKind.BOOKMARKS,
        ReportSectionKind.ANNOTATIONS,
        ReportSectionKind.BRANCH_COMPARISON,
    )
    sections = {item.kind: item for item in artifact.report.sections}
    deferred_sections_valid = all(
        sections[kind].availability
        is ReportSectionAvailability.NOT_AVAILABLE_UNTIL_WO36_E
        and thaw_json(sections[kind].payload)
        == {"reason": "NOT_AVAILABLE_UNTIL_WO36_E", "records": []}
        for kind in deferred_kinds
    )
    partial = build_portable_replay_report(artifact.report.frames[:2])
    trace_availability_valid = (
        sections[ReportSectionKind.CAUSAL_TRACES].availability
        is ReportSectionAvailability.AVAILABLE
        and next(
            item
            for item in partial.sections
            if item.kind is ReportSectionKind.CAUSAL_TRACES
        ).availability
        is ReportSectionAvailability.UNAVAILABLE
    )
    first_timestamp = build_portable_replay_report(
        artifact.report.frames,
        display_generated_at="2026-08-29T00:00:00Z",
    )
    second_timestamp = build_portable_replay_report(
        artifact.report.frames,
        display_generated_at="2026-08-29T00:00:01Z",
    )
    reordered = build_portable_replay_report(
        tuple(reversed(artifact.report.frames)),
        display_generated_at="2026-08-29T00:00:00Z",
    )
    report_identity_valid = (
        first_timestamp.report_id == second_timestamp.report_id
        and first_timestamp.report_id == reordered.report_id
        and first_timestamp.canonical_bytes() != second_timestamp.canonical_bytes()
        and first_timestamp.canonical_bytes() == reordered.canonical_bytes()
    )
    failures: list[str] = []
    if formatter_inventory != expected_formatters:
        failures.append("overlay formatter inventory or scale changed")
    if not presentation_inventory_valid:
        failures.append("presentation pane or overlay inventory changed")
    if not crossed_without_verdict:
        failures.append("crossed consolidated quote gained or lost an integrity claim")
    if not display_values_valid:
        failures.append("fixed-point overlay display formatting changed")
    if not early_relative_volume_unavailable:
        failures.append("early relative volume no longer explains unavailability")
    if not provenance_observations_valid:
        failures.append("presentation provenance lost selection or freshness metadata")
    if not authority_valid:
        failures.append("safe presentation metadata lost source-bound authority")
    if not section_inventory_valid or not deferred_sections_valid:
        failures.append("reserved or deferred report section contract changed")
    if not trace_availability_valid:
        failures.append(
            "causal trace section availability no longer follows recordings"
        )
    if not report_identity_valid:
        failures.append(
            "report semantic identity depends on order or display-only time"
        )
    return ReplayMicroscopeAuditCase(
        "presentation_preserves_panes_overlays_provenance_and_deferred_sections",
        (
            f"panes={len(PANE_ORDER)} overlays={len(OVERLAY_KIND_ORDER)} "
            f"sections={len(REPORT_SECTION_ORDER)} provenance={len(source_references)}"
        ),
        {
            "crossed_without_integrity_verdict": crossed_without_verdict,
            "deferred_sections_valid": deferred_sections_valid,
            "display_values_valid": display_values_valid,
            "formatter_inventory_valid": formatter_inventory == expected_formatters,
            "metadata_authority_valid": authority_valid,
            "presentation_inventory_valid": presentation_inventory_valid,
            "provenance_observations_valid": provenance_observations_valid,
            "report_identity_valid": report_identity_valid,
            "trace_availability_valid": trace_availability_valid,
        },
        tuple(failures),
    )


def _portable_offline_relocation_case() -> ReplayMicroscopeAuditCase:
    artifact = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )
    expected_members = {
        "assets/report.css",
        "assets/report.js",
        "index.html",
        "manifest.json",
    }
    with tempfile.TemporaryDirectory(prefix="kirby2-wo36d-relocation-") as directory:
        root = Path(directory).resolve()
        original = root / "original"
        relocated = root / "relocated"
        index_path = write_portable_report_bundle(artifact.bundle, original)
        original_verification = verify_portable_report_bundle(original)
        shutil.copytree(original, relocated)
        relocated_verification = verify_portable_report_bundle(relocated)
        actual_members = {
            path.relative_to(original).as_posix()
            for path in original.rglob("*")
            if path.is_file()
        }
        byte_identical = all(
            original.joinpath(name).read_bytes()
            == relocated.joinpath(name).read_bytes()
            for name in expected_members
        )
        asset_digests = {
            name: hashlib.sha256(
                original.joinpath("assets", name).read_bytes()
            ).hexdigest()
            for name in ("report.css", "report.js")
        }
        asset_pins_valid = all(
            asset_digests[name] == REPORT_ASSET_SHA256[name]
            for name in asset_digests
        )
        local_payload = b"\n".join(
            original.joinpath(name).read_bytes()
            for name in (
                "assets/report.css",
                "assets/report.js",
                "index.html",
            )
        ).lower()
        network_tokens = tuple(
            token.decode("ascii")
            for token in (
                b"http://",
                b"https://",
                b"fetch(",
                b"xmlhttprequest",
                b"websocket",
                b"eventsource",
                b"sendbeacon",
            )
            if token in local_payload
        )
        existing_destination_rejected = _wo36d_rejected(
            lambda: write_portable_report_bundle(artifact.bundle, original)
        )
    verification_valid = (
        original_verification == relocated_verification
        and original_verification["status"] == "PASS"
        and original_verification["member_count"] == 4
        and original_verification["report_id"] == artifact.report.report_id
        and original_verification["bundle_id"] == artifact.bundle.bundle_id
    )
    failures: list[str] = []
    if index_path != original / "index.html":
        failures.append("portable writer returned a noncanonical entry path")
    if actual_members != expected_members:
        failures.append("portable bundle materialized an unexpected member inventory")
    if not byte_identical or not verification_valid:
        failures.append("relocated portable bundle changed bytes or verification")
    if not asset_pins_valid:
        failures.append(
            "materialized renderer assets differ from installed digest pins"
        )
    if network_tokens:
        failures.append("portable renderer contains a network-capable token")
    if not existing_destination_rejected:
        failures.append("portable writer overwrote an existing destination")
    return ReplayMicroscopeAuditCase(
        "portable_bundle_is_relocatable_offline_and_digest_bound",
        (
            f"members={len(actual_members)} relocated={verification_valid} "
            f"network_tokens={len(network_tokens)}"
        ),
        {
            "asset_digests": asset_digests,
            "asset_pins_valid": asset_pins_valid,
            "bundle_id": artifact.bundle.bundle_id,
            "byte_identical_after_relocation": byte_identical,
            "existing_destination_rejected": existing_destination_rejected,
            "member_inventory": sorted(actual_members),
            "network_tokens": list(network_tokens),
            "verification": relocated_verification,
        },
        tuple(failures),
    )


def _portable_tamper_refusal_case() -> ReplayMicroscopeAuditCase:
    artifact = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )

    def manifest_at(root: Path) -> dict[str, object]:
        payload = json.loads((root / "manifest.json").read_bytes())
        if not isinstance(payload, dict):  # pragma: no cover - fixture invariant
            raise RuntimeError("portable fixture manifest is not an object")
        return payload

    def write_manifest(root: Path, payload: dict[str, object]) -> None:
        (root / "manifest.json").write_bytes(
            _audit_canonical_json_bytes(payload)
        )

    def repin_member(root: Path, name: str, suffix: bytes) -> None:
        path = root.joinpath(*name.split("/"))
        changed = path.read_bytes() + suffix
        path.write_bytes(changed)
        manifest = manifest_at(root)
        rows = manifest["members"]
        if not isinstance(rows, list):  # pragma: no cover - fixture invariant
            raise RuntimeError("portable fixture member rows are invalid")
        row = next(
            item
            for item in rows
            if isinstance(item, dict) and item.get("path") == name
        )
        row["sha256"] = hashlib.sha256(changed).hexdigest()
        row["size_bytes"] = len(changed)
        write_manifest(root, manifest)

    def change_report_id(root: Path) -> None:
        manifest = manifest_at(root)
        manifest["report_id"] = "portable-replay-report-000000000000000000000000"
        write_manifest(root, manifest)

    def change_semantic_digest(root: Path) -> None:
        manifest = manifest_at(root)
        manifest["report_semantic_sha256"] = "0" * 64
        write_manifest(root, manifest)

    def change_member_path(root: Path) -> None:
        manifest = manifest_at(root)
        rows = manifest["members"]
        if not isinstance(rows, list) or not isinstance(rows[0], dict):
            raise RuntimeError("portable fixture member rows are invalid")
        rows[0]["path"] = "../outside.css"
        write_manifest(root, manifest)

    def drop_manifest_row(root: Path) -> None:
        manifest = manifest_at(root)
        rows = manifest["members"]
        if not isinstance(rows, list):
            raise RuntimeError("portable fixture member rows are invalid")
        manifest["members"] = rows[:-1]
        write_manifest(root, manifest)

    def make_manifest_noncanonical(root: Path) -> None:
        path = root / "manifest.json"
        path.write_bytes(path.read_bytes() + b"\n")

    def add_extra_file(root: Path) -> None:
        (root / "unmanifested.txt").write_bytes(b"not declared")

    attacks = (
        ("repinned_report_id", change_report_id),
        ("repinned_semantic_digest", change_semantic_digest),
        (
            "repinned_css",
            lambda root: repin_member(root, "assets/report.css", b"\n/* forged */"),
        ),
        (
            "repinned_javascript",
            lambda root: repin_member(root, "assets/report.js", b"\n// forged"),
        ),
        (
            "repinned_index",
            lambda root: repin_member(root, "index.html", b"\n"),
        ),
        ("path_traversal", change_member_path),
        ("missing_manifest_row", drop_manifest_row),
        ("noncanonical_manifest", make_manifest_noncanonical),
        ("unmanifested_extra_file", add_extra_file),
    )
    with tempfile.TemporaryDirectory(prefix="kirby2-wo36d-tamper-") as directory:
        root = Path(directory).resolve()
        baseline = root / "baseline"
        write_portable_report_bundle(artifact.bundle, baseline)
        baseline_valid = verify_portable_report_bundle(baseline)["status"] == "PASS"
        refusals: dict[str, bool] = {}
        for name, mutate in attacks:
            target = root / name
            shutil.copytree(baseline, target)
            mutate(target)
            refusals[name] = _wo36d_rejected(
                lambda target=target: verify_portable_report_bundle(target)
            )
    failures: list[str] = []
    if not baseline_valid:
        failures.append("portable tamper fixture did not verify before mutation")
    for name, rejected in refusals.items():
        if not rejected:
            failures.append(f"portable verifier accepted hostile {name} mutation")
    return ReplayMicroscopeAuditCase(
        "portable_verifier_rejects_repinned_manifest_and_asset_tampering",
        (
            f"attacks={len(refusals)} "
            f"rejected={sum(refusals.values())} baseline={baseline_valid}"
        ),
        {
            "attack_refusals": refusals,
            "baseline_valid": baseline_valid,
        },
        tuple(failures),
    )


def _portable_factory_boundary_case() -> ReplayMicroscopeAuditCase:
    observed = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.AS_OBSERVED,
    )
    postmortem = build_microscope_demo_artifact(
        STALE_PARTIAL_CANCEL_RACE_FIXTURE,
        ObservationMode.POSTMORTEM,
    )
    mixed_authority_rejected = _wo36d_rejected(
        lambda: build_portable_replay_report(
            (observed.report.frames[0], postmortem.report.frames[0])
        )
    )
    duplicate_frame_rejected = _wo36d_rejected(
        lambda: build_portable_replay_report(
            (observed.report.frames[0], observed.report.frames[0])
        )
    )
    direct_frame_rejected = _wo36d_rejected(
        lambda: ReplayPresentationFrameV1(
            identity={},
            timeline_root={},
            cursor={},
            pane_snapshot={},
            overlay_set={},
            presentation=observed.report.frames[0].presentation,
            _construction_token=None,
        )
    )
    direct_report_rejected = _wo36d_rejected(
        lambda: PortableReplayReportV1(
            frames=(),
            sections=(),
            renderer_assets=(),
            _construction_token=None,
        )
    )
    direct_bundle_rejected = _wo36d_rejected(
        lambda: PortableReportBundle(
            report_id=observed.report.report_id,
            members={},
            manifest={},
            bundle_id=observed.bundle.bundle_id,
            _construction_token=None,
        )
    )
    unknown_fixture_rejected = _wo36d_rejected(
        lambda: build_microscope_demo_artifact(
            "unknown-fixture",
            ObservationMode.AS_OBSERVED,
        )
    )
    string_mode_rejected = _wo36d_rejected(
        lambda: build_microscope_demo_artifact(
            STALE_PARTIAL_CANCEL_RACE_FIXTURE,
            "AS_OBSERVED",  # type: ignore[arg-type]
        )
    )
    frame = observed.report.frames[0]
    before = frame.canonical_bytes()
    exported = frame.as_dict()
    identity = exported.get("identity")
    if not isinstance(identity, dict):  # pragma: no cover - fixture invariant
        raise RuntimeError("portable frame export identity is not an object")
    identity["query_id"] = "forged-query"
    export_detached = frame.canonical_bytes() == before
    direct_mutation_rejected = False
    try:
        frame.identity["query_id"] = "forged-query"  # type: ignore[index]
    except TypeError:
        direct_mutation_rejected = True

    forbidden_vocabulary = (
        "authorization_id",
        "backend_callback",
        "backend_handle",
        "capability_manifest_bytes",
        "event_count",
        "event_inventory_sha256",
        "ingestion_receipt",
        "inventory_commitment",
        "maximum_cursor_time_us",
        "maximum_policy_visible_time_us",
        "minimum_cursor_time_us",
        "minimum_policy_visible_time_us",
        "observation_query_result",
        "overlay_projection_receipt",
        "partition_count",
        "partition_inventory_sha256",
        "query_inventory_sha256",
        "raw_observed_evidence",
        "raw_reveal_evidence",
        "reveal_authorization",
        "reveal_authorization_id",
        "reveal_authorization_ids",
        "reveal_authorization_sha256",
        "timeline_receipt",
    )
    serialized_payloads = [
        observed.report.canonical_bytes(),
        postmortem.report.canonical_bytes(),
    ]
    serialized_payloads.extend(
        artifact.bundle.member_bytes(name)
        for artifact in (observed, postmortem)
        for name in (
            "assets/report.css",
            "assets/report.js",
            "index.html",
            "manifest.json",
        )
    )
    forbidden_findings = tuple(
        item
        for item in forbidden_vocabulary
        if any(
            (f'"{item}"').encode("ascii") in payload
            for payload in serialized_payloads
        )
    )
    boundary_checks = {
        "direct_bundle_rejected": direct_bundle_rejected,
        "direct_frame_rejected": direct_frame_rejected,
        "direct_report_rejected": direct_report_rejected,
        "duplicate_frame_rejected": duplicate_frame_rejected,
        "export_detached": export_detached,
        "frozen_direct_mutation_rejected": direct_mutation_rejected,
        "mixed_authority_rejected": mixed_authority_rejected,
        "string_mode_rejected": string_mode_rejected,
        "unknown_fixture_rejected": unknown_fixture_rejected,
    }
    failures: list[str] = []
    for name, passed in boundary_checks.items():
        if not passed:
            failures.append(name.replace("_", " "))
    if forbidden_findings:
        failures.append("portable output contains backend-only vocabulary")
    return ReplayMicroscopeAuditCase(
        "presentation_and_report_factories_reject_cross_root_and_backend_material",
        (
            f"boundary_checks={sum(boundary_checks.values())}/{len(boundary_checks)} "
            f"forbidden_findings={len(forbidden_findings)}"
        ),
        {
            **boundary_checks,
            "forbidden_vocabulary_findings": list(forbidden_findings),
        },
        tuple(failures),
    )


def _wo36d_rejected(operation: object) -> bool:
    if not callable(operation):  # pragma: no cover
        raise RuntimeError("WO36-D rejection probe must be callable")
    try:
        operation()
    except (OSError, TypeError, ValueError):
        return True
    return False


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


def _timeline_cursor_partition_case() -> ReplayMicroscopeAuditCase:
    observed = _wo36c_timeline_fixture()
    at_100 = query_as_observed(observed, ObservationQueryRequest(100, 100))
    at_200 = query_as_observed(observed, ObservationQueryRequest(200, 200))
    at_400 = query_as_observed(observed, ObservationQueryRequest(400, 400))
    player = timeline_event_from_query_result(
        at_100,
        "player-action-event-0001",
        TimelineEventKind.PLAYER_ACTION,
    )
    fill = timeline_event_from_query_result(
        at_200,
        "fill-event-0001",
        TimelineEventKind.FILL,
    )
    traffic = timeline_event_from_query_result(
        at_200,
        "traffic-transition-event-0001",
        TimelineEventKind.TRAFFIC_LIGHT_TRANSITION,
    )
    warning = timeline_event_from_query_result(
        at_400,
        "invariant-warning-event-0001",
        TimelineEventKind.INVARIANT_WARNING,
    )
    observed_update = timeline_event_from_query_result(
        at_400,
        "observed-update-event-0001",
        TimelineEventKind.OBSERVED_UPDATE,
    )
    branch = derive_timeline_event(
        "branch-divergence-derived-0001",
        TimelineEventKind.BRANCH_DIVERGENCE,
        6,
        (warning, observed_update),
    )
    inventory = (warning, fill, player, branch, observed_update, traffic)
    timeline, receipt = build_replay_timeline(
        at_400,
        inventory,
    )
    repeated, repeated_receipt = build_replay_timeline(
        at_400,
        tuple(reversed(inventory)),
    )

    cursor_100 = timeline.cursor(100)
    next_partition = timeline.step_event(cursor_100, TimelineDirection.NEXT)
    next_again = timeline.step_event(
        next_partition.cursor,
        TimelineDirection.NEXT,
    )
    fixed = timeline.step_fixed_time(
        next_partition.cursor,
        50,
        TimelineDirection.NEXT,
    )
    playing = timeline.play(next_partition.cursor)
    paused = timeline.pause(playing)
    jumps = {
        target: timeline.jump(
            timeline.cursor(0),
            target,
            TimelineDirection.NEXT,
        )
        for target in (
            TimelineJumpTarget.PLAYER_ACTION,
            TimelineJumpTarget.FILL,
            TimelineJumpTarget.TRAFFIC_LIGHT_TRANSITION,
            TimelineJumpTarget.INVARIANT_WARNING,
            TimelineJumpTarget.BRANCH_DIVERGENCE,
        )
    }
    reveal_refusal = timeline.jump(
        timeline.cursor(0),
        TimelineJumpTarget.REVEALED_REGIME_TRANSITION,
        TimelineDirection.NEXT,
    )
    boundary = timeline.step_event(
        next_again.cursor,
        TimelineDirection.NEXT,
    )
    bookmark = timeline.bookmark(next_partition.cursor)
    annotation = timeline.annotate(next_partition.cursor)

    policy_observed = _wo36c_timeline_fixture()
    policy_reveal = _wo36c_regime_reveal_fixture(policy_observed)
    policy_request = ObservationQueryRequest(
        300,
        action_time_us=300,
        requested_reveal_capabilities=(RevealCapability.GROUND_TRUTH,),
    )
    policy_authorization = _reveal_authorization(
        policy_reveal,
        (RevealCapability.GROUND_TRUTH,),
        observed=policy_observed,
    )
    policy_query = query_postmortem(
        policy_observed,
        policy_reveal,
        policy_authorization,
        policy_request,
    )
    regime = timeline_event_from_query_result(
        policy_query,
        "regime-transition-event-0001",
        TimelineEventKind.REVEALED_REGIME_TRANSITION,
    )
    postmortem_timeline, _ = build_replay_timeline(
        policy_query,
        (regime,),
    )
    reveal_jump = postmortem_timeline.jump(
        postmortem_timeline.cursor(0),
        TimelineJumpTarget.REVEALED_REGIME_TRANSITION,
        TimelineDirection.NEXT,
    )
    denied_policy_query = query_postmortem(
        policy_observed,
        None,
        None,
        policy_request,
    )
    unproven_postmortem, _ = build_replay_timeline(
        denied_policy_query,
        (),
    )
    unproven_reveal_jump = unproven_postmortem.jump(
        unproven_postmortem.cursor(0),
        TimelineJumpTarget.REVEALED_REGIME_TRANSITION,
        TimelineDirection.NEXT,
    )
    authorized_empty_query = query_postmortem(
        policy_observed,
        policy_reveal,
        policy_authorization,
        ObservationQueryRequest(
            0,
            action_time_us=0,
            requested_reveal_capabilities=(RevealCapability.GROUND_TRUTH,),
        ),
    )
    authorized_empty_timeline, _ = build_replay_timeline(
        authorized_empty_query,
        (),
    )
    authorized_empty_jump = authorized_empty_timeline.jump(
        authorized_empty_timeline.cursor(0),
        TimelineJumpTarget.REVEALED_REGIME_TRANSITION,
        TimelineDirection.NEXT,
    )
    hidden_regime_rejected = False
    hidden_reveal = _wo36c_regime_reveal_fixture(
        policy_observed,
        capability=RevealCapability.HIDDEN_STATE,
    )
    hidden_query = query_postmortem(
        policy_observed,
        hidden_reveal,
        _reveal_authorization(
            hidden_reveal,
            (RevealCapability.HIDDEN_STATE,),
            observed=policy_observed,
        ),
        ObservationQueryRequest(
            300,
            action_time_us=300,
            requested_reveal_capabilities=(RevealCapability.HIDDEN_STATE,),
        ),
    )
    try:
        timeline_event_from_query_result(
            hidden_query,
            "regime-transition-event-0001",
        )
    except ValueError:
        hidden_regime_rejected = True
    cross_grant_event_rejected = False
    alternate_authorization = replace(
        policy_authorization,
        authorization_id="authorization-wo36c-alternate-ground-truth",
    )
    alternate_policy_query = query_postmortem(
        policy_observed,
        policy_reveal,
        alternate_authorization,
        policy_request,
    )
    try:
        build_replay_timeline(alternate_policy_query, (regime,))
    except ValueError:
        cross_grant_event_rejected = True

    public_payload = timeline.as_dict()
    receipt_only_keys = {
        "event_count",
        "event_inventory_sha256",
        "maximum_cursor_time_us",
        "minimum_cursor_time_us",
        "partition_count",
        "partition_inventory_sha256",
        "reveal_availability",
        "reveal_authorization_ids",
        "reveal_evidence_sha256s",
        "root_query_id",
        "root_query_sha256",
        "root_render_cursor_time_us",
    }
    failures: list[str] = []
    expected_200 = ("fill-event-0001", "traffic-transition-event-0001")
    actual_200 = tuple(item.event_id for item in next_partition.cursor.current_events)
    if actual_200 != expected_200:
        failures.append("simultaneous cursor partition is not deterministic")
    expected_400 = (
        "invariant-warning-event-0001",
        "observed-update-event-0001",
        "branch-divergence-derived-0001",
    )
    actual_400 = tuple(item.event_id for item in next_again.cursor.current_events)
    if actual_400 != expected_400:
        failures.append("derived and direct hard-boundary events did not partition")
    if fixed.cursor.render_cursor_time_us != 250 or fixed.cursor.current_events:
        failures.append("fixed-time step did not preserve an exact empty cursor")
    if (
        playing.playback_state is not TimelinePlaybackState.PLAYING
        or paused.playback_state is not TimelinePlaybackState.PAUSED
        or next_partition.cursor.playback_state is not TimelinePlaybackState.PAUSED
    ):
        failures.append("play/pause was not a pure cursor-state operation")
    expected_jump_times = {
        TimelineJumpTarget.PLAYER_ACTION: 100,
        TimelineJumpTarget.FILL: 200,
        TimelineJumpTarget.TRAFFIC_LIGHT_TRANSITION: 200,
        TimelineJumpTarget.INVARIANT_WARNING: 400,
        TimelineJumpTarget.BRANCH_DIVERGENCE: 400,
    }
    for target, destination in expected_jump_times.items():
        result = jumps[target]
        if (
            result.availability is not TimelineNavigationAvailability.AVAILABLE
            or result.cursor.render_cursor_time_us != destination
            or any(item.event_kind.value != target.value for item in result.selected_events)
        ):
            failures.append(f"{target.value} jump changed semantics")
    if (
        reveal_refusal.availability
        is not TimelineNavigationAvailability.UNAVAILABLE
        or reveal_refusal.unavailable_reason
        is not TimelineNavigationUnavailableReason.REVEAL_NOT_AUTHORIZED
    ):
        failures.append("AS_OBSERVED revealed-regime jump did not fail closed")
    if (
        reveal_jump.availability is not TimelineNavigationAvailability.AVAILABLE
        or reveal_jump.cursor.render_cursor_time_us != 300
    ):
        failures.append("authorized postmortem revealed-regime jump is unavailable")
    if (
        unproven_reveal_jump.availability
        is not TimelineNavigationAvailability.UNAVAILABLE
        or unproven_reveal_jump.unavailable_reason
        is not TimelineNavigationUnavailableReason.REVEAL_NOT_AUTHORIZED
    ):
        failures.append("POSTMORTEM mode alone implied reveal authorization")
    if (
        authorized_empty_jump.availability
        is not TimelineNavigationAvailability.UNAVAILABLE
        or authorized_empty_jump.unavailable_reason
        is not TimelineNavigationUnavailableReason.NO_MATCHING_EVENT_IN_DIRECTION
    ):
        failures.append("authorized empty reveal was mislabeled as unauthorized")
    if not hidden_regime_rejected:
        failures.append("hidden-state evidence was relabeled as a regime transition")
    if not cross_grant_event_rejected:
        failures.append("timeline event crossed reveal authorization grants")
    if (
        boundary.availability is not TimelineNavigationAvailability.UNAVAILABLE
        or boundary.unavailable_reason
        is not TimelineNavigationUnavailableReason.TIMELINE_BOUNDARY
    ):
        failures.append("timeline boundary lacks its typed refusal")
    if any(
        item.reason is not TimelineSidecarRefusalReason.DEFERRED_TO_WO36_E
        for item in (bookmark, annotation)
    ):
        failures.append("WO36-C persisted a deferred sidecar operation")
    if receipt_only_keys & set(public_payload):
        failures.append("cursor-safe timeline root exposes full-run inventory facts")
    if (
        receipt.schema_id != TIMELINE_RECEIPT_SCHEMA_ID
        or public_payload.get("schema_id") != TIMELINE_SCHEMA_ID
        or receipt.event_count != 6
        or receipt.partition_count != 3
    ):
        failures.append("timeline backend receipt inventory changed")
    if (
        repeated.canonical_bytes() != timeline.canonical_bytes()
        or repeated_receipt.canonical_bytes() != receipt.canonical_bytes()
    ):
        failures.append("timeline build depends on input ordering")

    detail = (
        f"timeline_id={timeline.timeline_id} events={receipt.event_count} "
        f"partitions={receipt.partition_count} jumps=6 sidecars=DEFERRED_TO_WO36_E"
    )
    return ReplayMicroscopeAuditCase(
        "timeline_controls_partition_simultaneous_events_deterministically",
        detail,
        {
            "authorized_reveal_jump": reveal_jump.as_dict(),
            "authorized_empty_reveal_jump": authorized_empty_jump.as_dict(),
            "cursor_200_event_ids": list(actual_200),
            "cursor_400_event_ids": list(actual_400),
            "cross_grant_event_rejected": cross_grant_event_rejected,
            "public_timeline": public_payload,
            "receipt_sha256": receipt.receipt_sha256,
            "reveal_refusal": reveal_refusal.as_dict(),
            "timeline_id": timeline.timeline_id,
            "hidden_regime_rejected": hidden_regime_rejected,
            "unproven_postmortem_refusal": unproven_reveal_jump.as_dict(),
        },
        tuple(failures),
    )


def _synchronized_pane_inventory_case() -> ReplayMicroscopeAuditCase:
    expected_pane_order = (
        PaneKind.LEVEL_2_LADDER,
        PaneKind.TIME_AND_SALES,
        PaneKind.DEPTH_HEATMAP,
        PaneKind.INDIVIDUAL_QUEUE,
        PaneKind.PLAYER_ORDERS,
        PaneKind.ORDER_STATE_LIFECYCLE,
        PaneKind.POSITION,
        PaneKind.TRAFFIC_LIGHT,
        PaneKind.STRATEGY_RULE_EVIDENCE,
        PaneKind.FEATURE_PROVENANCE,
        PaneKind.AGENT_ACTIVITY,
        PaneKind.LATENCY_TIMELINE,
        PaneKind.VENUE_QUOTES,
        PaneKind.CONSOLIDATED_QUOTES,
        PaneKind.FILLS,
        PaneKind.EXECUTION_METRICS,
        PaneKind.MECHANISTIC_TRACE,
        PaneKind.COUNTERFACTUAL_COMPARISON,
    )
    observed = _wo36c_read_model_fixture()
    result = query_as_observed(
        observed,
        ObservationQueryRequest(60_000_000, action_time_us=59_750_000),
    )
    supported = tuple(
        item
        for item in expected_pane_order
        if item not in {PaneKind.AGENT_ACTIVITY, PaneKind.COUNTERFACTUAL_COMPARISON}
    )
    manifest_bytes, manifest_sha256 = _wo36c_capability_manifest(
        result,
        source_class="HISTORICAL",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
    )
    capabilities = load_verified_pane_capabilities(
        manifest_bytes,
        manifest_sha256,
    )
    snapshot = build_synchronized_panes(result, capabilities=capabilities)
    verified_ingress_snapshot = build_synchronized_panes(
        _load_ingress_fixture(_observed_ingress_fixture()).result(400)
    )
    verified_ingress_panes = {
        pane.pane_kind
        for pane in verified_ingress_snapshot.panes
        if pane.data
    }

    reversed_result = query_as_observed(
        _wo36c_read_model_fixture(reverse=True),
        ObservationQueryRequest(60_000_000, action_time_us=59_750_000),
    )
    reversed_manifest, reversed_pin = _wo36c_capability_manifest(
        reversed_result,
        source_class="HISTORICAL",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
    )
    repeated = build_synchronized_panes(
        reversed_result,
        capabilities=load_verified_pane_capabilities(
            reversed_manifest,
            reversed_pin,
        ),
    )

    before_boundary = query_as_observed(
        observed,
        ObservationQueryRequest(59_499_999),
    )
    at_boundary = query_as_observed(
        observed,
        ObservationQueryRequest(59_500_000),
    )
    before_ids = {item.event_id for item in before_boundary.values}
    boundary_ids = {item.event_id for item in at_boundary.values}
    result_event_ids = {item.event_id for item in result.values}
    provenance_ids: set[str] = set()
    comparison_laundering_rejected = False
    try:
        comparison_source = ObservedEvidenceSet(
            result.source_run_id,
            result.source_event_sha256,
            client_delivered=(
                ObservedValueRecord(
                    "comparison.counterfactual.fills",
                    "counterfactual-laundering-event-0001",
                    1,
                    _delivered_timing(1, 2, 3),
                    {"fill_quantity": 999},
                ),
            ),
        )
        build_synchronized_panes(
            query_as_observed(
                comparison_source,
                ObservationQueryRequest(3),
            )
        )
    except (TypeError, ValueError):
        comparison_laundering_rejected = True
    wrong_plane_signal_rejected = False
    try:
        wrong_plane_signal = ObservedEvidenceSet(
            result.source_run_id,
            result.source_event_sha256,
            client_delivered=(
                ObservedValueRecord(
                    "strategy.signal",
                    "wrong-plane-signal-event-0001",
                    1,
                    _delivered_timing(1, 2, 3),
                    {"recorded_signal": "GREEN"},
                ),
            ),
        )
        build_synchronized_panes(
            query_as_observed(wrong_plane_signal, ObservationQueryRequest(3))
        )
    except (TypeError, ValueError):
        wrong_plane_signal_rejected = True
    malformed_level2_rejected = False
    try:
        malformed_level2 = ObservedEvidenceSet(
            result.source_run_id,
            result.source_event_sha256,
            client_delivered=(
                ObservedValueRecord(
                    "book.level2.malformed",
                    "malformed-level2-event-0001",
                    1,
                    _delivered_timing(1, 2, 3),
                    {"asks": [], "bids": "not-depth", "record_kind": "LEVEL_2"},
                ),
            ),
        )
        build_synchronized_panes(
            query_as_observed(malformed_level2, ObservationQueryRequest(3))
        )
    except (TypeError, ValueError):
        malformed_level2_rejected = True
    unknown_signal_rejected = False
    try:
        unknown_signal = ObservedEvidenceSet(
            result.source_run_id,
            result.source_event_sha256,
            decision_snapshots=(
                ObservedValueRecord(
                    "strategy.signal",
                    "unknown-signal-event-0001",
                    1,
                    EvidenceTiming(
                        source_event_time_us=1,
                        venue_receipt=EvidenceTimestamp.not_applicable(
                            TimestampAbsenceReason.CLIENT_DECISION
                        ),
                        client_receive=EvidenceTimestamp.not_applicable(
                            TimestampAbsenceReason.RECORDED_SNAPSHOT
                        ),
                        client_knowledge=EvidenceTimestamp.recorded(3),
                    ),
                    {"recorded_signal": "PURPLE"},
                ),
            ),
        )
        build_synchronized_panes(
            query_as_observed(unknown_signal, ObservationQueryRequest(3))
        )
    except (TypeError, ValueError):
        unknown_signal_rejected = True

    def feature_timing(source_time_us: int, knowledge_time_us: int) -> EvidenceTiming:
        return EvidenceTiming(
            source_event_time_us=source_time_us,
            venue_receipt=EvidenceTimestamp.not_applicable(
                TimestampAbsenceReason.CLIENT_DECISION
            ),
            client_receive=EvidenceTimestamp.not_applicable(
                TimestampAbsenceReason.RECORDED_SNAPSHOT
            ),
            client_knowledge=EvidenceTimestamp.recorded(knowledge_time_us),
        )

    self_feature_event_id = "self-feature-event-0001"
    self_feature_result = query_as_observed(
        ObservedEvidenceSet(
            result.source_run_id,
            result.source_event_sha256,
            decision_snapshots=(
                ObservedValueRecord(
                    "feature.self-provenance",
                    self_feature_event_id,
                    1,
                    feature_timing(1, 2),
                    {
                        "provenance_event_ids": [self_feature_event_id],
                        "value_ppm": 1,
                    },
                ),
            ),
        ),
        ObservationQueryRequest(2),
    )
    future_feature_event_id = "future-feature-event-0001"
    future_quote_event_id = "future-feature-source-event-0001"
    future_feature_result = query_as_observed(
        ObservedEvidenceSet(
            result.source_run_id,
            result.source_event_sha256,
            client_delivered=(
                ObservedValueRecord(
                    "quote.consolidated.feature-probe",
                    future_quote_event_id,
                    2,
                    _delivered_timing(3, 3, 4),
                    {"best_ask_ticks": 102, "best_bid_ticks": 100},
                ),
            ),
            decision_snapshots=(
                ObservedValueRecord(
                    "feature.future-provenance",
                    future_feature_event_id,
                    1,
                    feature_timing(1, 2),
                    {
                        "provenance_event_ids": [future_quote_event_id],
                        "value_ppm": 1,
                    },
                ),
            ),
        ),
        ObservationQueryRequest(4),
    )
    reveal_feature_observed = ObservedEvidenceSet(
        result.source_run_id,
        result.source_event_sha256,
        decision_snapshots=(
            ObservedValueRecord(
                "feature.reveal-provenance",
                "reveal-feature-event-0001",
                1,
                feature_timing(59_700_000, 59_800_000),
                {
                    "provenance_event_ids": ["agent-activity-event-0001"],
                    "value_ppm": 1,
                },
            ),
        ),
    )
    reveal_feature_source = _wo36c_reveal_fixture(
        reveal_feature_observed,
        include_queue=False,
    )
    reveal_feature_requested = (RevealCapability.HIDDEN_STATE,)
    reveal_feature_result = query_postmortem(
        reveal_feature_observed,
        reveal_feature_source,
        _reveal_authorization(
            reveal_feature_source,
            reveal_feature_requested,
            observed=reveal_feature_observed,
        ),
        ObservationQueryRequest(
            60_000_000,
            requested_reveal_capabilities=reveal_feature_requested,
        ),
    )
    feature_provenance_refusals: dict[str, bool] = {}
    for label, hostile_result in (
        ("self", self_feature_result),
        ("future", future_feature_result),
        ("reveal", reveal_feature_result),
    ):
        try:
            build_synchronized_panes(hostile_result)
        except (TypeError, ValueError):
            feature_provenance_refusals[label] = True
        else:
            feature_provenance_refusals[label] = False
    failures: list[str] = []
    expected_ingress_panes = {
        PaneKind.PLAYER_ORDERS,
        PaneKind.ORDER_STATE_LIFECYCLE,
        PaneKind.TRAFFIC_LIGHT,
        PaneKind.STRATEGY_RULE_EVIDENCE,
        PaneKind.FEATURE_PROVENANCE,
        PaneKind.LATENCY_TIMELINE,
        PaneKind.CONSOLIDATED_QUOTES,
        PaneKind.FILLS,
    }
    if verified_ingress_panes != expected_ingress_panes:
        failures.append("verified DEV-0006 ingress no longer satisfies pane contracts")
    if PANE_ORDER != expected_pane_order or tuple(
        item.pane_kind for item in snapshot.panes
    ) != expected_pane_order:
        failures.append("synchronized pane inventory/order changed")
    expected_root = (
        result.request.render_cursor_time_us,
        result.policy.mode,
        result.policy.policy_id,
        result.query_id,
    )
    for pane in snapshot.panes:
        actual_root = (
            pane.render_cursor_time_us,
            pane.observation_mode,
            pane.policy_id,
            pane.query_id,
        )
        if actual_root != expected_root:
            failures.append(f"{pane.pane_kind.value} differs from the shared root")
        for datum in pane.data:
            for source in datum.source_events:
                provenance_ids.add(source.event_id)
                if (
                    source.event_id not in result_event_ids
                    or source.policy_visible_at_time_us
                    > result.request.render_cursor_time_us
                ):
                    failures.append(
                        f"{pane.pane_kind.value} cites unauthorized/future evidence"
                    )
            if datum.calculation is not None and datum.calculation.source_event_ids != tuple(
                sorted(item.event_id for item in datum.source_events)
            ):
                failures.append(
                    f"{pane.pane_kind.value} calculation lost its exact sources"
                )
        for estimate in pane.queue_estimates:
            for source in estimate.source_events:
                provenance_ids.add(source.event_id)
                if source.event_id not in result_event_ids:
                    failures.append("queue estimate cites an unqueried event")
    queue = snapshot.pane(PaneKind.INDIVIDUAL_QUEUE)
    if (
        queue.availability is not PaneAvailability.AVAILABLE
        or len(queue.queue_estimates) != 1
        or queue.queue_estimates[0].estimator_version != "queue-estimator.v1"
        or queue.queue_estimates[0].truth_availability
        is not QueueTruthAvailability.AUTHORIZATION_REQUIRED
        or queue.queue_estimates[0].truth_quantity_ahead is not None
    ):
        failures.append("historical queue estimate capability/truth contract changed")
    agent = snapshot.pane(PaneKind.AGENT_ACTIVITY)
    comparison = snapshot.pane(PaneKind.COUNTERFACTUAL_COMPARISON)
    if (
        agent.availability is not PaneAvailability.UNAVAILABLE
        or agent.explanation is None
        or agent.explanation.reason
        is not PaneUnavailableReason.AUTHORIZED_REVEAL_REQUIRED
    ):
        failures.append("AS_OBSERVED agent activity did not fail closed")
    if (
        comparison.availability is not PaneAvailability.UNAVAILABLE
        or comparison.explanation is None
        or comparison.explanation.reason
        is not PaneUnavailableReason.COUNTERFACTUAL_NOT_SELECTED
    ):
        failures.append("WO36-E comparison slot is not explicitly unavailable")
    if not comparison_laundering_rejected:
        failures.append("WO36-E comparison slot accepted an unowned query namespace")
    if not wrong_plane_signal_rejected:
        failures.append("traffic-light pane accepted the wrong evidence plane")
    if not malformed_level2_rejected:
        failures.append("Level 2 pane accepted a malformed payload")
    if not unknown_signal_rejected:
        failures.append("traffic-light pane accepted an unknown closed state")
    if not all(feature_provenance_refusals.values()):
        failures.append("feature pane accepted self, future, or reveal provenance")
    feature_source_ids = {
        source.event_id
        for datum in snapshot.pane(PaneKind.FEATURE_PROVENANCE).data
        for source in datum.source_events
    }
    if feature_source_ids != {"feature-event-0001", "top-book-event-late"}:
        failures.append("feature provenance did not resolve exact query source events")
    if snapshot.available_pane_count != 16:
        failures.append("expected sixteen source-backed ordinary panes")
    if (
        "top-book-event-late" in before_ids
        or "top-book-event-late" not in boundary_ids
    ):
        failures.append("hard cursor boundary exposed a quote early or late")
    if repeated.canonical_bytes() != snapshot.canonical_bytes():
        failures.append("pane snapshot depends on input ordering")
    if capabilities.authority is not PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST:
        failures.append("queue capability was not sourced from a pinned backend manifest")

    detail = (
        f"snapshot_id={snapshot.snapshot_id} panes={len(snapshot.panes)} "
        f"available={snapshot.available_pane_count} unavailable="
        f"{snapshot.unavailable_pane_count} cursor=60000000"
    )
    return ReplayMicroscopeAuditCase(
        "all_eighteen_panes_share_one_cursor_policy_and_provenance",
        detail,
        {
            "available_pane_count": snapshot.available_pane_count,
            "capability_authority": capabilities.authority.value,
            "manifest_sha256": manifest_sha256,
            "pane_order": [item.value for item in expected_pane_order],
            "provenance_event_ids": sorted(provenance_ids),
            "query_id": result.query_id,
            "comparison_laundering_rejected": comparison_laundering_rejected,
            "feature_provenance_refusals": feature_provenance_refusals,
            "malformed_level2_rejected": malformed_level2_rejected,
            "snapshot_id": snapshot.snapshot_id,
            "unavailable_pane_count": snapshot.unavailable_pane_count,
            "unknown_signal_rejected": unknown_signal_rejected,
            "verified_ingress_panes": sorted(
                item.value for item in verified_ingress_panes
            ),
            "wrong_plane_signal_rejected": wrong_plane_signal_rejected,
        },
        tuple(failures),
    )


def _unsupported_pane_explanation_case() -> ReplayMicroscopeAuditCase:
    run_id, source_sha256 = _wo36c_source_identity()
    result = query_as_observed(
        ObservedEvidenceSet(run_id, source_sha256),
        ObservationQueryRequest(0),
    )
    snapshot = build_synchronized_panes(result)
    level_2 = snapshot.pane(PaneKind.LEVEL_2_LADDER)
    heatmap = snapshot.pane(PaneKind.DEPTH_HEATMAP)
    queue = snapshot.pane(PaneKind.INDIVIDUAL_QUEUE)
    local_capability = bind_pane_capabilities(
        result,
        supported_panes=(),
    )
    local_level_2 = build_synchronized_panes(
        result,
        capabilities=local_capability,
    ).pane(PaneKind.LEVEL_2_LADDER)
    manifest_bytes, manifest_sha256 = _wo36c_capability_manifest(
        result,
        source_class="HISTORICAL",
        supported_panes=(PaneKind.LEVEL_2_LADDER,),
        queue_capability=QueueCapability.UNAVAILABLE,
        queue_estimator_version=None,
    )
    recorded_empty = build_synchronized_panes(
        result,
        capabilities=load_verified_pane_capabilities(
            manifest_bytes,
            manifest_sha256,
        ),
    ).pane(PaneKind.LEVEL_2_LADDER)
    failures: list[str] = []
    expected = (
        (level_2, PaneUnavailableReason.LEVEL_2_NOT_RECORDED),
        (heatmap, PaneUnavailableReason.DEPTH_HISTORY_NOT_RECORDED),
        (queue, PaneUnavailableReason.QUEUE_CAPABILITY_UNAVAILABLE),
    )
    for pane, reason in expected:
        if (
            pane.availability is not PaneAvailability.UNAVAILABLE
            or pane.explanation is None
            or pane.explanation.reason is not reason
            or not pane.explanation.detail
        ):
            failures.append(f"{pane.pane_kind.value} lacks typed unavailability")
    if (
        recorded_empty.availability is not PaneAvailability.RECORDED_EMPTY
        or recorded_empty.explanation is None
        or recorded_empty.explanation.reason
        is not PaneUnavailableReason.NO_VISIBLE_EVENTS_AT_CURSOR
    ):
        failures.append("declared empty Level 2 was collapsed into unavailable/success")
    if (
        local_level_2.availability is not PaneAvailability.UNAVAILABLE
        or local_level_2.explanation is None
        or local_level_2.explanation.reason
        is not PaneUnavailableReason.LEVEL_2_NOT_RECORDED
    ):
        failures.append("local non-authorizing metadata asserted recorded support")
    if local_capability.authority is not PaneCapabilityAuthority.LOCAL_NONAUTHORIZING:
        failures.append("local capability hint unexpectedly gained authority")
    detail = (
        "level2=LEVEL_2_NOT_RECORDED queue=QUEUE_CAPABILITY_UNAVAILABLE "
        "declared_empty=RECORDED_EMPTY"
    )
    return ReplayMicroscopeAuditCase(
        "unsupported_depth_and_queue_explain_typed_unavailability",
        detail,
        {
            "depth_heatmap": heatmap.as_dict(),
            "level_2": level_2.as_dict(),
            "local_level_2": local_level_2.as_dict(),
            "queue": queue.as_dict(),
            "recorded_empty_level_2": recorded_empty.as_dict(),
        },
        tuple(failures),
    )


def _synthetic_queue_truth_case() -> ReplayMicroscopeAuditCase:
    observed = _wo36c_read_model_fixture()
    reveal = _wo36c_reveal_fixture(observed)
    requested = (RevealCapability.GROUND_TRUTH, RevealCapability.HIDDEN_STATE)
    request = ObservationQueryRequest(
        60_000_000,
        action_time_us=59_750_000,
        requested_reveal_capabilities=requested,
    )
    authorization = _reveal_authorization(reveal, requested, observed=observed)
    result = query_postmortem(
        observed,
        reveal,
        authorization,
        request,
    )
    supported = tuple(
        item for item in PANE_ORDER if item is not PaneKind.COUNTERFACTUAL_COMPARISON
    )
    manifest_bytes, manifest_sha256 = _wo36c_capability_manifest(
        result,
        source_class="SYNTHETIC",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
        reveal_source=reveal.source,
        reveal_authorization=authorization,
    )
    capabilities = load_verified_pane_capabilities(
        manifest_bytes,
        manifest_sha256,
        query_result=result,
        reveal_source=reveal.source,
        reveal_authorization=authorization,
    )
    snapshot = build_synchronized_panes(result, capabilities=capabilities)
    queue = snapshot.pane(PaneKind.INDIVIDUAL_QUEUE)
    estimate = queue.queue_estimates[0]
    agent = snapshot.pane(PaneKind.AGENT_ACTIVITY)

    wrong_queue_reveal = _wo36c_reveal_fixture(
        observed,
        queue_capability=RevealCapability.HIDDEN_STATE,
        include_agent=False,
    )
    wrong_queue_requested = (RevealCapability.HIDDEN_STATE,)
    wrong_queue_authorization = _reveal_authorization(
        wrong_queue_reveal,
        wrong_queue_requested,
        observed=observed,
    )
    wrong_queue_result = query_postmortem(
        observed,
        wrong_queue_reveal,
        wrong_queue_authorization,
        ObservationQueryRequest(
            60_000_000,
            action_time_us=59_750_000,
            requested_reveal_capabilities=wrong_queue_requested,
        ),
    )
    wrong_queue_raw, wrong_queue_pin = _wo36c_capability_manifest(
        wrong_queue_result,
        source_class="SYNTHETIC",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
        reveal_source=wrong_queue_reveal.source,
        reveal_authorization=wrong_queue_authorization,
    )
    wrong_queue_plane_rejected = False
    try:
        build_synchronized_panes(
            wrong_queue_result,
            capabilities=load_verified_pane_capabilities(
                wrong_queue_raw,
                wrong_queue_pin,
                query_result=wrong_queue_result,
                reveal_source=wrong_queue_reveal.source,
                reveal_authorization=wrong_queue_authorization,
            ),
        )
    except ValueError:
        wrong_queue_plane_rejected = True

    empty_observed = ObservedEvidenceSet(
        observed.source_run_id,
        observed.source_event_sha256,
    )
    wrong_agent_reveal = _wo36c_reveal_fixture(
        empty_observed,
        agent_capability=RevealCapability.GROUND_TRUTH,
        include_queue=False,
    )
    wrong_agent_requested = (RevealCapability.GROUND_TRUTH,)
    wrong_agent_result = query_postmortem(
        empty_observed,
        wrong_agent_reveal,
        _reveal_authorization(
            wrong_agent_reveal,
            wrong_agent_requested,
            observed=empty_observed,
        ),
        ObservationQueryRequest(
            60_000_000,
            requested_reveal_capabilities=wrong_agent_requested,
        ),
    )
    wrong_agent_plane_rejected = False
    try:
        build_synchronized_panes(wrong_agent_result)
    except ValueError:
        wrong_agent_plane_rejected = True

    local_rejected = False
    try:
        build_synchronized_panes(
            result,
            capabilities=bind_pane_capabilities(
                result,
                supported_panes=(),
            ),
        )
    except ValueError:
        local_rejected = True

    historical_raw, historical_pin = _wo36c_capability_manifest(
        result,
        source_class="HISTORICAL",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
    )
    historical_rejected = False
    try:
        build_synchronized_panes(
            result,
            capabilities=load_verified_pane_capabilities(
                historical_raw,
                historical_pin,
            ),
        )
    except ValueError:
        historical_rejected = True

    tampered = json.loads(manifest_bytes.decode("ascii"))
    tampered["source_class"] = "HISTORICAL"
    tampered["source_schema_id"] = HISTORICAL_PANE_SOURCE_SCHEMA_ID
    tamper_rejected = False
    try:
        load_verified_pane_capabilities(
            _audit_canonical_json_bytes(tampered),
            manifest_sha256,
        )
    except ValueError:
        tamper_rejected = True

    historical_reveal_source = replace(
        reveal.source,
        source_schema_id=HISTORICAL_PANE_SOURCE_SCHEMA_ID,
    )
    historical_reveal = RevealEvidenceSet(historical_reveal_source, reveal.values)
    historical_authorization = _reveal_authorization(
        historical_reveal,
        requested,
        observed=observed,
    )
    historical_result = query_postmortem(
        observed,
        historical_reveal,
        historical_authorization,
        request,
    )
    forged_raw, forged_pin = _wo36c_capability_manifest(
        historical_result,
        source_class="SYNTHETIC",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
        reveal_source=historical_reveal_source,
        reveal_authorization=historical_authorization,
    )
    reveal_relabel_rejected = False
    try:
        load_verified_pane_capabilities(
            forged_raw,
            forged_pin,
            query_result=historical_result,
            reveal_source=historical_reveal_source,
            reveal_authorization=historical_authorization,
        )
    except ValueError:
        reveal_relabel_rejected = True

    failures: list[str] = []
    if (
        queue.availability is not PaneAvailability.AVAILABLE
        or estimate.truth_availability
        is not QueueTruthAvailability.AUTHORIZED_SYNTHETIC_POSTMORTEM
        or estimate.truth_quantity_ahead != 60
        or estimate.uncertainty_lower_quantity != 50
        or estimate.uncertainty_upper_quantity != 90
    ):
        failures.append("authorized synthetic queue truth/uncertainty changed")
    if agent.availability is not PaneAvailability.AVAILABLE:
        failures.append("authorized postmortem agent activity remained unavailable")
    if not wrong_queue_plane_rejected:
        failures.append("queue truth accepted hidden-state evidence")
    if not wrong_agent_plane_rejected:
        failures.append("agent activity accepted ground-truth evidence")
    if not local_rejected:
        failures.append("local capability metadata unlocked protected queue evidence")
    if not historical_rejected:
        failures.append("historical source classification unlocked queue truth")
    if not tamper_rejected:
        failures.append("source-class manifest relabel survived its original pin")
    if not reveal_relabel_rejected:
        failures.append("historical reveal source was relabeled synthetic")
    if (
        capabilities.authority is not PaneCapabilityAuthority.PINNED_BACKEND_MANIFEST
        or capabilities.manifest_sha256 != manifest_sha256
    ):
        failures.append("synthetic authority lacks its exact retained manifest pin")

    detail = (
        f"query_id={result.query_id} truth="
        f"{estimate.truth_availability.value} manifest_sha256={manifest_sha256}"
    )
    return ReplayMicroscopeAuditCase(
        "queue_truth_requires_pinned_synthetic_postmortem_authority",
        detail,
        {
            "agent_activity": agent.as_dict(),
            "historical_rejected": historical_rejected,
            "local_rejected": local_rejected,
            "manifest_sha256": manifest_sha256,
            "queue_estimate": estimate.as_dict(),
            "reveal_relabel_rejected": reveal_relabel_rejected,
            "tamper_rejected": tamper_rejected,
            "wrong_agent_plane_rejected": wrong_agent_plane_rejected,
            "wrong_queue_plane_rejected": wrong_queue_plane_rejected,
        },
        tuple(failures),
    )


def _overlay_contract_case() -> ReplayMicroscopeAuditCase:
    expected_overlay_contracts = (
        (
            "SPREAD",
            "KIRBY2_MICROSCOPE_SPREAD_OVERLAY_V1",
            1,
            "TICKS",
            "INSTANTANEOUS_AT_CURSOR",
            0,
        ),
        (
            "MICROPRICE",
            "KIRBY2_MICROSCOPE_MICROPRICE_OVERLAY_V1",
            1,
            "MICROTICKS",
            "INSTANTANEOUS_AT_CURSOR",
            0,
        ),
        (
            "IMBALANCE",
            "KIRBY2_MICROSCOPE_IMBALANCE_OVERLAY_V1",
            1,
            "SIGNED_RATIO_PPM",
            "INSTANTANEOUS_AT_CURSOR",
            0,
        ),
        (
            "TRADE_VELOCITY",
            "KIRBY2_MICROSCOPE_TRADE_VELOCITY_OVERLAY_V1",
            1,
            "MICROTRADES_PER_SECOND",
            "TRAILING_CLOSED_INTERVAL",
            1_000_000,
        ),
        (
            "CANCELLATION_VELOCITY",
            "KIRBY2_MICROSCOPE_CANCELLATION_VELOCITY_OVERLAY_V1",
            1,
            "MICROSHARES_PER_SECOND",
            "TRAILING_CLOSED_INTERVAL",
            1_000_000,
        ),
        (
            "REPLENISHMENT",
            "KIRBY2_MICROSCOPE_REPLENISHMENT_OVERLAY_V1",
            1,
            "MICROSHARES_PER_SECOND",
            "TRAILING_CLOSED_INTERVAL",
            1_000_000,
        ),
        (
            "RELATIVE_VOLUME",
            "KIRBY2_MICROSCOPE_RELATIVE_VOLUME_OVERLAY_V1",
            1,
            "RATIO_PPM",
            "TRAILING_CLOSED_INTERVAL",
            60_000_000,
        ),
        (
            "SHORT_TERM_VOLATILITY",
            "KIRBY2_MICROSCOPE_SHORT_TERM_VOLATILITY_OVERLAY_V1",
            1,
            "MICROBASIS_POINTS",
            "TRAILING_CLOSED_INTERVAL",
            5_000_000,
        ),
        (
            "IMPLEMENTATION_SHORTFALL",
            "KIRBY2_MICROSCOPE_IMPLEMENTATION_SHORTFALL_OVERLAY_V1",
            1,
            "X2_TICK_SHARES",
            "SESSION_START_TO_CURSOR",
            None,
        ),
    )
    observed = _wo36c_read_model_fixture()
    terminal_request = ObservationQueryRequest(
        60_000_000,
        action_time_us=59_750_000,
    )
    terminal = query_as_observed(observed, terminal_request)
    event_times = (
        1_000_000,
        10_000_000,
        56_000_000,
        59_200_000,
        59_400_000,
        59_500_000,
        59_600_000,
        59_700_000,
        59_760_000,
        59_800_000,
        59_900_000,
    )
    event_queries = tuple(
        query_as_observed(observed, ObservationQueryRequest(cursor))
        for cursor in event_times
    )
    projection, receipt = build_overlay_window_projection(
        terminal,
        event_queries,
    )
    selection = OverlayInputSelection(
        top_of_book_event_ids=(
            "top-book-event-early",
            "top-book-event-late",
        ),
        trade_event_ids=("trade-event-0001", "trade-event-0002"),
        cancellation_event_ids=("cancel-event-0001",),
        replenishment_event_ids=("replenishment-event-0001",),
        relative_volume_baseline_event_id=(
            "relative-volume-baseline-event-0001"
        ),
        execution_arrival_event_id="execution-arrival-event-0001",
        execution_fill_event_ids=(
            "execution-fill-event-0001",
            "execution-fill-event-0002",
        ),
    )
    overlay_set = build_overlay_set(terminal, projection, selection)

    reversed_observed = _wo36c_read_model_fixture(reverse=True)
    reversed_terminal = query_as_observed(reversed_observed, terminal_request)
    reversed_queries = tuple(
        query_as_observed(reversed_observed, ObservationQueryRequest(cursor))
        for cursor in reversed(event_times)
    )
    reversed_projection, reversed_receipt = build_overlay_window_projection(
        reversed_terminal,
        reversed_queries,
    )
    repeated = build_overlay_set(
        reversed_terminal,
        reversed_projection,
        selection,
    )

    laundered = build_overlay_set(
        terminal,
        projection,
        OverlayInputSelection(
            top_of_book_event_ids=("strategy-rule-event-0001",),
        ),
    )

    early_terminal = query_as_observed(
        observed,
        ObservationQueryRequest(500_000),
    )
    early_event_query = query_as_observed(
        observed,
        ObservationQueryRequest(250_000),
    )
    early_projection, _ = build_overlay_window_projection(
        early_terminal,
        (early_event_query,),
    )
    early_rate = build_overlay_set(
        early_terminal,
        early_projection,
        OverlayInputSelection(trade_event_ids=("trade-event-early",)),
    ).trade_velocity

    queried_event_ids = {
        item.event_id
        for query in (terminal, *event_queries)
        for item in query.values
    }
    failures: list[str] = []
    actual_overlay_contracts = tuple(
        (
            item.kind.value,
            item.schema_id,
            item.schema_version,
            item.unit.value,
            item.window.basis.value,
            item.window.lookback_us,
        )
        for item in overlay_set.overlays
    )
    if (
        tuple(item.value for item in OVERLAY_KIND_ORDER)
        != tuple(item[0] for item in expected_overlay_contracts)
        or actual_overlay_contracts != expected_overlay_contracts
    ):
        failures.append("overlay inventory/order changed")
    if len(overlay_set.overlays) != 9 or any(
        item.availability is not OverlayAvailability.AVAILABLE
        for item in overlay_set.overlays
    ):
        failures.append("complete fixture did not produce all nine overlays")
    expected_values = {
        "SPREAD": 2,
        "MICROPRICE": 102_500_000,
        "IMBALANCE": 500_000,
        "TRADE_VELOCITY": 2_000_000,
        "CANCELLATION_VELOCITY": 7_000_000,
        "REPLENISHMENT": 9_000_000,
        "RELATIVE_VOLUME": 500_000,
        "IMPLEMENTATION_SHORTFALL": 40,
    }
    for item in overlay_set.overlays:
        if item.kind.value in expected_values and item.value != expected_values[item.kind.value]:
            failures.append(f"{item.kind.value} exact calculation changed")
        if type(item.value) is not int:
            failures.append(f"{item.kind.value} is not exact integer output")
        expected_spec = OVERLAY_SPECIFICATIONS[OVERLAY_KIND_ORDER.index(item.kind)]
        if (
            item.schema_id != expected_spec.schema_id
            or item.schema_version != expected_spec.schema_version
            or item.unit is not expected_spec.unit
            or item.window.basis is not expected_spec.window_basis
            or item.window.lookback_us != expected_spec.lookback_us
            or item.calculation.as_dict() != expected_spec.calculation.as_dict()
        ):
            failures.append(f"{item.kind.value} version/window/unit contract changed")
        if not item.source_events or any(
            source.event_id not in queried_event_ids
            or source.policy_visible_at_time_us > terminal.request.render_cursor_time_us
            or not source.query_ids
            for source in item.source_events
        ):
            failures.append(f"{item.kind.value} lacks exact query provenance")
    if len(overlay_set.short_term_volatility.source_events) != 2:
        failures.append("repeated same-series quote history was lost")
    if len(overlay_set.trade_velocity.source_events) != 2:
        failures.append("repeated same-series trade history was lost")
    if (
        laundered.spread.availability is not OverlayAvailability.UNAVAILABLE
        or laundered.spread.unavailable_reason
        is not OverlayUnavailableReason.SEMANTIC_ROLE_MISMATCH
    ):
        failures.append("strategy evidence was laundered into a quote overlay")
    if (
        early_rate.availability is not OverlayAvailability.AVAILABLE
        or early_rate.window.start_time_us != 0
        or early_rate.window.end_time_us != 500_000
        or early_rate.window.duration_us != 500_000
        or early_rate.value != 2_000_000
    ):
        failures.append("early-session rate denominator differs from its window")
    if (
        repeated.canonical_bytes() != overlay_set.canonical_bytes()
        or reversed_receipt.canonical_bytes() != receipt.canonical_bytes()
        or reversed_projection.canonical_bytes() != projection.canonical_bytes()
    ):
        failures.append("overlay projection/output depends on input ordering")
    projection_payload = projection.as_dict()
    if {
        "event_count",
        "event_inventory_sha256",
        "query_count",
        "query_inventory_sha256",
    } & set(projection_payload):
        failures.append("cursor-safe overlay projection leaks backend inventory facts")

    detail = (
        f"overlay_set_id={overlay_set.overlay_set_id} overlays=9 "
        f"projection_id={projection.projection_id} projected_events="
        f"{receipt.event_count}"
    )
    return ReplayMicroscopeAuditCase(
        "nine_overlays_preserve_versions_windows_units_and_sources",
        detail,
        {
            "early_trade_velocity": early_rate.as_dict(),
            "overlay_set_id": overlay_set.overlay_set_id,
            "projection": projection_payload,
            "projection_receipt_sha256": receipt.receipt_sha256,
            "semantic_laundering_reason": (
                None
                if laundered.spread.unavailable_reason is None
                else laundered.spread.unavailable_reason.value
            ),
            "values": {
                item.kind.value: item.value for item in overlay_set.overlays
            },
        },
        tuple(failures),
    )


def _read_model_cross_binding_case() -> ReplayMicroscopeAuditCase:
    observed = _wo36c_read_model_fixture()
    full_result = query_as_observed(
        observed,
        ObservationQueryRequest(60_000_000, action_time_us=59_750_000),
    )
    earlier_result = query_as_observed(
        observed,
        ObservationQueryRequest(59_499_999),
    )
    early_top_query = query_as_observed(
        observed,
        ObservationQueryRequest(56_000_000),
    )
    early_projection, _ = build_overlay_window_projection(
        earlier_result,
        (early_top_query,),
    )
    future_selection_rejected = False
    try:
        build_overlay_set(
            earlier_result,
            early_projection,
            OverlayInputSelection(
                top_of_book_event_ids=("top-book-event-late",),
            ),
        )
    except ValueError:
        future_selection_rejected = True

    full_projection, _ = build_overlay_window_projection(
        full_result,
        (
            query_as_observed(observed, ObservationQueryRequest(59_500_000)),
        ),
    )
    alternate_terminal = query_as_observed(
        observed,
        ObservationQueryRequest(60_000_000),
    )
    projection_cross_query_rejected = False
    try:
        build_overlay_set(
            alternate_terminal,
            full_projection,
            OverlayInputSelection(
                top_of_book_event_ids=("top-book-event-late",),
            ),
        )
    except ValueError:
        projection_cross_query_rejected = True

    supported = tuple(
        item
        for item in PANE_ORDER
        if item not in {PaneKind.AGENT_ACTIVITY, PaneKind.COUNTERFACTUAL_COMPARISON}
    )
    capability_raw, capability_pin = _wo36c_capability_manifest(
        full_result,
        source_class="HISTORICAL",
        supported_panes=supported,
        queue_capability=QueueCapability.ESTIMATED,
        queue_estimator_version="queue-estimator.v1",
    )
    full_capabilities = load_verified_pane_capabilities(
        capability_raw,
        capability_pin,
    )
    pane_cross_query_rejected = False
    try:
        build_synchronized_panes(
            earlier_result,
            capabilities=full_capabilities,
        )
    except ValueError:
        pane_cross_query_rejected = True

    held_event_rejected = False
    try:
        timeline_event_from_query_result(full_result, "top-book-event-late")
    except ValueError:
        held_event_rejected = True
    semantic_relabel_rejected = False
    try:
        quote_query = query_as_observed(
            observed,
            ObservationQueryRequest(59_820_000),
        )
        timeline_event_from_query_result(
            quote_query,
            "consolidated-quote-pane-event-0001",
            TimelineEventKind.FILL,
        )
    except ValueError:
        semantic_relabel_rejected = True

    direct_timeline_event_rejected = False
    try:
        TimelineEvidenceEvent(
            full_result.source_run_id,
            full_result.source_event_sha256,
            ObservationMode.AS_OBSERVED,
            AS_OBSERVED_POLICY_ID,
            "fabricated-event-0001",
            TimelineEventKind.OBSERVED_UPDATE,
            1,
            0,
            0,
            TimelineEvidenceSource.CLIENT_DELIVERED,
            ("fabricated-event-0001",),
        )
    except TypeError:
        direct_timeline_event_rejected = True

    timeline_source = _wo36c_timeline_fixture()
    player_query = query_as_observed(
        timeline_source,
        ObservationQueryRequest(100, 100),
    )
    fill_query = query_as_observed(
        timeline_source,
        ObservationQueryRequest(200, 200),
    )
    player_event = timeline_event_from_query_result(
        player_query,
        "player-action-event-0001",
    )
    fill_event = timeline_event_from_query_result(
        fill_query,
        "fill-event-0001",
    )
    player_timeline, player_receipt = build_replay_timeline(
        player_query,
        (player_event,),
    )
    fill_timeline, fill_receipt = build_replay_timeline(
        fill_query,
        (fill_event,),
    )
    timeline_cross_inventory_rejected = False
    try:
        fill_timeline.play(player_timeline.cursor(100))
    except ValueError:
        timeline_cross_inventory_rejected = True

    run_id, source_sha256 = _wo36c_source_identity()
    laundered_agent = ObservedEvidenceSet(
        run_id,
        source_sha256,
        client_delivered=(
            ObservedValueRecord(
                "agent.fabricated",
                "agent-fabricated-event-0001",
                1,
                _delivered_timing(10, 20, 30),
                {"activity": "fabricated-observed-agent"},
            ),
        ),
    )
    observed_agent_rejected = False
    try:
        build_synchronized_panes(
            query_as_observed(laundered_agent, ObservationQueryRequest(30)),
        )
    except ValueError:
        observed_agent_rejected = True

    attack_sources = (
        "from kirby2.microscope.timeline import "
        "TimelineEvidenceEvent, ReplayTimeline, build_replay_timeline\n",
        "from kirby2.microscope.panes import "
        "PaneCapabilityRead, load_verified_pane_capabilities\n",
        "from kirby2.microscope.overlays import "
        "OverlayInputSelection, OverlayWindowProjection, "
        "build_overlay_window_projection, build_overlay_set\n",
        "import importlib\n"
        "backend = importlib.import_module('kirby2.microscope.timeline')\n"
        "event_type = backend.TimelineEvidenceEvent\n",
        "backend = __import__('kirby2.microscope.panes', "
        "fromlist=('PaneCapabilityRead',))\n",
    )
    attack_findings = tuple(
        finding
        for index, source in enumerate(attack_sources)
        for finding in _raw_evidence_imports_in_source(
            source,
            f"wo36c-attack-{index}.py",
        )
    )
    safe_findings = _raw_evidence_imports_in_source(
        "from kirby2.microscope.timeline import TimelineCursor\n"
        "from kirby2.microscope.panes import SynchronizedPaneSnapshot\n"
        "from kirby2.microscope.overlays import OverlaySet\n",
        "wo36c-safe-output.py",
    )
    live_ui_findings = _ui_raw_evidence_imports()

    failures: list[str] = []
    expected_refusals = {
        "future overlay selection": future_selection_rejected,
        "projection/query cross-binding": projection_cross_query_rejected,
        "pane/query cross-binding": pane_cross_query_rejected,
        "held timeline event": held_event_rejected,
        "timeline semantic relabel": semantic_relabel_rejected,
        "direct timeline event": direct_timeline_event_rejected,
        "timeline inventory substitution": timeline_cross_inventory_rejected,
        "observed agent laundering": observed_agent_rejected,
    }
    for label, rejected in expected_refusals.items():
        if not rejected:
            failures.append(f"{label} did not fail closed")
    if player_timeline.timeline_id != fill_timeline.timeline_id:
        failures.append("public timeline identity leaks a full-inventory comparison oracle")
    if (
        player_receipt.event_inventory_sha256
        == fill_receipt.event_inventory_sha256
        or player_receipt.receipt_sha256 == fill_receipt.receipt_sha256
    ):
        failures.append("backend timeline receipt does not bind the exact inventory")
    if len(attack_findings) != len(attack_sources):
        failures.append("UI backend-input import attack inventory was not blocked")
    if safe_findings:
        failures.append("UI output-only read-model imports were blocked")
    if live_ui_findings:
        failures.append("live UI imports replay backend input authority")

    detail = (
        f"refusals={sum(expected_refusals.values())}/{len(expected_refusals)} "
        f"ui_backend_attacks={len(attack_findings)} live_ui_findings="
        f"{len(live_ui_findings)}"
    )
    return ReplayMicroscopeAuditCase(
        "read_models_reject_cross_query_future_and_ui_backend_inputs",
        detail,
        {
            "attack_findings": list(attack_findings),
            "live_ui_findings": live_ui_findings,
            "refusals": expected_refusals,
            "safe_findings": safe_findings,
            "timeline_ids": [
                player_timeline.timeline_id,
                fill_timeline.timeline_id,
            ],
        },
        tuple(failures),
    )


def _wo36c_timeline_fixture(*, reverse: bool = False) -> ObservedEvidenceSet:
    run_id, source_sha256 = _wo36c_source_identity()
    delivered = (
        ObservedValueRecord(
            "fill.player-order-1",
            "fill-event-0001",
            2,
            _delivered_timing(180, 190, 200),
            {"filled_quantity": 10},
        ),
        ObservedValueRecord(
            "warning.invariant.position",
            "invariant-warning-event-0001",
            4,
            _delivered_timing(350, 360, 400),
            {"warning_code": "position-conservation"},
        ),
        ObservedValueRecord(
            "quote.consolidated.primary",
            "observed-update-event-0001",
            5,
            _delivered_timing(351, 361, 400),
            {
                "best_ask_ticks": 102,
                "best_bid_ticks": 100,
                "record_kind": "OBSERVED_UPDATE",
            },
        ),
    )
    decisions = (
        ObservedValueRecord(
            "order.client-intention",
            "player-action-event-0001",
            1,
            EvidenceTiming(
                source_event_time_us=80,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(100),
            ),
            {"side": "BUY", "venue_state": "NOT_OBSERVED"},
        ),
        ObservedValueRecord(
            "strategy.signal",
            "traffic-transition-event-0001",
            3,
            EvidenceTiming(
                source_event_time_us=170,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(200),
            ),
            {"recorded_signal": "GREEN"},
        ),
    )
    return ObservedEvidenceSet(
        run_id,
        source_sha256,
        client_delivered=(tuple(reversed(delivered)) if reverse else delivered),
        decision_snapshots=(tuple(reversed(decisions)) if reverse else decisions),
    )


def _wo36c_read_model_fixture(*, reverse: bool = False) -> ObservedEvidenceSet:
    run_id, source_sha256 = _wo36c_source_identity()
    rows = (
        (
            "market.top-of-book.primary",
            "top-book-event-early",
            56_000_000,
            {
                "best_ask_size": 20,
                "best_ask_ticks": 102,
                "best_bid_size": 20,
                "best_bid_ticks": 100,
                "record_role": "TOP_OF_BOOK",
            },
        ),
        (
            "market.top-of-book.primary",
            "top-book-event-late",
            59_500_000,
            {
                "best_ask_size": 10,
                "best_ask_ticks": 103,
                "best_bid_size": 30,
                "best_bid_ticks": 101,
                "record_role": "TOP_OF_BOOK",
            },
        ),
        (
            "market.trade.primary",
            "trade-event-early",
            250_000,
            {
                "price_ticks": 100,
                "quantity": 10,
                "record_role": "TRADE",
            },
        ),
        (
            "market.trade.primary",
            "trade-event-0001",
            59_200_000,
            {
                "price_ticks": 101,
                "quantity": 20,
                "record_role": "TRADE",
            },
        ),
        (
            "market.trade.primary",
            "trade-event-0002",
            59_800_000,
            {
                "price_ticks": 102,
                "quantity": 30,
                "record_role": "TRADE",
            },
        ),
        (
            "market.cancellation.primary",
            "cancel-event-0001",
            59_400_000,
            {"cancelled_quantity": 7, "record_role": "CANCELLATION"},
        ),
        (
            "market.replenishment.primary",
            "replenishment-event-0001",
            59_600_000,
            {"added_quantity": 9, "record_role": "REPLENISHMENT"},
        ),
        (
            "execution.fill.exec-1",
            "execution-fill-event-0001",
            59_700_000,
            {
                "execution_id": "exec-1",
                "correlation_id": "corr-1",
                "order_id": "player-order-1",
                "price_x2": 204,
                "quantity": 10,
                "record_role": "EXECUTION_FILL",
                "side": "BUY",
            },
        ),
        (
            "execution.fill.exec-1",
            "execution-fill-event-0002",
            59_900_000,
            {
                "execution_id": "exec-1",
                "correlation_id": "corr-1",
                "order_id": "player-order-1",
                "price_x2": 206,
                "quantity": 5,
                "record_role": "EXECUTION_FILL",
                "side": "BUY",
            },
        ),
        (
            "trade.pane.primary",
            "time-and-sales-pane-event-0001",
            59_810_000,
            {"price_ticks": 102, "quantity": 30},
        ),
        (
            "quote.consolidated.primary",
            "consolidated-quote-pane-event-0001",
            59_820_000,
            {"best_ask_ticks": 103, "best_bid_ticks": 101},
        ),
        (
            "fill.player-order-1",
            "fill-pane-event-0001",
            59_830_000,
            {"filled_quantity": 15},
        ),
        (
            "book.level2.primary",
            "level2-event-0001",
            59_300_000,
            {
                "asks": [[103, 10], [104, 20]],
                "bids": [[101, 30], [100, 40]],
                "record_kind": "LEVEL_2",
            },
        ),
        (
            "depth.heatmap.primary",
            "depth-heatmap-event-0001",
            59_310_000,
            {"record_kind": "DEPTH_HEATMAP", "rows": [[101, 30], [103, 10]]},
        ),
        (
            "order.player-order-1",
            "player-order-event-0001",
            59_320_000,
            {"order_id": "player-order-1", "state": "WORKING"},
        ),
        (
            "position.primary",
            "position-event-0001",
            59_330_000,
            {"quantity": 15},
        ),
        (
            "quote.venue.xnas",
            "venue-quote-event-0001",
            59_340_000,
            {"ask_ticks": 103, "bid_ticks": 101, "venue": "XNAS"},
        ),
        (
            "metrics.execution.exec-1",
            "execution-metrics-event-0001",
            59_350_000,
            {"filled_quantity": 15, "implementation_shortfall_x2": 40},
        ),
        (
            "trace.player-order-1",
            "trace-event-0001",
            59_360_000,
            {"trace_id": "trace-player-order-1"},
        ),
        (
            "queue.estimate.player-order-1",
            "queue-estimate-event-0001",
            59_370_000,
            {
                "estimated_quantity_ahead": 70,
                "uncertainty_lower_quantity": 50,
                "uncertainty_upper_quantity": 90,
            },
        ),
    )
    delivered = tuple(
        ObservedValueRecord(
            series_id,
            event_id,
            sequence,
            _delivered_timing(visible_at - 20, visible_at - 10, visible_at),
            payload,
        )
        for sequence, (series_id, event_id, visible_at, payload) in enumerate(
            rows,
            start=1,
        )
    )
    decisions = (
        ObservedValueRecord(
            "market.relative-volume-baseline.trailing-60s",
            "relative-volume-baseline-event-0001",
            28,
            EvidenceTiming(
                source_event_time_us=990_000,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(1_000_000),
            ),
            {
                "expected_volume": 100,
                "record_role": "RELATIVE_VOLUME_BASELINE",
                "window_duration_us": 60_000_000,
            },
        ),
        ObservedValueRecord(
            "execution.arrival.exec-1",
            "execution-arrival-event-0001",
            29,
            EvidenceTiming(
                source_event_time_us=9_990_000,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(10_000_000),
            ),
            {
                "arrival_midpoint_x2": 202,
                "correlation_id": "corr-1",
                "execution_id": "exec-1",
                "order_id": "player-order-1",
                "record_role": "EXECUTION_ARRIVAL",
                "side": "BUY",
            },
        ),
        ObservedValueRecord(
            "traffic-light.primary",
            "traffic-light-event-0001",
            30,
            EvidenceTiming(
                source_event_time_us=59_740_000,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(59_750_000),
            ),
            {"record_kind": "TRAFFIC_LIGHT_TRANSITION", "state": "GREEN"},
        ),
        ObservedValueRecord(
            "strategy.rule.primary",
            "strategy-rule-event-0001",
            31,
            EvidenceTiming(
                source_event_time_us=59_745_000,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(59_760_000),
            ),
            {"recorded_rule_id": "join-bid-if-green", "result": True},
        ),
        ObservedValueRecord(
            "feature.imbalance.primary",
            "feature-event-0001",
            32,
            EvidenceTiming(
                source_event_time_us=59_746_000,
                venue_receipt=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.CLIENT_DECISION
                ),
                client_receive=EvidenceTimestamp.not_applicable(
                    TimestampAbsenceReason.RECORDED_SNAPSHOT
                ),
                client_knowledge=EvidenceTimestamp.recorded(59_770_000),
            ),
            {
                "provenance_event_ids": ["top-book-event-late"],
                "value_ppm": 500_000,
            },
        ),
    )
    return ObservedEvidenceSet(
        run_id,
        source_sha256,
        client_delivered=(tuple(reversed(delivered)) if reverse else delivered),
        decision_snapshots=(tuple(reversed(decisions)) if reverse else decisions),
    )


def _wo36c_reveal_fixture(
    observed: ObservedEvidenceSet,
    *,
    queue_capability: RevealCapability = RevealCapability.GROUND_TRUTH,
    agent_capability: RevealCapability = RevealCapability.HIDDEN_STATE,
    include_queue: bool = True,
    include_agent: bool = True,
) -> RevealEvidenceSet:
    def timing(source_time_us: int) -> EvidenceTiming:
        return EvidenceTiming(
            source_event_time_us=source_time_us,
            venue_receipt=EvidenceTimestamp.recorded(source_time_us),
            client_receive=EvidenceTimestamp.unavailable(
                TimestampAbsenceReason.NEVER_CLIENT_DELIVERED
            ),
            client_knowledge=EvidenceTimestamp.unavailable(
                TimestampAbsenceReason.NEVER_CLIENT_KNOWN_DURING_RUN
            ),
        )

    values_list: list[RevealValueRecord] = []
    if include_queue:
        values_list.append(RevealValueRecord(
            "queue.truth.player-order-1",
            "queue-truth-event-0001",
            100,
            timing(59_850_000),
            queue_capability,
            {"truth_quantity_ahead": 60},
        ))
    if include_agent:
        values_list.append(RevealValueRecord(
            "agent.primary",
            "agent-activity-event-0001",
            101,
            timing(59_860_000),
            agent_capability,
            {"activity": "CANCEL", "agent_id": "synthetic-agent-1"},
        ))
    values = tuple(values_list)
    capability_evidence_list: list[SourceCapabilityEvidence] = []
    for capability in RevealCapability:
        capability_values = tuple(
            item for item in values if item.required_capability is capability
        )
        if capability_values:
            capability_evidence_list.append(
                SourceCapabilityEvidence(
                    capability,
                    SourceCapabilityAvailability.AVAILABLE,
                    source_artifact_id=(
                        "wo36c.synthetic."
                        + capability.value.lower().replace("_", "-")
                        + ".v1"
                    ),
                    source_artifact_sha256=reveal_artifact_sha256(
                        capability_values
                    ),
                )
            )
        else:
            capability_evidence_list.append(
                SourceCapabilityEvidence(
                    capability,
                    SourceCapabilityAvailability.UNAVAILABLE,
                    unavailable_reason=(
                        SourceCapabilityUnavailableReason.NOT_RECORDED_BY_SOURCE
                    ),
                )
            )
    capability_evidence = tuple(capability_evidence_list)
    source = ReplaySourceCapabilityManifest(
        observed.source_run_id,
        observed.source_event_sha256,
        SYNTHETIC_PANE_SOURCE_SCHEMA_ID,
        PANE_SOURCE_SCHEMA_VERSION,
        capability_evidence,
    )
    return RevealEvidenceSet(source, values)


def _wo36c_regime_reveal_fixture(
    observed: ObservedEvidenceSet,
    *,
    capability: RevealCapability = RevealCapability.GROUND_TRUTH,
) -> RevealEvidenceSet:
    value = RevealValueRecord(
        "regime.transition",
        "regime-transition-event-0001",
        100,
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
        {"from_regime": "QUIET", "to_regime": "BUSY"},
    )
    value_tuple = (value,)
    capability_evidence = tuple(
        SourceCapabilityEvidence(
            item,
            (
                SourceCapabilityAvailability.AVAILABLE
                if item is capability
                else SourceCapabilityAvailability.UNAVAILABLE
            ),
            source_artifact_id=(
                "wo36c.regime."
                + item.value.lower().replace("_", "-")
                + ".v1"
                if item is capability
                else None
            ),
            source_artifact_sha256=(
                reveal_artifact_sha256(value_tuple)
                if item is capability
                else None
            ),
            unavailable_reason=(
                None
                if item is capability
                else SourceCapabilityUnavailableReason.NOT_RECORDED_BY_SOURCE
            ),
        )
        for item in RevealCapability
    )
    source = ReplaySourceCapabilityManifest(
        observed.source_run_id,
        observed.source_event_sha256,
        "KIRBY2_WO36C_REGIME_REVEAL_SOURCE_V1",
        1,
        capability_evidence,
    )
    return RevealEvidenceSet(source, value_tuple)


def _wo36c_capability_manifest(
    result: ObservationQueryResult,
    *,
    source_class: str,
    supported_panes: tuple[PaneKind, ...],
    queue_capability: QueueCapability,
    queue_estimator_version: str | None,
    reveal_source: ReplaySourceCapabilityManifest | None = None,
    reveal_authorization: RevealAuthorization | None = None,
) -> tuple[bytes, str]:
    source_schema_id = {
        "HISTORICAL": HISTORICAL_PANE_SOURCE_SCHEMA_ID,
        "SYNTHETIC": SYNTHETIC_PANE_SOURCE_SCHEMA_ID,
    }[source_class]
    if source_class == "SYNTHETIC":
        if reveal_source is None or reveal_authorization is None:
            raise ValueError("synthetic pane fixture requires exact reveal authority")
        reveal_authorization_id = reveal_authorization.authorization_id
        reveal_authorization_sha256 = _audit_sha256(
            reveal_authorization.as_dict()
        )
        reveal_evidence_sha256 = result.reveal_evidence_sha256
        reveal_source_manifest_sha256 = reveal_source.manifest_sha256
    else:
        if reveal_source is not None or reveal_authorization is not None:
            raise ValueError("historical pane fixture cannot bind reveal authority")
        reveal_authorization_id = None
        reveal_authorization_sha256 = None
        reveal_evidence_sha256 = None
        reveal_source_manifest_sha256 = None
    payload = {
        "capability_scope": PANE_CAPABILITY_MANIFEST_SCOPE,
        "observation_mode": result.policy.mode.value,
        "observed_projection_sha256": result.observed_projection_sha256,
        "policy_id": result.policy.policy_id,
        "query_id": result.query_id,
        "queue_capability": queue_capability.value,
        "queue_estimator_version": queue_estimator_version,
        "render_cursor_time_us": result.request.render_cursor_time_us,
        "reveal_authorization_id": reveal_authorization_id,
        "reveal_authorization_sha256": reveal_authorization_sha256,
        "reveal_evidence_sha256": reveal_evidence_sha256,
        "reveal_source_capability_manifest_sha256": (
            reveal_source_manifest_sha256
        ),
        "schema_id": PANE_CAPABILITY_SCHEMA_ID,
        "schema_version": PANE_CAPABILITY_SCHEMA_VERSION,
        "source_class": source_class,
        "source_event_sha256": result.source_event_sha256,
        "source_run_id": result.source_run_id,
        "source_schema_id": source_schema_id,
        "source_schema_version": PANE_SOURCE_SCHEMA_VERSION,
        "supported_panes": [item.value for item in supported_panes],
    }
    raw = _audit_canonical_json_bytes(payload)
    return raw, hashlib.sha256(raw).hexdigest()


def _wo36c_source_identity() -> tuple[str, str]:
    digest = hashlib.sha256(b"wo36-c-synchronized-read-model-source-v1").hexdigest()
    return "run-" + digest[:24], digest


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
        "kirby2.microscope.overlays",
        "kirby2.microscope.panes",
        "kirby2.microscope.policy",
        "kirby2.microscope.query",
        "kirby2.microscope.timeline",
    }
    relative_sensitive_modules = {
        "microscope.ingestion",
        "microscope.overlays",
        "microscope.panes",
        "microscope.policy",
        "microscope.query",
        "microscope.timeline",
    }
    ui_safe_imports = {
        "kirby2.microscope.ingestion": frozenset(),
        "kirby2.microscope.overlays": frozenset(
            {
                "CancellationVelocityOverlay",
                "ImplementationShortfallOverlay",
                "ImbalanceOverlay",
                "MicropriceOverlay",
                "OverlayAvailability",
                "OverlayKind",
                "OverlaySet",
                "OverlaySourceEvent",
                "OverlayUnavailableReason",
                "OverlayUnit",
                "OverlayWindow",
                "OverlayWindowBasis",
                "RelativeVolumeOverlay",
                "ReplenishmentOverlay",
                "ShortTermVolatilityOverlay",
                "SpreadOverlay",
                "TradeVelocityOverlay",
            }
        ),
        "kirby2.microscope.panes": frozenset(
            {
                "CalculationKind",
                "DeclaredCalculation",
                "PaneAvailability",
                "PaneDatum",
                "PaneExplanation",
                "PaneKind",
                "PaneSourceEvent",
                "PaneUnavailableReason",
                "QueueEstimate",
                "QueueTruthAvailability",
                "ReplayPane",
                "SynchronizedPaneSnapshot",
            }
        ),
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
        "kirby2.microscope.timeline": frozenset(
            {
                "TimelineCursor",
                "TimelineDirection",
                "TimelineEventKind",
                "TimelineEventLink",
                "TimelineJumpTarget",
                "TimelineNavigationAvailability",
                "TimelineNavigationKind",
                "TimelineNavigationResult",
                "TimelineNavigationUnavailableReason",
                "TimelinePlaybackState",
                "TimelineSidecarOperation",
                "TimelineSidecarRefusal",
                "TimelineSidecarRefusalReason",
                "TimelineSidecarStatus",
            }
        ),
    }
    findings: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            dynamic_import = (
                isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
            if (
                dynamic_import
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                module_name = node.args[0].value
                if module_name == "kirby2" or module_name.startswith("kirby2."):
                    findings.append(
                        f"{filename}:{node.lineno}:dynamic import {module_name}"
                    )
            continue
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
