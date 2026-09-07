"""Chapter 2 public persistence and failure-boundary acceptance (no Qt)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.ui import (
    begin_simulation_practice_attempt, build_practice_attempt_request,
    build_practice_action_request, submit_simulation_practice_action,
    finalize_simulation_run, resolve_replay_artifact, build_replay_provider,
    release_simulation_episode, dispatch_simulation_command, advance_simulation_run,
)
from kirby2.ui.simulation_contract import canonical_digest
from kirby2.ui.practice_library import (
    save_practice_evidence, open_practice_evidence, list_practice_evidence,
    read_practice_note, write_practice_note, build_saved_practice_repeat_request,
)


def produce(root: str, *, finished: bool = True, complete: bool = False) -> dict:
    handle, result = begin_simulation_practice_attempt(build_practice_attempt_request(
        episode_id="practice.f3.cancel-partial-residual.v1", mode="UNASSISTED"))
    try:
        if finished:
            frame = result["current_frame"]
            result = submit_simulation_practice_action(handle, build_practice_action_request(
                attempt_id=result["attempt"]["attempt_id"], hold_id=None,
                source_run_id=frame["source_run_id"], origin_frame_id=frame["frame_id"],
                origin_cursor_id=frame["cursor"]["cursor_id"], operation="UNASSISTED",
                response_kind="SEMANTIC_ACTION", semantic_action_id="PLAYER_CANCEL_NEAREST"))
        frame = result["current_frame"]
        if complete:
            basis = {"schema_id":"KIRBY2_SIMULATION_COMMAND_REQUEST_V1", "schema_version":1,
                     "source_run_id":frame["source_run_id"], "origin_frame_id":frame["frame_id"],
                     "origin_cursor_id":frame["cursor"]["cursor_id"], "semantic_action_id":"SIMULATION_PLAY", "parameters":{}}
            frame = dispatch_simulation_command(handle, {**basis, "command_id":"simulation-command-"+canonical_digest(basis)[:24]})["destination_frame"]
            frame = advance_simulation_run(handle, frame["source_run_id"], frame["frame_id"],
                                            frame["cursor"]["cursor_id"], 90_000_000)["destination_frame"]
        final = finalize_simulation_run(handle, frame["source_run_id"], frame["frame_id"],
                                        frame["cursor"]["cursor_id"], "COMPLETE_ONLY" if complete else "ALLOW_PARTIAL")
        ref = final["run_result"]["replay_artifact"]
        entry = save_practice_evidence(ref, result["attempt"]["attempt_id"], root)
        assert save_practice_evidence(ref, result["attempt"]["attempt_id"], root) == entry
        return entry
    finally:
        release_simulation_episode(handle)


class PracticeLibraryAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.temp.name).resolve())

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_process_complete_and_partial_replay(self):
        writer = "from kirby2.audit.practice_library import produce; import sys,json; print(json.dumps([produce(sys.argv[1],complete=True),produce(sys.argv[1],finished=False)]))"
        entries = json.loads(subprocess.check_output([sys.executable,"-B","-c",writer,self.root],text=True))
        reader = """
import json,sys
from kirby2.ui.practice_library import list_practice_evidence,open_practice_evidence
from kirby2.ui import build_replay_provider,resolve_replay_artifact
entries=list_practice_evidence(sys.argv[1])['entries']
for entry in entries:
 source, record=open_practice_evidence(entry['evidence_id'],sys.argv[1])
 assert resolve_replay_artifact(record['reference'])[0] is None
 frame=build_replay_provider(source).initial_frame()
 assert frame['identity']['source_event_sha256']==entry['artifact_sha256']
print(json.dumps(entries))
"""
        reopened = json.loads(subprocess.check_output([sys.executable,"-B","-c",reader,self.root],text=True))
        self.assertEqual(sorted(entries,key=lambda x:x['evidence_id']), reopened)
        self.assertEqual({r['simulation_status'] for r in reopened},{'COMPLETE','SAVED_PARTIAL'})
        self.assertEqual({r['exercise_status'] for r in reopened},{'FINISHED','PARTIAL'})

    def test_interrupted_activation_and_retry_preserve_original(self):
        original = produce(self.root)
        from kirby2.ui import practice_library as library
        real_write = library._write
        def fail(path, raw):
            if path.name == 'manifest.json':
                raise OSError('injected interrupted activation')
            real_write(path,raw)
        with patch.object(library,'_write',side_effect=fail):
            with self.assertRaises(OSError):
                produce(self.root,finished=False)
        self.assertEqual(list_practice_evidence(self.root)['entries'],[original])
        self.assertEqual(produce(self.root,finished=False)['exercise_status'],'PARTIAL')

    def test_process_death_before_and_after_activation(self):
        original = produce(self.root)
        script = """
import os,sys
from kirby2.ui import practice_library as library
from kirby2.audit.practice_library import produce
real=library._sync_directory
def interrupt(path):
 real(path)
 if (sys.argv[2]=='before' and path.name.startswith('.pending-')) or (sys.argv[2]=='after' and path.name=='practice-library'):
  os._exit(73)
library._sync_directory=interrupt
produce(sys.argv[1],finished=False)
"""
        for boundary, count in [('before',1),('after',2)]:
            result=subprocess.run([sys.executable,'-B','-c',script,self.root,boundary],check=False)
            self.assertEqual(result.returncode,73)
            catalog=list_practice_evidence(self.root)
            self.assertEqual(len(catalog['entries']),count)
            self.assertEqual(catalog['rejected'],[])
            self.assertIn(original,catalog['entries'])

    def test_saved_repeat_preserves_guided_disclosures_across_processes(self):
        request=build_practice_attempt_request(episode_id='practice.f2.public-pressure.v1')
        entry=produce_guided(self.root,request)
        _,original=open_practice_evidence(entry['evidence_id'],self.root)
        self.assertIn('FAIL',[r['assessment']['outcome'] for r in original['dependencies']['history'] if r['assessment']])
        script="""
