# Kirby2 controlled strategy experiments

Run the included multi-scenario, multi-seed passive comparison:

```bash
python3 -m kirby2 experiment \
  experiment_manifests/momentum_rules_passive.json \
  --output .kirby2/experiments/momentum_rules_passive
```

The output directory is created as a new artifact and contains:

- `manifest.resolved.json`, with complete rule sources and their hashes.
- `result.json`, with per-seed, per-scenario, aggregate, traffic-agreement, and
  paired rule-delta measurements.
- one complete, independently replayable session recording per strategy,
  scenario, and seed under `recordings/`.

## Modes

`PASSIVE_OBSERVER` runs every script against the same seeded market path without
submitting player orders. The runner verifies the market digest remains identical
at every decision boundary. This is the cleanest way to compare classifications.

`FORKED_EXECUTION` warms each script to the configured fork time with no player
orders, verifies every copy has the identical exchange and flow digest, and only
then permits each strategy branch to execute independently. The deterministic
execution policy makes at most one market entry and one market exit: entry requires
a GREEN signal plus entry permission; exit requires a configured exit signal plus
exit permission. Post-fork books may diverge because the player actions are causal
interventions, not market-path noise.

## Interpretation

All comparisons are paired by scenario and seed. `mean_delta_b_minus_a` answers
what changed after changing the rule while holding the starting market path fixed.
Metrics include traffic-light agreement, entry/exit timing, trade count, spread
paid, implementation shortfall, adverse selection, completion, realized P&L in
tick-shares when flat, and discipline violations. Passive-only execution metrics
are explicitly `null` rather than fabricated.

Kirby2 never selects an automatic winner. A one-scenario manifest is explicitly
marked insufficient for a winner claim; multi-scenario results remain descriptive.
