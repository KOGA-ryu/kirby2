"""Disclosure-safe lineage inspection and deterministic strategy comparison."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from typing import Any

from kirby2.immutable import freeze_json, thaw_json

from .identity import canonical_identity_bytes
from .store import (
    DISCOVERY_CLAIM_SCOPE_V1,
    SEALED_FIELD_MARKER_V1,
    DiscoveryEventKindV1,
    DiscoveryLedgerV1,
    DiscoveryPhaseV1,
    DiscoveryStore,
)


DISCOVERY_REPORT_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_DISCOVERY_LINEAGE_REPORT_V1"
DISCOVERY_COMPARISON_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_DISCOVERY_COMPARISON_V1"
DISCOVERY_REPORT_SCHEMA_VERSION_V1 = 1
_REPORT_DOMAIN = b"KIRBY2_STRATEGY_DISCOVERY_LINEAGE_REPORT_V1\x00"
_COMPARISON_DOMAIN = b"KIRBY2_STRATEGY_DISCOVERY_COMPARISON_V1\x00"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DiscoveryLineageReportV1:
    discovery_id: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.discovery_id) is not str or not self.discovery_id:
            raise ValueError("lineage report discovery ID must be nonempty")
        if not isinstance(self.payload, dict):
            raise TypeError("lineage report payload must be an object")
        detached = dict(self.payload)
        canonical_identity_bytes(detached)
        object.__setattr__(self, "payload", freeze_json(detached))

    @property
    def report_sha256(self) -> str:
        return self._digest_without_self()

    def as_dict(self) -> dict[str, object]:
        return {
            **thaw_json(self.payload),
            "report_sha256": self._digest_without_self(),
            "schema_id": DISCOVERY_REPORT_SCHEMA_ID_V1,
            "schema_version": DISCOVERY_REPORT_SCHEMA_VERSION_V1,
        }

    def _digest_without_self(self) -> str:
        raw = canonical_identity_bytes(
            {
                **thaw_json(self.payload),
                "schema_id": DISCOVERY_REPORT_SCHEMA_ID_V1,
                "schema_version": DISCOVERY_REPORT_SCHEMA_VERSION_V1,
            }
        )
        return _domain_digest(_REPORT_DOMAIN, raw)

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    def render_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def render_text(self) -> str:
        payload = self.as_dict()
        lines = [
            f"KIRBY2_STRATEGY_LINEAGE discovery_id={self.discovery_id}",
            f"PHASE {payload['phase']}",
            f"LEDGER {payload['ledger_sha256']}",
            f"CLAIM_SCOPE {payload['claim_scope']}",
        ]
        for candidate in payload["candidates"]:
            assert isinstance(candidate, dict)
            lines.append(f"CANDIDATE {candidate['semantic_sha256']}")
            lines.append(f"  ANCESTOR {candidate['ancestor_semantic_sha256']}")
            lines.append(
                "  MUTATION "
                + json.dumps(
                    candidate["mutation"],
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            for field in (
                "training_result",
                "validation_result",
                "holdout_result",
                "adversarial_result",
                "rejection_reason",
                "selected_descendants",
            ):
                lines.append(
                    f"  {field.upper()} "
                    + json.dumps(
                        candidate[field],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        lines.append(f"OUTCOME {payload['scientific_outcome']}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class StrategyComparisonReportV1:
    strategy_a: str
    strategy_b: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        for value, label in (
            (self.strategy_a, "strategy A"),
            (self.strategy_b, "strategy B"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{label} must be semantic SHA-256")
        if self.strategy_a == self.strategy_b:
            raise ValueError("strategy comparison requires two distinct identities")
        detached = dict(self.payload)
        canonical_identity_bytes(detached)
        object.__setattr__(self, "payload", freeze_json(detached))

    @property
    def comparison_sha256(self) -> str:
        return str(self.as_dict()["comparison_sha256"])

    def as_dict(self) -> dict[str, object]:
        payload = {
            **thaw_json(self.payload),
            "schema_id": DISCOVERY_COMPARISON_SCHEMA_ID_V1,
            "schema_version": DISCOVERY_REPORT_SCHEMA_VERSION_V1,
            "strategy_a": self.strategy_a,
            "strategy_b": self.strategy_b,
        }
        payload["comparison_sha256"] = _domain_digest(
            _COMPARISON_DOMAIN,
            canonical_identity_bytes(payload),
        )
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_identity_bytes(self.as_dict())

    def render_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )


def build_lineage_report(ledger: DiscoveryLedgerV1) -> DiscoveryLineageReportV1:
    if not isinstance(ledger, DiscoveryLedgerV1):
        raise TypeError("lineage report requires a typed discovery ledger")
    candidates: dict[str, dict[str, Any]] = {
        ledger.binding.base_semantic_sha256: _candidate_row(
            ledger.binding.base_semantic_sha256,
            ancestor=None,
            mutation={"kind": "BASE", "source_sha256": ledger.binding.base_source_sha256},
        )
    }
    children: dict[str, list[str]] = {}
    warnings: list[dict[str, object]] = []
    partition_accesses: list[dict[str, object]] = []
    reveal_payload: dict[str, object] | None = None
    for record in ledger.records:
        payload = thaw_json(record.payload)
        candidate = record.candidate_semantic_sha256
        if record.event_kind is DiscoveryEventKindV1.MUTATION_RECORDED:
            assert candidate is not None and record.parent_semantic_sha256 is not None
            candidates[candidate] = _candidate_row(
                candidate,
                ancestor=record.parent_semantic_sha256,
                mutation={
                    "mutation_sha256": payload["mutation_sha256"],
                    "operation_id": payload["operation_id"],
                    "operation_version": payload["operation_version"],
                    "semantic_diff": payload["semantic_diff"],
                },
            )
            children.setdefault(record.parent_semantic_sha256, []).append(candidate)
        elif record.event_kind is DiscoveryEventKindV1.REJECTION_RECORDED:
            if candidate is not None and candidate in candidates:
                candidates[candidate]["rejection_reason"] = payload["rejection_reason"]
        elif record.event_kind is DiscoveryEventKindV1.TRAINING_EVALUATED:
            assert candidate is not None
            candidates[candidate]["training_result"] = _result_projection(payload)
            partition_accesses.append(_access_projection(record, payload))
        elif record.event_kind is DiscoveryEventKindV1.VALIDATION_EVALUATED:
            assert candidate is not None
            candidates[candidate]["validation_result"] = _result_projection(payload)
            partition_accesses.append(_access_projection(record, payload))
        elif record.event_kind in {
            DiscoveryEventKindV1.ROBUSTNESS_EVALUATED,
            DiscoveryEventKindV1.ROBUSTNESS_REJECTED,
        }:
            assert candidate is not None
            candidates[candidate]["robustness_result"] = _result_projection(payload)
            partition_accesses.append(_access_projection(record, payload))
        elif record.event_kind is DiscoveryEventKindV1.TERMINAL_REVEALED:
            reveal_payload = payload
            partition_accesses.append(
                {
                    "access_record": payload["access_record"],
                    "event": record.event_kind.value,
                    "record_sha256": record.record_sha256,
                }
            )
        elif record.event_kind is DiscoveryEventKindV1.HOLDOUT_EVALUATED:
            assert candidate is not None
            candidates[candidate]["holdout_result"] = _result_projection(payload)
            partition_accesses.append(_access_projection(record, payload))
        elif record.event_kind is DiscoveryEventKindV1.ADVERSARIAL_EVALUATED:
            assert candidate is not None
            candidates[candidate]["adversarial_result"] = _result_projection(payload)
            partition_accesses.append(_access_projection(record, payload))
        elif record.event_kind is DiscoveryEventKindV1.WARNING_RECORDED:
            warnings.append(
                {
                    "record_sha256": record.record_sha256,
                    "warning_code": payload["warning_code"],
                    "warning_detail": payload["warning_detail"],
                }
            )
    selected = ledger.selected_candidate_semantic_sha256
    if selected is not None:
        for semantic in candidates:
            if semantic != selected and _is_ancestor(semantic, selected, candidates):
                candidates[semantic]["selected_descendants"] = [selected]
    if reveal_payload is None:
        terminal_references: object = {"status": SEALED_FIELD_MARKER_V1}
    else:
        terminal_references = {
            "adversarial": reveal_payload["adversarial"],
            "holdout": reveal_payload["holdout"],
            "status": "REVEALED",
        }
    candidate_rows = tuple(
        candidates[key]
        for key in sorted(candidates, key=lambda item: item.encode("utf-8"))
    )
    payload = {
        "base_semantic_sha256": ledger.binding.base_semantic_sha256,
        "base_source_sha256": ledger.binding.base_source_sha256,
        "candidates": list(candidate_rows),
        "claim_scope": DISCOVERY_CLAIM_SCOPE_V1,
        "deployability_claim": False,
        "development_only": ledger.binding.development_only,
        "discovery_id": ledger.discovery_id,
        "experiment_id": ledger.binding.experiment_id,
        "implementation_commit": ledger.binding.implementation_commit,
        "ledger_sha256": ledger.ledger_sha256,
        "live_profitability_claim": False,
        "partition_accesses": partition_accesses,
        "phase": ledger.current_phase.value,
        "real_partition_execution": ledger.binding.real_partition_execution,
        "record_count": len(ledger.records),
        "scientific_outcome": (
            None
            if ledger.scientific_outcome is None
            else ledger.scientific_outcome.value
        ),
        "selected_candidate_semantic_sha256": selected,
        "terminal_references": terminal_references,
        "warnings": warnings,
    }
    return DiscoveryLineageReportV1(ledger.discovery_id, payload)


def compare_strategies(
    store: DiscoveryStore,
    strategy_a: str,
    strategy_b: str,
) -> StrategyComparisonReportV1:
    if not isinstance(store, DiscoveryStore):
        raise TypeError("strategy comparison requires a discovery store")
    for value, label in ((strategy_a, "strategy A"), (strategy_b, "strategy B")):
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ValueError(f"{label} must be semantic SHA-256")
    locations: dict[str, list[tuple[str, dict[str, object]]]] = {
        strategy_a: [],
        strategy_b: [],
    }
    for discovery_id in store.list_discoveries():
        report = build_lineage_report(store.load(discovery_id)).as_dict()
        for row in report["candidates"]:
            assert isinstance(row, dict)
            semantic = str(row["semantic_sha256"])
            if semantic in locations:
                locations[semantic].append((discovery_id, row))
    for semantic, rows in locations.items():
        if not rows:
            raise ValueError(f"unknown strategy semantic identity: {semantic}")
        locations[semantic] = sorted(
            rows,
            key=lambda item: (
                -_row_completeness(item[1]),
                item[0].encode("utf-8"),
                canonical_identity_bytes(item[1]),
            ),
        )
    discovery_a, row_a = locations[strategy_a][0]
    discovery_b, row_b = locations[strategy_b][0]
    fields = tuple(
        sorted(
            set(row_a) | set(row_b),
            key=lambda item: item.encode("utf-8"),
        )
    )
    differences = [
        {"field": field, "strategy_a": row_a.get(field), "strategy_b": row_b.get(field)}
        for field in fields
        if row_a.get(field) != row_b.get(field)
    ]
    return StrategyComparisonReportV1(
        strategy_a,
        strategy_b,
        {
            "claim_scope": DISCOVERY_CLAIM_SCOPE_V1,
            "differences": differences,
            "discovery_a": discovery_a,
            "discovery_b": discovery_b,
            "discovery_observations_a": [
                discovery_id for discovery_id, _row in locations[strategy_a]
            ],
            "discovery_observations_b": [
                discovery_id for discovery_id, _row in locations[strategy_b]
            ],
            "live_profitability_claim": False,
        },
    )


def _candidate_row(
    semantic_sha256: str,
    *,
    ancestor: str | None,
    mutation: dict[str, object],
) -> dict[str, object]:
    return {
        "adversarial_result": {"status": SEALED_FIELD_MARKER_V1},
        "ancestor_semantic_sha256": ancestor,
        "holdout_result": {"status": SEALED_FIELD_MARKER_V1},
        "mutation": mutation,
        "rejection_reason": None,
        "robustness_result": None,
        "selected_descendants": [],
        "semantic_sha256": semantic_sha256,
        "training_result": None,
        "validation_result": None,
    }


def _result_projection(payload: dict[str, object]) -> dict[str, object]:
    return {
        "data_source": payload["data_source"],
        "evidence_sha256": payload["evidence_sha256"],
        "partition": payload["partition"],
        "result": payload["result"],
    }


def _access_projection(record, payload: dict[str, object]) -> dict[str, object]:
    return {
        "candidate_semantic_sha256": record.candidate_semantic_sha256,
        "event": record.event_kind.value,
        "partition": payload["partition"],
        "real_partition_access_count": payload.get("real_partition_access_count", 0),
        "record_sha256": record.record_sha256,
    }


def _is_ancestor(
    ancestor: str,
    descendant: str,
    candidates: dict[str, dict[str, Any]],
) -> bool:
    current: str | None = descendant
    visited: set[str] = set()
    while current is not None and current not in visited:
        if current == ancestor:
            return True
        visited.add(current)
        row = candidates.get(current)
        current = None if row is None else row["ancestor_semantic_sha256"]
    return False


def _row_completeness(row: dict[str, object]) -> int:
    score = 0
    for field in (
        "training_result",
        "validation_result",
        "holdout_result",
        "adversarial_result",
    ):
        value = row.get(field)
        if value is not None and value != {"status": SEALED_FIELD_MARKER_V1}:
            score += 1
    if row.get("rejection_reason") is not None:
        score += 1
    return score


def _domain_digest(domain: bytes, raw: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(struct.pack(">Q", len(raw)))
    digest.update(raw)
    return digest.hexdigest()


__all__ = [
    "DISCOVERY_COMPARISON_SCHEMA_ID_V1",
    "DISCOVERY_REPORT_SCHEMA_ID_V1",
    "DISCOVERY_REPORT_SCHEMA_VERSION_V1",
    "DiscoveryLineageReportV1",
    "StrategyComparisonReportV1",
    "build_lineage_report",
    "compare_strategies",
]
