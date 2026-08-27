# Kirby2 Audit Trust Repair Closeout Evidence

Status: ATR-00 through ATR-19 complete

Evidence date: 2026-08-27

Scope: audit-trust repair only. This closeout does not begin Work Order 31, add
product features, alter immutable evidence, or authorize a push.

## 1. Outcome and identity boundary

The executable repair sequence began from baseline
`a71c3f2e98097bda0f733f0c7c0bd83ba390eec5` and the final evidence-producing
run executed the clean code commit
`e84047e42f4079c83f9542b2caa66058e7051381`. The only change after that run is
this documentation-only closeout, committed with the prescribed subject
`Close audit trust repair sequence`. Its commit hash is reported after commit;
a Git commit cannot contain its own hash without changing that hash.

The final automated result is `PASS_WITH_WARNINGS`:

- structural, coverage, replay, determinism, fault, provenance, and runtime
  invariant gates are `PASS`;
- the statistical gate is `WARNING`, with three explicitly preserved scientific
  warnings and no statistical failure;
- manual acceptance is separately `PENDING_HUMAN_REVIEW` and is not inferred
  from the automated result;
- unexpected violations, minimized unexpected failures, and saved reproducers
  are all zero.

The verified closeout packet is:

```text
packet_id=audit-bffd05b9d74bb12b0840bcf0
path=/Users/kogaryu/Documents/ChatGPT/kirby2/.kirby2/research/audit_lab/packets/audit-bffd05b9d74bb12b0840bcf0
schema_version=2
identity_scope=IDENTITY_AND_ARTIFACTS
artifact_count=13
manifest_sha256=7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578
verification_status=PASS
acceptance_record=acceptance-8a34abc8a267b064eaeb
manual_acceptance=PENDING_HUMAN_REVIEW
```

The `.kirby2` evidence store is ignored by Git. It remains an immutable local
evidence store and is not included in the closeout commit.

## 2. Execution environment and exact substantial command

The final packet-producing automated run used the repository virtual
environment:

```text
working_directory=/Users/kogaryu/Documents/ChatGPT/kirby2
python=3.14.3
duckdb=1.5.5
```

The unactivated interpreter's dependency refusal was re-confirmed with this
exact preflight during closeout review:

```text
command=PYTHONDONTWRITEBYTECODE=1 python3 -c "import duckdb; print(duckdb.__version__)"
resolved_python=/opt/homebrew/bin/python3
exit_code=1
stderr=Traceback (most recent call last):
  File "<string>", line 1, in <module>
    import duckdb; print(duckdb.__version__)
    ^^^^^^^^^^^^^
ModuleNotFoundError: No module named 'duckdb'
```

This proves the missing dependency directly; it does not claim the full audit
imports DuckDB before generated-case execution. The original pre-correction
full-command stderr was not retained and is not reconstructed here. During the
closeout review, a diagnostic full unactivated invocation entered case execution
because the DuckDB import is lazy; it was interrupted with `KeyboardInterrupt`
and exit code `130` before persistence. Packet and acceptance-ledger counts
remained seven, so that diagnostic is excluded from acceptance evidence.

No gate was weakened or substituted. Activating the existing project
environment was the concrete correction, after which the roadmap's exact
command completed once from a clean worktree:

```text
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 python3 -m kirby2 audit-lab --budget 10000 --seed 771 --save-failures
exit_code=0
```

Actual output:

