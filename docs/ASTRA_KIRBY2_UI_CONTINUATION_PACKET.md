# Astra continuation packet: completed simulation to Replay Studio

Status: `READY_FOR_CONTINUATION`
Packet date: `2026-09-04`
Packet purpose: operational continuation only
Canonical contract authority:
`/Users/kogaryu/Documents/ChatGPT/kirby2/docs/SIMULATION_UI_BACKEND_HANDOFF.md`

This packet tells the next Astra task where the standalone Qt UI build stopped and
what remains. It does not replace, amend, or reinterpret the canonical backend/UI
contract. If this packet conflicts with the canonical handoff, committed source, or
the user's current instruction, those authorities win.

## 1. Task for Astra

Finish one bounded UI slice in the standalone UI repository:

```text
completed governed synthetic simulation
    -> exact COMPLETE_ONLY finalization
    -> public artifact reference
    -> backend verification receipt plus private source handle
    -> private verified Replay provider
    -> validated initial Replay frame
    -> existing ReplayLibraryWorkspace
    -> atomic replacement of the existing Review/Replay surface
    -> backend-owned Replay navigation
```

Do not redesign the application, add a second Replay route, or change backend
semantics. Preserve the current paper/editor visual language and use the existing
Replay Studio components.

## 2. Repository and authority boundaries

### UI repository: the only production repository Astra should edit

```text
/Users/kogaryu/Documents/ChatGPT/kirby2-ui
```

- Branch: `main`
- Current committed HEAD:
  `e2beab0d9819b4a599bd769055ac722c232c7786`
- HEAD message: `Prepare verified simulation Replay handoff`
- Parent live-interaction commit:
  `a8a7e911dc400083996ca8d35089d089ff3f2e51`
- No Git remote is configured.

Read `/Users/kogaryu/Documents/ChatGPT/kirby2-ui/AGENTS.md` before editing.

### Backend repository: read-only contract and fixture authority

```text
/Users/kogaryu/Documents/ChatGPT/kirby2
```

- Canonical backend/UI handoff-document commit:
  `b812514bfad858e1385c5e7e60e9dea2ad64a2b9`
- Production integration pin:
  `655ccf495b015f2067f11d63adcf3dd63e4e4609`
- Deep artifact-verification pin:
  `49b8854d7739ad59cd3109d319c946e643c3c193`
- Finalization/artifact pin:
  `ccfc9669cc46c29dca226bb5481b13210394d2ca`

Treat the backend checkout as entirely read-only. Do not update its handoff record
from the UI task. Report the final UI commit to the backend/source task instead.

The backend owns:

- simulation and matching truth;
- live and Replay run identities;
- finalization and immutable artifact bytes;
- verification of recording, event-tape, and artifact digests;
- cursor selection and all Replay navigation semantics.

The UI owns only strict public projections, operational request correlation, Qt
presentation, and local view state.

## 3. Committed foundation and working-tree snapshot

The Replay-handoff foundation is committed in:

```text
e2beab0d9819b4a599bd769055ac722c232c7786
Prepare verified simulation Replay handoff
```

That commit contains exactly:

```text
M src/kirby2_ui/backend.py
M src/kirby2_ui/review_workspace.py
A src/kirby2_ui/simulation_finalize_contract.py
M tests/test_backend.py
M tests/test_simulation_backend_inventory.py
A tests/test_simulation_backend_replay_adapter.py
A tests/test_simulation_finalize_contract.py
```

The UI working tree after that commit contains only this unrelated user-owned file,
which must remain untouched and unstaged:

```text
?? CHANGELOG 2.md
```

The backend also contains unrelated untracked environment, evidence-copy, agent-map,
and wheelhouse material. Do not touch or stage any of it.

## 4. What is already implemented

### 4.1 Strict finalization and Replay handoff projections

New file:

```text
/Users/kogaryu/Documents/ChatGPT/kirby2-ui/src/kirby2_ui/simulation_finalize_contract.py
```

It is Qt-independent and backend-import-independent. It exports immutable strict
projections for:

- `ReplayArtifactRefProjection`;
- `SimulationRunResultProjection`;
- `SimulationFinalizeResultProjection`;
- `ReplayArtifactVerificationReceiptProjection`;
- `SimulationReplayProviderResponseProjection`.

