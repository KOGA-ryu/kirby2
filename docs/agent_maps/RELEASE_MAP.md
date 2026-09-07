# Kirby2 WO40 release map

Status: volatile roadmap packet; non-evidentiary  
Source snapshot and WO40-D commit:
`4a328723ffe8881f2358f1a72576f8ed905f2687`  
Canonical source: `KIRBY2_WORK_ORDERS_31_40_GOAL.md`, WO40-D through WO40-J

## Current position

```text
WO40-A data/migrations       COMPLETE
WO40-B crash recovery        COMPLETE
WO40-B1 backup/restore       COMPLETE
WO40-C first run/diagnostics COMPLETE
WO40-D release protocol      PRODUCTION COMMIT PRESENT
WO40-D1 resource preflight   NOT_READY
WO40-E source freeze         PENDING
WO40-F artifacts             PENDING
WO40-G macOS qualification   PENDING
WO40-H Linux qualification   PENDING
WO40-I performance           PENDING
WO40-J closeout              PENDING
```

The production code/protocol part of WO40-D is committed. Its prescribed audit gate
and the broader deferred hardening suite have not been claimed as run. Do not convert
that source state into a qualified-release claim.

## Dependency map

```text
two exact wheels + two real clean providers
  -> D1 PASS and committed preflight report
  -> E final source/launcher/docs freeze
  -> F build and verify immutable macOS/Linux artifacts
  -> G macOS qualification ─┐
     H Linux qualification ─┼-> J prerequisite aggregate and closeout
     I 10,000-run perf  ────┘
```

After WO40-E, WO40-F through WO40-J are evidence-only. A later need for source,
package, protocol, threshold, probe, or user-documentation changes invalidates the
candidate and requires an explicit release-restart amendment.

## D1 blocker packet

The generated `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md` is intentionally uncommitted
because status is `NOT_READY`. Repository-owned protocol, tools, starter manifests,
inventories, and deterministic starter archives passed. Missing resources are:

| Resource | Exact requirement |
| --- | --- |
| Linux wheel | `release/wheelhouse/linux-x86_64/duckdb-1.5.5-cp314-cp314-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl` |
| Linux SHA-256 | `fbf0f2d48b43c6c304d00463b463c27ead6c4b01c3c1816b750f728decf71afe` |
| macOS wheel | `release/wheelhouse/macos-arm64/duckdb-1.5.5-cp314-cp314-macosx_11_0_arm64.whl` |
| macOS SHA-256 | `8c11775cc99a447618d5f1840126db17f2652f3eae05529df4f81f40e2df7151` |
| Provider inventory | `.kirby2/release/clean-providers.toml`, secret-free V1 schema |
| macOS provider | Real clean Darwin arm64, CPython 3.14, offline install, existing access, at least 8 GiB RAM and 20 GiB free store |
| Linux provider | Real clean Linux x86_64, CPython 3.14, offline install, existing access, at least 8 GiB RAM and 20 GiB free store |

D1 discovery does not authorize downloads, installation, credential creation, or a
remote connection. Do not commit its report as passing or begin the canonical source
freeze until the preflight passes.

## Frozen WO40-D surfaces

| Surface | Responsibility |
| --- | --- |
| `release/platforms.toml` | Two minimum targets and designated performance target |
| `release/requirements.lock` | Exact offline dependency wheels and hashes |
| `release/artifact_layout.toml` | Artifact/member/source-class/starter layout |
| `release/qualification.toml` | Functional matrix, retry policy, closeout order |
| `release/performance_thresholds.toml` | Fixed workloads, samples, resources, thresholds, 10k-row generator/digest |
| `release/build.py` | Protocol bundle, resource preflight, build/verification planning |
| `release/manifest.py` | Release/artifact identities and mandatory product limitations |
| `release/packaging.py` | Canonical archive path/member/transport rules |
| `release/licenses.py` | Lock parsing and bounded license extraction |
| `release/qualification.py` | Platform/function/evidence dispatch and closeout prerequisites |
| `release/performance.py` | Row templates, source lock, result/attempt/auxiliary validation |
| `release/probes.py` | Production queue-reactive recording/replay probe |
| `release/commands.py` | Preflight, build, verify, qualify, performance, and closeout CLI |

The 10,000-row workload is represented by a compact deterministic generator plus its
corpus digest, not by embedding tens of megabytes of repeated TOML rows.

## WO40-E: last production-source card

WO40-E must create desktop/headless entry points, three exact POSIX launchers, six
user-documentation files, and the mechanically derived
`release/performance_runner_sources.lock`. It may modify version/UI/microscope/release
audit integration only within its owned list. The desktop is the terminal execution
trainer plus explicit local offline analysis—not a native-widget GUI.

Every user-facing set must say: synthetic training environment; not a broker; not a
live-market connector; no performance guarantee; reconstruction is not historical
proof. No updater, telemetry, account, brokerage, subscription, social feed,
leaderboard, or background daemon enters the candidate.

## Completion meaning

Source completion, artifact construction, platform qualification, performance, and
human acceptance are different states. WO40-J can close only if both platforms pass,
artifact identities bind the one WO40-E candidate, the preregistered performance
workload records exactly 10,000 complete work units, and every prerequisite evidence
row is independently non-red.