```text
KIRBY2_MODEL_RISK_LAB
RUN seed=771 budget=10000 status=PASS_WITH_WARNINGS
CASES executed=10000 unexpected_violations=0 replay_parity=PASS
FAULTS injected=1428 detected=1428 signatures=10 minimized=0
DETERMINISM status=PASS fresh_process_configurations=16 process_runs=32
PROVENANCE status=PASS git_commit=e84047e42f4079c83f9542b2caa66058e7051381 working_tree_dirty=false implementation_sha256=ed5c76015d72e910a1b9ad7346e3d8ad9ba5a03fb9fb224970b7b65d889fc628 manifest_sha256=2f625225e5513ad85b08ce3a08ef788fae413eeb22d55eccf429d12a6b48e664
STATISTICS calibration_train_vs_holdout=WARNING distribution_drift=WARNING scenario_overfitting=PASS seed_sensitivity=PASS unstable_hawkes=WARNING unrealistic_event_explosion=PASS degenerate_no_trade=PASS price_runaway=PASS permanent_crossed_composite_quote=PASS
PROBES advanced_order_instructions=PASS asynchronous_races=PASS auction_allocation=PASS branch_parent_consistency=PASS calibration_holdout=PASS data_quality_faults=PASS explicit_fault_semantics=PASS hawkes_certification=PASS hidden_observability=PASS multi_venue_reconciliation=PASS owned_agent_rng=PASS
ACCEPTANCE_RECORD id=acceptance-8a34abc8a267b064eaeb
PACKET id=audit-bffd05b9d74bb12b0840bcf0 path=/Users/kogaryu/Documents/ChatGPT/kirby2/.kirby2/research/audit_lab/packets/audit-bffd05b9d74bb12b0840bcf0 verification=PASS
STRUCTURAL_STATUS PASS
COVERAGE_STATUS PASS
REPLAY_STATUS PASS
DETERMINISM_STATUS PASS
FAULT_STATUS PASS
STATISTICAL_STATUS WARNING
PROVENANCE_STATUS PASS
MANUAL_ACCEPTANCE PENDING_HUMAN_REVIEW
AGGREGATE_STATUS PASS_WITH_WARNINGS
RUNTIME_INVARIANTS PASS
```

## 3. Focused audit restart after ATR-19A

ATR-19 was restarted from step 1 at clean commit
`e84047e42f4079c83f9542b2caa66058e7051381`. Every prescribed focused command
exited zero and left the worktree unchanged.

| Command | Cases | Failures | Result |
|---|---:|---:|---|
| `python3 -m kirby2 demo --seed 42` | deterministic scenario | 0 | `RUNTIME_INVARIANTS PASS` |
| `python3 -m kirby2 audit-hawkes-stability` | 11 | 0 | `PASS` |
| `python3 -m kirby2 audit-market-data` | 5 | 0 | `PASS` |
| `python3 -m kirby2 audit-latency` | 11 | 0 | `PASS` |
| `python3 -m kirby2 audit-market-mechanics` | 18 | 0 | `PASS` |
| `python3 -m kirby2 audit-hidden-liquidity` | 15 | 0 | `PASS` |
| `python3 -m kirby2 audit-multivenue` | 16 | 0 | `PASS` |
| `python3 -m kirby2 audit-execution-algorithms` | 15 | 0 | `PASS` |
| `python3 -m kirby2 audit-counterfactuals` | 10 | 0 | `PASS` |
| `python3 -m kirby2 audit-agent-ecology` | 13 | 0 | `PASS` |
| `python3 -m kirby2 audit-model-risk-lab` | 21 | 0 | `PASS` |
| `git diff --check` | n/a | 0 | clean |

All Python commands used `PYTHONDONTWRITEBYTECODE=1` inside `.venv`.

## 4. Repeated 512-case determinism evidence

Two sequential non-persisting seed-771 runs produced exact parity across every
roadmap-requested digest family:

| Structure | Run 1 SHA-256 | Run 2 | Match |
|---|---|---|---|
| Clean pre-closeout canonical `summary_dict` | `0756272db621a9700a6bc205b46f6fc1e82f6a54ce33d9fa9625f974aa73144b` | same | yes |
| Ordered event-digest vector | `35c6ecfa0abf6cbb0001595ef345e54034b40f8f0c5250bb30599c07685951fc` | same | yes |
| Ordered state-digest vector | `55fe521e2ee2a948bfec66993bb897be733d00602a7c0fe5fb229537f74a748c` | same | yes |
| Coverage object | `6434bfe6043e09ba00610391d4a809376b1256b34228c37b9228210901fc7829` | same | yes |
| Statistics list | `87f26b3680176d3259e96490f75b8bfdb3a063e4d1a15e89bf1986550cb307b4` | same | yes |
| Ordered per-case result-digest vector | `753c8a2db3b7a190bf28ced5dd98dd31fb48212938127c53029267d138714bf5` | same | yes |
| Canonical cases aggregate | `ec90f7c3b6c109e6d823e73eccdc992e62d7797285574f52c7c51d9d2c1a1815` | same | yes |

