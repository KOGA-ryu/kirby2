"""Durable hash-chained boundaries for recoverable interactive sessions."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from kirby2.immutable import freeze_json, thaw_json
from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes
from kirby2.research.paths import DataAreaId, DataPaths

from .records import RecoveryBoundaryKindV1, RecoveryEvidenceRecordV1

if TYPE_CHECKING:
    from .bindings import BindingMap, SessionCommand
    from .layouts import HotkeyLayout
    from .live import LiveMarketSession, SessionSnapshot
    from .records import InputRecord


LIVE_SESSION_JOURNAL_SCHEMA_ID_V1 = "KIRBY2_LIVE_SESSION_JOURNAL_V1"
LIVE_SESSION_CHECKPOINT_SCHEMA_ID_V1 = "KIRBY2_LIVE_SESSION_CHECKPOINT_V1"
LIVE_SESSION_ACTIVE_POINTER_SCHEMA_ID_V1 = "KIRBY2_LIVE_SESSION_ACTIVE_POINTER_V1"
LIVE_SESSION_RECOVERY_SCHEMA_VERSION_V1 = 1
TERMINAL_OBSERVATION_POLICY_ID_V1 = "KIRBY2_TERMINAL_OBSERVATION_POLICY_V1"
MAX_LIVE_JOURNAL_BYTES_V1 = 64 * 1024 * 1024
MAX_LIVE_RECORD_BYTES_V1 = 4 * 1024 * 1024
MAX_LIVE_CHECKPOINT_BYTES_V1 = 32 * 1024 * 1024

_ZERO_SHA256 = hashlib.sha256(canonical_json_bytes([])).hexdigest()
_RELEASE_SCALAR_RESERVED_V1 = "__kirby2_release_scalar_v1__"


@dataclass(frozen=True, slots=True)
class LiveSessionSourceV1:
    """Stable source and observation identity required for exact continuation."""

    source_run_id: str
    configuration_sha256: str
    compiled_scenario_sha256: str
    seed: int
    substreams_sha256: str
    active_lesson_id: str | None
    layout_sha256: str
    observation_policy_id: str
    pack_activation_sha256: str
    profile_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.source_run_id, "live-session source run ID")
        _identifier(self.observation_policy_id, "observation policy ID")
        if self.active_lesson_id is not None:
            _identifier(self.active_lesson_id, "active lesson ID")
        if type(self.seed) is not int:
            raise TypeError("live-session seed must be an integer")
        for value, label in (
            (self.configuration_sha256, "configuration"),
            (self.compiled_scenario_sha256, "compiled scenario"),
            (self.substreams_sha256, "substreams"),
            (self.layout_sha256, "layout"),
            (self.pack_activation_sha256, "pack activation"),
            (self.profile_sha256, "profile"),
        ):
            _sha256(value, f"live-session {label}")

    def as_dict(self) -> dict[str, object]:
        return {
            "active_lesson_id": self.active_lesson_id,
            "compiled_scenario_sha256": self.compiled_scenario_sha256,
            "configuration_sha256": self.configuration_sha256,
            "layout_sha256": self.layout_sha256,
            "observation_policy_id": self.observation_policy_id,
            "pack_activation_sha256": self.pack_activation_sha256,
            "profile_sha256": self.profile_sha256,
            "seed": self.seed,
            "source_run_id": self.source_run_id,
            "substreams_sha256": self.substreams_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> LiveSessionSourceV1:
        expected = {
            "active_lesson_id",
            "compiled_scenario_sha256",
            "configuration_sha256",
            "layout_sha256",
            "observation_policy_id",
            "pack_activation_sha256",
            "profile_sha256",
            "seed",
            "source_run_id",
            "substreams_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("live-session source fields differ")
        row = dict(value)
        lesson = row["active_lesson_id"]
        if lesson is not None and type(lesson) is not str:
            raise TypeError("live-session lesson ID must be text or null")
        return cls(
            source_run_id=_text(row, "source_run_id"),
            configuration_sha256=_text(row, "configuration_sha256"),
            compiled_scenario_sha256=_text(row, "compiled_scenario_sha256"),
            seed=_integer(row, "seed"),
            substreams_sha256=_text(row, "substreams_sha256"),
            active_lesson_id=lesson,
            layout_sha256=_text(row, "layout_sha256"),
            observation_policy_id=_text(row, "observation_policy_id"),
            pack_activation_sha256=_text(row, "pack_activation_sha256"),
            profile_sha256=_text(row, "profile_sha256"),
        )

    @classmethod
    def from_session(
        cls,
        session: LiveMarketSession,
        bindings: BindingMap,
        *,
        layout_name: str,
        observation_policy_id: str = TERMINAL_OBSERVATION_POLICY_ID_V1,
    ) -> LiveSessionSourceV1:
        from .layouts import HotkeyLayout

        layout = HotkeyLayout(layout_name, bindings)
        scenario = session.definition.as_dict()
        configuration = {
            "curriculum_drill": (
                None
                if session.curriculum_drill is None
                else session.curriculum_drill.as_dict()
            ),
            "duration_us": session.duration_us,
            "initial_quantity": session.initial_quantity,
            "layout": layout.as_dict(),
            "liquidity": session.dimensions.liquidity.value,
            "objective": (
                None if session.objective is None else session.objective.as_dict()
            ),
            "quantity_options": list(session.quantity_options),
            "relative_volume": session.dimensions.volume.value,
            "scenario": scenario,
            "seed": session.seed,
            "strategy_source": (
                None
                if session.strategy_definition is None
                else session.strategy_definition.source
            ),
        }
        configuration_sha256 = _digest(configuration)
        profile = {
            "liquidity": session.dimensions.liquidity.value,
            "relative_volume": session.dimensions.volume.value,
            "scenario_regime": session.definition.regime.value,
        }
        return cls(
            source_run_id=f"interactive-source-{configuration_sha256[:24]}",
            configuration_sha256=configuration_sha256,
            compiled_scenario_sha256=_digest({"scenario": scenario}),
            seed=session.seed,
            substreams_sha256=_digest(
                {
                    "initial_rng": session.engine.rng.runtime_state(),
                    "seed": session.seed,
                }
            ),
            active_lesson_id=(
                None
                if session.curriculum_drill is None
                else session.curriculum_drill.lesson_id
            ),
            layout_sha256=_digest(layout.as_dict()),
            observation_policy_id=observation_policy_id,
            pack_activation_sha256=_ZERO_SHA256,
            profile_sha256=_digest(profile),
        )


@dataclass(frozen=True, slots=True)
class LiveSessionJournalRecordV1:
    session_id: str
    sequence: int
    boundary: RecoveryBoundaryKindV1
    simulation_time_us: int
    transaction_id: str | None
    previous_record_sha256: str | None
    payload: Mapping[str, object]
    record_sha256: str = ""

    schema_id: ClassVar[str] = LIVE_SESSION_JOURNAL_SCHEMA_ID_V1
    schema_version: ClassVar[int] = LIVE_SESSION_RECOVERY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("live-session journal sequence must be positive")
        if type(self.boundary) is not RecoveryBoundaryKindV1:
            raise TypeError("live-session journal boundary is invalid")
        if type(self.simulation_time_us) is not int or self.simulation_time_us < 0:
            raise ValueError("live-session journal time must be nonnegative")
        if self.transaction_id is not None:
            _identifier(self.transaction_id, "live-session transaction ID")
        if self.previous_record_sha256 is not None:
            _sha256(self.previous_record_sha256, "previous journal record")
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("live-session journal payload must be a JSON object")
        object.__setattr__(self, "payload", frozen)
        expected = hashlib.sha256(canonical_json_bytes(self.projection())).hexdigest()
        if self.record_sha256:
            if not hmac.compare_digest(self.record_sha256, expected):
                raise ValueError("live-session journal record digest differs")
        else:
            object.__setattr__(self, "record_sha256", expected)

    def projection(self) -> dict[str, object]:
        return {
            "boundary": self.boundary.value,
            "payload": thaw_json(self.payload),
            "previous_record_sha256": self.previous_record_sha256,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "simulation_time_us": self.simulation_time_us,
            "transaction_id": self.transaction_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.projection(), "record_sha256": self.record_sha256}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> LiveSessionJournalRecordV1:
        value = load_canonical_json_bytes(raw, "live-session journal record")
        expected = {
            "boundary",
            "payload",
            "previous_record_sha256",
            "record_sha256",
            "schema_id",
            "schema_version",
            "sequence",
            "session_id",
            "simulation_time_us",
            "transaction_id",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("live-session journal record fields differ")
        if (
            value["schema_id"] != cls.schema_id
            or value["schema_version"] != cls.schema_version
        ):
            raise ValueError("live-session journal contract differs")
        payload = value["payload"]
        if type(payload) is not dict:
            raise TypeError("live-session journal payload must be an object")
        transaction_id = value["transaction_id"]
        previous = value["previous_record_sha256"]
        if transaction_id is not None and type(transaction_id) is not str:
            raise TypeError("journal transaction ID must be text or null")
        if previous is not None and type(previous) is not str:
            raise TypeError("journal previous digest must be text or null")
        restored = cls(
            session_id=_text(value, "session_id"),
            sequence=_integer(value, "sequence"),
            boundary=RecoveryBoundaryKindV1(_text(value, "boundary")),
            simulation_time_us=_integer(value, "simulation_time_us"),
            transaction_id=transaction_id,
            previous_record_sha256=previous,
            payload=payload,
            record_sha256=_text(value, "record_sha256"),
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("live-session journal changed during restoration")
        return restored


@dataclass(frozen=True, slots=True)
class LiveSessionCheckpointV1:
    checkpoint_id: str
    session_id: str
    source: LiveSessionSourceV1
    journal_sequence: int
    recording: Mapping[str, object]
    recovery_state: Mapping[str, object]

    schema_id: ClassVar[str] = LIVE_SESSION_CHECKPOINT_SCHEMA_ID_V1
    schema_version: ClassVar[int] = LIVE_SESSION_RECOVERY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        if type(self.source) is not LiveSessionSourceV1:
            raise TypeError("live-session checkpoint source is invalid")
        if type(self.journal_sequence) is not int or self.journal_sequence <= 0:
            raise ValueError("live-session checkpoint journal sequence is invalid")
        for value, label in (
            (self.recording, "recording"),
            (self.recovery_state, "recovery state"),
        ):
            frozen = freeze_json(value)
            if not isinstance(frozen, Mapping):
                raise TypeError(f"live-session checkpoint {label} must be an object")
            object.__setattr__(self, label.replace(" ", "_"), frozen)
        expected = "live-checkpoint-" + hashlib.sha256(
            canonical_json_bytes(self.projection())
        ).hexdigest()[:24]
        if not self.checkpoint_id:
            object.__setattr__(self, "checkpoint_id", expected)
        elif self.checkpoint_id != expected:
            raise ValueError("live-session checkpoint identity differs")
        _checkpoint_id(self.checkpoint_id)

    def projection(self) -> dict[str, object]:
        return {
            "journal_sequence": self.journal_sequence,
            "recording": thaw_json(self.recording),
            "recovery_state": thaw_json(self.recovery_state),
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source": self.source.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.projection(), "checkpoint_id": self.checkpoint_id}

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> LiveSessionCheckpointV1:
        value = load_canonical_json_bytes(raw, "live-session checkpoint")
        expected = {
            "checkpoint_id",
            "journal_sequence",
            "recording",
            "recovery_state",
            "schema_id",
            "schema_version",
            "session_id",
            "source",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("live-session checkpoint fields differ")
        if (
            value["schema_id"] != cls.schema_id
            or value["schema_version"] != cls.schema_version
            or type(value["recording"]) is not dict
            or type(value["recovery_state"]) is not dict
        ):
            raise ValueError("live-session checkpoint contract differs")
        restored = cls(
            checkpoint_id=_text(value, "checkpoint_id"),
            session_id=_text(value, "session_id"),
            source=LiveSessionSourceV1.from_dict(value["source"]),
            journal_sequence=_integer(value, "journal_sequence"),
            recording=value["recording"],
            recovery_state=value["recovery_state"],
        )
        if restored.canonical_bytes() != raw:
            raise ValueError("live-session checkpoint changed during restoration")
        return restored


class LiveSessionJournalV1:
    """One append-only journal and immutable checkpoint namespace."""

    def __init__(
        self,
        *,
        paths: DataPaths,
        source: LiveSessionSourceV1,
        session_id: str,
        records: tuple[LiveSessionJournalRecordV1, ...],
        journal_size: int,
    ) -> None:
        if type(paths) is not DataPaths:
            raise TypeError("live-session journal requires the exact DataPaths provider")
        if type(source) is not LiveSessionSourceV1:
            raise TypeError("live-session journal source is invalid")
        _session_id(session_id)
        self.paths = paths
        self.source = source
        self.session_id = session_id
        self.directory = paths.checkpoints / "interactive" / session_id
        self.checkpoints_directory = self.directory / "checkpoints"
        self.journal_path = self.directory / "journal.jsonl"
        self._records = list(records)
        self._journal_size = journal_size
        self._pending = _pending_transactions(records)
        (
            self._client_known_working_order_ids,
            self._pending_client_working_order_ids,
        ) = _client_observation_state(records)
        self._state_cache_token: tuple[object, ...] | None = None
        self._state_cache: dict[str, object] | None = None

    @classmethod
    def create(
        cls,
        *,
        paths: DataPaths,
        source: LiveSessionSourceV1,
        session: LiveMarketSession,
        supersedes_session_id: str | None = None,
        session_id: str | None = None,
    ) -> LiveSessionJournalV1:
        paths.ensure((DataAreaId.CHECKPOINTS,))
        paths.validate((DataAreaId.CHECKPOINTS,))
        selected_id = session_id or f"live-session-{secrets.token_hex(12)}"
        _session_id(selected_id)
        if supersedes_session_id is not None:
            _session_id(supersedes_session_id)
        root = paths.checkpoints / "interactive"
        _ensure_real_directory(root)
        directory = root / selected_id
        directory.mkdir(mode=0o700)
        checkpoints = directory / "checkpoints"
        checkpoints.mkdir(mode=0o700)
        journal_path = directory / "journal.jsonl"
        descriptor = os.open(
            journal_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fsync(descriptor)
        os.close(descriptor)
        _fsync_directory(directory)
        created = cls(
            paths=paths,
            source=source,
            session_id=selected_id,
            records=(),
            journal_size=0,
        )
        created._append(
            RecoveryBoundaryKindV1.SESSION_OPENED,
            simulation_time_us=session.simulation_time_us,
            payload={
                "source": source.as_dict(),
                "state": created._state(session),
                "supersedes_session_id": supersedes_session_id,
            },
        )
        return created

    @classmethod
    def open_existing(
        cls,
        *,
        paths: DataPaths,
        session_id: str,
    ) -> LiveSessionJournalV1:
        _session_id(session_id)
        paths.validate((DataAreaId.CHECKPOINTS,))
        root = paths.checkpoints / "interactive"
        _require_real_directory(root)
        directory = root / session_id
        _require_real_directory(directory)
        _require_real_directory(directory / "checkpoints")
        journal_path = directory / "journal.jsonl"
        records, size = _load_journal(journal_path, session_id=session_id)
        if not records or records[0].boundary is not RecoveryBoundaryKindV1.SESSION_OPENED:
            raise ValueError("live-session journal lacks its opening record")
        source = LiveSessionSourceV1.from_dict(records[0].payload.get("source"))
        return cls(
            paths=paths,
            source=source,
            session_id=session_id,
            records=records,
            journal_size=size,
        )

    @classmethod
    def discover(
        cls,
        *,
        paths: DataPaths,
        configuration_sha256: str,
    ) -> LiveSessionJournalV1 | None:
        _sha256(configuration_sha256, "live-session configuration")
        paths.validate((DataAreaId.CHECKPOINTS,))
        root = paths.checkpoints / "interactive"
        if not root.exists() and not root.is_symlink():
            return None
        _require_real_directory(root)
        pointer = _pointer_path(paths, configuration_sha256)
        if not pointer.exists() and not pointer.is_symlink():
            return None
        payload = _read_pointer(pointer)
        if payload["configuration_sha256"] != configuration_sha256:
            raise ValueError("active recovery pointer has another configuration")
        journal = cls.open_existing(
            paths=paths,
            session_id=_text(payload, "session_id"),
        )
        pointer_digest = _text(payload, "journal_record_sha256")
        if not any(
            hmac.compare_digest(record.record_sha256, pointer_digest)
            for record in journal.records
        ):
            raise ValueError("active recovery pointer is outside its journal chain")
        return journal

    @classmethod
    def clear_active_pointer(
        cls,
        *,
        paths: DataPaths,
        configuration_sha256: str,
    ) -> None:
        """Forget only the mutable locator; journal/checkpoint evidence remains."""

        _sha256(configuration_sha256, "live-session configuration")
        paths.validate((DataAreaId.CHECKPOINTS,))
        pointer = _pointer_path(paths, configuration_sha256)
        if pointer.exists() or pointer.is_symlink():
            _require_real_directory(pointer.parent)
            pointer.unlink()
            _fsync_directory(pointer.parent)

    @property
    def records(self) -> tuple[LiveSessionJournalRecordV1, ...]:
        return tuple(self._records)

    @property
    def pending_transactions(self) -> Mapping[str, RecoveryBoundaryKindV1]:
        return dict(self._pending)

    @property
    def terminal(self) -> bool:
        return bool(self._records) and self._records[-1].boundary in {
            RecoveryBoundaryKindV1.SESSION_CLOSED,
            RecoveryBoundaryKindV1.SESSION_ABANDONED,
        }

    def bind_source(self, source: LiveSessionSourceV1) -> None:
        if source != self.source:
            raise ValueError("live-session journal source differs from trainer startup")

    def append_lifecycle(
        self,
        boundary: RecoveryBoundaryKindV1,
        session: LiveMarketSession,
    ) -> LiveSessionJournalRecordV1:
        if boundary not in {
            RecoveryBoundaryKindV1.SESSION_STARTED,
            RecoveryBoundaryKindV1.SESSION_PAUSED,
        }:
            raise ValueError("unsupported live-session lifecycle boundary")
        return self._append(
            boundary,
            simulation_time_us=session.simulation_time_us,
            payload={"state": self._state(session)},
        )

    def commit_advance(
        self,
        *,
        session: LiveMarketSession,
        from_time_us: int,
        requested_delta_us: int,
    ) -> LiveSessionJournalRecordV1:
        return self._append(
            RecoveryBoundaryKindV1.ADVANCE_COMMITTED,
            simulation_time_us=session.simulation_time_us,
            payload={
                "from_time_us": from_time_us,
                "requested_delta_us": requested_delta_us,
                "state": self._state(session),
                "to_time_us": session.simulation_time_us,
            },
        )

    def begin_action(
        self,
        *,
        session: LiveMarketSession,
        key: str,
        command: SessionCommand | None,
    ) -> str:
        transaction_id = self._next_transaction_id("action")
        self._append(
            RecoveryBoundaryKindV1.ACTION_PENDING,
            simulation_time_us=session.simulation_time_us,
            transaction_id=transaction_id,
            payload={
                "input_key": key,
                "resolved_command": None if command is None else command.value,
                "state_before": self._state(session),
            },
        )
        return transaction_id

    def acknowledge_action(
        self,
        *,
        session: LiveMarketSession,
        transaction_id: str,
        record: InputRecord,
    ) -> LiveSessionJournalRecordV1:
        self._require_pending(
            transaction_id,
            RecoveryBoundaryKindV1.ACTION_PENDING,
        )
        return self._append(
            RecoveryBoundaryKindV1.ACTION_ACKNOWLEDGED,
            simulation_time_us=session.simulation_time_us,
            transaction_id=transaction_id,
            payload={
                "input_record": record.as_dict(),
                "state": self._state(
                    session,
                    remove_pending_transaction_id=transaction_id,
                ),
            },
        )

    def begin_client_message(
        self,
        *,
        session: LiveMarketSession,
        snapshot: SessionSnapshot,
    ) -> str:
        transaction_id = self._next_transaction_id("client")
        client_working_order_ids = tuple(
            sorted(order.order_id for order in snapshot.working_orders)
        )
        self._append(
            RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING,
            simulation_time_us=session.simulation_time_us,
            transaction_id=transaction_id,
            payload={
                "client_working_order_ids": list(client_working_order_ids),
                "exchange_event_sequence": snapshot.exchange_event_sequence,
                "market_state_id": snapshot.market_state_id,
                "state_before": self._state(session),
            },
        )
        return transaction_id

    def acknowledge_client_message(
        self,
        *,
        session: LiveMarketSession,
        transaction_id: str,
    ) -> LiveSessionJournalRecordV1:
        self._require_pending(
            transaction_id,
            RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING,
        )
        client_working_order_ids = self._pending_client_working_order_ids.get(
            transaction_id
        )
        if client_working_order_ids is None:
            raise RuntimeError("client acknowledgement lacks its proposed observation")
        return self._append(
            RecoveryBoundaryKindV1.CLIENT_MESSAGE_ACKNOWLEDGED,
            simulation_time_us=session.simulation_time_us,
            transaction_id=transaction_id,
            payload={
                "state": self._state(
                    session,
                    client_known_working_order_ids=client_working_order_ids,
                    remove_pending_transaction_id=transaction_id,
                )
            },
        )

    def begin_external_update(
        self,
        *,
        session: LiveMarketSession,
        boundary: RecoveryBoundaryKindV1,
        identity_sha256: str,
    ) -> str:
        if boundary not in {
            RecoveryBoundaryKindV1.PACK_ACTIVATION_PENDING,
            RecoveryBoundaryKindV1.PROFILE_UPDATE_PENDING,
        }:
            raise ValueError("unsupported external recovery boundary")
        _sha256(identity_sha256, "external update identity")
        transaction_id = self._next_transaction_id("external")
        self._append(
            boundary,
            simulation_time_us=session.simulation_time_us,
            transaction_id=transaction_id,
            payload={
                "identity_sha256": identity_sha256,
                "state_before": self._state(session),
            },
        )
        return transaction_id

    def commit_external_update(
        self,
        *,
        session: LiveMarketSession,
        transaction_id: str,
        boundary: RecoveryBoundaryKindV1,
        identity_sha256: str,
    ) -> LiveSessionJournalRecordV1:
        pairs = {
            RecoveryBoundaryKindV1.PACK_ACTIVATION_COMMITTED:
                RecoveryBoundaryKindV1.PACK_ACTIVATION_PENDING,
            RecoveryBoundaryKindV1.PROFILE_UPDATE_COMMITTED:
                RecoveryBoundaryKindV1.PROFILE_UPDATE_PENDING,
        }
        pending = pairs.get(boundary)
        if pending is None:
            raise ValueError("unsupported external recovery commit boundary")
        self._require_pending(transaction_id, pending)
        _sha256(identity_sha256, "external update identity")
        return self._append(
            boundary,
            simulation_time_us=session.simulation_time_us,
            transaction_id=transaction_id,
            payload={
                "identity_sha256": identity_sha256,
                "state": self._state(session),
            },
        )

    def commit_checkpoint(
        self,
        *,
        session: LiveMarketSession,
        layout: HotkeyLayout,
        auto_start: bool,
    ) -> LiveSessionCheckpointV1:
        from .replay import SessionRecording

        self.paths.validate((DataAreaId.CHECKPOINTS,))
        _require_real_directory(self.checkpoints_directory)
        if self._pending:
            raise RuntimeError("cannot commit a recovery checkpoint across a pending boundary")
        recording = SessionRecording.capture(session, layout, auto_start=auto_start)
        checkpoint = LiveSessionCheckpointV1(
            checkpoint_id="",
            session_id=self.session_id,
            source=self.source,
            journal_sequence=self._records[-1].sequence,
            recording=recovery_checkpoint_value_v1(recording.as_dict()),
            recovery_state=self._state(session),
        )
        path = self.checkpoints_directory / f"{checkpoint.checkpoint_id}.json"
        _write_immutable_file(path, checkpoint.canonical_bytes())
        restored = LiveSessionCheckpointV1.from_canonical_bytes(
            _read_regular_file(path, maximum_bytes=MAX_LIVE_CHECKPOINT_BYTES_V1)
        )
        if restored != checkpoint:
            raise RuntimeError("live-session checkpoint failed complete read-back")
        self._append(
            RecoveryBoundaryKindV1.CHECKPOINT_COMMITTED,
            simulation_time_us=session.simulation_time_us,
            payload={
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_journal_sequence": checkpoint.journal_sequence,
                "checkpoint_sha256": checkpoint.sha256,
                "state": self._state(session),
            },
        )
        return checkpoint

    def load_checkpoint(
        self,
        record: LiveSessionJournalRecordV1,
    ) -> LiveSessionCheckpointV1:
        self.paths.validate((DataAreaId.CHECKPOINTS,))
        _require_real_directory(self.checkpoints_directory)
        if record.boundary is not RecoveryBoundaryKindV1.CHECKPOINT_COMMITTED:
            raise ValueError("journal record is not a committed checkpoint")
        checkpoint_id = record.payload.get("checkpoint_id")
        checkpoint_sha256 = record.payload.get("checkpoint_sha256")
        if type(checkpoint_id) is not str or type(checkpoint_sha256) is not str:
            raise ValueError("checkpoint journal reference is incomplete")
        _checkpoint_id(checkpoint_id)
        path = self.checkpoints_directory / f"{checkpoint_id}.json"
        raw = _read_regular_file(path, maximum_bytes=MAX_LIVE_CHECKPOINT_BYTES_V1)
        if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), checkpoint_sha256):
            raise ValueError("checkpoint artifact digest differs from journal")
        checkpoint = LiveSessionCheckpointV1.from_canonical_bytes(raw)
        if (
            checkpoint.checkpoint_id != checkpoint_id
            or checkpoint.session_id != self.session_id
            or checkpoint.source != self.source
            or checkpoint.journal_sequence
            != record.payload.get("checkpoint_journal_sequence")
            or checkpoint.journal_sequence != record.sequence - 1
        ):
            raise ValueError("checkpoint artifact does not bind its journal cut")
        return checkpoint

    def append_recovery_completed(
        self,
        *,
        session: LiveMarketSession,
        checkpoint_id: str,
    ) -> LiveSessionJournalRecordV1:
        _checkpoint_id(checkpoint_id)
        return self._append(
            RecoveryBoundaryKindV1.RECOVERY_COMPLETED,
            simulation_time_us=session.simulation_time_us,
            payload={
                "checkpoint_id": checkpoint_id,
                "disposition": "EXACT_CONTINUATION",
                "reason_code": "COMPLETE_DURABLE_CUT",
                "state": self._state(session),
            },
        )

    def close(self, *, session: LiveMarketSession) -> LiveSessionJournalRecordV1:
        if self._pending:
            raise RuntimeError("cannot close a live-session journal with pending boundaries")
        record = self._append(
            RecoveryBoundaryKindV1.SESSION_CLOSED,
            simulation_time_us=session.simulation_time_us,
            payload={
                "disposition": "CLOSED_CLEANLY",
                "state": self._state(session),
            },
        )
        self._clear_pointer()
        return record

    def abandon(
        self,
        *,
        simulation_time_us: int,
        reason_code: str,
    ) -> LiveSessionJournalRecordV1:
        _identifier(reason_code, "abandonment reason code")
        record = self._append(
            RecoveryBoundaryKindV1.SESSION_ABANDONED,
            simulation_time_us=simulation_time_us,
            payload={
                "disposition": "ABANDONED",
                "reason_code": reason_code,
                "state": self._latest_state(),
            },
        )
        self._clear_pointer()
        return record

    def evidence_records(self) -> tuple[RecoveryEvidenceRecordV1, ...]:
        result: list[RecoveryEvidenceRecordV1] = []
        for record in self._records:
            raw_state = record.payload.get("state")
            if not isinstance(raw_state, Mapping):
                raw_state = record.payload.get("state_before")
            state = raw_state if isinstance(raw_state, Mapping) else {}
            result.append(
                RecoveryEvidenceRecordV1(
                    sequence=record.sequence,
                    session_id=record.session_id,
                    boundary=record.boundary,
                    simulation_time_us=record.simulation_time_us,
                    transaction_id=record.transaction_id,
                    checkpoint_id=(
                        str(record.payload["checkpoint_id"])
                        if record.payload.get("checkpoint_id") is not None
                        else None
                    ),
                    event_prefix_count=int(state.get("event_prefix_count", 0)),
                    event_prefix_sha256=str(
                        state.get("event_prefix_sha256", _ZERO_SHA256)
                    ),
                    ledger_prefix_count=int(state.get("ledger_prefix_count", 0)),
                    ledger_prefix_sha256=str(
                        state.get("ledger_prefix_sha256", _ZERO_SHA256)
                    ),
                    disposition=(
                        str(record.payload["disposition"])
                        if record.payload.get("disposition") is not None
                        else None
                    ),
                    reason_code=(
                        str(record.payload["reason_code"])
                        if record.payload.get("reason_code") is not None
                        else None
                    ),
                    details=record.payload,
                    record_sha256=record.record_sha256,
                )
            )
        return tuple(result)

    def _state(
        self,
        session: LiveMarketSession,
        *,
        client_known_working_order_ids: tuple[str, ...] | None = None,
        remove_pending_transaction_id: str | None = None,
    ) -> dict[str, object]:
        pending_delivery_ids = tuple(
            sorted(
                transaction_id
                for transaction_id, boundary in self._pending.items()
                if boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING
                and transaction_id != remove_pending_transaction_id
            )
        )
        pending_replay_ids = tuple(
            sorted(
                transaction_id
                for transaction_id, boundary in self._pending.items()
                if boundary is RecoveryBoundaryKindV1.ACTION_PENDING
                and transaction_id != remove_pending_transaction_id
            )
        )
        token = (
            id(session.engine),
            session.simulation_time_us,
            len(session.engine.book.journal.events),
            len(session.engine.flow_events),
            len(session.timeline),
            len(session.input_records),
            len(session.market_states),
            session.running,
            session.complete,
            session.selected_quantity,
            session.status_message,
        )
        if token != self._state_cache_token or self._state_cache is None:
            self._state_cache = recovery_state_projection(
                session,
                client_known_working_order_ids=(),
            )
            self._state_cache_token = token
        result = dict(self._state_cache)
        result["client_working_order_ids"] = list(
            self._client_known_working_order_ids
            if client_known_working_order_ids is None
            else client_known_working_order_ids
        )
        result["pending_delivery_ids"] = list(pending_delivery_ids)
        result["pending_replay_ids"] = list(pending_replay_ids)
        return result

    def _append(
        self,
        boundary: RecoveryBoundaryKindV1,
        *,
        simulation_time_us: int,
        payload: Mapping[str, object],
        transaction_id: str | None = None,
    ) -> LiveSessionJournalRecordV1:
        if self.terminal:
            raise RuntimeError("live-session journal is already terminal")
        self.paths.validate((DataAreaId.CHECKPOINTS,))
        _require_real_directory(self.directory)
        previous = None if not self._records else self._records[-1].record_sha256
        record = LiveSessionJournalRecordV1(
            session_id=self.session_id,
            sequence=len(self._records) + 1,
            boundary=boundary,
            simulation_time_us=simulation_time_us,
            transaction_id=transaction_id,
            previous_record_sha256=previous,
            payload=payload,
        )
        line = record.canonical_bytes() + b"\n"
        if len(line) > MAX_LIVE_RECORD_BYTES_V1:
            raise ValueError("live-session journal record exceeds its byte bound")
        descriptor = os.open(
            self.journal_path,
            os.O_RDWR
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != self._journal_size
                or metadata.st_size + len(line) > MAX_LIVE_JOURNAL_BYTES_V1
            ):
                raise RuntimeError("live-session journal changed or became unsafe")
            _write_all(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._records.append(record)
        self._journal_size += len(line)
        self._update_pending(record)
        self._update_client_observation(record)
        if record.sequence == 1:
            self._write_pointer(record)
        return record

    def _next_transaction_id(self, family: str) -> str:
        return (
            f"{family}-{len(self._records) + 1}-"
            + hashlib.sha256(
                f"{self.session_id}:{family}:{len(self._records) + 1}".encode("ascii")
            ).hexdigest()[:16]
        )

    def _require_pending(
        self,
        transaction_id: str,
        expected: RecoveryBoundaryKindV1,
    ) -> None:
        _identifier(transaction_id, "live-session transaction ID")
        if self._pending.get(transaction_id) is not expected:
            raise RuntimeError("live-session acknowledgement lacks its pending boundary")

    def _update_pending(self, record: LiveSessionJournalRecordV1) -> None:
        if record.transaction_id is None:
            return
        if record.boundary in _PENDING_BOUNDARIES:
            if record.transaction_id in self._pending:
                raise RuntimeError("live-session transaction ID was reused")
            self._pending[record.transaction_id] = record.boundary
            return
        expected = _ACKNOWLEDGEMENT_PAIRS.get(record.boundary)
        if expected is not None:
            if self._pending.get(record.transaction_id) is not expected:
                raise RuntimeError("live-session acknowledgement crosses transactions")
            del self._pending[record.transaction_id]

    def _update_client_observation(
        self,
        record: LiveSessionJournalRecordV1,
    ) -> None:
        if record.boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING:
            if record.transaction_id is None:
                raise RuntimeError("pending client observation lacks its transaction")
            self._pending_client_working_order_ids[record.transaction_id] = (
                _identifier_tuple(record.payload, "client_working_order_ids")
            )
        elif record.boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_ACKNOWLEDGED:
            if record.transaction_id is None:
                raise RuntimeError("client observation acknowledgement lacks transaction")
            candidate = self._pending_client_working_order_ids.pop(
                record.transaction_id,
                None,
            )
            if candidate is None:
                raise RuntimeError("client observation acknowledgement is unpaired")
            self._client_known_working_order_ids = candidate

    def _latest_state(self) -> Mapping[str, object]:
        for record in reversed(self._records):
            for key in ("state", "state_before"):
                value = record.payload.get(key)
                if isinstance(value, Mapping):
                    return value
        return {}

    def _write_pointer(self, record: LiveSessionJournalRecordV1) -> None:
        pointer = _pointer_path(self.paths, self.source.configuration_sha256)
        _require_real_directory(self.paths.checkpoints / "interactive")
        _ensure_real_directory(pointer.parent)
        raw = canonical_json_bytes(
            {
                "configuration_sha256": self.source.configuration_sha256,
                "journal_record_sha256": record.record_sha256,
                "schema_id": LIVE_SESSION_ACTIVE_POINTER_SCHEMA_ID_V1,
                "schema_version": LIVE_SESSION_RECOVERY_SCHEMA_VERSION_V1,
                "session_id": self.session_id,
            }
        )
        _write_atomic_file(pointer, raw)

    def _clear_pointer(self) -> None:
        self.paths.validate((DataAreaId.CHECKPOINTS,))
        pointer = _pointer_path(self.paths, self.source.configuration_sha256)
        if not pointer.exists() and not pointer.is_symlink():
            return
        _require_real_directory(pointer.parent)
        try:
            payload = _read_pointer(pointer)
        except (OSError, TypeError, ValueError):
            return
        if payload.get("session_id") == self.session_id:
            pointer.unlink()
            _fsync_directory(pointer.parent)


_PENDING_BOUNDARIES = frozenset(
    {
        RecoveryBoundaryKindV1.ACTION_PENDING,
        RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING,
        RecoveryBoundaryKindV1.PACK_ACTIVATION_PENDING,
        RecoveryBoundaryKindV1.PROFILE_UPDATE_PENDING,
    }
)

_ACKNOWLEDGEMENT_PAIRS = {
    RecoveryBoundaryKindV1.ACTION_ACKNOWLEDGED:
        RecoveryBoundaryKindV1.ACTION_PENDING,
    RecoveryBoundaryKindV1.CLIENT_MESSAGE_ACKNOWLEDGED:
        RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING,
    RecoveryBoundaryKindV1.PACK_ACTIVATION_COMMITTED:
        RecoveryBoundaryKindV1.PACK_ACTIVATION_PENDING,
    RecoveryBoundaryKindV1.PROFILE_UPDATE_COMMITTED:
        RecoveryBoundaryKindV1.PROFILE_UPDATE_PENDING,
}

_RECOVERY_STATE_FIELDS = frozenset(
    {
        "client_working_order_ids",
        "event_prefix_count",
        "event_prefix_sha256",
        "ledger_prefix_count",
        "ledger_prefix_sha256",
        "pending_delivery_ids",
        "pending_replay_ids",
        "runtime_state_sha256",
        "session_state_sha256",
        "simulation_time_us",
    }
)


def recovery_state_projection(
    session: LiveMarketSession,
    *,
    client_known_working_order_ids: tuple[str, ...] | None = None,
    pending_delivery_ids: tuple[str, ...] = (),
    pending_replay_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    events = [event.as_dict() for event in session.engine.book.journal.events]
    ledger = [record.as_dict() for record in session.timeline]
    runtime = session.branch_runtime_state()
    client_ids = (
        tuple(
            sorted(
                order.order_id
                for order in session.engine.book.active_orders.values()
                if order.owner.value == "player"
            )
        )
        if client_known_working_order_ids is None
        else client_known_working_order_ids
    )
    client_ids = _normalized_identifier_tuple(client_ids, "client working order IDs")
    delivery_ids = _normalized_identifier_tuple(
        pending_delivery_ids,
        "pending delivery IDs",
    )
    replay_ids = _normalized_identifier_tuple(
        pending_replay_ids,
        "pending replay IDs",
    )
    return {
        "client_working_order_ids": list(client_ids),
        "event_prefix_count": len(events),
        "event_prefix_sha256": _digest(events),
        "ledger_prefix_count": len(ledger),
        "ledger_prefix_sha256": _digest(ledger),
        "pending_delivery_ids": list(delivery_ids),
        "pending_replay_ids": list(replay_ids),
        "runtime_state_sha256": _digest(runtime),
        "session_state_sha256": session.state_sha256(),
        "simulation_time_us": session.simulation_time_us,
    }


def require_recovery_state_matches(
    session: LiveMarketSession,
    expected: object,
    *,
    label: str,
    client_known_working_order_ids: tuple[str, ...] | None = None,
    pending_delivery_ids: tuple[str, ...] | None = None,
    pending_replay_ids: tuple[str, ...] | None = None,
) -> None:
    if not isinstance(expected, Mapping):
        raise ValueError(f"{label} lacks a recovery state")
    durable = thaw_json(expected)
    if type(durable) is not dict or set(durable) != _RECOVERY_STATE_FIELDS:
        raise ValueError(f"{label} recovery-state fields differ")
    expected_client_ids = _identifier_tuple(durable, "client_working_order_ids")
    expected_delivery_ids = _identifier_tuple(durable, "pending_delivery_ids")
    expected_replay_ids = _identifier_tuple(durable, "pending_replay_ids")
    actual = recovery_state_projection(
        session,
        client_known_working_order_ids=(
            expected_client_ids
            if client_known_working_order_ids is None
            else client_known_working_order_ids
        ),
        pending_delivery_ids=(
            expected_delivery_ids
            if pending_delivery_ids is None
            else pending_delivery_ids
        ),
        pending_replay_ids=(
            expected_replay_ids
            if pending_replay_ids is None
            else pending_replay_ids
        ),
    )
    if actual != durable:
        raise RuntimeError(f"{label} does not reproduce its durable recovery state")


def _client_observation_state(
    records: tuple[LiveSessionJournalRecordV1, ...],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    known: tuple[str, ...] = ()
    pending: dict[str, tuple[str, ...]] = {}
    for record in records:
        if record.boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_PENDING:
            if record.transaction_id is None or record.transaction_id in pending:
                raise ValueError("journal client observation transaction is invalid")
            pending[record.transaction_id] = _identifier_tuple(
                record.payload,
                "client_working_order_ids",
            )
        elif record.boundary is RecoveryBoundaryKindV1.CLIENT_MESSAGE_ACKNOWLEDGED:
            if record.transaction_id is None:
                raise ValueError("journal client acknowledgement lacks transaction")
            candidate = pending.pop(record.transaction_id, None)
            if candidate is None:
                raise ValueError("journal client acknowledgement is unpaired")
            state = record.payload.get("state")
            if not isinstance(state, Mapping) or _identifier_tuple(
                state,
                "client_working_order_ids",
            ) != candidate:
                raise ValueError("journal acknowledged client observation differs")
            known = candidate
    return known, pending


def _pending_transactions(
    records: tuple[LiveSessionJournalRecordV1, ...],
) -> dict[str, RecoveryBoundaryKindV1]:
    pending: dict[str, RecoveryBoundaryKindV1] = {}
    for record in records:
        if record.transaction_id is None:
            continue
        if record.boundary in _PENDING_BOUNDARIES:
            if record.transaction_id in pending:
                raise ValueError("journal transaction ID is reused")
            pending[record.transaction_id] = record.boundary
        elif record.boundary in _ACKNOWLEDGEMENT_PAIRS:
            if pending.get(record.transaction_id) is not _ACKNOWLEDGEMENT_PAIRS[record.boundary]:
                raise ValueError("journal acknowledgement lacks its pending record")
            del pending[record.transaction_id]
    return pending


def _load_journal(
    path: Path,
    *,
    session_id: str,
) -> tuple[tuple[LiveSessionJournalRecordV1, ...], int]:
    raw = _read_regular_file(path, maximum_bytes=MAX_LIVE_JOURNAL_BYTES_V1)
    if not raw.endswith(b"\n"):
        raise ValueError("live-session journal has a partial final record")
    lines = raw[:-1].split(b"\n")
    if not lines or any(not line or len(line) > MAX_LIVE_RECORD_BYTES_V1 for line in lines):
        raise ValueError("live-session journal contains an invalid record boundary")
    records = tuple(LiveSessionJournalRecordV1.from_canonical_bytes(line) for line in lines)
    previous = None
    for sequence, record in enumerate(records, start=1):
        if (
            record.session_id != session_id
            or record.sequence != sequence
            or record.previous_record_sha256 != previous
        ):
            raise ValueError("live-session journal chain is forked or incomplete")
        previous = record.record_sha256
    _pending_transactions(records)
    return records, len(raw)


def _pointer_path(paths: DataPaths, configuration_sha256: str) -> Path:
    return (
        paths.checkpoints
        / "interactive"
        / "active"
        / f"{configuration_sha256}.json"
    )


def _read_pointer(path: Path) -> dict[str, object]:
    raw = _read_regular_file(path, maximum_bytes=MAX_LIVE_RECORD_BYTES_V1)
    value = load_canonical_json_bytes(raw, "live-session active pointer")
    expected = {
        "configuration_sha256",
        "journal_record_sha256",
        "schema_id",
        "schema_version",
        "session_id",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("live-session active pointer fields differ")
    if (
        value["schema_id"] != LIVE_SESSION_ACTIVE_POINTER_SCHEMA_ID_V1
        or value["schema_version"] != LIVE_SESSION_RECOVERY_SCHEMA_VERSION_V1
    ):
        raise ValueError("live-session active pointer contract differs")
    _session_id(_text(value, "session_id"))
    _sha256(_text(value, "configuration_sha256"), "pointer configuration")
    _sha256(_text(value, "journal_record_sha256"), "pointer journal record")
    return value


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise ValueError("recovery artifact is empty, linked, special, or oversized")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("recovery artifact changed during read")
        return raw
    finally:
        os.close(descriptor)


def _write_immutable_file(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        if _read_regular_file(path, maximum_bytes=MAX_LIVE_CHECKPOINT_BYTES_V1) != raw:
            raise RuntimeError("immutable recovery artifact already differs")
        return
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path)
        temporary.unlink()
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_atomic_file(path: Path, raw: bytes) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("recovery artifact write made no progress")
        view = view[written:]


def _ensure_real_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("recovery directory is linked or invalid")


def _require_real_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("recovery directory is missing, linked, or invalid")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest(value: object) -> str:
    # Built-in scenario definitions retain legacy binary-float fields in their
    # behavioral envelopes.  Recovery identity must preserve those exact values
    # without admitting JSON floats into canonical evidence, so reuse the frozen
    # release semantic projection that represents each float as its exact rational.
    from kirby2.release.performance import release_float_free_semantic

    projected = release_float_free_semantic(value)
    return hashlib.sha256(canonical_json_bytes(projected)).hexdigest()


def recovery_checkpoint_value_v1(value: object) -> object:
    """Project legacy recording floats into canonical, exact scalar records."""

    from kirby2.release.performance import release_float_free_semantic

    return release_float_free_semantic(value)


def restore_recovery_checkpoint_value_v1(value: object) -> object:
    """Restore exact scalar records for the legacy session-recording constructors."""

    if type(value) is list:
        return [restore_recovery_checkpoint_value_v1(item) for item in value]
    if type(value) is dict:
        marker = value.get(_RELEASE_SCALAR_RESERVED_V1)
        if marker == "EXACT_RATIONAL":
            if set(value) != {
                _RELEASE_SCALAR_RESERVED_V1,
                "denominator",
                "numerator",
            }:
                raise ValueError("recovery rational scalar fields differ")
            numerator = value["numerator"]
            denominator = value["denominator"]
            if (
                type(numerator) is not int
                or type(denominator) is not int
                or denominator <= 0
            ):
                raise ValueError("recovery rational scalar is invalid")
            restored = numerator / denominator
            if restored.as_integer_ratio() != (numerator, denominator):
                raise ValueError("recovery rational scalar is not an exact binary float")
            return restored
        if marker == "EXACT_DECIMAL":
            if set(value) != {
                _RELEASE_SCALAR_RESERVED_V1,
                "coefficient",
                "exponent",
            }:
                raise ValueError("recovery decimal scalar fields differ")
            coefficient = value["coefficient"]
            exponent = value["exponent"]
            if type(coefficient) is not int or type(exponent) is not int:
                raise ValueError("recovery decimal scalar is invalid")
            return Decimal(coefficient).scaleb(exponent)
        if marker is not None:
            raise ValueError("recovery scalar marker is unknown")
        return {
            key: restore_recovery_checkpoint_value_v1(item)
            for key, item in value.items()
        }
    return value


def _identifier_tuple(
    value: Mapping[str, object],
    key: str,
) -> tuple[str, ...]:
    selected = value.get(key)
    if not isinstance(selected, (list, tuple)):
        raise TypeError(f"{key} must be an array")
    if any(type(item) is not str for item in selected):
        raise TypeError(f"{key} must contain only text identifiers")
    return _normalized_identifier_tuple(tuple(selected), key)


def _normalized_identifier_tuple(
    values: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{label} must be a tuple")
    for value in values:
        _identifier(value, label)
    if len(values) != len(set(values)) or values != tuple(sorted(values)):
        raise ValueError(f"{label} must be unique and sorted")
    return values


def _identifier(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 256
        or any(character.isspace() or ord(character) < 0x20 for character in value)
        or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-"
            for character in value
        )
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _session_id(value: object) -> str:
    selected = _identifier(value, "live-session ID")
    prefix = "live-session-"
    suffix = selected.removeprefix(prefix)
    if (
        not selected.startswith(prefix)
        or not suffix
        or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
            for character in suffix
        )
    ):
        raise ValueError("live-session ID is not filename-safe")
    return selected


def _checkpoint_id(value: object) -> str:
    prefix = "live-checkpoint-"
    if (
        type(value) is not str
        or not value.startswith(prefix)
        or len(value) != len(prefix) + 24
        or any(character not in "0123456789abcdef" for character in value[len(prefix):])
    ):
        raise ValueError("live-session checkpoint ID is invalid")
    return value


def _sha256(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} digest is invalid")
    return value


def _text(value: dict[str, object], key: str) -> str:
    selected = value[key]
    if type(selected) is not str:
        raise TypeError(f"{key} must be text")
    return selected


def _integer(value: dict[str, object], key: str) -> int:
    selected = value[key]
    if type(selected) is not int:
        raise TypeError(f"{key} must be an integer")
    return selected


__all__ = [
    "LIVE_SESSION_ACTIVE_POINTER_SCHEMA_ID_V1",
    "LIVE_SESSION_CHECKPOINT_SCHEMA_ID_V1",
    "LIVE_SESSION_JOURNAL_SCHEMA_ID_V1",
    "LIVE_SESSION_RECOVERY_SCHEMA_VERSION_V1",
    "LiveSessionCheckpointV1",
    "LiveSessionJournalRecordV1",
    "LiveSessionJournalV1",
    "LiveSessionSourceV1",
    "TERMINAL_OBSERVATION_POLICY_ID_V1",
    "recovery_state_projection",
    "recovery_checkpoint_value_v1",
    "require_recovery_state_matches",
    "restore_recovery_checkpoint_value_v1",
]
