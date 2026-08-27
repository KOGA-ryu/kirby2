"""Runtime acceptance audit for deterministic asynchronous order lifecycles."""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from kirby2.exchange import Side
from kirby2.latency import (
    AsyncOrderState,
    AsynchronousExecutionSession,
    LatencyComponent,
    LatencyDistributionSpec,
    LatencyEventType,
    LatencyCommand,
    LatencyProfileName,
    LatencyRecording,
    LatencySampler,
    get_latency_profile,
    replay_latency_recording,
    run_cancel_race,
)


@dataclass(frozen=True, slots=True)
class LatencyAuditCase:
    name: str
    evidence: dict[str, object]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence": self.evidence,
            "failures": list(self.failures),
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
        }


def audit_latency() -> tuple[LatencyAuditCase, ...]:
    cancel_wins = run_cancel_race("cancel-wins", seed=42)
    fill_wins = run_cancel_race("fill-wins", seed=42)
    return (
        _distribution_case(),
        _profile_case(),
        _lifecycle_timestamp_case(fill_wins),
        _cancel_wins_case(cancel_wins),
        _fill_wins_case(fill_wins),
        _replace_and_stale_quote_case(),
        _early_cancel_causality_case(),
        _partial_fill_cancel_case(),
        _partial_market_expiration_case(),
        _venue_rejection_case(),
    )


def _distribution_case() -> LatencyAuditCase:
    specifications = (
        LatencyDistributionSpec.fixed(100),
        LatencyDistributionSpec.uniform(50, 150),
        LatencyDistributionSpec.lognormal(25, 500, 4.5, 0.7),
        LatencyDistributionSpec.empirical((10, 20, 50, 200)),
    )
    global_state = random.getstate()
    first = LatencySampler(17)
    second = LatencySampler(17)
    third = LatencySampler(18)
    traces: list[list[int]] = []
    second_traces: list[list[int]] = []
    third_traces: list[list[int]] = []
    for index, spec in enumerate(specifications):
        traces.append(
            [
                first.sample(
                    LatencyComponent.UPLINK,
                    spec,
                    index,
                    f"audit:{index}:{draw}",
                )
                for draw in range(32)
            ]
        )
        second_traces.append(
            [
                second.sample(
                    LatencyComponent.UPLINK,
                    spec,
                    index,
                    f"audit:{index}:{draw}",
                )
                for draw in range(32)
            ]
        )
        third_traces.append(
            [
                third.sample(
                    LatencyComponent.UPLINK,
                    spec,
                    index,
                    f"audit:{index}:{draw}",
                )
                for draw in range(32)
            ]
        )
    failures: list[str] = []
    if traces != second_traces:
        failures.append("same seeded latency distributions diverged")
    if traces == third_traces:
        failures.append("different latency seed did not change sampled traces")
    if random.getstate() != global_state:
        failures.append("latency sampling mutated global random state")
    for spec, values in zip(specifications, traces, strict=True):
        if any(not spec.lower_us <= value <= spec.upper_us for value in values):
            failures.append(f"{spec.kind.value} escaped configured bounds")
    evidence = {
        "distribution_kinds": [spec.kind.value for spec in specifications],
        "draw_count": len(first.draws),
        "global_random_unchanged": random.getstate() == global_state,
        "same_seed_equal": traces == second_traces,
    }
    return LatencyAuditCase(
        "owned_seeded_fixed_uniform_lognormal_empirical",
        evidence,
        tuple(failures),
    )


def _profile_case() -> LatencyAuditCase:
    profiles = tuple(get_latency_profile(name) for name in LatencyProfileName)
    required_states = {
        "CREATED",
        "PENDING_NEW",
        "WORKING",
        "PARTIALLY_FILLED",
        "PENDING_CANCEL",
        "CANCELLED",
        "FILLED",
        "REJECTED",
        "EXPIRED",
    }
    failures: list[str] = []
    if any(set(profile.components) != set(LatencyComponent) for profile in profiles):
        failures.append("one or more profiles omit a required latency component")
    if any(not profile.simulator_only for profile in profiles):
        failures.append("profile name lost its simulator-only qualification")
    if {state.value for state in AsyncOrderState} != required_states:
        failures.append("asynchronous order lifecycle state inventory is incomplete")
    evidence = {
        "component_count": len(LatencyComponent),
        "lifecycle_states": sorted(required_states),
        "profiles": [profile.name.value for profile in profiles],
        "simulator_only": all(profile.simulator_only for profile in profiles),
    }
    return LatencyAuditCase(
        "all_simulator_profiles_configure_all_components",
        evidence,
        tuple(failures),
    )


