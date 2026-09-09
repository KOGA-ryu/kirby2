"""Immutable world records, exact replay and pending-cut continuation.

Venue facts appear only after freezing. All three instruments survive review,
including exclusions and candidates which were never selected or presented.
"""
from __future__ import annotations
import copy
import re

from kirby2.full_day.models import canonical_sha256
from kirby2.full_day.runtime import FullDayRuntime
from . import radar_session as session
from . import playbook_store as store
from .playbooks import _parse, exact
from .playbook_trials import midpoint, fill_measurement
from .radar_recipes import SYMBOLS, RECIPE, READY_US, STEP_US
from .execution_commitments import ExecutionRefusal

SCHEMA='KIRBY2_RADAR_RECORD_V1'
PREFIX='radar-evidence-'


def _venue_market(runtime, time_us):
    # Every public book mutation creates a MARKET_STATE message. Frozen
    # assessment may read source-time states still awaiting client delivery.
    messages=runtime.delivery.delivered_messages+[m.as_dict() for m in runtime.delivery.pending_messages.values()]
    available=[m for m in messages if 'market_state' in m['client_payload'] and m['source_time_us']<=time_us]
    if not available:return None
    return max(available,key=lambda m:(m['source_time_us'],m['message_sequence']))['client_payload']['market_state']


def _metrics(world):
    document,_,_=_parse(world.source); policy=document['evaluation']
    rows=[]
    for symbol in SYMBOLS:
        runtime=world.runtimes[symbol]; orders=world.ledgers[symbol].orders
        submitted=[o for o in world.operations if o['action']=='SUBMIT' and o['refusal'] is None and o['payload']['symbol']==symbol]
        for i,operation in enumerate(submitted):
            identifier='RADAR-PLAYER-'+str(i+1); order=orders[identifier]
            cut=operation['at_us']; decision=next((e for e in reversed(world.evaluations) if e['cut_us']<=cut),None)
            market=next(r['observation']['market'] for r in decision['rows'] if r['symbol']==symbol)
            measurements=[]
            for event in runtime.engine.events:
                event=event.as_dict(); data=event['data']
                if event['event_type']!='TRADE' or identifier not in (data['maker_order_id'],data['taker_order_id']):continue
                horizon=event['simulation_time_us']+policy['markout_horizon_us']
                future=None if horizon>world.now else _venue_market(runtime,horizon)
                measure=fill_measurement(order['side'],data['quantity'],data['price_ticks'],midpoint(market),
                                        midpoint(_venue_market(runtime,cut+2699)),midpoint(future),policy['fee_milliticks_per_share'])
                measurements.append(dict(trade_id=data['trade_id'],fill_us=event['simulation_time_us'],horizon_us=horizon,
                                         horizon_status='TRUNCATED' if horizon>world.now else 'MISSING_MIDPOINT' if midpoint(future) is None else 'AVAILABLE',
                                         measurement=measure))
            venue_filled=sum(fill['measurement']['quantity'] for fill in measurements)
            rows.append(dict(symbol=symbol,order_id=identifier,decision_cut_us=cut,quantity=order['quantity'],
                             filled=venue_filled,known_filled=order['filled'],unresolved=order['unresolved'],
                             unresolved_basis='CLIENT_KNOWLEDGE_INCLUDES_UNREPORTED_FILLS',fills=measurements,
                             outcome='UNFILLED' if not venue_filled else 'PARTIAL' if venue_filled<order['quantity'] else 'FILLED'))
    return dict(orders=rows,execution_actor='MANUAL_LEARNER',
                markout_kind='SOURCE_TIME_VENUE_MIDPOINT_NOT_AN_EXECUTABLE_EXIT',
                realized_pnl=None,probability_of_success=None)


def _review(world):
    if not world.frozen:raise ExecutionRefusal('MUTABLE_REVIEW','Freeze the whole world before revealing venue evidence.')
    candidates=copy.deepcopy(world.candidates)
    for row in candidates:
        views=[v for v in world.visibility if row['candidate_id'] in v['candidate_ids']]
        row['visibility_receipts']=views
        row['attention']='PRESENTED_ATTENTION_UNKNOWN' if any(v['visibility']=='PRESENTED' for v in views) else 'NOT_CONFIRMED_PRESENTED'
        if row['selected_us'] is None and row['disposition'] in ('OPEN','EXPIRED','INVALIDATED'):
            row['review_disposition']='MISSED_OR_UNNOTICED' if row['attention']=='PRESENTED_ATTENTION_UNKNOWN' else 'BACKGROUND_OR_UNPRESENTED'
        else:row['review_disposition']=row['disposition']
    return dict(schema_id='KIRBY2_RADAR_REVIEW_V1',recipe_id=RECIPE,final_cut_us=world.now,
                mode=world.mode,candidates=candidates,evaluations=copy.deepcopy(world.evaluations),
                visibility=copy.deepcopy(world.visibility),operations=copy.deepcopy(world.operations),
                accounts={s:l.view() for s,l in world.ledgers.items()},metrics=_metrics(world),
                venue_events={s:[e.as_dict() for e in r.engine.events] for s,r in world.runtimes.items()},
                settlement='UNKNOWN' if world.locked or any(l.unknown for l in world.ledgers.values()) else
                           'OUTSTANDING' if any(o['unresolved'] or o['pending_cancel'] for l in world.ledgers.values() for o in l.orders.values()) else 'ACCOUNTED',
                notice='Synthetic conditional evidence. Presentation does not prove attention. Unfilled orders and exclusions remain evidence.')


