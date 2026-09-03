"""Backend-owned catalogs and resolution facade for synthetic simulation setup."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from kirby2.packs.formats import canonical_json_bytes, load_canonical_json_bytes
from kirby2.scenarios import ScenarioDefinition, load_scenario_definitions
from kirby2.session.bindings import Binding, SessionCommand
from kirby2.session.layouts import HotkeyLayout
from kirby2.session.live import DEFAULT_QUANTITIES
from kirby2.simulation import (
    Regime,
    accepted_hawkes_profile_for_regime,
    load_accepted_hawkes_configs,
)
from kirby2.simulation.flow_models import ACCEPTED_HAWKES_PATH
from kirby2.simulation.regimes import RegimeProfile, regime_profiles

from .simulation_contract import (
    ENGINE_CONTRACT_ID,
    LIQUIDITIES,
    PROFILE_CATALOG_SCHEMA_ID,
    PROFILE_RESOLUTION_SCHEMA_ID,
    PROFILE_SELECTION_SCHEMA_ID,
    RELATIVE_VOLUMES,
    RESOLVED_CONFIGURATION_SCHEMA_ID,
    TRAINING_RESOURCE_CATALOG_SCHEMA_ID,
    ResolvedSimulationConfigurationV1,
    SimulationComponentRefV1,
    SimulationContractDecodeError,
    SimulationContractIntegrityError,
    SimulationProfileCatalogV1,
    SimulationProfileRefV1,
    SimulationProfileResolutionV1,
    SimulationProfileSelectionV1,
    SimulationResolutionRefusal,
    SimulationTrainingResourceCatalogV1,
    canonical_digest,
)


COMPONENT_PAYLOAD_SCHEMA_ID = "KIRBY2_SIMULATION_COMPONENT_PAYLOAD_V1"
GOLDEN_MANIFEST_SCHEMA_ID = "KIRBY2_SIMULATION_CONTRACT_GOLDEN_MANIFEST_V1"
GOLDEN_FIXTURE_DIRECTORY = Path(__file__).with_name("fixtures") / "simulation_contract_v1"

_COMPONENT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "component_kind",
        "component_id",
        "component_version",
        "payload",
    }
)


@dataclass(frozen=True, slots=True)
class _Component:
    reference: SimulationComponentRefV1
    canonical_bytes: bytes


class _ComponentRegistry:
    def __init__(self) -> None:
        self._components: dict[tuple[str, str, int], _Component] = {}

    def register(
        self,
        component_kind: str,
        component_id: str,
        payload: Mapping[str, object],
        *,
        component_version: int = 1,
    ) -> SimulationComponentRefV1:
        record = {
            "schema_id": COMPONENT_PAYLOAD_SCHEMA_ID,
            "schema_version": 1,
            "component_kind": component_kind,
            "component_id": component_id,
            "component_version": component_version,
            "payload": dict(payload),
        }
        raw = canonical_json_bytes(record)
        reference = SimulationComponentRefV1.from_dict(
            {
                "component_kind": component_kind,
                "component_id": component_id,
                "component_version": component_version,
                "content_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        key = (component_kind, component_id, component_version)
        component = _Component(reference, raw)
        previous = self._components.get(key)
        if previous is not None and previous != component:
            raise SimulationContractIntegrityError(
                f"component identity was rebound: {component_kind}/{component_id}/{component_version}"
            )
        self._components[key] = component
        return reference

    def verify(self, reference: SimulationComponentRefV1) -> dict[str, object]:
        key = (
            reference.component_kind,
            reference.component_id,
            reference.component_version,
        )
        component = self._components.get(key)
        if component is None:
            raise SimulationResolutionRefusal(
                "COMPONENT_NOT_FOUND",
                f"Simulation component is unavailable: {reference.component_id}.",
            )
        if component.reference.content_sha256 != reference.content_sha256:
            raise SimulationResolutionRefusal(
                "COMPONENT_DIGEST_MISMATCH",
                f"Simulation component digest differs: {reference.component_id}.",
            )
        if hashlib.sha256(component.canonical_bytes).hexdigest() != reference.content_sha256:
            raise SimulationContractIntegrityError(
                f"registered component bytes changed: {reference.component_id}"
            )
        decoded = load_canonical_json_bytes(
            component.canonical_bytes, f"simulation component {reference.component_id}"
        )
        if type(decoded) is not dict or set(decoded) != _COMPONENT_PAYLOAD_FIELDS:
            raise SimulationContractIntegrityError(
                f"component payload schema differs: {reference.component_id}"
            )
        if (
            decoded["schema_id"] != COMPONENT_PAYLOAD_SCHEMA_ID
            or decoded["schema_version"] != 1
            or decoded["component_kind"] != reference.component_kind
            or decoded["component_id"] != reference.component_id
            or decoded["component_version"] != reference.component_version
            or type(decoded["payload"]) is not dict
        ):
            raise SimulationContractIntegrityError(
                f"component payload identity differs: {reference.component_id}"
            )
        return dict(decoded["payload"])


@dataclass(frozen=True, slots=True)
class _CatalogState:
    profiles: SimulationProfileCatalogV1
    training: SimulationTrainingResourceCatalogV1
    components: _ComponentRegistry


def _parts_per_million(
    value: object, label: str, *, allow_negative: bool = False
) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimulationContractIntegrityError(f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or (numeric < 0 and not allow_negative):
        qualifier = "finite" if allow_negative else "finite and nonnegative"
        raise SimulationContractIntegrityError(f"{label} must be {qualifier}")
    scaled = round(numeric * 1_000_000)
    if not math.isclose(numeric, scaled / 1_000_000, rel_tol=0.0, abs_tol=1e-12):
        raise SimulationContractIntegrityError(f"{label} is not exactly representable in ppm")
    return scaled


def _distribution_payload(distribution: object) -> dict[str, object]:
    values = getattr(distribution, "values", None)
    weights = getattr(distribution, "weights", None)
    if type(values) is not tuple or type(weights) is not tuple:
        raise SimulationContractIntegrityError("regime distribution is not weighted-discrete")
    return {"values": list(values), "weights": list(weights)}


def _regime_payload(profile: RegimeProfile) -> dict[str, object]:
    return {
        "regime": profile.regime.value,
        "rate_multipliers_ppm": [
            _parts_per_million(value, "regime rate multiplier")
            for value in profile.rate_multipliers
        ],
        "limit_buy_sizes": _distribution_payload(profile.limit_buy_sizes),
        "limit_sell_sizes": _distribution_payload(profile.limit_sell_sizes),
        "market_buy_sizes": _distribution_payload(profile.market_buy_sizes),
        "market_sell_sizes": _distribution_payload(profile.market_sell_sizes),
        "bid_depth": _distribution_payload(profile.bid_depth),
        "ask_depth": _distribution_payload(profile.ask_depth),
        "initial_queue_sizes": _distribution_payload(profile.initial_queue_sizes),
        "imbalance_feedback_ppm": _parts_per_million(
            profile.imbalance_feedback,
            "regime imbalance feedback",
            allow_negative=True,
        ),
        "trend_feedback_ppm": _parts_per_million(
            profile.trend_feedback,
            "regime trend feedback",
            allow_negative=True,
        ),
    }


def _scenario_payload(definition: ScenarioDefinition) -> dict[str, object]:
    unknown_overrides = set(definition.parameter_overrides) - {"event_intensity"}
    if unknown_overrides:
        raise SimulationContractIntegrityError(
            f"accepted scenario has unrepresented overrides: {sorted(unknown_overrides)!r}"
        )
    return {
        "accepted_replay_sha256": definition.accepted_replay_sha256,
        "duration_us": definition.duration_seconds * 1_000_000,
        "event_intensity_ppm": _parts_per_million(
            definition.parameter_overrides.get("event_intensity", 1.0),
            "scenario event intensity",
        ),
        "initial_depth": definition.initial_depth,
        "initial_mid_ticks": definition.initial_mid_ticks,
        "liquidity": definition.liquidity.value,
        "regime": definition.regime.value,
        "relative_volume": definition.relative_volume.value,
        "scenario_name": definition.name,
        "seed": definition.seed,
    }


def _profile_controls(definition: ScenarioDefinition) -> list[dict[str, object]]:
    intensity = _parts_per_million(
        definition.parameter_overrides.get("event_intensity", 1.0),
        "scenario event intensity",
    )
    if not 250_000 <= intensity <= 2_000_000 or (intensity - 250_000) % 250_000:
        raise SimulationContractIntegrityError(
            f"scenario {definition.name} intensity is outside the public V1 control grid"
        )
    return [
        {
            "control_id": "relative_volume",
            "label": "Relative volume",
            "value_kind": "ENUM",
            "scale": 1,
            "default_value": definition.relative_volume.value,
            "minimum_value": None,
            "maximum_value": None,
            "step": None,
            "unit": "",
            "options": list(RELATIVE_VOLUMES),
        },
        {
            "control_id": "liquidity",
            "label": "Displayed liquidity",
            "value_kind": "ENUM",
            "scale": 1,
            "default_value": definition.liquidity.value,
            "minimum_value": None,
            "maximum_value": None,
            "step": None,
            "unit": "",
            "options": list(LIQUIDITIES),
        },
        {
            "control_id": "intensity_scale_ppm",
            "label": "Event intensity",
            "value_kind": "FIXED_POINT",
            "scale": 1_000_000,
            "default_value": intensity,
            "minimum_value": 250_000,
            "maximum_value": 2_000_000,
            "step": 250_000,
            "unit": "ratio",
            "options": [],
        },
    ]


def _profile_semantics(
    profile_id: str,
    profile_version: int,
    arrival_model_family: str,
    regime: str,
    defaults: Mapping[str, object],
    controls: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_id": "KIRBY2_SIMULATION_PROFILE_SEMANTICS_V1",
        "schema_version": 1,
        "profile_id": profile_id,
        "profile_version": profile_version,
        "engine_contract_id": ENGINE_CONTRACT_ID,
        "arrival_model_family": arrival_model_family,
        "regime": regime,
        "defaults": dict(defaults),
        "controls": [
            {
                field: control[field]
                for field in (
                    "control_id",
                    "value_kind",
                    "scale",
                    "default_value",
                    "minimum_value",
                    "maximum_value",
                    "step",
                    "unit",
                    "options",
                )
            }
            for control in controls
        ],
    }


def _profile_row(
    definition: ScenarioDefinition,
    arrival_model_family: str,
    *,
    scenario_ref: SimulationComponentRefV1,
    regime_ref: SimulationComponentRefV1,
    distribution_ref: SimulationComponentRefV1,
    hawkes_ref: SimulationComponentRefV1 | None,
) -> dict[str, object]:
    controls = _profile_controls(definition)
    defaults: dict[str, object] = {
        "seed": definition.seed,
        "duration_us": definition.duration_seconds * 1_000_000,
        "intraday_phase": "NOT_APPLICABLE",
        "relative_volume": definition.relative_volume.value,
        "liquidity": definition.liquidity.value,
        "intensity_scale_ppm": _parts_per_million(
            definition.parameter_overrides.get("event_intensity", 1.0),
            "scenario event intensity",
        ),
        "scenario_definition_ref": scenario_ref.as_dict(),
        "regime_profile_ref": regime_ref.as_dict(),
        "distribution_bundle_ref": distribution_ref.as_dict(),
        "queue_reactive_ref": None,
        "hawkes_ref": None if hawkes_ref is None else hawkes_ref.as_dict(),
        "intraday_ref": None,
    }
    profile_id = f"accepted.{definition.name}.{arrival_model_family}"
    profile_version = 1
    profile_ref = SimulationProfileRefV1(
        profile_id,
        profile_version,
        canonical_digest(
            _profile_semantics(
                profile_id,
                profile_version,
                arrival_model_family,
                definition.regime.value,
                defaults,
                controls,
            )
        ),
    )
    flow_label = "Independent arrivals" if arrival_model_family == "simple" else "Clustered arrivals"
    flow_summary = (
        "independent Poisson arrivals"
        if arrival_model_family == "simple"
        else "an accepted regime-shaped Hawkes arrival model"
    )
    return {
        "profile_ref": profile_ref.as_dict(),
        "presentation": {
            "display_name": f"{definition.name.replace('_', ' ').title()} - {flow_label}",
            "summary": (
                f"Deterministic synthetic {definition.regime.value.lower().replace('_', ' ')} "
                f"flow using {flow_summary}."
            ),
        },
        "engine_contract_id": ENGINE_CONTRACT_ID,
        "arrival_model_family": arrival_model_family,
        "regime": definition.regime.value,
        "defaults": defaults,
        "controls": controls,
        "provenance": {
            "classification": "SYNTHETIC_SIMULATION_ONLY",
            "real_market_data": False,
            "matching_engine_derived": True,
            "generation_method": "ORDER_FLOW_THROUGH_MATCHING_ENGINE",
            "level2_origin": "MATCHING_ENGINE_BOOK_STATE",
            "display_label": "Synthetic matching-engine simulation",
        },
    }


_COMMAND_ACTIONS: Mapping[SessionCommand, tuple[str, str, str]] = {
    SessionCommand.BUY_BID: ("PLAYER_BUY_BID", "PLAYER_ACTION", "Buy bid"),
    SessionCommand.BUY_ASK: ("PLAYER_BUY_ASK", "PLAYER_ACTION", "Buy ask"),
    SessionCommand.MARKET_BUY: ("PLAYER_BUY_MARKET", "PLAYER_ACTION", "Buy market"),
    SessionCommand.SELL_ASK: ("PLAYER_SELL_ASK", "PLAYER_ACTION", "Sell ask"),
    SessionCommand.SELL_BID: ("PLAYER_SELL_BID", "PLAYER_ACTION", "Sell bid"),
    SessionCommand.MARKET_SELL: ("PLAYER_SELL_MARKET", "PLAYER_ACTION", "Sell market"),
    SessionCommand.CANCEL_NEAREST: ("PLAYER_CANCEL_NEAREST", "PLAYER_ACTION", "Cancel nearest"),
    SessionCommand.CANCEL_ALL: ("PLAYER_CANCEL_ALL", "PLAYER_ACTION", "Cancel all"),
    SessionCommand.REPLACE_NEAREST: ("PLAYER_REPLACE_NEAREST", "PLAYER_ACTION", "Replace nearest"),
    SessionCommand.FLATTEN: ("PLAYER_FLATTEN", "PLAYER_ACTION", "Flatten"),
    SessionCommand.INCREASE_QUANTITY: ("PLAYER_INCREASE_QUANTITY", "PLAYER_ACTION", "Quantity up"),
    SessionCommand.DECREASE_QUANTITY: ("PLAYER_DECREASE_QUANTITY", "PLAYER_ACTION", "Quantity down"),
    SessionCommand.RESET: ("SIMULATION_RESET", "LIFECYCLE", "Reset"),
    SessionCommand.QUIT: ("SIMULATION_CLOSE", "LIFECYCLE", "Close"),
}


def _public_key(binding: Binding) -> str:
    if binding.key == " ":
        return "SPACE"
    if len(binding.key) == 1 or binding.key.startswith("KEY_"):
        return binding.key
    raise SimulationContractIntegrityError(
        f"layout key is not representable by the public V1 adapter: {binding.key!r}"
    )


def _layout_actions(layout: HotkeyLayout) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for binding in layout.bindings.bindings:
        key = _public_key(binding)
        if binding.command is SessionCommand.TOGGLE_RUN:
            actions.extend(
                (
                    {
                        "semantic_action_id": "SIMULATION_PLAY",
                        "action_kind": "LIFECYCLE",
                        "display_label": "Play",
                        "bound_key": key,
                    },
                    {
                        "semantic_action_id": "SIMULATION_PAUSE",
                        "action_kind": "LIFECYCLE",
                        "display_label": "Pause",
                        "bound_key": key,
                    },
                )
            )
            continue
        semantic_action_id, action_kind, display_label = _COMMAND_ACTIONS[binding.command]
        actions.append(
            {
                "semantic_action_id": semantic_action_id,
                "action_kind": action_kind,
                "display_label": display_label,
                "bound_key": key,
            }
        )
    return actions


def _training_catalog(registry: _ComponentRegistry) -> SimulationTrainingResourceCatalogV1:
    layout = HotkeyLayout.default()
    actions = _layout_actions(layout)
    layout_ref = registry.register(
        "HOTKEY_LAYOUT",
        "layout.default.v1",
        {"layout_name": layout.name, "actions": actions},
    )
    policy_ref = registry.register(
        "OBSERVATION_POLICY",
        "observation.synthetic-own-queue.v1",
        {
            "player_queue_disclosure": "AVAILABLE",
            "scope": "PLAYER_OWN_RESTING_ORDERS_ONLY",
        },
    )
    basis = {
        "schema_id": TRAINING_RESOURCE_CATALOG_SCHEMA_ID,
        "schema_version": 1,
        "layouts": [
            {
                "layout_ref": layout_ref.as_dict(),
                "presentation": {
                    "display_name": "Default keyboard",
                    "summary": "The complete replayable Kirby2 keyboard layout.",
                },
                "actions": actions,
            }
        ],
        "observation_policies": [
            {
                "policy_ref": policy_ref.as_dict(),
                "presentation": {
                    "display_name": "Synthetic own-order queue",
                    "summary": "Disclose queue ahead only for the player's synthetic resting orders.",
                },
                "player_queue_disclosure": "AVAILABLE",
            }
        ],
        "strategies": [],
        "curriculum_drills": [],
        "defaults": {
            "layout_ref": layout_ref.as_dict(),
            "observation_policy_ref": policy_ref.as_dict(),
            "quantity_options": list(DEFAULT_QUANTITIES),
            "initial_quantity": 100,
        },
    }
    payload = {**basis, "catalog_sha256": canonical_digest(basis)}
    return SimulationTrainingResourceCatalogV1.from_dict(payload)


@lru_cache(maxsize=1)
def _catalog_state() -> _CatalogState:
    registry = _ComponentRegistry()
    definitions = load_scenario_definitions()
    definitions_by_regime = {definition.regime: definition for definition in definitions.values()}
    if set(definitions_by_regime) != set(Regime):
        raise SimulationContractIntegrityError(
            "accepted scenarios do not cover every simulation regime"
        )
    regime_rows = regime_profiles()
    hawkes_configs = load_accepted_hawkes_configs()
    hawkes_source_sha256 = hashlib.sha256(ACCEPTED_HAWKES_PATH.read_bytes()).hexdigest()
    distribution_ref = registry.register(
        "DISTRIBUTION_BUNDLE",
        "distribution.regime-native.v1",
        {
            "implementation_id": "KIRBY2_REGIME_NATIVE_DISTRIBUTIONS_V1",
            "ownership": "REGIME_PROFILE_FIELDS",
        },
    )
    profiles: list[dict[str, object]] = []
    for regime in Regime:
        definition = definitions_by_regime[regime]
        scenario_ref = registry.register(
            "SCENARIO_DEFINITION",
            f"scenario.accepted.{definition.name}.v1",
            _scenario_payload(definition),
        )
        regime_ref = registry.register(
            "REGIME_PROFILE",
            f"regime.{regime.value.lower()}.v1",
            _regime_payload(regime_rows[regime]),
        )
        profiles.append(
            _profile_row(
                definition,
                "simple",
                scenario_ref=scenario_ref,
                regime_ref=regime_ref,
                distribution_ref=distribution_ref,
                hawkes_ref=None,
            )
        )
        accepted_hawkes_id = accepted_hawkes_profile_for_regime(regime)
        if accepted_hawkes_id not in hawkes_configs:
            raise SimulationContractIntegrityError(
                f"accepted Hawkes mapping is missing {accepted_hawkes_id}"
            )
        hawkes_ref = registry.register(
            "HAWKES",
            f"hawkes.accepted.{accepted_hawkes_id}.{regime.value.lower()}.v1",
            {
                "accepted_profile_id": accepted_hawkes_id,
                "accepted_source_sha256": hawkes_source_sha256,
                "composition_id": "REGIME_SHAPED_HAWKES_V1",
                "regime": regime.value,
            },
        )
        profiles.append(
            _profile_row(
                definition,
                "hawkes",
                scenario_ref=scenario_ref,
                regime_ref=regime_ref,
                distribution_ref=distribution_ref,
                hawkes_ref=hawkes_ref,
            )
        )
    profile_basis = {
        "schema_id": PROFILE_CATALOG_SCHEMA_ID,
        "schema_version": 1,
        "profiles": profiles,
    }
    profile_catalog = SimulationProfileCatalogV1.from_dict(
        {**profile_basis, "catalog_sha256": canonical_digest(profile_basis)}
    )
    training_catalog = _training_catalog(registry)
    for profile in profile_catalog.profiles:
        defaults = profile["defaults"]
        for field in (
            "scenario_definition_ref",
            "regime_profile_ref",
            "distribution_bundle_ref",
            "queue_reactive_ref",
            "hawkes_ref",
            "intraday_ref",
        ):
            raw_ref = defaults[field]
            if raw_ref is not None:
                registry.verify(SimulationComponentRefV1.from_dict(raw_ref))
    return _CatalogState(profile_catalog, training_catalog, registry)


def list_simulation_profiles() -> dict[str, object]:
    """Return a detached, digest-pinned catalog of runnable synthetic profiles."""

    return _catalog_state().profiles.as_dict()


def list_simulation_training_resources() -> dict[str, object]:
    """Return backend-authored layouts and observation choices for setup."""

    return _catalog_state().training.as_dict()


def _refused(
    selection: SimulationProfileSelectionV1,
    reason_code: str,
    explanation: str,
    catalog: SimulationProfileCatalogV1,
) -> dict[str, object]:
    payload = {
        "schema_id": PROFILE_RESOLUTION_SCHEMA_ID,
        "schema_version": 1,
        "status": "REFUSED",
        "selection": selection.as_dict(),
        "selection_sha256": selection.selection_sha256,
        "resolved_configuration_sha256": None,
        "resolved_configuration": None,
        "refusal": {"reason_code": reason_code, "explanation": explanation},
    }
    return SimulationProfileResolutionV1.from_dict(payload, catalog=catalog).as_dict()


def resolve_simulation_profile(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Resolve one selection into a complete safe recipe or typed refusal."""

    state = _catalog_state()
    try:
        selection = SimulationProfileSelectionV1.from_dict(payload)
    except (TypeError, ValueError) as error:
        raise SimulationContractDecodeError(str(error)) from error
    try:
        profile = state.profiles.validate_selection(selection)
        if selection.duration_us % 1_000_000:
            raise SimulationResolutionRefusal(
                "INVALID_DURATION",
                "Version 1 simulation duration must be a whole number of seconds.",
            )
        defaults = profile["defaults"]
        for field in (
            "scenario_definition_ref",
            "regime_profile_ref",
            "distribution_bundle_ref",
            "queue_reactive_ref",
            "hawkes_ref",
            "intraday_ref",
        ):
            raw_ref = defaults[field]
            if raw_ref is not None:
                state.components.verify(SimulationComponentRefV1.from_dict(raw_ref))
    except SimulationResolutionRefusal as refusal:
        return _refused(
            selection,
            refusal.reason_code,
            refusal.explanation,
            state.profiles,
        )
    values = dict(selection.control_values)
    configuration = ResolvedSimulationConfigurationV1(
        profile_ref=selection.profile_ref,
        selection_sha256=selection.selection_sha256,
        engine_contract_id=ENGINE_CONTRACT_ID,
        seed=selection.seed,
        duration_us=selection.duration_us,
        arrival_model_family=str(profile["arrival_model_family"]),
        regime=str(profile["regime"]),
        intraday_phase=str(defaults["intraday_phase"]),
        relative_volume=str(values.get("relative_volume", defaults["relative_volume"])),
        liquidity=str(values.get("liquidity", defaults["liquidity"])),
        intensity_scale_ppm=int(
            values.get("intensity_scale_ppm", defaults["intensity_scale_ppm"])
        ),
        scenario_definition_ref=SimulationComponentRefV1.from_dict(
            defaults["scenario_definition_ref"]
        ),
        regime_profile_ref=SimulationComponentRefV1.from_dict(
            defaults["regime_profile_ref"]
        ),
        distribution_bundle_ref=SimulationComponentRefV1.from_dict(
            defaults["distribution_bundle_ref"]
        ),
        queue_reactive_ref=(
            None
            if defaults["queue_reactive_ref"] is None
            else SimulationComponentRefV1.from_dict(defaults["queue_reactive_ref"])
        ),
        hawkes_ref=(
            None
            if defaults["hawkes_ref"] is None
            else SimulationComponentRefV1.from_dict(defaults["hawkes_ref"])
        ),
        intraday_ref=(
            None
            if defaults["intraday_ref"] is None
            else SimulationComponentRefV1.from_dict(defaults["intraday_ref"])
        ),
        effective_control_values=selection.control_values,
    )
    configuration.validate_against(selection, profile)
    configuration_record = configuration.as_dict()
    resolution = {
        "schema_id": PROFILE_RESOLUTION_SCHEMA_ID,
        "schema_version": 1,
        "status": "AVAILABLE",
        "selection": selection.as_dict(),
        "selection_sha256": selection.selection_sha256,
        "resolved_configuration_sha256": canonical_digest(configuration_record),
        "resolved_configuration": configuration_record,
        "refusal": None,
    }
    return SimulationProfileResolutionV1.from_dict(
        resolution, catalog=state.profiles
    ).as_dict()


