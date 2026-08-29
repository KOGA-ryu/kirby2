"""Deterministic blinded-review selection for full-day qualification evidence.

The selector consumes only already-persisted observable windows.  Its dedicated
review root never enters a generation stream, and its output deliberately omits
candidate, seed, event, control, future, and truth fields.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, Sequence

from kirby2.immutable import freeze_json, thaw_json

from .models import canonical_json_bytes, canonical_sha256, validate_strict_json
from .profiles import (
    CANDIDATE_IDS,
    DISPLAY_LABELS,
    EVENT_SHOCK_PRESSURE,
    FULL_DAY_PROFILE_POLICY_VERSION,
    NOT_APPLICABLE,
    POLICY_SCALE_PPM,
    PROFILE_CANDIDATES_MANIFEST_SHA256,
    PROFILE_ENVELOPES_MANIFEST_SHA256,
    REVIEW_SELECTION_LABEL,
    REVIEW_SELECTION_ROOT,
    normalized_boundary_time_us,
    policy_tie_digest,
)


REVIEW_SELECTION_SCHEMA_VERSION: Final = 1
REVIEW_PACKET_SCHEMA_VERSION: Final = 1
REVIEWER_SIDECAR_SCHEMA_VERSION: Final = 1
REVIEW_SELECTION_POLICY_VERSION: Final = "FULL_DAY_REVIEW_SELECTION_V1"
REVIEW_WINDOW_DURATION_US: Final = 60_000_000
REVIEW_START_STEP_US: Final = 1_000_000
REVIEW_IOU_MAX_PPM: Final = 500_000
REVIEW_WINDOWS_PER_STRATUM: Final = 2
REVIEW_STRATA: Final = (
    "opening",
    "ordinary_morning",
    "midday",
    "event_post_event",
    "ordinary_afternoon",
    "close",
)
BLIND_FIELDS: Final = (
    "CANDIDATE_ID",
    "EVENT_TYPE",
    "FUTURE_OUTCOME",
    "PRESSURE_CONTROLS",
    "ROOT_SEED",
    "TRUTH",
)
HUMAN_REVIEW_STATUSES: Final = (
    "ACCEPTED",
    "REJECTED",
    "NEEDS_EDIT",
    "SUPERSEDED",
)
RUBRIC_OUTCOMES: Final = (
    "PLAUSIBLE",
    "QUESTIONABLE",
    "IMPLAUSIBLE",
    "INSUFFICIENT_EVIDENCE",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class ObservableSampleV1:
    """One public-feed observation retained in a blinded packet."""

    simulation_time_us: int
    phase_relative_time_us: int
    observable_feed: Mapping[str, object]

    def __post_init__(self) -> None:
        _exact_int(self.simulation_time_us, "simulation_time_us")
        _exact_int(self.phase_relative_time_us, "phase_relative_time_us")
        frozen = freeze_json(self.observable_feed)
        if not isinstance(frozen, Mapping):
            raise TypeError("observable_feed must be a mapping")
        validate_strict_json(frozen)
        object.__setattr__(self, "observable_feed", frozen)

    def as_dict(self) -> dict[str, object]:
        return {
            "observable_feed": thaw_json(self.observable_feed),
            "phase_relative_time_us": self.phase_relative_time_us,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class ReviewRunV1:
    """Private selector input for one completed immutable full day."""

    candidate_id: str
    root_seed: int
    run_digest: str
    session_start_us: int
    session_end_us: int
    continuous_start_us: int
    continuous_end_us: int
    phase_boundaries_us: tuple[int, ...]
    halt_intervals_us: tuple[tuple[int, int], ...]
    event_time_us: int | None
    samples: tuple[ObservableSampleV1, ...]

    def __post_init__(self) -> None:
        if self.candidate_id not in CANDIDATE_IDS and not self.candidate_id.startswith(
            "DEV_ONLY_"
        ):
            raise ValueError("review candidate identity is unknown")
        _exact_int(self.root_seed, "root_seed")
        _sha256(self.run_digest, "run_digest")
        bounds = (
            _exact_int(self.session_start_us, "session_start_us"),
            _exact_int(self.session_end_us, "session_end_us"),
            _exact_int(self.continuous_start_us, "continuous_start_us"),
            _exact_int(self.continuous_end_us, "continuous_end_us"),
        )
        if not bounds[0] <= bounds[2] < bounds[3] <= bounds[1]:
            raise ValueError("review session/continuous bounds are invalid")
        if self.phase_boundaries_us != tuple(sorted(set(self.phase_boundaries_us))):
            raise ValueError("phase boundaries must be unique and sorted")
        if any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not int
            or not self.session_start_us <= item[0] < item[1] <= self.session_end_us
            for item in self.halt_intervals_us
        ):
            raise ValueError("halt intervals are invalid")
        if self.event_time_us is not None and (
            type(self.event_time_us) is not int
            or not self.session_start_us <= self.event_time_us < self.session_end_us
        ):
            raise ValueError("review event time lies outside the session")
        if type(self.samples) is not tuple or any(
            type(item) is not ObservableSampleV1 for item in self.samples
        ):
            raise TypeError("review samples must be typed")
        times = tuple(item.simulation_time_us for item in self.samples)
        if times != tuple(sorted(times)) or len(times) != len(set(times)):
            raise ValueError("observable samples must have unique ascending times")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "continuous_end_us": self.continuous_end_us,
            "continuous_start_us": self.continuous_start_us,
            "event_time_us": self.event_time_us,
            "halt_intervals_us": [list(item) for item in self.halt_intervals_us],
            "phase_boundaries_us": list(self.phase_boundaries_us),
            "root_seed": self.root_seed,
            "run_digest": self.run_digest,
            "samples": [sample.as_dict() for sample in self.samples],
            "session_end_us": self.session_end_us,
            "session_start_us": self.session_start_us,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ReviewRunV1:
        expected = {
            "candidate_id",
            "continuous_end_us",
            "continuous_start_us",
            "event_time_us",
            "halt_intervals_us",
            "phase_boundaries_us",
            "root_seed",
            "run_digest",
            "samples",
            "session_end_us",
            "session_start_us",
        }
        if set(payload) != expected:
            raise ValueError("review-run fields differ")
        raw_boundaries = payload["phase_boundaries_us"]
        raw_halts = payload["halt_intervals_us"]
        raw_samples = payload["samples"]
        if type(raw_boundaries) is not list or any(
            type(item) is not int for item in raw_boundaries
        ):
            raise TypeError("review phase boundaries must be an integer array")
        if type(raw_halts) is not list or any(
            type(item) is not list
            or len(item) != 2
            or any(type(value) is not int for value in item)
            for item in raw_halts
        ):
            raise TypeError("review halt intervals must be pairs of integers")
        if type(raw_samples) is not list or any(
            not isinstance(item, Mapping) for item in raw_samples
        ):
            raise TypeError("review samples must be an array of objects")
        samples: list[ObservableSampleV1] = []
        for raw in raw_samples:
            if set(raw) != {
                "observable_feed",
                "phase_relative_time_us",
                "simulation_time_us",
            } or not isinstance(raw["observable_feed"], Mapping):
                raise ValueError("observable sample fields differ")
            samples.append(
                ObservableSampleV1(
                    simulation_time_us=_exact_int(
                        raw["simulation_time_us"], "simulation_time_us"
                    ),
                    phase_relative_time_us=_exact_int(
                        raw["phase_relative_time_us"], "phase_relative_time_us"
                    ),
                    observable_feed=dict(raw["observable_feed"]),
                )
            )
        event_time = payload["event_time_us"]
        if event_time is not None and type(event_time) is not int:
            raise TypeError("review event time must be an integer or absent")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            root_seed=_exact_int(payload["root_seed"], "root_seed"),
            run_digest=str(payload["run_digest"]),
            session_start_us=_exact_int(
                payload["session_start_us"], "session_start_us"
            ),
            session_end_us=_exact_int(payload["session_end_us"], "session_end_us"),
            continuous_start_us=_exact_int(
                payload["continuous_start_us"], "continuous_start_us"
            ),
            continuous_end_us=_exact_int(
                payload["continuous_end_us"], "continuous_end_us"
            ),
            phase_boundaries_us=tuple(raw_boundaries),
            halt_intervals_us=tuple((item[0], item[1]) for item in raw_halts),
            event_time_us=event_time,
            samples=tuple(samples),
        )


@dataclass(frozen=True, slots=True)
class EligibleWindowV1:
    run_digest: str
    candidate_id: str
    root_seed: int
    stratum: str
    start_us: int
    end_us: int
    selection_context: str
    selection_digest: str
    observable_window_sha256: str
    samples: tuple[ObservableSampleV1, ...]

    def __post_init__(self) -> None:
        _sha256(self.run_digest, "run_digest")
        if self.stratum not in REVIEW_STRATA:
            raise ValueError("unknown review stratum")
        if self.end_us - self.start_us != REVIEW_WINDOW_DURATION_US:
            raise ValueError("review window duration differs from policy")
        _sha256(self.selection_digest, "selection_digest")
        _sha256(self.observable_window_sha256, "observable_window_sha256")


@dataclass(frozen=True, slots=True)
class SelectedWindowManifestV1:
    run_digest: str
    stratum: str
    start_us: int | None
    end_us: int | None
    selection_context: str | None
    selection_digest: str | None
    observable_window_sha256: str | None
    shortfall_status: str
    blind_fields: tuple[str, ...] = BLIND_FIELDS
    schema_version: int = REVIEW_SELECTION_SCHEMA_VERSION
    selection_policy_version: str = REVIEW_SELECTION_POLICY_VERSION
    profile_candidates_manifest_sha256: str = PROFILE_CANDIDATES_MANIFEST_SHA256
    profile_envelopes_manifest_sha256: str = PROFILE_ENVELOPES_MANIFEST_SHA256

    def __post_init__(self) -> None:
        _sha256(self.run_digest, "run_digest")
        if self.schema_version != REVIEW_SELECTION_SCHEMA_VERSION:
            raise ValueError("selected-window schema version must be 1")
        if self.selection_policy_version != REVIEW_SELECTION_POLICY_VERSION:
            raise ValueError("selected-window policy version differs")
        if (
            self.profile_candidates_manifest_sha256
            != PROFILE_CANDIDATES_MANIFEST_SHA256
            or self.profile_envelopes_manifest_sha256
            != PROFILE_ENVELOPES_MANIFEST_SHA256
        ):
            raise ValueError("selected window does not bind exact WO31-H manifests")
        if self.stratum not in REVIEW_STRATA:
            raise ValueError("selected-window stratum is unknown")
        if self.blind_fields != BLIND_FIELDS:
            raise ValueError("selected-window blind fields differ from policy")
        if self.shortfall_status not in {"SELECTED", "SHORTFALL", NOT_APPLICABLE}:
            raise ValueError("selected-window shortfall status is invalid")
        optional = (
            self.start_us,
            self.end_us,
            self.selection_context,
            self.selection_digest,
            self.observable_window_sha256,
        )
        if self.shortfall_status == "SELECTED":
            if any(value is None for value in optional):
                raise ValueError("selected window is missing identity fields")
            if self.end_us - self.start_us != REVIEW_WINDOW_DURATION_US:  # type: ignore[operator]
                raise ValueError("selected window duration differs from policy")
            _sha256(self.selection_digest, "selection_digest")
            _sha256(self.observable_window_sha256, "observable_window_sha256")
            expected_selection = policy_tie_digest(
                REVIEW_SELECTION_POLICY_VERSION,
                self.selection_context,  # type: ignore[arg-type]
                REVIEW_SELECTION_ROOT,
                self.observable_window_sha256,  # type: ignore[arg-type]
            ).hex()
            if self.selection_digest != expected_selection:
                raise ValueError("selected-window review-only digest differs")
        elif any(value is not None for value in optional):
            raise ValueError("shortfall/nonapplicable window cannot carry selection")

    def as_dict(self) -> dict[str, object]:
        return {
            "blind_fields": list(self.blind_fields),
            "end_us": self.end_us,
            "observable_window_sha256": self.observable_window_sha256,
            "profile_candidates_manifest_sha256": self.profile_candidates_manifest_sha256,
            "profile_envelopes_manifest_sha256": self.profile_envelopes_manifest_sha256,
            "run_digest": self.run_digest,
            "schema_version": self.schema_version,
            "selection_context": self.selection_context,
            "selection_digest": self.selection_digest,
            "selection_policy_version": self.selection_policy_version,
            "shortfall_status": self.shortfall_status,
            "start_us": self.start_us,
            "stratum": self.stratum,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SelectedWindowManifestV1:
        expected = {
            "blind_fields",
            "end_us",
            "observable_window_sha256",
            "profile_candidates_manifest_sha256",
            "profile_envelopes_manifest_sha256",
            "run_digest",
            "schema_version",
            "selection_context",
            "selection_digest",
            "selection_policy_version",
            "shortfall_status",
            "start_us",
            "stratum",
        }
        if set(payload) != expected:
            raise ValueError("selected-window manifest fields differ")
        blind = payload["blind_fields"]
        if type(blind) is not list or any(type(item) is not str for item in blind):
            raise TypeError("selected-window blind fields must be an array")
        for field in ("start_us", "end_us"):
            if payload[field] is not None and type(payload[field]) is not int:
                raise TypeError(f"selected-window {field} must be integer or absent")
        return cls(
            run_digest=str(payload["run_digest"]),
            stratum=str(payload["stratum"]),
            start_us=payload["start_us"],  # type: ignore[arg-type]
            end_us=payload["end_us"],  # type: ignore[arg-type]
            selection_context=(
                None
                if payload["selection_context"] is None
                else str(payload["selection_context"])
            ),
            selection_digest=(
                None
                if payload["selection_digest"] is None
                else str(payload["selection_digest"])
            ),
            observable_window_sha256=(
                None
                if payload["observable_window_sha256"] is None
                else str(payload["observable_window_sha256"])
            ),
            shortfall_status=str(payload["shortfall_status"]),
            blind_fields=tuple(blind),
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
            selection_policy_version=str(payload["selection_policy_version"]),
            profile_candidates_manifest_sha256=str(
                payload["profile_candidates_manifest_sha256"]
            ),
            profile_envelopes_manifest_sha256=str(
                payload["profile_envelopes_manifest_sha256"]
            ),
        )


@dataclass(frozen=True, slots=True)
class BlindedReviewPacketV1:
    packet_id: str
    windows: tuple[Mapping[str, object], ...]
    schema_version: int = REVIEW_PACKET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REVIEW_PACKET_SCHEMA_VERSION:
            raise ValueError("review packet schema version must be 1")
        if type(self.packet_id) is not str or not self.packet_id:
            raise ValueError("review packet ID is required")
        if type(self.windows) is not tuple:
            raise TypeError("review packet windows must be immutable")
        frozen_windows: list[Mapping[str, object]] = []
        for window in self.windows:
            frozen = freeze_json(window)
            if not isinstance(frozen, Mapping):
                raise TypeError("review packet window must be a mapping")
            validate_strict_json(frozen)
            serialized = canonical_json_bytes(frozen).upper()
            if any(field.encode("ascii") in serialized for field in BLIND_FIELDS):
                raise ValueError("review packet leaks a blinded field name")
            frozen_windows.append(frozen)
        object.__setattr__(self, "windows", tuple(frozen_windows))

    def as_dict(self) -> dict[str, object]:
        return {
            "packet_id": self.packet_id,
            "schema_version": self.schema_version,
            "windows": [thaw_json(window) for window in self.windows],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> BlindedReviewPacketV1:
        if set(payload) != {"packet_id", "schema_version", "windows"}:
            raise ValueError("review packet fields differ")
        windows = payload["windows"]
        if type(windows) is not list or any(
            not isinstance(window, Mapping) for window in windows
        ):
            raise TypeError("review packet windows must be an array of objects")
        return cls(
            packet_id=str(payload["packet_id"]),
            windows=tuple(dict(window) for window in windows),
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
        )


@dataclass(frozen=True, slots=True)
class ReviewerSidecarV1:
    """Separate human authority; never changes an automated disposition."""

    packet_sha256: str
    reviewer_id: str
    human_status: str
    rubric_outcomes: tuple[tuple[str, str], ...]
    supersedes_sidecar_sha256: str | None = None
    schema_version: int = REVIEWER_SIDECAR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REVIEWER_SIDECAR_SCHEMA_VERSION:
            raise ValueError("reviewer sidecar schema version must be 1")
        _sha256(self.packet_sha256, "packet_sha256")
        if type(self.reviewer_id) is not str or not self.reviewer_id:
            raise ValueError("reviewer identity is required")
        if self.human_status not in HUMAN_REVIEW_STATUSES:
            raise ValueError("only a declared human review status is accepted")
        if self.rubric_outcomes != tuple(sorted(self.rubric_outcomes)):
            raise ValueError("rubric outcomes must use canonical key order")
        if any(outcome not in RUBRIC_OUTCOMES for _window, outcome in self.rubric_outcomes):
            raise ValueError("reviewer sidecar contains an unknown rubric outcome")
        if self.human_status == "SUPERSEDED":
            _sha256(self.supersedes_sidecar_sha256, "supersedes_sidecar_sha256")
        elif self.supersedes_sidecar_sha256 is not None:
            raise ValueError("only SUPERSEDED may bind a prior sidecar")

    def as_dict(self) -> dict[str, object]:
        return {
            "human_status": self.human_status,
            "packet_sha256": self.packet_sha256,
            "reviewer_id": self.reviewer_id,
            "rubric_outcomes": {
                window: outcome for window, outcome in self.rubric_outcomes
            },
            "schema_version": self.schema_version,
            "supersedes_sidecar_sha256": self.supersedes_sidecar_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ReviewerSidecarV1:
        expected = {
            "human_status",
            "packet_sha256",
            "reviewer_id",
            "rubric_outcomes",
            "schema_version",
            "supersedes_sidecar_sha256",
        }
        if set(payload) != expected:
            raise ValueError("reviewer-sidecar fields differ")
        outcomes = payload["rubric_outcomes"]
        if not isinstance(outcomes, Mapping) or any(
            type(key) is not str or type(value) is not str
            for key, value in outcomes.items()
        ):
            raise TypeError("reviewer rubric outcomes must be an object")
        return cls(
            packet_sha256=str(payload["packet_sha256"]),
            reviewer_id=str(payload["reviewer_id"]),
            human_status=str(payload["human_status"]),
            rubric_outcomes=tuple(sorted(outcomes.items())),  # type: ignore[arg-type]
            supersedes_sidecar_sha256=(
                None
                if payload["supersedes_sidecar_sha256"] is None
                else str(payload["supersedes_sidecar_sha256"])
            ),
            schema_version=_exact_int(payload["schema_version"], "schema_version"),
        )


class ReviewerSidecarStore:
    """Append-only human sidecars, physically separate from automated evidence."""

    def __init__(self, evidence_root: Path) -> None:
        supplied = Path(os.path.abspath(os.fspath(evidence_root)))
        if supplied.is_symlink():
            raise ValueError("reviewer-sidecar evidence root cannot be a symlink")
        self.evidence_root = supplied.resolve(strict=False)
        self.directory = self.evidence_root / "reviewer-sidecars"

    def path_for(self, sidecar: ReviewerSidecarV1) -> Path:
        return self.directory / f"review-{sidecar.sha256[:24]}.json"

    def persist(self, sidecar: ReviewerSidecarV1) -> Path:
        if type(sidecar) is not ReviewerSidecarV1:
            raise TypeError("reviewer sidecar store requires ReviewerSidecarV1")
        if self.evidence_root.is_symlink() or (
            self.directory.exists() and self.directory.is_symlink()
        ):
            raise ValueError("reviewer-sidecar path contains a symlink")
        packets = tuple(
            sorted(self.evidence_root.glob("runs/run-*/review-packet.json"))
        )
        if (
            len(packets) != 1
            or packets[0].is_symlink()
            or hashlib.sha256(packets[0].read_bytes()).hexdigest()
            != sidecar.packet_sha256
        ):
            raise ValueError("reviewer sidecar does not bind the immutable review packet")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(sidecar)
        payload = sidecar.canonical_bytes()
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if self.load(path).canonical_bytes() != payload:
                raise RuntimeError("reviewer sidecar identity collides with other bytes")
            return path
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_descriptor = os.open(
            self.directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return path

    def load(self, path: Path) -> ReviewerSidecarV1:
        if path.parent != self.directory or path.is_symlink() or not path.is_file():
            raise ValueError("reviewer sidecar path is unsafe or foreign")
        raw = path.read_bytes()
        import json

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("reviewer sidecar is not strict JSON") from error
        if not isinstance(payload, Mapping):
            raise ValueError("reviewer sidecar must be an object")
        sidecar = ReviewerSidecarV1.from_dict(payload)
        if sidecar.canonical_bytes() != raw or path != self.path_for(sidecar):
            raise ValueError("reviewer sidecar bytes or identity are not canonical")
        return sidecar


def interval_iou_ppm(left: tuple[int, int], right: tuple[int, int]) -> int:
    intersection = max(0, min(left[1], right[1]) - max(left[0], right[0]))
    union = max(left[1], right[1]) - min(left[0], right[0])
    if union <= 0:
        raise ValueError("review windows must have positive union")
    return (intersection * POLICY_SCALE_PPM) // union


def _intersects_open_interior(start_us: int, end_us: int, point_us: int) -> bool:
    return start_us < point_us < end_us


def _intersects_interval(start_us: int, end_us: int, interval: tuple[int, int]) -> bool:
    return max(start_us, interval[0]) < min(end_us, interval[1])


def _stratum_bounds(run: ReviewRunV1) -> dict[str, tuple[int, int] | None]:
    duration = run.continuous_end_us - run.continuous_start_us
    event = (
        None
        if run.event_time_us is None
        else (run.event_time_us - 120_000_000, run.event_time_us + 480_000_000)
    )
    return {
        "opening": (
            run.session_start_us,
            normalized_boundary_time_us(100_000, run.continuous_start_us, duration),
        ),
        "ordinary_morning": (
            normalized_boundary_time_us(100_000, run.continuous_start_us, duration),
            normalized_boundary_time_us(350_000, run.continuous_start_us, duration),
        ),
        "midday": (
            normalized_boundary_time_us(350_000, run.continuous_start_us, duration),
            normalized_boundary_time_us(600_000, run.continuous_start_us, duration),
        ),
        "event_post_event": event,
        "ordinary_afternoon": (
            normalized_boundary_time_us(600_000, run.continuous_start_us, duration),
            normalized_boundary_time_us(900_000, run.continuous_start_us, duration),
        ),
        "close": (
            normalized_boundary_time_us(900_000, run.continuous_start_us, duration),
            run.session_end_us,
        ),
    }


def _applicable(run: ReviewRunV1, stratum: str) -> bool:
    if stratum != "event_post_event":
        return True
    return run.candidate_id == EVENT_SHOCK_PRESSURE or run.candidate_id == "DEV_ONLY_EVENT"


def enumerate_eligible_windows(run: ReviewRunV1) -> tuple[EligibleWindowV1, ...]:
    """Exhaustively enumerate the fixed one-second universe in time order."""

    bounds = _stratum_bounds(run)
    rows: list[EligibleWindowV1] = []
    for start_us in range(
        run.session_start_us,
        run.session_end_us - REVIEW_WINDOW_DURATION_US + 1,
        REVIEW_START_STEP_US,
    ):
        end_us = start_us + REVIEW_WINDOW_DURATION_US
        if any(
            _intersects_open_interior(start_us, end_us, boundary)
            for boundary in run.phase_boundaries_us
        ):
            continue
        if any(_intersects_interval(start_us, end_us, halt) for halt in run.halt_intervals_us):
            continue
        strata: list[str] = []
        for stratum in REVIEW_STRATA:
            interval = bounds[stratum]
            if interval is None or not _applicable(run, stratum):
                continue
            if interval[0] <= start_us and end_us <= interval[1]:
                if (
                    stratum.startswith("ordinary_") or stratum == "midday"
                ) and bounds["event_post_event"] is not None and _intersects_interval(
                    start_us, end_us, bounds["event_post_event"]  # type: ignore[arg-type]
                ):
                    continue
                strata.append(stratum)
        if len(strata) != 1:
            continue
        stratum = strata[0]
        samples = tuple(
            sample
            for sample in run.samples
            if start_us <= sample.simulation_time_us < end_us
        )
        observable_sha256 = canonical_sha256(
            {
                "end_us": end_us,
                "samples": [sample.as_dict() for sample in samples],
                "start_us": start_us,
            }
        )
        context = (
            f"WO31_REVIEW/{run.candidate_id}/{stratum}/"
            f"{run.run_digest}/{start_us}"
        )
        digest = policy_tie_digest(
            REVIEW_SELECTION_POLICY_VERSION,
            context,
            REVIEW_SELECTION_ROOT,
            observable_sha256,
        ).hex()
        rows.append(
            EligibleWindowV1(
                run_digest=run.run_digest,
                candidate_id=run.candidate_id,
                root_seed=run.root_seed,
                stratum=stratum,
                start_us=start_us,
                end_us=end_us,
                selection_context=context,
                selection_digest=digest,
                observable_window_sha256=observable_sha256,
                samples=samples,
            )
        )
    return tuple(rows)


def select_review_windows(
    runs: Sequence[ReviewRunV1],
) -> tuple[SelectedWindowManifestV1, ...]:
    """Rank by review-only digest, then greedily enforce the frozen IoU cap."""

    if not runs:
        raise ValueError("review selection requires at least one completed run")
    candidates = tuple(run.candidate_id for run in runs)
    if len(set((run.run_digest for run in runs))) != len(runs):
        raise ValueError("review selection received a duplicate run digest")
    eligible = tuple(
        row for run in runs for row in enumerate_eligible_windows(run)
    )
    output: list[SelectedWindowManifestV1] = []
    candidate_order = sorted(
        set(candidates),
        key=lambda candidate: (
            tuple(DISPLAY_LABELS).index(candidate)
            if candidate in DISPLAY_LABELS
            else len(DISPLAY_LABELS),
            candidate,
        ),
    )
    representative = {run.candidate_id: run.run_digest for run in runs}
    for candidate_id in candidate_order:
        for stratum in REVIEW_STRATA:
            applicable = any(
                run.candidate_id == candidate_id and _applicable(run, stratum)
                for run in runs
            )
            if not applicable:
                output.append(
                    SelectedWindowManifestV1(
                        run_digest=representative[candidate_id],
                        stratum=stratum,
                        start_us=None,
                        end_us=None,
                        selection_context=None,
                        selection_digest=None,
                        observable_window_sha256=None,
                        shortfall_status=NOT_APPLICABLE,
                    )
                )
                continue
            ranked = sorted(
                (
                    row
                    for row in eligible
                    if row.candidate_id == candidate_id and row.stratum == stratum
                ),
                key=lambda row: (row.selection_digest, row.start_us, row.run_digest),
            )
            selected: list[EligibleWindowV1] = []
            for row in ranked:
                if all(
                    interval_iou_ppm(
                        (row.start_us, row.end_us),
                        (prior.start_us, prior.end_us),
                    )
                    <= REVIEW_IOU_MAX_PPM
                    for prior in selected
                ):
                    selected.append(row)
                if len(selected) == REVIEW_WINDOWS_PER_STRATUM:
                    break
            for row in selected:
                output.append(
                    SelectedWindowManifestV1(
                        run_digest=row.run_digest,
                        stratum=row.stratum,
                        start_us=row.start_us,
                        end_us=row.end_us,
                        selection_context=row.selection_context,
                        selection_digest=row.selection_digest,
                        observable_window_sha256=row.observable_window_sha256,
                        shortfall_status="SELECTED",
                    )
                )
            for _missing in range(REVIEW_WINDOWS_PER_STRATUM - len(selected)):
                output.append(
                    SelectedWindowManifestV1(
                        run_digest=representative[candidate_id],
                        stratum=stratum,
                        start_us=None,
                        end_us=None,
                        selection_context=None,
                        selection_digest=None,
                        observable_window_sha256=None,
                        shortfall_status="SHORTFALL",
                    )
                )
    return tuple(output)


def build_blinded_packet(
    runs: Sequence[ReviewRunV1],
    selections: Sequence[SelectedWindowManifestV1],
) -> BlindedReviewPacketV1:
    """Render only observable feed and phase-relative time in selected order."""

    by_digest = {run.run_digest: run for run in runs}
    windows: list[dict[str, object]] = []
    for ordinal, selection in enumerate(selections):
        if selection.shortfall_status != "SELECTED":
            windows.append(
                {
                    "packet_window_id": f"WINDOW-{ordinal:04d}",
                    "samples": [],
                    "shortfall_status": selection.shortfall_status,
                    "stratum": selection.stratum,
                }
            )
            continue
        run = by_digest.get(selection.run_digest)
        if run is None:
            raise ValueError("selection references an unknown immutable run")
        samples = tuple(
            sample
            for sample in run.samples
            if selection.start_us <= sample.simulation_time_us < selection.end_us  # type: ignore[operator]
        )
        actual_digest = canonical_sha256(
            {
                "end_us": selection.end_us,
                "samples": [sample.as_dict() for sample in samples],
                "start_us": selection.start_us,
            }
        )
        if actual_digest != selection.observable_window_sha256:
            raise ValueError("observable window bytes changed after selection")
        windows.append(
            {
                "packet_window_id": f"WINDOW-{ordinal:04d}",
                "samples": [
                    {
                        "observable_feed": dict(sample.observable_feed),
                        "phase_relative_time_us": sample.phase_relative_time_us,
                    }
                    for sample in samples
                ],
                "shortfall_status": "SELECTED",
                "stratum": selection.stratum,
            }
        )
    packet_identity = canonical_sha256(
        {
            "manifest_digests": [
                selection.observable_window_sha256
                for selection in selections
                if selection.observable_window_sha256 is not None
            ],
            "review_label": REVIEW_SELECTION_LABEL,
        }
    )
    return BlindedReviewPacketV1(
        packet_id="review-packet-" + packet_identity[:24],
        windows=tuple(windows),
    )


__all__ = [
    "BLIND_FIELDS",
    "BlindedReviewPacketV1",
    "EligibleWindowV1",
    "HUMAN_REVIEW_STATUSES",
    "ObservableSampleV1",
    "REVIEW_STRATA",
    "ReviewRunV1",
    "ReviewerSidecarV1",
    "ReviewerSidecarStore",
    "SelectedWindowManifestV1",
    "build_blinded_packet",
    "enumerate_eligible_windows",
    "interval_iou_ppm",
    "select_review_windows",
]