Both runs had `PASS_WITH_WARNINGS`, `512/512` loaded replay parity,
`73/73` detected injected faults, `16` fresh-process samples over `32` process
runs with zero mismatch, seven of seven covered lanes, zero missing configured
values, zero failed or unexercised required checks, zero unexpected violations,
and zero minimized failures. Neither run persisted a packet.

## 5. Four-seed 512-case matrix

| Seed | Cases SHA-256 | Aggregate | Replay | Faults | Determinism | Coverage | Unexpected / minimized |
|---:|---|---|---|---|---|---|---|
| 1 | `5d63b774061faacb6aecdc3bb970c995fe2fa8b7058937faa9623c4e5a546f08` | `PASS_WITH_WARNINGS` | 512/512, 0 fail | 73/73, 0 miss | 16 samples, 32 runs, 0 mismatch | 7/7, 0 missing, 0 unexercised, 0 failed | 0 / 0 |
| 42 | `bc7249b36da955093567681ae824572c67713c7ab88ae42c2dbfdae6eb61c886` | `PASS_WITH_WARNINGS` | 512/512, 0 fail | 73/73, 0 miss | 16 samples, 32 runs, 0 mismatch | 7/7, 0 missing, 0 unexercised, 0 failed | 0 / 0 |
| 771 | `ec90f7c3b6c109e6d823e73eccdc992e62d7797285574f52c7c51d9d2c1a1815` | `PASS_WITH_WARNINGS` | 512/512, 0 fail | 73/73, 0 miss | 16 samples, 32 runs, 0 mismatch | 7/7, 0 missing, 0 unexercised, 0 failed | 0 / 0 |
| 4,294,967,295 | `95cedb5aa3386699646b23d0cd41dacacf3ed101bca084624018cea37bf5f650` | `PASS_WITH_WARNINGS` | 512/512, 0 fail | 73/73, 0 miss | 16 samples, 32 runs, 0 mismatch | 7/7, 0 missing, 0 unexercised, 0 failed | 0 / 0 |

Each seed scheduled the same real lane counts:

```text
CORE_FLOW=74
ALGORITHM=73
ECOLOGY=73
FAULT=73
FRAGMENTED=73
LATENCY=73
MECHANICS=73
```

All eleven subsystem probes passed for every seed. Every configured dimension
value and every within-lane configured pair was exercised. Each run formed 72
complete six-replicate scientific cells. There were no invariant, replay,
fault-detector, determinism, coverage, subsystem-probe, or provenance failures.

## 6. Final 10,000-case runtime evidence

### 6.1 Real executor lane counts

| Lane | Cases |
|---|---:|
| `CORE_FLOW` | 1,429 |
| `MECHANICS` | 1,429 |
| `LATENCY` | 1,429 |
| `FRAGMENTED` | 1,429 |
| `ALGORITHM` | 1,428 |
| `ECOLOGY` | 1,428 |
| `FAULT` | 1,428 |
| Total | 10,000 |

### 6.2 Coverage and required checks

Coverage status was `PASS`: seven of seven lanes, 10,000 result records, zero
missing configured values, zero mismatched exercise records, zero unexercised
required checks, and zero failed required checks.

Configured and exercised cardinalities were identical:

- core flow: 25 durations (`8000` through `32000` microseconds), 2 flow
  models, 5 liquidity states, 12 regimes, 1,429 generated seeds, and 6 volume
  scales;
- mechanics: 4 auction states, 4 instruction groups, and 8 session phases;
- latency: 5 profiles;
- fragmented venues: 3 hidden-liquidity modes and 4 venue counts;
- algorithms: 4 objectives and 9 execution strategies;
- ecology: 8 agent counts and 3 populations;
- fault lane: all 10 injected fault kinds.

All within-lane configured pairs were exercised with zero missing pairs.
Required runtime checks were reported and exercised once per applicable case:

