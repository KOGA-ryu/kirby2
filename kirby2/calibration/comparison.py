"""Explicit descriptive differences between two calibration reports."""

from __future__ import annotations

from dataclasses import dataclass

from .models import CalibrationMetric, CalibrationReport


MAJOR_MEASUREMENT_PATHS = (
    ("spread_distribution", "mean"),
    ("top_of_book_depth_distribution", "mean"),
    ("multi_level_depth_distribution", "mean"),
    ("trade_size_distribution", "mean"),
    ("limit_order_size_distribution", "mean"),
    ("cancel_size_distribution", "mean"),
    ("event_rates", "trade"),
    ("event_rates", "limit"),
    ("event_rates", "cancel"),
    ("inter_event_time_distribution", "mean"),
    ("buy_sell_aggressor_ratio", None),
    ("cancellation_rate", None),
    ("queue_lifetime", "mean"),
    ("imbalance_distribution", "mean"),
    ("short_term_return_distribution", "mean"),
    ("realized_volatility", None),
    ("price_impact", "mean"),
    *(("volume_profile", f"bucket_{index:02d}") for index in range(1, 11)),
    ("trade_clustering", "index_of_dispersion"),
    ("cancel_clustering", "index_of_dispersion"),
)


@dataclass(frozen=True, slots=True)
class CalibrationDifference:
    metric: str
    component: str | None
    unit: str
    reference_sample_count: int
    candidate_sample_count: int
    reference_value: float | None
    candidate_value: float | None
    absolute_difference: float | None
    relative_difference: float | None
    available: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "absolute_difference": self.absolute_difference,
            "available": self.available,
            "candidate_sample_count": self.candidate_sample_count,
            "candidate_value": self.candidate_value,
            "component": self.component,
            "metric": self.metric,
            "reference_sample_count": self.reference_sample_count,
            "reference_value": self.reference_value,
            "relative_difference": self.relative_difference,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class CalibrationComparison:
    reference: CalibrationReport
    candidate: CalibrationReport
    differences: tuple[CalibrationDifference, ...]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_source_id": self.candidate.source_id,
            "descriptive_only": True,
            "differences": [difference.as_dict() for difference in self.differences],
            "reference_source_id": self.reference.source_id,
            "statistical_equivalence_claimed": False,
            "warnings": list(self.warnings),
        }


def compare_reports(
    reference: CalibrationReport,
    candidate: CalibrationReport,
) -> CalibrationComparison:
    differences = tuple(
        _difference(
            reference.metric(metric_name),
            candidate.metric(metric_name),
            component,
        )
        for metric_name, component in MAJOR_MEASUREMENT_PATHS
    )
    warnings = [
        "Differences are descriptive; no hypothesis test or equivalence claim was performed."
    ]
    low_count = [
        difference
        for difference in differences
        if difference.available
        and min(
            difference.reference_sample_count,
            difference.candidate_sample_count,
        )
        < 30
    ]
    if low_count:
        warnings.append(
            f"{len(low_count)} compared components include a sample count below 30."
        )
    return CalibrationComparison(reference, candidate, differences, tuple(warnings))


def _difference(
    reference: CalibrationMetric,
    candidate: CalibrationMetric,
    component: str | None,
) -> CalibrationDifference:
    if reference.unit != candidate.unit:
        raise ValueError(
            f"cannot compare {reference.name}: unit mismatch "
            f"{reference.unit!r} vs {candidate.unit!r}"
        )
    reference_value = _component(reference, component)
    candidate_value = _component(candidate, component)
    available = reference_value is not None and candidate_value is not None
    absolute = (
        None
        if not available
        else round(candidate_value - reference_value, 9)
    )
    relative = (
        None
        if not available or reference_value == 0
        else round((candidate_value - reference_value) / abs(reference_value), 9)
    )
    return CalibrationDifference(
        metric=reference.name,
        component=component,
        unit=reference.unit,
        reference_sample_count=reference.sample_count,
        candidate_sample_count=candidate.sample_count,
        reference_value=reference_value,
        candidate_value=candidate_value,
        absolute_difference=absolute,
        relative_difference=relative,
        available=available,
    )


def _component(metric: CalibrationMetric, component: str | None) -> float | None:
    if not metric.available:
        return None
    value = metric.value
    if component is not None:
        if not isinstance(value, dict) or component not in value:
            raise ValueError(f"metric {metric.name} lacks component {component}")
        value = value[component]
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"metric {metric.name} component is not numeric")
    return float(value)