def _lifecycle_timestamp_case(result) -> LatencyAuditCase:
    required = {
        LatencyEventType.MARKET_EVENT_OCCURRED,
        LatencyEventType.MARKET_DATA_PUBLISHED,
        LatencyEventType.CLIENT_RECEIVED_MARKET_DATA,
        LatencyEventType.UI_RENDERED_MARKET_STATE,
        LatencyEventType.KEY_PRESSED,
        LatencyEventType.CLIENT_CREATED_ORDER,
        LatencyEventType.ORDER_LEFT_CLIENT,
        LatencyEventType.GATEWAY_RECEIVED_ORDER,
        LatencyEventType.VENUE_RECEIVED_ORDER,
        LatencyEventType.VENUE_ACKNOWLEDGED_ORDER,
        LatencyEventType.FILL_OCCURRED,
        LatencyEventType.FILL_REPORT_LEFT_VENUE,
        LatencyEventType.CLIENT_RECEIVED_FILL,
        LatencyEventType.UI_DISPLAYED_FILL,
    }
    actual = {event.event_type for event in result.session.events}
    failures: list[str] = []
    if not required <= actual:
        failures.append("latency timeline omits one or more required timestamps")
    timestamps = result.order.timestamps
    ordered_keys = (
        "player_pressed_key_us",
        "client_created_order_us",
        "order_left_client_us",
        "gateway_received_order_us",
        "venue_received_us",
        "venue_acknowledged_us",
    )
    ordered = tuple(timestamps[key] for key in ordered_keys)
    if ordered != tuple(sorted(ordered)):
        failures.append("new-order communication timestamps are not causal")
    fill_keys = (
        "first_fill_occurred_us",
        "fill_report_left_venue_us",
        "client_received_fill_us",
        "ui_displayed_fill_us",
    )
    fill_ordered = tuple(timestamps[key] for key in fill_keys)
    if fill_ordered != tuple(sorted(fill_ordered)):
        failures.append("fill-report communication timestamps are not causal")
    evidence = {
        "event_types_present": sorted(item.value for item in required),
        "fill_timestamps_us": dict(zip(fill_keys, fill_ordered, strict=True)),
        "order_timestamps_us": dict(zip(ordered_keys, ordered, strict=True)),
    }
    return LatencyAuditCase(
        "separate_causal_market_order_and_fill_timestamps",
        evidence,
        tuple(failures),
    )


def _cancel_wins_case(result) -> LatencyAuditCase:
    failures: list[str] = []
    if result.order.state is not AsyncOrderState.CANCELLED:
        failures.append("cancel-wins order did not end cancelled")
    if result.order.filled_quantity or result.session.player_position:
        failures.append("cancel-wins race created a player fill or position")
    if result.order.cancel_race_outcome != "CANCEL_WON":
        failures.append("cancel-wins race outcome is mislabeled")
    if not result.replay.passed:
        failures.append("cancel-wins recording did not replay exactly")
    evidence = {
        "event_stream_sha256": result.session.event_stream_sha256(),
        "filled_quantity": result.order.filled_quantity,
        "outcome": result.order.cancel_race_outcome,
        "player_position": result.session.player_position,
        "recording_sha256": result.recording.sha256(),
        "replay_status": "PASS" if result.replay.passed else "FAIL",
        "state": result.order.state.value,
    }
    return LatencyAuditCase(
        "cancel_reaches_venue_before_aggressive_fill",
        evidence,
        tuple(failures),
    )


