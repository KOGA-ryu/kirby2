# Kirby2 system map

Status: source router, not architecture authority  
Source snapshot: `4a328723ffe8881f2358f1a72576f8ed905f2687`

## Product and truth boundary

Kirby2 is a deterministic mathematical market-execution training sandbox. It is not
a broker, live-market connector, investment adviser, profitability claim, or proof
that a reconstruction matches historical market state. Prices, quantities, event
sequences, and simulation clocks use exact semantic representations; wall time and
operational attempts do not enter scientific identity.

## Execution spine

```text
kirby2.__main__
  -> legacy parser/dispatch OR cli.expansion + cli.registry
  -> domain command module
  -> versioned plan/configuration and explicit seed ownership
  -> simulation + exchange + session/full_day composition
  -> immutable run/artifact stores under research.paths.DataPaths
  -> replay/microscope/audit projections
  -> packs/orchestration/release transport and qualification
```

Dependency direction is toward named lower-level contracts. Persistence, audit, UI,
packs, orchestration, and release may consume the simulation/exchange domains; the
exchange core must not depend on those outer layers.

## Package neighborhoods

| Neighborhood | Owns | Start here |
| --- | --- | --- |
| CLI routing | Legacy entry point plus explicit command modules | `kirby2/__main__.py`, `kirby2/cli/expansion.py`, `kirby2/cli/registry.py` |
| Synthetic kernel | Clock/RNG/distributions/flow, FIFO book, advanced mechanics | `kirby2/simulation/`, `kirby2/exchange/book.py`, `kirby2/exchange/mechanics_engine.py` |
| Interactive execution | Journal, live session, strategy/features, position, terminal | `kirby2/session/`, `kirby2/strategy/`, `kirby2/features/`, `kirby2/ui/terminal.py` |
| Scenario and day composition | Accepted scenario construction and full-day scheduler/checkpoints/store | `kirby2/scenarios/market.py`, `kirby2/full_day/runtime.py`, `kirby2/full_day/store.py` |
| Evidence and analysis | Governed paths, immutable ledger, DuckDB tables, microscope, audits | `kirby2/research/`, `kirby2/microscope/`, `kirby2/audit/`, `kirby2/auditlab/` |
| Data/provenance | Local market-data normalization, historical labeling, calibration | `kirby2/marketdata/`, `kirby2/historical/`, `kirby2/calibration/` |
| Training expansion | Latency, venues, algorithms, counterfactuals, agents, curriculum/mining/discovery/instructor | Matching top-level packages; route through `PACKAGE_MAP.tsv` |
| Portability/distribution | Data-only packs, deterministic work orchestration, offline release | `kirby2/packs/`, `kirby2/orchestration/`, `kirby2/release/` |

The largest trees at this snapshot are `audit` (~59k lines), `full_day` (~43k),
`instructor` (~22.5k), `microscope` (~21k), `packs` (~18.8k), `orchestration`
(~17.4k), and `release` (~14.5k). Never read one of those packages linearly to
orient a task; select a flow below.

## Primary flows

### Synthetic scenario

```text
accepted_scenarios.json
  -> scenarios.market.get_scenario_definition
  -> scenarios.market.create_market_engine
  -> simulation flow model + owned SeededRng
  -> exchange OrderBook / mechanics events
  -> ScenarioRun observations and replayable result
```

Behavior must emerge from ordinary orders, cancels, queues, auctions, venue, latency,
and participant interfaces. Never add a target-price write or post-hoc favorable-path
edit.

### Full-day run

```text
FullDayPlanV1
  -> FullDayRuntime.create / advance_to
  -> ordered ScheduledWorkKeyV1 stages and component owners
  -> events + checkpoints + native ledgers
  -> FullDayStore.generate_day
  -> canonical manifest and immutable registered artifacts
```

Start in `models.py` for the plan, `runtime.py` for scheduling/rollback,
`components*.py` for owner adapters, `checkpoint_contract.py` for restore authority,
and `store.py` for persistence. `kirby2/audit/full_day.py` is verification, not the
runtime source of truth.

### Portable pack

```text
domain source or verified registered run
  -> domain adapter
  -> canonical manifest + logical pack ID
  -> deterministic archive transport digest
  -> hostile archive preflight
  -> private staging + dependency resolution
  -> atomic install + immutable registry/CAS receipt
```

Logical pack identity and archive transport identity are intentionally different.
Start with `packs/formats.py`, `identity.py`, `archive.py`, `builders.py`, then the
specific `*_pack.py` adapter. Installation authority lives in `staging.py`,
`dependencies.py`, `install.py`, and `registry.py`.

### Distributed work

```text
immutable LogicalWorkUnit
  -> planner and seed derivation
  -> data-only protocol
  -> direct/subprocess/TLS worker attempt
  -> coordinator verification + content-addressed registration
  -> deterministic late-result reduction and aggregate
```

Scientific work identity excludes leases, workers, attempts, process IDs, and wall
time. Operational attempt history is append-only and separate. Start with
`orchestration/models.py`, `planner.py`, `protocol.py`, `worker.py`,
`coordinator.py`, `content_store.py`, `recovery.py`, and `aggregation.py`.

### Replay microscope

```text
verified source recording/artifact
  -> ingestion + lineage/index
  -> observable/reveal/data-age policy
  -> panes/comparison/annotations
  -> self-contained offline report assets
```

The microscope explains recorded causality; it does not invent unavailable links or
turn reconstruction into observed history. Start with `microscope/models.py`,
`ingestion.py`, `lineage.py`, `policy.py`, and `report.py`.

### Offline release

```text
WO40-D frozen protocols
  -> D1 resource preflight
  -> WO40-E source lock/freeze
  -> WO40-F deterministic artifact builds
  -> WO40-G/H clean-platform qualification + WO40-I performance
  -> WO40-J immutable closeout
```

Use `RELEASE_MAP.md`; do not infer release readiness from source completeness.

## Governed writable state

`kirby2/research/paths.py::DataPaths` is the shared path authority. Its semantic areas
include runs, evidence, checkpoints, packs, identity mappings, config, cache, staging,
backups, diagnostics, release, datasets, logs, crash reports, temporary files, and
exports. Constructing `DataPaths` does not create them; exact owners call `ensure` at
write boundaries and revalidate against symlink/path rebinding.

Immutable evidence belongs in `runs` or `evidence`. Direct identity material belongs
in separately erasable `identity_mappings`. Temporary/staging bytes never become
evidence merely because they exist on disk.

## Identities that must stay separate

| Identity | Meaning |
| --- | --- |
| Source digest | Exact imported/source bytes and provenance |
| Semantic digest | Fully resolved behavior/configuration |
| Artifact digest | Exact serialized artifact bytes |
| Run ID | Content-derived registered run identity |
| Pack ID | Logical payload identity, excluding archive transport variation |
| Transport digest | Exact archive/bundle bytes |
| Logical work ID | Scientific work, excluding operational attempts |
| Attempt ID | Append-only execution attempt and environment |
| Human sidecar | Review authority excluded from reviewed object identity |

Likewise keep observable, reveal-only, ground truth, observed, derived,
reconstructed, counterfactual, unavailable, and rejected evidence classes distinct.

