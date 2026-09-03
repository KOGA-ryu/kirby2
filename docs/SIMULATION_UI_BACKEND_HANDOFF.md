# Simulation UI/backend integration handoff

Status: `READY_FOR_UI_INTEGRATION`
Handoff ID: `KIRBY2_SIMULATION_UI_BACKEND_HANDOFF_V1`
Contract schema target: version 1
Backend implementation commit: `655ccf495b015f2067f11d63adcf3dd63e4e4609`
UI integration commit: `PENDING`
Backend setup-contract slice: `IMPLEMENTED`
Backend setup-contract commit: `19b5fae21e891c798b6bfd6c149761a82597feac`
Backend run-start slice: `IMPLEMENTED`
Backend run-start commit: `80372cbb12d4a2262e189f9ae63e20f0fadb9a11`
Backend interaction slice: `IMPLEMENTED`
Backend interaction commit: `78c82f01af640d20616347fd021f86b92db5cfd2`
Backend reset/close lifecycle slice: `IMPLEMENTED`
Backend reset/close lifecycle commit: `9fa910fa475716cd2e6e1ce4bfd0f87a4ddd730f`
Backend finalization/artifact slice: `IMPLEMENTED`
Backend finalization/artifact commit: `ccfc9669cc46c29dca226bb5481b13210394d2ca`
Backend Replay-artifact verification slice: `IMPLEMENTED`
Backend Replay-artifact verification commit: `49b8854d7739ad59cd3109d319c946e643c3c193`
Backend verified Replay-provider slice: `IMPLEMENTED`
Backend verified Replay-provider commit: `655ccf495b015f2067f11d63adcf3dd63e4e4609`
UI setup-contract projector commit: `66de3b4d9ce2d213e94c68a8f759859566c520cf`
UI verified Setup integration commits:
`77e6d5f28c3e3d254257a37bd8d74a1c786f3958`,
`d0a3d2d2bed4901f45dc1c0ce322c8d3c1459320`
UI verified Start integration commit: `63fed733a77b206d5629dec72ab3325a7d61ce97`

This is the handoff contract for connecting the standalone Kirby2 Qt UI to the
mathematical simulation backend. It is deliberately separate from release
qualification and from the WO36 Replay Studio presentation contract.

The UI worker may integrate an individual public surface only when its backend row
below says `IMPLEMENTED` and names an exact path and commit. The backend handoff is
ready because this header says `READY_FOR_UI_INTEGRATION`, the backend implementation
commit is filled in, and no backend-owned row remains `PENDING`. UI-owned rows remain
open until the separate UI repository commits the corresponding wiring.

## 1. Repository locations

- Backend repository: `/Users/kogaryu/Documents/ChatGPT/kirby2`
- UI repository: `/Users/kogaryu/Documents/ChatGPT/kirby2-ui`
- This handoff document:
  `/Users/kogaryu/Documents/ChatGPT/kirby2/docs/SIMULATION_UI_BACKEND_HANDOFF.md`

The backend repository owns simulation meaning, matching, run identity, replay
identity, and public wire contracts. The UI repository owns Qt presentation,
interaction state, request correlation, and local view state.

## 2. Product boundary

Kirby2 is a deterministic synthetic simulation and training environment. The live
Level 2 display must be derived from orders and cancellations processed by the
matching engine:

```text
profile -> resolved equations and parameters -> order flow -> matching engine
        -> authoritative book snapshot -> UI frame
```

The UI must not:

- generate, interpolate, or repair authoritative prices or depth;
- evaluate Hawkes, Poisson, queue-reactive, regime, distribution, or intraday math;
- infer missing market state from deltas;
- import private exchange truth, backend-only query/engine receipts, or full event
  inventories; the public Replay verification receipt defined here is allowed;
- label synthetic output as observed or reconstructed real-market data;
- import `kirby2.release`, performance-corpus, qualification, or WO40-I records;
- add brokerage, order submission to a real venue, credentials, telemetry, or network
  market-data behavior.

Every user-visible live frame must carry an explicit synthetic-simulation label.

## 3. Existing seams to preserve

### Backend

- `kirby2.session.live.SessionSnapshot` already carries scenario and regime labels,
  seed, simulation clock, Level 2 bids and asks, tape, working orders, player account,
  objective, strategy state, `market_state_id`, `market_state_time_us`, and
  `exchange_event_sequence`.
- `kirby2.session.live.LiveMarketSession` already owns start, pause, reset, command,
  advance, snapshot, and deterministic state behavior.
- `kirby2.scenarios.market.create_market_engine()` already composes a scenario with a
  flow model, optional intensity modifier, distributions, intraday profile/window,
  volume/liquidity dimensions, and parameter overrides.
- `kirby2.simulation.flow_models` already implements simple Poisson and Hawkes arrival
  models.
- `kirby2.simulation.regimes` already implements the twelve regime policies.
- `kirby2.simulation.queue_reactive` already implements queue-state response rules.
- `kirby2.scenarios.accepted_scenarios.json` and
  `kirby2.simulation.accepted_hawkes.json` already hold accepted inputs.

### UI

- `src/kirby2_ui/backend.py::KirbyBackend` is the lazy compatibility facade. Qt
  widgets must continue to use this facade rather than importing simulator objects.
- `src/kirby2_ui/setup.py::SetupPage` already discovers backend options and creates
  one launch request.
- `src/kirby2_ui/workstation.py::project_snapshot()` is the current Level 2
  disclosure boundary.
- `src/kirby2_ui/workspace.py::bind_run_id()` is available for canonical backend run
  identity.
- The Replay controller/projector/store pattern is the reference for stale-response
  handling and atomic commits, but its Replay DTO is not the live-simulation DTO.

### Replay separation

WO36-C/D Replay frames are detached retrospective presentation records. They can
reduce values to display text and operate over immutable recorded evidence. A live
simulation frame has different synchronization, lifecycle, and typing requirements.

Do not extend or reinterpret `ReplayPresentationFrameV1` for live simulation. Add a
sibling live-simulation projector, controller, and atomic frame store in the UI.

## 4. Implementation and readiness table

| Public surface | Owner | Status | Implementation path |
| --- | --- | --- | --- |
| Existing live session and snapshot | Backend | `IMPLEMENTED` | `kirby2/session/live.py` |
| Existing equation/runtime components | Backend | `IMPLEMENTED` | `kirby2/simulation/`, `kirby2/scenarios/` |
| `SimulationComponentRefV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `SimulationProfileRefV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `SimulationProfileCatalogV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `SimulationTrainingResourceCatalogV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `SimulationProfileSelectionV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `SimulationProfileResolutionV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `ResolvedSimulationConfigurationV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_contract.py` |
| `SimulationTrainingOptionsV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_live_contract.py` at `80372cbb12d4a2262e189f9ae63e20f0fadb9a11` |
| `SimulationStartResultV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_live_contract.py` at `80372cbb12d4a2262e189f9ae63e20f0fadb9a11` |
| `SimulationFrameV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_live_contract.py` at `80372cbb12d4a2262e189f9ae63e20f0fadb9a11` |
| `SimulationCommandRequestV1` / `SimulationCommandResultV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_interaction_contract.py` at `78c82f01af640d20616347fd021f86b92db5cfd2` |
| `SimulationAdvanceResultV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_interaction_contract.py` at `78c82f01af640d20616347fd021f86b92db5cfd2` |
| `SimulationResetResultV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_lifecycle_contract.py` at `9fa910fa475716cd2e6e1ce4bfd0f87a4ddd730f` |
| Current-frame recovery facade | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_run_facade.py` at `78c82f01af640d20616347fd021f86b92db5cfd2` |
| Two-phase reset facade | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_run_facade.py` at `9fa910fa475716cd2e6e1ce4bfd0f87a4ddd730f` |
| `SimulationCloseResultV1` and idempotent close facade | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_lifecycle_contract.py`, `kirby2/ui/simulation_run_facade.py` at `9fa910fa475716cd2e6e1ce4bfd0f87a4ddd730f` |
| `SimulationRunResultV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_artifact_contract.py` at `ccfc9669cc46c29dca226bb5481b13210394d2ca` |
| `SimulationReplayArtifactV1` / `ReplayArtifactRefV1` | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_artifact_contract.py` at `ccfc9669cc46c29dca226bb5481b13210394d2ca` |
| `ReplayArtifactVerificationReceiptV1` and deep resolver | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_replay_contract.py`, `kirby2/ui/simulation_replay_facade.py` at `49b8854d7739ad59cd3109d319c946e643c3c193` |
| Verified Replay provider bridge | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_replay_provider.py` at `655ccf495b015f2067f11d63adcf3dd63e4e4609` |
| Profile list/resolve facade | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_facade.py` |
| Start facade and fresh run materialization | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_run_facade.py` at `80372cbb12d4a2262e189f9ae63e20f0fadb9a11` |
| Command/advance facade | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_run_facade.py` at `78c82f01af640d20616347fd021f86b92db5cfd2` |
| Finalize/artifact facade | Backend | `IMPLEMENTED` | `kirby2/ui/simulation_finalize_facade.py` at `ccfc9669cc46c29dca226bb5481b13210394d2ca` |
| Backend-produced setup/start/interaction/lifecycle/finalization/Replay golden fixtures | Backend | `IMPLEMENTED` | 31 records in `kirby2/ui/fixtures/simulation_contract_v1/` at `655ccf495b015f2067f11d63adcf3dd63e4e4609` |
| Strict setup-contract projector | UI | `IMPLEMENTED` | `src/kirby2_ui/simulation_contract.py` at `66de3b4d9ce2d213e94c68a8f759859566c520cf` |
| Strict live-frame projector and store | UI | `PENDING` | UI worker selects paths |
| Setup/profile selector integration | UI | `IMPLEMENTED` | `src/kirby2_ui/` at `77e6d5f28c3e3d254257a37bd8d74a1c786f3958` and `d0a3d2d2bed4901f45dc1c0ce322c8d3c1459320` |
| Live workstation integration | UI | `PENDING` | UI worker selects paths |
| Completed-run handoff to Replay | Backend + UI | `PENDING` | To be recorded after both sides land |

This table reports implementation state only. It is not release, audit, statistical,
or human-acceptance evidence.

## 5. Canonical identity and numeric rules

All public records below are exact-field, schema-versioned records. For version 1,
`C(value)` means the shared Kirby2 canonical-JSON encoding implemented by
`kirby2.full_day.models.canonical_json_bytes()` and, for the setup boundary, the
byte-equivalent `kirby2.packs.formats.canonical_json_bytes()`: exact JSON values
only, object keys sorted, compact separators, ASCII escaping, UTF-8/ASCII-compatible
bytes, and no binary floats or non-finite values. `H(value)` means the lowercase
hexadecimal SHA-256 of `C(value)`.

Before canonical encoding, all strings must already be Unicode NFC, and identifier
strings must have no outer whitespace. Decoders reject unknown or missing fields,
duplicate keys, invalid enums, booleans in integer fields, malformed digests, and
noncanonical bytes. Arrays preserve their declared order. Values requiring a
fraction use a canonical decimal string or a named scaled integer, never a JSON
float.

Version-1 identity recipes are exact:

```text
component_payload_bytes = C(the component payload object)
component_ref.content_sha256 = lowercase_hex_sha256(component_payload_bytes)

