"""Terminal presentation and provenance report for historical-mode runs."""

from __future__ import annotations

import json
import math
from typing import Any

from .models import HistoricalDataMode, HistoricalRun


RECONSTRUCTION_TITLE = "HISTORICAL RECONSTRUCTION"
RECONSTRUCTION_DISCLOSURE = (
    "Synthetic order book constrained by historical market data."
)


def historical_metrics(run: HistoricalRun) -> dict[str, Any]:
    run.book.assert_invariants()
    trades = run.generated_trades
    prices = [trade.price_ticks for trade in trades]
    average_spread = (
        round(sum(run.spread_samples_ticks) / len(run.spread_samples_ticks), 6)
        if run.spread_samples_ticks
        else None
    )
    metrics: dict[str, Any] = {
        "average_spread_ticks": average_spread,
        "ending_best_ask_ticks": run.book.best_ask,
        "ending_best_bid_ticks": run.book.best_bid,
        "ending_depth": {
            "active_orders": len(run.book.active_orders),
            "ask_levels": len(run.book.ask_prices),
            "ask_quantity": sum(level.total_quantity for level in run.book.asks.values()),
            "bid_levels": len(run.book.bid_prices),
            "bid_quantity": sum(level.total_quantity for level in run.book.bids.values()),
        },
        "exchange_event_count": len(run.exchange_events),
        "generated_close_ticks": prices[-1] if prices else None,
        "generated_high_ticks": max(prices) if prices else None,
        "generated_low_ticks": min(prices) if prices else None,
        "generated_open_ticks": prices[0] if prices else None,
        "generated_realized_volatility_bps": _realized_volatility_bps(prices),
        "invariant_status": "PASS",
        "replay_sha256": run.replay_sha256(),
        "source_message_count": run.source_message_count,
        "source_trade_count": run.source_trade_count,
        "synthetic_command_count": run.synthetic_command_count,
        "trade_count": len(trades),
        "traded_volume": sum(trade.quantity for trade in trades),
    }
    if run.constraints is not None:
        constraints = run.constraints
        metrics["constraint_residuals"] = {
            "close_ticks": _difference(prices[-1] if prices else None, constraints.close_ticks),
            "high_ticks": _difference(max(prices) if prices else None, constraints.high_ticks),
            "low_ticks": _difference(min(prices) if prices else None, constraints.low_ticks),
            "open_ticks": _difference(prices[0] if prices else None, constraints.open_ticks),
            "realized_volatility_bps": _difference(
                metrics["generated_realized_volatility_bps"],
                float(constraints.realized_volatility_bps),
            ),
            "traded_volume": metrics["traded_volume"] - constraints.aggregate_volume,
        }
        metrics["constraint_targets"] = {
            "aggregate_volume": constraints.aggregate_volume,
            "close_ticks": constraints.close_ticks,
            "high_ticks": constraints.high_ticks,
            "low_ticks": constraints.low_ticks,
            "open_ticks": constraints.open_ticks,
            "realized_volatility_bps": str(constraints.realized_volatility_bps),
            "spread_observation_count": len(constraints.spread_observations),
            "trade_print_observation_count": len(constraints.trade_prints),
        }
    return metrics


def render_historical_ui(run: HistoricalRun, levels: int = 4) -> str:
    if type(levels) is not int or levels <= 0:
        raise ValueError("historical ladder levels must be positive")
    if run.mode is HistoricalDataMode.RECONSTRUCTION:
        mode_lines = [RECONSTRUCTION_TITLE, RECONSTRUCTION_DISCLOSURE]
    else:
        mode_lines = [
            "EXACT REPLAY FIXTURE",
            "Source fixture order messages replayed exactly.",
        ]
    lines = [
        "KIRBY2_HISTORICAL_VIEW",
        *mode_lines,
        f"FIXTURE {run.fixture_id} | {run.label}",
        (
            f"DATA_SCOPE real_market_data={str(run.provenance.real_market_data).lower()} "
            f"orders={str(run.provenance.provides_order_events).lower()} "
            f"trades={str(run.provenance.provides_trade_events).lower()} "
            "aggressor_side="
            f"{str(run.provenance.provides_trade_aggressor_side).lower()} "
            f"book={str(run.provenance.provides_book_events).lower()}"
        ),
        f"ORDER_PROVENANCE {run.order_provenance_label}",
        "",
        "PRICE LADDER",
    ]
    selected_asks = run.book.ask_prices[:levels]
    for price in reversed(selected_asks):
        lines.append(
            f"ASK  {_price(run, price):>9}  {run.book.asks[price].total_quantity:>7}"
        )
    lines.append(
        f"----  bid={_price(run, run.book.best_bid)} ask={_price(run, run.book.best_ask)}"
    )
    for price in run.book.bid_prices[:levels]:
        lines.append(
            f"BID  {_price(run, price):>9}  {run.book.bids[price].total_quantity:>7}"
        )
    lines.extend(("", "RECENT TAPE"))
    if run.generated_trades:
        for trade in run.generated_trades[-6:]:
            lines.append(
                f"{trade.trade_id}  {_price(run, trade.price_ticks):>9}  "
                f"{trade.quantity:>6}  taker={trade.taker_side.value}"
            )
    else:
        lines.append("NO_TRADES")
    return "\n".join(lines)


