# Kirby2 Work Orders 31-40 Sealed-Baseline Attestation

This is a human-readable rendering of the canonical JSON attestation. It verifies the inherited repaired baseline only; it does not create a new scientific run or certify a new capability.

Canonical JSON SHA-256: `41b934c01794435e4477143a7894faf2f88bb7d4fd11b49c078cf962a955318d`

## Baseline identities and statuses

| Field | Value |
|---|---|
| `AUDITED_IMPLEMENTATION_COMMIT` | `e84047e42f4079c83f9542b2caa66058e7051381` |
| `AUTOMATED_STATUS` | `PASS_WITH_WARNINGS` |
| `BASELINE_ACCEPTANCE_RECORD_ID` | `acceptance-8a34abc8a267b064eaeb` |
| `BASELINE_AUDIT_PACKET_ID` | `audit-bffd05b9d74bb12b0840bcf0` |
| `BASELINE_PACKET_MANIFEST_SHA256` | `7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578` |
| `MANUAL_ACCEPTANCE_STATUS` | `PENDING_HUMAN_REVIEW` |
| `SEALED_CLOSEOUT_HEAD` | `4a962c58feab88f25e5dccfcd85c66dcf8723313` |

## Immutable inherited store

| Fact | Value |
|---|---|
| Root | `.kirby2` |
| Entries / regular files | `139` / `139` |
| File bytes | `337279773` |
| Canonical tree SHA-256 | `577ded9ae3a9a8230a9723df6838a348a2e61b3593603c8bfdd4afe2f7ee729e` |
| Packet ledger SHA-256 | `af75122eee697274c0002360b83faa26395172135c0042f73adff71b63c4b5cf` |
| Acceptance ledger SHA-256 | `b928eb1684f7802d115d93e494c2a85a70066716ba864b2f8f95ce4658ebfe39` |
| Ledger inventories | `7/7` (7/7) |
| Target occurrences | `1/1` |

## Legacy CLI compatibility inventory

- Help width: `80` columns
- Top-level commands: `55`
- Command/nested parser nodes: `62`
- Canonical parser projection SHA-256: `8b5da0c0ff5d2e769f7252104f8eb907c91c1653d3c82ebb38c2d93a72770a8e`
- Additions relative to the sealed inventory: `[]`
- Command order: `demo, latency-demo, mechanics-demo, agent-ecology, hidden-liquidity-demo, multivenue-demo, benchmark-execution, counterfactual, simulate, compare-flow, inspect-intensity, probe-intensity, features, inspect-distribution, inspect-session, measure-compare, calibrate, scenario, audit-scenarios, audit-hawkes-stability, audit-strategy-time, audit-distribution-truth, audit-historical-features, audit-historical-lessons, audit-run-store, audit-market-data, audit-latency, audit-market-mechanics, audit-hidden-liquidity, audit-multivenue, audit-execution-algorithms, audit-counterfactuals, audit-agent-ecology, audit-model-risk-lab, audit-lab, ingest-market-data, inspect-dataset, validate-dataset, replay-capability, record-run, inspect-run, query-runs, verify-run, matrix, ui, strategy, experiment, layout, replay, report, curriculum, timeline, lesson-list, lesson-run, historical`
- Sorted command names: `agent-ecology, audit-agent-ecology, audit-counterfactuals, audit-distribution-truth, audit-execution-algorithms, audit-hawkes-stability, audit-hidden-liquidity, audit-historical-features, audit-historical-lessons, audit-lab, audit-latency, audit-market-data, audit-market-mechanics, audit-model-risk-lab, audit-multivenue, audit-run-store, audit-scenarios, audit-strategy-time, benchmark-execution, calibrate, compare-flow, counterfactual, curriculum, demo, experiment, features, hidden-liquidity-demo, historical, ingest-market-data, inspect-dataset, inspect-distribution, inspect-intensity, inspect-run, inspect-session, latency-demo, layout, lesson-list, lesson-run, matrix, measure-compare, mechanics-demo, multivenue-demo, probe-intensity, query-runs, record-run, replay, replay-capability, report, scenario, simulate, strategy, timeline, ui, validate-dataset, verify-run`