def _fill_wins_case(result) -> LatencyAuditCase:
    failures: list[str] = []
    timestamps = result.order.timestamps
    fill_before_cancel = (
        timestamps["first_fill_occurred_us"]
        < timestamps["venue_cancel_acknowledged_us"]
    )
    report_before_cancel_ack = (
        timestamps["client_received_fill_us"]
        < timestamps["client_received_cancel_ack_us"]
    )
    fill_position = sum(
        fill.side.sign * fill.quantity
        for fill in result.session.book.player_position.fills
    )
    if result.order.state is not AsyncOrderState.FILLED:
        failures.append("fill-wins order did not end filled")
    if result.order.cancel_race_outcome != "FILL_BEFORE_CANCEL":
        failures.append("fill-wins race outcome is mislabeled")
    if not fill_before_cancel or not report_before_cancel_ack:
        failures.append("fill/cancel acknowledgement race ordering is incorrect")
    if fill_position != result.session.player_position:
        failures.append("player position was not derived from exchange fills")
    if not result.replay.passed:
        failures.append("fill-wins recording did not replay exactly")
    evidence = {
        "cancel_ack_after_fill_report": report_before_cancel_ack,
        "event_stream_sha256": result.session.event_stream_sha256(),
        "fill_before_cancel_at_venue": fill_before_cancel,
        "metrics": result.metrics.as_dict(),
        "outcome": result.order.cancel_race_outcome,
        "player_position": result.session.player_position,
        "position_from_fills": fill_position,
        "recording_sha256": result.recording.sha256(),
        "replay_status": "PASS" if result.replay.passed else "FAIL",
        "state": result.order.state.value,
    }
    return LatencyAuditCase(
        "aggressive_fill_reaches_order_before_cancel",
        evidence,
        tuple(failures),
    )


def _replace_and_stale_quote_case() -> LatencyAuditCase:
    profile = get_latency_profile(LatencyProfileName.NORMAL)
    replace_session = AsynchronousExecutionSession(seed=7, profile=profile)
    replace_session.advance_to(2_000)
    order_id = replace_session.request_limit(
        Side.BUY,
        100,
        99,
        order_id="PENDING-ORIGINAL",
    )
    replacement = replace_session.request_replace(
        order_id,
        quantity=200,
        price_ticks=99,
    )
    rejected_before_ack = any(
        event.event_type is LatencyEventType.REPLACE_REJECTED_BEFORE_ACK
        for event in replace_session.events
    )

    stale_session = AsynchronousExecutionSession(seed=9, profile=profile)
    stale_session.advance_to(2_000)
    stale_id = stale_session.request_market(
        Side.BUY,
        100,
        order_id="STALE-MARKET-BUY",
    )
    stale_session.schedule_liquidity_reprice(
        3_000,
        target_order_id="ASYNC-SIM-ASK-1",
        new_order_id="ASYNC-SIM-ASK-2",
        side=Side.SELL,
        quantity=100,
        price_ticks=103,
    )
    stale_session.advance_to(8_000)
    stale_session.assert_invariants()
    metrics = stale_session.metrics(stale_id)
    failures: list[str] = []
    if replacement is not None or not rejected_before_ack:
        failures.append("replace-before-ack race was not rejected explicitly")
    if not metrics.execution_against_stale_quote:
        failures.append("market order against moved liquidity was not marked stale")
    if Decimal(metrics.latency_induced_slippage_ticks or "0") != Decimal("2"):
        failures.append("latency-induced market slippage did not reconcile")
    evidence = {
        "latency_induced_slippage_ticks": metrics.latency_induced_slippage_ticks,
        "observed_quote_age_us": metrics.observed_quote_age_us,
        "replace_before_ack_rejected": rejected_before_ack,
        "stale_quote_execution": metrics.execution_against_stale_quote,
        "venue_execution_time_us": metrics.venue_execution_time_us,
    }
    return LatencyAuditCase(
        "replace_before_ack_and_market_order_stale_quote",
        evidence,
        tuple(failures),
    )