| Lane | Required check families | Applicable cases | Missing | Not exercised | Failed |
|---|---:|---:|---:|---:|---:|
| `CORE_FLOW` | 9 | 1,429 | 0 | 0 | 0 |
| `MECHANICS` | 6 | 1,429 | 0 | 0 | 0 |
| `LATENCY` | 6 | 1,429 | 0 | 0 | 0 |
| `FRAGMENTED` | 6 | 1,429 | 0 | 0 | 0 |
| `ALGORITHM` | 5 | 1,428 | 0 | 0 | 0 |
| `ECOLOGY` | 5 | 1,428 | 0 | 0 | 0 |
| `FAULT` | 3 | 1,428 | 0 | 0 | 0 |

### 6.3 Replay, injected faults, determinism, and probes

```text
loaded_replays=10000
passed_replays=10000
failed_replays=0
recording_schema_version=2

faults_injected=1428
faults_detected=1428
faults_missed=0
fault_observations=1428
unique_detector_signatures=10

fresh_process_samples=16
fresh_process_runs=32
fresh_process_mismatches=0

unexpected_violations=0
minimized_unexpected_failures=0
saved_reproducers=0
```

All eleven required subsystem probes passed:
`advanced_order_instructions`, `asynchronous_races`, `auction_allocation`,
`branch_parent_consistency`, `calibration_holdout`, `data_quality_faults`,
`explicit_fault_semantics`, `hawkes_certification`, `hidden_observability`,
`multi_venue_reconciliation`, and `owned_agent_rng`.

### 6.4 Statistical statuses and predeclared thresholds

All checks bind threshold-manifest SHA-256
`4a6944591dd37d2400898fb98f17576e5fcabf784ddd909878acff73feb0e02e`.

| Check | Status | Complete cells / comparisons | Threshold | Observed evidence |
|---|---|---:|---|---|
| `calibration_train_vs_holdout` | `WARNING` | 2 fitting and 2 heldout seeds | finite losses and disjoint derived seed sets; warn when heldout/fitting final-loss ratio exceeds 4.0 or heldout loss does not improve | finite and disjoint; fitting `0.401585454`, heldout `0.298802929`, ratio `0.744058148`; heldout did not improve from its initial value |
| `distribution_drift` | `WARNING` | 238 comparisons | maximum matched-cell train/holdout histogram total variation <= 7,500 bps | maximum 10,000 bps |
| `scenario_overfitting` | `PASS` | 3 contexts | matched scenario/objective strategy-rank concordance >= 5,000 bps | 9,815 bps; no universal winner declared |
| `seed_sensitivity` | `PASS` | 1,428 complete cells | maximum within-cell event-count range/median <= 50,000 bps | 685 changed cells; maximum 22,000 bps |
| `unstable_hawkes` | `WARNING` | 4 production-profile certifications | no rejected production profile; near-critical certifications remain warnings | zero rejected; `panic` warning at upper bound `0.805136029246` versus warning threshold `0.8` |
| `unrealistic_event_explosion` | `PASS` | 119 capped comparisons; 119 uncapped cells | no capped count breaches the one-sided Poisson-dominating envelope at Bonferroni-corrected family-wise alpha `1e-6` | zero failed and zero invalid cells; maximum observed/cap 10,417 bps; minimum adjusted upper-tail probability `1.0` |
| `degenerate_no_trade` | `PASS` | 1,161 continuous eligible cells | no-trade rate <= 9,000 bps in continuous trade-eligible matched cells | 3,279 of 6,966 eligible cases; 4,707 bps |
| `price_runaway` | `PASS` | 1,428 complete cells | maximum absolute displacement from each recorded initial reference <= 200 x2 ticks | maximum 14 x2 ticks across six scientific lanes |
| `permanent_crossed_composite_quote` | `PASS` | 1,428 timeline cases | no continuous locked/crossed episode exceeds `max(100000 us, 4 * maximum market-data latency)` | zero permanent episodes; 1,434 short episodes measured |

The three warnings remain visible in the immutable acceptance record. They are
not relabeled as failures or passes.

### 6.5 Provenance

