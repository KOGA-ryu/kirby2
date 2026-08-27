# Counterfactual Execution Debugger

Kirby2 branches only from verified immutable run artifacts. A branch is rebuilt to
its fork time twice and the complete branch-snapshot digest must match before a
mutation is allowed to run.

`EXOGENOUS_REPLAY` fixes the stored external command path. Player actions can change
fills in the branch, but they do not regenerate the reference market path. This is
the supported mode for immutable multi-venue algorithm recordings and for
impact-ignored decision comparisons.

`ENDOGENOUS_FORK` continues the seeded simulation from the preserved exchange,
flow-model, RNG, clock, queue, player, strategy, and feature-window state. Changed
orders can therefore affect queues and later generated flow. Components that are
not active in a parent, such as Hawkes state in a simple-flow session or WO29 agent
state, are recorded explicitly as `ABSENT`; they are never represented as measured
zero.

The mutation contract supports command, order type, integer-tick price, quantity,
venue, timing, removal, insertion, and hotkey-outcome changes. Single-venue session
runs accept only the explicit `PRIMARY` venue. Venue changes require an immutable
multi-venue algorithm parent, where pending route legs, every venue state, latency
RNGs, fees, and per-venue positions are preserved.

Reports contain paired timelines, the first structural divergence, fill,
completion, slippage, fee, adverse-selection, position, risk, traffic-light,
deadline, and P&L fields. Unsupported economics are labelled `UNAVAILABLE` or
`NOT_APPLICABLE`. Static mutations are resolved only against state available at the
action time; later information is permitted only in post-hoc analysis.

Every persisted branch has a content-derived run ID, parent run ID, mutation
manifest, branch snapshot, result digest, and byte-verified TOML artifacts. The
standard timing sweep persists five branches at -500 ms, -250 ms, original time,
+250 ms, and +500 ms.

A simulated counterfactual is evidence about Kirby2's configured model, not proof
of what the real market would have done.
