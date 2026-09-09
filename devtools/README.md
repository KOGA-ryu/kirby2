# Local performance investigation

Use the existing Python `cProfile`/`pstats` profiler on the actual export/recovery
owners. The command has no extra dependencies and creates only text, JSON and raw
profile data. It does not import Qt, launch the application, render a graph, or
send diagnostics anywhere. This package is outside the installed product CLI.

From the backend checkout:

```sh
.venv/bin/python -B -m devtools.profile_training export \
  --source /absolute/path/to/training-data \
  --output /absolute/path/to/new-profile-folder --profile

.venv/bin/python -B -m devtools.profile_training recover \
  --source /absolute/path/to/new-profile-folder/export \
  --output /absolute/path/to/new-recovery-profile --profile
```

Export first copies only the supported training trees into the diagnostic folder;
the original folder is never passed to a mutating owner. Recovery writes a new
folder. Output must be outside the source, must not contain the source, and must
not already exist. These diagnostic folders contain copied training evidence and
notes, so treat them with the same privacy as your local training folder.

Read `profile.txt` for cumulative time, own time, call counts, and selected caller
relationships. `profile.pstats` can be queried with Python's `pstats` module; no
graph viewer is needed. `events.jsonl` contains correlated phase starts/finishes.
`summary.json` records the operation result, inclusive phase times, wall time,
process CPU time, environment, and source identity. Parent/child times overlap;
never add all phase durations to estimate total time.

Repeat the command into a different new output folder **without `--profile`** for
the wall-time comparison. Imports and preparation of the disposable input are
outside the measured operation. Lightweight phase timing remains enabled. Each
invocation is a fresh process, but this does not imply cold operating-system disk
caches. Do not compare a profiled run's duration to an unprofiled run's duration.
Use the same evidence bytes and interpreter; keep competing workloads quiet.

Deterministic profiling can add substantial overhead to code making billions of
small calls. Start with a representative bounded history. If you interrupt a
capture, retain its raw profile as partial diagnostic evidence; `KeyboardInterrupt`
and a non-PASS summary must not be counted as a completed export or benchmark.

Compare evidence inventories and payload bytes across exports. Whole export IDs
can legitimately differ: the backup contract includes source-root provenance, and
each run captures into a different temporary folder. Verify both manifests before
attributing differences solely to that location-dependent metadata.

The source snapshots detect an unsettled run if backend source changes during
measurement. A profile is diagnostic evidence, never a replacement for domain
verification. An optimization must preserve accepted results and refusal behavior.
The bounded task and stop condition are in `docs/KIRBY2_PERFORMANCE_001.md`.