The public projectors are:

- `project_replay_artifact_ref()`;
- `project_simulation_run_result()`;
- `project_simulation_finalize_result()`;
- `project_replay_artifact_verification_receipt()`;
- `project_simulation_replay_provider_response()`;
- `replay_run_id_for_source()`.

The module already validates exact V1 fields and schemas, canonical identity forms,
content-derived IDs, live-to-Replay run mapping, final cursor/frame consistency,
typed availability, finalization mode, artifact and receipt identity, provider
response echo, PLAYBACK/NAVIGATION payload nullability, and complete Replay-frame
projection. It recursively freezes retained public data and returns detached plain
values from `as_dict()`.

Important caller obligations remain:

- always pass the held terminal live frame as `origin_frame` and `final_frame` when
  projecting a successful completed-run finalization;
- always pass both the submitted artifact projection and its run result when
  projecting the verification receipt;
- always bind a provider response to the pending request ID and the Replay store's
  actual source generation;
- let the existing Replay transport/controller enforce request-operation kind and
  destination semantics after the five-field provider envelope is projected.

### 4.2 Lazy backend adapter

Modified file:

```text
/Users/kogaryu/Documents/ChatGPT/kirby2-ui/src/kirby2_ui/backend.py
```

`KirbyBackend` now inventories and installation-origin-checks these public modules:

- `kirby2.ui.simulation_finalize_facade`;
- `kirby2.ui.simulation_replay_facade`;
- `kirby2.ui.simulation_replay_provider`.

It exposes:

```text
finalize_simulation_run(
    handle,
    source_run_id,
    origin_frame_id,
    origin_cursor_id,
    mode,
) -> detached public mapping

resolve_replay_artifact(reference_mapping)
    -> (private verified source handle or None, detached public receipt mapping)

build_replay_provider(private verified source handle)
    -> adapter-private wrapper
```

The wrapper deliberately exposes only:

```text
initial_frame() -> detached public mapping
respond(exact_request_mapping) -> detached public mapping
```

Inputs and outputs are deep-detached. Backend decode errors become
`BackendCompatibilityError`; backend integrity errors become
`BackendIntegrityError`. Opaque handles retain identity and never enter public state.

The adapter does not decide whether a `None` source agrees with receipt status. The
controller must require a non-null private source if and only if the strictly
projected receipt is `AVAILABLE`.

### 4.3 Atomic Review-surface replacement

Modified file:

```text
/Users/kogaryu/Documents/ChatGPT/kirby2-ui/src/kirby2_ui/review_workspace.py
```

`ReviewWorkspace.replace_surface(surface_id, view)` now performs owner-thread,
same-index replacement with rollback. It preserves the logical current surface and
deletes the old widget only after success.

This method currently has no caller and no dedicated test.

### 4.4 Existing Replay machinery to reuse

Do not rebuild these responsibilities:

- `project_replay_library_document()` and `ReplayLibraryCatalog` in
  `src/kirby2_ui/replay_library_model.py`;
- `ReplayLibraryWorkspace.open_entry()` and `bind_navigation_provider()` in
  `src/kirby2_ui/replay_library.py`;
- `ReplayTransportRequest.as_dict()` in `src/kirby2_ui/replay_controller.py`;
- `ReplayTransportResponse.playback()` and `.navigation()` in
  `src/kirby2_ui/replay_transport.py`;
- `ReplayNavigationProvider` in `src/kirby2_ui/replay_transport.py`;
- existing Replay controller/store settlement, stale-response gates, and atomic
  multi-surface publication.

On a fresh `ReplayLibraryWorkspace`, `open_entry()` installs source generation `0`.
Read and use the store's actual generation. Do not maintain a second invented Replay
generation counter.

## 5. What is not implemented

The draft is not connected to application behavior yet.

- `SimulationStartBackend` in `src/kirby2_ui/simulation_start.py` ends at
  `read_current_simulation_frame()`.
- `SimulationStartController` has no finalization fence, finalization transition,
  verification lifecycle, private source/provider ownership, or Replay response
  method.
- `KirbyMainWindow._build_workspaces()` still mounts `ReviewPage` directly at the
  existing `review` shell route.
