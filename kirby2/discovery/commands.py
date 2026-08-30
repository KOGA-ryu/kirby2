"""Public strategy-discovery commands and the WO35-F development fixture."""

from __future__ import annotations

import argparse
import heapq
import hashlib
import json
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from kirby2.cli.registry import CommandModule, CommandSpec
from kirby2.exchange import Order, OrderBook, OrderOwner, OrderType, Side
from kirby2.session.events import SimulationEvent
from kirby2.simulation import FlowEvent, FlowEventFamily, LiquidityPreset, VolumePreset
from kirby2.strategy import StrategyDefinition, TrafficLightRuntime, parse_strategy

from .ast import parse_strategy_ast
from .evaluation import (
    CandidatePartitionEvidenceV1,
    ComponentDeltaV1,
    RootDeltaV1,
    validation_qualification,
)
from .identity import canonical_identity_bytes, strategy_semantic_sha256
from .lineage import semantic_strategy_diff
from .observability import (
    CandidatePermissionV1,
    CandidateSignalV1,
    ObservableDecisionInputV1,
    ObservationStatusV1,
    REFERENCE_EXECUTION_ORACLE_ID_V1,
    ReferenceDecisionLabelV1,
    ScientificConclusionV1,
    _token_sha256,
    bind_reference_decision_label,
    project_candidate_decision,
    score_candidate_decision,
    seal_terminal_material,
)
from .objectives import (
    POLICY_SCALE_V1,
    REQUIRED_OBJECTIVE_SPECS_V1,
    EvidenceCompatibilityKeyV1,
    ObjectiveApplicabilityV1,
    ObjectiveValueV1,
    StrategyObjectiveIdV1,
    balanced_classification_utility,
    clamp,
    completion_utility,
    cross_cell_stability_utility,
    discipline_compatibility_utility,
    execution_opportunity_utility,
    false_green_utility,
    missed_opportunity_utility,
    nearest_rank_p50,
    root_composite,
    round_div_even,
    signed_cost_utility,
    turnover_utility,
)
from .partitions import StrategyPartitionV1
from .report import build_lineage_report, compare_strategies
from .robustness import (
    ControlledRobustnessEnvironmentV1,
    MANDATORY_ROBUSTNESS_FAMILIES_V1,
    ROBUSTNESS_POLICY_ID_V1,
    ROBUSTNESS_ROOTS_V1,
    ROBUSTNESS_SETTINGS_V1,
    PerturbationStatusV1,
    RobustnessCellV1,
    RobustnessEvidenceV1,
    RobustnessFamilyEvidenceV1,
    RobustnessFamilyV1,
    RobustnessOutcomeV1,
    RobustnessQualificationV1,
    SINGLE_VENUE_CAPABILITY_ID_V1,
    SyntheticRobustnessModeV1,
    VenueMixDeclarationV1,
    apply_robustness_setting,
    build_synthetic_robustness_evidence,
    controlled_robustness_environment,
    qualify_adversarial,
    qualify_holdout,
    qualify_robustness,
    robustness_composite_delta,
)
from .search import (
    CONTROLLED_BASE_SOURCE_SHA256_V1,
    ControlledSearchSpaceV1,
    SearchCandidateV1,
    StrategySearchManifestV1,
    TrainedCandidateV1,
    ValidatedCandidateV1,
    _final_training_rank_key,
    _validation_rank_key,
    controlled_search_parameters,
    load_search_manifest,
)
from .store import (
    DiscoveryBindingV1,
    DiscoveryEventKindV1,
    DiscoveryStore,
    DiscoveryStoreError,
)


LINEAGE_DEVELOPMENT_SCHEMA_ID_V1 = "KIRBY2_LINEAGE_DEVELOPMENT_MANIFEST_V1"
LINEAGE_DEVELOPMENT_ORACLE_ID_V1 = "LINEAGE_DEVELOPMENT_INTEGER_ORACLE_V1"
LINEAGE_DEVELOPMENT_DATA_SOURCE_V1 = "SYNTHETIC_INTEGER_FUNCTION_ONLY_V1"
CONTROLLED_DATA_SOURCE_V1 = "KIRBY2_FIXED_EXOGENOUS_SESSION_V1"
CONTROLLED_IMPLEMENTATION_COMMIT_SUBJECT_V1 = "Implement strategy discovery lineage"
CONTROLLED_EVIDENCE_REASON_V1 = "IMMUTABLE_CONTROLLED_EVIDENCE_MISSING"
DEFAULT_DISCOVERY_STORE = Path(".kirby2") / "discovery"
_BOUNDED_REAL_ROOTS = frozenset(
    (*range(3_501_000, 3_501_012), *range(3_502_000, 3_502_008),
     *range(3_503_000, 3_503_008), *range(3_504_000, 3_504_008),
     *range(3_505_000, 3_505_004))
)
_PARTITION_NAMES = (
    "train",
    "validation",
    "robustness",
    "holdout",
    "adversarial",
)
_CONTROLLED_DURATION_SECONDS = 90
_CONTROLLED_DECISION_START_US = 30_000_000
_CONTROLLED_DECISION_END_GUARD_US = 10_000_000
_CONTROLLED_DECISION_INTERVAL_US = 1_000_000
_CONTROLLED_OBJECTIVE_SHARES = 100
_CONTROLLED_SCENARIO_FAMILIES = (
    ("QUIET_RANGE_PRESSURE", "balanced"),
    ("TREND_PRESSURE", "momentum_up"),
    ("EVENT_SHOCK_PRESSURE", "panic"),
    ("DISORDERLY_OPEN_STABILIZATION_PRESSURE", "high_cancellation"),
)
_CONTROLLED_CELLS = {
    StrategyPartitionV1.TRAIN: (
        (VolumePreset.X0_50, LiquidityPreset.THIN),
        (VolumePreset.X1_00, LiquidityPreset.NORMAL),
        (VolumePreset.X2_00, LiquidityPreset.DEEP),
    ),
    StrategyPartitionV1.VALIDATION: (
        (VolumePreset.X0_50, LiquidityPreset.DEEP),
        (VolumePreset.X2_00, LiquidityPreset.THIN),
    ),
    StrategyPartitionV1.HOLDOUT: (
        (VolumePreset.X0_25, LiquidityPreset.NORMAL),
        (VolumePreset.X5_00, LiquidityPreset.NORMAL),
    ),
    StrategyPartitionV1.ADVERSARIAL_HOLDOUT: (
        (VolumePreset.X0_25, LiquidityPreset.VERY_DEEP),
        (VolumePreset.X10_00, LiquidityPreset.VERY_THIN),
    ),
    StrategyPartitionV1.ROBUSTNESS: (
        (VolumePreset.X1_00, LiquidityPreset.NORMAL),
    ),
}
_CONTROLLED_CASES = {
    StrategyPartitionV1.TRAIN: (
        *("U" for _ in range(5)),
        "T",
        *("D" for _ in range(14)),
        *("W" for _ in range(24)),
        *("R" for _ in range(7)),
    ),
    StrategyPartitionV1.VALIDATION: (
        *("U" for _ in range(15)),
        *("C" for _ in range(24)),
        *("W" for _ in range(4)),
        *("R" for _ in range(8)),
    ),
}
_CONTROLLED_CASES[StrategyPartitionV1.HOLDOUT] = _CONTROLLED_CASES[
    StrategyPartitionV1.VALIDATION
]
_CONTROLLED_CASES[StrategyPartitionV1.ADVERSARIAL_HOLDOUT] = _CONTROLLED_CASES[
    StrategyPartitionV1.VALIDATION
]
_CONTROLLED_CASES[StrategyPartitionV1.ROBUSTNESS] = _CONTROLLED_CASES[
    StrategyPartitionV1.VALIDATION
]
_CONTROLLED_CASE_PARAMETERS = {
    "U": (1, 450_000, 100),
    "T": (2, 350_000, 100),
    "D": (2, 250_000, 50),
    "C": (2, 350_000, 50),
    "W": (3, 350_000, 50),
    "R": (5, 350_000, 100),
}
_VOLUME_RELATIVE_PPM = {
    VolumePreset.X0_25: 250_000,
    VolumePreset.X0_50: 500_000,
    VolumePreset.X1_00: 1_000_000,
    VolumePreset.X2_00: 2_000_000,
    VolumePreset.X5_00: 5_000_000,
    VolumePreset.X10_00: 10_000_000,
}
_VOLUME_DISPLAYED_QUEUE_PPM = {
    VolumePreset.X0_25: 700_000,
    VolumePreset.X0_50: 850_000,
    VolumePreset.X1_00: 1_000_000,
    VolumePreset.X2_00: 1_200_000,
    VolumePreset.X5_00: 1_500_000,
    VolumePreset.X10_00: 1_800_000,
}
_LIQUIDITY_QUEUE_PPM = {
    LiquidityPreset.VERY_THIN: 250_000,
    LiquidityPreset.THIN: 550_000,
    LiquidityPreset.NORMAL: 1_000_000,
    LiquidityPreset.DEEP: 2_000_000,
    LiquidityPreset.VERY_DEEP: 4_000_000,
}


