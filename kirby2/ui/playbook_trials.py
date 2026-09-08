"""One bounded synthetic trial, with decision knowledge separate from assessment."""
from __future__ import annotations
import copy

from kirby2.full_day.models import canonical_sha256
from . import execution_practice as practice
from . import execution_library as library
from .execution_recipes import READY_US, STOP_US
from .execution_commitments import integer
from .playbooks import _parse, observation, evaluate_playbook

BRANCH = 'REBUILT_MATCHING_AND_BOOK_RELATIVE_PLACEMENT_V1'


def midpoint(market):
    if market is None: return None
    bid, ask = market['best_bid_ticks'], market['best_ask_ticks']
    return None if bid is None or ask is None else bid+ask


def venue_midpoint(runtime):
    book = runtime.engine.book.snapshot()
    return None if not book['bids'] or not book['asks'] else book['bids'][0]['price_ticks']+book['asks'][0]['price_ticks']


def fill_measurement(side, quantity, price_ticks, decision_mid2, arrival_mid2, horizon_mid2, fee_milliticks):
    """Signed arithmetic: positive cost is adverse; positive markout is favorable."""
    if side not in {'buy','sell'}: raise ValueError('invalid measurement side')
    integer(quantity,'measured shares',1,1000000);integer(price_ticks,'fill price',1,1000000)
    integer(fee_milliticks,'fee milli-ticks per share',0,10000)
    for value in (decision_mid2,arrival_mid2,horizon_mid2):
        if value is not None:integer(value,'midpoint half ticks',2,2000000)
    sign = 1 if side=='buy' else -1
    return dict(quantity=quantity, price_ticks=price_ticks, side=side,
                decision_cost_half_tick_shares=None if decision_mid2 is None else sign*(2*price_ticks-decision_mid2)*quantity,
                arrival_cost_half_tick_shares=None if arrival_mid2 is None else sign*(2*price_ticks-arrival_mid2)*quantity,
                markout_half_tick_shares=None if horizon_mid2 is None else sign*(horizon_mid2-2*price_ticks)*quantity,
                fee_millitick_shares=fee_milliticks*quantity)


