# Kirby2 change router

Status: edit-location heuristic; verify against current source  
Source snapshot: `4a328723ffe8881f2358f1a72576f8ed905f2687`

## Minimal read algorithm

1. Select one row below.
2. Read the public models/contracts named in `First read`.
3. Locate direct producers and consumers with `rg`; do not read the package tree.
4. Read the persistence encoder/decoder if identity-bearing bytes change.
5. Read the matching audit only after understanding production behavior.
6. Before editing, determine whether the change alters semantic identity, artifact
   identity, operational metadata, or no governed identity.

## Change-to-owner map

| Change intent | First read | Usually coupled | Never bypass |
| --- | --- | --- | --- |
| FIFO matching, fills, cancels, queue position | `exchange/models.py`, `exchange/book.py` | `player/position.py`, session journal, exchange invariants | Integer ticks/shares, price-time priority, fill conservation |
| Auctions, sessions, advanced instructions | `exchange/mechanics_models.py`, `mechanics_engine.py` | `auction.py`, `mechanics_replay.py`, full-day mechanics adapter | Public mechanics replay and session ordering |
| Synthetic arrivals or RNG | `simulation/flow.py`, `flow_models.py`, `rng.py` | distributions, regimes, scenarios, release probes | Explicit seed ownership and no direct price commands |
| Accepted scenario behavior | `scenarios/market.py`, `accepted_scenarios.json` | simulation profiles, scenario audit, release fixture digests | Observable behavior versus hidden labels |
| Live player/strategy behavior | `session/live.py`, `strategy/runtime.py`, `features/engine.py` | session records/replay, UI terminal | Decision-time observability and simulation-time deadlines |
| Full-day plan or scheduler | `full_day/models.py`, `events.py`, `runtime.py` | components, checkpoints, restore, store | Total work ordering, rollback atomicity, exact continuation |
| Full-day persistence | `full_day/store.py`, `research/models.py` | checkpoint contracts, research paths/tables | Canonical bytes, immutable manifests, no path rebinding |
| Data location or lifecycle | `research/paths.py` | release backup/migration/recovery, every store writer | Semantic area ownership and symlink refusal |
| Research schema or artifact family | `research/models.py`, `tables.py`, `store.py` | producer store, verifier, packs, backup | Source/semantic/artifact/run identity separation |
| Market-data ingestion | `marketdata/models.py`, `normalization.py`, `store.py` | historical, calibration, capability audits | Missing data is not zero; source capability stays explicit |
| Historical replay/reconstruction | `historical/models.py`, `runner.py`, `features.py` | lesson runtime/presentation, microscope | Observed versus reconstructed evidence labels |
| Latency, venue, execution algorithm | Owning package `models.py` then runtime/engine | session, full-day component, replay, audit | No-lookahead and same-input deterministic replay |
| Microscope or offline report | `microscope/models.py`, `ingestion.py`, `policy.py`, `report.py` | lineage/index, panes, assets, release | Source-linked causality and unavailable-link truth |
| Pack format/identity | `packs/formats.py`, `models.py`, `identity.py` | archive, validation, every domain adapter | Pack ID is not transport digest |
| Pack install/dependency behavior | `packs/staging.py`, `dependencies.py`, `install.py` | registry, signatures, research paths | Preflight before extraction; atomic activation; no partial install |
| Distributed work/result behavior | `orchestration/models.py`, `protocol.py` | planner, worker, coordinator, recovery, aggregation | Logical work separate from attempts/leases/wall time |
| Release package or qualification | `release/*.toml`, `release/build.py`, `manifest.py`, `qualification.py` | packaging, licenses, performance, commands | Frozen protocol, offline inputs, candidate/evidence separation |
| New top-level CLI command | Owning `commands.py`, `cli/registry.py`, `cli/expansion.py` | package `__init__`, provenance/audit registration | Declarative semantic command ID; no new dispatch ladder |
| Audit/gate behavior | Production source first, then matching `audit/*.py` | `audit/expansion.py`, `auditlab` only if generic trust changes | Audit never becomes the production oracle it checks |
| Wire record or canonical codec | Owning `models.py`/contract and all `from_dict`/`as_dict` users | stores, packs, orchestration, release, hostile validators | Accepted/refused byte behavior and versioned identity |

## Fast source searches

```text
# Definition and exports
rg -n "class NAME|def NAME|\"NAME\"" kirby2/PACKAGE

# Direct construction/calls
rg -n "\bNAME\(" kirby2

# One wire field across encoders, decoders, TOML, and audits
rg -n '"field_name"|field_name[[:space:]]*=' kirby2 release

# Command registration and handler
rg -n 'name="command-name"|_handle_.*command' kirby2 -g 'commands.py'

# Gate ownership and evidence consumers
rg -n 'CARD_ID|gate_id' kirby2/audit KIRBY2_*ROADMAP.md KIRBY2_*GOAL.md

# Artifact/schema identity
rg -n 'SCHEMA_ID|SCHEMA_VERSION|artifact_type|media_type' kirby2/PACKAGE
```

Read imports/exports, the named contract, and only direct call sites. If more than
roughly five large files are needed merely to locate ownership, update this router
rather than making the next agent repeat the search.

## Coupling checklist

Before declaring a production slice structurally complete, ask:

- Did a public field, enum, schema/version, digest projection, or canonical ordering
  change?
- Does persistence round-trip that value, and does hostile input validate it
  independently?
- Does replay restore every authority-bearing field?
- Does a pack, distributed result, microscope view, backup, or release manifest carry
  the changed identity?
- Is the status automated, statistical, platform, or human—and did it stay in its
  own vocabulary?
- Did a new command use the central registry?
- Did documentation claim more than actual evidence?

These questions locate coupling; they do not authorize tests, audits, evidence
mutation, commits, or roadmap deviations. Follow the current user workflow for those
actions.