profile_ref.profile_sha256 = H({
    "schema_id": "KIRBY2_SIMULATION_PROFILE_SEMANTICS_V1",
    "schema_version": 1,
    "profile_id": catalog_row.profile_ref.profile_id,
    "profile_version": catalog_row.profile_ref.profile_version,
    "engine_contract_id": catalog_row.engine_contract_id,
    "arrival_model_family": catalog_row.arrival_model_family,
    "regime": catalog_row.regime,
    "defaults": catalog_row.defaults,
    "controls": [
        {
            "control_id": control.control_id,
            "value_kind": control.value_kind,
            "scale": control.scale,
            "default_value": control.default_value,
            "minimum_value": control.minimum_value,
            "maximum_value": control.maximum_value,
            "step": control.step,
            "unit": control.unit,
            "options": control.options,
        }
        for control in catalog_row.controls in their declared order
    ],
})

catalog_sha256 = H(SimulationProfileCatalogV1 without catalog_sha256)
selection_sha256 = H(SimulationProfileSelectionV1)
resolved_configuration_sha256 = H(ResolvedSimulationConfigurationV1)

run_request_sha256 = H({
    "schema_id": "KIRBY2_SIMULATION_RUN_REQUEST_V1",
    "schema_version": 1,
    "resolved_configuration_sha256": resolved_configuration_sha256,
    "training_options": SimulationTrainingOptionsV1.as_dict(),
})

book_state_sha256 = H(SimulationFrameV1.book)
cursor_id = "simulation-cursor-" +
    H(SimulationFrameV1.cursor without cursor_id)[0:24]
market_state_id = "simulation-market-state-" + H({
    "source_run_id": source_run_id,
    "market_state_time_us": market_state_time_us,
    "exchange_event_sequence": cursor.exchange_event_sequence,
    "book_state_sha256": book_state_sha256,
})[0:24]
frame_id = "simulation-frame-" +
    H(SimulationFrameV1 without frame_id)[0:24]
```

`source_run_id` is the backend-issued execution identity, has the exact form
`simulation-run-` followed by 32 lowercase hexadecimal characters, and is never
reused, including after reset. It is deliberately not a content digest: identical
configuration bytes can produce separate practice attempts. Mathematical identity
and determinism are proven by the profile, selection, resolved-configuration,
run-request, book, event-tape, and artifact digests. The UI validates the run-ID
format and equality across records but never manufactures it.

`reset_token_id` is a separate backend-issued one-time correlation identity with
the exact form `simulation-reset-token-` followed by 32 lowercase hexadecimal
characters. It is never reused, is not a content digest, and never becomes the
identity of the replacement run.

The complete event tape digest is `H(the ordered array of canonical recorded event
rows)`, with `H([])` as the empty-tape value. A frame ID binds the complete render
transaction, including the clock, book, recent trade window, working orders,
account, strategy, objective, diagnostics, metrics, status, and provenance.

UI `request_id`, source generation, focus, selection, animation, input source,
originating view, and loading state are operational sidecar state and never enter
canonical simulation identity.

## 6. Implemented backend records

The record names and responsibilities in this section are the implemented version-1
contract. Their exact `as_dict()`/`from_dict()` behavior and serializer-produced
examples are available at the backend implementation commit recorded above.

### 6.1 `SimulationComponentRefV1`

```text
component_kind:
    "SCENARIO_DEFINITION" | "REGIME_PROFILE" | "DISTRIBUTION_BUNDLE" |
    "QUEUE_REACTIVE" | "HAWKES" | "INTRADAY" | "HOTKEY_LAYOUT" |
    "STRATEGY_DEFINITION" | "CURRICULUM_DRILL" | "OBSERVATION_POLICY"
component_id: string
component_version: positive integer
content_sha256: SHA-256
```

`component_id` matches `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Resolution loads the
referenced canonical bytes and independently verifies kind, ID, version, and digest.
A missing or mismatched component is refused; there is no fallback to a same-named
or “latest” component. A simple arrival model has a null Hawkes reference rather
than a dummy component.

The hashed payload is a schema-versioned component object, never this reference and
never an object containing `content_sha256`; the recipe is therefore non-circular.
Its exact stored bytes must equal `C(payload_object)`. Every component kind must have
an exact public payload schema and strict decoder before any catalog row may
reference it.

### 6.2 `SimulationProfileRefV1`

```text
profile_id: string
profile_version: positive integer
profile_sha256: SHA-256
```

This is the only profile identity the UI persists or sends back. A stale or changed
digest is refused; it is never silently rebound by profile name.

### 6.3 `SimulationProfileCatalogV1`

```text
schema_id: "KIRBY2_SIMULATION_PROFILE_CATALOG_V1"
schema_version: 1
catalog_sha256: SHA-256
profiles: ordered array of
    profile_ref: SimulationProfileRefV1
    presentation:
        display_name: string
        summary: string
    engine_contract_id: "KIRBY2_SIMULATION_ENGINE_V1"
    arrival_model_family: "simple" | "hawkes"
    regime:
        "BALANCED" | "BUY_PRESSURE" | "SELL_PRESSURE" | "MOMENTUM_UP" |
        "MOMENTUM_DOWN" | "ABSORPTION_BID" | "ABSORPTION_ASK" |
        "THIN_LIQUIDITY" | "LIQUIDITY_VACUUM" | "MEAN_REVERSION" |
        "HIGH_CANCELLATION" | "PANIC"
    defaults:
        seed: nonnegative integer
        duration_us: positive integer
        intraday_phase:
            "PREOPEN" | "OPENING" | "MORNING" | "MIDDAY" |
            "AFTERNOON" | "CLOSE" | "NOT_APPLICABLE"
        relative_volume: "0.25x" | "0.50x" | "1.00x" | "2.00x" | "5.00x" | "10.00x"
        liquidity: "VERY_THIN" | "THIN" | "NORMAL" | "DEEP" | "VERY_DEEP"
        intensity_scale_ppm: nonnegative integer
        scenario_definition_ref: SimulationComponentRefV1
        regime_profile_ref: SimulationComponentRefV1
        distribution_bundle_ref: SimulationComponentRefV1
        queue_reactive_ref: SimulationComponentRefV1 | null
        hawkes_ref: SimulationComponentRefV1 | null
        intraday_ref: SimulationComponentRefV1 | null
    controls: ordered array of
        control_id: string
        label: string
        value_kind: "INTEGER" | "FIXED_POINT" | "ENUM" | "BOOLEAN"
        scale: positive power-of-ten integer
        default_value: integer | string | boolean
        minimum_value: integer | null
        maximum_value: integer | null
        step: positive integer | null
        unit: string
        options: ordered array of strings
    provenance:
        classification: "SYNTHETIC_SIMULATION_ONLY"
        real_market_data: false
        matching_engine_derived: true
        generation_method: "ORDER_FLOW_THROUGH_MATCHING_ENGINE"
        level2_origin: "MATCHING_ENGINE_BOOK_STATE"
        display_label: string
```

Catalog rows and control IDs are unique. `INTEGER` and `FIXED_POINT` controls use an
integer default, empty `options`, integer bounds, and an integer step;
`FIXED_POINT` means `value / scale`. `ENUM` uses a string default contained in its
nonempty ordered `options`, null numeric bounds and step, and scale 1. `BOOLEAN`
uses a boolean default, empty options, null bounds and step, and scale 1. The
component kind of every default reference must agree with its field name.

For numeric controls, both the default and every selected value must lie on the
grid anchored at `minimum_value`: `(value - minimum_value) % step == 0`. The grid is
not anchored at zero. Boolean values are never accepted for integer or fixed-point
controls even though Python's `bool` is an `int` subclass.

One catalog response drives both the simple profile picker and its optional advanced
controls. The UI does not hard-code which controls apply to a model. Hawkes matrices,
queue-response equations, and raw distributions remain backend-owned; the catalog
exposes only bounded selectors and scalar controls intended for users.

The first catalog should be curated from already supported combinations. It must not
materialize an uncontrolled Cartesian product merely because components can be
combined in theory.

#### `SimulationTrainingResourceCatalogV1`

Setup obtains all valid non-profile training references from this second
backend-owned catalog; it never invents component IDs or command bindings.

```text
schema_id: "KIRBY2_SIMULATION_TRAINING_RESOURCE_CATALOG_V1"
schema_version: 1
catalog_sha256: SHA-256
layouts: ordered nonempty array of
    layout_ref: SimulationComponentRefV1
    presentation:
        display_name: string
        summary: string
    actions: ordered nonempty array of
        semantic_action_id: string
        action_kind: "PLAYER_ACTION" | "LIFECYCLE"
        display_label: string
        bound_key: string | null
observation_policies: ordered nonempty array of
    policy_ref: SimulationComponentRefV1
    presentation:
        display_name: string
        summary: string
    player_queue_disclosure: "AVAILABLE" | "UNAVAILABLE"
strategies: ordered array of
    strategy_ref: SimulationComponentRefV1
    presentation:
        display_name: string
        summary: string
curriculum_drills: ordered array of
    curriculum_drill_ref: SimulationComponentRefV1
    presentation:
        display_name: string
        summary: string
defaults:
    layout_ref: SimulationComponentRefV1
    observation_policy_ref: SimulationComponentRefV1
    quantity_options: ordered nonempty array of positive integers
    initial_quantity: positive integer
```

Its digest is `H(the record without catalog_sha256)`. Reference kinds must agree
with their containing rows, IDs and refs are unique within each array, defaults must
equal exact rows in the catalog, quantities are strictly ascending and unique, and
the initial quantity is a member. Each layout action ID is unique. A nonnull
`bound_key` is the exact normalized key token the adapter maps to that semantic
action; null means button-only. Every layout exposes `SIMULATION_PLAY` and
`SIMULATION_PAUSE` as lifecycle actions. Player action IDs and their key bindings
come only from this digest-pinned projection.

