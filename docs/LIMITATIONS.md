# Known limitations

This document defines claim boundaries for Kirby2 0.1. A passing deterministic run,
artifact verification, platform qualification, or performance measurement does not
remove these limitations.

## Scope boundary

- Kirby2 is a simulation and training environment.
- Kirby2 is not a broker.
- Kirby2 is not a live market connector.
- Kirby2 provides no performance guarantee.
- A reconstruction is not proof of historical market state.

## Product form and supported targets

- V1 supports macOS arm64 and Linux x86_64 under CPython 3.14.
- Windows, Linux arm64, mobile platforms, and browser-hosted execution are outside V1.
- `DESKTOP_V1` is a terminal trainer plus explicitly opened local offline HTML
  analysis. It is not a native-widget GUI.
- The release contains no updater, telemetry, account system, subscription, social
  feed, public leaderboard, background daemon, or cloud synchronization.

## No real trading or financial claim

- There is no broker, exchange, order-routing, live account, credential, market-data
  feed, or live-execution integration.
- Simulated orders, fills, positions, P&L-like quantities, queues, and traffic lights
  have meaning only inside the declared mathematical model.
- Kirby2 does not provide investment advice or claim profitable strategies, validated
  mastery, suitability, or transfer to real-market decisions.

## Model and data limits

- Core outputs are deterministic mathematical simulation artifacts. They are not
  observed market data, predictions, or evidence of empirical resemblance.
- Supported mechanics are bounded by the implemented event, venue, latency, agent,
  liquidity, and session models. Missing mechanisms do not become negligible because
  invariants pass.
- Parameter calibration and distribution diagnostics describe their supplied data and
  declared model only. They do not establish stationarity or external validity.
- A traffic-light script is an observable-only training annotation. It is not a
  recommendation, permission, or order instruction for a real market.

## Historical and counterfactual limits

- `EXACT_REPLAY` means source-provided order events within a fixture's stated scope. It
  does not independently prove source authenticity, completeness, causation, or full
  market state.
- `RECONSTRUCTION` and `SYNTHETIC_RECONSTRUCTION` are modeled. They cannot be relabeled
  as observations or used as proof of historical market state.
- `COUNTERFACTUAL` output is an alternative under declared assumptions, not a claim
  about what would have happened.
- `AS_OBSERVED` protects a declared observation boundary; `POSTMORTEM` can use later
  evidence and therefore answers a different question.

## Determinism and portability limits

- Determinism applies to the exact bound source, inputs, runtime contract, seeds, and
  integer or explicitly normalized operations that make the claim.
- Float/libm-driven full-day outputs are runtime/platform scoped. Kirby2 does not claim
  cross-platform byte identity for them.
- The preregistered cross-platform exact comparison is limited to its declared
  integer-core workload.
- Content-derived IDs and SHA-256 digests detect byte changes; they do not by themselves
  authenticate a publisher or prove scientific correctness.

## Performance limits

- Release performance results describe the preregistered machine, runtime, artifacts,
  workload, warmups, samples, and thresholds only.
- Passing latency, memory, storage-growth, replay, or report-load thresholds is not a
  promise for another computer, workload, dataset, terminal, or future release.
- Operational timing and memory measurements are evidence outside semantic run
  identity and can vary with scheduling and host conditions.

## Security and privacy limits

- Kirby2 minimizes and separates governed local data, but the user controls host
  security, filesystem permissions, backups, recipients, and external tools.
- Pack structural validity, digest integrity, authenticity, compatibility, provenance,
  privacy, and scientific status are separate. One passing dimension does not imply
  another.
- Portable report IDs detect internal consistency but do not authenticate the issuer
  without a separately trusted pin, signature, or receipt.
- Redacted diagnostics and exports still require human review before sharing.
- Uninstalling application files preserves user data by design; deletion is a separate
  explicit responsibility.

## Release-state limits

Source completion, artifact construction, macOS qualification, Linux qualification,
performance evidence, and final closeout are distinct states. A source-ready or
preflight-passing repository is not automatically a qualified or accepted release.
Only the closeout packet may aggregate the independently passing evidence, and even a
closed release remains subject to every limitation above.
