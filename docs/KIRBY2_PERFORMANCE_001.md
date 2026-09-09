# Local export/recovery performance pass

Active work order: KIRBY2-PERFORMANCE-001, revision 1, authorized by the user's
"make it so" after the profiling-tool proposal. Chapter 8 is complete; this is a
bounded performance follow-up, not release qualification or another chapter.

1. Use cProfile/pstats and local phase timings on disposable copies of the
   recorded Chapter 8 mixed evidence workload. Preserve its original bytes.
2. Identify the dominant measured cost. Make a narrow backend repair only when
   it preserves canonical evidence, reconstruction, refusal, and recovery rules.
3. Compare the same workload without profiling before/after, and run causal
   regression and hostile-boundary checks for the changed seam.
4. Retain text profiles, structured timings, source/input fingerprints, and a
   reproducible developer command. Stop with a reviewed diff and measured result.

Ownership: developer harness under `devtools`, backend evidence owners and their
audits only as justified by the profile. UI source remains untouched. No semantic
schema, recipe, simulation clock, or authoritative evidence identity changes.
Diagnostics remain outside training evidence and are not acceptance oracles.

Retained constraints: no images or image viewers, no Qt launch/import, no worker
contact, no commit/push, no release/remote qualification, and no network service.
Use the standard-library profiler first; another package is unnecessary unless
the first profile leaves an unresolved question.

Baseline: backend main `83dbded` is clean; the UI contains earlier chapter work.
Full pins, UI dirt inventory and original fixture hashes are in `baseline.json`
under the session artifact directory's `kirby2-performance-001` folder.

Measured seam adjustment P1: the bounded, interrupted diagnostic capture recorded
6.83 billion calls, including 2.67 billion `ord()` calls. Strict full-day JSON
validation accounts for about 70% of captured time. The repair therefore belongs
in `kirby2/full_day/models.py`'s text validation, not in an evidence cache. ASCII
strings and keys can bypass the NFC/surrogate scans because they already satisfy
both conditions. Non-ASCII rules, key typing, cycle detection, serialization,
canonical identities, and every caller's reconstruction/invariants remain intact.
The partial capture is diagnostic only; complete unprofiled runs establish the
performance comparison. This is an internal implementation optimization with no
contract change. Existing strict-wire and dependent execution/evidence audits apply.

## Closeout

Completed locally without committing or pushing. On the recorded 83-file mixed
training workload, one complete fresh-process run per operation/version measured:

| Operation | Baseline | Optimized | Elapsed reduction |
| --- | ---: | ---: | ---: |
| Export | 197.993 s | 112.598 s | 43.13% |
| Recovery of the same export | 203.309 s | 115.669 s | 43.11% |

Both versions reconstruct identical scientific results and evidence payloads.
Export wrapping identities differ only through existing source-root provenance;
both backup manifests verify. The recovered 300-share fill, 200-share unresolved
commitment and pending cancellation remain unchanged. No live authority resumes.

Validation: 13 focused tests and 51 dependent backend regressions passed with no
skips/errors/failures; 2,228,260 old/new text-validation comparisons and the existing
strict-wire audit passed. Source remained settled through all four measurements
and the regression gate. Original fixture hashes and UI dirt inventory are intact.

Use `devtools/README.md` for repeatable cProfile/pstats and local JSONL timing
commands. The complete evidence is in the authorized session artifact folder at
`kirby2-performance-001/ACCEPTANCE.md`. The first diagnostic capture was deliberately
interrupted; its partial profile is retained and is not a completed benchmark.
The wall-time results are local single-run observations, not statistical or release
qualification. No Qt was imported; no images, workers, network services or releases
were involved. Further optimization, repeated benchmarking and UI work were not
started after this bounded repair.
