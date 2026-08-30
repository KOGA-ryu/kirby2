"""Build player-action lineages only from explicit recorded relationships."""

from __future__ import annotations

from collections import defaultdict

from .models import (
    EXPECTED_ARTIFACT_KIND,
    TRACE_EDGE_ORDER,
    TRACE_STAGE_ORDER,
    PlayerActionTrace,
    RecordedTraceEvent,
    TraceAvailability,
    TraceEdge,
    TraceEdgeKind,
    TraceLinkStatus,
    TraceNode,
    TraceSourceRecording,
    TraceStage,
    TraceUnavailableReason,
)


def build_player_action_trace(
    source: TraceSourceRecording,
    action_id: str,
) -> PlayerActionTrace:
    """Build one trace without using timestamp proximity as a relationship."""

    action_events = tuple(item for item in source.events if item.action_id == action_id)
    by_stage: dict[TraceStage, list[RecordedTraceEvent]] = defaultdict(list)
    for event in action_events:
        by_stage[event.stage].append(event)

    nodes = tuple(_node_for_stage(stage, by_stage[stage]) for stage in TRACE_STAGE_ORDER)
    player_node = nodes[TRACE_STAGE_ORDER.index(TraceStage.PLAYER_INPUT)]
    if (
        player_node.availability is not TraceAvailability.RECORDED
        or player_node.source_event_id is None
    ):
        raise RuntimeError("source player action lost its recorded input event")
    edges = tuple(
        _edge_for_nodes(kind, nodes[index], nodes[index + 1], action_events)
        for index, kind in enumerate(TRACE_EDGE_ORDER)
    )
    return PlayerActionTrace(
        action_id=action_id,
        player_input_event_id=player_node.source_event_id,
        nodes=nodes,
        edges=edges,
    )


def _node_for_stage(
    stage: TraceStage,
    events: list[RecordedTraceEvent],
) -> TraceNode:
    if not events:
        return TraceNode(
            stage,
            TraceAvailability.UNAVAILABLE,
            None,
            None,
            TraceUnavailableReason.SOURCE_EVENT_MISSING,
        )
    if len(events) != 1:
        return TraceNode(
            stage,
            TraceAvailability.UNAVAILABLE,
            None,
            None,
            TraceUnavailableReason.AMBIGUOUS_SOURCE_EVENTS,
        )
    event = events[0]
    if event.artifact_kind is not EXPECTED_ARTIFACT_KIND[stage]:
        return TraceNode(
            stage,
            TraceAvailability.UNAVAILABLE,
            event.event_id,
            event.provenance,
            TraceUnavailableReason.RECORDED_ARTIFACT_KIND_MISMATCH,
        )
    return TraceNode(
        stage,
        TraceAvailability.RECORDED,
        event.event_id,
        event.provenance,
        None,
    )


def _edge_for_nodes(
    kind: TraceEdgeKind,
    left: TraceNode,
    right: TraceNode,
    action_events: tuple[RecordedTraceEvent, ...],
) -> TraceEdge:
    events_by_id = {item.event_id: item for item in action_events}
    left_event = (
        None
        if left.source_event_id is None
        else events_by_id.get(left.source_event_id)
    )
    right_event = (
        None
        if right.source_event_id is None
        else events_by_id.get(right.source_event_id)
    )
    provenance = tuple(
        item.provenance for item in (left_event, right_event) if item is not None
    )
    if (
        left.availability is TraceAvailability.UNAVAILABLE
        or right.availability is TraceAvailability.UNAVAILABLE
        or left_event is None
        or right_event is None
    ):
        return TraceEdge(
            kind,
            left.stage,
            right.stage,
            TraceLinkStatus.UNAVAILABLE,
            left.source_event_id,
            right.source_event_id,
            (),
            provenance,
            TraceUnavailableReason.ENDPOINT_UNAVAILABLE,
        )
    shared_correlations = tuple(
        sorted(set(left_event.correlation_ids) & set(right_event.correlation_ids))
    )
    if left_event.event_id not in right_event.parent_event_ids:
        reason = TraceUnavailableReason.EXPLICIT_PARENT_LINK_MISSING
    elif not shared_correlations:
        reason = TraceUnavailableReason.CORRELATION_ID_MISSING
    elif right_event.provenance.event_sequence <= left_event.provenance.event_sequence:
        reason = TraceUnavailableReason.RECORDED_ORDER_INVALID
    else:
        return TraceEdge(
            kind,
            left.stage,
            right.stage,
            TraceLinkStatus.LINKED,
            left_event.event_id,
            right_event.event_id,
            shared_correlations,
            provenance,
            None,
        )
    return TraceEdge(
        kind,
        left.stage,
        right.stage,
        TraceLinkStatus.UNAVAILABLE,
        left_event.event_id,
        right_event.event_id,
        shared_correlations,
        provenance,
        reason,
    )