```text
status=PASS
git_commit=e84047e42f4079c83f9542b2caa66058e7051381
working_tree_dirty=false
execution_window_stable=true
implementation_file_count=192
loaded_repository_module_count=153
unbound_loaded_repository_modules=0
implementation_sha256=ed5c76015d72e910a1b9ad7346e3d8ad9ba5a03fb9fb224970b7b65d889fc628
provenance_manifest_sha256=2f625225e5513ad85b08ce3a08ef788fae413eeb22d55eccf429d12a6b48e664
dirty_state_sha256=370578f2db8c2b933169692e265275bbe48a746d36a56897db03f16b3c7882d1
```

## 7. Packet and ledger verification

`AuditLabStore.verify()` returned `PASS`. The v2 packet ID independently
recomputed from schema version, complete artifact references, and identity
payload as `audit-bffd05b9d74bb12b0840bcf0`. There were no symlinks, unsafe
paths, missing files, unexpected files, digest mismatches, or byte-size
mismatches.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `acceptance_record.json` | 1,643 | `d8ea967a8b0739adabf780beab2ef442138fcf83a5362a5876b8ddaccc6fce94` |
| `cases.jsonl` | 106,548,353 | `2771ca8accdaf5635e1d71c8887823a2ecf23269fb377b8290b6fa8d73b3e0dc` |
| `coverage.json` | 71,750 | `6879d734973b43cc8ae70a1775b7bb1c04a510f00de273cf8dee9fbaa89e7c19` |
| `determinism.json` | 3,903 | `5f8f0a0cf09c046dadb7a8fd57a41cc061e0b1cb32dde22a5d2cb6959b0461b5` |
| `faults.jsonl` | 6,766,046 | `12aac204e2bb27caade22cb185fc215c8983f2c0faf73cc8d80d6d3d76df2fd8` |
| `minimized_failures.json` | 3 | `37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570` |
| `probes.json` | 14,369 | `ccd4c8749710738396880c3e92be897c70487d465ab4dc7a3a690fc6c4814cbb` |
| `provenance.json` | 25,373 | `6436ab2f6e27eb1d20a8bb168f69d3cc311ff7491c683c0a14d1f0da6f484223` |
| `replay_parity.json` | 127 | `ac333c3ca070507e0cbde1736d4925bf19ef8ca622ec20deb87412fee6888f67` |
| `report.txt` | 1,436 | `dd4085b1a5666c2d3414313ddbe2d652e5b3734211f55c499d379a84fafd5aff` |
| `statistical_thresholds.json` | 2,986 | `73d86b38f1aba045213a6bd6a6817d6aec5ad5d0e920de86c362f78321caa8c3` |
| `statistics.json` | 7,128,148 | `77cf713ac4b9ebbdc392f7378b467023cfac13e7b4427c8a72ab6a2d4ebc8b44` |
| `unexpected_violations.json` | 3 | `37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570` |

Key logical identity digests:

```text
case_result_sha256=7083356195c8643adbac75c106bafc114bfe75b0661d2dc05489d57f478cb4fb
coverage_sha256=5db6a6e3f3bb25e71597befafe178a633ab2fdc54911d1d3436e863504c7ce43
determinism_sha256=1467c6b370a1d8448d1a4c5dbff4e4808b618d07f92b3470697de7cc6896a8e6
fault_observations_sha256=008a6e016d0cc986f056fc813e3eb18e839c1a500a50b1fa415260d55952bd6a
minimized_failures_sha256=4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945
probes_sha256=6c51a512b7033771cceb174f0972d21dba8cfbf45b6d6514aab3a528465fbd1c
statistics_sha256=d55955e5cafe5246ff3c943820e65e12a1e2920fc62e03640a8b7f89161fc9b8
```

`AuditLabStore.verify_ledgers()` returned:

```text
status=PASS
packet_count=7
acceptance_record_count=7
failures=[]
```

The target packet and target acceptance record each occur exactly once. The
packet copy and immutable acceptance-record copy are byte-identical and bind to
SHA-256 `d8ea967a8b0739adabf780beab2ef442138fcf83a5362a5876b8ddaccc6fce94`.

## 8. Non-persisting 10,000-case parity rerun

