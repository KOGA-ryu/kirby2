"""CLI demo and deterministic acceptance fixture for the offline microscope."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec

from .data_age import EvidenceTimestamp, EvidenceTiming, TimestampAbsenceReason
from .overlays import (
    OverlayInputSelection,
    build_overlay_set,
    build_overlay_window_projection,
)
from .panes import build_synchronized_panes
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
            "Bookmarks, annotations, and branch comparison remain deferred to WO36-E.",
        ),
    )


def _assemble_demo_frames(
    fixture: MicroscopeDemoFixture,
    mode: ObservationMode,
) -> tuple[ReplayTimeline, tuple[ReplayPresentationFrameV1, ...]]:
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
    for render_cursor_time_us in _SELECTED_FRAME_TIMES_US:
        query = query_at(render_cursor_time_us)
        pane_snapshot = build_synchronized_panes(query)
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
                _presentation_context(fixture, render_cursor_time_us),
                source_queries=source_queries,
            )
        )
    return timeline, tuple(frames)


def build_microscope_demo_artifact(
    fixture_name: str,
    mode: ObservationMode,
) -> MicroscopeDemoArtifact:
    """Assemble the named fixture into one deterministic offline report bundle."""

    if fixture_name != STALE_PARTIAL_CANCEL_RACE_FIXTURE:
        raise ValueError("unknown microscope demo fixture")
    if type(mode) is not ObservationMode:
        raise TypeError("microscope demo mode must use ObservationMode")
    fixture = build_stale_partial_cancel_race_fixture()
    _timeline, frames = _assemble_demo_frames(fixture, mode)
    report = build_portable_replay_report(frames)
    bundle = render_portable_report_bundle(report)
    return MicroscopeDemoArtifact(
        fixture_name=fixture_name,
        mode=mode,
        report=report,
        bundle=bundle,
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
        required=True,
        choices=("as-observed", "postmortem"),
        help="observation boundary applied to every report frame",
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
    print(
        json.dumps(
            {
                "bundle_id": artifact.bundle.bundle_id,
                "command": "microscope-demo",
                "fixture": artifact.fixture_name,
                "frame_ids": [item.frame_id for item in artifact.report.frames],
                "member_count": verification["member_count"],
                "mode": artifact.mode.value,
                "output": output,
                "report_id": artifact.report.report_id,
                "status": verification["status"],
            },
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
