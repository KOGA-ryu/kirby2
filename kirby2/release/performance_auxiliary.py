"""Closed installed-artifact runners for the five WO40-I auxiliary workloads.

The module accepts only one canonical, digest-bound execution envelope and writes
one immutable result/evidence directory.  Every workload is local and deterministic:
there is no socket, live-market, brokerage, account, credential, or discovery path.
Operational timing and RSS observations are deliberately outside semantic run
identity, but are retained as exact integer evidence by the release result contract.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import multiprocessing
import os
import platform
import pty
import re
import resource
import stat
import struct
import sys
import threading
import time
import tty
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from kirby2.packs.formats import (
    canonical_json_bytes,
    load_canonical_json_bytes,
    require_nfc_text,
    require_sha256,
)

from .performance import (
    RELEASE_AUXILIARY_THRESHOLDS_V1,
    ReleaseAuxiliaryPerformanceResultV1,
    ReleaseAuxiliaryPerformanceTemplateV1,
    RunnerSourceTreeV1,
    nearest_rank,
    round_div_even,
)


AUXILIARY_EXECUTION_POLICY_V1: Final[str] = (
    "KIRBY2_RELEASE_AUXILIARY_EXECUTION_V1"
)

_RESULT_NAME: Final[str] = "result.json"
_EVIDENCE_DIRECTORY_NAME: Final[str] = "evidence"
_WORKSPACE_DIRECTORY_NAME: Final[str] = "workspace"
_TERMINAL_COLUMNS: Final[int] = 120
_TERMINAL_ROWS: Final[int] = 40
_TERMINAL_ENCODING: Final[str] = "UTF-8"
_TERMINAL_TERM: Final[str] = "dumb"
_FRESH_PROCESS_POLICY_V1: Final[str] = "SPAWNED_CPYTHON_PROCESS_V1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_RUN_ID = re.compile(r"run-[0-9a-f]{24}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

_WORKLOAD_IDS: Final[tuple[str, ...]] = (
    "RELEASE_INTERACTIVE_ACK_V1",
    "RELEASE_TERMINAL_UPDATE_V1",
    "RELEASE_FULL_DAY_GENERATION_V1",
    "RELEASE_FULL_DAY_REPLAY_V1",
    "RELEASE_MICROSCOPE_LOAD_V1",
)

_TEMPLATE_PANE_IDS: Final[tuple[str, ...]] = (
    "LEVEL2_LADDER",
    "TIME_AND_SALES",
    "DEPTH_HEATMAP",
    "INDIVIDUAL_QUEUE",
    "PLAYER_ORDERS",
    "ORDER_LIFECYCLE",
    "POSITION",
    "TRAFFIC_LIGHT",
    "STRATEGY_EVIDENCE",
    "FEATURE_PROVENANCE",
    "AGENT_ACTIVITY",
    "LATENCY_TIMELINE",
    "VENUE_QUOTES",
    "CONSOLIDATED_QUOTES",
    "FILLS",
    "EXECUTION_METRICS",
    "MECHANISTIC_TRACE",
    "COUNTERFACTUAL_COMPARISON",
)

_RUNTIME_PANE_IDS: Final[tuple[str, ...]] = (
    "LEVEL_2_LADDER",
    "TIME_AND_SALES",
    "DEPTH_HEATMAP",
    "INDIVIDUAL_QUEUE",
    "PLAYER_ORDERS",
    "ORDER_STATE_LIFECYCLE",
    "POSITION",
    "TRAFFIC_LIGHT",
    "STRATEGY_RULE_EVIDENCE",
    "FEATURE_PROVENANCE",
    "AGENT_ACTIVITY",
    "LATENCY_TIMELINE",
    "VENUE_QUOTES",
    "CONSOLIDATED_QUOTES",
    "FILLS",
    "EXECUTION_METRICS",
    "MECHANISTIC_TRACE",
    "COUNTERFACTUAL_COMPARISON",
)

_TEMPLATE_TO_RUNTIME_PANE_ID: Final[Mapping[str, str]] = dict(
    zip(_TEMPLATE_PANE_IDS, _RUNTIME_PANE_IDS, strict=True)
)

_REDUCTION_ORDER: Final[Mapping[str, tuple[str, ...]]] = {
    "RELEASE_INTERACTIVE_ACK_V1": (
        "ACK_LATENCY_P95",
        "ACK_LATENCY_P99",
        "ACK_PEAK_RSS_MAX",
    ),
    "RELEASE_TERMINAL_UPDATE_V1": (
        "TERMINAL_UPDATE_P99",
        "TERMINAL_UPDATE_MAX",
        "TERMINAL_PEAK_RSS_MAX",
    ),
    "RELEASE_FULL_DAY_GENERATION_V1": (
        "GENERATION_WALL_P50",
        "GENERATION_PEAK_RSS_MAX",
        "LARGEST_CHECKPOINT_MAX",
        "FULL_DAY_BYTES_MAX",
        "LEDGER_GROWTH_MAX",
        "CHECKPOINT_GROWTH_MAX",
    ),
    "RELEASE_FULL_DAY_REPLAY_V1": (
        "REPLAY_WALL_P50",
        "REPLAY_PEAK_RSS_MAX",
    ),
    "RELEASE_MICROSCOPE_LOAD_V1": (
        "MICROSCOPE_WALL_P95",
        "MICROSCOPE_PEAK_RSS_MAX",
    ),
}

_EXPECTED_COUNTS: Final[
    Mapping[str, tuple[Mapping[str, int], Mapping[str, int]]]
] = {
    "RELEASE_INTERACTIVE_ACK_V1": (
        {"ack_latency_ns": 100},
        {"ack_latency_ns": 1000, "peak_rss_bytes": 1},
    ),
    "RELEASE_TERMINAL_UPDATE_V1": (
        {"update_latency_ns": 100},
        {"update_latency_ns": 5000, "peak_rss_bytes": 1},
    ),
    "RELEASE_FULL_DAY_GENERATION_V1": (
        {"peak_rss_bytes": 1, "wall_time_ns": 1},
        {
            "checkpoint_growth_bytes_per_simulation_hour": 3,
            "full_day_bytes": 3,
            "largest_checkpoint_bytes": 3,
            "ledger_growth_bytes_per_1000_events": 3,
            "peak_rss_bytes": 3,
            "wall_time_ns": 3,
        },
    ),
    "RELEASE_FULL_DAY_REPLAY_V1": (
        {"peak_rss_bytes": 1, "wall_time_ns": 1},
        {"peak_rss_bytes": 3, "wall_time_ns": 3},
    ),
    "RELEASE_MICROSCOPE_LOAD_V1": (
        {"peak_rss_bytes": 1, "wall_time_ns": 1},
        {"peak_rss_bytes": 20, "wall_time_ns": 20},
    ),
}

_METRIC_UNITS: Final[Mapping[str, str]] = {
    "ack_latency_ns": "NANOSECONDS",
    "checkpoint_growth_bytes_per_simulation_hour": (
        "BYTES_PER_SIMULATION_HOUR"
    ),
    "full_day_bytes": "BYTES",
    "largest_checkpoint_bytes": "BYTES",
    "ledger_growth_bytes_per_1000_events": "BYTES_PER_1000_EVENTS",
    "peak_rss_bytes": "BYTES",
    "update_latency_ns": "NANOSECONDS",
    "wall_time_ns": "NANOSECONDS",
}

_MICROSCOPE_ASSET_NAMES: Final[tuple[str, ...]] = (
    "report.css",
    "report.html",
    "report.js",
)

_THRESHOLD_BY_ID: Final[Mapping[str, dict[str, object]]] = {
    reduction_id: {
        "hard_failure": hard_failure,
        "metric_id": metric_id,
        "pass_upper_inclusive": pass_upper_inclusive,
        "reduction_id": reduction_id,
        "statistic": statistic,
        "warning_upper_inclusive": warning_upper_inclusive,
    }
    for (
        reduction_id,
        metric_id,
        statistic,
        pass_upper_inclusive,
        warning_upper_inclusive,
        hard_failure,
    ) in RELEASE_AUXILIARY_THRESHOLDS_V1
}

_WORKLOAD_FAILURE_CODES: Final[Mapping[str, str]] = {
    "RELEASE_INTERACTIVE_ACK_V1": "INTERACTIVE_ACK_WORKLOAD_INVARIANT_FAILED",
    "RELEASE_TERMINAL_UPDATE_V1": "TERMINAL_UPDATE_WORKLOAD_INVARIANT_FAILED",
    "RELEASE_FULL_DAY_GENERATION_V1": (
        "FULL_DAY_GENERATION_WORKLOAD_INVARIANT_FAILED"
    ),
    "RELEASE_FULL_DAY_REPLAY_V1": "FULL_DAY_REPLAY_WORKLOAD_INVARIANT_FAILED",
    "RELEASE_MICROSCOPE_LOAD_V1": "MICROSCOPE_LOAD_WORKLOAD_INVARIANT_FAILED",
}


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} fields differ from the V1 schema")
    return value


def _text(value: object, label: str, maximum_bytes: int = 4096) -> str:
    return require_nfc_text(value, label, maximum_bytes=maximum_bytes)


def _nonnegative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _absolute_path(value: object, label: str) -> Path:
    selected = Path(_text(value, label, 4096))
    if not selected.is_absolute():
        raise ValueError(f"{label} must be absolute")
    resolved = selected.resolve(strict=False)
    if selected != resolved or resolved == Path(resolved.anchor):
        raise ValueError(f"{label} must be resolved and cannot be a filesystem root")
    return resolved


def _source_path(value: object, label: str) -> str:
    selected = _text(value, label, 1024)
    parts = PurePosixPath(selected)
    if (
        parts.is_absolute()
        or "\\" in selected
        or not parts.parts
        or any(part in {"", ".", ".."} for part in parts.parts)
    ):
        raise ValueError(f"{label} must be a normalized relative path")
    return parts.as_posix()


def _evidence_id(value: object) -> str:
    selected = _source_path(value, "auxiliary evidence ID")
    if len(selected.encode("utf-8")) > 256:
        raise ValueError("auxiliary evidence ID is too long")
    return selected


def _canonical_copy(value: object) -> object:
    return load_canonical_json_bytes(canonical_json_bytes(value), "canonical copy")


@dataclass(frozen=True, slots=True)
class ReleaseAuxiliarySourceRunV1:
    artifact_id: str
    ordinal: int
    store_root: str
    run_id: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        artifact_id = _text(self.artifact_id, "auxiliary source artifact ID", 128)
        if _IDENTIFIER.fullmatch(artifact_id) is None:
            raise ValueError("auxiliary source artifact ID is invalid")
        _nonnegative_integer(self.ordinal, "auxiliary source ordinal")
        root = _absolute_path(self.store_root, "auxiliary source store root")
        if _RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("auxiliary source run ID is invalid")
        require_sha256(self.manifest_sha256, "auxiliary source manifest digest")
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "store_root", os.fspath(root))

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "manifest_sha256": self.manifest_sha256,
            "ordinal": self.ordinal,
            "run_id": self.run_id,
            "store_root": self.store_root,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseAuxiliarySourceRunV1":
        row = _exact_object(
            value,
            {"artifact_id", "manifest_sha256", "ordinal", "run_id", "store_root"},
            "auxiliary source run",
        )
        return cls(
            artifact_id=_text(row["artifact_id"], "auxiliary source artifact ID", 128),
            ordinal=_nonnegative_integer(row["ordinal"], "auxiliary source ordinal"),
            store_root=os.fspath(
                _absolute_path(row["store_root"], "auxiliary source store root")
            ),
            run_id=_text(row["run_id"], "auxiliary source run ID", 64),
            manifest_sha256=_text(
                row["manifest_sha256"], "auxiliary source manifest digest", 64
            ),
        )


def _template_from_dict(value: object) -> ReleaseAuxiliaryPerformanceTemplateV1:
    row = _exact_object(
        value,
        {
            "artifact_selector",
            "entrypoint_paths",
            "input_identity",
            "runner_source_policy_id",
            "sample_contract_id",
            "workload_id",
        },
        "auxiliary template",
    )
    entrypoints = row["entrypoint_paths"]
    if type(entrypoints) is not list:
        raise TypeError("auxiliary template entrypoints must be an array")
    input_identity = row["input_identity"]
    if type(input_identity) is not dict:
        raise TypeError("auxiliary template input identity must be an object")
    copied_identity = _canonical_copy(input_identity)
    assert type(copied_identity) is dict
    return ReleaseAuxiliaryPerformanceTemplateV1(
        workload_id=_text(row["workload_id"], "auxiliary workload ID", 128),
        entrypoint_paths=tuple(
            _source_path(item, "auxiliary entrypoint") for item in entrypoints
        ),
        artifact_selector=_text(
            row["artifact_selector"], "auxiliary artifact selector", 128
        ),
        input_identity=copied_identity,
        sample_contract_id=_text(
            row["sample_contract_id"], "auxiliary sample contract ID", 128
        ),
        runner_source_policy_id=_text(
            row["runner_source_policy_id"], "auxiliary runner source policy", 128
        ),
    )


@dataclass(frozen=True, slots=True)
class ReleaseAuxiliaryExecutionV1:
    """Canonical installed-run envelope for exactly one auxiliary workload."""

    template: ReleaseAuxiliaryPerformanceTemplateV1
    candidate_commit: str
    source_tree: RunnerSourceTreeV1
    artifact_manifest_sha256: str
    asset_manifest_sha256: str | None
    source_runs: tuple[ReleaseAuxiliarySourceRunV1, ...] = ()
    execution_policy_id: str = AUXILIARY_EXECUTION_POLICY_V1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.execution_policy_id != AUXILIARY_EXECUTION_POLICY_V1
        ):
            raise ValueError("auxiliary execution policy identity differs")
        if type(self.template) is not ReleaseAuxiliaryPerformanceTemplateV1:
            raise TypeError("auxiliary execution requires one typed template")
        if type(self.source_tree) is not RunnerSourceTreeV1:
            raise TypeError("auxiliary execution requires one typed source lock")
        if _COMMIT.fullmatch(self.candidate_commit) is None:
            raise ValueError("auxiliary candidate commit is invalid")
        require_sha256(
            self.artifact_manifest_sha256,
            "auxiliary artifact-manifest digest",
        )
        if self.template.workload_id == "RELEASE_MICROSCOPE_LOAD_V1":
            require_sha256(
                self.asset_manifest_sha256,
                "microscope asset-manifest digest",
            )
        elif self.asset_manifest_sha256 is not None:
            raise ValueError("non-microscope execution carries an asset digest")
        if type(self.source_runs) is not tuple or any(
            type(item) is not ReleaseAuxiliarySourceRunV1
            for item in self.source_runs
        ):
            raise TypeError("auxiliary source runs must be a typed tuple")
        ordinals = tuple(item.ordinal for item in self.source_runs)
        artifact_ids = tuple(item.artifact_id for item in self.source_runs)
        if (
            ordinals != tuple(sorted(ordinals))
            or len(ordinals) != len(set(ordinals))
            or len(artifact_ids) != len(set(artifact_ids))
        ):
            raise ValueError("auxiliary source runs must be unique and ordinal-sorted")
        expected_ordinals = {
            "RELEASE_INTERACTIVE_ACK_V1": (),
            "RELEASE_TERMINAL_UPDATE_V1": (),
            "RELEASE_FULL_DAY_GENERATION_V1": (),
            "RELEASE_FULL_DAY_REPLAY_V1": (0, 1, 2, 3),
            "RELEASE_MICROSCOPE_LOAD_V1": (1,),
        }[self.template.workload_id]
        if ordinals != expected_ordinals:
            raise ValueError("auxiliary source-run ordinal inventory differs")
        locked = self.source_tree.by_path()
        if any(path not in locked for path in self.template.entrypoint_paths):
            raise ValueError("auxiliary entrypoint is absent from the exact source lock")
        _validate_template_contract(self.template)

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_manifest_sha256": self.artifact_manifest_sha256,
            "asset_manifest_sha256": self.asset_manifest_sha256,
            "candidate_commit": self.candidate_commit,
            "execution_policy_id": self.execution_policy_id,
            "schema_version": self.schema_version,
            "source_runs": [item.as_dict() for item in self.source_runs],
            "source_tree": self.source_tree.as_dict(),
            "template": self.template.as_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @classmethod
    def from_dict(cls, value: object) -> "ReleaseAuxiliaryExecutionV1":
        row = _exact_object(
            value,
            {
                "artifact_manifest_sha256",
                "asset_manifest_sha256",
                "candidate_commit",
                "execution_policy_id",
                "schema_version",
                "source_runs",
                "source_tree",
                "template",
            },
            "auxiliary execution",
        )
        source_runs = row["source_runs"]
        if type(source_runs) is not list:
            raise TypeError("auxiliary source runs must be an array")
        source_tree = row["source_tree"]
        if type(source_tree) is not dict:
            raise TypeError("auxiliary source tree must be an object")
        asset_digest = row["asset_manifest_sha256"]
        if asset_digest is not None:
            asset_digest = _text(
                asset_digest, "auxiliary asset-manifest digest", 64
            )
        return cls(
            schema_version=row["schema_version"],  # type: ignore[arg-type]
            execution_policy_id=_text(
                row["execution_policy_id"], "auxiliary execution policy", 128
            ),
            template=_template_from_dict(row["template"]),
            candidate_commit=_text(
                row["candidate_commit"], "auxiliary candidate commit", 40
            ),
            source_tree=RunnerSourceTreeV1.from_dict(source_tree),
            artifact_manifest_sha256=_text(
                row["artifact_manifest_sha256"],
                "auxiliary artifact-manifest digest",
                64,
            ),
            asset_manifest_sha256=asset_digest,
            source_runs=tuple(
                ReleaseAuxiliarySourceRunV1.from_dict(item)
                for item in source_runs
            ),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReleaseAuxiliaryExecutionV1":
        return cls.from_dict(load_canonical_json_bytes(raw, "auxiliary execution"))


def _terminal_identity(parameters: Mapping[str, object]) -> None:
    terminal = parameters.get("terminal")
    if terminal != {
        "color": False,
        "columns": _TERMINAL_COLUMNS,
        "drain_policy": "CONTINUOUS",
        "encoding": _TERMINAL_ENCODING,
        "rows": _TERMINAL_ROWS,
        "term": _TERMINAL_TERM,
    }:
        raise ValueError("auxiliary terminal identity differs")


def _validate_template_contract(
    template: ReleaseAuxiliaryPerformanceTemplateV1,
) -> None:
    identity = template.input_identity
    parameters = identity["parameters"]
    assert type(parameters) is dict
    workload_id = template.workload_id
    if workload_id == "RELEASE_INTERACTIVE_ACK_V1":
        expected_keys = {
            "checkpoint_selector_id",
            "curriculum_manifest_path",
            "curriculum_manifest_sha256",
            "curriculum_pack_id",
            "lesson_id",
            "lesson_seed_policy",
            "pairs",
            "quantity_shares",
            "scenario_manifest_path",
            "scenario_manifest_sha256",
            "scenario_pack_id",
            "starter_entries_sha256",
            "starter_set_id",
            "terminal",
            "warmup_pairs",
        }
        if set(parameters) != expected_keys or {
            "checkpoint_selector_id": parameters["checkpoint_selector_id"],
            "lesson_id": parameters["lesson_id"],
            "lesson_seed_policy": parameters["lesson_seed_policy"],
            "pairs": parameters["pairs"],
            "quantity_shares": parameters["quantity_shares"],
            "starter_set_id": parameters["starter_set_id"],
            "warmup_pairs": parameters["warmup_pairs"],
        } != {
            "checkpoint_selector_id": "FIRST_QUIESCENT_CONTINUOUS_TWO_SIDED_V1",
            "lesson_id": "KIRBY2_STARTER_PLACE_CANCEL_V1",
            "lesson_seed_policy": "FROM_LESSON_MANIFEST",
            "pairs": 550,
            "quantity_shares": 1,
            "starter_set_id": "RELEASE_STARTER_SET_V1",
            "warmup_pairs": 50,
        }:
            raise ValueError("interactive ACK template contract differs")
        for field in (
            "starter_entries_sha256",
            "scenario_manifest_sha256",
            "scenario_pack_id",
            "curriculum_manifest_sha256",
            "curriculum_pack_id",
        ):
            require_sha256(parameters[field], f"interactive ACK {field}")
        _terminal_identity(parameters)
    elif workload_id == "RELEASE_TERMINAL_UPDATE_V1":
        if set(parameters) != {
            "artifact_selection_policy_id",
            "measured_updates",
            "profile_id",
            "qualification_evidence_path",
            "qualification_evidence_sha256",
            "root_seed",
            "source_artifact_manifest_sha256",
            "start_selector",
            "terminal",
            "warmup_updates",
        } or {
            "artifact_selection_policy_id": parameters[
                "artifact_selection_policy_id"
            ],
            "measured_updates": parameters["measured_updates"],
            "profile_id": parameters["profile_id"],
            "qualification_evidence_path": parameters[
                "qualification_evidence_path"
            ],
            "root_seed": parameters["root_seed"],
            "start_selector": parameters["start_selector"],
            "warmup_updates": parameters["warmup_updates"],
        } != {
            "artifact_selection_policy_id": "UNIQUE_VERIFIED_PROFILE_ROOT_V1",
            "measured_updates": 5000,
            "profile_id": "QUIET_RANGE_PRESSURE",
            "qualification_evidence_path": "KIRBY2_FULL_DAY_QUALIFICATION_EVIDENCE.md",
            "root_seed": 3_102_000,
            "start_selector": "CONTINUOUS_START",
            "warmup_updates": 100,
        }:
            raise ValueError("terminal-update template contract differs")
        for field in (
            "qualification_evidence_sha256",
            "source_artifact_manifest_sha256",
        ):
            require_sha256(parameters[field], f"terminal update {field}")
        _terminal_identity(parameters)
    elif workload_id == "RELEASE_FULL_DAY_GENERATION_V1":
        if set(parameters) != {
            "measured_ordinals",
            "profile_id",
            "profile_manifest_path",
            "profile_manifest_sha256",
            "repetition_ordinals",
            "root_seed",
            "selected_plan_sha256",
            "warmup_ordinals",
        } or {
            "measured_ordinals": parameters["measured_ordinals"],
            "profile_id": parameters["profile_id"],
            "profile_manifest_path": parameters["profile_manifest_path"],
            "repetition_ordinals": parameters["repetition_ordinals"],
            "root_seed": parameters["root_seed"],
            "warmup_ordinals": parameters["warmup_ordinals"],
        } != {
            "measured_ordinals": [1, 2, 3],
            "profile_id": "QUIET_RANGE_PRESSURE",
            "profile_manifest_path": "kirby2/full_day/profile_candidates.toml",
            "repetition_ordinals": [0, 1, 2, 3],
            "root_seed": 3_101_000,
            "warmup_ordinals": [0],
        }:
            raise ValueError("full-day generation template contract differs")
        for field in ("profile_manifest_sha256", "selected_plan_sha256"):
            require_sha256(parameters[field], f"full-day generation {field}")
    elif workload_id == "RELEASE_FULL_DAY_REPLAY_V1":
        if parameters != {
            "measured_ordinals": [1, 2, 3],
            "producer_workload_id": "RELEASE_FULL_DAY_GENERATION_V1",
            "repetition_ordinals": [0, 1, 2, 3],
            "source_artifact_policy_id": "MATCH_GENERATION_ORDINAL_V1",
            "warmup_ordinals": [0],
        }:
            raise ValueError("full-day replay template contract differs")
    elif workload_id == "RELEASE_MICROSCOPE_LOAD_V1":
        if parameters != {
            "cursor_policy_id": "CONTINUOUS_MIDPOINT_V1",
            "measured_repetitions": 20,
            "mode": "AS_OBSERVED",
            "pane_ids": list(_TEMPLATE_PANE_IDS),
            "producer_workload_id": "RELEASE_FULL_DAY_GENERATION_V1",
            "report_policy_id": "PORTABLE_OFFLINE_REPORT_V1",
            "source_ordinal": 1,
            "warmup_repetitions": 1,
        }:
            raise ValueError("microscope-load template contract differs")
    else:  # pragma: no cover - the template type has the same closed registry
        raise ValueError("unknown auxiliary workload")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o444) -> None:
    if type(payload) is not bytes:
        raise TypeError("immutable auxiliary payload must be exact bytes")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_output_root(output_root: Path) -> tuple[Path, Path]:
    if type(output_root) is not Path or not output_root.is_absolute():
        raise ValueError("auxiliary output root must be an absolute Path")
    resolved = output_root.resolve(strict=False)
    if output_root != resolved or resolved == Path(resolved.anchor):
        raise ValueError("auxiliary output root must be resolved and bounded")
    parent = resolved.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("auxiliary output parent is unavailable")
    if resolved.exists() or resolved.is_symlink():
        raise FileExistsError("auxiliary output root already exists")
    resolved.mkdir(mode=0o700)
    evidence = resolved / _EVIDENCE_DIRECTORY_NAME
    workspace = resolved / _WORKSPACE_DIRECTORY_NAME
    evidence.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    return evidence, workspace


def _tree_bytes(root: Path) -> int:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("auxiliary tree root is not a plain directory")
    seen: set[tuple[int, int]] = set()
    total = 0
    for path in root.rglob("*"):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("auxiliary tree contains a symlink")
        if stat.S_ISREG(metadata.st_mode):
            identity = (metadata.st_dev, metadata.st_ino)
            if identity not in seen:
                seen.add(identity)
                total += metadata.st_size
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("auxiliary tree contains a special filesystem node")
    return total


def _darwin_peak_rss_bytes() -> int:
    if platform.system() != "Darwin":
        raise RuntimeError("auxiliary performance RSS requires the Darwin target")
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if type(value) is not int or value <= 0:
        raise RuntimeError("Darwin ru_maxrss did not return positive bytes")
    return value


class _ContinuouslyDrainedPty:
    """One raw 120x40 pseudo-terminal with a continuously draining reader."""

    def __init__(self) -> None:
        self._master = -1
        self._slave = -1
        self._thread: threading.Thread | None = None
        self._condition = threading.Condition()
        self._bytes_read = 0
        self._bytes_written = 0
        self._digest = hashlib.sha256()
        self._failure: BaseException | None = None
        self._closed = False

    def __enter__(self) -> "_ContinuouslyDrainedPty":
        master, slave = pty.openpty()
        self._master, self._slave = master, slave
        tty.setraw(slave)
        fcntl.ioctl(
            slave,
            getattr(__import__("termios"), "TIOCSWINSZ"),
            struct.pack("HHHH", _TERMINAL_ROWS, _TERMINAL_COLUMNS, 0, 0),
        )
        self._thread = threading.Thread(
            target=self._drain,
            name="kirby2-auxiliary-terminal-drain",
            daemon=True,
        )
        self._thread.start()
        return self

    def _drain(self) -> None:
        try:
            while True:
                try:
                    payload = os.read(self._master, 64 * 1024)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not payload:
                    break
                with self._condition:
                    self._digest.update(payload)
                    self._bytes_read += len(payload)
                    self._condition.notify_all()
        except BaseException as error:  # surfaced synchronously by write_and_flush
            with self._condition:
                self._failure = error
                self._condition.notify_all()

    def write_and_flush(self, lines: tuple[str, ...]) -> int:
        if self._closed or self._slave < 0:
            raise RuntimeError("terminal sink is not open")
        if type(lines) is not tuple or not lines or any(
            type(item) is not str for item in lines
        ):
            raise TypeError("terminal frame must be a nonempty text tuple")
        if len(lines) > _TERMINAL_ROWS or any(
            len(item.encode("utf-8")) > _TERMINAL_COLUMNS for item in lines
        ):
            raise RuntimeError("terminal frame exceeds the frozen 120x40 geometry")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(self._slave, payload[offset:])
        with self._condition:
            self._bytes_written += len(payload)
            target = self._bytes_written
            deadline = time.monotonic() + 10.0
            while self._bytes_read < target and self._failure is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("continuous terminal drain did not flush a frame")
                self._condition.wait(remaining)
            if self._failure is not None:
                raise RuntimeError("continuous terminal drain failed") from self._failure
        return len(payload)

    def receipt(self) -> dict[str, object]:
        with self._condition:
            if self._failure is not None:
                raise RuntimeError("continuous terminal drain failed") from self._failure
            if self._bytes_read != self._bytes_written:
                raise RuntimeError("continuous terminal drain byte counts differ")
            return {
                "bytes_drained": self._bytes_read,
                "bytes_written": self._bytes_written,
                "columns": _TERMINAL_COLUMNS,
                "color": False,
                "drain_policy": "CONTINUOUS",
                "encoding": _TERMINAL_ENCODING,
                "rows": _TERMINAL_ROWS,
                "sha256": self._digest.hexdigest(),
                "term": _TERMINAL_TERM,
            }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._slave >= 0:
            os.close(self._slave)
            self._slave = -1
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                raise TimeoutError("continuous terminal drain did not close")
        if self._master >= 0:
            os.close(self._master)
            self._master = -1

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _source_store(
    source: ReleaseAuxiliarySourceRunV1,
):
    from kirby2.full_day.store import FullDayStore

    root = Path(source.store_root)
    if not root.is_dir() or root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("auxiliary source store root is not a plain resolved directory")
    store = FullDayStore(root)
    manifest_path = store.run_directory(source.run_id) / "manifest.toml"
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or _sha256_file(manifest_path) != source.manifest_sha256
    ):
        raise ValueError("auxiliary source manifest differs from its bound digest")
    manifest = store.load_manifest(source.run_id)
    return store, manifest


def _materialize_terminal_source(
    request: ReleaseAuxiliaryExecutionV1,
    workspace: Path,
):
    """Materialize the one DEV-0016 terminal source in the private workspace.

    WO31 normally protects qualification roots behind its one-time authority.  The
    DEV-0016 restart grants this installed, closed runner a narrower purpose: rebuild
    only the already-revealed quiet/root-3102000 source and accept it only when its
    semantic evidence digest is byte-for-byte the preregistered digest.  The store is
    never returned by the public API or copied into published evidence.
    """

    from kirby2.full_day.qualification import (
        RealQualificationAuthorityV1,
        _build_candidate_runtime,
        real_qualification_identity,
    )
    from kirby2.full_day.profiles import (
        QUIET_RANGE_PRESSURE,
        load_full_day_profile_bundle,
    )
    from kirby2.full_day.store import FullDayStore

    parameters = request.template.input_identity["parameters"]
    assert type(parameters) is dict
    expected_evidence_sha256 = _text(
        parameters.get("source_artifact_manifest_sha256"),
        "terminal source evidence digest",
        64,
    )
    require_sha256(expected_evidence_sha256, "terminal source evidence digest")
    bundle = load_full_day_profile_bundle()
    candidate = bundle.candidates.candidate(QUIET_RANGE_PRESSURE)
    authority = RealQualificationAuthorityV1(
        mode="EXECUTE_ONCE",
        implementation_commit=request.candidate_commit,
        qualification_identity=real_qualification_identity(
            request.candidate_commit, bundle
        ),
        profile_bundle_sha256=bundle.bundle_sha256,
        existing_run_id=None,
    )
    plan, workload, runtime, maximum_initial_pending = _build_candidate_runtime(
        candidate,
        3_102_000,
        authority=authority,
    )
    store_root = workspace / "terminal-source"
    store = FullDayStore(store_root)
    manifest = store.generate_day(plan, runtime)
    verification = store.verify_day(manifest.run_id)
    if not verification.passed:
        raise RuntimeError(
            "materialized terminal source failed full-day verification: "
            + "; ".join(verification.failures)
        )
    if (
        manifest.seed != 3_102_000
        or manifest.evidence_digest != expected_evidence_sha256
    ):
        raise RuntimeError(
            "materialized terminal source differs from the preregistered identity"
        )
    return store, manifest, {
        "candidate_id": candidate.candidate_id,
        "evidence_sha256": manifest.evidence_digest,
        "maximum_initial_pending": maximum_initial_pending,
        "plan_sha256": plan.semantic_sha256,
        "root_seed": manifest.seed,
        "run_id": manifest.run_id,
        "verification": verification.as_dict(),
        "workload_sha256": workload.sha256,
    }


def _continuous_bounds(plan: object) -> tuple[int, int]:
    calendar = getattr(plan, "calendar")
    phases = getattr(calendar, "phases")
    rows = tuple(phase for phase in phases if phase.phase_id == "CONTINUOUS")
    if len(rows) != 1:
        raise ValueError("full-day source has no unique continuous phase")
    start = rows[0].start.simulation_time_us
    end = rows[0].end.simulation_time_us
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError("full-day continuous phase bounds are invalid")
    return start, end


def _generation_attempt(payload: dict[str, object]) -> dict[str, object]:
    from kirby2.full_day.qualification import (
        build_release_performance_full_day_source_v1,
    )
    from kirby2.full_day.store import FullDayStore
    from kirby2.research.models import ArtifactType

    ordinal = _nonnegative_integer(payload.get("ordinal"), "generation ordinal")
    store_root = _absolute_path(payload.get("store_root"), "generation store root")
    started_ns = time.monotonic_ns()
    plan, _workload, runtime, maximum_initial_pending = (
        build_release_performance_full_day_source_v1()
    )
    if plan.seed_policy.root_seed != 3_101_000:
        raise RuntimeError("generation source root differs from 3101000")
    expected_plan_sha256 = _text(
        payload.get("selected_plan_sha256"), "selected plan digest", 64
    )
    if plan.semantic_sha256 != expected_plan_sha256:
        raise RuntimeError("generation plan differs from the frozen selected plan")
    store = FullDayStore(store_root)
    manifest = store.generate_day(plan, runtime)
    verification = store.verify_day(manifest.run_id)
    if not verification.passed:
        raise RuntimeError(
            "generated full day failed verification: "
            + "; ".join(verification.failures)
        )
    run_directory = store.run_directory(manifest.run_id)
    full_day_bytes = _tree_bytes(run_directory)
    checkpoints = tuple(
        item
        for item in manifest.artifacts
        if item.artifact_type is ArtifactType.FULL_DAY_CHECKPOINT
    )
    outer = tuple(
        item
        for item in manifest.artifacts
        if item.artifact_type is ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER
    )
    ledgers = tuple(
        item
        for item in manifest.artifacts
        if item.artifact_type
        in {
            ArtifactType.FULL_DAY_OUTER_EVENT_LEDGER,
            ArtifactType.FULL_DAY_SUBSYSTEM_LEDGER,
        }
    )
    if not checkpoints or len(outer) != 1 or outer[0].row_count in {None, 0}:
        raise RuntimeError("generated full-day size denominators are unavailable")
    checkpoint_sizes = tuple(
        (run_directory / item.relative_path).stat().st_size for item in checkpoints
    )
    ledger_sizes = tuple(
        (run_directory / item.relative_path).stat().st_size for item in ledgers
    )
    if any(
        _sha256_file(run_directory / item.relative_path) != item.sha256
        for item in (*checkpoints, *ledgers)
    ):
        raise RuntimeError("generated size evidence differs from artifact digests")
    outer_event_count = outer[0].row_count
    assert type(outer_event_count) is int
    day_duration_us = plan.calendar.end_time_us
    if day_duration_us <= 0:
        raise RuntimeError("generated day duration is zero")
    ended_ns = time.monotonic_ns()
    manifest_path = run_directory / "manifest.toml"
    return {
        "checkpoint_count": len(checkpoints),
        "checkpoint_growth_bytes_per_simulation_hour": round_div_even(
            sum(checkpoint_sizes) * 3_600_000_000,
            day_duration_us,
        ),
        "day_duration_us": day_duration_us,
        "fresh_process_policy_id": _FRESH_PROCESS_POLICY_V1,
        "full_day_bytes": full_day_bytes,
        "largest_checkpoint_bytes": max(checkpoint_sizes),
        "ledger_growth_bytes_per_1000_events": round_div_even(
            sum(ledger_sizes) * 1000,
            outer_event_count,
        ),
        "manifest_sha256": _sha256_file(manifest_path),
        "maximum_initial_pending": maximum_initial_pending,
        "ordinal": ordinal,
        "outer_event_count": outer_event_count,
        "peak_rss_bytes": _darwin_peak_rss_bytes(),
        "plan_sha256": plan.semantic_sha256,
        "run_id": manifest.run_id,
        "status": "PASS",
        "wall_time_ns": ended_ns - started_ns,
    }


def _replay_attempt(payload: dict[str, object]) -> dict[str, object]:
    source = ReleaseAuxiliarySourceRunV1.from_dict(payload.get("source"))
    store, manifest = _source_store(source)
    started_ns = time.monotonic_ns()
    verification = store.verify_day(source.run_id)
    if not verification.passed:
        raise RuntimeError(
            "replay source failed exact verification: "
            + "; ".join(verification.failures)
        )
    ended_ns = time.monotonic_ns()
    return {
        "fresh_process_policy_id": _FRESH_PROCESS_POLICY_V1,
        "manifest_result_sha256": manifest.result_digest,
        "ordinal": source.ordinal,
        "peak_rss_bytes": _darwin_peak_rss_bytes(),
        "run_id": source.run_id,
        "status": "PASS",
        "verification": verification.as_dict(),
        "wall_time_ns": ended_ns - started_ns,
    }


def _microscope_attempt(payload: dict[str, object]) -> dict[str, object]:
    from kirby2.microscope.overlays import (
        OverlayInputSelection,
        build_overlay_set,
        build_overlay_window_projection,
    )
    from kirby2.microscope.panes import PANE_ORDER, build_synchronized_panes
    from kirby2.microscope.query import (
        ObservationQueryRequest,
        ObservedEvidenceSet,
        query_as_observed,
    )
    from kirby2.microscope.report import (
        ClockPresentation,
        ClockTimeBasis,
        InstrumentPresentation,
        PresentationMetadataAuthority,
        RecordingPresentation,
        ReplayPresentationContext,
        ReportPresentation,
        build_portable_replay_report,
        build_replay_presentation_frame,
        load_installed_renderer_assets,
        render_portable_report_bundle,
        verify_portable_report_bundle,
        write_portable_report_bundle,
    )
    from kirby2.microscope.timeline import build_replay_timeline

    source = ReleaseAuxiliarySourceRunV1.from_dict(payload.get("source"))
    report_root = _absolute_path(payload.get("report_root"), "microscope report root")
    expected_template_panes = payload.get("template_pane_ids")
    if expected_template_panes != list(_TEMPLATE_PANE_IDS):
        raise RuntimeError("microscope template pane inventory differs")
    store, manifest = _source_store(source)
    started_ns = time.monotonic_ns()
    initial = store.seek(source.run_id, 0)
    continuous_start, continuous_end = _continuous_bounds(initial.runtime.plan)
    cursor_time_us = continuous_start + (continuous_end - continuous_start) // 2
    sought = store.seek(source.run_id, cursor_time_us)
    inspection = store.inspect_day(source.run_id)
    assets = load_installed_renderer_assets()
    runtime_panes = tuple(item.value for item in PANE_ORDER)
    if runtime_panes != _RUNTIME_PANE_IDS or tuple(
        _TEMPLATE_TO_RUNTIME_PANE_ID[item] for item in _TEMPLATE_PANE_IDS
    ) != runtime_panes:
        raise RuntimeError("installed microscope PANE_ORDER differs from the frozen map")
    observed = ObservedEvidenceSet(source.run_id, manifest.result_digest)
    query = query_as_observed(
        observed,
        ObservationQueryRequest(render_cursor_time_us=cursor_time_us),
    )
    panes = build_synchronized_panes(query)
    if tuple(item.pane_kind for item in panes.panes) != PANE_ORDER:
        raise RuntimeError("microscope did not load every pane in exact PANE_ORDER")
    projection, _projection_receipt = build_overlay_window_projection(query)
    overlays = build_overlay_set(query, projection, OverlayInputSelection())
    timeline, _timeline_receipt = build_replay_timeline(query, ())
    cursor = timeline.cursor(cursor_time_us)
    context = ReplayPresentationContext(
        source_run_id=source.run_id,
        source_event_sha256=manifest.result_digest,
        metadata_authority=(
            PresentationMetadataAuthority.SOURCE_BOUND_DISPLAY_DECLARATION
        ),
        recording=RecordingPresentation(
            recording_id=source.run_id,
            display_name="Kirby2 verified simulated full day",
            content_sha256=manifest.result_digest,
        ),
        report=ReportPresentation(
            "AS_OBSERVED midpoint replay from a verified simulated full day."
        ),
        clock=ClockPresentation(
            time_basis=ClockTimeBasis.SIMULATION_TIME,
            session_origin_time_us=0,
            display_precision_us=1,
            cursor_label=f"T+{cursor_time_us}us",
        ),
        instrument=InstrumentPresentation(
            instrument_id="kirby2-simulated-instrument-v1",
            symbol="K2SIM",
            display_name="Kirby2 simulated instrument",
            venue_labels=("SIMULATED",),
            currency="USD",
            tick_numerator=1,
            tick_denominator=1,
            price_precision=0,
            quantity_unit="shares",
            lot_size=1,
        ),
        limitations=(
            "The report contains simulated AS_OBSERVED evidence only.",
            "No real market, brokerage, order-routing, or account source is present.",
        ),
    )
    frame = build_replay_presentation_frame(
        timeline,
        cursor,
        query,
        panes,
        overlays,
        context,
    )
    report = build_portable_replay_report((frame,))
    bundle = render_portable_report_bundle(report)
    report_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_portable_report_bundle(bundle, report_root)
    verification = verify_portable_report_bundle(report_root)
    if verification.get("status") != "PASS":
        raise RuntimeError("portable microscope report did not verify")
    ended_ns = time.monotonic_ns()
    return {
        "asset_inventory": [
            {
                **item.as_dict(),
                "size": len(item.bytes_payload),
            }
            for item in assets
        ],
        "bundle_id": bundle.bundle_id,
        "cursor_time_us": cursor_time_us,
        "fresh_process_policy_id": _FRESH_PROCESS_POLICY_V1,
        "inspection_run_id": inspection["run_id"],
        "pane_ids": list(_TEMPLATE_PANE_IDS),
        "peak_rss_bytes": _darwin_peak_rss_bytes(),
        "report_id": report.report_id,
        "run_id": source.run_id,
        "seek_event_count": sought.uninterrupted_event_count,
        "status": "PASS",
        "verification": verification,
        "wall_time_ns": ended_ns - started_ns,
    }


_FRESH_ATTEMPT_HANDLERS: Final[Mapping[str, object]] = {
    "GENERATION": _generation_attempt,
    "REPLAY": _replay_attempt,
    "MICROSCOPE": _microscope_attempt,
}


def _fresh_attempt_entry(
    operation: str,
    payload: dict[str, object],
    receipt_path_text: str,
) -> None:
    receipt_path = Path(receipt_path_text)
    try:
        handler = _FRESH_ATTEMPT_HANDLERS[operation]
        if not callable(handler):  # pragma: no cover - closed registry invariant
            raise RuntimeError("fresh attempt handler is unavailable")
        result = handler(payload)
        if type(result) is not dict:
            raise RuntimeError("fresh attempt did not return one object")
        receipt = result
    except Exception as error:
        receipt = {
            "error": str(error)[:4096],
            "error_type": type(error).__name__,
            "status": "FAIL",
        }
    _write_exclusive(receipt_path, canonical_json_bytes(receipt))


def _spawn_fresh_attempt(
    operation: str,
    payload: dict[str, object],
    receipt_path: Path,
) -> dict[str, object]:
    if operation not in _FRESH_ATTEMPT_HANDLERS:
        raise ValueError("fresh auxiliary operation is unsupported")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_fresh_attempt_entry,
        args=(operation, payload, os.fspath(receipt_path)),
        name=f"kirby2-auxiliary-{operation.casefold()}",
    )
    process.start()
    process.join()
    if process.exitcode != 0 or not receipt_path.is_file():
        raise RuntimeError(
            f"fresh auxiliary {operation.casefold()} process exited {process.exitcode}"
        )
    value = load_canonical_json_bytes(
        receipt_path.read_bytes(), f"fresh {operation.casefold()} receipt"
    )
    if type(value) is not dict:
        raise TypeError("fresh auxiliary receipt must be an object")
    if value.get("status") != "PASS":
        raise RuntimeError(
            f"fresh {operation.casefold()} failed: "
            f"{value.get('error_type', 'ERROR')}: {value.get('error', '')}"
        )
    return value


def _render_session_frame(session: object, sink: _ContinuouslyDrainedPty) -> int:
    from kirby2.session.bindings import BindingMap
    from kirby2.ui.terminal import TerminalUiConfig, render_terminal_frame

    snapshot = session.snapshot()
    frame = render_terminal_frame(
        snapshot,
        BindingMap.default(),
        TerminalUiConfig(),
        width=_TERMINAL_COLUMNS,
    )
    return sink.write_and_flush(frame)


def _interactive_ack_workload(
    request: ReleaseAuxiliaryExecutionV1,
    workspace: Path,
) -> tuple[
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    dict[str, bytes],
]:
    from kirby2.release.first_run import (
        build_release_starter_set,
        install_release_starter_set,
    )
    from kirby2.research.paths import DataPaths
    from kirby2.scenarios import get_scenario_definition
    from kirby2.session.bindings import SessionCommand
    from kirby2.session.live import LiveMarketSession

    parameters = request.template.input_identity["parameters"]
    assert type(parameters) is dict
    starter = build_release_starter_set()
    if starter.entries_sha256 != parameters["starter_entries_sha256"]:
        raise RuntimeError("installed starter-set entries differ from the template")
    scenario_entry, curriculum_entry = starter.entries
    if {
        "manifest_path": scenario_entry.manifest_path,
        "manifest_sha256": scenario_entry.manifest_sha256,
        "pack_id": scenario_entry.pack_id,
    } != {
        "manifest_path": parameters["scenario_manifest_path"],
        "manifest_sha256": parameters["scenario_manifest_sha256"],
        "pack_id": parameters["scenario_pack_id"],
    } or {
        "manifest_path": curriculum_entry.manifest_path,
        "manifest_sha256": curriculum_entry.manifest_sha256,
        "pack_id": curriculum_entry.pack_id,
    } != {
        "manifest_path": parameters["curriculum_manifest_path"],
        "manifest_sha256": parameters["curriculum_manifest_sha256"],
        "pack_id": parameters["curriculum_pack_id"],
    }:
        raise RuntimeError("installed starter pack identity differs from the template")
    installation = install_release_starter_set(
        DataPaths(workspace / "starter-data"), starter
    )
    if not installation.complete:
        raise RuntimeError("exact starter set did not install and activate")

    class _PerformanceAckSession(LiveMarketSession):
        def _next_order_id(self) -> str:
            ordinal = self._order_sequence
            self._order_sequence += 1
            return f"perf-{ordinal:04d}"

    session = _PerformanceAckSession(
        get_scenario_definition("momentum_up"),
        seed=73,
        duration_seconds=1,
        initial_quantity=1,
        quantity_options=(1,),
    )
    initial = session.snapshot()
    if session.running or not initial.bids or not initial.asks:
        raise RuntimeError("starter checkpoint is not paused and two-sided")
    best_bid = initial.bids[0].price_ticks
    warmup: list[int] = []
    measured: list[int] = []
    acknowledgements: list[dict[str, object]] = []
    with _ContinuouslyDrainedPty() as sink:
        for pair_ordinal in range(550):
            expected_order_id = f"perf-{pair_ordinal:04d}"
            started_ns = time.monotonic_ns()
            submitted = session.execute(
                SessionCommand.BUY_BID,
                quantity_override=1,
                price_ticks_override=best_bid,
            )
            _render_session_frame(session, sink)
            submit_latency_ns = time.monotonic_ns() - started_ns
            snapshot = session.snapshot()
            if (
                not submitted.accepted
                or submitted.order_ids != (expected_order_id,)
                or submitted.parameters.get("filled_quantity") != 0
                or submitted.parameters.get("remaining_quantity") != 1
                or tuple(item.order_id for item in snapshot.working_orders)
                != (expected_order_id,)
            ):
                raise RuntimeError("interactive submit acknowledgement differs")
            acknowledgements.append(
                {
                    "action": "SUBMIT",
                    "ack_ordinal": 2 * pair_ordinal,
                    "latency_ns": submit_latency_ns,
                    "order_id": expected_order_id,
                    "pair_ordinal": pair_ordinal,
                }
            )
            started_ns = time.monotonic_ns()
            cancelled = session.execute(SessionCommand.CANCEL_NEAREST)
            _render_session_frame(session, sink)
            cancel_latency_ns = time.monotonic_ns() - started_ns
            if (
                not cancelled.accepted
                or cancelled.parameters.get("target_order_id") != expected_order_id
                or cancelled.parameters.get("cancelled_quantity") != 1
                or session.snapshot().working_orders
            ):
                raise RuntimeError("interactive cancel acknowledgement differs")
            acknowledgements.append(
                {
                    "action": "CANCEL",
                    "ack_ordinal": 2 * pair_ordinal + 1,
                    "latency_ns": cancel_latency_ns,
                    "order_id": expected_order_id,
                    "pair_ordinal": pair_ordinal,
                }
            )
            target = warmup if pair_ordinal < 50 else measured
            target.extend((submit_latency_ns, cancel_latency_ns))
        sink_receipt = sink.receipt()
    if len(acknowledgements) != 1100 or len(
        {
            (row["ack_ordinal"], row["action"], row["order_id"])
            for row in acknowledgements
        }
    ) != 1100:
        raise RuntimeError("interactive acknowledgement inventory is incomplete")
    acknowledgement_bytes = canonical_json_bytes(acknowledgements)
    receipt = {
        "acknowledgement_count": len(acknowledgements),
        "acknowledgement_inventory_sha256": hashlib.sha256(
            acknowledgement_bytes
        ).hexdigest(),
        "best_bid_ticks": best_bid,
        "lesson_id": parameters["lesson_id"],
        "pair_count": 550,
        "peak_rss_bytes": _darwin_peak_rss_bytes(),
        "starter_install": installation.as_dict(),
        "starter_set": starter.layout_dict(),
        "status": "PASS",
        "terminal": sink_receipt,
    }
    return (
        {"ack_latency_ns": tuple(warmup)},
        {
            "ack_latency_ns": tuple(measured),
            "peak_rss_bytes": (receipt["peak_rss_bytes"],),  # type: ignore[arg-type]
        },
        {
            "interactive-ack/acknowledgements.json": acknowledgement_bytes,
            "interactive-ack/receipt.json": canonical_json_bytes(receipt),
        },
    )


def _market_state_snapshot(
    market: Mapping[str, object],
    *,
    message_sequence: int,
    duration_us: int,
):
    from kirby2.session.live import LevelView, SessionSnapshot

    simulation_time_us = _nonnegative_integer(
        market.get("simulation_time_us"), "terminal market-state time"
    )

    def levels(name: str) -> tuple[LevelView, ...]:
        raw = market.get(name)
        if type(raw) is not list:
            raise TypeError("terminal market-state levels must be an array")
        output: list[LevelView] = []
        for item in raw[:10]:
            row = _exact_object(item, {"price_ticks", "quantity"}, "market level")
            price = _nonnegative_integer(row["price_ticks"], "market level price")
            quantity = _nonnegative_integer(row["quantity"], "market level quantity")
            output.append(
                LevelView(
                    price_ticks=price,
                    price=str(price),
                    aggregate_quantity=quantity,
                    player_quantity=0,
                    queue_ahead_quantity=None,
                )
            )
        return tuple(output)

    bids = levels("bid_levels")
    asks = levels("ask_levels")
    market_state_id = hashlib.sha256(canonical_json_bytes(dict(market))).hexdigest()
    return SessionSnapshot(
        scenario_name="QUIET_RANGE_PRESSURE",
        regime=_text(market.get("session_state"), "terminal session state", 64),
        seed=None,
        relative_volume="PROFILE",
        liquidity="PROFILE",
        simulation_time_us=simulation_time_us,
        duration_us=duration_us,
        running=True,
        complete=False,
        selected_quantity=1,
        position=0,
        bought_quantity=0,
        sold_quantity=0,
        bids=bids,
        asks=asks,
        tape=(),
        working_orders=(),
        traffic_light="RECORDED",
        traffic_setup=None,
        strategy_state=None,
        strategy_entry_permission="RECORDED",
        strategy_exit_permission="RECORDED",
        traffic_reason="Client-observable simulated full-day state",
        objective_type=None,
        objective_target_quantity=0,
        objective_completed_quantity=0,
        objective_completion_percentage="0",
        objective_time_limit_us=None,
        status_message=f"OBSERVED UPDATE {message_sequence}",
        exchange_event_sequence=message_sequence,
        market_state_id=market_state_id,
        market_state_time_us=simulation_time_us,
    )


def _terminal_update_workload(
    request: ReleaseAuxiliaryExecutionV1,
    workspace: Path,
) -> tuple[
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    dict[str, bytes],
]:
    from kirby2.session.bindings import BindingMap
    from kirby2.ui.terminal import TerminalUiConfig, render_terminal_frame

    store, manifest, source_receipt = _materialize_terminal_source(
        request, workspace
    )
    sought = store.seek(manifest.run_id, manifest.simulation_end_us)
    runtime = sought.runtime
    continuous_start, _continuous_end = _continuous_bounds(runtime.plan)
    delivery = runtime.delivery
    if delivery is None:
        raise RuntimeError("terminal source has no client delivery owner")
    updates: list[tuple[int, dict[str, object]]] = []
    for raw in delivery.delivered_messages:
        if raw.get("kind") != "MARKET_STATE":
            continue
        sequence = raw.get("message_sequence")
        delivery_time = raw.get("delivery_time_us")
        client_payload = raw.get("client_payload")
        market = (
            None
            if not isinstance(client_payload, Mapping)
            else client_payload.get("market_state")
        )
        if (
            type(sequence) is not int
            or type(delivery_time) is not int
            or not isinstance(market, Mapping)
        ):
            raise RuntimeError("client-observable terminal update is malformed")
        if delivery_time >= continuous_start:
            updates.append((sequence, dict(market)))
    selected = updates[:5100]
    sequences = tuple(item[0] for item in selected)
    if (
        len(selected) != 5100
        or sequences != tuple(sorted(sequences))
        or len(sequences) != len(set(sequences))
    ):
        raise RuntimeError("terminal source lacks 5100 ordered unique updates")
    warmup: list[int] = []
    measured: list[int] = []
    config = TerminalUiConfig()
    bindings = BindingMap.default()
    rendered_rows: list[dict[str, object]] = []
    with _ContinuouslyDrainedPty() as sink:
        for ordinal, (sequence, market) in enumerate(selected):
            started_ns = time.monotonic_ns()
            snapshot = _market_state_snapshot(
                market,
                message_sequence=sequence,
                duration_us=manifest.simulation_end_us,
            )
            frame = render_terminal_frame(
                snapshot,
                bindings,
                config,
                width=_TERMINAL_COLUMNS,
            )
            frame_bytes = sink.write_and_flush(frame)
            latency_ns = time.monotonic_ns() - started_ns
            (warmup if ordinal < 100 else measured).append(latency_ns)
            rendered_rows.append(
                {
                    "frame_bytes": frame_bytes,
                    "latency_ns": latency_ns,
                    "market_state_id": snapshot.market_state_id,
                    "message_sequence": sequence,
                    "ordinal": ordinal,
                }
            )
        sink_receipt = sink.receipt()
    peak_rss = _darwin_peak_rss_bytes()
    inventory_bytes = canonical_json_bytes(rendered_rows)
    receipt = {
        "first_message_sequence": sequences[0],
        "last_message_sequence": sequences[-1],
        "peak_rss_bytes": peak_rss,
        "rendered_update_count": len(selected),
        "run_id": manifest.run_id,
        "source_evidence_sha256": manifest.evidence_digest,
        "source_materialization": source_receipt,
        "status": "PASS",
        "terminal": sink_receipt,
        "update_inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
    }
    return (
        {"update_latency_ns": tuple(warmup)},
        {
            "peak_rss_bytes": (peak_rss,),
            "update_latency_ns": tuple(measured),
        },
        {
            "terminal-update/receipt.json": canonical_json_bytes(receipt),
            "terminal-update/update-inventory.json": inventory_bytes,
        },
    )


def _generation_workload(
    request: ReleaseAuxiliaryExecutionV1,
    workspace: Path,
) -> tuple[
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    dict[str, bytes],
]:
    parameters = request.template.input_identity["parameters"]
    assert type(parameters) is dict
    attempt_root = workspace / "generation"
    receipt_root = workspace / "fresh-receipts"
    attempt_root.mkdir(mode=0o700)
    receipt_root.mkdir(mode=0o700)
    receipts: list[dict[str, object]] = []
    source_rows: list[dict[str, object]] = []
    for ordinal in range(4):
        store_root = attempt_root / f"ordinal-{ordinal:04d}"
        receipt_path = receipt_root / f"generation-{ordinal:04d}.json"
        receipt = _spawn_fresh_attempt(
            "GENERATION",
            {
                "ordinal": ordinal,
                "selected_plan_sha256": parameters["selected_plan_sha256"],
                "store_root": os.fspath(store_root),
            },
            receipt_path,
        )
        receipts.append(receipt)
        source_rows.append(
            {
                "artifact_id": f"release-full-day-generation-{ordinal:04d}",
                "manifest_sha256": receipt["manifest_sha256"],
                "ordinal": ordinal,
                "run_id": receipt["run_id"],
                "store_relative_root": (
                    f"{_WORKSPACE_DIRECTORY_NAME}/generation/ordinal-{ordinal:04d}"
                ),
            }
        )
    warmup_receipt = receipts[0]
    measured_receipts = receipts[1:]
    warmup = {
        "peak_rss_bytes": (warmup_receipt["peak_rss_bytes"],),
        "wall_time_ns": (warmup_receipt["wall_time_ns"],),
    }
    measured = {
        metric: tuple(receipt[metric] for receipt in measured_receipts)
        for metric in (
            "checkpoint_growth_bytes_per_simulation_hour",
            "full_day_bytes",
            "largest_checkpoint_bytes",
            "ledger_growth_bytes_per_1000_events",
            "peak_rss_bytes",
            "wall_time_ns",
        )
    }
    return (
        warmup,  # type: ignore[return-value]
        measured,  # type: ignore[return-value]
        {
            "full-day-generation/attempts.json": canonical_json_bytes(receipts),
            "full-day-generation/sources.json": canonical_json_bytes(
                {
                    "producer_workload_id": request.template.workload_id,
                    "sources": source_rows,
                    "status": "PASS",
                }
            ),
        },
    )


def _replay_workload(
    request: ReleaseAuxiliaryExecutionV1,
    workspace: Path,
) -> tuple[
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    dict[str, bytes],
]:
    receipt_root = workspace / "fresh-receipts"
    receipt_root.mkdir(mode=0o700)
    receipts = [
        _spawn_fresh_attempt(
            "REPLAY",
            {"source": source.as_dict()},
            receipt_root / f"replay-{source.ordinal:04d}.json",
        )
        for source in request.source_runs
    ]
    warmup = {
        "peak_rss_bytes": (receipts[0]["peak_rss_bytes"],),
        "wall_time_ns": (receipts[0]["wall_time_ns"],),
    }
    measured = {
        "peak_rss_bytes": tuple(item["peak_rss_bytes"] for item in receipts[1:]),
        "wall_time_ns": tuple(item["wall_time_ns"] for item in receipts[1:]),
    }
    return (
        warmup,  # type: ignore[return-value]
        measured,  # type: ignore[return-value]
        {"full-day-replay/attempts.json": canonical_json_bytes(receipts)},
    )


def _microscope_workload(
    request: ReleaseAuxiliaryExecutionV1,
    workspace: Path,
) -> tuple[
    dict[str, tuple[int, ...]],
    dict[str, tuple[int, ...]],
    dict[str, bytes],
]:
    from kirby2.microscope.report import load_installed_renderer_assets

    source = request.source_runs[0]
    receipt_root = workspace / "fresh-receipts"
    report_parent = workspace / "microscope-reports"
    receipt_root.mkdir(mode=0o700)
    report_parent.mkdir(mode=0o700)
    receipts: list[dict[str, object]] = []
    for repetition in range(21):
        receipts.append(
            _spawn_fresh_attempt(
                "MICROSCOPE",
                {
                    "report_root": os.fspath(
                        report_parent / f"repetition-{repetition:04d}"
                    ),
                    "source": source.as_dict(),
                    "template_pane_ids": list(_TEMPLATE_PANE_IDS),
                },
                receipt_root / f"microscope-{repetition:04d}.json",
            )
        )
    identities = {
        (
            item["cursor_time_us"],
            tuple(item["pane_ids"]),  # type: ignore[arg-type]
            item["run_id"],
        )
        for item in receipts
    }
    if len(identities) != 1:
        raise RuntimeError("microscope repetitions changed source/cursor/pane identity")
    representative = report_parent / "repetition-0001"
    evidence: dict[str, bytes] = {
        "microscope-load/attempts.json": canonical_json_bytes(receipts),
    }
    for asset in load_installed_renderer_assets():
        evidence[f"microscope-load/installed-assets/{asset.name}"] = (
            asset.bytes_payload
        )
    for path in sorted(
        (item for item in representative.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(representative).as_posix().encode("utf-8"),
    ):
        relative = path.relative_to(representative).as_posix()
        evidence[f"microscope-load/report/{relative}"] = path.read_bytes()
    warmup = {
        "peak_rss_bytes": (receipts[0]["peak_rss_bytes"],),
        "wall_time_ns": (receipts[0]["wall_time_ns"],),
    }
    measured = {
        "peak_rss_bytes": tuple(item["peak_rss_bytes"] for item in receipts[1:]),
        "wall_time_ns": tuple(item["wall_time_ns"] for item in receipts[1:]),
    }
    return (
        warmup,  # type: ignore[return-value]
        measured,  # type: ignore[return-value]
        evidence,
    )


_WORKLOAD_HANDLERS: Final[Mapping[str, object]] = {
    "RELEASE_INTERACTIVE_ACK_V1": _interactive_ack_workload,
    "RELEASE_TERMINAL_UPDATE_V1": _terminal_update_workload,
    "RELEASE_FULL_DAY_GENERATION_V1": _generation_workload,
    "RELEASE_FULL_DAY_REPLAY_V1": _replay_workload,
    "RELEASE_MICROSCOPE_LOAD_V1": _microscope_workload,
}


def _execution_evidence(request: ReleaseAuxiliaryExecutionV1) -> bytes:
    return canonical_json_bytes(
        {
            "artifact_manifest_sha256": request.artifact_manifest_sha256,
            "asset_manifest_sha256": request.asset_manifest_sha256,
            "candidate_commit": request.candidate_commit,
            "execution_policy_id": request.execution_policy_id,
            "input_identity_sha256": hashlib.sha256(
                canonical_json_bytes(request.template.input_identity)
            ).hexdigest(),
            "source_manifest_sha256": request.source_tree.source_manifest_sha256,
            "source_runs": [
                {
                    "artifact_id": item.artifact_id,
                    "manifest_sha256": item.manifest_sha256,
                    "ordinal": item.ordinal,
                    "run_id": item.run_id,
                }
                for item in request.source_runs
            ],
            "workload_id": request.template.workload_id,
        }
    )


def _provenance(request: ReleaseAuxiliaryExecutionV1) -> dict[str, object]:
    sources = request.source_tree.by_path()
    return {
        "artifact_manifest_sha256": request.artifact_manifest_sha256,
        "asset_manifest_sha256": request.asset_manifest_sha256,
        "candidate_commit": request.candidate_commit,
        "entrypoint_sources": [
            {"path": path, "sha256": sources[path]}
            for path in request.template.entrypoint_paths
        ],
        "input_identity_sha256": hashlib.sha256(
            canonical_json_bytes(request.template.input_identity)
        ).hexdigest(),
        "source_manifest_sha256": request.source_tree.source_manifest_sha256,
    }


def _reduce(values: tuple[int, ...], statistic: object) -> int:
    if statistic == "MAX":
        return max(values)
    percentile = {"P50": 500_000, "P95": 950_000, "P99": 990_000}.get(
        statistic
    )
    if percentile is None:
        raise ValueError("auxiliary reduction statistic is unsupported")
    return nearest_rank(values, percentile)


def _evaluate(
    workload_id: str,
    measured: dict[str, tuple[int, ...]],
    hard_failure_codes: tuple[str, ...],
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[str, ...],
    str,
]:
    codes = set(hard_failure_codes)
    reductions: list[dict[str, object]] = []
    thresholds: list[dict[str, object]] = []
    for reduction_id in _REDUCTION_ORDER[workload_id]:
        protocol = _THRESHOLD_BY_ID[reduction_id]
        metric_id = protocol["metric_id"]
        assert type(metric_id) is str
        values = measured[metric_id]
        complete = len(values) == _EXPECTED_COUNTS[workload_id][1][metric_id]
        value = _reduce(values, protocol["statistic"]) if complete else None
        availability = "AVAILABLE" if complete else "UNAVAILABLE"
        reductions.append(
            {
                "availability": availability,
                "metric_id": metric_id,
                "reduction_id": reduction_id,
                "statistic": protocol["statistic"],
                "unit": _METRIC_UNITS[metric_id],
                "value": value,
            }
        )
        reason_code: str | None
        if not complete:
            threshold_status = "NOT_EVALUATED"
            reason_code = (
                "UPSTREAM_HARD_FAILURE" if codes else "INCOMPLETE_SAMPLES"
            )
        else:
            assert type(value) is int
            if value <= protocol["pass_upper_inclusive"]:  # type: ignore[operator]
                threshold_status, reason_code = "PASS", None
            elif value <= protocol["warning_upper_inclusive"]:  # type: ignore[operator]
                threshold_status, reason_code = "WARNING", None
            elif reduction_id == "TERMINAL_UPDATE_MAX":
                threshold_status = "FAIL"
                reason_code = "TERMINAL_UPDATE_PAUSE_OVER_500_MS"
                codes.add(reason_code)
            else:
                threshold_status, reason_code = "FAIL", "THRESHOLD_MISS"
        thresholds.append(
            {
                "hard_failure": protocol["hard_failure"],
                "metric_id": metric_id,
                "pass_upper_inclusive": protocol["pass_upper_inclusive"],
                "reason_code": reason_code,
                "reduction_id": reduction_id,
                "statistic": protocol["statistic"],
                "status": threshold_status,
                "warning_upper_inclusive": protocol[
                    "warning_upper_inclusive"
                ],
            }
        )
    statuses = tuple(item["status"] for item in thresholds)
    status = (
        "FAIL"
        if codes or any(item in {"FAIL", "NOT_EVALUATED"} for item in statuses)
        else ("WARNING" if "WARNING" in statuses else "PASS")
    )
    ordered_codes = tuple(sorted(codes, key=lambda item: item.encode("utf-8")))
    return tuple(reductions), tuple(thresholds), ordered_codes, status


def _evidence_records(payloads: Mapping[str, bytes]) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for evidence_id in sorted(payloads, key=lambda item: item.encode("utf-8")):
        normalized = _evidence_id(evidence_id)
        payload = payloads[evidence_id]
        if type(payload) is not bytes:
            raise TypeError("auxiliary evidence payload must be exact bytes")
        rows.append(
            {
                "evidence_id": normalized,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    if len(rows) != len(payloads):
        raise ValueError("auxiliary evidence IDs collide after normalization")
    return tuple(rows)


def _publish_evidence(evidence_root: Path, payloads: Mapping[str, bytes]) -> None:
    for evidence_id in sorted(payloads, key=lambda item: item.encode("utf-8")):
        normalized = _evidence_id(evidence_id)
        target = evidence_root.joinpath(*PurePosixPath(normalized).parts)
        _write_exclusive(target, payloads[evidence_id])


def _series_evidence(
    workload_id: str,
    warmup: Mapping[str, tuple[int, ...]],
    measured: Mapping[str, tuple[int, ...]],
) -> bytes:
    return canonical_json_bytes(
        {
            "measured_series": {
                metric_id: list(values) for metric_id, values in measured.items()
            },
            "schema_version": 1,
            "warmup_series": {
                metric_id: list(values) for metric_id, values in warmup.items()
            },
            "workload_id": workload_id,
        }
    )


def _evidence_value(
    evidence_payloads: Mapping[str, bytes], evidence_id: str, label: str
) -> object:
    try:
        raw = evidence_payloads[evidence_id]
    except KeyError as error:
        raise ValueError(f"auxiliary {label} evidence is missing") from error
    if type(raw) is not bytes:
        raise TypeError(f"auxiliary {label} evidence must be exact bytes")
    return load_canonical_json_bytes(raw, f"auxiliary {label} evidence")


def _series_object(value: object, label: str) -> dict[str, tuple[int, ...]]:
    if type(value) is not dict:
        raise TypeError(f"auxiliary {label} must be an object")
    output: dict[str, tuple[int, ...]] = {}
    for metric_id, raw in value.items():
        if type(metric_id) is not str or type(raw) is not list:
            raise TypeError(f"auxiliary {label} entries are invalid")
        values = tuple(
            _nonnegative_integer(item, f"auxiliary {label} sample") for item in raw
        )
        output[metric_id] = values
    return output


def _decode_series_evidence(
    workload_id: str, evidence_payloads: Mapping[str, bytes]
) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
    row = _exact_object(
        _evidence_value(evidence_payloads, "series.json", "series"),
        {"measured_series", "schema_version", "warmup_series", "workload_id"},
        "auxiliary series evidence",
    )
    if row["schema_version"] != 1 or row["workload_id"] != workload_id:
        raise ValueError("auxiliary series evidence identity differs")
    warmup = _series_object(row["warmup_series"], "warmup evidence series")
    measured = _series_object(row["measured_series"], "measured evidence series")
    expected_warmup, expected_measured = _EXPECTED_COUNTS[workload_id]
    if set(warmup) != set(expected_warmup) or set(measured) != set(
        expected_measured
    ):
        raise ValueError("auxiliary evidence series metric inventory differs")
    if any(len(warmup[key]) > count for key, count in expected_warmup.items()) or any(
        len(measured[key]) > count for key, count in expected_measured.items()
    ):
        raise ValueError("auxiliary evidence series count exceeds the frozen contract")
    return warmup, measured


def _verify_ack_evidence(
    request: ReleaseAuxiliaryExecutionV1,
    evidence_payloads: Mapping[str, bytes],
    warmup: Mapping[str, tuple[int, ...]],
    measured: Mapping[str, tuple[int, ...]],
) -> None:
    rows = _evidence_value(
        evidence_payloads,
        "interactive-ack/acknowledgements.json",
        "interactive acknowledgements",
    )
    if type(rows) is not list or len(rows) != 1100:
        raise ValueError("interactive acknowledgement evidence count differs")
    latencies: list[int] = []
    for ordinal, raw in enumerate(rows):
        row = _exact_object(
            raw,
            {"ack_ordinal", "action", "latency_ns", "order_id", "pair_ordinal"},
            "interactive acknowledgement evidence row",
        )
        pair_ordinal = ordinal // 2
        if (
            row["ack_ordinal"] != ordinal
            or row["pair_ordinal"] != pair_ordinal
            or row["action"] != ("SUBMIT" if ordinal % 2 == 0 else "CANCEL")
            or row["order_id"] != f"perf-{pair_ordinal:04d}"
        ):
            raise ValueError("interactive acknowledgement evidence ordering differs")
        latencies.append(
            _nonnegative_integer(row["latency_ns"], "interactive latency")
        )
    receipt = _evidence_value(
        evidence_payloads, "interactive-ack/receipt.json", "interactive receipt"
    )
    if type(receipt) is not dict:
        raise TypeError("interactive receipt evidence must be an object")
    parameters = request.template.input_identity["parameters"]
    assert type(parameters) is dict
    acknowledgement_raw = evidence_payloads[
        "interactive-ack/acknowledgements.json"
    ]
    if (
        receipt.get("status") != "PASS"
        or receipt.get("lesson_id") != parameters["lesson_id"]
        or receipt.get("acknowledgement_count") != 1100
        or receipt.get("acknowledgement_inventory_sha256")
        != hashlib.sha256(acknowledgement_raw).hexdigest()
        or warmup["ack_latency_ns"] != tuple(latencies[:100])
        or measured["ack_latency_ns"] != tuple(latencies[100:])
        or measured["peak_rss_bytes"]
        != (
            _nonnegative_integer(
                receipt.get("peak_rss_bytes"), "interactive peak RSS"
            ),
        )
    ):
        raise ValueError("interactive evidence does not reconcile to its series")


def _verify_terminal_evidence(
    request: ReleaseAuxiliaryExecutionV1,
    evidence_payloads: Mapping[str, bytes],
    warmup: Mapping[str, tuple[int, ...]],
    measured: Mapping[str, tuple[int, ...]],
) -> None:
    rows = _evidence_value(
        evidence_payloads,
        "terminal-update/update-inventory.json",
        "terminal update inventory",
    )
    if type(rows) is not list or len(rows) != 5100:
        raise ValueError("terminal update evidence count differs")
    latencies: list[int] = []
    sequences: list[int] = []
    for ordinal, raw in enumerate(rows):
        row = _exact_object(
            raw,
            {
                "frame_bytes",
                "latency_ns",
                "market_state_id",
                "message_sequence",
                "ordinal",
            },
            "terminal update evidence row",
        )
        if row["ordinal"] != ordinal:
            raise ValueError("terminal update evidence ordinals differ")
        _nonnegative_integer(row["frame_bytes"], "terminal frame bytes")
        require_sha256(row["market_state_id"], "terminal market-state digest")
        latencies.append(
            _nonnegative_integer(row["latency_ns"], "terminal update latency")
        )
        sequences.append(
            _nonnegative_integer(row["message_sequence"], "terminal sequence")
        )
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ValueError("terminal update evidence sequences differ")
    receipt = _evidence_value(
        evidence_payloads, "terminal-update/receipt.json", "terminal receipt"
    )
    if type(receipt) is not dict:
        raise TypeError("terminal receipt evidence must be an object")
    parameters = request.template.input_identity["parameters"]
    assert type(parameters) is dict
    inventory_raw = evidence_payloads["terminal-update/update-inventory.json"]
    source_materialization = receipt.get("source_materialization")
    source_verification = (
        None
        if type(source_materialization) is not dict
        else source_materialization.get("verification")
    )
    if (
        receipt.get("status") != "PASS"
        or receipt.get("rendered_update_count") != 5100
        or receipt.get("first_message_sequence") != sequences[0]
        or receipt.get("last_message_sequence") != sequences[-1]
        or receipt.get("source_evidence_sha256")
        != parameters["source_artifact_manifest_sha256"]
        or type(source_materialization) is not dict
        or source_materialization.get("candidate_id") != "QUIET_RANGE_PRESSURE"
        or source_materialization.get("root_seed") != 3_102_000
        or source_materialization.get("run_id") != receipt.get("run_id")
        or source_materialization.get("evidence_sha256")
        != parameters["source_artifact_manifest_sha256"]
        or type(source_verification) is not dict
        or source_verification.get("status") != "PASS"
        or receipt.get("update_inventory_sha256")
        != hashlib.sha256(inventory_raw).hexdigest()
        or warmup["update_latency_ns"] != tuple(latencies[:100])
        or measured["update_latency_ns"] != tuple(latencies[100:])
        or measured["peak_rss_bytes"]
        != (
            _nonnegative_integer(receipt.get("peak_rss_bytes"), "terminal peak RSS"),
        )
    ):
        raise ValueError("terminal evidence does not reconcile to its series")


def _attempt_rows(
    evidence_payloads: Mapping[str, bytes], evidence_id: str, count: int
) -> list[dict[str, object]]:
    value = _evidence_value(evidence_payloads, evidence_id, "attempt inventory")
    if type(value) is not list or len(value) != count or any(
        type(item) is not dict for item in value
    ):
        raise ValueError("auxiliary attempt evidence count or type differs")
    rows = value
    assert all(type(item) is dict for item in rows)
    if any(
        item.get("status") != "PASS" or item.get("ordinal", ordinal) != ordinal
        for ordinal, item in enumerate(rows)
    ):
        raise ValueError("auxiliary attempt evidence ordering or status differs")
    return rows  # type: ignore[return-value]


def _attempt_metric(
    rows: Sequence[Mapping[str, object]], metric_id: str
) -> tuple[int, ...]:
    return tuple(
        _nonnegative_integer(item.get(metric_id), f"auxiliary {metric_id} evidence")
        for item in rows
    )


def _verify_fresh_attempt_evidence(
    request: ReleaseAuxiliaryExecutionV1,
    evidence_payloads: Mapping[str, bytes],
    warmup: Mapping[str, tuple[int, ...]],
    measured: Mapping[str, tuple[int, ...]],
) -> None:
    workload_id = request.template.workload_id
    if workload_id == "RELEASE_FULL_DAY_GENERATION_V1":
        rows = _attempt_rows(
            evidence_payloads, "full-day-generation/attempts.json", 4
        )
        measured_rows = rows[1:]
        for metric_id in measured:
            if measured[metric_id] != _attempt_metric(measured_rows, metric_id):
                raise ValueError("generation attempt evidence series differs")
        if (
            warmup["peak_rss_bytes"] != _attempt_metric(rows[:1], "peak_rss_bytes")
            or warmup["wall_time_ns"] != _attempt_metric(rows[:1], "wall_time_ns")
        ):
            raise ValueError("generation warmup evidence series differs")
        sources = _evidence_value(
            evidence_payloads,
            "full-day-generation/sources.json",
            "generation sources",
        )
        if type(sources) is not dict or sources.get("status") != "PASS":
            raise ValueError("generation source evidence status differs")
        source_rows = sources.get("sources")
        if type(source_rows) is not list or len(source_rows) != 4:
            raise ValueError("generation source evidence count differs")
        for ordinal, (source, attempt) in enumerate(zip(source_rows, rows, strict=True)):
            if type(source) is not dict or (
                source.get("ordinal") != ordinal
                or source.get("run_id") != attempt.get("run_id")
                or source.get("manifest_sha256") != attempt.get("manifest_sha256")
            ):
                raise ValueError("generation source evidence differs from attempts")
        return
    if workload_id == "RELEASE_FULL_DAY_REPLAY_V1":
        rows = _attempt_rows(evidence_payloads, "full-day-replay/attempts.json", 4)
    else:
        rows = _attempt_rows(evidence_payloads, "microscope-load/attempts.json", 21)
    measured_rows = rows[1:]
    if (
        warmup["peak_rss_bytes"] != _attempt_metric(rows[:1], "peak_rss_bytes")
        or warmup["wall_time_ns"] != _attempt_metric(rows[:1], "wall_time_ns")
        or measured["peak_rss_bytes"]
        != _attempt_metric(measured_rows, "peak_rss_bytes")
        or measured["wall_time_ns"] != _attempt_metric(measured_rows, "wall_time_ns")
    ):
        raise ValueError("fresh attempt evidence series differs")
    if workload_id == "RELEASE_MICROSCOPE_LOAD_V1":
        inventories = tuple(item.get("asset_inventory") for item in rows)
        if any(item != inventories[0] for item in inventories[1:]):
            raise ValueError("microscope attempt asset inventories differ")
        inventory = inventories[0]
        if type(inventory) is not list or len(inventory) != len(
            _MICROSCOPE_ASSET_NAMES
        ):
            raise ValueError("microscope asset inventory count differs")
        expected_asset_evidence = tuple(
            f"microscope-load/installed-assets/{name}"
            for name in _MICROSCOPE_ASSET_NAMES
        )
        actual_asset_evidence = tuple(
            sorted(
                (
                    evidence_id
                    for evidence_id in evidence_payloads
                    if evidence_id.startswith("microscope-load/installed-assets/")
                ),
                key=lambda item: item.encode("utf-8"),
            )
        )
        if actual_asset_evidence != expected_asset_evidence:
            raise ValueError("microscope installed-asset evidence inventory differs")
        manifest_assets: list[dict[str, object]] = []
        for name, raw in zip(_MICROSCOPE_ASSET_NAMES, inventory, strict=True):
            asset = _exact_object(
                raw,
                {"license_id", "name", "sha256", "size"},
                "microscope asset evidence",
            )
            if (
                asset["name"] != name
                or asset["license_id"] != "KIRBY2_PROJECT_LICENSE"
            ):
                raise ValueError("microscope asset identity differs")
            require_sha256(asset["sha256"], "microscope asset digest")
            size = _nonnegative_integer(asset["size"], "microscope asset size")
            if size == 0:
                raise ValueError("microscope asset is empty")
            report_asset = evidence_payloads.get(
                f"microscope-load/installed-assets/{name}"
            )
            if (
                type(report_asset) is not bytes
                or len(report_asset) != size
                or hashlib.sha256(report_asset).hexdigest() != asset["sha256"]
            ):
                raise ValueError("microscope report asset bytes differ")
            manifest_assets.append(
                {
                    "path": f"assets/microscope/{name}",
                    "sha256": asset["sha256"],
                    "size": size,
                }
            )
        if hashlib.sha256(canonical_json_bytes(manifest_assets)).hexdigest() != (
            request.asset_manifest_sha256
        ):
            raise ValueError("microscope assets differ from the release manifest")


def verify_auxiliary_performance_evidence(
    request: ReleaseAuxiliaryExecutionV1,
    result: ReleaseAuxiliaryPerformanceResultV1,
    evidence_payloads: Mapping[str, bytes],
) -> ReleaseAuxiliaryPerformanceResultV1:
    """Purely reconcile result arithmetic with the exact published evidence bytes."""

    if type(request) is not ReleaseAuxiliaryExecutionV1:
        raise TypeError("auxiliary evidence verification requires a typed request")
    if type(result) is not ReleaseAuxiliaryPerformanceResultV1:
        raise TypeError("auxiliary evidence verification requires a typed result")
    if not isinstance(evidence_payloads, Mapping):
        raise TypeError("auxiliary evidence payloads must be a mapping")
    copied = dict(evidence_payloads)
    if any(type(key) is not str or type(value) is not bytes for key, value in copied.items()):
        raise TypeError("auxiliary evidence payload mapping is invalid")
    expected_records = _evidence_records(copied)
    if expected_records != result.evidence_records:
        raise ValueError("auxiliary evidence bytes differ from the result inventory")
    if copied.get("execution-envelope.json") != _execution_evidence(request):
        raise ValueError("auxiliary execution evidence differs from its request")
    result.validate_protocol_binding(
        request.template,
        request.source_tree,
        artifact_manifest_sha256=request.artifact_manifest_sha256,
        asset_manifest_sha256=request.asset_manifest_sha256,
    )
    if result.provenance["candidate_commit"] != request.candidate_commit:
        raise ValueError("auxiliary result candidate differs from its execution")
    workload_id = request.template.workload_id
    warmup, measured = _decode_series_evidence(workload_id, copied)
    failure = copied.get("failure.json")
    initial_codes: tuple[str, ...] = ()
    if failure is not None:
        row = _exact_object(
            load_canonical_json_bytes(failure, "auxiliary failure evidence"),
            {"error", "error_type", "failure_code", "workload_id"},
            "auxiliary failure evidence",
        )
        expected_code = _WORKLOAD_FAILURE_CODES[workload_id]
        if row["workload_id"] != workload_id or row["failure_code"] != expected_code:
            raise ValueError("auxiliary failure evidence identity differs")
        if any(warmup.values()) or any(measured.values()):
            raise ValueError("failed auxiliary evidence carries measurement samples")
        initial_codes = (expected_code,)
    elif workload_id == "RELEASE_INTERACTIVE_ACK_V1":
        _verify_ack_evidence(request, copied, warmup, measured)
    elif workload_id == "RELEASE_TERMINAL_UPDATE_V1":
        _verify_terminal_evidence(request, copied, warmup, measured)
    else:
        _verify_fresh_attempt_evidence(request, copied, warmup, measured)
    reductions, thresholds, codes, status = _evaluate(
        workload_id, measured, initial_codes
    )
    if (
        result.warmup_series != warmup
        or result.measured_series != measured
        or result.reductions != reductions
        or result.threshold_results != thresholds
        or result.hard_failure_codes != codes
        or result.status != status
    ):
        raise ValueError("auxiliary result does not reconcile to evidence bytes")
    return result


def execute_auxiliary_performance_workload(
    request: ReleaseAuxiliaryExecutionV1,
    output_root: Path,
) -> ReleaseAuxiliaryPerformanceResultV1:
    """Execute and immutably publish exactly one frozen auxiliary workload."""

    if type(request) is not ReleaseAuxiliaryExecutionV1:
        raise TypeError("auxiliary execution requires ReleaseAuxiliaryExecutionV1")
    if platform.system() != "Darwin":
        raise RuntimeError("WO40-I auxiliary execution requires the Darwin target")
    evidence_root, workspace = _prepare_output_root(output_root)
    workload_id = request.template.workload_id
    warmup: dict[str, tuple[int, ...]] = {
        metric_id: () for metric_id in _EXPECTED_COUNTS[workload_id][0]
    }
    measured: dict[str, tuple[int, ...]] = {
        metric_id: () for metric_id in _EXPECTED_COUNTS[workload_id][1]
    }
    evidence_payloads: dict[str, bytes] = {
        "execution-envelope.json": _execution_evidence(request),
    }
    hard_failure_codes: tuple[str, ...] = ()
    try:
        handler = _WORKLOAD_HANDLERS[workload_id]
        if not callable(handler):  # pragma: no cover - closed registry invariant
            raise RuntimeError("auxiliary workload handler is unavailable")
        actual_warmup, actual_measured, workload_evidence = handler(
            request, workspace
        )
        warmup = dict(actual_warmup)
        measured = dict(actual_measured)
        evidence_payloads.update(workload_evidence)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, TimeoutError) as error:
        hard_failure_codes = (_WORKLOAD_FAILURE_CODES[workload_id],)
        evidence_payloads["failure.json"] = canonical_json_bytes(
            {
                "error": str(error)[:4096],
                "error_type": type(error).__name__,
                "failure_code": hard_failure_codes[0],
                "workload_id": workload_id,
            }
        )
        warmup = {
            metric_id: tuple(warmup.get(metric_id, ()))
            for metric_id in _EXPECTED_COUNTS[workload_id][0]
        }
        measured = {
            metric_id: tuple(measured.get(metric_id, ()))
            for metric_id in _EXPECTED_COUNTS[workload_id][1]
        }
    reductions, thresholds, hard_failure_codes, status = _evaluate(
        workload_id,
        measured,
        hard_failure_codes,
    )
    evidence_payloads["series.json"] = _series_evidence(
        workload_id, warmup, measured
    )
    records = _evidence_records(evidence_payloads)
    result = ReleaseAuxiliaryPerformanceResultV1(
        workload_id=workload_id,
        status=status,
        provenance=_provenance(request),
        warmup_series=warmup,
        measured_series=measured,
        reductions=reductions,
        threshold_results=thresholds,
        hard_failure_codes=hard_failure_codes,
        evidence_records=records,
    )
    _publish_evidence(evidence_root, evidence_payloads)
    _write_exclusive(output_root / _RESULT_NAME, result.canonical_bytes())
    return verify_auxiliary_performance_execution(request, output_root, result)


def verify_auxiliary_performance_execution(
    request: ReleaseAuxiliaryExecutionV1,
    output_root: Path,
    result: ReleaseAuxiliaryPerformanceResultV1,
) -> ReleaseAuxiliaryPerformanceResultV1:
    """Verify an immutable auxiliary publication without rerunning its workload."""

    if type(request) is not ReleaseAuxiliaryExecutionV1:
        raise TypeError("auxiliary verification requires a typed execution envelope")
    if type(result) is not ReleaseAuxiliaryPerformanceResultV1:
        raise TypeError("auxiliary verification requires a typed result")
    if type(output_root) is not Path or not output_root.is_absolute():
        raise ValueError("auxiliary verification root must be an absolute Path")
    root = output_root.resolve(strict=True)
    if root != output_root or root.is_symlink() or not root.is_dir():
        raise ValueError("auxiliary verification root is not a plain resolved directory")
    result.validate_protocol_binding(
        request.template,
        request.source_tree,
        artifact_manifest_sha256=request.artifact_manifest_sha256,
        asset_manifest_sha256=request.asset_manifest_sha256,
    )
    if result.provenance["candidate_commit"] != request.candidate_commit:
        raise ValueError("auxiliary result candidate differs from its execution")
    result_path = root / _RESULT_NAME
    if (
        not result_path.is_file()
        or result_path.is_symlink()
        or result_path.read_bytes() != result.canonical_bytes()
    ):
        raise ValueError("auxiliary canonical result bytes differ")
    reparsed = ReleaseAuxiliaryPerformanceResultV1.from_bytes(
        result_path.read_bytes()
    )
    evidence_root = root / _EVIDENCE_DIRECTORY_NAME
    if not evidence_root.is_dir() or evidence_root.is_symlink():
        raise ValueError("auxiliary evidence root is unavailable")
    expected = {item["evidence_id"]: item for item in result.evidence_records}
    actual: set[str] = set()
    for path in evidence_root.rglob("*"):
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("auxiliary evidence contains a symlink")
        if stat.S_ISREG(metadata.st_mode):
            actual.add(path.relative_to(evidence_root).as_posix())
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("auxiliary evidence contains a special node")
    if actual != set(expected):
        raise ValueError("auxiliary evidence inventory differs from the result")
    evidence_payloads: dict[str, bytes] = {}
    for evidence_id, record in expected.items():
        path = evidence_root.joinpath(*PurePosixPath(evidence_id).parts)
        raw = path.read_bytes()
        if (
            len(raw) != record["size"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
        ):
            raise ValueError(f"auxiliary evidence digest differs: {evidence_id}")
        evidence_payloads[evidence_id] = raw
    verify_auxiliary_performance_evidence(request, result, evidence_payloads)
    for source in request.source_runs:
        _source_store(source)
    if reparsed.canonical_bytes() != result.canonical_bytes():
        raise RuntimeError("auxiliary typed result changed during canonical reparse")
    return reparsed


def _absolute_cli_path(value: str) -> Path:
    try:
        return _absolute_path(value, "auxiliary CLI path")
    except (OSError, TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one canonical request into one new explicit output root."""

    if isinstance(argv, (str, bytes)):
        raise TypeError("auxiliary arguments must be a sequence of strings")
    parser = argparse.ArgumentParser(
        prog="python -m kirby2.release.performance_auxiliary",
        allow_abbrev=False,
    )
    parser.add_argument("--request", required=True, type=_absolute_cli_path)
    parser.add_argument("--output-root", required=True, type=_absolute_cli_path)
    arguments = parser.parse_args(None if argv is None else list(argv))
    request_path: Path = arguments.request
    if not request_path.is_file() or request_path.is_symlink():
        raise ValueError("auxiliary request path is not a plain file")
    request = ReleaseAuxiliaryExecutionV1.from_bytes(request_path.read_bytes())
    result = execute_auxiliary_performance_workload(
        request,
        arguments.output_root,
    )
    sys.stdout.buffer.write(result.canonical_bytes() + b"\n")
    return 0 if result.status in {"PASS", "WARNING"} else 1


if __name__ == "__main__":  # pragma: no cover - installed module entrypoint
    raise SystemExit(main())


__all__ = [
    "AUXILIARY_EXECUTION_POLICY_V1",
    "ReleaseAuxiliaryExecutionV1",
    "ReleaseAuxiliarySourceRunV1",
    "execute_auxiliary_performance_workload",
    "main",
    "verify_auxiliary_performance_evidence",
    "verify_auxiliary_performance_execution",
]
