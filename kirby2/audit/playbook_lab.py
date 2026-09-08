"""C5 causal acceptance. Pure backend execution; no Qt or image operations."""
from __future__ import annotations
import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.full_day.models import canonical_sha256
from kirby2.ui import playbooks as rules, playbook_lab as lab, playbook_drills as drills
from kirby2.ui import execution_practice as execution, execution_library as evidence, playbook_store as store
from kirby2.ui.playbook_trials import run_trial, fill_measurement, scientific_projection, grouped_summary


class PlaybookAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kirby2-c5-');self.root=str(Path(self.temp.name).resolve())
        self.sources=[v['source'] for v in rules.playbook_catalog()['templates']]
        self.case=dict(recipe_id='execution.cancel-race.v1',seed=11,role='RESERVED')
    def tearDown(self):
        self.assertEqual(lab._ACTIVE,{})
        self.assertEqual(execution._ACTIVE,{})
        self.temp.cleanup()
    def spec(self,variants=None,cases=None):
        return dict(title='Frozen comparison',variants=variants or self.sources,cases=cases or [self.case])
    def finish(self,job):
        try:
            while True:
                state=lab.advance_study(job)
                if state['terminal_count']==state['planned_count']:return state
        finally:lab.close_study(job)

    def test_one_evaluator_in_preview_practice_and_actual_lab(self):
        source=self.sources[0];preview=rules.preview_playbook(source)
        handle,_=execution.start_execution_practice()
        try:self.assertEqual(rules.evaluate_practice_playbook(handle,source),preview)
        finally:execution.close_execution_practice(handle)
        job,planned=lab.prepare_study(self.spec([source]),self.root)
        self.assertEqual(planned['terminal_count'],0)
        self.finish(job)
        opened=lab.open_study(planned['study_id'],self.root)
        first=opened['cells'][0]['result']['trace'][0]
        self.assertEqual(first['observation'],preview['observation']);self.assertEqual(first['result'],preview['result'])

    def test_form_preserves_source_and_refuses_advanced_missing_policy(self):
        document=json.loads(self.sources[0]);document['rule_source']='# keep my explanation\n'+document['rule_source']
        source=json.dumps(document);edited=rules.edit_playbook_form(source,dict(execution=document['execution'],evaluation=document['evaluation']))
        self.assertEqual(json.loads(edited)['rule_source'],document['rule_source'])
        self.assertEqual(rules.validate_playbook(source)['playbook_id'],rules.validate_playbook(edited)['playbook_id'])
        for bad in (source.replace('REFUSE','AS_ZERO'),source.replace('spread_ticks','relative_volume'),source[:-1]+',"advanced":true}'):
            refused=rules.validate_playbook(bad)
            self.assertEqual(refused['status'],'REFUSED');self.assertEqual(refused['source'],bad)

    def test_expiry_missing_cut_and_private_future_independence(self):
        source=self.sources[0];preview=rules.preview_playbook(source)
        frame={'simulation_time_us':preview['observation']['simulation_time_us'],'knowledge':{'market':preview['observation']['market']},'hidden_future':'first'}
        initial=rules.evaluate_playbook(source,rules.observation(frame))
        frame['hidden_future']={'price':999999,'regime':'perfect'}
        self.assertEqual(rules.evaluate_playbook(source,rules.observation(frame)),initial)
        frame['simulation_time_us']+=15000
        self.assertEqual(rules.evaluate_playbook(source,rules.observation(frame),1000000)['status'],'EXPIRED')
        frame['knowledge']['market']=None
        self.assertEqual(rules.evaluate_playbook(source,rules.observation(frame))['unavailable_reason'],'MISSING_MARKET')
        self.assertIsNone(initial['success_probability'])
        hostile=copy.deepcopy(preview['observation']);hostile['market']['simulation_time_us']=1000001
        hostile['observation_id']='observation-'+canonical_sha256({k:v for k,v in hostile.items() if k!='observation_id'})
        with self.assertRaises(ValueError):rules.evaluate_playbook(source,hostile)

    def test_failure_cancellation_interruption_retry_and_all_planned_cells(self):
        job,planned=lab.prepare_study(self.spec(),self.root)
        with patch.object(lab,'run_trial',side_effect=RuntimeError('deliberate acquisition failure')):
            first=lab.advance_study(job)
        self.assertEqual([c['status'] for c in first['cells']],['FAILED','PLANNED'])
        lab.cancel_study(job);self.finish(job)
        closed=lab.open_study(planned['study_id'],self.root)
        self.assertEqual([c['status'] for c in closed['cells']],['FAILED','CANCELLED'])
        retry,state=lab.retry_study(planned['study_id'],self.root)
        self.assertNotEqual(state['study_id'],planned['study_id']);self.assertEqual(state['parent_study_id'],planned['study_id'])
        lab.close_study(retry)
        self.assertEqual(lab.open_study(state['study_id'],self.root)['status'],'INCOMPLETE')
        self.assertEqual(len(lab.list_studies(self.root)['entries']),2)

    def test_valid_pair_reverse_order_and_fresh_process_reproduction(self):
        job,planned=lab.prepare_study(self.spec(),self.root);self.finish(job)
        opened=lab.open_study(planned['study_id'],self.root)
        results=[c['result'] for c in opened['cells']]
        self.assertEqual([r['outcome'] for r in results],['NO_FILL','PARTIAL_FILL'])
        self.assertEqual(results[0]['streams'],results[1]['streams'])
        for index in (1,0):
            reverse=run_trial(self.sources[index],results[index]['case'],threading.Event(),self.root)
            self.assertEqual(scientific_projection(reverse),scientific_projection(results[index]))
        script='import json,sys;from kirby2.ui.playbook_lab import verify_study;print(json.dumps(verify_study(sys.argv[1],sys.argv[2]),sort_keys=True))'
        proof=json.loads(subprocess.check_output([sys.executable,'-B','-c',script,planned['study_id'],self.root],text=True))
        self.assertEqual([c['status'] for c in proof['cells']],['PASS','PASS'])

    def test_denominators_no_opportunity_and_independent_arithmetic(self):
        fixture=[]
        for eligible,selected,shares,fills,markout in [(1,1,300,2,40),(1,1,0,0,20),(0,0,0,0,None)]:
            result=dict(eligible_ideas=eligible,selected_ideas=selected,orders=selected,filled_shares=shares,fill_count=fills,
                fills=[] if not fills else [dict(markout_status='AVAILABLE',markout_half_tick_shares=1200),dict(markout_status='AVAILABLE',markout_half_tick_shares=-300)],
                candidate_markout_half_ticks=markout)
            fixture.append(dict(playbook_id='same',case=self.case,status='COMPLETE',result=result))
        summary=grouped_summary(fixture)[0]
        self.assertEqual((summary['planned_ideas'],summary['eligible_ideas'],summary['selected_ideas'],summary['filled_ideas'],summary['unfilled_selected_ideas'],summary['fills']),(3,2,2,1,1,2))
        self.assertEqual((summary['markout_ideas_available'],summary['markout_half_tick_shares']),(1,900))
        no_opportunity=self.sources[0].replace('spread_ticks <= 2','spread_ticks <= 0')
        job,planned=lab.prepare_study(self.spec([no_opportunity]),self.root);self.finish(job)
        opened=lab.open_study(planned['study_id'],self.root)
        self.assertEqual(opened['cells'][0]['result']['outcome'],'NO_OPPORTUNITY')

    def test_signed_markout_cost_units_and_missing_horizon(self):
        buy=fill_measurement('buy',10,101,200,201,206,25)
        sell=fill_measurement('sell',10,101,200,201,198,25)
        self.assertEqual((buy['decision_cost_half_tick_shares'],buy['arrival_cost_half_tick_shares'],buy['markout_half_tick_shares'],buy['fee_millitick_shares']),(20,10,40,250))
        self.assertEqual((sell['decision_cost_half_tick_shares'],sell['markout_half_tick_shares']),(-20,40))
        self.assertIsNone(fill_measurement('buy',10,101,None,None,None,25)['markout_half_tick_shares'])
        doc=json.loads(self.sources[1]);doc['execution']['lifetime_us']=20000;doc['execution']['decision_delay_us']=10000
        doc['rule_source']=doc['rule_source'].replace('<= 2','<= 100').replace('<= 4','<= 100')
        doc['evaluation'].update(markout_horizon_us=10000,fee_milliticks_per_share=25)
        source=json.dumps(doc);case=dict(self.case,recipe_id='execution.queue-pressure.v1')
        result=run_trial(source,case,threading.Event(),self.root)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(result['outcome'],'NOT_SELECTED')
        self.assertEqual(next(r for r in result['trace'] if r['stage']=='NOT_SELECTED')['reason'],'UNAVAILABLE')
        result=run_trial(source,self.case,threading.Event(),self.root)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertGreater(result['filled_shares'],0)
        self.assertTrue(all(m['markout_status']=='TRUNCATED_END_OF_RECORD' and m['markout_half_tick_shares'] is None for m in result['fills']))
        self.assertEqual(result['fees_millitick_shares'],result['filled_shares']*25)
        self.assertEqual(next(r for r in result['trace'] if r['stage']=='INTENT')['at_us'],1010000)

    def test_mixed_matrix_accounts_for_success_no_fill_no_opportunity_failure_cancel(self):
        no_opportunity=self.sources[0].replace('spread_ticks <= 2','spread_ticks <= 0')
        delayed=json.loads(self.sources[1]);delayed['execution']['decision_delay_us']=1000
        cases=[self.case,dict(self.case,recipe_id='execution.queue-pressure.v1')]
        job,planned=lab.prepare_study(self.spec(self.sources+[no_opportunity,json.dumps(delayed)],cases),self.root)
        try:
            for _ in range(6):lab.advance_study(job)
            with patch.object(lab,'run_trial',side_effect=RuntimeError('deliberate seventh-cell failure')):lab.advance_study(job)
            lab.cancel_study(job);lab.advance_study(job)
        finally:lab.close_study(job)
        report=lab.open_study(planned['study_id'],self.root)
        self.assertEqual(len(report['cells']),8)
        self.assertEqual([c['status'] for c in report['cells']],['COMPLETE']*6+['FAILED','CANCELLED'])
        outcomes={c['result']['outcome'] for c in report['cells'] if c['result']}
        self.assertTrue({'FILLED','PARTIAL_FILL','NO_FILL','NO_OPPORTUNITY'}<=outcomes)

    def test_cancellation_inside_trial_preserves_pending_instruction_evidence(self):
        cancelled=threading.Event();original=execution.act_execution_practice
        def action(*args,**kwargs):
            frame=original(*args,**kwargs)
            if frame['simulation_time_us']>=1001000:cancelled.set()
            return frame
        with patch.object(execution,'act_execution_practice',side_effect=action):
            result=run_trial(self.sources[1],self.case,cancelled,self.root)
        self.assertEqual(result['status'],'CANCELLED')
        self.assertEqual(result['review']['final_knowledge']['account']['buy_commitment'],1000)
        self.assertEqual(result['review']['settlement'],'OUTSTANDING')
        self.assertEqual(evidence.open_execution_evidence(result['execution_evidence_id'],self.root)['review'],result['review'])

    def test_reserved_reveal_rename_export_and_group_split(self):
        job,planned=lab.prepare_study(self.spec([self.sources[0]]),self.root);lab.close_study(job)
        self.assertEqual(store.reveals(self.root),{})
        exported=lab.export_study(planned['study_id'],self.root)
        case_id=store.lineage(self.case['recipe_id'],self.case['seed'])
        self.assertIn('EXPORTED',exported['study']['reveal_history'][case_id])
        changed=self.spec([self.sources[0]]);changed['title']='renamed';job,new=lab.prepare_study(changed,self.root);lab.close_study(job)
        self.assertIn('EXPORTED',lab.open_study(new['study_id'],self.root)['reveal_history'][case_id])
        with self.assertRaises(ValueError):lab.prepare_study(self.spec(cases=[self.case,dict(self.case,role='PRACTICE')]),self.root)

    def personal(self):
        h,f=execution.start_execution_practice()
        try:
            for index,(action,payload) in enumerate([('SUBMIT',dict(side='buy',quantity=1000,price_ticks=10000)),('ADVANCE',dict(delta_us=4950)),('CANCEL',dict(order_id='EX-PLAYER-1')),('ADVANCE',dict(delta_us=2675)),('FREEZE',dict(policy='PARTIAL'))]):
                f=execution.act_execution_practice(h,f['frame_id'],str(index),action,payload)
            return evidence.save_execution_evidence(h,self.root)['evidence_id']
        finally:execution.close_execution_practice(h)

    def test_nonzero_personal_prefix_review_restore_and_cold_repeat(self):
        parent=self.personal()
        candidate=drills.create_drill_candidate(parent,1007625,5000,dict(kind='OPEN_JUDGMENT',text='Explain why the unreported fill remains committed.'),self.root)
        identifier=candidate['candidate']['artifact_id']
        with self.assertRaises(ValueError):drills.start_drill_candidate(identifier,self.root)
        drills.review_drill_candidate(identifier,self.root)
        h,f=drills.start_drill_candidate(identifier,self.root)
        try:
            self.assertEqual(f['simulation_time_us'],1007625)
            self.assertEqual(f['knowledge']['account']['confirmed_position'],300)
            self.assertTrue(f['knowledge']['account']['orders']['EX-PLAYER-1']['pending_cancel'])
            next_frame=execution.act_execution_practice(h,f['frame_id'],'repeat-advance','ADVANCE',dict(delta_us=1000))
            self.assertEqual(next_frame['knowledge']['account']['confirmed_position'],500)
            final=execution.act_execution_practice(h,next_frame['frame_id'],'repeat-freeze','FREEZE',dict(policy='PARTIAL'))
            evidence.save_execution_evidence(h,self.root)
        finally:execution.close_execution_practice(h)
        expected=drills.verify_drill_candidate(identifier,1000,self.root)
        script='import json,sys;from kirby2.ui.playbook_drills import verify_drill_candidate;print(json.dumps(verify_drill_candidate(sys.argv[1],1000,sys.argv[2]),sort_keys=True))'
        actual=json.loads(subprocess.check_output([sys.executable,'-B','-c',script,identifier,self.root],text=True))
        self.assertEqual(actual,expected);self.assertIn(1004950,actual['nonzero_instruction_times'])
        path=Path(self.root)/'evidence/execution-practice-v1'/(parent+'.json');path.unlink()
        with self.assertRaises(FileNotFoundError):drills.start_drill_candidate(identifier,self.root)

    def test_hostile_rehashed_result_cannot_change_scientific_claim(self):
        job,planned=lab.prepare_study(self.spec([self.sources[1]]),self.root);self.finish(job)
        row=next(iter(lab._results(job.plan,self.root).values()))
        path=store.directory(self.root)/(row['artifact_id']+'.json');path.unlink()
        row['result']['fills'][0]['markout_half_tick_shares']+=1
        row=store.seal({k:v for k,v in row.items() if k!='artifact_id'},'trial-');store.publish(row,self.root)
        with self.assertRaisesRegex(ValueError,'scientific claims'):lab.open_study(planned['study_id'],self.root)


if __name__=='__main__':unittest.main()
