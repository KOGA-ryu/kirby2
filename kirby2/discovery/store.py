"""Durable, append-only strategy-discovery lineage for WO35-F.

The earlier discovery work orders define typed candidates, partition access, search,
robustness, and an in-process terminal reveal controller.  This module is the durable
boundary around those contracts.  A discovery is reconstructed from immutable
canonical records; no mutable "current state" file exists.  The reveal receipt is
also the globally single-use token file, so the filesystem operation that consumes a
token is the operation that makes the two terminal references visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import unicodedata
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from kirby2.immutable import freeze_json, thaw_json

from .identity import canonical_identity_bytes
from .observability import (
    ScientificConclusionV1,
    SealedTerminalMaterialV1,
    TerminalAccessRecordV1,
    TERMINAL_ROOT_ORDER_V1,
)
from .robustness import (
    RobustnessEvidenceV1,
    RobustnessOutcomeV1,
    RobustnessQualificationV1,
    qualify_robustness,
)


DISCOVERY_BINDING_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_DISCOVERY_BINDING_V1"
DISCOVERY_RECORD_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_DISCOVERY_RECORD_V1"
DISCOVERY_STORE_POLICY_ID_V1 = "APPEND_ONLY_DISCOVERY_LINEAGE_V1"
DISCOVERY_SCHEMA_VERSION_V1 = 1
DISCOVERY_CLAIM_SCOPE_V1 = "SIMULATOR_RESEARCH_WITHIN_NAMED_PARTITIONS_ONLY_V1"
SEALED_FIELD_MARKER_V1 = "SEALED_UNTIL_ATOMIC_TERMINAL_REVEAL_V1"
_BINDING_DOMAIN = b"KIRBY2_STRATEGY_DISCOVERY_BINDING_V1\x00"
_RECORD_DOMAIN = b"KIRBY2_STRATEGY_DISCOVERY_RECORD_V1\x00"
_LEDGER_DOMAIN = b"KIRBY2_STRATEGY_DISCOVERY_LEDGER_V1\x00"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DISCOVERY_ID = re.compile(r"^discovery-[0-9a-f]{24}$")


class DiscoveryStoreError(RuntimeError):
    """A deterministic persistence or lifecycle refusal."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class DiscoveryPhaseV1(str, Enum):
    NEW = "NEW"
    SEARCH_OPEN = "SEARCH_OPEN"
    CANDIDATES_FROZEN = "CANDIDATES_FROZEN"
    SELECTION_FROZEN = "SELECTION_FROZEN"
    ROBUSTNESS_PASSED = "ROBUSTNESS_PASSED"
    TERMINAL_REVEALED = "TERMINAL_REVEALED"
    HOLDOUT_COMPLETE = "HOLDOUT_COMPLETE"
    ADVERSARIAL_COMPLETE = "ADVERSARIAL_COMPLETE"
    CLOSED = "CLOSED"


class DiscoveryEventKindV1(str, Enum):
    CREATED = "CREATED"
    MUTATION_RECORDED = "MUTATION_RECORDED"
    REJECTION_RECORDED = "REJECTION_RECORDED"
    TRAINING_EVALUATED = "TRAINING_EVALUATED"
    CANDIDATES_FROZEN = "CANDIDATES_FROZEN"
    VALIDATION_EVALUATED = "VALIDATION_EVALUATED"
    SELECTION_FROZEN = "SELECTION_FROZEN"
    ROBUSTNESS_EVALUATED = "ROBUSTNESS_EVALUATED"
    ROBUSTNESS_REJECTED = "ROBUSTNESS_REJECTED"
    TERMINAL_REVEALED = "TERMINAL_REVEALED"
    HOLDOUT_EVALUATED = "HOLDOUT_EVALUATED"
    ADVERSARIAL_EVALUATED = "ADVERSARIAL_EVALUATED"
    WARNING_RECORDED = "WARNING_RECORDED"
    CLOSED = "CLOSED"


