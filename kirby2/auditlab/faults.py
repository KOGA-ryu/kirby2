"""Explicit, deterministic fault manifests and detector outcomes."""

from __future__ import annotations

from .models import FaultEvidence, FaultKind, GeneratedConfiguration


_EXPECTED_CODES = {
    FaultKind.DUPLICATE_MESSAGE: "DUPLICATE_MESSAGE_ID",
    FaultKind.DROPPED_MARKET_DATA: "MARKET_DATA_SEQUENCE_GAP",
    FaultKind.DELAYED_ACKNOWLEDGEMENT: "ACK_LATENCY_BUDGET_EXCEEDED",
    FaultKind.OUT_OF_ORDER_DELIVERY: "DELIVERY_SEQUENCE_REVERSAL",
    FaultKind.SNAPSHOT_GAP: "SNAPSHOT_INTERVAL_GAP",
    FaultKind.CORRUPTED_DATASET_ROW: "INVALID_DATASET_QUANTITY",
    FaultKind.VENUE_REJECTION: "VENUE_INSTRUCTION_REJECTED",
    FaultKind.HALT_DURING_PENDING_ORDER: "PENDING_ORDER_HALTED",
    FaultKind.CANCEL_FILL_RACE: "TERMINAL_RACE_CLASSIFIED",
    FaultKind.SCHEMA_MISMATCH: "UNSUPPORTED_SCHEMA_VERSION",
}


def inject_and_detect(configuration: GeneratedConfiguration) -> FaultEvidence | None:
    """Build a minimal ingress trace, inject one declared fault, and classify it."""

    fault = configuration.injected_fault
    if fault is None:
        return None
    detector, detected, details = _detector(fault, configuration)
    return FaultEvidence(
        fault=fault,
        detector=detector,
        expected_code=_EXPECTED_CODES[fault],
        detected_code=detected,
        injection_event=min(2, configuration.duration_events),
        details=details,
    )


def expected_fault_code(fault: FaultKind) -> str:
    return _EXPECTED_CODES[fault]


def _detector(
    fault: FaultKind,
    configuration: GeneratedConfiguration,
) -> tuple[str, str | None, dict[str, object]]:
    if fault is FaultKind.DUPLICATE_MESSAGE:
        ids = ("MSG-1", "MSG-2", "MSG-2")
        duplicate = next((value for i, value in enumerate(ids) if value in ids[:i]), None)
        return (
            "INGRESS_IDEMPOTENCY_GATE",
            _EXPECTED_CODES[fault] if duplicate is not None else None,
            {"duplicate_id": duplicate, "message_ids": list(ids)},
        )
    if fault is FaultKind.DROPPED_MARKET_DATA:
        sequences = (1, 2, 4)
        gaps = [
            [left + 1, right - 1]
            for left, right in zip(sequences, sequences[1:])
            if right != left + 1
        ]
        return (
            "MARKET_DATA_SEQUENCE_GATE",
            _EXPECTED_CODES[fault] if gaps else None,
            {"gaps": gaps, "sequences": list(sequences)},
        )
    if fault is FaultKind.DELAYED_ACKNOWLEDGEMENT:
        profile_budget = {
            "ZERO_LATENCY": 0,
            "LOW_LATENCY": 1_000,
            "NORMAL": 5_000,
            "STRESSED": 25_000,
            "UNSTABLE": 50_000,
        }[configuration.latency]
        observed = profile_budget + 1
        return (
            "ASYNCHRONOUS_ACK_DEADLINE_GATE",
            _EXPECTED_CODES[fault] if observed > profile_budget else None,
            {"budget_us": profile_budget, "observed_us": observed},
        )
    if fault is FaultKind.OUT_OF_ORDER_DELIVERY:
        source = (1, 2, 3)
        delivered = (1, 3, 2)
        reversal = delivered != tuple(sorted(delivered))
        return (
            "DELIVERY_MONOTONICITY_GATE",
            _EXPECTED_CODES[fault] if reversal else None,
            {"delivered_sequence": list(delivered), "source_sequence": list(source)},
        )
    if fault is FaultKind.SNAPSHOT_GAP:
        snapshots = (0, 1_000_000_000, 4_000_000_000)
        expected_ns = 1_000_000_000
        gaps = [
            right - left
            for left, right in zip(snapshots, snapshots[1:])
            if right - left > expected_ns
        ]
        return (
            "SNAPSHOT_CADENCE_GATE",
            _EXPECTED_CODES[fault] if gaps else None,
            {"expected_interval_ns": expected_ns, "observed_gaps_ns": gaps},
        )
    if fault is FaultKind.CORRUPTED_DATASET_ROW:
        row = {"price_ticks": 100, "quantity": -1, "row": 2}
        invalid = int(row["quantity"]) <= 0
        return (
            "NORMALIZED_DATASET_SCHEMA_GATE",
            _EXPECTED_CODES[fault] if invalid else None,
            {"rejected_row": row, "silent_repair": False},
        )
    if fault is FaultKind.VENUE_REJECTION:
        supported = {"LIMIT", "MARKET"}
        instruction = "POST_ONLY"
        return (
            "VENUE_CAPABILITY_GATE",
            _EXPECTED_CODES[fault] if instruction not in supported else None,
            {"instruction": instruction, "supported": sorted(supported)},
        )
    if fault is FaultKind.HALT_DURING_PENDING_ORDER:
        submit_us, halt_us, arrival_us = 100, 200, 300
        detected = submit_us < halt_us < arrival_us
        return (
            "SESSION_STATE_AT_ARRIVAL_GATE",
            _EXPECTED_CODES[fault] if detected else None,
            {
                "arrival_us": arrival_us,
                "halt_us": halt_us,
                "order_state": "PENDING",
                "submit_us": submit_us,
            },
        )
    if fault is FaultKind.CANCEL_FILL_RACE:
        cancel_arrival_us = 8_000 + (configuration.seed % 2) * 4_000
        fill_arrival_us = 10_000
        outcome = "CANCEL_WON" if cancel_arrival_us < fill_arrival_us else "FILL_BEFORE_CANCEL"
        return (
            "ASYNCHRONOUS_TERMINAL_RACE_GATE",
            _EXPECTED_CODES[fault] if cancel_arrival_us != fill_arrival_us else None,
            {
                "cancel_arrival_us": cancel_arrival_us,
                "fill_arrival_us": fill_arrival_us,
                "outcome": outcome,
            },
        )
    if fault is FaultKind.SCHEMA_MISMATCH:
        expected, actual = 1, 2
        return (
            "SCHEMA_VERSION_GATE",
            _EXPECTED_CODES[fault] if actual != expected else None,
            {"actual_schema_version": actual, "expected_schema_version": expected},
        )
    raise AssertionError(f"unhandled fault {fault}")