Version 1 treats nonnull key tokens as case-sensitive and unique within a layout,
with exactly one exception: the `SIMULATION_PLAY` and `SIMULATION_PAUSE` lifecycle
pair may share one token to represent a toggle. Multiple null bindings are valid,
and token reuse across separate layouts is valid. The transport token for the
spacebar is the literal string `SPACE`; translation to a Qt or terminal key value
belongs to the UI keyboard adapter.

### 6.4 `SimulationProfileSelectionV1`

```text
schema_id: "KIRBY2_SIMULATION_PROFILE_SELECTION_V1"
schema_version: 1
profile_ref: SimulationProfileRefV1
seed: nonnegative integer
duration_us: positive integer
control_values: object from catalog-authored control_id to integer, string, or boolean
```

The selection does not repeat authoritative `regime` or `arrival_model_family`
fields. Those meanings come from the digest-pinned profile. The UI must not assemble
component objects or send unadvertised controls. Every catalog control appears
exactly once in `control_values`; the value type, range, step, scale, and enum
membership must match that catalog row.

### 6.5 `ResolvedSimulationConfigurationV1` and resolution

`ResolvedSimulationConfigurationV1` is the complete safe construction recipe that
crosses the UI boundary. It contains references and effective public values, not
instantiated models, matrices, distributions, or private runtime state.

```text
schema_id: "KIRBY2_RESOLVED_SIMULATION_CONFIGURATION_V1"
schema_version: 1
profile_ref: SimulationProfileRefV1
selection_sha256: SHA-256
engine_contract_id: "KIRBY2_SIMULATION_ENGINE_V1"
seed: nonnegative integer
duration_us: positive integer
arrival_model_family: "simple" | "hawkes"
regime:
    "BALANCED" | "BUY_PRESSURE" | "SELL_PRESSURE" | "MOMENTUM_UP" |
    "MOMENTUM_DOWN" | "ABSORPTION_BID" | "ABSORPTION_ASK" |
    "THIN_LIQUIDITY" | "LIQUIDITY_VACUUM" | "MEAN_REVERSION" |
    "HIGH_CANCELLATION" | "PANIC"
intraday_phase:
    "PREOPEN" | "OPENING" | "MORNING" | "MIDDAY" | "AFTERNOON" |
    "CLOSE" | "NOT_APPLICABLE"
relative_volume: "0.25x" | "0.50x" | "1.00x" | "2.00x" | "5.00x" | "10.00x"
liquidity: "VERY_THIN" | "THIN" | "NORMAL" | "DEEP" | "VERY_DEEP"
intensity_scale_ppm: nonnegative integer
scenario_definition_ref: SimulationComponentRefV1
regime_profile_ref: SimulationComponentRefV1
distribution_bundle_ref: SimulationComponentRefV1
queue_reactive_ref: SimulationComponentRefV1 | null
hawkes_ref: SimulationComponentRefV1 | null
intraday_ref: SimulationComponentRefV1 | null
effective_control_values: object from control_id to integer, string, or boolean
```

The engine contract ID must equal the selected catalog row and pins how component
bytes and effective controls are composed. It changes whenever that interpretation
changes. The component kinds must match their fields. `arrival_model_family == "simple"`
requires `hawkes_ref == null`; `"hawkes"` requires a `HAWKES` reference.
`intraday_phase == "NOT_APPLICABLE"` if and only if `intraday_ref == null`.
`effective_control_values` contains the same exact key set as the selected catalog
profile after validation. In version 1 it is byte-for-byte semantically equal to
the selection's `control_values`, including exact scalar types; there is no
normalization, coercion, default substitution, or clamping. It never contains
binary floats.

The resolved `seed` and `duration_us` equal the selection exactly. An `AVAILABLE`
configuration requires `duration_us` to be a positive whole number of seconds
(`duration_us % 1_000_000 == 0`). A structurally valid positive fractional-second
selection is preserved unchanged in a `REFUSED / INVALID_DURATION` resolution so
the UI can correlate and display the rejected request accurately.

All six resolved component-reference fields equal the corresponding selected
profile defaults exactly, including nulls. Version 1 does not derive, upgrade, or
substitute a different component reference during resolution.

`SimulationProfileResolutionV1` is self-contained so Start never depends on a
process-global “last resolved profile” cache:

```text
schema_id: "KIRBY2_SIMULATION_PROFILE_RESOLUTION_V1"
schema_version: 1
status: "AVAILABLE" | "REFUSED"
selection: SimulationProfileSelectionV1
selection_sha256: SHA-256
resolved_configuration_sha256: SHA-256 | null
resolved_configuration: ResolvedSimulationConfigurationV1 | null
refusal: ResolutionRefusalV1 | null

ResolutionRefusalV1:
    reason_code:
        "UNKNOWN_PROFILE" | "PROFILE_DIGEST_MISMATCH" |
        "UNSUPPORTED_COMPONENT_COMBINATION" | "COMPONENT_NOT_FOUND" |
        "COMPONENT_DIGEST_MISMATCH" | "UNKNOWN_CONTROL" |
        "CONTROL_VALUE_OUT_OF_RANGE" | "INVALID_INTRADAY_WINDOW" |
        "INVALID_DURATION"
    explanation: nonempty string
```

For `AVAILABLE`, both resolved fields are nonnull and `refusal` is null. For
`REFUSED`, both resolved fields are null and `refusal` is nonnull. In both cases the
selection digest must equal `H(selection)`. For an available result, the resolved
digest must equal `H(resolved_configuration)`, and the configuration's profile and
selection identities must equal the resolution's.

The resolver is the sole construction point for `SimpleFlowModel`,
`HawkesFlowModel`, queue-reactive modifiers, distribution profiles, and intraday
components. A refusal is data, not a partially constructed run.

All implemented setup, run-start, interaction, lifecycle, finalization, artifact-
verification, and Replay-provider slices publish backend-authored compatibility
records under `kirby2/ui/fixtures/simulation_contract_v1/`. `manifest.json` pins
the exact canonical bytes for 31 records. These cover the catalogs and selections,
available and refused resolution/Start paths, live interaction and lifecycle
results, finalized artifact/reference/verification records, and the Replay
provider's initial frame plus one event-step request/response. They are generated by
`kirby2.ui.simulation_facade.write_simulation_contract_golden_fixtures()`; they are
not hand-written examples. The current catalog deliberately contains only the 24
supported combinations formed by the twelve accepted regimes and the existing
simple/accepted-Hawkes arrival families.

At Start, the backend decodes the supplied available resolution, resolves its
embedded selection again against the current digest-pinned catalog and component
store, and compares the new canonical configuration bytes and digest to those in
the request. Any difference refuses Start before a session is created. No hidden
cache or object identity participates.

The implemented Start boundary is
`kirby2.ui.simulation_run_facade.start_simulation_run()`. It validates the complete
resolution and training records, rechecks every referenced component against the
current accepted sources, creates fresh simple or Hawkes runtime state, and emits a
detached initial `SimulationFrameV1`. A public execution identity is allocated only
after all typed refusal checks pass. The returned handle is intentionally opaque and
backend-private. The separately versioned interaction and lifecycle slices below
state which operations are callable; no UI surface may infer callable status from
Start alone.

The interaction slice at `78c82f01af640d20616347fd021f86b92db5cfd2`
implements exact command, command-outcome, advance, and current-frame records plus
their backend facade operations. Commands are semantic-action based and fenced by
source run, origin frame, and origin cursor. Every processed command publishes one
complete next frame even when the domain action is rejected; stale or otherwise
unavailable calls do not mutate the handle. Absolute-time advance publishes the
first complete frame at the duration boundary, and current-frame recovery returns
the exact already-published frame without changing any sequence. The golden
manifest now covers those interaction records alongside the setup and Start records.

The reset/close lifecycle slice at
`9fa910fa475716cd2e6e1ce4bfd0f87a4ddd730f` implements strict reset-preparation,
reset-commit, and close records; a private two-phase replacement handle; mutation
fencing while a replacement is pending; discard recovery; atomic commit; and
idempotent close. The golden manifest now covers 21 mechanically generated records,
including successful reset preparation, a commit mismatch, successful commit,
abandoned-source recovery, exact repeated close behavior, and a conflicting close.
Finalization and immutable artifact persistence landed at
`ccfc9669cc46c29dca226bb5481b13210394d2ca`. Deep artifact verification landed at
`49b8854d7739ad59cd3109d319c946e643c3c193`, and the verified Replay provider landed
at `655ccf495b015f2067f11d63adcf3dd63e4e4609`. The current golden manifest covers
31 mechanically generated records, including verification receipts and the Replay
provider's initial-frame and event-step transport examples.

### 6.6 `SimulationTrainingOptionsV1`

```text
schema_id: "KIRBY2_SIMULATION_TRAINING_OPTIONS_V1"
schema_version: 1
quantity_options: nonempty ordered array of positive integers
initial_quantity: positive integer
layout_ref: SimulationComponentRefV1
strategy_ref: SimulationComponentRefV1 | null
objective: ObjectiveDefinitionV1 | null
curriculum_drill_ref: SimulationComponentRefV1 | null
initial_run_state: "READY" | "RUNNING"
observation_policy_ref: SimulationComponentRefV1

ObjectiveDefinitionV1:
    objective_type: "ACQUIRE" | "LIQUIDATE" | "ROUND_TRIP" | "OBSERVE_ONLY"
    target_quantity: nonnegative integer
    time_limit_us: positive integer
    preferred_slippage_ticks: nonnegative integer
```

`quantity_options` is strictly ascending and unique, and contains
`initial_quantity`. Reference kinds must be `HOTKEY_LAYOUT`,
`STRATEGY_DEFINITION`, `CURRICULUM_DRILL`, and `OBSERVATION_POLICY` respectively.
`OBSERVE_ONLY` requires target zero; every trading objective requires a positive
target. An objective time limit cannot exceed the resolved run duration. If a
curriculum drill is present, its digest-pinned profile, seed, duration, dimensions,
layout, strategy, and objective must agree with this request or Start is refused;
there is no precedence fallback.

`initial_run_state`, quantity selection, layout, strategy, objective, curriculum,
and observation policy affect run behavior and therefore enter
`run_request_sha256`. UI speed, repaint cadence, ladder row count, window layout,
theme, and save destination do not.

`SimulationStartResultV1` has:

```text
schema_id: "KIRBY2_SIMULATION_START_RESULT_V1"
schema_version: 1
result_id: content-derived ID
status: "AVAILABLE" | "REFUSED"
source_run_id: string | null
run_request_sha256: SHA-256 | null
initial_frame: SimulationFrameV1 | null
refusal: StartRefusalV1 | null

StartRefusalV1:
    reason_code:
        "RESOLUTION_NOT_AVAILABLE" | "RESOLUTION_CHANGED" |
        "COMPONENT_NOT_FOUND" | "COMPONENT_DIGEST_MISMATCH" |
        "INVALID_TRAINING_OPTIONS" | "CURRICULUM_CONFLICT" |
        "OBJECTIVE_EXCEEDS_DURATION"
    explanation: nonempty string
```

