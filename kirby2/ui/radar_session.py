"""Backend-owned world: sole clock authority, radar decisions and order routing.

No GUI handles, timers, network access, hidden labels in live frames, or detached
execution copy. Manual instructions run on the instrument already in this world.
"""
from __future__ import annotations
import copy
import threading
import uuid

from kirby2.full_day.models import canonical_sha256
from kirby2.features.market_observations import observe_delivered_book
from .execution_commitments import Commitments, ExecutionRefusal, integer
from .execution_recipes import order
from .playbooks import _parse, evaluate_session_playbook, template, exact
from .practice_library import _bytes
from .radar_recipes import SYMBOLS, READY_US, STOP_US, STEP_US, RECIPE, create_instrument

SCHEMA = 'KIRBY2_RADAR_WORLD_V1'
TTL_US, COOLDOWN_US, HYSTERESIS = 1_500_000, 750_000, 100
MAX_OPERATIONS, MAX_CANDIDATES, MAX_EVENTS, MAX_ORDERS = 512, 128, 1000, 16
_LOCK, _ACTIVE = threading.RLock(), {}


def capability():
    source = template(); source['execution']['quantity'] = 500
    return dict(schema_id=SCHEMA, recipe_id=RECIPE, symbols=list(SYMBOLS), ready_us=READY_US,
                stop_us=STOP_US, step_us=STEP_US, default_source=_bytes(source).decode(),
                candidate_ttl_us=TTL_US, cooldown_us=COOLDOWN_US, hysteresis_margin=HYSTERESIS,
                maximum_operations=MAX_OPERATIONS, maximum_candidates=MAX_CANDIDATES,
                maximum_events=MAX_EVENTS, maximum_orders=MAX_ORDERS, maximum_backlog_steps=4,
                actor='MANUAL_LEARNER', flow_activity='UNAVAILABLE_NO_PUBLIC_TAPE',
                notice='Eight-second synthetic rotation. One exposed instrument. Independent streams; no shared market factor. '
                       'Manual orders; playbook supplies eligibility only. Quotes are delayed; no public volume feed or relative-activity prior.')


class _World:
    def __init__(self, seed, source, mode):
        integer(seed, 'seed', 0, 2**31-1); _parse(source)
        if mode not in ('GUIDED','CONTINUOUS'): raise ValueError('unknown world mode')
        self.seed, self.source, self.mode = seed, source, mode
        self.world_id = 'world-'+uuid.uuid4().hex
        self.runtimes = {}; self.ledgers = {}; self.cursors = {}
        self.now = READY_US; self.revision = 0
        self.paused = mode == 'GUIDED'; self.frozen = False; self.locked = None
        self.armed = None; self.selected = None; self.inspected = SYMBOLS[0]
        self.candidates = []; self.current = {}; self.cooldowns = {}; self.ranking = []
        self.evaluations = []; self.visibility = []; self.operations = []; self.requests = {}
        self.busy = False; self.closed = False
        try:
            for symbol in SYMBOLS:
                self.runtimes[symbol] = create_instrument(seed, symbol)
                self.ledgers[symbol] = Commitments(); self.cursors[symbol] = 0
            _reconcile(self); _scan(self)
        except Exception:
            self.runtimes.clear(); self.ledgers.clear(); self.closed = True
            raise

    def __reduce__(self): raise TypeError('world authority cannot be serialized')


def _owned(world):
    if type(world) is not _World or _ACTIVE.get(world.world_id) is not world or world.closed:
        raise ExecutionRefusal('STALE_WORLD', 'This world no longer owns execution authority.')
    if world.busy: raise ExecutionRefusal('BUSY', 'A world instruction is settling.')


def _reconcile(world):
    for symbol in SYMBOLS:
        messages = world.runtimes[symbol].delivery.delivered_messages
        for message in messages[world.cursors[symbol]:]: world.ledgers[symbol].observe(message)
        world.cursors[symbol] = len(messages)


def _observation(world, symbol):
    result = dict(schema_id='KIRBY2_SESSION_RULE_OBSERVATION_V1', simulation_time_us=world.now,
                  market=copy.deepcopy(world.runtimes[symbol].delivery.latest_market_state))
    result['observation_id'] = 'observation-'+canonical_sha256(result)
    return result


