"""Bounded, durable Lab jobs. One cell at a time; no cluster or optimizer."""
from __future__ import annotations
import copy
import tempfile
import threading
import uuid
from pathlib import Path

from kirby2.full_day.models import canonical_sha256
from .playbooks import _parse, exact
from .execution_recipes import RECIPES
from .execution_commitments import integer
from . import playbook_store as store
from .playbook_trials import run_trial, scientific_projection, grouped_summary, BRANCH
from .execution_library import open_execution_evidence

SCHEMA = 'KIRBY2_LAB_STUDY_V1'
_LOCK = threading.RLock()
_ACTIVE = {}
_TOKEN = object()


def _spec(spec):
    exact(spec,('title','variants','cases'),'study')
    if type(spec['title']) is not str or not 1<=len(spec['title'])<=120: raise ValueError('study title must be 1–120 characters')
    if type(spec['variants']) is not list or not 1<=len(spec['variants'])<=4: raise ValueError('select 1–4 frozen variants')
    variants=[]
    for source in spec['variants']:
        _,_,identifier=_parse(source)
        if any(v['playbook_id']==identifier for v in variants): raise ValueError('duplicate semantic variant')
        variants.append(dict(playbook_id=identifier,source=source))
    if type(spec['cases']) is not list or not 1<=len(spec['cases'])<=8: raise ValueError('select 1–8 explicit cases')
    cases=[]
    for case in spec['cases']:
        exact(case,('recipe_id','seed','role'),'case')
        if case['recipe_id'] not in RECIPES or case['role'] not in {'PRACTICE','RESERVED'}: raise ValueError('unsupported case or role')
        integer(case['seed'],'case seed',0,2**31-1)
        lineage=store.lineage(case['recipe_id'],case['seed'])
        if any(c['case_id']==lineage for c in cases): raise ValueError('one episode lineage cannot occupy two case/split positions')
        cases.append(dict(case,case_id=lineage))
    if len(cases)*len(variants)>16: raise ValueError('study exceeds 16 planned cells')
    cells=[]
    for variant in variants:
        for case in cases:
            body=dict(playbook_id=variant['playbook_id'],case=case)
            cells.append(dict(body,cell_id='cell-'+canonical_sha256(body)))
    return variants,cells


class _Job:
    def __init__(self, token, plan, root):
        if token is not _TOKEN: raise TypeError('Lab authority is backend-owned')
        self.plan=plan;self.root=root;self.cancel=threading.Event();self.busy=False;self.closed=False
    def __reduce__(self): raise TypeError('Lab handles cannot be serialized')


def _owned(job):
    if type(job) is not _Job or job.closed or _ACTIVE.get(job.plan['artifact_id']) is not job:
        raise ValueError('stale Lab job')
    return job


def _read_plan(identifier,root):
    plan=store.load(identifier,'study-',root)
    exact(plan,('schema_id','attempt_id','parent_study_id','spec','variants','cells','branch_semantics','worker_concurrency','artifact_id'),'study plan')
    variants,cells=_spec(plan['spec'])
    if plan['schema_id']!=SCHEMA or plan['variants']!=variants or plan['cells']!=cells or plan['branch_semantics']!=BRANCH or plan['worker_concurrency']!=1:
        raise ValueError('planned study claims differ from frozen specification')
    if type(plan['attempt_id']) is not str or len(plan['attempt_id'])!=32 or any(c not in '0123456789abcdef' for c in plan['attempt_id']): raise ValueError('invalid study attempt')
    if plan['parent_study_id'] is not None: store.identity(plan['parent_study_id'],'study-')
    return plan


