# Instructor and research use

Kirby2 supports local instruction, pseudonymous learner workflows, deterministic
experiments, and explicitly redacted exports. These tools organize training evidence;
they do not validate a person, strategy, market model, or real-world outcome.

## Scope boundary

- Kirby2 is a simulation and training environment.
- Kirby2 is not a broker.
- Kirby2 is not a live market connector.
- Kirby2 provides no performance guarantee.
- A reconstruction is not proof of historical market state.

## Instruction workflow

Define the lesson and its declared scenario or pack dependency before assigning it.
Keep learning and blind-evaluation partitions distinct. A learner response freezes
before debrief, answer material, or identity-sensitive joins are revealed.

The bounded local instructor demonstration produces the pseudonymous workflow fixture:

```sh
kirby2-headless instructor-demo --seed 42
```

Use the result to inspect schema, attempts, assignments, claims, and evidence links.
It is a deterministic fixture, not a learner assessment or a benchmark of instructional
quality.

## Pseudonymous identities and claims

Direct identity and pseudonymous training identity occupy separate governed areas.
Retain the minimum link needed for the declared study. A score, completion, detector,
or rubric result is evidence within its exact lesson contract only. It must not be
promoted to mastery, suitability, employability, profitability, or investment advice.

When comparing cohorts or attempts:

- bind the exact curriculum, scenario, engine, and scoring versions;
- record inclusion/exclusion and partition rules before viewing outcomes;
- keep warmup, training, validation, holdout, and terminal evaluation distinct;
- record refusals and missing evidence rather than dropping them silently;
- disclose synthetic and reconstructed inputs in every downstream report.

## Historical and counterfactual labels

`EXACT_REPLAY` identifies source-provided order events within a bounded fixture; it
does not assert completeness or authenticity beyond that source. `RECONSTRUCTION` and
`SYNTHETIC_RECONSTRUCTION` are modeled. `COUNTERFACTUAL` branches are alternatives.
`AS_OBSERVED` analysis limits the display to the declared observation boundary, while
`POSTMORTEM` may include evidence available only later.

Never treat a reconstruction, postmortem explanation, or counterfactual comparison as
proof of what occurred or why it occurred in a historical market.

## Authorized export and deletion

The privacy/export fixture exercises consent, explicit scope, redaction, clean import,
profile deletion, and retained pseudonymous evidence:

```sh
kirby2-headless instructor-export-demo \
  --fixture kirby2/instructor/fixtures/privacy_export.toml \
  --seed 42
```

Exports are local files. They are never uploaded automatically. Review the export
manifest and redaction manifest before sharing. The export may retain approved
pseudonymous evidence while separately erasing direct identity mappings; the retained
artifact must remain byte-identical after deletion.

## Reproducible research packet

A defensible local packet records:

1. engine, source, runtime, schema, scenario, pack, and scoring identities;
2. root seeds, partitions, attempts, exclusions, and refusal counts;
3. immutable run and evidence digests;
4. observation mode and historical provenance labels;
5. redaction policy, consent decision, export inventory, and recipient purpose;
6. known model and measurement limitations.

Reproducibility means the declared computation can be repeated under its bound inputs.
It does not establish external validity, market resemblance, causation, or future
performance.

## Storage and retention

Use `kirby2-headless data-paths` to inspect the governed root. Runs and evidence are
immutable areas; identity mappings are separately erasable; exports and backups are
explicit user actions. Do not store direct identity in scenario, pack, run, report,
diagnostic, or shared evidence fields that are not declared for it.

Before deleting a profile or data root, create and verify any required backup, list
the exact retained evidence references, and confirm the consent scope. Uninstalling
the application does not imply deletion of user data.
