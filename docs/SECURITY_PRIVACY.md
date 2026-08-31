# Security and privacy

Kirby2 V1 is designed for contained local simulation. Security claims are limited to
the implemented boundaries below; content digests detect changed bytes but do not by
themselves authenticate a publisher or make untrusted data safe for another program.

## Scope boundary

- Kirby2 is a simulation and training environment.
- Kirby2 is not a broker.
- Kirby2 is not a live market connector.
- Kirby2 provides no performance guarantee.
- A reconstruction is not proof of historical market state.

## Network and account boundary

The release has no broker, exchange, order-routing, live-account, credential,
telemetry, updater, subscription, social feed, public leaderboard, or background
daemon. Installation uses `pip --no-index` with the bundled wheelhouse. The local HTML
microscope starts no web server and its content-security policy blocks network
connections.

Kirby2 does not need an account. Do not place brokerage credentials, API keys,
passwords, signing secrets, or direct identity in scenarios, packs, command arguments,
diagnostic exports, or report annotations.

## Installation boundary

Verify that the release manifest and wheel digests match the release evidence. Create
the Python 3.14 environment outside the extracted bundle, install only from its
wheelhouse, and keep the bundle immutable. The launchers never install packages,
contact a service, or create a background process; they resolve the explicitly named
`KIRBY2_PYTHON` runtime or an already installed release entrypoint.

Writable state belongs under the one governed data root reported by `data-paths`, not
inside the application or package directory. Existing symlinks, aliases, path escapes,
and non-directory components are refused at write boundaries.

## Pack boundary

`.k2pack` files are data-only archives. Preflight rejects absolute paths, traversal,
unsafe types, collisions, invalid manifests, excess size/count/depth, and digest
mismatch before activation. Installation stages and revalidates, resolves exact
dependencies, and atomically updates the local registry. Conflicts are explicit and
are not silently overwritten.

Structural validity, digest integrity, signer authenticity, compatibility,
capability, provenance, privacy, and scientific status are separate results. An
unsigned pack can be structurally valid; a valid signature does not prove that its
training claims are true.

## Reports and browser opening

A portable report is verified as a complete relocated bundle before `open-report`
passes its local `index.html` URL to the operating system. Opening is always an
explicit user action. Report JavaScript is bundled locally and cannot fetch network
content under the declared content-security policy.

Content-derived report IDs detect internal inconsistency. If the recipient must know
who issued a report, use a separately trusted signature, pinned digest, or issuance
receipt; the report ID alone is not publisher authentication.

## Identity, learner data, and historical material

Direct identity mappings are separately erasable from pseudonymous runs/evidence.
Use only the declared consent scope and keep hidden lesson truth out of learner-facing
and diagnostic material. Historical labels (`EXACT_REPLAY`, `RECONSTRUCTION`,
`SYNTHETIC_RECONSTRUCTION`, `COUNTERFACTUAL`, `UNAVAILABLE`) must survive export and
analysis.

Synthetic or reconstructed data can still be sensitive when joined with a person,
class, employer, or proprietary dataset. Minimize joins and exports even when a field
is not a traditional credential.

## Diagnostics

Run `doctor` before exporting support material. `export-diagnostics` writes a new
file only at an explicit absolute destination, uses an allowlist, reports field-level
redaction, and sends nothing. Review that file before sharing it. Hidden lesson truth
is excluded in V1 even when authorization is recorded.

## Backup, restore, and recovery

Backups are content-addressed and require explicit consent and destination. Restore
verifies the backup and writes to an explicit destination root; the default conflict
policy fails rather than overwriting. Dataset bytes can be embedded, referenced, or
omitted according to the selected backup policy.

Crash recovery distinguishes durable state from client acknowledgement. If the
journal cannot prove whether an action was observed, Kirby2 offers exact continuation,
safe replay, start-new, or abandonment instead of inventing certainty.

## Uninstall and deletion

Removing the application runtime or extracted bundle does not remove governed user
data. Preserve or delete data only as a separate explicit action after resolving the
exact data root and any required backup/retention policy. Do not recursively delete a
home directory, filesystem root, unresolved environment variable, or broad wildcard.

## Reporting a problem

Preserve the release version, command, exit status, `doctor` result, relevant artifact
digest, and a redacted diagnostic export. Do not attach secrets, direct identity,
proprietary raw datasets, or hidden lesson truth unless a separately authorized and
secure channel explicitly requires them.