def run_trial(source, case, cancel, root):
    document, _, playbook_id = _parse(source)
    policy = document['execution']; evaluation_policy = document['evaluation']
    handle, frame = practice.start_execution_practice(case['recipe_id'],case['seed'])
    trace, samples, fills = [], {}, {}
    submitted = None; arrival_mid2 = None; decision_mid2 = None; eligible = False
    status = 'COMPLETE'; error = None; candidate_mid2 = None

    def act(action, **payload):
        nonlocal frame
        frame = practice.act_execution_practice(handle,frame['frame_id'],str(len(handle.operations)+1),action,payload)
        if frame['refusal'] is not None: raise ValueError('script instruction refused: '+frame['refusal']['code'])

    def evaluate(since=None):
        cut = observation(frame); result = evaluate_playbook(source,cut,since)
        trace.append(dict(stage='EVALUATION',observation=cut,result=result))
        return result

    def collect():
        orders = handle.ledger.orders
        for event in handle.runtime.engine.events:
            if event.event_type.value != 'TRADE': continue
            row = event.as_dict(); data = row['data']
            if not set(orders).intersection((data['maker_order_id'],data['taker_order_id'])): continue
            fills.setdefault(data['trade_id'], row)

    try:
        initial = evaluate(); eligible = initial['status']=='ELIGIBLE'
        candidate_mid2 = midpoint(frame['knowledge']['market'])
        trace.append(dict(stage='ELIGIBILITY',selected=eligible,reason=initial['status']))
        if eligible:
            delay = policy['decision_delay_us']
            if delay: act('ADVANCE',delta_us=delay)
            if cancel.is_set(): status='CANCELLED'
            else:
                decision = evaluate(READY_US)
                if decision['status']=='ELIGIBLE':
                    submitted = frame['simulation_time_us']
                    decision_mid2 = midpoint(frame['knowledge']['market'])
                    act('SUBMIT',side='buy',quantity=policy['quantity'],price_ticks=policy['price_ticks'])
                    trace.append(dict(stage='INTENT',at_us=submitted,order_id='EX-PLAYER-1',policy=copy.deepcopy(policy)))
                else: trace.append(dict(stage='NOT_SELECTED',reason=decision['status']))
        expiry = READY_US+policy['lifetime_us']
        # A 1 ms observation cadence is part of the frozen V1 script policy.
        # Benchmark/markout sampling does not execute extra rule decisions.
        decisions = set(range(READY_US+1000,STOP_US+1,1000)) | {expiry}
        decisions = {t for t in decisions if t>frame['simulation_time_us']}
        arrival_cut = None if submitted is None else submitted+2699
        while frame['simulation_time_us']<STOP_US and status=='COMPLETE':
            if cancel.is_set(): status='CANCELLED'; break
            now = frame['simulation_time_us']
            collect()
            horizons = {f['simulation_time_us']+evaluation_policy['markout_horizon_us'] for f in fills.values()}
            targets = {STOP_US} | {t for t in decisions|horizons|{READY_US+evaluation_policy['markout_horizon_us']} if now<t<=STOP_US}
            if arrival_cut is not None and now<arrival_cut<=STOP_US: targets.add(arrival_cut)
            target = min(targets)
            act('ADVANCE',delta_us=target-now)
            collect()
            samples[target] = venue_midpoint(handle.runtime)
            if target==arrival_cut: arrival_mid2=samples[target]
            if target in decisions:
                result = evaluate(READY_US if eligible else None)
                if submitted is not None and result['status']!='ELIGIBLE':
                    order = handle.ledger.orders['EX-PLAYER-1']
                    if order['unresolved'] and not order['pending_cancel']:
                        act('CANCEL',order_id='EX-PLAYER-1')
                        trace.append(dict(stage='CANCELLATION',at_us=target,reason=result['status']))
    except Exception as failure:
        status='FAILED'; error=f'{type(failure).__name__}: {failure}'[:1000]
    try:
        collect()
        if not handle.frozen: act('FREEZE',policy='PARTIAL')
        saved = library.save_execution_evidence(handle,root)
        measures = []
        for fill in fills.values():
            data = fill['data']; horizon = fill['simulation_time_us']+evaluation_policy['markout_horizon_us']
            mid2 = samples.get(horizon)
            reason = ('TRUNCATED_END_OF_RECORD' if horizon>frame['simulation_time_us'] else
                      'MISSING_MIDPOINT' if mid2 is None else 'AVAILABLE')
            measures.append(dict(event_id=fill['sequence'],trade_id=data['trade_id'],fill_time_us=fill['simulation_time_us'],
                horizon_us=horizon,horizon_mid_half_ticks=mid2,markout_status=reason,
                **fill_measurement('buy',data['quantity'],data['price_ticks'],decision_mid2,arrival_mid2,mid2,evaluation_policy['fee_milliticks_per_share'])))
        quantity = sum(m['quantity'] for m in measures)
        cost = sum(m['quantity']*m['price_ticks'] for m in measures)
        final_mid2 = venue_midpoint(handle.runtime)
        outcome = ('NO_OPPORTUNITY' if not eligible else 'NOT_SELECTED' if submitted is None else
                   'NO_FILL' if quantity==0 else 'PARTIAL_FILL' if quantity<policy['quantity'] else 'FILLED')
        trace.append(dict(stage='ASSESSMENT',outcome=outcome,settlement=saved['review']['settlement']))
        return dict(status=status,error=error,playbook_id=playbook_id,case=copy.deepcopy(case),branch_semantics=BRANCH,
                    actor='DECLARATIVE_PLAYBOOK_V1',decision_policy='ONE_IDEA_AT_READY_REVALIDATED_AFTER_DELAY; OBSERVE_EVERY_1000_US',
                    streams=handle.runtime.plan.seed_policy.as_dict(),trace=trace,execution_evidence_id=saved['evidence_id'],
                    checkpoint_sha256=saved['checkpoint_sha256'],review=saved['review'],outcome=outcome,
                    eligible_ideas=int(eligible),selected_ideas=int(submitted is not None),orders=int(submitted is not None),
                    fill_count=len(measures),filled_shares=quantity,fills=measures,
                    decision_mid_half_ticks=decision_mid2,arrival_mid_half_ticks=arrival_mid2,
                    candidate_markout_half_ticks=(None if not eligible or candidate_mid2 is None or samples.get(READY_US+evaluation_policy['markout_horizon_us']) is None
                        else samples[READY_US+evaluation_policy['markout_horizon_us']]-candidate_mid2),
                    candidate_markout_definition='VENUE_MID_AT_READY_PLUS_HORIZON_MINUS_DELIVERED_READY_MID; NOT_A_FILL',
                    realized_pnl_tick_shares=0,open_inventory_shares=quantity,
                    final_mid_half_ticks=final_mid2,
                    unrealized_half_tick_shares=None if final_mid2 is None else quantity*final_mid2-2*cost,
                    fees_millitick_shares=sum(m['fee_millitick_shares'] for m in measures),
                    notice='No exit policy. Midpoint markout/inventory marking is not realized P&L or an executable exit fill.')
    finally: practice.close_execution_practice(handle)


def scientific_projection(result):
    return {k:v for k,v in result.items() if k!='execution_evidence_id'}


def grouped_summary(cells):
    groups = {}
    for cell in cells:
        key = (cell['playbook_id'],cell['case']['recipe_id'])
        group = groups.setdefault(key,dict(playbook_id=key[0],recipe_id=key[1],planned_ideas=0,completed_ideas=0,
            failed=0,incomplete=0,eligible_ideas=0,selected_ideas=0,filled_ideas=0,unfilled_selected_ideas=0,
            orders=0,fills=0,filled_shares=0,markout_ideas_available=0,markout_half_tick_shares=0,
            eligible_markout_ideas_available=0,eligible_markout_half_ticks=0))
        group['planned_ideas']+=1
        result=cell.get('result')
        if cell['status']!='COMPLETE':
            group['failed' if cell['status']=='FAILED' else 'incomplete']+=1
            continue
        group['completed_ideas']+=1
        for field in ('eligible_ideas','selected_ideas','orders','filled_shares'): group[field]+=result[field]
        group['fills']+=result['fill_count']
        group['filled_ideas']+=int(result['filled_shares']>0)
        group['unfilled_selected_ideas']+=int(result['selected_ideas'] and result['filled_shares']==0)
        if result['candidate_markout_half_ticks'] is not None:
            group['eligible_markout_ideas_available']+=1
            group['eligible_markout_half_ticks']+=result['candidate_markout_half_ticks']
        if result['fills'] and all(m['markout_status']=='AVAILABLE' for m in result['fills']):
            group['markout_ideas_available']+=1
            group['markout_half_tick_shares']+=sum(m['markout_half_tick_shares'] for m in result['fills'])
    return [groups[key] for key in sorted(groups)]
