# Market-data normalization boundary

Kirby2 imports local evidence through a declared source capability. The adapter
may parse a format, but it cannot grant exact-replay status. Normalization and
data-quality gates compute that decision from the capability and accepted facts.

## Capabilities

From least to most informative:

1. `BARS_ONLY`
2. `TRADES`
3. `TRADES_AND_QUOTES`
4. `LEVEL2_SNAPSHOTS`
5. `LEVEL2_DELTAS`
6. `MARKET_BY_ORDER`

Bars are not trades. Quotes are not Level 2. Market-by-price snapshots and deltas
do not identify individual queue order. Snapshots omit activity between captures.
Historical reconstruction is not exact replay. Only a clean, complete, ordered
`MARKET_BY_ORDER` message stream can pass the exact-replay gate.

Normalized records cover bars, trades, quotes, book snapshots, book deltas,
individual order events, auctions, halts, resumes, symbol metadata, and session
metadata. Exchange prices remain integer ticks. Every record preserves its source
timestamp text, UTC-normalized epoch nanoseconds, source timezone, declared
precision, optional source sequence, symbol, and session.

The normalized integer uses nanoseconds as a lossless container; it does not imply
nanosecond source precision. `timestamp_precision` remains authoritative, and a
timestamp whose text is less precise than its declaration is rejected.

## Quality policy

Default ingestion is strict. Duplicate or out-of-order records, timestamp
reversals, crossed/locked quotes, negative quantities, invalid prices, and
session-boundary violations are rejected and reported. Missing source sequences,
unknown aggressor sides, and snapshot gaps remain explicit warnings/gaps. No row
is silently reordered, imputed, crossed-book corrected, or assigned a guessed
aggressor. `repairs` therefore remains empty unless a future caller introduces a
named explicit repair policy with before/after evidence.

Each `DataQualityReport` reconciles input, accepted, and rejected rows and records
capability, time range, symbols, sessions, gaps, repairs, and source SHA-256.

## Adapters and fixtures

- `csv`: canonical flat CSV plus a same-stem TOML capability sidecar.
- `parquet`: canonical flat Parquet plus a same-stem TOML capability sidecar.
- `kirby-mbo`: the existing local exact order-message fixture format.

The included bar and trades/quotes fixtures are small, locally authored format
examples, not claims about a real security or venue. The Kirby MBO fixture is also
pedagogical; it validates exact-message architecture, not market realism.

## Immutable provenance

Imports live beside runs in the Work Order 21 research root:

```text
STORE/
  catalog.duckdb
  datasets/
    dataset-<24 hex>/
      dataset_manifest.toml
      quality_report.toml
      normalized_records.parquet
      quality_issues.parquet
```

The manifest commits the source digest, logical normalized-record digest, quality
digest, replay decision, schemas, and artifact digests. Reimport is idempotent;
tampered evidence is never overwritten. The rebuildable DuckDB catalog exposes
these entries through `dataset_registry` and `dataset_provenance`, keeping source
provenance in the same ledger as run evidence.

## Commands

```bash
python3 -m kirby2 ingest-market-data --adapter csv SOURCE --store STORE
python3 -m kirby2 inspect-dataset DATASET_ID --store STORE
python3 -m kirby2 validate-dataset DATASET_ID --store STORE
python3 -m kirby2 replay-capability DATASET_ID --store STORE
python3 -m kirby2 audit-market-data
```