def review_world(world):
    with session._LOCK:
        session._owned(world);return _review(world)


def _record(world):
    return store.seal(dict(schema_id=SCHEMA,recipe_id=RECIPE,attempt_id=world.world_id,seed=world.seed,source=world.source,
                          mode=world.mode,operations=copy.deepcopy(world.operations),
                          checkpoints={s:r.checkpoint_state() for s,r in world.runtimes.items()},
                          state=session.scientific_frame(session._frame(world)),
                          review=_review(world) if world.frozen else None),PREFIX)


def verify_record(record):
    store.verify(record,PREFIX)
    exact(record,('schema_id','recipe_id','attempt_id','seed','source','mode','operations','checkpoints','state','review','artifact_id'),'world record')
    if record['schema_id']!=SCHEMA or record['recipe_id']!=RECIPE:raise ValueError('unsupported world record')
    if type(record['attempt_id']) is not str or re.fullmatch(r'world-[0-9a-f]{32}',record['attempt_id']) is None:raise ValueError('invalid world attempt identity')
    operations=record['operations']
    if type(operations) is not list or not 1<=len(operations)<=session.MAX_OPERATIONS:raise ValueError('world operation budget differs')
    if operations[-1]['action'] not in ('CHECKPOINT','FREEZE'):raise ValueError('record must end at an explicit world checkpoint')
    exact(record['checkpoints'],SYMBOLS,'world checkpoint inventory')
    world=session._World(record['seed'],record['source'],record['mode'])
    for operation in operations:
        exact(operation,('action','payload','at_us','before_sha256','refusal'),'world operation')
        if world.frozen:raise ValueError('world instructions cannot follow freeze')
        if len(world.operations)>=session.MAX_OPERATIONS-1 and operation['action']!='FREEZE':raise ValueError('final instruction slot is reserved for freeze')
        session._execute(world,operation['action'],operation['payload'])
        if world.operations[-1]!=operation:raise ValueError('recorded world decision or refusal differs from replay')
    if operations[-1]['refusal'] is not None:raise ValueError('record does not end at a successful checkpoint')
    if session.scientific_frame(session._frame(world))!=record['state']:raise ValueError('world projection differs from replay')
    for symbol in SYMBOLS:
        restored=FullDayRuntime.from_checkpoint_state(record['checkpoints'][symbol])
        if restored.canonical_state_bytes()!=world.runtimes[symbol].canonical_state_bytes():raise ValueError('world checkpoint differs from replay')
        world.runtimes[symbol]=restored
    if record['review']!=(_review(world) if world.frozen else None):raise ValueError('world review differs from replay')
    return world


def checkpoint_world(world):
    with session._LOCK:
        session._owned(world)
        if world.frozen:raise ExecutionRefusal('FROZEN','Use Save for a frozen world.')
        if len(world.operations)>=session.MAX_OPERATIONS-1:raise ExecutionRefusal('OPERATION_BUDGET','Freeze or close the world.')
        session._execute(world,'CHECKPOINT',{})
        return _record(world)


def restore_world(record):
    with session._LOCK:
        world=verify_record(record)
        if world.frozen:raise ValueError('frozen records are review-only')
        frame=session._frame(world);session._ACTIVE[world.world_id]=world
        return world,frame


def save_world(world,root=None):
    with session._LOCK:
        session._owned(world)
        if not world.frozen:raise ExecutionRefusal('MUTABLE_SAVE','Freeze the whole world before saving its evidence.')
        record=_record(world);verify_record(record);store.publish(record,root)
        return dict(evidence_id=record['artifact_id'],review=record['review'])


def open_world(identifier,root=None):
    record=store.load(identifier,PREFIX,root);world=verify_record(record)
    if not world.frozen:raise ValueError('saved world is not frozen')
    return dict(evidence_id=identifier,review=record['review'])


def list_worlds(root=None):
    return store.scan(PREFIX,root)