- `KirbyMainWindow._present_simulation_interaction()` still ends a terminal frame
  with `Simulation complete - Finalize and Replay are not connected`.
- No one-entry `ReplayLibraryWorkspace` is built from the verified initial frame.
- No `ReplayNavigationProvider` callback sends exact backend requests.
- No atomic Review/Replay replacement or route switch occurs.
- No theme/text-scale propagation or failed-preparation cleanup exists for the new
  Replay workspace.
- No controller/app wiring tests or complete live-backend integration test exists.
- Reset, close, and explicit partial-save flows remain outside this slice.

## 6. Required implementation sequence

### 6.1 Add a pure controller-owned finalization lifecycle

Extend the controller protocol with the three adapter operations. Keep all private
handles in the controller/adapter, never in `SimulationRunState`, a Qt widget, a
signal, or a public transition.

Add a non-reentrant, locally idempotent completed-run transition. It should:

1. require the controller to be active with a held frame whose
   `cursor.run_state == COMPLETE`;
2. capture that frame and its exact `source_run_id`, `frame_id`, and `cursor_id`;
3. call `finalize_simulation_run()` once with those values and the literal
   `COMPLETE_ONLY`;
4. strictly project the result against the captured frame;
5. return a typed unavailable transition without changing the visible live frame
   when finalization is unavailable;
6. pass only `run_result.replay_artifact.as_dict()` to artifact resolution;
7. project the receipt against both the submitted reference and run result;
8. require `(private_source is not None) == receipt.available`;
9. call `build_replay_provider(private_source)` only when the receipt is available;
10. call the private provider's `initial_frame()` and validate it through the
    existing Replay presentation projector/library projector;
11. require initial Replay `source_run_id == run_result.replay_run_id`;
12. require initial Replay `source_event_sha256 == artifact_ref.artifact_sha256`;
13. require `AS_OBSERVED` with `MICROSCOPE_AS_OBSERVED_V1`;
14. require the initial provider frame to be paused if the public frame exposes its
    playback state, matching the current provider contract;
15. retain the private provider and detached initial-frame bytes only after every
    check succeeds;
16. clear live dispatchable actions and publish an explicit terminal/finalized
    controller condition so a finalized backend handle cannot still appear mutable;
17. return the same prepared result on a repeated local completion callback without
    invoking backend finalization again.

If malformed or contradictory data appears after a possibly mutating finalization
call, quarantine/fail closed. Keep the last complete live frame visible but mark it
stale; do not submit another mutation from its origins.

The completed-run finalization mode and provider observation mode are intentionally
different:

```text
finalize mode:       COMPLETE_ONLY
provider mode:       AS_OBSERVED
provider policy:     MICROSCOPE_AS_OBSERVED_V1
```

Do not substitute `AS_OBSERVED` for the finalization mode.

### 6.2 Prepare Replay completely off-stack

Only after the controller returns a fully verified initial frame:

1. call `project_replay_library_document(payload, source_label=...)`;
2. make a one-entry `ReplayLibraryCatalog`;
3. construct `ReplayLibraryWorkspace` with current theme and text scale;
4. call `open_entry(entry.entry_id)` and require success;
5. read/assert its actual Replay store source generation (`0` for this fresh source);
6. construct a `ReplayNavigationProvider` parented to that workspace;
7. bind it only after the entry is open;
8. retain the workspace/provider through the Qt ownership graph;
9. replace the prepared placeholder with
   `review_workspace.replace_surface("replay", prepared_workspace)`;
10. only after successful replacement, select Review/Replay and enable the existing
    shell `review` route.

If any preparation step fails, destroy the new off-stack workspace and leave the
prior Review surfaces and the completed live frame unchanged.

### 6.3 Mount the existing two-surface Review workspace

In `KirbyMainWindow._build_workspaces()`:

- keep `ReviewPage` as the evidence surface;
- create a small disabled/planned Replay placeholder;
- create `ReviewWorkspace(review_page, replay_placeholder)`;
- mount that wrapper at the existing shell workspace ID `review`;
- do not add a second shell route.

Before legacy `finish_session()` or `open_recording()` presents evidence, explicitly
select the Review `evidence` surface so a prior Replay surface cannot remain visible.

When preferences change, propagate theme and text scale to a prepared live
`ReplayLibraryWorkspace` if one exists.