class ControlledEvidenceInsufficientError(RuntimeError):
    """A measured controlled run could not supply a required denominator."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ControlledRootSpecV1:
    partition: StrategyPartitionV1
    root_seed: int
    scenario_family: str
    scenario_name: str
    cell_ordinal: int
    volume: VolumePreset
    liquidity: LiquidityPreset
    environment_tag: str = "UNPERTURBED"
    event_intensity_ppm: int = POLICY_SCALE_V1
    initial_queue_ppm: int = POLICY_SCALE_V1

    def as_dict(self) -> dict[str, object]:
        return {
            "cell_ordinal": self.cell_ordinal,
            "environment_tag": self.environment_tag,
            "event_intensity_ppm": self.event_intensity_ppm,
            "historical_period": "NOT_APPLICABLE",
            "initial_queue_ppm": self.initial_queue_ppm,
            "liquidity": self.liquidity.value,
            "partition": self.partition.value,
            "root_seed": self.root_seed,
            "scenario_family": self.scenario_family,
            "scenario_name": self.scenario_name,
            "source_day": "NOT_APPLICABLE",
            "volume": self.volume.value,
        }


@dataclass(frozen=True, slots=True)
class ControlledSourceTapeV1:
    spec: ControlledRootSpecV1
    events: tuple[FlowEvent, ...]
    labels: tuple[ReferenceDecisionLabelV1, ...]
    source_tape_sha256: str
    reference_inventory_sha256: str

    def label_by_time(self) -> dict[int, ReferenceDecisionLabelV1]:
        return {item.decision_time_us: item for item in self.labels}


@dataclass(frozen=True, slots=True)
class ControlledDecisionCountsV1:
    correct_green: int
    reference_green: int
    correct_wait: int
    reference_wait: int
    correct_red: int
    reference_red: int
    discipline_eligible: int
    discipline_violations: int
    opportunity_true_positive: int
    predicted_green_allow: int
    true_opportunities: int
    false_green: int
    non_green: int
    missed_opportunities: int

    def __add__(self, other: object) -> "ControlledDecisionCountsV1":
        if not isinstance(other, ControlledDecisionCountsV1):
            return NotImplemented
        return ControlledDecisionCountsV1(
            *(left + right for left, right in zip(self.values(), other.values(), strict=True))
        )

    def values(self) -> tuple[int, ...]:
        return (
            self.correct_green,
            self.reference_green,
            self.correct_wait,
            self.reference_wait,
            self.correct_red,
            self.reference_red,
            self.discipline_eligible,
            self.discipline_violations,
            self.opportunity_true_positive,
            self.predicted_green_allow,
            self.true_opportunities,
            self.false_green,
            self.non_green,
            self.missed_opportunities,
        )

    @classmethod
    def zero(cls) -> "ControlledDecisionCountsV1":
        return cls(*(0 for _ in range(14)))

    def as_dict(self) -> dict[str, object]:
        return {
            "correct_green": self.correct_green,
            "correct_red": self.correct_red,
            "correct_wait": self.correct_wait,
            "discipline_eligible": self.discipline_eligible,
            "discipline_violations": self.discipline_violations,
            "false_green": self.false_green,
            "missed_opportunities": self.missed_opportunities,
            "non_green": self.non_green,
            "opportunity_true_positive": self.opportunity_true_positive,
            "predicted_green_allow": self.predicted_green_allow,
            "reference_green": self.reference_green,
            "reference_red": self.reference_red,
            "reference_wait": self.reference_wait,
            "true_opportunities": self.true_opportunities,
        }


@dataclass(frozen=True, slots=True)
class ControlledRootRunV1:
    spec: ControlledRootSpecV1
    semantic_sha256: str
    counts: ControlledDecisionCountsV1
    execution_utilities: tuple[tuple[StrategyObjectiveIdV1, int], ...]
    completed_shares: int
    traded_shares: int
    trade_count: int
    spread_paid_milliticks_per_share: int
    adverse_milliticks_per_share: int
    projection_inventory_sha256: str
    execution_sha256: str
    source_tape_sha256: str
    reference_inventory_sha256: str
    invariant_clean: bool
    replay_valid: bool

    def utility(self, objective_id: StrategyObjectiveIdV1) -> int:
        return dict(self.execution_utilities)[objective_id]

    def as_dict(self) -> dict[str, object]:
        return {
            "adverse_milliticks_per_share": self.adverse_milliticks_per_share,
            "completed_shares": self.completed_shares,
            "decision_counts": self.counts.as_dict(),
            "execution_sha256": self.execution_sha256,
            "execution_utilities": {
                objective_id.value: value
                for objective_id, value in self.execution_utilities
            },
            "invariant_clean": self.invariant_clean,
            "projection_inventory_sha256": self.projection_inventory_sha256,
            "reference_inventory_sha256": self.reference_inventory_sha256,
            "replay_valid": self.replay_valid,
            "semantic_sha256": self.semantic_sha256,
            "source_tape_sha256": self.source_tape_sha256,
            "spec": self.spec.as_dict(),
            "spread_paid_milliticks_per_share": self.spread_paid_milliticks_per_share,
            "trade_count": self.trade_count,
            "traded_shares": self.traded_shares,
        }


@dataclass(frozen=True, slots=True)
class _ControlledFillV1:
    simulation_time_us: int
    order_id: str
    side: Side
    liquidity: str
    quantity: int
    price_ticks: int
    arrival_mid_x2: int
    fee_milliticks_per_share: int

    def as_dict(self) -> dict[str, object]:
        return {
            "arrival_mid_x2": self.arrival_mid_x2,
            "fee_milliticks_per_share": self.fee_milliticks_per_share,
            "liquidity": self.liquidity,
            "order_id": self.order_id,
            "price_ticks": self.price_ticks,
            "quantity": self.quantity,
            "side": self.side.value,
            "simulation_time_us": self.simulation_time_us,
        }


@dataclass(frozen=True, slots=True)
class ControlledPartitionRunV1:
    evidence: CandidatePartitionEvidenceV1
    root_runs: tuple[ControlledRootRunV1, ...]
    base_root_runs: tuple[ControlledRootRunV1, ...]
    candidate_utilities: tuple[tuple[int, tuple[tuple[StrategyObjectiveIdV1, int], ...]], ...]
    base_utilities: tuple[tuple[int, tuple[tuple[StrategyObjectiveIdV1, int], ...]], ...]

    @property
    def real_partition_access_count(self) -> int:
        return len(self.root_runs) + len(self.base_root_runs)

    def as_dict(self, trained_candidate_count: int) -> dict[str, object]:
        return {
            "accesses": [item.as_dict() for item in (*self.base_root_runs, *self.root_runs)],
            "evidence": self.evidence.as_dict(trained_candidate_count),
            "real_partition_access_count": self.real_partition_access_count,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentCandidateSpecV1:
    green_spread_ticks: int
    green_imbalance_ppm: int
    wait_spread_ticks: int
    window_us: int
    training_score: int
    validation_score: int
    holdout_score: int
    adversarial_score: int

    def __post_init__(self) -> None:
        if self.green_spread_ticks not in {1, 2, 3}:
            raise ValueError("development green spread is outside controlled domain")
        if self.green_imbalance_ppm not in {100_000, 200_000, 300_000, 400_000}:
            raise ValueError("development imbalance is outside controlled domain")
        if self.wait_spread_ticks not in {2, 4, 6}:
            raise ValueError("development wait spread is outside controlled domain")
        if self.window_us not in {2_000_000, 5_000_000, 10_000_000}:
            raise ValueError("development window is outside controlled domain")
        if self.wait_spread_ticks < self.green_spread_ticks:
            raise ValueError("development candidate is semantically invalid")
        if any(
            type(value) is not int
            for value in (
                self.training_score,
                self.validation_score,
                self.holdout_score,
                self.adversarial_score,
            )
        ):
            raise TypeError("development scores must be integers")


@dataclass(frozen=True, slots=True)
class LineageDevelopmentManifestV1:
    experiment_id: str
    budget: int
    reveal_token: str
    base_source: bytes
    partitions: tuple[tuple[str, tuple[int, ...]], ...]
    candidates: tuple[DevelopmentCandidateSpecV1, ...]
    raw_sha256: str
    development_only: bool = True
    real_partition_execution: bool = False

    def __post_init__(self) -> None:
        if type(self.experiment_id) is not str or not self.experiment_id:
            raise ValueError("development experiment ID must be nonempty")
        if type(self.budget) is not int or not 1 <= self.budget <= 64:
            raise ValueError("development lineage budget must be in 1..64")
        if type(self.reveal_token) is not str or not self.reveal_token:
            raise ValueError("development reveal token must be nonempty")
        if type(self.base_source) is not bytes or not self.base_source.endswith(b"\n"):
            raise ValueError("development base source must be final-LF bytes")
        if tuple(name for name, _roots in self.partitions) != _PARTITION_NAMES:
            raise ValueError("development partition inventory or order differs")
        roots = tuple(root for _name, values in self.partitions for root in values)
        if (
            not roots
            or len(roots) != len(set(roots))
            or any(type(root) is not int or root < 0 for root in roots)
        ):
            raise ValueError("development partition roots must be unique nonnegative integers")
        if set(roots) & _BOUNDED_REAL_ROOTS:
            raise ValueError("development fixture overlaps bounded-search partitions")
        if type(self.candidates) is not tuple or len(self.candidates) < 2:
            raise ValueError("development fixture requires at least two candidates")
        if self.budget < len(self.candidates):
            raise ValueError("development budget cannot be below candidate inventory")
        if self.development_only is not True or self.real_partition_execution is not False:
            raise ValueError("lineage development manifest cannot authorize real execution")
        _require_sha256(self.raw_sha256, "development manifest digest")
        parse_strategy_ast(self.base_source.decode("utf-8"))

    @property
    def partition_manifest_sha256(self) -> str:
        return hashlib.sha256(
            canonical_identity_bytes(
                {"partitions": {name: list(roots) for name, roots in self.partitions}}
            )
        ).hexdigest()

    def roots(self, partition: str) -> tuple[int, ...]:
        for name, roots in self.partitions:
            if name == partition:
                return roots
        raise KeyError(partition)

    @classmethod
    def load(cls, path: Path) -> LineageDevelopmentManifestV1:
        if not isinstance(path, Path):
            raise TypeError("development manifest path must be pathlib.Path")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
            payload = tomllib.loads(text)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("development manifest must be UTF-8 TOML") from error
        expected = {
            "base_source",
            "budget",
            "candidates",
            "development_only",
            "experiment_id",
            "partitions",
            "real_partition_execution",
            "reveal_token",
            "schema_id",
            "schema_version",
        }
        if set(payload) != expected:
            raise ValueError("development manifest top-level fields differ")
        if payload["schema_id"] != LINEAGE_DEVELOPMENT_SCHEMA_ID_V1:
            raise ValueError("unsupported development manifest schema ID")
        if payload["schema_version"] != 1:
            raise ValueError("unsupported development manifest schema version")
        raw_partitions = payload["partitions"]
        if not isinstance(raw_partitions, dict) or set(raw_partitions) != set(
            _PARTITION_NAMES
        ):
            raise ValueError("development partitions differ from exact inventory")
        partitions = tuple(
            (
                name,
                _integer_tuple(raw_partitions[name], f"{name} roots"),
            )
            for name in _PARTITION_NAMES
        )
        raw_candidates = payload["candidates"]
        if not isinstance(raw_candidates, list) or any(
            not isinstance(item, dict) for item in raw_candidates
        ):
            raise ValueError("development candidates must be an array of tables")
        candidate_fields = {
            "adversarial_score",
            "green_imbalance_ppm",
            "green_spread_ticks",
            "holdout_score",
            "training_score",
            "validation_score",
            "wait_spread_ticks",
            "window_us",
        }
        if any(set(item) != candidate_fields for item in raw_candidates):
            raise ValueError("development candidate fields differ")
        candidates = tuple(
            DevelopmentCandidateSpecV1(
                green_spread_ticks=_integer(item, "green_spread_ticks"),
                green_imbalance_ppm=_integer(item, "green_imbalance_ppm"),
                wait_spread_ticks=_integer(item, "wait_spread_ticks"),
                window_us=_integer(item, "window_us"),
                training_score=_integer(item, "training_score"),
                validation_score=_integer(item, "validation_score"),
                holdout_score=_integer(item, "holdout_score"),
                adversarial_score=_integer(item, "adversarial_score"),
            )
            for item in raw_candidates
        )
        base_source = payload["base_source"]
        if type(base_source) is not str:
            raise TypeError("development base source must be text")
        return cls(
            experiment_id=_text(payload, "experiment_id"),
            budget=_integer(payload, "budget"),
            reveal_token=_text(payload, "reveal_token"),
            base_source=base_source.encode("utf-8"),
            partitions=partitions,
            candidates=candidates,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            development_only=_boolean(payload, "development_only"),
            real_partition_execution=_boolean(payload, "real_partition_execution"),
        )


@dataclass(frozen=True, slots=True)
class DevelopmentLineageDemoResultV1:
    primary_discovery_id: str
    discovery_ids: tuple[str, ...]
    ledger_sha256: str
    report_sha256: str
    comparison_sha256: str
    outcomes: tuple[str, ...]
    record_count: int
    crash_reopen_passed: bool
    conflict_refused: bool
    repeat_reveal_refused: bool
    sealed_before_reveal: bool
    revealed_after_consume: bool
    real_partition_access_count: int
    verification_passed: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.crash_reopen_passed,
                self.conflict_refused,
                self.repeat_reveal_refused,
                self.sealed_before_reveal,
                self.revealed_after_consume,
                self.real_partition_access_count == 0,
                self.verification_passed,
                set(self.outcomes)
                == {item.value for item in ScientificConclusionV1},
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "comparison_sha256": self.comparison_sha256,
            "conflict_refused": self.conflict_refused,
            "crash_reopen_passed": self.crash_reopen_passed,
            "discovery_ids": list(self.discovery_ids),
            "ledger_sha256": self.ledger_sha256,
            "outcomes": list(self.outcomes),
            "primary_discovery_id": self.primary_discovery_id,
            "real_partition_access_count": self.real_partition_access_count,
            "record_count": self.record_count,
            "repeat_reveal_refused": self.repeat_reveal_refused,
            "report_sha256": self.report_sha256,
            "revealed_after_consume": self.revealed_after_consume,
            "sealed_before_reveal": self.sealed_before_reveal,
            "status": "PASS" if self.passed else "FAIL",
            "verification_passed": self.verification_passed,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryExecutionPreflightV1:
    repository: Path
    implementation_commit: str
    manifest_sha256: str
    base_source_sha256: str
    evidence_root: Path
    clean_head: bool
    committed_inputs_match: bool
    fresh_evidence_root: bool

    @property
    def passed(self) -> bool:
        return self.clean_head and self.committed_inputs_match and self.fresh_evidence_root

    def require(self) -> None:
        if not self.clean_head:
            raise DiscoveryStoreError(
                "DIRTY_IMPLEMENTATION_HEAD",
                "discovery execution requires a clean committed implementation HEAD",
            )
        if not self.committed_inputs_match:
            raise DiscoveryStoreError(
                "UNCOMMITTED_DISCOVERY_INPUT",
                "base and experiment bytes must exactly match committed HEAD",
            )
        if not self.fresh_evidence_root:
            raise DiscoveryStoreError(
                "EVIDENCE_ROOT_NOT_FRESH",
                "discovery refuses an existing or partial evidence root",
            )


def inspect_execution_preflight(
    *,
    repository: Path,
    base_path: Path,
    manifest_path: Path,
    evidence_root: Path,
) -> DiscoveryExecutionPreflightV1:
    repository = repository.resolve(strict=True)
    base = base_path.resolve(strict=True)
    manifest = manifest_path.resolve(strict=True)
    try:
        base_relative = base.relative_to(repository).as_posix()
        manifest_relative = manifest.relative_to(repository).as_posix()
    except ValueError as error:
        raise DiscoveryStoreError(
            "DISCOVERY_INPUT_OUTSIDE_REPOSITORY",
            "base and experiment inputs must be repository-relative",
        ) from error
    head = _git(repository, "rev-parse", "HEAD").strip()
    if len(head) != 40:
        raise DiscoveryStoreError("INVALID_IMPLEMENTATION_HEAD", "Git HEAD is not SHA-1")
    dirty = bool(_git(repository, "status", "--porcelain", "--untracked-files=all"))
    committed_match = True
    for relative, path in ((base_relative, base), (manifest_relative, manifest)):
        try:
            committed = subprocess.run(
                ("git", "show", f"HEAD:{relative}"),
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        except subprocess.CalledProcessError:
            committed_match = False
            continue
        committed_match = committed_match and committed == path.read_bytes()
    return DiscoveryExecutionPreflightV1(
        repository,
        head,
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
        hashlib.sha256(base.read_bytes()).hexdigest(),
        evidence_root,
        not dirty,
        committed_match,
        not evidence_root.exists() and not evidence_root.is_symlink(),
    )


def robustness_policy_sha256() -> str:
    return hashlib.sha256(
        canonical_identity_bytes(
            {
                "policy_id": ROBUSTNESS_POLICY_ID_V1,
                "settings": [item.as_dict() for item in ROBUSTNESS_SETTINGS_V1],
            }
        )
    ).hexdigest()


def run_development_lineage_demo(
    manifest_path: Path,
    *,
    store_root: Path,
    implementation_commit: str | None = None,
) -> DevelopmentLineageDemoResultV1:
    manifest = LineageDevelopmentManifestV1.load(manifest_path)
    repository = Path(__file__).resolve().parents[2]
    head = implementation_commit or _git(repository, "rev-parse", "HEAD").strip()
    space = ControlledSearchSpaceV1(controlled_search_parameters())
    base_ast = parse_strategy_ast(manifest.base_source.decode("utf-8"))
    base_semantic = strategy_semantic_sha256(base_ast)
    candidates = tuple(
        space.candidate_for_vector(
            space.vector(
                (
                    item.green_spread_ticks,
                    item.green_imbalance_ppm,
                    item.wait_spread_ticks,
                    item.window_us,
                )
            ),
            universe_ordinal=index,
        )
        for index, item in enumerate(manifest.candidates)
    )
    if any(item is None for item in candidates):
        raise ValueError("development manifest produced an invalid controlled candidate")
    typed_candidates = tuple(item for item in candidates if item is not None)
    if len({item.semantic_sha256 for item in typed_candidates}) != len(typed_candidates):
        raise ValueError("development manifest produced semantic duplicate candidates")
    selected_index = max(
        range(len(manifest.candidates)),
        key=lambda index: (
            manifest.candidates[index].validation_score,
            typed_candidates[index].semantic_sha256,
        ),
    )
    selected = typed_candidates[selected_index]
    primary_material = _terminal_material(
        manifest,
        selected.semantic_sha256,
        reveal_token=manifest.reveal_token,
    )
    binding = _development_binding(
        manifest,
        head,
        base_semantic,
        primary_material.reveal_token_sha256,
        experiment_suffix="primary",
    )
    store = DiscoveryStore(store_root)
    ledger = store.create(binding)
    for candidate, spec in zip(typed_candidates, manifest.candidates, strict=True):
        mutation = _mutation_payload(base_ast, candidate.source, candidate.semantic_sha256)
        store.append_event(
            ledger.discovery_id,
            DiscoveryEventKindV1.MUTATION_RECORDED,
            payload=mutation,
            candidate_semantic_sha256=candidate.semantic_sha256,
            parent_semantic_sha256=base_semantic,
        )
        store.append_event(
            ledger.discovery_id,
            DiscoveryEventKindV1.TRAINING_EVALUATED,
            payload=_development_result(
                candidate.semantic_sha256,
                "TRAIN",
                manifest.roots("train"),
                spec.training_score,
            ),
            candidate_semantic_sha256=candidate.semantic_sha256,
        )
    conflict_refused = False
    try:
        store.append_event(
            ledger.discovery_id,
            DiscoveryEventKindV1.TRAINING_EVALUATED,
            payload=_development_result(
                typed_candidates[0].semantic_sha256,
                "TRAIN",
                manifest.roots("train"),
                manifest.candidates[0].training_score + 1,
            ),
            candidate_semantic_sha256=typed_candidates[0].semantic_sha256,
        )
    except DiscoveryStoreError as error:
        conflict_refused = error.code == "CONFLICTING_DISCOVERY_RESULT"
    training_order = tuple(
        sorted(
            range(len(typed_candidates)),
            key=lambda index: (
                -manifest.candidates[index].training_score,
                typed_candidates[index].semantic_sha256,
            ),
        )
    )
    frozen = tuple(typed_candidates[index].semantic_sha256 for index in training_order)
    freeze_projection = {
        "candidate_semantic_sha256": list(frozen),
        "training_star_semantic_sha256": frozen[0],
    }
    freeze_sha256 = hashlib.sha256(canonical_identity_bytes(freeze_projection)).hexdigest()
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.CANDIDATES_FROZEN,
        payload={**freeze_projection, "freeze_sha256": freeze_sha256},
    )
    pre_reveal = build_lineage_report(store.load(ledger.discovery_id))
    sealed_before = (
        pre_reveal.as_dict()["terminal_references"]["status"]
        == "SEALED_UNTIL_ATOMIC_TERMINAL_REVEAL_V1"
    )
    for candidate, spec in zip(typed_candidates, manifest.candidates, strict=True):
        store.append_event(
            ledger.discovery_id,
            DiscoveryEventKindV1.VALIDATION_EVALUATED,
            payload=_development_result(
                candidate.semantic_sha256,
                "VALIDATION",
                manifest.roots("validation"),
                spec.validation_score,
            ),
            candidate_semantic_sha256=candidate.semantic_sha256,
        )
        if candidate.semantic_sha256 != selected.semantic_sha256:
            request_sha256 = hashlib.sha256(
                canonical_identity_bytes(
                    {"candidate": candidate.semantic_sha256, "partition": "VALIDATION"}
                )
            ).hexdigest()
            store.append_event(
                ledger.discovery_id,
                DiscoveryEventKindV1.REJECTION_RECORDED,
                payload={
                    "rejection_reason": "VALIDATION_NOT_SELECTED",
                    "request_sha256": request_sha256,
                },
                candidate_semantic_sha256=candidate.semantic_sha256,
            )
    selection_projection = {
        "selected_candidate_semantic_sha256": selected.semantic_sha256,
        "validation_score": manifest.candidates[selected_index].validation_score,
    }
    selection_sha256 = hashlib.sha256(
        canonical_identity_bytes(selection_projection)
    ).hexdigest()
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.SELECTION_FROZEN,
        payload={
            "sealed_material_commitment_sha256": primary_material.commitment_sha256,
            "selected_candidate_semantic_sha256": selected.semantic_sha256,
            "selection_sha256": selection_sha256,
        },
    )
    robustness = replace(
        build_synthetic_robustness_evidence(SyntheticRobustnessModeV1.PASS),
        candidate_semantic_sha256=selected.semantic_sha256,
        base_semantic_sha256=base_semantic,
    )
    robustness_decision = qualify_robustness(robustness)
    store.record_robustness(ledger.discovery_id, robustness, robustness_decision)
    store.consume_reveal_token(
        ledger.discovery_id,
        primary_material,
        reveal_token=manifest.reveal_token,
    )
    repeat_refused = False
    try:
        store.consume_reveal_token(
            ledger.discovery_id,
            primary_material,
            reveal_token=manifest.reveal_token,
        )
    except DiscoveryStoreError as error:
        repeat_refused = error.code == "REVEAL_ALREADY_CONSUMED"
    revealed_report = build_lineage_report(store.load(ledger.discovery_id))
    revealed_after = revealed_report.as_dict()["terminal_references"]["status"] == "REVEALED"
    selected_spec = manifest.candidates[selected_index]
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.HOLDOUT_EVALUATED,
        payload=_development_result(
            selected.semantic_sha256,
            "HOLDOUT",
            manifest.roots("holdout"),
            selected_spec.holdout_score,
        ),
        candidate_semantic_sha256=selected.semantic_sha256,
    )
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.ADVERSARIAL_EVALUATED,
        payload=_development_result(
            selected.semantic_sha256,
            "ADVERSARIAL_HOLDOUT",
            manifest.roots("adversarial"),
            selected_spec.adversarial_score,
        ),
        candidate_semantic_sha256=selected.semantic_sha256,
    )
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.WARNING_RECORDED,
        payload={
            "warning_code": "DEVELOPMENT_SYNTHETIC_ONLY",
            "warning_detail": "fixture evidence is not live profitability evidence",
        },
    )
    store.close(
        ledger.discovery_id,
        ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE,
        conclusion_detail="all named synthetic development partitions qualified",
    )
    primary = store.load(ledger.discovery_id)
    reopened = DiscoveryStore(store_root).load(ledger.discovery_id)
    verification = DiscoveryStore(store_root).verify(ledger.discovery_id)
    final_report = build_lineage_report(reopened)
    comparison = compare_strategies(
        DiscoveryStore(store_root),
        typed_candidates[0].semantic_sha256,
        typed_candidates[1].semantic_sha256,
    )
    other_ledgers = (
        _terminal_path_fixture(
            store,
            manifest,
            head,
            base_semantic,
            typed_candidates[0].semantic_sha256,
            "no-winner",
            ScientificConclusionV1.NO_CANDIDATE_MET_CRITERIA,
        ),
        _terminal_path_fixture(
            store,
            manifest,
            head,
            base_semantic,
            typed_candidates[0].semantic_sha256,
            "insufficient",
            ScientificConclusionV1.INSUFFICIENT_EVIDENCE,
        ),
        _terminal_path_fixture(
            store,
            manifest,
            head,
            base_semantic,
            typed_candidates[0].semantic_sha256,
            "invalid",
            ScientificConclusionV1.EXPERIMENT_INVALID,
        ),
    )
    all_ledgers = (primary, *other_ledgers)
    all_verifications = tuple(store.verify(item.discovery_id) for item in all_ledgers)
    real_access_count = sum(
        int(payload.get("real_partition_access_count", 0))
        for item in all_ledgers
        for record in item.records
        for payload in (dict(_payload(record)),)
    )
    outcomes = tuple(
        sorted(
            (
                item.scientific_outcome.value
                for item in all_ledgers
                if item.scientific_outcome is not None
            )
        )
    )
    return DevelopmentLineageDemoResultV1(
        primary_discovery_id=primary.discovery_id,
        discovery_ids=tuple(sorted(item.discovery_id for item in all_ledgers)),
        ledger_sha256=primary.ledger_sha256,
        report_sha256=final_report.report_sha256,
        comparison_sha256=comparison.comparison_sha256,
        outcomes=outcomes,
        record_count=len(primary.records),
        crash_reopen_passed=primary.ledger_sha256 == reopened.ledger_sha256,
        conflict_refused=conflict_refused,
        repeat_reveal_refused=repeat_refused,
        sealed_before_reveal=sealed_before,
        revealed_after_consume=revealed_after,
        real_partition_access_count=real_access_count,
        verification_passed=verification.passed
        and all(item.passed for item in all_verifications),
    )


def _manifest_roots(
    manifest: StrategySearchManifestV1,
    partition: StrategyPartitionV1,
) -> tuple[int, ...]:
    payload = dict(manifest.payload)
    raw_partitions = payload.get("partitions")
    if not isinstance(raw_partitions, Mapping):
        raise ControlledEvidenceInsufficientError(
            "PARTITION_MANIFEST_UNAVAILABLE",
            "controlled manifest has no typed partition inventory",
        )
    key = {
        StrategyPartitionV1.TRAIN: "train",
        StrategyPartitionV1.VALIDATION: "validation",
        StrategyPartitionV1.HOLDOUT: "holdout",
        StrategyPartitionV1.ADVERSARIAL_HOLDOUT: "adversarial",
        StrategyPartitionV1.ROBUSTNESS: "robustness",
    }[partition]
    raw = raw_partitions.get(key)
    if not isinstance(raw, (list, tuple)) or any(type(item) is not int for item in raw):
        raise ControlledEvidenceInsufficientError(
            "PARTITION_ROOTS_UNAVAILABLE",
            f"controlled {partition.value} roots are unavailable",
        )
    return tuple(raw)


def _controlled_root_specs(
    manifest: StrategySearchManifestV1,
    partition: StrategyPartitionV1,
    *,
    environment_tag: str = "UNPERTURBED",
    event_intensity_ppm: int = POLICY_SCALE_V1,
    initial_queue_ppm: int = POLICY_SCALE_V1,
    scenario_shift: int = 0,
) -> tuple[ControlledRootSpecV1, ...]:
    roots = _manifest_roots(manifest, partition)
    cells = _CONTROLLED_CELLS[partition]
    expected_count = len(cells) * len(_CONTROLLED_SCENARIO_FAMILIES)
    if len(roots) != expected_count:
        raise ControlledEvidenceInsufficientError(
            "PARTITION_CELL_MAPPING_INVALID",
            f"{partition.value} root count differs from its exact family/cell product",
        )
    rows: list[ControlledRootSpecV1] = []
    for ordinal, root in enumerate(roots):
        family_ordinal = ordinal % len(_CONTROLLED_SCENARIO_FAMILIES)
        cell_ordinal = ordinal // len(_CONTROLLED_SCENARIO_FAMILIES)
        scenario_family, scenario_name = _CONTROLLED_SCENARIO_FAMILIES[
            (family_ordinal + scenario_shift) % len(_CONTROLLED_SCENARIO_FAMILIES)
        ]
        volume, liquidity = cells[cell_ordinal]
        rows.append(
            ControlledRootSpecV1(
                partition,
                root,
                scenario_family,
                scenario_name,
                cell_ordinal,
                volume,
                liquidity,
                environment_tag,
                event_intensity_ppm,
                initial_queue_ppm,
            )
        )
    return tuple(rows)


def _decision_times(duration_us: int) -> tuple[int, ...]:
    last = duration_us - _CONTROLLED_DECISION_END_GUARD_US
    if last < _CONTROLLED_DECISION_START_US:
        raise ControlledEvidenceInsufficientError(
            "DECISION_WINDOW_EMPTY",
            "controlled source duration has no legal decision time",
        )
    return tuple(
        range(
            _CONTROLLED_DECISION_START_US,
            last + 1,
            _CONTROLLED_DECISION_INTERVAL_US,
        )
    )


def _flow_event_id(root_seed: int, event: FlowEvent) -> str:
    return f"FLOW-{root_seed:07d}-{event.sequence:010d}"


def _mul_ppm(left: int, right: int) -> int:
    return round_div_even(left * right, POLICY_SCALE_V1)


def _case_codes(spec: ControlledRootSpecV1) -> tuple[str, ...]:
    cases = _CONTROLLED_CASES[spec.partition]
    tail = cases[1:]
    shift = spec.root_seed % len(tail)
    return (cases[0], *tail[shift:], *tail[:shift])


def _queue_scale_ppm(spec: ControlledRootSpecV1) -> int:
    return _mul_ppm(
        _mul_ppm(
            _VOLUME_DISPLAYED_QUEUE_PPM[spec.volume],
            _LIQUIDITY_QUEUE_PPM[spec.liquidity],
        ),
        spec.initial_queue_ppm,
    )


def _fixed_flow_event(
    sequence: int,
    simulation_time_us: int,
    family: FlowEventFamily,
    command: dict[str, object],
) -> FlowEvent:
    return FlowEvent(
        sequence,
        simulation_time_us,
        family,
        True,
        command,
        None,
        None,
        None,
    )


def _build_fixed_source_events(spec: ControlledRootSpecV1) -> tuple[FlowEvent, ...]:
    sequence = 0
    events: list[FlowEvent] = []
    previous_ids: tuple[str, ...] = ()
    root_bid = 1_000 + (spec.root_seed % 7) * 10
    queue_scale = _queue_scale_ppm(spec)
    for ordinal, (decision_time, case_id) in enumerate(
        zip(_decision_times(_CONTROLLED_DURATION_SECONDS * 1_000_000), _case_codes(spec), strict=True)
    ):
        setup_time = decision_time - 1
        prefix = f"SRC-{spec.root_seed:07d}-{ordinal:03d}"
        base_bid = root_bid + ordinal * 10
        if previous_ids:
            sequence += 1
            events.append(
                _fixed_flow_event(
                    sequence,
                    setup_time,
                    FlowEventFamily.CANCEL_BID,
                    {
                        "affected_orders": [
                            {
                                "command_id": f"{prefix}-CANCEL-{index:02d}",
                                "target_order_id": target,
                            }
                            for index, target in enumerate(previous_ids)
                        ],
                        "command_id": f"{prefix}-CANCEL-BATCH",
                        "order_type": OrderType.CANCEL.value,
                        "target_order_id": previous_ids[0],
                    },
                )
            )
        spread_ticks, imbalance_ppm, fill_target = _CONTROLLED_CASE_PARAMETERS[case_id]
        raw_bid = 1_000 + imbalance_ppm // 1_000
        raw_ask = 2_000 - raw_bid
        bid_quantity = max(1, _mul_ppm(raw_bid, queue_scale))
        ask_quantity = max(1, _mul_ppm(raw_ask, queue_scale))
        if bid_quantity <= 100:
            raise ControlledEvidenceInsufficientError(
                "CONTROLLED_QUEUE_TOO_SMALL",
                "controlled best-bid queue cannot preserve the four-fill exit fixture",
            )
        lower_ids = tuple(f"{prefix}-LOWER-{index}" for index in range(4))
        best_slice_ids = tuple(f"{prefix}-BID-SLICE-{index}" for index in range(4))
        best_bid_id = f"{prefix}-BID-REMAINDER"
        ask_id = f"{prefix}-ASK"
        for order_id in lower_ids:
            sequence += 1
            events.append(
                _fixed_flow_event(
                    sequence,
                    setup_time,
                    FlowEventFamily.LIMIT_BUY,
                    {
                        "order_id": order_id,
                        "order_type": OrderType.LIMIT.value,
                        "price_ticks": base_bid - 1,
                        "quantity": 25,
                        "side": Side.BUY.value,
                    },
                )
            )
        for order_id in best_slice_ids:
            sequence += 1
            events.append(
                _fixed_flow_event(
                    sequence,
                    setup_time,
                    FlowEventFamily.LIMIT_BUY,
                    {
                        "order_id": order_id,
                        "order_type": OrderType.LIMIT.value,
                        "price_ticks": base_bid,
                        "quantity": 25,
                        "side": Side.BUY.value,
                    },
                )
            )
        sequence += 1
        events.append(
            _fixed_flow_event(
                sequence,
                setup_time,
                FlowEventFamily.LIMIT_BUY,
                {
                    "order_id": best_bid_id,
                    "order_type": OrderType.LIMIT.value,
                    "price_ticks": base_bid,
                    "quantity": bid_quantity - 100,
                    "side": Side.BUY.value,
                },
            )
        )
        sequence += 1
        events.append(
            _fixed_flow_event(
                sequence,
                setup_time,
                FlowEventFamily.LIMIT_SELL,
                {
                    "order_id": ask_id,
                    "order_type": OrderType.LIMIT.value,
                    "price_ticks": base_bid + spread_ticks,
                    "quantity": ask_quantity,
                    "side": Side.SELL.value,
                },
            )
        )
        sequence += 1
        events.append(
            _fixed_flow_event(
                sequence,
                decision_time + 10_000,
                FlowEventFamily.MARKET_SELL,
                {
                    "order_id": f"{prefix}-CONTRA",
                    "order_type": OrderType.MARKET.value,
                    "quantity": bid_quantity + fill_target,
                    "side": Side.SELL.value,
                },
            )
        )
        previous_ids = (*lower_ids, *best_slice_ids, best_bid_id, ask_id)
    return tuple(events)


def _midpoint_x2(book: OrderBook) -> int | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return book.best_bid + book.best_ask


def _best_level_quantity(book: OrderBook, side: Side) -> int:
    price = book.best_bid if side is Side.BUY else book.best_ask
    if price is None:
        return 0
    levels = book.bids if side is Side.BUY else book.asks
    return levels[price].total_quantity


def _book_imbalance_ppm(book: OrderBook) -> int | None:
    bid = _best_level_quantity(book, Side.BUY)
    ask = _best_level_quantity(book, Side.SELL)
    denominator = bid + ask
    if denominator <= 0:
        return None
    return round_div_even((bid - ask) * POLICY_SCALE_V1, denominator)


def _apply_fixed_flow_event(
    book: OrderBook,
    event: FlowEvent,
) -> tuple[SimulationEvent, ...]:
    command = event.command
    if not event.applied or command is None:
        return ()
    order_type = OrderType(str(command["order_type"]))
    if order_type is OrderType.LIMIT:
        return book.process(
            Order.limit(
                str(command["order_id"]),
                Side(str(command["side"])),
                int(command["quantity"]),
                int(command["price_ticks"]),
            ),
            validate=False,
        )
    if order_type is OrderType.MARKET:
        return book.process(
            Order.market(
                str(command["order_id"]),
                Side(str(command["side"])),
                int(command["quantity"]),
            ),
            validate=False,
        )
    affected = command.get("affected_orders")
    if isinstance(affected, (list, tuple)):
        rows = tuple(
            (str(item["command_id"]), str(item["target_order_id"]))
            for item in affected
            if isinstance(item, Mapping)
        )
    else:
        rows = ((str(command["command_id"]), str(command["target_order_id"])),)
    captured: list[SimulationEvent] = []
    for command_id, target_order_id in rows:
        captured.extend(book.cancel(target_order_id, command_id, validate=False))
    return tuple(captured)


def _fork_active_book(book: OrderBook) -> OrderBook:
    """Fork the observable queue state without replaying unrelated prior history."""

    fork = OrderBook()
    for prices, levels in (
        (book.bid_prices, book.bids),
        (book.ask_prices, book.asks),
    ):
        for price in prices:
            for view in levels[price].orders:
                if view.side is None or view.price_ticks is None:
                    raise RuntimeError("active fixed-tape order is not a priced limit")
                fork.process(
                    Order.limit(
                        view.order_id,
                        view.side,
                        view.remaining_quantity,
                        view.price_ticks,
                        view.owner,
                    ),
                    validate=False,
                )
    fork.assert_invariants()
    return fork


def _reference_label(
    *,
    spec: ControlledRootSpecV1,
    decision_time_us: int,
    decision_book: OrderBook,
    future_events: tuple[FlowEvent, ...],
    source_tape_sha256: str,
) -> ReferenceDecisionLabelV1:
    book = _fork_active_book(decision_book)
    decision_midpoint = _midpoint_x2(book)
    decision_spread = (
        None
        if book.best_bid is None or book.best_ask is None
        else book.best_ask - book.best_bid
    )
    entry_time = decision_time_us + 1
    horizon = decision_time_us + 2_000_001
    interval_events = tuple(
        event
        for event in future_events
        if entry_time <= event.simulation_time_us < horizon
    )
    for event in interval_events:
        if event.simulation_time_us > entry_time:
            break
        _apply_fixed_flow_event(book, event)
    probe_id = f"REFERENCE-{spec.root_seed:07d}-{decision_time_us:012d}"
    observed_bid = book.best_bid
    if observed_bid is not None:
        book.process(
            Order.limit(
                probe_id,
                Side.BUY,
                _CONTROLLED_OBJECTIVE_SHARES,
                observed_bid,
                OrderOwner.PLAYER,
            )
        )
    future_midpoints: list[int] = []
    already_applied = {
        event.sequence
        for event in interval_events
        if event.simulation_time_us <= entry_time
    }
    for event in interval_events:
        if event.sequence in already_applied:
            midpoint = _midpoint_x2(book)
            if midpoint is not None:
                future_midpoints.append(midpoint)
            continue
        _apply_fixed_flow_event(book, event)
        midpoint = _midpoint_x2(book)
        if midpoint is not None:
            future_midpoints.append(midpoint)
    if probe_id in book.active_orders:
        book.cancel(probe_id, f"{probe_id}-CANCEL", validate=False)
    book.assert_invariants()
    terminal_midpoint = _midpoint_x2(book)
    if terminal_midpoint is not None:
        future_midpoints.append(terminal_midpoint)
    fill_quantity = (
        0
        if observed_bid is None
        else book.all_orders[probe_id].filled_quantity
    )
    adverse = (
        None
        if decision_midpoint is None or not future_midpoints
        else max(0, decision_midpoint - min(future_midpoints))
    )
    if (
        decision_spread is not None
        and fill_quantity >= 80
        and adverse is not None
        and decision_spread <= 2
        and adverse <= 2
    ):
        state = CandidateSignalV1.GREEN
    elif (
        decision_spread is not None
        and fill_quantity >= 20
        and adverse is not None
        and decision_spread <= 4
        and adverse <= 4
    ):
        state = CandidateSignalV1.WAIT
    else:
        state = CandidateSignalV1.RED
    source_ids = tuple(_flow_event_id(spec.root_seed, item) for item in interval_events)
    if not source_ids:
        source_ids = (f"SOURCE-CUT-{spec.root_seed:07d}-{horizon:012d}",)
    oracle_projection = {
        "adverse_move_mid_x2": adverse,
        "decision_mid_x2": decision_midpoint,
        "decision_spread_ticks": decision_spread,
        "fill_quantity": fill_quantity,
        "horizon_exclusive_us": horizon,
        "source_tape_sha256": source_tape_sha256,
    }
    return bind_reference_decision_label(
        label_id=(
            f"LABEL:{spec.partition.value}:{spec.root_seed}:{decision_time_us}"
        ),
        root_seed=spec.root_seed,
        decision_time_us=decision_time_us,
        reference_state=state,
        opportunity=state is CandidateSignalV1.GREEN,
        source_event_ids=source_ids,
        oracle_sha256=hashlib.sha256(
            canonical_identity_bytes(oracle_projection)
        ).hexdigest(),
    )


def _build_controlled_source_tape(
    spec: ControlledRootSpecV1,
) -> ControlledSourceTapeV1:
    events = _build_fixed_source_events(spec)
    source_projection = {
        "events": [item.as_dict() for item in events],
        "spec": spec.as_dict(),
    }
    tape_sha256 = hashlib.sha256(
        canonical_identity_bytes(source_projection)
    ).hexdigest()
    replay = OrderBook()
    event_index = 0
    labels: list[ReferenceDecisionLabelV1] = []
    for decision_time in _decision_times(_CONTROLLED_DURATION_SECONDS * 1_000_000):
        while (
            event_index < len(events)
            and events[event_index].simulation_time_us <= decision_time
        ):
            event = events[event_index]
            _apply_fixed_flow_event(replay, event)
            event_index += 1
        labels.append(
            _reference_label(
                spec=spec,
                decision_time_us=decision_time,
                decision_book=replay,
                future_events=events[event_index:],
                source_tape_sha256=tape_sha256,
            )
        )
    reference_sha256 = hashlib.sha256(
        canonical_identity_bytes([item.as_dict() for item in labels])
    ).hexdigest()
    replay.assert_invariants()
    return ControlledSourceTapeV1(
        spec,
        events,
        tuple(labels),
        tape_sha256,
        reference_sha256,
    )


def _relative_volume_ppm(spec: ControlledRootSpecV1) -> int:
    return _mul_ppm(_VOLUME_RELATIVE_PPM[spec.volume], spec.event_intensity_ppm)


def _aggressive_source_volume(
    events: tuple[FlowEvent, ...],
    *,
    decision_time_us: int,
    window_us: int,
    side: Side,
) -> int:
    lower = decision_time_us - window_us
    total = 0
    for event in events:
        if not lower <= event.simulation_time_us <= decision_time_us:
            continue
        command = event.command
        if command is None or command.get("order_type") != OrderType.MARKET.value:
            continue
        if command.get("side") == side.value:
            total += int(command["quantity"])
    return total


def _capture_player_fills(
    book: OrderBook,
    *,
    start_index: int,
    simulation_time_us: int,
    arrival_midpoints: Mapping[str, int],
    maker_fee_milliticks_per_share: int,
    taker_fee_milliticks_per_share: int,
) -> tuple[_ControlledFillV1, ...]:
    rows: list[_ControlledFillV1] = []
    for fill in book.fills[start_index:]:
        if fill.owner is not OrderOwner.PLAYER:
            continue
        arrival = arrival_midpoints.get(fill.order_id)
        if arrival is None:
            raise ControlledEvidenceInsufficientError(
                "ARRIVAL_MIDPOINT_MISSING",
                f"player fill {fill.order_id} lacks its frozen arrival midpoint",
            )
        rows.append(
            _ControlledFillV1(
                simulation_time_us,
                fill.order_id,
                fill.side,
                fill.liquidity,
                fill.quantity,
                fill.price_ticks,
                arrival,
                (
                    maker_fee_milliticks_per_share
                    if fill.liquidity == "maker"
                    else taker_fee_milliticks_per_share
                ),
            )
        )
    return tuple(rows)


def _run_controlled_root(
    candidate: SearchCandidateV1,
    tape: ControlledSourceTapeV1,
    *,
    source_override: bytes | None = None,
    decision_latency_us: int = 1,
    routing_latency_us: int = 0,
    maker_fee_milliticks_per_share: int = 0,
    taker_fee_milliticks_per_share: int = 0,
) -> ControlledRootRunV1:
    source = candidate.source if source_override is None else source_override
    definition = parse_strategy(source.decode("utf-8"))
    if not isinstance(definition, StrategyDefinition):
        raise ControlledEvidenceInsufficientError(
            "CONTROLLED_STRATEGY_KIND_INVALID",
            "controlled discovery requires a traffic-light strategy",
        )
    book = OrderBook()
    runtime = TrafficLightRuntime(
        definition,
        Decimal(_relative_volume_ppm(tape.spec)) / Decimal(POLICY_SCALE_V1),
    )
    runtime.reset(0, book)
    queue: list[tuple[int, int, int, str, object]] = []
    for event in tape.events:
        heapq.heappush(
            queue,
            (event.simulation_time_us, 0, event.sequence, "SOURCE", event),
        )
    for ordinal, label in enumerate(tape.labels):
        heapq.heappush(
            queue,
            (label.decision_time_us, 2, ordinal, "DECISION", label),
        )
    action_sequence = 1_000_000
    entry_scheduled = False
    entry_order_id: str | None = None
    arrival_midpoints: dict[str, int] = {}
    fills: list[_ControlledFillV1] = []
    source_midpoints: list[tuple[int, int | None]] = []
    projections: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    counts = ControlledDecisionCountsV1.zero()
    processed_source = 0
    while queue:
        simulation_time_us, _priority, _ordinal, kind, payload = heapq.heappop(queue)
        if kind == "SOURCE":
            event = payload
            assert isinstance(event, FlowEvent)
            fill_start = len(book.fills)
            exchange_events = _apply_fixed_flow_event(book, event)
            runtime.observe(simulation_time_us, exchange_events, book)
            fills.extend(
                _capture_player_fills(
                    book,
                    start_index=fill_start,
                    simulation_time_us=simulation_time_us,
                    arrival_midpoints=arrival_midpoints,
                    maker_fee_milliticks_per_share=maker_fee_milliticks_per_share,
                    taker_fee_milliticks_per_share=taker_fee_milliticks_per_share,
                )
            )
            source_midpoints.append((simulation_time_us, _midpoint_x2(book)))
            processed_source += 1
            continue
        if kind == "DECISION":
            label = payload
            assert isinstance(label, ReferenceDecisionLabelV1)
            runtime.observe(simulation_time_us, (), book)
            midpoint = _midpoint_x2(book)
            imbalance = _book_imbalance_ppm(book)
            if midpoint is None or imbalance is None or runtime.current is None:
                raise ControlledEvidenceInsufficientError(
                    "UNAVAILABLE_DECISION_OBSERVATION",
                    f"root {tape.spec.root_seed} decision {simulation_time_us} is one-sided",
                )
            assert book.best_ask is not None and book.best_bid is not None
            spread = book.best_ask - book.best_bid
            pending = sum(
                item.owner is OrderOwner.PLAYER
                for item in book.active_orders.values()
            )
            observable_projection = {
                "book_state_sha256": book.state_sha256(),
                "decision_id": label.label_id,
                "decision_time_us": simulation_time_us,
                "root_seed": tape.spec.root_seed,
                "source_tape_sha256": tape.source_tape_sha256,
            }
            observation = ObservableDecisionInputV1(
                label.label_id,
                tape.spec.root_seed,
                simulation_time_us,
                hashlib.sha256(
                    canonical_identity_bytes(observable_projection)
                ).hexdigest(),
                ObservationStatusV1.AVAILABLE,
                spread,
                imbalance,
                _relative_volume_ppm(tape.spec),
                _aggressive_source_volume(
                    tape.events,
                    decision_time_us=simulation_time_us,
                    window_us=definition.window_us,
                    side=Side.BUY,
                ),
                _aggressive_source_volume(
                    tape.events,
                    decision_time_us=simulation_time_us,
                    window_us=definition.window_us,
                    side=Side.SELL,
                ),
                book.player_position.position,
                pending,
            )
            signal = CandidateSignalV1(runtime.current.state.value)
            permission = (
                CandidatePermissionV1.ALLOW
                if signal is CandidateSignalV1.GREEN
                else CandidatePermissionV1.DENY
            )
            projection = project_candidate_decision(
                observation,
                label,
                candidate_state=signal,
                permission=permission,
            )
            scored = score_candidate_decision(projection, label)
            projections.append(projection.as_dict())
            reference_green = int(label.reference_state is CandidateSignalV1.GREEN)
            reference_wait = int(label.reference_state is CandidateSignalV1.WAIT)
            reference_red = int(label.reference_state is CandidateSignalV1.RED)
            eligible = int(bool(reference_wait or reference_red))
            predicted = int(
                signal is CandidateSignalV1.GREEN
                and permission is CandidatePermissionV1.ALLOW
            )
            counts += ControlledDecisionCountsV1(
                int(scored.classification_correct and bool(reference_green)),
                reference_green,
                int(scored.classification_correct and bool(reference_wait)),
                reference_wait,
                int(scored.classification_correct and bool(reference_red)),
                reference_red,
                eligible,
                int(projection.discipline_violation),
                int(predicted and label.opportunity),
                predicted,
                int(label.opportunity),
                int(scored.false_green),
                int(not label.opportunity),
                int(scored.missed_opportunity),
            )
            if predicted and not entry_scheduled:
                observed_bid = book.best_bid
                if observed_bid is None:
                    raise ControlledEvidenceInsufficientError(
                        "ENTRY_BID_MISSING",
                        "GREEN decision lacks its observed best bid",
                    )
                entry_scheduled = True
                entry_order_id = (
                    f"PLAYER-{tape.spec.root_seed:07d}-{candidate.semantic_sha256[:12]}-ENTRY"
                )
                action_sequence += 1
                heapq.heappush(
                    queue,
                    (
                        simulation_time_us + decision_latency_us + routing_latency_us,
                        1,
                        action_sequence,
                        "ENTRY",
                        {"order_id": entry_order_id, "price_ticks": observed_bid},
                    ),
                )
            continue
        action = payload
        assert isinstance(action, Mapping)
        if kind == "ENTRY":
            order_id = str(action["order_id"])
            midpoint = _midpoint_x2(book)
            if midpoint is None:
                raise ControlledEvidenceInsufficientError(
                    "ARRIVAL_MIDPOINT_MISSING",
                    "entry arrival has no two-sided midpoint",
                )
            arrival_midpoints[order_id] = midpoint
            fill_start = len(book.fills)
            exchange_events = book.process(
                Order.limit(
                    order_id,
                    Side.BUY,
                    _CONTROLLED_OBJECTIVE_SHARES,
                    int(action["price_ticks"]),
                    OrderOwner.PLAYER,
                )
            )
            runtime.observe(simulation_time_us, exchange_events, book)
            fills.extend(
                _capture_player_fills(
                    book,
                    start_index=fill_start,
                    simulation_time_us=simulation_time_us,
                    arrival_midpoints=arrival_midpoints,
                    maker_fee_milliticks_per_share=maker_fee_milliticks_per_share,
                    taker_fee_milliticks_per_share=taker_fee_milliticks_per_share,
                )
            )
            actions.append(
                {"kind": kind, "order_id": order_id, "simulation_time_us": simulation_time_us}
            )
            action_sequence += 1
            heapq.heappush(
                queue,
                (
                    simulation_time_us + 2_000_000,
                    1,
                    action_sequence,
                    "CANCEL",
                    {"order_id": order_id},
                ),
            )
            continue
        if kind == "CANCEL":
            order_id = str(action["order_id"])
            view = book.all_orders[order_id]
            filled_quantity = view.filled_quantity
            if order_id in book.active_orders:
                exchange_events = book.cancel(
                    order_id,
                    f"{order_id}-CANCEL",
                )
                runtime.observe(simulation_time_us, exchange_events, book)
            actions.append(
                {
                    "filled_entry_quantity": filled_quantity,
                    "kind": kind,
                    "order_id": order_id,
                    "simulation_time_us": simulation_time_us,
                }
            )
            if filled_quantity > 0:
                action_sequence += 1
                heapq.heappush(
                    queue,
                    (
                        simulation_time_us + 1,
                        1,
                        action_sequence,
                        "EXIT",
                        {"quantity": filled_quantity},
                    ),
                )
            continue
        if kind != "EXIT":
            raise AssertionError(kind)
        quantity = int(action["quantity"])
        exit_id = (
            f"PLAYER-{tape.spec.root_seed:07d}-{candidate.semantic_sha256[:12]}-EXIT"
        )
        midpoint = _midpoint_x2(book)
        if midpoint is None:
            raise ControlledEvidenceInsufficientError(
                "ARRIVAL_MIDPOINT_MISSING",
                "exit arrival has no two-sided midpoint",
            )
        arrival_midpoints[exit_id] = midpoint
        fill_start = len(book.fills)
        exchange_events = book.process(
            Order.market(exit_id, Side.SELL, quantity, OrderOwner.PLAYER)
        )
        runtime.observe(simulation_time_us, exchange_events, book)
        fills.extend(
            _capture_player_fills(
                book,
                start_index=fill_start,
                simulation_time_us=simulation_time_us,
                arrival_midpoints=arrival_midpoints,
                maker_fee_milliticks_per_share=maker_fee_milliticks_per_share,
                taker_fee_milliticks_per_share=taker_fee_milliticks_per_share,
            )
        )
        actions.append(
            {
                "kind": kind,
                "order_id": exit_id,
                "quantity": quantity,
                "simulation_time_us": simulation_time_us,
            }
        )
    if not entry_scheduled or entry_order_id is None:
        raise ControlledEvidenceInsufficientError(
            "NO_EXECUTABLE_ENTRY",
            f"candidate {candidate.semantic_sha256} produced no GREEN/ALLOW entry",
        )
    book.assert_invariants()
    if processed_source != len(tape.events):
        raise RuntimeError("fixed source tape was not completely replayed")
    if not fills:
        raise ControlledEvidenceInsufficientError(
            "ZERO_FILL_DENOMINATOR",
            f"candidate {candidate.semantic_sha256} produced no player fills",
        )
    total_quantity = sum(item.quantity for item in fills)
    spread_numerator = 0
    adverse_numerator = 0
    last_midpoint = next(
        (midpoint for _time, midpoint in reversed(source_midpoints) if midpoint is not None),
        None,
    )
    for fill in fills:
        signed_spread = (
            fill.price_ticks * 1_000
            - fill.arrival_mid_x2 * 500
            + fill.fee_milliticks_per_share
            if fill.side is Side.BUY
            else fill.arrival_mid_x2 * 500
            - fill.price_ticks * 1_000
            + fill.fee_milliticks_per_share
        )
        spread_numerator += fill.quantity * signed_spread
        horizon = fill.simulation_time_us + 2_000_000
        horizon_midpoint = next(
            (
                midpoint
                for time_us, midpoint in source_midpoints
                if time_us >= horizon and midpoint is not None
            ),
            last_midpoint,
        )
        if horizon_midpoint is None:
            raise ControlledEvidenceInsufficientError(
                "HORIZON_MIDPOINT_MISSING",
                f"fill {fill.order_id} lacks a two-sided adverse horizon",
            )
        signed_adverse = (
            fill.price_ticks * 1_000 - horizon_midpoint * 500
            if fill.side is Side.BUY
            else horizon_midpoint * 500 - fill.price_ticks * 1_000
        )
        adverse_numerator += fill.quantity * signed_adverse
    spread_cost = round_div_even(spread_numerator, total_quantity)
    adverse_cost = round_div_even(adverse_numerator, total_quantity)
    bought = sum(item.quantity for item in fills if item.side is Side.BUY)
    sold = sum(item.quantity for item in fills if item.side is Side.SELL)
    completed = min(_CONTROLLED_OBJECTIVE_SHARES, bought, sold)
    complexity_cost = clamp(
        round_div_even(candidate.complexity_points * POLICY_SCALE_V1, 200),
        0,
        POLICY_SCALE_V1,
    )
    execution_utilities = (
        (StrategyObjectiveIdV1.ADVERSE_SELECTION, signed_cost_utility(adverse_cost)),
        (
            StrategyObjectiveIdV1.COMPLETION,
            completion_utility(
                completed_shares=completed,
                objective_shares=_CONTROLLED_OBJECTIVE_SHARES,
            ),
        ),
        (StrategyObjectiveIdV1.COMPLEXITY, POLICY_SCALE_V1 - complexity_cost),
        (StrategyObjectiveIdV1.SPREAD_PAID, signed_cost_utility(spread_cost)),
        (
            StrategyObjectiveIdV1.TURNOVER,
            turnover_utility(
                traded_shares=total_quantity,
                objective_shares=_CONTROLLED_OBJECTIVE_SHARES,
            ),
        ),
    )
    execution_projection = {
        "actions": actions,
        "fills": [item.as_dict() for item in fills],
        "final_book_state_sha256": book.state_sha256(),
        "source_tape_sha256": tape.source_tape_sha256,
    }
    return ControlledRootRunV1(
        tape.spec,
        candidate.semantic_sha256,
        counts,
        execution_utilities,
        completed,
        total_quantity,
        len(fills),
        spread_cost,
        adverse_cost,
        hashlib.sha256(canonical_identity_bytes(projections)).hexdigest(),
        hashlib.sha256(canonical_identity_bytes(execution_projection)).hexdigest(),
        tape.source_tape_sha256,
        tape.reference_inventory_sha256,
        True,
        True,
    )


def _pooled_utilities(
    runs: tuple[ControlledRootRunV1, ...],
) -> dict[StrategyObjectiveIdV1, int]:
    counts = ControlledDecisionCountsV1.zero()
    for run in runs:
        counts += run.counts
    return {
        StrategyObjectiveIdV1.BALANCED_CLASSIFICATION: (
            balanced_classification_utility(
                correct_green=counts.correct_green,
                reference_green=counts.reference_green,
                correct_wait=counts.correct_wait,
                reference_wait=counts.reference_wait,
                correct_red=counts.correct_red,
                reference_red=counts.reference_red,
            )
        ),
        StrategyObjectiveIdV1.DISCIPLINE_COMPATIBILITY: (
            discipline_compatibility_utility(
                violations=counts.discipline_violations,
                eligible=counts.discipline_eligible,
            )
        ),
        StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY: (
            execution_opportunity_utility(
                true_positive=counts.opportunity_true_positive,
                predicted_green_allow=counts.predicted_green_allow,
                true_opportunities=counts.true_opportunities,
            )
        ),
        StrategyObjectiveIdV1.FALSE_GREEN: false_green_utility(
            false_green=counts.false_green,
            non_green=counts.non_green,
        ),
        StrategyObjectiveIdV1.MISSED_OPPORTUNITY: missed_opportunity_utility(
            missed=counts.missed_opportunities,
            true_opportunities=counts.true_opportunities,
        ),
    }


def _root_utility_map(
    run: ControlledRootRunV1,
    pooled: Mapping[StrategyObjectiveIdV1, int],
    *,
    stability: int | None,
) -> dict[StrategyObjectiveIdV1, int | None]:
    values: dict[StrategyObjectiveIdV1, int | None] = {
        **pooled,
        **dict(run.execution_utilities),
        StrategyObjectiveIdV1.CROSS_CELL_STABILITY: stability,
    }
    required = {item.objective_id for item in REQUIRED_OBJECTIVE_SPECS_V1}
    if set(values) != required:
        raise RuntimeError("controlled root objective inventory changed")
    return values


def _composite(
    values: Mapping[StrategyObjectiveIdV1, int | None],
) -> int:
    return root_composite(
        tuple(
            ObjectiveValueV1(
                objective_id,
                (
                    ObjectiveApplicabilityV1.NOT_APPLICABLE
                    if utility is None
                    else ObjectiveApplicabilityV1.APPLICABLE
                ),
                utility,
            )
            for objective_id, utility in sorted(
                values.items(), key=lambda item: item[0].value
            )
        )
    )


def _partition_utility_rows(
    runs: tuple[ControlledRootRunV1, ...],
) -> tuple[
    dict[StrategyObjectiveIdV1, int],
    dict[int, dict[StrategyObjectiveIdV1, int | None]],
]:
    pooled = _pooled_utilities(runs)
    without_stability = {
        run.spec.root_seed: _root_utility_map(run, pooled, stability=None)
        for run in runs
    }
    cell_medians = tuple(
        nearest_rank_p50(
            tuple(
                _composite(without_stability[run.spec.root_seed])
                for run in runs
                if run.spec.cell_ordinal == cell_ordinal
            )
        )
        for cell_ordinal in sorted({item.spec.cell_ordinal for item in runs})
    )
    stability = (
        cross_cell_stability_utility(cell_medians)
        if len(cell_medians) >= 2
        else None
    )
    return pooled, {
        run.spec.root_seed: _root_utility_map(run, pooled, stability=stability)
        for run in runs
    }


def _evaluate_controlled_partition(
    manifest: StrategySearchManifestV1,
    candidate: SearchCandidateV1,
    base: SearchCandidateV1,
    tapes: tuple[ControlledSourceTapeV1, ...],
    *,
    base_runs: tuple[ControlledRootRunV1, ...] | None = None,
    candidate_source_override: bytes | None = None,
    base_source_override: bytes | None = None,
    decision_latency_us: int = 1,
    routing_latency_us: int = 0,
    maker_fee_milliticks_per_share: int = 0,
    taker_fee_milliticks_per_share: int = 0,
) -> ControlledPartitionRunV1:
    candidate_runs = tuple(
        _run_controlled_root(
            candidate,
            tape,
            source_override=candidate_source_override,
            decision_latency_us=decision_latency_us,
            routing_latency_us=routing_latency_us,
            maker_fee_milliticks_per_share=maker_fee_milliticks_per_share,
            taker_fee_milliticks_per_share=taker_fee_milliticks_per_share,
        )
        for tape in tapes
    )
    resolved_base_runs = (
        tuple(
            _run_controlled_root(
                base,
                tape,
                source_override=base_source_override,
                decision_latency_us=decision_latency_us,
                routing_latency_us=routing_latency_us,
                maker_fee_milliticks_per_share=maker_fee_milliticks_per_share,
                taker_fee_milliticks_per_share=taker_fee_milliticks_per_share,
            )
            for tape in tapes
        )
        if base_runs is None
        else base_runs
    )
    if tuple(item.spec for item in candidate_runs) != tuple(
        item.spec for item in resolved_base_runs
    ):
        raise RuntimeError("candidate and base controlled roots differ")
    candidate_pooled, candidate_values = _partition_utility_rows(candidate_runs)
    base_pooled, base_values = _partition_utility_rows(resolved_base_runs)
    roots = tuple(item.spec.root_seed for item in candidate_runs)
    root_deltas = tuple(
        RootDeltaV1(
            root,
            _composite(candidate_values[root]) - _composite(base_values[root]),
        )
        for root in roots
    )
    pooled_ids = {
        StrategyObjectiveIdV1.BALANCED_CLASSIFICATION,
        StrategyObjectiveIdV1.DISCIPLINE_COMPATIBILITY,
        StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY,
        StrategyObjectiveIdV1.FALSE_GREEN,
        StrategyObjectiveIdV1.MISSED_OPPORTUNITY,
    }
    component_deltas: list[ComponentDeltaV1] = []
    for objective_id in (item.objective_id for item in REQUIRED_OBJECTIVE_SPECS_V1):
        if objective_id in pooled_ids:
            delta = candidate_pooled[objective_id] - base_pooled[objective_id]
        elif objective_id is StrategyObjectiveIdV1.CROSS_CELL_STABILITY:
            candidate_stability = candidate_values[roots[0]][objective_id]
            base_stability = base_values[roots[0]][objective_id]
            if candidate_stability is None or base_stability is None:
                raise ControlledEvidenceInsufficientError(
                    "STABILITY_NOT_APPLICABLE",
                    f"partition {tapes[0].spec.partition.value} has fewer than two cells",
                )
            delta = candidate_stability - base_stability
        else:
            delta = nearest_rank_p50(
                tuple(
                    int(candidate_values[root][objective_id])
                    - int(base_values[root][objective_id])
                    for root in roots
                )
            )
        component_deltas.append(ComponentDeltaV1(objective_id, delta))
    evidence = CandidatePartitionEvidenceV1(
        candidate.candidate_id,
        candidate.semantic_sha256,
        tapes[0].spec.partition,
        manifest.compatibility,
        root_deltas,
        tuple(component_deltas),
        sum(item.trade_count for item in candidate_runs),
        sum(item.trade_count for item in resolved_base_runs),
        candidate.complexity_points,
        REFERENCE_EXECUTION_ORACLE_ID_V1,
    )
    return ControlledPartitionRunV1(
        evidence,
        candidate_runs,
        resolved_base_runs,
        tuple(
            (
                root,
                tuple(
                    (objective_id, int(utility))
                    for objective_id, utility in sorted(
                        candidate_values[root].items(), key=lambda item: item[0].value
                    )
                    if utility is not None
                ),
            )
            for root in roots
        ),
        tuple(
            (
                root,
                tuple(
                    (objective_id, int(utility))
                    for objective_id, utility in sorted(
                        base_values[root].items(), key=lambda item: item[0].value
                    )
                    if utility is not None
                ),
            )
            for root in roots
        ),
    )


def _controlled_partition_tapes(
    manifest: StrategySearchManifestV1,
    partition: StrategyPartitionV1,
) -> tuple[ControlledSourceTapeV1, ...]:
    return tuple(
        _build_controlled_source_tape(spec)
        for spec in _controlled_root_specs(manifest, partition)
    )


def _window_token(window_us: int) -> str:
    if window_us % 1_000_000 == 0:
        return f"{window_us // 1_000_000}s"
    if window_us % 1_000 == 0:
        return f"{window_us // 1_000}ms"
    raise ControlledEvidenceInsufficientError(
        "ROBUSTNESS_WINDOW_NOT_RENDERABLE",
        "robustness window is not an exact strategy-language duration",
    )


def _ppm_token(value: int) -> str:
    whole, remainder = divmod(value, POLICY_SCALE_V1)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:06d}".rstrip("0")


def _environment_source(environment: ControlledRobustnessEnvironmentV1) -> bytes:
    return (
        "setup BOUNDED_ROBUSTNESS_V1\n"
        f"window {_window_token(environment.window_us)}\n"
        "unavailable REFUSE\n"
        "GREEN when\n"
        f"spread_ticks <= {environment.green_spread_ticks}\n"
        f"book_imbalance >= {_ppm_token(environment.green_imbalance_ppm)}\n"
        "WAIT when\n"
        f"spread_ticks <= {environment.wait_spread_ticks}\n"
        "RED otherwise\n"
    ).encode("utf-8")


def _candidate_environment(
    candidate: SearchCandidateV1,
    *,
    candidate_template: bool,
) -> ControlledRobustnessEnvironmentV1:
    window_us, green_ticks, imbalance_ppm, wait_ticks = candidate.oracle_values
    return replace(
        controlled_robustness_environment(candidate=candidate_template),
        window_us=window_us,
        green_spread_ticks=green_ticks,
        green_imbalance_ppm=imbalance_ppm,
        wait_spread_ticks=wait_ticks,
    )


def _robustness_spec(
    manifest: StrategySearchManifestV1,
    root_index: int,
    environment: ControlledRobustnessEnvironmentV1,
    family: RobustnessFamilyV1,
    scalar: int,
) -> ControlledRootSpecV1:
    scenario_shift = (
        -1
        if family is RobustnessFamilyV1.REGIME_MIX and scalar < 0
        else 1
        if family is RobustnessFamilyV1.REGIME_MIX
        else 0
    )
    base_spec = _controlled_root_specs(
        manifest,
        StrategyPartitionV1.ROBUSTNESS,
        environment_tag=f"{family.value}:{scalar}",
        event_intensity_ppm=environment.volume.relative_volume_ppm,
        initial_queue_ppm=_mul_ppm(
            environment.volume.displayed_queue_ppm,
            environment.liquidity.queue_size_ppm,
        ),
        scenario_shift=scenario_shift,
    )[root_index]
    return base_spec


def _run_controlled_robustness(
    manifest: StrategySearchManifestV1,
    selected: SearchCandidateV1,
    base: SearchCandidateV1,
) -> tuple[RobustnessEvidenceV1, RobustnessQualificationV1]:
    candidate_baseline = _candidate_environment(selected, candidate_template=True)
    base_baseline = _candidate_environment(base, candidate_template=False)
    families: list[RobustnessFamilyEvidenceV1] = []
    per_cell_ids = tuple(
        item.objective_id
        for item in REQUIRED_OBJECTIVE_SPECS_V1
        if item.objective_id
        not in {
            StrategyObjectiveIdV1.BALANCED_CLASSIFICATION,
            StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY,
            StrategyObjectiveIdV1.CROSS_CELL_STABILITY,
        }
    )
    for family in MANDATORY_ROBUSTNESS_FAMILIES_V1:
        settings = tuple(
            item for item in ROBUSTNESS_SETTINGS_V1 if item.family is family
        )
        raw_cells: list[
            tuple[str, int, ControlledRootRunV1, ControlledRootRunV1]
        ] = []
        for root_index, root_seed in enumerate(ROBUSTNESS_ROOTS_V1):
            for setting in settings:
                candidate_probe = apply_robustness_setting(candidate_baseline, setting)
                base_probe = apply_robustness_setting(base_baseline, setting)
                if (
                    candidate_probe.status is not PerturbationStatusV1.APPLIED
                    or base_probe.status is not PerturbationStatusV1.APPLIED
                    or candidate_probe.environment is None
                    or base_probe.environment is None
                    or candidate_probe.changed_paths != base_probe.changed_paths
                ):
                    raise ControlledEvidenceInsufficientError(
                        "ROBUSTNESS_SETTING_UNAVAILABLE",
                        f"{family.value}/{setting.setting_id} was not identically applicable",
                    )
                assert setting.scalar is not None
                spec = _robustness_spec(
                    manifest,
                    root_index,
                    candidate_probe.environment,
                    family,
                    setting.scalar,
                )
                if spec.root_seed != root_seed:
                    raise RuntimeError("robustness root order changed")
                tape = _build_controlled_source_tape(spec)
                candidate_run = _run_controlled_root(
                    selected,
                    tape,
                    source_override=_environment_source(candidate_probe.environment),
                    decision_latency_us=candidate_probe.environment.decision_latency_us,
                    routing_latency_us=candidate_probe.environment.routing_latency_us,
                    maker_fee_milliticks_per_share=(
                        candidate_probe.environment.maker_fee_milliticks_per_share
                    ),
                    taker_fee_milliticks_per_share=(
                        candidate_probe.environment.taker_fee_milliticks_per_share
                    ),
                )
                base_run = _run_controlled_root(
                    base,
                    tape,
                    source_override=_environment_source(base_probe.environment),
                    decision_latency_us=base_probe.environment.decision_latency_us,
                    routing_latency_us=base_probe.environment.routing_latency_us,
                    maker_fee_milliticks_per_share=(
                        base_probe.environment.maker_fee_milliticks_per_share
                    ),
                    taker_fee_milliticks_per_share=(
                        base_probe.environment.taker_fee_milliticks_per_share
                    ),
                )
                raw_cells.append(
                    (setting.setting_id, root_seed, candidate_run, base_run)
                )
        candidate_pooled = _pooled_utilities(
            tuple(item[2] for item in raw_cells)
        )
        base_pooled = _pooled_utilities(tuple(item[3] for item in raw_cells))
        pooled_classification = (
            candidate_pooled[StrategyObjectiveIdV1.BALANCED_CLASSIFICATION]
            - base_pooled[StrategyObjectiveIdV1.BALANCED_CLASSIFICATION]
        )
        pooled_opportunity = (
            candidate_pooled[StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY]
            - base_pooled[StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY]
        )
        cells: list[RobustnessCellV1] = []
        for setting_id, root_seed, candidate_run, base_run in raw_cells:
            candidate_values = _root_utility_map(
                candidate_run,
                _pooled_utilities((candidate_run,)),
                stability=None,
            )
            base_values = _root_utility_map(
                base_run,
                _pooled_utilities((base_run,)),
                stability=None,
            )
            components = tuple(
                ComponentDeltaV1(
                    objective_id,
                    int(candidate_values[objective_id])
                    - int(base_values[objective_id]),
                )
                for objective_id in per_cell_ids
            )
            composite_delta = robustness_composite_delta(
                components,
                pooled_classification_delta=pooled_classification,
                pooled_opportunity_delta=pooled_opportunity,
            )
            cells.append(
                RobustnessCellV1(
                    family,
                    setting_id,
                    root_seed,
                    composite_delta,
                    components,
                    candidate_run.trade_count,
                    base_run.trade_count,
                    True,
                    candidate_run.completed_shares == _CONTROLLED_OBJECTIVE_SHARES
                    and base_run.completed_shares == _CONTROLLED_OBJECTIVE_SHARES,
                    candidate_run.replay_valid and base_run.replay_valid,
                    candidate_run.invariant_clean and base_run.invariant_clean,
                )
            )
        families.append(
            RobustnessFamilyEvidenceV1(
                family,
                tuple(cells),
                pooled_classification,
                pooled_opportunity,
            )
        )
    evidence = RobustnessEvidenceV1(
        selected.semantic_sha256,
        base.semantic_sha256,
        candidate_baseline.environment_sha256,
        base_baseline.environment_sha256,
        tuple(families),
        VenueMixDeclarationV1(
            PerturbationStatusV1.NOT_APPLICABLE,
            SINGLE_VENUE_CAPABILITY_ID_V1,
        ),
    )
    return evidence, qualify_robustness(evidence)


def _controlled_result_payload(
    partition_run: ControlledPartitionRunV1,
    *,
    trained_candidate_count: int,
    qualification: Mapping[str, object] | None = None,
) -> dict[str, object]:
    result = partition_run.as_dict(trained_candidate_count)
    if qualification is not None:
        result["qualification"] = dict(qualification)
    return {
        "data_source": CONTROLLED_DATA_SOURCE_V1,
        "evidence_sha256": hashlib.sha256(
            canonical_identity_bytes(result)
        ).hexdigest(),
        "partition": partition_run.evidence.partition.value,
        "real_partition_access_count": partition_run.real_partition_access_count,
        "result": result,
    }


def _terminal_inventory_commitment(
    manifest: StrategySearchManifestV1,
    partition: StrategyPartitionV1,
) -> str:
    return hashlib.sha256(
        canonical_identity_bytes(
            {
                "data_source": CONTROLLED_DATA_SOURCE_V1,
                "manifest_sha256": manifest.manifest_sha256,
                "partition": partition.value,
                "roots": list(_manifest_roots(manifest, partition)),
            }
        )
    ).hexdigest()


def _execute_controlled_discovery(
    store: DiscoveryStore,
    discovery_id: str,
    source: bytes,
    manifest: StrategySearchManifestV1,
    reveal_token: str,
) -> None:
    base_ast = parse_strategy_ast(source.decode("utf-8"))
    space = manifest.search_space
    base = space.base_candidate()
    universe = space.universe()[:64]
    if len(universe) != 64:
        raise ControlledEvidenceInsufficientError(
            "CONTROLLED_UNIVERSE_INCOMPLETE",
            "the committed grid has fewer than 64 valid non-base candidates",
        )
    train_tapes = _controlled_partition_tapes(manifest, StrategyPartitionV1.TRAIN)
    train_base_runs = tuple(_run_controlled_root(base, tape) for tape in train_tapes)
    trained: list[TrainedCandidateV1] = []
    train_partition_runs: dict[str, ControlledPartitionRunV1] = {}
    for candidate in universe:
        store.append_event(
            discovery_id,
            DiscoveryEventKindV1.MUTATION_RECORDED,
            payload=_mutation_payload(base_ast, candidate.source, candidate.semantic_sha256),
            candidate_semantic_sha256=candidate.semantic_sha256,
            parent_semantic_sha256=base.semantic_sha256,
        )
        partition_run = _evaluate_controlled_partition(
            manifest,
            candidate,
            base,
            train_tapes,
            base_runs=train_base_runs,
        )
        train_partition_runs[candidate.semantic_sha256] = partition_run
        row = TrainedCandidateV1(candidate, partition_run.evidence)
        trained.append(row)
        store.append_event(
            discovery_id,
            DiscoveryEventKindV1.TRAINING_EVALUATED,
            payload=_controlled_result_payload(
                partition_run,
                trained_candidate_count=64,
            ),
            candidate_semantic_sha256=candidate.semantic_sha256,
        )
    ranked = tuple(
        sorted(trained, key=lambda item: _final_training_rank_key(item, 64))
    )
    frozen = ranked[: manifest.finalist_limit]
    frozen_ids = tuple(item.candidate.semantic_sha256 for item in frozen)
    freeze_projection = {
        "candidate_semantic_sha256": list(frozen_ids),
        "training_star_semantic_sha256": frozen_ids[0],
    }
    store.append_event(
        discovery_id,
        DiscoveryEventKindV1.CANDIDATES_FROZEN,
        payload={
            **freeze_projection,
            "freeze_sha256": hashlib.sha256(
                canonical_identity_bytes(freeze_projection)
            ).hexdigest(),
        },
    )
    validation_tapes = _controlled_partition_tapes(
        manifest, StrategyPartitionV1.VALIDATION
    )
    validation_base_runs = tuple(
        _run_controlled_root(base, tape) for tape in validation_tapes
    )
    finalists: list[ValidatedCandidateV1] = []
    validation_partition_runs: dict[str, ControlledPartitionRunV1] = {}
    for row in frozen:
        partition_run = _evaluate_controlled_partition(
            manifest,
            row.candidate,
            base,
            validation_tapes,
            base_runs=validation_base_runs,
        )
        validation_partition_runs[row.candidate.semantic_sha256] = partition_run
        qualification = validation_qualification(
            partition_run.evidence,
            trained_candidate_count=64,
        )
        validated = ValidatedCandidateV1(row, partition_run.evidence, qualification)
        finalists.append(validated)
        store.append_event(
            discovery_id,
            DiscoveryEventKindV1.VALIDATION_EVALUATED,
            payload=_controlled_result_payload(
                partition_run,
                trained_candidate_count=64,
                qualification=qualification.as_dict(),
            ),
            candidate_semantic_sha256=row.candidate.semantic_sha256,
        )
        if not qualification.qualified:
            training_median = row.evidence.statistic(64).median_delta
            validation_median = partition_run.evidence.statistic(64).median_delta
            overfit = (
                row.candidate.semantic_sha256 == frozen_ids[0]
                and training_median >= 80_000
                and validation_median <= 0
            )
            reasons = (
                ("TRAIN_VALIDATION_DIVERGENCE", *qualification.reasons)
                if overfit
                else qualification.reasons
            )
            request_projection = {
                "candidate": row.candidate.semantic_sha256,
                "partition": StrategyPartitionV1.VALIDATION.value,
                "reasons": list(reasons),
            }
            store.append_event(
                discovery_id,
                DiscoveryEventKindV1.REJECTION_RECORDED,
                payload={
                    "rejection_reason": ",".join(reasons),
                    "request_sha256": hashlib.sha256(
                        canonical_identity_bytes(request_projection)
                    ).hexdigest(),
                },
                candidate_semantic_sha256=row.candidate.semantic_sha256,
            )
    qualified = tuple(item for item in finalists if item.qualification.qualified)
    if not qualified:
        store.close(
            discovery_id,
            ScientificConclusionV1.NO_CANDIDATE_MET_CRITERIA,
            conclusion_detail="no frozen real-Kirby2 validation finalist met the committed criteria",
        )
        return
    selected_row = min(
        qualified, key=lambda item: _validation_rank_key(item, 64)
    )
    training_star = finalists[0]
    if (
        selected_row.trained.candidate.semantic_sha256 == frozen_ids[0]
        or training_star.qualification.qualified
        or training_star.trained.evidence.statistic(64).median_delta < 80_000
        or training_star.evidence.statistic(64).median_delta > 0
    ):
        raise ControlledEvidenceInsufficientError(
            "TRAIN_VALIDATION_DIVERGENCE_MISSING",
            "the frozen training star was not a distinct validation-rejected overfit candidate",
        )
    selected = selected_row.trained.candidate
    material = seal_terminal_material(
        candidate_semantic_sha256=selected.semantic_sha256,
        holdout_manifest_sha256=manifest.partition_manifest_sha256,
        holdout_member_inventory_sha256=_terminal_inventory_commitment(
            manifest, StrategyPartitionV1.HOLDOUT
        ),
        adversarial_manifest_sha256=manifest.partition_manifest_sha256,
        adversarial_member_inventory_sha256=_terminal_inventory_commitment(
            manifest, StrategyPartitionV1.ADVERSARIAL_HOLDOUT
        ),
        reveal_token=reveal_token,
    )
    selection_projection = {
        "selected_candidate_semantic_sha256": selected.semantic_sha256,
        "validation_evidence_sha256": hashlib.sha256(
            canonical_identity_bytes(
                validation_partition_runs[selected.semantic_sha256].evidence.as_dict(64)
            )
        ).hexdigest(),
    }
    store.append_event(
        discovery_id,
        DiscoveryEventKindV1.SELECTION_FROZEN,
        payload={
            "sealed_material_commitment_sha256": material.commitment_sha256,
            "selected_candidate_semantic_sha256": selected.semantic_sha256,
            "selection_sha256": hashlib.sha256(
                canonical_identity_bytes(selection_projection)
            ).hexdigest(),
        },
    )
    robustness, robustness_decision = _run_controlled_robustness(
        manifest, selected, base
    )
    store.record_robustness(
        discovery_id,
        robustness,
        robustness_decision,
        data_source=CONTROLLED_DATA_SOURCE_V1,
        real_partition_access_count=sum(
            len(item.cells) * 2 for item in robustness.families
        ),
    )
    if robustness_decision.outcome is not RobustnessOutcomeV1.PASSED:
        store.close(
            discovery_id,
            (
                ScientificConclusionV1.EXPERIMENT_INVALID
                if robustness_decision.outcome is RobustnessOutcomeV1.EXPERIMENT_INVALID
                else ScientificConclusionV1.INSUFFICIENT_EVIDENCE
            ),
            conclusion_detail="the frozen selected candidate did not pass robustness",
        )
        return
    store.consume_reveal_token(
        discovery_id,
        material,
        reveal_token=reveal_token,
    )
    holdout_tapes = _controlled_partition_tapes(
        manifest, StrategyPartitionV1.HOLDOUT
    )
    holdout_base_runs = tuple(
        _run_controlled_root(base, tape) for tape in holdout_tapes
    )
    holdout_run = _evaluate_controlled_partition(
        manifest,
        selected,
        base,
        holdout_tapes,
        base_runs=holdout_base_runs,
    )
    holdout_decision = qualify_holdout(
        holdout_run.evidence,
        validation_partition_runs[selected.semantic_sha256].evidence,
    )
    store.append_event(
        discovery_id,
        DiscoveryEventKindV1.HOLDOUT_EVALUATED,
        payload=_controlled_result_payload(
            holdout_run,
            trained_candidate_count=64,
            qualification=holdout_decision.as_dict(),
        ),
        candidate_semantic_sha256=selected.semantic_sha256,
    )
    adversarial_tapes = _controlled_partition_tapes(
        manifest, StrategyPartitionV1.ADVERSARIAL_HOLDOUT
    )
    adversarial_base_runs = tuple(
        _run_controlled_root(base, tape) for tape in adversarial_tapes
    )
    adversarial_run = _evaluate_controlled_partition(
        manifest,
        selected,
        base,
        adversarial_tapes,
        base_runs=adversarial_base_runs,
    )
    adversarial_decision = qualify_adversarial(
        adversarial_run.evidence,
        trained_candidate_count=64,
    )
    store.append_event(
        discovery_id,
        DiscoveryEventKindV1.ADVERSARIAL_EVALUATED,
        payload=_controlled_result_payload(
            adversarial_run,
            trained_candidate_count=64,
            qualification=adversarial_decision.as_dict(),
        ),
        candidate_semantic_sha256=selected.semantic_sha256,
    )
    confirmed = holdout_decision.qualified and adversarial_decision.qualified
    store.close(
        discovery_id,
        (
            ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE
            if confirmed
            else ScientificConclusionV1.INSUFFICIENT_EVIDENCE
        ),
        conclusion_detail=(
            "validation, robustness, holdout, and adversarial evidence qualified within the named simulator scope"
            if confirmed
            else "one or more revealed terminal partitions did not meet the frozen criteria"
        ),
    )
    verification = store.verify(discovery_id)
    if not verification.passed:
        raise DiscoveryStoreError(
            "CONTROLLED_LINEAGE_VERIFICATION_FAILED",
            ",".join(verification.failures),
        )


def run_frozen_search_to_store(
    *,
    base_path: Path,
    experiment_path: Path,
    budget: int,
    evidence_root: Path,
    repository: Path,
) -> str:
    preflight = inspect_execution_preflight(
        repository=repository,
        base_path=base_path,
        manifest_path=experiment_path,
        evidence_root=evidence_root,
    )
    preflight.require()
    subject = _git(repository, "show", "-s", "--format=%s", "HEAD").strip()
    if subject != CONTROLLED_IMPLEMENTATION_COMMIT_SUBJECT_V1:
        raise DiscoveryStoreError(
            "IMPLEMENTATION_COMMIT_SUBJECT_MISMATCH",
            "controlled discovery requires the exact frozen WO35-F implementation commit",
        )
    if budget != 64:
        raise DiscoveryStoreError(
            "INVALID_SEARCH_BUDGET",
            "controlled discovery requires the exact budget 64",
        )
    manifest = load_search_manifest(experiment_path)
    source = base_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != CONTROLLED_BASE_SOURCE_SHA256_V1:
        raise DiscoveryStoreError(
            "BASE_SOURCE_DIGEST_MISMATCH",
            "base source differs from the committed controlled identity",
        )
    token = _execution_token(preflight.implementation_commit, manifest)
    binding = DiscoveryBindingV1(
        experiment_id=manifest.experiment_id,
        implementation_commit=preflight.implementation_commit,
        base_source_sha256=hashlib.sha256(source).hexdigest(),
        base_semantic_sha256=strategy_semantic_sha256(
            parse_strategy_ast(source.decode("utf-8"))
        ),
        experiment_manifest_sha256=manifest.manifest_sha256,
        partition_manifest_sha256=manifest.partition_manifest_sha256,
        robustness_policy_sha256=robustness_policy_sha256(),
        reveal_token_sha256=_token_sha256(token),
        development_only=False,
        real_partition_execution=True,
    )
    store = DiscoveryStore(evidence_root)
    ledger = store.create(binding)
    try:
        _execute_controlled_discovery(
            store,
            ledger.discovery_id,
            source,
            manifest,
            token,
        )
    except ControlledEvidenceInsufficientError as error:
        current = store.load(ledger.discovery_id)
        if current.scientific_outcome is None:
            store.append_event(
                ledger.discovery_id,
                DiscoveryEventKindV1.WARNING_RECORDED,
                payload={
                    "warning_code": error.code,
                    "warning_detail": error.detail,
                },
            )
            store.close(
                ledger.discovery_id,
                ScientificConclusionV1.INSUFFICIENT_EVIDENCE,
                conclusion_detail=f"{error.code}: {error.detail}",
            )
    except Exception as error:
        current = store.load(ledger.discovery_id)
        if current.scientific_outcome is None:
            store.append_event(
                ledger.discovery_id,
                DiscoveryEventKindV1.WARNING_RECORDED,
                payload={
                    "warning_code": "CONTROLLED_EXECUTION_EXCEPTION",
                    "warning_detail": f"{type(error).__name__}: {error}",
                },
            )
            store.close(
                ledger.discovery_id,
                ScientificConclusionV1.EXPERIMENT_INVALID,
                conclusion_detail="controlled execution raised an unexpected exception",
            )
        raise
    return ledger.discovery_id


def validate_controlled_evidence(
    *,
    manifest_path: Path,
    evidence_root: Path,
) -> dict[str, object]:
    manifest = load_search_manifest(manifest_path)
    if not evidence_root.is_dir() or evidence_root.is_symlink():
        return {
            "manifest_sha256": manifest.manifest_sha256,
            "reason_code": CONTROLLED_EVIDENCE_REASON_V1,
            "status": "NOT_EXERCISED",
        }
    store = DiscoveryStore(evidence_root)
    matches = []
    for discovery_id in store.list_discoveries():
        ledger = store.load(discovery_id)
        if ledger.binding.experiment_manifest_sha256 == manifest.manifest_sha256:
            matches.append((ledger, store.verify(discovery_id)))
    confirmed = tuple(
        (ledger, verification)
        for ledger, verification in matches
        if not ledger.binding.development_only
        and ledger.binding.real_partition_execution
        and ledger.scientific_outcome
        is ScientificConclusionV1.CONFIRMED_WITHIN_DECLARED_SCOPE
        and verification.passed
    )
    if len(confirmed) != 1:
        return {
            "matching_discovery_count": len(matches),
            "manifest_sha256": manifest.manifest_sha256,
            "reason_code": CONTROLLED_EVIDENCE_REASON_V1,
            "status": "NOT_EXERCISED",
        }
    ledger, verification = confirmed[0]
    failures: list[str] = []
    if ledger.binding.base_source_sha256 != CONTROLLED_BASE_SOURCE_SHA256_V1:
        failures.append("controlled base source digest differs")
    if ledger.binding.partition_manifest_sha256 != manifest.partition_manifest_sha256:
        failures.append("controlled partition manifest digest differs")
    if ledger.binding.robustness_policy_sha256 != robustness_policy_sha256():
        failures.append("controlled robustness policy digest differs")
    repository = Path(__file__).resolve().parents[2]
    try:
        subject = _git(
            repository,
            "show",
            "-s",
            "--format=%s",
            ledger.binding.implementation_commit,
        ).strip()
    except DiscoveryStoreError as error:
        failures.append(error.code)
    else:
        if subject != CONTROLLED_IMPLEMENTATION_COMMIT_SUBJECT_V1:
            failures.append("controlled implementation commit subject differs")
    by_kind = {
        kind: tuple(item for item in ledger.records if item.event_kind is kind)
        for kind in DiscoveryEventKindV1
    }
    training = by_kind[DiscoveryEventKindV1.TRAINING_EVALUATED]
    validation = by_kind[DiscoveryEventKindV1.VALIDATION_EVALUATED]
    freezes = by_kind[DiscoveryEventKindV1.CANDIDATES_FROZEN]
    selections = by_kind[DiscoveryEventKindV1.SELECTION_FROZEN]
    robustness = by_kind[DiscoveryEventKindV1.ROBUSTNESS_EVALUATED]
    reveals = by_kind[DiscoveryEventKindV1.TERMINAL_REVEALED]
    holdout = by_kind[DiscoveryEventKindV1.HOLDOUT_EVALUATED]
    adversarial = by_kind[DiscoveryEventKindV1.ADVERSARIAL_EVALUATED]
    if len(training) != 64 or len({item.candidate_semantic_sha256 for item in training}) != 64:
        failures.append("controlled training inventory is not 64 unique candidates")
    if len(freezes) != 1:
        failures.append("controlled finalist freeze count differs")
        frozen_ids: tuple[str, ...] = ()
        training_star = None
    else:
        freeze_payload = _payload(freezes[0])
        raw_frozen = freeze_payload.get("candidate_semantic_sha256")
        frozen_ids = (
            tuple(str(item) for item in raw_frozen)
            if isinstance(raw_frozen, list)
            else ()
        )
        training_star = str(freeze_payload.get("training_star_semantic_sha256"))
        if len(frozen_ids) != 8 or training_star != frozen_ids[0]:
            failures.append("controlled finalist freeze inventory differs")
    if len(validation) != 8 or {
        item.candidate_semantic_sha256 for item in validation
    } != set(frozen_ids):
        failures.append("controlled validation inventory differs from frozen finalists")
    selected = ledger.selected_candidate_semantic_sha256
    if len(selections) != 1 or selected is None or selected == training_star:
        failures.append("controlled selected candidate is absent or equals training star")
    star_rejections = tuple(
        item
        for item in by_kind[DiscoveryEventKindV1.REJECTION_RECORDED]
        if item.candidate_semantic_sha256 == training_star
        and "TRAIN_VALIDATION_DIVERGENCE" in str(
            _payload(item).get("rejection_reason", "")
        )
    )
    if len(star_rejections) != 1:
        failures.append("training star lacks exact divergence rejection evidence")
    star_train = next(
        (item for item in training if item.candidate_semantic_sha256 == training_star),
        None,
    )
    star_validation = next(
        (item for item in validation if item.candidate_semantic_sha256 == training_star),
        None,
    )
    if star_train is None or star_validation is None:
        failures.append("training star partition evidence is incomplete")
    else:
        train_result = _payload(star_train).get("result")
        validation_result = _payload(star_validation).get("result")
        try:
            train_median = int(
                _nested_value(train_result, "evidence", "statistic", "median_delta")
            )
            validation_median = int(
                _nested_value(
                    validation_result,
                    "evidence",
                    "statistic",
                    "median_delta",
                )
            )
        except (KeyError, TypeError, ValueError):
            failures.append("training star statistics are unavailable")
        else:
            if train_median < 80_000 or validation_median > 0:
                failures.append("training star divergence predicate does not hold")
    expected_terminal = (
        (robustness, "ROBUSTNESS", 128),
        (holdout, "HOLDOUT", 16),
        (adversarial, "ADVERSARIAL_HOLDOUT", 16),
    )
    for records, partition, access_count in expected_terminal:
        if len(records) != 1:
            failures.append(f"controlled {partition} result count differs")
            continue
        payload = _payload(records[0])
        if (
            payload.get("data_source") != CONTROLLED_DATA_SOURCE_V1
            or payload.get("partition") != partition
            or payload.get("real_partition_access_count") != access_count
        ):
            failures.append(f"controlled {partition} binding differs")
    if len(reveals) != 1:
        failures.append("controlled terminal reveal count differs")
    if all(
        len(items) == 1
        for items in (robustness, reveals, holdout, adversarial)
    ) and not (
        robustness[0].ordinal
        < reveals[0].ordinal
        < holdout[0].ordinal
        < adversarial[0].ordinal
    ):
        failures.append("controlled protected access order differs")
    for records in (holdout, adversarial):
        if records:
            result = _payload(records[0]).get("result")
            try:
                qualified = _nested_value(result, "qualification", "qualified")
            except (KeyError, TypeError):
                qualified = False
            if qualified is not True:
                failures.append("controlled terminal qualification did not pass")
    if robustness:
        robust_result = _payload(robustness[0]).get("result")
        try:
            robust_outcome = _nested_value(
                robust_result,
                "qualification",
                "outcome",
            )
        except (KeyError, TypeError):
            robust_outcome = None
        if robust_outcome != RobustnessOutcomeV1.PASSED.value:
            failures.append("controlled robustness qualification did not pass")
    if not verification.passed:
        failures.extend(verification.failures)
    return {
        "discovery_id": ledger.discovery_id,
        "failures": failures,
        "ledger_sha256": ledger.ledger_sha256,
        "manifest_sha256": manifest.manifest_sha256,
        "selected_candidate_semantic_sha256": selected,
        "status": "PASS" if not failures else "FAIL",
        "training_star_semantic_sha256": training_star,
        "verification": verification.as_dict(),
    }


def _terminal_path_fixture(
    store: DiscoveryStore,
    manifest: LineageDevelopmentManifestV1,
    head: str,
    base_semantic: str,
    candidate_semantic: str,
    suffix: str,
    outcome: ScientificConclusionV1,
):
    token = f"{manifest.reveal_token}/{suffix}"
    material = _terminal_material(
        manifest,
        candidate_semantic,
        reveal_token=token,
    )
    binding = _development_binding(
        manifest,
        head,
        base_semantic,
        material.reveal_token_sha256,
        experiment_suffix=suffix,
    )
    ledger = store.create(binding)
    if outcome is ScientificConclusionV1.EXPERIMENT_INVALID:
        store.close(
            ledger.discovery_id,
            outcome,
            conclusion_detail="development protocol violation fixture",
        )
        return store.load(ledger.discovery_id)
    candidate_source = ControlledSearchSpaceV1(
        controlled_search_parameters()
    ).candidate_for_vector(
        ControlledSearchSpaceV1(controlled_search_parameters()).vector(
            (
                manifest.candidates[0].green_spread_ticks,
                manifest.candidates[0].green_imbalance_ppm,
                manifest.candidates[0].wait_spread_ticks,
                manifest.candidates[0].window_us,
            )
        ),
        universe_ordinal=0,
    )
    assert candidate_source is not None
    base_ast = parse_strategy_ast(manifest.base_source.decode("utf-8"))
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.MUTATION_RECORDED,
        payload=_mutation_payload(
            base_ast,
            candidate_source.source,
            candidate_source.semantic_sha256,
        ),
        candidate_semantic_sha256=candidate_source.semantic_sha256,
        parent_semantic_sha256=base_semantic,
    )
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.TRAINING_EVALUATED,
        payload=_development_result(
            candidate_source.semantic_sha256,
            "TRAIN",
            manifest.roots("train"),
            manifest.candidates[0].training_score,
        ),
        candidate_semantic_sha256=candidate_source.semantic_sha256,
    )
    freeze_projection = {
        "candidate_semantic_sha256": [candidate_source.semantic_sha256],
        "training_star_semantic_sha256": candidate_source.semantic_sha256,
    }
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.CANDIDATES_FROZEN,
        payload={
            **freeze_projection,
            "freeze_sha256": hashlib.sha256(
                canonical_identity_bytes(freeze_projection)
            ).hexdigest(),
        },
    )
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.VALIDATION_EVALUATED,
        payload=_development_result(
            candidate_source.semantic_sha256,
            "VALIDATION",
            manifest.roots("validation"),
            manifest.candidates[0].validation_score,
        ),
        candidate_semantic_sha256=candidate_source.semantic_sha256,
    )
    if outcome is ScientificConclusionV1.NO_CANDIDATE_MET_CRITERIA:
        store.close(
            ledger.discovery_id,
            outcome,
            conclusion_detail="development no-winner terminal path",
        )
        return store.load(ledger.discovery_id)
    selection = {
        "selected_candidate_semantic_sha256": candidate_source.semantic_sha256,
        "fixture": suffix,
    }
    store.append_event(
        ledger.discovery_id,
        DiscoveryEventKindV1.SELECTION_FROZEN,
        payload={
            "sealed_material_commitment_sha256": material.commitment_sha256,
            "selected_candidate_semantic_sha256": candidate_source.semantic_sha256,
            "selection_sha256": hashlib.sha256(
                canonical_identity_bytes(selection)
            ).hexdigest(),
        },
    )
    store.close(
        ledger.discovery_id,
        outcome,
        conclusion_detail="development insufficient-evidence terminal path",
    )
    return store.load(ledger.discovery_id)


def _development_binding(
    manifest: LineageDevelopmentManifestV1,
    head: str,
    base_semantic: str,
    token_sha256: str,
    *,
    experiment_suffix: str,
) -> DiscoveryBindingV1:
    return DiscoveryBindingV1(
        experiment_id=f"{manifest.experiment_id}/{experiment_suffix}",
        implementation_commit=head,
        base_source_sha256=hashlib.sha256(manifest.base_source).hexdigest(),
        base_semantic_sha256=base_semantic,
        experiment_manifest_sha256=manifest.raw_sha256,
        partition_manifest_sha256=manifest.partition_manifest_sha256,
        robustness_policy_sha256=robustness_policy_sha256(),
        reveal_token_sha256=token_sha256,
        development_only=True,
        real_partition_execution=False,
    )


def _terminal_material(
    manifest: LineageDevelopmentManifestV1,
    candidate_semantic_sha256: str,
    *,
    reveal_token: str,
):
    return seal_terminal_material(
        candidate_semantic_sha256=candidate_semantic_sha256,
        holdout_manifest_sha256=manifest.partition_manifest_sha256,
        holdout_member_inventory_sha256=hashlib.sha256(
            canonical_identity_bytes(list(manifest.roots("holdout")))
        ).hexdigest(),
        adversarial_manifest_sha256=manifest.partition_manifest_sha256,
        adversarial_member_inventory_sha256=hashlib.sha256(
            canonical_identity_bytes(list(manifest.roots("adversarial")))
        ).hexdigest(),
        reveal_token=reveal_token,
    )


def _mutation_payload(base_ast, child_source: bytes, child_semantic: str) -> dict[str, object]:
    child_ast = parse_strategy_ast(child_source.decode("utf-8"))
    semantic_diff = [item.as_dict() for item in semantic_strategy_diff(base_ast, child_ast)]
    projection = {
        "child_semantic_sha256": child_semantic,
        "child_source_sha256": hashlib.sha256(child_source).hexdigest(),
        "operation_id": "CONTROLLED_PARAMETER_VECTOR",
        "operation_version": 1,
        "semantic_diff": semantic_diff,
    }
    return {
        **projection,
        "mutation_sha256": hashlib.sha256(
            canonical_identity_bytes(projection)
        ).hexdigest(),
    }


def _development_result(
    semantic_sha256: str,
    partition: str,
    roots: tuple[int, ...],
    score: int,
) -> dict[str, object]:
    result = {
        "qualified": score >= 30_000,
        "root_count": len(roots),
        "root_scores": [score + (index % 2) for index, _root in enumerate(roots)],
        "score": score,
    }
    evidence = {
        "candidate_semantic_sha256": semantic_sha256,
        "data_source": LINEAGE_DEVELOPMENT_DATA_SOURCE_V1,
        "partition": partition,
        "result": result,
        "roots": list(roots),
    }
    return {
        **evidence,
        "evidence_sha256": hashlib.sha256(
            canonical_identity_bytes(evidence)
        ).hexdigest(),
        "real_partition_access_count": 0,
    }


def _execution_token(commit: str, manifest: StrategySearchManifestV1) -> str:
    return hashlib.sha256(
        canonical_identity_bytes(
            {
                "implementation_commit": commit,
                "manifest_sha256": manifest.manifest_sha256,
                "partition_manifest_sha256": manifest.partition_manifest_sha256,
                "robustness_policy_sha256": robustness_policy_sha256(),
            }
        )
    ).hexdigest()


def _payload(record) -> dict[str, object]:
    from kirby2.immutable import thaw_json

    return thaw_json(record.payload)


def _nested_value(value: object, *keys: str) -> object:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            raise TypeError(f"nested discovery value {key} is not in an object")
        current = current[key]
    return current


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise DiscoveryStoreError(
            "GIT_PREFLIGHT_FAILED",
            error.stderr.strip() or "Git preflight failed",
        ) from error
    return result.stdout


def _positive_budget(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("budget must be an integer") from error
    if not 1 <= parsed <= 64:
        raise argparse.ArgumentTypeError("budget must be in 1..64")
    return parsed


def _configure_discover(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", required=True, type=Path, metavar="BASE")
    parser.add_argument("--experiment", required=True, type=Path, metavar="FILE")
    parser.add_argument("--budget", required=True, type=_positive_budget, metavar="N")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=DEFAULT_DISCOVERY_STORE,
        help="fresh immutable discovery evidence root",
    )


def _handle_discover(args: argparse.Namespace) -> int:
    try:
        discovery_id = run_frozen_search_to_store(
            base_path=args.base,
            experiment_path=args.experiment,
            budget=args.budget,
            evidence_root=args.evidence_root,
            repository=Path(__file__).resolve().parents[2],
        )
    except DiscoveryStoreError as error:
        _print({"reason_code": error.code, "status": "REFUSED"})
        return 2
    ledger = DiscoveryStore(args.evidence_root).load(discovery_id)
    _print(build_lineage_report(ledger).as_dict())
    return 0


def _configure_inspect(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("discovery_id", metavar="DISCOVERY_ID")
    parser.add_argument("--store-root", type=Path, default=DEFAULT_DISCOVERY_STORE)


def _handle_inspect(args: argparse.Namespace) -> int:
    report = build_lineage_report(
        DiscoveryStore(args.store_root).load(args.discovery_id)
    )
    _print(report.as_dict())
    return 0


def _configure_compare(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("strategy_a", metavar="STRATEGY_A")
    parser.add_argument("strategy_b", metavar="STRATEGY_B")
    parser.add_argument("--store-root", type=Path, default=DEFAULT_DISCOVERY_STORE)


def _handle_compare(args: argparse.Namespace) -> int:
    _print(
        compare_strategies(
            DiscoveryStore(args.store_root),
            args.strategy_a,
            args.strategy_b,
        ).as_dict()
    )
    return 0


def _configure_dev_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--store-root", type=Path)


def _handle_dev_demo(args: argparse.Namespace) -> int:
    if args.store_root is None:
        with tempfile.TemporaryDirectory(prefix="kirby2-lineage-dev-") as directory:
            result = run_development_lineage_demo(
                args.manifest,
                store_root=Path(directory),
            )
    else:
        result = run_development_lineage_demo(
            args.manifest,
            store_root=args.store_root,
        )
    _print(result.as_dict())
    return 0 if result.passed else 1


def _configure_controlled_demo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)


def _handle_controlled_demo(args: argparse.Namespace) -> int:
    if not args.evidence_root.exists() and not args.evidence_root.is_symlink():
        repository = Path(__file__).resolve().parents[2]
        try:
            run_frozen_search_to_store(
                base_path=(
                    repository
                    / "kirby2"
                    / "discovery"
                    / "examples"
                    / "bounded_base.strategy"
                ),
                experiment_path=args.manifest,
                budget=64,
                evidence_root=args.evidence_root,
                repository=repository,
            )
        except DiscoveryStoreError as error:
            _print({"reason_code": error.code, "status": "REFUSED"})
            return 2
    result = validate_controlled_evidence(
        manifest_path=args.manifest,
        evidence_root=args.evidence_root,
    )
    _print(result)
    return 0 if result["status"] == "PASS" else 2


def _print(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _integer(mapping: dict[str, object], key: str) -> int:
    value = mapping[key]
    if type(value) is not int:
        raise TypeError(f"{key} must be an integer")
    return value


def _text(mapping: dict[str, object], key: str) -> str:
    value = mapping[key]
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _boolean(mapping: dict[str, object], key: str) -> bool:
    value = mapping[key]
    if type(value) is not bool:
        raise TypeError(f"{key} must be Boolean")
    return value


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or any(type(item) is not int for item in value):
        raise TypeError(f"{label} must be a nonempty integer array")
    return tuple(value)


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be lowercase SHA-256")


STRATEGY_DISCOVERY_COMMAND_MODULE = CommandModule(
    module_id="STRATEGY_DISCOVERY",
    commands=(
        CommandSpec(
            command_id="DISCOVER_STRATEGY",
            name="discover-strategy",
            help="run one clean committed bounded strategy discovery",
            handler=_handle_discover,
            configure=_configure_discover,
        ),
        CommandSpec(
            command_id="INSPECT_STRATEGY_LINEAGE",
            name="inspect-lineage",
            help="inspect immutable strategy ancestry and sealed results",
            handler=_handle_inspect,
            configure=_configure_inspect,
        ),
        CommandSpec(
            command_id="COMPARE_DISCOVERED_STRATEGIES",
            name="compare-strategies",
            help="compare two stored semantic strategy identities",
            handler=_handle_compare,
            configure=_configure_compare,
        ),
        CommandSpec(
            command_id="STRATEGY_DISCOVERY_DEV_DEMO",
            name="strategy-discovery-dev-demo",
            help="exercise development-only durable discovery lineage",
            handler=_handle_dev_demo,
            configure=_configure_dev_demo,
        ),
        CommandSpec(
            command_id="STRATEGY_DISCOVERY_CONTROLLED_DEMO",
            name="strategy-discovery-demo",
            help="run once or verify the controlled strategy-discovery evidence",
            handler=_handle_controlled_demo,
            configure=_configure_controlled_demo,
        ),
    ),
)


__all__ = [
    "CONTROLLED_EVIDENCE_REASON_V1",
    "DEFAULT_DISCOVERY_STORE",
    "DevelopmentCandidateSpecV1",
    "DevelopmentLineageDemoResultV1",
    "DiscoveryExecutionPreflightV1",
    "LINEAGE_DEVELOPMENT_DATA_SOURCE_V1",
    "LINEAGE_DEVELOPMENT_ORACLE_ID_V1",
    "LINEAGE_DEVELOPMENT_SCHEMA_ID_V1",
    "LineageDevelopmentManifestV1",
    "STRATEGY_DISCOVERY_COMMAND_MODULE",
    "inspect_execution_preflight",
    "robustness_policy_sha256",
    "run_development_lineage_demo",
    "run_frozen_search_to_store",
    "validate_controlled_evidence",
]
