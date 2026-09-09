"""Versioned, untimed clinics over actual delivered C4 execution knowledge.

The runtime is reconstructed for each response and never retained by a client.
Prepared orders, fixed observation advances and learner instructions are distinct.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import json

from kirby2.curriculum.skills import SKILL_GRAPH_V1, require_stable_skill_v1
from kirby2.full_day.models import canonical_sha256
from . import execution_practice as execution
from .execution_recipes import READY_US
from .execution_commitments import ExecutionRefusal
from .playbooks import exact

SCHEMA = 'KIRBY2_CURRICULUM_CASE_V1'
RECIPE = 'execution.cancel-race.v1'
STRANDS = (
    ('control', 'Control accuracy', 'HOTKEY_ACCURACY'),
    ('observation', 'Observation interpretation', 'BOOK_READING'),
    ('selection', 'Selection and decline', 'SPREAD_DECISION'),
    ('timing', 'Timing and invalidation', 'LATENCY_AWARENESS'),
    ('pending', 'Pending exposure and recovery', 'PARTIAL_FILL_MANAGEMENT'),
    ('research', 'Research and review', 'SCRIPT_DISCIPLINE'),
)
SUPPORT = {'pending': ['POSITION_MANAGEMENT'], 'timing': ['CANCEL_TIMING']}
FORMS = ('ISOLATED', 'JOINED')
NOTICE = ('Untimed synthetic clinic. Fixed observation cuts; no reaction-time measurement. '
          'Prepared actions are not learner trades. Related cases share familiarity. '
          'This is not a real-market or validated learning claim.')


def catalog():
    return [dict(recipe_id=key, title=title, skill_id=require_stable_skill_v1(skill),
                 supporting_skills=SUPPORT.get(key, []), forms=list(FORMS),
                 prerequisites={s: list(SKILL_GRAPH_V1.prerequisites(s))
                                for s in [skill] + SUPPORT.get(key, [])},
                 prerequisite_readiness='NOT_INFERRED', pace='UNTIMED')
            for key, title, skill in STRANDS]


def specification(strand, seed, form='ISOLATED'):
    if strand not in {r[0] for r in STRANDS} or form not in FORMS:
        raise ValueError('unsupported curriculum recipe or form')
    if type(seed) is not int or not 0 <= seed < 2**31:
        raise ValueError('invalid curriculum seed')
    return dict(schema_id=SCHEMA, strand=strand, seed=seed, form=form,
                engine_recipe=RECIPE, quantity=400 + 100 * (seed % 3),
                preparation_offset_us=100 * (seed % 3),
                selection_condition=('SPREAD', 'NO_ENTRY', 'MISSING_VOLUME')[(seed // 3) % 3],
                lead_in='FULL_RECORDED_PRECURSOR_V1', pace='UNTIMED')


def validate_spec(spec):
    if type(spec) is not dict or spec != specification(spec.get('strand'), spec.get('seed'), spec.get('form')):
        raise ValueError('case recipe, timing or required lead-in differs')
    return spec


def group_id(spec):
    validate_spec(spec)
    # No title, symbol, form or tested skill can reset exposure to the same world.
    return 'curriculum-group-' + canonical_sha256(dict(recipe=RECIPE, seed=spec['seed']))


def _call(handle, action, payload, actor, trace):
    before = execution._decision(handle)
    refusal = None
    try:
        execution._apply(handle, action, copy.deepcopy(payload))
    except ExecutionRefusal as error:
        refusal = dict(code=error.code, category=error.category, message=str(error))
    handle.operations.append(dict(action=action, payload=copy.deepcopy(payload),
                                  refusal=refusal, decision=before))
    trace.append(dict(action=action, payload=copy.deepcopy(payload), actor=actor,
                      before=before, refusal=refusal, after=execution._knowledge(handle)))
    return refusal


def _reading(handle):
    k = execution._knowledge(handle)
    market = k['market']
    if not market or not market.get('bid_levels') or not market.get('ask_levels'):
        return None
    return market['ask_levels'][0]['price_ticks'] - market['bid_levels'][0]['price_ticks']


def _question(spec, step, handle):
    strand = spec['strand']
    knowledge = execution._knowledge(handle)
    account = knowledge['account']
    qty = spec['quantity']
    submit = dict(action='SUBMIT', payload=dict(side='buy', quantity=qty, price_ticks=10000))
    # Questions are graded only against already-delivered facts and the declared
    # instruction. No private engine state or future event enters this function.
    position = dict(confirmed=account['confirmed_position'], possible=account['possible_position'])
    spread = _reading(handle)
    selection = ('INSUFFICIENT' if spec['selection_condition']=='MISSING_VOLUME' or spread is None or knowledge['market_status']!='CURRENT'
                 else 'DECLINE' if spec['selection_condition']=='NO_ENTRY' or spread > 2 else 'ENTER')
    selection_text = 'Use current delivered quotes; stale or missing data is insufficient. ' + {'SPREAD': 'Enter only if the delivered spread is at most 2 ticks.',
                      'NO_ENTRY': 'Enter only if the delivered spread is at most 0 ticks.',
                      'MISSING_VOLUME': 'Enter only with a complete public tape showing at least 100 executed shares. This quote feed has no complete tape.'}[spec['selection_condition']]
    questions = {
        'control': [(f'Submit a buy limit for {qty} shares at 10000 ticks. A stale quote makes this opportunity unscored.', submit, 'AVAILABLE' if knowledge['market_status']=='CURRENT' else 'AMBIGUOUS'),
                    ('After the declared delivery interval, report confirmed and possible position.', position, 'AVAILABLE')],
        'observation': [('Report the spread in integer ticks, or INSUFFICIENT when the delivered book is unavailable.', spread if spread is not None else 'INSUFFICIENT', 'AVAILABLE' if spread is not None else 'AMBIGUOUS'),
                        (selection_text + ' Answer ENTER, DECLINE or INSUFFICIENT.', selection,
                         'AMBIGUOUS' if selection=='INSUFFICIENT' else 'NO_OPPORTUNITY' if selection=='DECLINE' else 'AVAILABLE')],
        'selection': [(selection_text + ' Answer ENTER, DECLINE or INSUFFICIENT.', selection,
                       'AMBIGUOUS' if selection=='INSUFFICIENT' else 'NO_OPPORTUNITY' if selection=='DECLINE' else 'AVAILABLE'),
                      ('Carry out the declared decision: submit the specified buy, or NO_ACTION. Quantity '+str(qty)+', limit 10000.',
                       submit if selection=='ENTER' else dict(action='NO_ACTION', payload={}),
                       'AVAILABLE' if selection=='ENTER' else 'NO_OPPORTUNITY')],
        'timing': [('A prepared cancel was just sent. Report confirmed and possible position before its reply.', position, 'AVAILABLE'),
                   ('After the fixed reply interval, report confirmed and possible position again.', position, 'AVAILABLE')],
        'pending': [('A prepared order has partially filled. Report confirmed and possible position.', position, 'AVAILABLE'),
                    ('Cancel the remaining commitment on EX-PLAYER-1.', dict(action='CANCEL', payload=dict(order_id='EX-PLAYER-1')), 'AVAILABLE'),
                    ('After the fixed reply interval, report confirmed and possible position.', position, 'AVAILABLE')],
        'research': [('Can this delivered quote alone establish complete executed volume? Answer AVAILABLE or UNAVAILABLE.', 'UNAVAILABLE', 'AVAILABLE'),
                     ('Report the number of prepared order submissions in this precursor; do not call them learner trades.',
                      1, 'AVAILABLE')],
    }
    text, answer, opportunity = questions[strand][step]
    skill_id={'control':['HOTKEY_ACCURACY','POSITION_MANAGEMENT'],
              'observation':['BOOK_READING','SPREAD_DECISION'],
              'selection':['SPREAD_DECISION','HOTKEY_ACCURACY'],
              'timing':['LATENCY_AWARENESS','CANCEL_TIMING'],
              'pending':['PARTIAL_FILL_MANAGEMENT','HOTKEY_ACCURACY','POSITION_MANAGEMENT'],
              'research':['SCRIPT_DISCIPLINE','SCRIPT_DISCIPLINE']}[strand][step]
    return dict(text=text, expected=answer, opportunity=opportunity, skill_id=skill_id,
                kind='INSTRUCTION' if type(answer) is dict and 'action' in answer else 'INTERPRETATION',
                answer_format='INSTRUCTION' if type(answer) is dict and 'action' in answer else 'POSITION' if type(answer) is dict else 'INTEGER' if type(answer) is int else 'CHOICE',
                choices=['ENTER','DECLINE','INSUFFICIENT'] if strand in {'observation','selection'} else ['AVAILABLE','UNAVAILABLE'])


def _count(spec):
    return 1 if spec['form']=='ISOLATED' else 3 if spec['strand']=='pending' else 2


def _instruction(value):
    exact(value, ('action', 'payload'), 'learner instruction')
    if value['action'] not in {'SUBMIT', 'CANCEL', 'NO_ACTION'} or type(value['payload']) is not dict:
        raise ValueError('unsupported clinic instruction')
    if value['action']=='NO_ACTION' and value['payload']:
        raise ValueError('NO_ACTION has no payload')


def simulate(spec, responses=()):
    validate_spec(spec)
    if len(json.dumps(list(responses),allow_nan=False))>16384:
        raise ValueError('clinic responses exceed the bounded input size')
    # Cache only immutable scientific input, never operational authority.
    return copy.deepcopy(_simulate(json.dumps(spec, sort_keys=True), json.dumps(list(responses), sort_keys=True)))


@lru_cache(maxsize=256)
def _simulate(spec_json, responses_json):
    spec, responses = json.loads(spec_json), json.loads(responses_json)
    if len(responses) > _count(spec):
        raise ValueError('too many clinic responses')
    handle = execution._Handle(execution._TOKEN, spec['seed'], RECIPE)
    trace = []
    lead = [dict(at_us=READY_US, knowledge=execution._knowledge(handle), actor='PREPARATION')]
    offset = spec['preparation_offset_us']
    if offset:
        _call(handle, 'ADVANCE', dict(delta_us=offset), 'PREPARATION', trace)
    if spec['strand'] in {'pending', 'timing', 'research'}:
        _call(handle, 'SUBMIT', dict(side='buy', quantity=spec['quantity'], price_ticks=10000), 'PREPARATION', trace)
        lead.append(dict(at_us=handle.runtime.clock.current_time_us, knowledge=execution._knowledge(handle), actor='PREPARATION'))
        _call(handle, 'ADVANCE', dict(delta_us=3200), 'FIXED_OBSERVATION', trace)
        lead.append(dict(at_us=handle.runtime.clock.current_time_us, knowledge=execution._knowledge(handle), actor='PREPARATION'))
        if spec['strand']=='timing':
            _call(handle, 'CANCEL', dict(order_id='EX-PLAYER-1'), 'PREPARATION', trace)
            lead.append(dict(at_us=handle.runtime.clock.current_time_us, knowledge=execution._knowledge(handle), actor='PREPARATION'))
    results = []
    for step, response in enumerate(responses):
        exact(response, ('value', 'actor'), 'response')
        if response['actor'] not in {'HUMAN_DECLARED', 'SCRIPT'}:
            raise ValueError('invalid action provenance')
        question = _question(spec, step, handle)
        before = execution._knowledge(handle)
        refusal = None
        if question['kind']=='INSTRUCTION':
            _instruction(response['value'])
            action = response['value']['action']
            if action != 'NO_ACTION':
                refusal = _call(handle, action, response['value']['payload'], response['actor'], trace)
        correct = canonical_sha256(response['value'])==canonical_sha256(question['expected']) and refusal is None
        results.append(dict(step=step, question=question, response=response, correct=correct,
                            opportunity=question['opportunity'], before=before, refusal=refusal))
        # A recipe-bound interval, never one chosen after seeing an outcome.
        if spec['strand'] in {'control', 'timing'} or (spec['strand']=='pending' and step==1) or (spec['strand']=='selection' and step==1):
            _call(handle, 'ADVANCE', dict(delta_us=4000), 'FIXED_OBSERVATION', trace)
    done = len(responses)==_count(spec)
    question = None if done else _question(spec, len(responses), handle)
    body = dict(specification=spec, group_id=group_id(spec), lead_in=lead, trace=trace,
                knowledge=execution._knowledge(handle), at_us=handle.runtime.clock.current_time_us,
                step=len(responses), step_count=_count(spec), results=results, question=question,
                done=done, pace='UNTIMED', human_reaction_us=None, notice=NOTICE)
    body['scientific_sha256'] = canonical_sha256(body)
    return body


def public_case(result, *, hints=False, closed=False):
    value = copy.deepcopy(result)
    if not closed:
        value.pop('results')
        if value['question'] and not hints:
            value['question'].pop('expected')
            value['question'].pop('opportunity')
        # Internal science digest includes the answer. It is never exposed as a
        # pre-answer oracle; bind a separate public projection instead.
    value.pop('scientific_sha256')
    value['projection_sha256'] = canonical_sha256(value)
    return value
