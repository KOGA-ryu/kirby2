"""Descriptive calibration measurements over normalized observable events."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import Counter

from .models import (
    CalibrationMetric,
    CalibrationReport,
    NormalizedEventType,
    NormalizedMarketEvent,
    NormalizedMarketStream,
    ObservationCapability,
)


MINIMUM_INFERENCE_SAMPLE_COUNT = 30
RETURN_HORIZON_US = 1_000_000
CLUSTER_BUCKET_US = 1_000_000
VOLUME_PROFILE_BUCKETS = 10


def measure_stream(stream: NormalizedMarketStream) -> CalibrationReport:
    events = stream.events
    capabilities = set(stream.capabilities)
    has_orders = ObservationCapability.ORDER_EVENTS in capabilities
    has_trades = ObservationCapability.TRADE_EVENTS in capabilities
    has_aggressors = ObservationCapability.AGGRESSOR_SIDE in capabilities
    has_spreads = ObservationCapability.BOOK_SPREAD in capabilities
    has_prices = ObservationCapability.BOOK_PRICES in capabilities
    has_depth = ObservationCapability.BOOK_DEPTH in capabilities
    books = _book_series(events)
    commands = tuple(
        event
        for event in events
        if event.event_type
        in {
            NormalizedEventType.LIMIT,
            NormalizedEventType.MARKET,
            NormalizedEventType.CANCEL,
        }
    )
    limits = tuple(event for event in events if event.event_type is NormalizedEventType.LIMIT)
    cancels = tuple(event for event in events if event.event_type is NormalizedEventType.CANCEL)
    trades = tuple(event for event in events if event.event_type is NormalizedEventType.TRADE)

    spreads = _spread_samples(events) if has_spreads else []
    top_depth = [
        book[3] + book[4]
        for book in books
        if has_depth and book[3] > 0 and book[4] > 0
    ]
    multi_depth = [
        book[5] + book[6]
        for book in books
        if has_depth and book[5] > 0 and book[6] > 0
    ]
    imbalances = [
        (book[3] - book[4]) / (book[3] + book[4])
        for book in books
        if has_depth and book[3] + book[4] > 0
    ]
    trade_sizes = [event.quantity for event in trades if event.quantity is not None]
    limit_sizes = [event.quantity for event in limits if event.quantity is not None]
    placement_depths = [
        event.placement_depth_ticks
        for event in limits
        if event.placement_depth_ticks is not None
    ]
    cancel_sizes = [event.quantity for event in cancels if event.quantity is not None]
    inter_event_times = [
        current.timestamp_us - previous.timestamp_us
        for previous, current in zip(commands, commands[1:])
    ]
    queue_lifetimes = _queue_lifetimes(events) if has_orders else []
    short_returns = (
        _short_term_returns(books, RETURN_HORIZON_US) if has_prices else []
    )
    realized_volatility = _realized_volatility_bps(books) if has_prices else 0.0
    impacts = (
        _price_impacts(trades, books, RETURN_HORIZON_US)
        if has_prices and has_aggressors
        else []
    )

    duration_seconds = stream.duration_us / 1_000_000.0
    event_counts = Counter(event.event_type.value.lower() for event in commands)
    event_rates: dict[str, float | None] = {
        name: round(event_counts[name] / duration_seconds, 9) if has_orders else None
        for name in ("cancel", "limit", "market")
    }
    event_rates["trade"] = (
        round(len(trades) / duration_seconds, 9) if has_trades else None
    )
    buy_volume = sum(
        event.quantity or 0 for event in trades if event.aggressor_side == "buy"
    )
    sell_volume = sum(
        event.quantity or 0 for event in trades if event.aggressor_side == "sell"
    )
    ratio = buy_volume / sell_volume if sell_volume else None
    volume_profile = _volume_profile(trades, stream.duration_us)

    metrics = (
        _distribution_metric("spread_distribution", "ticks", spreads),
        _distribution_metric(
            "top_of_book_depth_distribution",
            "shares_combined_best_bid_ask",
            top_depth,
        ),
        _distribution_metric(
            "multi_level_depth_distribution",
            "shares_all_visible_levels",
            multi_depth,
        ),
        _distribution_metric("trade_size_distribution", "shares", trade_sizes)
        if has_trades
        else _unavailable("trade_size_distribution", "shares", "trade events not supplied"),
        _distribution_metric("limit_order_size_distribution", "shares", limit_sizes)
        if has_orders
        else _unavailable(
            "limit_order_size_distribution", "shares", "order events not supplied"
        ),
        _distribution_metric(
            "limit_placement_depth_distribution",
            "ticks_behind_same_side_touch",
            placement_depths,
        )
        if has_orders
        else _unavailable(
            "limit_placement_depth_distribution",
            "ticks_behind_same_side_touch",
            "order events not supplied",
        ),
        _distribution_metric("cancel_size_distribution", "shares", cancel_sizes)
        if has_orders
        else _unavailable("cancel_size_distribution", "shares", "order events not supplied"),
        CalibrationMetric(
            "event_rates",
            "events_per_second",
            len(commands) + len(trades),
            event_rates,
            available=has_orders or has_trades,
            note="null channels were not supplied by the source",
        ),
        _distribution_metric(
            "inter_event_time_distribution",
            "microseconds",
            inter_event_times,
        )
        if has_orders
        else _unavailable(
            "inter_event_time_distribution",
            "microseconds",
            "order events not supplied",
        ),
        CalibrationMetric(
            "buy_sell_aggressor_ratio",
            "buy_volume_per_sell_volume",
            len(trades),
            round(ratio, 9),
        )
        if has_aggressors and trades and ratio is not None
        else CalibrationMetric(
            "buy_sell_aggressor_ratio",
            "buy_volume_per_sell_volume",
            len(trades) if has_aggressors else 0,
            None,
            available=False,
            note=(
                "aggressor side not supplied"
                if not has_aggressors
                else "ratio undefined because observed sell-aggressor volume is zero"
            ),
        ),
        CalibrationMetric(
            "cancellation_rate",
            "events_per_second",
            len(cancels),
            round(len(cancels) / duration_seconds, 9),
        )
        if has_orders
        else _unavailable(
            "cancellation_rate", "events_per_second", "order events not supplied"
        ),
        _distribution_metric("queue_lifetime", "microseconds", queue_lifetimes)
        if has_orders
        else _unavailable("queue_lifetime", "microseconds", "order events not supplied"),
        _distribution_metric("imbalance_distribution", "ratio", imbalances),
        _distribution_metric(
            "short_term_return_distribution",
            "midpoint_ticks_per_1_second",
            short_returns,
        ),
        CalibrationMetric(
            "realized_volatility",
            "basis_points_root_sum_squared_log_returns",
            max(0, len(_mid_series(books)) - 1),
            round(realized_volatility, 9),
            available=len(_mid_series(books)) >= 2,
            note=None if len(_mid_series(books)) >= 2 else "requires at least two midpoint observations",
        )
        if has_prices and len(_mid_series(books)) >= 2
        else _unavailable(
            "realized_volatility",
            "basis_points_root_sum_squared_log_returns",
            "requires at least two midpoint observations",
        ),
        _distribution_metric(
            "price_impact",
            "signed_midpoint_ticks_after_1_second",
            impacts,
        )
        if has_prices and has_aggressors
        else _unavailable(
            "price_impact",
            "signed_midpoint_ticks_after_1_second",
            "book prices and aggressor side are required",
        ),
        CalibrationMetric(
            "volume_profile",
            "shares_per_equal_session_decile",
            len(trades),
            volume_profile,
            note="ten equal-duration buckets over the normalized stream",
        )
        if has_trades
        else _unavailable(
            "volume_profile",
            "shares_per_equal_session_decile",
            "trade events not supplied",
        ),
        _clustering_metric("trade_clustering", trades, stream.duration_us)
        if has_trades
        else _unavailable(
            "trade_clustering",
            "one_second_count_dispersion",
            "trade events not supplied",
        ),
        _clustering_metric("cancel_clustering", cancels, stream.duration_us)
        if has_orders
        else _unavailable(
            "cancel_clustering",
            "one_second_count_dispersion",
            "order events not supplied",
        ),
    )
    warnings: list[str] = []
    for metric in metrics:
        if not metric.available:
            warnings.append(f"{metric.name}: unavailable from supplied observations")
        elif metric.sample_count < MINIMUM_INFERENCE_SAMPLE_COUNT:
            warnings.append(
                f"{metric.name}: sample_count={metric.sample_count} is below "
                f"{MINIMUM_INFERENCE_SAMPLE_COUNT}; descriptive only"
            )
    warnings.append(
        "No statistical-equivalence claim is made; measurements are descriptive calibration targets."
    )
    return CalibrationReport(
        source_id=stream.source_id,
        source_kind=stream.source_kind,
        real_market_data=stream.real_market_data,
        duration_us=stream.duration_us,
        normalized_stream_sha256=stream.sha256(),
        observation_capabilities=stream.capabilities,
        metrics=metrics,
        warnings=tuple(warnings),
    )


def _distribution_metric(
    name: str,
    unit: str,
    samples: list[int | float],
) -> CalibrationMetric:
    if not samples:
        return _unavailable(name, unit)
    ordered = sorted(float(value) for value in samples)
    return CalibrationMetric(
        name=name,
        unit=unit,
        sample_count=len(ordered),
        value={
            "maximum": round(ordered[-1], 9),
            "mean": round(sum(ordered) / len(ordered), 9),
            "median": round(_quantile(ordered, 0.50), 9),
            "minimum": round(ordered[0], 9),
            "q25": round(_quantile(ordered, 0.25), 9),
            "q75": round(_quantile(ordered, 0.75), 9),
        },
    )


def _unavailable(name: str, unit: str, note: str | None = None) -> CalibrationMetric:
    return CalibrationMetric(name, unit, 0, None, available=False, note=note)


def _book_series(
    events: tuple[NormalizedMarketEvent, ...],
) -> list[tuple[int, int | None, int | None, int, int, int, int]]:
    by_time: dict[int, NormalizedMarketEvent] = {}
    for event in events:
        if event.event_type is NormalizedEventType.BOOK:
            by_time[event.timestamp_us] = event
    result = []
    for timestamp, event in sorted(by_time.items()):
        bid = event.bid_levels[0].price_ticks if event.bid_levels else None
        ask = event.ask_levels[0].price_ticks if event.ask_levels else None
        result.append(
            (
                timestamp,
                bid,
                ask,
                event.bid_levels[0].quantity if event.bid_levels else 0,
                event.ask_levels[0].quantity if event.ask_levels else 0,
                sum(level.quantity for level in event.bid_levels),
                sum(level.quantity for level in event.ask_levels),
            )
        )
    return result


def _spread_samples(events: tuple[NormalizedMarketEvent, ...]) -> list[int]:
    by_time: dict[int, int] = {}
    for event in events:
        if event.event_type is not NormalizedEventType.BOOK:
            continue
        if event.spread_ticks is not None:
            by_time[event.timestamp_us] = event.spread_ticks
        elif event.bid_levels and event.ask_levels:
            by_time[event.timestamp_us] = (
                event.ask_levels[0].price_ticks - event.bid_levels[0].price_ticks
            )
    return [spread for _, spread in sorted(by_time.items())]


def _mid_series(
    books: list[tuple[int, int | None, int | None, int, int, int, int]],
) -> list[tuple[int, float]]:
    return [
        (book[0], (book[1] + book[2]) / 2.0)
        for book in books
        if book[1] is not None and book[2] is not None
    ]


def _short_term_returns(
    books: list[tuple[int, int | None, int | None, int, int, int, int]],
    horizon_us: int,
) -> list[float]:
    mids = _mid_series(books)
    returns: list[float] = []
    prior_index = 0
    for index, (timestamp, midpoint) in enumerate(mids):
        target = timestamp - horizon_us
        while prior_index + 1 < index and mids[prior_index + 1][0] <= target:
            prior_index += 1
        if index > 0 and mids[prior_index][0] <= target:
            returns.append(midpoint - mids[prior_index][1])
    return returns


def _realized_volatility_bps(
    books: list[tuple[int, int | None, int | None, int, int, int, int]],
) -> float:
    mids = _mid_series(books)
    squared = [
        math.log(current[1] / previous[1]) ** 2
        for previous, current in zip(mids, mids[1:])
        if previous[1] > 0 and current[1] > 0
    ]
    return math.sqrt(sum(squared)) * 10_000.0


def _price_impacts(
    trades: tuple[NormalizedMarketEvent, ...],
    books: list[tuple[int, int | None, int | None, int, int, int, int]],
    horizon_us: int,
) -> list[float]:
    mids = _mid_series(books)
    timestamps = [item[0] for item in mids]
    impacts: list[float] = []
    for trade in trades:
        before_index = bisect_left(timestamps, trade.timestamp_us) - 1
        after_index = bisect_left(timestamps, trade.timestamp_us + horizon_us)
        if (
            before_index < 0
            or after_index >= len(mids)
            or trade.aggressor_side is None
        ):
            continue
        sign = 1.0 if trade.aggressor_side == "buy" else -1.0
        impacts.append(sign * (mids[after_index][1] - mids[before_index][1]))
    return impacts


def _queue_lifetimes(events: tuple[NormalizedMarketEvent, ...]) -> list[int]:
    active: dict[str, list[int]] = {}
    lifetimes: list[int] = []
    for event in events:
        if (
            event.event_type is NormalizedEventType.LIMIT
            and event.order_id is not None
            and event.remaining_quantity is not None
            and event.remaining_quantity > 0
        ):
            active[event.order_id] = [event.timestamp_us, event.remaining_quantity]
        elif (
            event.event_type is NormalizedEventType.TRADE
            and event.maker_order_id in active
            and event.quantity is not None
        ):
            state = active[event.maker_order_id]
            state[1] -= event.quantity
            if state[1] <= 0:
                lifetimes.append(event.timestamp_us - state[0])
                del active[event.maker_order_id]
        elif (
            event.event_type is NormalizedEventType.CANCEL
            and event.target_order_id in active
        ):
            state = active.pop(event.target_order_id)
            lifetimes.append(event.timestamp_us - state[0])
    return lifetimes


def _volume_profile(
    trades: tuple[NormalizedMarketEvent, ...],
    duration_us: int,
) -> dict[str, int]:
    buckets = [0 for _ in range(VOLUME_PROFILE_BUCKETS)]
    for trade in trades:
        index = min(
            VOLUME_PROFILE_BUCKETS - 1,
            trade.timestamp_us * VOLUME_PROFILE_BUCKETS // duration_us,
        )
        buckets[index] += trade.quantity or 0
    return {f"bucket_{index + 1:02d}": value for index, value in enumerate(buckets)}


def _clustering_metric(
    name: str,
    events: tuple[NormalizedMarketEvent, ...],
    duration_us: int,
) -> CalibrationMetric:
    bucket_count = max(1, math.ceil(duration_us / CLUSTER_BUCKET_US))
    counts = [0 for _ in range(bucket_count)]
    for event in events:
        index = min(bucket_count - 1, event.timestamp_us // CLUSTER_BUCKET_US)
        counts[index] += 1
    mean = sum(counts) / bucket_count
    variance = sum((value - mean) ** 2 for value in counts) / bucket_count
    return CalibrationMetric(
        name=name,
        unit="one_second_count_dispersion",
        sample_count=bucket_count,
        value={
            "index_of_dispersion": round(variance / mean, 9) if mean > 0 else 0.0,
            "maximum_bucket_count": max(counts),
            "mean_bucket_count": round(mean, 9),
        },
        note="variance-to-mean index above one indicates over-dispersed arrivals",
    )


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