def render_historical_report(run: HistoricalRun) -> str:
    from .features import (
        HistoricalEvidenceScope,
        historical_feature_provenance_summary,
        replay_historical_features,
    )

    metrics = historical_metrics(run)
    source_features = replay_historical_features(
        run,
        windows_us=(1_000_000,),
    )
    if run.mode is HistoricalDataMode.RECONSTRUCTION:
        disclosure = [RECONSTRUCTION_TITLE, RECONSTRUCTION_DISCLOSURE]
    else:
        disclosure = [
            "EXACT REPLAY FIXTURE",
            "Exact means the supplied fixture messages were replayed without synthesis.",
        ]
    lines = [
        "KIRBY2_HISTORICAL_REPORT",
        *disclosure,
        f"MODE {run.mode.value}",
        f"FIXTURE {run.fixture_id}",
        f"DATASET {run.provenance.dataset_id}",
        f"SOURCE {run.provenance.source_name}",
        f"SOURCE_LOCATOR {run.provenance.source_locator}",
        f"SOURCE_DESCRIPTION {run.provenance.description}",
        f"REAL_MARKET_DATA {str(run.provenance.real_market_data).lower()}",
        (
            "SOURCE_CAPABILITIES "
            f"orders={str(run.provenance.provides_order_events).lower()} "
            f"trades={str(run.provenance.provides_trade_events).lower()} "
            "aggressor_side="
            f"{str(run.provenance.provides_trade_aggressor_side).lower()} "
            f"book={str(run.provenance.provides_book_events).lower()}"
        ),
        f"ORDER_PROVENANCE {run.order_provenance_label}",
        "SOURCE_FEATURE_PROVENANCE "
        + json.dumps(
            historical_feature_provenance_summary(source_features.terminal_frame),
            sort_keys=True,
            separators=(",", ":"),
        ),
        f"HISTORICAL_FEATURE_REPLAY_SHA256 {source_features.replay_sha256()}",
    ]
    if run.mode is HistoricalDataMode.EXACT_REPLAY:
        lines.append(
            "SOURCE_TRADE_VALIDATION PASS "
            f"source={run.source_trade_count} replayed={metrics['trade_count']}"
        )
        if not run.provenance.real_market_data:
            lines.append(
                "REAL_MARKET_CLAIM none; this is a local pedagogical exact-event fixture"
            )
    else:
        reconstruction_features = replay_historical_features(
            run,
            windows_us=(1_000_000,),
            evidence_scope=HistoricalEvidenceScope.INCLUDE_RECONSTRUCTION,
        )
        calibration = (
            run.reconstruction_config.get("deterministic_calibration", {})
            if run.reconstruction_config is not None
            else {}
        )
        lines.extend(
            (
                f"RECONSTRUCTION_SEED {run.reconstruction_seed}",
                "DETERMINISTIC_CALIBRATION "
                + json.dumps(calibration, sort_keys=True, separators=(",", ":")),
                "CONSTRAINT_FIT residuals are disclosed; generated orders are not observations",
                "RECONSTRUCTION_FEATURE_PROVENANCE "
                + json.dumps(
                    historical_feature_provenance_summary(
                        reconstruction_features.terminal_frame
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    {
                        "generated": {
                            key: metrics[key]
                            for key in (
                                "average_spread_ticks",
                                "generated_close_ticks",
                                "generated_high_ticks",
                                "generated_low_ticks",
                                "generated_open_ticks",
                                "generated_realized_volatility_bps",
                                "trade_count",
                                "traded_volume",
                            )
                        },
                        "residuals": metrics["constraint_residuals"],
                        "targets": metrics["constraint_targets"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        if run.constraints is not None:
            for event in run.constraints.known_market_events:
                lines.append(f"KNOWN_MARKET_EVENT {event}")
    lines.extend(
        (
            "OUTCOME "
            + json.dumps(
                {
                    key: metrics[key]
                    for key in (
                        "ending_best_ask_ticks",
                        "ending_best_bid_ticks",
                        "ending_depth",
                        "exchange_event_count",
                        "trade_count",
                        "traded_volume",
                    )
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            f"REPLAY_SHA256 {metrics['replay_sha256']}",
            "RUNTIME_INVARIANTS PASS",
        )
    )
    return "\n".join(lines)


def _price(run: HistoricalRun, price_ticks: int | None) -> str:
    if price_ticks is None:
        return "EMPTY"
    return format(run.tick_size * price_ticks, "f")


def _realized_volatility_bps(prices: list[int]) -> float | None:
    if len(prices) < 2:
        return None
    squared_returns = [
        ((current - previous) / previous) ** 2
        for previous, current in zip(prices, prices[1:])
    ]
    return round(math.sqrt(sum(squared_returns)) * 10_000.0, 6)


def _difference(generated: float | int | None, target: float | int) -> float | None:
    if generated is None:
        return None
    return round(float(generated) - float(target), 6)