An available result has all three run fields, no refusal, and identities that agree
with the initial frame. A refused result has null run fields and a refusal; no
handle, engine, store entry, or run ID is created. Its ID is
`"simulation-start-result-" + H(the record without result_id)[0:24]`.

### 6.7 `SimulationFrameV1`

```text
schema_id: "KIRBY2_SIMULATION_FRAME_V1"
schema_version: 1
frame_id: content-derived ID
frame_sequence: positive integer
source_run_id: string
run_request_sha256: SHA-256
resolved_configuration_sha256: SHA-256
profile_ref: SimulationProfileRefV1

cursor: SimulationCursorV1
    cursor_id: content-derived ID
    source_run_id: string
    simulation_time_us: nonnegative integer
    duration_us: positive integer
    run_state: "READY" | "RUNNING" | "PAUSED" | "COMPLETE"
    input_sequence: nonnegative integer
    flow_sequence: nonnegative integer
    exchange_event_sequence: nonnegative integer
    trade_sequence: nonnegative integer

market_state:
    market_state_id: string
    market_state_time_us: nonnegative integer
    book_state_sha256: SHA-256

instrument:
    instrument_id: string
    symbol: string
    display_name: string
    venue_labels: ordered array of strings
    tick_numerator: positive integer
    tick_denominator: positive integer
    price_precision: nonnegative integer
    lot_size: positive integer

clock:
    time_basis: "SIMULATION_ELAPSED" | "SESSION_CLOCK"
    session_origin_time_us: nonnegative integer | null
    display_precision_us: positive integer
    cursor_label: string
    intraday_phase:
        "PREOPEN" | "OPENING" | "MORNING" | "MIDDAY" | "AFTERNOON" |
        "CLOSE" | "NOT_APPLICABLE"

book:
    bids: ordered array of Level2LevelV1
    asks: ordered array of Level2LevelV1

recent_trades: ordered bounded array of RecentTradeV1
working_orders: ordered array of WorkingOrderV1
account: AccountProjectionV1
strategy: StrategyProjectionV1
objective: ObjectiveProjectionV1
diagnostics: ordered array of DiagnosticRowV1
metrics: ordered array of MetricV1
status_message: string
status_role: "NEUTRAL" | "READY" | "RUNNING" | "PAUSED" | "COMPLETE" | "WARNING" | "ERROR"
provenance: SimulationProvenanceV1
```

The initial frame has `frame_sequence == 1`. Within one `source_run_id`, every
successfully returned destination frame increments it by exactly one, including a
processed-but-rejected player command or a lifecycle change that leaves the book
unchanged. All cursor sequences are run-local and nondecreasing. The source run,
request, configuration, profile, and cursor identities must agree everywhere in a
frame.

Cursor time is between zero and duration inclusive. `READY` exists only at time
zero; `RUNNING` and `PAUSED` require time below duration; `COMPLETE` is present if
and only if time equals duration. Input, flow, exchange-event, and trade sequences
equal the total canonical records of each kind through that cursor. A trade's
exchange-event sequence cannot exceed the cursor exchange-event sequence.

`SIMULATION_ELAPSED` requires a null session origin and a backend-authored `T+...`
label. `SESSION_CLOCK` requires `session_origin_time_us` in microseconds since local
session midnight. An inactive intraday component requires `NOT_APPLICABLE`; an
active component requires one of the six real phases. The backend owns the label
and phase transition. `display_precision_us` must be a power of ten no greater than
1,000,000 and must divide 1,000,000. `market_state_time_us` cannot exceed cursor
time. `status_role` is sealed semantic styling metadata and must agree with the run
state unless the backend is surfacing `WARNING` or `ERROR`.

Instrument tick numerator and denominator are coprime. The denominator must divide
`10 ** price_precision`; consequently every tick price is represented exactly,
without rounding. Each `display_price` is the unsigned base-10 value of
`price_ticks * tick_numerator / tick_denominator`, with no grouping and exactly
`price_precision` fractional digits (or no decimal point when precision is zero).

`Level2LevelV1`:

```text
price_ticks: positive integer
display_price: canonical display string
aggregate_quantity: nonnegative integer
player_quantity: nonnegative integer
first_player_queue_ahead: QueueAheadV1
```

The backend declares and preserves book ordering. Version 1 uses best-to-worst
order: bids descend by `price_ticks`, asks ascend by `price_ticks`. The UI validates
that order and renders it in its chosen visual direction without re-sorting semantic
data. Prices are unique within a side and `player_quantity <= aggregate_quantity`.

`first_player_queue_ahead` is deliberately named: when player orders exist at the
level it means the total remaining quantity strictly ahead of the earliest resting
player order, including no quantity behind it. Per-order queue values live on the
working-order records. This lets the ladder retain its queue column without
pretending that one value describes every player order at the price.

`RecentTradeV1`:

```text
trade_sequence: positive integer
exchange_event_sequence: positive integer
trade_id: string
simulation_time_us: nonnegative integer
price_ticks: positive integer
display_price: canonical display string
quantity: positive integer
aggressor_side: "BUY" | "SELL"
```

Trade sequences are one-based, unique, and contiguous for a run. The frame supplies
the canonical tail `max(1, cursor.trade_sequence - 255)` through
`cursor.trade_sequence`, ordered oldest-to-newest by increasing trade sequence; it
is empty exactly when the cursor trade sequence is zero. Equal timestamps preserve
exchange-event order. The complete tape belongs to the finalized artifact and is
not copied through every frame. A UI may visually reverse this array, but must not
change its semantic order or imply that omitted older rows were absent.

`QueueAheadV1`:

```text
availability: "AVAILABLE" | "UNAVAILABLE" | "NOT_APPLICABLE"
quantity: nonnegative integer | null
reason:
    null | "HIDDEN_BY_OBSERVATION_POLICY" | "CAPABILITY_NOT_AVAILABLE" |
    "NO_PLAYER_ORDER_AT_LEVEL"
```

`AVAILABLE` requires a quantity and null reason. `UNAVAILABLE` requires null
quantity and one of the first two reasons. `NOT_APPLICABLE` requires null quantity
and `NO_PLAYER_ORDER_AT_LEVEL`; it is allowed on a book level only. The default
local synthetic policy may disclose queue ahead for the player's own resting
orders. Any policy that does not explicitly allow that disclosure returns
`UNAVAILABLE`. The UI never derives queue position from timestamps, row position,
aggregate depth, or display geometry.

`WorkingOrderV1`:

```text
order_id: string
side: "BUY" | "SELL"
price_ticks: positive integer
display_price: canonical display string
remaining_quantity: positive integer
filled_quantity: nonnegative integer
resting_sequence: positive integer
queue_ahead: QueueAheadV1
```

Working orders are active orders only, ordered by `(resting_sequence, order_id)`.
Their queue status is `AVAILABLE` or `UNAVAILABLE`, never `NOT_APPLICABLE`. An
available value is the sum of remaining quantity of every FIFO order strictly
before that exact order at its price, including earlier player orders.

`AccountProjectionV1`:

```text
selected_quantity: positive integer
position: integer
bought_quantity: nonnegative integer
sold_quantity: nonnegative integer
working_order_count: nonnegative integer
```

`position == bought_quantity - sold_quantity`; all three values come only from
matching-engine fills. Working-order count equals the frame array length. Cash,
P&L, exposure, and buying power remain absent until an authoritative ledger exists.

`StrategyProjectionV1`:

```text
configured: boolean
strategy_kind: "TRAFFIC_LIGHT" | "STATE_MACHINE" | null
traffic_light: "GREEN" | "WAIT" | "RED" | "UNCONFIGURED"
traffic_setup: string | null
strategy_state: string | null
entry_permission: "ALLOW" | "DENY" | "UNRESTRICTED"
exit_permission: "ALLOW" | "DENY" | "UNRESTRICTED"
reason: nonempty string
```

When unconfigured, kind/setup/state are null, the light is `UNCONFIGURED`, and both
permissions are `UNRESTRICTED`. When configured as `TRAFFIC_LIGHT`, setup is
nonempty, the light is `GREEN`, `WAIT`, or `RED`, state is null, and both permissions
are `UNRESTRICTED`. When configured as `STATE_MACHINE`, setup and state are nonempty,
the light is `GREEN`, `WAIT`, or `RED`, and each permission is `ALLOW` or `DENY`.
No other combination is valid. The reason is backend-authored evidence text, not a
UI inference.

`ObjectiveProjectionV1`:

```text
configured: boolean
objective_type: "ACQUIRE" | "LIQUIDATE" | "ROUND_TRIP" | "OBSERVE_ONLY" | null
target_quantity: nonnegative integer
completed_quantity: nonnegative integer
completion_ppm: integer from 0 through 1_000_000
display_completion: canonical display string
time_limit_us: positive integer | null
preferred_slippage_ticks: nonnegative integer | null
complete: boolean
completion_time_us: nonnegative integer | null
```

When unconfigured, type, time limit, preferred slippage, and completion time are
null; both quantities and completion are zero; `complete` is false; and display
completion is `"0%"`. A configured row repeats every field of the exact objective
definition. For a trading objective, `0 <= completed_quantity <= target_quantity`,
`complete` is true exactly when the two quantities are equal, and completion time is
nonnull exactly when complete. Its rounded-half-up completion is:

```text
completion_ppm = min(
    1_000_000,
    floor((2 * completed_quantity * 1_000_000 + target_quantity) /
          (2 * target_quantity)),
)
```

`OBSERVE_ONLY` has target zero, completed quantity zero, and is complete at time
zero with `completion_ppm == 1_000_000`. `display_completion` renders
`completion_ppm / 10_000` as a base-10 percentage with at most two fractional
digits, trailing fractional zeroes removed, followed by `%`.

`MetricV1`:

```text
metric_id: string
label: string
availability: "AVAILABLE" | "UNAVAILABLE" | "NOT_APPLICABLE"
scaled_value: integer | null
display_value: string | null
scale: positive power-of-ten integer
unit: string
sample_count: nonnegative integer
aggregation_scope: "INSTANTANEOUS" | "ROLLING_WINDOW" | "RUN_TO_CURSOR"
window_us: positive integer | null
heuristic: boolean
as_of_exchange_event_sequence: nonnegative integer
unavailable_reason: string | null
semantic_role:
    "NEUTRAL" | "BID" | "ASK" | "POSITIVE" | "NEGATIVE" |
    "WARNING" | "UNAVAILABLE"
```