_CANDIDATE_EVENTS = frozenset(
    {
        DiscoveryEventKindV1.MUTATION_RECORDED,
        DiscoveryEventKindV1.TRAINING_EVALUATED,
        DiscoveryEventKindV1.VALIDATION_EVALUATED,
        DiscoveryEventKindV1.ROBUSTNESS_EVALUATED,
        DiscoveryEventKindV1.ROBUSTNESS_REJECTED,
        DiscoveryEventKindV1.HOLDOUT_EVALUATED,
        DiscoveryEventKindV1.ADVERSARIAL_EVALUATED,
    }
)
_RESULT_EVENTS = frozenset(
    {
        DiscoveryEventKindV1.TRAINING_EVALUATED,
        DiscoveryEventKindV1.VALIDATION_EVALUATED,
        DiscoveryEventKindV1.ROBUSTNESS_EVALUATED,
        DiscoveryEventKindV1.ROBUSTNESS_REJECTED,
        DiscoveryEventKindV1.HOLDOUT_EVALUATED,
        DiscoveryEventKindV1.ADVERSARIAL_EVALUATED,
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveryBindingV1:
    experiment_id: str
    implementation_commit: str
    base_source_sha256: str
    base_semantic_sha256: str
    experiment_manifest_sha256: str
    partition_manifest_sha256: str
    robustness_policy_sha256: str
    reveal_token_sha256: str
    development_only: bool
    real_partition_execution: bool
    schema_version: int = DISCOVERY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        _require_nfc(self.experiment_id, "discovery experiment ID")
        if _GIT_COMMIT.fullmatch(self.implementation_commit) is None:
            raise ValueError("discovery implementation commit must be exact Git SHA-1")
        for value, label in (
            (self.base_source_sha256, "base source digest"),
            (self.base_semantic_sha256, "base semantic digest"),
            (self.experiment_manifest_sha256, "experiment manifest digest"),
            (self.partition_manifest_sha256, "partition manifest digest"),
            (self.robustness_policy_sha256, "robustness policy digest"),
            (self.reveal_token_sha256, "reveal token digest"),
        ):
            _require_sha256(value, label)
        if type(self.development_only) is not bool or type(
            self.real_partition_execution
        ) is not bool:
            raise TypeError("discovery execution-scope flags must be Boolean")
        if self.development_only and self.real_partition_execution:
            raise ValueError("development discovery cannot execute real partitions")
        if self.schema_version != DISCOVERY_SCHEMA_VERSION_V1:
            raise ValueError("unsupported discovery binding schema")

    @property
    def binding_sha256(self) -> str:
        return _domain_digest(_BINDING_DOMAIN, self.canonical_bytes())

    @property
    def discovery_id(self) -> str:
        return "discovery-" + self.binding_sha256[:24]

    def as_dict(self) -> dict[str, object]:
        return {
            "base_semantic_sha256": self.base_semantic_sha256,
            "base_source_sha256": self.base_source_sha256,
            "development_only": self.development_only,
            "experiment_id": self.experiment_id,
            "experiment_manifest_sha256": self.experiment_manifest_sha256,
            "implementation_commit": self.implementation_commit,
            "partition_manifest_sha256": self.partition_manifest_sha256,
            "real_partition_execution": self.real_partition_execution,
            "reveal_token_sha256": self.reveal_token_sha256,
            "robustness_policy_sha256": self.robustness_policy_sha256,
            "schema_id": DISCOVERY_BINDING_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: object) -> DiscoveryBindingV1:
        row = _exact_object(
            payload,
            {
                "base_semantic_sha256",
                "base_source_sha256",
                "development_only",
                "experiment_id",
                "experiment_manifest_sha256",
                "implementation_commit",
                "partition_manifest_sha256",
                "real_partition_execution",
                "reveal_token_sha256",
                "robustness_policy_sha256",
                "schema_id",
                "schema_version",
            },
            "discovery binding",
        )
        if _text(row, "schema_id") != DISCOVERY_BINDING_SCHEMA_ID_V1:
            raise ValueError("unsupported discovery binding schema ID")
        return cls(
            experiment_id=_text(row, "experiment_id"),
            implementation_commit=_text(row, "implementation_commit"),
            base_source_sha256=_text(row, "base_source_sha256"),
            base_semantic_sha256=_text(row, "base_semantic_sha256"),
            experiment_manifest_sha256=_text(row, "experiment_manifest_sha256"),
            partition_manifest_sha256=_text(row, "partition_manifest_sha256"),
            robustness_policy_sha256=_text(row, "robustness_policy_sha256"),
            reveal_token_sha256=_text(row, "reveal_token_sha256"),
            development_only=_boolean(row, "development_only"),
            real_partition_execution=_boolean(row, "real_partition_execution"),
            schema_version=_integer(row, "schema_version"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> DiscoveryBindingV1:
        payload = _load_canonical_json(raw, "discovery binding")
        result = cls.from_dict(payload)
        if result.canonical_bytes() != raw:
            raise ValueError("discovery binding bytes are not canonical")
        return result


@dataclass(frozen=True, slots=True)
class DiscoveryRecordV1:
    discovery_id: str
    binding_sha256: str
    ordinal: int
    previous_record_sha256: str | None
    event_kind: DiscoveryEventKindV1
    phase_before: DiscoveryPhaseV1
    phase_after: DiscoveryPhaseV1
    candidate_semantic_sha256: str | None
    parent_semantic_sha256: str | None
    payload: Mapping[str, object]
    scientific_outcome: ScientificConclusionV1 | None = None
    schema_version: int = DISCOVERY_SCHEMA_VERSION_V1

    def __post_init__(self) -> None:
        if _DISCOVERY_ID.fullmatch(self.discovery_id) is None:
            raise ValueError("strategy discovery ID is invalid")
        _require_sha256(self.binding_sha256, "discovery binding digest")
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise ValueError("discovery record ordinal must be positive")
        if self.previous_record_sha256 is not None:
            _require_sha256(self.previous_record_sha256, "previous discovery record")
        if (self.ordinal == 1) != (self.previous_record_sha256 is None):
            raise ValueError("discovery record predecessor and ordinal disagree")
        if not isinstance(self.event_kind, DiscoveryEventKindV1):
            raise TypeError("discovery record event kind must be typed")
        if not isinstance(self.phase_before, DiscoveryPhaseV1) or not isinstance(
            self.phase_after,
            DiscoveryPhaseV1,
        ):
            raise TypeError("discovery record phases must be typed")
        for value, label in (
            (self.candidate_semantic_sha256, "record candidate semantic digest"),
            (self.parent_semantic_sha256, "record parent semantic digest"),
        ):
            if value is not None:
                _require_sha256(value, label)
        if not isinstance(self.payload, Mapping):
            raise TypeError("discovery record payload must be a mapping")
        detached = dict(self.payload)
        canonical_identity_bytes(detached)
        object.__setattr__(self, "payload", freeze_json(detached))
        if self.scientific_outcome is not None and not isinstance(
            self.scientific_outcome,
            ScientificConclusionV1,
        ):
            raise TypeError("discovery scientific outcome must be typed")
        if self.schema_version != DISCOVERY_SCHEMA_VERSION_V1:
            raise ValueError("unsupported discovery record schema")
        self._validate_event_shape()

    def _validate_event_shape(self) -> None:
        event = self.event_kind
        if event in _CANDIDATE_EVENTS and self.candidate_semantic_sha256 is None:
            raise ValueError(f"{event.value} requires a candidate semantic identity")
        if event is DiscoveryEventKindV1.MUTATION_RECORDED:
            if (
                self.parent_semantic_sha256 is None
                or self.parent_semantic_sha256 == self.candidate_semantic_sha256
            ):
                raise ValueError("mutation lineage requires distinct parent and child")
        elif self.parent_semantic_sha256 is not None:
            raise ValueError("only mutation records carry a parent semantic identity")
        if event is DiscoveryEventKindV1.CLOSED:
            if self.scientific_outcome is None:
                raise ValueError("closed discovery requires a scientific outcome")
        elif self.scientific_outcome is not None:
            raise ValueError("only the close record carries a scientific outcome")
        expected_after = _phase_after(event, self.phase_before)
        if self.phase_after is not expected_after:
            raise ValueError("discovery record phase transition is invalid")
        payload = thaw_json(self.payload)
        if event is DiscoveryEventKindV1.CREATED:
            _require_payload_keys(payload, {"claim_scope", "store_policy_id"}, event)
        elif event is DiscoveryEventKindV1.MUTATION_RECORDED:
            _require_payload_keys(
                payload,
                {"mutation_sha256", "operation_id", "operation_version", "semantic_diff"},
                event,
            )
        elif event is DiscoveryEventKindV1.REJECTION_RECORDED:
            _require_payload_keys(payload, {"rejection_reason", "request_sha256"}, event)
        elif event in _RESULT_EVENTS:
            _require_payload_keys(
                payload,
                {"data_source", "evidence_sha256", "partition", "result"},
                event,
            )
        elif event is DiscoveryEventKindV1.CANDIDATES_FROZEN:
            _require_payload_keys(
                payload,
                {
                    "candidate_semantic_sha256",
                    "freeze_sha256",
                    "training_star_semantic_sha256",
                },
                event,
            )
        elif event is DiscoveryEventKindV1.SELECTION_FROZEN:
            _require_payload_keys(
                payload,
                {
                    "sealed_material_commitment_sha256",
                    "selected_candidate_semantic_sha256",
                    "selection_sha256",
                },
                event,
            )
        elif event is DiscoveryEventKindV1.TERMINAL_REVEALED:
            _require_payload_keys(
                payload,
                {
                    "access_record",
                    "access_recorded_before_exposure",
                    "adversarial",
                    "execution_order",
                    "holdout",
                },
                event,
            )
            if payload["access_recorded_before_exposure"] is not True:
                raise ValueError("terminal references were exposed before access record")
            if tuple(payload["execution_order"]) != TERMINAL_ROOT_ORDER_V1:
                raise ValueError("terminal execution order changed")
        elif event is DiscoveryEventKindV1.WARNING_RECORDED:
            _require_payload_keys(payload, {"warning_code", "warning_detail"}, event)
        elif event is DiscoveryEventKindV1.CLOSED:
            _require_payload_keys(payload, {"conclusion_detail"}, event)

    @property
    def record_sha256(self) -> str:
        return _domain_digest(_RECORD_DOMAIN, self.canonical_bytes())

    def as_dict(self) -> dict[str, object]:
        return {
            "binding_sha256": self.binding_sha256,
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "discovery_id": self.discovery_id,
            "event_kind": self.event_kind.value,
            "ordinal": self.ordinal,
            "parent_semantic_sha256": self.parent_semantic_sha256,
            "payload": thaw_json(self.payload),
            "phase_after": self.phase_after.value,
            "phase_before": self.phase_before.value,
            "previous_record_sha256": self.previous_record_sha256,
            "schema_id": DISCOVERY_RECORD_SCHEMA_ID_V1,
            "schema_version": self.schema_version,
            "scientific_outcome": (
                None if self.scientific_outcome is None else self.scientific_outcome.value
            ),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, payload: object) -> DiscoveryRecordV1:
        row = _exact_object(
            payload,
            {
                "binding_sha256",
                "candidate_semantic_sha256",
                "discovery_id",
                "event_kind",
                "ordinal",
                "parent_semantic_sha256",
                "payload",
                "phase_after",
                "phase_before",
                "previous_record_sha256",
                "schema_id",
                "schema_version",
                "scientific_outcome",
            },
            "discovery record",
        )
        if _text(row, "schema_id") != DISCOVERY_RECORD_SCHEMA_ID_V1:
            raise ValueError("unsupported discovery record schema ID")
        raw_payload = row["payload"]
        if not isinstance(raw_payload, dict):
            raise TypeError("discovery record payload must be an object")
        raw_outcome = row["scientific_outcome"]
        if raw_outcome is not None and type(raw_outcome) is not str:
            raise TypeError("scientific outcome must be text or null")
        return cls(
            discovery_id=_text(row, "discovery_id"),
            binding_sha256=_text(row, "binding_sha256"),
            ordinal=_integer(row, "ordinal"),
            previous_record_sha256=_nullable_text(row, "previous_record_sha256"),
            event_kind=DiscoveryEventKindV1(_text(row, "event_kind")),
            phase_before=DiscoveryPhaseV1(_text(row, "phase_before")),
            phase_after=DiscoveryPhaseV1(_text(row, "phase_after")),
            candidate_semantic_sha256=_nullable_text(
                row,
                "candidate_semantic_sha256",
            ),
            parent_semantic_sha256=_nullable_text(row, "parent_semantic_sha256"),
            payload=raw_payload,
            scientific_outcome=(
                None if raw_outcome is None else ScientificConclusionV1(raw_outcome)
            ),
            schema_version=_integer(row, "schema_version"),
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> DiscoveryRecordV1:
        payload = _load_canonical_json(raw, "discovery record")
        result = cls.from_dict(payload)
        if result.canonical_bytes() != raw:
            raise ValueError("discovery record bytes are not canonical")
        return result


@dataclass(frozen=True, slots=True)
class DiscoveryLedgerV1:
    binding: DiscoveryBindingV1
    records: tuple[DiscoveryRecordV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DiscoveryBindingV1):
            raise TypeError("discovery ledger binding must be typed")
        if type(self.records) is not tuple or not self.records or any(
            not isinstance(item, DiscoveryRecordV1) for item in self.records
        ):
            raise TypeError("discovery ledger records must be a nonempty typed tuple")
        previous: str | None = None
        phase = DiscoveryPhaseV1.NEW
        for ordinal, record in enumerate(self.records, start=1):
            if (
                record.discovery_id != self.binding.discovery_id
                or record.binding_sha256 != self.binding.binding_sha256
                or record.ordinal != ordinal
                or record.previous_record_sha256 != previous
                or record.phase_before is not phase
            ):
                raise ValueError("discovery ledger record chain is forked or incomplete")
            previous = record.record_sha256
            phase = record.phase_after
        if self.records[0].event_kind is not DiscoveryEventKindV1.CREATED:
            raise ValueError("discovery ledger must begin with CREATED")
        if any(
            record.event_kind is DiscoveryEventKindV1.CREATED
            for record in self.records[1:]
        ):
            raise ValueError("discovery ledger contains more than one creation")
        self._validate_semantics()

    def _validate_semantics(self) -> None:
        candidates: set[str] = {self.binding.base_semantic_sha256}
        frozen: tuple[str, ...] | None = None
        selected: str | None = None
        result_keys: set[tuple[DiscoveryEventKindV1, str]] = set()
        reveal_count = 0
        for record in self.records:
            payload = thaw_json(record.payload)
            candidate = record.candidate_semantic_sha256
            if record.event_kind is DiscoveryEventKindV1.MUTATION_RECORDED:
                assert candidate is not None
                if record.parent_semantic_sha256 not in candidates:
                    raise ValueError("discovery mutation parent is absent from prior lineage")
                if candidate in candidates:
                    raise ValueError("discovery lineage repeats a candidate semantic identity")
                candidates.add(candidate)
            if (
                record.event_kind is DiscoveryEventKindV1.REJECTION_RECORDED
                and candidate is not None
                and candidate not in candidates
            ):
                raise ValueError("discovery rejection references an unknown candidate")
            if record.event_kind in _RESULT_EVENTS:
                assert candidate is not None
                if candidate not in candidates:
                    raise ValueError("discovery evaluation references an unknown candidate")
                key = (record.event_kind, candidate)
                if key in result_keys:
                    raise ValueError("discovery contains conflicting duplicate results")
                result_keys.add(key)
                if self.binding.development_only and payload.get(
                    "real_partition_access_count",
                    0,
                ) != 0:
                    raise ValueError("development lineage accessed a real partition")
            if record.event_kind is DiscoveryEventKindV1.CANDIDATES_FROZEN:
                raw_candidates = payload["candidate_semantic_sha256"]
                if not isinstance(raw_candidates, list) or not raw_candidates:
                    raise ValueError("candidate freeze inventory must be nonempty")
                frozen = tuple(str(item) for item in raw_candidates)
                if len(frozen) != len(set(frozen)) or not set(frozen) <= candidates:
                    raise ValueError("candidate freeze contains duplicate or unknown identity")
                if payload["training_star_semantic_sha256"] not in frozen:
                    raise ValueError("training star is absent from frozen finalists")
            if record.event_kind is DiscoveryEventKindV1.VALIDATION_EVALUATED:
                if frozen is None or candidate not in frozen:
                    raise ValueError("validation result is outside the frozen finalist set")
            if record.event_kind is DiscoveryEventKindV1.SELECTION_FROZEN:
                selected = str(payload["selected_candidate_semantic_sha256"])
                if frozen is None or selected not in frozen:
                    raise ValueError("selection is outside the frozen finalist set")
            if record.event_kind in {
                DiscoveryEventKindV1.ROBUSTNESS_EVALUATED,
                DiscoveryEventKindV1.ROBUSTNESS_REJECTED,
                DiscoveryEventKindV1.HOLDOUT_EVALUATED,
                DiscoveryEventKindV1.ADVERSARIAL_EVALUATED,
            } and candidate != selected:
                raise ValueError("sealed partition result is not for the frozen selection")
            if record.event_kind is DiscoveryEventKindV1.TERMINAL_REVEALED:
                reveal_count += 1
                access = payload["access_record"]
                if not isinstance(access, dict) or access.get(
                    "candidate_semantic_sha256"
                ) != selected:
                    raise ValueError("terminal reveal is not bound to the frozen selection")
        if reveal_count > 1:
            raise ValueError("discovery ledger contains repeated terminal reveal")
        if self.current_phase is DiscoveryPhaseV1.CLOSED:
            outcome = self.scientific_outcome
            assert outcome is not None
            if (
                outcome is ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE
                and not any(
                    item.event_kind is DiscoveryEventKindV1.ADVERSARIAL_EVALUATED
                    for item in self.records
                )
            ):
                raise ValueError("confirmed conclusion lacks adversarial evidence")

    @property
    def discovery_id(self) -> str:
        return self.binding.discovery_id

    @property
    def current_phase(self) -> DiscoveryPhaseV1:
        return self.records[-1].phase_after

    @property
    def selected_candidate_semantic_sha256(self) -> str | None:
        for record in reversed(self.records):
            if record.event_kind is DiscoveryEventKindV1.SELECTION_FROZEN:
                return str(thaw_json(record.payload)["selected_candidate_semantic_sha256"])
        return None

    @property
    def scientific_outcome(self) -> ScientificConclusionV1 | None:
        return self.records[-1].scientific_outcome

    @property
    def reveal_record(self) -> DiscoveryRecordV1 | None:
        return next(
            (
                item
                for item in self.records
                if item.event_kind is DiscoveryEventKindV1.TERMINAL_REVEALED
            ),
            None,
        )

    @property
    def ledger_sha256(self) -> str:
        projection = {
            "binding_sha256": self.binding.binding_sha256,
            "record_sha256": [item.record_sha256 for item in self.records],
        }
        return _domain_digest(_LEDGER_DOMAIN, canonical_identity_bytes(projection))

    def as_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.as_dict(),
            "current_phase": self.current_phase.value,
            "discovery_id": self.discovery_id,
            "ledger_sha256": self.ledger_sha256,
            "record_sha256": [item.record_sha256 for item in self.records],
            "scientific_outcome": (
                None if self.scientific_outcome is None else self.scientific_outcome.value
            ),
            "selected_candidate_semantic_sha256": (
                self.selected_candidate_semantic_sha256
            ),
        }


@dataclass(frozen=True, slots=True)
class DiscoveryVerificationV1:
    discovery_id: str
    binding_valid: bool
    record_chain_valid: bool
    canonical_bytes_valid: bool
    file_inventory_valid: bool
    single_use_reveal_valid: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.binding_valid,
                self.record_chain_valid,
                self.canonical_bytes_valid,
                self.file_inventory_valid,
                self.single_use_reveal_valid,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "binding_valid": self.binding_valid,
            "canonical_bytes_valid": self.canonical_bytes_valid,
            "discovery_id": self.discovery_id,
            "failures": list(self.failures),
            "file_inventory_valid": self.file_inventory_valid,
            "record_chain_valid": self.record_chain_valid,
            "single_use_reveal_valid": self.single_use_reveal_valid,
            "status": "PASS" if self.passed else "FAIL",
        }


class DiscoveryStore:
    """Filesystem-backed immutable discovery store with deterministic recovery."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError("discovery store root must be pathlib.Path")
        self.root = root
        self.discoveries_directory = root / "discoveries"
        self.tokens_directory = root / "reveal-tokens"
        self.staging_directory = root / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise DiscoveryStoreError("UNSAFE_STORE_ROOT", "store root cannot be a symlink")
        self.discoveries_directory.mkdir(exist_ok=True)
        self.tokens_directory.mkdir(exist_ok=True)
        self.staging_directory.mkdir(exist_ok=True)
        if any(
            item.is_symlink()
            for item in (
                self.discoveries_directory,
                self.tokens_directory,
                self.staging_directory,
            )
        ):
            raise DiscoveryStoreError(
                "UNSAFE_STORE_DIRECTORY",
                "discovery store directories cannot be symlinks",
            )

    def create(self, binding: DiscoveryBindingV1) -> DiscoveryLedgerV1:
        if not isinstance(binding, DiscoveryBindingV1):
            raise TypeError("discovery creation requires a typed binding")
        with self._lock():
            target = self._discovery_directory(binding.discovery_id)
            if target.exists() or target.is_symlink():
                ledger = self._load_unlocked(binding.discovery_id)
                if ledger.binding != binding:
                    raise DiscoveryStoreError(
                        "DISCOVERY_ID_COLLISION",
                        "existing discovery has a different immutable binding",
                    )
                return ledger
            created = DiscoveryRecordV1(
                discovery_id=binding.discovery_id,
                binding_sha256=binding.binding_sha256,
                ordinal=1,
                previous_record_sha256=None,
                event_kind=DiscoveryEventKindV1.CREATED,
                phase_before=DiscoveryPhaseV1.NEW,
                phase_after=DiscoveryPhaseV1.SEARCH_OPEN,
                candidate_semantic_sha256=None,
                parent_semantic_sha256=None,
                payload={
                    "claim_scope": DISCOVERY_CLAIM_SCOPE_V1,
                    "store_policy_id": DISCOVERY_STORE_POLICY_ID_V1,
                },
            )
            with tempfile.TemporaryDirectory(
                dir=self.staging_directory,
                prefix=f"{binding.discovery_id}-",
            ) as temporary:
                staged = Path(temporary) / binding.discovery_id
                records = staged / "records"
                records.mkdir(parents=True)
                _write_fsync(staged / "binding.json", binding.canonical_bytes())
                _write_fsync(
                    records / _record_filename(created),
                    created.canonical_bytes(),
                )
                _fsync_directory(records)
                _fsync_directory(staged)
                try:
                    os.rename(staged, target)
                except FileExistsError:
                    ledger = self._load_unlocked(binding.discovery_id)
                    if ledger.binding != binding:
                        raise DiscoveryStoreError(
                            "DISCOVERY_ID_COLLISION",
                            "concurrent discovery has another immutable binding",
                        )
                    return ledger
                _fsync_directory(self.discoveries_directory)
            return self._load_unlocked(binding.discovery_id)

    def append_event(
        self,
        discovery_id: str,
        event_kind: DiscoveryEventKindV1,
        *,
        payload: Mapping[str, object],
        candidate_semantic_sha256: str | None = None,
        parent_semantic_sha256: str | None = None,
        scientific_outcome: ScientificConclusionV1 | None = None,
    ) -> DiscoveryRecordV1:
        _require_discovery_id(discovery_id)
        if not isinstance(event_kind, DiscoveryEventKindV1):
            raise TypeError("discovery event kind must be typed")
        if event_kind is DiscoveryEventKindV1.TERMINAL_REVEALED:
            raise DiscoveryStoreError(
                "REVEAL_REQUIRES_ATOMIC_CONSUME",
                "terminal reveal must use consume_reveal_token",
            )
        with self._lock():
            ledger = self._load_unlocked(discovery_id)
            return self._append_unlocked(
                ledger,
                event_kind,
                payload=payload,
                candidate_semantic_sha256=candidate_semantic_sha256,
                parent_semantic_sha256=parent_semantic_sha256,
                scientific_outcome=scientific_outcome,
            )

    def record_robustness(
        self,
        discovery_id: str,
        evidence: RobustnessEvidenceV1,
        qualification: RobustnessQualificationV1,
        *,
        data_source: str = "TYPED_ROBUSTNESS_EVIDENCE_V1",
        real_partition_access_count: int = 0,
    ) -> DiscoveryRecordV1:
        if not isinstance(evidence, RobustnessEvidenceV1) or not isinstance(
            qualification,
            RobustnessQualificationV1,
        ):
            raise TypeError("durable robustness requires typed evidence and qualification")
        expected = qualify_robustness(evidence)
        if qualification != expected or qualification.evidence_sha256 != evidence.evidence_sha256:
            raise DiscoveryStoreError(
                "ROBUSTNESS_BINDING_MISMATCH",
                "stored robustness qualification does not recompute exactly",
            )
        _require_nfc(data_source, "robustness data source")
        if (
            type(real_partition_access_count) is not int
            or real_partition_access_count < 0
        ):
            raise ValueError("robustness real-partition access count must be nonnegative")
        payload = {
            "data_source": data_source,
            "evidence_sha256": evidence.evidence_sha256,
            "partition": "ROBUSTNESS",
            "qualification": qualification.as_dict(),
            "real_partition_access_count": real_partition_access_count,
            "result": {
                "evidence": evidence.as_dict(),
                "qualification": qualification.as_dict(),
            },
        }
        return self.append_event(
            discovery_id,
            (
                DiscoveryEventKindV1.ROBUSTNESS_EVALUATED
                if qualification.outcome is RobustnessOutcomeV1.PASSED
                else DiscoveryEventKindV1.ROBUSTNESS_REJECTED
            ),
            payload=payload,
            candidate_semantic_sha256=evidence.candidate_semantic_sha256,
        )

    def consume_reveal_token(
        self,
        discovery_id: str,
        material: SealedTerminalMaterialV1,
        *,
        reveal_token: str,
    ) -> DiscoveryRecordV1:
        _require_discovery_id(discovery_id)
        if not isinstance(material, SealedTerminalMaterialV1):
            raise TypeError("durable reveal requires typed sealed terminal material")
        _require_nfc(reveal_token, "reveal token")
        with self._lock():
            ledger = self._load_unlocked(discovery_id)
            from .observability import _token_sha256

            token_sha256 = _token_sha256(reveal_token)
            token_path = self.tokens_directory / f"{token_sha256}.json"
            if token_path.exists() or token_path.is_symlink():
                raise DiscoveryStoreError(
                    "REVEAL_ALREADY_CONSUMED",
                    "the durable reveal token was already consumed",
                )
            if ledger.current_phase is not DiscoveryPhaseV1.ROBUSTNESS_PASSED:
                raise DiscoveryStoreError(
                    "ROBUSTNESS_NOT_PASSED",
                    "durable reveal requires exactly one passing robustness result",
                )
            if ledger.selected_candidate_semantic_sha256 != material.candidate_semantic_sha256:
                raise DiscoveryStoreError(
                    "SEALED_MATERIAL_MISMATCH",
                    "sealed terminal material is bound to another candidate",
                )
            selection = next(
                item
                for item in ledger.records
                if item.event_kind is DiscoveryEventKindV1.SELECTION_FROZEN
            )
            selection_payload = thaw_json(selection.payload)
            if selection_payload["sealed_material_commitment_sha256"] != (
                material.commitment_sha256
            ):
                raise DiscoveryStoreError(
                    "SEALED_MATERIAL_MISMATCH",
                    "sealed material differs from the frozen selection commitment",
                )
            if (
                token_sha256 != material.reveal_token_sha256
                or token_sha256 != ledger.binding.reveal_token_sha256
            ):
                raise DiscoveryStoreError(
                    "REVEAL_TOKEN_MISMATCH",
                    "reveal token differs from the committed token",
                )
            robustness = next(
                item
                for item in reversed(ledger.records)
                if item.event_kind is DiscoveryEventKindV1.ROBUSTNESS_EVALUATED
            )
            robustness_sha256 = str(thaw_json(robustness.payload)["evidence_sha256"])
            access = TerminalAccessRecordV1(
                access_ordinal=1,
                candidate_semantic_sha256=material.candidate_semantic_sha256,
                sealed_material_commitment_sha256=material.commitment_sha256,
                robustness_evidence_sha256=robustness_sha256,
                partitions=(material.holdout.partition, material.adversarial.partition),
                token_sha256=token_sha256,
            )
            record = DiscoveryRecordV1(
                discovery_id=discovery_id,
                binding_sha256=ledger.binding.binding_sha256,
                ordinal=len(ledger.records) + 1,
                previous_record_sha256=ledger.records[-1].record_sha256,
                event_kind=DiscoveryEventKindV1.TERMINAL_REVEALED,
                phase_before=ledger.current_phase,
                phase_after=DiscoveryPhaseV1.TERMINAL_REVEALED,
                candidate_semantic_sha256=None,
                parent_semantic_sha256=None,
                payload={
                    "access_record": access.as_dict(),
                    "access_recorded_before_exposure": True,
                    "adversarial": material.adversarial.as_dict(),
                    "execution_order": list(TERMINAL_ROOT_ORDER_V1),
                    "holdout": material.holdout.as_dict(),
                },
            )
            # The receipt is both token consumption and reference exposure.  A single
            # no-replace link makes those two facts indivisible to every reader.
            self._record_exact_file(token_path, record.canonical_bytes())
            return record

    def close(
        self,
        discovery_id: str,
        outcome: ScientificConclusionV1,
        *,
        conclusion_detail: str,
    ) -> DiscoveryRecordV1:
        if not isinstance(outcome, ScientificConclusionV1):
            raise TypeError("discovery close requires a typed scientific conclusion")
        _require_nfc(conclusion_detail, "scientific conclusion detail")
        with self._lock():
            ledger = self._load_unlocked(discovery_id)
            if (
                outcome is ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE
                and ledger.current_phase is not DiscoveryPhaseV1.ADVERSARIAL_COMPLETE
            ):
                raise DiscoveryStoreError(
                    "CONFIRMATION_SCOPE_INCOMPLETE",
                    "confirmation requires validation, robustness, holdout, and adversarial evidence",
                )
            return self._append_unlocked(
                ledger,
                DiscoveryEventKindV1.CLOSED,
                payload={"conclusion_detail": conclusion_detail},
                scientific_outcome=outcome,
            )

    def load(self, discovery_id: str) -> DiscoveryLedgerV1:
        _require_discovery_id(discovery_id)
        with self._lock(shared=True):
            return self._load_unlocked(discovery_id)

    def list_discoveries(self) -> tuple[str, ...]:
        rows = []
        for path in sorted(self.discoveries_directory.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise DiscoveryStoreError(
                    "UNSAFE_DISCOVERY_INVENTORY",
                    f"unexpected discovery entry: {path.name}",
                )
            _require_discovery_id(path.name)
            rows.append(path.name)
        return tuple(rows)

    def verify(self, discovery_id: str) -> DiscoveryVerificationV1:
        failures: list[str] = []
        flags = {
            "binding_valid": False,
            "record_chain_valid": False,
            "canonical_bytes_valid": False,
            "file_inventory_valid": False,
            "single_use_reveal_valid": False,
        }
        try:
            ledger = self.load(discovery_id)
            flags["binding_valid"] = ledger.binding.discovery_id == discovery_id
            flags["record_chain_valid"] = True
            flags["canonical_bytes_valid"] = True
            directory = self._discovery_directory(discovery_id)
            ordinary = tuple(sorted((directory / "records").glob("*.json")))
            ordinary_records = tuple(
                item
                for item in ledger.records
                if item.event_kind is not DiscoveryEventKindV1.TERMINAL_REVEALED
            )
            expected_names = {_record_filename(item) for item in ordinary_records}
            flags["file_inventory_valid"] = (
                {item.name for item in ordinary} == expected_names
                and not tuple(directory.rglob("*.tmp"))
            )
            if not flags["file_inventory_valid"]:
                failures.append("discovery record file inventory differs from ledger")
            reveal_records = tuple(
                item
                for item in ledger.records
                if item.event_kind is DiscoveryEventKindV1.TERMINAL_REVEALED
            )
            token_matches = []
            for path in sorted(self.tokens_directory.glob("*.json")):
                record = DiscoveryRecordV1.from_json_bytes(self._read_exact_file(path))
                if record.discovery_id == discovery_id:
                    token_matches.append(path)
            flags["single_use_reveal_valid"] = len(token_matches) == len(
                reveal_records
            ) <= 1
            if reveal_records and token_matches:
                access = thaw_json(reveal_records[0].payload)["access_record"]
                flags["single_use_reveal_valid"] = (
                    flags["single_use_reveal_valid"]
                    and token_matches[0].stem == access["token_sha256"]
                )
            if not flags["single_use_reveal_valid"]:
                failures.append("durable reveal token inventory is inconsistent")
        except Exception as error:
            failures.append(str(error))
        return DiscoveryVerificationV1(
            discovery_id,
            flags["binding_valid"],
            flags["record_chain_valid"],
            flags["canonical_bytes_valid"],
            flags["file_inventory_valid"],
            flags["single_use_reveal_valid"],
            tuple(failures),
        )

    def _append_unlocked(
        self,
        ledger: DiscoveryLedgerV1,
        event_kind: DiscoveryEventKindV1,
        *,
        payload: Mapping[str, object],
        candidate_semantic_sha256: str | None = None,
        parent_semantic_sha256: str | None = None,
        scientific_outcome: ScientificConclusionV1 | None = None,
    ) -> DiscoveryRecordV1:
        if ledger.current_phase is DiscoveryPhaseV1.CLOSED:
            raise DiscoveryStoreError(
                "DISCOVERY_ALREADY_CLOSED",
                "closed discovery lineage cannot be extended",
            )
        record = DiscoveryRecordV1(
            discovery_id=ledger.discovery_id,
            binding_sha256=ledger.binding.binding_sha256,
            ordinal=len(ledger.records) + 1,
            previous_record_sha256=ledger.records[-1].record_sha256,
            event_kind=event_kind,
            phase_before=ledger.current_phase,
            phase_after=_phase_after(event_kind, ledger.current_phase),
            candidate_semantic_sha256=candidate_semantic_sha256,
            parent_semantic_sha256=parent_semantic_sha256,
            payload=payload,
            scientific_outcome=scientific_outcome,
        )
        key = _logical_key(record)
        if key is not None:
            existing = next(
                (item for item in ledger.records if _logical_key(item) == key),
                None,
            )
            if existing is not None:
                if _record_content(existing) == _record_content(record):
                    return existing
                raise DiscoveryStoreError(
                    "CONFLICTING_DISCOVERY_RESULT",
                    f"immutable result already exists for {key}",
                )
        prospective = DiscoveryLedgerV1(ledger.binding, (*ledger.records, record))
        assert prospective.records[-1] == record
        path = self._discovery_directory(ledger.discovery_id) / "records" / (
            _record_filename(record)
        )
        self._record_exact_file(path, record.canonical_bytes())
        return record

    def _load_unlocked(self, discovery_id: str) -> DiscoveryLedgerV1:
        directory = self._discovery_directory(discovery_id)
        if directory.is_symlink() or not directory.is_dir():
            raise DiscoveryStoreError(
                "UNKNOWN_OR_UNSAFE_DISCOVERY",
                f"unknown or unsafe discovery: {discovery_id}",
            )
        records_directory = directory / "records"
        if records_directory.is_symlink() or not records_directory.is_dir():
            raise DiscoveryStoreError(
                "UNSAFE_RECORD_DIRECTORY",
                "discovery records directory is missing or unsafe",
            )
        binding_raw = self._read_exact_file(directory / "binding.json")
        binding = DiscoveryBindingV1.from_json_bytes(binding_raw)
        if binding.discovery_id != discovery_id:
            raise DiscoveryStoreError(
                "DISCOVERY_BINDING_PATH_MISMATCH",
                "discovery binding identity differs from its directory",
            )
        records: list[DiscoveryRecordV1] = []
        for path in sorted(records_directory.glob("*.json")):
            record = DiscoveryRecordV1.from_json_bytes(self._read_exact_file(path))
            if path.name != _record_filename(record):
                raise DiscoveryStoreError(
                    "DISCOVERY_RECORD_PATH_MISMATCH",
                    "record identity differs from its filename",
                )
            records.append(record)
        for path in sorted(self.tokens_directory.glob("*.json")):
            record = DiscoveryRecordV1.from_json_bytes(self._read_exact_file(path))
            if record.event_kind is not DiscoveryEventKindV1.TERMINAL_REVEALED:
                raise DiscoveryStoreError(
                    "INVALID_REVEAL_RECEIPT",
                    "token directory contains a non-reveal record",
                )
            access = thaw_json(record.payload)["access_record"]
            if path.stem != access["token_sha256"]:
                raise DiscoveryStoreError(
                    "REVEAL_TOKEN_PATH_MISMATCH",
                    "reveal receipt token differs from its filename",
                )
            if record.discovery_id == discovery_id:
                records.append(record)
        records.sort(key=lambda item: (item.ordinal, item.record_sha256))
        return DiscoveryLedgerV1(binding, tuple(records))

    def _record_exact_file(self, path: Path, raw: bytes) -> None:
        if path.parent not in {
            self.tokens_directory,
            *(item / "records" for item in self.discoveries_directory.iterdir()),
        }:
            raise DiscoveryStoreError(
                "UNSAFE_DISCOVERY_PATH",
                "artifact path is outside the discovery store",
            )
        if type(raw) is not bytes:
            raise TypeError("discovery artifact must be exact bytes")
        if path.exists() or path.is_symlink():
            if self._read_exact_file(path) == raw:
                return
            raise DiscoveryStoreError(
                "IMMUTABLE_ARTIFACT_CONFLICT",
                f"existing immutable artifact differs: {path.name}",
            )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self._read_exact_file(path) != raw:
                    raise DiscoveryStoreError(
                        "IMMUTABLE_ARTIFACT_CONFLICT",
                        f"concurrent immutable artifact differs: {path.name}",
                    )
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_exact_file(self, path: Path) -> bytes:
        if path.is_symlink():
            raise DiscoveryStoreError(
                "UNSAFE_DISCOVERY_ARTIFACT",
                f"symlink artifact refused: {path.name}",
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise DiscoveryStoreError(
                "UNKNOWN_OR_UNSAFE_ARTIFACT",
                f"cannot open immutable artifact: {path.name}",
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise DiscoveryStoreError(
                    "UNSAFE_DISCOVERY_ARTIFACT",
                    f"artifact is not a regular file: {path.name}",
                )
            chunks = []
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _discovery_directory(self, discovery_id: str) -> Path:
        _require_discovery_id(discovery_id)
        return self.discoveries_directory / discovery_id

    @contextmanager
    def _lock(self, *, shared: bool = False):
        import fcntl

        lock_path = self.root / "discovery.lock"
        with lock_path.open("a+b") as stream:
            fcntl.flock(
                stream.fileno(),
                fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
            )
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _phase_after(
    event: DiscoveryEventKindV1,
    before: DiscoveryPhaseV1,
) -> DiscoveryPhaseV1:
    if event is DiscoveryEventKindV1.CREATED and before is DiscoveryPhaseV1.NEW:
        return DiscoveryPhaseV1.SEARCH_OPEN
    if event in {
        DiscoveryEventKindV1.MUTATION_RECORDED,
        DiscoveryEventKindV1.TRAINING_EVALUATED,
    } and before is DiscoveryPhaseV1.SEARCH_OPEN:
        return before
    if event is DiscoveryEventKindV1.REJECTION_RECORDED and before in {
        DiscoveryPhaseV1.SEARCH_OPEN,
        DiscoveryPhaseV1.CANDIDATES_FROZEN,
        DiscoveryPhaseV1.SELECTION_FROZEN,
    }:
        return before
    if (
        event is DiscoveryEventKindV1.CANDIDATES_FROZEN
        and before is DiscoveryPhaseV1.SEARCH_OPEN
    ):
        return DiscoveryPhaseV1.CANDIDATES_FROZEN
    if (
        event is DiscoveryEventKindV1.VALIDATION_EVALUATED
        and before is DiscoveryPhaseV1.CANDIDATES_FROZEN
    ):
        return before
    if (
        event is DiscoveryEventKindV1.SELECTION_FROZEN
        and before is DiscoveryPhaseV1.CANDIDATES_FROZEN
    ):
        return DiscoveryPhaseV1.SELECTION_FROZEN
    if (
        event is DiscoveryEventKindV1.ROBUSTNESS_EVALUATED
        and before is DiscoveryPhaseV1.SELECTION_FROZEN
    ):
        return DiscoveryPhaseV1.ROBUSTNESS_PASSED
    if (
        event is DiscoveryEventKindV1.ROBUSTNESS_REJECTED
        and before is DiscoveryPhaseV1.SELECTION_FROZEN
    ):
        return before
    if (
        event is DiscoveryEventKindV1.TERMINAL_REVEALED
        and before is DiscoveryPhaseV1.ROBUSTNESS_PASSED
    ):
        return DiscoveryPhaseV1.TERMINAL_REVEALED
    if (
        event is DiscoveryEventKindV1.HOLDOUT_EVALUATED
        and before is DiscoveryPhaseV1.TERMINAL_REVEALED
    ):
        return DiscoveryPhaseV1.HOLDOUT_COMPLETE
    if (
        event is DiscoveryEventKindV1.ADVERSARIAL_EVALUATED
        and before is DiscoveryPhaseV1.HOLDOUT_COMPLETE
    ):
        return DiscoveryPhaseV1.ADVERSARIAL_COMPLETE
    if event is DiscoveryEventKindV1.WARNING_RECORDED and before not in {
        DiscoveryPhaseV1.NEW,
        DiscoveryPhaseV1.CLOSED,
    }:
        return before
    if event is DiscoveryEventKindV1.CLOSED and before not in {
        DiscoveryPhaseV1.NEW,
        DiscoveryPhaseV1.CLOSED,
    }:
        return DiscoveryPhaseV1.CLOSED
    raise ValueError(f"event {event.value} is invalid during {before.value}")


def _logical_key(record: DiscoveryRecordV1) -> tuple[str, ...] | None:
    if record.event_kind is DiscoveryEventKindV1.WARNING_RECORDED:
        return None
    if record.event_kind in {
        DiscoveryEventKindV1.MUTATION_RECORDED,
        DiscoveryEventKindV1.TRAINING_EVALUATED,
        DiscoveryEventKindV1.VALIDATION_EVALUATED,
        DiscoveryEventKindV1.ROBUSTNESS_EVALUATED,
        DiscoveryEventKindV1.ROBUSTNESS_REJECTED,
        DiscoveryEventKindV1.HOLDOUT_EVALUATED,
        DiscoveryEventKindV1.ADVERSARIAL_EVALUATED,
    }:
        assert record.candidate_semantic_sha256 is not None
        return (record.event_kind.value, record.candidate_semantic_sha256)
    if record.event_kind is DiscoveryEventKindV1.REJECTION_RECORDED:
        return (
            record.event_kind.value,
            str(thaw_json(record.payload)["request_sha256"]),
        )
    return (record.event_kind.value,)


def _record_content(record: DiscoveryRecordV1) -> dict[str, object]:
    return {
        "candidate_semantic_sha256": record.candidate_semantic_sha256,
        "event_kind": record.event_kind.value,
        "parent_semantic_sha256": record.parent_semantic_sha256,
        "payload": thaw_json(record.payload),
        "scientific_outcome": (
            None if record.scientific_outcome is None else record.scientific_outcome.value
        ),
    }


def _record_filename(record: DiscoveryRecordV1) -> str:
    return f"{record.ordinal:08d}-{record.record_sha256}.json"


def _domain_digest(domain: bytes, raw: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(struct.pack(">Q", len(raw)))
    digest.update(raw)
    return digest.hexdigest()


def _require_payload_keys(
    payload: dict[str, object],
    required: set[str],
    event: DiscoveryEventKindV1,
) -> None:
    missing = required - set(payload)
    if missing:
        raise ValueError(f"{event.value} payload misses required fields: {sorted(missing)}")


def _load_canonical_json(raw: bytes, label: str) -> object:
    if type(raw) is not bytes:
        raise TypeError(f"{label} must be exact bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be UTF-8 JSON") from error


def _exact_object(
    payload: object,
    expected: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(f"{label} fields differ from schema")
    return payload


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _nullable_text(row: dict[str, object], key: str) -> str | None:
    value = row[key]
    if value is not None and type(value) is not str:
        raise TypeError(f"{key} must be text or null")
    return value


def _integer(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _boolean(row: dict[str, object], key: str) -> bool:
    value = row[key]
    if type(value) is not bool:
        raise TypeError(f"{key} must be Boolean")
    return value


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _require_discovery_id(value: object) -> None:
    if type(value) is not str or _DISCOVERY_ID.fullmatch(value) is None:
        raise ValueError("strategy discovery ID is invalid")


def _require_nfc(value: object, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC")


def _write_fsync(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DISCOVERY_BINDING_SCHEMA_ID_V1",
    "DISCOVERY_CLAIM_SCOPE_V1",
    "DISCOVERY_RECORD_SCHEMA_ID_V1",
    "DISCOVERY_SCHEMA_VERSION_V1",
    "DISCOVERY_STORE_POLICY_ID_V1",
    "DiscoveryBindingV1",
    "DiscoveryEventKindV1",
    "DiscoveryLedgerV1",
    "DiscoveryPhaseV1",
    "DiscoveryRecordV1",
    "DiscoveryStore",
    "DiscoveryStoreError",
    "DiscoveryVerificationV1",
    "SEALED_FIELD_MARKER_V1",
]