def _early_cancel_causality_case() -> LatencyAuditCase:
    session = AsynchronousExecutionSession(
        seed=0,
        profile=get_latency_profile(LatencyProfileName.LOW_LATENCY),
    )
    commands = (
        LatencyCommand(
            1,
            500,
            "LIMIT",
            {
                "order_id": "EARLY-CANCEL",
                "price_ticks": 99,
                "quantity": 100,
                "side": "buy",
            },
        ),
        LatencyCommand(
            2,
            500,
            "CANCEL",
            {
                "cancel_id": "ASYNC-CANCEL-000001",
                "target_order_id": "EARLY-CANCEL",
            },
        ),
    )
    session.advance_to(500)
    session.request_limit(Side.BUY, 100, 99, order_id="EARLY-CANCEL")
    session.request_cancel("EARLY-CANCEL")
    session.advance_to(3_000)
    session.assert_invariants()
    recording = LatencyRecording.capture(session, commands)
    replay = replay_latency_recording(recording)
    order = next(item for item in session.orders if item.order_id == "EARLY-CANCEL")
    held = any(
        event.event_type is LatencyEventType.CANCEL_HELD_PENDING_NEW
        for event in session.events
    )
    failures: list[str] = []
    if not held:
        failures.append("fast cancel did not overtake and exercise the gateway hold")
    if order.state is not AsyncOrderState.CANCELLED:
        failures.append("gateway-held early cancel did not cancel acknowledged order")
    if order.cancel_race_outcome != "CANCEL_WON":
        failures.append("gateway-held early cancel outcome did not reconcile")
    if not replay.passed:
        failures.append("gateway-held early cancel did not replay exactly")
    evidence = {
        "cancel_held_pending_new": held,
        "event_stream_sha256": session.event_stream_sha256(),
        "final_state": order.state.value,
        "outcome": order.cancel_race_outcome,
        "replay_status": "PASS" if replay.passed else "FAIL",
    }
    return LatencyAuditCase(
        "early_cancel_preserves_new_before_cancel_causality",
        evidence,
        tuple(failures),
    )


def _partial_fill_cancel_case() -> LatencyAuditCase:
    session = AsynchronousExecutionSession(
        seed=42,
        profile=get_latency_profile(LatencyProfileName.NORMAL),
    )
    commands = (
        LatencyCommand(
            1,
            2_000,
            "LIMIT",
            {
                "order_id": "PARTIAL-CANCEL",
                "price_ticks": 99,
                "quantity": 100,
                "side": "buy",
            },
        ),
        LatencyCommand(
            2,
            6_000,
            "CANCEL",
            {
                "cancel_id": "ASYNC-CANCEL-000001",
                "target_order_id": "PARTIAL-CANCEL",
            },
        ),
        LatencyCommand(
            3,
            8_000,
            "EXTERNAL_MARKET",
            {
                "order_id": "PARTIAL-AGGRESSOR",
                "quantity": 150,
                "side": "sell",
            },
        ),
    )
    session.advance_to(2_000)
    session.request_limit(Side.BUY, 100, 99, order_id="PARTIAL-CANCEL")
    session.advance_to(6_000)
    session.request_cancel("PARTIAL-CANCEL")
    session.advance_to(8_000)
    session.schedule_aggressive_order(
        8_000,
        Side.SELL,
        150,
        order_id="PARTIAL-AGGRESSOR",
    )
    session.advance_to(15_000)
    session.assert_invariants()
    recording = LatencyRecording.capture(session, commands)
    replay = replay_latency_recording(recording)
    order = next(
        item for item in session.orders if item.order_id == "PARTIAL-CANCEL"
    )
    failures: list[str] = []
    if order.state is not AsyncOrderState.CANCELLED:
        failures.append("partially filled order did not cancel its remainder")
    if order.filled_quantity != 50 or order.cancelled_quantity != 50:
        failures.append("partial fill did not reconcile to 50 filled and 50 cancelled")
    if order.cancel_race_outcome != "PARTIAL_FILL_THEN_CANCELLED":
        failures.append("partial-fill cancel race outcome is ambiguous")
    if session.player_position != 50 or session.client_position != 50:
        failures.append("partial-fill player/client positions did not reconcile")
    if not replay.passed:
        failures.append("partial-fill cancel race did not replay exactly")
    evidence = {
        "client_position": session.client_position,
        "filled_quantity": order.filled_quantity,
        "outcome": order.cancel_race_outcome,
        "player_position": session.player_position,
        "cancelled_quantity": order.cancelled_quantity,
        "remaining_quantity": order.remaining_quantity,
        "replay_status": "PASS" if replay.passed else "FAIL",
        "state": order.state.value,
    }
    return LatencyAuditCase(
        "partial_fill_while_cancel_travels",
        evidence,
        tuple(failures),
    )