The same 10,000 generated configuration set was rerun through
`run_audit_lab(budget=10000, seed=771, persist=False, save_failures=True)`.
The call returned `packet=None`; packet and acceptance ledger counts remained
seven. The script asserted equality against the persisted packet's declared
case-result digest and exited zero:

```text
persisted_case_result_sha256=7083356195c8643adbac75c106bafc114bfe75b0661d2dc05489d57f478cb4fb
rerun_case_result_sha256=7083356195c8643adbac75c106bafc114bfe75b0661d2dc05489d57f478cb4fb
match=true
event_vector_sha256=b3fba0fde237d195d293cd9f6267d196f7c4f88a5a69a9973f0eba5d7f688deb
state_vector_sha256=6c6e313f40995d4dee57cadcbbdcd8f02c656d7f6a683d9e01b045faf3aa8bc4
coverage_sha256=5db6a6e3f3bb25e71597befafe178a633ab2fdc54911d1d3436e863504c7ce43
statistics_sha256=d55955e5cafe5246ff3c943820e65e12a1e2920fc62e03640a8b7f89161fc9b8
result_vector_sha256=2d3e9a64ef2e75c854af43988b54bb96c979c27b0339dd7ffaa53fefb4fed560
replay_parity_sha256=d389e2c2974e4d72a6e30816bf484a540980f31930d6db7fb5ab323fa9d390d9
fault_summary_sha256=ed27f95dcea9ab3f44e87050a48f3ea01b140855f0a5b23ca46169392a5d8211
fault_observations_sha256=008a6e016d0cc986f056fc813e3eb18e839c1a500a50b1fa415260d55952bd6a
determinism_sha256=1467c6b370a1d8448d1a4c5dbff4e4808b618d07f92b3470697de7cc6896a8e6
rerun_replay_loaded_passed_failed=10000/10000/0
rerun_faults_injected_detected_missed=1428/1428/0
rerun_determinism_samples_processes_mismatches=16/32/0
```

The direct Python API launch has a different loaded-module provenance manifest
than the `python -m kirby2` CLI launch; it therefore produces a different
acceptance-record ID. That expected launch-provenance distinction is not hidden
or treated as packet identity parity. The roadmap-required declared result
digest matched; the rerun independently reproduced all 10,000 case outputs and
coverage/statistics digests, and independently passed replay, fault, and
fresh-process determinism with the same counts and semantic digests.

## 9. Reproducer accounting and prior immutable evidence

The closeout packet was created with `save_failures=true`, but contains no
`failures/` subtree because:

```text
unexpected_violations=0
unique_minimized_unexpected_failures=0
saved_reproducer_artifacts=0
```

Expected fault observations are stored in `faults.jsonl`; they are not product
defects and are not minimized as reproducers.

All six pre-existing packets remain intact and independently verifiable. In
particular:

- `audit-2fe994077b7bf5c9818750ee` is the immutable first 10,000-case ATR-19
  packet whose automated precheck rejected the pre-ATR-19A statistical defect;
  its `acceptance-20d989f2c7cd8c0b8d15` decision remains
  `REJECT_AUTOMATED_PRECHECK`, and
  its manifest remains
  `3493a1c25f40235f50f57970c8ecd4b5b2cac791593d611a1a8d72ac657b6e1f`;
- `audit-8ef6c47015027fe3040527fd` is an intermediate 512-case v2 packet created
  while diagnosing ATR-19A, not the closeout packet; its
  `acceptance-19058cb2ac62fab8e710` decision remains `PENDING_HUMAN_REVIEW`, and
  its manifest remains
  `e9fa128fba58772f5589a686db1feca88a0aced7892bcca4493e13461138c69f`;
- four earlier schema-v1 packets remain readable with
  `IDENTITY_ONLY_LEGACY` scope.

No existing packet or acceptance record was edited, removed, or relabeled.

## 10. Exact repair and amendment commit inventory

The following is the complete sequence from the stated baseline through the
code-under-audit commit. Paths are exact for each commit.

- `5f60af1623e93cb26b6f0ce64d37b8c0da7a480b` — `Document audit trust repair sequence`
  - `KIRBY2_AUDIT_TRUST_REPAIR_ROADMAP.md`
