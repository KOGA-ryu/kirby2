# Kirby2 0.1 user guide

Kirby2 is an offline, deterministic market-execution trainer. The V1 desktop is a
keyboard-first terminal application with explicit local HTML replay analysis; it is
not a native-widget application.

## Scope boundary

- Kirby2 is a simulation and training environment.
- Kirby2 is not a broker.
- Kirby2 is not a live market connector.
- Kirby2 provides no performance guarantee.
- A reconstruction is not proof of historical market state.

## Install from a release bundle

Kirby2 V1 supports macOS arm64 and Linux x86_64 with CPython 3.14. Keep the extracted
bundle read-only and create the Python environment outside it. Replace the example
paths below with absolute paths on your machine.

```sh
python3.14 -m venv /absolute/path/to/kirby2-runtime
/absolute/path/to/kirby2-runtime/bin/python -m pip install \
  --no-index \
  --find-links /absolute/path/to/extracted-bundle/wheelhouse \
  kirby2==0.1.0
```

This command uses only the bundled project and DuckDB wheels. It must not contact a
package index. Point the launcher at that environment:

```sh
export KIRBY2_PYTHON=/absolute/path/to/kirby2-runtime/bin/python
```

Use `release/launchers/macos/kirby2` on macOS,
`release/launchers/linux/kirby2` on Linux, or
`release/launchers/headless/kirby2` for the complete non-desktop CLI. You can also use
the installed `kirby2-desktop`, `kirby2-headless`, and `kirby2` commands directly.

Confirm the runtime identity before creating data:

```sh
kirby2-headless version
kirby2-headless data-paths
kirby2-headless doctor
```

## First run

`release-first-run-demo` checks all governed writable areas, offers the two exact
bundled starter packs together, resolves the curriculum dependency, and performs a
one-share simulated place/cancel cycle. Supply an absolute persistent data root if
you want to keep its starter installation; omitting it intentionally uses a temporary
root.

```sh
kirby2-headless release-first-run-demo \
  --seed 42 \
  --data-root /absolute/path/to/kirby2-data
kirby2-headless verify-installation \
  --data-root /absolute/path/to/kirby2-data
```

The platform defaults reported by `data-paths` are under macOS Application Support
or Linux `XDG_DATA_HOME` (with `~/.local/share` as the Linux fallback). An explicit
data root must be absolute and already resolved.

## Start the terminal trainer

Run the platform launcher with no mode, or name `trainer` explicitly:

```sh
release/launchers/macos/kirby2 trainer --scenario balanced --seed 42
release/launchers/linux/kirby2 trainer --scenario balanced --seed 42
```

The terminal must be interactive and at least 116 columns by 34 rows. The default
controls are:

| Key | Simulated action |
| --- | --- |
| `a`, `s`, `d` | Buy at bid, buy at ask, market buy |
| `j`, `k`, `l` | Sell at ask, sell at bid, market sell |
| `c`, `C` | Cancel nearest, cancel all |
| `v`, `f` | Replace nearest, flatten simulated position |
| `[`, `]` | Decrease or increase order quantity |
| `Space` | Start or pause simulated time |
| `r`, `q` | Reset or quit |

Use `--bind KEY=COMMAND`, `--unbind KEY`, `--save-layout NAME`, and `--layout NAME`
to manage complete hotkey layouts. The UI refuses layouts that omit a required
action. Orders, fills, position, queue state, and time shown here exist only inside
the simulation.

## Traffic-light scripts

Traffic-light rules consume observable simulated features and produce training
states, reasons, and entry/exit permissions. They never submit orders. Inspect a
rule before using it, then pass it to the trainer:

```sh
kirby2 strategy /absolute/path/to/rule-file
kirby2-desktop trainer --strategy /absolute/path/to/rule-file
```

Unavailable inputs stay explicit; a rule is not permission or advice for real-market
activity. See [SCENARIO_AUTHORING.md](SCENARIO_AUTHORING.md) for deterministic source
and pack workflows.

## Scenarios and lesson packs

Use `scenario-source lint`, `explain`, `compile`, `diff`, and `run` for declarative
scenario files. Use `pack inspect` and `pack verify` before an explicit local install.
Pack identities are content-derived, dependencies are exact, and conflicts are
offered rather than overwritten. The bundled starter set is one scenario pack plus a
five-lesson curriculum pack and must resolve as a pair.

## Replay and offline analysis

Session recordings can be replayed, inspected on a timeline, and rendered as local
analysis. Portable microscope reports contain `index.html` plus local CSS/JavaScript;
they require no web server and their content-security policy forbids network access.

```sh
kirby2-desktop microscope \
  --fixture stale_partial_cancel_race \
  --mode as-observed \
  --output /absolute/new/path/report
kirby2-desktop open-report /absolute/new/path/report
```

`open-report` first verifies the complete relocated bundle and opens it only because
you explicitly requested that action. `AS_OBSERVED` keeps the selected observation
boundary; `POSTMORTEM` may show later available evidence and must not be read as what
was knowable earlier.

## Historical labels

`EXACT_REPLAY` means Kirby2 received source-provided order events within the declared
fixture. `RECONSTRUCTION` and `SYNTHETIC_RECONSTRUCTION` are modeled outputs used to
study mechanisms. Neither label establishes completeness, authenticity, causation,
or empirical resemblance. Counterfactual branches are alternatives, not history.

## Data, backup, recovery, and diagnostics

`data-paths` lists the one governed root and its areas for runs, evidence,
checkpoints, packs, configuration, backups, diagnostics, exports, cache, and staging.
No writable user state belongs in the installation bundle.

Create a content-addressed backup and restore it into a separate root:

```sh
kirby2 backup --data-root /absolute/source/root --output /absolute/new/backup
kirby2 restore /absolute/new/backup \
  --destination-root /absolute/new/restore-root
```

After an interrupted interactive session, Kirby2 distinguishes exact continuation,
safe replay, starting new, and abandonment. It does not guess whether an
unacknowledged command was applied.

For support, run `doctor` first. `export-diagnostics` writes a new explicitly chosen
JSON file from an allowlist and reports redactions; it does not send anything.

## More information

- [SCENARIO_AUTHORING.md](SCENARIO_AUTHORING.md)
- [INSTRUCTOR_RESEARCH.md](INSTRUCTOR_RESEARCH.md)
- [SECURITY_PRIVACY.md](SECURITY_PRIVACY.md)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- [LIMITATIONS.md](LIMITATIONS.md)
