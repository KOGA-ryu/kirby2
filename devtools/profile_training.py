"""Text-only cProfile and phase timings for the actual training-folder owners.

Run from the backend checkout with ``python -m devtools.profile_training --help``.
No Qt, images, network, or new runtime dependencies. Every output is a NEW folder.
Export uses a verified disposable copy; recovery uses the supplied export read-only.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import cProfile
import hashlib
import json
from pathlib import Path
import platform
import pstats
import sys
import time
import traceback
from unittest.mock import patch
import uuid

from kirby2.ui import daily_use as daily


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def source_state():
    root = Path(daily.__file__).resolve().parents[1]
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(root.rglob('*'))
             if p.is_file() and p.suffix in {'.py', '.json', '.toml'}}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return dict(root=str(root), sha256=digest, files=files)


def run(operation, source, output, profile=False):
    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().absolute()
    if output != output.resolve(strict=False):
        raise ValueError('diagnostic output must use a real path')
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('diagnostic output must be separate from the input')
    output.mkdir(parents=True, exist_ok=False)
    before = source_state()
    write_json(output / 'source-before.json', before)
    operation_id = uuid.uuid4().hex
    summary = dict(operation_id=operation_id, operation=operation, profiled=profile,
                   python=sys.version, executable=sys.executable, platform=platform.platform(),
                   status='INCOMPLETE', source_sha256=before['sha256'],
                   harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   timing_scope='Owner operation only; imports and input preparation excluded.',
                   images=False, qt=False)
    events = output / 'events.jsonl'
    stack = []
    phases = []
    started = time.perf_counter()

    def event(kind, **fields):
        row = dict(operation_id=operation_id, event=kind,
                   offset_seconds=time.perf_counter()-started, **fields)
        with events.open('a') as stream:
            stream.write(json.dumps(row, sort_keys=True) + '\n')

    def timed(name, function):
        def call(*args, **kwargs):
            span_id = len(phases)
            row = dict(span_id=span_id, parent_span_id=stack[-1] if stack else None,
                       phase=name, status='INCOMPLETE')
            phases.append(row)
            event('phase_started', **row)
            stack.append(span_id)
            wall, cpu = time.perf_counter(), time.process_time()
            try:
                value = function(*args, **kwargs)
                row['status'] = 'PASS'
                return value
            except BaseException as error:
                row['error_type'] = type(error).__name__
                row['status'] = 'FAIL'
                raise
            finally:
                row['elapsed_seconds'] = time.perf_counter()-wall
                row['process_cpu_seconds'] = time.process_time()-cpu
                stack.pop()
                event('phase_finished', **row)
                print(f"{name}: {row['status']} {row['elapsed_seconds']:.3f}s", flush=True)
        return call

    profiler = cProfile.Profile() if profile else None
    try:
        if operation == 'export':
            inventory, excluded = daily._copy(source, output / 'input')
            write_json(output / 'input-inventory.json', dict(files=inventory, excluded=excluded))
            callback = lambda: daily.export_training(str(output / 'export'), str(output / 'input'))
        else:
            callback = lambda: daily.recover_training(str(source), str(output / 'recovered'))
            write_json(output / 'input-export.json', json.loads((source / 'export.json').read_bytes()))
        with ExitStack() as patches:
            for name in ('_inventory', '_copy', '_verify_training', '_reveal_export',
                         '_verified_copy', 'create_backup', 'verify_backup', 'restore_backup'):
                patches.enter_context(patch.object(daily, name, timed(name, getattr(daily, name))))
            event('operation_started')
            wall, cpu = time.perf_counter(), time.process_time()
            try:
                summary['result'] = profiler.runcall(callback) if profiler else callback()
                summary['status'] = 'PASS'
            finally:
                summary['elapsed_seconds'] = time.perf_counter()-wall
                summary['process_cpu_seconds'] = time.process_time()-cpu
    except BaseException as error:
        summary.update(status='FAIL', error_type=type(error).__name__, error=str(error))
        (output / 'exception.txt').write_text(traceback.format_exc())
        raise
    finally:
        summary['profile_available'] = profiler is not None and bool(profiler.getstats())
        if summary['profile_available']:
            profiler.dump_stats(str(output / 'profile.pstats'))
            with (output / 'profile.txt').open('w') as stream:
                stats = pstats.Stats(profiler, stream=stream).strip_dirs()
                for key in ('cumulative', 'tottime', 'calls'):
                    stream.write('\nSORTED BY ' + key + '\n')
                    stats.sort_stats(key).print_stats(50)
                stats.sort_stats('cumulative').print_callers('fsync|_verify|run_trial|checkpoint')
        after = source_state()
        write_json(output / 'source-after.json', after)
        if before != after:
            summary['status'] = 'UNSETTLED_SOURCE_CHANGED'
        summary['phases'] = phases
        summary['phase_accounting'] = 'Inclusive nested wall/CPU times; do not sum parent and child spans.'
        summary['qt_imported'] = any(n.startswith(('PySide6', 'PyQt')) for n in sys.modules)
        if summary['qt_imported']:
            summary['status'] = 'FAIL'
        write_json(output / 'summary.json', summary)
        event('operation_finished', status=summary['status'])
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('export', 'recover'))
    parser.add_argument('--source', type=Path, required=True, help='training data for export; portable export for recovery')
    parser.add_argument('--output', type=Path, required=True, help='new diagnostic folder outside the source')
    parser.add_argument('--profile', action='store_true', help='collect cProfile; omit for baseline wall time')
    args = parser.parse_args()
    result = run(args.operation, args.source, args.output, args.profile)
    print(json.dumps({k:v for k,v in result.items() if k not in {'phases','result'}}, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
