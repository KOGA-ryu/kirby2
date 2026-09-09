# Local installation and daily use

Kirby2 is a synthetic execution trainer. The current product supports short
control/observation drills, saved practice, market-mechanism comparisons, delayed
execution, declarative playbooks, bounded Lab studies, three-instrument Radar and
source-linked practice paths. It does not connect to a broker or real market.

## Installation without a checkout

The Chapter 8 engineering path is CPython 3.14 on macOS arm64, with the exact two
built product wheels plus DuckDB 1.5.5 and PySide6/Shiboken6 6.11.2 wheels. The
Chapter 8 evidence records their hashes. This is an installed-package engineering
proof; these development wheels are not signed or release-qualified artifacts.
The project version remains 0.1.0, so compare wheel hashes, not only version text.

Use a new virtual environment and a local directory containing that exact wheel
set. No editable install, PYTHONPATH, source checkout, network or credentials are
needed at runtime. Substitute actual wheel and folder paths below:

```sh
python3.14 -m venv ~/kirby2-runtime
~/kirby2-runtime/bin/python -m pip install --no-index --find-links /path/to/wheels /path/to/wheels/kirby2-0.1.0-py3-none-any.whl /path/to/wheels/kirby2_ui-0.1.0-py3-none-any.whl
~/kirby2-runtime/bin/python -m pip check
~/kirby2-runtime/bin/kirby2-ui --data-root /absolute/path/to/my-training
```

The installed entry point prepares the existing protected Qt runtime before Qt
imports. Normal launch starts at Home. `--data-root` selects persistent training
storage; omitting it uses the backend's platform application-data provider.
`--kirby-root` and `KIRBY2_ROOT` remain explicit development overrides; unset them
when validating an installed backend. Desktop preferences have their own platform
configuration location and are not included in a training export.

1. From Home, open Practice and choose an exercise. F1 is untimed; F2/F3 offer
   backend-authored observation passages at 1× or 0.5×. Guided and Unassisted
   attempts keep separate evidence.
2. Complete a decision, inspect its debrief, and save; or explicitly save partial
   evidence. Library opens saved Replay after restart and keeps editable notes
   separate. Save must confirm disk publication; temporary Replay is not a save.
3. Market workbench offers versioned fictional profiles and a bounded comparison.
   Playbook Lab validates its supported source/form fields, freezes variants and
   cases, and keeps every completed/failed/cancelled/missing cell.
4. Radar presents three independent fictional books, explicit selection/arming,
   delayed reports and a single exposed instrument. Freeze, save and review both
   declined candidates and unresolved partial execution. Close releases authority;
   it does not turn a pending cancellation into a confirmed one.
5. Practice path records local clinics and reserved comparisons. It makes no
   mastery, reaction-time, causal learning or real-market-transfer claim.

## Export, verification and recovery

In Library, open **Training folders**. Inspect rebuilds the inventory from saved
records. Enter a NEW export folder, acknowledge the disclosure/notes notice and
choose Export training. Browse selects an existing folder; edit the field to add
a new child name for export or recovery. Verify export accepts an existing export
folder. Recover to new folder requires a destination which does not exist, then
shows a launch command for that folder. It never switches or overwrites the
current folder automatically.

Exports wrap the existing governed backup format and its atomic restore owner.
They carry supported C2–C7 evidence, embedded recipe/checkpoint dependencies,
original units/versions, all study dispositions, reveal history and separate notes.
They exclude live handles, pending publication debris, cached indexes, personal
identity mappings, UI preferences and unrelated older research datasets. They are
NOT ENCRYPTED. Notes may contain personal text.

Export discloses reserved case seeds. The source and recovered history both mark
those cases revealed; later use cannot claim they are unfamiliar. End an active
curriculum attempt before exporting. A failed export may already have written
conservative disclosure records. This prevents a failed or renamed export from
becoming a way to restore a blind-assessment claim.

A new-version install should be a separate runtime. Keep the old runtime/data,
export from it, verify/recover with the new runtime into a NEW folder, then select
that folder explicitly. An incompatible chain is refused; no silent migration,
recipe substitution, auto-updater or overwrite is performed.

| Failure | Supported recovery |
| --- | --- |
| App exits during mutable practice | Restart at Home. Unsaved live authority is gone; a saved frozen attempt can be reviewed, not resumed as a live order. |
| Save interrupted before publication | Original saved entries remain; pending debris is reported/excluded. Retry from retained temporary evidence if that process still owns it. After process loss, do not invent the missing save. |
| Save completed before process loss | Rebuild Library inventory and reopen its verified immutable record. |
| Lab exits before all cells finish | Open the saved plan: missing cells are INCOMPLETE. A retry is a separately identified, disclosed attempt. |
| Disposable index lost | Refresh Library or Inspect training folders; original evidence is authoritative. |
| Truncated/incompatible/tampered evidence | Refuse it with a reason; retain the original folder and compatible entries. Recover a known verified export into a separate folder. |
| Export/restore fails or publication is uncertain | Inspect/verify the named destination before retrying. Existing folders are never overwritten by retry. |
| Active curriculum clinic after exit | Refresh Practice path to inspect durable instructions. No engine/order continuation is manufactured. End it before exporting. |

## Supported workload and explicit limits

Radar V1 has three instruments over eight simulated seconds, observed at 500 ms
cuts. Its recipe combines 0.1 stochastic additions/second/side/instrument with
scheduled ordinary orders and deep-book additions. This is not a calibrated
market or public trade-volume feed. Continuous UI processing permits four queued
cuts; further delay records OVERLOAD and pauses the entire world. It does not drop
semantic events. The hard guards are 16 learner orders, 128 candidates, 512
operations and 1,000 world events. Capacity/integrity failures retain cleanup
ownership. Frozen evidence remains distinct from delivered client knowledge.

Lab has at most four distinct variants, eight explicit cases and sixteen cells,
with one executing cell. Training-folder jobs similarly have one owner and block
new sessions and exit through result publication. The measured Chapter 8 artifact
contains per-operation/cell durations and peak process RSS. These are local
engineering observations, not cross-machine qualification or human latency.

Portable training is bounded to 2,048 files, 64 MiB per file and 256 MiB total
source bytes. The 2,048-file boundary is measured with small valid disclosure
records. The byte ceilings are refusal guards, not a claim that a 256 MiB mixture
of expensive scientific replays has been performance-qualified. Over-limit export
refuses the complete operation and retains original evidence; it never silently
trims history or drops dependencies.

Images, screenshots, renders and visible Qt are prohibited during this build.
Protected non-image keyboard/focus and geometry checks are engineering evidence
only. Human usability, visual acceptance and learning effectiveness remain
unexercised. See `KIRBY2_DAILY_RELEASE_PLAN.md` for the separately gated release.
