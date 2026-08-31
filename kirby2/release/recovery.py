"""Truthful startup decisions and deterministic restoration for live sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from kirby2.curriculum.models import CurriculumDrill
from kirby2.immutable import thaw_json
from kirby2.research.paths import DataPaths
from kirby2.session.bindings import BindingMap
from kirby2.session.journal import (
    LiveSessionCheckpointV1,
    LiveSessionJournalRecordV1,
    LiveSessionJournalV1,
    LiveSessionSourceV1,
    require_recovery_state_matches,
    restore_recovery_checkpoint_value_v1,
)
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import LiveMarketSession
from kirby2.session.objectives import SessionObjective
from kirby2.session.records import (
    InputRecord,
    MarketStateRecord,
    RecoveryBoundaryKindV1,
)
from kirby2.session.replay import (
    RECORDING_SCHEMA_VERSION,
    SessionRecording,
    replay_recording,
)
from kirby2.simulation import LiquidityPreset, VolumePreset


class RecoveryDispositionV1(str, Enum):
    NO_RECOVERY = "NO_RECOVERY"
    EXACT_CONTINUATION = "EXACT_CONTINUATION"
    SAFE_REPLAY_ONLY = "SAFE_REPLAY_ONLY"
    ABANDON_ONLY = "ABANDON_ONLY"


class RecoveryActionV1(str, Enum):
    START_NEW = "START_NEW"
    CONTINUE_EXACT = "CONTINUE_EXACT"
    REPLAY_SAFE = "REPLAY_SAFE"
    ABANDON = "ABANDON"


class RecoveryReasonCodeV1(str, Enum):
    NO_ACTIVE_SESSION = "NO_ACTIVE_SESSION"
    COMPLETE_DURABLE_CUT = "COMPLETE_DURABLE_CUT"
    JOURNAL_CORRUPT = "JOURNAL_CORRUPT"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    CHECKPOINT_MISSING = "CHECKPOINT_MISSING"
    CHECKPOINT_CORRUPT = "CHECKPOINT_CORRUPT"
    ACTION_ACKNOWLEDGEMENT_PENDING = "ACTION_ACKNOWLEDGEMENT_PENDING"
    CLIENT_ACKNOWLEDGEMENT_PENDING = "CLIENT_ACKNOWLEDGEMENT_PENDING"
    PACK_ACTIVATION_PENDING = "PACK_ACTIVATION_PENDING"
    PROFILE_UPDATE_PENDING = "PROFILE_UPDATE_PENDING"
    EXTERNAL_STATE_SUFFIX = "EXTERNAL_STATE_SUFFIX"
    SUFFIX_INVALID = "SUFFIX_INVALID"
    USER_SELECTED_SAFE_REPLAY = "USER_SELECTED_SAFE_REPLAY"
    USER_ABANDONED_SESSION = "USER_ABANDONED_SESSION"


@dataclass(frozen=True, slots=True)
class RecoveryOfferV1:
    disposition: RecoveryDispositionV1
    actions: tuple[RecoveryActionV1, ...]
    reason_code: RecoveryReasonCodeV1
    detail: str
    session_id: str | None = None
    checkpoint_id: str | None = None
    checkpoint_record_sequence: int | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not RecoveryDispositionV1:
            raise TypeError("recovery disposition is invalid")
        if type(self.actions) is not tuple or not self.actions or any(
            type(action) is not RecoveryActionV1 for action in self.actions
        ):
            raise TypeError("recovery actions must be a nonempty typed tuple")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("recovery actions must be unique")
        if type(self.reason_code) is not RecoveryReasonCodeV1:
            raise TypeError("recovery reason code is invalid")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("recovery detail is required")
        expected = {
            RecoveryDispositionV1.NO_RECOVERY: (RecoveryActionV1.START_NEW,),
            RecoveryDispositionV1.EXACT_CONTINUATION: (
                RecoveryActionV1.CONTINUE_EXACT,
                RecoveryActionV1.REPLAY_SAFE,
                RecoveryActionV1.ABANDON,
            ),
            RecoveryDispositionV1.SAFE_REPLAY_ONLY: (
                RecoveryActionV1.REPLAY_SAFE,
                RecoveryActionV1.ABANDON,
            ),
            RecoveryDispositionV1.ABANDON_ONLY: (RecoveryActionV1.ABANDON,),
        }[self.disposition]
        if self.actions != expected:
            raise ValueError("recovery actions overclaim the proven disposition")
        if self.disposition is RecoveryDispositionV1.EXACT_CONTINUATION and (
            self.session_id is None
            or self.checkpoint_id is None
            or self.checkpoint_record_sequence is None
        ):
            raise ValueError("exact continuation lacks its durable checkpoint")

    def as_dict(self) -> dict[str, object]:
        return {
            "actions": [action.value for action in self.actions],
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_record_sequence": self.checkpoint_record_sequence,
            "detail": self.detail,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code.value,
            "session_id": self.session_id,
        }


class InteractiveRecoveryCoordinatorV1:
    """Inspect one governed active pointer and act only within the offered set."""

    def __init__(self, paths: DataPaths) -> None:
        if type(paths) is not DataPaths:
            raise TypeError("interactive recovery requires the exact DataPaths provider")
        self.paths = paths

    def inspect(self, source: LiveSessionSourceV1) -> RecoveryOfferV1:
        if type(source) is not LiveSessionSourceV1:
            raise TypeError("interactive recovery source is invalid")
        try:
            journal = LiveSessionJournalV1.discover(
                paths=self.paths,
                configuration_sha256=source.configuration_sha256,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            return RecoveryOfferV1(
                disposition=RecoveryDispositionV1.SAFE_REPLAY_ONLY,
                actions=(RecoveryActionV1.REPLAY_SAFE, RecoveryActionV1.ABANDON),
                reason_code=RecoveryReasonCodeV1.JOURNAL_CORRUPT,
                detail=(
                    "Recovery metadata is corrupt or incomplete. Exact continuation "
                    f"is unavailable; start a safe replay or abandon it. ({error})"
                ),
            )
        if journal is None or journal.terminal:
            return RecoveryOfferV1(
                disposition=RecoveryDispositionV1.NO_RECOVERY,
                actions=(RecoveryActionV1.START_NEW,),
                reason_code=RecoveryReasonCodeV1.NO_ACTIVE_SESSION,
                detail="No unfinished interactive session matches this trainer setup.",
            )
        if journal.source != source:
            return self._safe_offer(
                journal,
                RecoveryReasonCodeV1.SOURCE_MISMATCH,
                "The unfinished session uses another source, layout, pack/profile, "
                "seed, or observation policy.",
            )
        if journal.pending_transactions:
            pending = set(journal.pending_transactions.values())
            if RecoveryBoundaryKindV1.ACTION_PENDING in pending:
                reason = RecoveryReasonCodeV1.ACTION_ACKNOWLEDGEMENT_PENDING
                detail = "A player action may have crossed the crash boundary."
            elif RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING in pending:
                reason = RecoveryReasonCodeV1.CLIENT_ACKNOWLEDGEMENT_PENDING
                detail = "The last displayed client state was not acknowledged durably."
            elif RecoveryBoundaryKindV1.PACK_ACTIVATION_PENDING in pending:
                reason = RecoveryReasonCodeV1.PACK_ACTIVATION_PENDING
                detail = "Pack activation did not reach a proven durable commit."
            else:
                reason = RecoveryReasonCodeV1.PROFILE_UPDATE_PENDING
                detail = "Profile update did not reach a proven durable commit."
            return self._safe_offer(journal, reason, detail)
        checkpoint_record = _latest_checkpoint_record(journal.records)
        if checkpoint_record is None:
            return self._safe_offer(
                journal,
                RecoveryReasonCodeV1.CHECKPOINT_MISSING,
                "No complete durable checkpoint exists for exact continuation.",
            )
        try:
            checkpoint = journal.load_checkpoint(checkpoint_record)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            return self._safe_offer(
                journal,
                RecoveryReasonCodeV1.CHECKPOINT_CORRUPT,
                f"The latest committed checkpoint failed verification. ({error})",
            )
        suffix = journal.records[checkpoint_record.sequence :]
        if any(
            record.boundary
            in {
                RecoveryBoundaryKindV1.PACK_ACTIVATION_COMMITTED,
                RecoveryBoundaryKindV1.PROFILE_UPDATE_COMMITTED,
            }
            for record in suffix
        ):
            return self._safe_offer(
                journal,
                RecoveryReasonCodeV1.EXTERNAL_STATE_SUFFIX,
                "Pack/profile state changed after the last complete checkpoint.",
            )
        return RecoveryOfferV1(
            disposition=RecoveryDispositionV1.EXACT_CONTINUATION,
            actions=(
                RecoveryActionV1.CONTINUE_EXACT,
                RecoveryActionV1.REPLAY_SAFE,
                RecoveryActionV1.ABANDON,
            ),
            reason_code=RecoveryReasonCodeV1.COMPLETE_DURABLE_CUT,
            detail=(
                "The checkpoint and complete action/message/event/ledger suffix "
                "are durable and eligible for exact deterministic continuation."
            ),
            session_id=journal.session_id,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_record_sequence=checkpoint_record.sequence,
        )

    def start_new(
        self,
        *,
        session: LiveMarketSession,
        source: LiveSessionSourceV1,
        layout: HotkeyLayout,
        supersedes_session_id: str | None = None,
    ) -> LiveSessionJournalV1:
        journal = LiveSessionJournalV1.create(
            paths=self.paths,
            source=source,
            session=session,
            supersedes_session_id=supersedes_session_id,
        )
        session.bind_recovery_journal(journal)
        session.start()
        journal.commit_checkpoint(
            session=session,
            layout=layout,
            auto_start=True,
        )
        return journal

    def continue_exact(
        self,
        *,
        session: LiveMarketSession,
        source: LiveSessionSourceV1,
        bindings: BindingMap,
    ) -> LiveSessionJournalV1:
        offer = self.inspect(source)
        if offer.disposition is not RecoveryDispositionV1.EXACT_CONTINUATION:
            raise RuntimeError(
                f"exact continuation is unavailable: {offer.reason_code.value}"
            )
        journal = LiveSessionJournalV1.discover(
            paths=self.paths,
            configuration_sha256=source.configuration_sha256,
        )
        if journal is None:
            raise RuntimeError("active recovery journal disappeared")
        checkpoint_record = _record_by_sequence(
            journal.records,
            offer.checkpoint_record_sequence,
        )
        checkpoint = journal.load_checkpoint(checkpoint_record)
        recording = _recording_from_checkpoint(checkpoint)
        replay = replay_recording(recording)
        if not replay.passed:
            raise RuntimeError("complete recovery checkpoint failed exact replay")
        restored = replay.session
        _replay_suffix(
            restored,
            bindings,
            journal.records[checkpoint_record.sequence :],
            initial_recovery_state=checkpoint.recovery_state,
        )
        session.adopt_recovered_session(restored)
        journal.bind_source(source)
        session.bind_recovery_journal(journal)
        journal.append_recovery_completed(
            session=session,
            checkpoint_id=checkpoint.checkpoint_id,
        )
        return journal

    def select_safe_replay(
        self,
        *,
        session: LiveMarketSession,
        source: LiveSessionSourceV1,
        layout: HotkeyLayout,
    ) -> LiveSessionJournalV1:
        previous_id: str | None = None
        try:
            existing = LiveSessionJournalV1.discover(
                paths=self.paths,
                configuration_sha256=source.configuration_sha256,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            existing = None
        if existing is not None and not existing.terminal:
            previous_id = existing.session_id
            existing.abandon(
                simulation_time_us=session.simulation_time_us,
                reason_code=RecoveryReasonCodeV1.USER_SELECTED_SAFE_REPLAY.value,
            )
        session.reset()
        return self.start_new(
            session=session,
            source=source,
            layout=layout,
            supersedes_session_id=previous_id,
        )

    def abandon(
        self,
        *,
        source: LiveSessionSourceV1,
        simulation_time_us: int,
    ) -> None:
        try:
            journal = LiveSessionJournalV1.discover(
                paths=self.paths,
                configuration_sha256=source.configuration_sha256,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            LiveSessionJournalV1.clear_active_pointer(
                paths=self.paths,
                configuration_sha256=source.configuration_sha256,
            )
            return
        if journal is not None and not journal.terminal:
            journal.abandon(
                simulation_time_us=simulation_time_us,
                reason_code=RecoveryReasonCodeV1.USER_ABANDONED_SESSION.value,
            )

    @staticmethod
    def _safe_offer(
        journal: LiveSessionJournalV1,
        reason: RecoveryReasonCodeV1,
        detail: str,
    ) -> RecoveryOfferV1:
        return RecoveryOfferV1(
            disposition=RecoveryDispositionV1.SAFE_REPLAY_ONLY,
            actions=(RecoveryActionV1.REPLAY_SAFE, RecoveryActionV1.ABANDON),
            reason_code=reason,
            detail=detail + " Exact continuation is unavailable.",
            session_id=journal.session_id,
        )


def _replay_suffix(
    session: LiveMarketSession,
    bindings: BindingMap,
    records: tuple[LiveSessionJournalRecordV1, ...],
    *,
    initial_recovery_state: Mapping[str, object],
) -> None:
    client_known_working_order_ids = _recovery_identifier_tuple(
        initial_recovery_state,
        "client_working_order_ids",
    )
    pending_actions: set[str] = set()
    pending_client_messages: dict[str, tuple[str, ...]] = {}
    if (
        _recovery_identifier_tuple(
            initial_recovery_state,
            "pending_delivery_ids",
        )
        or _recovery_identifier_tuple(
            initial_recovery_state,
            "pending_replay_ids",
        )
    ):
        raise RuntimeError("recovery checkpoint crosses an unresolved transaction")

    def require_state(expected: object, *, label: str) -> None:
        require_recovery_state_matches(
            session,
            expected,
            label=label,
            client_known_working_order_ids=client_known_working_order_ids,
            pending_delivery_ids=tuple(sorted(pending_client_messages)),
            pending_replay_ids=tuple(sorted(pending_actions)),
        )

    require_state(
        initial_recovery_state,
        label="replayed recovery checkpoint",
    )
    for record in records:
        boundary = record.boundary
        if boundary is RecoveryBoundaryKindV1.ACTION_PENDING:
            if record.transaction_id is None:
                raise RuntimeError("recovery action pending record lacks a transaction")
            if record.transaction_id in pending_actions:
                raise RuntimeError("recovery action transaction is reused")
            require_state(
                record.payload.get("state_before"),
                label=f"journal record {record.sequence}",
            )
            pending_actions.add(record.transaction_id)
            continue
        if boundary in {
            RecoveryBoundaryKindV1.SESSION_STARTED,
            RecoveryBoundaryKindV1.SESSION_PAUSED,
        }:
            if pending_actions:
                continue
            if boundary is RecoveryBoundaryKindV1.SESSION_STARTED:
                session.start()
            else:
                session.pause()
            require_state(
                record.payload.get("state"),
                label=f"journal record {record.sequence}",
            )
            continue
        if boundary is RecoveryBoundaryKindV1.ACTION_ACKNOWLEDGED:
            transaction_id = record.transaction_id
            if transaction_id is None or transaction_id not in pending_actions:
                raise RuntimeError("recovery action acknowledgement is unpaired")
            expected = record.payload.get("input_record")
            if not isinstance(expected, Mapping):
                raise RuntimeError("recovery action acknowledgement lacks its input")
            input_key = expected.get("input_key")
            if type(input_key) is not str:
                raise RuntimeError("recovery action input key is invalid")
            actual = session.handle_input(input_key, bindings)
            if actual.as_dict() != thaw_json(expected):
                raise RuntimeError("recovered action differs from its durable acknowledgement")
            pending_actions.remove(transaction_id)
            require_state(
                record.payload.get("state"),
                label=f"journal record {record.sequence}",
            )
            continue
        if boundary is RecoveryBoundaryKindV1.ADVANCE_COMMITTED:
            from_time_us = _payload_int(record.payload, "from_time_us")
            to_time_us = _payload_int(record.payload, "to_time_us")
            requested_delta_us = _payload_int(record.payload, "requested_delta_us")
            if session.simulation_time_us != from_time_us:
                raise RuntimeError("recovery advance suffix is not contiguous")
            session.advance_by(requested_delta_us)
            if session.simulation_time_us != to_time_us:
                raise RuntimeError("recovery advance did not reach its durable time")
            require_state(
                record.payload.get("state"),
                label=f"journal record {record.sequence}",
            )
            continue
        if boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING:
            transaction_id = record.transaction_id
            if transaction_id is None:
                raise RuntimeError("recovery client message lacks a transaction")
            if transaction_id in pending_client_messages:
                raise RuntimeError("recovery client transaction is reused")
            require_state(
                record.payload.get("state_before"),
                label=f"journal record {record.sequence}",
            )
            pending_client_messages[transaction_id] = _recovery_identifier_tuple(
                record.payload,
                "client_working_order_ids",
            )
            continue
        if boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_ACKNOWLEDGED:
            transaction_id = record.transaction_id
            if transaction_id is None or transaction_id not in pending_client_messages:
                raise RuntimeError("recovery client acknowledgement is unpaired")
            client_known_working_order_ids = pending_client_messages.pop(transaction_id)
            require_state(
                record.payload.get("state"),
                label=f"journal record {record.sequence}",
            )
            continue
        if boundary is RecoveryBoundaryKindV1.RECOVERY_COMPLETED:
            require_state(
                record.payload.get("state"),
                label=f"journal record {record.sequence}",
            )
            continue
        if boundary in {
            RecoveryBoundaryKindV1.CHECKPOINT_COMMITTED,
            RecoveryBoundaryKindV1.SESSION_OPENED,
        }:
            continue
        raise RuntimeError(f"unsupported exact-recovery suffix boundary: {boundary.value}")
    if pending_actions:
        raise RuntimeError("exact-recovery suffix ends with an unacknowledged action")
    if pending_client_messages:
        raise RuntimeError(
            "exact-recovery suffix ends with an unacknowledged client message"
        )


def _recording_from_checkpoint(
    checkpoint: LiveSessionCheckpointV1,
) -> SessionRecording:
    value = restore_recovery_checkpoint_value_v1(
        thaw_json(checkpoint.recording)
    )
    if type(value) is not dict:
        raise TypeError("checkpoint recording must be an object")
    expected = {
        "auto_start",
        "complete",
        "completed_time_us",
        "curriculum_drill",
        "duration_seconds",
        "expected_state_sha256",
        "expected_timeline_sha256",
        "initial_quantity",
        "inputs",
        "layout",
        "liquidity",
        "market_states",
        "objective",
        "quantity_options",
        "record_type",
        "relative_volume",
        "scenario_definition",
        "schema_version",
        "seed",
        "strategy_source",
    }
    if set(value) != expected:
        raise ValueError("checkpoint session-recording fields differ")
    if (
        value["record_type"] != "kirby2_session_recording"
        or value["schema_version"] != RECORDING_SCHEMA_VERSION
    ):
        raise ValueError("checkpoint session-recording contract differs")
    scenario = _payload_dict(value, "scenario_definition")
    layout = _payload_dict(value, "layout")
    inputs = _payload_list(value, "inputs")
    market_states = _payload_list(value, "market_states")
    quantities = _payload_list(value, "quantity_options")
    objective = value["objective"]
    curriculum = value["curriculum_drill"]
    if objective is not None and type(objective) is not dict:
        raise TypeError("checkpoint objective must be an object or null")
    if curriculum is not None and type(curriculum) is not dict:
        raise TypeError("checkpoint curriculum must be an object or null")
    if type(value["auto_start"]) is not bool or type(value["complete"]) is not bool:
        raise TypeError("checkpoint recording booleans are invalid")
    strategy = value["strategy_source"]
    if strategy is not None and type(strategy) is not str:
        raise TypeError("checkpoint strategy source must be text or null")
    return SessionRecording(
        scenario_definition=scenario,
        seed=_payload_int(value, "seed"),
        duration_seconds=_payload_int(value, "duration_seconds"),
        relative_volume=VolumePreset.parse(_payload_text(value, "relative_volume")),
        liquidity=LiquidityPreset.parse(_payload_text(value, "liquidity")),
        initial_quantity=_payload_int(value, "initial_quantity"),
        quantity_options=tuple(_exact_int(item, "quantity option") for item in quantities),
        layout=HotkeyLayout.from_dict(layout),
        strategy_source=strategy,
        objective=(
            None if objective is None else SessionObjective.from_dict(objective)
        ),
        auto_start=value["auto_start"],
        input_records=tuple(
            InputRecord.from_dict(_exact_dict(item, "checkpoint input"))
            for item in inputs
        ),
        market_states=tuple(
            MarketStateRecord.from_dict(_exact_dict(item, "checkpoint market state"))
            for item in market_states
        ),
        completed_time_us=_payload_int(value, "completed_time_us"),
        complete=value["complete"],
        expected_state_sha256=_payload_text(value, "expected_state_sha256"),
        expected_timeline_sha256=_payload_text(value, "expected_timeline_sha256"),
        curriculum_drill=(
            None if curriculum is None else CurriculumDrill.from_dict(curriculum)
        ),
    )


def _latest_checkpoint_record(
    records: tuple[LiveSessionJournalRecordV1, ...],
) -> LiveSessionJournalRecordV1 | None:
    return next(
        (
            record
            for record in reversed(records)
            if record.boundary is RecoveryBoundaryKindV1.CHECKPOINT_COMMITTED
        ),
        None,
    )


def _record_by_sequence(
    records: tuple[LiveSessionJournalRecordV1, ...],
    sequence: int | None,
) -> LiveSessionJournalRecordV1:
    if sequence is None or sequence <= 0 or sequence > len(records):
        raise RuntimeError("recovery checkpoint journal sequence is unavailable")
    record = records[sequence - 1]
    if record.sequence != sequence:
        raise RuntimeError("recovery journal sequence is not contiguous")
    return record


def _recovery_identifier_tuple(
    value: Mapping[str, object],
    key: str,
) -> tuple[str, ...]:
    selected = value.get(key)
    if not isinstance(selected, (list, tuple)) or any(
        type(item) is not str or not item for item in selected
    ):
        raise TypeError(f"recovery {key} must contain text identifiers")
    result = tuple(selected)
    if len(result) != len(set(result)) or result != tuple(sorted(result)):
        raise ValueError(f"recovery {key} must be unique and sorted")
    return result


def _payload_dict(value: Mapping[str, object], key: str) -> dict[str, object]:
    selected = value[key]
    if type(selected) is not dict:
        raise TypeError(f"{key} must be an object")
    return selected


def _payload_list(value: Mapping[str, object], key: str) -> list[object]:
    selected = value[key]
    if type(selected) is not list:
        raise TypeError(f"{key} must be an array")
    return selected


def _payload_text(value: Mapping[str, object], key: str) -> str:
    selected = value[key]
    if type(selected) is not str:
        raise TypeError(f"{key} must be text")
    return selected


def _payload_int(value: Mapping[str, object], key: str) -> int:
    selected = value[key]
    if type(selected) is not int:
        raise TypeError(f"{key} must be an integer")
    return selected


def _exact_dict(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    return value


def _exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


__all__ = [
    "InteractiveRecoveryCoordinatorV1",
    "RecoveryActionV1",
    "RecoveryDispositionV1",
    "RecoveryOfferV1",
    "RecoveryReasonCodeV1",
]
