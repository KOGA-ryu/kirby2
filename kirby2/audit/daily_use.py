"""Chapter 8 portable recovery and hostile-boundary acceptance. No Qt/images."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kirby2.ui import daily_use as daily, playbook_lab as lab, playbooks, playbook_store as store
from kirby2.ui import curriculum, curriculum_store as ledger, curriculum_cases as cases
from kirby2.ui.market_workbench import save_market_configuration
from kirby2.ui.practice_library import list_practice_evidence, write_practice_note, read_practice_note
from kirby2.audit.practice_library import produce


def seed_training(root):
    """Public persisted routes; an incomplete job is intentionally closed."""
    root=str(root)
    complete=produce(root,complete=True);partial=produce(root,finished=False)
    note=read_practice_note(complete['evidence_id'],root)
    write_practice_note(complete['evidence_id'],'Wait for the cancellation report.',note['revision'],root)
    profile=save_market_configuration('market.replenishment.v1',11,root)
    sources=[v['source'] for v in playbooks.playbook_catalog()['templates']]
    spec=dict(title='Two frozen policies',variants=sources,cases=[dict(recipe_id='execution.cancel-race.v1',seed=11,role='RESERVED')])
    job,planned=lab.prepare_study(spec,root)
    try:
        while lab.advance_study(job)['terminal_count'] < planned['planned_count']:pass
    finally:lab.close_study(job)
    incomplete,unfinished=lab.prepare_study(dict(spec,title='Interrupted before first cell'),root)
    lab.close_study(incomplete)
    curriculum.prepare_protocol(root)
    return dict(complete=complete,partial=partial,study=planned['study_id'],incomplete=unfinished['study_id'],profile=profile['saved_configuration_id'])


class DailyUseAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kirby2-c8-')
        self.base=Path(self.temp.name).resolve();self.root=self.base/'data';self.package=self.base/'export'
    def tearDown(self):
        self.assertFalse(lab._ACTIVE)
        self.temp.cleanup()
    def test_completed_partial_notes_dependencies_and_cold_recovery(self):
        original=seed_training(self.root)
        exported=daily.export_training(str(self.package),str(self.root))
        self.assertEqual(exported['status'],'EXPORTED')
        target=self.base/'recovered'
        code='from kirby2.ui.daily_use import recover_training; import sys,json; print(json.dumps(recover_training(sys.argv[1],sys.argv[2])))'
        recovered=json.loads(subprocess.check_output([sys.executable,'-B','-c',code,str(self.package),str(target)],text=True))
        self.assertFalse(recovered['live_authority_restored'])
        self.assertEqual({r['simulation_status'] for r in list_practice_evidence(str(target))['entries']},{'COMPLETE','SAVED_PARTIAL'})
        self.assertEqual(read_practice_note(original['complete']['evidence_id'],str(target))['text'],'Wait for the cancellation report.')
        self.assertEqual(lab.open_study(original['incomplete'],str(target))['status'],'INCOMPLETE')
        self.assertEqual(store.reveals(str(self.root)),store.reveals(str(target)))
        self.assertEqual(daily.inspect_training(str(target))['counts']['market_configurations'],1)
    def test_reserved_curriculum_seeds_revealed_on_both_sides(self):
        curriculum.prepare_protocol(str(self.root))
        daily.export_training(str(self.package),str(self.root))
        daily.recover_training(str(self.package),str(self.base/'restored'))
        with ledger.locked(str(self.root)) as path:plan=curriculum._state(ledger.read(path))['plan']
        for root in (self.root,self.base/'restored'):
            for bank in plan['bank'].values():
                for spec in bank:self.assertIn('EXPORTED',store.reveals(str(root))[store.lineage(cases.RECIPE,spec['seed'])])
    def test_tamper_truncation_unknown_version_extra_object_refused(self):
        produce(str(self.root),finished=False)
        daily.export_training(str(self.package),str(self.root))
        for kind in ('tamper','truncated','schema','extra'):
            path=self.base/kind;shutil.copytree(self.package,path)
            if kind=='extra':(path/'unexpected.txt').write_text('extra')
            elif kind=='schema':
                value=daily._decode(daily._read(path/'export.json'));value['schema_id']='FUTURE'
                (path/'export.json').write_bytes(daily._bytes(value))
            else:
                item=next(p for p in (path/'backup/objects').rglob('*') if p.is_file())
                raw=item.read_bytes();item.write_bytes(raw[:-1] if kind=='truncated' else raw+b'x')
            with self.assertRaises((ValueError,RuntimeError)):
                daily.recover_training(str(path),str(self.base/('recovered-'+kind)))
            self.assertFalse((self.base/('recovered-'+kind)).exists())
    def test_source_edit_during_capture_refused(self):
        entry=produce(str(self.root),finished=False)
        note=read_practice_note(entry['evidence_id'],str(self.root))
        write_practice_note(entry['evidence_id'],'before',note['revision'],str(self.root))
        original=daily._write;changed=[False]
        def write(path,raw):
            original(path,raw)
            if not changed[0] and '/data/' in str(path) and path.suffix=='.json':
                changed[0]=True
                note=read_practice_note(entry['evidence_id'],str(self.root))
                write_practice_note(entry['evidence_id'],'after',note['revision'],str(self.root))
        with patch.object(daily,'_write',side_effect=write),self.assertRaisesRegex(ValueError,'source changed'):
            daily.export_training(str(self.package),str(self.root))
        self.assertFalse(self.package.exists())
    def test_existing_destination_and_symlink_refused(self):
        daily.export_training(str(self.package),str(self.root))
        target=self.base/'keep';target.mkdir();(target/'personal').write_text('keep')
        with self.assertRaises(ValueError):daily.recover_training(str(self.package),str(target))
        self.assertEqual((target/'personal').read_text(),'keep')
        link=self.base/'link';link.symlink_to(self.package,target_is_directory=True)
        with self.assertRaises(ValueError):daily.verify_training_export(str(link))
        (self.package/'extra').symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):daily.verify_training_export(str(self.package))
    def test_interrupted_publication_never_creates_success(self):
        entry=produce(str(self.root),finished=False)
        rename=daily.os.rename
        def fail(src,dst):
            if Path(dst)==self.package:raise OSError('interrupted export activation')
            return rename(src,dst)
        with patch.object(daily.os,'rename',side_effect=fail),self.assertRaises(OSError):
            daily.export_training(str(self.package),str(self.root))
        self.assertFalse(self.package.exists())
        self.assertEqual(list_practice_evidence(str(self.root))['entries'][0]['evidence_id'],entry['evidence_id'])
        self.assertEqual(daily.export_training(str(self.package),str(self.root))['status'],'EXPORTED')
    def test_lost_disposable_index_and_pending_debris_do_not_become_attempts(self):
        produce(str(self.root),finished=False)
        pending=self.root/'evidence/practice-library/.pending-interrupted';pending.mkdir();(pending/'partial').write_text('partial')
        index=self.root/'cache';index.mkdir();(index/'index.json').write_text('not authoritative');shutil.rmtree(index)
        result=daily.inspect_training(str(self.root))
        self.assertEqual(result['counts']['practice'],1)
        self.assertIn('evidence/practice-library/.pending-interrupted',result['excluded_operational_objects'])
        self.assertTrue(pending.exists())
    def test_incompatible_scientific_evidence_is_not_replaced(self):
        entry=produce(str(self.root),finished=False)
        bundle=self.root/'evidence/practice-library'/entry['evidence_id']/'practice.json'
        raw=bundle.read_bytes();bundle.write_bytes(b'{}\n')
        with self.assertRaises(ValueError):daily.inspect_training(str(self.root))
        self.assertEqual(bundle.read_bytes(),b'{}\n')
    def test_explicit_workload_limit_refuses_without_dropping(self):
        produce(str(self.root),finished=False)
        with patch.object(daily,'MAX_FILES',1),self.assertRaisesRegex(ValueError,'workload'):
            daily.export_training(str(self.package),str(self.root))
        self.assertFalse(self.package.exists())
        self.assertEqual(len(list_practice_evidence(str(self.root))['entries']),1)
    def test_inspection_does_not_reveal_original_reserved_cases(self):
        curriculum.prepare_protocol(str(self.root))
        before=daily._inventory(self.root)[0]
        daily.inspect_training(str(self.root))
        self.assertEqual(before,daily._inventory(self.root)[0])
    def test_single_export_owner(self):
        with daily._LOCK:
            with self.assertRaisesRegex(ValueError,'another export'):daily.export_training(str(self.package),str(self.root))
    def test_orphan_unknown_lab_record_refused(self):
        path=self.root/'evidence/playbook-lab-v1';path.mkdir(parents=True)
        (path/'future.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'unsupported Lab'):daily.inspect_training(str(self.root))
    def test_resealed_transport_cannot_remove_reserved_reveal_history(self):
        curriculum.prepare_protocol(str(self.root));daily.export_training(str(self.package),str(self.root))
        candidate=self.base/'candidate'
        daily.restore_backup(backup_root=self.package/'backup',destination_paths=daily.DataPaths(candidate))
        for p in (candidate/'evidence/playbook-lab-v1').glob('reveal-*.json'):p.unlink()
        forged=self.base/'forged';forged.mkdir();shutil.copyfile(self.package/'README.txt',forged/'README.txt')
        backup=daily.create_backup(paths=daily.DataPaths(candidate),selection=daily.BackupSelectionV1.all_portable(),destination=forged/'backup')
        body=daily._decode(daily._read(self.package/'export.json'));body.pop('export_id')
        body['files']=daily._inventory(candidate)[0];body['backup_sha256']=backup.manifest.sha256
        (forged/'export.json').write_bytes(daily._bytes(dict(body,export_id='training-export-'+daily._sha(daily._bytes(body)))))
        with self.assertRaisesRegex(ValueError,'reveal lineage'):
            daily.recover_training(str(forged),str(self.base/'should-not-exist'))
        self.assertFalse((self.base/'should-not-exist').exists())
    def test_unknown_bundle_dependency_is_not_silently_exported(self):
        entry=produce(str(self.root),finished=False)
        (self.root/'evidence/practice-library'/entry['evidence_id']/'extra.txt').write_text('not part of evidence')
        with self.assertRaisesRegex(ValueError,'unexpected dependencies'):
            daily.export_training(str(self.package),str(self.root))
        self.assertFalse(self.package.exists())
    def test_source_changed_after_semantic_verification_is_not_recaptured_as_valid(self):
        entry=produce(str(self.root),finished=False)
        original=daily._verify_training
        def verify(root):
            result=original(root)
            bundle=self.root/'evidence/practice-library'/entry['evidence_id']/'practice.json'
            bundle.write_bytes(b'{}\n')
            return result
        with patch.object(daily,'_verify_training',side_effect=verify),self.assertRaisesRegex(ValueError,'after semantic verification'):
            daily.export_training(str(self.package),str(self.root))
        self.assertFalse(self.package.exists())
