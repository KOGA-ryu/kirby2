"""Durable, source-linked learning paths; no mastery or profitability inference."""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from collections import Counter

from kirby2.full_day.models import canonical_sha256
from . import curriculum_cases as cases
from . import curriculum_store as ledger
from . import playbook_store as evidence
from .playbooks import exact, template

POLICY = 'KIRBY2_CLINIC_SELECTION_V1'
STATUS = 'UNVALIDATED_FOR_LEARNING_OUTCOMES'
PHASES = ('BASELINE', 'PRACTICE', 'FRESH', 'RETENTION', 'COMPLETE')
RETENTION_SECONDS = 86400


def _now():
    return int(time.time())


def _seed(identifier, phase, strand, ordinal=0):
    return int(canonical_sha256([identifier, phase, strand, ordinal])[:8], 16) % (2**31)


def _plan(identifier):
    if type(identifier) is not str or re.fullmatch(r'[0-9a-f]{32}',identifier) is None:
        raise ValueError('invalid protocol identity')
    bank = {phase: [cases.specification(row['recipe_id'],
                    (_seed(identifier,phase,row['recipe_id']) // 9) * 9 +
                    (3 if row['recipe_id']=='observation' else 0) + phase_index)
                    for row in cases.catalog()]
            for phase_index,phase in enumerate(('BASELINE','FRESH','RETENTION'))}
    seeds = [s['seed'] for rows in bank.values() for s in rows]
    if len(set(seeds))!=18:
        raise ValueError('assessment population collision; prepare another protocol')
    return dict(protocol_id=identifier, policy=POLICY, model_status=STATUS, bank=bank,
                skill_catalog=cases.catalog(), sampling='FOCUSED_EQUAL_STRAND_OVERSAMPLING',
                assessment_condition='UNTIMED_NO_HINTS_UNFAMILIAR_CASE_WITHIN_KNOWN_RECIPE',
                retention_delay_seconds=RETENTION_SECONDS,
                metrics=['correct/opportunities by skill and condition', 'no opportunity', 'ambiguity',
                         'abort', 'system failure', 'assistance', 'familiarity', 'action provenance'],
                reaction_time='UNAVAILABLE', human_learning='NOT_EXERCISED',
                comparison='DESCRIPTIVE_SINGLE_LEARNER_NOT_CAUSAL_OR_REAL_MARKET_TRANSFER')


def _state(events):
    state = dict(plan=None, phase='BASELINE', sessions=[], active=None, retention_due=None)
    for event in events:
        kind, p = event['kind'], event['payload']
        active = state['active']
        if kind=='PROTOCOL':
            if state['plan'] is not None or p!=_plan(p.get('protocol_id')):
                raise ValueError('protocol is not the frozen supported population')
            state['plan']=p
            continue
        if state['plan'] is None:
            raise ValueError('curriculum event precedes its protocol')
        if kind=='BEGIN':
            exact(p, ('attempt_id','specification','phase','mode','familiarity','reason','prior_ledger','kind','initial_scientific_sha256'), 'enrollment')
            if active or len(state['sessions'])>=64 or p['kind'] not in {'CLINIC','WORKFLOW'}:
                raise ValueError('overlapping or excessive curriculum attempts')
            if p['prior_ledger']!=event['parent'] or p['phase'] not in PHASES[:-1] or p['mode'] not in {'GUIDED','UNASSISTED'}:
                raise ValueError('enrollment conditions differ')
            if p['familiarity'] not in {'REHEARSAL','UNFAMILIAR_CASE'} or type(p['reason']) is not str:
                raise ValueError('invalid enrollment familiarity or reason')
            if type(p['attempt_id']) is not str or len(p['attempt_id'])!=32 or any(s['attempt_id']==p['attempt_id'] for s in state['sessions']):
                raise ValueError('invalid or repeated attempt identity')
            cases.validate_spec(p['specification'])
            expected_initial=cases.simulate(p['specification'])['scientific_sha256'] if p['kind']=='CLINIC' else None
            if p['initial_scientific_sha256']!=expected_initial:
                raise ValueError('frozen case source differs from its enrollment')
            previously_seen=any(cases.group_id(s['specification'])==cases.group_id(p['specification']) for s in state['sessions'])
            if p['familiarity']=='UNFAMILIAR_CASE' and (p['phase']=='PRACTICE' or previously_seen):
                raise ValueError('rehearsal cannot become unfamiliar')
            if p['phase']!='PRACTICE':
                if p['phase']!=state['phase'] or p['specification'] not in state['plan']['bank'][p['phase']] or p['mode']!='UNASSISTED' or p['kind']!='CLINIC':
                    raise ValueError('assessment population or assistance differs')
                if any(s['phase']==p['phase'] and s['specification']==p['specification'] for s in state['sessions']):
                    raise ValueError('reserved assessment was already enrolled')
                if p['phase']=='RETENTION' and (state['retention_due'] is None or event['recorded_at']<state['retention_due']):
                    raise ValueError('retention was started too early')
            active = dict(p, responses=[], hints=[], status='ACTIVE', started_at=event['recorded_at'],
                          closed_at=None, world_id=None, evidence_id=None, terminal=None)
            state['sessions'].append(active); state['active']=active
        elif kind=='PHASE':
            exact(p, ('phase',), 'phase')
            next_phase = p['phase']
            if active or PHASES.index(next_phase)!=PHASES.index(state['phase'])+1:
                raise ValueError('invalid curriculum phase transition')
            prior = [s for s in state['sessions'] if s['phase']==state['phase']]
            if state['phase']=='PRACTICE':
                if not any(s['status']=='COMPLETED' and s['kind']=='CLINIC' for s in prior):
                    raise ValueError('complete targeted practice before the fresh assessment')
            elif len(prior)!=6 or any(s['status']=='ACTIVE' for s in prior):
                raise ValueError('account for all six reserved cases before advancing')
            state['phase']=next_phase
            if next_phase=='RETENTION':
                state['retention_due']=event['recorded_at']+RETENTION_SECONDS
        else:
            if active is None or p.get('attempt_id')!=active['attempt_id']:
                raise ValueError('event belongs to no active curriculum attempt')
            if kind=='HINT':
                exact(p, ('attempt_id','step'), 'hint')
                if active['phase']!='PRACTICE' or active['kind']!='CLINIC' or p['step']!=len(active['responses']):
                    raise ValueError('assessment hints or stale step refused')
                active['hints'].append(p['step'])
            elif kind=='ANSWER':
                exact(p, ('attempt_id','step','response','scientific_sha256'), 'answer')
                if active['kind']!='CLINIC' or type(p['step']) is not int or p['step']!=len(active['responses']):
                    raise ValueError('answer belongs to another step')
                responses=active['responses']+[p['response']]
                result=cases.simulate(active['specification'],responses)
                if p['scientific_sha256']!=result['scientific_sha256']:
                    raise ValueError('recorded response source or assessment differs from reconstruction')
                active['responses']=responses
                if result['done']:
                    active['status']='COMPLETED';active['closed_at']=event['recorded_at']
                    active['terminal']=event['event_id'];state['active']=None
            elif kind=='BIND':
                exact(p, ('attempt_id','world_id'), 'world binding')
                if active['kind']!='WORKFLOW' or active['world_id'] is not None or type(p['world_id']) is not str or not p['world_id'].startswith('world-'):
                    raise ValueError('invalid workflow binding')
                active['world_id']=p['world_id']
            elif kind=='ATTACH':
                exact(p, ('attempt_id','evidence_id','world_id'), 'world evidence')
                if active['kind']!='WORKFLOW' or active['world_id']!=p['world_id'] or active['world_id'] is None:
                    raise ValueError('world evidence has different authority')
                evidence.identity(p['evidence_id'],'radar-evidence-')
                active['evidence_id']=p['evidence_id'];active['status']='REVIEW_REQUIRED'
                active['closed_at']=event['recorded_at'];active['terminal']=event['event_id'];state['active']=None
            elif kind=='END':
                exact(p, ('attempt_id','status'), 'ending')
                if p['status'] not in {'ABORTED','SYSTEM_FAILURE'}:
                    raise ValueError('unsupported attempt termination')
                active['status']=p['status'];active['closed_at']=event['recorded_at']
                active['terminal']=event['event_id'];state['active']=None
            else:
                raise ValueError('unsupported curriculum event')
    return state


def _append(path, events, kind, payload):
    now=_now()
    # Validate proposed semantics before a single immutable publication.
    draft=dict(kind=kind,payload=payload,recorded_at=now,parent=events[-1]['event_id'] if events else None,event_id='PENDING')
    _state(events+[draft])
    ledger.append(path,events,kind,payload,now)
    return _state(events)


def _exposed(spec, state, root):
    group=cases.group_id(spec)
    if any(cases.group_id(s['specification'])==group for s in state['sessions']):
        return True
    # Earlier C5 practice/reveal of the same engine world also contaminates it.
    return evidence.lineage(cases.RECIPE,spec['seed']) in evidence.reveals(root)


def _summary(session):
    if session['kind']=='WORKFLOW':
        return dict(attempt_id=session['attempt_id'],skill_id='WORKFLOW_REVIEW_ONLY',status=session['status'],
                    phase=session['phase'],kind='WORKFLOW',evidence_id=session['evidence_id'],
                    correct=0,opportunities=0,condition='RADAR_ORIGINAL_ASSISTANCE_AND_ACTION_PROVENANCE_REQUIRE_REVIEW')
    result=cases.simulate(session['specification'],session['responses'])
    counts=Counter(r['opportunity'] for r in result['results'])
    manual=[r for r in result['results'] if r['response']['actor']=='HUMAN_DECLARED']
    primary=next(r[2] for r in cases.STRANDS if r[0]==session['specification']['strand'])
    opportunities=[r for r in manual if r['opportunity']=='AVAILABLE' and r['question']['skill_id']==primary]
    skill_evidence=[]
    for skill in sorted({r['question']['skill_id'] for r in result['results']}):
        eligible=[r for r in manual if r['question']['skill_id']==skill and r['opportunity']=='AVAILABLE']
        skill_evidence.append(dict(skill_id=skill,correct=sum(r['correct'] for r in eligible),opportunities=len(eligible)))
    skill=next(r[2] for r in cases.STRANDS if r[0]==session['specification']['strand'])
    return dict(attempt_id=session['attempt_id'],skill_id=skill,status=session['status'],phase=session['phase'],
                kind='CLINIC',evidence_id=session['terminal'],form=session['specification']['form'],
                correct=sum(r['correct'] for r in opportunities),opportunities=len(opportunities),skill_evidence=skill_evidence,
                decision_correct=sum(r['correct'] for r in manual),decisions=len(manual),
                no_opportunity=counts['NO_OPPORTUNITY'],ambiguous=counts['AMBIGUOUS'],
                scripted=sum(r['response']['actor']=='SCRIPT' for r in result['results']),
                assistance=len(session['hints']),mode=session['mode'],familiarity=session['familiarity'],
                pace='UNTIMED',reaction_time_us=None,
                uncertainty='DESCRIPTIVE_SMALL_SAMPLE_NO_MASTERY_ESTIMATE')


def _ranking(state):
    rows=[]
    for definition in cases.catalog():
        history=[_summary(s) for s in state['sessions'] if s['kind']=='CLINIC' and s['specification']['strand']==definition['recipe_id'] and s['status']=='COMPLETED']
        recent=history[-3:]
        misses=sum(r['opportunities']-r['correct'] for r in recent)
        opportunities=sum(r['opportunities'] for r in history)
        last=max((i for i,s in enumerate(state['sessions']) if s['specification']['strand']==definition['recipe_id']),default=-1)
        rows.append(dict(recipe_id=definition['recipe_id'],skill_id=definition['skill_id'],
                         recent_misses=misses,opportunities=opportunities,last_attempt_index=last,
                         reason='RECENT_MISS' if misses else 'INSUFFICIENT_SAMPLE' if opportunities<3 else 'LEAST_RECENT'))
    rows.sort(key=lambda r:(-r['recent_misses'],r['opportunities']>=3,r['last_attempt_index'],r['recipe_id']))
    return rows


def _projection_body(state,events,root):
    active=state['active']
    rows=[_summary(s) for s in state['sessions']]
    groups={}
    for row in rows:
        if row['kind']!='CLINIC':continue
        key=tuple(row[k] for k in ('skill_id','phase','form','mode','familiarity','pace'))+(bool(row['assistance']), bool(row['scripted']))
        group=groups.setdefault(key,dict(conditions=list(key),attempts=0,completed=0,correct=0,opportunities=0,
                                          no_opportunity=0,ambiguous=0,aborted=0,system_failure=0,scripted=0))
        group['attempts']+=1
        if row['status']=='COMPLETED':
            group['completed']+=1
            for field in ('correct','opportunities'):group[field]+=row[field]
        group['no_opportunity']+=row['no_opportunity'];group['ambiguous']+=row['ambiguous'];group['scripted']+=row['scripted']
        group['aborted']+=row['status']=='ABORTED';group['system_failure']+=row['status']=='SYSTEM_FAILURE'
    for g in groups.values():
        g['accuracy']=None if not g['opportunities'] else dict(numerator=g['correct'],denominator=g['opportunities'])
        g['uncertainty']='INSUFFICIENT_FOR_LEARNING_OR_MASTERY_CLAIM'
    public_active=None
    if active:
        public_active={k:copy.deepcopy(active[k]) for k in ('attempt_id','kind','phase','mode','familiarity','status','world_id')}
        if active['kind']=='CLINIC':
            r=cases.simulate(active['specification'],active['responses'])
            public_active['case']=cases.public_case(r,hints=len(active['responses']) in active['hints'])
    phase=state['phase']
    available=[]
    if state['plan'] and phase in state['plan']['bank']:
        for index,spec in enumerate(state['plan']['bank'][phase]):
            if not any(s['phase']==phase and s['specification']==spec for s in state['sessions']):
                available.append(dict(slot=index,skill_id=cases.catalog()[index]['skill_id'],
                                      familiarity='REHEARSAL' if _exposed(spec,state,root) else 'UNFAMILIAR_CASE'))
    return dict(schema_id='KIRBY2_CURRICULUM_DASHBOARD_V1',policy=POLICY,model_status=STATUS,
                revision=events[-1]['event_id'] if events else None,protocol_id=state['plan']['protocol_id'] if state['plan'] else None,
                phase=phase,catalog=cases.catalog(),active=public_active,history=rows,progress=list(groups.values()),
                ranking=_ranking(state),assessment_slots=available,retention_due=state['retention_due'],
                retention_available=state['retention_due'] is not None and _now()>=state['retention_due'] and (not events or _now()>=events[-1]['recorded_at']),
                human_learning='NOT_EXERCISED' if not any(r.get('decisions',0) for r in rows) else 'DECLARED_RESPONSES_ONLY_NO_VALIDATED_BENEFIT',
                notice=cases.NOTICE)


def _projection(state,events,root):
    body=_projection_body(state,events,root)
    return dict(body,projection_sha256=canonical_sha256(body))


def dashboard(root=None):
    with ledger.locked(root) as path:
        events=ledger.read(path);state=_state(events)
        return _projection(state,events,root)


def prepare_protocol(root=None):
    with ledger.locked(root) as path:
        events=ledger.read(path)
        if not events:
            _append(path,events,'PROTOCOL',_plan(uuid.uuid4().hex))
        return _projection(_state(events),events,root)


def _current(path,revision):
    events=ledger.read(path);state=_state(events)
    if not events or events[-1]['event_id']!=revision:
        raise ValueError('stale curriculum view; refresh before changing it')
    if _now()<events[-1]['recorded_at']:
        raise ValueError('local clock moved backwards; no enrollment or reveal will be written')
    return events,state


def begin(revision,choice,root=None):
    exact(choice,('phase','recipe_id','form','mode','repeat_attempt','kind'),'practice choice')
    with ledger.locked(root) as path:
        events,state=_current(path,revision)
        if state['active'] or len(state['sessions'])>=64 or len(events)>ledger.MAX_EVENTS-12:
            raise ValueError('finish the active attempt or the bounded protocol first')
        phase=choice['phase'];kind=choice['kind']
        if phase=='PRACTICE':
            if choice['repeat_attempt']:
                prior=next(s for s in state['sessions'] if s['attempt_id']==choice['repeat_attempt'])
                if prior['kind']!=kind:
                    raise ValueError('an exact repeat cannot change clinic versus world meaning')
                spec=copy.deepcopy(prior['specification']);reason='EXACT_REPEAT_REHEARSAL'
            else:
                strand=choice['recipe_id'] or _ranking(state)[0]['recipe_id']
                reserved={s['seed'] for rows in state['plan']['bank'].values() for s in rows}
                seed=_seed(state['plan']['protocol_id'],'PRACTICE',strand,len(state['sessions']))
                while seed in reserved:seed=(seed+1)%(2**31)
                spec=cases.specification(strand,seed,choice['form'])
                reason='MANUAL_CHOICE' if choice['recipe_id'] else _ranking(state)[0]['reason']
            familiarity='REHEARSAL';mode=choice['mode']
        else:
            if phase!=state['phase'] or phase not in state['plan']['bank'] or choice['repeat_attempt'] or kind!='CLINIC':
                raise ValueError('assessment does not match the current frozen phase')
            remaining=[s for s in state['plan']['bank'][phase] if not any(a['phase']==phase and a['specification']==s for a in state['sessions'])]
            if not remaining:raise ValueError('assessment population complete; advance the protocol')
            spec=remaining[0];reason='FROZEN_ASSESSMENT_ORDER';mode='UNASSISTED'
            familiarity='REHEARSAL' if _exposed(spec,state,root) else 'UNFAMILIAR_CASE'
        # Build the case before publishing enrollment. No failure invents a started run.
        if kind=='CLINIC':cases.simulate(spec)
        payload=dict(attempt_id=uuid.uuid4().hex,specification=spec,phase=phase,mode=mode,
                     familiarity=familiarity,reason=reason,prior_ledger=events[-1]['event_id'],kind=kind,
                     initial_scientific_sha256=cases.simulate(spec)['scientific_sha256'] if kind=='CLINIC' else None)
        _state(events+[dict(kind='BEGIN',payload=payload,recorded_at=_now(),
                            parent=events[-1]['event_id'],event_id='PENDING')])
        evidence.reveal(evidence.lineage(cases.RECIPE,spec['seed']),'PRACTICED',root)
        _append(path,events,'BEGIN',payload)
        # Enrollment itself is a durable reveal, including an abandoned attempt.
        return _projection(_state(events),events,root)


def answer(revision,attempt_id,step,value,actor='SCRIPT',root=None):
    with ledger.locked(root) as path:
        events,state=_current(path,revision)
        active=state['active']
        if not active or active['attempt_id']!=attempt_id or active['kind']!='CLINIC' or type(step) is not int or step!=len(active['responses']):
            raise ValueError('answer does not belong to the active clinic step')
        response=dict(value=value,actor=actor)
        result=cases.simulate(active['specification'],active['responses']+[response])
        p=dict(attempt_id=attempt_id,step=step,response=response,scientific_sha256=result['scientific_sha256'])
        _append(path,events,'ANSWER',p)
        return _projection(_state(events),events,root)


def hint(revision,attempt_id,step,root=None):
    with ledger.locked(root) as path:
        events,state=_current(path,revision)
        _append(path,events,'HINT',dict(attempt_id=attempt_id,step=step))
        return _projection(_state(events),events,root)


def end(revision,attempt_id,status='ABORTED',root=None):
    with ledger.locked(root) as path:
        events,state=_current(path,revision)
        _append(path,events,'END',dict(attempt_id=attempt_id,status=status))
        return _projection(_state(events),events,root)


def advance_phase(revision,root=None):
    with ledger.locked(root) as path:
        events,state=_current(path,revision)
        index=PHASES.index(state['phase'])
        if index==len(PHASES)-1:raise ValueError('protocol complete')
        _append(path,events,'PHASE',dict(phase=PHASES[index+1]))
        return _projection(_state(events),events,root)


def open_attempt(attempt_id,root=None):
    with ledger.locked(root) as path:
        events=ledger.read(path);state=_state(events)
        session=next(s for s in state['sessions'] if s['attempt_id']==attempt_id)
        if session['status']=='ACTIVE':raise ValueError('finish or abort before opening the answer-bearing review')
        original=[e for e in events if e['payload'].get('attempt_id')==attempt_id]
        result=dict(enrollment=copy.deepcopy(session),events=original,summary=_summary(session),
                    human_reaction_us=None,learning_claim=STATUS)
        if session['kind']=='CLINIC':
            result['case']=cases.public_case(cases.simulate(session['specification'],session['responses']),closed=True)
        elif session['evidence_id']:
            from .radar_evidence import verify_record
            record=evidence.load(session['evidence_id'],'radar-evidence-',root)
            world=verify_record(record)
            if not world.frozen or record['attempt_id']!=session['world_id'] or record['seed']!=session['specification']['seed'] or record['mode']!='CONTINUOUS' or record['source']!=json.dumps(template()):
                raise ValueError('original world does not match its curriculum enrollment')
            result['world']=dict(evidence_id=record['artifact_id'],review=record['review'])
        return result


def workflow_launch(attempt_id,root=None):
    with ledger.locked(root) as path:
        state=_state(ledger.read(path));active=state['active']
        if not active or active['attempt_id']!=attempt_id or active['kind']!='WORKFLOW' or active['world_id']:
            raise ValueError('workflow is not ready for its one world')
        return dict(seed=active['specification']['seed'],source=json.dumps(template()),mode='CONTINUOUS')


def bind_workflow(revision,attempt_id,world_id,root=None):
    with ledger.locked(root) as path:
        events,state=_current(path,revision)
        _append(path,events,'BIND',dict(attempt_id=attempt_id,world_id=world_id))
        return _projection(_state(events),events,root)


def attach_workflow(revision,attempt_id,evidence_id,root=None):
    from .radar_evidence import verify_record
    record=evidence.load(evidence_id,'radar-evidence-',root)
    world=verify_record(record)
    if not world.frozen:raise ValueError('workflow review needs frozen world evidence')
    with ledger.locked(root) as path:
        events,state=_current(path,revision);active=state['active']
        if not active or active['attempt_id']!=attempt_id or record['seed']!=active['specification']['seed'] or record['source']!=json.dumps(template()) or record['mode']!='CONTINUOUS':
            raise ValueError('saved world differs from the predeclared workflow')
        _append(path,events,'ATTACH',dict(attempt_id=attempt_id,evidence_id=evidence_id,world_id=record['attempt_id']))
        return _projection(_state(events),events,root)
