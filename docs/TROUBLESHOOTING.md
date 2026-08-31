# Troubleshooting

Start with `kirby2-headless version`, `kirby2-headless data-paths`, and
`kirby2-headless doctor`. Preserve the complete error and exit status; Kirby2 refusal
codes are more useful than retrying with weaker options.

## Scope boundary

- Kirby2 is a simulation and training environment.
- Kirby2 is not a broker.
- Kirby2 is not a live market connector.
- Kirby2 provides no performance guarantee.
- A reconstruction is not proof of historical market state.

## Launcher cannot find Kirby2

Message: `Kirby2 is not installed in an available Python 3.14 runtime.`

1. Confirm `python3.14 --version` reports CPython 3.14.
2. Create a virtual environment outside the extracted bundle.
3. Install with `--no-index --find-links /absolute/bundle/wheelhouse`.
4. Set `KIRBY2_PYTHON` to the environment's absolute executable path.
5. Run `"$KIRBY2_PYTHON" -m kirby2 version` before retrying the launcher.

Do not remove `--no-index` to make installation succeed. A missing wheel is a release
resource error, not permission to fetch an unbound dependency.

## Wrong platform or architecture

The macOS launcher accepts Darwin arm64; the Linux launcher accepts Linux x86_64.
Check `uname -s` and `uname -m`. Windows and Linux arm64 are outside V1. Use the
artifact whose embedded manifest names the exact target.

## Terminal trainer refuses to start

The trainer needs an interactive input and output terminal at least 116 columns by 34
rows. Do not pipe it to a file. Resize the terminal, confirm `TERM` is usable, and run:

```sh
kirby2-desktop trainer --help
```

For scripts, automation, or redirected output, use `kirby2-headless` instead.

## Hotkey layout is refused

A valid layout contains every required session action exactly through a unique key
mapping. Removing a key without binding its action elsewhere makes the layout
incomplete. Inspect the default table in [USER_GUIDE.md](USER_GUIDE.md), then apply
`--bind` and `--unbind` together before `--save-layout`.

## First run did not persist

`release-first-run-demo` intentionally uses a clean temporary root when `--data-root`
is omitted. For a persistent installation, pass an explicit resolved absolute root.
Use `data-paths` to inspect the platform default and `verify-installation` to check the
selected root afterward.

## Starter pack conflict

Kirby2 offers the bundled starter set but will not overwrite a conflicting or inactive
binding in the starter namespace. Run `pack list --data-root /absolute/root`, inspect
the exact pack IDs, back up the root, and explicitly deactivate/remove the unwanted
pack if appropriate. Do not edit the registry by hand.

## Scenario source is refused

Run `scenario-source lint` and then `scenario-source explain`. Fix the first diagnostic
at its source path. Common causes are an invalid schema field, unresolved definition
pack, duplicate namespace, disallowed seed override, incompatible capability, or an
unfinalized plan. Do not bypass compilation by calling internal runtime objects.

## Recovery asks what to do

That prompt means the durable journal and client acknowledgement do not justify a
single inferred outcome. Choose exact continuation only when offered, safe replay to
restart from verified history, start new to abandon the unfinished flow, or abandon
to exit. Kirby2 intentionally does not guess whether an unacknowledged simulated
action was applied.

## Offline report will not open

`open-report` first verifies the exact report directory. If verification fails, keep
the original error; a member may be missing, changed, symlinked, or added. If
verification passes but no browser accepts the request, open the printed absolute
`index.html` path manually. Do not serve the directory from a web server merely to
work around a local browser association.

## Replay or historical result looks different

Compare engine, source, runtime, scenario/pack, root seed, observation mode, and input
digests. `AS_OBSERVED` and `POSTMORTEM` intentionally expose different evidence.
`EXACT_REPLAY` and `RECONSTRUCTION` are different provenance modes. A reconstruction
must remain labeled and cannot be used to fill missing observed state silently.

## Backup or restore is refused

All paths must be absolute and resolved. The backup destination must be a new explicit
location. Restore defaults to conflict failure and writes to a separate destination
root. Verify the backup before changing policy; use `accept-identical-only` only when
the existing bytes are expected to be exactly identical.

## Permissions or data-path failure

Run `data-paths` and inspect the named area. Kirby2 refuses symlinks and non-directory
components in governed write chains. Correct ownership or choose a new explicit root;
do not run the complete application as root to mask a path problem.

## Diagnostic export

Choose a new absolute output file and run:

```sh
kirby2-headless export-diagnostics \
  --data-root /absolute/root \
  --output /absolute/new/path/kirby2-diagnostics.json
```

Review the preview/redaction result and the written JSON before sharing. The command
does not upload anything.

## What to preserve for support

Preserve the release version, target, exact command, exit status, refusal code,
artifact or run ID, and redacted diagnostic file. Never include passwords, API keys,
brokerage credentials, direct learner identity, proprietary raw data, or hidden lesson
truth in an ordinary support report.