The numeric value in the declared unit is `scaled_value / scale`; scale 1 is used
for exact counts, shares, integer ticks, and microseconds. A percentage uses unit
`PERCENT`, so 50 percent is `50_000_000 / 1_000_000`. `ROLLING_WINDOW` requires a
positive window; `INSTANTANEOUS` and `RUN_TO_CURSOR` require a null window. The UI
never chooses or silently substitutes a metric window.

`AVAILABLE` requires value and display value, and a null unavailable reason.
`UNAVAILABLE` and `NOT_APPLICABLE` require both values null and a nonempty reason.
Zero is a measurement, never a missing-value sentinel. Zero denominators,
incomplete horizons, or absent qualifying shocks produce typed unavailability, not
`999`, infinity, or an invented value. `as_of_exchange_event_sequence` cannot exceed
the frame cursor. Heuristic rows set `heuristic == true`, and the UI preserves that
disclosure.

`DiagnosticRowV1`:

```text
diagnostic_id: string
label: string
availability: "AVAILABLE" | "UNAVAILABLE" | "NOT_APPLICABLE"
status: "PASS" | "WARN" | "FAIL" | "INFO" | "UNAVAILABLE" | "NOT_APPLICABLE"
display_value: string | null
unit: string | null
explanation: nonempty string
as_of_exchange_event_sequence: nonnegative integer
unavailable_reason: string | null
semantic_role:
    "NEUTRAL" | "BID" | "ASK" | "POSITIVE" | "NEGATIVE" |
    "WARNING" | "UNAVAILABLE"
```

Available diagnostics use one of `PASS`, `WARN`, `FAIL`, or `INFO`, have a display
value, and have no unavailable reason. Unavailable/not-applicable rows use the
matching status, have no display value, and have a nonempty reason. Diagnostic and
metric IDs are unique within their ordered arrays. Diagnostics describe runtime
facts only; they are not release, audit, or performance acceptance evidence.
Unavailable rows use semantic role `UNAVAILABLE`; `WARN` and `FAIL` rows use
`WARNING`. These roles, bid/ask roles, lifecycle roles, provenance, and strategy
light meanings are sealed accessibility roles: user themes may skin them only
within contrast-safe mappings and cannot reassign their meaning.

Metrics are calculated by the backend. The initial live set should remain bounded:
spread, top-of-book depth, imbalance, add/cancel/execute counts or ratios, aggressive
flow imbalance, and price displacement. Price impact, volatility, and resilience may
be added when their exact backend definitions are implemented; the UI must not
invent formulas for missing metrics.

`SimulationProvenanceV1` has exactly:

```text
classification: "SYNTHETIC_SIMULATION_ONLY"
real_market_data: false
matching_engine_derived: true
generation_method: "ORDER_FLOW_THROUGH_MATCHING_ENGINE"
level2_origin: "MATCHING_ENGINE_BOOK_STATE"
profile_sha256: SHA-256
resolved_configuration_sha256: SHA-256
run_request_sha256: SHA-256
observation_policy_ref: SimulationComponentRefV1
display_label: string
```

The first five semantic values are sealed. A theme cannot recolor or relabel them
into a real-market claim. Profile/configuration/request digests must match their
frame roots, and the observation-policy reference must match the training options.

### 6.8 Commands and lifecycle mutations

`SimulationCommandRequestV1` is the canonical semantic action request:

```text
schema_id: "KIRBY2_SIMULATION_COMMAND_REQUEST_V1"
schema_version: 1
command_id: content-derived ID
source_run_id: string
origin_frame_id: string
origin_cursor_id: string
semantic_action_id: string
parameters: {}
```

Version 1 accepts only the exact empty parameters object: quantity changes and order
choices are semantic action IDs from the digest-pinned hotkey layout, not arbitrary
order payloads. Required lifecycle IDs are `SIMULATION_PLAY` and
`SIMULATION_PAUSE`; player action IDs come from the active layout. The ID recipe is:

```text
command_id = "simulation-command-" +
    H(SimulationCommandRequestV1 without command_id)[0:24]
```

UI input source and originating-view metadata remain in the UI sidecar. They are not
accepted as canonical backend command fields.

`SimulationCommandResultV1`:

```text
schema_id: "KIRBY2_SIMULATION_COMMAND_RESULT_V1"
schema_version: 1
result_id: content-derived ID
status: "AVAILABLE" | "UNAVAILABLE"
command_id: string
source_run_id: string
origin_frame_id: string
origin_cursor_id: string
outcome: SimulationCommandOutcomeV1 | null
destination_frame: SimulationFrameV1 | null
unavailable_reason: string | null

SimulationCommandOutcomeV1:
    action_kind: "PLAYER_ACTION" | "LIFECYCLE"
    semantic_action_id: string
    accepted: boolean
    message: nonempty string
    rejection_reason: string | null
    input_sequence: positive integer | null
    resulting_order_ids: ordered array of strings
```

For `AVAILABLE`, outcome and a complete destination frame are mandatory and the
unavailable reason is null. `accepted == false` is a processed domain rejection,
still returns the complete post-command frame, and requires a nonempty rejection
reason. A player action always has an input sequence equal to the destination cursor
input sequence; a lifecycle action has a null input sequence and no resulting order
IDs. `SIMULATION_PLAY` transitions `READY` or `PAUSED` to `RUNNING`;
`SIMULATION_PAUSE` transitions `RUNNING` to `PAUSED`. An inapplicable lifecycle
request is a processed result with `accepted == false`.

For `UNAVAILABLE`, outcome and destination frame are null and the reason is one of
`STALE_ORIGIN`, `SOURCE_RUN_MISMATCH`, `RUN_COMPLETE`, `RUN_FINALIZED`, or
`RESET_PENDING`; the visible frame is unchanged. The result echoes command and
origin identities exactly.

```text
result_id = "simulation-command-result-" +
    H(SimulationCommandResultV1 without result_id)[0:24]
```

### 6.9 Advance and reset results

`SimulationAdvanceResultV1`:

```text
schema_id: "KIRBY2_SIMULATION_ADVANCE_RESULT_V1"
schema_version: 1
result_id: content-derived ID
status: "AVAILABLE" | "UNAVAILABLE"
source_run_id: string
origin_frame_id: string
origin_cursor_id: string
target_time_us: nonnegative integer
destination_frame: SimulationFrameV1 | null
unavailable_reason: string | null
```

An available result contains exactly one complete destination frame. A valid domain
unavailability contains no partial frame and does not replace the visible frame.
The closed unavailable set is `STALE_ORIGIN`, `SOURCE_RUN_MISMATCH`,
`RUN_NOT_RUNNING`, `TARGET_NOT_AFTER_CURSOR`, `RUN_COMPLETE`, `RUN_FINALIZED`, and
`RESET_PENDING`. The result echoes run and origin identities, and:

```text
result_id = "simulation-advance-result-" +
    H(SimulationAdvanceResultV1 without result_id)[0:24]
```

The first advance whose target reaches the configured duration returns `AVAILABLE`
with the final complete frame: cursor time equals duration and run state is
`COMPLETE`. Only a subsequent advance from that frame returns unavailable
`RUN_COMPLETE`. The UI must therefore commit the final frame before offering
finalization or Replay.

Reset is a source replacement, not a market event and not an in-place clock rewind.
`SimulationResetResultV1` is a prepared, not-yet-committed replacement:

```text
schema_id: "KIRBY2_SIMULATION_RESET_RESULT_V1"
schema_version: 1
result_id: content-derived ID
status: "AVAILABLE" | "UNAVAILABLE"
previous_source_run_id: string
origin_frame_id: string
origin_cursor_id: string
reset_token_id: string | null
previous_run_disposition_on_commit: "ABANDONED_BY_RESET" | null
new_source_run_id: string | null
run_request_sha256: SHA-256 | null
initial_frame: SimulationFrameV1 | null
unavailable_reason: string | null
```

An available preparation reconstructs from the exact same available resolution and
training options, mints a never-reused source run ID and reset token, and returns a
private pending-reset handle plus this public result. The initial frame has time,
input, flow, and trade sequences zero; frame sequence 1; deterministic initial book
content; and the original configured `READY` or `RUNNING` state. Its exchange-event
sequence retains the deterministic events used to seed that authoritative initial
book, so it is nonzero when the matching engine starts with resting liquidity. The
old handle remains intact but temporarily refuses mutations with `RESET_PENDING`.

For an unavailable preparation, the token, the three new-source fields, and the
pending disposition are null; the reason is `STALE_ORIGIN`,
`SOURCE_RUN_MISMATCH`, `RUN_FINALIZED`, or `RESET_PENDING`, and the old handle/frame
remain active. Its exact ID recipe is:

```text
result_id = "simulation-reset-result-" +
    H(SimulationResetResultV1 without result_id)[0:24]
```

After off-stack validation, the UI either commits or discards the private pending
handle. `commit_simulation_reset()` atomically closes the old handle as
`ABANDONED_BY_RESET` and activates the prepared handle; it returns the new active
handle plus:

```text
SimulationResetCommitResultV1:
    schema_id: "KIRBY2_SIMULATION_RESET_COMMIT_RESULT_V1"
    schema_version: 1
    status: "COMMITTED" | "UNAVAILABLE"
    reset_token_id: string
    previous_source_run_id: string
    new_source_run_id: string | null
    initial_frame_id: string | null
    previous_run_disposition: "ABANDONED_BY_RESET" | null
    unavailable_reason: null | "UNKNOWN_RESET_TOKEN" | "RESET_TOKEN_MISMATCH"
```

A commit result uses all nonnull destination/disposition fields exactly when
`COMMITTED`; its expected source/frame arguments must equal the validated prepared
result or it returns `RESET_TOKEN_MISMATCH` without closing the old handle.
`discard_simulation_reset()` destroys only the pending replacement,
unfreezes the old handle, and returns no canonical DTO. A previously finalized
artifact remains immutable; neither path silently finalizes or overwrites the old
run.

`SimulationCurrentFrameResultV1` is the non-mutating recovery surface:

```text
schema_id: "KIRBY2_SIMULATION_CURRENT_FRAME_RESULT_V1"
schema_version: 1
status: "AVAILABLE" | "UNAVAILABLE"
source_run_id: string
current_frame: SimulationFrameV1 | null
unavailable_reason: null | "SOURCE_RUN_MISMATCH" | "RUN_ABANDONED"
```

An available read returns the exact current frame without incrementing any sequence
or changing its ID. If a command/advance response fails UI projection after the
backend mutated, the adapter performs this one governed resnapshot. If that frame
also fails, the controller enters `INTEGRITY_LOCKED`, disables all mutation and
finalization controls, retains the last visibly valid frame as explicitly stale,
and requires closing or restarting the run. Closing calls
`close_simulation_run(handle, "INTEGRITY_LOCKED")`. It never continues sending
commands from an origin known to be stale.

