"""Optional governed execution practice API with one mutable owner.

Live projections contain delivered knowledge only. Frozen review explicitly
reveals venue truth. No network, Qt, global replay inventory or second market.
"""
from __future__ import annotations
import copy
import threading
import uuid

from kirby2.full_day.models import canonical_sha256
from .execution_commitments import Commitments, ExecutionRefusal, integer
from .execution_recipes import READY_US, STOP_US, PRICE_MIN, PRICE_MAX, RECIPES, create_runtime, order

_LOCK = threading.RLock()
_ACTIVE = {}
_TOKEN = object()
SCHEMA = "KIRBY2_EXECUTION_PRACTICE_V1"
MAX_OPERATIONS = 256
STALE_US = 500_000


def execution_capability():
    return dict(schema_id=SCHEMA,schema_version=1,ready=True,recipe_id="execution.cancel-race.v1",
        title="Delayed execution · synthetic practice",recipes=[dict(recipe_id=k,title=v[0]) for k,v in RECIPES.items()],
        default_seed=11,ready_us=READY_US,stop_us=STOP_US,
        maximum_shares=1000,price_min_ticks=PRICE_MIN,price_max_ticks=PRICE_MAX,stale_after_us=STALE_US,
        notice="Single synthetic venue. Step simulation time to observe delayed reports. "
               "Reservations include possible fills. No real-market or human-reaction claim.")


class _Handle:
    __slots__ = ('source_id','runtime','ledger','cursor','revision','held','intent','intent_generation',
                 'requests','operations','closed','frozen','busy','seed','recipe_id')
    def __init__(self, token, seed, recipe_id='execution.cancel-race.v1'):
        if token is not _TOKEN: raise TypeError('execution authority is backend-owned')
        self.source_id = 'execution-'+uuid.uuid4().hex
        self.runtime = create_runtime(seed,recipe_id)
        self.ledger = Commitments()
        self.cursor = 0; self.revision = 0; self.held = False
        self.intent = None; self.intent_generation = 0
        self.requests = {}; self.operations = []
        self.closed = False; self.frozen = False; self.busy = False; self.seed = seed; self.recipe_id = recipe_id
        _reconcile(self)
    def __reduce__(self): raise TypeError('execution handles cannot be serialized')


def _owned(handle):
    if type(handle) is not _Handle or _ACTIVE.get(handle.source_id) is not handle or handle.closed:
        raise ExecutionRefusal('STALE_SOURCE','This practice no longer owns mutable execution authority.')
    return handle


def _reconcile(handle):
    messages = handle.runtime.delivery.delivered_messages
    for message in messages[handle.cursor:]: handle.ledger.observe(message)
    handle.cursor = len(messages)
    if handle.intent and handle.runtime.clock.current_time_us >= handle.intent['expires_us']:
        _disarm(handle)


def _disarm(handle):
    handle.intent = None
    handle.intent_generation += 1


def _knowledge(handle):
    runtime = handle.runtime
    market = copy.deepcopy(runtime.delivery.latest_market_state)
    age = None if market is None else runtime.clock.current_time_us-market['simulation_time_us']
    receipts = []
    for message in runtime.delivery.delivered_messages:
        data = message['client_payload'].get('event_data',{})
        if set(handle.ledger.orders).intersection(v for k,v in data.items() if k.endswith('order_id') and type(v) is str):
            receipts.append(dict(kind=message['kind'],event_type=message['client_payload']['event_type'],
                event_id=message['client_payload']['mechanics_sequence'],source_us=message['source_time_us'],
                available_us=message['delivery_time_us'],data=copy.deepcopy(data)))
    return dict(market=market,market_age_us=age,market_status='MISSING' if age is None else 'STALE' if age>STALE_US else 'CURRENT',
                account=handle.ledger.view(),receipts=receipts)


