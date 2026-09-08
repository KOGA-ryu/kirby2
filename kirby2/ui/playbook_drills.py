"""Reviewed personal passages from verified, timed C4 instruction prefixes.

This is a separate schedule/restore extension, not C1's T=0 preparation V1.
"""
from __future__ import annotations
import copy

from kirby2.full_day.models import canonical_sha256
from kirby2.full_day.runtime import FullDayRuntime
from . import execution_practice as practice
from .execution_library import _load
from .execution_commitments import ExecutionRefusal, integer
from .execution_recipes import READY_US, STOP_US
from .playbooks import exact, observation
from . import playbook_store as store


def _operation(handle, action, payload):
    decision=practice._decision(handle);refusal=None
    try: practice._apply(handle,action,copy.deepcopy(payload))
    except ExecutionRefusal as error: refusal=dict(code=error.code,category=error.category,message=str(error))
    handle.operations.append(dict(action=action,payload=copy.deepcopy(payload),refusal=refusal,decision=decision))
    handle.revision+=1


def _prefix(record, cut_us):
    integer(cut_us,'decision cut',READY_US,min(STOP_US-1,record['runtime']['clock']['current_time_us']))
    handle=practice._Handle(practice._TOKEN,record['seed'],record['recipe_id'])
    for operation in record['operations']:
        now=handle.runtime.clock.current_time_us
        if now>cut_us or operation['action']=='FREEZE': break
        action,payload=operation['action'],operation['payload']
        if action=='ADVANCE' and operation['refusal'] is None and now+payload['delta_us']>cut_us:
            if now<cut_us: _operation(handle,'ADVANCE',dict(delta_us=cut_us-now))
            break
        _operation(handle,action,payload)
    if handle.runtime.clock.current_time_us!=cut_us: raise ValueError('source does not reconstruct this cut')
    if handle.held or handle.intent is not None or handle.ledger.unknown:
        raise ValueError('held, armed or unknown cuts are unsupported; choose a stable unarmed decision cut')
    return handle


def _objective(value):
    exact(value,('kind','text'),'objective')
    if value['kind'] not in {'EXACT_INSTRUCTION','DECLARED_RULE','OPEN_JUDGMENT'}: raise ValueError('unsupported objective kind')
    if type(value['text']) is not str or not 1<=len(value['text'])<=1000: raise ValueError('objective must be 1–1000 characters')
    return copy.deepcopy(value)


def describe_drill_source(evidence_id,root=None):
    record,_=_load(evidence_id,root)
    return dict(evidence_id=evidence_id,recipe_id=record['recipe_id'],seed=record['seed'],
                start_us=READY_US,end_us=record['runtime']['clock']['current_time_us'],
                instructions=[dict(action=o['action'],at_us=o['decision']['simulation_time_us'],refusal=o['refusal']) for o in record['operations']])


