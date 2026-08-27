# Synthetic Market Agent Ecology

This package is a deterministic execution-training environment. It has no live
exchange adapter, network message emitter, or real-market order export path.

Every `MarketAgent` receives one immutable observation containing aggregated
displayed depth, public trades, session or auction state, and its own inventory,
budget, and working orders. The interface returns only ordinary limit, market, or
cancel intents. Agents never receive an exchange engine, queue object, other actor
identity, future event, hidden-liquidity ledger, or ground-truth log. The exchange
gateway alone assigns neutral order IDs and applies matching, auction, halt,
self-trade-prevention, and session rules.

Hard bounds cover accepted quantity, absolute inventory, working quantity, order
size, rolling order rate, price distance, information set, owned latency, and
simulation-time lifetime. Each agent owns a seed derived deterministically from the
run seed and stable agent identity.

The baseline families are noise trader, passive market maker,
inventory-sensitive market maker, momentum trader, mean-reversion trader,
scheduled metaorder, distressed liquidator, liquidity withdrawer, controlled
latent-value trader, and auction participant. Population definitions compose these
families without directly assigning prices or queues; descriptive regime labels do
not affect matching.

`DECEPTIVE_DISPLAY` exists only in named Kirby2 recognition drills. Generic
population composition rejects it. Its internal behavior parameters are redacted
from population manifests and replay records, and the player exercise teaches only
that displayed liquidity can be unreliable and execution plans should manage that
risk. The package does not expose it as a configurable real-market tactic.

During a run, the player record contains aggregated book snapshots, trades,
auction indications, and session transitions without actor identity or intent.
The causal actor ledger is withheld until completion, when the post-session report
may attribute the observed synthetic behavior. Canonical population replay records
store a population ID and definition digest, not portable recognition-agent
parameters.

Run a population:

```console
python3 -m kirby2 agent-ecology --population momentum_ecology --seed 42
```

Run a defensive recognition drill:

```console
python3 -m kirby2 agent-ecology --drill halt_disorderly_reopen --seed 42
```

Run the runtime acceptance audit:

```console
python3 -m kirby2 audit-agent-ecology
```
