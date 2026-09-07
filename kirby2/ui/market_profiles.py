"""Optional finite market-workbench capability. Every recipe is reconstructible.

These are fictional mechanisms, not market calibration or forecast labels.
The original accepted profile catalog and its component identities are untouched.
"""
from __future__ import annotations

import copy
from functools import lru_cache
from types import MappingProxyType

from kirby2.simulation.flow import FlowEventFamily
from kirby2.simulation.intraday import IntradayModifiers, IntradayPhase, IntradayProfile, IntradaySegment, IntradayWindow
from kirby2.simulation.queue_reactive import QueueReactiveConfig, QueueStateVariable, StateResponseTerm, PiecewiseResponse
from .simulation_contract import (SimulationProfileCatalogV1, SimulationProfileRefV1,
                                  canonical_digest)
from .simulation_live_contract import SimulationStartRefusal
from .simulation_facade import (_catalog_state, _CatalogState, _ComponentRegistry,
                                _profile_semantics, PROFILE_CATALOG_SCHEMA_ID)

DURATION_US = 30_000_000
EVALUATION_SEEDS = (11, 23, 37, 53, 71, 89)
PRIOR_SEEDS = (101, 103)
# Fixed before population output was inspected. No cell is selected by its price path.
DIAGNOSTICS = {"maximum_events": 12_000, "maximum_spread_ticks": 500,
               "maximum_absolute_mid_change_ticks": 1_000,
               "minimum_executed_shares": 1}
_BASE = dict(regime="BUY_PRESSURE", model="simple", heating_seconds=6,
             persistence_seconds=8, cooling_seconds=6, replenishment_peak=1.4,
             liquidity="NORMAL", intensity=(.3, .8, 2., 1., .4, .2))
_RECIPE_CHANGES = {
    "quiet": ("Balanced / quiet", dict(regime="BALANCED", intensity=(.4,)*6)),
    "warming": ("Warming continuation", {}),
    "replenishment": ("Pressure meeting replenishment", dict(replenishment_peak=3.0)),
    "thin": ("Thin-book movement", dict(liquidity="THIN")),
    "cooling": ("Cooling / exhaustion", dict(intensity=(2.,2.,1.,.5,.2,.2))),
    "reactivation": ("Reactivation after a pause", dict(intensity=(.3,.8,1.,.2,.2,2.))),
    "fast-heating": ("Warming · faster heating", dict(heating_seconds=2)),
    "long-persistence": ("Warming · longer persistence", dict(persistence_seconds=12)),
    "slow-cooling": ("Warming · slower cooling", dict(cooling_seconds=10)),
    "clustered": ("Warming · accepted clustered arrivals", dict(model="hawkes")),
}


def _recipe(key):
    title, changes = _RECIPE_CHANGES[key]
    return {**_BASE, **changes, "key": key, "title": title}


def _identity_payload(value):
    # Canonical component identity forbids binary floats. Every model coefficient
    # is represented as an explicitly scaled integer, without string ambiguity.
    if type(value) is float:
        return {"value_ppm": round(value * 1_000_000)}
    if isinstance(value, dict):
        return {key: _identity_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_identity_payload(item) for item in value]
    return value


