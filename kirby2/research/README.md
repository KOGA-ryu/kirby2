# Immutable run ledger and research store

Kirby2 stores each completed identity in a content-addressed directory:

```text
STORE/
  catalog.duckdb                 # rebuildable query index and derived views
  catalog.lock                   # short catalog-refresh lock
  runs/
    run-<24 hex>/
      manifest.toml              # immutable identity, schemas, artifact digests
      configuration.toml         # complete session/replay configuration
      tables/
        events.parquet
        book_snapshots.parquet
        orders.parquet
        fills.parquet
        trades.parquet
        player_actions.parquet
        traffic_light_transitions.parquet
        strategy_states.parquet
        features.parquet
        latency_messages.parquet
        scores.parquet
        calibration_metrics.parquet
        experiment_membership.parquet
        data_provenance.parquet
```

The run directory is authoritative. `catalog.duckdb` is derived from manifests and
Parquet files and is rebuilt through a locked temporary database followed by an
atomic replacement. Queries do not mutate immutable run directories.

## Identity and immutability

`RunManifest` supports `SIMULATION`, `REPLAY`, `LESSON`, `CALIBRATION`, and
`EXPERIMENT` identities. A run ID is the first 24 hexadecimal characters of the
canonical identity digest, prefixed with `run-`. The identity includes run type,
parent, scenario/lesson, seed, flow model, market profile, strategy, layout,
objective, simulation bounds, software and Git versions, schema inventory, input
dataset references, configuration digest, evidence digest, and result digest. It
does not include filename order or creation time.

Re-recording the same completed identity returns the existing verified directory.
If that directory is missing an artifact, has a digest mismatch, or otherwise
fails verification, Kirby2 refuses to overwrite it. A different seed,
configuration, evidence table, software version, Git commit, or result gets a
different run ID.

Every configuration and Parquet artifact has a SHA-256 reference in the manifest.
The evidence digest also commits to the canonical logical rows independently of
Parquet byte encoding. Schema versions are explicit and unsupported versions fail
closed. No migration framework is implied by version 1.

## Replay chain

The current concrete writer captures `LiveMarketSession` training runs. Its TOML
configuration embeds the scenario definition, seed, duration, volume/liquidity,
flow-model identity, hotkey layout, strategy source when present, objective,
quantity choices, latency observation convention, completion state, and expected
state/timeline digests. `player_actions.parquet` stores the ordered input keys,
resolved commands, order parameters, observed market-state references, and action
latency. `book_snapshots.parquet` retains each referenced decision state plus the
final state.

`verify-run` checks:

- the manifest parses and its directory identity matches;
- every artifact exists and matches its SHA-256 and row count;
- all manifest, configuration, recording, and table schemas are supported;
- all logical Parquet rows reproduce the evidence digest and carry the same run ID;
- exchange event sequences are contiguous from 1;
- the embedded recording can be rebuilt without the old process;
- deterministic replay reproduces inputs, decision states, final state, timeline,
  and the manifest result digest.

The replay does not depend on `catalog.duckdb`. Deleting or moving only the catalog
does not remove run evidence; the next query rebuilds it from the immutable runs.

## Commands

After installing the project dependencies:

```bash
python3 -m kirby2 record-run \
  --store /tmp/kirby2-research \
  --scenario balanced --seed 42 --seconds 2 \
  --player-action 1000000:d

python3 -m kirby2 inspect-run RUN_ID --store /tmp/kirby2-research
python3 -m kirby2 query-runs --store /tmp/kirby2-research --scenario balanced
python3 -m kirby2 verify-run RUN_ID --store /tmp/kirby2-research
python3 -m kirby2 audit-run-store
```

`TIME_US:KEY` actions use simulation time and the embedded hotkey layout. If no
action is supplied, `record-run` submits one default market buy halfway through an
ACQUIRE training session.

## DuckDB views

The catalog exposes:

- `run_summary`
- `execution_summary`
- `strategy_summary`
- `scenario_summary`
- `historical_lesson_summary`
- `experiment_comparison`
- `invariant_violations`

It also exposes `all_<table>` views over every canonical Parquet table. Summary
views derive counts and totals rather than duplicating them into run artifacts.

For the Work Order 21 acceptance run, nothing is missing for exact replay. The
transport/exchange latency model is explicitly `NONE_WORK_ORDER_21`; observed UI
decision latency is preserved, while asynchronous latency messages remain an empty
versioned table until Work Order 23 introduces that subsystem. Synthetic scenario
provenance is stored as synthetic and makes no real-market claim.