- `5032b668821c1716bdae7c5cda72b7ea23102e2e` — `Contain audit packet artifact paths`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/store.py`
- `f468247b44ac4b876abf2259c90ffc0f39d13297` — `Bind audit packet identity to artifacts`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py`, `kirby2/auditlab/store.py`
- `c73622e9d956f782bc39b6a8006a86e872e0c3d3` — ATR-03A, `Accept immutable replay mappings in scoring`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/session/scoring.py`
- `d60926a742f41b5e955c470aaea3185e2993f5ef` — `Freeze core replay payloads`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/immutable.py`, `kirby2/session/events.py`, `kirby2/session/records.py`
- `3a4e39e8fe7a90592f99d6766c2d9913e1d341b5` — `Freeze subsystem evidence payloads`
  - `kirby2/agents/ecology.py`, `kirby2/agents/models.py`, `kirby2/audit/counterfactuals.py`, `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/kernel.py`, `kirby2/auditlab/models.py`, `kirby2/counterfactual/models.py`, `kirby2/exchange/mechanics_engine.py`, `kirby2/exchange/mechanics_models.py`, `kirby2/latency/models.py`, `kirby2/multivenue/models.py`, `kirby2/observability/models.py`, `kirby2/observability/scenarios.py`, `kirby2/observability/venue.py`, `kirby2/simulation/flow.py`
- `28a31006c2d942798de8b58c9e0a6a7f44851e5f` — `Enforce exchange state ownership`
  - `kirby2/audit/invariants.py`, `kirby2/audit/model_risk_lab.py`, `kirby2/exchange/__init__.py`, `kirby2/exchange/book.py`, `kirby2/exchange/mechanics_engine.py`, `kirby2/exchange/models.py`, `kirby2/scenarios/demo.py`, `kirby2/session/live.py`
- `609156a4d0d34d1408dba6a5284ac5eb6237c416` — `Define truthful audit execution contracts`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/base.py`, `kirby2/auditlab/generator.py`, `kirby2/auditlab/models.py`, `kirby2/auditlab/probes.py`
- `51b948a9e3f67a10b124a9137d45506b50329cb9` — ATR-06A, `Align core flow duration capability`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/base.py`
- `635dd64f0b2b996f62494fe8a3297f4a7d3048d5` — ATR-06B, `Cycle audit axes within executor lanes`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/generator.py`
- `1621964f1ca02f0ee7c5b951ea5fff937fcdea78` — `Exercise real synthetic flow in audit lab`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/core_flow.py`, `kirby2/simulation/comparison.py`
- `3d8d24cc0c48d2c1a518d918e1092427dfd54449` — ATR-08A, `Preserve mechanics expiry classification`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/audit/market_mechanics.py`, `kirby2/exchange/mechanics_engine.py`
- `66720c1f33e765bd3e399a08b2d9d105af416a2a` — ATR-08B, `Implement genuine GTC mechanics`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/audit/market_mechanics.py`, `kirby2/exchange/MECHANICS.md`, `kirby2/exchange/mechanics_models.py`
- `134c54fff53a2de21bbaab1dddebfd74388d1434` — `Exercise real market mechanics in audit lab`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/mechanics.py`, `kirby2/exchange/mechanics_scenarios.py`
- `b45755cfd1d7d77b78c79cca4ca021dc7d359668` — ATR-09A, `Expose latency pending event horizon`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/audit/latency.py`, `kirby2/latency/engine.py`
- `d66fd81264522b2c1ec06826de7d7f88429c08cc` — `Exercise real asynchronous latency in audit lab`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/latency.py`, `kirby2/latency/scenarios.py`
- `29672795911674705b6566efa7c8252f178c8199` — `Exercise real fragmented venues in audit lab`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/fragmented.py`, `kirby2/multivenue/scenarios.py`, `kirby2/observability/scenarios.py`
- `a93eb3ca119dd74d3781fc8a3651272b38aeeaaf` — ATR-11A, `Embed ecology definitions in replay records`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/agents/__init__.py`, `kirby2/agents/models.py`, `kirby2/agents/replay.py`, `kirby2/audit/agent_ecology.py`
- `4a877f18a5c79325b3d2b0615d4da085d595cb89` — `Exercise real agent ecology in audit lab`
  - `kirby2/agents/populations.py`, `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/ecology.py`
- `ab10eb1946e6b966ac5959868046c2fdd2b03969` — ATR-12A, `Expose automated algorithm strategy axis`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/auditlab/generator.py`
- `afcae0f5e672037dd8f28dfeeb1491fab0430c15` — `Exercise real execution algorithms in audit lab`
  - `kirby2/algorithms/__init__.py`, `kirby2/algorithms/benchmark.py`, `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/algorithms.py`