def simulation_contract_golden_records() -> dict[str, dict[str, object]]:
    """Return the backend-produced cross-repository V1 compatibility records."""

    catalog = list_simulation_profiles()
    training = list_simulation_training_resources()
    profiles = catalog["profiles"]
    if type(profiles) is not list or not profiles or type(profiles[0]) is not dict:
        raise SimulationContractIntegrityError("profile catalog cannot seed golden records")
    profile = profiles[0]
    controls = profile["controls"]
    defaults = profile["defaults"]
    if type(controls) is not list or type(defaults) is not dict:
        raise SimulationContractIntegrityError("profile catalog golden row is malformed")
    selection: dict[str, object] = {
        "schema_id": PROFILE_SELECTION_SCHEMA_ID,
        "schema_version": 1,
        "profile_ref": dict(profile["profile_ref"]),
        "seed": defaults["seed"],
        "duration_us": defaults["duration_us"],
        "control_values": {
            str(control["control_id"]): control["default_value"]
            for control in controls
        },
    }
    refused_selection = {
        **selection,
        "profile_ref": dict(selection["profile_ref"]),
        "control_values": dict(selection["control_values"]),
        "duration_us": int(selection["duration_us"]) + 500_000,
    }
    available_resolution = resolve_simulation_profile(selection)
    refused_resolution = resolve_simulation_profile(refused_selection)
    training_defaults = training["defaults"]
    if type(training_defaults) is not dict:
        raise SimulationContractIntegrityError("training catalog golden defaults are malformed")
    training_options: dict[str, object] = {
        "schema_id": "KIRBY2_SIMULATION_TRAINING_OPTIONS_V1",
        "schema_version": 1,
        "quantity_options": list(training_defaults["quantity_options"]),
        "initial_quantity": training_defaults["initial_quantity"],
        "layout_ref": dict(training_defaults["layout_ref"]),
        "strategy_ref": None,
        "objective": None,
        "curriculum_drill_ref": None,
        "initial_run_state": "READY",
        "observation_policy_ref": dict(training_defaults["observation_policy_ref"]),
    }
    from .simulation_run_facade import _start_simulation_run_with_source_id

    run_handle, available_start = _start_simulation_run_with_source_id(
        available_resolution,
        training_options,
        "simulation-run-00000000000000000000000000000001",
    )
    _, refused_start = _start_simulation_run_with_source_id(
        refused_resolution,
        training_options,
        "simulation-run-00000000000000000000000000000002",
    )
    if run_handle is None:
        raise SimulationContractIntegrityError("available golden Start omitted its run handle")
    initial_frame = available_start["initial_frame"]
    if type(initial_frame) is not dict or type(initial_frame.get("cursor")) is not dict:
        raise SimulationContractIntegrityError("available golden Start frame is malformed")

    def command_request(
        frame: dict[str, object],
        semantic_action_id: str,
    ) -> dict[str, object]:
        cursor = frame.get("cursor")
        if type(cursor) is not dict:
            raise SimulationContractIntegrityError("golden command origin cursor is malformed")
        basis = {
            "schema_id": "KIRBY2_SIMULATION_COMMAND_REQUEST_V1",
            "schema_version": 1,
            "source_run_id": frame["source_run_id"],
            "origin_frame_id": frame["frame_id"],
            "origin_cursor_id": cursor["cursor_id"],
            "semantic_action_id": semantic_action_id,
            "parameters": {},
        }
        return {
            **basis,
            "command_id": f"simulation-command-{canonical_digest(basis)[:24]}",
        }

    from .simulation_run_facade import (
        _prepare_simulation_reset_with_ids,
        advance_simulation_run,
        close_simulation_run,
        commit_simulation_reset,
        dispatch_simulation_command,
        read_current_simulation_frame,
    )

    play_request = command_request(initial_frame, "SIMULATION_PLAY")
    play_result = dispatch_simulation_command(run_handle, play_request)
    running_frame = play_result["destination_frame"]
    if type(running_frame) is not dict or type(running_frame.get("cursor")) is not dict:
        raise SimulationContractIntegrityError("golden Play frame is malformed")
    running_cursor = running_frame["cursor"]
    advance_result = advance_simulation_run(
        run_handle,
        str(running_frame["source_run_id"]),
        str(running_frame["frame_id"]),
        str(running_cursor["cursor_id"]),
        1_000_000,
    )
    advanced_frame = advance_result["destination_frame"]
    if type(advanced_frame) is not dict:
        raise SimulationContractIntegrityError("golden advance frame is malformed")
    buy_bid_request = command_request(advanced_frame, "PLAYER_BUY_BID")
    buy_bid_result = dispatch_simulation_command(run_handle, buy_bid_request)
    stale_result = dispatch_simulation_command(run_handle, play_request)
    current_result = read_current_simulation_frame(
        run_handle,
        str(advanced_frame["source_run_id"]),
    )
    reset_origin = current_result["current_frame"]
    if type(reset_origin) is not dict or type(reset_origin.get("cursor")) is not dict:
        raise SimulationContractIntegrityError("golden reset origin is malformed")
    pending_reset, reset_result = _prepare_simulation_reset_with_ids(
        run_handle,
        str(reset_origin["source_run_id"]),
        str(reset_origin["frame_id"]),
        str(reset_origin["cursor"]["cursor_id"]),
        new_source_run_id="simulation-run-00000000000000000000000000000003",
        reset_token_id=(
            "simulation-reset-token-00000000000000000000000000000001"
        ),
    )
    if pending_reset is None:
        raise SimulationContractIntegrityError("golden reset preparation omitted its handle")
    reset_initial_frame = reset_result["initial_frame"]
    if type(reset_initial_frame) is not dict:
        raise SimulationContractIntegrityError("golden reset initial frame is malformed")
    _, reset_commit_mismatch = commit_simulation_reset(
        run_handle,
        pending_reset,
        str(reset_result["reset_token_id"]),
        "simulation-run-ffffffffffffffffffffffffffffffff",
        str(reset_initial_frame["frame_id"]),
    )
    replacement_handle, reset_commit_result = commit_simulation_reset(
        run_handle,
        pending_reset,
        str(reset_result["reset_token_id"]),
        str(reset_result["new_source_run_id"]),
        str(reset_initial_frame["frame_id"]),
    )
    if replacement_handle is None:
        raise SimulationContractIntegrityError("golden reset commit omitted its new handle")
    abandoned_current_result = read_current_simulation_frame(
        run_handle,
        str(reset_origin["source_run_id"]),
    )
    close_result = close_simulation_run(replacement_handle, "USER_ABANDONED")
    if close_simulation_run(replacement_handle, "USER_ABANDONED") != close_result:
        raise SimulationContractIntegrityError("golden close is not idempotent")
    close_mismatch = close_simulation_run(replacement_handle, "INTEGRITY_LOCKED")
    return {
        "profile_catalog.json": catalog,
        "training_resource_catalog.json": training,
        "profile_selection.json": selection,
        "profile_resolution_available.json": available_resolution,
        "profile_resolution_refused.json": refused_resolution,
        "simulation_training_options.json": training_options,
        "simulation_start_available.json": available_start,
        "simulation_start_refused.json": refused_start,
        "simulation_command_request_play.json": play_request,
        "simulation_command_result_play.json": play_result,
        "simulation_advance_result.json": advance_result,
        "simulation_command_request_buy_bid.json": buy_bid_request,
        "simulation_command_result_buy_bid.json": buy_bid_result,
        "simulation_command_result_stale.json": stale_result,
        "simulation_current_frame_result.json": current_result,
        "simulation_reset_result_available.json": reset_result,
        "simulation_reset_commit_mismatch.json": reset_commit_mismatch,
        "simulation_reset_commit_committed.json": reset_commit_result,
        "simulation_current_frame_abandoned.json": abandoned_current_result,
        "simulation_close_result_closed.json": close_result,
        "simulation_close_result_mismatch.json": close_mismatch,
    }


