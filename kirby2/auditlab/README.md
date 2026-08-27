# Kirby2 model-risk laboratory

The laboratory attacks simulator correctness through runtime invariants, exact
replay, generated scenario coverage, explicit fault injection, deterministic
failure reduction, statistical holdouts, and immutable evidence. It is not a
conventional unit-test framework.

Every configuration declares fourteen axes: seed, flow model, regime, volume,
liquidity, latency, session phase, order type family, hidden-liquidity mode,
venue count, auction state, participant population, strategy, and objective.
The fast kernel executes every case against ordinary `OrderBook` instances.
Targeted probes additionally exercise the actual auction, asynchronous latency,
hidden-liquidity, multi-venue, agent-ecology, Hawkes, market-data, and causal
branch implementations.

The ten injected faults are never hidden in random noise. Each has a manifest,
injection point, detector, expected code, detected code, and evidence. Detected
injected faults are expected adversarial observations; detector misses and
structural invariant failures are unexpected violations.

Each stable violation signature is reduced by rerunning candidates while
shrinking duration, event count, participant count, venue count, and remaining
configuration complexity. A reduction is accepted only if the same signature
survives.

Run the requested substantial audit:

```text
python3 -m kirby2 audit-lab --budget 10000 --seed 771 --save-failures
```

Packets are content-addressed beneath `.kirby2/research/audit_lab/packets` and
indexed by an append-only ledger. Passing automated gates creates a separate
immutable acceptance record whose decision remains `PENDING_HUMAN_REVIEW`.
`AuditLabStore.record_acceptance` can append a human decision that names this
record in `supersedes_record_id`; earlier records are never rewritten.