def _frame(handle, refusal=None):
    body = dict(schema_id=SCHEMA,schema_version=1,source_id=handle.source_id,
        frame_id=f'{handle.source_id}:{handle.revision}',simulation_time_us=handle.runtime.clock.current_time_us,
        phase='FROZEN' if handle.frozen else 'INTEGRITY_LOCKED' if handle.ledger.unknown else 'HELD' if handle.held else 'READY',
        knowledge=_knowledge(handle),intent=copy.deepcopy(handle.intent),intent_generation=handle.intent_generation,
        refusal=refusal,simulated_timing=True,human_reaction_measurement=None)
    body['frame_sha256'] = canonical_sha256(body)
    return body


def _decision(handle):
    knowledge = _knowledge(handle)
    market = knowledge['market']
    return dict(simulation_time_us=handle.runtime.clock.current_time_us,
        known_market_cut_us=None if market is None else market['simulation_time_us'],
        market_status=knowledge['market_status'],account=knowledge['account'],
        received_event_ids=[r['event_id'] for r in knowledge['receipts']],
        knowledge_sha256=canonical_sha256(knowledge))


def refresh_execution_practice(handle):
    """Refresh already-delivered knowledge after a client presentation failure."""
    with _LOCK:
        _owned(handle)
        if handle.busy: raise ExecutionRefusal('BUSY','An execution action is settling.')
        _reconcile(handle)
        return _frame(handle)


def start_execution_practice(recipe_id='execution.cancel-race.v1',seed=11):
    if recipe_id not in RECIPES: raise ValueError('unsupported execution recipe')
    with _LOCK:
        handle = _Handle(_TOKEN,seed,recipe_id)
        # Acquisition is published only after the complete projection succeeds.
        frame = _frame(handle)
        _ACTIVE[handle.source_id] = handle
        return handle,frame


def _new_order(handle, payload):
    if set(payload) != {'side','quantity','price_ticks'}:
        raise ExecutionRefusal('INVALID_INPUT','Provide side, quantity and limit price.','LEARNER_INSTRUCTION')
    price = integer(payload['price_ticks'],'limit price',PRICE_MIN,PRICE_MAX)
    if _knowledge(handle)['market_status'] != 'CURRENT':
        raise ExecutionRefusal('STALE_MARKET','Market observation is stale or missing; new exposure is refused. Cancellation remains available.')
    identifier = 'EX-PLAYER-'+str(len(handle.ledger.orders)+1)
    # Reserve before any route can be enqueued. An unexpected routing error
    # retains reservation and locks exposure rather than freeing possible risk.
    handle.ledger.reserve(identifier,payload['side'],payload['quantity'])
    handle.runtime.submit_request(order(identifier,payload['side'],payload['quantity'],price,player=True))
    return identifier


def _cancel(handle, identifier):
    if type(identifier) is not str:
        raise ExecutionRefusal('INVALID_ORDER','Select an order from this practice.','LEARNER_INSTRUCTION')
    row = handle.ledger.orders.get(identifier)
    if row is None: raise ExecutionRefusal('UNKNOWN_ORDER','Select an order from this practice.','LEARNER_INSTRUCTION')
    if row['pending_cancel'] or row['unresolved']==0: return
    row['pending_cancel'] = True
    handle.runtime.cancel_order(identifier)


