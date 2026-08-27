"""Explainable simulator routing baselines over immutable public snapshots."""

from __future__ import annotations

from abc import ABC, abstractmethod

from kirby2.exchange import OrderInstruction, SessionState
from kirby2.exchange.models import Side

from .models import (
    ConsolidatedFeed,
    RouteDecision,
    RouteLegPlan,
    RoutePolicy,
    RouteStyle,
    RoutingRequest,
    VenueQuote,
)


class SmartOrderRouter(ABC):
    """Baseline interface. Implementations receive no venue or ground-truth object."""

    policy: RoutePolicy

    @abstractmethod
    def decide(
        self,
        route_id: str,
        request: RoutingRequest,
        feed: ConsolidatedFeed,
    ) -> RouteDecision:
        raise NotImplementedError

    def _decision(
        self,
        route_id: str,
        request: RoutingRequest,
        feed: ConsolidatedFeed,
        legs: tuple[RouteLegPlan, ...],
        explanation: str,
    ) -> RouteDecision:
        evidence = feed.as_dict()
        return RouteDecision(
            route_id,
            feed.simulation_time_us,
            self.policy,
            feed.sha256(),
            evidence,
            legs,
            explanation,
        )


class DirectRouter(SmartOrderRouter):
    policy = RoutePolicy.DIRECT

    def decide(self, route_id, request, feed):
        quote = _quote(feed, request.direct_venue_id)
        leg = _leg(quote, request, request.quantity, "explicit venue selected")
        return self._decision(
            route_id,
            request,
            feed,
            (leg,),
            f"DIRECT selected {quote.venue_id} from the explicit player instruction",
        )


class BestDisplayedPriceRouter(SmartOrderRouter):
    policy = RoutePolicy.BEST_DISPLAYED_PRICE

    def decide(self, route_id, request, feed):
        quotes = _eligible_quotes(feed, request)
        selected = min(quotes, key=lambda quote: _price_key(quote, request))
        return self._decision(
            route_id,
            request,
            feed,
            (_leg(selected, request, request.quantity, "best displayed price"),),
            f"BEST_DISPLAYED_PRICE selected {selected.venue_id} using displayed quotes only",
        )


class LowestExpectedCostRouter(SmartOrderRouter):
    policy = RoutePolicy.LOWEST_EXPECTED_COST

    def decide(self, route_id, request, feed):
        quotes = _eligible_quotes(feed, request)
        selected = min(quotes, key=lambda quote: _expected_cost_key(quote, request))
        return self._decision(
            route_id,
            request,
            feed,
            (_leg(selected, request, request.quantity, "lowest displayed price plus known fee"),),
            f"LOWEST_EXPECTED_COST selected {selected.venue_id}; hidden liquidity and future fills were unavailable",
        )


class PassiveQueueRouter(SmartOrderRouter):
    policy = RoutePolicy.PASSIVE_QUEUE

    def decide(self, route_id, request, feed):
        if request.style is not RouteStyle.PASSIVE:
            raise ValueError("PASSIVE_QUEUE requires PASSIVE route style")
        ranked = sorted(
            _eligible_quotes(feed, request),
            key=lambda quote: _passive_key(quote, request),
        )[: request.max_venues]
        remaining = request.quantity
        legs: list[RouteLegPlan] = []
        for index, quote in enumerate(ranked):
            venues_left = len(ranked) - index
            quantity = (remaining + venues_left - 1) // venues_left
            legs.append(
                _leg(
                    quote,
                    request,
                    quantity,
                    "passive price, displayed queue, and maker rebate",
                )
            )
            remaining -= quantity
        return self._decision(
            route_id,
            request,
            feed,
            tuple(legs),
            "PASSIVE_QUEUE ranked only displayed queue size, displayed price, and published rebate",
        )


class SweepRouter(SmartOrderRouter):
    policy = RoutePolicy.SWEEP

    def decide(self, route_id, request, feed):
        if request.style is not RouteStyle.AGGRESSIVE:
            raise ValueError("SWEEP requires AGGRESSIVE route style")
        ranked = sorted(_eligible_quotes(feed, request), key=lambda q: _price_key(q, request))
        remaining = request.quantity
        legs: list[RouteLegPlan] = []
        for quote in ranked:
            if remaining <= 0 or len(legs) >= request.max_venues:
                break
            displayed = quote.displayed_quantity(request.side, request.style)
            if displayed <= 0:
                continue
            quantity = min(remaining, displayed)
            legs.append(_leg(quote, request, quantity, "sweep displayed quantity in price order"))
            remaining -= quantity
        return self._decision(
            route_id,
            request,
            feed,
            tuple(legs),
            "SWEEP allocated no more than displayed quantity at each observed venue",
        )