### Command and nested-parser help digests

| Parser path | Help SHA-256 |
|---|---|
| `demo` | `5ac1ba8761e7f7c7c6d6e964e6ad2d54142e0835c49b41ebb0f1f01ef749e632` |
| `latency-demo` | `5dc3ca7e6e3be09b9a752ac5bb4b5e0e20df4eddc4936d32a9eb0b7bacfadb06` |
| `mechanics-demo` | `7f41f49f065622b032c21873310977e1521c7936e3c13c620b4d93f2b72e8c51` |
| `agent-ecology` | `d41e911049b9fe29ef9f70644ba6a87176e46517e7a7cc4eb8941425d33cb79f` |
| `hidden-liquidity-demo` | `6d25bc3e0687d29a1441d00ecda08ca3420bac21a92004302f6109e832a8fffd` |
| `multivenue-demo` | `c72ff9da70f9a2fed9f96b0371a7df9c91429db9e40bc609be972fa755a79bec` |
| `benchmark-execution` | `e9aa1dfc7c1473744285bf8483b5e187f40d1eee7592f3dc62580f8fd0e68dee` |
| `counterfactual` | `45ce5b60f69634a8e57e924b402e39957fff055a87d16205a1d4c15605c37bb4` |
| `simulate` | `73d682001bfafc0783b2ae5fb7520d356f52c4aa189631e7bcb38eac28a1d08a` |
| `compare-flow` | `df28ebc68c3041daa409888599039f00e38e555e5487405d73679c07e9b26a0e` |
| `inspect-intensity` | `e0cb344380f24208c259941f2befc1a744e6dc4f29d0efe0bca183af9d3064c7` |
| `probe-intensity` | `7ca40bfa4e9af2b660f99ef387a92c1174750c2b754e69ab4598667d1f76559a` |
| `features` | `4d3030e589ddc76836531e7f9adbc4113d2874a13e9c23f81b6405dec21865e9` |
| `inspect-distribution` | `7a47659e7945baa087d48740cc1211a330bcfe21883388cbcfc0e02e96f85b65` |
| `inspect-session` | `09a56d5d41b055a691d8f8034c2c18f19ef009889176575972b0370daa742c8e` |
| `measure-compare` | `5f4b79e7d9a450fb8538dfe82c04ac43856e4c9e4f9dc06a84ef7bb3d6d2733f` |
| `calibrate` | `9053260639b6b1dd0b1cb4877e62d8e45e5662450e79269be6623a012eb96dd9` |
| `scenario` | `e308bb5b9a65a841801ee12fc406d11e382485c9af10ce53ed4983d5b1fb1de7` |
| `audit-scenarios` | `371178c6d11a9f4ef4e14d91a1eb8d6bfd96c815e9493d906ff2c82e482d8d58` |
| `audit-hawkes-stability` | `c027fdb4f591d430568e59adb5ec49a64f587b4e4cfc8c6f82055d6e72d67d04` |
| `audit-strategy-time` | `968656d4b727dc6c23edcac7d7171cdf2489d992dc623bbe90b3e31ed867c336` |
| `audit-distribution-truth` | `9deba24336cfdd7051f36f29e1572909baab2fd9ce32f191a4452c4c972a32e7` |
| `audit-historical-features` | `4688cdd08a00cc59127ec3e7ffad9fc70ec2c05eb9419cfbfd2f791878d4ac2f` |
| `audit-historical-lessons` | `e48518ada87f26b41d020d1ca6e2f64d878c234a831b38ef2fdbf9efaa883ac1` |
| `audit-run-store` | `a5573ced8f2a7b28242142603f474796d8f4d3178ccf58149f044528132d283a` |
| `audit-market-data` | `677011799c573f29ed98144c40e09b6712e7f623cf17edb73d6b5d9520d7149b` |
| `audit-latency` | `0a627637590aff96d88406aa0527aefaf8dc073565351e95a1e570d3ef0680fc` |
| `audit-market-mechanics` | `1ab13e99f365c890d8070f0ab3984f41ad5e6122637a1cc642b59358ad32965e` |
| `audit-hidden-liquidity` | `da2ffb235fb50c19019a85bdf5a37cabe937e18965e1d98baf0861f144f77b18` |
| `audit-multivenue` | `f7ccbec480a93198f5db6813913fba769765c14f350d4c1112fd3c0819ce5028` |
| `audit-execution-algorithms` | `878bb491373d072c331534016fadf4543b92be6f9ab77eb726ff498e68e46ce0` |
| `audit-counterfactuals` | `be47c268a3abff71b2a84c7094a8d4c89119add0f9945d1e4223f254e188a714` |
| `audit-agent-ecology` | `0e5457027899035f86b300d2bb8cdb093ac9b0abad4a19e7f88803a299f85999` |
| `audit-model-risk-lab` | `4774fa1004fecbfd6afde431cf051a688d06484d19169c92aa83197a30441796` |
| `audit-lab` | `172225619de2b317f4ad990a5953350b6611b9ba97833c968d372a89f501e232` |
| `ingest-market-data` | `fe0fb62e51aae86217e0613c0eb84d8ec6408418ed11aa10704c66717f2c16b1` |
| `inspect-dataset` | `f7a4f1057fccf6415e4b713409f274b4500b4240a97953758c061e0f54d3552e` |
| `validate-dataset` | `3639ecb849c871a8ce00124612ac79b1d6f348f62af5cd5df25b9a459547f772` |
| `replay-capability` | `d257f3f1835b9c6d2a55ea9eaf939bf950714ab4cee3a4f7931416b5776afe2b` |
| `record-run` | `1c457f83a876ad2026f7f910d5bb6beab62afdfddd36b3b87560896448b816ad` |
| `inspect-run` | `1304b586785f639d8ea18360767a7ab5a713c65c3328469c0c6aabb1b5ba35eb` |
| `query-runs` | `99b95fa99ce43ebfbada2c7f74a509d269ef010bbae3e74cbd4753ab5fef17c5` |
| `verify-run` | `57c463b021e6d4b78f1d85e6e4bd624af1cab48bc0e97419f73fe8301384f6f4` |
| `matrix` | `f1b8915f2f8fc29ba16de5d4ce9e0cdfdd7e5dd3de1d667ecd7f8b7d27763ae7` |
| `ui` | `ebd824df7f6e9b3e89ca0e675dedf7f201bdbc427c9e4bfb8fb9f7730be1c5b6` |
| `strategy` | `51e3d427483199ac039f8751aedcf64f13d19d64a508fdf59ff05b485d9c0d0e` |
| `experiment` | `d005592da5d636647bcb507e920395987e974bce069ec791cd7e98565eaaf97a` |
| `layout` | `9d3efd9d99cf6c38a60b4306f681fe9d78437d92f1508abfa0b6a6ee46af2bd1` |
| `layout list` | `45247ac750303c2056759c33da37dc0cb15b593660c09cd04bf2a94183a066ab` |
| `layout show` | `1d7de90d0be8e62665bb1b942f26f447824443024eb055fb2a63323f5ad2b855` |
| `layout save` | `81617f731f3597968ae6d54d5a2780d2d86fded5bd9b17229b27fbe811c5ac17` |
| `replay` | `1c2fe998479450d22d1f06286dafffb2c67c35cbe2c0788b3533746926e29008` |
| `report` | `53529ef63040b2961b08978f909ebbd5ce754d00cdab1afed22e8c08cf9ea979` |
| `curriculum` | `3e30e02a32f21585afe7099eecd908777c94ba61fd07a65ba87f4a09e93fe463` |
| `curriculum list` | `84e68b7ea7f311fafff25c49f0813b92c76b2282cbdb7f3d13160529a29815e8` |
| `curriculum run` | `7fa10e922bca349653079c759561c564c58b90ee6bc6867c3a39460b08707776` |
| `timeline` | `6c3457e8a498bab3a0cbeb62fc133df5feebbabbe5f5a56060378045fee2e3c8` |
| `lesson-list` | `31a3a6b018ba7889b1259322ddb83c2262a94d5a23f449edf08a450d2c64f753` |
| `lesson-run` | `5d81ea0a03ce8a1e47c42fd268a52a2baf72a1219d6df864f676d1995facfbc3` |
| `historical` | `3354f257120db1b96d8a4a2176bf5806961b580d95e47a264b94aea42d918122` |
| `historical list` | `d367e62959ad17c733e253b7881e7ea14db5fa20446f94662f847f88e0baff12` |
| `historical run` | `3caecb4874797ace8f71191013dafcd2afd0c9b206bd5ad55f3f38d476fa1027` |

