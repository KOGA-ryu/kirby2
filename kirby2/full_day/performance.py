"""Frozen performance measurement and platform classification for WO31-I."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, Sequence

from .models import canonical_json_bytes, canonical_sha256
from .profiles import (
    PerformancePlatformFingerprintV1,
    PerformanceThresholdsManifestV1,
    aggregate_performance_status,
    median,
    round_div_even,
)


PERFORMANCE_EVIDENCE_SCHEMA_VERSION: Final = 1
MEASURED_GENERATION_COUNT: Final = 3
_METRIC_ORDER: Final = (
    "complete_run_bytes",
    "generation_p50_elapsed_ns",
    "generation_throughput_events_per_second",
    "largest_checkpoint_bytes",
    "peak_rss_bytes",
    "replay_p50_elapsed_ns",
)
_HARD_ABORT_FIELDS: Final = (
    "complete_staged_run_bytes",
    "generation_elapsed_ns",
    "maximum_canonical_checkpoint_bytes",
    "outer_event_count",
    "peak_rss_bytes",
    "pending_item_count",
    "replay_elapsed_ns",
    "timestamp_distinct_microsteps",
    "timestamp_emitted_event_count",
)


def _nonnegative(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


@dataclass(frozen=True, slots=True)
class PerformanceObservationV1:
    """Raw operational measurements for one completed or aborted artifact."""

    artifact_digest: str
    generation_elapsed_ns: int
    replay_elapsed_ns: int
    outer_event_count: int
    complete_run_bytes: int
    largest_checkpoint_bytes: int
    peak_rss_bytes: int
    maximum_pending_item_count: int
    maximum_timestamp_distinct_microsteps: int
    maximum_timestamp_emitted_event_count: int
    aborted: bool = False
    abort_code: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.artifact_digest) is not str
            or len(self.artifact_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.artifact_digest)
        ):
            raise ValueError("performance artifact digest must be a SHA-256")
        for field in (
            "generation_elapsed_ns",
            "replay_elapsed_ns",
            "outer_event_count",
            "complete_run_bytes",
            "largest_checkpoint_bytes",
            "peak_rss_bytes",
            "maximum_pending_item_count",
            "maximum_timestamp_distinct_microsteps",
            "maximum_timestamp_emitted_event_count",
        ):
            _nonnegative(getattr(self, field), field)
        if type(self.aborted) is not bool:
            raise TypeError("performance aborted flag must be a bool")
        if self.aborted:
            if (
                type(self.abort_code) is not str
                or not self.abort_code
                or any(
                    character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                    for character in self.abort_code
                )
            ):
                raise ValueError("aborted performance observation requires an abort code")
        elif self.abort_code is not None:
            raise ValueError("completed performance observation cannot carry an abort code")

    def as_dict(self) -> dict[str, object]:
        return {
            "abort_code": self.abort_code,
            "aborted": self.aborted,
            "artifact_digest": self.artifact_digest,
            "complete_run_bytes": self.complete_run_bytes,
            "generation_elapsed_ns": self.generation_elapsed_ns,
            "largest_checkpoint_bytes": self.largest_checkpoint_bytes,
            "maximum_pending_item_count": self.maximum_pending_item_count,
            "maximum_timestamp_distinct_microsteps": self.maximum_timestamp_distinct_microsteps,
            "maximum_timestamp_emitted_event_count": self.maximum_timestamp_emitted_event_count,
            "outer_event_count": self.outer_event_count,
            "peak_rss_bytes": self.peak_rss_bytes,
            "replay_elapsed_ns": self.replay_elapsed_ns,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PerformanceObservationV1:
        expected = {
            "abort_code",
            "aborted",
            "artifact_digest",
            "complete_run_bytes",
            "generation_elapsed_ns",
            "largest_checkpoint_bytes",
            "maximum_pending_item_count",
            "maximum_timestamp_distinct_microsteps",
            "maximum_timestamp_emitted_event_count",
            "outer_event_count",
            "peak_rss_bytes",
            "replay_elapsed_ns",
        }
        if set(payload) != expected:
            raise ValueError("performance observation fields differ")
        return cls(**{name: payload[name] for name in expected})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PerformanceEvaluationV1:
    platform: PerformancePlatformFingerprintV1
    platform_status: str
    raw_observations: tuple[PerformanceObservationV1, ...]
    metrics: tuple[tuple[str, int], ...]
    metric_statuses: tuple[tuple[str, str], ...]
    aggregate_status: str
    abort_reasons: tuple[str, ...]
    schema_version: int = PERFORMANCE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PERFORMANCE_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("performance evidence schema version must be 1")
        if self.platform_status not in {"ELIGIBLE", "UNSUPPORTED"}:
            raise ValueError("performance platform status is invalid")
        if self.metrics != tuple(sorted(self.metrics, key=lambda row: _METRIC_ORDER.index(row[0]))):
            raise ValueError("performance metrics differ from canonical order")
        if tuple(name for name, _value in self.metrics) != _METRIC_ORDER:
            raise ValueError("performance metric inventory differs from policy")
        if tuple(name for name, _status in self.metric_statuses) != _METRIC_ORDER:
            raise ValueError("performance status inventory differs from policy")
        allowed = {"PASS", "WARNING", "FAIL", "UNSUPPORTED", "NOT_RUN"}
        if any(status not in allowed for _name, status in self.metric_statuses):
            raise ValueError("performance metric status is unknown")
        if self.aggregate_status not in allowed:
            raise ValueError("performance aggregate status is unknown")
        if self.platform_status == "UNSUPPORTED" and (
            self.aggregate_status != "UNSUPPORTED"
            or any(status != "UNSUPPORTED" for _name, status in self.metric_statuses)
        ):
            raise ValueError("ineligible platform cannot receive threshold PASS")
        if self.abort_reasons and self.aggregate_status not in {"FAIL", "UNSUPPORTED"}:
            raise ValueError("hard abort cannot produce a passing performance status")

    def as_dict(self) -> dict[str, object]:
        return {
            "abort_reasons": list(self.abort_reasons),
            "aggregate_status": self.aggregate_status,
            "metric_statuses": {name: status for name, status in self.metric_statuses},
            "metrics": {name: value for name, value in self.metrics},
            "operational_measurements_in_semantic_identity": False,
            "platform": self.platform.as_dict(),
            "platform_status": self.platform_status,
            "raw_observations": [row.as_dict() for row in self.raw_observations],
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> PerformanceEvaluationV1:
        expected = {
            "abort_reasons",
            "aggregate_status",
            "metric_statuses",
            "metrics",
            "operational_measurements_in_semantic_identity",
            "platform",
            "platform_status",
            "raw_observations",
            "schema_version",
        }
        if set(payload) != expected:
            raise ValueError("performance evidence fields differ")
        if payload["operational_measurements_in_semantic_identity"] is not False:
            raise ValueError("operational performance data entered semantic identity")
        raw_platform = payload["platform"]
        observations = payload["raw_observations"]
        metrics = payload["metrics"]
        statuses = payload["metric_statuses"]
        reasons = payload["abort_reasons"]
        if not isinstance(raw_platform, Mapping):
            raise TypeError("performance platform must be an object")
        if type(observations) is not list or any(
            not isinstance(row, Mapping) for row in observations
        ):
            raise TypeError("performance observations must be an array")
        if not isinstance(metrics, Mapping) or not isinstance(statuses, Mapping):
            raise TypeError("performance metrics/statuses must be objects")
        if type(reasons) is not list or any(type(row) is not str for row in reasons):
            raise TypeError("performance abort reasons must be strings")
        expected_platform = {
            "FREE_GOVERNED_STORE_BYTES_BEFORE_WARMUP",
            "LOGICAL_CPU_COUNT",
            "MACHINE",
            "PHYSICAL_MEMORY_BYTES",
            "PYTHON_IMPLEMENTATION",
            "PYTHON_MAJOR_MINOR_PATCH",
            "PYTHON_RUNTIME",
            "RU_MAXRSS_NORMALIZATION_RULE",
            "SYSTEM",
        }
        if set(raw_platform) != expected_platform:
            raise ValueError("performance platform field inventory differs")
        version = raw_platform["PYTHON_MAJOR_MINOR_PATCH"]
        if type(version) is not list or len(version) != 3:
            raise ValueError("performance Python version must have three integers")
        platform_row = PerformancePlatformFingerprintV1(
            system=str(raw_platform["SYSTEM"]),
            machine=str(raw_platform["MACHINE"]),
            python_implementation=str(raw_platform["PYTHON_IMPLEMENTATION"]),
            python_major=_nonnegative(version[0], "python_major"),
            python_minor=_nonnegative(version[1], "python_minor"),
            python_patch=_nonnegative(version[2], "python_patch"),
            python_runtime=str(raw_platform["PYTHON_RUNTIME"]),
            logical_cpu_count=_nonnegative(
                raw_platform["LOGICAL_CPU_COUNT"], "logical_cpu_count"
            ),
            physical_memory_bytes=_nonnegative(
                raw_platform["PHYSICAL_MEMORY_BYTES"], "physical_memory_bytes"
            ),
            free_governed_store_bytes_before_warmup=_nonnegative(
                raw_platform["FREE_GOVERNED_STORE_BYTES_BEFORE_WARMUP"],
                "free_governed_store_bytes_before_warmup",
            ),
            ru_maxrss_normalization_rule=str(
                raw_platform["RU_MAXRSS_NORMALIZATION_RULE"]
            ),
        )
        return cls(
            platform=platform_row,
            platform_status=str(payload["platform_status"]),
            raw_observations=tuple(
                PerformanceObservationV1.from_dict(row) for row in observations
            ),
            metrics=tuple(
                (name, _nonnegative(metrics[name], name)) for name in _METRIC_ORDER
            ),
            metric_statuses=tuple(
                (name, str(statuses[name])) for name in _METRIC_ORDER
            ),
            aggregate_status=str(payload["aggregate_status"]),
            abort_reasons=tuple(reasons),
            schema_version=_nonnegative(payload["schema_version"], "schema_version"),
        )


def probe_performance_platform(governed_store: Path) -> PerformancePlatformFingerprintV1:
    """Record the exact host fields required by the frozen platform predicate."""

    root = governed_store.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    logical = os.cpu_count() or 0
    physical_memory = 0
    try:
        physical_memory = int(os.sysconf("SC_PHYS_PAGES")) * int(
            os.sysconf("SC_PAGE_SIZE")
        )
    except (OSError, ValueError):
        physical_memory = 0
    system = platform.system()
    return PerformancePlatformFingerprintV1(
        system=system,
        machine=platform.machine(),
        python_implementation=platform.python_implementation(),
        python_major=sys.version_info.major,
        python_minor=sys.version_info.minor,
        python_patch=sys.version_info.micro,
        python_runtime=platform.python_version(),
        logical_cpu_count=logical,
        physical_memory_bytes=physical_memory,
        free_governed_store_bytes_before_warmup=shutil.disk_usage(root).free,
        ru_maxrss_normalization_rule=(
            "DARWIN_RU_MAXRSS_VALUE_IS_BYTES"
            if system == "Darwin"
            else (
                "LINUX_RU_MAXRSS_KIB_TIMES_1024"
                if system == "Linux"
                else "UNSUPPORTED_PLATFORM_RU_MAXRSS_UNIT"
            )
        ),
    )


def _abort_reasons(
    observations: Sequence[PerformanceObservationV1],
    policy: Mapping[str, object],
) -> tuple[str, ...]:
    raw = policy.get("hard_aborts")
    if not isinstance(raw, Mapping) or set(raw) != {
        *_HARD_ABORT_FIELDS,
        "abort_outcome",
        "deterministic_operation_trigger",
        "operational_abort_data_in_semantic_identity",
        "operational_measurement_trigger",
    }:
        raise ValueError("performance hard-abort policy inventory differs")
    reasons: list[str] = []
    for ordinal, row in enumerate(observations):
        checks = (
            ("complete_staged_run_bytes", row.complete_run_bytes),
            ("generation_elapsed_ns", row.generation_elapsed_ns),
            ("maximum_canonical_checkpoint_bytes", row.largest_checkpoint_bytes),
            ("outer_event_count", row.outer_event_count),
            ("peak_rss_bytes", row.peak_rss_bytes),
            ("pending_item_count", row.maximum_pending_item_count),
            ("replay_elapsed_ns", row.replay_elapsed_ns),
            (
                "timestamp_distinct_microsteps",
                row.maximum_timestamp_distinct_microsteps,
            ),
            (
                "timestamp_emitted_event_count",
                row.maximum_timestamp_emitted_event_count,
            ),
        )
        if row.aborted:
            reasons.append(f"observation[{ordinal}]:{row.abort_code}")
        for field, value in checks:
            limit = raw[field]
            if type(limit) is not int:
                raise TypeError("hard-abort thresholds must be exact integers")
            if value > limit:
                reasons.append(f"observation[{ordinal}]:{field.upper()}")
    return tuple(reasons)


def evaluate_performance(
    observations: Sequence[PerformanceObservationV1],
    platform_fingerprint: PerformancePlatformFingerprintV1,
    thresholds: PerformanceThresholdsManifestV1,
) -> PerformanceEvaluationV1:
    """Aggregate the exact frozen workload; unsupported hosts remain unsupported."""

    rows = tuple(observations)
    if len(rows) != MEASURED_GENERATION_COUNT:
        raise ValueError("performance workload requires exactly three measured artifacts")
    if len({row.artifact_digest for row in rows}) != len(rows):
        raise ValueError("performance workload cannot reuse a measured artifact")
    total_generation_ns = sum(row.generation_elapsed_ns for row in rows)
    throughput = (
        0
        if total_generation_ns == 0
        else round_div_even(
            sum(row.outer_event_count for row in rows) * 1_000_000_000,
            total_generation_ns,
        )
    )
    metrics = (
        ("complete_run_bytes", max(row.complete_run_bytes for row in rows)),
        (
            "generation_p50_elapsed_ns",
            median([row.generation_elapsed_ns for row in rows]),
        ),
        ("generation_throughput_events_per_second", throughput),
        (
            "largest_checkpoint_bytes",
            max(row.largest_checkpoint_bytes for row in rows),
        ),
        ("peak_rss_bytes", max(row.peak_rss_bytes for row in rows)),
        (
            "replay_p50_elapsed_ns",
            median([row.replay_elapsed_ns for row in rows]),
        ),
    )
    reasons = _abort_reasons(rows, thresholds.as_dict())
    if not platform_fingerprint.threshold_eligible:
        statuses = tuple((name, "UNSUPPORTED") for name, _value in metrics)
        aggregate = "UNSUPPORTED"
        platform_status = "UNSUPPORTED"
    else:
        statuses = tuple(
            (name, thresholds.classify(name, value)) for name, value in metrics
        )
        aggregate = aggregate_performance_status([status for _name, status in statuses])
        if reasons:
            aggregate = "FAIL"
        platform_status = "ELIGIBLE"
    return PerformanceEvaluationV1(
        platform=platform_fingerprint,
        platform_status=platform_status,
        raw_observations=rows,
        metrics=metrics,
        metric_statuses=statuses,
        aggregate_status=aggregate,
        abort_reasons=reasons,
    )


__all__ = [
    "MEASURED_GENERATION_COUNT",
    "PerformanceEvaluationV1",
    "PerformanceObservationV1",
    "evaluate_performance",
    "probe_performance_platform",
]