def _results(plan,root):
    records={}
    planned={c['cell_id']:c for c in plan['cells']}
    for identifier in store.scan('trial-',root):
        row=store.load(identifier,'trial-',root)
        exact(row,('schema_id','study_id','cell_id','status','error','result','artifact_id'),'trial receipt')
        if row['study_id']!=plan['artifact_id']: continue
        if row['schema_id']!='KIRBY2_LAB_CELL_V1' or row['cell_id'] not in planned: raise ValueError('trial has no planned parent')
        if row['cell_id'] in records: raise ValueError('multiple terminal results for one planned cell')
        if row['status'] not in {'COMPLETE','FAILED','CANCELLED'}: raise ValueError('unsupported terminal disposition')
        result=row['result']; cell=planned[row['cell_id']]
        if result is not None:
            if result['status']!=row['status'] or result['case']!=cell['case'] or result['playbook_id']!=cell['playbook_id'] or result['error']!=row['error']:
                raise ValueError('trial result changed its planned cell')
        elif row['status']=='COMPLETE': raise ValueError('completed cell has no pipeline result')
        records[row['cell_id']]=row
    return records


def _snapshot(plan,root,live=False):
    records=_results(plan,root)
    cells=[dict(cell,status=records[cell['cell_id']]['status'] if cell['cell_id'] in records else 'PLANNED' if live else 'INCOMPLETE',
                trial_id=records[cell['cell_id']]['artifact_id'] if cell['cell_id'] in records else None)
           for cell in plan['cells']]
    statuses={c['status'] for c in cells}
    status='RUNNING' if 'PLANNED' in statuses else 'INCOMPLETE' if statuses & {'CANCELLED','INCOMPLETE'} else 'FINISHED_WITH_FAILURES' if 'FAILED' in statuses else 'COMPLETE'
    return dict(schema_id=SCHEMA,study_id=plan['artifact_id'],title=plan['spec']['title'],parent_study_id=plan['parent_study_id'],
                status=status,planned_count=len(cells),terminal_count=len(records),cells=cells)


def prepare_study(spec,root=None,parent_study_id=None):
    variants,cells=_spec(spec)
    if parent_study_id is not None:
        parent=_read_plan(parent_study_id,root)
        for cell in parent['cells']:store.reveal(cell['case']['case_id'],'USED_FOR_RETRY',root)
    plan=store.seal(dict(schema_id=SCHEMA,attempt_id=uuid.uuid4().hex,parent_study_id=parent_study_id,spec=copy.deepcopy(spec),
        variants=variants,cells=cells,branch_semantics=BRANCH,worker_concurrency=1),'study-')
    # Publication precedes runtime acquisition, including for a one-cell study.
    store.publish(plan,root)
    job=_Job(_TOKEN,plan,root)
    with _LOCK:
        snapshot=_snapshot(plan,root,True)
        _ACTIVE[plan['artifact_id']]=job
    return job,snapshot


def cancel_study(job):
    with _LOCK:
        _owned(job);job.cancel.set()
        return dict(status='CANCELLATION_REQUESTED',study_id=job.plan['artifact_id'])


def _record(job,cell,status,result=None,error=None):
    return store.publish(store.seal(dict(schema_id='KIRBY2_LAB_CELL_V1',study_id=job.plan['artifact_id'],cell_id=cell['cell_id'],
                                        status=status,result=result,error=error),'trial-'),job.root)


def advance_study(job):
    with _LOCK:
        _owned(job)
        if job.busy: raise ValueError('Lab cell already settling')
        job.busy=True
    try:
        records=_results(job.plan,job.root)
        pending=[cell for cell in job.plan['cells'] if cell['cell_id'] not in records]
        if job.cancel.is_set():
            for cell in pending: _record(job,cell,'CANCELLED',error='Cancelled before this cell acquired a runtime')
        elif pending:
            cell=pending[0]
            source=next(v['source'] for v in job.plan['variants'] if v['playbook_id']==cell['playbook_id'])
            try:
                result=run_trial(source,cell['case'],job.cancel,job.root)
            except Exception as error:
                # No fake outcome for a failed acquisition, checkpoint, or disk
                # operation. Planned parent remains visible even if this write fails.
                _record(job,cell,'FAILED',error=f'{type(error).__name__}: {error}'[:1000])
            else: _record(job,cell,result['status'],result,result['error'])
        return _snapshot(job.plan,job.root,True)
    finally:
        with _LOCK: job.busy=False