def _evaluation(world, symbol):
    observation = _observation(world, symbol)
    result = evaluate_session_playbook(world.source, observation)
    market = observation['market']
    messages=world.runtimes[symbol].delivery.delivered_messages
    availability=next((m['delivery_time_us'] for m in reversed(messages)
                       if m['client_payload'].get('market_state')==market),None) if market else None
    measured=observe_delivered_book(market,observed_at_us=world.now,available_at_us=availability)
    score = None
    components=None
    if result['status'] == 'ELIGIBLE' and measured['status']=='AVAILABLE':
        # Availability/eligibility is decided BEFORE score. Larger scores prefer
        # a tighter spread, then visible bid size. No outcome or scenario inputs.
        if market and market['bid_levels'] and market['ask_levels']:
            spread = market['best_ask_ticks']-market['best_bid_ticks']
            depth=min(measured['values']['bid_depth_shares'],999)
            components=dict(base=1_000_000,spread_penalty=spread*1000,bid_depth_bonus=depth)
            score=components['base']-components['spread_penalty']+components['bid_depth_bonus']
    return dict(symbol=symbol, observation=observation, book_observation=measured,
                result=result, score=score,score_components=components)


def _scan(world):
    rows = [_evaluation(world, s) for s in SYMBOLS]
    for row in rows:
        symbol = row['symbol']; old = world.current.get(symbol)
        eligible = row['result']['status']=='ELIGIBLE' and row['score'] is not None
        if old and (world.now>=old['expires_us'] or not eligible):
            if old['disposition']=='OPEN': old['disposition'] = 'EXPIRED' if world.now>=old['expires_us'] else 'INVALIDATED'
            old['ended_us'] = world.now
            world.cooldowns[symbol] = world.now+COOLDOWN_US
            del world.current[symbol]; old = None
        if eligible and old is None and world.now>=world.cooldowns.get(symbol,0):
            if len(world.candidates)>=MAX_CANDIDATES: raise RuntimeError('candidate budget exhausted')
            old = dict(candidate_id='candidate-'+str(len(world.candidates)+1), symbol=symbol,
                       original_cut_us=world.now, expires_us=world.now+TTL_US, ended_us=None,
                       disposition='OPEN', selected_us=None, armed_us=None, orders=[],
                       initial_observation_id=row['observation']['observation_id'])
            world.current[symbol] = old; world.candidates.append(old)
        row['candidate_id'] = None if old is None else old['candidate_id']
        row['suppression'] = 'COOLDOWN' if eligible and old is None else None
    rankable=[r for r in rows if r['candidate_id'] and world.current[r['symbol']]['disposition']=='OPEN']
    world.ranking = rank_candidates(rankable,world.ranking[0] if world.ranking else None)
    world.evaluations.append(dict(cut_us=world.now, rows=rows, ranking=list(world.ranking)))


def rank_candidates(rows,previous_leader=None):
    """Availability precedes rank even if an input carries an extreme score."""
    ranked=sorted((r for r in rows if r['result']['status']=='ELIGIBLE' and r['score'] is not None),key=lambda r:(-r['score'],r['symbol']))
    incumbent=next((r for r in ranked if r['symbol']==previous_leader),None)
    if ranked and incumbent and ranked[0]['score']-incumbent['score']<HYSTERESIS:
        ranked.remove(incumbent);ranked.insert(0,incumbent)
    return [r['symbol'] for r in ranked]


def _exposed(world):
    return [s for s,l in world.ledgers.items() if l.position or l.unknown or
            any(o['unresolved'] or o['pending_cancel'] for o in l.orders.values())]


def _frame(world, refusal=None):
    body = dict(schema_id=SCHEMA, world_id=world.world_id, frame_id=f'{world.world_id}:{world.revision}',
                simulation_time_us=world.now, phase='FROZEN' if world.frozen else 'INTEGRITY_LOCKED' if world.locked else 'PAUSED' if world.paused else 'RUNNING',
                mode=world.mode, armed=world.armed, inspected=world.inspected, selected=world.selected,
                ranking=list(world.ranking), scan=copy.deepcopy(world.evaluations[-1]),
                candidates=copy.deepcopy(list(world.current.values())),
                accounts={s:l.view() for s,l in world.ledgers.items()},
                exposure_symbols=_exposed(world), refusal=refusal, integrity_reason=world.locked,
                remaining_operations=MAX_OPERATIONS-len(world.operations),
                world_times={s:r.clock.current_time_us for s,r in world.runtimes.items()})
    body['frame_sha256'] = canonical_sha256(body)
    return body


def start_world(seed=11, source=None, mode='GUIDED'):
    with _LOCK:
        world = _World(seed, capability()['default_source'] if source is None else source, mode)
        frame = _frame(world)
        _ACTIVE[world.world_id] = world
        return world, frame


def refresh_world(world):
    with _LOCK:
        _owned(world); return _frame(world)


def _candidate(world, identifier):
    row = next((r for r in world.candidates if r['candidate_id']==identifier),None)
    if row is None: raise ExecutionRefusal('UNKNOWN_CANDIDATE','Candidate does not belong to this world.')
    return row


