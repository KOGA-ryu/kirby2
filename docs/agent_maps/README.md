# Kirby2 agent map router

Status: navigation aid; non-canonical and non-evidentiary  
Packet ID: `KIRBY2_AGENT_MAPS_V1`  
Source snapshot: `4a328723ffe8881f2358f1a72576f8ed905f2687`  
Snapshot meaning: committed production source through WO40-D; the map files
themselves are later working-tree documentation.

## Use this packet in under five minutes

1. Run `git status --short --branch`.
2. Compare `git rev-parse HEAD` with the source snapshot above.
3. Select exactly one map below.
4. Open the named source entry points, their direct callers, and their governing
   protocol. Do not recursively read the whole package.
5. If source contradicts a map, source wins and the map needs a bounded update.

| Need | Open | Then inspect |
| --- | --- | --- |
| Understand the execution/data architecture | `SYSTEM_MAP.md` | One flow and its named entry points |
| Locate files for a proposed change | `CHANGE_ROUTER.md` | Producer, consumer, persistence, and audit seam only |
| Continue the current release sequence | `RELEASE_MAP.md` | Current WO40 card and frozen release TOML |
| Route a task to a top-level package mechanically | `PACKAGE_MAP.tsv` | The row's `start_here` files |
| Decide whether to automate a map/index | `TOOLING.md` | Start with the dependency-free AST proposal |
| Determine whether a claim is accepted | Do not use these maps | Canonical evidence, gate output, and human sidecars |

## Authority order

These packets grant no authority. Resolve conflicts in this order:

1. current user instruction and applicable repository agent instruction;
2. active canonical work-order section plus append-only deviation ledger;
3. committed protocol/configuration bytes and public wire contracts;
4. current source and immutable evidence produced from an identified commit;
5. these maps;
6. older roadmap descriptions, historical reports, and advisory analyses.

Automated and human statuses remain separate. `PASS`, `PASS_WITH_WARNINGS`, `FAIL`,
and `NOT_EXERCISED` do not imply human `ACCEPTED`. A source-ready artifact is not a
qualified release, and an observed historical value is not interchangeable with a
synthetic or reconstructed value.

## Freshness check

When HEAD differs from the source snapshot, first run:

```text
git diff --name-only 4a328723ffe8881f2358f1a72576f8ed905f2687..HEAD
```

Only revalidate map rows intersecting those paths. Update the snapshot after a
bounded review when one of these changes:

- package responsibility or dependency direction;
- public entry point, command registration, or governed data root;
- wire identity, persistence boundary, or evidence authority;
- active roadmap card, release freeze, or external blocker.

Do not update maps for an internal refactor that preserves those seams. Do not paste
field-by-field schemas, tests, command output, or work-order prose into this packet;
link to their source instead.

## Why this is useful—and when it becomes waste

This is useful because Kirby2 has more than 300 Python files and its largest trees
mix runtime, contracts, persistence, and adversarial evidence. A routing packet can
replace repeated repository-wide rereads with a short path:

```text
task intent -> owning package -> public contract -> direct producer/consumer -> gate
```

It becomes waste when it tries to restate implementation details, is treated as
proof, or is not refreshed after a seam changes. The maintenance budget is therefore
small: keep this router below roughly 100 lines, keep each focused map independently
readable, and delete obsolete rows rather than appending a narrative history.