def _body(record,cut_us,lead_in_us,objective):
    integer(lead_in_us,'lead-in duration',1,cut_us-READY_US)
    handle=_prefix(record,cut_us)
    # Checkpoint markers belong to this audit copy. A live repetition rebuilds
    # the same timed prefix without injecting an unrecorded checkpoint operation
    # into the existing C4 bundle's instruction history.
    handle.runtime.capture_quiescent_cut()
    if not any(max(READY_US,cut_us-lead_in_us)<o['decision']['simulation_time_us']<cut_us and o['action'] not in {'ADVANCE','FREEZE'} and o['refusal'] is None for o in handle.operations):
        raise ValueError('generalized extraction requires a nonzero-time action in its supported prefix')
    start=cut_us-lead_in_us
    # Lead-in cuts are reconstructed independently; sampling never advances the
    # held decision owner or moves a recorded action to a different timestamp.
    cuts=sorted({start,cut_us} | {start+(cut_us-start)*i//4 for i in range(1,4)} |
                {o['decision']['simulation_time_us'] for o in handle.operations if start<=o['decision']['simulation_time_us']<=cut_us})
    if len(cuts)>64: raise ValueError('selected passage exceeds 64 observation cuts; narrow the lead-in')
    observations=[]
    for cut in cuts:
        at=_prefix(record,cut)
        observations.append(dict(cut_us=cut,observation=observation(practice._frame(at)),account=practice._knowledge(at)['account']))
    return dict(schema_id='KIRBY2_PERSONAL_PASSAGE_V1',parent_evidence_id=record['evidence_id'],
                case_id=store.lineage(record['recipe_id'],record['seed']),recipe_id=record['recipe_id'],seed=record['seed'],
                cut_us=cut_us,lead_in_us=lead_in_us,objective=_objective(objective),
                schedule=copy.deepcopy(handle.operations),checkpoint_sha256=canonical_sha256(handle.runtime.checkpoint_state()),
                observations=observations,grading='REVIEW_ONLY_NO_HIDDEN_ANSWER',
                preparation_contract='RECORDED_TIMED_EXECUTION_PREFIX_V1')


def create_drill_candidate(evidence_id,cut_us,lead_in_us,objective,root=None):
    record,_=_load(evidence_id,root)
    # Validate the cut before arithmetic or publication, including bool rejection.
    integer(cut_us,'decision cut',READY_US+1,min(STOP_US-1,record['runtime']['clock']['current_time_us']))
    body=_body(record,cut_us,lead_in_us,objective)
    store.reveal(body['case_id'],'INSPECTED',root)
    candidate=store.seal(body,'drill-')
    store.publish(candidate,root)
    return dict(candidate=candidate,reviewed=False)


def _verified(identifier,root):
    candidate=store.load(identifier,'drill-',root)
    record,_=_load(candidate['parent_evidence_id'],root)
    expected=store.seal(_body(record,candidate['cut_us'],candidate['lead_in_us'],candidate['objective']),'drill-')
    if candidate!=expected: raise ValueError('drill schedule, observations or dependencies differ from reconstruction')
    return candidate,record


def _review_record(identifier):
    return store.seal(dict(schema_id='KIRBY2_DRILL_RECIPE_REVIEW_V1',candidate_id=identifier,
                           decision='RECIPE_REVIEWED',grading='REVIEW_ONLY_NO_HIDDEN_ANSWER'),'drill-review-')


def open_drill_candidate(identifier,root=None):
    candidate,_=_verified(identifier,root)
    store.reveal(candidate['case_id'],'INSPECTED',root)
    receipt=_review_record(identifier)
    try:
        reviewed=store.load(receipt['artifact_id'],'drill-review-',root)==receipt
    except FileNotFoundError: reviewed=False
    return dict(candidate=candidate,reviewed=reviewed)


def review_drill_candidate(identifier,root=None):
    _verified(identifier,root)
    store.publish(_review_record(identifier),root)
    return open_drill_candidate(identifier,root)


def list_drill_candidates(root=None):
    entries=[];rejected=[]
    for identifier in store.scan('drill-',root):
        if identifier.startswith('drill-review-'): continue
        try:
            candidate=store.load(identifier,'drill-',root)
            entries.append(dict(candidate_id=identifier,cut_us=candidate['cut_us'],objective=candidate['objective']))
        except (ValueError,TypeError,KeyError,OSError): rejected.append(identifier)
    return dict(entries=entries,rejected=rejected)


def start_drill_candidate(identifier,root=None):
    opened=open_drill_candidate(identifier,root)
    if not opened['reviewed']: raise ValueError('review the frozen recipe before practicing this candidate')
    candidate,record=_verified(identifier,root)
    handle=_prefix(record,candidate['cut_us'])
    store.reveal(candidate['case_id'],'PRACTICED',root)
    with practice._LOCK:
        frame=practice._frame(handle)
        practice._ACTIVE[handle.source_id]=handle
    return handle,frame


def verify_drill_candidate(identifier,delta_us,root=None):
    candidate,record=_verified(identifier,root)
    delta=integer(delta_us,'continuation',1,STOP_US-candidate['cut_us'])
    rebuilt=_prefix(record,candidate['cut_us'])
    rebuilt.runtime.capture_quiescent_cut()
    restored=FullDayRuntime.from_checkpoint_state(rebuilt.runtime.checkpoint_state())
    target=candidate['cut_us']+delta
    for runtime in (rebuilt.runtime,restored):
        runtime.advance_to(target);runtime.capture_quiescent_cut()
    if rebuilt.runtime.canonical_state_bytes()!=restored.canonical_state_bytes(): raise ValueError('personal passage continuation differs')
    return dict(status='PASS',candidate_id=identifier,cut_us=candidate['cut_us'],after_us=target,
                state_sha256=canonical_sha256(restored.checkpoint_state()),
                nonzero_instruction_times=[o['decision']['simulation_time_us'] for o in candidate['schedule'] if o['action'] not in {'ADVANCE','FREEZE'}],
                preparation_contract=candidate['preparation_contract'])