`SimulationCloseResultV1` closes an unfinalized handle without creating a Replay
artifact:

```text
schema_id: "KIRBY2_SIMULATION_CLOSE_RESULT_V1"
schema_version: 1
status: "CLOSED" | "UNAVAILABLE"
source_run_id: string
disposition:
    "UNPUBLISHED_START_REJECTED_BY_UI" | "INTEGRITY_LOCKED" |
    "USER_ABANDONED"
unavailable_reason: null | "RUN_FINALIZED" | "DISPOSITION_MISMATCH"
```

`CLOSED` requires a null reason. A repeated close of the same handle with the same
disposition returns the exact same `CLOSED` result; it does not leak or recreate the
run. A different disposition after closure returns `DISPOSITION_MISMATCH`.
Finalized runs return `RUN_FINALIZED` and retain their immutable artifact. The
private handle, not a possibly malformed initial DTO, is sufficient to identify the
run for cleanup.

### 6.10 Finalization and Replay reference

`SimulationReplayArtifactV1` is backend storage, not a Qt DTO, but its stored bytes
and digest projection are exact:

```text
schema_id: "KIRBY2_SIMULATION_REPLAY_ARTIFACT_V1"
schema_version: 1
source_run_id: string
replay_run_id: string
profile_ref: SimulationProfileRefV1
selection: SimulationProfileSelectionV1
resolved_configuration_sha256: SHA-256
resolved_configuration: ResolvedSimulationConfigurationV1
training_options: SimulationTrainingOptionsV1
run_request_sha256: SHA-256
component_payloads: ordered array of EmbeddedComponentV1
session_recording: EmbeddedSessionRecordingV1
event_tape: ordered array of SimulationTimelineEventV1
event_tape_sha256: SHA-256
terminal_status: "COMPLETE" | "SAVED_PARTIAL"
final_frame: SimulationFrameV1
provenance: SimulationProvenanceV1

EmbeddedComponentV1:
    component_ref: SimulationComponentRefV1
    payload: exact strict-JSON component payload object

EmbeddedSessionRecordingV1:
    media_type: "application/vnd.kirby2.session-recording+json"
    recording_schema_version: 2
    encoding: "BASE64_RFC4648"
    bytes_base64: padded RFC 4648 base64 string with no whitespace
    bytes_sha256: SHA-256 of the decoded exact recording bytes

SimulationTimelineEventV1:
    sequence: positive integer
    simulation_time_us: nonnegative integer
    kind:
        "INPUT" | "COMMAND" | "REJECTED" | "PARTIAL_FILL" | "FILL" |
        "POSITION" | "CANCEL" | "REPLACE" | "TRAFFIC" |
        "STRATEGY_EVALUATION" | "OBJECTIVE" | "CURRICULUM" | "MID" |
        "BOOK"
    message: nonempty string
    data: exact strict-JSON object
```

Component payloads are sorted by
`(component_kind, component_id, component_version, content_sha256)`, contain every
distinct reference reachable from the resolved configuration and training options
exactly once, and satisfy `H(payload) == component_ref.content_sha256`. Timeline
sequences are contiguous from one and timestamps are nondecreasing. The event-tape
digest is `H(event_tape)` and must equal the run result. The embedded recording bytes
must load as the declared version-2 `SessionRecording`; their digest is over decoded
bytes, not the base64 text. The exact stored artifact bytes are `C(the complete
SimulationReplayArtifactV1 object)` and `artifact_sha256` is their ordinary SHA-256.
The provider bridge reconstructs unified-profile runs from this envelope and its
embedded component payloads; it must not ask legacy `SessionRecording` alone to
rediscover Hawkes, queue-reactive, distribution, intraday, layout, or policy state.

The live-to-Replay ID mapping preserves the existing WO36 Replay grammar without
pretending the two IDs are the same:

```text
replay_run_id = "run-" + H({
    "schema_id": "KIRBY2_LIVE_TO_REPLAY_RUN_ID_V1",
    "schema_version": 1,
    "source_run_id": source_run_id,
})[0:24]
```

`ReplayArtifactRefV1` is the exact immutable handoff to Replay:

```text
schema_id: "KIRBY2_REPLAY_ARTIFACT_REFERENCE_V1"
schema_version: 1
artifact_id: content-derived ID
artifact_kind: "SIMULATION_REPLAY_ARTIFACT"
artifact_schema_id: "KIRBY2_SIMULATION_REPLAY_ARTIFACT_V1"
artifact_schema_version: 1
artifact_sha256: SHA-256
source_run_id: string
replay_run_id: string
store_id: string
object_key: string
```

The artifact digest hashes the exact immutable bytes defined above, and
`artifact_id == "replay-artifact-" + artifact_sha256[0:24]`. `store_id` and
`object_key` are governed operational locators. Host paths, URLs, credentials, and
open file handles are forbidden in this record and do not enter artifact identity.

The current version-1 implementation uses the governed process-local immutable store
`kirby2-in-process-replay-store-v1`. It provides exact same-process finalization and
Replay handoff without writing user data or temporary artifacts to the host
filesystem. It is intentionally not restart-durable. Cross-process persistence is a
future storage contract and must preserve these exact artifact bytes and reference
semantics rather than silently re-encoding them.

`SimulationRunResultV1`:

```text
schema_id: "KIRBY2_SIMULATION_RUN_RESULT_V1"
schema_version: 1
result_id: content-derived ID
source_run_id: string
replay_run_id: string
profile_ref: SimulationProfileRefV1
selection_sha256: SHA-256
resolved_configuration_sha256: SHA-256
run_request_sha256: SHA-256
terminal_status: "COMPLETE" | "SAVED_PARTIAL"
final_frame_id: string
final_cursor: exact SimulationCursorV1
final_book_state_sha256: SHA-256
event_tape_sha256: SHA-256
replay_artifact: ReplayArtifactRefV1
metrics: ordered array of MetricV1
diagnostics: ordered array of DiagnosticRowV1
provenance: SimulationProvenanceV1
```

This record stays off the per-frame rendering path. It bridges a completed live run
into recording and later WO36 Replay processing. Replay Studio receives a run or
recording reference, never an active engine or private event inventory.

The live source run, mapped Replay run, profile, configuration, request, final
cursor/frame/book, tape, and artifact identities must reconcile.
`terminal_status == "COMPLETE"` requires a complete final cursor; `SAVED_PARTIAL`
requires a non-complete final cursor. The result recipe is:

```text
result_id = "simulation-run-result-" +
    H(SimulationRunResultV1 without result_id)[0:24]
```

`SimulationFinalizeResultV1`:

```text
schema_id: "KIRBY2_SIMULATION_FINALIZE_RESULT_V1"
schema_version: 1
result_id: content-derived ID
status: "AVAILABLE" | "UNAVAILABLE"
mode: "COMPLETE_ONLY" | "ALLOW_PARTIAL"
source_run_id: string
origin_frame_id: string
origin_cursor_id: string
run_result: SimulationRunResultV1 | null
unavailable_reason: string | null
```

`AVAILABLE` requires a run result and null reason. `UNAVAILABLE` requires a null
result and one of `STALE_ORIGIN`, `SOURCE_RUN_MISMATCH`, `RUN_NOT_COMPLETE`,
`ALREADY_ABANDONED`, or `RESET_PENDING`. The result ID is
`"simulation-finalize-result-" + H(the record without result_id)[0:24]`.
Successful finalization is idempotent: repeated calls for the same run and mode
return the exact same run result and artifact reference without rewriting or
duplicating bytes. A repeat after successful finalization is therefore `AVAILABLE`,
not `RUN_FINALIZED`.

Artifact resolution returns a public verification receipt alongside a private
source handle:

```text
ReplayArtifactVerificationReceiptV1:
    schema_id: "KIRBY2_REPLAY_ARTIFACT_VERIFICATION_RECEIPT_V1"
    schema_version: 1
    status: "AVAILABLE" | "UNAVAILABLE"
    artifact_ref: ReplayArtifactRefV1
    verified_artifact_sha256: SHA-256 | null
    verified_recording_sha256: SHA-256 | null
    verified_event_tape_sha256: SHA-256 | null
    source_run_id: string | null
    replay_run_id: string | null
    unavailable_reason:
        null | "UNKNOWN_STORE" | "OBJECT_NOT_FOUND" |
        "UNSUPPORTED_ARTIFACT_SCHEMA" | "REFERENCE_MISMATCH" |
        "ARTIFACT_DIGEST_MISMATCH" | "RECORDING_DIGEST_MISMATCH" |
        "EVENT_TAPE_DIGEST_MISMATCH"
```

`AVAILABLE` requires all verified fields, a null reason, and exact equality to the
reference, artifact, and run-result identities. `UNAVAILABLE` requires null verified
and run fields plus a nonnull reason. Only the backend reads or re-hashes stored
bytes. The private source handle produced with an available receipt is the sole
input to a provider bridge implementing the unchanged WO36 public Replay
timeline/frame calls; Replay frames use `replay_run_id`, not the live
`source_run_id`.

## 7. Target backend facade

The backend must expose the following semantic operations through a UI-compatible
facade. Internal Python objects may remain opaque inside `KirbyBackend`; Qt widgets
receive only detached standard-library values or UI-owned projections.

At backend commit `655ccf495b015f2067f11d63adcf3dd63e4e4609`, every operation
below is implemented and exported. This includes deep artifact resolution and the
verified Replay provider; neither is merely a target contract.

```text
list_simulation_profiles()
    -> SimulationProfileCatalogV1

list_simulation_training_resources()
    -> SimulationTrainingResourceCatalogV1

resolve_simulation_profile(SimulationProfileSelectionV1)
    -> SimulationProfileResolutionV1

start_simulation_run(
    SimulationProfileResolutionV1,
    SimulationTrainingOptionsV1,
)
    -> (opaque handle | null, SimulationStartResultV1)

dispatch_simulation_command(handle, SimulationCommandRequestV1)
    -> SimulationCommandResultV1

advance_simulation_run(
    handle,
    source_run_id,
    origin_frame_id,
    origin_cursor_id,
    target_time_us,
)
    -> SimulationAdvanceResultV1

prepare_simulation_reset(
    handle,
    source_run_id,
    origin_frame_id,
    origin_cursor_id,
)
    -> (opaque pending-reset handle | null, SimulationResetResultV1)

commit_simulation_reset(
    handle,
    pending-reset handle,
    reset_token_id,
    expected_new_source_run_id,
    expected_initial_frame_id,
)
    -> (new opaque active handle | null, SimulationResetCommitResultV1)

discard_simulation_reset(handle, pending-reset handle)
    -> None

read_current_simulation_frame(handle, source_run_id)
    -> SimulationCurrentFrameResultV1

close_simulation_run(handle, disposition)
    -> SimulationCloseResultV1

finalize_simulation_run(
    handle,
    source_run_id,
    origin_frame_id,
    origin_cursor_id,
    mode,
)
    -> SimulationFinalizeResultV1

resolve_replay_artifact(ReplayArtifactRefV1)
    -> (opaque verified replay-source handle | null,
        ReplayArtifactVerificationReceiptV1)

build_replay_provider(opaque verified replay-source handle)
    -> opaque provider with initial_frame() and respond(request)
```

