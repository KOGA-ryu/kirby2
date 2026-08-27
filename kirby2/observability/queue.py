"""Observable queue estimator that refuses exactness without MBO evidence."""

from __future__ import annotations

from decimal import Decimal

from kirby2.exchange.models import Side

from .models import (
    ObservableEventType,
    ObservableMarketFeed,
    QueueDataMode,
    QueuePositionEstimate,
)


class QueuePositionEstimator:
    def estimate(
        self,
        feed: ObservableMarketFeed,
        order_id: str,
        *,
        data_mode: QueueDataMode,
        market_by_order_quantity_ahead: int | None = None,
    ) -> QueuePositionEstimate:
        own = next(
            (order for order in feed.own_orders if order.order_id == order_id),
            None,
        )
        if own is None or not own.acknowledged:
            raise ValueError(f"player order is not acknowledged: {order_id}")
        if own.price_ticks is None:
            raise ValueError("queue position applies only to a resting priced order")
        if data_mode is QueueDataMode.MARKET_BY_ORDER:
            if (
                type(market_by_order_quantity_ahead) is not int
                or market_by_order_quantity_ahead < 0
            ):
                raise ValueError("MBO estimate requires exact nonnegative queue evidence")
            return QueuePositionEstimate(
                order_id,
                market_by_order_quantity_ahead,
                market_by_order_quantity_ahead,
                market_by_order_quantity_ahead,
                Decimal(1),
                feed.simulation_time_us,
                (
                    "complete market-by-order sequence is available",
                    "venue priority rules are known",
                ),
                data_mode,
                True,
            )
        if market_by_order_quantity_ahead is not None:
            raise ValueError("aggregated-depth mode cannot accept exact MBO queue truth")
        levels = feed.book.bids if own.side is Side.BUY else feed.book.asks
        level = levels.get(own.price_ticks)
        displayed_total = 0 if level is None else level.total_quantity
        visible_opponent = max(0, displayed_total - own.displayed_quantity)
        evidence_events = sum(
            event.event_type
            in {
                ObservableEventType.TRADE,
                ObservableEventType.EXPLICIT_REPLENISHMENT,
                ObservableEventType.DISPLAY_QUANTITY_CHANGED,
            }
            for event in feed.events
        )
        confidence = Decimal("0.55") if evidence_events else Decimal("0.35")
        upper = visible_opponent * 2 + own.displayed_quantity
        return QueuePositionEstimate(
            order_id,
            visible_opponent,
            0,
            max(visible_opponent, upper),
            confidence,
            feed.simulation_time_us,
            (
                "only aggregate displayed depth is observable",
                "opponent time priority within the level is unknown",
                "hidden and reserve quantity are excluded",
                "display changes can reflect execution cancellation refresh delay or replacement",
            ),
            data_mode,
            False,
        )