def close_study(job):
    with _LOCK:
        if type(job) is _Job and job.closed: return dict(status='CLOSED')
        _owned(job)
        if job.busy: raise ValueError('Lab cleanup waits for the active cell')
        job.closed=True;del _ACTIVE[job.plan['artifact_id']]
        return dict(status='CLOSED')


def list_studies(root=None):
    entries=[];rejected=[]
    for identifier in store.scan('study-',root):
        try: entries.append(_snapshot(_read_plan(identifier,root),root))
        except (ValueError,TypeError,KeyError,OSError): rejected.append(identifier)
    return dict(entries=entries,rejected=rejected)


def open_study(identifier,root=None):
    plan=_read_plan(identifier,root); records=_results(plan,root)
    # Mark every reserved lineage before returning any result or summary. The
    # ledger is independent of source names, study titles, retries and exports.
    for cell in plan['cells']: store.reveal(cell['case']['case_id'],'INSPECTED',root)
    cells=[]
    for cell in plan['cells']:
        row=records.get(cell['cell_id'])
        result=None if row is None else row['result']
        if result is not None:
            evidence=open_execution_evidence(result['execution_evidence_id'],root)
            if evidence['review']!=result['review'] or evidence['checkpoint_sha256']!=result['checkpoint_sha256']:
                raise ValueError('cell execution evidence differs')
            if row['status']=='COMPLETE':
                source=next(v['source'] for v in plan['variants'] if v['playbook_id']==cell['playbook_id'])
                with tempfile.TemporaryDirectory(prefix='kirby2-lab-open-') as temporary:
                    reproduced=run_trial(source,cell['case'],threading.Event(),str(Path(temporary).resolve()))
                if scientific_projection(reproduced)!=scientific_projection(result):
                    raise ValueError('cell scientific claims differ from reconstructed policy execution')
        cells.append(dict(cell,status='INCOMPLETE' if row is None else row['status'],result=result,
                          error=None if row is None else row['error'],trial_id=None if row is None else row['artifact_id']))
    return dict(schema_id=SCHEMA,plan=plan,status=_snapshot(plan,root)['status'],cells=cells,summary=grouped_summary(cells),
                reveal_history=store.reveals(root),winner=None,unfamiliar_assessment=False,
                notice='All tried cells retained. Curated synthetic cases; reserved Lab results become revealed on inspection.')


def retry_study(identifier,root=None):
    plan=_read_plan(identifier,root)
    for cell in plan['cells']: store.reveal(cell['case']['case_id'],'USED_FOR_RETRY',root)
    return prepare_study(plan['spec'],root,identifier)


def verify_study(identifier,root=None):
    """Rebuild each successful trial; no dependence on wall time or run order."""
    opened=open_study(identifier,root);checks=[]
    for cell in opened['cells']:
        if cell['status']!='COMPLETE':
            checks.append(dict(cell_id=cell['cell_id'],status='NOT_REPRODUCIBLE_OPERATIONAL_DISPOSITION',disposition=cell['status']))
        else:
            checks.append(dict(cell_id=cell['cell_id'],status='PASS',scientific_sha256=canonical_sha256(scientific_projection(cell['result']))))
    return dict(status='PASS',study_id=identifier,cells=checks,qualification='REPRODUCTION_ONLY')


def export_study(identifier,root=None):
    # The detached portable record carries reveal history, never an unexposed alias.
    opened=open_study(identifier,root)
    for cell in opened['cells']: store.reveal(cell['case']['case_id'],'EXPORTED',root)
    opened['reveal_history']=store.reveals(root)
    from .execution_library import _load
    dependencies={}
    for cell in opened['cells']:
        if cell['result'] is not None:
            identifier=cell['result']['execution_evidence_id']
            record,_=_load(identifier,root);dependencies[identifier]=record
    return store.seal(dict(schema_id='KIRBY2_LAB_EXPORT_V1',study=opened,execution_dependencies=dependencies),'lab-export-')
