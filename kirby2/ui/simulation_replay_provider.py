"""Qt-independent WO36 Replay provider for verified simulation artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from kirby2.microscope.data_age import EvidenceTimestamp, EvidenceTiming
from kirby2.microscope.overlays import (
    OverlayInputSelection,
    build_overlay_set,
    build_overlay_window_projection,
)
from kirby2.microscope.panes import build_synchronized_panes
from kirby2.microscope.query import (
    ObservationQueryRequest,
    ObservationQueryResult,
    ObservedEvidenceSet,
    ObservedValueRecord,
    RecordDisposition,
    SelectionKind,
    query_as_observed,
)
from kirby2.microscope.report import (
    ClockPresentation,
    ClockTimeBasis,
    InstrumentPresentation,
    PresentationMetadataAuthority,
    RecordingPresentation,
    ReplayPresentationContext,
    ReplayPresentationFrameV1,
    ReportPresentation,
    build_replay_presentation_frame,
)
from kirby2.microscope.timeline import (
    ReplayTimeline,
    TimelineCursor,
    TimelineDirection,
    TimelineJumpTarget,
    TimelineNavigationAvailability,
    TimelinePlaybackState,
    build_replay_timeline,
    timeline_event_from_query_result,
)
from kirby2.session.live import SessionSnapshot

from .simulation_replay_facade import _VerifiedSimulationReplaySource


_REQUEST_ID = re.compile(r"replay-request-[0-9]{8,}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:[-_.:][A-Za-z0-9]+)*\Z")


@dataclass(frozen=True, slots=True)
class _EvidenceCandidate:
    plane: str
    series_id: str
    event_id: str
    source_time_us: int
    knowledge_time_us: int
    payload: object
    disposition: RecordDisposition = RecordDisposition.VALUE


@dataclass(frozen=True, slots=True)
class _IssuedReplayFrame:
    frame: ReplayPresentationFrameV1
    cursor: TimelineCursor


def _observed_timing(source_time_us: int, knowledge_time_us: int) -> EvidenceTiming:
    return EvidenceTiming(
        source_event_time_us=source_time_us,
        venue_receipt=EvidenceTimestamp.recorded(source_time_us),
        client_receive=EvidenceTimestamp.recorded(knowledge_time_us),
        client_knowledge=EvidenceTimestamp.recorded(knowledge_time_us),
    )


def _book_pairs(snapshot: SessionSnapshot) -> tuple[list[list[int]], list[list[int]]]:
    bids = [
        [level.price_ticks, level.aggregate_quantity]
        for level in snapshot.bids
        if level.aggregate_quantity > 0
    ]
    asks = [
        [level.price_ticks, level.aggregate_quantity]
        for level in snapshot.asks
        if level.aggregate_quantity > 0
    ]
    return bids, asks


def _order_identifier(value: str) -> bool:
    return _IDENTIFIER.fullmatch(value) is not None


def _snapshot_candidates(
    snapshots: tuple[SessionSnapshot, ...],
) -> list[_EvidenceCandidate]:
    candidates: list[_EvidenceCandidate] = []
    seen_trades: set[str] = set()
    active_orders: set[str] = set()
    prior_position: int | None = None
    prior_signal: str | None = None
    order_event_sequence = 0
    trade_event_sequence = 0
    for index, snapshot in enumerate(snapshots):
        time_us = snapshot.simulation_time_us
        bids, asks = _book_pairs(snapshot)
        candidates.append(
            _EvidenceCandidate(
                "CLIENT",
                "book.level2.snapshot",
                f"simulation-book-{index:08d}",
                snapshot.market_state_time_us,
                time_us,
                {"record_kind": "LEVEL_2", "bids": bids, "asks": asks},
            )
        )
        depth_rows = sorted((*bids, *asks), key=lambda item: item[0])
        candidates.append(
            _EvidenceCandidate(
                "CLIENT",
                "depth.heatmap.snapshot",
                f"simulation-depth-{index:08d}",
                snapshot.market_state_time_us,
                time_us,
                {"record_kind": "DEPTH_HEATMAP", "rows": depth_rows},
            )
        )
        if bids and asks:
            candidates.append(
                _EvidenceCandidate(
                    "CLIENT",
                    "market.top-of-book.snapshot",
                    f"simulation-top-book-{index:08d}",
                    snapshot.market_state_time_us,
                    time_us,
                    {
                        "record_role": "TOP_OF_BOOK",
                        "best_bid_ticks": bids[0][0],
                        "best_bid_size": bids[0][1],
                        "best_ask_ticks": asks[0][0],
                        "best_ask_size": asks[0][1],
                    },
                )
            )
        if snapshot.position != prior_position:
            candidates.append(
                _EvidenceCandidate(
                    "CLIENT",
                    "position.player",
                    f"simulation-position-{index:08d}",
                    time_us,
                    time_us,
                    {"quantity": snapshot.position},
                )
            )
            prior_position = snapshot.position
        if snapshot.traffic_light in {"GREEN", "RED", "WAIT"} and (
            snapshot.traffic_light != prior_signal
        ):
            candidates.append(
                _EvidenceCandidate(
                    "DECISION",
                    "strategy.signal",
                    f"simulation-signal-{index:08d}",
                    time_us,
                    time_us,
                    {"recorded_signal": snapshot.traffic_light},
                )
            )
            prior_signal = snapshot.traffic_light
        for trade in snapshot.tape:
            if trade.trade_id in seen_trades:
                continue
            seen_trades.add(trade.trade_id)
            trade_event_sequence += 1
            identity = f"simulation-{trade_event_sequence:08d}"
            candidates.extend(
                (
                    _EvidenceCandidate(
                        "CLIENT",
                        f"trade.{identity}",
                        f"simulation-trade-{trade_event_sequence:08d}",
                        trade.simulation_time_us,
                        time_us,
                        {
                            "price_ticks": trade.price_ticks,
                            "quantity": trade.quantity,
                        },
                    ),
                    _EvidenceCandidate(
                        "CLIENT",
                        f"market.trade.{identity}",
                        f"simulation-overlay-trade-{trade_event_sequence:08d}",
                        trade.simulation_time_us,
                        time_us,
                        {
                            "record_role": "TRADE",
                            "price_ticks": trade.price_ticks,
                            "quantity": trade.quantity,
                        },
                    ),
                )
            )
        current_orders = {
            item.order_id for item in snapshot.working_orders if _order_identifier(item.order_id)
        }
        for order_id in sorted(current_orders - active_orders):
            order_event_sequence += 1
            candidates.append(
                _EvidenceCandidate(
                    "CLIENT",
                    f"order.{order_id}",
                    f"simulation-order-{order_event_sequence:08d}",
                    time_us,
                    time_us,
                    {"order_id": order_id, "state": "WORKING"},
                )
            )
        for order_id in sorted(active_orders - current_orders):
            order_event_sequence += 1
            candidates.append(
                _EvidenceCandidate(
                    "CLIENT",
                    f"order.{order_id}",
                    f"simulation-order-{order_event_sequence:08d}",
                    time_us,
                    time_us,
                    None,
                    RecordDisposition.TOMBSTONE,
                )
            )
        active_orders = current_orders
    return candidates


def _artifact_event_candidates(
    source: _VerifiedSimulationReplaySource,
) -> list[_EvidenceCandidate]:
    candidates: list[_EvidenceCandidate] = []
    for record in source.recording.input_records:
        command = record.resolved_command or ""
        if "buy" in command:
            side = "BUY"
        elif "sell" in command:
            side = "SELL"
        else:
            continue
        candidates.append(
            _EvidenceCandidate(
                "DECISION",
                "order.client-intention",
                f"simulation-action-{record.sequence:08d}",
                record.simulation_time_us,
                record.simulation_time_us,
                {
                    "side": side,
                    "venue_state": "RECEIVED" if record.accepted else "NOT_OBSERVED",
                },
            )
        )
    artifact = source.artifact.as_dict()
    events = artifact["event_tape"]
    if type(events) is not list:  # pragma: no cover - verified source invariant
        raise RuntimeError("verified simulation Replay source lost its event tape")
    for event in events:
        if type(event) is not dict:  # pragma: no cover - verified source invariant
            raise RuntimeError("verified simulation Replay event lost its object shape")
        kind = event["kind"]
        data = event["data"]
        if kind not in {"FILL", "PARTIAL_FILL", "CANCEL", "REPLACE"}:
            continue
        if type(data) is not dict:  # pragma: no cover - verified source invariant
            raise RuntimeError("verified simulation Replay event data is not detached")
        event_sequence = int(event["sequence"])
        time_us = int(event["simulation_time_us"])
        if kind in {"FILL", "PARTIAL_FILL"}:
            filled_quantity = data.get("fill_quantity")
            order_id = data.get("order_id")
            if type(filled_quantity) is int and filled_quantity >= 0:
                candidates.append(
                    _EvidenceCandidate(
                        "CLIENT",
                        f"fill.simulation-{event_sequence:08d}",
                        f"simulation-fill-{event_sequence:08d}",
                        time_us,
                        time_us,
                        {"filled_quantity": filled_quantity},
                    )
                )
            state = "FILLED" if kind == "FILL" else "PARTIALLY_FILLED"
            if type(order_id) is str and _order_identifier(order_id):
                candidates.append(
                    _EvidenceCandidate(
                        "CLIENT",
                        f"lifecycle.{order_id}",
                        f"simulation-lifecycle-{event_sequence:08d}",
                        time_us,
                        time_us,
                        {"order_id": order_id, "state": state},
                    )
                )
        elif kind == "CANCEL":
            order_id = data.get("order_id")
            if type(order_id) is str and _order_identifier(order_id):
                candidates.append(
                    _EvidenceCandidate(
                        "CLIENT",
                        f"lifecycle.{order_id}",
                        f"simulation-lifecycle-{event_sequence:08d}",
                        time_us,
                        time_us,
                        {"order_id": order_id, "state": "CANCELLED"},
                    )
                )
        else:
            order_id = data.get("old_order_id")
            if type(order_id) is str and _order_identifier(order_id):
                candidates.append(
                    _EvidenceCandidate(
                        "CLIENT",
                        f"lifecycle.{order_id}",
                        f"simulation-lifecycle-{event_sequence:08d}",
                        time_us,
                        time_us,
                        {"order_id": order_id, "state": "REPLACED"},
                    )
                )
    return candidates


def _build_observed_evidence(
    source: _VerifiedSimulationReplaySource,
) -> ObservedEvidenceSet:
    candidates = [
        *_snapshot_candidates(source.reconstruction.snapshots),
        *_artifact_event_candidates(source),
    ]
    candidates.sort(
        key=lambda item: (
            item.knowledge_time_us,
            item.source_time_us,
            item.event_id,
        )
    )
    delivered: list[ObservedValueRecord] = []
    decisions: list[ObservedValueRecord] = []
    for sequence, candidate in enumerate(candidates, start=1):
        value = ObservedValueRecord(
            series_id=candidate.series_id,
            event_id=candidate.event_id,
            sequence=sequence,
            timing=_observed_timing(
                candidate.source_time_us,
                candidate.knowledge_time_us,
            ),
            payload=candidate.payload,
            disposition=candidate.disposition,
        )
        (decisions if candidate.plane == "DECISION" else delivered).append(value)
    return ObservedEvidenceSet(
        source_run_id=source.artifact.replay_run_id,
        source_event_sha256=source.artifact_ref.artifact_sha256,
        client_delivered=tuple(delivered),
        decision_snapshots=tuple(decisions),
    )


def _queries_by_event_time(
    evidence: ObservedEvidenceSet,
) -> dict[int, ObservationQueryResult]:
    times = {
        record.timing.client_knowledge_time_us
        for record in (*evidence.client_delivered, *evidence.decision_snapshots)
    }
    if None in times:  # pragma: no cover - ObservedEvidenceSet invariant
        raise RuntimeError("simulation Replay evidence lost a knowledge timestamp")
    return {
        time_us: query_as_observed(evidence, ObservationQueryRequest(time_us))
        for time_us in sorted(times)
        if time_us is not None
    }


def _presentation_context(
    source: _VerifiedSimulationReplaySource,
    cursor_time_us: int,
) -> ReplayPresentationContext:
    artifact = source.artifact.as_dict()
    frame = source.artifact.final_frame.as_dict()
    instrument = frame["instrument"]
    selection = artifact["selection"]
    if type(instrument) is not dict or type(selection) is not dict:
        raise RuntimeError("verified simulation Replay presentation fields disappeared")
    profile_ref = selection["profile_ref"]
    if type(profile_ref) is not dict:
        raise RuntimeError("verified simulation Replay profile reference disappeared")
    profile_id = str(profile_ref["profile_id"])
    artifact_sha256 = source.artifact_ref.artifact_sha256
    return ReplayPresentationContext(
        source_run_id=source.artifact.replay_run_id,
        source_event_sha256=artifact_sha256,
        metadata_authority=(
            PresentationMetadataAuthority.SOURCE_BOUND_DISPLAY_DECLARATION
        ),
        recording=RecordingPresentation(
            recording_id=f"simulation-recording-{artifact_sha256[:24]}",
            display_name=f"Kirby2 synthetic simulation: {profile_id}",
            content_sha256=artifact_sha256,
        ),
        report=ReportPresentation(
            summary=f"Deterministic synthetic simulation replay for {profile_id}."
        ),
        clock=ClockPresentation(
            time_basis=ClockTimeBasis.SIMULATION_TIME,
            session_origin_time_us=0,
            display_precision_us=1,
            cursor_label=f"T+{cursor_time_us}us",
        ),
        instrument=InstrumentPresentation(
            instrument_id=str(instrument["instrument_id"]),
            symbol=str(instrument["symbol"]),
            display_name=str(instrument["display_name"]),
            venue_labels=tuple(str(item) for item in instrument["venue_labels"]),
            currency="USD",
            tick_numerator=int(instrument["tick_numerator"]),
            tick_denominator=int(instrument["tick_denominator"]),
            price_precision=int(instrument["price_precision"]),
            quantity_unit="shares",
            lot_size=int(instrument["lot_size"]),
        ),
        limitations=(
            "Exact hidden queue truth and agent activity are unavailable in AS_OBSERVED mode.",
            "Replay is limited to states reconstructed from the finalized artifact.",
            "Synthetic simulation evidence only; no real-market data.",
        ),
    )


class _SimulationReplayProvider:
    """Private stateful adapter exposing detached Replay transport records only."""

    __slots__ = (
        "_evidence",
        "_event_queries",
        "_issued",
        "_source",
        "_timeline",
    )

    def __init__(self, source: _VerifiedSimulationReplaySource) -> None:
        self._source = source
        self._evidence = _build_observed_evidence(source)
        queries = _queries_by_event_time(self._evidence)
        terminal_time_us = source.recording.completed_time_us
        root_query = query_as_observed(
            self._evidence,
            ObservationQueryRequest(terminal_time_us),
        )
        events = []
        for query in queries.values():
            for value in query.values:
                if (
                    value.selection is SelectionKind.EXACT_RECORDED
                    and value.data_age.policy_visible_at_time_us
                    == query.request.render_cursor_time_us
                ):
                    events.append(timeline_event_from_query_result(query, value.event_id))
        self._timeline, _receipt = build_replay_timeline(root_query, tuple(events))
        self._event_queries = queries
        self._issued: dict[str, _IssuedReplayFrame] = {}
        self._issue(self._timeline.cursor(0))

    def __reduce__(self) -> object:
        raise TypeError("simulation Replay providers are not serializable")

    def initial_frame(self) -> dict[str, object]:
        cursor = self._timeline.cursor(0)
        return self._issue(cursor).frame.as_dict()

    def respond(self, request_payload: Mapping[str, object]) -> dict[str, object]:
        request_id, source_generation, origin, command = self._request(request_payload)
        operation = command["operation"]
        if operation in {"PLAY", "PAUSE"}:
            cursor = (
                self._timeline.play(origin.cursor)
                if operation == "PLAY"
                else self._timeline.pause(origin.cursor)
            )
            frame = self._issue(cursor).frame.as_dict()
            return {
                "request_id": request_id,
                "source_generation": source_generation,
                "kind": "PLAYBACK",
                "navigation_payload": None,
                "frame_payload": frame,
            }
        direction = TimelineDirection(str(command["direction"]))
        if operation == "EVENT_STEP":
            navigation = self._timeline.step_event(origin.cursor, direction)
        elif operation == "FIXED_TIME_STEP":
            navigation = self._timeline.step_fixed_time(
                origin.cursor,
                int(command["fixed_step_us"]),
                direction,
            )
        else:
            navigation = self._timeline.jump(
                origin.cursor,
                TimelineJumpTarget(str(command["jump_target"])),
                direction,
            )
        frame_payload = None
        if navigation.availability is TimelineNavigationAvailability.AVAILABLE:
            frame_payload = self._issue(navigation.cursor).frame.as_dict()
        return {
            "request_id": request_id,
            "source_generation": source_generation,
            "kind": "NAVIGATION",
            "navigation_payload": navigation.as_dict(),
            "frame_payload": frame_payload,
        }

    def _issue(self, cursor: TimelineCursor) -> _IssuedReplayFrame:
        query = query_as_observed(
            self._evidence,
            ObservationQueryRequest(cursor.render_cursor_time_us),
        )
        event_queries = tuple(
            value
            for time_us, value in self._event_queries.items()
            if time_us <= cursor.render_cursor_time_us
        )
        panes = build_synchronized_panes(query)
        projection, _receipt = build_overlay_window_projection(query, event_queries)
        top_event_ids = tuple(
            record.event_id
            for record in self._evidence.client_delivered
            if record.series_id == "market.top-of-book.snapshot"
            and record.timing.client_knowledge_time_us is not None
            and record.timing.client_knowledge_time_us <= cursor.render_cursor_time_us
        )
        trade_event_ids = tuple(
            record.event_id
            for record in self._evidence.client_delivered
            if record.series_id.startswith("market.trade.")
            and record.timing.client_knowledge_time_us is not None
            and record.timing.client_knowledge_time_us <= cursor.render_cursor_time_us
        )
        overlays = build_overlay_set(
            query,
            projection,
            OverlayInputSelection(
                top_of_book_event_ids=top_event_ids,
                trade_event_ids=trade_event_ids,
            ),
        )
        frame = build_replay_presentation_frame(
            self._timeline,
            cursor,
            query,
            panes,
            overlays,
            _presentation_context(self._source, cursor.render_cursor_time_us),
            source_queries=event_queries,
        )
        issued = _IssuedReplayFrame(frame, cursor)
        self._issued[frame.frame_id] = issued
        return issued

    def _request(
        self,
        payload: Mapping[str, object],
    ) -> tuple[str, int, _IssuedReplayFrame, dict[str, object]]:
        if type(payload) is not dict or set(payload) != {
            "request_id",
            "source_generation",
            "origin",
            "command",
        }:
            raise ValueError("Replay provider request fields differ from V1")
        request_id = payload["request_id"]
        source_generation = payload["source_generation"]
        origin_value = payload["origin"]
        command_value = payload["command"]
        if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("Replay provider request ID is invalid")
        if type(source_generation) is not int or source_generation < 0:
            raise ValueError("Replay provider source generation is invalid")
        if type(origin_value) is not dict or set(origin_value) != {
            "cursor_id",
            "frame_id",
            "observation_mode",
            "policy_id",
            "render_cursor_time_us",
            "source_event_sha256",
            "source_run_id",
            "timeline_id",
        }:
            raise ValueError("Replay provider origin fields differ from V1")
        frame_id = origin_value["frame_id"]
        if type(frame_id) is not str or frame_id not in self._issued:
            raise ValueError("Replay provider origin frame was not issued here")
        origin = self._issued[frame_id]
        identity = origin.frame.as_dict()["identity"]
        cursor_record = origin.cursor.as_dict()
        if type(identity) is not dict:
            raise RuntimeError("issued Replay frame lost its identity")
        expected_origin = {
            "cursor_id": origin.cursor.cursor_id,
            "frame_id": origin.frame.frame_id,
            "observation_mode": identity["observation_mode"],
            "policy_id": identity["policy_id"],
            "render_cursor_time_us": origin.cursor.render_cursor_time_us,
            "source_event_sha256": origin.cursor.source_event_sha256,
            "source_run_id": origin.cursor.source_run_id,
            "timeline_id": origin.cursor.timeline_id,
        }
        if origin_value != expected_origin or cursor_record != origin.frame.as_dict()["cursor"]:
            raise ValueError("Replay provider request origin is stale or inconsistent")
        if type(command_value) is not dict or set(command_value) != {
            "direction",
            "fixed_step_us",
            "jump_target",
            "operation",
        }:
            raise ValueError("Replay provider command fields differ from V1")
        operation = command_value["operation"]
        direction = command_value["direction"]
        fixed_step_us = command_value["fixed_step_us"]
        jump_target = command_value["jump_target"]
        if operation in {"PLAY", "PAUSE"}:
            valid = direction is None and fixed_step_us is None and jump_target is None
        elif operation == "EVENT_STEP":
            valid = direction in {"PREVIOUS", "NEXT"} and (
                fixed_step_us is None and jump_target is None
            )
        elif operation == "FIXED_TIME_STEP":
            valid = direction in {"PREVIOUS", "NEXT"} and (
                type(fixed_step_us) is int
                and fixed_step_us > 0
                and jump_target is None
            )
        elif operation == "JUMP":
            valid = direction in {"PREVIOUS", "NEXT"} and (
                fixed_step_us is None
                and type(jump_target) is str
                and jump_target in {item.value for item in TimelineJumpTarget}
            )
        else:
            valid = False
        if not valid:
            raise ValueError("Replay provider command is invalid")
        return request_id, source_generation, origin, command_value


def build_replay_provider(source_value: object) -> object:
    """Build one opaque, synchronous Replay provider from a verified source."""

    if type(source_value) is not _VerifiedSimulationReplaySource:
        raise TypeError("Replay provider requires a verified Replay source handle")
    return _SimulationReplayProvider(source_value)


__all__ = ["build_replay_provider"]