## Deterministic smoke evidence

| Argv | Exit | Stdout SHA-256 | Stderr SHA-256 | Terminal status | Byte pinned |
|---|---:|---|---|---|---|
| `["demo","--seed","42"]` | `0` | `4657d132082fc48ca2d354b50d591267d542a7fcb03401d3509f91a3c1d4995a` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `RUNTIME_INVARIANTS PASS` | `true` |
| `["audit-scenarios"]` | `0` | `74b5b8e18f916990936377b08025a8e51560b7d0b28b81cf33645cb3995124a9` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `SCENARIO_AUDIT PASS accepted=12 failures=0` | `true` |
| `["inline-live-replay","--seed","771","--budget","7"]` | `0` | `14914d62620fcd7bd1e7656516c65b64db688bb5dacdbf51d5872e801d4ea92e` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `LIVE_REPLAY PASS lanes=CORE_FLOW,MECHANICS,LATENCY,FRAGMENTED,ECOLOGY,ALGORITHM,FAULT digest=5a50577e7d8b64923946762d6efd949eb531da1306ca41ad1e3b217f30bc8b6d` | `false` |
| `["audit-model-risk-lab"]` | `0` | `0989cbd1a7c091a97ae8f8924db65fbf2096a34ec08ad77816c4e33dca8dd660` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `MODEL_RISK_LAB_AUDIT PASS cases=21 failures=0` | `false` |