### 6.4 Bridge provider responses through existing transport gates

The callback receives one exact `ReplayTransportRequest`. It must:

1. call the private controller bridge with `request.as_dict()` exactly;
2. project the returned five-field envelope with
   `expected_request_id=request.request_id` and
   `expected_source_generation=request.source_generation`;
3. materialize a fresh plain mapping with the projection's `as_dict()` before using
   the response factories, because retained projection payloads are immutable
   mappings while the transport factories require exact plain dictionary roots;
4. either add an `expected_kind` input to the envelope projector or explicitly bind
   PLAY/PAUSE to `PLAYBACK` and all navigation operations to `NAVIGATION`;
5. convert `PLAYBACK` with `ReplayTransportResponse.playback()`;
6. convert `NAVIGATION` with `ReplayTransportResponse.navigation()`;
7. return the governed response to `ReplayNavigationProvider`.

Do not directly call Replay store commit methods, controller settlement methods, or
`set_navigation_connected(True)`. The existing Replay workspace owns those steps and
already binds response kind to PLAY/PAUSE versus navigation requests.

## 7. Failure behavior

Keep these outcomes distinct:

| Outcome | Required behavior |
| --- | --- |
| Finalization `UNAVAILABLE` except source mismatch | Keep terminal live frame; show exact typed reason; do not invent a resnapshot/retry policy or enter Replay |
| Finalization `SOURCE_RUN_MISMATCH` | Quarantine/fail closed because the held handle and visible source authority disagree |
| Verification `UNAVAILABLE` | Keep terminal live frame; show exact typed reason; do not build provider |
| Malformed/digest/identity contradiction | Quarantine/fail closed; preserve stale complete frame; no more live mutation |
| Provider construction/initial-frame failure | Do not install a Replay surface; preserve prior surfaces |
| Replay request failure | Existing Replay transport refuses it and disables/recovers controls as already designed |
| Surface preparation/replacement failure | Delete prepared replacement; retain previous Review route and surface |

`SOURCE_RUN_MISMATCH` is an integrity problem. Other valid typed unavailability must
not be collapsed into a generic exception or an empty Replay.

## 8. Explicit exclusions and forbidden shortcuts

This slice does not authorize:

- backend edits;
- raw artifact bytes, embedded recording bytes, component payloads, event tapes, or
  private Replay inventory in the UI;
- a backend source handle or provider object in public/Qt state;
- UI-derived cursor positions, scans for next events, or manufactured navigation;
- a new top-level shell route;
- direct `ReplayFrameStore` commits from `app.py`;
- direct Replay controller settlement from `app.py`;
- `set_navigation_connected(True)`;
- POSTMORTEM or reveal controls for this AS_OBSERVED-only provider;
- reset/close integration or partial-save UI;
- live brokerage, real order submission, credentials, telemetry, updater, or network
  market-data behavior;
- a visible Qt launch during this implementation pass.

The three new Replay backend modules are now part of `KirbyBackend`'s global lazy
inventory. Therefore backend commit `655ccf...` is currently the minimum compatible
backend. Do not silently claim compatibility with an older backend unless the loader
is deliberately refactored and tested for optional Replay capability.

There is a known lifecycle edge after successful finalization: the current
`_simulation_run_active` window-close/new-practice guard still treats the run as
active, while `SimulationStartController` cannot start another run and the backend
handle is finalized. Do not casually clear that flag or revive the handle. Either
keep reset/close/new-practice explicitly deferred and report the limitation, or land
a separately justified lifecycle transition with its own tests.

## 9. Tests Astra must add

### Pure controller tests

- exact call sequence: finalize -> resolve -> build -> initial frame;
- literal `COMPLETE_ONLY` and exact visible origins;
- local idempotence on repeated completion notification;
- each typed finalization and verification unavailability;
- source/receipt nullability disagreement;
- finalization origin mismatch;
- initial Replay run/artifact/mode/policy mismatch;
- malformed provider frame and backend exception quarantine;
- private provider never appears in public state.

### Provider response tests

- PLAY and PAUSE yield PLAYBACK responses;
- event, fixed-time, and jump operations yield NAVIGATION responses;
- unavailable navigation carries no frame;
- available navigation carries one complete frame with matching cursor;
- wrong request ID, generation, kind, extra field, origin, or cursor is refused;
- exact request mapping is passed once without UI rewriting.