def _components(recipe):
    peak = recipe["replenishment_peak"]
    response = PiecewiseResponse(((0.,1.),(.5,peak)), minimum=1., maximum=3.)
    rules = {family: () for family in FlowEventFamily}
    rules[FlowEventFamily.LIMIT_BUY] = (StateResponseTerm(QueueStateVariable.DEPLETION_RATIO_BID, response),)
    rules[FlowEventFamily.LIMIT_SELL] = (StateResponseTerm(QueueStateVariable.DEPLETION_RATIO_ASK, response),)
    queue = QueueReactiveConfig("market.replenishment." + str(peak), MappingProxyType(rules),
                                minimum_multiplier=1., maximum_multiplier=3., maximum_intensity=100.)
    heat, hold, cool = (recipe[field] for field in ("heating_seconds", "persistence_seconds", "cooling_seconds"))
    lengths = (heat//2, heat//2, hold, cool//2, cool//2, 30-heat-hold-cool)
    segments, start = [], 0
    for phase, length, intensity in zip(IntradayPhase, lengths, recipe["intensity"]):
        modifiers = IntradayModifiers(1., intensity, 1., 1., 1., 1., 1.)
        segments.append(IntradaySegment(phase, start, start+length, modifiers))
        start += length
    profile = IntradayProfile("market.schedule." + recipe["key"], tuple(segments))
    return queue, profile, IntradayWindow(0,30)


class _MarketComponents(_ComponentRegistry):
    def verify(self, reference):
        if reference.component_id.startswith("market."):
            return super().verify(reference)
        return _catalog_state().components.verify(reference)


@lru_cache(maxsize=1)
def _market_state():
    base = _catalog_state()
    registry, profiles = _MarketComponents(), []
    originals = base.profiles.as_dict()["profiles"]
    for key in _RECIPE_CHANGES:
        recipe = _recipe(key)
        row = copy.deepcopy(next(row for row in originals
                                 if row["regime"] == recipe["regime"] and row["arrival_model_family"] == recipe["model"]))
        queue, schedule, window = _components(recipe)
        queue_ref = registry.register("QUEUE_REACTIVE", "market.queue."+key, _identity_payload(queue.as_dict()))
        time_ref = registry.register("INTRADAY", "market.intraday."+key,
                                     {"profile":_identity_payload(schedule.as_dict()),"window":window.as_dict(),
                                      "coordinate":"SYNTHETIC_ELAPSED_UTC", "start_offset_us":0})
        defaults = row["defaults"]
        defaults.update(seed=11, duration_us=DURATION_US, intraday_phase="PREOPEN",
                        relative_volume="1.00x", liquidity=recipe["liquidity"], intensity_scale_ppm=1_000_000,
                        queue_reactive_ref=queue_ref.as_dict(), intraday_ref=time_ref.as_dict())
        row["controls"] = []  # Finite named compositions, no unsupported arbitrary combinations.
        profile_id = "market."+key+".v1"
        row["profile_ref"] = SimulationProfileRefV1(profile_id,1,canonical_digest(
            _profile_semantics(profile_id,1,recipe["model"],recipe["regime"],defaults,[]))).as_dict()
        row["presentation"] = {"display_name":recipe["title"],
                               "summary":"Fictional flow mechanism; direction is not guaranteed. 30 synthetic seconds."}
        profiles.append(row)
    basis = {"schema_id":PROFILE_CATALOG_SCHEMA_ID,"schema_version":1,"profiles":profiles}
    return _CatalogState(SimulationProfileCatalogV1.from_dict({**basis,"catalog_sha256":canonical_digest(basis)}),
                         base.training, registry)


def list_market_workbench_profiles():
    """Public readiness, catalog, units and frozen experiment policy; detached JSON."""
    rows = []
    for key in _RECIPE_CHANGES:
        recipe = _recipe(key)
        rows.append({**recipe, "intensity":list(recipe["intensity"]), "profile_id":"market."+key+".v1"})
    return {"schema_id":"KIRBY2_MARKET_WORKBENCH_V1", "schema_version":1,
            "catalog":_market_state().profiles.as_dict(), "recipes":rows,
            "duration_us":DURATION_US, "window_us":5_000_000,
            "evaluation_seeds":list(EVALUATION_SEEDS), "prior_seeds":list(PRIOR_SEEDS),
            "diagnostics":dict(DIAGNOSTICS), "coordinate":"SYNTHETIC_ELAPSED_UTC",
            "notice":"Synthetic mechanisms only. Heating/cooling are stepwise rate schedules, not price paths. "
                     "Intensity is a model input; presentation pace does not change this recipe. "
                     "Replenishment peak multiplies limit arrival rates after observable depletion (fills or cancels)."}


def materialize_market_components(configuration):
    key = configuration.profile_ref.profile_id.removeprefix("market.").removesuffix(".v1")
    if key not in _RECIPE_CHANGES:
        raise SimulationStartRefusal("RESOLUTION_CHANGED", "No governed composition exists for these components.")
    row = next(row for row in _market_state().profiles.as_dict()["profiles"]
               if row["profile_ref"] == configuration.profile_ref.as_dict())
    for name in ("queue_reactive_ref", "intraday_ref"):
        reference = getattr(configuration, name)
        if reference is None or reference.as_dict() != row["defaults"][name]:
            raise SimulationStartRefusal("RESOLUTION_CHANGED", "Market component combination differs from its frozen recipe.")
        _market_state().components.verify(reference)
    if configuration.duration_us != DURATION_US:
        raise SimulationStartRefusal("RESOLUTION_CHANGED", "Market session window changed.")
    return _components(_recipe(key))