The provider's detached adapter envelope exactly matches the UI-owned
`ReplayTransportRequest.as_dict()` shape:

```text
provider.initial_frame()
    -> ReplayPresentationFrameV1 dictionary

provider.respond({
    "request_id": "replay-request-00000001",
    "source_generation": nonnegative integer,
    "origin": {
        "cursor_id": string,
        "frame_id": string,
        "observation_mode": "AS_OBSERVED",
        "policy_id": "MICROSCOPE_AS_OBSERVED_V1",
        "render_cursor_time_us": nonnegative integer,
        "source_event_sha256": SHA-256,
        "source_run_id": replay run ID,
        "timeline_id": string,
    },
    "command": {
        "operation": "PLAY" | "PAUSE" | "EVENT_STEP" |
                     "FIXED_TIME_STEP" | "JUMP",
        "direction": null | "PREVIOUS" | "NEXT",
        "fixed_step_us": null | positive integer,
        "jump_target": null | existing WO36 jump-target token,
    },
}) -> {
    "request_id": exact request ID,
    "source_generation": exact source generation,
    "kind": "PLAYBACK" | "NAVIGATION",
    "navigation_payload": null | TimelineNavigationResult dictionary,
    "frame_payload": null | ReplayPresentationFrameV1 dictionary,
}
```

`PLAY` and `PAUSE` return `PLAYBACK`, no navigation payload, and one complete
frame. The three navigation operations return `NAVIGATION`; `frame_payload` is
present exactly when the navigation result is `AVAILABLE`. The provider accepts an
origin only when all eight fields identify a frame previously issued by that same
provider. Mutated, foreign, or stale origin records fail before navigation. The
provider returns fresh detached dictionaries on every call.

The exact in-process handle types are backend-private, held only by the UI adapter,
and absent from canonical JSON. Start re-resolves the embedded selection and verifies
all component bytes as specified in section 6.5. The Replay resolver verifies the
artifact reference, loads only from the governed `store_id`/`object_key`, re-hashes
the immutable bytes, and returns no source on any mismatch. The provider bridge is
the only consumer of its private verified handle. Widgets never receive an absolute
path, raw event tape, or recording internals.

Names in the signatures denote wire schemas, not Python dataclass instances. Except
for explicitly private handles, façade inputs and outputs are detached standard-
library dictionaries/lists/scalars. The backend applies strict `from_dict()` before
mutation and returns a fresh `as_dict()`; the UI applies its own strict projector.
Decode/schema/type errors fail before mutation as `SimulationContractDecodeError`;
digest or cross-identity failures fail as `SimulationContractIntegrityError`.
`KirbyBackend` maps both to its compatibility/integrity error surface rather than
passing simulator objects or arbitrary exceptions to widgets.

Version 1 is strictly synchronous and single-call-at-a-time per adapter. A call
returns before another start, command, advance, reset, finalize, artifact resolve,
or Replay-provider operation begins. The live-simulation DTOs remain free of local
request correlation. Replay navigation uses the adapter-owned envelope shown above:
`source_generation` and `request_id` are echoed for the UI delivery gate but do not
enter canonical evidence, timeline, or presentation identity. An asynchronous
backend remains a future contract version and must preserve this separation.

## 8. UI connection procedure

### Step 1: verify the handoff is ready

Before changing production UI wiring, the UI worker verifies:

1. this document says `READY_FOR_UI_INTEGRATION`;
2. `Backend implementation commit` is an exact commit, not `PENDING`;
3. every backend row in the readiness table says `IMPLEMENTED`;
4. the named public symbols exist at the recorded paths;
5. serialized examples in this document were produced by those implementations.

If any check fails, report the missing backend surface instead of creating a UI-side
substitute.

### Step 2: extend the lazy backend adapter

Extend `src/kirby2_ui/backend.py::KirbyBackend` and its private lazy API inventory to
load the new public backend functions. Preserve installation-origin checking and
standard-library-only values at the Qt boundary.

Keep `available_scenarios()` as a temporary compatibility alias. New setup behavior
uses `list_simulation_profiles()` as the semantic authority.

### Step 3: add a strict live projector and atomic store

Create a live-simulation projector that:

- checks the exact schema ID, version, and field set;
- recomputes catalog/profile/selection/configuration identities when those source
  records are present, and recomputes book, cursor, market-state, and frame identity
  on every frame;
- checks profile, configuration, run, cursor, market-state, and sequence agreement;
- validates book order and numeric types;
- rejects private or release-only fields;
- produces immutable UI-owned values.

Create a sibling atomic frame store patterned after `ReplayFrameStore`, without
reusing the Replay DTO. Construct the complete next projection before replacing the
visible frame.

### Step 4: drive Setup from the catalog

Replace hard-coded scenario/volume/liquidity authority with one catalog-driven
profile choice:

1. request both the profile and training-resource catalogs and verify their
   digests;
2. populate the profile selector using presentation labels while retaining the full
   `profile_ref` as item data;
3. create advanced controls only from that profile's `controls` rows;
4. build one `SimulationProfileSelectionV1`;
5. resolve it through the backend;
6. display typed refusal text and keep Start disabled when refused;
7. select layout, observation policy, optional strategy/curriculum, quantity
   defaults, and action bindings only from the training-resource catalog;
8. show `SYNTHETIC SIMULATION` provenance before launch.

The UI does not submit its own regime or arrival-model value alongside the selected
profile.

### Step 5: start and bind one run

On an available resolution:

1. build the exact `SimulationTrainingOptionsV1`;
2. call `start_simulation_run()` through `KirbyBackend`;
3. if refused, retain no handle and show its typed refusal;
4. if available, validate the Start identities and project the complete initial
   frame off-stack;
5. retain the opaque session handle in the controller/adapter;
6. increment UI `source_generation`, bind `source_run_id` through the existing
   workspace run-ID seam, and commit the initial frame as one source replacement;
7. only then enter the live workspace.

A failed Start-result or initial-frame projection calls `close_simulation_run()` on
the returned private handle with `UNPUBLISHED_START_REJECTED_BY_UI`, then leaves
Setup visible and reports a compatibility or integrity error. It never leaks the
run or opens a partially initialized workstation.

### Step 6: advance without split-brain state

For each live update:

1. capture the current source run, frame ID, cursor ID, and UI source generation;
2. call `advance_simulation_run()` synchronously with those exact origins and a
   target simulation time strictly after the cursor;
3. confirm the source generation still matches, then validate the result's echoed
   origins and an available destination frame completely;
4. atomically commit it;
5. render all panes from that one projection.

Do not merge the old book with new trades, or old metrics with a new clock. Deltas,
if added in a later version, are optimization hints only; complete frames remain
the authority. Commit the first returned `COMPLETE` frame; do not turn it into a
`RUN_COMPLETE` error locally.

### Step 7: dispatch commands and replace a reset source

For a semantic button or hotkey, construct one `SimulationCommandRequestV1` from
the current source/frame/cursor, dispatch it synchronously, validate the complete
destination frame even when `outcome.accepted == false`, then atomically commit the
frame and show its outcome. UI source/originating-view details stay in the existing
sidecar.

On Reset, call `prepare_simulation_reset()` from the current origin. If unavailable,
retain the old run. If available, validate the new initial frame off-stack while the
old handle is still recoverable. On projection failure, discard the pending reset
and unfreeze the old handle. On success, call `commit_simulation_reset()`, then
increment `source_generation`, replace the workspace session/run binding, install
the new active handle, and commit the already-validated initial frame as one UI
transaction. If the final UI publication itself fails, resnapshot the new handle or
enter `INTEGRITY_LOCKED`; never revive the now-abandoned old handle.

The current `WorkspaceController.bind_run_id()` intentionally refuses rebinding;
the UI must add one explicit controller-level source-replacement operation that
composes clear/new session/bind/store publication. Do not weaken `bind_run_id()`
itself or expose a moment with the new run ID and old frame.

### Step 8: render backend-owned meaning

- Use backend clock metadata instead of assuming a 09:30 origin.
- Render `display_price`; retain `price_ticks` for identity and ordering checks.
- Preserve the backend's oldest-to-newest recent-trade order in projection; a
  newest-first widget may reverse only its visual iteration.
- Render metric labels, display values, units, availability, and as-of sequence as
  supplied. Do not recompute metric formulas in Qt.
- Display typed unavailable states rather than converting them to zero.
- Keep synthetic provenance visible in Setup and the live workstation.
- Render level queue as the explicitly named earliest-player summary and per-order
  queue on the order row. Do not display exact queue-ahead truth when its capability
  says unavailable.

### Step 9: finalize and enter Replay by reference

At completion or explicit save:

1. call `finalize_simulation_run()` with the exact current origins and
   `COMPLETE_ONLY`, or explicitly use `ALLOW_PARTIAL` for a user-requested partial
   save;
2. validate only the returned shape and identities exposed in the DTO; the UI does
   not claim to verify hidden tape or artifact bytes;
3. pass only `ReplayArtifactRefV1` to `resolve_replay_artifact()`;
4. require an available `ReplayArtifactVerificationReceiptV1` whose exposed IDs and
   digests equal the run result;
5. pass the private verified source handle to `build_replay_provider()`;
6. open Replay Studio only after the backend verification receipt and provider are
   accepted.

The live controller does not hand Replay Studio its active session object.

## 9. Response and failure behavior

The UI keeps three classes distinct:

| Class | Examples | UI behavior |
| --- | --- | --- |
| Stale operational response | Superseded UI request ID or source generation | Ignore the UI wrapper; keep current frame |
| Valid domain unavailability/refusal | Stale origin, run complete, unknown profile, invalid control | Show the typed result; do not replace current frame with partial data |
| Malformed, unsupported, or integrity failure | Wrong schema, bad digest, split identities, invalid book order | Reject response; retain prior frame; surface compatibility/integrity error |

Transport exceptions and domain refusals are not converted into synthetic successful
frames. `UNAVAILABLE` is not rendered as an empty book unless the backend explicitly
provides an available frame whose authoritative book is empty.