### App/architecture tests

- terminal frame is installed before finalization begins;
- only the existing `review` shell route is used;
- evidence and Replay remain separate surfaces inside `ReviewWorkspace`;
- Replay is prepared before `replace_surface()` and navigation;
- failure leaves the previous Replay surface installed;
- legacy recording/evidence paths select the evidence surface;
- theme/text scale reaches the prepared Replay workspace;
- no private backend Replay imports in UI presentation modules;
- no app-level store settlement, cursor derivation, or boolean navigation enablement.

Replace the obsolete negative assertions in
`tests/test_simulation_app_wiring.py` that currently require the completion path to
contain no finalization or Replay wiring.

### Pinned cross-repository compatibility test

Extend `tests/test_simulation_backend_golden.py` or add a sibling pure test that
archives/loads exact backend commit `655ccf...` and executes in a child process:

```text
start
-> play
-> advance to the configured completion time
-> finalize with COMPLETE_ONLY
-> resolve artifact reference
-> build provider
-> initial_frame
-> one EVENT_STEP response
```

Project every public response through the UI-owned strict contracts. Keep backend
imports isolated to the child process and do not import Qt.

Add a protected Review-surface replacement test only if needed. If Qt coverage is
necessary, use the repository's protected launcher; never instantiate raw
`QApplication`.

## 10. Verified baseline before Astra changes anything

No Qt application was launched during this audit.

Current no-Qt results:

```text
simulation-focused suite: 188 passed
backend adapter suite against sibling backend: 26 passed
new finalization contract: 9 passed
new Replay adapter: 7 passed
adapter inventory: 12 passed
git diff --check: clean
```

Safe pure simulation command:

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/Users/kogaryu/Documents/ChatGPT/kirby2-ui/src \
  /Users/kogaryu/Documents/ChatGPT/kirby2-ui/.venv/bin/python -B \
  -m unittest discover \
  -s /Users/kogaryu/Documents/ChatGPT/kirby2-ui/tests \
  -p 'test_simulation*.py' -v
```

Safe backend-adapter command:

```sh
env PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/Users/kogaryu/Documents/ChatGPT/kirby2-ui/src \
  KIRBY2_ROOT=/Users/kogaryu/Documents/ChatGPT/kirby2 \
  /Users/kogaryu/Documents/ChatGPT/kirby2-ui/.venv/bin/python -B \
  -m unittest discover \
  -s /Users/kogaryu/Documents/ChatGPT/kirby2-ui/tests \
  -p 'test_backend.py' -v
```

If offscreen Qt coverage is genuinely needed after the pure logic is complete, use
only:

```sh
./scripts/test_replay_ui.sh 'test_review_workspace.py'
```

Do not run the application or a visible window unless the user later asks.

## 11. Completion and commit gate

Before committing:

1. rerun the pure simulation suite;
2. rerun the backend adapter suite with explicit `KIRBY2_ROOT`;
3. run the pinned end-to-end child-process compatibility test;
4. run `git diff --check`;
5. inspect `git status --short --branch` in both repositories;
6. confirm the backend working tree is unchanged;
7. stage only the UI slice and explicitly exclude `CHANGELOG 2.md`;
8. inspect the staged diff before commit.

Suggested UI commit message:

```text
Connect completed simulations to Replay Studio
```

After committing, report:

- exact UI commit SHA;
- files changed;
- exact test commands and counts;
- confirmation that Qt was not visibly launched;
- confirmation that the backend was not modified;
- confirmation that `CHANGELOG 2.md` remains untouched;
- explicit reminder that reset, close, and partial-save UI remain deferred.

## 12. First actions for Astra

```text
1. Read UI AGENTS.md and the canonical backend handoff.
2. Inspect both repository statuses; preserve every unrelated file.
3. Read the seven current-slice UI files before editing.
4. Implement and test the pure controller lifecycle first.
5. Add the pinned backend full-chain test.
6. Wire the existing Replay Library and ReviewWorkspace only after the pure boundary
   is proven.
7. Do not launch Qt.
8. Commit only when the entire bounded slice is green.
```
