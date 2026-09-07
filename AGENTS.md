# Kirby2 agent navigation

For nontrivial repository work, read `docs/agent_maps/README.md` before doing a
broad source search. Open only the packet selected by its routing table, then verify
the relevant claims in source.

Before editing:

1. inspect `git status --short --branch` and preserve unrelated or untracked work;
2. identify the active canonical work-order section and any deviation record;
3. use `docs/agent_maps/CHANGE_ROUTER.md` to locate the narrow ownership seam;
4. treat the maps as navigation aids, never as implementation or acceptance evidence.

Current user instructions and canonical repository contracts override these maps.
Do not infer that a card, audit, platform, or human review passed from a map entry.
Do not add live brokerage, order submission, credential, telemetry, updater, or
real-market-claim behavior. New top-level commands use the declarative registry in
`kirby2/cli/registry.py`; do not extend dispatch with another conditional ladder.