import sys,json
from kirby2.ui.practice_library import build_saved_practice_repeat_request,open_practice_evidence
from kirby2.audit.practice_library import produce_guided
request=build_saved_practice_repeat_request(sys.argv[2],sys.argv[1])
entry=produce_guided(sys.argv[1],request)
_,record=open_practice_evidence(entry['evidence_id'],sys.argv[1])
print(json.dumps(record))
"""
        restored=json.loads(subprocess.check_output([sys.executable,'-B','-c',script,self.root,entry['evidence_id']],text=True))
        self.assertEqual(restored['summary']['prior_attempt_id'],entry['attempt_id'])
        self.assertNotEqual(restored['summary']['attempt_id'],entry['attempt_id'])
        self.assertEqual(restored['dependencies']['ancestors'],original['dependencies']['history'])

    def test_corruption_and_unsupported_manifest_refuse(self):
        entry = produce(self.root)
        directory = Path(self.root)/'evidence'/'practice-library'/entry['evidence_id']
        path = directory/'practice.json'
        original = path.read_bytes()
        path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'digest'):
            open_practice_evidence(entry['evidence_id'],self.root)
        self.assertEqual(len(list_practice_evidence(self.root)['rejected']),1)
        path.write_bytes(original)
        manifest=directory/'manifest.json'
        manifest_bytes=manifest.read_bytes()
        manifest.write_bytes(manifest_bytes.replace(b'{',b'{"schema_version":1,',1))
        with self.assertRaisesRegex(ValueError,'duplicate'):
            open_practice_evidence(entry['evidence_id'],self.root)
        manifest.write_bytes(manifest_bytes)
        data=json.loads(manifest.read_text())
        data['schema_version']=True; manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'schema'):
            open_practice_evidence(entry['evidence_id'],self.root)

    def test_notes_optimistic_revision_and_repeat_lineage(self):
        entry=produce(self.root)
        _, before=open_practice_evidence(entry['evidence_id'],self.root)
        note=read_practice_note(entry['evidence_id'],self.root)
        changed=write_practice_note(entry['evidence_id'],'Check filled position',note['revision'],self.root)
        with self.assertRaisesRegex(ValueError,'changed'):
            write_practice_note(entry['evidence_id'],'stale',note['revision'],self.root)
        write_practice_note(entry['evidence_id'],'',changed['revision'],self.root)
        self.assertEqual(before,open_practice_evidence(entry['evidence_id'],self.root)[1])
        request=build_saved_practice_repeat_request(entry['evidence_id'],self.root)
        handle,result=begin_simulation_practice_attempt(request)
        try:
            self.assertEqual(result['attempt']['prior_attempt_id'],entry['attempt_id'])
            self.assertNotEqual(result['attempt']['attempt_id'],entry['attempt_id'])
        finally:
            release_simulation_episode(handle)

    def test_inventory_rebuild_and_review_only_recipe(self):
        entry=produce(self.root)
        cache=Path(self.root)/'cache';cache.mkdir()
        (cache/'index.json').write_text('broken')
        self.assertEqual(list_practice_evidence(self.root)['entries'],[entry])
        from kirby2.ui import practice_library as library
        with patch.object(library,'get_practice_episode_v1',side_effect=ValueError('unknown version')):
            source,record=open_practice_evidence(entry['evidence_id'],self.root)
            self.assertIsNotNone(source)
            self.assertFalse(record['summary']['can_repeat'])
            with self.assertRaisesRegex(ValueError,'Review only'):
                build_saved_practice_repeat_request(entry['evidence_id'],self.root)

    def test_symlink_and_wrong_evidence_identity_refuse(self):
        entry=produce(self.root)
        parent=Path(self.root)/'evidence'/'practice-library'
        alias='practice-evidence-'+'0'*64
        (parent/alias).symlink_to(parent/entry['evidence_id'],target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink'):
            open_practice_evidence(alias,self.root)
        with self.assertRaises(ValueError):
            open_practice_evidence('../'+entry['evidence_id'],self.root)


def produce_guided(root: str, request: dict) -> dict:
    handle, result=begin_simulation_practice_attempt(request)
    try:
        for operation,kind in [('STAGE','REPLENISHMENT_BLOCKS'),('STAGE','PRESSURE_PRESENT'),('CONTINUE','PRESSURE_PRESENT')]:
            frame=result['current_frame']
            result=submit_simulation_practice_action(handle,build_practice_action_request(
                attempt_id=result['attempt']['attempt_id'],hold_id=result['hold_id'],
                source_run_id=frame['source_run_id'],origin_frame_id=frame['frame_id'],
                origin_cursor_id=frame['cursor']['cursor_id'],operation=operation,response_kind=kind))
            assert result['status']=='AVAILABLE',result
        frame=result['current_frame']
        final=finalize_simulation_run(handle,frame['source_run_id'],frame['frame_id'],frame['cursor']['cursor_id'],'ALLOW_PARTIAL')
        return save_practice_evidence(final['run_result']['replay_artifact'],result['attempt']['attempt_id'],root)
    finally:
        release_simulation_episode(handle)


if __name__ == '__main__':
    unittest.main()
