"""Explicit execution bundle schema alongside the Chapter 2 evidence library.

Frozen pending state is durable. Opening verifies both exact checkpoint restore
and deterministic reconstruction of the recorded instructions and refusals.
The old practice bundle schema and its repeat semantics remain unchanged.
"""
from __future__ import annotations
import copy
import os
import re
import shutil
import tempfile
from pathlib import Path

from kirby2.full_day.models import canonical_sha256
from kirby2.full_day.runtime import FullDayRuntime
from kirby2.research.paths import DataAreaId
from .practice_library import _bytes, _decode, _paths, _child, _read, _write, _sync_directory
from .execution_practice import _LOCK, _Handle, _TOKEN, _owned, _apply, _knowledge, _review, _reconcile, _decision, MAX_OPERATIONS
from .execution_commitments import ExecutionRefusal, integer
from .execution_recipes import STOP_US

SCHEMA = 'KIRBY2_EXECUTION_BUNDLE_V1'
_ID = re.compile(r'execution-evidence-[0-9a-f]{64}\Z')


def _directory(root,create=False):
    paths = _paths(root)
    paths.validate(DataAreaId.EVIDENCE)
    if create: paths.ensure(DataAreaId.EVIDENCE)
    directory = _child(paths.area(DataAreaId.EVIDENCE),'execution-practice-v1')
    if create: directory.mkdir(exist_ok=True)
    return directory


def _verify(record):
    if type(record) is not dict or set(record)!={'schema_id','seed','recipe_id','attempt_id','operations','runtime','knowledge','review','evidence_id'} or record['schema_id']!=SCHEMA:
        raise ValueError('unsupported execution evidence schema')
    if type(record['attempt_id']) is not str or re.fullmatch(r'execution-[0-9a-f]{32}',record['attempt_id']) is None:
        raise ValueError('invalid execution attempt identity')
    identifier = 'execution-evidence-'+canonical_sha256({k:v for k,v in record.items() if k!='evidence_id'})
    if identifier!=record['evidence_id']: raise ValueError('execution evidence identity mismatch')
    operations = record['operations']
    if type(operations) is not list or not 1<=len(operations)<=MAX_OPERATIONS:
        raise ValueError('execution instruction count is out of bounds')
    runtime = FullDayRuntime.from_checkpoint_state(record['runtime'])
    reconstructed = _Handle(_TOKEN,record['seed'],record['recipe_id'])
    for operation in operations:
        if type(operation) is not dict or set(operation)!={'action','payload','refusal','decision'}:
            raise ValueError('invalid recorded execution instruction')
        if operation['decision']!=_decision(reconstructed): raise ValueError('recorded client decision knowledge differs')
        refusal = None
        try: _apply(reconstructed,operation['action'],operation['payload'])
        except ExecutionRefusal as error:
            refusal = dict(code=error.code,category=error.category,message=str(error))
        if refusal!=operation['refusal']: raise ValueError('recorded refusal differs from reconstruction')
        reconstructed.operations.append(copy.deepcopy(operation))
    if not reconstructed.frozen: raise ValueError('execution evidence does not end at a frozen cut')
    if runtime.canonical_state_bytes()!=reconstructed.runtime.canonical_state_bytes():
        raise ValueError('execution checkpoint differs from its recorded instructions')
    if record['knowledge']!=_knowledge(reconstructed) or record['review']!=_review(reconstructed):
        raise ValueError('execution evidence changes knowledge or review claims')
    return reconstructed


def save_execution_evidence(handle,root=None):
    with _LOCK:
        _owned(handle)
        if not handle.frozen: raise ExecutionRefusal('MUTABLE_SAVE','Freeze the attempt before saving its evidence.')
        record = dict(schema_id=SCHEMA,seed=handle.seed,recipe_id=handle.recipe_id,attempt_id=handle.source_id,operations=copy.deepcopy(handle.operations),
            runtime=handle.runtime.checkpoint_state(),knowledge=_knowledge(handle),review=_review(handle))
        record['evidence_id'] = 'execution-evidence-'+canonical_sha256(record)
        _verify(record)
        directory = _directory(root,True)
        destination = _child(directory,record['evidence_id']+'.json')
        staging = Path(tempfile.mkdtemp(prefix='.pending-',dir=directory))
        try:
            raw = _bytes(record)
            _write(staging/'bundle.json',raw)
            try: os.link(staging/'bundle.json',destination)
            except FileExistsError:
                if _read(destination)!=raw: raise ValueError('immutable execution evidence collision')
            _sync_directory(directory)
        finally: shutil.rmtree(staging)
        return dict(schema_id=SCHEMA,evidence_id=record['evidence_id'],review=copy.deepcopy(record['review']),
                    checkpoint_sha256=canonical_sha256(record['runtime']))


def _load(evidence_id,root):
    if type(evidence_id) is not str or not _ID.fullmatch(evidence_id): raise ValueError('invalid execution evidence identity')
    record = _decode(_read(_child(_directory(root),evidence_id+'.json')))
    if type(record) is not dict or record.get('evidence_id')!=evidence_id: raise ValueError('execution filename identity differs')
    return record,_verify(record)


def open_execution_evidence(evidence_id,root=None):
    record,_ = _load(evidence_id,root)
    return dict(schema_id=SCHEMA,evidence_id=evidence_id,review=copy.deepcopy(record['review']),
                checkpoint_sha256=canonical_sha256(record['runtime']))


def list_execution_evidence(root=None):
    directory = _directory(root)
    if not directory.exists(): return dict(entries=[],rejected=[])
    entries,rejected = [],[]
    for path in sorted(directory.iterdir()):
        if path.name.startswith('.pending-'): continue
        try:
            if path.suffix!='.json': raise ValueError('unexpected execution evidence file')
            opened = open_execution_evidence(path.stem,root)
            entries.append(dict(evidence_id=path.stem,settlement=opened['review']['settlement']))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError): rejected.append(path.name)
    return dict(entries=entries,rejected=rejected)


def verify_execution_continuation(evidence_id,delta_us,root=None):
    """Read-only audit branch of a durable cut; never resumes the learner's intent.

    Reports reproducible model continuation, not a mutable practice or a claim
    that the recorded attempt already observed its future reports.
    """
    record,reconstructed = _load(evidence_id,root)
    delta = integer(delta_us,'continuation',1,STOP_US-reconstructed.runtime.clock.current_time_us)
    restored = FullDayRuntime.from_checkpoint_state(record['runtime'])
    target = restored.clock.current_time_us+delta
    for runtime in (restored,reconstructed.runtime):
        runtime.advance_to(target)
        runtime.capture_quiescent_cut()
    if restored.canonical_state_bytes()!=reconstructed.runtime.canonical_state_bytes():
        raise ValueError('restored pending execution continuation differs')
    _reconcile(reconstructed)
    return dict(status='PASS',before_us=record['runtime']['clock']['current_time_us'],after_us=target,
        state_sha256=canonical_sha256(restored.checkpoint_state()),knowledge=_knowledge(reconstructed),
        branch='AUDIT_CONTINUATION_ONLY',recorded_evidence_unchanged=True)
