"""CLI demo and deterministic acceptance fixture for the offline microscope."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec

from .annotations import (
    ReplayAnnotationKind,
    ReplayAnnotationV1,
    ReplayBookmarkV1,
    ReplaySidecarTargetV1,
    TimingLieReviewPacketV1,
    TimingLieReviewResultV1,
    TimingLieRubricSearch,
    bind_replay_sidecar_target,
    build_timing_lie_review_packet,
    create_replay_annotation,
    create_replay_bookmark,
    resolve_timing_lie_review,
)
from .comparison import (
    BranchComparisonV1,
    ComparisonAvailability,
    ComparisonEventInput,
    ComparisonEvidenceScope,
    ComparisonOverlayInput,
    ComparisonOverlayKind,
    ComparisonRecordInput,
    ComparisonRunInput,
    ComparisonSeriesInput,
    ComparisonSeriesKind,
    CounterfactualBranchInput,
    CounterfactualRngPolicy,
    build_branch_comparison,
)
from kirby2.counterfactual.models import CounterfactualMode
from .data_age import EvidenceTimestamp, EvidenceTiming, TimestampAbsenceReason
from .index import build_trace_index
from .models import (
    EXPECTED_ARTIFACT_KIND,
    TRACE_STAGE_ORDER,
    MechanisticTraceIndex,
    RecordedTraceEvent,
    TraceProvenance,
    TraceSourceRecording,
    TraceStage,
)
from .overlays import (
    OverlayInputSelection,
    build_overlay_set,
    build_overlay_window_projection,
)
from .panes import (
    PANE_ORDER,
    PaneKind,
    attach_counterfactual_comparison,
    build_synchronized_panes,
)
from .policy import (
    ObservationMode,
    ReplaySourceCapabilityManifest,
    RevealAuthorization,
    RevealCapability,
    SourceCapabilityAvailability,
    SourceCapabilityEvidence,
)
from .query import (
    ObservationQueryRequest,
    ObservationQueryResult,
    ObservedEvidenceSet,
    ObservedValueRecord,
    RevealEvidenceSet,
    RevealValueRecord,
    query_as_observed,
    query_postmortem,
    reveal_artifact_sha256,
)
from .report import (
    ClockPresentation,
    ClockTimeBasis,
    DeferredCapabilityKind,
    InstrumentPresentation,
    PortableReplayReportV1,
    PortableReportBundle,
    PresentationMetadataAuthority,
    RecordingPresentation,
    ReplayPresentationContext,
    ReplayPresentationFrameV1,
    ReportPresentation,
    build_portable_replay_report,
    build_replay_presentation_frame,
    render_portable_report_bundle,
    verify_portable_report_bundle,
    write_portable_report_bundle,
)
from .timeline import (
    ReplayTimeline,
    TimelineEventKind,
    build_replay_timeline,
    derive_timeline_event,
    timeline_event_from_query_result,
)


STALE_PARTIAL_CANCEL_RACE_FIXTURE = "stale_partial_cancel_race"
_FIXTURE_SCHEMA_ID = "KIRBY2_STALE_PARTIAL_CANCEL_RACE_FIXTURE_V1"
_ROOT_CURSOR_US = 60_000_000
_ACTION_TIME_US = 59_750_000
_SELECTED_FRAME_TIMES_US = (59_750_000, 59_830_000, _ROOT_CURSOR_US)

_OVERLAY_TOP_OF_BOOK_EVENTS = (
    (56_000_000, "top-book-event-early"),
    (59_500_000, "top-book-event-late"),
)
_OVERLAY_TRADE_EVENTS = (
    (250_000, "trade-event-early"),
    (59_200_000, "trade-event-0001"),
    (59_800_000, "trade-event-0002"),
)
_OVERLAY_CANCELLATION_EVENTS = ((59_400_000, "cancel-event-0001"),)
_OVERLAY_REPLENISHMENT_EVENTS = (
    (59_600_000, "replenishment-event-0001"),
)
_OVERLAY_EXECUTION_FILL_EVENTS = (
    (59_700_000, "execution-fill-event-0001"),
    (59_900_000, "execution-fill-event-0002"),
)
_REGIME_TRANSITION_TIME_US = 59_610_000


@dataclass(frozen=True, slots=True)
class MicroscopeDemoFixture:
    observed: ObservedEvidenceSet
    reveal: RevealEvidenceSet
    authorization: RevealAuthorization
    overlay_event_times_us: tuple[int, ...]
    timeline_event_specs: tuple[tuple[int, str, TimelineEventKind], ...]


@dataclass(frozen=True, slots=True)
class MicroscopeDemoArtifact:
    """Safe, portable result of assembling the named acceptance fixture."""

    fixture_name: str
    mode: ObservationMode
    report: PortableReplayReportV1
    bundle: PortableReportBundle
    branch_comparison: BranchComparisonV1 | None = None
    bookmarks: tuple[ReplayBookmarkV1, ...] = ()
    annotations: tuple[ReplayAnnotationV1, ...] = ()
    timing_review_packet: TimingLieReviewPacketV1 | None = None
    timing_review_result: TimingLieReviewResultV1 | None = None
    trace_index: MechanisticTraceIndex | None = None


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _delivered_timing(source: int, venue: int, client: int) -> EvidenceTiming:
    return EvidenceTiming(
        source_event_time_us=source,
        venue_receipt=EvidenceTimestamp.recorded(venue),
        client_receive=EvidenceTimestamp.recorded(client),
        client_knowledge=EvidenceTimestamp.recorded(client),
    )


def _decision_timing(source: int, knowledge: int) -> EvidenceTiming:
    return EvidenceTiming(
        source_event_time_us=source,
        venue_receipt=EvidenceTimestamp.not_applicable(
            TimestampAbsenceReason.CLIENT_DECISION
        ),
        client_receive=EvidenceTimestamp.not_applicable(
            TimestampAbsenceReason.RECORDED_SNAPSHOT
        ),
        client_knowledge=EvidenceTimestamp.recorded(knowledge),
    )


def _reveal_timing(source: int) -> EvidenceTiming:
    return EvidenceTiming(
        source_event_time_us=source,
        venue_receipt=EvidenceTimestamp.recorded(source),
        client_receive=EvidenceTimestamp.unavailable(
            TimestampAbsenceReason.NEVER_CLIENT_DELIVERED
        ),
        client_knowledge=EvidenceTimestamp.unavailable(
            TimestampAbsenceReason.NEVER_CLIENT_KNOWN_DURING_RUN
        ),
    )


def build_stale_partial_cancel_race_fixture() -> MicroscopeDemoFixture:
    """Build the named multivenue, stale, partial-fill, cancel-race fixture."""

    delivered_specs: tuple[
        tuple[str, str, int, int, int, dict[str, object]], ...
    ] = (
        (
            "market.trade.primary",
            "trade-event-early",
            200_000,
            225_000,
            250_000,
            {"price_ticks": 100, "quantity": 10, "record_role": "TRADE"},
        ),
        (
            "market.top-of-book.primary",
            "top-book-event-early",
            55_900_000,
            55_950_000,
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
            "market.trade.primary",
            "trade-event-0001",
            59_175_000,
            59_185_000,
            59_200_000,
            {"price_ticks": 101, "quantity": 20, "record_role": "TRADE"},
        ),
        (
            "market.cancellation.primary",
            "cancel-event-0001",
            59_375_000,
            59_385_000,
            59_400_000,
            {"cancelled_quantity": 7, "record_role": "CANCELLATION"},
        ),
        (
            "market.top-of-book.primary",
            "top-book-event-late",
            59_000_000,
            59_100_000,
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
            "market.replenishment.primary",
            "replenishment-event-0001",
            59_575_000,
            59_585_000,
            59_600_000,
            {"added_quantity": 9, "record_role": "REPLENISHMENT"},
        ),
        (
            "execution.fill.exec-1",
            "execution-fill-event-0001",
            59_675_000,
            59_685_000,
            59_700_000,
            {
                "correlation_id": "corr-1",
                "execution_id": "exec-1",
                "order_id": "player-order-1",
                "price_x2": 204,
                "quantity": 10,
                "record_role": "EXECUTION_FILL",
                "side": "BUY",
            },
        ),
        (
            "quote.venue.alpha",
            "venue-alpha-quote-event-0001",
            59_680_000,
            59_690_000,
            59_720_000,
            {"ask_ticks": 105, "bid_ticks": 104, "venue": "ALPHA"},
        ),
        (
            "quote.venue.beta",
            "venue-beta-quote-event-0001",
            59_681_000,
            59_691_000,
            59_720_000,
            {"ask_ticks": 103, "bid_ticks": 102, "venue": "BETA"},
        ),
        (
            "book.level2.primary",
            "level2-event-0001",
            59_700_000,
            59_710_000,
            59_730_000,
            {
                "asks": [[103, 10], [104, 20]],
                "bids": [[101, 30], [100, 40]],
                "record_kind": "LEVEL_2",
            },
        ),
        (
            "depth.heatmap.primary",
            "depth-heatmap-event-0001",
            59_701_000,
            59_711_000,
            59_731_000,
            {"record_kind": "DEPTH_HEATMAP", "rows": [[101, 30], [103, 10]]},
        ),
        (
            "order.player-order-1",
            "player-order-working-event-0001",
            59_740_000,
            59_745_000,
            59_755_000,
            {"order_id": "player-order-1", "state": "WORKING"},
        ),
        (
            "lifecycle.player-order-1.pending-new",
            "lifecycle-pending-new-event-0001",
            59_741_000,
            59_746_000,
            59_756_000,
            {"state": "PENDING_NEW"},
        ),
        (
            "lifecycle.player-order-1.partial",
            "lifecycle-partial-event-0001",
            59_780_000,
            59_790_000,
            59_805_000,
            {"state": "PARTIALLY_FILLED"},
        ),
        (
            "market.trade.primary",
            "trade-event-0002",
            59_775_000,
            59_785_000,
            59_800_000,
            {"price_ticks": 102, "quantity": 30, "record_role": "TRADE"},
        ),
        (
            "trade.pane.primary",
            "time-and-sales-pane-event-0001",
            59_790_000,
            59_800_000,
            59_810_000,
            {"price_ticks": 102, "quantity": 30},
        ),
        (
            "quote.consolidated.primary",
            "consolidated-cross-event-0001",
            59_800_000,
            59_810_000,
            59_820_000,
            {"best_ask_ticks": 103, "best_bid_ticks": 104},
        ),
        (
            "fill.player-order-1",
            "partial-fill-event-0001",
            59_805_000,
            59_815_000,
            59_830_000,
            {"filled_quantity": 15},
        ),
        (
            "execution.fill.exec-1",
            "execution-fill-event-0002",
            59_875_000,
            59_885_000,
            59_900_000,
            {
                "correlation_id": "corr-1",
                "execution_id": "exec-1",
                "order_id": "player-order-1",
                "price_x2": 206,
                "quantity": 5,
                "record_role": "EXECUTION_FILL",
                "side": "BUY",
            },
        ),
        (
            "lifecycle.player-order-1.pending-cancel",
            "lifecycle-pending-cancel-event-0001",
            59_880_000,
            59_890_000,
            59_905_000,
            {"state": "PENDING_CANCEL"},
        ),
        (
            "lifecycle.player-order-1.cancelled",
            "lifecycle-cancelled-event-0001",
            59_900_000,
            59_905_000,
            59_910_000,
            {"state": "CANCELLED"},
        ),
        (
            "position.primary",
            "position-event-0001",
            59_905_000,
            59_910_000,
            59_920_000,
            {"quantity": 15},
        ),
        (
            "metrics.execution.exec-1",
            "execution-metrics-event-0001",
            59_910_000,
            59_920_000,
            59_930_000,
            {"filled_quantity": 15, "implementation_shortfall_x2": 40},
        ),
        (
            "trace.player-order-1",
            "trace-event-0001",
            59_920_000,
            59_930_000,
            59_940_000,
            {"trace_id": "trace-player-order-1"},
        ),
        (
            "warning.invariant.cancel-race",
            "invariant-warning-event-0001",
            59_930_000,
            59_940_000,
            59_950_000,
            {"warning_code": "cancel-fill-race-reconciled"},
        ),
        (
            "quote.consolidated.after",
            "adverse-selection-event-0001",
            59_940_000,
            59_960_000,
            59_980_000,
            {"best_ask_ticks": 100, "best_bid_ticks": 99},
        ),
    )
    decision_specs: tuple[
        tuple[str, str, int, int, dict[str, object]], ...
    ] = (
        (
            "market.relative-volume-baseline.trailing-60s",
            "relative-volume-baseline-event-0001",
            990_000,
            1_000_000,
            {
                "expected_volume": 100,
                "record_role": "RELATIVE_VOLUME_BASELINE",
                "window_duration_us": 60_000_000,
            },
        ),
        (
            "execution.arrival.exec-1",
            "execution-arrival-event-0001",
            9_990_000,
            10_000_000,
            {
                "arrival_midpoint_x2": 202,
                "correlation_id": "corr-1",
                "execution_id": "exec-1",
                "order_id": "player-order-1",
                "record_role": "EXECUTION_ARRIVAL",
                "side": "BUY",
            },
        ),
        (
            "strategy.signal",
            "traffic-transition-event-0001",
            59_720_000,
            59_740_000,
            {"recorded_signal": "GREEN"},
        ),
        (
            "traffic-light.primary",
            "traffic-light-pane-event-0001",
            59_721_000,
            59_741_000,
            {"record_kind": "TRAFFIC_LIGHT_TRANSITION", "state": "GREEN"},
        ),
        (
            "strategy.rule.primary",
            "strategy-rule-event-0001",
            59_725_000,
            59_745_000,
            {"recorded_rule_id": "join-bid-if-green", "result": True},
        ),
        (
            "feature.imbalance.primary",
            "feature-event-0001",
            59_726_000,
            59_746_000,
            {
                "provenance_event_ids": ["top-book-event-late"],
                "value_ppm": 500_000,
            },
        ),
        (
            "order.client-intention",
            "player-action-event-0001",
            59_730_000,
            _ACTION_TIME_US,
            {"side": "BUY", "venue_state": "NOT_OBSERVED"},
        ),
    )
    delivered = tuple(
        ObservedValueRecord(
            series_id,
            event_id,
            sequence,
            _delivered_timing(source, venue, client),
            payload,
        )
        for sequence, (series_id, event_id, source, venue, client, payload)
        in enumerate(delivered_specs, start=1)
    )
    decisions = tuple(
        ObservedValueRecord(
            series_id,
            event_id,
            len(delivered) + sequence,
            _decision_timing(source, knowledge),
            payload,
        )
        for sequence, (series_id, event_id, source, knowledge, payload)
        in enumerate(decision_specs, start=1)
    )
    source_digest = hashlib.sha256(
        _canonical_json_bytes(
            {
                "client_delivered": [item.as_dict() for item in delivered],
                "decision_snapshots": [item.as_dict() for item in decisions],
                "schema_id": _FIXTURE_SCHEMA_ID,
            }
        )
    ).hexdigest()
    run_id = "run-" + source_digest[:24]
    observed = ObservedEvidenceSet(
        run_id,
        source_digest,
        client_delivered=delivered,
        decision_snapshots=decisions,
    )

    reveal_values = (
        RevealValueRecord(
            "regime.transition",
            "regime-transition-event-0001",
            len(delivered) + len(decisions) + 1,
            _reveal_timing(59_610_000),
            RevealCapability.GROUND_TRUTH,
            {"from_regime": "QUIET", "to_regime": "STRESSED"},
        ),
        RevealValueRecord(
            "agent.primary",
            "hidden-agent-event-0001",
            len(delivered) + len(decisions) + 2,
            _reveal_timing(59_650_000),
            RevealCapability.HIDDEN_STATE,
            {"activity": "CANCEL", "agent_id": "synthetic-liquidity-agent-1"},
        ),
    )
    capability_evidence = tuple(
        SourceCapabilityEvidence(
            capability,
            SourceCapabilityAvailability.AVAILABLE,
            source_artifact_id=(
                "stale-partial-cancel-race."
                + capability.value.lower().replace("_", "-")
                + ".v1"
            ),
            source_artifact_sha256=reveal_artifact_sha256(
                tuple(
                    item
                    for item in reveal_values
                    if item.required_capability is capability
                )
            ),
        )
        for capability in RevealCapability
    )
    reveal_source = ReplaySourceCapabilityManifest(
        run_id,
        source_digest,
        "KIRBY2_STALE_PARTIAL_CANCEL_RACE_REVEAL_V1",
        1,
        capability_evidence,
    )
    reveal = RevealEvidenceSet(reveal_source, reveal_values)
    authorization = RevealAuthorization(
        "authorization-stale-partial-cancel-race-v1",
        run_id,
        source_digest,
        observed.evidence_sha256,
        reveal_source.manifest_sha256,
        reveal.evidence_sha256,
        tuple(RevealCapability),
    )
    overlay_times = tuple(
        sorted(
            {
                item.timing.client_knowledge.time_us
                for item in (*delivered, *decisions)
                if item.timing.client_knowledge.time_us is not None
            }
        )
    )
    timeline_specs = (
        (_ACTION_TIME_US, "player-action-event-0001", TimelineEventKind.PLAYER_ACTION),
        (59_740_000, "traffic-transition-event-0001", TimelineEventKind.TRAFFIC_LIGHT_TRANSITION),
        (59_830_000, "partial-fill-event-0001", TimelineEventKind.FILL),
        (59_950_000, "invariant-warning-event-0001", TimelineEventKind.INVARIANT_WARNING),
        (59_980_000, "adverse-selection-event-0001", TimelineEventKind.OBSERVED_UPDATE),
    )
    return MicroscopeDemoFixture(
        observed=observed,
        reveal=reveal,
        authorization=authorization,
        overlay_event_times_us=overlay_times,
        timeline_event_specs=timeline_specs,
    )


def _query_fixture(
    fixture: MicroscopeDemoFixture,
    mode: ObservationMode,
    render_cursor_time_us: int,
) -> ObservationQueryResult:
    action_time_us = (
        _ACTION_TIME_US
        if render_cursor_time_us >= _ACTION_TIME_US
        else None
    )
    capabilities = tuple(RevealCapability) if mode is ObservationMode.POSTMORTEM else ()
    request = ObservationQueryRequest(
        render_cursor_time_us=render_cursor_time_us,
        action_time_us=action_time_us,
        requested_reveal_capabilities=capabilities,
    )
    if mode is ObservationMode.AS_OBSERVED:
        return query_as_observed(fixture.observed, request)
    if mode is ObservationMode.POSTMORTEM:
        return query_postmortem(
            fixture.observed,
            fixture.reveal,
            fixture.authorization,
            request,
        )
    raise ValueError("microscope demo observation mode is unsupported")


def _visible_event_ids(
    rows: tuple[tuple[int, str], ...],
    render_cursor_time_us: int,
) -> tuple[str, ...]:
    return tuple(
        event_id
        for visible_time_us, event_id in rows
        if visible_time_us <= render_cursor_time_us
    )


def _overlay_selection(render_cursor_time_us: int) -> OverlayInputSelection:
    return OverlayInputSelection(
        top_of_book_event_ids=_visible_event_ids(
            _OVERLAY_TOP_OF_BOOK_EVENTS,
            render_cursor_time_us,
        ),
        trade_event_ids=_visible_event_ids(
            _OVERLAY_TRADE_EVENTS,
            render_cursor_time_us,
        ),
        cancellation_event_ids=_visible_event_ids(
            _OVERLAY_CANCELLATION_EVENTS,
            render_cursor_time_us,
        ),
        replenishment_event_ids=_visible_event_ids(
            _OVERLAY_REPLENISHMENT_EVENTS,
            render_cursor_time_us,
        ),
        relative_volume_baseline_event_id=(
            "relative-volume-baseline-event-0001"
            if render_cursor_time_us >= 1_000_000
            else None
        ),
        execution_arrival_event_id=(
            "execution-arrival-event-0001"
            if render_cursor_time_us >= 10_000_000
            else None
        ),
        execution_fill_event_ids=_visible_event_ids(
            _OVERLAY_EXECUTION_FILL_EVENTS,
            render_cursor_time_us,
        ),
    )


def _presentation_context(
    fixture: MicroscopeDemoFixture,
    render_cursor_time_us: int,
    *,
    available_wo36e_capabilities: tuple[DeferredCapabilityKind, ...] = (),
) -> ReplayPresentationContext:
    return ReplayPresentationContext(
        source_run_id=fixture.observed.source_run_id,
        source_event_sha256=fixture.observed.source_event_sha256,
        metadata_authority=(
            PresentationMetadataAuthority.SOURCE_BOUND_DISPLAY_DECLARATION
        ),
        recording=RecordingPresentation(
            recording_id="stale-partial-cancel-race-recording-v1",
            display_name="Stale partial cancel race",
            content_sha256=fixture.observed.source_event_sha256,
        ),
        report=ReportPresentation(
            summary=(
                "A deterministic multivenue replay of stale market data, a partial "
                "fill, a cancel/fill race, and later adverse selection."
            ),
        ),
        clock=ClockPresentation(
            time_basis=ClockTimeBasis.SIMULATION_TIME,
            session_origin_time_us=0,
            display_precision_us=1,
            cursor_label=f"T+{render_cursor_time_us}us",
        ),
        instrument=InstrumentPresentation(
            instrument_id="fixture-instrument-v1",
            symbol="K2X",
            display_name="Kirby2 acceptance instrument",
            venue_labels=("ALPHA", "BETA", "CONSOLIDATED"),
            currency="USD",
            tick_numerator=1,
            tick_denominator=1,
            price_precision=0,
            quantity_unit="shares",
            lot_size=1,
        ),
        limitations=(
            "Fixture-owned display metadata is bound to the recording digest.",
            "Crossed consolidated quotes are reported without an integrity verdict.",
            (
                "Available WO36-E analysis producers are immutable and "
                "source-bound; unavailable producers remain explicitly deferred."
                if available_wo36e_capabilities
                else (
                    "Bookmarks, annotations, and branch comparison remain "
                    "deferred to WO36-E."
                )
            ),
        ),
        available_wo36e_capabilities=available_wo36e_capabilities,
    )


def _assemble_demo_frames(
    fixture: MicroscopeDemoFixture,
    mode: ObservationMode,
    *,
    branch_comparison: BranchComparisonV1 | None = None,
    available_wo36e_capabilities: tuple[DeferredCapabilityKind, ...] | None = None,
) -> tuple[
    ReplayTimeline,
    tuple[ReplayPresentationFrameV1, ...],
    dict[tuple[int, PaneKind], ReplaySidecarTargetV1],
]:
    if available_wo36e_capabilities is None:
        available_wo36e_capabilities = (
            tuple(DeferredCapabilityKind) if branch_comparison is not None else ()
        )
    elif type(available_wo36e_capabilities) is not tuple or any(
        type(item) is not DeferredCapabilityKind
        for item in available_wo36e_capabilities
    ):
        raise TypeError("demo WO36-E capabilities must be a typed tuple")

    query_cache: dict[int, ObservationQueryResult] = {}

    def query_at(render_cursor_time_us: int) -> ObservationQueryResult:
        return query_cache.setdefault(
            render_cursor_time_us,
            _query_fixture(fixture, mode, render_cursor_time_us),
        )

    root_query = query_at(_ROOT_CURSOR_US)
    direct_events = [
        timeline_event_from_query_result(
            query_at(render_cursor_time_us),
            event_id,
            event_kind,
        )
        for render_cursor_time_us, event_id, event_kind in sorted(
            fixture.timeline_event_specs,
            key=lambda item: (item[0], item[1]),
        )
    ]
    if mode is ObservationMode.POSTMORTEM:
        direct_events.append(
            timeline_event_from_query_result(
                query_at(_REGIME_TRANSITION_TIME_US),
                "regime-transition-event-0001",
                TimelineEventKind.REVEALED_REGIME_TRANSITION,
            )
        )
    direct_by_id = {item.event_id: item for item in direct_events}
    branch_sources = (
        direct_by_id["invariant-warning-event-0001"],
        direct_by_id["adverse-selection-event-0001"],
    )
    direct_events.append(
        derive_timeline_event(
            "cancel-race-branch-divergence-v1",
            TimelineEventKind.BRANCH_DIVERGENCE,
            max(item.sequence for item in direct_events) + 1,
            branch_sources,
        )
    )
    timeline, _receipt = build_replay_timeline(root_query, tuple(direct_events))

    frames = []
    sidecar_targets: dict[tuple[int, PaneKind], ReplaySidecarTargetV1] = {}
    for render_cursor_time_us in _SELECTED_FRAME_TIMES_US:
        query = query_at(render_cursor_time_us)
        pane_snapshot = build_synchronized_panes(query)
        if branch_comparison is not None:
            pane_snapshot = attach_counterfactual_comparison(
                pane_snapshot,
                branch_comparison,
            )
        overlay_queries = tuple(
            query_at(event_time_us)
            for event_time_us in fixture.overlay_event_times_us
            if event_time_us <= render_cursor_time_us
        )
        projection, _projection_receipt = build_overlay_window_projection(
            query,
            overlay_queries,
        )
        overlay_set = build_overlay_set(
            query,
            projection,
            _overlay_selection(render_cursor_time_us),
        )
        cursor = timeline.cursor(render_cursor_time_us)
        source_queries = tuple(
            query_cache[cursor_time_us]
            for cursor_time_us in sorted(query_cache)
            if cursor_time_us <= render_cursor_time_us
        )
        frames.append(
            build_replay_presentation_frame(
                timeline,
                cursor,
                query,
                pane_snapshot,
                overlay_set,
                _presentation_context(
                    fixture,
                    render_cursor_time_us,
                    available_wo36e_capabilities=available_wo36e_capabilities,
                ),
                source_queries=source_queries,
            )
        )
        if branch_comparison is not None:
            for pane_kind in PANE_ORDER:
                sidecar_targets[(render_cursor_time_us, pane_kind)] = (
                    bind_replay_sidecar_target(cursor, pane_snapshot, pane_kind)
                )
    return timeline, tuple(frames), sidecar_targets


def _build_demo_annotation_sidecars(
    targets: dict[tuple[int, PaneKind], ReplaySidecarTargetV1],
) -> tuple[
    tuple[ReplayBookmarkV1, ...],
    tuple[ReplayAnnotationV1, ...],
    TimingLieReviewPacketV1,
    TimingLieReviewResultV1,
]:
    trace_target = targets[(_ROOT_CURSOR_US, PaneKind.MECHANISTIC_TRACE)]
    bookmark_v1 = create_replay_bookmark(
        trace_target,
        label="Cancel/fill race",
        author_id="kirby2-acceptance-reviewer",
        tags=("cancel-race", "stale-quote"),
    )
    bookmark_v2 = create_replay_bookmark(
        trace_target,
        label="Cancel/fill race and later adverse selection",
        author_id="kirby2-acceptance-reviewer",
        tags=("adverse-selection", "cancel-race", "stale-quote"),
        predecessor=bookmark_v1,
    )
    annotation_v1 = create_replay_annotation(
        trace_target,
        kind=ReplayAnnotationKind.TIMING_LIE_CANDIDATE,
        body=(
            "Inspect the client acknowledgement, partial fill, cancel/fill race, "
            "and later markout at their exact recorded visibility times."
        ),
        author_id="kirby2-acceptance-reviewer",
        tags=("human-review", "timing"),
    )
    annotation_v2 = create_replay_annotation(
        trace_target,
        kind=ReplayAnnotationKind.TIMING_LIE_CANDIDATE,
        body=(
            "Inspect acknowledgement and client-report timing without interpolating "
            "future quotes or treating the recorded links as a real-market cause."
        ),
        author_id="kirby2-acceptance-reviewer",
        tags=("causal-wording", "human-review", "timing"),
        predecessor=annotation_v1,
    )
    rubric_targets = {
        TimingLieRubricSearch.ORDER_BEFORE_ACKNOWLEDGEMENT: (
            targets[(_SELECTED_FRAME_TIMES_US[0], PaneKind.PLAYER_ORDERS)],
        ),
        TimingLieRubricSearch.FILL_BEFORE_CLIENT_REPORT: (
            targets[(_SELECTED_FRAME_TIMES_US[1], PaneKind.FILLS)],
        ),
        TimingLieRubricSearch.FUTURE_QUOTE_OR_FEATURE_INTERPOLATION: (
            targets[(_SELECTED_FRAME_TIMES_US[0], PaneKind.FEATURE_PROVENANCE)],
        ),
        TimingLieRubricSearch.HIDDEN_FIELD_LEAKAGE: (
            targets[(_ROOT_CURSOR_US, PaneKind.AGENT_ACTIVITY)],
        ),
        TimingLieRubricSearch.RECOMPUTED_EXPLANATIONS: (trace_target,),
        TimingLieRubricSearch.MISLEADING_CAUSAL_WORDING: (
            targets[(_ROOT_CURSOR_US, PaneKind.COUNTERFACTUAL_COMPARISON)],
        ),
    }
    packet = build_timing_lie_review_packet(rubric_targets)
    return (
        (bookmark_v1, bookmark_v2),
        (annotation_v1, annotation_v2),
        packet,
        resolve_timing_lie_review(packet),
    )


def _build_demo_complete_trace(
    fixture: MicroscopeDemoFixture,
) -> MechanisticTraceIndex:
    action_id = "player-action-0001"
    correlation_ids = ("corr-1", "player-order-1")
    artifact_name = "stale-partial-cancel-race-complete-trace-v1"
    artifact_sha256 = hashlib.sha256(artifact_name.encode("ascii")).hexdigest()
    times_us = (
        59_500_000,
        59_730_000,
        59_735_000,
        59_740_000,
        _ACTION_TIME_US,
        59_751_000,
        59_752_000,
        59_760_000,
        59_761_000,
        59_910_000,
        59_980_000,
    )
    payloads: dict[TraceStage, dict[str, object]] = {
        TraceStage.OBSERVABLE_EVENT: {
            "client_knowledge_time_us": 59_500_000,
            "source_event_id": "top-book-event-late",
        },
        TraceStage.FEATURE_UPDATE: {
            "feature_id": "feature.imbalance.primary",
            "source_event_ids": ["top-book-event-late"],
            "value_ppm": 500_000,
        },
        TraceStage.STRATEGY_RULE_EVALUATION: {
            "result": True,
            "rule_id": "join-bid-if-green",
            "source_event_id": "strategy-rule-event-0001",
        },
        TraceStage.TRAFFIC_LIGHT_TRANSITION: {
            "source_event_id": "traffic-light-pane-event-0001",
            "state": "GREEN",
        },
        TraceStage.PLAYER_INPUT: {
            "input_key": "BUY_BID",
            "side": "BUY",
            "source_event_id": "player-action-event-0001",
        },
        TraceStage.CLIENT_ORDER_CREATION: {
            "limit_price_ticks": 102,
            "order_id": "player-order-1",
            "quantity": 20,
            "side": "BUY",
        },
        TraceStage.ROUTING: {
            "order_id": "player-order-1",
            "route_id": "route-parent-0001",
            "venue_id": "ALPHA",
        },
        TraceStage.VENUE_RECEIPT: {
            "acknowledgement_id": "ack-parent-0001",
            "order_id": "player-order-1",
            "venue_id": "ALPHA",
        },
        TraceStage.QUEUE_PLACEMENT: {
            "order_id": "player-order-1",
            "quantity_ahead": 10,
            "venue_id": "ALPHA",
        },
        TraceStage.FILL_OR_CANCEL: {
            "cancelled_quantity": 5,
            "filled_quantity": 15,
            "order_id": "player-order-1",
            "race_resolution": "FILL_THEN_CANCEL_ACK",
        },
        TraceStage.LATER_ADVERSE_SELECTION: {
            "horizon_us": 50_000,
            "markout_ticks": -3,
            "source_event_id": "adverse-selection-event-0001",
        },
    }
    events: list[RecordedTraceEvent] = []
    previous_event_id: str | None = None
    for sequence, (stage, simulation_time_us) in enumerate(
        zip(TRACE_STAGE_ORDER, times_us, strict=True),
        start=1,
    ):
        event_id = f"wo36e-chain-event-{sequence:04d}"
        events.append(
            RecordedTraceEvent(
                event_id=event_id,
                action_id=action_id,
                stage=stage,
                artifact_kind=EXPECTED_ARTIFACT_KIND[stage],
                simulation_time_us=simulation_time_us,
                correlation_ids=correlation_ids,
                parent_event_ids=(
                    () if previous_event_id is None else (previous_event_id,)
                ),
                provenance=TraceProvenance(
                    fixture.observed.source_run_id,
                    artifact_name,
                    artifact_sha256,
                    "kirby2-wo36e-acceptance-chain-v1",
                    1,
                    sequence,
                ),
                payload=payloads[stage],
            )
        )
        previous_event_id = event_id
    return build_trace_index(
        TraceSourceRecording(fixture.observed.source_run_id, tuple(events))
    )


def _build_demo_branch_comparison(
    fixture: MicroscopeDemoFixture,
    mode: ObservationMode,
    trace_index: MechanisticTraceIndex,
) -> BranchComparisonV1:
    parent_events = _demo_comparison_events(branch=False, mode=mode)
    branch_events = _demo_comparison_events(branch=True, mode=mode)
    branch_run_id = "run-" + hashlib.sha256(
        _canonical_json_bytes(
            {
                "fixture": STALE_PARTIAL_CANCEL_RACE_FIXTURE,
                "intervention": "ROUTE_TO_BETA",
                "parent_run_id": fixture.observed.source_run_id,
            }
        )
    ).hexdigest()[:24]
    parent = ComparisonRunInput(
        run_id=fixture.observed.source_run_id,
        source_event_sha256=fixture.observed.source_event_sha256,
        timeline_sha256=hashlib.sha256(
            _canonical_json_bytes([item.semantic_dict() for item in parent_events])
        ).hexdigest(),
        observation_mode=mode,
        events=parent_events,
        series=_demo_comparison_series(parent_events, branch=False, mode=mode),
    )
    branch_source_sha256 = hashlib.sha256(
        _canonical_json_bytes([item.semantic_dict() for item in branch_events])
    ).hexdigest()
    branch = ComparisonRunInput(
        run_id=branch_run_id,
        source_event_sha256=branch_source_sha256,
        timeline_sha256=branch_source_sha256,
        observation_mode=mode,
        events=branch_events,
        series=_demo_comparison_series(branch_events, branch=True, mode=mode),
    )
    synchronized_prefix = [
        item.semantic_dict() for item in parent_events[:4]
    ]
    intervention = {
        "action": "ROUTE_TO_BETA",
        "target_action_id": "player-action-0001",
        "timing_delta_us": 0,
    }
    selection = CounterfactualBranchInput(
        parent_run_id=parent.run_id,
        branch_run_id=branch.run_id,
        parent_prefix_sha256=hashlib.sha256(
            _canonical_json_bytes(synchronized_prefix)
        ).hexdigest(),
        snapshot_sha256=hashlib.sha256(
            _canonical_json_bytes(
                {
                    "clock_time_us": _ACTION_TIME_US,
                    "fixture": STALE_PARTIAL_CANCEL_RACE_FIXTURE,
                    "rng_state_policy": "PRESERVED_AT_FORK",
                    "working_order_ids": [],
                }
            )
        ).hexdigest(),
        fork_time_us=_ACTION_TIME_US,
        intervention=intervention,
        mutation_manifest_sha256=hashlib.sha256(
            _canonical_json_bytes(intervention)
        ).hexdigest(),
        branch_mode=CounterfactualMode.ENDOGENOUS_FORK,
        rng_policy=CounterfactualRngPolicy.FORK_SNAPSHOT_OWNED_RNG_STATE,
    )
    return build_branch_comparison(
        parent,
        branch,
        selection,
        mode,
        overlays=_demo_comparison_overlays(
            parent,
            branch,
            mode,
        ),
        reveal_authorization=(
            fixture.authorization if mode is ObservationMode.POSTMORTEM else None
        ),
        mechanistic_trace=trace_index,
        require_complete_trace=True,
    )


def _demo_comparison_events(
    *,
    branch: bool,
    mode: ObservationMode,
) -> tuple[ComparisonEventInput, ...]:
    if type(mode) is not ObservationMode:
        raise TypeError("demo comparison event mode is invalid")
    branch_queue_payload: dict[str, object] = {
        "order_id": "player-order-1",
        "venue_id": "BETA",
    }
    branch_fill_payload: dict[str, object] = {
        "filled_quantity": 20,
        "order_id": "player-order-1",
        "price_ticks": 103,
    }
    if mode is ObservationMode.POSTMORTEM:
        branch_queue_payload.update(
            {
                "liquidity": "HIDDEN_ICEBERG",
                "quantity_ahead": 4,
            }
        )
        branch_fill_payload["liquidity"] = "HIDDEN_ICEBERG"
    else:
        branch_queue_payload.update(
            {
                "estimate_kind": "MODEL_ESTIMATE",
                "estimated_quantity_ahead": 4,
            }
        )
    suffix = (
        (
            "ROUTE_DECISION",
            59_752_000,
            {
                "order_id": "player-order-1",
                "route_id": "route-branch-0001",
                "venue_id": "BETA",
            },
        ),
        (
            "VENUE_ACKNOWLEDGEMENT",
            59_760_000,
            {
                "acknowledgement_id": "ack-branch-0001",
                "order_id": "player-order-1",
                "venue_id": "BETA",
            },
        ),
        (
            "QUEUE_PLACEMENT",
            59_761_000,
            branch_queue_payload,
        ),
        (
            "FILL",
            59_830_000,
            branch_fill_payload,
        ),
        (
            "CANCEL_FILL_RACE",
            59_910_000,
            {
                "cancelled_quantity": 0,
                "filled_quantity": 20,
                "resolution": "CANCEL_REJECTED_ALREADY_FILLED",
            },
        ),
        (
            "ENDOGENOUS_MARKET_PATH",
            59_980_000,
            {
                "best_ask_ticks": 101,
                "best_bid_ticks": 100,
                "path_effect": "BRANCH_ORDER_CHANGED_LATER_QUEUE_DEPLETION",
            },
        ),
    ) if branch else (
        (
            "ROUTE_DECISION",
            59_752_000,
            {
                "order_id": "player-order-1",
                "route_id": "route-parent-0001",
                "venue_id": "ALPHA",
            },
        ),
        (
            "VENUE_ACKNOWLEDGEMENT",
            59_760_000,
            {
                "acknowledgement_id": "ack-parent-0001",
                "order_id": "player-order-1",
                "venue_id": "ALPHA",
            },
        ),
        (
            "QUEUE_PLACEMENT",
            59_761_000,
            {
                "liquidity": "DISPLAYED",
                "order_id": "player-order-1",
                "quantity_ahead": 10,
                "venue_id": "ALPHA",
            },
        ),
        (
            "FILL",
            59_830_000,
            {
                "filled_quantity": 15,
                "liquidity": "DISPLAYED",
                "order_id": "player-order-1",
                "price_ticks": 102,
            },
        ),
        (
            "CANCEL_FILL_RACE",
            59_910_000,
            {
                "cancelled_quantity": 5,
                "filled_quantity": 15,
                "resolution": "FILL_THEN_CANCEL_ACK",
            },
        ),
        (
            "ENDOGENOUS_MARKET_PATH",
            59_980_000,
            {
                "best_ask_ticks": 100,
                "best_bid_ticks": 99,
                "path_effect": "PARENT_QUEUE_DEPLETION",
            },
        ),
    )
    specifications = (
        (
            "OBSERVABLE_QUOTE",
            59_500_000,
            {
                "ask_ticks": 103,
                "bid_ticks": 101,
                "quote_age_us": 400_000,
            },
        ),
        (
            "FEATURE_UPDATE",
            59_730_000,
            {"feature_id": "imbalance-v1", "value_ppm": 500_000},
        ),
        (
            "STRATEGY_AND_TRAFFIC_LIGHT",
            59_740_000,
            {"rule_id": "join-bid-if-green", "traffic_light": "GREEN"},
        ),
        (
            "PLAYER_INPUT",
            _ACTION_TIME_US,
            {"action_id": "player-action-0001", "command": "BUY_BID"},
        ),
        *suffix,
    )
    return tuple(
        ComparisonEventInput(sequence, time_us, kind, payload)
        for sequence, (kind, time_us, payload) in enumerate(
            specifications,
            start=1,
        )
    )


def _demo_comparison_series(
    events: tuple[ComparisonEventInput, ...],
    *,
    branch: bool,
    mode: ObservationMode,
) -> tuple[ComparisonSeriesInput, ...]:
    if type(mode) is not ObservationMode:
        raise TypeError("demo comparison series mode is invalid")
    def record(
        key: str,
        event_index: int,
        value: object,
        *,
        calculation_id: str | None = None,
    ) -> ComparisonRecordInput:
        event = events[event_index]
        return ComparisonRecordInput(
            record_key=key,
            simulation_time_us=event.simulation_time_us,
            value=value,
            source_event_ids=(event.event_id,),
            calculation_id=calculation_id,
            calculation_version=(1 if calculation_id is not None else None),
        )

    orders = (
        record(
            "player-order-1",
            4,
            {
                "limit_price_ticks": 102 if not branch else 103,
                "order_id": "player-order-1",
                "quantity": 20,
                "side": "BUY",
                "venue_id": "ALPHA" if not branch else "BETA",
            },
        ),
    )
    queues = (
        record(
            "player-order-1",
            6,
            (
                {
                    "estimate_kind": "MODEL_ESTIMATE",
                    "estimated_quantity_ahead": 4,
                }
                if branch and mode is ObservationMode.AS_OBSERVED
                else {
                    "estimate_kind": "SOURCE_RECORDED_QUEUE",
                    "liquidity": (
                        "HIDDEN_ICEBERG" if branch else "DISPLAYED"
                    ),
                    "quantity_ahead": 4 if branch else 10,
                }
            ),
            calculation_id=(
                "wo36e-queue-estimate-v1"
                if branch and mode is ObservationMode.AS_OBSERVED
                else None
            ),
        ),
    )
    fills = (
        record(
            "player-order-1",
            7,
            {
                "filled_quantity": 15 if not branch else 20,
                "price_ticks": 102 if not branch else 103,
            },
        ),
    )
    metrics = tuple(
        record(
            key,
            event_index,
            value,
            calculation_id=f"wo36e-{key}-v1",
        )
        for key, event_index, value in (
            ("adverse-selection-ticks", 9, -3 if not branch else -1),
            ("filled-quantity", 7, 15 if not branch else 20),
            ("implementation-shortfall-x2", 7, 40 if not branch else 20),
            ("latency-us", 5, 30_000 if not branch else 18_000),
            ("stale-quote-age-us", 4, 400_000 if not branch else 100_000),
        )
    )
    market_path = (
        record(
            "later-top-of-book",
            9,
            {
                "best_ask_ticks": 100 if not branch else 101,
                "best_bid_ticks": 99 if not branch else 100,
                "path_class": "ENDOGENOUS_AFTER_SYNCHRONIZED_PREFIX",
            },
        ),
    )
    rows = {
        ComparisonSeriesKind.ORDERS: orders,
        ComparisonSeriesKind.QUEUE_STATES: queues,
        ComparisonSeriesKind.FILLS: fills,
        ComparisonSeriesKind.DECLARED_METRICS: metrics,
        ComparisonSeriesKind.ENDOGENOUS_MARKET_PATH: market_path,
    }
    return tuple(
        ComparisonSeriesInput.from_records(kind, rows[kind])
        for kind in ComparisonSeriesKind
    )


def _demo_comparison_overlays(
    parent: ComparisonRunInput,
    branch: ComparisonRunInput,
    mode: ObservationMode,
) -> tuple[ComparisonOverlayInput, ...]:
    specifications = (
        (ComparisonOverlayKind.SPREAD, 2, 1, "ticks", 9),
        (ComparisonOverlayKind.MICROPRICE, 202, 203, "price_x2", 9),
        (ComparisonOverlayKind.IMBALANCE, 500_000, 350_000, "ppm", 9),
        (ComparisonOverlayKind.TRADE_VELOCITY, 2, 3, "events_per_second", 7),
        (ComparisonOverlayKind.CANCELLATION_VELOCITY, 1, 0, "events_per_second", 8),
        (ComparisonOverlayKind.REPLENISHMENT, 9, 14, "quantity", 9),
        (ComparisonOverlayKind.RELATIVE_VOLUME, 1_500_000, 1_700_000, "ppm", 9),
        (ComparisonOverlayKind.SHORT_TERM_VOLATILITY, 1, 2, "ticks", 9),
        (ComparisonOverlayKind.IMPLEMENTATION_SHORTFALL, 40, 20, "price_x2", 7),
        (ComparisonOverlayKind.LATENCY, 30_000, 18_000, "microseconds", 5),
        (ComparisonOverlayKind.STALE_QUOTE_AGE, 400_000, 100_000, "microseconds", 4),
        (ComparisonOverlayKind.QUEUE_ESTIMATE, 10, 4, "quantity", 6),
        (ComparisonOverlayKind.ADVERSE_SELECTION, -3, -1, "ticks", 9),
        (
            ComparisonOverlayKind.ALGORITHM_SCHEDULE,
            {"slice": 1, "status": "ON_SCHEDULE"},
            {"slice": 1, "status": "ACCELERATED_BY_FILL"},
            "state",
            7,
        ),
        (
            ComparisonOverlayKind.REGIME_STATE,
            {"market_state": "STRESSED", "regime": "LIQUIDITY_VACUUM"},
            {"market_state": "RECOVERING", "regime": "REPLENISHMENT"},
            "state",
            9,
        ),
    )
    output = []
    for kind, parent_value, branch_value, unit, event_index in specifications:
        parent_event = parent.events[event_index]
        branch_event = branch.events[event_index]
        output.append(
            ComparisonOverlayInput(
                kind=kind,
                availability=ComparisonAvailability.AVAILABLE,
                parent_value=parent_value,
                branch_value=branch_value,
                unit=unit,
                calculation_id=(
                    "wo36e-comparison-" + kind.value.lower().replace("_", "-")
                ),
                calculation_version=1,
                parent_run_id=parent.run_id,
                branch_run_id=branch.run_id,
                parent_source_event_ids=(parent_event.event_id,),
                branch_source_event_ids=(branch_event.event_id,),
                parent_source_payload_sha256=(parent_event.payload_sha256,),
                branch_source_payload_sha256=(branch_event.payload_sha256,),
                evidence_scope=ComparisonEvidenceScope.DECLARED_CALCULATION,
            )
        )
    if mode is ObservationMode.POSTMORTEM:
        parent_event = parent.events[6]
        branch_event = branch.events[6]
        output.append(
            ComparisonOverlayInput(
                kind=ComparisonOverlayKind.AGENT_TRUTH,
                availability=ComparisonAvailability.AVAILABLE,
                parent_value={"agent_state": "PROVIDING_DISPLAYED_LIQUIDITY"},
                branch_value={"agent_state": "REFRESHING_HIDDEN_ICEBERG"},
                unit="state",
                calculation_id="wo36e-comparison-agent-truth",
                calculation_version=1,
                parent_run_id=parent.run_id,
                branch_run_id=branch.run_id,
                parent_source_event_ids=(parent_event.event_id,),
                branch_source_event_ids=(branch_event.event_id,),
                parent_source_payload_sha256=(parent_event.payload_sha256,),
                branch_source_payload_sha256=(branch_event.payload_sha256,),
                evidence_scope=ComparisonEvidenceScope.POSTMORTEM_HIDDEN_STATE,
                required_capability=RevealCapability.HIDDEN_STATE,
            )
        )
    else:
        output.append(
            ComparisonOverlayInput.unavailable(
                ComparisonOverlayKind.AGENT_TRUTH,
                "AUTHORIZED_POSTMORTEM_REQUIRED",
            )
        )
    return tuple(output)


def build_microscope_demo_artifact(
    fixture_name: str,
    mode: ObservationMode,
    *,
    compare_counterfactual: bool = False,
) -> MicroscopeDemoArtifact:
    """Assemble the named fixture into one deterministic offline report bundle."""

    if fixture_name != STALE_PARTIAL_CANCEL_RACE_FIXTURE:
        raise ValueError("unknown microscope demo fixture")
    if type(mode) is not ObservationMode:
        raise TypeError("microscope demo mode must use ObservationMode")
    if type(compare_counterfactual) is not bool:
        raise TypeError("counterfactual comparison flag must be boolean")
    fixture = build_stale_partial_cancel_race_fixture()
    branch_comparison = None
    trace_index = None
    if compare_counterfactual:
        trace_index = _build_demo_complete_trace(fixture)
        branch_comparison = _build_demo_branch_comparison(
            fixture,
            mode,
            trace_index,
        )
    _timeline, frames, targets = _assemble_demo_frames(
        fixture,
        mode,
        branch_comparison=branch_comparison,
    )
    bookmarks: tuple[ReplayBookmarkV1, ...] = ()
    annotations: tuple[ReplayAnnotationV1, ...] = ()
    timing_review_packet = None
    timing_review_result = None
    if compare_counterfactual:
        if branch_comparison is None or trace_index is None:  # pragma: no cover
            raise RuntimeError("counterfactual comparison construction disappeared")
        (
            bookmarks,
            annotations,
            timing_review_packet,
            timing_review_result,
        ) = _build_demo_annotation_sidecars(targets)
        report = build_portable_replay_report(
            frames,
            bookmarks=bookmarks,
            annotations=annotations,
            branch_comparison=branch_comparison,
            timing_review_packet=timing_review_packet,
            timing_review_result=timing_review_result,
        )
    else:
        report = build_portable_replay_report(frames)
    bundle = render_portable_report_bundle(report)
    return MicroscopeDemoArtifact(
        fixture_name=fixture_name,
        mode=mode,
        report=report,
        bundle=bundle,
        branch_comparison=branch_comparison,
        bookmarks=bookmarks,
        annotations=annotations,
        timing_review_packet=timing_review_packet,
        timing_review_result=timing_review_result,
        trace_index=trace_index,
    )


def _parse_mode(value: str) -> ObservationMode:
    values = {
        "as-observed": ObservationMode.AS_OBSERVED,
        "postmortem": ObservationMode.POSTMORTEM,
    }
    try:
        return values[value]
    except KeyError as error:
        raise ValueError("unknown microscope demo mode") from error


def _configure_microscope_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fixture",
        required=True,
        choices=(STALE_PARTIAL_CANCEL_RACE_FIXTURE,),
        help="exact deterministic microscope acceptance fixture",
    )
    parser.add_argument(
        "--mode",
        choices=("as-observed", "postmortem"),
        default="as-observed",
        help="observation boundary applied to every report frame",
    )
    parser.add_argument(
        "--compare-counterfactual",
        action="store_true",
        help="include the immutable WO36-E parent/branch comparison sidecars",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional new directory for the relocatable offline report",
    )


def _handle_microscope_demo(args: argparse.Namespace) -> int:
    artifact = build_microscope_demo_artifact(
        args.fixture,
        _parse_mode(args.mode),
        compare_counterfactual=args.compare_counterfactual,
    )
    if args.output is None:
        with tempfile.TemporaryDirectory(prefix="kirby2-microscope-demo-") as directory:
            root = Path(directory).resolve() / "report"
            write_portable_report_bundle(artifact.bundle, root)
            verification = verify_portable_report_bundle(root)
        output = "NOT_PERSISTED"
    else:
        root = args.output.expanduser().resolve()
        write_portable_report_bundle(artifact.bundle, root)
        verification = verify_portable_report_bundle(root)
        output = str(root)
    response = {
        "bundle_id": artifact.bundle.bundle_id,
        "command": "microscope-demo",
        "fixture": artifact.fixture_name,
        "frame_ids": [item.frame_id for item in artifact.report.frames],
        "member_count": verification["member_count"],
        "mode": artifact.mode.value,
        "output": output,
        "report_id": artifact.report.report_id,
        "status": verification["status"],
    }
    if artifact.branch_comparison is not None:
        if (
            artifact.timing_review_packet is None
            or artifact.timing_review_result is None
            or artifact.trace_index is None
        ):
            raise RuntimeError("WO36-E demo artifact is incomplete")
        response.update(
            {
                "annotation_ids": [
                    item.annotation_id for item in artifact.annotations
                ],
                "bookmark_ids": [item.bookmark_id for item in artifact.bookmarks],
                "comparison_id": artifact.branch_comparison.comparison_id,
                "complete_action_count": (
                    artifact.trace_index.complete_action_count
                ),
                "human_review_status": (
                    artifact.timing_review_result.human_result.value
                ),
                "timing_review_packet_id": (
                    artifact.timing_review_packet.packet_id
                ),
                "timing_review_status": (
                    artifact.timing_review_result.technical_status.value
                ),
            }
        )
    print(
        json.dumps(
            response,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


MICROSCOPE_COMMAND_MODULE = CommandModule(
    module_id="REPLAY_MICROSCOPE",
    commands=(
        CommandSpec(
            command_id="MICROSCOPE_DEMO",
            name="microscope-demo",
            help="render the named deterministic replay fixture as an offline report",
            handler=_handle_microscope_demo,
            configure=_configure_microscope_demo,
        ),
    ),
)


__all__ = [
    "MICROSCOPE_COMMAND_MODULE",
    "STALE_PARTIAL_CANCEL_RACE_FIXTURE",
    "MicroscopeDemoArtifact",
    "MicroscopeDemoFixture",
    "build_microscope_demo_artifact",
    "build_stale_partial_cancel_race_fixture",
]