def _valid_candidate(world, identifier):
    row = _candidate(world, identifier)
    if row['disposition'] not in ('OPEN','SELECTED') or world.now>=row['expires_us']:
        raise ExecutionRefusal('EXPIRED_CANDIDATE','Candidate expired or was already resolved. Select a current candidate.')
    current=_evaluation(world,row['symbol'])
    if current['result']['status']!='ELIGIBLE' or current['score'] is None:
        raise ExecutionRefusal('INELIGIBLE_CANDIDATE','Current delivered conditions no longer meet the playbook.')
    return row


FIELDS = {'ADVANCE':{'steps'},'INSPECT':{'symbol'},'SELECT':{'candidate_id'},'DECLINE':{'candidate_id'},
          'ARM':{'candidate_id'},'SUBMIT':{'symbol','side','quantity','price_ticks'},'CANCEL':{'symbol','order_id'},
          'PAUSE':set(),'RESUME':set(),'FREEZE':set(),'CHECKPOINT':set(),'OVERLOAD':set(),
          'PRESENT':{'candidate_ids','visibility'}}


def _apply(world, action, payload):
    if action not in FIELDS: raise ExecutionRefusal('INVALID_ACTION','Unknown world instruction.')
    if type(payload) is not dict or set(payload)!=FIELDS[action]:
        raise ExecutionRefusal('INVALID_INPUT','World instruction fields do not match the selected action.')
    if world.frozen: raise ExecutionRefusal('FROZEN','Frozen world is immutable.')
    if world.locked and action not in ('FREEZE','CHECKPOINT'):
        raise ExecutionRefusal('INTEGRITY_LOCKED','Freeze or close the world; current execution knowledge is uncertain.')
    if action=='ADVANCE':
        steps = integer(payload['steps'],'world steps',1,4)
        if world.paused: raise ExecutionRefusal('WORLD_PAUSED','Resume the whole world first.')
        if world.now+steps*STEP_US>STOP_US: raise ExecutionRefusal('WORLD_HORIZON','Freeze or close this bounded session.')
        for _ in range(steps):
            target = world.now+STEP_US
            # Fixed symbol order. A partial unexpected failure locks the composite;
            # no frame advertises coherent clocks until replay verifies recovery.
            for symbol in SYMBOLS: world.runtimes[symbol].advance_to(target)
            world.now = target; _reconcile(world); _scan(world)
            if sum(len(r.events) for r in world.runtimes.values())>MAX_EVENTS: raise RuntimeError('world event budget exhausted')
        if world.now==STOP_US: world.paused = True
    elif action=='INSPECT':
        if payload['symbol'] not in SYMBOLS: raise ExecutionRefusal('INVALID_SYMBOL','Unknown instrument.')
        world.inspected = payload['symbol']
    elif action in ('SELECT','DECLINE','ARM'):
        row = _valid_candidate(world,payload['candidate_id'])
        if action=='DECLINE': row['disposition']='DECLINED'
        else:
            if any(s!=row['symbol'] for s in _exposed(world)):
                raise ExecutionRefusal('OTHER_EXPOSURE','Settle the currently exposed instrument, including cancel receipts, before transferring execution.')
            if action=='SELECT':
                if row['selected_us'] is not None:raise ExecutionRefusal('ALREADY_SELECTED','This candidate has already been selected; its original selection cut is retained.')
                row['selected_us']=world.now; row['disposition']='SELECTED'; world.selected=row['candidate_id']
            else:
                if world.selected!=row['candidate_id']: raise ExecutionRefusal('SELECT_FIRST','Select and inspect the current candidate before arming.')
                row['armed_us']=world.now; world.armed=row['symbol']
    elif action=='SUBMIT':
        if world.paused: raise ExecutionRefusal('WORLD_PAUSED','Resume before routing an order.')
        symbol=payload['symbol']
        if symbol!=world.armed: raise ExecutionRefusal('ARMED_SYMBOL_CHANGED','Order target differs from the explicitly armed instrument.')
        if any(s!=symbol for s in _exposed(world)): raise ExecutionRefusal('OTHER_EXPOSURE','Another instrument retains exposure.')
        if sum(len(l.orders) for l in world.ledgers.values())>=MAX_ORDERS:
            raise ExecutionRefusal('ORDER_BUDGET','This world has reached its declared order budget. Cancel, freeze or close; no more orders can be routed.')
        if payload['side']=='buy':
            row=_valid_candidate(world,world.selected)
            if row['symbol']!=symbol: raise ExecutionRefusal('ARMED_SYMBOL_CHANGED','Selected and armed identities differ.')
        market=_observation(world,symbol)['market']
        if not market or world.now-market['simulation_time_us']>500000: raise ExecutionRefusal('STALE_MARKET','New orders require current delivered quotes.')
        price=integer(payload['price_ticks'],'limit price',9900,10100)
        ledger=world.ledgers[symbol]; identifier='RADAR-PLAYER-'+str(len(ledger.orders)+1)
        ledger.reserve(identifier,payload['side'],payload['quantity'])
        world.runtimes[symbol].submit_request(order(identifier,payload['side'],payload['quantity'],price,player=True))
        row=_candidate(world,world.selected)
        row['orders'].append(identifier)
    elif action=='CANCEL':
        symbol=payload['symbol']
        if symbol!=world.armed: raise ExecutionRefusal('ARMED_SYMBOL_CHANGED','Cancel targets the armed instrument only.')
        ledger=world.ledgers[symbol]; row=ledger.orders.get(payload['order_id'])
        if row is None: raise ExecutionRefusal('UNKNOWN_ORDER','Order does not belong to the armed instrument.')
        if row['unresolved'] and not row['pending_cancel']:
            row['pending_cancel']=True; world.runtimes[symbol].cancel_order(payload['order_id'])
    elif action=='PAUSE': world.paused=True
    elif action=='RESUME':
        if world.now==STOP_US: raise ExecutionRefusal('WORLD_HORIZON','Session has reached its final cut.')
        world.paused=False
    elif action=='OVERLOAD': world.paused=True
    elif action=='PRESENT':
        ids=payload['candidate_ids']; state=payload['visibility']
        if type(ids) is not list or len(ids)>3 or len(set(ids))!=len(ids): raise ValueError('invalid presented candidates')
        if state not in ('PRESENTED','BACKGROUND','MINIMIZED','SUPPRESSED','UNKNOWN'): raise ValueError('invalid visibility')
        allowed={r['candidate_id'] for r in world.current.values()}
        if not set(ids)<=allowed: raise ExecutionRefusal('STALE_PRESENTATION','Presentation no longer matches the current scan.')
        world.visibility.append(dict(cut_us=world.now,candidate_ids=list(ids),visibility=state,noticed=None,response_time_us=None))
    elif action in ('CHECKPOINT','FREEZE'):
        for runtime in world.runtimes.values(): runtime.capture_quiescent_cut()
        if action=='FREEZE': world.frozen=True; world.paused=True


