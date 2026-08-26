# Kirby2 traffic-light rules

Traffic-light files are declarative text. They cannot import modules, call functions,
or access simulator regime labels. Validate a file before launching the UI:

```bash
python3 -m kirby2 strategy strategies/momentum_long.k2
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
