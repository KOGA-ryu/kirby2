"""Independent C7 cases: real execution, exposure, conditions and recovery."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.ui import curriculum as c, curriculum_cases as cases, curriculum_store as ledger
from kirby2.ui import playbook_store as store
from kirby2.full_day.models import canonical_sha256
from kirby2.curriculum.skills import SKILL_GRAPH_V1


class CurriculumAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=str(Path(self.temp.name).resolve())
        self.d=c.prepare_protocol(self.root)

    def tearDown(self):self.temp.cleanup()

    def begin(self,strand='pending',form='JOINED',phase='PRACTICE',mode='UNASSISTED',repeat=None,kind='CLINIC'):
        self.d=c.begin(self.d['revision'],dict(phase=phase,recipe_id=strand,form=form,mode=mode,repeat_attempt=repeat,kind=kind),self.root)
        return self.d['active']

    def submit(self,value,actor='HUMAN_DECLARED'):
        a=self.d['active'];self.d=c.answer(self.d['revision'],a['attempt_id'],a['case']['step'],value,actor,self.root)
        return self.d

    def finish(self,actor='HUMAN_DECLARED'):
        responses=[];a=self.d['active'];spec=a['case']['specification']
        while self.d['active']:
            r=cases.simulate(spec,responses);value=r['question']['expected']
            responses.append(dict(value=value,actor=actor));self.submit(value,actor)
        return c.open_attempt(a['attempt_id'],self.root)

    def abort(self,status='ABORTED'):
        a=self.d['active'];self.d=c.end(self.d['revision'],a['attempt_id'],status,self.root)

    def test_canonical_mapping_and_deterministic_selector(self):
        self.assertEqual(len(cases.catalog()),6)
        for row in cases.catalog():
            self.assertIn(row['skill_id'],SKILL_GRAPH_V1.skills)
            self.assertEqual(row['prerequisites'],{s:list(SKILL_GRAPH_V1.prerequisites(s)) for s in [row['skill_id']]+row['supporting_skills']})
        self.begin('pending','ISOLATED');self.submit(dict(confirmed=0,possible=[0,0]))
        first=c.dashboard(self.root);second=c.dashboard(self.root)
        self.assertEqual(first['ranking'],second['ranking']);self.assertEqual(first['ranking'][0]['recipe_id'],'pending')
        self.assertEqual(first['ranking'][0]['recent_misses'],1)
        self.begin('research','ISOLATED');self.assertEqual(self.d['active']['case']['specification']['strand'],'research')
        self.abort();self.assertEqual(c.dashboard(self.root)['ranking'][0]['recipe_id'],'pending')

    def test_join_actual_partial_fill_cancel_confirmation_and_lead_in(self):
        self.begin();a=self.d['active'];case=a['case'];spec=case['specification']
        self.assertGreaterEqual(len(case['lead_in']),3)
        account=case['knowledge']['account'];self.assertEqual(account['confirmed_position'],300)
        self.assertEqual(account['possible_position'],[300,spec['quantity']])
        self.submit(dict(confirmed=300,possible=[300,spec['quantity']]))
        self.submit(dict(action='CANCEL',payload=dict(order_id='EX-PLAYER-1')))
        case=self.d['active']['case'];self.assertEqual(case['knowledge']['account']['possible_position'],[300,300])
        self.assertFalse(case['knowledge']['account']['orders']['EX-PLAYER-1']['pending_cancel'])
        self.submit(dict(confirmed=300,possible=[300,300]))
        opened=c.open_attempt(a['attempt_id'],self.root)
        self.assertEqual([r['correct'] for r in opened['case']['results']],[True,True,True])
        self.assertEqual([r['actor'] for r in opened['case']['trace'] if r['action']=='SUBMIT'],['PREPARATION'])
        self.assertEqual(opened['summary']['opportunities'],1)
        self.assertEqual({e['skill_id'] for e in opened['summary']['skill_evidence']},{'PARTIAL_FILL_MANAGEMENT','POSITION_MANAGEMENT','HOTKEY_ACCURACY'})
        bad=dict(spec,lead_in='REMOVED')
        with self.assertRaises(ValueError):cases.simulate(bad)

    def test_wrong_control_reaches_engine_and_never_changes_grade_to_profit(self):
        self.begin('control','JOINED');self.submit(dict(action='SUBMIT',payload=dict(side='buy',quantity=100,price_ticks=10000)))
        case=self.d['active']['case'];self.assertEqual(case['knowledge']['account']['confirmed_position'],100)
        self.submit(dict(confirmed=100,possible=[100,100]))
        summary=self.d['history'][-1];self.assertEqual(summary['correct'],0);self.assertEqual(summary['opportunities'],1)
        result=c.open_attempt(summary['attempt_id'],self.root)['case']['results']
        self.assertFalse(result[0]['correct']);self.assertTrue(result[1]['correct'])

    def test_reserved_no_hints_no_preclosure_review_or_answer(self):
        a=self.begin(phase='BASELINE');self.assertEqual(a['mode'],'UNASSISTED')
        self.assertNotIn('expected',a['case']['question']);self.assertNotIn('results',a['case'])
        with self.assertRaises(ValueError):c.hint(self.d['revision'],a['attempt_id'],0,self.root)
        with self.assertRaises(ValueError):c.open_attempt(a['attempt_id'],self.root)
        self.abort()
        self.assertEqual(self.d['history'][0]['status'],'ABORTED')

    def test_script_and_assistance_cannot_inflate_manual_results(self):
        self.begin('pending','ISOLATED',mode='GUIDED');a=self.d['active']
        self.d=c.hint(self.d['revision'],a['attempt_id'],0,self.root)
        self.assertIn('expected',self.d['active']['case']['question'])
        self.finish('SCRIPT');row=self.d['history'][-1]
        self.assertEqual((row['correct'],row['opportunities'],row['scripted'],row['assistance']),(0,0,1,1))
        self.assertEqual(self.d['human_learning'],'NOT_EXERCISED')
        self.begin('pending','ISOLATED');self.finish()
        self.assertEqual(len(self.d['progress']),2)
        self.assertTrue(all(g['uncertainty']=='INSUFFICIENT_FOR_LEARNING_OR_MASTERY_CLAIM' for g in self.d['progress']))

    def test_familiarity_repeat_form_alias_and_cross_chapter_reveal(self):
        self.begin('pending','ISOLATED');spec=self.d['active']['case']['specification'];attempt=self.d['active']['attempt_id'];self.abort()
        self.begin(repeat=attempt);self.assertEqual(self.d['active']['familiarity'],'REHEARSAL')
        self.assertEqual(self.d['active']['case']['specification'],spec);self.abort()
        self.assertEqual(cases.group_id(spec),cases.group_id(dict(spec,form='JOINED')))
        with ledger.locked(self.root) as path:state=c._state(ledger.read(path))
        baseline=state['plan']['bank']['BASELINE'][0]
        store.reveal(store.lineage(cases.RECIPE,baseline['seed']),'INSPECTED',self.root)
        self.d=c.dashboard(self.root);a=self.begin(phase='BASELINE')
        self.assertEqual(a['familiarity'],'REHEARSAL')
        self.assertIn(store.lineage(cases.RECIPE,baseline['seed']),store.reveals(self.root))

    def test_cold_reopen_pending_answer_and_rehashed_forgery(self):
        a=self.begin();payload=self.d
        code='from kirby2.ui.curriculum import dashboard; import json,sys; print(json.dumps(dashboard(sys.argv[1])))'
        raw=subprocess.check_output([sys.executable,'-B','-c',code,self.root],text=True)
        reopened=json.loads(raw);self.assertEqual(reopened['active'],payload['active'])
        self.finish();directory=ledger.directory(self.root);target=directory/'000002.json';row=json.loads(target.read_text())
        row['payload']['familiarity']='UNFAMILIAR_CASE'
        row['event_id']='curriculum-event-'+canonical_sha256({k:v for k,v in row.items() if k!='event_id'})
        # A fully rehashed chain still cannot reclassify practice as fresh.
        target.write_text(json.dumps(row));parent=row['event_id']
        for p in sorted(directory.glob('*.json'))[2:]:
            v=json.loads(p.read_text());v['parent']=parent;v['event_id']='curriculum-event-'+canonical_sha256({k:x for k,x in v.items() if k!='event_id'});p.write_text(json.dumps(v));parent=v['event_id']
        with self.assertRaises(ValueError):c.dashboard(self.root)

    def test_stale_response_double_submit_and_storage_symlink(self):
        self.begin('observation','ISOLATED');a=self.d['active'];old=self.d['revision']
        self.submit(2)
        with self.assertRaises(ValueError):c.answer(old,a['attempt_id'],0,2,'HUMAN_DECLARED',self.root)
        self.assertEqual(len(c.dashboard(self.root)['history']),1)
        directory=ledger.directory(self.root);p=directory/'000001.json';raw=p.read_bytes();p.unlink()
        elsewhere=Path(self.root)/'other.json';elsewhere.write_bytes(raw);p.symlink_to(elsewhere)
        with self.assertRaises(ValueError):c.dashboard(self.root)

    def test_silent_ambiguous_and_boolean_answers_keep_denominators(self):
        for seed,status in ((3,'NO_OPPORTUNITY'),(6,'AMBIGUOUS')):
            spec=cases.specification('selection',seed);r=cases.simulate(spec)
            self.assertEqual(r['question']['opportunity'],status)
            done=cases.simulate(spec,[dict(value=r['question']['expected'],actor='HUMAN_DECLARED')])
            self.assertTrue(done['results'][0]['correct'])
            self.assertEqual(done['results'][0]['opportunity'],status)
        # Strict JSON identity, not Python bool/int coercion.
        spec=cases.specification('control',0,'JOINED')
        r=cases.simulate(spec,[dict(value=dict(action='NO_ACTION',payload={}),actor='SCRIPT'),dict(value=dict(confirmed=False,possible=[False,False]),actor='HUMAN_DECLARED')])
        self.assertFalse(r['results'][-1]['correct'])

    def test_protocol_populations_retention_clock_and_exclusion_accounting(self):
        with patch.object(c,'_now',return_value=2000000000):
            # Start fresh to bind a completely controlled local schedule.
            other=str((Path(self.root)/'clock').resolve());self.root=other;self.d=c.prepare_protocol(other)
            with ledger.locked(other) as path:plan=c._state(ledger.read(path))['plan']
            for index in range(6):
                b,f,r=[plan['bank'][phase][index] for phase in ('BASELINE','FRESH','RETENTION')]
                self.assertEqual(b['selection_condition'],f['selection_condition'])
                self.assertNotEqual(b['quantity'],f['quantity']);self.assertNotEqual(b['preparation_offset_us'],f['preparation_offset_us'])
                self.assertEqual(len({cases.group_id(x) for x in (b,f,r)}),3)
            for _ in range(6):self.begin(phase='BASELINE');self.abort('SYSTEM_FAILURE')
            self.d=c.advance_phase(self.d['revision'],other);self.assertEqual(self.d['phase'],'PRACTICE')
            self.begin('observation','ISOLATED');self.finish('SCRIPT')
            self.d=c.advance_phase(self.d['revision'],other)
            for _ in range(6):self.begin(phase='FRESH');self.abort()
            self.d=c.advance_phase(self.d['revision'],other)
            self.assertFalse(self.d['retention_available'])
            before_reveals=store.reveals(other)
            with self.assertRaises(ValueError):self.begin(phase='RETENTION')
            self.assertEqual(store.reveals(other),before_reveals)
        with patch.object(c,'_now',return_value=2000086400):
            self.d=c.dashboard(other);self.assertTrue(self.d['retention_available']);self.begin(phase='RETENTION');self.abort()
        with patch.object(c,'_now',return_value=1999999999):
            self.d=c.dashboard(other);self.assertFalse(self.d['retention_available'])
            with self.assertRaises(ValueError):self.begin('observation','ISOLATED')
        self.assertEqual(sum(r['status']=='SYSTEM_FAILURE' for r in self.d['history']),6)

    def test_clock_rollback_writes_no_exposure_and_stale_quotes_cannot_authorize_entry(self):
        before=store.reveals(self.root)
        with patch.object(c,'_now',return_value=1):
            with self.assertRaises(ValueError):self.begin('observation','ISOLATED')
        self.assertEqual(store.reveals(self.root),before)
        self.assertFalse(c.dashboard(self.root)['history'])
        spec=cases.specification('selection',0)
        knowledge=cases.simulate(spec)['knowledge'];knowledge['market_status']='STALE'
        with patch.object(cases.execution,'_knowledge',return_value=knowledge):
            question=cases._question(spec,0,object())
        self.assertEqual(question['expected'],'INSUFFICIENT')
        self.assertEqual(question['opportunity'],'AMBIGUOUS')

    def test_semantic_hash_prevents_silent_regrading(self):
        self.begin('observation','ISOLATED');self.submit(2)
        original=cases.simulate
        def changed(spec,responses=()):
            result=original(spec,responses)
            result['scientific_sha256']='f'*64
            return result
        with patch.object(cases,'simulate',side_effect=changed):
            with self.assertRaises(ValueError):c.dashboard(self.root)

    def test_workflow_binding_does_not_accept_an_unrelated_saved_world(self):
        from kirby2.ui import radar_session as radar, radar_evidence
        a=self.begin(kind='WORKFLOW');launch=c.workflow_launch(a['attempt_id'],self.root)
        world,frame=radar.start_world(**launch)
        try:
            self.d=c.bind_workflow(self.d['revision'],a['attempt_id'],frame['world_id'],self.root)
            radar.act_world(world,frame['frame_id'],'freeze','FREEZE',{})
            saved=radar_evidence.save_world(world,self.root)
            with self.assertRaises(ValueError):c.attach_workflow(self.d['revision'],'wrong',saved['evidence_id'],self.root)
            self.d=c.attach_workflow(self.d['revision'],a['attempt_id'],saved['evidence_id'],self.root)
            row=self.d['history'][-1];self.assertEqual(row['status'],'REVIEW_REQUIRED');self.assertEqual(row['opportunities'],0)
            opened=c.open_attempt(a['attempt_id'],self.root);self.assertEqual(opened['world']['evidence_id'],saved['evidence_id'])
        finally:radar.close_world(world)


if __name__=='__main__':unittest.main()