def _execute(world, action, payload):
    before=canonical_sha256(scientific_frame(_frame(world))); refusal=None
    try: _apply(world,action,payload)
    except ExecutionRefusal as error: refusal=dict(code=error.code,message=str(error))
    world.operations.append(dict(action=action,payload=copy.deepcopy(payload),at_us=world.now,
                                 before_sha256=before,refusal=refusal))
    world.revision+=1
    return _frame(world,refusal)


def scientific_frame(frame):
    """Session identity is operational; derived world state is deterministic."""
    return {k:v for k,v in frame.items() if k not in ('world_id','frame_id','frame_sha256')}


def act_world(world, frame_id, request_id, action, payload):
    with _LOCK:
        _owned(world)
        if type(request_id) is not str or not 1<=len(request_id)<=128: raise ValueError('invalid request identity')
        signature=canonical_sha256(dict(action=action,payload=payload))
        if request_id in world.requests:
            if world.requests[request_id][0]!=signature: raise ExecutionRefusal('REQUEST_CONFLICT','Retry changed the original instruction.')
            return _frame(world,world.requests[request_id][1])
        if frame_id!=_frame(world)['frame_id']: raise ExecutionRefusal('STALE_FRAME','Instruction was bound to an older world cut.')
        if len(world.operations)>=MAX_OPERATIONS-1 and action!='FREEZE':
            raise ExecutionRefusal('OPERATION_BUDGET','Operation budget reached; freeze or close the world.')
        if world.frozen: raise ExecutionRefusal('FROZEN','Frozen world is immutable.')
        world.busy=True
        try:
            result=_execute(world,action,payload); world.requests[request_id]=(signature,result['refusal'])
            return result
        except (ValueError,TypeError) as error:
            # Validation failures cannot free reservations; return explicit lock
            # if a route may already have been acquired.
            world.locked='INSTRUCTION_FAILED: '+str(error); world.paused=True
            raise
        except Exception as error:
            world.locked='WORLD_ADVANCE_FAILED: '+str(error); world.paused=True
            raise
        finally: world.busy=False


def close_world(world):
    with _LOCK:
        _owned(world)
        world.closed=True; del _ACTIVE[world.world_id]
        world.runtimes.clear()
        return dict(status='CLOSED',execution_settlement='NOT_CLAIMED')


def retained_worlds():
    with _LOCK: return len(_ACTIVE)
