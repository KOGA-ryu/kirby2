"""Small append-only local evidence seam; no mutable scientific result files."""
from __future__ import annotations
import os
import re
import shutil
import tempfile
from pathlib import Path

from kirby2.research.paths import DataAreaId
from kirby2.full_day.models import canonical_sha256
from .practice_library import _paths, _child, _read, _write, _bytes, _decode, _sync_directory


def directory(root, create=False):
    paths = _paths(root); paths.validate(DataAreaId.EVIDENCE)
    if create: paths.ensure(DataAreaId.EVIDENCE)
    path = _child(paths.area(DataAreaId.EVIDENCE), 'playbook-lab-v1')
    if create: path.mkdir(exist_ok=True)
    return path


def identity(value, prefix):
    if type(value) is not str or re.fullmatch(re.escape(prefix)+'[0-9a-f]{64}', value) is None:
        raise ValueError('invalid '+prefix+' identity')
    return value


def seal(body, prefix):
    return dict(body, artifact_id=prefix+canonical_sha256(body))


def verify(record, prefix):
    if type(record) is not dict or 'artifact_id' not in record: raise ValueError('missing immutable identity')
    identity(record['artifact_id'], prefix)
    if record != seal({k:v for k,v in record.items() if k!='artifact_id'}, prefix): raise ValueError('immutable digest differs')
    return record


def publish(record, root):
    path = directory(root, True)
    name = record['artifact_id']+'.json'
    target = _child(path, name)
    temporary = tempfile.mkdtemp(prefix='.pending-', dir=path)
    try:
        staged = _child(Path(temporary), 'record.json')
        raw = _bytes(record)
        if len(raw)>16*1024*1024: raise ValueError('Lab record exceeds the 16 MiB bound')
        _write(staged, raw)
        try: os.link(staged, target)
        except FileExistsError:
            if _read(target)!=raw: raise ValueError('immutable record collision')
        _sync_directory(path)
    finally: shutil.rmtree(temporary)
    return record


def load(identifier, prefix, root):
    identity(identifier, prefix)
    record = verify(_decode(_read(_child(directory(root),identifier+'.json'))), prefix)
    if record['artifact_id']!=identifier: raise ValueError('filename identity differs')
    return record


def scan(prefix, root):
    path = directory(root)
    if not path.exists(): return []
    return sorted(p.stem for p in path.iterdir() if p.name.startswith(prefix) and p.suffix=='.json')


def lineage(recipe, seed):
    return 'case-'+canonical_sha256(dict(recipe_id=recipe,seed=seed,origin='EXECUTION_CURRICULUM_V1'))


def reveal(case_id, reason, root):
    identity(case_id, 'case-')
    if reason not in {'INSPECTED','PRACTICED','EXPORTED','USED_FOR_RETRY'}: raise ValueError('unsupported reveal reason')
    record = seal(dict(schema_id='KIRBY2_CASE_REVEAL_V1',case_id=case_id,reason=reason), 'reveal-')
    return publish(record,root)


def reveals(root):
    result = {}
    for identifier in scan('reveal-',root):
        row = load(identifier,'reveal-',root)
        if set(row)!={'schema_id','case_id','reason','artifact_id'} or row['schema_id']!='KIRBY2_CASE_REVEAL_V1': raise ValueError('unsupported reveal record')
        identity(row['case_id'],'case-')
        result.setdefault(row['case_id'],[]).append(row['reason'])
    return result
