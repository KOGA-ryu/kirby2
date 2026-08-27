"""Deterministic statistical risk screens over compact generative results."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean

from kirby2.simulation import load_accepted_hawkes_configs

from .models import StatisticalCheck


def statistical_checks(cases: tuple[dict[str, object], ...]) -> tuple[StatisticalCheck, ...]:
    if not cases:
        raise ValueError("statistical checks require at least one case")
    train = tuple(item for index, item in enumerate(cases) if index % 2 == 0)
    holdout = tuple(item for index, item in enumerate(cases) if index % 2 == 1) or train
    return (
        _calibration_holdout(train, holdout),
        _distribution_drift(train, holdout),
        _scenario_overfit(train, holdout),
        _seed_sensitivity(cases),
        _hawkes_stability(),
        _event_explosion(cases),
        _degenerate_no_trade(cases),
        _price_runaway(cases),
        _permanent_cross(cases),
    )


def _metric(case: dict[str, object], name: str) -> float:
    metrics = case["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("case metrics are missing")
    value = metrics[name]
    return 0.0 if value is None else float(value)


def _calibration_holdout(train, holdout) -> StatisticalCheck:
    train_rate = fmean(_metric(item, "traded_volume") for item in train)
    holdout_rate = fmean(_metric(item, "traded_volume") for item in holdout)
    denominator = max(1.0, abs(train_rate))
    relative_gap = abs(train_rate - holdout_rate) / denominator
    return StatisticalCheck(
        "calibration_train_vs_holdout",
        "PASS" if relative_gap <= 0.50 else "WARNING",
        {
            "holdout_case_count": len(holdout),
            "holdout_mean_traded_volume": round(holdout_rate, 6),
            "relative_gap": round(relative_gap, 6),
            "train_case_count": len(train),
            "train_mean_traded_volume": round(train_rate, 6),
        },
        "relative mean traded-volume gap <= 0.50",
    )


def _distribution_drift(train, holdout) -> StatisticalCheck:
    train_events = fmean(_metric(item, "event_count") for item in train)
    holdout_events = fmean(_metric(item, "event_count") for item in holdout)
    relative = abs(train_events - holdout_events) / max(1.0, train_events)
    return StatisticalCheck(
        "distribution_drift",
        "PASS" if relative <= 0.20 else "WARNING",
        {
            "holdout_mean_event_count": round(holdout_events, 6),
            "relative_mean_shift": round(relative, 6),
            "train_mean_event_count": round(train_events, 6),
        },
        "train/holdout mean event-count shift <= 0.20",
    )


def _scenario_overfit(train, holdout) -> StatisticalCheck:
    def ranking(sample):
        values: dict[str, list[float]] = defaultdict(list)
        for item in sample:
            config = item["configuration"]
            if not isinstance(config, dict):
                continue
            values[str(config["strategy"])].append(_metric(item, "traded_volume"))
        return sorted(
            ((round(fmean(scores), 6), name) for name, scores in values.items()),
            reverse=True,
        )

    train_rank = ranking(train)
    holdout_rank = ranking(holdout)
    same_leader = bool(train_rank and holdout_rank and train_rank[0][1] == holdout_rank[0][1])
    return StatisticalCheck(
        "scenario_overfitting",
        "PASS" if same_leader else "WARNING",
        {
            "holdout_ranking": [name for _, name in holdout_rank],
            "same_leading_strategy": same_leader,
            "train_ranking": [name for _, name in train_rank],
        },
        "leading traded-volume strategy stable across deterministic split",
    )


def _seed_sensitivity(cases) -> StatisticalCheck:
    volumes = [_metric(item, "traded_volume") for item in cases]
    mean = fmean(volumes)
    spread = max(volumes) - min(volumes)
    normalized = spread / max(1.0, mean)
    return StatisticalCheck(
        "seed_sensitivity",
        "PASS" if normalized <= 12.0 else "WARNING",
        {
            "maximum_traded_volume": int(max(volumes)),
            "minimum_traded_volume": int(min(volumes)),
            "normalized_range": round(normalized, 6),
        },
        "normalized traded-volume range <= 12.0",
    )


def _hawkes_stability() -> StatisticalCheck:
    certifications = {
        name: config.stability_certification.as_dict()
        for name, config in sorted(load_accepted_hawkes_configs().items())
    }
    rejected = [
        name
        for name, item in certifications.items()
        if str(item["classification"]).startswith("REJECT")
    ]
    return StatisticalCheck(
        "unstable_hawkes",
        "PASS" if not rejected else "FAIL",
        {"certifications": certifications, "rejected_profiles": rejected},
        "all accepted Hawkes profiles retain a non-rejected stability certificate",
    )


def _event_explosion(cases) -> StatisticalCheck:
    return StatisticalCheck(
        "unrealistic_event_explosion",
        "NOT_EXERCISED",
        {
            "case_count": len(cases),
            "missing_measurement": "events_per_simulated_second",
            "reason": (
                "duration_events is a facsimile command-count control and is "
                "not a common time unit across real executor lanes"
            ),
            "required_reference": "configured_production_event_rate_cap",
        },
        "requires native events per simulated second and configured cap",
    )


def _degenerate_no_trade(cases) -> StatisticalCheck:
    fraction = sum(_metric(item, "trade_count") == 0 for item in cases) / len(cases)
    return StatisticalCheck(
        "degenerate_no_trade",
        "PASS" if fraction <= 0.90 else "WARNING",
        {"no_trade_fraction": round(fraction, 6)},
        "no-trade fraction <= 0.90 across mixed session phases and strategies",
    )


def _price_runaway(cases) -> StatisticalCheck:
    maximum = max(_metric(item, "price_displacement_ticks") for item in cases)
    return StatisticalCheck(
        "price_runaway",
        "PASS" if maximum <= 100 else "FAIL",
        {"maximum_displacement_ticks": int(maximum)},
        "maximum trade displacement <= 100 ticks",
    )


def _permanent_cross(cases) -> StatisticalCheck:
    crossed = sum(
        _metric(item, "spread_ticks") <= 0
        for item in cases
        if item["metrics"]["spread_ticks"] is not None
    )
    return StatisticalCheck(
        "permanent_crossed_composite_quote",
        "PASS" if crossed == 0 else "FAIL",
        {"crossed_case_count": crossed},
        "no ending composite quote is locked or crossed",
    )
