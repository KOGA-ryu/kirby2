"""Versioned, single-owner synthetic execution curriculum.

Only the authoritative runtime submits/matches orders. Counterparty schedules
are recipe inputs, not price paths or guaranteed fills. Nothing here imports
an audit fixture or changes the existing governed simulation profile catalog.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from kirby2.exchange import AdvancedOrderRequest, OrderInstruction, OrderOwner, SessionState, Side
from kirby2.full_day.calendar import BoundaryOperationV1, CalendarPhaseV1, LocalBoundaryV1, PHASE_IDS, TradingDayCalendarV1
from kirby2.full_day.components_delivery import DELIVERY_RNG_LABEL, DeliveryConfigurationV1, DeliveryOwnerV1
from kirby2.full_day.components_flow import SIMPLE_FLOW_RNG_LABEL, SimpleFlowConfigurationV1, SimpleFlowOwnerV1
from kirby2.full_day.composition import DELIVERY_PROFILE_ID, executable_delivery_composition_matrix
from kirby2.full_day.models import (
    CheckpointPolicyV1, ComponentConfigurationBindingV1, DeterministicLimitsV1,
    FlowSideV1, FullDayPlanV1, HaltReopenRulesV1, MacroRegimeSegmentV1,
    MechanicsRulesV1, PressureKindV1, PressureProfileV1, PressureSegmentV1,
    ResolvedInstrumentProfileV1, SeedPolicyV1, SubstreamDeclarationV1,
    UnscheduledShockPolicyV1, VersionedReferenceV1, canonical_sha256,
    derive_substream_seed,
)
from kirby2.full_day.runtime import FullDayRuntime
from kirby2.full_day.states import (
    DAY_STATE_RNG_SUBSTREAM_PATH_V1, LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
    DayStateDefinitionV1, DayStateV1, DurationExhaustionBehaviorV1, DurationLawV1,
    DurationMassV1, LocalStateDefinitionV1, LocalStateV1, StateModelV1,
    StateTransitionV1, TriggerInformationClassV1,
)

READY_US = 1_000_000
STOP_US = 1_020_000
END_US = 10_002_000
PRICE_MIN, PRICE_MAX = 9_900, 10_100
RECIPES = {
    'execution.cancel-race.v1': ('Cancel in flight / queue touch', ((1_004_900,200),)),
    'execution.queue-pressure.v1': ('Queue pressure / adverse selection', ((1_004_900,1300),(1_009_000,900))),
}


def _reference(name, body):
    return VersionedReferenceV1(name, 1, canonical_sha256(body))


def _calendar():
    offsets = (0, 1_000, 2_000, 10_000_000, 10_001_000, END_US)
    start = datetime(2024, 1, 2)
    boundaries = tuple(LocalBoundaryV1(1, "2024-01-02", (start+timedelta(microseconds=t)).strftime("%H:%M:%S.%f"),
                                        "UTC", 0, 0, t) for t in offsets)
    destinations = (SessionState.PREOPEN, SessionState.OPENING_AUCTION, SessionState.CONTINUOUS,
                    SessionState.CLOSING_AUCTION, SessionState.POSTCLOSE, SessionState.CLOSED)
    return TradingDayCalendarV1(1, "EXECUTION_SYNTHETIC_CALENDAR_V1", "2024-01-02", "UTC",
        tuple(CalendarPhaseV1(1, phase, boundaries[i], boundaries[i+1]) for i, phase in enumerate(PHASE_IDS)),
        tuple(BoundaryOperationV1(1, b, s, u) for b,s,u in zip(boundaries,destinations,(False,False,True,False,True,False))))


def _states():
    # All transition clocks are beyond this short curriculum's horizon. Their
    # declared graph stays valid without synthesizing a regime transition.
    law = DurationLawV1(END_US, END_US, (DurationMassV1(END_US, 1),))
    def definitions(enum, cls, prefix):
        states = tuple(enum)
        return tuple(cls(state, law, (), (StateTransitionV1(
            f"{prefix}_{state.value}_NEXT", state.value, states[(i+1)%len(states)].value,
            END_US, DurationExhaustionBehaviorV1.WAIT_FOR_TRIGGER, 1, "AGE_ELIGIBLE_V1", 1, (),
            TriggerInformationClassV1.OBSERVABLE_AT_TIME, ()),)) for i,state in enumerate(states))
    return StateModelV1(1, DayStateV1.QUIET, LocalStateV1.BALANCED,
        DAY_STATE_RNG_SUBSTREAM_PATH_V1, LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
        definitions(DayStateV1,DayStateDefinitionV1,"EXECUTION_DAY"),
        definitions(LocalStateV1,LocalStateDefinitionV1,"EXECUTION_LOCAL"))


def configuration(seed=11):
    if type(seed) is not int or not 0 <= seed <= 2**31-1:
        raise ValueError("execution seed must be an integer in [0,2147483647]")
    flow = SimpleFlowConfigurationV1(1,"EXECUTION_BACKGROUND_ADDITIONS_V1",1,
        5_000_000,5_000_000,0,0,0,0,1,5,20,40,"EXECUTION_BACKGROUND")
    delivery = DeliveryConfigurationV1.from_builtin(configuration_id="EXECUTION_NORMAL_DELIVERY_V1",
        configuration_version=1,latency_profile_name="NORMAL")
    rules = MechanicsRulesV1(1,1,100,1,1,1_000_000,1,1_000_000,("DAY","LIMIT","MARKET"),(),True,10_000,None,None,None,())
    halt = _reference("EXECUTION_HALT_DISABLED_V1",{"enabled":False,"kind":"halt"})
    resume = _reference("EXECUTION_RESUME_DISABLED_V1",{"enabled":False,"kind":"resume"})
    quantity = _reference("EXECUTION_SHOCK_DISABLED_V1",{"enabled":False,"quantity":1})
    shock_label = "full_day/runtime/shock/execution/candidate"
    labels = sorted((DAY_STATE_RNG_SUBSTREAM_PATH_V1,LOCAL_STATE_RNG_SUBSTREAM_PATH_V1,
                     shock_label,SIMPLE_FLOW_RNG_LABEL,DELIVERY_RNG_LABEL))
    policy = "FULL_DAY_SUBSTREAM_V1"
    bindings = (("ENGINE_MARKET_MECHANICS_V1",halt),("ENGINE_MARKET_MECHANICS_V1",resume),
                ("FULL_DAY_RUNTIME_V1",quantity),("FLOW_SIMPLE_V1",flow.reference),("DELIVERY_ASYNC_V1",delivery.reference))
    plan = FullDayPlanV1(1,"EXECUTION_CURRICULUM_PLAN_V1",1,
        _reference("EXECUTION_MARKET_V1",{"seeded_additions":flow.as_dict()}),
        ResolvedInstrumentProfileV1(_reference("EXECUTION_INSTRUMENT_V1",rules.as_dict()),rules),_calendar(),
        tuple(PressureProfileV1("EXECUTION_"+kind.value+"_V1",1,kind,1_000_000,1_000_000,
                               (PressureSegmentV1(0,END_US,1_000_000),)) for kind in PressureKindV1),
        _states(),(MacroRegimeSegmentV1(0,END_US,DayStateV1.QUIET),),(),(),(),
        UnscheduledShockPolicyV1("EXECUTION_NO_SHOCKS_V1",1,False,0,1,0,0,0,0,1,shock_label,(FlowSideV1.BUY,FlowSideV1.SELL),quantity,()),
        HaltReopenRulesV1("EXECUTION_NO_HALTS_V1",1,halt,resume,1,1,1,0,True,False,True),
        SeedPolicyV1(1,policy,seed,tuple(SubstreamDeclarationV1(label,derive_substream_seed(seed,policy,label)) for label in labels)),
        CheckpointPolicyV1(1,None,(0,),True,True,True,True,1024),
        DeterministicLimitsV1(1,END_US,50_000,5_000,128,10_000,64*1024*1024,10_000,1_000),
        _reference("EXECUTION_BOUNDED_PILOT_V1",{"duration_us":END_US,"outer_events":50_000}),
        VersionedReferenceV1(DELIVERY_PROFILE_ID,1,executable_delivery_composition_matrix().sha256),
        tuple(sorted((ComponentConfigurationBindingV1(owner,ref) for owner,ref in bindings),key=lambda b:b.sort_key)))
    return plan,flow,delivery


def order(order_id, side, quantity, price=None, *, player=False):
    return AdvancedOrderRequest(order_id=order_id,side=Side(side),quantity=quantity,
        instruction=OrderInstruction.MARKET if price is None else OrderInstruction.LIMIT,
        owner=OrderOwner.PLAYER if player else OrderOwner.SIMULATED,
        account_id="EXECUTION_PLAYER" if player else "EXECUTION_CURRICULUM",
        price_ticks=price,time_in_force=OrderInstruction.DAY)


def create_runtime(seed=11,recipe_id='execution.cancel-race.v1'):
    if recipe_id not in RECIPES: raise ValueError('unsupported execution recipe')
    plan,flow,delivery = configuration(seed)
    runtime = FullDayRuntime.create(plan,simple_flow=SimpleFlowOwnerV1(plan,flow),delivery=DeliveryOwnerV1(plan,delivery))
    runtime.submit_request(order("EX-CURRICULUM-BID","buy",500,9_998),at_time_us=2_000)
    runtime.submit_request(order("EX-CURRICULUM-ASK","sell",300,10_000),at_time_us=2_000)
    for index,(time_us,quantity) in enumerate(RECIPES[recipe_id][1]):
        runtime.submit_request(order("EX-CURRICULUM-SELL-"+str(index),"sell",quantity),at_time_us=time_us)
    runtime.advance_to(READY_US)
    return runtime
