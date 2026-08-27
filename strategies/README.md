# Kirby2 traffic-light rules

Traffic-light files are declarative text. They cannot import modules, call functions,
or access simulator regime labels. Validate a file before launching the UI:

```bash
python3 -m kirby2 strategy strategies/momentum_long.k2
python3 -m kirby2 strategy strategies/momentum_stateful.k2
```

Launch and record a session with the rule file embedded in its replay artifact:

```bash
python3 -m kirby2 ui \
  --scenario balanced \
  --strategy strategies/momentum_long.k2 \
  --record .kirby2/recordings/momentum-session.json
```

## Grammar

```text
setup setup_name
window 5s

GREEN when
    observable_feature > 1.0
    another_feature <= 3

WAIT when
    observable_feature > 0

RED otherwise
```

Conditions inside a block are combined with AND. GREEN is evaluated first, then
WAIT, with RED as the fallback. The rolling window defaults to 5 seconds and may
be written as integer seconds (`5s`) or milliseconds (`500ms`). An unavailable
feature does not satisfy a condition. Comments begin with `#`.

## Observable vocabulary

- `spread_ticks`: best ask minus best bid.
- `best_bid_size`, `best_ask_size`: displayed quantity at the touch.
- `book_imbalance`: `(best_bid_size - best_ask_size) / total_touch_size`.
- `aggressive_buy_volume`, `aggressive_sell_volume`: tape volume by taker side
  inside the rolling window.
- `buy_sell_ratio`: `(aggressive_buy_volume + 1) / (aggressive_sell_volume + 1)`.
- `trade_velocity`: tape prints per second inside the rolling window.
- `bid_depletion_rate`, `ask_depletion_rate`: executed quantity per second against
  each side.
- `bid_replenishment_rate`, `ask_replenishment_rate`: displayed limit quantity
  added per second to each side.
- `bid_cancel_rate`, `ask_cancel_rate`: displayed quantity cancelled per second.
- `relative_volume`: configured observable relative-volume multiplier.
- `short_term_price_change`: midpoint change in ticks across the rolling window.
- `microprice`: touch-size-weighted price in ticks.

All rates use simulation time rather than wall-clock time. Traffic transitions and
their complete feature and condition explanations are available through:

```bash
python3 -m kirby2 timeline .kirby2/recordings/momentum-session.json --kind TRAFFIC
```

## Stateful grammar

The stateful form keeps the same restricted observable vocabulary and adds named
states, deterministic transitions, entry/exit permissions, position predicates,
and simulation-time qualifiers. It cannot execute Python or inspect hidden regime
labels.

```text
machine setup_name
window 1s
initial IDLE

state IDLE signal WAIT entry DENY exit ALLOW
state ARMED signal WAIT entry DENY exit ALLOW
state GREEN signal GREEN entry ALLOW exit ALLOW
state COOLDOWN signal RED entry DENY exit ALLOW cooldown 2s

transition IDLE -> ARMED when for 500ms
    book_imbalance > 0.50

transition ARMED -> GREEN when events 3 within 1s
    ask_depletion_rate > 0

transition GREEN -> COOLDOWN after entry

transition COOLDOWN -> IDLE when
    position == 0
```

Transitions are considered in source order. Supported qualifiers are an immediate
`when`, continuously true `when for TIME`, `when events N within TIME`,
`when occurred within TIME`, and `after entry`. Cooldown time is measured by the
simulation clock. Event counts refer to matching exchange-driven evaluations.
`after entry` means an accepted order with position-increasing or reversing intent,
not an exit-only order. Position-aware conditions are `position`,
`bought_quantity`, `sold_quantity`, and `working_order_count`. A reversal requires
both exit and entry permission. A denied permission rejects the player action
before it can change exchange state; cancellations remain available. Session
reports and `timeline --kind TRAFFIC` include the transition reason, qualifier,
condition evidence, and permissions.

## Simulation-time boundary order

Strategy time advances independently of exchange activity. At a timestamp shared by
market activity and a strategy deadline, Kirby2 applies scheduled market activity,
emits and consumes its exchange events, updates rolling features, and only then
evaluates the state machine. Zero-time transitions are settled in source order under
a deterministic cycle bound. The originating exchange batch is consumed only once.

`when for TIME` transitions occur at their exact simulation-time deadline, including
during a quiet market. Cooldowns are reevaluated at exact expiry. Event and
occurrence windows use inclusive endpoints, so evidence recorded at time `T` expires
at `T + window + 1` microsecond. Feature-window and timer deadlines are semantic
boundaries; arbitrary UI update chunking does not add canonical strategy records or
change replay results. Every causal evaluation is recorded as
`STRATEGY_EVALUATION`, while actual state changes remain `TRAFFIC` timeline records.