def write_simulation_contract_golden_fixtures(
    destination: Path = GOLDEN_FIXTURE_DIRECTORY,
) -> tuple[Path, ...]:
    """Mechanically write canonical golden records and their digest manifest."""

    if not isinstance(destination, Path):
        raise TypeError("golden fixture destination must be pathlib.Path")
    destination.mkdir(parents=True, exist_ok=True)
    records = simulation_contract_golden_records()
    written: list[Path] = []
    manifest_rows: list[dict[str, object]] = []
    for name in sorted(records):
        raw = canonical_json_bytes(records[name])
        path = destination / name
        path.write_bytes(raw)
        written.append(path)
        manifest_rows.append(
            {"name": name, "sha256": hashlib.sha256(raw).hexdigest()}
        )
    manifest = {
        "schema_id": GOLDEN_MANIFEST_SCHEMA_ID,
        "schema_version": 1,
        "files": manifest_rows,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    written.append(manifest_path)
    return tuple(written)


__all__ = [
    "COMPONENT_PAYLOAD_SCHEMA_ID",
    "GOLDEN_FIXTURE_DIRECTORY",
    "GOLDEN_MANIFEST_SCHEMA_ID",
    "list_simulation_profiles",
    "list_simulation_training_resources",
    "resolve_simulation_profile",
    "simulation_contract_golden_records",
    "write_simulation_contract_golden_fixtures",
]
