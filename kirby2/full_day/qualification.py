"""Frozen WO31-I qualification arithmetic, dispositions, and evidence store.

This module separates observed integers from derived metrics, separates automated
status from human authority, and makes the protected qualification/holdout identity
one-shot.  Development fixtures use a disjoint namespace and never reveal or
estimate a WO31-H candidate outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping, Sequence

from kirby2.research.models import (
    RUN_MANIFEST_SCHEMA_VERSION,
    ArtifactReference,
    ArtifactType,
    RunManifest,
    RunType,
)
from kirby2.research.paths import DataPaths
from kirby2.research.runtime import software_version
from kirby2.research.toml_codec import file_sha256, load_toml
from kirby2.exchange import (
    AdvancedOrderRequest,
    MechanicsEvent,
    MechanicsEventType,
    OrderInstruction,
    OrderOwner,
    Side,
)
from kirby2.simulation.rng import SeededRng

from .models import (
    PressureKindV1,
    PressureProfileV1,
    PressureSegmentV1,
    canonical_json_bytes,
    canonical_sha256,
    derive_substream_seed,
    parse_canonical_json_object,
    validate_strict_json,
)
from .performance import (
    PerformanceEvaluationV1,
    PerformanceObservationV1,
    evaluate_performance,
    probe_performance_platform,
)
from .profiles import (
    CANDIDATE_IDS,
    DEVELOPMENT_ROOTS,
    DISORDERLY_OPEN_STABILIZATION_PRESSURE,
    EVENT_SHOCK_PRESSURE,
    FULL_DAY_PROFILE_POLICY_VERSION,
    HOLDOUT_ROOTS,
    INSUFFICIENT_EVIDENCE,
    POLICY_SCALE_PPM,
    QUALIFICATION_ROOTS,
    QUIET_RANGE_PRESSURE,
    TREND_PRESSURE,
    FullDayProfileBundleV1,
    PerformancePlatformFingerprintV1,
    ProfileCandidateV1,
    apply_multiplier_chain,
    derive_labeled_seed,
    load_full_day_profile_bundle,
    median,
    normalized_boundary_time_us,
    ratio_ppm,
    share_ppm,
    time_weighted_nearest_rank,
    unsigned_share_ppm,
)
from .review import (
    BlindedReviewPacketV1,
    ObservableSampleV1,
    ReviewRunV1,
    ReviewerSidecarV1,
    ReviewerSidecarStore,
    SelectedWindowManifestV1,
    build_blinded_packet,
    select_review_windows,
)


QUALIFICATION_METRICS_SCHEMA_VERSION: Final = 1
QUALIFICATION_EVIDENCE_SCHEMA_VERSION: Final = 1
QUALIFICATION_LEDGER_SCHEMA_VERSION: Final = 1
REVEAL_TOKEN_SCHEMA_VERSION: Final = 1
WO31_H_PREREGISTRATION_COMMIT: Final = (
    "1d1a1bc1c189c75d0d1aac4d223256b1aed67e9a"
)
WO31_I_COMMIT_SUBJECT: Final = "Implement full day qualification gates"
WO31_F_BASE_PLAN_SHA256: Final = (
    "24ebad3b86eebdd0db1ff8dea33fbf9f4d57ee92478354944de9c0e48fefb860"
)
REAL_PARTITIONS: Final = ("QUALIFICATION", "HOLDOUT")
PROFILE_KINDS: Final = CANDIDATE_IDS
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RUN_ID = re.compile(r"^run-[0-9a-f]{24}$")


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _git_oid(value: object, field: str) -> str:
    if type(value) is not str or _GIT_OID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase Git object ID")
    return value


@dataclass(frozen=True, slots=True)
class TimeWeightedValueV1:
    value: int
    duration_us: int
    ordinal: int

    def __post_init__(self) -> None:
        _exact_int(self.value, "time-weighted value")
        _exact_int(self.duration_us, "time-weighted duration", minimum=1)
        _exact_int(self.ordinal, "time-weighted ordinal")

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.value, self.duration_us, self.ordinal)


@dataclass(frozen=True, slots=True)
class QualificationRunProofV1:
    """Immutable link from one reduced metric row to its verified full-day run."""

    candidate_id: str
    partition: str
    root_seed: int
    run_digest: str
    plan_sha256: str
    workload_sha256: str
    full_day_run_id: str | None
    full_day_evidence_digest: str | None
    final_checkpoint_sha256: str | None
    event_prefix_sha256: str | None
    outer_event_count: int
    replay_verification_status: str
    abort_code: str | None = None

    def __post_init__(self) -> None:
        if self.candidate_id not in CANDIDATE_IDS:
            raise ValueError("run proof candidate is unknown")
        if self.partition not in REAL_PARTITIONS:
            raise ValueError("run proof partition is unknown")
        _exact_int(self.root_seed, "run proof root_seed")
        for field in ("run_digest", "plan_sha256", "workload_sha256"):
            _sha256(getattr(self, field), field)
        _exact_int(self.outer_event_count, "outer_event_count")
        if self.replay_verification_status not in {"PASS", "FAIL"}:
            raise ValueError("run proof replay status is invalid")
        digests = (
            self.full_day_evidence_digest,
            self.final_checkpoint_sha256,
            self.event_prefix_sha256,
        )
        if self.replay_verification_status == "PASS":
            if self.full_day_run_id is None or _RUN_ID.fullmatch(self.full_day_run_id) is None:
                raise ValueError("passing run proof requires a full-day run ID")
            for value, field in zip(
                digests,
                (
                    "full_day_evidence_digest",
                    "final_checkpoint_sha256",
                    "event_prefix_sha256",
                ),
                strict=True,
            ):
                _sha256(value, field)
            if self.abort_code is not None:
                raise ValueError("passing run proof cannot carry an abort code")
        else:
            if any(value is not None for value in (self.full_day_run_id, *digests)):
                raise ValueError("failed run proof cannot claim completed artifact identity")
            if type(self.abort_code) is not str or not self.abort_code:
                raise ValueError("failed run proof requires an abort code")

    def as_dict(self) -> dict[str, object]:
        return {
            "abort_code": self.abort_code,
            "candidate_id": self.candidate_id,
            "event_prefix_sha256": self.event_prefix_sha256,
            "final_checkpoint_sha256": self.final_checkpoint_sha256,
            "full_day_evidence_digest": self.full_day_evidence_digest,
            "full_day_run_id": self.full_day_run_id,
            "outer_event_count": self.outer_event_count,
            "partition": self.partition,
            "plan_sha256": self.plan_sha256,
            "replay_verification_status": self.replay_verification_status,
            "root_seed": self.root_seed,
            "run_digest": self.run_digest,
            "workload_sha256": self.workload_sha256,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> QualificationRunProofV1:
        expected = {
            "abort_code",
            "candidate_id",
            "event_prefix_sha256",
            "final_checkpoint_sha256",
            "full_day_evidence_digest",
            "full_day_run_id",
            "outer_event_count",
            "partition",
            "plan_sha256",
            "replay_verification_status",
            "root_seed",
            "run_digest",
            "workload_sha256",
        }
        if set(payload) != expected:
            raise ValueError("qualification run-proof fields differ")
        for field in (
            "abort_code",
            "event_prefix_sha256",
            "final_checkpoint_sha256",
            "full_day_evidence_digest",
            "full_day_run_id",
        ):
            if payload[field] is not None and type(payload[field]) is not str:
                raise TypeError(f"run proof {field} must be text or absent")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            partition=str(payload["partition"]),
            root_seed=_exact_int(payload["root_seed"], "root_seed"),
            run_digest=str(payload["run_digest"]),
            plan_sha256=str(payload["plan_sha256"]),
            workload_sha256=str(payload["workload_sha256"]),
            full_day_run_id=payload["full_day_run_id"],  # type: ignore[arg-type]
            full_day_evidence_digest=payload["full_day_evidence_digest"],  # type: ignore[arg-type]
            final_checkpoint_sha256=payload["final_checkpoint_sha256"],  # type: ignore[arg-type]
            event_prefix_sha256=payload["event_prefix_sha256"],  # type: ignore[arg-type]
            outer_event_count=_exact_int(payload["outer_event_count"], "outer_event_count"),
            replay_verification_status=str(payload["replay_verification_status"]),
            abort_code=payload["abort_code"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class QualificationRunMetricsV1:
    """Observed raw integers for one completed/aborted qualification root."""

    candidate_id: str
    profile_kind: str
    partition: str
    root_seed: int
    run_digest: str
    runtime_invariants_passed: bool
    exact_replay_passed: bool
    safety_abort_count: int
    trade_count: int
    continuous_quote_occupied_us: int
    continuous_quote_eligible_us: int
    maximum_nonhalt_empty_side_episode_us: int
    maximum_continuous_spread_ticks: int
    target_price_operations: int
    forced_trade_operations: int
    spread_segments: tuple[TimeWeightedValueV1, ...] = ()
    aggressive_buy_shares: int = 0
    aggressive_sell_shares: int = 0
    first_trade_ticks: int | None = None
    last_trade_ticks: int | None = None
    maximum_absolute_trade_displacement_ticks: int | None = None
    favored_side: str | None = None
    pre_aggressive_shares: int | None = None
    shock_aggressive_shares: int | None = None
    pre_quote_range_ticks: int | None = None
    shock_quote_range_ticks: int | None = None
    pre_trade_range_ticks: int | None = None
    shock_trade_range_ticks: int | None = None
    event_ratio_halt_affected: bool = False
    shock_spread_segments: tuple[TimeWeightedValueV1, ...] = ()
    recovery_spread_segments: tuple[TimeWeightedValueV1, ...] = ()
    recovery_quote_occupied_us: int | None = None
    recovery_quote_eligible_us: int | None = None
    open_spread_segments: tuple[TimeWeightedValueV1, ...] = ()
    midday_spread_segments: tuple[TimeWeightedValueV1, ...] = ()
    final_spread_segments: tuple[TimeWeightedValueV1, ...] = ()
    open_cancel_count: int | None = None
    open_cancel_eligible_us: int | None = None
    midday_cancel_count: int | None = None
    midday_cancel_eligible_us: int | None = None
    final_quote_occupied_us: int | None = None
    final_quote_eligible_us: int | None = None
    schema_version: int = QUALIFICATION_METRICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QUALIFICATION_METRICS_SCHEMA_VERSION:
            raise ValueError("qualification run-metric schema version must be 1")
        if self.profile_kind not in PROFILE_KINDS:
            raise ValueError("qualification profile kind is unknown")
        if self.candidate_id not in CANDIDATE_IDS and not self.candidate_id.startswith(
            "DEV_ONLY_"
        ):
            raise ValueError("qualification candidate ID is unknown")
        if self.partition not in {"DEVELOPMENT", *REAL_PARTITIONS}:
            raise ValueError("qualification partition is unknown")
        if self.partition in REAL_PARTITIONS and self.candidate_id != self.profile_kind:
            raise ValueError("real candidate ID must equal its preregistered profile kind")
        _exact_int(self.root_seed, "root_seed")
        if self.partition == "DEVELOPMENT" and self.root_seed in {
            *DEVELOPMENT_ROOTS,
            *QUALIFICATION_ROOTS,
            *HOLDOUT_ROOTS,
        }:
            raise ValueError("development evidence cannot access a WO31-H seed")
        _sha256(self.run_digest, "run_digest")
        if type(self.runtime_invariants_passed) is not bool:
            raise TypeError("runtime invariant status must be a bool")
        if type(self.exact_replay_passed) is not bool:
            raise TypeError("exact replay status must be a bool")
        if type(self.event_ratio_halt_affected) is not bool:
            raise TypeError("event halt status must be a bool")
        for field in (
            "safety_abort_count",
            "trade_count",
            "continuous_quote_occupied_us",
            "continuous_quote_eligible_us",
            "maximum_nonhalt_empty_side_episode_us",
            "maximum_continuous_spread_ticks",
            "target_price_operations",
            "forced_trade_operations",
            "aggressive_buy_shares",
            "aggressive_sell_shares",
        ):
            _exact_int(getattr(self, field), field)
        if self.continuous_quote_occupied_us > self.continuous_quote_eligible_us:
            raise ValueError("continuous quote occupancy exceeds eligible duration")
        optional_integers = (
            "first_trade_ticks",
            "last_trade_ticks",
            "maximum_absolute_trade_displacement_ticks",
            "pre_aggressive_shares",
            "shock_aggressive_shares",
            "pre_quote_range_ticks",
            "shock_quote_range_ticks",
            "pre_trade_range_ticks",
            "shock_trade_range_ticks",
            "recovery_quote_occupied_us",
            "recovery_quote_eligible_us",
            "open_cancel_count",
            "open_cancel_eligible_us",
            "midday_cancel_count",
            "midday_cancel_eligible_us",
            "final_quote_occupied_us",
            "final_quote_eligible_us",
        )
        for field in optional_integers:
            value = getattr(self, field)
            if value is not None:
                _exact_int(value, field)
        if self.favored_side not in {None, "BUY", "SELL"}:
            raise ValueError("favored side must be BUY, SELL, or absent")
        for field in (
            "spread_segments",
            "shock_spread_segments",
            "recovery_spread_segments",
            "open_spread_segments",
            "midday_spread_segments",
            "final_spread_segments",
        ):
            rows = getattr(self, field)
            if type(rows) is not tuple or any(
                type(item) is not TimeWeightedValueV1 for item in rows
            ):
                raise TypeError(f"{field} must contain typed immutable segments")

    def as_dict(self) -> dict[str, object]:
        return {
            "aggressive_buy_shares": self.aggressive_buy_shares,
            "aggressive_sell_shares": self.aggressive_sell_shares,
            "candidate_id": self.candidate_id,
            "continuous_quote_eligible_us": self.continuous_quote_eligible_us,
            "continuous_quote_occupied_us": self.continuous_quote_occupied_us,
            "event_ratio_halt_affected": self.event_ratio_halt_affected,
            "exact_replay_passed": self.exact_replay_passed,
            "favored_side": self.favored_side,
            "final_quote_eligible_us": self.final_quote_eligible_us,
            "final_quote_occupied_us": self.final_quote_occupied_us,
            "final_spread_segments": [row.as_tuple() for row in self.final_spread_segments],
            "first_trade_ticks": self.first_trade_ticks,
            "forced_trade_operations": self.forced_trade_operations,
            "last_trade_ticks": self.last_trade_ticks,
            "maximum_absolute_trade_displacement_ticks": self.maximum_absolute_trade_displacement_ticks,
            "maximum_continuous_spread_ticks": self.maximum_continuous_spread_ticks,
            "maximum_nonhalt_empty_side_episode_us": self.maximum_nonhalt_empty_side_episode_us,
            "midday_cancel_count": self.midday_cancel_count,
            "midday_cancel_eligible_us": self.midday_cancel_eligible_us,
            "midday_spread_segments": [row.as_tuple() for row in self.midday_spread_segments],
            "open_cancel_count": self.open_cancel_count,
            "open_cancel_eligible_us": self.open_cancel_eligible_us,
            "open_spread_segments": [row.as_tuple() for row in self.open_spread_segments],
            "partition": self.partition,
            "pre_aggressive_shares": self.pre_aggressive_shares,
            "pre_quote_range_ticks": self.pre_quote_range_ticks,
            "pre_trade_range_ticks": self.pre_trade_range_ticks,
            "profile_kind": self.profile_kind,
            "recovery_quote_eligible_us": self.recovery_quote_eligible_us,
            "recovery_quote_occupied_us": self.recovery_quote_occupied_us,
            "recovery_spread_segments": [row.as_tuple() for row in self.recovery_spread_segments],
            "root_seed": self.root_seed,
            "run_digest": self.run_digest,
            "runtime_invariants_passed": self.runtime_invariants_passed,
            "safety_abort_count": self.safety_abort_count,
            "schema_version": self.schema_version,
            "shock_aggressive_shares": self.shock_aggressive_shares,
            "shock_quote_range_ticks": self.shock_quote_range_ticks,
            "shock_spread_segments": [row.as_tuple() for row in self.shock_spread_segments],
            "shock_trade_range_ticks": self.shock_trade_range_ticks,
            "spread_segments": [row.as_tuple() for row in self.spread_segments],
            "target_price_operations": self.target_price_operations,
            "trade_count": self.trade_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> QualificationRunMetricsV1:
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if set(payload) != expected:
            raise ValueError("qualification run-metric fields differ")
        segment_fields = {
            "spread_segments",
            "shock_spread_segments",
            "recovery_spread_segments",
            "open_spread_segments",
            "midday_spread_segments",
            "final_spread_segments",
        }
        arguments = dict(payload)
        for field in segment_fields:
            raw = payload[field]
            if type(raw) is not list or any(
                type(row) is not list
                or len(row) != 3
                or any(type(value) is not int for value in row)
                for row in raw
            ):
                raise TypeError(f"{field} must contain integer triples")
            arguments[field] = tuple(TimeWeightedValueV1(*row) for row in raw)
        return cls(**arguments)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class CandidateQualificationV1:
    candidate_id: str
    profile_kind: str
    partition: str
    root_count: int
    engineering_status: str
    behavioral_envelope_status: str
    statistical_status: str
    platform_performance_status: str
    automated_disposition: str
    human_review_status: str
    universal_failures: tuple[str, ...]
    behavioral_failures: tuple[str, ...]
    metrics: tuple[tuple[str, int | str], ...]
    schema_version: int = QUALIFICATION_METRICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QUALIFICATION_METRICS_SCHEMA_VERSION:
            raise ValueError("qualification metric schema version must be 1")
        if self.engineering_status not in {"PASS", "FAIL"}:
            raise ValueError("engineering status is invalid")
        if self.behavioral_envelope_status not in {"PASS", "WARNING"}:
            raise ValueError("behavioral envelope status is invalid")
        if self.statistical_status not in {"PASS", "FAIL", "NOT_APPLICABLE"}:
            raise ValueError("statistical status is invalid")
        if self.platform_performance_status not in {
            "PASS",
            "WARNING",
            "FAIL",
            "UNSUPPORTED",
            "NOT_RUN",
        }:
            raise ValueError("platform performance status is invalid")
        if self.automated_disposition not in {"READY", "NOT_READY"}:
            raise ValueError("automated disposition is invalid")
        if self.human_review_status != "PENDING":
            raise ValueError("only a reviewer sidecar may set human review")
        expected_ready = (
            self.engineering_status == "PASS"
            and self.behavioral_envelope_status == "PASS"
            and self.statistical_status in {"PASS", "NOT_APPLICABLE"}
            and self.platform_performance_status == "PASS"
        )
        if (self.automated_disposition == "READY") != expected_ready:
            raise ValueError("automated disposition differs from component statuses")
        if self.metrics != tuple(sorted(self.metrics)):
            raise ValueError("qualification metrics must use canonical name order")

    def as_dict(self) -> dict[str, object]:
        return {
            "automated_disposition": self.automated_disposition,
            "behavioral_envelope_status": self.behavioral_envelope_status,
            "behavioral_failures": list(self.behavioral_failures),
            "candidate_id": self.candidate_id,
            "engineering_status": self.engineering_status,
            "human_review_status": self.human_review_status,
            "metrics": {name: value for name, value in self.metrics},
            "partition": self.partition,
            "platform_performance_status": self.platform_performance_status,
            "profile_kind": self.profile_kind,
            "root_count": self.root_count,
            "schema_version": self.schema_version,
            "statistical_status": self.statistical_status,
            "universal_failures": list(self.universal_failures),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CandidateQualificationV1:
        expected = {
            "automated_disposition",
            "behavioral_envelope_status",
            "behavioral_failures",
            "candidate_id",
            "engineering_status",
            "human_review_status",
            "metrics",
            "partition",
            "platform_performance_status",
            "profile_kind",
            "root_count",
            "schema_version",
            "statistical_status",
            "universal_failures",
        }
        if set(payload) != expected:
            raise ValueError("candidate qualification fields differ")
        metrics = payload["metrics"]
        universal = payload["universal_failures"]
        behavioral = payload["behavioral_failures"]
        if not isinstance(metrics, Mapping) or any(
            type(name) is not str or type(value) not in {int, str}
            for name, value in metrics.items()
        ):
            raise TypeError("candidate qualification metrics are invalid")
        if any(
            type(rows) is not list or any(type(row) is not str for row in rows)
            for rows in (universal, behavioral)
        ):
            raise TypeError("candidate qualification failures must be string arrays")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            profile_kind=str(payload["profile_kind"]),
            partition=str(payload["partition"]),
            root_count=_exact_int(payload["root_count"], "root_count", minimum=1),
            engineering_status=str(payload["engineering_status"]),
            behavioral_envelope_status=str(payload["behavioral_envelope_status"]),
            statistical_status=str(payload["statistical_status"]),
            platform_performance_status=str(payload["platform_performance_status"]),
            automated_disposition=str(payload["automated_disposition"]),
            human_review_status=str(payload["human_review_status"]),
            universal_failures=tuple(universal),
            behavioral_failures=tuple(behavioral),
            metrics=tuple(sorted((str(name), value) for name, value in metrics.items())),
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
        )


def _weighted_rows(
    rows: Sequence[QualificationRunMetricsV1], field: str
) -> tuple[tuple[int, int, str], ...]:
    return tuple(
        (
            segment.value,
            segment.duration_us,
            f"{row.root_seed:020d}:{segment.ordinal:020d}",
        )
        for row in sorted(rows, key=lambda item: (item.root_seed, item.run_digest))
        for segment in getattr(row, field)
    )


def _required(value: int | None, field: str) -> int:
    if value is None:
        raise ValueError(f"{field}:{INSUFFICIENT_EVIDENCE}")
    return value


def _universal_failures(rows: Sequence[QualificationRunMetricsV1]) -> tuple[str, ...]:
    failures: list[str] = []
    for row in sorted(rows, key=lambda item: (item.root_seed, item.run_digest)):
        prefix = f"root={row.root_seed}"
        if not row.runtime_invariants_passed:
            failures.append(f"{prefix}:RUNTIME_INVARIANTS")
        if not row.exact_replay_passed:
            failures.append(f"{prefix}:EXACT_REPLAY")
        if row.safety_abort_count != 0:
            failures.append(f"{prefix}:SAFETY_ABORT_COUNT")
        if row.trade_count < 100:
            failures.append(f"{prefix}:MINIMUM_TRADE_COUNT")
        if row.continuous_quote_eligible_us == 0:
            failures.append(f"{prefix}:CONTINUOUS_OCCUPANCY_{INSUFFICIENT_EVIDENCE}")
        elif unsigned_share_ppm(
            row.continuous_quote_occupied_us,
            row.continuous_quote_eligible_us,
        ) < 950_000:
            failures.append(f"{prefix}:CONTINUOUS_TWO_SIDED_OCCUPANCY")
        if row.maximum_nonhalt_empty_side_episode_us > 5_000_000:
            failures.append(f"{prefix}:MAXIMUM_NONHALT_EMPTY_SIDE_EPISODE")
        if row.maximum_continuous_spread_ticks > 20:
            failures.append(f"{prefix}:MAXIMUM_CONTINUOUS_SPREAD")
        if row.target_price_operations != 0:
            failures.append(f"{prefix}:TARGET_PRICE_OPERATION")
        if row.forced_trade_operations != 0:
            failures.append(f"{prefix}:FORCED_TRADE_OPERATION")
    return tuple(failures)


def _quiet_metrics(
    rows: Sequence[QualificationRunMetricsV1],
) -> tuple[dict[str, int | str], tuple[str, ...]]:
    metrics: dict[str, int | str] = {}
    failures: list[str] = []
    try:
        metrics["spread_p50_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "spread_segments"), 500_000
        )
        metrics["spread_p95_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "spread_segments"), 950_000
        )
    except ValueError:
        metrics["spread_distribution"] = INSUFFICIENT_EVIDENCE
        failures.append("SPREAD_DISTRIBUTION_INSUFFICIENT")
    buy = sum(row.aggressive_buy_shares for row in rows)
    sell = sum(row.aggressive_sell_shares for row in rows)
    if buy + sell == 0:
        metrics["absolute_aggressive_volume_imbalance_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("AGGRESSIVE_VOLUME_IMBALANCE_INSUFFICIENT")
    else:
        metrics["absolute_aggressive_volume_imbalance_ppm"] = abs(
            share_ppm(buy - sell, buy + sell)
        )
    displacements = tuple(
        row.maximum_absolute_trade_displacement_ticks
        for row in rows
        if row.maximum_absolute_trade_displacement_ticks is not None
    )
    if len(displacements) != len(rows):
        metrics["maximum_absolute_trade_displacement_ticks"] = INSUFFICIENT_EVIDENCE
        failures.append("MAXIMUM_DISPLACEMENT_INSUFFICIENT")
    else:
        metrics["maximum_absolute_trade_displacement_ticks"] = max(displacements)
    thresholds = (
        ("spread_p50_ticks", 4, "SPREAD_P50"),
        ("spread_p95_ticks", 8, "SPREAD_P95"),
        (
            "absolute_aggressive_volume_imbalance_ppm",
            250_000,
            "AGGRESSIVE_VOLUME_IMBALANCE",
        ),
        (
            "maximum_absolute_trade_displacement_ticks",
            80,
            "MAXIMUM_ABSOLUTE_TRADE_DISPLACEMENT",
        ),
    )
    for metric, maximum, code in thresholds:
        value = metrics.get(metric)
        if type(value) is int and value > maximum:
            failures.append(code)
    return metrics, tuple(failures)


def _trend_metrics(
    rows: Sequence[QualificationRunMetricsV1], partition: str
) -> tuple[dict[str, int | str], tuple[str, ...]]:
    metrics: dict[str, int | str] = {}
    failures: list[str] = []
    favored = 0
    total = 0
    displacements: list[int] = []
    for row in rows:
        total += row.aggressive_buy_shares + row.aggressive_sell_shares
        if row.favored_side == "BUY":
            favored += row.aggressive_buy_shares
            if row.first_trade_ticks is not None and row.last_trade_ticks is not None:
                displacements.append(row.last_trade_ticks - row.first_trade_ticks)
        elif row.favored_side == "SELL":
            favored += row.aggressive_sell_shares
            if row.first_trade_ticks is not None and row.last_trade_ticks is not None:
                displacements.append(row.first_trade_ticks - row.last_trade_ticks)
        else:
            failures.append(f"root={row.root_seed}:FAVORED_SIDE_INSUFFICIENT")
    if total == 0:
        metrics["favored_aggressive_volume_share_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("FAVORED_AGGRESSIVE_VOLUME_SHARE_INSUFFICIENT")
    else:
        metrics["favored_aggressive_volume_share_ppm"] = unsigned_share_ppm(
            favored, total
        )
    if len(displacements) != len(rows):
        metrics["median_favored_signed_displacement_ticks"] = INSUFFICIENT_EVIDENCE
        metrics["positive_root_count"] = sum(value > 0 for value in displacements)
        failures.append("FAVORED_SIGNED_DISPLACEMENT_INSUFFICIENT")
    else:
        metrics["median_favored_signed_displacement_ticks"] = median(displacements)
        metrics["positive_root_count"] = sum(value > 0 for value in displacements)
    value = metrics.get("favored_aggressive_volume_share_ppm")
    if type(value) is int and value < 600_000:
        failures.append("FAVORED_AGGRESSIVE_VOLUME_SHARE")
    value = metrics.get("median_favored_signed_displacement_ticks")
    if type(value) is int and value < 2:
        failures.append("MEDIAN_FAVORED_SIGNED_DISPLACEMENT")
    positive_minimum = (
        6
        if partition == "QUALIFICATION"
        else 3
        if partition == "HOLDOUT"
        else (3 * len(rows) + 3) // 4
    )
    metrics["positive_root_count_minimum"] = positive_minimum
    if metrics["positive_root_count"] < positive_minimum:  # type: ignore[operator]
        failures.append("POSITIVE_ROOT_COUNT")
    return metrics, tuple(failures)


def _event_range_ratio(row: QualificationRunMetricsV1) -> int:
    if row.event_ratio_halt_affected:
        raise ValueError(INSUFFICIENT_EVIDENCE)
    if row.pre_quote_range_ticks is not None and row.shock_quote_range_ticks is not None:
        pre, shock = row.pre_quote_range_ticks, row.shock_quote_range_ticks
    else:
        pre = _required(row.pre_trade_range_ticks, "pre_trade_range_ticks")
        shock = _required(row.shock_trade_range_ticks, "shock_trade_range_ticks")
    return ratio_ppm(shock, pre)


def _event_metrics(
    rows: Sequence[QualificationRunMetricsV1],
) -> tuple[dict[str, int | str], tuple[str, ...]]:
    metrics: dict[str, int | str] = {}
    failures: list[str] = []
    try:
        pre = sum(_required(row.pre_aggressive_shares, "pre_aggressive_shares") for row in rows)
        shock = sum(
            _required(row.shock_aggressive_shares, "shock_aggressive_shares")
            for row in rows
        )
        metrics["shock_over_pre_aggressive_volume_ppm"] = ratio_ppm(shock, pre)
    except ValueError:
        metrics["shock_over_pre_aggressive_volume_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("SHOCK_OVER_PRE_VOLUME_INSUFFICIENT")
    try:
        metrics["shock_over_pre_range_p50_ppm"] = median(
            [_event_range_ratio(row) for row in rows]
        )
    except ValueError:
        metrics["shock_over_pre_range_p50_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("SHOCK_OVER_PRE_RANGE_INSUFFICIENT")
    try:
        recovery_occupied = sum(
            _required(row.recovery_quote_occupied_us, "recovery_quote_occupied_us")
            for row in rows
        )
        recovery_eligible = sum(
            _required(row.recovery_quote_eligible_us, "recovery_quote_eligible_us")
            for row in rows
        )
        metrics["recovery_two_sided_occupancy_ppm"] = unsigned_share_ppm(
            recovery_occupied, recovery_eligible
        )
    except ValueError:
        metrics["recovery_two_sided_occupancy_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("RECOVERY_OCCUPANCY_INSUFFICIENT")
    try:
        metrics["shock_spread_p50_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "shock_spread_segments"), 500_000
        )
        metrics["recovery_spread_p50_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "recovery_spread_segments"), 500_000
        )
    except ValueError:
        metrics["shock_spread_p50_ticks"] = INSUFFICIENT_EVIDENCE
        metrics["recovery_spread_p50_ticks"] = INSUFFICIENT_EVIDENCE
        failures.append("SHOCK_RECOVERY_SPREAD_INSUFFICIENT")
    minima = (
        ("shock_over_pre_aggressive_volume_ppm", 1_500_000, "SHOCK_OVER_PRE_VOLUME"),
        ("shock_over_pre_range_p50_ppm", 1_200_000, "SHOCK_OVER_PRE_RANGE"),
        ("recovery_two_sided_occupancy_ppm", 900_000, "RECOVERY_OCCUPANCY"),
    )
    for metric, minimum, code in minima:
        value = metrics.get(metric)
        if type(value) is int and value < minimum:
            failures.append(code)
    shock_spread = metrics.get("shock_spread_p50_ticks")
    recovery_spread = metrics.get("recovery_spread_p50_ticks")
    if (
        type(shock_spread) is int
        and type(recovery_spread) is int
        and recovery_spread > shock_spread
    ):
        failures.append("RECOVERY_SPREAD_VS_SHOCK")
    return metrics, tuple(failures)


def _disorderly_metrics(
    rows: Sequence[QualificationRunMetricsV1],
) -> tuple[dict[str, int | str], tuple[str, ...]]:
    metrics: dict[str, int | str] = {}
    failures: list[str] = []
    try:
        metrics["first_eight_percent_spread_p50_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "open_spread_segments"), 500_000
        )
        metrics["midday_spread_p50_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "midday_spread_segments"), 500_000
        )
        metrics["final_eighty_percent_spread_p50_ticks"] = time_weighted_nearest_rank(
            _weighted_rows(rows, "final_spread_segments"), 500_000
        )
    except ValueError:
        failures.append("DISORDERLY_SPREAD_DISTRIBUTION_INSUFFICIENT")
        metrics.setdefault("first_eight_percent_spread_p50_ticks", INSUFFICIENT_EVIDENCE)
        metrics.setdefault("midday_spread_p50_ticks", INSUFFICIENT_EVIDENCE)
        metrics.setdefault("final_eighty_percent_spread_p50_ticks", INSUFFICIENT_EVIDENCE)
    try:
        open_count = sum(_required(row.open_cancel_count, "open_cancel_count") for row in rows)
        open_duration = sum(
            _required(row.open_cancel_eligible_us, "open_cancel_eligible_us")
            for row in rows
        )
        midday_count = sum(
            _required(row.midday_cancel_count, "midday_cancel_count") for row in rows
        )
        midday_duration = sum(
            _required(row.midday_cancel_eligible_us, "midday_cancel_eligible_us")
            for row in rows
        )
        metrics["first_eight_over_midday_cancel_rate_ppm"] = ratio_ppm(
            open_count * midday_duration, midday_count * open_duration
        )
    except ValueError:
        metrics["first_eight_over_midday_cancel_rate_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("DISORDERLY_CANCEL_RATE_RATIO_INSUFFICIENT")
    try:
        occupied = sum(
            _required(row.final_quote_occupied_us, "final_quote_occupied_us")
            for row in rows
        )
        eligible = sum(
            _required(row.final_quote_eligible_us, "final_quote_eligible_us")
            for row in rows
        )
        metrics["final_eighty_percent_two_sided_occupancy_ppm"] = unsigned_share_ppm(
            occupied, eligible
        )
    except ValueError:
        metrics["final_eighty_percent_two_sided_occupancy_ppm"] = INSUFFICIENT_EVIDENCE
        failures.append("FINAL_OCCUPANCY_INSUFFICIENT")
    opened = metrics.get("first_eight_percent_spread_p50_ticks")
    midday = metrics.get("midday_spread_p50_ticks")
    if type(opened) is int and type(midday) is int and opened < midday:
        failures.append("OPEN_SPREAD_VS_MIDDAY")
    ratio = metrics.get("first_eight_over_midday_cancel_rate_ppm")
    if type(ratio) is int and ratio < 1_500_000:
        failures.append("OPEN_CANCEL_RATE_VS_MIDDAY")
    occupancy = metrics.get("final_eighty_percent_two_sided_occupancy_ppm")
    if type(occupancy) is int and occupancy < 950_000:
        failures.append("FINAL_TWO_SIDED_OCCUPANCY")
    final_spread = metrics.get("final_eighty_percent_spread_p50_ticks")
    if type(final_spread) is int and final_spread > 8:
        failures.append("FINAL_MEDIAN_SPREAD")
    return metrics, tuple(failures)


def evaluate_candidate_qualification(
    rows: Sequence[QualificationRunMetricsV1],
    *,
    platform_performance_status: str,
    statistical_status: str = "NOT_APPLICABLE",
) -> CandidateQualificationV1:
    """Evaluate one candidate without dropping aborted or failed roots."""

    observations = tuple(rows)
    if not observations:
        raise ValueError("candidate qualification requires at least one root")
    identities = {
        (row.candidate_id, row.profile_kind, row.partition) for row in observations
    }
    if len(identities) != 1:
        raise ValueError("candidate qualification rows cross identity boundaries")
    candidate_id, profile_kind, partition = next(iter(identities))
    roots = tuple(row.root_seed for row in observations)
    if len(roots) != len(set(roots)):
        raise ValueError("candidate qualification contains a duplicate root")
    expected_roots = (
        QUALIFICATION_ROOTS
        if partition == "QUALIFICATION"
        else HOLDOUT_ROOTS
        if partition == "HOLDOUT"
        else None
    )
    if expected_roots is not None and tuple(sorted(roots)) != expected_roots:
        raise ValueError("real qualification root inventory differs from preregistration")
    universal = _universal_failures(observations)
    reducers = {
        QUIET_RANGE_PRESSURE: lambda: _quiet_metrics(observations),
        TREND_PRESSURE: lambda: _trend_metrics(observations, partition),
        EVENT_SHOCK_PRESSURE: lambda: _event_metrics(observations),
        DISORDERLY_OPEN_STABILIZATION_PRESSURE: lambda: _disorderly_metrics(
            observations
        ),
    }
    metrics, behavioral = reducers[profile_kind]()
    engineering = "FAIL" if universal else "PASS"
    behavioral_status = "WARNING" if behavioral else "PASS"
    ready = (
        engineering == "PASS"
        and behavioral_status == "PASS"
        and statistical_status in {"PASS", "NOT_APPLICABLE"}
        and platform_performance_status == "PASS"
    )
    return CandidateQualificationV1(
        candidate_id=candidate_id,
        profile_kind=profile_kind,
        partition=partition,
        root_count=len(observations),
        engineering_status=engineering,
        behavioral_envelope_status=behavioral_status,
        statistical_status=statistical_status,
        platform_performance_status=platform_performance_status,
        automated_disposition="READY" if ready else "NOT_READY",
        human_review_status="PENDING",
        universal_failures=universal,
        behavioral_failures=behavioral,
        metrics=tuple(sorted(metrics.items())),
    )


@dataclass(frozen=True, slots=True)
class OneTimeRevealTokenV1:
    token_id: str
    qualification_identity: str
    implementation_commit: str
    consumed: bool
    schema_version: int = REVEAL_TOKEN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REVEAL_TOKEN_SCHEMA_VERSION:
            raise ValueError("reveal-token schema version must be 1")
        if not self.token_id.startswith("reveal-") or len(self.token_id) != 31:
            raise ValueError("reveal token ID is invalid")
        _sha256(self.qualification_identity, "qualification_identity")
        _git_oid(self.implementation_commit, "implementation_commit")
        if type(self.consumed) is not bool:
            raise TypeError("reveal token consumption status must be a bool")

    @classmethod
    def issue(
        cls, qualification_identity: str, implementation_commit: str
    ) -> OneTimeRevealTokenV1:
        identity = _sha256(qualification_identity, "qualification_identity")
        commit = _git_oid(implementation_commit, "implementation_commit")
        token = canonical_sha256(
            {
                "implementation_commit": commit,
                "qualification_identity": identity,
                "token_domain": "WO31_I_ONE_TIME_REVEAL_V1",
            }
        )
        return cls("reveal-" + token[:24], identity, commit, False)

    def consume(self) -> OneTimeRevealTokenV1:
        if self.consumed:
            raise RuntimeError("one-time reveal token has already been consumed")
        return OneTimeRevealTokenV1(
            self.token_id,
            self.qualification_identity,
            self.implementation_commit,
            True,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "consumed": self.consumed,
            "implementation_commit": self.implementation_commit,
            "qualification_identity": self.qualification_identity,
            "schema_version": self.schema_version,
            "token_id": self.token_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> OneTimeRevealTokenV1:
        expected = {
            "consumed",
            "implementation_commit",
            "qualification_identity",
            "schema_version",
            "token_id",
        }
        if set(payload) != expected:
            raise ValueError("reveal-token fields differ")
        return cls(
            token_id=str(payload["token_id"]),
            qualification_identity=str(payload["qualification_identity"]),
            implementation_commit=str(payload["implementation_commit"]),
            consumed=payload["consumed"],  # type: ignore[arg-type]
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
        )


def _reveal_claim_path(
    evidence_root: Path, token: OneTimeRevealTokenV1
) -> Path:
    if type(token) is not OneTimeRevealTokenV1 or not token.consumed:
        raise ValueError("one-time reveal claim requires a consumed token")
    root = Path(os.path.abspath(os.fspath(evidence_root)))
    return root.parent / (
        f".{root.name}.reveal-{token.qualification_identity[:24]}.json"
    )


def _reveal_claim_bytes(token: OneTimeRevealTokenV1) -> bytes:
    if type(token) is not OneTimeRevealTokenV1 or not token.consumed:
        raise ValueError("one-time reveal claim requires a consumed token")
    return canonical_json_bytes(
        {
            "implementation_commit": token.implementation_commit,
            "qualification_identity": token.qualification_identity,
            "reveal_token_id": token.token_id,
            "schema_version": 1,
            "status": "CONSUMED_BEFORE_PROTECTED_EXECUTION",
        }
    )


def _claim_one_time_reveal(
    evidence_root: Path, token: OneTimeRevealTokenV1
) -> Path:
    """Durably burn the reveal identity before the first protected seed draw."""

    claim = _reveal_claim_path(evidence_root, token)
    claim.parent.mkdir(parents=True, exist_ok=True)
    payload = _reveal_claim_bytes(token)
    descriptor = os.open(
        claim,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_descriptor = os.open(
        claim.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return claim


def _verify_one_time_reveal_claim(
    evidence_root: Path, token: OneTimeRevealTokenV1
) -> Path:
    """Require the exact durable claim before a fresh child may reveal a root."""

    claim = _reveal_claim_path(evidence_root, token)
    if claim.is_symlink() or not claim.is_file():
        raise RuntimeError("qualification child has no durable one-time reveal claim")
    if claim.read_bytes() != _reveal_claim_bytes(token):
        raise RuntimeError("qualification one-time reveal claim bytes differ")
    return claim


@dataclass(frozen=True, slots=True)
class QualificationEvidenceBundleV1:
    qualification_identity: str
    execution_kind: str
    implementation_commit: str
    profile_bundle_sha256: str
    run_metrics: tuple[QualificationRunMetricsV1, ...]
    run_proofs: tuple[QualificationRunProofV1, ...]
    qualifications: tuple[CandidateQualificationV1, ...]
    review_runs: tuple[ReviewRunV1, ...]
    selections: tuple[SelectedWindowManifestV1, ...]
    review_packet: BlindedReviewPacketV1
    performance: PerformanceEvaluationV1
    reveal_token: OneTimeRevealTokenV1
    protected_seed_access: str
    replay_verification_status: str
    schema_version: int = QUALIFICATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != QUALIFICATION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("qualification evidence schema version must be 1")
        _sha256(self.qualification_identity, "qualification_identity")
        _git_oid(self.implementation_commit, "implementation_commit")
        _sha256(self.profile_bundle_sha256, "profile_bundle_sha256")
        if self.execution_kind not in {"DEVELOPMENT_ONLY", "REAL_ONE_TIME"}:
            raise ValueError("qualification execution kind is invalid")
        if self.protected_seed_access not in {"ABSENT", "QUALIFICATION_AND_HOLDOUT"}:
            raise ValueError("protected seed access status is invalid")
        if (self.execution_kind == "DEVELOPMENT_ONLY") != (
            self.protected_seed_access == "ABSENT"
        ):
            raise ValueError("development execution cannot access protected roots")
        if not self.qualifications:
            raise ValueError("qualification evidence requires dispositions")
        if not self.run_metrics or type(self.run_metrics) is not tuple or any(
            type(row) is not QualificationRunMetricsV1 for row in self.run_metrics
        ):
            raise TypeError("qualification evidence requires typed raw run metrics")
        if type(self.qualifications) is not tuple or any(
            type(row) is not CandidateQualificationV1 for row in self.qualifications
        ):
            raise TypeError("qualification evidence rows must be typed")
        qualification_keys = tuple(
            (row.candidate_id, row.partition) for row in self.qualifications
        )
        if len(qualification_keys) != len(set(qualification_keys)):
            raise ValueError("qualification evidence contains a duplicate disposition")
        metric_keys = {
            (row.candidate_id, row.partition) for row in self.run_metrics
        }
        if set(qualification_keys) != metric_keys:
            raise ValueError("raw run metrics and dispositions have different identities")
        for disposition in self.qualifications:
            source = tuple(
                row
                for row in self.run_metrics
                if (row.candidate_id, row.partition)
                == (disposition.candidate_id, disposition.partition)
            )
            recomputed = evaluate_candidate_qualification(
                source,
                platform_performance_status=disposition.platform_performance_status,
                statistical_status=disposition.statistical_status,
            )
            if recomputed.as_dict() != disposition.as_dict():
                raise ValueError("stored disposition differs from raw metric reduction")
        if self.execution_kind == "REAL_ONE_TIME":
            expected_keys = {
                (candidate_id, partition)
                for candidate_id in CANDIDATE_IDS
                for partition in REAL_PARTITIONS
            }
            if set(qualification_keys) != expected_keys:
                raise ValueError("real evidence omits a candidate/partition disposition")
            if any(
                row.platform_performance_status != self.performance.aggregate_status
                for row in self.qualifications
            ):
                raise ValueError("real dispositions differ from measured performance status")
            if type(self.run_proofs) is not tuple or any(
                type(row) is not QualificationRunProofV1 for row in self.run_proofs
            ):
                raise TypeError("real qualification requires typed run proofs")
            metric_identities = {
                (row.candidate_id, row.partition, row.root_seed, row.run_digest)
                for row in self.run_metrics
            }
            proof_identities = {
                (row.candidate_id, row.partition, row.root_seed, row.run_digest)
                for row in self.run_proofs
            }
            if (
                len(self.run_proofs) != len(proof_identities)
                or proof_identities != metric_identities
            ):
                raise ValueError("run proofs and raw metrics have different identities")
            for metric in self.run_metrics:
                proof = next(
                    row
                    for row in self.run_proofs
                    if (
                        row.candidate_id,
                        row.partition,
                        row.root_seed,
                        row.run_digest,
                    )
                    == (
                        metric.candidate_id,
                        metric.partition,
                        metric.root_seed,
                        metric.run_digest,
                    )
                )
                if metric.exact_replay_passed != (
                    proof.replay_verification_status == "PASS"
                ):
                    raise ValueError("metric replay status differs from its run proof")
        elif self.run_proofs:
            raise ValueError("development-only evidence cannot claim real run proofs")
        if not self.review_runs or type(self.review_runs) is not tuple or any(
            type(row) is not ReviewRunV1 for row in self.review_runs
        ):
            raise TypeError("qualification evidence requires typed review source runs")
        if len({row.run_digest for row in self.review_runs}) != len(self.review_runs):
            raise ValueError("review source contains a duplicate run digest")
        if type(self.selections) is not tuple or any(
            type(row) is not SelectedWindowManifestV1 for row in self.selections
        ):
            raise TypeError("qualification review selections must be typed")
        if len(self.review_packet.windows) != len(self.selections):
            raise ValueError("review packet count differs from selected-window manifests")
        for selection, packet_window in zip(
            self.selections, self.review_packet.windows
        ):
            if (
                packet_window.get("stratum") != selection.stratum
                or packet_window.get("shortfall_status")
                != selection.shortfall_status
            ):
                raise ValueError("review packet ordering differs from selection manifests")
        recomputed_selections = select_review_windows(self.review_runs)
        if tuple(row.as_dict() for row in recomputed_selections) != tuple(
            row.as_dict() for row in self.selections
        ):
            raise ValueError("stored review selection differs from raw observable source")
        recomputed_packet = build_blinded_packet(
            self.review_runs, recomputed_selections
        )
        if recomputed_packet.canonical_bytes() != self.review_packet.canonical_bytes():
            raise ValueError("stored review packet differs from raw observable source")
        if self.reveal_token.qualification_identity != self.qualification_identity:
            raise ValueError("reveal token does not bind qualification identity")
        if self.reveal_token.implementation_commit != self.implementation_commit:
            raise ValueError("reveal token does not bind implementation commit")
        if not self.reveal_token.consumed:
            raise ValueError("persisted evidence requires a consumed reveal token")
        if self.replay_verification_status != "PASS":
            raise ValueError("persisted qualification requires replay verification PASS")

    @property
    def ledger(self) -> dict[str, object]:
        return {
            "execution_kind": self.execution_kind,
            "implementation_commit": self.implementation_commit,
            "one_time_reveal_token_id": self.reveal_token.token_id,
            "profile_bundle_sha256": self.profile_bundle_sha256,
            "protected_seed_access": self.protected_seed_access,
            "qualification_identity": self.qualification_identity,
            "reentry_policy": "VERIFY_ONLY_NEVER_RERUN_OR_OVERWRITE",
            "replay_verification_status": self.replay_verification_status,
            "schema_version": QUALIFICATION_LEDGER_SCHEMA_VERSION,
        }


@dataclass(frozen=True, slots=True)
class QualificationVerificationReport:
    run_id: str
    manifest_valid: bool
    artifact_inventory_valid: bool
    artifact_digests_valid: bool
    canonical_payloads_valid: bool
    schema_inventory_valid: bool
    evidence_digest_valid: bool
    result_digest_valid: bool
    reveal_token_valid: bool
    replay_verification_valid: bool
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures and all(
            (
                self.manifest_valid,
                self.artifact_inventory_valid,
                self.artifact_digests_valid,
                self.canonical_payloads_valid,
                self.schema_inventory_valid,
                self.evidence_digest_valid,
                self.result_digest_valid,
                self.reveal_token_valid,
                self.replay_verification_valid,
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_digests_valid": self.artifact_digests_valid,
            "artifact_inventory_valid": self.artifact_inventory_valid,
            "canonical_payloads_valid": self.canonical_payloads_valid,
            "evidence_digest_valid": self.evidence_digest_valid,
            "failures": list(self.failures),
            "manifest_valid": self.manifest_valid,
            "replay_verification_valid": self.replay_verification_valid,
            "result_digest_valid": self.result_digest_valid,
            "reveal_token_valid": self.reveal_token_valid,
            "run_id": self.run_id,
            "schema_inventory_valid": self.schema_inventory_valid,
            "status": "PASS" if self.passed else "FAIL",
        }


_ARTIFACT_SPECS: Final = (
    (
        "profile-qualification",
        "qualification.json",
        ArtifactType.FULL_DAY_PROFILE_QUALIFICATION,
        "application/vnd.kirby2.full-day-profile-qualification+json",
    ),
    (
        "qualification-run-proofs",
        "run-proofs.json",
        ArtifactType.FULL_DAY_QUALIFICATION_RUN_PROOFS,
        "application/vnd.kirby2.full-day-qualification-run-proofs+json",
    ),
    (
        "review-source",
        "review-source.json",
        ArtifactType.FULL_DAY_REVIEW_SOURCE,
        "application/vnd.kirby2.full-day-review-source+json",
    ),
    (
        "review-selection",
        "review-selection.json",
        ArtifactType.FULL_DAY_REVIEW_SELECTION,
        "application/vnd.kirby2.full-day-review-selection+json",
    ),
    (
        "review-packet",
        "review-packet.json",
        ArtifactType.FULL_DAY_REVIEW_PACKET,
        "application/vnd.kirby2.full-day-review-packet+json",
    ),
    (
        "performance-evidence",
        "performance.json",
        ArtifactType.FULL_DAY_PERFORMANCE_EVIDENCE,
        "application/vnd.kirby2.full-day-performance+json",
    ),
    (
        "qualification-ledger",
        "ledger.json",
        ArtifactType.FULL_DAY_QUALIFICATION_LEDGER,
        "application/vnd.kirby2.full-day-qualification-ledger+json",
    ),
    (
        "reveal-token",
        "reveal-token.json",
        ArtifactType.FULL_DAY_REVEAL_TOKEN,
        "application/vnd.kirby2.full-day-reveal-token+json",
    ),
)


def _artifact_payloads(
    bundle: QualificationEvidenceBundleV1,
) -> dict[str, bytes]:
    return {
        "qualification.json": canonical_json_bytes(
            {
                "qualifications": [row.as_dict() for row in bundle.qualifications],
                "run_metrics": [row.as_dict() for row in bundle.run_metrics],
                "schema_version": QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
            }
        ),
        "run-proofs.json": canonical_json_bytes(
            {
                "run_proofs": [row.as_dict() for row in bundle.run_proofs],
                "schema_version": 1,
            }
        ),
        "review-source.json": canonical_json_bytes(
            {
                "runs": [row.as_dict() for row in bundle.review_runs],
                "schema_version": 1,
            }
        ),
        "review-selection.json": canonical_json_bytes(
            {
                "schema_version": 1,
                "windows": [row.as_dict() for row in bundle.selections],
            }
        ),
        "review-packet.json": bundle.review_packet.canonical_bytes(),
        "performance.json": bundle.performance.canonical_bytes(),
        "ledger.json": canonical_json_bytes(bundle.ledger),
        "reveal-token.json": canonical_json_bytes(bundle.reveal_token.as_dict()),
    }


def _decode_evidence_bundle(
    decoded: Mapping[str, Mapping[str, object]],
) -> QualificationEvidenceBundleV1:
    qualification = decoded["qualification.json"]
    proofs = decoded["run-proofs.json"]
    review_source = decoded["review-source.json"]
    selection = decoded["review-selection.json"]
    ledger = decoded["ledger.json"]
    expected_qualification = {"qualifications", "run_metrics", "schema_version"}
    expected_proofs = {"run_proofs", "schema_version"}
    expected_review_source = {"runs", "schema_version"}
    expected_selection = {"schema_version", "windows"}
    expected_ledger = {
        "execution_kind",
        "implementation_commit",
        "one_time_reveal_token_id",
        "profile_bundle_sha256",
        "protected_seed_access",
        "qualification_identity",
        "reentry_policy",
        "replay_verification_status",
        "schema_version",
    }
    if set(qualification) != expected_qualification:
        raise ValueError("qualification artifact fields differ")
    if set(proofs) != expected_proofs:
        raise ValueError("qualification run-proof artifact fields differ")
    if set(review_source) != expected_review_source:
        raise ValueError("review-source artifact fields differ")
    if set(selection) != expected_selection:
        raise ValueError("review-selection artifact fields differ")
    if set(ledger) != expected_ledger:
        raise ValueError("qualification ledger fields differ")
    if (
        qualification["schema_version"] != QUALIFICATION_EVIDENCE_SCHEMA_VERSION
        or proofs["schema_version"] != 1
        or review_source["schema_version"] != 1
        or selection["schema_version"] != 1
        or ledger["schema_version"] != QUALIFICATION_LEDGER_SCHEMA_VERSION
        or ledger["reentry_policy"] != "VERIFY_ONLY_NEVER_RERUN_OR_OVERWRITE"
    ):
        raise ValueError("qualification artifact policy/schema fields differ")
    raw_metrics = qualification["run_metrics"]
    raw_qualifications = qualification["qualifications"]
    raw_proofs = proofs["run_proofs"]
    raw_review_runs = review_source["runs"]
    raw_selections = selection["windows"]
    if any(
        type(rows) is not list or any(not isinstance(row, Mapping) for row in rows)
        for rows in (
            raw_metrics,
            raw_qualifications,
            raw_proofs,
            raw_review_runs,
            raw_selections,
        )
    ):
        raise TypeError("qualification evidence rows must be arrays of objects")
    token = OneTimeRevealTokenV1.from_dict(decoded["reveal-token.json"])
    if token.token_id != ledger["one_time_reveal_token_id"]:
        raise ValueError("qualification ledger token ID differs")
    performance = PerformanceEvaluationV1.from_dict(decoded["performance.json"])
    recomputed_performance = evaluate_performance(
        performance.raw_observations,
        performance.platform,
        load_full_day_profile_bundle().performance,
    )
    if recomputed_performance.as_dict() != performance.as_dict():
        raise ValueError("stored performance reduction differs from raw observations")
    return QualificationEvidenceBundleV1(
        qualification_identity=str(ledger["qualification_identity"]),
        execution_kind=str(ledger["execution_kind"]),
        implementation_commit=str(ledger["implementation_commit"]),
        profile_bundle_sha256=str(ledger["profile_bundle_sha256"]),
        run_metrics=tuple(
            QualificationRunMetricsV1.from_dict(row) for row in raw_metrics
        ),
        run_proofs=tuple(QualificationRunProofV1.from_dict(row) for row in raw_proofs),
        qualifications=tuple(
            CandidateQualificationV1.from_dict(row) for row in raw_qualifications
        ),
        review_runs=tuple(ReviewRunV1.from_dict(row) for row in raw_review_runs),
        selections=tuple(
            SelectedWindowManifestV1.from_dict(row) for row in raw_selections
        ),
        review_packet=BlindedReviewPacketV1.from_dict(decoded["review-packet.json"]),
        performance=performance,
        reveal_token=token,
        protected_seed_access=str(ledger["protected_seed_access"]),
        replay_verification_status=str(ledger["replay_verification_status"]),
    )


def _configuration_digest(ledger: Mapping[str, object]) -> str:
    return canonical_sha256(
        {
            "execution_kind": ledger["execution_kind"],
            "implementation_commit": ledger["implementation_commit"],
            "profile_bundle_sha256": ledger["profile_bundle_sha256"],
            "qualification_identity": ledger["qualification_identity"],
        }
    )


def _evidence_digest(
    references: Sequence[ArtifactReference],
) -> str:
    # Raw timing/RSS are persisted and verified, but excluded from the semantic
    # evidence identity frozen before those operational measurements exist.
    return canonical_sha256(
        {
            "artifacts": [
                {
                    "artifact_type": reference.artifact_type.value,
                    "sha256": reference.sha256,
                }
                for reference in references
                if reference.artifact_type
                is not ArtifactType.FULL_DAY_PERFORMANCE_EVIDENCE
            ],
            "operational_performance_bytes_excluded": True,
        }
    )


def _result_digest(payloads: Mapping[str, bytes]) -> str:
    qualification = parse_canonical_json_object(payloads["qualification.json"])
    selections = parse_canonical_json_object(payloads["review-selection.json"])
    packet = parse_canonical_json_object(payloads["review-packet.json"])
    performance = parse_canonical_json_object(payloads["performance.json"])
    return canonical_sha256(
        {
            "performance_aggregate_status": performance["aggregate_status"],
            "platform_status": performance["platform_status"],
            "qualifications": qualification["qualifications"],
            "run_metrics": qualification["run_metrics"],
            "run_proofs": parse_canonical_json_object(
                payloads["run-proofs.json"]
            )["run_proofs"],
            "review_source_sha256": hashlib.sha256(
                payloads["review-source.json"]
            ).hexdigest(),
            "review_packet_sha256": canonical_sha256(packet),
            "selected_window_manifests": selections["windows"],
        }
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class QualificationEvidenceStore:
    """Single-identity, fsync-before-rename immutable qualification destination."""

    def __init__(self, root: Path) -> None:
        supplied = Path(os.path.abspath(os.fspath(root)))
        if supplied.is_symlink():
            raise ValueError("qualification evidence root cannot be a symlink")
        self.paths = DataPaths(supplied.resolve(strict=False))
        self.root = self.paths.root
        self.runs_directory = self.root / "runs"

    def run_directory(self, run_id: str) -> Path:
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("qualification run ID is invalid")
        return self.runs_directory / run_id

    def _single_run_id(self) -> str:
        self.paths.validate()
        if self.root.is_symlink():
            raise ValueError("qualification evidence root cannot be a symlink")
        if not self.runs_directory.is_dir():
            raise ValueError("qualification evidence has no immutable run directory")
        run_ids = tuple(
            path.name
            for path in sorted(self.runs_directory.iterdir())
            if path.is_dir() and _RUN_ID.fullmatch(path.name)
        )
        if len(run_ids) != 1 or tuple(
            path.name for path in sorted(self.runs_directory.iterdir())
        ) != run_ids:
            raise ValueError("qualification destination must contain exactly one run")
        return run_ids[0]

    def load_manifest(self, run_id: str | None = None) -> RunManifest:
        selected = self._single_run_id() if run_id is None else run_id
        raw = (self.run_directory(selected) / "manifest.toml").read_bytes()
        manifest = RunManifest.from_dict(load_toml(self.run_directory(selected) / "manifest.toml"))
        if manifest.to_toml().encode("utf-8") != raw:
            raise ValueError("qualification manifest is not canonical TOML")
        return manifest

    def _existing_identity(self) -> str:
        run_id = self._single_run_id()
        payload = parse_canonical_json_object(
            (self.run_directory(run_id) / "ledger.json").read_bytes()
        )
        return _sha256(payload.get("qualification_identity"), "qualification_identity")

    def persist(self, bundle: QualificationEvidenceBundleV1) -> RunManifest:
        """Persist once; an exact re-entry verifies and returns without executing."""

        parent = self.root.parent
        self.paths.validate()
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = parent / f".{self.root.name}.qualification.lock"
        import fcntl

        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self.paths.validate()
                if self.root.exists() or self.root.is_symlink():
                    if self.root.is_symlink() or not self.root.is_dir():
                        raise RuntimeError("qualification destination is not a safe directory")
                    if self._existing_identity() != bundle.qualification_identity:
                        raise RuntimeError(
                            "qualification destination belongs to another one-time identity"
                        )
                    existing = self.load_manifest()
                    report = self.verify(existing.run_id)
                    if not report.passed:
                        raise RuntimeError(
                            "existing qualification evidence is invalid and will not be overwritten: "
                            + "; ".join(report.failures)
                        )
                    return existing
                return self._persist_fresh(bundle)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _persist_fresh(self, bundle: QualificationEvidenceBundleV1) -> RunManifest:
        payloads = _artifact_payloads(bundle)
        row_counts = {
            "qualification.json": len(bundle.run_metrics),
            "run-proofs.json": len(bundle.run_proofs),
            "review-source.json": len(bundle.review_runs),
            "review-selection.json": len(bundle.selections),
            "review-packet.json": len(bundle.review_packet.windows),
        }
        references = tuple(
            ArtifactReference(
                name=name,
                relative_path=relative_path,
                sha256=hashlib.sha256(payloads[relative_path]).hexdigest(),
                schema_version=1,
                row_count=row_counts.get(relative_path),
                media_type=media_type,
                artifact_type=artifact_type,
            )
            for name, relative_path, artifact_type, media_type in _ARTIFACT_SPECS
        )
        manifest = RunManifest.create(
            parent_run_id=None,
            run_type=RunType.FULL_DAY_QUALIFICATION,
            scenario_id=bundle.qualification_identity,
            lesson_id=None,
            seed=None,
            flow_model="FULL_DAY_PROFILE_QUALIFICATION_V1",
            market_profile="FULL_DAY_PROFILE_BUNDLE:" + bundle.profile_bundle_sha256,
            strategy_id="NONE",
            hotkey_layout_id="NONE",
            session_objective="ONE_TIME_FULL_DAY_PROFILE_QUALIFICATION",
            simulation_start_us=0,
            simulation_end_us=0,
            software_version=software_version(),
            git_commit=bundle.implementation_commit,
            schema_versions={
                "qualification_evidence": QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
                "qualification_ledger": QUALIFICATION_LEDGER_SCHEMA_VERSION,
                "reveal_token": REVEAL_TOKEN_SCHEMA_VERSION,
                "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
            },
            input_dataset_references=(
                "full-day-profile-bundle:" + bundle.profile_bundle_sha256,
                "qualification-identity:" + bundle.qualification_identity,
            ),
            configuration_digest=_configuration_digest(bundle.ledger),
            evidence_digest=_evidence_digest(references),
            result_digest=_result_digest(payloads),
            creation_timestamp_utc=_utc_now(),
            artifacts=references,
        )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{self.root.name}.staging-", dir=self.root.parent)
        )
        activated = False
        try:
            directory = staging / "runs" / manifest.run_id
            directory.mkdir(parents=True)
            for relative_path, payload in payloads.items():
                path = directory / relative_path
                path.write_bytes(payload)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            manifest_path = directory / "manifest.toml"
            manifest_path.write_text(manifest.to_toml(), encoding="utf-8")
            with manifest_path.open("rb") as stream:
                os.fsync(stream.fileno())
            for path in (directory, directory.parent, staging):
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self.paths.validate()
            staging.rename(self.root)
            activated = True
        finally:
            if not activated and staging.exists():
                shutil.rmtree(staging)
        report = self.verify(manifest.run_id)
        if not report.passed:
            raise RuntimeError(
                "new immutable qualification evidence failed verification: "
                + "; ".join(report.failures)
            )
        return self.load_manifest(manifest.run_id)

    def verify(self, run_id: str | None = None) -> QualificationVerificationReport:
        failures: list[str] = []
        flags = {
            "manifest_valid": False,
            "artifact_inventory_valid": False,
            "artifact_digests_valid": False,
            "canonical_payloads_valid": False,
            "schema_inventory_valid": False,
            "evidence_digest_valid": False,
            "result_digest_valid": False,
            "reveal_token_valid": False,
            "replay_verification_valid": False,
        }
        selected = run_id or "run-000000000000000000000000"
        try:
            selected = self._single_run_id() if run_id is None else run_id
            manifest = self.load_manifest(selected)
            if manifest.run_type is not RunType.FULL_DAY_QUALIFICATION:
                raise ValueError("manifest run type is not full-day qualification")
            if manifest.run_id != selected:
                raise ValueError("manifest run ID differs from directory")
            flags["manifest_valid"] = True
        except (OSError, TypeError, ValueError) as error:
            failures.append(f"manifest invalid: {error}")
            return QualificationVerificationReport(selected, **flags, failures=tuple(failures))
        expected_inventory = tuple(
            (name, path, artifact_type, media_type)
            for name, path, artifact_type, media_type in _ARTIFACT_SPECS
        )
        actual_inventory = tuple(
            (
                reference.name,
                reference.relative_path,
                reference.artifact_type,
                reference.media_type,
            )
            for reference in manifest.artifacts
        )
        flags["artifact_inventory_valid"] = actual_inventory == expected_inventory
        if not flags["artifact_inventory_valid"]:
            failures.append("typed qualification artifact inventory differs")
        directory = self.run_directory(selected)
        payloads: dict[str, bytes] = {}
        try:
            for reference in manifest.artifacts:
                path = directory / reference.relative_path
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"unsafe or missing artifact: {reference.relative_path}")
                payloads[reference.relative_path] = path.read_bytes()
            extras = {
                path.name
                for path in directory.iterdir()
                if path.name != "manifest.toml"
            } - set(payloads)
            if extras:
                raise ValueError("qualification run contains unregistered artifact files")
            flags["artifact_digests_valid"] = all(
                hashlib.sha256(payloads[reference.relative_path]).hexdigest()
                == reference.sha256
                for reference in manifest.artifacts
            )
            if not flags["artifact_digests_valid"]:
                failures.append("one or more qualification artifact digests differ")
        except (OSError, ValueError) as error:
            failures.append(f"qualification artifact inventory invalid: {error}")
        decoded: dict[str, dict[str, object]] = {}
        evidence_bundle: QualificationEvidenceBundleV1 | None = None
        if len(payloads) == len(_ARTIFACT_SPECS):
            try:
                for relative_path, raw in payloads.items():
                    decoded[relative_path] = parse_canonical_json_object(raw)
                evidence_bundle = _decode_evidence_bundle(decoded)
                expected_bundle = load_full_day_profile_bundle().bundle_sha256
                if evidence_bundle.profile_bundle_sha256 != expected_bundle:
                    raise ValueError("qualification evidence binds a foreign profile bundle")
                if manifest.git_commit != evidence_bundle.implementation_commit:
                    raise ValueError("qualification manifest commit differs from ledger")
                if manifest.configuration_digest != _configuration_digest(
                    evidence_bundle.ledger
                ):
                    raise ValueError("qualification configuration digest differs")
                expected_counts = {
                    "qualification.json": len(evidence_bundle.run_metrics),
                    "run-proofs.json": len(evidence_bundle.run_proofs),
                    "review-source.json": len(evidence_bundle.review_runs),
                    "review-selection.json": len(evidence_bundle.selections),
                    "review-packet.json": len(evidence_bundle.review_packet.windows),
                }
                if any(
                    reference.row_count != expected_counts.get(reference.relative_path)
                    for reference in manifest.artifacts
                ):
                    raise ValueError("qualification artifact row counts differ")
                if evidence_bundle.execution_kind == "REAL_ONE_TIME":
                    expected_identity = real_qualification_identity(
                        evidence_bundle.implementation_commit,
                        load_full_day_profile_bundle(),
                    )
                    if evidence_bundle.qualification_identity != expected_identity:
                        raise ValueError("real qualification identity differs from policy")
                flags["canonical_payloads_valid"] = True
            except (TypeError, ValueError) as error:
                failures.append(f"qualification artifact JSON invalid: {error}")
        expected_schemas = {
            "qualification_evidence": QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
            "qualification_ledger": QUALIFICATION_LEDGER_SCHEMA_VERSION,
            "reveal_token": REVEAL_TOKEN_SCHEMA_VERSION,
            "run_manifest": RUN_MANIFEST_SCHEMA_VERSION,
        }
        flags["schema_inventory_valid"] = (
            manifest.schema_versions == expected_schemas
            and all(reference.schema_version == 1 for reference in manifest.artifacts)
        )
        if not flags["schema_inventory_valid"]:
            failures.append("qualification schema inventory differs")
        if flags["canonical_payloads_valid"]:
            ledger = decoded["ledger.json"]
            token = decoded["reveal-token.json"]
            try:
                flags["evidence_digest_valid"] = (
                    _evidence_digest(manifest.artifacts) == manifest.evidence_digest
                )
                flags["result_digest_valid"] = (
                    _result_digest(payloads) == manifest.result_digest
                )
                flags["reveal_token_valid"] = (
                    token.get("consumed") is True
                    and token.get("qualification_identity")
                    == ledger.get("qualification_identity")
                    and token.get("implementation_commit")
                    == ledger.get("implementation_commit")
                    and token.get("token_id") == ledger.get("one_time_reveal_token_id")
                )
                flags["replay_verification_valid"] = (
                    ledger.get("replay_verification_status") == "PASS"
                )
            except (KeyError, TypeError, ValueError) as error:
                failures.append(f"qualification derived evidence invalid: {error}")
            for flag, detail in (
                ("evidence_digest_valid", "qualification evidence digest differs"),
                ("result_digest_valid", "qualification result digest differs"),
                ("reveal_token_valid", "one-time reveal token binding differs"),
                (
                    "replay_verification_valid",
                    "qualification replay verification is not PASS",
                ),
            ):
                if not flags[flag]:
                    failures.append(detail)
        return QualificationVerificationReport(
            selected, **flags, failures=tuple(dict.fromkeys(failures))
        )


@dataclass(frozen=True, slots=True)
class DevelopmentQualificationReportV1:
    fixture_id: str
    case_results: tuple[tuple[str, str, str, str], ...]
    performance_branches: tuple[tuple[str, str], ...]
    review_selected_count: int
    review_shortfall_count: int
    review_not_applicable_count: int
    persistence_run_id: str
    verification_status: str
    refusal_count: int
    protected_seed_access: str
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        return {
            "case_results": [
                {
                    "automated": automated,
                    "behavioral": behavioral,
                    "case_id": case_id,
                    "engineering": engineering,
                }
                for case_id, engineering, behavioral, automated in self.case_results
            ],
            "failures": list(self.failures),
            "fixture_id": self.fixture_id,
            "performance_branches": dict(self.performance_branches),
            "persistence_run_id": self.persistence_run_id,
            "protected_seed_access": self.protected_seed_access,
            "refusal_count": self.refusal_count,
            "review_not_applicable_count": self.review_not_applicable_count,
            "review_selected_count": self.review_selected_count,
            "review_shortfall_count": self.review_shortfall_count,
            "status": "PASS" if self.passed else "FAIL",
            "verification_status": self.verification_status,
        }


_FIXTURE_FIELDS: Final = {
    "cases",
    "execution_kind",
    "fixture_id",
    "profile_bundle_sha256",
    "protected_seed_access",
    "schema_version",
    "toy_values",
}
_CASE_FIELDS: Final = {
    "candidate_id",
    "case_id",
    "expected_automated",
    "expected_behavioral",
    "expected_engineering",
    "performance_status",
    "profile_kind",
    "root_count",
    "root_seed_base",
    "variant",
}
_TOY_VALUE_FIELDS: Final = {
    "aggressive_buy_shares",
    "aggressive_sell_shares",
    "continuous_quote_eligible_us",
    "continuous_quote_occupied_us",
    "disorderly_cancel_eligible_us",
    "disorderly_final_quote_eligible_us",
    "disorderly_final_quote_occupied_us",
    "disorderly_final_spread_ticks",
    "disorderly_midday_cancel_count",
    "disorderly_midday_spread_ticks",
    "disorderly_open_cancel_count",
    "disorderly_open_spread_ticks",
    "event_pre_aggressive_shares",
    "event_pre_range_ticks",
    "event_recovery_quote_eligible_us",
    "event_recovery_quote_occupied_us",
    "event_recovery_spread_ticks",
    "event_shock_aggressive_shares",
    "event_shock_range_ticks",
    "event_shock_spread_ticks",
    "maximum_continuous_spread_ticks",
    "maximum_nonhalt_empty_side_episode_us",
    "quiet_displacement_ticks",
    "quiet_spread_ticks",
    "trade_count",
    "trend_favored_shares",
    "trend_first_trade_ticks",
    "trend_last_trade_ticks",
    "trend_other_shares",
}


def load_qualification_development_fixture(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    payload = tomllib.loads(raw.decode("utf-8"))
    if set(payload) != _FIXTURE_FIELDS:
        raise ValueError("qualification development fixture fields differ")
    if payload["schema_version"] != 1:
        raise ValueError("qualification development fixture schema version must be 1")
    if payload["execution_kind"] != "DEVELOPMENT_ONLY":
        raise ValueError("qualification development fixture must be development-only")
    if payload["protected_seed_access"] != "ABSENT":
        raise ValueError("qualification development fixture cannot access protected seeds")
    bundle = load_full_day_profile_bundle()
    if payload["profile_bundle_sha256"] != bundle.bundle_sha256:
        raise ValueError("development fixture does not bind exact WO31-H manifests")
    values = payload["toy_values"]
    cases = payload["cases"]
    if not isinstance(values, dict) or set(values) != _TOY_VALUE_FIELDS:
        raise ValueError("development toy-value inventory differs")
    if any(type(value) is not int or value < 0 for value in values.values()):
        raise ValueError("development toy values must be nonnegative integers")
    if type(cases) is not list or not cases:
        raise ValueError("development fixture cases must be a nonempty array")
    if any(not isinstance(case, dict) or set(case) != _CASE_FIELDS for case in cases):
        raise ValueError("development fixture case fields differ")
    case_ids = tuple(str(case["case_id"]) for case in cases)
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("development fixture case IDs must be unique")
    used_roots: set[int] = set()
    protected = {*DEVELOPMENT_ROOTS, *QUALIFICATION_ROOTS, *HOLDOUT_ROOTS}
    for case in cases:
        base = _exact_int(case["root_seed_base"], "root_seed_base")
        count = _exact_int(case["root_count"], "root_count", minimum=1)
        roots = set(range(base, base + count))
        if roots & protected or roots & used_roots:
            raise ValueError("development roots overlap protected or prior fixture roots")
        used_roots.update(roots)
        if case["profile_kind"] not in PROFILE_KINDS:
            raise ValueError("development case profile kind is unknown")
        if not str(case["candidate_id"]).startswith("DEV_ONLY_"):
            raise ValueError("development candidate ID lacks DEV_ONLY namespace")
        if case["variant"] not in {
            "PASS",
            "BEHAVIOR_WARNING",
            "UNIVERSAL_FAILURE",
            "ABORT",
            "INSUFFICIENT",
        }:
            raise ValueError("development case variant is unknown")
    return payload


def _development_rows(
    case: Mapping[str, object], values: Mapping[str, object]
) -> tuple[QualificationRunMetricsV1, ...]:
    rows: list[QualificationRunMetricsV1] = []
    profile_kind = str(case["profile_kind"])
    variant = str(case["variant"])
    base = int(case["root_seed_base"])
    count = int(case["root_count"])
    value = lambda name: int(values[name])
    for ordinal, root in enumerate(range(base, base + count)):
        trade_count = value("trade_count")
        abort_count = 0
        if variant == "UNIVERSAL_FAILURE" and ordinal == 0:
            trade_count = 99
        if variant == "ABORT" and ordinal == 0:
            abort_count = 1
        quiet_spread = (
            12
            if variant == "BEHAVIOR_WARNING"
            and profile_kind == QUIET_RANGE_PRESSURE
            else value("quiet_spread_ticks")
        )
        favored_side = "BUY" if ordinal % 2 == 0 else "SELL"
        favored = value("trend_favored_shares")
        other = value("trend_other_shares")
        trend_buy = favored if favored_side == "BUY" else other
        trend_sell = favored if favored_side == "SELL" else other
        first = value("trend_first_trade_ticks")
        last = value("trend_last_trade_ticks")
        if favored_side == "SELL":
            last = first - (last - first)
        insufficient = variant == "INSUFFICIENT" and profile_kind == EVENT_SHOCK_PRESSURE
        identity = {
            "candidate_id": case["candidate_id"],
            "fixture_root": root,
            "profile_kind": profile_kind,
            "variant": variant,
        }
        rows.append(
            QualificationRunMetricsV1(
                candidate_id=str(case["candidate_id"]),
                profile_kind=profile_kind,
                partition="DEVELOPMENT",
                root_seed=root,
                run_digest=canonical_sha256(identity),
                runtime_invariants_passed=True,
                exact_replay_passed=True,
                safety_abort_count=abort_count,
                trade_count=trade_count,
                continuous_quote_occupied_us=value(
                    "continuous_quote_occupied_us"
                ),
                continuous_quote_eligible_us=value(
                    "continuous_quote_eligible_us"
                ),
                maximum_nonhalt_empty_side_episode_us=value(
                    "maximum_nonhalt_empty_side_episode_us"
                ),
                maximum_continuous_spread_ticks=value(
                    "maximum_continuous_spread_ticks"
                ),
                target_price_operations=0,
                forced_trade_operations=0,
                spread_segments=(TimeWeightedValueV1(quiet_spread, 100, ordinal),),
                aggressive_buy_shares=(
                    trend_buy
                    if profile_kind == TREND_PRESSURE
                    else value("aggressive_buy_shares")
                ),
                aggressive_sell_shares=(
                    trend_sell
                    if profile_kind == TREND_PRESSURE
                    else value("aggressive_sell_shares")
                ),
                first_trade_ticks=first,
                last_trade_ticks=last,
                maximum_absolute_trade_displacement_ticks=value(
                    "quiet_displacement_ticks"
                ),
                favored_side=favored_side,
                pre_aggressive_shares=(
                    0 if insufficient else value("event_pre_aggressive_shares")
                ),
                shock_aggressive_shares=value("event_shock_aggressive_shares"),
                pre_quote_range_ticks=(
                    None if insufficient else value("event_pre_range_ticks")
                ),
                shock_quote_range_ticks=(
                    None if insufficient else value("event_shock_range_ticks")
                ),
                pre_trade_range_ticks=None,
                shock_trade_range_ticks=None,
                event_ratio_halt_affected=insufficient,
                shock_spread_segments=(
                    ()
                    if insufficient
                    else (
                        TimeWeightedValueV1(
                            value("event_shock_spread_ticks"), 100, ordinal
                        ),
                    )
                ),
                recovery_spread_segments=(
                    ()
                    if insufficient
                    else (
                        TimeWeightedValueV1(
                            value("event_recovery_spread_ticks"), 100, ordinal
                        ),
                    )
                ),
                recovery_quote_occupied_us=(
                    None
                    if insufficient
                    else value("event_recovery_quote_occupied_us")
                ),
                recovery_quote_eligible_us=(
                    None
                    if insufficient
                    else value("event_recovery_quote_eligible_us")
                ),
                open_spread_segments=(
                    TimeWeightedValueV1(
                        value("disorderly_open_spread_ticks"), 100, ordinal
                    ),
                ),
                midday_spread_segments=(
                    TimeWeightedValueV1(
                        value("disorderly_midday_spread_ticks"), 100, ordinal
                    ),
                ),
                final_spread_segments=(
                    TimeWeightedValueV1(
                        value("disorderly_final_spread_ticks"), 100, ordinal
                    ),
                ),
                open_cancel_count=value("disorderly_open_cancel_count"),
                open_cancel_eligible_us=value("disorderly_cancel_eligible_us"),
                midday_cancel_count=value("disorderly_midday_cancel_count"),
                midday_cancel_eligible_us=value("disorderly_cancel_eligible_us"),
                final_quote_occupied_us=value(
                    "disorderly_final_quote_occupied_us"
                ),
                final_quote_eligible_us=value(
                    "disorderly_final_quote_eligible_us"
                ),
            )
        )
    return tuple(rows)


def _development_platform(*, eligible: bool) -> PerformancePlatformFingerprintV1:
    return PerformancePlatformFingerprintV1(
        system="Darwin" if eligible else "Linux",
        machine="arm64" if eligible else "x86_64",
        python_implementation="CPython",
        python_major=3,
        python_minor=14,
        python_patch=0,
        python_runtime="3.14.0-development-fixture",
        logical_cpu_count=8,
        physical_memory_bytes=16 * 1024**3,
        free_governed_store_bytes_before_warmup=24 * 1024**3,
        ru_maxrss_normalization_rule=(
            "DARWIN_RU_MAXRSS_VALUE_IS_BYTES"
            if eligible
            else "LINUX_RU_MAXRSS_KIB_TIMES_1024"
        ),
    )


def _performance_observations(
    label: str, generation_ns: int, event_count: int
) -> tuple[PerformanceObservationV1, ...]:
    return tuple(
        PerformanceObservationV1(
            artifact_digest=canonical_sha256(
                {"development_performance": label, "ordinal": ordinal}
            ),
            generation_elapsed_ns=generation_ns,
            replay_elapsed_ns=100_000_000_000,
            outer_event_count=event_count,
            complete_run_bytes=1024**3,
            largest_checkpoint_bytes=64 * 1024**2,
            peak_rss_bytes=1024**3,
            maximum_pending_item_count=1_000,
            maximum_timestamp_distinct_microsteps=10,
            maximum_timestamp_emitted_event_count=100,
        )
        for ordinal in range(3)
    )


def _development_performance_branches(
    bundle: FullDayProfileBundleV1,
) -> tuple[dict[str, PerformanceEvaluationV1], tuple[str, ...]]:
    evaluations = {
        "PASS": evaluate_performance(
            _performance_observations("pass", 2_000_000_000, 2_000),
            _development_platform(eligible=True),
            bundle.performance,
        ),
        "WARNING": evaluate_performance(
            _performance_observations("warning", 400_000_000_000, 200_000),
            _development_platform(eligible=True),
            bundle.performance,
        ),
        "FAIL": evaluate_performance(
            _performance_observations("fail", 901_000_000_000, 50_000),
            _development_platform(eligible=True),
            bundle.performance,
        ),
        "UNSUPPORTED": evaluate_performance(
            _performance_observations("unsupported", 2_000_000_000, 2_000),
            _development_platform(eligible=False),
            bundle.performance,
        ),
    }
    failures = [
        f"performance {name} branch returned {evaluation.aggregate_status}"
        for name, evaluation in evaluations.items()
        if evaluation.aggregate_status != name
    ]
    abort_evaluation = evaluate_performance(
        tuple(
            _failed_performance_observation(
                ordinal,
                bundle.performance.as_dict(),
                "DEVELOPMENT_ABORT",
            )
            for ordinal in range(3)
        ),
        _development_platform(eligible=True),
        bundle.performance,
    )
    abort_codes = tuple(
        reason
        for reason in abort_evaluation.abort_reasons
        if reason.endswith("WORKER_DEVELOPMENT_ABORT")
    )
    if abort_evaluation.aggregate_status != "FAIL" or len(abort_codes) != 3:
        failures.append("explicit performance abort branch did not fail with diagnostics")
    return evaluations, tuple(failures)


def _development_review() -> tuple[
    tuple[ReviewRunV1, ...],
    tuple[SelectedWindowManifestV1, ...],
    BlindedReviewPacketV1,
]:
    samples = tuple(
        ObservableSampleV1(
            simulation_time_us=time_us,
            phase_relative_time_us=time_us,
            observable_feed={
                "best_ask_ticks": 101 + (ordinal % 3),
                "best_bid_ticks": 99 - (ordinal % 2),
                "last_trade_ticks": 100 + (ordinal % 5),
                "visible_ask_shares": 20 + ordinal,
                "visible_bid_shares": 22 + ordinal,
            },
        )
        for ordinal, time_us in enumerate(range(0, 3_600_000_001, 30_000_000))
    )
    run = ReviewRunV1(
        candidate_id="DEV_ONLY_EVENT",
        root_seed=9_199_001,
        run_digest=canonical_sha256({"development_review": "observable-run-v1"}),
        session_start_us=0,
        session_end_us=3_600_000_000,
        continuous_start_us=0,
        continuous_end_us=3_600_000_000,
        phase_boundaries_us=(),
        halt_intervals_us=(),
        event_time_us=1_800_000_000,
        samples=samples,
    )
    runs = (run,)
    selections = select_review_windows(runs)
    packet = build_blinded_packet(runs, selections)
    return runs, selections, packet


def _development_review_shortfalls() -> tuple[SelectedWindowManifestV1, ...]:
    run = ReviewRunV1(
        candidate_id="DEV_ONLY_QUIET",
        root_seed=9_199_002,
        run_digest=canonical_sha256({"development_review": "shortfall-run-v1"}),
        session_start_us=0,
        session_end_us=120_000_000,
        continuous_start_us=0,
        continuous_end_us=120_000_000,
        phase_boundaries_us=(),
        halt_intervals_us=(),
        event_time_us=None,
        samples=(),
    )
    return select_review_windows((run,))


def run_qualification_development_fixture(
    fixture_path: Path,
) -> DevelopmentQualificationReportV1:
    """Exercise every frozen branch without touching any WO31-H execution seed."""

    payload = load_qualification_development_fixture(fixture_path)
    fixture_id = str(payload["fixture_id"])
    values = payload["toy_values"]
    cases = payload["cases"]
    if not isinstance(values, Mapping) or not isinstance(cases, list):
        raise AssertionError("validated fixture container changed type")
    failures: list[str] = []
    dispositions: list[CandidateQualificationV1] = []
    all_run_metrics: list[QualificationRunMetricsV1] = []
    results: list[tuple[str, str, str, str]] = []
    for case in cases:
        if not isinstance(case, Mapping):
            raise AssertionError("validated fixture case changed type")
        case_rows = _development_rows(case, values)
        all_run_metrics.extend(case_rows)
        disposition = evaluate_candidate_qualification(
            case_rows,
            platform_performance_status=str(case["performance_status"]),
        )
        dispositions.append(disposition)
        result = (
            str(case["case_id"]),
            disposition.engineering_status,
            disposition.behavioral_envelope_status,
            disposition.automated_disposition,
        )
        results.append(result)
        expected = (
            str(case["case_id"]),
            str(case["expected_engineering"]),
            str(case["expected_behavioral"]),
            str(case["expected_automated"]),
        )
        if result != expected:
            failures.append(f"development case {case['case_id']} returned {result[1:]}")
    bundle = load_full_day_profile_bundle()
    performance, performance_failures = _development_performance_branches(bundle)
    failures.extend(performance_failures)
    review_runs, selections, packet = _development_review()
    shortfall_probe = _development_review_shortfalls()
    selected_count = sum(row.shortfall_status == "SELECTED" for row in selections)
    shortfall_count = sum(
        row.shortfall_status == "SHORTFALL" for row in shortfall_probe
    )
    not_applicable_count = sum(
        row.shortfall_status == "NOT_APPLICABLE" for row in shortfall_probe
    )
    if selected_count != 12 or shortfall_count != 10 or not_applicable_count != 1:
        failures.append("development review selection/shortfall branches differ")
    if any(
        key in packet.canonical_bytes().upper()
        for key in (
            b"CANDIDATE_ID",
            b"ROOT_SEED",
            b"PRESSURE_CONTROLS",
            b"FUTURE_OUTCOME",
            b"TRUTH",
        )
    ):
        failures.append("development review packet contains a blinded field")
    refusal_count = 0
    mutable_feed: dict[str, object] = {"depth": {"bids": [100]}}
    ownership_sample = ObservableSampleV1(
        simulation_time_us=1,
        phase_relative_time_us=1,
        observable_feed=mutable_feed,
    )
    ownership_bytes = canonical_json_bytes(ownership_sample.as_dict())
    nested_depth = mutable_feed["depth"]
    assert isinstance(nested_depth, dict)
    nested_bids = nested_depth["bids"]
    assert isinstance(nested_bids, list)
    nested_bids.append(99)
    if canonical_json_bytes(ownership_sample.as_dict()) == ownership_bytes:
        refusal_count += 1
    else:
        failures.append("observable sample retained caller-owned review data")
    try:
        ownership_sample.observable_feed.update({"forged": True})  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        refusal_count += 1
    else:
        failures.append("observable sample accepted direct nested mutation")
    packet_bytes = packet.canonical_bytes()
    packet_export = packet.as_dict()
    exported_windows = packet_export["windows"]
    assert isinstance(exported_windows, list)
    exported_windows.append({"forged": True})
    if packet.canonical_bytes() == packet_bytes:
        refusal_count += 1
    else:
        failures.append("blinded packet export mutated immutable review evidence")
    sidecar = ReviewerSidecarV1(
        packet_sha256=packet.sha256,
        reviewer_id="DEVELOPMENT_REVIEWER",
        human_status="ACCEPTED",
        rubric_outcomes=(("WINDOW-0000", "PLAUSIBLE"),),
    )
    if sidecar.human_status != "ACCEPTED" or any(
        disposition.human_review_status != "PENDING" for disposition in dispositions
    ):
        failures.append("reviewer sidecar changed automated evidence authority")
    try:
        ReviewerSidecarV1(
            packet_sha256=packet.sha256,
            reviewer_id="DEVELOPMENT_REVIEWER",
            human_status="PENDING",
            rubric_outcomes=(),
        )
    except ValueError:
        refusal_count += 1
    else:
        failures.append("reviewer sidecar accepted an automated PENDING status")
    implementation_commit = canonical_sha256(
        {"development_fixture": fixture_id, "source_card": "WO31-I"}
    )
    qualification_identity = canonical_sha256(
        {
            "dispositions": [row.as_dict() for row in dispositions],
            "fixture_id": fixture_id,
            "profile_bundle_sha256": bundle.bundle_sha256,
        }
    )
    token = OneTimeRevealTokenV1.issue(
        qualification_identity, implementation_commit
    )
    consumed = token.consume()
    try:
        consumed.consume()
    except RuntimeError:
        refusal_count += 1
    else:
        failures.append("consumed one-time reveal token was accepted again")
    evidence = QualificationEvidenceBundleV1(
        qualification_identity=qualification_identity,
        execution_kind="DEVELOPMENT_ONLY",
        implementation_commit=implementation_commit,
        profile_bundle_sha256=bundle.bundle_sha256,
        run_metrics=tuple(all_run_metrics),
        run_proofs=(),
        qualifications=tuple(dispositions),
        review_runs=review_runs,
        selections=selections,
        review_packet=packet,
        performance=performance["PASS"],
        reveal_token=consumed,
        protected_seed_access="ABSENT",
        replay_verification_status="PASS",
    )
    persistence_run_id = "UNAVAILABLE"
    verification_status = "FAIL"
    with tempfile.TemporaryDirectory(prefix="kirby2-qualification-dev-") as temporary:
        temporary_root = Path(temporary)
        claim_root = temporary_root / "claim-target"
        try:
            _verify_one_time_reveal_claim(claim_root, consumed)
        except RuntimeError:
            refusal_count += 1
        else:
            failures.append("qualification child accepted a missing reveal claim")
        claim_path = _claim_one_time_reveal(claim_root, consumed)
        if _verify_one_time_reveal_claim(claim_root, consumed) == claim_path:
            refusal_count += 1
        else:
            failures.append("durable one-time reveal claim did not verify")
        try:
            _claim_one_time_reveal(claim_root, consumed)
        except FileExistsError:
            refusal_count += 1
        else:
            failures.append("one-time reveal claim was overwritten or reused")
        evidence_root = temporary_root / "evidence"
        store = QualificationEvidenceStore(evidence_root)
        manifest = store.persist(evidence)
        persistence_run_id = manifest.run_id
        verification = store.verify(manifest.run_id)
        verification_status = "PASS" if verification.passed else "FAIL"
        if not verification.passed:
            failures.extend(verification.failures)
        from kirby2.research.store import RunStore

        research_store = RunStore(evidence_root)
        research_verification = research_store.verify_run(manifest.run_id)
        qualification_artifacts = research_store.query_qualification_artifacts(
            manifest.run_id
        )
        if not research_verification.passed:
            failures.extend(research_verification.failures)
        if len(qualification_artifacts) != len(_ARTIFACT_SPECS):
            failures.append("research catalog omitted typed qualification artifacts")
        sidecar_store = ReviewerSidecarStore(evidence_root)
        sidecar_path = sidecar_store.persist(sidecar)
        if sidecar_store.load(sidecar_path).sha256 == sidecar.sha256:
            refusal_count += 1
        else:
            failures.append("immutable reviewer sidecar did not verify")
        if sidecar_store.persist(sidecar) == sidecar_path:
            refusal_count += 1
        else:
            failures.append("reviewer sidecar exact re-entry changed identity")
        repeated = store.persist(evidence)
        if repeated.run_id == manifest.run_id:
            refusal_count += 1
        else:
            failures.append("exact re-entry did not return stored evidence")
        other_identity = canonical_sha256(
            {"prior": qualification_identity, "probe": "identity-reuse"}
        )
        other_token = OneTimeRevealTokenV1.issue(
            other_identity, implementation_commit
        ).consume()
        other = replace(
            evidence,
            qualification_identity=other_identity,
            reveal_token=other_token,
        )
        try:
            store.persist(other)
        except RuntimeError:
            refusal_count += 1
        else:
            failures.append("used evidence destination accepted another identity")
        tampered_root = temporary_root / "tampered"
        shutil.copytree(evidence_root, tampered_root)
        tampered_manifest = QualificationEvidenceStore(tampered_root).load_manifest()
        qualification_path = (
            tampered_root / "runs" / tampered_manifest.run_id / "qualification.json"
        )
        qualification_path.write_bytes(qualification_path.read_bytes() + b"\n")
        if not QualificationEvidenceStore(tampered_root).verify(
            tampered_manifest.run_id
        ).passed:
            refusal_count += 1
        else:
            failures.append("tampered qualification artifact verified")
        partial_root = temporary_root / "partial"
        partial_root.mkdir()
        (partial_root / "partial.json").write_text("{}", encoding="utf-8")
        try:
            QualificationEvidenceStore(partial_root).persist(evidence)
        except (OSError, RuntimeError, ValueError):
            refusal_count += 1
        else:
            failures.append("partial evidence destination was accepted")
    try:
        replace(
            _development_rows(cases[0], values)[0],
            root_seed=QUALIFICATION_ROOTS[0],
        )
    except ValueError:
        refusal_count += 1
    else:
        failures.append("development row accepted a protected qualification seed")
    return DevelopmentQualificationReportV1(
        fixture_id=fixture_id,
        case_results=tuple(results),
        performance_branches=tuple(
            (name, evaluation.aggregate_status)
            for name, evaluation in performance.items()
        ),
        review_selected_count=selected_count,
        review_shortfall_count=shortfall_count,
        review_not_applicable_count=not_applicable_count,
        persistence_run_id=persistence_run_id,
        verification_status=verification_status,
        refusal_count=refusal_count,
        protected_seed_access="ABSENT",
        failures=tuple(failures),
    )


# These base-flow values are frozen by WO31-I.  Rates use the same integer
# microevents-per-second unit as SimpleFlowConfigurationV1.  The generated work
# is ordinary mechanics work and becomes part of the initialization checkpoint;
# there is no out-of-band price path or replay-time generator.
_BASE_LIMIT_RATE: Final = 500
_BASE_MARKET_RATE: Final = 1_000
_BASE_CANCEL_RATE: Final = 200
_BASE_LIMIT_QUANTITIES: Final = (5, 8, 13)
_BASE_MARKET_QUANTITIES: Final = (1, 2)
_BASE_INITIAL_QUEUE_QUANTITIES: Final = (1_000, 1_200, 1_400)
_BASE_INITIAL_QUEUE_LEVELS: Final = 5
_BASE_MAXIMUM_PLACEMENT_DEPTH_TICKS: Final = 4
_BASE_SCHEDULED_LIVENESS_CHILDREN: Final = 110
_BASE_SCHEDULED_LIVENESS_BATCHES: Final = 11
_QUALIFICATION_ACCOUNT: Final = "WO31_I_QUALIFICATION_FLOW"


def _require_protected_seed_authority(
    root_seed: int, authority: object | None
) -> None:
    if root_seed in DEVELOPMENT_ROOTS:
        raise RuntimeError("WO31-H development roots are unavailable to WO31-I")
    if root_seed not in {*QUALIFICATION_ROOTS, *HOLDOUT_ROOTS}:
        return
    authority_type = globals().get("RealQualificationAuthorityV1")
    if (
        authority_type is None
        or type(authority) is not authority_type
        or authority.mode != "EXECUTE_ONCE"
        or authority.existing_run_id is not None
    ):
        raise RuntimeError(
            "protected qualification/holdout root requires clean committed one-time authority"
        )


@dataclass(frozen=True, slots=True)
class FrozenProfileActionV1:
    simulation_time_us: int
    ordinal: int
    action: str
    request: AdvancedOrderRequest | None = None
    cancel_order_id: str | None = None

    def __post_init__(self) -> None:
        _exact_int(self.simulation_time_us, "profile action time")
        _exact_int(self.ordinal, "profile action ordinal")
        if self.action == "SUBMIT":
            if type(self.request) is not AdvancedOrderRequest or self.cancel_order_id is not None:
                raise ValueError("submit profile action has invalid payload")
        elif self.action == "CANCEL":
            if (
                self.request is not None
                or type(self.cancel_order_id) is not str
                or not self.cancel_order_id
            ):
                raise ValueError("cancel profile action has invalid payload")
        else:
            raise ValueError("profile action is unknown")

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "cancel_order_id": self.cancel_order_id,
            "ordinal": self.ordinal,
            "request": None if self.request is None else self.request.as_dict(),
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class FrozenProfileWorkloadV1:
    candidate_id: str
    root_seed: int
    favored_side: str | None
    event_side: str | None
    actions: tuple[FrozenProfileActionV1, ...]

    def __post_init__(self) -> None:
        if self.candidate_id not in CANDIDATE_IDS:
            raise ValueError("frozen workload candidate is unknown")
        _exact_int(self.root_seed, "frozen workload root")
        if self.favored_side not in {None, "BUY", "SELL"}:
            raise ValueError("frozen workload favored side is invalid")
        if self.event_side not in {None, "BUY", "SELL"}:
            raise ValueError("frozen workload event side is invalid")
        if type(self.actions) is not tuple or any(
            type(row) is not FrozenProfileActionV1 for row in self.actions
        ):
            raise TypeError("frozen workload actions must be typed")
        ordering = tuple(
            (row.simulation_time_us, row.ordinal) for row in self.actions
        )
        if ordering != tuple(sorted(ordering)):
            raise ValueError("frozen workload actions are not canonically ordered")
        if tuple(row.ordinal for row in self.actions) != tuple(range(len(self.actions))):
            raise ValueError("frozen workload action ordinals are not contiguous")
        order_ids = tuple(
            row.request.order_id
            for row in self.actions
            if row.request is not None
        )
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("frozen workload contains duplicate order IDs")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "actions": [row.as_dict() for row in self.actions],
            "base_flow": {
                "cancel_bid_ask_microevents_per_second": _BASE_CANCEL_RATE,
                "initial_queue_levels": _BASE_INITIAL_QUEUE_LEVELS,
                "initial_queue_quantities": list(_BASE_INITIAL_QUEUE_QUANTITIES),
                "limit_buy_sell_microevents_per_second": _BASE_LIMIT_RATE,
                "limit_quantities": list(_BASE_LIMIT_QUANTITIES),
                "market_buy_sell_microevents_per_second": _BASE_MARKET_RATE,
                "market_quantities": list(_BASE_MARKET_QUANTITIES),
                "maximum_placement_depth_ticks": _BASE_MAXIMUM_PLACEMENT_DEPTH_TICKS,
                "scheduled_liveness_children": _BASE_SCHEDULED_LIVENESS_CHILDREN,
                "scheduled_liveness_batches": _BASE_SCHEDULED_LIVENESS_BATCHES,
            },
            "candidate_id": self.candidate_id,
            "event_side": self.event_side,
            "favored_side": self.favored_side,
            "generator_policy_version": FULL_DAY_PROFILE_POLICY_VERSION,
            "root_seed": self.root_seed,
            "schema_version": 1,
        }


def _continuous_bounds(plan: object) -> tuple[int, int]:
    phases = getattr(getattr(plan, "calendar"), "phases")
    rows = tuple(phase for phase in phases if phase.phase_id == "CONTINUOUS")
    if len(rows) != 1:
        raise ValueError("base plan has no unique continuous phase")
    return (
        rows[0].start.simulation_time_us,
        rows[0].end.simulation_time_us,
    )


def _candidate_side(root_seed: int, candidate_id: str, label: str) -> str:
    seed = derive_labeled_seed(root_seed, FULL_DAY_PROFILE_POLICY_VERSION, label)
    return "BUY" if seed & 1 == 0 else "SELL"


def _candidate_interval_bounds(
    candidate: ProfileCandidateV1,
    continuous_start_us: int,
    continuous_end_us: int,
) -> tuple[tuple[object, int, int], ...]:
    duration = continuous_end_us - continuous_start_us
    return tuple(
        (
            interval,
            normalized_boundary_time_us(
                interval.start_ppm, continuous_start_us, duration
            ),
            normalized_boundary_time_us(
                interval.end_ppm, continuous_start_us, duration
            ),
        )
        for interval in candidate.intervals
    )


def _scaled_required_quantity(base: int, multipliers: Sequence[int]) -> int:
    if base == 0:
        return 0
    return max(1, apply_multiplier_chain(base, multipliers))


def _profile_request(
    order_id: str,
    side: str,
    quantity: int,
    instruction: OrderInstruction,
    *,
    price_ticks: int | None = None,
) -> AdvancedOrderRequest:
    return AdvancedOrderRequest(
        order_id=order_id,
        side=Side.BUY if side == "BUY" else Side.SELL,
        quantity=quantity,
        instruction=instruction,
        owner=OrderOwner.SIMULATED,
        account_id=_QUALIFICATION_ACCOUNT,
        price_ticks=price_ticks,
        time_in_force=OrderInstruction.DAY,
    )


def _partition_quantity(total: int, count: int) -> tuple[int, ...]:
    if total <= 0 or count <= 0:
        raise ValueError("scheduled parent quantity and child count must be positive")
    child_count = min(total, count)
    quotient, remainder = divmod(total, child_count)
    return tuple(
        quotient + int(ordinal < remainder) for ordinal in range(child_count)
    )


def _generate_profile_workload(
    candidate: ProfileCandidateV1,
    root_seed: int,
    *,
    continuous_start_us: int,
    continuous_end_us: int,
    authority: object | None = None,
) -> FrozenProfileWorkloadV1:
    """Generate the complete candidate schedule before any market outcome exists."""

    _require_protected_seed_authority(root_seed, authority)

    favored_side = (
        _candidate_side(
            root_seed,
            candidate.candidate_id,
            f"full_day/{TREND_PRESSURE}/favored_side",
        )
        if candidate.candidate_id == TREND_PRESSURE
        else None
    )
    event_side = (
        _candidate_side(
            root_seed,
            candidate.candidate_id,
            f"full_day/{EVENT_SHOCK_PRESSURE}/shock_side",
        )
        if candidate.candidate_id == EVENT_SHOCK_PRESSURE
        else None
    )
    primary_side = favored_side or event_side
    interval_rows = _candidate_interval_bounds(
        candidate, continuous_start_us, continuous_end_us
    )
    draft: list[tuple[int, int, str, AdvancedOrderRequest | None, str | None]] = []
    next_order = 1
    insertion = 0

    def allocate(prefix: str) -> str:
        nonlocal next_order
        value = f"QI-{prefix}-{root_seed:07d}-{next_order:07d}"
        next_order += 1
        return value

    def add_submit(time_us: int, request: AdvancedOrderRequest) -> None:
        nonlocal insertion
        draft.append((time_us, insertion, "SUBMIT", request, None))
        insertion += 1

    def add_cancel(time_us: int, order_id: str) -> None:
        nonlocal insertion
        draft.append((time_us, insertion, "CANCEL", None, order_id))
        insertion += 1

    initial_interval = candidate.intervals[0]
    initial_rng = SeededRng(
        derive_labeled_seed(
            root_seed,
            FULL_DAY_PROFILE_POLICY_VERSION,
            f"full_day/{candidate.candidate_id}/qualification_flow/initial_queue",
        )
    )
    for depth in range(_BASE_INITIAL_QUEUE_LEVELS):
        for side in ("BUY", "SELL"):
            base_quantity = _BASE_INITIAL_QUEUE_QUANTITIES[
                initial_rng.index(len(_BASE_INITIAL_QUEUE_QUANTITIES))
            ]
            quantity = _scaled_required_quantity(
                base_quantity, (initial_interval.liquidity_ppm,)
            )
            price = 9_999 - depth if side == "BUY" else 10_001 + depth
            add_submit(
                continuous_start_us,
                _profile_request(
                    allocate("IQ"),
                    side,
                    quantity,
                    OrderInstruction.LIMIT,
                    price_ticks=price,
                ),
            )

    liveness_rng = SeededRng(
        derive_labeled_seed(
            root_seed,
            FULL_DAY_PROFILE_POLICY_VERSION,
            f"full_day/{candidate.candidate_id}/qualification_flow/scheduled_liveness",
        )
    )
    liveness_first_side = "BUY" if liveness_rng.integer(0, 1) == 0 else "SELL"
    continuous_duration = continuous_end_us - continuous_start_us
    if _BASE_SCHEDULED_LIVENESS_CHILDREN % _BASE_SCHEDULED_LIVENESS_BATCHES:
        raise RuntimeError("scheduled liveness children do not partition exactly")
    liveness_batch_size = (
        _BASE_SCHEDULED_LIVENESS_CHILDREN // _BASE_SCHEDULED_LIVENESS_BATCHES
    )
    for ordinal in range(_BASE_SCHEDULED_LIVENESS_CHILDREN):
        batch_ordinal = ordinal // liveness_batch_size
        at = continuous_start_us + (
            (batch_ordinal + 1) * continuous_duration
        ) // (_BASE_SCHEDULED_LIVENESS_BATCHES + 1)
        normalized = ((at - continuous_start_us) * POLICY_SCALE_PPM) // continuous_duration
        interval = candidate.interval_at(min(normalized, POLICY_SCALE_PPM - 1))
        side = (
            liveness_first_side
            if ordinal % 2 == 0
            else ("SELL" if liveness_first_side == "BUY" else "BUY")
        )
        base_quantity = _BASE_MARKET_QUANTITIES[
            liveness_rng.index(len(_BASE_MARKET_QUANTITIES))
        ]
        quantity = _scaled_required_quantity(
            base_quantity, (interval.volume_ppm,)
        )
        add_submit(
            at,
            _profile_request(
                allocate("LIVE"), side, quantity, OrderInstruction.MARKET
            ),
        )

    limit_orders: dict[str, list[tuple[int, str]]] = {"BUY": [], "SELL": []}
    families = (
        ("limit_buy", "BUY", "LIMIT"),
        ("limit_sell", "SELL", "LIMIT"),
        ("market_buy", "BUY", "MARKET"),
        ("market_sell", "SELL", "MARKET"),
    )
    for family, side, kind in families:
        rng = SeededRng(
            derive_labeled_seed(
                root_seed,
                FULL_DAY_PROFILE_POLICY_VERSION,
                f"full_day/{candidate.candidate_id}/qualification_flow/{family}",
            )
        )
        for interval, start_us, end_us in interval_rows:
            if kind == "LIMIT":
                rate = apply_multiplier_chain(
                    _BASE_LIMIT_RATE,
                    (interval.volume_ppm, interval.liquidity_ppm),
                )
            else:
                aggressive = (
                    interval.aggressive_primary_ppm
                    if interval.aggressive_mode == "SYMMETRIC" or side == primary_side
                    else interval.aggressive_other_ppm
                )
                rate = apply_multiplier_chain(
                    _BASE_MARKET_RATE,
                    (interval.volume_ppm, interval.volatility_ppm, aggressive),
                )
            if rate == 0:
                continue
            cursor = start_us
            while True:
                cursor += rng.exponential_interval_microseconds(rate / 1_000_000.0)
                if cursor >= end_us:
                    break
                if kind == "LIMIT":
                    base_quantity = _BASE_LIMIT_QUANTITIES[
                        rng.index(len(_BASE_LIMIT_QUANTITIES))
                    ]
                    quantity = _scaled_required_quantity(
                        base_quantity,
                        (interval.volume_ppm, interval.liquidity_ppm),
                    )
                    depth = rng.integer(
                        0, _BASE_MAXIMUM_PLACEMENT_DEPTH_TICKS
                    )
                    price = 9_999 - depth if side == "BUY" else 10_001 + depth
                    order_id = allocate("L")
                    request = _profile_request(
                        order_id,
                        side,
                        quantity,
                        OrderInstruction.LIMIT,
                        price_ticks=price,
                    )
                    limit_orders[side].append((cursor, order_id))
                else:
                    base_quantity = _BASE_MARKET_QUANTITIES[
                        rng.index(len(_BASE_MARKET_QUANTITIES))
                    ]
                    quantity = _scaled_required_quantity(
                        base_quantity,
                        (interval.volume_ppm, interval.volatility_ppm),
                    )
                    request = _profile_request(
                        allocate("M"), side, quantity, OrderInstruction.MARKET
                    )
                add_submit(cursor, request)

    for family, side in (("cancel_bid", "BUY"), ("cancel_ask", "SELL")):
        rng = SeededRng(
            derive_labeled_seed(
                root_seed,
                FULL_DAY_PROFILE_POLICY_VERSION,
                f"full_day/{candidate.candidate_id}/qualification_flow/{family}",
            )
        )
        available = list(limit_orders[side])
        used: set[str] = set()
        for interval, start_us, end_us in interval_rows:
            rate = apply_multiplier_chain(
                _BASE_CANCEL_RATE, (interval.volume_ppm, interval.cancel_ppm)
            )
            if rate == 0:
                continue
            cursor = start_us
            while True:
                cursor += rng.exponential_interval_microseconds(rate / 1_000_000.0)
                if cursor >= end_us:
                    break
                eligible = [
                    order_id
                    for order_time, order_id in available
                    if order_time < cursor and order_id not in used
                ]
                if not eligible:
                    continue
                target = eligible[rng.index(len(eligible))]
                used.add(target)
                add_cancel(cursor, target)

    duration = continuous_end_us - continuous_start_us
    if candidate.candidate_id == TREND_PRESSURE:
        start = normalized_boundary_time_us(200_000, continuous_start_us, duration)
        end = normalized_boundary_time_us(800_000, continuous_start_us, duration)
        interval = candidate.interval_at(200_000)
        total = _scaled_required_quantity(30, (interval.volume_ppm,))
        children = _partition_quantity(total, 10)
        for ordinal, quantity in enumerate(children):
            at = start + ((end - start) * ordinal) // len(children)
            add_submit(
                at,
                _profile_request(
                    allocate("META"),
                    favored_side or "BUY",
                    quantity,
                    OrderInstruction.MARKET,
                ),
            )
    if candidate.candidate_id == EVENT_SHOCK_PRESSURE:
        start = normalized_boundary_time_us(450_000, continuous_start_us, duration)
        end = normalized_boundary_time_us(550_000, continuous_start_us, duration)
        interval = candidate.interval_at(450_000)
        total = _scaled_required_quantity(30, (interval.volume_ppm,))
        children = _partition_quantity(total, 10)
        for ordinal, quantity in enumerate(children):
            at = start + ((end - start) * ordinal) // len(children)
            add_submit(
                at,
                _profile_request(
                    allocate("DIST"),
                    event_side or "SELL",
                    quantity,
                    OrderInstruction.MARKET,
                ),
            )
        shock_quantity = _scaled_required_quantity(
            5, (interval.volume_ppm, interval.volatility_ppm)
        )
        add_submit(
            start,
            _profile_request(
                allocate("SHOCK"),
                event_side or "SELL",
                shock_quantity,
                OrderInstruction.MARKET,
            ),
        )

    ordered = sorted(
        draft,
        key=lambda row: (
            row[0],
            0 if row[2] == "SUBMIT" else 1,
            row[1],
        ),
    )
    actions = tuple(
        FrozenProfileActionV1(
            simulation_time_us=row[0],
            ordinal=ordinal,
            action=row[2],
            request=row[3],
            cancel_order_id=row[4],
        )
        for ordinal, row in enumerate(ordered)
    )
    return FrozenProfileWorkloadV1(
        candidate_id=candidate.candidate_id,
        root_seed=root_seed,
        favored_side=favored_side,
        event_side=event_side,
        actions=actions,
    )


def _candidate_pressure_profiles(
    base_plan: object,
    candidate: ProfileCandidateV1,
) -> tuple[PressureProfileV1, ...]:
    continuous_start, continuous_end = _continuous_bounds(base_plan)
    end_us = base_plan.calendar.end_time_us
    interval_rows = _candidate_interval_bounds(
        candidate, continuous_start, continuous_end
    )
    values = {
        PressureKindV1.VOLUME: "volume_ppm",
        PressureKindV1.LIQUIDITY: "liquidity_ppm",
        PressureKindV1.VOLATILITY: "volatility_ppm",
    }
    profiles: list[PressureProfileV1] = []
    for kind in PressureKindV1:
        field = values[kind]
        segments: list[PressureSegmentV1] = []
        if continuous_start:
            segments.append(
                PressureSegmentV1(0, continuous_start, POLICY_SCALE_PPM)
            )
        segments.extend(
            PressureSegmentV1(start_us, end, getattr(interval, field))
            for interval, start_us, end in interval_rows
        )
        if continuous_end < end_us:
            segments.append(
                PressureSegmentV1(continuous_end, end_us, POLICY_SCALE_PPM)
            )
        modifiers = tuple(row.modifier_ppm for row in segments)
        profiles.append(
            PressureProfileV1(
                profile_id=(
                    f"WO31_I_{candidate.candidate_id}_{kind.value}_PRESSURE_V1"
                ),
                profile_version=1,
                pressure_kind=kind,
                minimum_ppm=min(modifiers),
                maximum_ppm=max(modifiers),
                segments=tuple(segments),
            )
        )
    return tuple(profiles)


def _materialize_candidate_plan(
    candidate: ProfileCandidateV1,
    root_seed: int,
    workload: FrozenProfileWorkloadV1,
    *,
    authority: object | None = None,
):
    """Bind the exact F plan, candidate controls, root, schedule, and checkpoints."""

    from kirby2.audit.full_day import _wo31f_plan

    _require_protected_seed_authority(root_seed, authority)
    if (
        type(workload) is not FrozenProfileWorkloadV1
        or workload.candidate_id != candidate.candidate_id
        or workload.root_seed != root_seed
    ):
        raise ValueError("candidate plan and frozen workload identities differ")

    base = _wo31f_plan()
    if base.semantic_sha256 != WO31_F_BASE_PLAN_SHA256:
        raise RuntimeError("committed WO31-F base plan identity changed")
    seed_policy = replace(
        base.seed_policy,
        root_seed=root_seed,
        substreams=tuple(
            replace(
                declaration,
                derived_seed=derive_substream_seed(
                    root_seed,
                    base.seed_policy.policy_version,
                    declaration.semantic_path,
                ),
            )
            for declaration in base.seed_policy.substreams
        ),
    )
    continuous_start, continuous_end = _continuous_bounds(base)
    checkpoint_times = tuple(
        continuous_start + 900_000_000 * ordinal
        for ordinal in range(1, 4)
        if continuous_start + 900_000_000 * ordinal < continuous_end
    )
    checkpoint_policy = replace(
        base.checkpoint_policy,
        explicit_times_us=tuple(sorted({0, *checkpoint_times})),
        interval_us=None,
        at_phase_boundaries=True,
    )
    plan = replace(
        base,
        pressure_profiles=_candidate_pressure_profiles(base, candidate),
        seed_policy=seed_policy,
        checkpoint_policy=checkpoint_policy,
    )
    if plan.seed_policy.root_seed != root_seed:
        raise RuntimeError("candidate plan did not retain its exact supplied root")
    return plan


def _compose_candidate_runtime(plan: object):
    from kirby2.audit.full_day import (
        _wo31e3_delivery_configuration,
        _wo31e3_flow_configuration,
        _wo31e4_research_configuration,
        _wo31f_distribution,
        _wo31f_population,
        _wo31f_specifications,
    )
    from kirby2.full_day.runtime import FullDayRuntime

    _initial, replacements = _wo31f_specifications()
    return FullDayRuntime.compose_with_agent_scheduler(
        plan,
        _wo31f_population(plan),
        shock_quantity_distribution=_wo31f_distribution(plan),
        participant_specifications=replacements,
        simple_flow_configuration=_wo31e3_flow_configuration(),
        delivery_configuration=_wo31e3_delivery_configuration(),
        research_configuration=_wo31e4_research_configuration(),
    )


def _enqueue_profile_workload(runtime: object, workload: FrozenProfileWorkloadV1) -> int:
    """Install ordinary mechanics work into the runtime-owned replay heap."""

    from kirby2.full_day.composition import FULL_DAY_RUNTIME_COMPONENT
    from kirby2.full_day.events import WorkStageV1
    from kirby2.full_day.runtime import (
        _WORK_MECHANICS_BATCH_SUBMIT,
        _WORK_MECHANICS_CANCEL,
        _WORK_MECHANICS_SUBMIT,
    )

    before = len(runtime.pending_work)
    enqueued = 0
    cursor = 0
    while cursor < len(workload.actions):
        action = workload.actions[cursor]
        if action.action == "SUBMIT":
            assert action.request is not None
            grouped = [action]
            lookahead = cursor + 1
            while (
                lookahead < len(workload.actions)
                and workload.actions[lookahead].action == "SUBMIT"
                and workload.actions[lookahead].simulation_time_us
                == action.simulation_time_us
            ):
                grouped.append(workload.actions[lookahead])
                lookahead += 1
            if len(grouped) == 1:
                work_type = _WORK_MECHANICS_SUBMIT
                payload = {"request": action.request.as_dict()}
            else:
                work_type = _WORK_MECHANICS_BATCH_SUBMIT
                payload = {
                    "requests": [
                        row.request.as_dict()
                        for row in grouped
                        if row.request is not None
                    ]
                }
            cursor = lookahead
        else:
            assert action.cancel_order_id is not None
            work_type = _WORK_MECHANICS_CANCEL
            payload = {
                "order_id": action.cancel_order_id,
                "reason": "WO31_I_PROFILE_CANCEL",
            }
            cursor += 1
        runtime._enqueue_new(
            simulation_time_us=action.simulation_time_us,
            microstep=0,
            stage=WorkStageV1.PENDING_VENUE_ARRIVAL,
            source_component_id=FULL_DAY_RUNTIME_COMPONENT,
            work_type=work_type,
            payload=payload,
        )
        enqueued += 1
    runtime.assert_invariants()
    added = len(runtime.pending_work) - before
    if added != enqueued:
        raise RuntimeError("profile workload batch count differs from its pending queue")
    return len(runtime.pending_work)


def _build_candidate_runtime(
    candidate: ProfileCandidateV1,
    root_seed: int,
    *,
    authority: object | None = None,
):
    from kirby2.audit.full_day import _wo31f_plan

    base = _wo31f_plan()
    continuous_start, continuous_end = _continuous_bounds(base)
    workload = _generate_profile_workload(
        candidate,
        root_seed,
        continuous_start_us=continuous_start,
        continuous_end_us=continuous_end,
        authority=authority,
    )
    plan = _materialize_candidate_plan(
        candidate, root_seed, workload, authority=authority
    )
    runtime = _compose_candidate_runtime(plan)
    maximum_initial_pending = _enqueue_profile_workload(runtime, workload)
    return plan, workload, runtime, maximum_initial_pending


def _halt_intervals(
    events: Sequence[MechanicsEvent],
    continuous_start_us: int,
    continuous_end_us: int,
) -> tuple[tuple[int, int], ...]:
    entered: int | None = None
    rows: list[tuple[int, int]] = []
    for event in events:
        if event.event_type is not MechanicsEventType.SESSION_STATE_CHANGED:
            continue
        state = event.data.get("current_state")
        if state == "HALTED" and entered is None:
            entered = max(continuous_start_us, event.simulation_time_us)
        elif state == "CONTINUOUS" and entered is not None:
            end = min(continuous_end_us, event.simulation_time_us)
            if entered < end:
                rows.append((entered, end))
            entered = None
    if entered is not None and entered < continuous_end_us:
        rows.append((entered, continuous_end_us))
    return tuple(rows)


def _subtract_intervals(
    window: tuple[int, int], exclusions: Sequence[tuple[int, int]]
) -> tuple[tuple[int, int], ...]:
    pieces = [window]
    for excluded_start, excluded_end in exclusions:
        next_pieces: list[tuple[int, int]] = []
        for start, end in pieces:
            if excluded_end <= start or end <= excluded_start:
                next_pieces.append((start, end))
                continue
            if start < excluded_start:
                next_pieces.append((start, excluded_start))
            if excluded_end < end:
                next_pieces.append((excluded_end, end))
        pieces = next_pieces
    return tuple((start, end) for start, end in pieces if start < end)


def _intersects_any(
    window: tuple[int, int], intervals: Sequence[tuple[int, int]]
) -> bool:
    return any(max(window[0], start) < min(window[1], end) for start, end in intervals)


def _truth_quote_observations(runtime: object) -> tuple[tuple[int, int | None, int | None, dict[str, object]], ...]:
    delivery = runtime.delivery
    if delivery is None:
        raise RuntimeError("qualification runtime has no observable delivery owner")
    by_source_time: dict[int, tuple[int, dict[str, object]]] = {}
    for raw in delivery.delivered_messages:
        if raw.get("kind") != "MARKET_STATE":
            continue
        source_time = raw.get("source_time_us")
        sequence = raw.get("message_sequence")
        payload = raw.get("client_payload")
        if (
            type(source_time) is not int
            or type(sequence) is not int
            or not isinstance(payload, Mapping)
            or not isinstance(payload.get("market_state"), Mapping)
        ):
            raise RuntimeError("delivered market-state evidence is malformed")
        market = dict(payload["market_state"])
        prior = by_source_time.get(source_time)
        if prior is None or sequence > prior[0]:
            by_source_time[source_time] = (sequence, market)
    rows: list[tuple[int, int | None, int | None, dict[str, object]]] = []
    for source_time in sorted(by_source_time):
        market = by_source_time[source_time][1]
        bid = market.get("best_bid_ticks")
        ask = market.get("best_ask_ticks")
        if bid is not None and type(bid) is not int:
            raise RuntimeError("observable best bid is malformed")
        if ask is not None and type(ask) is not int:
            raise RuntimeError("observable best ask is malformed")
        rows.append((source_time, bid, ask, market))
    return tuple(rows)


def _quote_state_at(
    observations: Sequence[tuple[int, int | None, int | None, dict[str, object]]],
    time_us: int,
) -> tuple[int | None, int | None]:
    bid: int | None = None
    ask: int | None = None
    for observed_time, observed_bid, observed_ask, _feed in observations:
        if observed_time > time_us:
            break
        bid, ask = observed_bid, observed_ask
    return bid, ask


def _quote_segments(
    observations: Sequence[tuple[int, int | None, int | None, dict[str, object]]],
    eligible_pieces: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int, int | None, int | None], ...]:
    output: list[tuple[int, int, int | None, int | None]] = []
    times = tuple(row[0] for row in observations)
    states = {row[0]: (row[1], row[2]) for row in observations}
    for start, end in eligible_pieces:
        boundaries = (start, *(time for time in times if start < time < end), end)
        bid, ask = _quote_state_at(observations, start)
        for left, right in zip(boundaries, boundaries[1:]):
            if left in states and left != start:
                bid, ask = states[left]
            if left < right:
                output.append((left, right, bid, ask))
    return tuple(output)


def _occupancy(
    observations: Sequence[tuple[int, int | None, int | None, dict[str, object]]],
    pieces: Sequence[tuple[int, int]],
) -> tuple[int, int]:
    segments = _quote_segments(observations, pieces)
    occupied = sum(
        end - start
        for start, end, bid, ask in segments
        if bid is not None and ask is not None
    )
    eligible = sum(end - start for start, end in pieces)
    return occupied, eligible


def _spread_values(
    observations: Sequence[tuple[int, int | None, int | None, dict[str, object]]],
    pieces: Sequence[tuple[int, int]],
) -> tuple[TimeWeightedValueV1, ...]:
    output: list[TimeWeightedValueV1] = []
    for start, end, bid, ask in _quote_segments(observations, pieces):
        if bid is not None and ask is not None and ask >= bid:
            output.append(TimeWeightedValueV1(ask - bid, end - start, len(output)))
    return tuple(output)


def _maximum_empty_episode(
    observations: Sequence[tuple[int, int | None, int | None, dict[str, object]]],
    pieces: Sequence[tuple[int, int]],
) -> int:
    maximum = 0
    for piece in pieces:
        current = 0
        for start, end, bid, ask in _quote_segments(observations, (piece,)):
            if bid is None or ask is None:
                current += end - start
                maximum = max(maximum, current)
            else:
                current = 0
    return maximum


def _trade_rows(
    runtime: object,
    pieces: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int, int, str], ...]:
    order_sides = {
        row.request.order_id: row.request.side.value.upper()
        for row in runtime.engine.orders
    }
    rows: list[tuple[int, int, int, str]] = []
    for event in runtime.engine.events:
        if event.event_type is not MechanicsEventType.TRADE or not any(
            start <= event.simulation_time_us < end for start, end in pieces
        ):
            continue
        price = event.data.get("price_ticks")
        quantity = event.data.get("quantity")
        taker = event.data.get("taker_order_id")
        if type(price) is not int or type(quantity) is not int or type(taker) is not str:
            raise RuntimeError("mechanics trade evidence is malformed")
        try:
            side = order_sides[taker]
        except KeyError as error:
            raise RuntimeError("mechanics trade taker has no retained order side") from error
        rows.append((event.simulation_time_us, price, quantity, side))
    return tuple(rows)


def _trade_range(rows: Sequence[tuple[int, int, int, str]]) -> int | None:
    prices = tuple(row[1] for row in rows)
    return None if not prices else max(prices) - min(prices)


def _quote_range(rows: Sequence[TimeWeightedValueV1]) -> int | None:
    values = tuple(row.value for row in rows)
    return None if not values else max(values) - min(values)


def _cancel_count(
    events: Sequence[MechanicsEvent], pieces: Sequence[tuple[int, int]]
) -> int:
    return sum(
        event.event_type is MechanicsEventType.ORDER_CANCELLED
        and any(start <= event.simulation_time_us < end for start, end in pieces)
        for event in events
    )


def _review_samples(runtime: object, plan: object) -> tuple[ObservableSampleV1, ...]:
    delivery = runtime.delivery
    if delivery is None:
        return ()
    by_delivery_time: dict[int, tuple[int, dict[str, object]]] = {}
    for raw in delivery.delivered_messages:
        if raw.get("kind") != "MARKET_STATE":
            continue
        delivery_time = raw.get("delivery_time_us")
        sequence = raw.get("message_sequence")
        payload = raw.get("client_payload")
        if (
            type(delivery_time) is not int
            or delivery_time >= plan.calendar.end_time_us
            or type(sequence) is not int
            or not isinstance(payload, Mapping)
            or not isinstance(payload.get("market_state"), Mapping)
        ):
            continue
        prior = by_delivery_time.get(delivery_time)
        if prior is None or sequence > prior[0]:
            by_delivery_time[delivery_time] = (
                sequence,
                dict(payload["market_state"]),
            )
    output: list[ObservableSampleV1] = []
    for delivery_time in sorted(by_delivery_time):
        phase_start = 0
        for phase in plan.calendar.phases:
            if (
                phase.start.simulation_time_us
                <= delivery_time
                < phase.end.simulation_time_us
            ):
                phase_start = phase.start.simulation_time_us
                break
        output.append(
            ObservableSampleV1(
                simulation_time_us=delivery_time,
                phase_relative_time_us=delivery_time - phase_start,
                observable_feed=by_delivery_time[delivery_time][1],
            )
        )
    return tuple(output)


def _extract_qualification_rows(
    *,
    candidate: ProfileCandidateV1,
    partition: str,
    root_seed: int,
    run_digest: str,
    replay_passed: bool,
    plan: object,
    workload: FrozenProfileWorkloadV1,
    runtime: object,
) -> tuple[QualificationRunMetricsV1, ReviewRunV1]:
    continuous_start, continuous_end = _continuous_bounds(plan)
    duration = continuous_end - continuous_start
    mechanics = runtime.engine.events
    halts = _halt_intervals(mechanics, continuous_start, continuous_end)
    continuous_pieces = _subtract_intervals(
        (continuous_start, continuous_end), halts
    )
    quotes = _truth_quote_observations(runtime)
    occupied, eligible = _occupancy(quotes, continuous_pieces)
    continuous_spreads = _spread_values(quotes, continuous_pieces)
    trades = _trade_rows(runtime, continuous_pieces)
    buy_shares = sum(row[2] for row in trades if row[3] == "BUY")
    sell_shares = sum(row[2] for row in trades if row[3] == "SELL")
    first_price = None if not trades else trades[0][1]
    last_price = None if not trades else trades[-1][1]
    maximum_displacement = (
        None
        if first_price is None
        else max(abs(row[1] - first_price) for row in trades)
    )

    def window(normalized_start: int, normalized_end: int) -> tuple[int, int]:
        return (
            normalized_boundary_time_us(
                normalized_start, continuous_start, duration
            ),
            normalized_boundary_time_us(normalized_end, continuous_start, duration),
        )

    pre_window = window(350_000, 450_000)
    shock_window = window(450_000, 550_000)
    recovery_window = window(550_000, 750_000)
    open_window = window(0, 80_000)
    midday_window = window(350_000, 650_000)
    final_window = window(200_000, POLICY_SCALE_PPM)
    pre_pieces = _subtract_intervals(pre_window, halts)
    shock_pieces = _subtract_intervals(shock_window, halts)
    recovery_pieces = _subtract_intervals(recovery_window, halts)
    open_pieces = _subtract_intervals(open_window, halts)
    midday_pieces = _subtract_intervals(midday_window, halts)
    final_pieces = _subtract_intervals(final_window, halts)
    pre_trades = _trade_rows(runtime, pre_pieces)
    shock_trades = _trade_rows(runtime, shock_pieces)
    recovery_occupied, recovery_eligible = _occupancy(quotes, recovery_pieces)
    final_occupied, final_eligible = _occupancy(quotes, final_pieces)
    maximum_spread = max(
        (row.value for row in continuous_spreads), default=0
    )
    metrics = QualificationRunMetricsV1(
        candidate_id=candidate.candidate_id,
        profile_kind=candidate.candidate_id,
        partition=partition,
        root_seed=root_seed,
        run_digest=run_digest,
        runtime_invariants_passed=True,
        exact_replay_passed=replay_passed,
        safety_abort_count=0,
        trade_count=len(trades),
        continuous_quote_occupied_us=occupied,
        continuous_quote_eligible_us=eligible,
        maximum_nonhalt_empty_side_episode_us=_maximum_empty_episode(
            quotes, continuous_pieces
        ),
        maximum_continuous_spread_ticks=maximum_spread,
        target_price_operations=0,
        forced_trade_operations=0,
        spread_segments=continuous_spreads,
        aggressive_buy_shares=buy_shares,
        aggressive_sell_shares=sell_shares,
        first_trade_ticks=first_price,
        last_trade_ticks=last_price,
        maximum_absolute_trade_displacement_ticks=maximum_displacement,
        favored_side=workload.favored_side,
        pre_aggressive_shares=sum(row[2] for row in pre_trades),
        shock_aggressive_shares=sum(row[2] for row in shock_trades),
        pre_quote_range_ticks=_quote_range(_spread_values(quotes, pre_pieces)),
        shock_quote_range_ticks=_quote_range(_spread_values(quotes, shock_pieces)),
        pre_trade_range_ticks=_trade_range(pre_trades),
        shock_trade_range_ticks=_trade_range(shock_trades),
        event_ratio_halt_affected=(
            _intersects_any(pre_window, halts)
            or _intersects_any(shock_window, halts)
        ),
        shock_spread_segments=_spread_values(quotes, shock_pieces),
        recovery_spread_segments=_spread_values(quotes, recovery_pieces),
        recovery_quote_occupied_us=recovery_occupied,
        recovery_quote_eligible_us=recovery_eligible,
        open_spread_segments=_spread_values(quotes, open_pieces),
        midday_spread_segments=_spread_values(quotes, midday_pieces),
        final_spread_segments=_spread_values(quotes, final_pieces),
        open_cancel_count=_cancel_count(mechanics, open_pieces),
        open_cancel_eligible_us=sum(end - start for start, end in open_pieces),
        midday_cancel_count=_cancel_count(mechanics, midday_pieces),
        midday_cancel_eligible_us=sum(end - start for start, end in midday_pieces),
        final_quote_occupied_us=final_occupied,
        final_quote_eligible_us=final_eligible,
    )
    phase_boundaries = tuple(
        sorted(
            {
                value
                for phase in plan.calendar.phases
                for value in (
                    phase.start.simulation_time_us,
                    phase.end.simulation_time_us,
                )
                if 0 < value < plan.calendar.end_time_us
            }
        )
    )
    event_time = (
        window(450_000, 550_000)[0]
        if candidate.candidate_id == EVENT_SHOCK_PRESSURE
        else None
    )
    review = ReviewRunV1(
        candidate_id=candidate.candidate_id,
        root_seed=root_seed,
        run_digest=run_digest,
        session_start_us=0,
        session_end_us=plan.calendar.end_time_us,
        continuous_start_us=continuous_start,
        continuous_end_us=continuous_end,
        phase_boundaries_us=phase_boundaries,
        halt_intervals_us=halts,
        event_time_us=event_time,
        samples=_review_samples(runtime, plan),
    )
    return metrics, review


def _run_candidate_once(
    *,
    store: object,
    repository: Path,
    authority: object,
    candidate: ProfileCandidateV1,
    partition: str,
    root_seed: int,
) -> tuple[
    QualificationRunMetricsV1,
    ReviewRunV1,
    QualificationRunProofV1,
    dict[str, int | str],
]:
    from kirby2.full_day.store import FullDayStore

    if type(store) is not FullDayStore:
        raise TypeError("qualification execution requires FullDayStore")
    plan, workload, runtime, maximum_initial_pending = _build_candidate_runtime(
        candidate, root_seed, authority=authority
    )
    started = time.perf_counter_ns()
    manifest = store.generate_day(plan, runtime, repository=repository)
    elapsed = time.perf_counter_ns() - started
    verification = store.verify_day(manifest.run_id)
    if not verification.passed:
        raise RuntimeError(
            "full-day artifact verification failed: "
            + "; ".join(verification.failures)
        )
    runtime.assert_invariants()
    metric, review = _extract_qualification_rows(
        candidate=candidate,
        partition=partition,
        root_seed=root_seed,
        run_digest=manifest.result_digest,
        replay_passed=verification.replay_valid,
        plan=plan,
        workload=workload,
        runtime=runtime,
    )
    checkpoint_references = tuple(
        reference
        for reference in manifest.artifacts
        if reference.artifact_type is ArtifactType.FULL_DAY_CHECKPOINT
    )
    if not checkpoint_references or runtime.latest_quiescent_cut is None:
        raise RuntimeError("verified full day omitted its final checkpoint proof")
    proof = QualificationRunProofV1(
        candidate_id=candidate.candidate_id,
        partition=partition,
        root_seed=root_seed,
        run_digest=manifest.result_digest,
        plan_sha256=plan.semantic_sha256,
        workload_sha256=workload.sha256,
        full_day_run_id=manifest.run_id,
        full_day_evidence_digest=manifest.evidence_digest,
        final_checkpoint_sha256=checkpoint_references[-1].sha256,
        event_prefix_sha256=runtime.latest_quiescent_cut.event_prefix_sha256,
        outer_event_count=len(runtime.events),
        replay_verification_status="PASS",
    )
    diagnostics: dict[str, int | str] = {
        "complete_run_bytes": sum(
            path.stat().st_size
            for path in store.run_directory(manifest.run_id).rglob("*")
            if path.is_file()
        ),
        "generation_elapsed_ns": elapsed,
        "largest_checkpoint_bytes": max(
            (store.run_directory(manifest.run_id) / reference.relative_path).stat().st_size
            for reference in checkpoint_references
        ),
        "maximum_pending_item_count": maximum_initial_pending,
        "maximum_timestamp_distinct_microsteps": max(
            (len(values) for values in runtime._microsteps_at_time.values()),
            default=0,
        ),
        "maximum_timestamp_emitted_event_count": max(
            runtime._events_at_time.values(), default=0
        ),
        "outer_event_count": len(runtime.events),
        "result_digest": manifest.result_digest,
        "run_id": manifest.run_id,
    }
    return metric, review, proof, diagnostics


def _aborted_candidate_rows(
    *,
    candidate: ProfileCandidateV1,
    partition: str,
    root_seed: int,
    error: BaseException,
) -> tuple[QualificationRunMetricsV1, ReviewRunV1, QualificationRunProofV1]:
    from kirby2.audit.full_day import _wo31f_plan

    base = _wo31f_plan()
    continuous_start, continuous_end = _continuous_bounds(base)
    abort_code = "RUN_ABORT_" + type(error).__name__.upper()
    identity = {
        "abort_code": abort_code,
        "candidate_id": candidate.candidate_id,
        "partition": partition,
        "root_seed": root_seed,
        "workload": "WO31_I1_ONE_TIME_PROFILE_QUALIFICATION_V1",
    }
    run_digest = canonical_sha256(identity)
    workload_sha256 = canonical_sha256(
        {
            "candidate_id": candidate.candidate_id,
            "generator_policy_version": FULL_DAY_PROFILE_POLICY_VERSION,
            "root_seed": root_seed,
            "status": abort_code,
        }
    )
    duration = continuous_end - continuous_start
    metric = QualificationRunMetricsV1(
        candidate_id=candidate.candidate_id,
        profile_kind=candidate.candidate_id,
        partition=partition,
        root_seed=root_seed,
        run_digest=run_digest,
        runtime_invariants_passed=False,
        exact_replay_passed=False,
        safety_abort_count=1,
        trade_count=0,
        continuous_quote_occupied_us=0,
        continuous_quote_eligible_us=duration,
        maximum_nonhalt_empty_side_episode_us=duration,
        maximum_continuous_spread_ticks=0,
        target_price_operations=0,
        forced_trade_operations=0,
        favored_side=(
            _candidate_side(
                root_seed,
                candidate.candidate_id,
                f"full_day/{TREND_PRESSURE}/favored_side",
            )
            if candidate.candidate_id == TREND_PRESSURE
            else None
        ),
        pre_aggressive_shares=0,
        shock_aggressive_shares=0,
        recovery_quote_occupied_us=0,
        recovery_quote_eligible_us=(duration * 200_000) // POLICY_SCALE_PPM,
        open_cancel_count=0,
        open_cancel_eligible_us=(duration * 80_000) // POLICY_SCALE_PPM,
        midday_cancel_count=0,
        midday_cancel_eligible_us=(duration * 300_000) // POLICY_SCALE_PPM,
        final_quote_occupied_us=0,
        final_quote_eligible_us=(duration * 800_000) // POLICY_SCALE_PPM,
    )
    phase_boundaries = tuple(
        sorted(
            {
                value
                for phase in base.calendar.phases
                for value in (
                    phase.start.simulation_time_us,
                    phase.end.simulation_time_us,
                )
                if 0 < value < base.calendar.end_time_us
            }
        )
    )
    review = ReviewRunV1(
        candidate_id=candidate.candidate_id,
        root_seed=root_seed,
        run_digest=run_digest,
        session_start_us=0,
        session_end_us=base.calendar.end_time_us,
        continuous_start_us=continuous_start,
        continuous_end_us=continuous_end,
        phase_boundaries_us=phase_boundaries,
        halt_intervals_us=(),
        event_time_us=(
            normalized_boundary_time_us(450_000, continuous_start, duration)
            if candidate.candidate_id == EVENT_SHOCK_PRESSURE
            else None
        ),
        samples=(),
    )
    proof = QualificationRunProofV1(
        candidate_id=candidate.candidate_id,
        partition=partition,
        root_seed=root_seed,
        run_digest=run_digest,
        plan_sha256=WO31_F_BASE_PLAN_SHA256,
        workload_sha256=workload_sha256,
        full_day_run_id=None,
        full_day_evidence_digest=None,
        final_checkpoint_sha256=None,
        event_prefix_sha256=None,
        outer_event_count=0,
        replay_verification_status="FAIL",
        abort_code=abort_code,
    )
    return metric, review, proof


def _normalized_peak_rss_bytes() -> int:
    try:
        import resource
    except ImportError:
        return 0
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return value
    if sys.platform.startswith("linux"):
        return value * 1024
    return 0


def _worker_authority(arguments: Sequence[str]):
    if len(arguments) < 3:
        raise ValueError("qualification worker authority arguments are incomplete")
    repository = Path(arguments[0]).resolve(strict=True)
    manifest_path = Path(arguments[1]).resolve(strict=True)
    evidence_root = Path(arguments[2])
    authority = authorize_real_qualification(
        repository=repository,
        manifest_path=manifest_path,
        evidence_root=evidence_root,
    )
    if authority.mode != "EXECUTE_ONCE":
        raise RuntimeError("qualification child cannot execute after evidence activation")
    token = OneTimeRevealTokenV1.issue(
        authority.qualification_identity, authority.implementation_commit
    ).consume()
    _verify_one_time_reveal_claim(evidence_root, token)
    return repository, authority


def _performance_generation_worker() -> int:
    """Fresh-child generation entrypoint; callable only through real authority."""

    from kirby2.full_day.store import FullDayStore

    if len(sys.argv) != 6:
        raise ValueError("performance generation worker arguments differ")
    repository, authority = _worker_authority(sys.argv[1:4])
    store_root = Path(sys.argv[4]).resolve(strict=False)
    ordinal = _exact_int(int(sys.argv[5]), "performance ordinal")
    bundle = load_full_day_profile_bundle()
    candidate = bundle.candidates.candidate(QUIET_RANGE_PRESSURE)
    store = FullDayStore(store_root)
    _metric, _review, proof, diagnostics = _run_candidate_once(
        store=store,
        repository=repository,
        authority=authority,
        candidate=candidate,
        partition="QUALIFICATION",
        root_seed=QUALIFICATION_ROOTS[0],
    )
    payload = {
        **diagnostics,
        "ordinal": ordinal,
        "peak_rss_bytes": _normalized_peak_rss_bytes(),
        "proof_sha256": canonical_sha256(proof.as_dict()),
        "status": "PASS",
    }
    sys.stdout.buffer.write(canonical_json_bytes(payload))
    return 0


def _performance_replay_worker() -> int:
    """Fresh-child exact replay entrypoint for one measured generated artifact."""

    from kirby2.full_day.store import FullDayStore

    if len(sys.argv) != 6:
        raise ValueError("performance replay worker arguments differ")
    _repository, _authority = _worker_authority(sys.argv[1:4])
    store_root = Path(sys.argv[4]).resolve(strict=True)
    run_id = sys.argv[5]
    started = time.perf_counter_ns()
    report = FullDayStore(store_root).verify_day(run_id)
    elapsed = time.perf_counter_ns() - started
    payload = {
        "failures": list(report.failures),
        "peak_rss_bytes": _normalized_peak_rss_bytes(),
        "replay_elapsed_ns": elapsed,
        "run_id": run_id,
        "status": "PASS" if report.passed else "FAIL",
    }
    sys.stdout.buffer.write(canonical_json_bytes(payload))
    return 0 if report.passed else 1


def _invoke_qualification_worker(
    function_name: str,
    *,
    repository: Path,
    manifest_path: Path,
    evidence_root: Path,
    arguments: Sequence[str],
    timeout_seconds: int,
) -> dict[str, object]:
    source = (
        "import sys;from kirby2.full_day.qualification import "
        + function_name
        + ";raise SystemExit("
        + function_name
        + "())"
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            os.fspath(repository),
            os.fspath(manifest_path),
            os.fspath(evidence_root),
            *arguments,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{function_name} failed: {detail}")
    return parse_canonical_json_object(result.stdout)


def _failed_performance_observation(
    ordinal: int, thresholds: Mapping[str, object], reason: str
) -> PerformanceObservationV1:
    hard = thresholds["hard_aborts"]
    if not isinstance(hard, Mapping):
        raise TypeError("performance hard-abort policy is malformed")
    abort_code = (
        "WORKER_"
        + re.sub(r"[^A-Z0-9]+", "_", reason.upper()).strip("_")
    )
    return PerformanceObservationV1(
        artifact_digest=canonical_sha256(
            {
                "failure_reason": reason,
                "measurement_ordinal": ordinal,
                "workload": "FULL_DAY_PERFORMANCE_V1",
            }
        ),
        generation_elapsed_ns=int(hard["generation_elapsed_ns"]) + 1,
        replay_elapsed_ns=int(hard["replay_elapsed_ns"]) + 1,
        outer_event_count=0,
        complete_run_bytes=0,
        largest_checkpoint_bytes=0,
        peak_rss_bytes=0,
        maximum_pending_item_count=0,
        maximum_timestamp_distinct_microsteps=0,
        maximum_timestamp_emitted_event_count=0,
        aborted=True,
        abort_code=abort_code,
    )


def _measure_real_performance(
    *,
    repository: Path,
    manifest_path: Path,
    evidence_root: Path,
    bundle: FullDayProfileBundleV1,
) -> PerformanceEvaluationV1:
    """Run one warmup and three fresh generation/replay child pairs exactly once."""

    governed_parent = evidence_root.parent.resolve(strict=False)
    platform_fingerprint = probe_performance_platform(governed_parent)
    observations: list[PerformanceObservationV1] = []
    warmup_failure: str | None = None
    threshold_payload = bundle.performance.as_dict()
    with tempfile.TemporaryDirectory(
        prefix=".wo31-i-performance-", dir=governed_parent
    ) as temporary:
        root = Path(temporary)
        for ordinal in range(4):
            store_root = root / f"generation-{ordinal}"
            if ordinal != 0 and warmup_failure is not None:
                observations.append(
                    _failed_performance_observation(
                        ordinal, threshold_payload, warmup_failure
                    )
                )
                continue
            try:
                generation = _invoke_qualification_worker(
                    "_performance_generation_worker",
                    repository=repository,
                    manifest_path=manifest_path,
                    evidence_root=evidence_root,
                    arguments=(os.fspath(store_root), str(ordinal)),
                    timeout_seconds=905,
                )
                if generation.get("status") != "PASS":
                    raise RuntimeError("generation child did not report PASS")
                if ordinal == 0:
                    continue
                replay = _invoke_qualification_worker(
                    "_performance_replay_worker",
                    repository=repository,
                    manifest_path=manifest_path,
                    evidence_root=evidence_root,
                    arguments=(
                        os.fspath(store_root),
                        str(generation["run_id"]),
                    ),
                    timeout_seconds=605,
                )
                if replay.get("status") != "PASS":
                    raise RuntimeError("replay child did not report PASS")
                observations.append(
                    PerformanceObservationV1(
                        artifact_digest=canonical_sha256(
                            {
                                "measurement_ordinal": ordinal,
                                "proof_sha256": generation["proof_sha256"],
                                "result_digest": generation["result_digest"],
                                "run_id": generation["run_id"],
                            }
                        ),
                        generation_elapsed_ns=int(
                            generation["generation_elapsed_ns"]
                        ),
                        replay_elapsed_ns=int(replay["replay_elapsed_ns"]),
                        outer_event_count=int(generation["outer_event_count"]),
                        complete_run_bytes=int(generation["complete_run_bytes"]),
                        largest_checkpoint_bytes=int(
                            generation["largest_checkpoint_bytes"]
                        ),
                        peak_rss_bytes=max(
                            int(generation["peak_rss_bytes"]),
                            int(replay["peak_rss_bytes"]),
                        ),
                        maximum_pending_item_count=int(
                            generation["maximum_pending_item_count"]
                        ),
                        maximum_timestamp_distinct_microsteps=int(
                            generation["maximum_timestamp_distinct_microsteps"]
                        ),
                        maximum_timestamp_emitted_event_count=int(
                            generation["maximum_timestamp_emitted_event_count"]
                        ),
                    )
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
                if ordinal == 0:
                    warmup_failure = "WARMUP_" + type(error).__name__
                else:
                    observations.append(
                        _failed_performance_observation(
                            ordinal,
                            threshold_payload,
                            type(error).__name__,
                        )
                    )
    if len(observations) != 3:
        raise RuntimeError("performance workload did not retain exactly three measurements")
    return evaluate_performance(
        observations, platform_fingerprint, bundle.performance
    )


def probe_frozen_profile_workload_development() -> dict[str, object]:
    """Exercise the real execution adapter with one disjoint development-only root."""

    from kirby2.audit.full_day import _wo31f_plan

    root_seed = 9_211_001
    protected = {*DEVELOPMENT_ROOTS, *QUALIFICATION_ROOTS, *HOLDOUT_ROOTS}
    if root_seed in protected:
        raise RuntimeError("execution-adapter probe root overlaps WO31-H")
    candidate = load_full_day_profile_bundle().candidates.candidate(
        QUIET_RANGE_PRESSURE
    )
    continuous_start, continuous_end = _continuous_bounds(_wo31f_plan())
    protected_seed_refusals = 0
    for protected_root in (*QUALIFICATION_ROOTS, *HOLDOUT_ROOTS):
        try:
            _generate_profile_workload(
                candidate,
                protected_root,
                continuous_start_us=continuous_start,
                continuous_end_us=continuous_end,
            )
        except RuntimeError:
            protected_seed_refusals += 1
        else:
            raise RuntimeError("development probe generated a protected root")
    plan, workload, runtime, initial_pending = _build_candidate_runtime(
        candidate, root_seed
    )
    market_actions = tuple(
        row
        for row in workload.actions
        if row.request is not None
        and row.request.instruction is OrderInstruction.MARKET
    )
    if not market_actions:
        raise RuntimeError("development workload produced no ordinary market order")
    runtime.advance_to(market_actions[0].simulation_time_us)
    runtime.assert_invariants()
    trade_count = sum(
        event.event_type is MechanicsEventType.TRADE
        for event in runtime.engine.events
    )
    if trade_count < 1:
        raise RuntimeError("development execution seam produced no ordinary match")
    return {
        "action_count": len(workload.actions),
        "base_plan_sha256": WO31_F_BASE_PLAN_SHA256,
        "candidate_plan_sha256": plan.semantic_sha256,
        "initial_pending_item_count": initial_pending,
        "protected_seed_access": "ABSENT",
        "protected_seed_refusals": protected_seed_refusals,
        "root_seed": root_seed,
        "status": "PASS",
        "trade_count_at_first_market": trade_count,
        "workload_sha256": workload.sha256,
    }


@dataclass(frozen=True, slots=True)
class RealQualificationAuthorityV1:
    mode: str
    implementation_commit: str
    qualification_identity: str
    profile_bundle_sha256: str
    existing_run_id: str | None

    def __post_init__(self) -> None:
        if self.mode not in {"EXECUTE_ONCE", "VERIFY_ONLY"}:
            raise ValueError("real qualification authority mode is invalid")
        _git_oid(self.implementation_commit, "implementation_commit")
        _sha256(self.qualification_identity, "qualification_identity")
        _sha256(self.profile_bundle_sha256, "profile_bundle_sha256")
        if (self.mode == "VERIFY_ONLY") != (self.existing_run_id is not None):
            raise ValueError("verify-only authority must bind an existing run")


def _git_output(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def real_qualification_identity(
    implementation_commit: str, bundle: FullDayProfileBundleV1
) -> str:
    return canonical_sha256(
        {
            "holdout_roots": list(HOLDOUT_ROOTS),
            "implementation_commit": _git_oid(
                implementation_commit, "implementation_commit"
            ),
            "profile_bundle_sha256": bundle.bundle_sha256,
            "qualification_roots": list(QUALIFICATION_ROOTS),
            "review_selection_root_is_separate": True,
            "workload": "WO31_I1_ONE_TIME_PROFILE_QUALIFICATION_V1",
        }
    )


def authorize_real_qualification(
    *,
    repository: Path,
    manifest_path: Path,
    evidence_root: Path,
) -> RealQualificationAuthorityV1:
    """Authorize exact committed WO31-I once, or verify immutable prior evidence."""

    repository = repository.resolve(strict=True)
    evidence_root = Path(os.path.abspath(os.fspath(evidence_root)))
    bundle = load_full_day_profile_bundle()
    if evidence_root.exists() or evidence_root.is_symlink():
        store = QualificationEvidenceStore(evidence_root)
        manifest = store.load_manifest()
        report = store.verify(manifest.run_id)
        if not report.passed:
            raise RuntimeError(
                "existing one-time qualification evidence is invalid: "
                + "; ".join(report.failures)
            )
        ledger = parse_canonical_json_object(
            (store.run_directory(manifest.run_id) / "ledger.json").read_bytes()
        )
        identity = _sha256(
            ledger.get("qualification_identity"), "qualification_identity"
        )
        commit = _git_oid(
            ledger.get("implementation_commit"), "implementation_commit"
        )
        expected = real_qualification_identity(commit, bundle)
        if identity != expected:
            raise RuntimeError("existing evidence has a foreign qualification identity")
        return RealQualificationAuthorityV1(
            mode="VERIFY_ONLY",
            implementation_commit=commit,
            qualification_identity=identity,
            profile_bundle_sha256=bundle.bundle_sha256,
            existing_run_id=manifest.run_id,
        )
    supplied = manifest_path.resolve(strict=True).read_bytes()
    if supplied != bundle.envelopes.canonical_bytes():
        raise RuntimeError("qualification manifest is not the exact WO31-H envelope")
    status = _git_output(repository, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise RuntimeError("real qualification requires an exact clean worktree")
    head = _git_output(repository, "rev-parse", "HEAD").decode("ascii").strip()
    _git_oid(head, "HEAD commit")
    subject = _git_output(repository, "show", "-s", "--format=%s", "HEAD").decode(
        "utf-8"
    ).strip()
    if subject != WO31_I_COMMIT_SUBJECT:
        raise RuntimeError("HEAD is not the committed WO31-I implementation")
    ancestor = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            WO31_H_PREREGISTRATION_COMMIT,
            head,
        ],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("exact WO31-H preregistration commit is not an ancestor")
    manifest_files = (
        "profile_candidates.toml",
        "profile_envelopes.toml",
        "performance_thresholds.toml",
    )
    for filename in manifest_files:
        committed = _git_output(
            repository,
            "show",
            f"{WO31_H_PREREGISTRATION_COMMIT}:kirby2/full_day/{filename}",
        )
        current = (repository / "kirby2" / "full_day" / filename).read_bytes()
        if committed != current:
            raise RuntimeError(f"WO31-H manifest bytes changed after preregistration: {filename}")
    identity = real_qualification_identity(head, bundle)
    return RealQualificationAuthorityV1(
        mode="EXECUTE_ONCE",
        implementation_commit=head,
        qualification_identity=identity,
        profile_bundle_sha256=bundle.bundle_sha256,
        existing_run_id=None,
    )


def verify_qualification_evidence_root(
    evidence_root: Path,
) -> QualificationVerificationReport | None:
    """Generic I1 verifier: absence means NOT_EXERCISED, never regeneration."""

    resolved = Path(os.path.abspath(os.fspath(evidence_root)))
    if not resolved.exists() and not resolved.is_symlink():
        return None
    store = QualificationEvidenceStore(resolved)
    try:
        run_id = store._single_run_id()
    except (OSError, TypeError, ValueError):
        return store.verify()
    return store.verify(run_id)


def qualify_day_profiles_once(
    *,
    repository: Path,
    manifest_path: Path,
    evidence_root: Path,
) -> tuple[str, QualificationVerificationReport]:
    """Public one-time entrypoint; an existing identity is verification-only.

    The protected workload materializer is intentionally reached only after every
    clean-commit and preregistration check.  WO31-I development and audits cannot
    call it because their worktree is necessarily not the committed implementation.
    """

    authority = authorize_real_qualification(
        repository=repository,
        manifest_path=manifest_path,
        evidence_root=evidence_root,
    )
    store = QualificationEvidenceStore(evidence_root)
    if authority.mode == "VERIFY_ONLY":
        assert authority.existing_run_id is not None
        return authority.existing_run_id, store.verify(authority.existing_run_id)
    bundle = _execute_frozen_real_workload(
        authority,
        evidence_root,
        repository=repository.resolve(strict=True),
        manifest_path=manifest_path.resolve(strict=True),
    )
    manifest = store.persist(bundle)
    return manifest.run_id, store.verify(manifest.run_id)


def _execute_frozen_real_workload(
    authority: RealQualificationAuthorityV1,
    evidence_root: Path,
    *,
    repository: Path,
    manifest_path: Path,
) -> QualificationEvidenceBundleV1:
    """Execute all preregistered roots once and reduce only verified artifacts."""

    from kirby2.full_day.store import FullDayStore

    if type(authority) is not RealQualificationAuthorityV1 or authority.mode != "EXECUTE_ONCE":
        raise RuntimeError("frozen workload requires one-time execution authority")
    evidence_root = Path(os.path.abspath(os.fspath(evidence_root)))
    profile_bundle = load_full_day_profile_bundle()
    if profile_bundle.bundle_sha256 != authority.profile_bundle_sha256:
        raise RuntimeError("execution authority profile bundle changed")
    token = OneTimeRevealTokenV1.issue(
        authority.qualification_identity, authority.implementation_commit
    ).consume()
    _claim_one_time_reveal(evidence_root, token)
    _verify_one_time_reveal_claim(evidence_root, token)
    evidence_root.parent.mkdir(parents=True, exist_ok=True)
    metrics: list[QualificationRunMetricsV1] = []
    review_runs: list[ReviewRunV1] = []
    proofs: list[QualificationRunProofV1] = []
    with tempfile.TemporaryDirectory(
        prefix=".wo31-i-qualification-", dir=evidence_root.parent
    ) as temporary:
        store = FullDayStore((Path(temporary) / "full-day-artifacts").resolve())
        for candidate_id in CANDIDATE_IDS:
            candidate = profile_bundle.candidates.candidate(candidate_id)
            for partition, roots in (
                ("QUALIFICATION", QUALIFICATION_ROOTS),
                ("HOLDOUT", HOLDOUT_ROOTS),
            ):
                for root_seed in roots:
                    try:
                        metric, review, proof, _diagnostics = _run_candidate_once(
                            store=store,
                            repository=repository,
                            authority=authority,
                            candidate=candidate,
                            partition=partition,
                            root_seed=root_seed,
                        )
                    except (OSError, TypeError, ValueError, RuntimeError) as error:
                        metric, review, proof = _aborted_candidate_rows(
                            candidate=candidate,
                            partition=partition,
                            root_seed=root_seed,
                            error=error,
                        )
                    metrics.append(metric)
                    review_runs.append(review)
                    proofs.append(proof)
    performance = _measure_real_performance(
        repository=repository,
        manifest_path=manifest_path,
        evidence_root=evidence_root,
        bundle=profile_bundle,
    )
    qualifications = tuple(
        evaluate_candidate_qualification(
            tuple(
                row
                for row in metrics
                if row.candidate_id == candidate_id and row.partition == partition
            ),
            platform_performance_status=performance.aggregate_status,
        )
        for candidate_id in CANDIDATE_IDS
        for partition in REAL_PARTITIONS
    )
    selections = select_review_windows(tuple(review_runs))
    packet = build_blinded_packet(tuple(review_runs), selections)
    return QualificationEvidenceBundleV1(
        qualification_identity=authority.qualification_identity,
        execution_kind="REAL_ONE_TIME",
        implementation_commit=authority.implementation_commit,
        profile_bundle_sha256=authority.profile_bundle_sha256,
        run_metrics=tuple(metrics),
        run_proofs=tuple(proofs),
        qualifications=qualifications,
        review_runs=tuple(review_runs),
        selections=selections,
        review_packet=packet,
        performance=performance,
        reveal_token=token,
        protected_seed_access="QUALIFICATION_AND_HOLDOUT",
        replay_verification_status="PASS",
    )


__all__ = [
    "CandidateQualificationV1",
    "DevelopmentQualificationReportV1",
    "OneTimeRevealTokenV1",
    "QualificationEvidenceBundleV1",
    "QualificationEvidenceStore",
    "QualificationRunMetricsV1",
    "QualificationRunProofV1",
    "QualificationVerificationReport",
    "RealQualificationAuthorityV1",
    "TimeWeightedValueV1",
    "authorize_real_qualification",
    "evaluate_candidate_qualification",
    "load_qualification_development_fixture",
    "probe_frozen_profile_workload_development",
    "qualify_day_profiles_once",
    "real_qualification_identity",
    "run_qualification_development_fixture",
    "verify_qualification_evidence_root",
]