The byte-pinned compatibility set is exactly `demo --seed 42` and `audit-scenarios`. Live replay records stable observed bytes, while model-risk output is status-pinned but not byte-pinned across legitimate source-provenance changes.

## Runtime

- Python: `3.14.3`
- DuckDB: `1.5.5`
- System: `Darwin`
- System release: `25.5.0`
- Machine: `arm64`
- Python implementation: `CPython`
- Cache tag: `cpython-314`

## Inherited warnings

- `calibration_train_vs_holdout`
- `distribution_drift`
- `unstable_hawkes`

## Limitations

- `BASELINE_SCOPE_ONLY` — This attestation verifies only the inherited repaired baseline and its pre-existing capability surface.
- `INHERITED_MANUAL_REVIEW_PENDING` — The inherited acceptance record remains PENDING_HUMAN_REVIEW; automation has not granted human acceptance.
- `INHERITED_STATISTICAL_WARNINGS` — The inherited packet retains calibration_train_vs_holdout, distribution_drift, and unstable_hawkes warnings.
- `NO_NEW_CAPABILITY_CERTIFIED` — K2X-01 certifies no capability added after the audited implementation commit.

## Attestation status

The inherited automated status remains `PASS_WITH_WARNINGS`; manual acceptance remains `PENDING_HUMAN_REVIEW`. The immutable store, 7/7 ledgers, parser inventory, and four smoke commands verified without changing `.kirby2`.