class LatencyAwareRouter(SmartOrderRouter):
    policy = RoutePolicy.LATENCY_AWARE

    def decide(self, route_id, request, feed):
        quotes = _eligible_quotes(feed, request)
        selected = min(quotes, key=lambda quote: _latency_key(quote, request))
        return self._decision(
            route_id,
            request,
            feed,
            (_leg(selected, request, request.quantity, "price, quote age, expected latency, and published fill prior"),),
            f"LATENCY_AWARE selected {selected.venue_id} from public simulator priors; it did not inspect hidden or future state",
        )


def router_for_policy(policy: RoutePolicy) -> SmartOrderRouter:
    routers = {
        RoutePolicy.DIRECT: DirectRouter,
        RoutePolicy.BEST_DISPLAYED_PRICE: BestDisplayedPriceRouter,
        RoutePolicy.LOWEST_EXPECTED_COST: LowestExpectedCostRouter,
        RoutePolicy.PASSIVE_QUEUE: PassiveQueueRouter,
        RoutePolicy.SWEEP: SweepRouter,
        RoutePolicy.LATENCY_AWARE: LatencyAwareRouter,
    }
    return routers[policy]()


def _quote(feed: ConsolidatedFeed, venue_id: str | None) -> VenueQuote:
    for quote in feed.quotes:
        if quote.venue_id == venue_id:
            return quote
    raise ValueError(f"route references unknown venue: {venue_id}")


def _eligible_quotes(
    feed: ConsolidatedFeed,
    request: RoutingRequest,
) -> tuple[VenueQuote, ...]:
    instruction = (
        OrderInstruction.MARKET
        if request.style is RouteStyle.AGGRESSIVE
        else OrderInstruction.LIMIT
    )
    quotes = tuple(
        quote
        for quote in feed.quotes
        if quote.session_state is SessionState.CONTINUOUS
        and instruction.value in quote.supported_instructions
        and quote.displayed_price(request.side, request.style) is not None
    )
    if not quotes:
        raise ValueError("no observable eligible venue for route")
    return quotes


def _leg(
    quote: VenueQuote,
    request: RoutingRequest,
    quantity: int,
    rationale: str,
) -> RouteLegPlan:
    price = request.limit_price_ticks
    if price is None:
        price = quote.displayed_price(request.side, request.style)
    return RouteLegPlan(
        quote.venue_id,
        quantity,
        price,
        quote.quote_age_us,
        rationale,
    )


def _price_key(quote: VenueQuote, request: RoutingRequest) -> tuple[int, str]:
    price = quote.displayed_price(request.side, request.style)
    if price is None:  # pragma: no cover - eligibility filters this
        raise RuntimeError("eligible quote lost its displayed price")
    return ((price if request.side is Side.BUY else -price), quote.venue_id)


def _expected_cost_key(quote: VenueQuote, request: RoutingRequest) -> tuple[int, str]:
    price_key, _ = _price_key(quote, request)
    fee = (
        quote.taker_fee_micros_per_share
        if request.style is RouteStyle.AGGRESSIVE
        else -quote.maker_rebate_micros_per_share
    )
    return price_key * quote.tick_value_micros + fee, quote.venue_id


def _passive_key(quote: VenueQuote, request: RoutingRequest) -> tuple[int, int, int, str]:
    price_key, _ = _price_key(quote, request)
    return (
        price_key,
        quote.displayed_quantity(request.side, request.style),
        -quote.maker_rebate_micros_per_share,
        quote.venue_id,
    )


def _latency_key(quote: VenueQuote, request: RoutingRequest) -> tuple[int, str]:
    price_key, _ = _price_key(quote, request)
    explicit_cost = (
        quote.taker_fee_micros_per_share
        if request.style is RouteStyle.AGGRESSIVE
        else -quote.maker_rebate_micros_per_share
    )
    score = (
        price_key * quote.tick_value_micros
        + explicit_cost
        + quote.expected_routing_latency_us // 100
        + quote.quote_age_us // 100
        + (10_000 - quote.expected_fill_probability_bps) * 20
    )
    return score, quote.venue_id
