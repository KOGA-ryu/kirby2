"""Exact one-factor robustness mechanics for strategy discovery.

WO35-E defines perturbations and qualification over typed synthetic evidence.  It
does not run a market simulation or open validation, robustness, holdout, or
adversarial datasets.  Real execution remains deferred to WO35-F1.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum

from .evaluation import (
    CandidatePartitionEvidenceV1,
    ComponentDeltaV1,
)
from .identity import canonical_identity_bytes
from .objectives import (
    POLICY_SCALE_V1,
    REQUIRED_OBJECTIVE_SPECS_V1,
    StrategyObjectiveIdV1,
    median_and_mad,
    nearest_rank_p50,
    partition_statistic,
    ratio_ppm,
    round_div_even,
)
from .partitions import StrategyPartitionV1


ROBUSTNESS_SCHEMA_ID_V1 = "KIRBY2_STRATEGY_ROBUSTNESS_EVIDENCE_V1"
ROBUSTNESS_SCHEMA_VERSION_V1 = 1
ROBUSTNESS_DIGEST_DOMAIN_V1 = b"KIRBY2_STRATEGY_ROBUSTNESS_EVIDENCE_V1\x00"
ROBUSTNESS_POLICY_ID_V1 = "STRATEGY_ROBUSTNESS_V1"
ROBUSTNESS_ROOTS_V1 = tuple(range(3_505_000, 3_505_004))
ROBUSTNESS_REQUIRED_FAMILY_COUNT_V1 = 7
ROBUSTNESS_REQUIRED_NONNEGATIVE_FAMILIES_V1 = 6
ROBUSTNESS_EXPECTED_CELL_COUNT_V1 = 64
SINGLE_VENUE_CAPABILITY_ID_V1 = "SINGLE_VENUE_CONTROLLED_SOURCE_V1"
ENTRY_REMAINDER_HORIZON_US_V1 = 2_000_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _require_nfc(value: object, label: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be nonempty text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{label} must already be NFC")


class RobustnessFamilyV1(str, Enum):
    THRESHOLD = "THRESHOLD"
    ROLLING_WINDOW = "ROLLING_WINDOW"
    LATENCY = "LATENCY"
    FEES = "FEES"
    VOLUME = "VOLUME"
    LIQUIDITY = "LIQUIDITY"
    REGIME_MIX = "REGIME_MIX"
    VENUE_MIX = "VENUE_MIX"


MANDATORY_ROBUSTNESS_FAMILIES_V1 = tuple(RobustnessFamilyV1)[:-1]


class PerturbationStatusV1(str, Enum):
    APPLIED = "APPLIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVALID = "INVALID"


class RobustnessOutcomeV1(str, Enum):
    PASSED = "PASSED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXPERIMENT_INVALID = "EXPERIMENT_INVALID"


class SyntheticRobustnessModeV1(str, Enum):
    PASS = "PASS"
    BRITTLE = "BRITTLE"
    OBSERVATION_UNAVAILABLE = "OBSERVATION_UNAVAILABLE"
    REPLAY_INVALID = "REPLAY_INVALID"


@dataclass(frozen=True, slots=True)
class ControlledExecutionTimingV1:
    decision_time_us: int
    decision_latency_us: int
    routing_latency_us: int
    entry_arrival_us: int
    cancellation_us: int
    exit_arrival_us: int | None

    def __post_init__(self) -> None:
        for name, value in (
            ("decision time", self.decision_time_us),
            ("decision latency", self.decision_latency_us),
            ("routing latency", self.routing_latency_us),
            ("entry arrival", self.entry_arrival_us),
            ("cancellation time", self.cancellation_us),
        ):
            _require_nonnegative_int(value, name)
        if self.exit_arrival_us is not None:
            _require_nonnegative_int(self.exit_arrival_us, "exit arrival")
        if self.entry_arrival_us != (
            self.decision_time_us + self.decision_latency_us + self.routing_latency_us
        ):
            raise ValueError("entry arrival differs from the exact execution formula")
        if self.cancellation_us != self.entry_arrival_us + ENTRY_REMAINDER_HORIZON_US_V1:
            raise ValueError("entry cancellation differs from the exact horizon")
        if self.exit_arrival_us is not None and self.exit_arrival_us != self.cancellation_us + 1:
            raise ValueError("exit arrival differs from the exact post-cancel timestamp")

    def as_dict(self) -> dict[str, object]:
        return {
            "cancellation_us": self.cancellation_us,
            "decision_latency_us": self.decision_latency_us,
            "decision_time_us": self.decision_time_us,
            "entry_arrival_us": self.entry_arrival_us,
            "exit_arrival_us": self.exit_arrival_us,
            "routing_latency_us": self.routing_latency_us,
        }


def derive_execution_timing(
    *,
    decision_time_us: int,
    decision_latency_us: int,
    routing_latency_us: int,
    filled_entry_quantity: int,
) -> ControlledExecutionTimingV1:
    for name, value in (
        ("decision time", decision_time_us),
        ("decision latency", decision_latency_us),
        ("routing latency", routing_latency_us),
        ("filled entry quantity", filled_entry_quantity),
    ):
        _require_nonnegative_int(value, name)
    entry_arrival = decision_time_us + decision_latency_us + routing_latency_us
    cancellation = entry_arrival + ENTRY_REMAINDER_HORIZON_US_V1
    return ControlledExecutionTimingV1(
        decision_time_us,
        decision_latency_us,
        routing_latency_us,
        entry_arrival,
        cancellation,
        cancellation + 1 if filled_entry_quantity > 0 else None,
    )


@dataclass(frozen=True, slots=True)
class VolumeVectorV1:
    relative_volume_ppm: int
    event_rate_ppm: int
    order_size_ppm: int
    displayed_queue_ppm: int
    market_frequency_ppm: int
    cancellation_activity_ppm: int
    replenishment_ppm: int

    def __post_init__(self) -> None:
        for name, value in self._values().items():
            _require_nonnegative_int(value, f"volume {name}")

    def _values(self) -> dict[str, int]:
        return {
            "cancellation_activity_ppm": self.cancellation_activity_ppm,
            "displayed_queue_ppm": self.displayed_queue_ppm,
            "event_rate_ppm": self.event_rate_ppm,
            "market_frequency_ppm": self.market_frequency_ppm,
            "order_size_ppm": self.order_size_ppm,
            "relative_volume_ppm": self.relative_volume_ppm,
            "replenishment_ppm": self.replenishment_ppm,
        }

    def as_dict(self) -> dict[str, object]:
        return self._values()


@dataclass(frozen=True, slots=True)
class LiquidityVectorV1:
    initial_depth_ppm: int
    queue_size_ppm: int
    replenishment_rate_ppm: int
    replenishment_size_ppm: int
    cancellation_rate_ppm: int
    placement_depth_offset_ticks: int

    def __post_init__(self) -> None:
        for name, value in self._values().items():
            if type(value) is not int:
                raise TypeError(f"liquidity {name} must be an integer")
        if any(
            value < 0
            for name, value in self._values().items()
            if name != "placement_depth_offset_ticks"
        ):
            raise ValueError("liquidity multipliers must be nonnegative")

    def _values(self) -> dict[str, int]:
        return {
            "cancellation_rate_ppm": self.cancellation_rate_ppm,
            "initial_depth_ppm": self.initial_depth_ppm,
            "placement_depth_offset_ticks": self.placement_depth_offset_ticks,
            "queue_size_ppm": self.queue_size_ppm,
            "replenishment_rate_ppm": self.replenishment_rate_ppm,
            "replenishment_size_ppm": self.replenishment_size_ppm,
        }

    def as_dict(self) -> dict[str, object]:
        return self._values()


@dataclass(frozen=True, slots=True)
class RegimeWeightV1:
    destination: str
    weight_ppm: int

    def __post_init__(self) -> None:
        _require_nfc(self.destination, "regime destination")
        if type(self.weight_ppm) is not int or not 0 <= self.weight_ppm <= POLICY_SCALE_V1:
            raise ValueError("regime destination weight must be in 0..S")

    def as_dict(self) -> dict[str, object]:
        return {"destination": self.destination, "weight_ppm": self.weight_ppm}


@dataclass(frozen=True, slots=True)
class RegimeProbabilityRowV1:
    source: str
    destinations: tuple[RegimeWeightV1, ...]

    def __post_init__(self) -> None:
        _require_nfc(self.source, "regime source")
        if type(self.destinations) is not tuple or len(self.destinations) < 2 or any(
            not isinstance(item, RegimeWeightV1) for item in self.destinations
        ):
            raise ValueError("regime row requires at least two typed destinations")
        ordered = tuple(
            sorted(self.destinations, key=lambda item: item.destination.encode("utf-8"))
        )
        names = tuple(item.destination for item in ordered)
        if len(names) != len(set(names)):
            raise ValueError("regime destination names must be unique")
        if sum(item.weight_ppm for item in ordered) != POLICY_SCALE_V1:
            raise ValueError("regime destination weights must sum exactly to S")
        object.__setattr__(self, "destinations", ordered)

    def as_dict(self) -> dict[str, object]:
        return {
            "destinations": [item.as_dict() for item in self.destinations],
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ControlledRobustnessEnvironmentV1:
    window_us: int
    green_spread_ticks: int
    green_imbalance_ppm: int
    wait_spread_ticks: int
    decision_latency_us: int
    routing_latency_us: int
    maker_fee_milliticks_per_share: int
    taker_fee_milliticks_per_share: int
    volume: VolumeVectorV1
    liquidity: LiquidityVectorV1
    regime_rows: tuple[RegimeProbabilityRowV1, ...]
    venue_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("window_us", self.window_us),
            ("green_spread_ticks", self.green_spread_ticks),
            ("green_imbalance_ppm", self.green_imbalance_ppm),
            ("wait_spread_ticks", self.wait_spread_ticks),
            ("decision_latency_us", self.decision_latency_us),
            ("routing_latency_us", self.routing_latency_us),
        ):
            _require_nonnegative_int(value, name)
        for name, value in (
            ("maker fee", self.maker_fee_milliticks_per_share),
            ("taker fee", self.taker_fee_milliticks_per_share),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be integer milliticks per share")
        if not isinstance(self.volume, VolumeVectorV1):
            raise TypeError("controlled robustness volume vector must be typed")
        if not isinstance(self.liquidity, LiquidityVectorV1):
            raise TypeError("controlled robustness liquidity vector must be typed")
        if type(self.regime_rows) is not tuple or not self.regime_rows or any(
            not isinstance(item, RegimeProbabilityRowV1) for item in self.regime_rows
        ):
            raise TypeError("controlled robustness regime rows must be a typed tuple")
        ordered_rows = tuple(
            sorted(self.regime_rows, key=lambda item: item.source.encode("utf-8"))
        )
        sources = tuple(item.source for item in ordered_rows)
        if len(sources) != len(set(sources)):
            raise ValueError("controlled robustness regime sources must be unique")
        object.__setattr__(self, "regime_rows", ordered_rows)
        if type(self.venue_ids) is not tuple or not self.venue_ids:
            raise ValueError("controlled robustness venue IDs must be nonempty")
        for venue_id in self.venue_ids:
            _require_nfc(venue_id, "venue ID")
        ordered_venues = tuple(sorted(set(self.venue_ids), key=lambda item: item.encode("utf-8")))
        if len(ordered_venues) != len(self.venue_ids):
            raise ValueError("controlled robustness venue IDs must be unique")
        object.__setattr__(self, "venue_ids", ordered_venues)

    @property
    def environment_sha256(self) -> str:
        return hashlib.sha256(canonical_identity_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_latency_us": self.decision_latency_us,
            "green_imbalance_ppm": self.green_imbalance_ppm,
            "green_spread_ticks": self.green_spread_ticks,
            "liquidity": self.liquidity.as_dict(),
            "maker_fee_milliticks_per_share": self.maker_fee_milliticks_per_share,
            "regime_rows": [item.as_dict() for item in self.regime_rows],
            "routing_latency_us": self.routing_latency_us,
            "taker_fee_milliticks_per_share": self.taker_fee_milliticks_per_share,
            "venue_ids": list(self.venue_ids),
            "volume": self.volume.as_dict(),
            "wait_spread_ticks": self.wait_spread_ticks,
            "window_us": self.window_us,
        }


@dataclass(frozen=True, slots=True)
class RobustnessSettingV1:
    family: RobustnessFamilyV1
    setting_id: str
    scalar: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.family, RobustnessFamilyV1):
            raise TypeError("robustness setting family must be typed")
        _require_nfc(self.setting_id, "robustness setting ID")
        if self.family is RobustnessFamilyV1.VENUE_MIX:
            if self.scalar is not None:
                raise ValueError("single-venue declaration cannot carry a scalar")
        elif type(self.scalar) is not int:
            raise TypeError("applicable robustness setting scalar must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "scalar": self.scalar,
            "setting_id": self.setting_id,
        }


ROBUSTNESS_SETTINGS_V1 = (
    RobustnessSettingV1(RobustnessFamilyV1.THRESHOLD, "THRESHOLD_900000", 900_000),
    RobustnessSettingV1(RobustnessFamilyV1.THRESHOLD, "THRESHOLD_950000", 950_000),
    RobustnessSettingV1(RobustnessFamilyV1.THRESHOLD, "THRESHOLD_1050000", 1_050_000),
    RobustnessSettingV1(RobustnessFamilyV1.THRESHOLD, "THRESHOLD_1100000", 1_100_000),
    RobustnessSettingV1(RobustnessFamilyV1.ROLLING_WINDOW, "ROLLING_WINDOW_800000", 800_000),
    RobustnessSettingV1(RobustnessFamilyV1.ROLLING_WINDOW, "ROLLING_WINDOW_1200000", 1_200_000),
    RobustnessSettingV1(RobustnessFamilyV1.LATENCY, "LATENCY_PLUS_250_US", 250),
    RobustnessSettingV1(RobustnessFamilyV1.LATENCY, "LATENCY_PLUS_1000_US", 1_000),
    RobustnessSettingV1(RobustnessFamilyV1.FEES, "FEES_PLUS_250", 250),
    RobustnessSettingV1(RobustnessFamilyV1.FEES, "FEES_PLUS_1000", 1_000),
    RobustnessSettingV1(RobustnessFamilyV1.VOLUME, "VOLUME_750000", 750_000),
    RobustnessSettingV1(RobustnessFamilyV1.VOLUME, "VOLUME_1250000", 1_250_000),
    RobustnessSettingV1(RobustnessFamilyV1.LIQUIDITY, "LIQUIDITY_750000", 750_000),
    RobustnessSettingV1(RobustnessFamilyV1.LIQUIDITY, "LIQUIDITY_1250000", 1_250_000),
    RobustnessSettingV1(RobustnessFamilyV1.REGIME_MIX, "REGIME_MAX_TO_MIN", -200_000),
    RobustnessSettingV1(RobustnessFamilyV1.REGIME_MIX, "REGIME_MIN_TO_MAX", 200_000),
    RobustnessSettingV1(RobustnessFamilyV1.VENUE_MIX, "VENUE_MIX_SINGLE_VENUE", None),
)


@dataclass(frozen=True, slots=True)
class RobustnessProbeV1:
    setting: RobustnessSettingV1
    status: PerturbationStatusV1
    environment: ControlledRobustnessEnvironmentV1 | None
    changed_paths: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.setting, RobustnessSettingV1):
            raise TypeError("robustness probe setting must be typed")
        if not isinstance(self.status, PerturbationStatusV1):
            raise TypeError("robustness probe status must be typed")
        if self.environment is not None and not isinstance(
            self.environment,
            ControlledRobustnessEnvironmentV1,
        ):
            raise TypeError("robustness probe environment must be typed or absent")
        if type(self.changed_paths) is not tuple or any(
            type(item) is not str or not item.startswith("/")
            for item in self.changed_paths
        ):
            raise TypeError("robustness changed paths must be JSON pointers")
        if len(self.changed_paths) != len(set(self.changed_paths)):
            raise ValueError("robustness changed paths must be unique")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("robustness probe reason must be nonempty")
        if self.status is PerturbationStatusV1.APPLIED:
            if self.environment is None or not self.changed_paths:
                raise ValueError("applied robustness probe lacks its exact changes")
        elif self.environment is not None or self.changed_paths:
            raise ValueError("non-applied robustness probe cannot expose a mutation")

    def as_dict(self) -> dict[str, object]:
        return {
            "changed_paths": list(self.changed_paths),
            "environment_sha256": (
                None if self.environment is None else self.environment.environment_sha256
            ),
            "reason": self.reason,
            "setting": self.setting.as_dict(),
            "status": self.status.value,
        }


def apply_robustness_setting(
    baseline: ControlledRobustnessEnvironmentV1,
    setting: RobustnessSettingV1,
) -> RobustnessProbeV1:
    if not isinstance(baseline, ControlledRobustnessEnvironmentV1):
        raise TypeError("robustness baseline environment must be typed")
    if not isinstance(setting, RobustnessSettingV1):
        raise TypeError("robustness setting must be typed")
    scalar = setting.scalar
    if setting.family is RobustnessFamilyV1.VENUE_MIX:
        if len(baseline.venue_ids) == 1:
            return RobustnessProbeV1(
                setting,
                PerturbationStatusV1.NOT_APPLICABLE,
                None,
                (),
                SINGLE_VENUE_CAPABILITY_ID_V1,
            )
        return RobustnessProbeV1(
            setting,
            PerturbationStatusV1.INVALID,
            None,
            (),
            "MULTIVENUE_REQUIRES_PREREGISTERED_VENUE_SETTINGS",
        )
    assert scalar is not None
    if setting.family is RobustnessFamilyV1.THRESHOLD:
        values = (
            _mul_ppm(baseline.green_spread_ticks, scalar),
            _mul_ppm(baseline.green_imbalance_ppm, scalar),
            _mul_ppm(baseline.wait_spread_ticks, scalar),
        )
        if not (1 <= values[0] <= 5 and 0 <= values[1] <= 500_000 and 1 <= values[2] <= 10):
            return _failed_probe(setting, PerturbationStatusV1.INVALID, "THRESHOLD_OUTSIDE_ROBUSTNESS_BOUNDS")
        changed = replace(
            baseline,
            green_spread_ticks=values[0],
            green_imbalance_ppm=values[1],
            wait_spread_ticks=values[2],
        )
        return _applied_probe(
            baseline,
            changed,
            setting,
            (
                "/green/0/threshold_ticks",
                "/green/1/threshold_ppm",
                "/wait/0/threshold_ticks",
            ),
        )
    if setting.family is RobustnessFamilyV1.ROLLING_WINDOW:
        window_us = _mul_ppm(baseline.window_us, scalar)
        if not 1_000_000 <= window_us <= 20_000_000:
            return _failed_probe(setting, PerturbationStatusV1.INVALID, "ROLLING_WINDOW_OUTSIDE_ROBUSTNESS_BOUNDS")
        return _applied_probe(
            baseline,
            replace(baseline, window_us=window_us),
            setting,
            ("/window_us",),
        )
    if setting.family is RobustnessFamilyV1.LATENCY:
        decision = baseline.decision_latency_us
        routing = baseline.routing_latency_us
        changed = replace(
            baseline,
            decision_latency_us=(decision + scalar if decision != 0 else 0),
            routing_latency_us=(routing + scalar if routing != 0 else 0),
        )
        paths = tuple(
            path
            for path, original in (
                ("/runtime/decision_latency_us", decision),
                ("/runtime/routing_latency_us", routing),
            )
            if original != 0
        )
        return _applied_probe(baseline, changed, setting, paths)
    if setting.family is RobustnessFamilyV1.FEES:
        return _applied_probe(
            baseline,
            replace(
                baseline,
                maker_fee_milliticks_per_share=(
                    baseline.maker_fee_milliticks_per_share + scalar
                ),
                taker_fee_milliticks_per_share=(
                    baseline.taker_fee_milliticks_per_share + scalar
                ),
            ),
            setting,
            (
                "/fees/maker_milliticks_per_share",
                "/fees/taker_milliticks_per_share",
            ),
        )
    if setting.family is RobustnessFamilyV1.VOLUME:
        volume = baseline.volume
        changed_volume = VolumeVectorV1(
            *(_mul_ppm(value, scalar) for value in (
                volume.relative_volume_ppm,
                volume.event_rate_ppm,
                volume.order_size_ppm,
                volume.displayed_queue_ppm,
                volume.market_frequency_ppm,
                volume.cancellation_activity_ppm,
                volume.replenishment_ppm,
            ))
        )
        return _applied_probe(
            baseline,
            replace(baseline, volume=changed_volume),
            setting,
            tuple(f"/volume/{name}" for name in (
                "relative_volume_ppm",
                "event_rate_ppm",
                "order_size_ppm",
                "displayed_queue_ppm",
                "market_frequency_ppm",
                "cancellation_activity_ppm",
                "replenishment_ppm",
            )),
        )
    if setting.family is RobustnessFamilyV1.LIQUIDITY:
        liquidity = baseline.liquidity
        changed_liquidity = replace(
            liquidity,
            initial_depth_ppm=_mul_ppm(liquidity.initial_depth_ppm, scalar),
            queue_size_ppm=_mul_ppm(liquidity.queue_size_ppm, scalar),
            replenishment_rate_ppm=_mul_ppm(
                liquidity.replenishment_rate_ppm,
                scalar,
            ),
            replenishment_size_ppm=_mul_ppm(
                liquidity.replenishment_size_ppm,
                scalar,
            ),
        )
        return _applied_probe(
            baseline,
            replace(baseline, liquidity=changed_liquidity),
            setting,
            tuple(f"/liquidity/{name}" for name in (
                "initial_depth_ppm",
                "queue_size_ppm",
                "replenishment_size_ppm",
                "replenishment_rate_ppm",
            )),
        )
    if setting.family is RobustnessFamilyV1.REGIME_MIX:
        shifted = _shift_regime_rows(baseline.regime_rows, scalar)
        if shifted is None:
            return _failed_probe(
                setting,
                PerturbationStatusV1.INSUFFICIENT_EVIDENCE,
                "REGIME_DONOR_NOT_ABOVE_200000",
            )
        return _applied_probe(
            baseline,
            replace(baseline, regime_rows=shifted),
            setting,
            tuple(f"/regime_rows/{row.source}" for row in baseline.regime_rows),
        )
    raise AssertionError(setting.family)


def build_robustness_probes(
    baseline: ControlledRobustnessEnvironmentV1,
) -> tuple[RobustnessProbeV1, ...]:
    return tuple(apply_robustness_setting(baseline, item) for item in ROBUSTNESS_SETTINGS_V1)


def controlled_robustness_environment(
    *,
    candidate: bool,
) -> ControlledRobustnessEnvironmentV1:
    return ControlledRobustnessEnvironmentV1(
        window_us=5_000_000,
        green_spread_ticks=(1 if candidate else 2),
        green_imbalance_ppm=(300_000 if candidate else 200_000),
        wait_spread_ticks=4,
        decision_latency_us=1,
        routing_latency_us=0,
        maker_fee_milliticks_per_share=0,
        taker_fee_milliticks_per_share=0,
        volume=VolumeVectorV1(
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
        ),
        liquidity=LiquidityVectorV1(
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
            1_000_000,
            0,
        ),
        regime_rows=(
            RegimeProbabilityRowV1(
                "QUIET",
                (
                    RegimeWeightV1("QUIET", 600_000),
                    RegimeWeightV1("TREND", 400_000),
                ),
            ),
            RegimeProbabilityRowV1(
                "TREND",
                (
                    RegimeWeightV1("QUIET", 400_000),
                    RegimeWeightV1("TREND", 600_000),
                ),
            ),
        ),
        venue_ids=("PRIMARY",),
    )


_PER_CELL_OBJECTIVES = frozenset(
    {
        item.objective_id
        for item in REQUIRED_OBJECTIVE_SPECS_V1
        if item.objective_id
        not in {
            StrategyObjectiveIdV1.BALANCED_CLASSIFICATION,
            StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY,
            StrategyObjectiveIdV1.CROSS_CELL_STABILITY,
        }
    }
)
_ROBUSTNESS_OBJECTIVE_WEIGHTS = {
    item.objective_id: item.weight
    for item in REQUIRED_OBJECTIVE_SPECS_V1
    if item.objective_id is not StrategyObjectiveIdV1.CROSS_CELL_STABILITY
}


def robustness_composite_delta(
    component_deltas: tuple[ComponentDeltaV1, ...],
    *,
    pooled_classification_delta: int,
    pooled_opportunity_delta: int,
) -> int:
    if type(component_deltas) is not tuple or any(
        not isinstance(item, ComponentDeltaV1) for item in component_deltas
    ):
        raise TypeError("robustness composite requires typed per-cell components")
    values = {item.objective_id: item.delta for item in component_deltas}
    if len(values) != len(component_deltas) or set(values) != _PER_CELL_OBJECTIVES:
        raise ValueError("robustness composite per-cell inventory changed")
    if type(pooled_classification_delta) is not int or type(
        pooled_opportunity_delta
    ) is not int:
        raise TypeError("robustness pooled component deltas must be integers")
    values[StrategyObjectiveIdV1.BALANCED_CLASSIFICATION] = (
        pooled_classification_delta
    )
    values[StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY] = pooled_opportunity_delta
    if set(values) != set(_ROBUSTNESS_OBJECTIVE_WEIGHTS):
        raise ValueError("robustness composite objective inventory changed")
    numerator = sum(
        _ROBUSTNESS_OBJECTIVE_WEIGHTS[objective_id] * value
        for objective_id, value in values.items()
    )
    denominator = sum(_ROBUSTNESS_OBJECTIVE_WEIGHTS.values())
    return round_div_even(numerator, denominator)


@dataclass(frozen=True, slots=True)
class RobustnessCellV1:
    family: RobustnessFamilyV1
    setting_id: str
    root_seed: int
    composite_delta: int
    component_deltas: tuple[ComponentDeltaV1, ...]
    candidate_trades: int
    base_trades: int
    observable: bool
    complete: bool
    replay_valid: bool
    invariant_clean: bool

    def __post_init__(self) -> None:
        if self.family not in MANDATORY_ROBUSTNESS_FAMILIES_V1:
            raise ValueError("robustness cell family must be mandatory and applicable")
        if self.setting_id not in _setting_ids(self.family):
            raise ValueError("robustness cell setting is outside its family")
        if self.root_seed not in ROBUSTNESS_ROOTS_V1:
            raise ValueError("robustness cell root is outside the fixed roots")
        if type(self.composite_delta) is not int:
            raise TypeError("robustness cell composite delta must be an integer")
        if type(self.component_deltas) is not tuple or any(
            not isinstance(item, ComponentDeltaV1) for item in self.component_deltas
        ):
            raise TypeError("robustness cell components must be a typed tuple")
        ordered = tuple(sorted(self.component_deltas, key=lambda item: item.objective_id.value))
        ids = tuple(item.objective_id for item in ordered)
        if len(ids) != len(set(ids)) or set(ids) != _PER_CELL_OBJECTIVES:
            raise ValueError("robustness cell components differ from the per-cell inventory")
        object.__setattr__(self, "component_deltas", ordered)
        for name, value in (
            ("candidate trades", self.candidate_trades),
            ("base trades", self.base_trades),
        ):
            _require_nonnegative_int(value, name)
        for name, value in (
            ("observable", self.observable),
            ("complete", self.complete),
            ("replay valid", self.replay_valid),
            ("invariant clean", self.invariant_clean),
        ):
            if type(value) is not bool:
                raise TypeError(f"robustness cell {name} flag must be Boolean")

    def component_delta(self, objective_id: StrategyObjectiveIdV1) -> int:
        for item in self.component_deltas:
            if item.objective_id is objective_id:
                return item.delta
        raise KeyError(objective_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "base_trades": self.base_trades,
            "candidate_trades": self.candidate_trades,
            "complete": self.complete,
            "component_deltas": [item.as_dict() for item in self.component_deltas],
            "composite_delta": self.composite_delta,
            "family": self.family.value,
            "invariant_clean": self.invariant_clean,
            "observable": self.observable,
            "replay_valid": self.replay_valid,
            "root_seed": self.root_seed,
            "setting_id": self.setting_id,
        }


@dataclass(frozen=True, slots=True)
class RobustnessFamilyEvidenceV1:
    family: RobustnessFamilyV1
    cells: tuple[RobustnessCellV1, ...]
    pooled_classification_delta: int
    pooled_opportunity_delta: int

    def __post_init__(self) -> None:
        if self.family not in MANDATORY_ROBUSTNESS_FAMILIES_V1:
            raise ValueError("family evidence must use a mandatory robustness family")
        if type(self.cells) is not tuple or not self.cells or any(
            not isinstance(item, RobustnessCellV1) for item in self.cells
        ):
            raise TypeError("robustness family cells must be a nonempty typed tuple")
        expected = tuple(
            (root, setting_id)
            for root in ROBUSTNESS_ROOTS_V1
            for setting_id in _setting_ids(self.family)
        )
        actual = tuple((item.root_seed, item.setting_id) for item in self.cells)
        if actual != expected or any(item.family is not self.family for item in self.cells):
            raise ValueError("robustness cells are not in ascending root then setting order")
        if type(self.pooled_classification_delta) is not int or type(
            self.pooled_opportunity_delta
        ) is not int:
            raise TypeError("pooled robustness component deltas must be integers")
        for cell in self.cells:
            expected_composite = robustness_composite_delta(
                cell.component_deltas,
                pooled_classification_delta=self.pooled_classification_delta,
                pooled_opportunity_delta=self.pooled_opportunity_delta,
            )
            if cell.composite_delta != expected_composite:
                raise ValueError(
                    "robustness cell composite did not reuse family-pooled utilities"
                )

    @property
    def family_median(self) -> int:
        return nearest_rank_p50(tuple(item.composite_delta for item in self.cells))

    @property
    def minimum_cell(self) -> int:
        return min(item.composite_delta for item in self.cells)

    @property
    def candidate_trades(self) -> int:
        return sum(item.candidate_trades for item in self.cells)

    @property
    def base_trades(self) -> int:
        return sum(item.base_trades for item in self.cells)

    def component_medians(self) -> dict[StrategyObjectiveIdV1, int]:
        medians = {
            objective_id: nearest_rank_p50(
                tuple(item.component_delta(objective_id) for item in self.cells)
            )
            for objective_id in sorted(
                _PER_CELL_OBJECTIVES,
                key=lambda item: item.value,
            )
        }
        medians[StrategyObjectiveIdV1.BALANCED_CLASSIFICATION] = (
            self.pooled_classification_delta
        )
        medians[StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY] = (
            self.pooled_opportunity_delta
        )
        return medians

    def as_dict(self) -> dict[str, object]:
        return {
            "base_trades": self.base_trades,
            "candidate_trades": self.candidate_trades,
            "cells": [item.as_dict() for item in self.cells],
            "component_medians": {
                key.value: value
                for key, value in sorted(
                    self.component_medians().items(),
                    key=lambda item: item[0].value,
                )
            },
            "family": self.family.value,
            "family_median": self.family_median,
            "minimum_cell": self.minimum_cell,
            "pooled_classification_delta": self.pooled_classification_delta,
            "pooled_opportunity_delta": self.pooled_opportunity_delta,
        }


@dataclass(frozen=True, slots=True)
class VenueMixDeclarationV1:
    status: PerturbationStatusV1
    capability_id: str

    def __post_init__(self) -> None:
        if self.status is not PerturbationStatusV1.NOT_APPLICABLE:
            raise ValueError("controlled venue mix must be declared NOT_APPLICABLE")
        if self.capability_id != SINGLE_VENUE_CAPABILITY_ID_V1:
            raise ValueError("controlled venue-mix capability declaration changed")

    def as_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "family": RobustnessFamilyV1.VENUE_MIX.value,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class RobustnessEvidenceV1:
    candidate_semantic_sha256: str
    base_semantic_sha256: str
    candidate_environment_sha256: str
    base_environment_sha256: str
    families: tuple[RobustnessFamilyEvidenceV1, ...]
    venue_mix: VenueMixDeclarationV1

    def __post_init__(self) -> None:
        for value, label in (
            (self.candidate_semantic_sha256, "candidate semantic digest"),
            (self.base_semantic_sha256, "base semantic digest"),
            (self.candidate_environment_sha256, "candidate environment digest"),
            (self.base_environment_sha256, "base environment digest"),
        ):
            _require_sha256(value, label)
        if self.candidate_semantic_sha256 == self.base_semantic_sha256:
            raise ValueError("robustness candidate must differ from the base")
        if type(self.families) is not tuple or any(
            not isinstance(item, RobustnessFamilyEvidenceV1) for item in self.families
        ):
            raise TypeError("robustness families must be a typed tuple")
        if tuple(item.family for item in self.families) != MANDATORY_ROBUSTNESS_FAMILIES_V1:
            raise ValueError("robustness evidence must contain seven ordered families")
        if sum(len(item.cells) for item in self.families) != ROBUSTNESS_EXPECTED_CELL_COUNT_V1:
            raise ValueError("robustness evidence must contain exactly 64 cells")
        if not isinstance(self.venue_mix, VenueMixDeclarationV1):
            raise TypeError("robustness venue declaration must be typed")

    @property
    def evidence_sha256(self) -> str:
        raw = canonical_identity_bytes(self.as_dict())
        digest = hashlib.sha256()
        digest.update(ROBUSTNESS_DIGEST_DOMAIN_V1)
        digest.update(struct.pack(">Q", len(raw)))
        digest.update(raw)
        return digest.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "base_environment_sha256": self.base_environment_sha256,
            "base_semantic_sha256": self.base_semantic_sha256,
            "candidate_environment_sha256": self.candidate_environment_sha256,
            "candidate_semantic_sha256": self.candidate_semantic_sha256,
            "families": [item.as_dict() for item in self.families],
            "policy_id": ROBUSTNESS_POLICY_ID_V1,
            "schema_id": ROBUSTNESS_SCHEMA_ID_V1,
            "schema_version": ROBUSTNESS_SCHEMA_VERSION_V1,
            "venue_mix": self.venue_mix.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RobustnessQualificationV1:
    outcome: RobustnessOutcomeV1
    reasons: tuple[str, ...]
    evidence_sha256: str
    family_medians: tuple[tuple[str, int], ...]
    minimum_cell: int
    nonnegative_family_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RobustnessOutcomeV1):
            raise TypeError("robustness qualification outcome must be typed")
        if type(self.reasons) is not tuple or any(
            type(item) is not str or not item for item in self.reasons
        ):
            raise TypeError("robustness qualification reasons must be a text tuple")
        if (self.outcome is RobustnessOutcomeV1.PASSED) == bool(self.reasons):
            raise ValueError("robustness outcome and reasons disagree")
        _require_sha256(self.evidence_sha256, "robustness evidence digest")
        if type(self.family_medians) is not tuple or len(self.family_medians) != 7:
            raise ValueError("robustness qualification must record seven family medians")
        if type(self.minimum_cell) is not int:
            raise TypeError("robustness minimum cell must be an integer")
        if type(self.nonnegative_family_count) is not int:
            raise TypeError("robustness nonnegative-family count must be an integer")

    @property
    def qualified(self) -> bool:
        return self.outcome is RobustnessOutcomeV1.PASSED

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_sha256": self.evidence_sha256,
            "family_medians": [list(item) for item in self.family_medians],
            "minimum_cell": self.minimum_cell,
            "nonnegative_family_count": self.nonnegative_family_count,
            "outcome": self.outcome.value,
            "reasons": list(self.reasons),
        }


def qualify_robustness(evidence: RobustnessEvidenceV1) -> RobustnessQualificationV1:
    if not isinstance(evidence, RobustnessEvidenceV1):
        raise TypeError("robustness qualification requires typed evidence")
    invalid: list[str] = []
    insufficient: list[str] = []
    family_medians = tuple(
        (family.family.value, family.family_median) for family in evidence.families
    )
    minimum_cell = min(family.minimum_cell for family in evidence.families)
    nonnegative = sum(value >= 0 for _, value in family_medians)
    for family in evidence.families:
        for cell in family.cells:
            if not cell.observable:
                insufficient.append(
                    f"UNAVAILABLE_OBSERVATION:{family.family.value}:{cell.setting_id}:{cell.root_seed}"
                )
            if not cell.complete:
                insufficient.append(
                    f"INCOMPLETE_CELL:{family.family.value}:{cell.setting_id}:{cell.root_seed}"
                )
            if not cell.replay_valid:
                invalid.append(
                    f"REPLAY_INVALID:{family.family.value}:{cell.setting_id}:{cell.root_seed}"
                )
            if not cell.invariant_clean:
                invalid.append(
                    f"INVARIANT_VIOLATION:{family.family.value}:{cell.setting_id}:{cell.root_seed}"
                )
        if family.family_median < -20_000:
            insufficient.append(f"FAMILY_MEDIAN_BELOW_NEGATIVE_20000:{family.family.value}")
        for objective_id, value in sorted(
            family.component_medians().items(),
            key=lambda item: item[0].value,
        ):
            if value < -50_000:
                insufficient.append(
                    f"COMPONENT_MEDIAN_BELOW_NEGATIVE_50000:{family.family.value}:{objective_id.value}"
                )
            if objective_id in {
                StrategyObjectiveIdV1.FALSE_GREEN,
                StrategyObjectiveIdV1.MISSED_OPPORTUNITY,
                StrategyObjectiveIdV1.COMPLETION,
            } and value < -20_000:
                insufficient.append(
                    f"SENSITIVE_COMPONENT_BELOW_NEGATIVE_20000:{family.family.value}:{objective_id.value}"
                )
        if family.base_trades <= 0:
            insufficient.append(f"ZERO_BASE_TRADES:{family.family.value}")
        else:
            trade_ratio = ratio_ppm(family.candidate_trades, family.base_trades)
            if not 600_000 <= trade_ratio <= 1_600_000:
                insufficient.append(f"TRADE_RATIO_OUT_OF_RANGE:{family.family.value}")
    if nonnegative < ROBUSTNESS_REQUIRED_NONNEGATIVE_FAMILIES_V1:
        insufficient.append("FEWER_THAN_SIX_NONNEGATIVE_FAMILIES")
    if minimum_cell < -75_000:
        insufficient.append("MINIMUM_CELL_BELOW_NEGATIVE_75000")
    if invalid:
        outcome = RobustnessOutcomeV1.EXPERIMENT_INVALID
        reasons = tuple(invalid + insufficient)
    elif insufficient:
        outcome = RobustnessOutcomeV1.INSUFFICIENT_EVIDENCE
        reasons = tuple(insufficient)
    else:
        outcome = RobustnessOutcomeV1.PASSED
        reasons = ()
    return RobustnessQualificationV1(
        outcome,
        reasons,
        evidence.evidence_sha256,
        family_medians,
        minimum_cell,
        nonnegative,
    )


def build_synthetic_robustness_evidence(
    mode: SyntheticRobustnessModeV1,
) -> RobustnessEvidenceV1:
    if not isinstance(mode, SyntheticRobustnessModeV1):
        raise TypeError("synthetic robustness mode must be typed")
    candidate_environment = controlled_robustness_environment(candidate=True)
    base_environment = controlled_robustness_environment(candidate=False)
    candidate_probes = build_robustness_probes(candidate_environment)
    base_probes = build_robustness_probes(base_environment)
    for candidate_probe, base_probe in zip(candidate_probes, base_probes, strict=True):
        if (
            candidate_probe.setting != base_probe.setting
            or candidate_probe.status is not base_probe.status
            or candidate_probe.changed_paths != base_probe.changed_paths
        ):
            raise ValueError("candidate and base did not receive identical perturbations")
    base_median = {
        RobustnessFamilyV1.THRESHOLD: 18_000,
        RobustnessFamilyV1.ROLLING_WINDOW: 16_000,
        RobustnessFamilyV1.LATENCY: 14_000,
        RobustnessFamilyV1.FEES: 12_000,
        RobustnessFamilyV1.VOLUME: 10_000,
        RobustnessFamilyV1.LIQUIDITY: 8_000,
        RobustnessFamilyV1.REGIME_MIX: 6_000,
    }
    families: list[RobustnessFamilyEvidenceV1] = []
    first_cell = True
    for family in MANDATORY_ROBUSTNESS_FAMILIES_V1:
        cells: list[RobustnessCellV1] = []
        settings = _settings(family)
        brittle_family = (
            mode is SyntheticRobustnessModeV1.BRITTLE
            and family is RobustnessFamilyV1.THRESHOLD
        )
        pooled_classification = -30_000 if brittle_family else 10_000
        pooled_opportunity = -30_000 if brittle_family else 10_000
        for root_index, root in enumerate(ROBUSTNESS_ROOTS_V1):
            for setting_index, setting in enumerate(settings):
                component_value = (
                    -30_000 - root_index * 1_000
                    if brittle_family
                    else base_median[family]
                    + (root_index - 1) * 1_000
                    + setting_index * 500
                )
                flags = {
                    "observable": not (
                        mode is SyntheticRobustnessModeV1.OBSERVATION_UNAVAILABLE
                        and first_cell
                    ),
                    "complete": True,
                    "replay_valid": not (
                        mode is SyntheticRobustnessModeV1.REPLAY_INVALID and first_cell
                    ),
                    "invariant_clean": True,
                }
                per_cell = tuple(
                    ComponentDeltaV1(
                        objective_id,
                        (
                            -25_000
                            if brittle_family
                            and objective_id is StrategyObjectiveIdV1.FALSE_GREEN
                            else component_value
                        ),
                    )
                    for objective_id in sorted(_PER_CELL_OBJECTIVES, key=lambda item: item.value)
                )
                delta = robustness_composite_delta(
                    per_cell,
                    pooled_classification_delta=pooled_classification,
                    pooled_opportunity_delta=pooled_opportunity,
                )
                cells.append(
                    RobustnessCellV1(
                        family,
                        setting.setting_id,
                        root,
                        delta,
                        per_cell,
                        40,
                        40,
                        **flags,
                    )
                )
                first_cell = False
        families.append(
            RobustnessFamilyEvidenceV1(
                family,
                tuple(cells),
                pooled_classification,
                pooled_opportunity,
            )
        )
    return RobustnessEvidenceV1(
        candidate_semantic_sha256=_digest("wo35e/synthetic/candidate"),
        base_semantic_sha256=_digest("wo35e/synthetic/base"),
        candidate_environment_sha256=candidate_environment.environment_sha256,
        base_environment_sha256=base_environment.environment_sha256,
        families=tuple(families),
        venue_mix=VenueMixDeclarationV1(
            PerturbationStatusV1.NOT_APPLICABLE,
            SINGLE_VENUE_CAPABILITY_ID_V1,
        ),
    )


@dataclass(frozen=True, slots=True)
class TerminalPartitionQualificationV1:
    partition: StrategyPartitionV1
    qualified: bool
    reasons: tuple[str, ...]
    median_delta: int
    mad: int

    def __post_init__(self) -> None:
        if self.partition not in {
            StrategyPartitionV1.HOLDOUT,
            StrategyPartitionV1.ADVERSARIAL_HOLDOUT,
        }:
            raise ValueError("terminal qualification partition is invalid")
        if type(self.qualified) is not bool:
            raise TypeError("terminal qualification must be Boolean")
        if type(self.reasons) is not tuple:
            raise TypeError("terminal qualification reasons must be a tuple")
        if self.qualified == bool(self.reasons):
            raise ValueError("terminal qualification decision and reasons disagree")
        if type(self.median_delta) is not int or type(self.mad) is not int or self.mad < 0:
            raise ValueError("terminal qualification reduction is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "mad": self.mad,
            "median_delta": self.median_delta,
            "partition": self.partition.value,
            "qualified": self.qualified,
            "reasons": list(self.reasons),
        }


def qualify_holdout(
    evidence: CandidatePartitionEvidenceV1,
    validation: CandidatePartitionEvidenceV1,
) -> TerminalPartitionQualificationV1:
    if evidence.partition is not StrategyPartitionV1.HOLDOUT:
        raise ValueError("holdout qualification requires holdout evidence")
    if validation.partition is not StrategyPartitionV1.VALIDATION:
        raise ValueError("holdout comparison requires validation evidence")
    _require_terminal_compatibility(evidence, validation)
    median, mad = median_and_mad(evidence.deltas)
    failures: list[str] = []
    if tuple(item.root_seed for item in evidence.root_deltas) != tuple(range(3_503_000, 3_503_008)):
        failures.append("HOLDOUT_ROOT_SET_INCOMPLETE")
    if median < 20_000:
        failures.append("HOLDOUT_MEDIAN_BELOW_20000")
    if sum(item > 0 for item in evidence.deltas) < 5:
        failures.append("FEWER_THAN_FIVE_POSITIVE_HOLDOUT_ROOTS")
    if median - mad < -10_000:
        failures.append("HOLDOUT_MEDIAN_MINUS_MAD_BELOW_NEGATIVE_10000")
    _terminal_component_failures(evidence, failures)
    for item in evidence.component_deltas:
        allowed_drop = (
            20_000
            if item.objective_id in {
                StrategyObjectiveIdV1.FALSE_GREEN,
                StrategyObjectiveIdV1.MISSED_OPPORTUNITY,
                StrategyObjectiveIdV1.COMPLETION,
            }
            else 50_000
        )
        if item.delta < validation.component_delta(item.objective_id) - allowed_drop:
            failures.append(f"HOLDOUT_COMPONENT_DROPPED_FROM_VALIDATION:{item.objective_id.value}")
    _terminal_trade_failures(evidence, failures)
    return TerminalPartitionQualificationV1(
        evidence.partition,
        not failures,
        tuple(failures),
        median,
        mad,
    )


def qualify_adversarial(
    evidence: CandidatePartitionEvidenceV1,
    *,
    trained_candidate_count: int,
) -> TerminalPartitionQualificationV1:
    if evidence.partition is not StrategyPartitionV1.ADVERSARIAL_HOLDOUT:
        raise ValueError("adversarial qualification requires adversarial evidence")
    statistic = partition_statistic(
        evidence.deltas,
        trained_candidate_count=trained_candidate_count,
    )
    failures: list[str] = []
    if tuple(item.root_seed for item in evidence.root_deltas) != tuple(range(3_504_000, 3_504_008)):
        failures.append("ADVERSARIAL_ROOT_SET_INCOMPLETE")
    if statistic.statistic < 30_000:
        failures.append("STATISTIC_BELOW_30000")
    if sum(item > 0 for item in evidence.deltas) < 6:
        failures.append("FEWER_THAN_SIX_POSITIVE_ROOTS")
    if max(
        evidence.component_delta(StrategyObjectiveIdV1.BALANCED_CLASSIFICATION),
        evidence.component_delta(StrategyObjectiveIdV1.EXECUTION_OPPORTUNITY),
    ) < 50_000:
        failures.append("NO_CLASSIFICATION_OR_OPPORTUNITY_EFFECT_50000")
    _terminal_component_failures(evidence, failures)
    _terminal_trade_failures(evidence, failures)
    return TerminalPartitionQualificationV1(
        evidence.partition,
        not failures,
        tuple(failures),
        statistic.median_delta,
        statistic.mad,
    )


def _terminal_component_failures(
    evidence: CandidatePartitionEvidenceV1,
    failures: list[str],
) -> None:
    for item in evidence.component_deltas:
        if item.delta < -50_000:
            failures.append(f"REQUIRED_COMPONENT_BELOW_NEGATIVE_50000:{item.objective_id.value}")
        if item.objective_id in {
            StrategyObjectiveIdV1.FALSE_GREEN,
            StrategyObjectiveIdV1.MISSED_OPPORTUNITY,
            StrategyObjectiveIdV1.COMPLETION,
        } and item.delta < -20_000:
            failures.append(f"SENSITIVE_COMPONENT_BELOW_NEGATIVE_20000:{item.objective_id.value}")


def _terminal_trade_failures(
    evidence: CandidatePartitionEvidenceV1,
    failures: list[str],
) -> None:
    if evidence.candidate_trades < 30:
        failures.append("FEWER_THAN_THIRTY_CANDIDATE_TRADES")
    if evidence.base_trades <= 0:
        failures.append("ZERO_BASE_TRADES")
    elif not 600_000 <= ratio_ppm(evidence.candidate_trades, evidence.base_trades) <= 1_600_000:
        failures.append("TRADE_RATIO_OUTSIDE_600000_1600000")


def _require_terminal_compatibility(
    first: CandidatePartitionEvidenceV1,
    second: CandidatePartitionEvidenceV1,
) -> None:
    if (
        first.semantic_sha256 != second.semantic_sha256
        or first.compatibility != second.compatibility
        or first.oracle_id != second.oracle_id
    ):
        raise ValueError("terminal evidence is incompatible with validation")


def _settings(family: RobustnessFamilyV1) -> tuple[RobustnessSettingV1, ...]:
    return tuple(item for item in ROBUSTNESS_SETTINGS_V1 if item.family is family)


def _setting_ids(family: RobustnessFamilyV1) -> tuple[str, ...]:
    return tuple(item.setting_id for item in _settings(family))


def _mul_ppm(value: int, multiplier_ppm: int) -> int:
    return round_div_even(value * multiplier_ppm, POLICY_SCALE_V1)


def _shift_regime_rows(
    rows: tuple[RegimeProbabilityRowV1, ...],
    signed_shift: int,
) -> tuple[RegimeProbabilityRowV1, ...] | None:
    if signed_shift not in {-200_000, 200_000}:
        raise ValueError("regime shift must be exactly plus or minus 200000")
    shifted_rows: list[RegimeProbabilityRowV1] = []
    for row in rows:
        maximum_weight = max(item.weight_ppm for item in row.destinations)
        maximum = next(
            item for item in row.destinations if item.weight_ppm == maximum_weight
        )
        minimum_weight = min(
            item.weight_ppm
            for item in row.destinations
            if item.destination != maximum.destination
        )
        minimum = next(
            item
            for item in row.destinations
            if item.destination != maximum.destination
            and item.weight_ppm == minimum_weight
        )
        donor = maximum if signed_shift < 0 else minimum
        receiver = minimum if signed_shift < 0 else maximum
        if donor.weight_ppm <= 200_000:
            return None
        replacements: list[RegimeWeightV1] = []
        for item in row.destinations:
            if item.destination == donor.destination:
                replacements.append(
                    RegimeWeightV1(item.destination, item.weight_ppm - 200_000)
                )
            elif item.destination == receiver.destination:
                replacements.append(
                    RegimeWeightV1(item.destination, item.weight_ppm + 200_000)
                )
            else:
                replacements.append(item)
        shifted_rows.append(RegimeProbabilityRowV1(row.source, tuple(replacements)))
    return tuple(shifted_rows)


def _applied_probe(
    baseline: ControlledRobustnessEnvironmentV1,
    changed: ControlledRobustnessEnvironmentV1,
    setting: RobustnessSettingV1,
    paths: tuple[str, ...],
) -> RobustnessProbeV1:
    if baseline.environment_sha256 == changed.environment_sha256:
        return _failed_probe(setting, PerturbationStatusV1.INVALID, "PERTURBATION_NO_OP")
    return RobustnessProbeV1(
        setting,
        PerturbationStatusV1.APPLIED,
        changed,
        paths,
        "ONE_FACTOR_APPLIED_V1",
    )


def _failed_probe(
    setting: RobustnessSettingV1,
    status: PerturbationStatusV1,
    reason: str,
) -> RobustnessProbeV1:
    return RobustnessProbeV1(setting, status, None, (), reason)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")


def _require_nonnegative_int(value: object, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


__all__ = [
    "ControlledRobustnessEnvironmentV1",
    "ControlledExecutionTimingV1",
    "ENTRY_REMAINDER_HORIZON_US_V1",
    "LiquidityVectorV1",
    "MANDATORY_ROBUSTNESS_FAMILIES_V1",
    "PerturbationStatusV1",
    "ROBUSTNESS_EXPECTED_CELL_COUNT_V1",
    "ROBUSTNESS_POLICY_ID_V1",
    "ROBUSTNESS_REQUIRED_FAMILY_COUNT_V1",
    "ROBUSTNESS_REQUIRED_NONNEGATIVE_FAMILIES_V1",
    "ROBUSTNESS_ROOTS_V1",
    "ROBUSTNESS_SCHEMA_ID_V1",
    "ROBUSTNESS_SETTINGS_V1",
    "RegimeProbabilityRowV1",
    "RegimeWeightV1",
    "RobustnessCellV1",
    "RobustnessEvidenceV1",
    "RobustnessFamilyEvidenceV1",
    "RobustnessFamilyV1",
    "RobustnessOutcomeV1",
    "RobustnessProbeV1",
    "RobustnessQualificationV1",
    "RobustnessSettingV1",
    "SINGLE_VENUE_CAPABILITY_ID_V1",
    "SyntheticRobustnessModeV1",
    "TerminalPartitionQualificationV1",
    "VenueMixDeclarationV1",
    "VolumeVectorV1",
    "apply_robustness_setting",
    "build_robustness_probes",
    "build_synthetic_robustness_evidence",
    "controlled_robustness_environment",
    "derive_execution_timing",
    "qualify_adversarial",
    "qualify_holdout",
    "qualify_robustness",
    "robustness_composite_delta",
]