Because command and advance calls mutate before projection, a malformed destination
triggers exactly one `read_current_simulation_frame()` recovery. A valid resnapshot
is committed atomically. A second projection failure enters `INTEGRITY_LOCKED` as
defined in section 6.9; the UI never pretends its retained frame is current or sends
another stale-origin mutation.

## 10. Migration from the current UI

The initial integration is additive.

- Preserve the current `SessionConfig`/`available_scenarios()` path as a compatibility
  adapter until the new profile path is working.
- Make the resolved profile the only new semantic authority; scenario, model,
  regime, volume, and liquidity must not remain independently mutable duplicates.
- Replace the UI's fixed 09:30 formatting with frame-provided clock metadata.
- Preserve `market_state_id`, `market_state_time_us`, exchange-event sequence, and
  trade IDs through projection.
- Replace per-frame copying of the full tape with the bounded `recent_trades` field;
  keep the wire/projector order oldest-to-newest and reverse only at a newest-first
  widget boundary.
- Preserve the existing hotkey/semantic command path and user-action sidecars.
- Preserve Replay Studio and Replay Library contracts unchanged.

Known current-code seams are bounded, not architectural blockers:

- `workstation.project_snapshot()` consumes the legacy `SessionSnapshot`; add a
  sibling strict projector instead of widening that compatibility projection.
- The legacy ladder has one queue-ahead cell per price. Bind it to
  `first_player_queue_ahead`; bind exact per-order values only in the working-order
  surface.
- `WorkspaceController.bind_run_id()` correctly refuses a second run ID. Add one
  explicit atomic source-replacement method for reset instead of relaxing this
  guard.
- `KirbyBackend.reset()` currently resets a `LiveMarketSession` in place. The new
  profile path must use the prepare/validate/commit reset facade and swap to its new
  handle/run ID.
- The current core `LiveMarketSession.reset()` reconstructs only the legacy
  scenario/seed/volume/liquidity path and can lose resolved Hawkes,
  queue-reactive, distribution, and intraday configuration. It cannot implement
  the new reset contract until that construction seam is corrected.
- The current `SessionRecording` does not canonically retain every resolved
  component reference. It is not a sufficient unified-profile Replay artifact by
  itself; the backend-owned `SimulationReplayArtifactV1` envelope must supply the
  missing identity and reconstruction material.

Remove the compatibility adapter only under a later explicit migration after saved
settings and recordings have a defined upgrade path.

## 11. UI completion checklist

The UI integration is structurally complete when all statements below are true:

- Setup options come from `SimulationProfileCatalogV1`.
- Layouts, observation policies, semantic action bindings, and optional training
  refs come from `SimulationTrainingResourceCatalogV1`.
- The selected `SimulationProfileRefV1` survives unchanged through resolution,
  start, every live frame, finalization, and the recording reference.
- Advanced controls are backend-authored and range-bounded.
- The UI never constructs a flow model or simulation component.
- Start consumes the exact self-contained resolution and training-options records;
  it does not depend on backend cache state.
- One complete validated frame updates the live surface atomically.
- A stale response cannot replace a newer run or frame.
- A failed post-mutation projection either recovers the current backend frame or
  visibly locks the run; it cannot continue from a stale origin.
- A rejected initial frame and an integrity-locked exit close their private backend
  handles idempotently without creating artifacts.
- Clock, book, trades, orders, account, metrics, and diagnostics share one frame ID.
- Full authoritative snapshots, not accumulated UI deltas, determine displayed Level
  2 state.
- The display does not convert unavailable data into zero.
- Level and per-order queue-ahead truth follow the backend capability decision and
  their distinct definitions.
- A rejected processed command commits its complete destination frame; an
  unavailable command commits nothing.
- The first completion-producing advance commits its `COMPLETE` frame.
- Reset replaces handle, source run, workspace binding, and frame atomically and
  increments the UI source generation only after its prepared frame validates.
- Synthetic-simulation provenance is visible.
- Completed or explicitly saved partial runs enter Replay only through a verified
  immutable artifact reference.
- No UI production module imports release/performance/qualification evidence.
- Existing Replay Studio behavior remains unchanged.

These are implementation checks, not authorization to run release qualification,
WO40-I, a 10,000-row corpus, cross-platform providers, or other expensive work.

## 12. Backend handoff completion checklist

Before changing this document to `READY_FOR_UI_INTEGRATION`, the backend worker must:

- implement and export every backend record and operation in the readiness table;
- validate the root schema version of accepted scenario data;
- give profile/component semantics strict version and digest boundaries;
- make resolution and Start self-contained and prove Start has no last-resolution
  or process-global-cache dependency;
- let run construction consume the full resolved component bundle, and make reset
  reconstruct that same bundle behind a new handle/source run;
- implement atomic command and lifecycle results with exact origin checks;
- serialize complete immutable frames and run results;
- implement the fixed trade-tail order/window, clock relationships, metric
  availability/scale rules, diagnostic rows, and both queue-ahead definitions;
- package resolved components and the session recording into one immutable
  `SimulationReplayArtifactV1`, then implement its verified governed resolver;
- implement the live-to-Replay run-ID mapping and provider bridge without changing
  the existing Replay DTO grammar;
- implement current-frame recovery and two-phase reset prepare/commit/discard;
- implement idempotent unfinalized-run cleanup for unpublished and locked handles;
- prove completion-edge behavior and finalize idempotence in focused contract tests;
- record exact source paths and the backend implementation commit here;
- publish serializer-produced examples in the governed golden-fixture directory;
- leave Replay and release-performance contracts separate;
- send this absolute document path to the UI task.

No broad release or performance run is required to complete this handoff.

## 13. Illustrative lifecycle

The following remains a compact lifecycle outline. Exact serializer output is in
`kirby2/ui/fixtures/simulation_contract_v1/`; its 31 files and manifest are the
machine-consumable integration examples.

```text
catalog = backend.list_simulation_profiles()
training_catalog = backend.list_simulation_training_resources()
row = user_selected_catalog_row
selected_layout_ref = user_selected_or_default_layout(training_catalog)
selected_policy_ref = user_selected_or_default_policy(training_catalog)

selection = SimulationProfileSelectionV1(
    profile_ref=row.profile_ref,
    seed=44001,
    duration_us=300_000_000,
    control_values=validated_values_for_every_control(row.controls),
)

training_options = SimulationTrainingOptionsV1(
    quantity_options=[25, 50, 100, 200, 500, 1000, 2000],
    initial_quantity=100,
    layout_ref=selected_layout_ref,
    strategy_ref=None,
    objective=None,
    curriculum_drill_ref=None,
    initial_run_state="READY",
    observation_policy_ref=selected_policy_ref,
)

resolution = backend.resolve_simulation_profile(selection)
if resolution.status == "REFUSED":
    show_refusal_without_starting(resolution.refusal)
    stop_this_launch()
else:
    handle, start_result = backend.start_simulation_run(
        resolution,
        training_options,
    )
    if start_result.status == "REFUSED":
        show_refusal_without_starting(start_result.refusal)
        stop_this_launch()
    else:
        source_generation += 1
        frame_store.replace_source(project(start_result.initial_frame))

origin = frame_store.current
play_request = SimulationCommandRequestV1(
    source_run_id=origin.source_run_id,
    origin_frame_id=origin.frame_id,
    origin_cursor_id=origin.cursor.cursor_id,
    semantic_action_id="SIMULATION_PLAY",
    parameters={},
)
play_result = backend.dispatch_simulation_command(handle, play_request)
if play_result.status == "AVAILABLE":
    frame_store.commit(project(play_result.destination_frame))

while frame_store.current.cursor.run_state != "COMPLETE":
    origin = frame_store.current
    result = backend.advance_simulation_run(
        handle,
        origin.source_run_id,
        origin.frame_id,
        origin.cursor.cursor_id,
        target_time_us,
    )
    if result.status == "AVAILABLE":
        frame_store.commit(project(result.destination_frame))

origin = frame_store.current
finalize_result = backend.finalize_simulation_run(
    handle,
    origin.source_run_id,
    origin.frame_id,
    origin.cursor.cursor_id,
    "COMPLETE_ONLY",
)
if finalize_result.status == "AVAILABLE":
    artifact_ref = finalize_result.run_result.replay_artifact
    replay_source, receipt = backend.resolve_replay_artifact(artifact_ref)
    if receipt.status == "AVAILABLE":
        replay_provider = backend.build_replay_provider(replay_source)
        open_replay_from_verified_provider(replay_provider)
```

## 14. Handoff record

| Field | Value |
| --- | --- |
| Contract authoring status | `COMPLETE` |
| Backend implementation status | `COMPLETE_READY_FOR_UI_INTEGRATION` |
| UI integration status | `IN_PROGRESS_SETUP_INTEGRATED` |
| Backend setup-contract slice | `IMPLEMENTED` |
| Backend setup-contract commit | `19b5fae21e891c798b6bfd6c149761a82597feac` |
| Backend run-start slice | `IMPLEMENTED` |
| Backend run-start commit | `80372cbb12d4a2262e189f9ae63e20f0fadb9a11` |
| Backend interaction slice | `IMPLEMENTED` |
| Backend interaction commit | `78c82f01af640d20616347fd021f86b92db5cfd2` |
| Backend reset/close lifecycle slice | `IMPLEMENTED` |
| Backend reset/close lifecycle commit | `9fa910fa475716cd2e6e1ce4bfd0f87a4ddd730f` |
| Backend finalization/artifact slice | `IMPLEMENTED` |
| Backend finalization/artifact commit | `ccfc9669cc46c29dca226bb5481b13210394d2ca` |
| Backend Replay-artifact verification slice | `IMPLEMENTED` |
| Backend Replay-artifact verification commit | `49b8854d7739ad59cd3109d319c946e643c3c193` |
| Backend verified Replay-provider slice | `IMPLEMENTED` |
| Backend verified Replay-provider commit | `655ccf495b015f2067f11d63adcf3dd63e4e4609` |
| UI setup-contract projector commit | `66de3b4d9ce2d213e94c68a8f759859566c520cf` |
| UI verified Setup integration commits | `77e6d5f28c3e3d254257a37bd8d74a1c786f3958`, `d0a3d2d2bed4901f45dc1c0ce322c8d3c1459320` |
| UI verified Start integration commit | `63fed733a77b206d5629dec72ab3325a7d61ce97` |
| Backend implementation commit | `655ccf495b015f2067f11d63adcf3dd63e4e4609` |
| UI integration commit | `PENDING` |
| Expensive qualification authorization | `NOT_GRANTED` |
| Live brokerage/network authority | `NOT_GRANTED` |

When implementation lands, update this record in place. Do not create a competing
handoff document with a different contract.
