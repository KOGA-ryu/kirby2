# Scenario and traffic-light authoring

Kirby2 separates declarative training inputs from the runtime that executes them.
Linting and explanation do not execute a scenario; compilation creates an immutable
artifact; execution accepts only a finalized compiled artifact.

## Scope boundary

- Kirby2 is a simulation and training environment.
- Kirby2 is not a broker.
- Kirby2 is not a live market connector.
- Kirby2 provides no performance guarantee.
- A reconstruction is not proof of historical market state.

## Start from a bundled example

Copy an example such as `kirby2/scenario_lang/examples/full_day.toml` to a new working
location. Keep the source, compiled artifact, run output, and evidence as separate
files. A source file can declare only the supported schema, plan, phases, seeds,
definitions, and capabilities; it cannot load executable pack code.

## Authoring lifecycle

Lint first:

```sh
kirby2-headless scenario-source lint /absolute/path/scenario.toml
```

Ask the fixed explanation questions without running:

```sh
kirby2-headless scenario-source explain /absolute/path/scenario.toml
```

Persist the compiled artifact to a new path:

```sh
kirby2-headless scenario-source compile \
  /absolute/path/scenario.toml \
  --output /absolute/new/path/scenario.compiled.json
```

Compare two materialized sources before choosing one:

```sh
kirby2-headless scenario-source diff \
  /absolute/path/left.toml \
  /absolute/path/right.toml
```

Run only after lint and explanation are satisfactory:

```sh
kirby2-headless scenario-source run \
  /absolute/path/scenario.toml \
  --artifact /absolute/new/path/scenario.compiled.json
```

An explicit `--seed` is permitted only where the schema allows a root-seed override.
The emitted compiled digest identifies the materialized plan; it does not imply that
the scenario resembles observed markets.

## Definition packs

Confined definition packs are activated by an explicit namespace and absolute path:

```sh
kirby2-headless scenario-source lint scenario.toml \
  --pack training=/absolute/path/to/definition-pack
```

Namespaces must be unique. Missing dependencies, incompatible capabilities, digest
conflicts, and cycles refuse compilation. Pack-supplied content is data, never Python
or another executable extension.

To distribute a scenario as a `.k2pack`, construct a `pack-source.toml` directory,
build it, then inspect and verify the resulting archive before installation:

```sh
kirby2-headless pack build /absolute/path/source-directory \
  --output /absolute/new/path/scenario.k2pack
kirby2-headless pack inspect /absolute/new/path/scenario.k2pack
kirby2-headless pack verify /absolute/new/path/scenario.k2pack
```

Installation is an explicit local action with an explicit data root. Existing
conflicting logical bindings are not silently replaced.

## Traffic-light rules

Traffic-light rules are a restricted observable-only state-machine language. They may
classify simulated setup, entry, exit, and explanatory reasons; they do not route or
submit orders. Validate a rule before supplying it to the terminal trainer:

```sh
kirby2 strategy /absolute/path/rule-file
kirby2-desktop trainer --strategy /absolute/path/rule-file
```

Use only declared features and bounded rolling windows. Treat unavailable values as
unavailable rather than filling them from future events or hidden lesson truth. A
traffic-light result is a training annotation, not a real-world signal.

## Lessons and historical material

Lesson packs bind their scenario dependency, detector, questions, and reveal policy.
Blind answers freeze before debrief or hidden material is revealed. Historical inputs
must retain their label:

- `EXACT_REPLAY`: source-provided events within the fixture's stated scope.
- `RECONSTRUCTION` / `SYNTHETIC_RECONSTRUCTION`: modeled events, queues, or features.
- `COUNTERFACTUAL`: an explicitly branched alternative.
- `UNAVAILABLE`: information that the source cannot support.

Do not rename reconstructed values as observed values. Do not infer historical cause
from a deterministic replay or counterfactual result.

## Author checklist

Before sharing a source or pack, confirm that:

1. lint and explanation complete without unresolved diagnostics;
2. seeds and phase boundaries are explicit;
3. all quantities, price units, clocks, and observation modes are declared;
4. required packs are content-addressed and dependency-complete;
5. no file, network, credential, broker, or hidden-state side effect is requested;
6. synthetic, reconstructed, counterfactual, and unavailable values stay labeled;
7. the compiled artifact and pack verify from a clean local path.
