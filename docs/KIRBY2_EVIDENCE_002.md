# KIRBY2-EVIDENCE-002 — keep the practice evidence

The user authorized Astra to build Chapter 2 directly on 2026-09-07. This replaces
the earlier planning-only role for this chapter. Stop before Chapter 3; no push,
visible Qt, real-market claims, or release qualification. Backend and UI retain
separate ownership. Preserve all pre-existing untracked backend material.

Starting pins: backend `9a6173e229d37323204f5c3b97dd3b2266de54f8`; UI
`ca07faf81079083ca9350aaba1ca3b19b6fa17b5`. Chapter 1 and the untimed F1 exception
remain unchanged. This work implements the revision-3 Chapter 2 product contract
and C2-01 through C2-06. No new model, episode recipe, CLI command, or player
command is introduced.

## Public storage seam

`kirby2.ui.practice_library` is an optional public module. Its functions are
`save_practice_evidence`, `open_practice_evidence`, `list_practice_evidence`,
`read_practice_note`, `write_practice_note`, and
`build_saved_practice_repeat_request`. Existing `kirby2.ui` exports retain their
contract; older backends report this optional feature unavailable.

Callers exchange detached public records and an opaque verified Replay source.
Only the backend resolves storage or accesses artifact bytes. An explicit root
must already be resolved, following DataPaths. Without one, the canonical platform
provider selects the data root. The UI shows the selected root in Library.

Immutable bundles occupy `EVIDENCE/practice-library/practice-evidence-<sha256>/`:

- `replay.json`: exact original governed Replay bytes, including recording,
  event tape, resolved simulation configuration, and training dependencies.
- `practice.json`: strict public attempt results, action requests, any published
  observation passage, and retained prior-attempt result history.
- `manifest.json`: strict `KIRBY2_PRACTICE_BUNDLE_V1`, version 1, original Replay
  reference, dependency digest, and simulation terminal status. The canonical
  manifest digest determines the bundle ID.

Original Replay reference, durable bundle ID, live handle, and local path remain
distinct. Disk Open invokes the existing deep simulation verifier on bytes;
it does not repopulate the old process-local artifact store. Verification rebuilds
the recorded model and checks its input/event/state chain. Practice dependencies
are retained original evidence, strictly decoded and digest-bound; opening does
not silently regrade them against a later recipe.

Save requires a finalized verified Replay and backend-authored history for the
same source. It writes to a sibling staging directory, fsyncs, reopens and deeply
verifies the staged bundle, then renames it into visibility and syncs the parent.
An identical Save is idempotent. Conflicting or incomplete bytes cannot replace a
verified bundle. Interrupted staging directories are ignored. Process death after
activation may leave a valid bundle even if the caller never received success.

Notes live in `CONFIG/practice-library/<evidence-id>.json`. They use an OS lock,
optimistic revision comparison, fsync, and atomic replacement. Clearing note text
does not erase assistance, assessment, or exposure history. Notes can be stale;
reload before retrying a rejected edit.

No database or cache index is introduced: Library rebuilds inventory directly
from verified manifests. Counts reflect retained evidence, grouped by primary
skill, rather than mastery. Bad bundles are reported separately and excluded from
verified counts. Nothing is auto-deleted during a scan.

## Ownership and lifecycle

Backend practice state retains detached successful-result history, original
requests, and any published passage. A verified saved parent can authorize a
fresh exact repeat without reviving its old handle. Its ancestry survives another
Save across process boundaries. A recipe mismatch disables Repeat while allowing
Review when the recorded model still verifies. Unsupported Replay reconstruction
refuses verification instead of substituting a newer model.

The UI adapter loads only this public module and strictly projects metadata.
Library widgets emit evidence IDs and note edits. The application owns Save,
Open, and navigation; the existing controller retains all mutable session handles.

Finished exercises expose Save attempt. The existing explicit partial-save route
can end an unfinished exercise. Both finalize first, preserve verified temporary
Replay, and claim disk Save only after activation succeeds. FINISHED/PARTIAL
exercise status is separate from COMPLETE/SAVED_PARTIAL simulation status.

Failed Saves retain immutable retry identities. Starting another practice does
not retarget them, and multiple unsaved attempts remain queued for explicit retry.
These unsaved retries are process-local. Opening another saved Replay does not
turn it into the current mutable practice. Library operations reject active,
starting, held, or quarantined authority, including failed unpublished acquisition.
Application fencing blocks nested Start, lifecycle actions, and window close.
Generation and retained-resource checks reject stale responses. Replay is prepared
before publication; failed preparation or route publication retains prior Replay.
Saved Repeat uses the existing governed Start route with a new attempt identity.

## Evidence and deliberate limits

Backend executable acceptance: `python3 -B -m unittest
kirby2.audit.practice_library`. It covers fresh-process completed and partial
Replay, actual process death around activation, idempotence, corruption, missing
or unsupported dependencies, note revisions, recipe unavailability, ancestry, and
inventory rebuild. The existing 17 episode/practice/passage checks remain the
regression boundary for unchanged C1 semantics.

UI acceptance uses `./scripts/test_replay_ui.sh 'test_practice_library_app.py'`.
It exercises the actual Save attempt button, notes, saved Repeat, clean fresh
windows, retry queues, hostile reentry, stale responses, presentation failure,
and the two required offscreen sizes. Set `KIRBY2_LIBRARY_PROOF_OUTPUT` to an
external directory to retain the renders and their manifest.

Saved evidence is local to the device. There is no cloud sync, import/export UI,
automatic backup, live-session resurrection, learning-effectiveness claim, or
human/release acceptance. Selected pace and backend-authored passage are retained;
this does not assert that every passage observation was actually displayed or
measure presentation timing. F1 remains visibly untimed. The manifest inventory
is intentionally a synchronous scan; large-library indexing and background I/O
are future performance work, not new authority over stored evidence.

Implementation and acceptance evidence are recorded in the external Chapter 2
closeout. Working changes are left uncommitted for review; Chapter 3 is not started.