def _apply(handle, action, payload):
    if type(payload) is not dict: raise ExecutionRefusal('INVALID_INPUT','Action payload must be an object.','LEARNER_INSTRUCTION')
    if handle.frozen: raise ExecutionRefusal('FROZEN','This immutable attempt is available for review only.')
    if handle.held and action not in ('RESUME','DISARM','CANCEL','FREEZE'):
        raise ExecutionRefusal('GUIDED_HOLD','Resume the held passage before advancing or adding exposure.','PLAYBOOK_CONDITION')
    if handle.ledger.unknown and action not in ('CANCEL','DISARM','FREEZE'):
        raise ExecutionRefusal('UNKNOWN_EXPOSURE','Execution state is uncertain; cancellation and frozen review remain available.')
    expected = {'ADVANCE':{'delta_us'},'SUBMIT':{'side','quantity','price_ticks'},'CANCEL':{'order_id'},
                'ARM':{'target','price_ticks','expires_us'},'FIRE':{'intent_generation'},'REDUCE':{'quantity','price_ticks'},
                'DISARM':set(),'HOLD':set(),'RESUME':set(),'FREEZE':{'policy'}}
    if action not in expected or set(payload)!=expected[action]:
        raise ExecutionRefusal('INVALID_INPUT','Unsupported action or fields.','LEARNER_INSTRUCTION')
    if action=='ADVANCE':
        delta = integer(payload['delta_us'],'advance',1,STOP_US-READY_US)
        target = handle.runtime.clock.current_time_us+delta
        if target>STOP_US: raise ExecutionRefusal('END_OF_EXERCISE','Reached the bounded exercise horizon; freeze for review.')
        handle.runtime.advance_to(target)
        _reconcile(handle)
    elif action=='SUBMIT': _new_order(handle,payload)
    elif action=='CANCEL': _cancel(handle,payload['order_id'])
    elif action=='ARM':
        target = integer(payload['target'],'target',0,1000)
        price = integer(payload['price_ticks'],'limit price',PRICE_MIN,PRICE_MAX)
        expiry = integer(payload['expires_us'],'expiry',handle.runtime.clock.current_time_us+1,STOP_US)
        _disarm(handle)
        handle.intent = dict(target=target,price_ticks=price,expires_us=expiry,generation=handle.intent_generation)
    elif action=='FIRE':
        if handle.intent is None or type(payload['intent_generation']) is not int or payload['intent_generation']!=handle.intent_generation:
            raise ExecutionRefusal('DISARMED_INTENT','This intent has expired or was disarmed. Arm a new intent explicitly.','PLAYBOOK_CONDITION')
        intent = copy.deepcopy(handle.intent)
        # One explicit firing, never an automatically recurring target.
        _disarm(handle)
        low,high = handle.ledger.interval()
        quantity = intent['target']-high
        if quantity<=0: raise ExecutionRefusal('TARGET_RESERVED','Confirmed and possible exposure already cover this entry target.','PLAYBOOK_CONDITION')
        _new_order(handle,dict(side='buy',quantity=quantity,price_ticks=intent['price_ticks']))
    elif action=='REDUCE':
        quantity = integer(payload['quantity'],'reduction',1,1000)
        if quantity>handle.ledger.interval()[0]:
            raise ExecutionRefusal('REDUCTION_RESERVED','Existing sell commitments already reserve that confirmed exposure.')
        _new_order(handle,dict(side='sell',quantity=quantity,price_ticks=payload['price_ticks']))
    elif action=='DISARM': _disarm(handle)
    elif action=='HOLD': handle.held = True
    elif action=='RESUME': handle.held = False
    elif action=='FREEZE':
        if payload['policy'] not in ('PARTIAL','COMPLETE'): raise ExecutionRefusal('INVALID_INPUT','Choose PARTIAL or COMPLETE.','LEARNER_INSTRUCTION')
        if payload['policy']=='COMPLETE' and (handle.ledger.unknown or any(o['unresolved'] or o['pending_cancel'] for o in handle.ledger.orders.values())):
            raise ExecutionRefusal('UNRESOLVED_EXECUTION','Outstanding execution requires a partial recording; completion cannot claim settlement.')
        _disarm(handle)
        handle.runtime.capture_quiescent_cut()
        handle.frozen = True


