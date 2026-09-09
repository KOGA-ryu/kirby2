"""Bounded portable training folders, using the governed backup/restore owners.

No live authority is serialized. Inspect/reopen reconstruct scientific evidence
in a disposable copy; verification never marks the original source inspected.
Export is an explicit reveal of reserved cases, even if later publication fails.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import importlib.metadata
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading

from kirby2.research.paths import DataPaths
from kirby2.release.backup import BackupSelectionV1, create_backup, verify_backup
from kirby2.release.restore import restore_backup
from .practice_library import _paths, _read, _write, _bytes, _decode, _sync_directory
from . import playbook_store as store

SCHEMA = 'KIRBY2_TRAINING_EXPORT_V1'
MAX_FILES = 2048
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
TREES = ('evidence/practice-library', 'config/practice-library',
         'evidence/execution-practice-v1', 'evidence/market-workbench',
         'evidence/playbook-lab-v1', 'evidence/curriculum-v1')
UNITS = dict(price='integer ticks; no currency conversion', quantity='integer shares',
             simulation_time='integer microseconds', costs='milliticks per share',
             scheduling_time='local UTC epoch seconds; not reaction time')
_LOCK = threading.Lock()
README = '''Kirby2 portable training evidence V1

Synthetic practice only. No real-market, profitability, learning or release claim.
This folder contains a governed backup, not an automatically resumed session.
Use Library > Verify export, then Recover to new folder. Launch with --data-root
pointing to that new folder. Recovery never overwrites the active data folder.
Install both kirby2 and kirby2-ui; supported scientific owners verify every chain.
A producer version or byte digest alone is not proof of scientific compatibility.

Evidence, recipe dependencies, all tried study cells, and reveal history travel
together. Editable notes are separate in config/practice-library, not evidence.
Export reveals reserved cases in BOTH the source and recovered training history.
Unfamiliar-assessment claims cannot be recovered by renaming or exporting a case.
Incomplete studies remain INCOMPLETE; frozen partial fills remain unresolved.
Curriculum instructions may be reopened, but private execution handles never resume.
Pending save debris, disposable indexes, runtime caches and identity mappings are
excluded. Preserve the original folder when investigating a refused record.

Units: prices in integer ticks, quantities in shares, simulation time in integer
microseconds, costs in milliticks per share, scheduling in local UTC epoch seconds.
Notes can contain personal text. This export is NOT ENCRYPTED. Share deliberately.
'''


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(value):
    path = Path(value).expanduser().absolute()
    if path != path.resolve(strict=False):
        raise ValueError('use a resolved real path; symlinks are not supported')
    return path


def _inventory(root):
    """Bound before allocation; do not traverse links or operational debris."""
    DataPaths(root).validate()
    rows, skipped = {}, []
    total = 0
    def visit(path):
        nonlocal total
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError('training evidence contains a symbolic link')
        if path.name.startswith('.'):
            if path.name in {'.lock', '.notes.lock'} or path.name.startswith(('.pending-', '.note-')):
                skipped.append(str(path.relative_to(root))); return
            raise ValueError('unexpected hidden training object')
        if stat.S_ISDIR(mode):
            with os.scandir(path) as items:
                for item in items:
                    visit(Path(item.path))
            return
        if not stat.S_ISREG(mode):
            raise ValueError('training evidence is not a regular file')
        size = path.stat().st_size
        if len(rows) >= MAX_FILES or size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            raise ValueError('training folder exceeds the declared export workload; no files dropped')
        raw = _read(path)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('training folder grew past export workload')
        rows[str(path.relative_to(root))] = dict(sha256=_sha(raw), bytes=len(raw))
    for rel in TREES:
        path = root / rel
        if path.exists() or path.is_symlink(): visit(path)
    return dict(sorted(rows.items())), sorted(skipped)


def _copy(root, destination):
    before, skipped = _inventory(root)
    destination.mkdir()
    for relative, expected in before.items():
        raw = _read(root / relative)
        if dict(sha256=_sha(raw), bytes=len(raw)) != expected:
            raise ValueError('source changed during evidence capture')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _write(target, raw)
    after, _ = _inventory(root)
    if before != after:
        raise ValueError('source changed during evidence capture; retry when idle')
    return before, skipped


def _verify_training(root):
    """Only canonical domain owners can accept a saved scientific chain."""
    from . import practice_library as practice, execution_library as execution
    from . import market_workbench as market, playbook_lab as lab, playbook_drills as drills
    from . import radar_evidence as radar, curriculum, curriculum_store as ledger
    counts = Counter(); studies = []; handled = set()
    for family, loader in (('practice', practice.list_practice_evidence),
                           ('execution', execution.list_execution_evidence)):
        result = loader(str(root))
        if result['rejected']: raise ValueError(f'{family} evidence refused: {result["rejected"]}')
        counts[family] = len(result['entries'])
        if family == 'practice':
            for item in result['entries']:
                bundle = root/'evidence/practice-library'/item['evidence_id']
                if set(p.name for p in bundle.iterdir()) != {'manifest.json','practice.json','replay.json'}:
                    raise ValueError('practice bundle contains unexpected dependencies')
    notes = root / 'config/practice-library'
    for path in notes.iterdir() if notes.exists() else ():
        if path.name.startswith('.'): continue
        if path.suffix != '.json': raise ValueError('unsupported note object')
        practice.read_practice_note(path.stem, str(root)); counts['notes'] += 1
    for identifier in store.scan('study-', str(root)):
        opened = lab.open_study(identifier, str(root))
        studies.append(dict(study_id=identifier, status=opened['status']))
        handled.add(identifier)
        # Missing/failed/cancelled cells keep their own dispositions.
        for cell in opened['cells']:
            if cell['result'] is not None: counts['verified_trial_dependencies'] += 1
    counts['studies'] = len(studies)
    for path in store.directory(str(root)).iterdir() if store.directory(str(root)).exists() else ():
        if path.name.startswith('.'): continue
        identifier = path.stem
        if path.suffix != '.json': raise ValueError('unsupported Lab object')
        if identifier in handled: continue
        if identifier.startswith('trial-'):
            row = store.load(identifier, 'trial-', str(root))
            if row.get('study_id') not in handled: raise ValueError('orphan trial receipt')
            # _results rejects unplanned cells, duplicates and false statuses.
        elif identifier.startswith('radar-evidence-'):
            radar.open_world(identifier, str(root)); counts['radar'] += 1
        elif identifier.startswith('drill-review-'):
            row = store.load(identifier, 'drill-review-', str(root))
            if row != drills._review_record(row.get('candidate_id')):
                raise ValueError('unsupported drill review')
            drills.open_drill_candidate(row['candidate_id'], str(root)); counts['drill_reviews'] += 1
        elif identifier.startswith('drill-'):
            drills.open_drill_candidate(identifier, str(root)); counts['drills'] += 1
        elif identifier.startswith('reveal-'):
            store.load(identifier, 'reveal-', str(root)); counts['reveals'] += 1
        else: raise ValueError('unsupported Lab evidence: '+path.name)
    store.reveals(str(root))
    market_root = root / 'evidence/market-workbench'
    for path in market_root.iterdir() if market_root.exists() else ():
        if path.name.startswith('.'): continue
        if path.name.startswith('market-report-'):
            report = market.open_market_report(path.name, str(root)); counts['market_reports'] += 1
            expected_files = {'report.json'} | {cell['artifact_ref']['artifact_sha256']+'.json'
                for cell in report['priors']+report['cells'] if cell['status']=='AVAILABLE'}
            if set(p.name for p in path.iterdir()) != expected_files:
                raise ValueError('market report contains unexpected dependencies')
        elif path.name in {'configurations', 'exposures'}:
            for child in path.iterdir():
                if child.name.startswith('.'): continue
                row = _decode(_read(child))
                if path.name == 'configurations':
                    if set(row) != {'schema_id','configuration','familiarity'} or row['schema_id'] != 'KIRBY2_MARKET_CONFIGURATION_V1':
                        raise ValueError('unsupported market configuration')
                    config = row['configuration']
                    selection = config['resolution']['selection']
                    expected = market.market_start_configuration(selection['profile_ref']['profile_id'], selection['seed'])
                    if row['familiarity'] not in {'RECIPE_KNOWN; workbench practice is not a blind assessment','EXPOSED_BY_PREVIEW; workbench practice is not a blind assessment'}:
                        raise ValueError('unsupported configuration familiarity')
                    if config != expected or child.name != 'market-config-'+market.canonical_digest(row)+'.json':
                        raise ValueError('market configuration differs')
                else:
                    if set(row) != {'schema_id','selection','familiarity'} or row['schema_id'] != 'KIRBY2_MARKET_EXPOSURE_V1' or row['familiarity'] != 'EXPOSED_BY_PREVIEW':
                        raise ValueError('unsupported market exposure')
                    if child.name != market.canonical_digest(row['selection'])+'.json': raise ValueError('market exposure identity differs')
                if path.name == 'exposures' and market.resolve_simulation_profile(row['selection'])['status'] != 'AVAILABLE':
                    raise ValueError('unsupported exposed market recipe')
                counts['market_'+path.name] += 1
        else: raise ValueError('unsupported market evidence')
    active = None
    if (root / 'evidence/curriculum-v1').exists():
        dashboard = curriculum.dashboard(str(root))
        with ledger.locked(str(root)) as path:
            events = ledger.read(path); state = curriculum._state(events)
        for attempt in state['sessions']: curriculum.open_attempt(attempt['attempt_id'], str(root))
        counts['curriculum_events'] = len(events)
        active = None if dashboard['active'] is None else dashboard['active']['kind']
    return dict(counts=dict(counts), studies=studies, durable_active_instructions=active,
                live_authority_restored=False)


def _reveal_export(root):
    from . import curriculum, curriculum_store as ledger, curriculum_cases as cases, playbook_lab as lab
    for identifier in store.scan('study-', str(root)):
        plan = lab._read_plan(identifier, str(root))
        for cell in plan['cells']:
            for reason in ('INSPECTED','EXPORTED'): store.reveal(cell['case']['case_id'], reason, str(root))
    # The exported protocol contains reserved seed identities. Export cannot
    # provide a back door to seeing future cases while retaining blindness.
    if (root / 'evidence/curriculum-v1').exists():
        with ledger.locked(str(root)) as path:
            state = curriculum._state(ledger.read(path))
            if state['active'] is not None:
                raise ValueError('end the active curriculum attempt before exporting its reserved cases')
            if state['plan']:
                for bank in state['plan']['bank'].values():
                    for spec in bank: store.reveal(store.lineage(cases.RECIPE, spec['seed']), 'EXPORTED', str(root))
    for identifier in store.scan('drill-', str(root)):
        if identifier.startswith('drill-review-'): continue
        row = store.load(identifier, 'drill-', str(root))
        for reason in ('INSPECTED','EXPORTED'): store.reveal(row['case_id'], reason, str(root))


def inspect_training(root=None):
    source = _paths(root).root
    with tempfile.TemporaryDirectory(prefix='kirby2-training-inspect-') as temporary:
        copied = Path(temporary).resolve() / 'data'
        rows, skipped = _copy(source, copied)
        report = _verify_training(copied)
    return dict(status='VERIFIED', data_root=str(source), file_count=len(rows),
                byte_count=sum(r['bytes'] for r in rows.values()), excluded_operational_objects=skipped,
                recovery='Inventory rebuilt from immutable records; no live continuation.', **report)


def export_training(destination, root=None):
    if not _LOCK.acquire(blocking=False): raise ValueError('another export/recovery is running')
    try:
        source = _paths(root).root; target = _path(destination)
        if target == source or source in target.parents or target in source.parents:
            raise ValueError('export must be outside the active data folder')
        if target.exists(): raise ValueError('export destination already exists')
        target.parent.mkdir(parents=True, exist_ok=True)
        _inventory(source)  # bound/refuse malformed paths before writing reveal history
        _reveal_export(source)
        with tempfile.TemporaryDirectory(prefix='.kirby2-export-', dir=target.parent) as temporary:
            temporary = Path(temporary).resolve(); snapshot = temporary / 'data'
            rows, skipped = _copy(source, snapshot)
            report = _verify_training(snapshot)
            verified_rows, _ = _inventory(snapshot)
            # Domain verification can only add conservative inspection records.
            # Copy those back through the owning append-only publishers before
            # final capture, so source and export retain the same reveal ledger.
            for identifier in store.scan('reveal-', str(snapshot)):
                store.publish(store.load(identifier, 'reveal-', str(snapshot)), str(source))
            shutil.rmtree(snapshot)
            rows, skipped = _copy(source, snapshot)
            if rows != verified_rows:
                raise ValueError('source changed after semantic verification; no export published')
            package = temporary / 'package'; package.mkdir()
            backup = create_backup(paths=DataPaths(snapshot), selection=BackupSelectionV1.all_portable(), destination=package/'backup')
            _write(package/'README.txt', README.encode())
            try: version = importlib.metadata.version('kirby2')
            except importlib.metadata.PackageNotFoundError: version = '0.1.0-source'
            body = dict(schema_id=SCHEMA, backup_sha256=backup.manifest.sha256,
                        readme_sha256=_sha(README.encode()), files=rows, units=UNITS,
                        producer_version=version, excluded_operational_objects=skipped,
                        privacy='NOT_ENCRYPTED; editable notes included',
                        reveal_policy='EXPORTED_RESERVED_CASES_ARE_REVEALED')
            manifest = dict(body, export_id='training-export-'+_sha(_bytes(body)))
            _write(package/'export.json', _bytes(manifest))
            if _inventory(source)[0] != rows: raise ValueError('source changed before export publication')
            _path(target)
            if target.exists(): raise ValueError('export destination appeared during verification')
            os.rename(package, target); _sync_directory(target.parent)
        return dict(status='EXPORTED', export_root=str(target), export_id=manifest['export_id'], **report)
    finally: _LOCK.release()


def _verified_copy(export_root, temporary):
    source = _path(export_root)
    # Bound the entire transport BEFORE the generic backup parser reads it.
    total = count = 0
    for base, directories, names in os.walk(source, followlinks=False):
        for name in directories + names:
            path = Path(base)/name; mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('export contains a link or non-regular object')
            count += 1
            if count > MAX_FILES * 4: raise ValueError('export file budget exceeded')
            if stat.S_ISREG(mode):
                size = path.stat().st_size; total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES * 2:
                    raise ValueError('export byte budget exceeded')
    if set(p.name for p in source.iterdir()) != {'backup','README.txt','export.json'}:
        raise ValueError('export is incomplete or contains unexpected files')
    copied = temporary / 'package'; copied.mkdir()
    copied_bytes = copied_count = 0
    def copy_bounded(source_dir, target_dir):
        nonlocal copied_bytes, copied_count
        with os.scandir(source_dir) as items:
            for item in items:
                path = Path(item.path); mode = path.lstat().st_mode
                copied_count += 1
                if copied_count > MAX_FILES * 4: raise ValueError('transport grew past file budget')
                target = target_dir / item.name
                if stat.S_ISDIR(mode):
                    target.mkdir(); copy_bounded(path, target)
                elif stat.S_ISREG(mode):
                    raw = _read(path); copied_bytes += len(raw)
                    if copied_bytes > MAX_TOTAL_BYTES * 2: raise ValueError('transport grew past byte budget')
                    _write(target, raw)
                else: raise ValueError('transport changed to a link or non-regular object')
    copy_bounded(source, copied)
    # verify_backup refuses copied links too, including a source race.
    manifest = _decode(_read(copied/'export.json'))
    fields = {'schema_id','backup_sha256','readme_sha256','files','units','producer_version',
              'excluded_operational_objects','privacy','reveal_policy','export_id'}
    if type(manifest) is not dict or set(manifest) != fields or manifest['schema_id'] != SCHEMA or manifest['units'] != UNITS:
        raise ValueError('unsupported export contract')
    if (manifest['privacy'] != 'NOT_ENCRYPTED; editable notes included'
            or manifest['reveal_policy'] != 'EXPORTED_RESERVED_CASES_ARE_REVEALED'
            or type(manifest['producer_version']) is not str or len(manifest['producer_version']) > 128):
        raise ValueError('unsupported export policy')
    body = {k:v for k,v in manifest.items() if k != 'export_id'}
    if manifest['export_id'] != 'training-export-'+_sha(_bytes(body)):
        raise ValueError('export manifest digest differs')
    if manifest['readme_sha256'] != _sha(_read(copied/'README.txt')):
        raise ValueError('export readme differs')
    backup = verify_backup(copied/'backup')
    if backup.manifest.sha256 != manifest['backup_sha256']: raise ValueError('export backup differs')
    entries = {}
    for entry in backup.manifest.entries:
        relative = entry.area_id.value+'/'+entry.relative_path
        if entry.disposition.value != 'INCLUDED' or not any(relative.startswith(tree+'/') for tree in TREES):
            raise ValueError('export contains unsupported or external dependencies')
        entries[relative] = dict(sha256=entry.sha256, bytes=entry.byte_count)
    if entries != manifest['files']: raise ValueError('backup selection differs from training inventory')
    restored = temporary / 'verified-data'
    restore_backup(backup_root=copied/'backup', destination_paths=DataPaths(restored))
    rows, _ = _inventory(restored)
    if rows != manifest['files']: raise ValueError('export dependency inventory differs')
    report = _verify_training(restored)
    # Missing conservative reveal records must not become unfamiliar on recovery.
    _reveal_export(restored)
    if _inventory(restored)[0] != rows: raise ValueError('export lost required reveal lineage')
    return copied/'backup', manifest, report


def verify_training_export(export_root):
    with tempfile.TemporaryDirectory(prefix='kirby2-export-verify-') as temporary:
        _, manifest, report = _verified_copy(export_root, Path(temporary).resolve())
    return dict(status='VERIFIED', export_id=manifest['export_id'], **report)


def recover_training(export_root, destination):
    if not _LOCK.acquire(blocking=False): raise ValueError('another export/recovery is running')
    try:
        target = _path(destination)
        if target.exists(): raise ValueError('recovery requires a NEW data folder; nothing overwritten')
        with tempfile.TemporaryDirectory(prefix='kirby2-export-recover-') as temporary:
            backup, manifest, report = _verified_copy(export_root, Path(temporary).resolve())
            result = restore_backup(backup_root=backup, destination_paths=DataPaths(target))
        return dict(status='RECOVERED', data_root=str(target), export_id=manifest['export_id'],
                    restore=result.as_dict(), **report)
    finally: _LOCK.release()
