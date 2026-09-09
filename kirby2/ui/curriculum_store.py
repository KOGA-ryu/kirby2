"""Bounded append-only curriculum ledger with serialized atomic publication."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import tempfile
import threading

from kirby2.full_day.models import canonical_sha256
from kirby2.research.paths import DataAreaId
from .practice_library import _paths, _child, _read, _bytes, _decode, _write, _sync_directory
from .playbooks import exact

SCHEMA = 'KIRBY2_CURRICULUM_LEDGER_V1'
MAX_EVENTS = 512
_LOCK = threading.RLock()


def directory(root):
    paths = _paths(root)
    paths.validate(DataAreaId.EVIDENCE)
    paths.ensure(DataAreaId.EVIDENCE)
    path = _child(paths.area(DataAreaId.EVIDENCE), 'curriculum-v1')
    path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise ValueError('curriculum location is not a directory')
    return path


@contextmanager
def locked(root):
    with _LOCK:
        path = directory(root)
        fd = os.open(_child(path, '.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('curriculum lock is not a regular file')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield path
        finally:
            os.close(fd)


def read(path):
    names = sorted(p.name for p in path.iterdir() if not p.name.startswith('.'))
    if len(names)>MAX_EVENTS:
        raise ValueError('curriculum event budget exceeded')
    events = []
    parent = None
    for ordinal, name in enumerate(names, 1):
        if name != f'{ordinal:06d}.json':
            raise ValueError('curriculum ledger has a gap or unexpected object')
        row = _decode(_read(_child(path, name)))
        exact(row, ('schema_id','ordinal','parent','kind','payload','recorded_at','event_id'), 'curriculum event')
        if row['schema_id']!=SCHEMA or type(row['ordinal']) is not int or row['ordinal']!=ordinal or row['parent']!=parent:
            raise ValueError('curriculum chain differs')
        if type(row['recorded_at']) is not int or row['recorded_at']<0 or (events and row['recorded_at']<events[-1]['recorded_at']):
            raise ValueError('curriculum chronology differs')
        body = {k:v for k,v in row.items() if k!='event_id'}
        if row['event_id']!='curriculum-event-'+canonical_sha256(body):
            raise ValueError('curriculum event digest differs')
        parent = row['event_id']; events.append(row)
    return events


def append(path, events, kind, payload, now):
    if type(now) is not int or now<0:
        raise ValueError('invalid local scheduling clock')
    if len(events)>=MAX_EVENTS:
        raise ValueError('curriculum ledger full; existing evidence remains readable')
    if events and now<events[-1]['recorded_at']:
        raise ValueError('local clock moved backwards; no new scheduling claim is available')
    body = dict(schema_id=SCHEMA, ordinal=len(events)+1, parent=events[-1]['event_id'] if events else None,
                kind=kind, payload=payload, recorded_at=now)
    row = dict(body, event_id='curriculum-event-'+canonical_sha256(body))
    raw = _bytes(row)
    if len(raw)>2*1024*1024:
        raise ValueError('curriculum event exceeds 2 MiB')
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path)
    os.close(fd)
    temporary = Path(name)
    try:
        # mkstemp owns the path; use the common durable writer on a fresh child.
        temporary.unlink()
        _write(temporary, raw)
        os.link(temporary, _child(path, f'{len(events)+1:06d}.json'))
        _sync_directory(path)
    finally:
        temporary.unlink(missing_ok=True)
    events.append(row)
    return row
