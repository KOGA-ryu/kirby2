# Kirby2 Strategy Discovery Evidence

This file records the Git-visible identifiers and acceptance result for the one-shot
WO35-F1 controlled strategy-discovery experiment. The complete immutable lineage,
partition-access history, reveal receipt, and run evidence remain in the governed
evidence store at `.kirby2/discovery/controlled`.

## Frozen execution

- Implementation commit: `174a0f7679ecc5df83e9ced835e13e6ce1e4c9be`
- Implementation commit subject: `Implement strategy discovery lineage`
- Experiment: `bounded-search-controlled-v1`
- Discovery ID: `discovery-4a4bc31fbc3e6af11d8beb30`
- Experiment manifest SHA-256: `c60c13dfa617b1e4eaf4c5464f5456ad03ebb04c529e2d201d2ebe1ecfa59ca0`
- Partition manifest SHA-256: `163ec7759aad83ffa46089abaa2fcc6d18117bcf8074e2c5be07caf5a74a39c9`
- Robustness policy SHA-256: `bef8932ca1eaa2034f2aa2b9f3bfe8ce1b41d058233ed65fe730972185daf892`
- Base strategy source SHA-256: `1f0a39b847093703e58061e396fc80beb02509e3d3b128c3c8c5a22fd51d1df3`
- Final ledger SHA-256: `c739bee99e37d783e76f7cc08507341f9a720c97472c3ed772b45432f823d5fb`

## Controlled result

- Execution status: `PASS`
- Terminal scientific outcome: `CONFIRMED_WITHIN_DECLARED_SCOPE`
- Selected candidate semantic SHA-256: `f1c0acfd43a32a68fcfa9f8eb0e242143dca57acdf7fd09b2b4081e37146c436`
- Training-star semantic SHA-256: `4bce4ae9f093036e749871e1824c7f2a24986b4d47f884496413d17815c3ad7f`
- The training star was rejected during validation with the preregistered
  `TRAIN_VALIDATION_DIVERGENCE` label and was not executed on robustness, holdout,
  or adversarial partitions.
- The distinct selected candidate qualified on validation, the frozen 64-cell
  robustness execution, holdout, and adversarial evaluation after the atomic
  single-use reveal.
- Verification passed for binding identity, canonical bytes, file inventory, the
  append-only record chain, and single-use reveal semantics.

## Required verification

The required commands completed against the immutable evidence:

```text
strategy-discovery-demo: PASS, failures=0
audit-expansion --gate WO35-F1: PASS, checks=1, failures=0, warnings=0
audit-model-risk-lab: PASS, cases=21, failures=0
```

The WO35-F1 validator reports `VERIFY_ONLY_NEVER_RERUN`; later checks validate this
same evidence rather than executing the controlled search again. No sealed partition
contents or reveal token plaintext are copied into Git.

## Scope limitation

`CONFIRMED_WITHIN_DECLARED_SCOPE` applies only to the named, committed Kirby2
simulator fixture, partitions, metrics, thresholds, and robustness policy. It is not
evidence or a claim of live-market profitability, deployability, attainable real-world
fills, or external validity, and it does not authorize live trading.