def act_execution_practice(handle, frame_id, request_id, action, payload):
    with _LOCK:
        _owned(handle)
        if handle.busy: raise ExecutionRefusal('BUSY','An execution action is settling.')
        if type(request_id) is not str or not 1<=len(request_id)<=128:
            raise ExecutionRefusal('INVALID_REQUEST','A bounded correlated request ID is required.')
        signature = canonical_sha256(dict(action=action,payload=payload))
        prior = handle.requests.get(request_id)
        if prior is not None:
            if prior[0]!=signature: raise ExecutionRefusal('REQUEST_CONFLICT','A correlated retry changed its instruction.')
            return _frame(handle,prior[1])
        if handle.frozen:
            raise ExecutionRefusal('FROZEN','This immutable attempt accepts only review, saving and cleanup.')
        if frame_id!=f'{handle.source_id}:{handle.revision}': raise ExecutionRefusal('STALE_FRAME','Action belongs to an abandoned client frame.')
        if len(handle.operations)>=MAX_OPERATIONS or (len(handle.operations)>=MAX_OPERATIONS-1 and action!='FREEZE'):
            raise ExecutionRefusal('OPERATION_LIMIT','Freeze this bounded attempt before starting another.')
        handle.busy = True
        refusal = None
        try:
            decision = _decision(handle)
            try: _apply(handle,action,copy.deepcopy(payload))
            except ExecutionRefusal as error:
                refusal = dict(code=error.code,category=error.category,message=str(error))
            except Exception:
                handle.ledger.unknown = 'UNSETTLED_BACKEND_OPERATION'
                _disarm(handle)
                raise
            finally:
                handle.revision += 1
            handle.operations.append(dict(action=action,payload=copy.deepcopy(payload),refusal=copy.deepcopy(refusal),decision=decision))
            handle.requests[request_id] = (signature,copy.deepcopy(refusal))
            return _frame(handle,refusal)
        finally: handle.busy = False


def close_execution_practice(handle):
    with _LOCK:
        if type(handle) is _Handle and handle.closed:
            return dict(status='CLOSED',execution_settlement='NOT_CLAIMED')
        _owned(handle)
        if handle.busy: raise ExecutionRefusal('BUSY','Execution settlement is in progress.')
        _disarm(handle)
        handle.closed = True
        del _ACTIVE[handle.source_id]
        return dict(status='CLOSED',execution_settlement='NOT_CLAIMED')


def frozen_execution_review(handle):
    with _LOCK:
        _owned(handle)
        if not handle.frozen: raise ExecutionRefusal('MUTABLE_REVIEW','Freeze practice before revealing venue truth.')
        return _review(handle)


def _review(handle):
    # Venue truth is never included in _frame or any live callback.
    venue = [event.as_dict() for event in handle.runtime.engine.events]
    comparisons = []
    for operation in handle.operations:
        decision = operation['decision']
        position,events = 0,[]
        for event in venue:
            if event['simulation_time_us']>decision['simulation_time_us'] or event['event_type']!='TRADE': continue
            data = event['data']
            for key in (data.get('maker_order_id'),data.get('taker_order_id')):
                if key not in handle.ledger.orders: continue
                position += data['quantity'] if handle.ledger.orders[key]['side']=='buy' else -data['quantity']
                events.append(event['sequence'])
        comparisons.append(dict(action=operation['action'],simulation_time_us=decision['simulation_time_us'],
            client_confirmed_position=decision['account']['confirmed_position'],venue_position=position,
            possible_position=decision['account']['possible_position'],venue_fill_event_ids=events,
            known_market_cut_us=decision['known_market_cut_us'],knowledge_sha256=decision['knowledge_sha256']))
    return dict(schema_id='KIRBY2_EXECUTION_REVIEW_V1',seed=handle.seed,recipe_id=handle.recipe_id,
        final_knowledge=_knowledge(handle),venue_events=venue,operations=copy.deepcopy(handle.operations),decision_comparisons=comparisons,
        timing='SIMULATED_MICROSECONDS',human_reaction_measurement=None,
        settlement='UNKNOWN' if handle.ledger.unknown else 'OUTSTANDING' if any(o['unresolved'] for o in handle.ledger.orders.values()) else 'ACCOUNTED',
        resource_release_does_not_cancel_orders=True)