def _partial_market_expiration_case() -> LatencyAuditCase:
    session = AsynchronousExecutionSession(
        seed=42,
        profile=get_latency_profile(LatencyProfileName.NORMAL),
    )
    commands = (
        LatencyCommand(
            1,
            2_000,
            "MARKET",
            {
                "order_id": "PARTIAL-EXPIRE",
                "quantity": 150,
                "side": "buy",
            },
        ),
    )
    session.advance_to(2_000)
    session.request_market(Side.BUY, 150, order_id="PARTIAL-EXPIRE")
    session.advance_to(10_000)
    session.assert_invariants()
    recording = LatencyRecording.capture(session, commands)
    replay = replay_latency_recording(recording)
    order = session.orders[0]
    failures: list[str] = []
    if order.state is not AsyncOrderState.EXPIRED:
        failures.append("partially unfilled market order did not expire")
    if (
        order.filled_quantity != 100
        or order.expired_quantity != 50
        or order.remaining_quantity != 0
    ):
        failures.append("market expiration quantities do not conserve original size")
    if session.player_position != 100 or session.client_position != 100:
        failures.append("expired market order position does not reconcile to fills")
    if not replay.passed:
        failures.append("partial market expiration did not replay exactly")
    evidence = {
        "client_position": session.client_position,
        "expired_quantity": order.expired_quantity,
        "filled_quantity": order.filled_quantity,
        "player_position": session.player_position,
        "remaining_quantity": order.remaining_quantity,
        "replay_status": "PASS" if replay.passed else "FAIL",
        "state": order.state.value,
    }
    return LatencyAuditCase(
        "partially_filled_market_remainder_expires",
        evidence,
        tuple(failures),
    )


def _venue_rejection_case() -> LatencyAuditCase:
    session = AsynchronousExecutionSession(
        seed=42,
        profile=get_latency_profile(LatencyProfileName.NORMAL),
    )
    commands = (
        LatencyCommand(
            1,
            2_000,
            "LIMIT",
            {
                "order_id": "FUTURE-COLLISION",
                "price_ticks": 99,
                "quantity": 100,
                "side": "buy",
            },
        ),
        LatencyCommand(
            2,
            3_000,
            "EXTERNAL_REPRICE",
            {
                "new_order_id": "FUTURE-COLLISION",
                "price_ticks": 103,
                "quantity": 100,
                "side": "sell",
                "target_order_id": "ASYNC-SIM-ASK-1",
            },
        ),
        LatencyCommand(
            3,
            5_000,
            "CANCEL",
            {
                "cancel_id": "ASYNC-CANCEL-000001",
                "target_order_id": "FUTURE-COLLISION",
            },
        ),
    )
    session.advance_to(2_000)
    session.request_limit(Side.BUY, 100, 99, order_id="FUTURE-COLLISION")
    session.advance_to(3_000)
    session.schedule_liquidity_reprice(
        3_000,
        target_order_id="ASYNC-SIM-ASK-1",
        new_order_id="FUTURE-COLLISION",
        side=Side.SELL,
        quantity=100,
        price_ticks=103,
    )
    session.advance_to(5_000)
    session.request_cancel("FUTURE-COLLISION")
    session.advance_to(12_000)
    session.assert_invariants()
    recording = LatencyRecording.capture(session, commands)
    replay = replay_latency_recording(recording)
    order = session.orders[0]
    rejected = any(
        event.event_type is LatencyEventType.VENUE_REJECTED_ORDER
        for event in session.events
    )
    failures: list[str] = []
    if order.state is not AsyncOrderState.REJECTED or not rejected:
        failures.append("venue identity collision did not reject pending order")
    if "venue_rejected_us" not in order.timestamps:
        failures.append("venue rejection timestamp is absent")
    if order.filled_quantity or session.player_position:
        failures.append("rejected order changed fills or player position")
    collision_order_active = "FUTURE-COLLISION" in session.book.active_orders
    if not collision_order_active:
        failures.append("late cancel removed another owner's colliding order ID")
    if not replay.passed:
        failures.append("venue rejection did not replay exactly")
    evidence = {
        "filled_quantity": order.filled_quantity,
        "player_position": session.player_position,
        "rejection_event": rejected,
        "replay_status": "PASS" if replay.passed else "FAIL",
        "simulated_collision_order_active": collision_order_active,
        "state": order.state.value,
        "venue_rejected_us": order.timestamps.get("venue_rejected_us"),
    }
    return LatencyAuditCase(
        "venue_rejects_future_order_id_collision",
        evidence,
        tuple(failures),
    )