- `b13c16d93fd585242268c325dfd3b3785467c211` — ATR-13A, `Refuse invalid mixed-lane event expansion rate`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/auditlab/statistics.py`
- `88fda166987eb36ee416ed1de00758ab3c9f77f2` — `Route generated audits through real systems`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/__init__.py`, `kirby2/auditlab/executors/__init__.py`, `kirby2/auditlab/executors/core_flow.py`, `kirby2/auditlab/executors/fault.py`, `kirby2/auditlab/generator.py`, `kirby2/auditlab/kernel.py`, `kirby2/auditlab/minimizer.py`, `kirby2/auditlab/models.py`, `kirby2/auditlab/probes.py`, `kirby2/auditlab/projectors.py`, `kirby2/auditlab/runner.py`, `kirby2/auditlab/worker.py`
- `f43a7c9f1d561282568028df1a46197943bdc611` — `Replace fabricated audit fault detectors`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/__init__.py`, `kirby2/auditlab/executors/base.py`, `kirby2/auditlab/executors/fault.py`, `kirby2/auditlab/fault_oracle.py`, `kirby2/auditlab/faults.py`, `kirby2/auditlab/kernel.py`, `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py`, `kirby2/latency/__init__.py`, `kirby2/latency/diagnostics.py`, `kirby2/multivenue/__init__.py`, `kirby2/multivenue/diagnostics.py`
- `aeb608cfae04bc62ca96b185252e86276dd66239` — `Replay serialized generated recordings`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/executors/algorithms.py`, `kirby2/auditlab/executors/base.py`, `kirby2/auditlab/executors/core_flow.py`, `kirby2/auditlab/executors/ecology.py`, `kirby2/auditlab/executors/fault.py`, `kirby2/auditlab/executors/fragmented.py`, `kirby2/auditlab/executors/latency.py`, `kirby2/auditlab/executors/mechanics.py`, `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py`
- `28d3229111b46f9ced571ac5708911785232764d` — `Minimize only reproducible audit defects`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/minimizer.py`, `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py`
- `6ef14dfa3cefaa363306f4d65149fb9c50b29ba3` — `Build controlled audit risk statistics`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/generator.py`, `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py`, `kirby2/auditlab/statistics.py`
- `570875ac10ef9d9771e4ef309d10992792f00a13` — `Bind audit provenance and gate statuses`
  - `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/models.py`, `kirby2/auditlab/runner.py`, `kirby2/auditlab/store.py`
- `e84047e42f4079c83f9542b2caa66058e7051381` — ATR-19A, `Correct capped event count statistics`
  - `KIRBY2_AUDIT_TRUST_REPAIR_DEVIATIONS.md`, `kirby2/audit/model_risk_lab.py`, `kirby2/auditlab/README.md`, `kirby2/auditlab/executors/core_flow.py`, `kirby2/auditlab/runner.py`, `kirby2/auditlab/statistics.py`

## 11. Closeout gate

Before the closeout commit:

```text
code_under_audit=e84047e42f4079c83f9542b2caa66058e7051381
tracked_closeout_change=KIRBY2_AUDIT_TRUST_REPAIR_CLOSEOUT.md
generated_.kirby2_changes=none_tracked
```

The closeout commit must contain only this evidence document, use subject
`Close audit trust repair sequence`, pass `git diff --check`, and leave
`git status --short` empty. The exact resulting commit and clean status are
reported in the final handoff.

Stop after this gate. Do not begin Work Order 31.
