"""Committed complete and legacy fixtures for the mechanistic trace index."""

from __future__ import annotations

import hashlib

from .models import (
    EXPECTED_ARTIFACT_KIND,
    TRACE_STAGE_ORDER,
    RecordedTraceEvent,
    TraceArtifactKind,
    TraceProvenance,
    TraceSourceRecording,
    TraceStage,
)


COMPLETE_FIXTURE_NAME = "wo36-a-complete-action-chain-v1"
LEGACY_FIXTURE_NAME = "wo36-a-incomplete-legacy-v1"


def complete_trace_fixture() -> TraceSourceRecording:
    run_id = _run_id(COMPLETE_FIXTURE_NAME)
    artifact_name = "wo36-a-complete-events"
    artifact_sha256 = _sha256(COMPLETE_FIXTURE_NAME)
    events: list[RecordedTraceEvent] = []
    sequence = 1
    for action_number, start_time_us in ((1, 100_000), (2, 200_000)):
        action_id = f"action-{action_number:04d}"
        correlation_id = f"correlation-{action_number:04d}"
        previous_event_id: str | None = None
        for stage_number, stage in enumerate(TRACE_STAGE_ORDER, start=1):
            event_id = f"event-{sequence:06d}"
            events.append(
                RecordedTraceEvent(
                    event_id=event_id,
                    action_id=action_id,
                    stage=stage,
                    artifact_kind=EXPECTED_ARTIFACT_KIND[stage],
                    simulation_time_us=start_time_us + stage_number * 100,
                    correlation_ids=(correlation_id,),
                    parent_event_ids=(
                        () if previous_event_id is None else (previous_event_id,)
                    ),
                    provenance=TraceProvenance(
                        run_id,
                        artifact_name,
                        artifact_sha256,
                        "kirby2-microscope-acceptance-event-v1",
                        1,
                        sequence,
                    ),
                    payload=_complete_payload(stage, action_number),
                )
            )
            previous_event_id = event_id
            sequence += 1
    return TraceSourceRecording(run_id, tuple(events))


def incomplete_legacy_trace_fixture() -> TraceSourceRecording:
    run_id = _run_id(LEGACY_FIXTURE_NAME)
    artifact_name = "wo36-a-legacy-events"
    artifact_sha256 = _sha256(LEGACY_FIXTURE_NAME)
    action_id = "legacy-action-0001"
    specifications = (
        (
            TraceStage.OBSERVABLE_EVENT,
            TraceArtifactKind.OBSERVABLE_EVENT,
            "legacy-event-0001",
            (),
            ("legacy-observation",),
        ),
        (
            TraceStage.FEATURE_UPDATE,
            TraceArtifactKind.FEATURE_UPDATE,
            "legacy-event-0002",
            (),
            ("legacy-observation",),
        ),
        (
            TraceStage.PLAYER_INPUT,
            TraceArtifactKind.PLAYER_INPUT,
            "legacy-event-0003",
            (),
            ("legacy-order",),
        ),
        (
            TraceStage.CLIENT_ORDER_CREATION,
            TraceArtifactKind.CLIENT_ORDER,
            "legacy-event-0004",
            ("legacy-event-0003",),
            ("legacy-order",),
        ),
        (
            TraceStage.VENUE_RECEIPT,
            TraceArtifactKind.VENUE_RECEIPT,
            "legacy-event-0005",
            ("legacy-event-0004",),
            ("legacy-order",),
        ),
        (
            TraceStage.QUEUE_PLACEMENT,
            TraceArtifactKind.QUEUE_STATE,
            "legacy-event-0006",
            ("legacy-event-0005",),
            (),
        ),
        (
            TraceStage.FILL_OR_CANCEL,
            TraceArtifactKind.ORDER_OUTCOME,
            "legacy-event-0007",
            (),
            (),
        ),
    )
    events = tuple(
        RecordedTraceEvent(
            event_id=event_id,
            action_id=action_id,
            stage=stage,
            artifact_kind=artifact_kind,
            simulation_time_us=500_000 + index,
            correlation_ids=correlation_ids,
            parent_event_ids=parent_event_ids,
            provenance=TraceProvenance(
                run_id,
                artifact_name,
                artifact_sha256,
                "kirby2-legacy-session-event-v1",
                1,
                index,
            ),
            payload={"legacy_event": event_id},
        )
        for index, (
            stage,
            artifact_kind,
            event_id,
            parent_event_ids,
            correlation_ids,
        ) in enumerate(specifications, start=1)
    )
    return TraceSourceRecording(run_id, events)


def _complete_payload(stage: TraceStage, action_number: int) -> dict[str, object]:
    order_id = f"client-order-{action_number:04d}"
    payloads: dict[TraceStage, dict[str, object]] = {
        TraceStage.OBSERVABLE_EVENT: {
            "best_ask_ticks": 10_001,
            "best_bid_ticks": 10_000,
            "client_receive_time_us": 99_900 + action_number * 100_000,
        },
        TraceStage.FEATURE_UPDATE: {
            "feature_id": "top-book-imbalance-v1",
            "source_event_ids": [f"market-event-{action_number:04d}"],
            "value_millionths": 450_000,
        },
        TraceStage.STRATEGY_RULE_EVALUATION: {
            "decision_artifact_id": f"decision-{action_number:04d}",
            "matched_rule_lines": [5, 6],
            "signal": "GREEN",
        },
        TraceStage.TRAFFIC_LIGHT_TRANSITION: {
            "after": "GREEN",
            "before": "WAIT",
            "reason": "recorded-strategy-decision",
        },
        TraceStage.PLAYER_INPUT: {
            "command": "BUY_ASK",
            "input_key": "s",
            "quantity": 100,
        },
        TraceStage.CLIENT_ORDER_CREATION: {
            "client_order_id": order_id,
            "limit_price_ticks": 10_001,
            "quantity": 100,
            "side": "BUY",
        },
        TraceStage.ROUTING: {
            "client_order_id": order_id,
            "route_policy": "DIRECT",
            "venue_id": "venue-a",
        },
        TraceStage.VENUE_RECEIPT: {
            "client_order_id": order_id,
            "venue_id": "venue-a",
            "venue_order_id": f"venue-order-{action_number:04d}",
        },
        TraceStage.QUEUE_PLACEMENT: {
            "price_ticks": 10_001,
            "quantity_ahead": 0,
            "venue_order_id": f"venue-order-{action_number:04d}",
        },
        TraceStage.FILL_OR_CANCEL: {
            "event": "FULL_FILL" if action_number == 1 else "CANCELLED",
            "filled_quantity": 100 if action_number == 1 else 0,
            "venue_order_id": f"venue-order-{action_number:04d}",
        },
        TraceStage.LATER_ADVERSE_SELECTION: {
            "horizon_us": 100_000,
            "markout_ticks": -1 if action_number == 1 else 1,
            "measurement": (
                "POST_FILL_MARKOUT" if action_number == 1 else "POST_CANCEL_MARKOUT"
            ),
        },
    }
    return payloads[stage]


def _run_id(label: str) -> str:
    return "run-" + _sha256(label)[:24]


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()
