# Tooling for code maps

Status: recommendation record; no installation authorization  
Reviewed: 2026-08-31  
Repository source snapshot: `4a328723ffe8881f2358f1a72576f8ed905f2687`

## Decision

Use the smallest tool chain that keeps maps local, deterministic, and cheap to
refresh. Do not adopt a hosted code-intelligence platform or add a runtime dependency
just to produce navigation documents.

Recommended order:

1. keep `rg` as the interactive lookup tool;
2. build one repository-owned, read-only `ast` indexer with no third-party packages;
3. add Import Linter only after dependency rules are observed and agreed;
4. add ast-grep only for repeated structural queries that plain `rg` cannot express;
5. render optional DOT/SVG views with Graphviz after the underlying TSV/JSON map is
   useful without a picture.

## Current local inventory

| Tool | Local state | Use |
| --- | --- | --- |
| ripgrep 15.1.0 | Present | Fast textual ownership/caller/wire-field lookup |
| Apple/Xcode `ctags` | Present, basic implementation | Editor tags only; no stable JSON map output |
| Universal Ctags | Missing | Optional JSONL symbol index |
| Import Linter / Grimp | Missing | Enforce package dependency contracts |
| ast-grep | Missing | Syntax-aware search and custom structural rules |
| pydeps + Graphviz | Missing | Exploratory Python import visualization |
| SCIP Python / Sourcegraph | Missing | Precise large-scale code navigation |
| MkDocs | Missing | Searchable presentation of Markdown packets |

The inventory is environmental and may become stale; it is not a project dependency
declaration.

## First tool to build: static agent-map generator

A small stdlib-only tool should parse tracked `kirby2/**/*.py` with Python's `ast`
module without importing project modules. It should consume a sorted explicit file
list and emit canonical, sorted outputs:

| Output | Minimum fields | Purpose |
| --- | --- | --- |
| `modules.tsv` | module, path, package, lines | Complete source inventory |
| `imports.tsv` | importer, imported module, level, guarded | Forward/reverse dependency map |
| `symbols.jsonl` | qualified name, kind, path, line, public | Definition index |
| `commands.tsv` | module ID, command ID, CLI name, handler path | CLI routing map |
| `schemas.tsv` | constant name, schema ID/version, owner path | Wire-contract locator |
| `map_manifest.json` | generator version, source commit, input digest, output digests | Staleness and reproducibility |

Constraints:

- read only Git-tracked source unless an explicit staged-source mode is selected;
- never import or execute Kirby2;
- exclude `.venv`, `.kirby2`, evidence, caches, and generated artifacts;
- store generated working indexes under an ignored cache/evidence-neutral location;
- keep only the small curated maps in Git;
- make duplicate command IDs, duplicate schema constants, unresolved internal imports,
  and nondeterministic ordering visible, not silently repaired;
- report dynamic imports separately because a static map cannot prove their target.

This tool would remove most recurring inventory work while preserving human judgment
for responsibility, truth boundaries, and coupled-change advice.

## External tools worth piloting later

### Import Linter

[Import Linter](https://import-linter.readthedocs.io/en/stable/) can declare and check
forbidden, layered, independent, and protected import contracts. Its best Kirby2 use
is preventing inner domains such as `exchange` and `simulation` from acquiring
dependencies on outer UI, release, audit, or orchestration layers. Do not write these
contracts from aspiration; first compare them with the generated import graph and
record deliberate legacy exceptions.

### ast-grep

[ast-grep](https://ast-grep.github.io/) is a syntax-aware search, lint, and rewrite
tool. It is useful for repeated shapes such as handwritten dataclass codecs, command
registration, status projection, or unsafe dispatch patterns. Use it first in search
or reporting mode. Automated rewrites of identity-bearing codecs need their own
bounded authorization and compatibility proof.

### Universal Ctags

[Universal Ctags](https://docs.ctags.io/en/stable/) can emit JSON Lines containing
symbol kind, path, scope, and line data when built with JSON support. It is useful for
fast definition indexes, but it does not establish call semantics, runtime ownership,
or evidence authority. The locally installed Apple/Xcode `ctags` is not this tool.

### pydeps and Graphviz

[pydeps](https://github.com/thebjorn/pydeps) can expose import cycles and focused
module neighborhoods, and Graphviz can render its DOT output. pydeps derives imports
through Python bytecode/import machinery and only sees importable modules, so it is a
helpful secondary visualization—not the canonical inventory for a strict no-import
mapping workflow.

### SCIP Python / Sourcegraph

[SCIP Python](https://github.com/sourcegraph/scip-python) can build a precise symbol
index for larger code-navigation systems. It adds Node tooling and is commonly paired
with a Sourcegraph upload/server workflow. Kirby2 does not yet need that operational
weight, and no repository source or index should be uploaded without explicit user
authorization.

## Tools that are currently waste

- A hosted AI/code-map service: duplicates local agent capability, creates privacy and
  freshness obligations, and does not understand Kirby2 evidence authority.
- A generated full call graph: Python's dynamic dispatch makes it noisy, and the
  resulting visual is too dense to route work.
- MkDocs or another documentation site: five small packets are faster to search as
  plain files; add presentation only if navigation volume grows.
- A dependency just to count lines or files: `rg`, `find`, `wc`, and the proposed AST
  manifest already cover that need.
- Automatic narrative generation on every commit: it creates review churn and hides
  the small seam changes that actually matter.

The useful split is mechanical inventory generated by tools plus a small curated map
of responsibility and truth boundaries. Neither should try to replace the other.

