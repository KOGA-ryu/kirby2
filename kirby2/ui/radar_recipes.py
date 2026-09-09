"""Three independent books in a versioned, eight-second fictional rotation."""
from dataclasses import replace

from kirby2.full_day.models import canonical_sha256
from kirby2.full_day.runtime import FullDayRuntime
from kirby2.full_day.components_flow import SimpleFlowOwnerV1
from kirby2.full_day.components_delivery import DeliveryOwnerV1
from .execution_recipes import configuration, order, _reference

SYMBOLS = ('ASTER', 'BRIO', 'CINDER')
READY_US, STOP_US, STEP_US = 1_000_000, 9_000_000, 500_000
RECIPE = 'radar.rotation.v1'


def instrument_seed(seed, symbol):
    return int(canonical_sha256(dict(recipe=RECIPE, seed=seed, symbol=symbol))[:8], 16) % 2**31


def create_instrument(seed, symbol):
    if symbol not in SYMBOLS: raise ValueError('unknown fictional instrument')
    plan, flow, delivery = configuration(instrument_seed(seed, symbol))
    flow = replace(flow, configuration_id='RADAR_SPARSE_BACKGROUND_V1',
                   limit_buy_microevents_per_second=100_000,
                   limit_sell_microevents_per_second=100_000,
                   minimum_placement_depth_ticks=20, maximum_placement_depth_ticks=20)
    plan = replace(plan, plan_id='RADAR_ROTATION_V1',
                   market_profile=_reference('RADAR_ROTATION_V1', dict(recipe=RECIPE, symbol=symbol, flow=flow.as_dict())),
                   component_configurations=tuple(sorted((replace(b, configuration=flow.reference)
                       if b.component_id=='FLOW_SIMPLE_V1' else b for b in plan.component_configurations), key=lambda b:b.sort_key)))
    runtime = FullDayRuntime.create(plan, simple_flow=SimpleFlowOwnerV1(plan, flow),
                                   delivery=DeliveryOwnerV1(plan, delivery))
    ask = 10_000 if symbol == 'ASTER' else 10_004 if symbol == 'BRIO' else 10_010
    runtime.submit_request(order('RADAR-BID', 'buy', 500, 9998), at_time_us=2000)
    runtime.submit_request(order('RADAR-ASK', 'sell', 300, ask), at_time_us=2000)
    # Scheduled orders use ordinary routing/matching. Delays and learner orders
    # can change outcomes; these are not executable promises or hidden ranks.
    if symbol == 'ASTER':
        runtime.cancel_order('RADAR-ASK', at_time_us=2_400_000)
        runtime.submit_request(order('ASTER-WIDE', 'sell', 600, 10_008), at_time_us=2_400_000)
        runtime.submit_request(order('ASTER-RETURN', 'sell', 200, 10_000), at_time_us=7_000_000)
    if symbol == 'BRIO':
        runtime.submit_request(order('BRIO-TIGHT', 'sell', 300, 10_000), at_time_us=3_000_000)
        runtime.submit_request(order('BRIO-PRESSURE', 'sell', 700), at_time_us=4_900_000)
        runtime.submit_request(order('BRIO-REPLENISH', 'buy', 500, 9998), at_time_us=6_000_000)
    # A bounded quote-heartbeat through actual deep additions makes freshness
    # observable even in quiet periods; it never invents public trade volume.
    for i, time_us in enumerate(range(950_000, STOP_US, STEP_US)):
        runtime.submit_request(order('RADAR-DEEP-'+str(i), 'buy', 1, 9950), at_time_us=time_us)
    runtime.advance_to(READY_US)
    return runtime
