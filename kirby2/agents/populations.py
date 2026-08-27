"""Composable participant populations and bounded adversarial recognition drills."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from kirby2.exchange import SessionState, Side

from .models import (
    AgentBounds,
    AgentFamily,
    AgentInformationSet,
    AgentPolicyParameters,
    AgentSafetyClass,
    AgentSpec,
    EcologyTransition,
    PopulationDefinition,
)


POPULATION_IDS = (
    "liquidity_provision",
    "momentum_ecology",
    "liquidation_ecology",
)

ADVERSARIAL_DRILL_IDS = (
    "liquidity_mirage",
    "repeated_wall_withdrawal",
    "absorption_hidden_reserve",
    "momentum_ignition_exhaustion",
    "distressed_liquidation",
    "stop_like_cascade",
    "auction_imbalance_reversal",
    "halt_disorderly_reopen",
)

BOUNDED_POPULATION_TEMPLATES: Mapping[str, tuple[AgentFamily, ...]] = {
    "liquidity_provision": (
        AgentFamily.NOISE_TRADER,
        AgentFamily.PASSIVE_MARKET_MAKER,
        AgentFamily.LIQUIDITY_WITHDRAWER,
        AgentFamily.SCHEDULED_METAORDER,
    ),
    "momentum_ecology": (
        AgentFamily.MOMENTUM_TRADER,
        AgentFamily.INVENTORY_SENSITIVE_MARKET_MAKER,
        AgentFamily.NOISE_TRADER,
        AgentFamily.MEAN_REVERSION_TRADER,
    ),
    "liquidation_ecology": (
        AgentFamily.DISTRESSED_LIQUIDATOR,
        AgentFamily.LIQUIDITY_WITHDRAWER,
        AgentFamily.MEAN_REVERSION_TRADER,
        AgentFamily.NOISE_TRADER,
    ),
}


def _bounds(
    duration_us: int,
    *,
    start_us: int = 0,
    end_us: int | None = None,
    budget: int = 1_600,
    inventory: int | None = None,
    working: int = 400,
    max_order: int = 200,
    rate: int = 5,
    latency_us: int = 2_000,
    interval_us: int = 250_000,
    information_set: AgentInformationSet = (
        AgentInformationSet.PUBLIC_MARKET_AND_OWN_STATE
    ),
) -> AgentBounds:
    return AgentBounds(
        quantity_budget=budget,
        max_abs_inventory=budget if inventory is None else inventory,
        max_working_quantity=working,
        max_orders_per_second=rate,
        max_order_quantity=max_order,
        max_price_distance_ticks=8,
        latency_us=latency_us,
        lifetime_start_us=start_us,
        lifetime_end_us=duration_us if end_us is None else end_us,
        decision_interval_us=interval_us,
        information_set=information_set,
    )


def _spec(
    agent_id: str,
    family: AgentFamily,
    duration_us: int,
    *,
    side: Side | None = None,
    start_us: int = 0,
    end_us: int | None = None,
    activation_us: int | None = None,
    withdrawal_us: int | None = None,
    clip: int = 100,
    budget: int = 1_600,
    working: int = 400,
    max_order: int = 200,
    rate: int = 5,
    latency_us: int = 2_000,
    interval_us: int = 250_000,
    quote_offset: int = 0,
    latent_value: int | None = None,
    reserve_price: int | None = None,
    repeat_display: bool = False,
    safety: AgentSafetyClass = AgentSafetyClass.STANDARD_SYNTHETIC,
) -> AgentSpec:
    information = (
        AgentInformationSet.CONTROLLED_LATENT_VALUE
        if family is AgentFamily.LATENT_VALUE_TRADER
        else AgentInformationSet.PUBLIC_MARKET_AND_OWN_STATE
    )
    return AgentSpec(
        agent_id,
        family,
        _bounds(
            duration_us,
            start_us=start_us,
            end_us=end_us,
            budget=budget,
            working=working,
            max_order=max_order,
            rate=rate,
            latency_us=latency_us,
            interval_us=interval_us,
            information_set=information,
        ),
        AgentPolicyParameters(
            clip_quantity=clip,
            preferred_side=side,
            activation_time_us=(start_us if activation_us is None else activation_us),
            withdrawal_time_us=withdrawal_us,
            latent_value_ticks=latent_value,
            quote_offset_ticks=quote_offset,
            reserve_price_ticks=reserve_price,
            auction_only=(family is AgentFamily.AUCTION_PARTICIPANT),
            repeat_display=repeat_display,
        ),
        safety,
    )


def compose_population(
    population_id: str,
    counts: Mapping[AgentFamily, int],
    *,
    duration_us: int = 4_000_000,
    description: str = "User-composed bounded synthetic participant population.",
    max_orders_per_second: int = 5,
    latency_us: int = 2_000,
    decision_interval_us: int = 250_000,
) -> PopulationDefinition:
    """Compose safe synthetic families; recognition-only agents use named drills."""

    if not counts:
        raise ValueError("population composition must contain at least one family")
    if AgentFamily.DECEPTIVE_DISPLAY in counts:
        raise ValueError(
            "DECEPTIVE_DISPLAY is unavailable to generic composition; use a canonical "
            "recognition drill"
        )
    agents: list[AgentSpec] = []
    for family in sorted(counts, key=lambda item: item.value):
        count = counts[family]
        if type(count) is not int or count < 0:
            raise ValueError("population family counts must be nonnegative integers")
        for index in range(1, count + 1):
            side = None
            safety = AgentSafetyClass.STANDARD_SYNTHETIC
            latent = None
            if family is AgentFamily.SCHEDULED_METAORDER:
                side = Side.BUY
            elif family is AgentFamily.DISTRESSED_LIQUIDATOR:
                side = Side.SELL
            elif family is AgentFamily.LATENT_VALUE_TRADER:
                latent = 10_000 + (1 if index % 2 else -1)
                safety = AgentSafetyClass.CONTROLLED_LATENT_INFORMATION
            agents.append(
                _spec(
                    f"{family.value}-{index:02d}",
                    family,
                    duration_us,
                    side=side,
                    withdrawal_us=(
                        duration_us // 2
                        if family is AgentFamily.LIQUIDITY_WITHDRAWER
                        else None
                    ),
                    latent_value=latent,
                    rate=max_orders_per_second,
                    latency_us=latency_us,
                    interval_us=decision_interval_us,
                    safety=safety,
                )
            )
    if not agents:
        raise ValueError("population composition resolved to zero agents")
    return PopulationDefinition(
        population_id,
        description,
        tuple(agents),
        duration_us,
    )


def compose_bounded_population(
    population_id: str,
    agent_count: int,
    *,
    duration_us: int,
) -> PopulationDefinition:
    """Cycle a named safe template into exactly one through eight agents."""

    try:
        template = BOUNDED_POPULATION_TEMPLATES[population_id]
    except KeyError as error:
        available = ", ".join(sorted(BOUNDED_POPULATION_TEMPLATES))
        raise ValueError(
            f"unknown bounded population {population_id!r}; available: {available}"
        ) from error
    if type(agent_count) is not int or not 1 <= agent_count <= 8:
        raise ValueError("bounded population agent count must be from one through eight")
    if type(duration_us) is not int or duration_us <= 0:
        raise ValueError("bounded population duration must be positive simulation time")
    families = tuple(template[index % len(template)] for index in range(agent_count))
    decision_interval_us = max(1, duration_us // 5)
    return compose_population(
        population_id,
        Counter(families),
        duration_us=duration_us,
        description=(
            "Deterministically cycled bounded audit population with exactly "
            f"{agent_count} agents."
        ),
        max_orders_per_second=(
            1_000_000 + decision_interval_us - 1
        )
        // decision_interval_us,
        latency_us=min(2_000, max(0, duration_us // 10)),
        decision_interval_us=decision_interval_us,
    )


def get_population(population_id: str) -> PopulationDefinition:
    builders = {
        "liquidity_provision": _liquidity_provision,
        "momentum_ecology": _momentum_ecology,
        "liquidation_ecology": _liquidation_ecology,
        **{name: (lambda value=name: _adversarial_drill(value)) for name in ADVERSARIAL_DRILL_IDS},
    }
    try:
        return builders[population_id]()
    except KeyError as error:
        available = ", ".join((*POPULATION_IDS, *ADVERSARIAL_DRILL_IDS))
        raise ValueError(f"unknown agent population {population_id!r}; available: {available}") from error


def get_adversarial_drill(drill_id: str) -> PopulationDefinition:
    if drill_id not in ADVERSARIAL_DRILL_IDS:
        raise ValueError(f"unknown adversarial drill: {drill_id}")
    return _adversarial_drill(drill_id)


def _liquidity_provision() -> PopulationDefinition:
    duration = 4_000_000
    agents = [
        *(
            _spec(f"NOISE-{index:02d}", AgentFamily.NOISE_TRADER, duration)
            for index in range(1, 9)
        ),
        *(
            _spec(
                f"PASSIVE-MM-{index:02d}",
                AgentFamily.PASSIVE_MARKET_MAKER,
                duration,
                latency_us=1_000 + index * 250,
            )
            for index in range(1, 4)
        ),
        _spec(
            "SCHEDULED-BUYER-01",
            AgentFamily.SCHEDULED_METAORDER,
            duration,
            side=Side.BUY,
            activation_us=500_000,
            clip=100,
            budget=800,
        ),
        _spec(
            "WITHDRAWER-01",
            AgentFamily.LIQUIDITY_WITHDRAWER,
            duration,
            withdrawal_us=2_500_000,
            clip=100,
            budget=600,
        ),
    ]
    return PopulationDefinition(
        "liquidity_provision",
        "Eight noise agents, three passive makers, one scheduled buyer, and one bounded withdrawer.",
        tuple(agents),
        duration,
        descriptive_regime_label="mixed_liquidity_provision",
    )


def _momentum_ecology() -> PopulationDefinition:
    duration = 4_000_000
    agents = [
        *(
            _spec(f"NOISE-{index:02d}", AgentFamily.NOISE_TRADER, duration)
            for index in range(1, 4)
        ),
        _spec(
            "INVENTORY-MM-01",
            AgentFamily.INVENTORY_SENSITIVE_MARKET_MAKER,
            duration,
            clip=100,
            budget=1_200,
        ),
        *(
            _spec(
                f"MOMENTUM-{index:02d}",
                AgentFamily.MOMENTUM_TRADER,
                duration,
                clip=100,
                budget=1_200,
                latency_us=1_000 + index * 300,
            )
            for index in range(1, 4)
        ),
        _spec(
            "SCHEDULED-BUYER-01",
            AgentFamily.SCHEDULED_METAORDER,
            duration,
            side=Side.BUY,
            activation_us=250_000,
            clip=150,
            budget=1_800,
            max_order=200,
        ),
        _spec(
            "WITHDRAWER-01",
            AgentFamily.LIQUIDITY_WITHDRAWER,
            duration,
            withdrawal_us=1_500_000,
            clip=100,
            budget=400,
        ),
    ]
    return PopulationDefinition(
        "momentum_ecology",
        "Scheduled buying interacts with public-momentum responders and thinning liquidity.",
        tuple(agents),
        duration,
        descriptive_regime_label="emergent_buy_momentum",
    )


def _liquidation_ecology() -> PopulationDefinition:
    duration = 4_000_000
    agents = [
        *(
            _spec(f"NOISE-{index:02d}", AgentFamily.NOISE_TRADER, duration)
            for index in range(1, 4)
        ),
        _spec(
            "INVENTORY-MM-01",
            AgentFamily.INVENTORY_SENSITIVE_MARKET_MAKER,
            duration,
            clip=100,
            budget=1_000,
        ),
        *(
            _spec(
                f"MEAN-REVERSION-{index:02d}",
                AgentFamily.MEAN_REVERSION_TRADER,
                duration,
                start_us=1_000_000,
                clip=100,
                budget=600,
            )
            for index in range(1, 3)
        ),
        _spec(
            "DISTRESSED-SELLER-01",
            AgentFamily.DISTRESSED_LIQUIDATOR,
            duration,
            side=Side.SELL,
            activation_us=250_000,
            clip=150,
            budget=2_000,
            max_order=300,
        ),
        _spec(
            "LATENT-BUYER-01",
            AgentFamily.LATENT_VALUE_TRADER,
            duration,
            start_us=1_500_000,
            clip=100,
            budget=1_000,
            latent_value=9_999,
            safety=AgentSafetyClass.CONTROLLED_LATENT_INFORMATION,
        ),
    ]
    return PopulationDefinition(
        "liquidation_ecology",
        "A bounded distressed seller meets inventory-sensitive and controlled-value demand.",
        tuple(agents),
        duration,
        descriptive_regime_label="emergent_sell_pressure",
    )


def _adversarial_drill(drill_id: str) -> PopulationDefinition:
    builders = {
        "liquidity_mirage": _liquidity_mirage,
        "repeated_wall_withdrawal": _repeated_wall_withdrawal,
        "absorption_hidden_reserve": _absorption_hidden_reserve,
        "momentum_ignition_exhaustion": _momentum_ignition_exhaustion,
        "distressed_liquidation": _distressed_liquidation_drill,
        "stop_like_cascade": _stop_like_cascade,
        "auction_imbalance_reversal": _auction_imbalance_reversal,
        "halt_disorderly_reopen": _halt_disorderly_reopen,
    }
    return builders[drill_id]()


def _drill(
    drill_id: str,
    description: str,
    agents: tuple[AgentSpec, ...],
    explanation: str,
    *,
    duration: int = 4_000_000,
    start_state: SessionState = SessionState.CONTINUOUS,
    transitions: tuple[EcologyTransition, ...] = (),
) -> PopulationDefinition:
    return PopulationDefinition(
        drill_id,
        description,
        agents,
        duration,
        descriptive_regime_label=f"recognition_drill_{drill_id}",
        recognition_drill=True,
        post_session_explanation=explanation,
        start_state=start_state,
        transitions=transitions,
    )


def _drill_noise(duration: int, count: int = 2) -> tuple[AgentSpec, ...]:
    return tuple(
        _spec(f"NOISE-{index:02d}", AgentFamily.NOISE_TRADER, duration, budget=600)
        for index in range(1, count + 1)
    )


def _liquidity_mirage() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        *_drill_noise(duration),
        _spec(
            "RECOGNITION-DISPLAY-01",
            AgentFamily.DECEPTIVE_DISPLAY,
            duration,
            side=Side.SELL,
            withdrawal_us=1_250_000,
            clip=400,
            budget=800,
            working=500,
            max_order=500,
            repeat_display=False,
            safety=AgentSafetyClass.RECOGNITION_DRILL_ONLY,
        ),
        _spec(
            "SCHEDULED-BUYER-01",
            AgentFamily.SCHEDULED_METAORDER,
            duration,
            side=Side.BUY,
            activation_us=1_000_000,
            clip=100,
            budget=900,
        ),
    )
    return _drill(
        "liquidity_mirage",
        "Recognition exercise with displayed liquidity that proves unreliable before execution.",
        agents,
        "The large displayed offer came from a simulator-only recognition actor and was withdrawn; execution plans should discount display reliability.",
    )


def _repeated_wall_withdrawal() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        *_drill_noise(duration, 3),
        _spec(
            "RECOGNITION-DISPLAY-01",
            AgentFamily.DECEPTIVE_DISPLAY,
            duration,
            side=Side.BUY,
            clip=300,
            budget=1_500,
            working=400,
            max_order=400,
            repeat_display=True,
            safety=AgentSafetyClass.RECOGNITION_DRILL_ONLY,
        ),
    )
    return _drill(
        "repeated_wall_withdrawal",
        "Recognition exercise with repeated large-display appearance and withdrawal.",
        agents,
        "Post-session truth identifies repeated recognition-only display withdrawals; the player feed intentionally withheld actor identity and intent.",
    )


def _absorption_hidden_reserve() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        *_drill_noise(duration),
        _spec(
            "RESERVE-BUYER-01",
            AgentFamily.PASSIVE_MARKET_MAKER,
            duration,
            side=Side.BUY,
            reserve_price=9_999,
            clip=100,
            budget=1_600,
            working=200,
        ),
        _spec(
            "SCHEDULED-SELLER-01",
            AgentFamily.SCHEDULED_METAORDER,
            duration,
            side=Side.SELL,
            activation_us=500_000,
            clip=100,
            budget=1_200,
        ),
    )
    return _drill(
        "absorption_hidden_reserve",
        "Repeated causal replenishment makes a private reserve visible only through public outcomes.",
        agents,
        "A bounded passive buyer replenished one displayed slice at a time; reserve identity and remaining budget were available only in post-session truth.",
    )


def _momentum_ignition_exhaustion() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        *_drill_noise(duration),
        _spec(
            "SCHEDULED-BUYER-01",
            AgentFamily.SCHEDULED_METAORDER,
            duration,
            side=Side.BUY,
            end_us=1_750_000,
            clip=150,
            budget=1_200,
            max_order=200,
        ),
        *(
            _spec(
                f"MOMENTUM-{index:02d}",
                AgentFamily.MOMENTUM_TRADER,
                duration,
                end_us=2_000_000,
                clip=100,
                budget=800,
            )
            for index in range(1, 3)
        ),
        _spec(
            "MEAN-REVERSION-01",
            AgentFamily.MEAN_REVERSION_TRADER,
            duration,
            start_us=1_500_000,
            clip=100,
            budget=900,
        ),
        _spec(
            "LATENT-SELLER-01",
            AgentFamily.LATENT_VALUE_TRADER,
            duration,
            start_us=1_750_000,
            side=Side.SELL,
            latent_value=10_000,
            clip=100,
            budget=900,
            safety=AgentSafetyClass.CONTROLLED_LATENT_INFORMATION,
        ),
    )
    return _drill(
        "momentum_ignition_exhaustion",
        "A bounded initial buyer and public-momentum responders give way to opposing demand.",
        agents,
        "The early move emerged from scheduled and reactive orders; exhaustion followed when their bounded lifetimes ended and opposing actors remained.",
    )


def _distressed_liquidation_drill() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        *_drill_noise(duration),
        _spec(
            "DISTRESSED-SELLER-01",
            AgentFamily.DISTRESSED_LIQUIDATOR,
            duration,
            side=Side.SELL,
            activation_us=250_000,
            clip=200,
            budget=2_000,
            max_order=400,
        ),
        _spec(
            "INVENTORY-MM-01",
            AgentFamily.INVENTORY_SENSITIVE_MARKET_MAKER,
            duration,
            clip=100,
            budget=1_200,
        ),
    )
    return _drill(
        "distressed_liquidation",
        "A quantity-bounded urgent seller consumes displayed bids through normal matching.",
        agents,
        "The sell pressure was generated by a bounded distressed-liquidator actor; no price was directly assigned by the scenario.",
    )


def _stop_like_cascade() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        *_drill_noise(duration),
        _spec(
            "INITIAL-SELLER-01",
            AgentFamily.SCHEDULED_METAORDER,
            duration,
            side=Side.SELL,
            clip=200,
            budget=800,
            end_us=1_250_000,
        ),
        *(
            _spec(
                f"MOMENTUM-{index:02d}",
                AgentFamily.MOMENTUM_TRADER,
                duration,
                clip=100,
                budget=1_000,
            )
            for index in range(1, 4)
        ),
        _spec(
            "WITHDRAWER-01",
            AgentFamily.LIQUIDITY_WITHDRAWER,
            duration,
            withdrawal_us=750_000,
            clip=100,
            budget=400,
        ),
    )
    return _drill(
        "stop_like_cascade",
        "Public-momentum responses and liquidity withdrawal create a stop-like synthetic cascade.",
        agents,
        "The cascade is a descriptive outcome of bounded public-signal agents and thinning queues, not evidence of actual stop orders or hidden real-market intent.",
    )


def _auction_imbalance_reversal() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        _spec(
            "AUCTION-BUYER-01",
            AgentFamily.AUCTION_PARTICIPANT,
            duration,
            side=Side.BUY,
            clip=400,
            budget=400,
            working=500,
            max_order=400,
            end_us=1_500_000,
        ),
        _spec(
            "AUCTION-SELLER-01",
            AgentFamily.AUCTION_PARTICIPANT,
            duration,
            side=Side.SELL,
            clip=150,
            budget=150,
            working=200,
            max_order=150,
            end_us=1_500_000,
        ),
        _spec(
            "LATENT-SELLER-01",
            AgentFamily.LATENT_VALUE_TRADER,
            duration,
            start_us=1_500_001,
            latent_value=9_998,
            clip=150,
            budget=1_200,
            safety=AgentSafetyClass.CONTROLLED_LATENT_INFORMATION,
        ),
        *_drill_noise(duration),
    )
    return _drill(
        "auction_imbalance_reversal",
        "A genuine synthetic opening auction imbalance is followed by opposing continuous flow.",
        agents,
        "The opening imbalance came from bounded auction participants; the reversal emerged after the uncross from new ordinary continuous orders.",
        start_state=SessionState.PREOPEN,
        transitions=(
            EcologyTransition(1_500_000, SessionState.OPENING_AUCTION),
            EcologyTransition(1_500_001, SessionState.CONTINUOUS, uncross_before=True),
        ),
    )


def _halt_disorderly_reopen() -> PopulationDefinition:
    duration = 4_000_000
    agents = (
        _spec(
            "DISTRESSED-SELLER-01",
            AgentFamily.DISTRESSED_LIQUIDATOR,
            duration,
            side=Side.SELL,
            clip=150,
            budget=1_800,
            max_order=300,
        ),
        _spec(
            "WITHDRAWER-01",
            AgentFamily.LIQUIDITY_WITHDRAWER,
            duration,
            withdrawal_us=750_000,
            clip=100,
            budget=400,
        ),
        _spec(
            "REOPEN-BUYER-01",
            AgentFamily.AUCTION_PARTICIPANT,
            duration,
            side=Side.BUY,
            start_us=2_000_000,
            end_us=2_500_000,
            clip=250,
            budget=250,
            working=300,
            max_order=250,
        ),
        _spec(
            "REOPEN-SELLER-01",
            AgentFamily.AUCTION_PARTICIPANT,
            duration,
            side=Side.SELL,
            start_us=2_000_000,
            end_us=2_500_000,
            clip=400,
            budget=400,
            working=500,
            max_order=400,
        ),
        *_drill_noise(duration),
    )
    return _drill(
        "halt_disorderly_reopen",
        "A halt and genuine synthetic reopening auction expose depleted and imbalanced liquidity.",
        agents,
        "The halt blocked continuous submissions; the reopening outcome came from bounded auction orders and the post-reopen book, with no direct price mutation.",
        transitions=(
            EcologyTransition(1_000_000, SessionState.HALTED),
            EcologyTransition(2_000_000, SessionState.REOPENING_AUCTION),
            EcologyTransition(2_500_000, SessionState.CONTINUOUS, uncross_before=True),
        ),
    )
