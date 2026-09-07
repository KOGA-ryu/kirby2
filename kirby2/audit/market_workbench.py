"""Chapter 3 causal and restoration acceptance; standard library, no Qt/images."""
from __future__ import annotations
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.features.market_observations import observe_market
from kirby2.ui.market_profiles import list_market_workbench_profiles
from kirby2.ui import market_workbench as market
from kirby2.ui.simulation_facade import simulation_contract_golden_records, GOLDEN_FIXTURE_DIRECTORY, resolve_simulation_profile
from kirby2.ui.simulation_run_facade import (start_simulation_run,advance_simulation_run,close_simulation_run,
                                            dispatch_simulation_command,prepare_simulation_reset,commit_simulation_reset)
from kirby2.ui.simulation_contract import canonical_digest,SimulationContractDecodeError


def fixture():
    # At ask 101: 100 shares executed; 30 cancelled; 80 added. Bid-side
    # execution consumes 20 at 99 and a later 50-share addition replaces only 20.
    events = [dict(sequence=i,time_us=t,kind=k,side=s,price_ticks=p,quantity=q)
              for i,(t,k,s,p,q) in enumerate([(1,'TRADE','BUY',101,100),(2,'CANCEL','SELL',101,30),
                                             (3,'ADD','SELL',101,80),(4,'TRADE','SELL',99,20),
                                             (5,'ADD','BUY',99,50)],1)]
    books = [dict(time_us=t,bid_ticks=99,ask_ticks=101,bid_shares=1000,ask_shares=1000) for t in (0,5_000_000)]
    return events,books


class MarketObservationAcceptance(unittest.TestCase):
    def test_independent_execution_cancellation_and_matching_level_fixture(self):
        events,books=fixture();v=observe_market(events,books,cut_us=5_000_000)['values']
        self.assertEqual(v['executed_buy_shares'],100);self.assertEqual(v['executed_sell_shares'],20)
        self.assertEqual(v['executed_notional_tick_shares'],12080)
        self.assertEqual(v['cancelled_ask_shares'],30);self.assertEqual(v['added_ask_shares'],80)
        self.assertEqual(v['replenished_ask_shares'],80);self.assertEqual(v['replenished_bid_shares'],20)
        self.assertEqual(v['mid_change_half_ticks'],0)
        events[2]['price_ticks']=102
        self.assertEqual(observe_market(events,books,cut_us=5_000_000)['values']['replenished_ask_shares'],0)

    def test_identical_terminal_depth_different_flow_is_not_static_imbalance(self):
        events,books=fixture();other=copy.deepcopy(events)
        other[0].update(kind='CANCEL',side='SELL');other[3].update(kind='CANCEL',side='BUY')
        a=observe_market(events,books,cut_us=5_000_000);b=observe_market(other,books,cut_us=5_000_000)
        self.assertEqual(a['values']['bid_depth_shares'],b['values']['bid_depth_shares'])
        self.assertEqual(b['values']['executed_buy_shares'],0);self.assertEqual(b['values']['replenished_ask_shares'],0)
        self.assertNotEqual(a['source_prefix_sha256'],b['source_prefix_sha256'])

    def test_unavailable_summaries_keep_seed_positions(self):
        cell={'profile_id':'market.test.v1','status':'AVAILABLE','diagnostics':{'status':'PASS'},'observations':[
            {'values':dict(executed_buy_shares=100,executed_sell_shares=50,cancelled_ask_shares=0,added_ask_shares=0,replenished_ask_shares=0,mid_change_half_ticks=None)}]}
        summary=market._summary([cell,{'profile_id':'market.test.v1','status':'FAILED'}])[0]
        self.assertEqual(summary['metrics']['mid_change_half_ticks']['values_in_seed_order'],[None,None])
        self.assertIsNone(summary['metrics']['mid_change_half_ticks']['median'])
        self.assertEqual(summary['metrics']['executed_buy_shares']['values_in_seed_order'],[100,None])
        self.assertEqual(summary['metrics']['executed_buy_shares']['sample_count'],1)

    def test_missing_zero_and_independent_prior_units(self):
        events,books=fixture()
        prior=dict(schema_id='KIRBY2_SYNTHETIC_PRIOR_V1',profile_sha256='a'*64,seeds=[101,103],cut_us=5_000_000,window_us=5_000_000,notional_sum=24160,sample_count=2)
        missing=observe_market(events,books,cut_us=5_000_000)
        self.assertEqual(missing['relative_activity']['status'],'MISSING_BASELINE')
        measured=observe_market(events,books,cut_us=5_000_000,baseline=prior)
        self.assertEqual(measured['relative_activity']['ratio'],'1.000000')
        zero=observe_market(events,books,cut_us=5_000_000,baseline={**prior,'notional_sum':0})
        self.assertEqual(zero['relative_activity']['status'],'ZERO_BASELINE');self.assertIsNone(zero['relative_activity']['ratio'])
        wrong=observe_market(events,books,cut_us=5_000_000,baseline={**prior,'cut_us':10_000_000})
        self.assertEqual(wrong['relative_activity']['status'],'BASELINE_COORDINATE_MISMATCH')

    def test_future_and_hidden_labels_cannot_change_blind_projection(self):
        events,books=fixture();baseline=observe_market(events,books,cut_us=5_000_000)
        changed=[{**row,'secret_family':'different','future_outcome':'profit'} for row in events]
        changed.append(dict(sequence=6,time_us=5_000_001,kind='TRADE',side='BUY',price_ticks=9999,quantity=90000))
        self.assertEqual(observe_market(changed,books+[dict(time_us=6_000_000,bid_ticks=9000,ask_ticks=9001,bid_shares=1,ask_shares=1)],cut_us=5_000_000),baseline)

    def test_gaps_short_windows_left_exclusion_and_stale_delivery(self):
        events,books=fixture()
        gap=observe_market(events[1:],books,cut_us=5_000_000)
        self.assertEqual(gap['status'],'OBSERVATION_GAP');self.assertTrue(all(v is None for v in gap['values'].values()))
        short=observe_market(events,books,cut_us=4_000_000)
        self.assertEqual(short['status'],'INSUFFICIENT_WINDOW')
        stale=observe_market(events,books,cut_us=5_000_000,delivered_at_us=5_000_500)
        self.assertEqual(stale['delivery_status'],'STALE');self.assertEqual(stale['age_us'],500)
        events[0]['time_us']=0
        self.assertEqual(observe_market(events,books,cut_us=5_000_000)['values']['executed_buy_shares'],0)


class MarketGovernanceAcceptance(unittest.TestCase):
    def test_original_public_golden_records_remain_identical(self):
        for name,value in simulation_contract_golden_records().items():
            self.assertEqual(value,json.loads((GOLDEN_FIXTURE_DIRECTORY/name).read_text()),name)

    def test_capability_and_single_parameter_isolation(self):
        capability=list_market_workbench_profiles();self.assertEqual(len(capability['recipes']),10)
        recipes={row['key']:row for row in capability['recipes']}
        for key,field in [('replenishment','replenishment_peak'),('thin','liquidity'),('fast-heating','heating_seconds'),('long-persistence','persistence_seconds'),('slow-cooling','cooling_seconds'),('clustered','model')]:
            exclude={'key','title','profile_id',field}
            self.assertEqual({k:v for k,v in recipes[key].items() if k not in exclude},{k:v for k,v in recipes['warming'].items() if k not in exclude})
        self.assertFalse(set(capability['evaluation_seeds'])&set(capability['prior_seeds']))

    def test_invalid_duration_control_seed_and_malformed_start_refuse(self):
        selection=market.market_start_configuration('market.warming.v1')['resolution']['selection']
        self.assertEqual(resolve_simulation_profile({**selection,'duration_us':31_000_000})['status'],'REFUSED')
        self.assertEqual(resolve_simulation_profile({**selection,'control_values':{'intensity_scale_ppm':2_000_000}})['status'],'REFUSED')
        for seed in (True,-1,2**31):
            with self.assertRaises(ValueError): market.market_start_configuration('market.warming.v1',seed)
        with self.assertRaises(SimulationContractDecodeError): start_simulation_run({}, {})

    def test_all_recipes_restore_and_keep_matching_invariants(self):
        from kirby2.ui.simulation_artifact_contract import ReplayArtifactRefV1
        from kirby2.ui.simulation_replay_facade import _verify_replay_artifact_bytes
        for row in list_market_workbench_profiles()['recipes']:
            with self.subTest(recipe=row['key']):
                cell,raw=market._run(row['profile_id'],11)
                self.assertEqual(cell['diagnostics']['status'],'PASS',cell)
                source,receipt=_verify_replay_artifact_bytes(ReplayArtifactRefV1.from_dict(cell['artifact_ref']),raw)
                self.assertIsNotNone(source,receipt)
                observations,diagnostics=market._recompute(source)
                self.assertEqual(observations,cell['observations']);self.assertEqual(diagnostics,cell['diagnostics'])

    def test_presentation_sampling_and_same_time_player_action_preserve_model(self):
        def run(step):
            config=market.market_start_configuration('market.clustered.v1',23);config['training_options']['initial_run_state']='RUNNING'
            h,start=start_simulation_run(config['resolution'],config['training_options']);f=start['initial_frame']
            try:
                for target in range(step,30_000_001,step):
                    f=advance_simulation_run(h,f['source_run_id'],f['frame_id'],f['cursor']['cursor_id'],target)['destination_frame']
                    if target==10_000_000:
                        basis=dict(schema_id='KIRBY2_SIMULATION_COMMAND_REQUEST_V1',schema_version=1,source_run_id=f['source_run_id'],origin_frame_id=f['frame_id'],origin_cursor_id=f['cursor']['cursor_id'],semantic_action_id='PLAYER_BUY_MARKET',parameters={})
                        result=dispatch_simulation_command(h,{**basis,'command_id':'simulation-command-'+canonical_digest(basis)[:24]})
                        f=result['destination_frame']
                return h.session.state_sha256()
            finally: close_simulation_run(h,'USER_ABANDONED')
        self.assertEqual(run(1_000_000),run(5_000_000))

    def test_reset_restores_fresh_queue_and_intraday_state(self):
        config=market.market_start_configuration('market.clustered.v1',37);config['training_options']['initial_run_state']='RUNNING'
        h,start=start_simulation_run(config['resolution'],config['training_options']);f=start['initial_frame'];original=h.session.state_sha256()
        f=advance_simulation_run(h,f['source_run_id'],f['frame_id'],f['cursor']['cursor_id'],15_000_000)['destination_frame']
        pending,result=prepare_simulation_reset(h,f['source_run_id'],f['frame_id'],f['cursor']['cursor_id'])
        self.assertIsNotNone(pending,result)
        replacement,committed=commit_simulation_reset(h,pending,result['reset_token_id'],result['new_source_run_id'],result['initial_frame']['frame_id'])
        try:
            self.assertEqual(replacement.session.state_sha256(),original)
            self.assertIsNot(replacement.session.engine.intensity_modifier,h.session.engine.intensity_modifier)
        finally: close_simulation_run(replacement,'USER_ABANDONED')


class MarketPersistenceAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=str(Path(cls.temp.name).resolve())
        cls.report=market.run_market_comparison(root=cls.root)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_full_population_durable_fresh_process_restore(self):
        self.assertEqual(len(self.report['cells']),12)
        self.assertTrue(all(c['status']=='AVAILABLE' for c in self.report['cells']))
        self.assertTrue(all(c['diagnostics']['status']=='PASS' for c in self.report['cells']))
        script="from kirby2.ui.market_workbench import open_market_report; import sys; r=open_market_report(sys.argv[2],sys.argv[1]); assert r['familiarity']=='EXPOSED_BY_PREVIEW'; print(r['report_id'])"
        restored=subprocess.check_output([sys.executable,'-B','-c',script,self.root,self.report['report_id']],text=True).strip()
        self.assertEqual(restored,self.report['report_id'])

    def test_preview_exposure_survives_report_removal(self):
        directory=Path(self.root)/'evidence/market-workbench'/self.report['report_id']
        temporary=directory.with_name('.pending-hidden-report')
        directory.rename(temporary)
        try:
            saved=market.save_market_configuration('market.replenishment.v1',11,self.root)
            self.assertIn('EXPOSED_BY_PREVIEW',saved['familiarity'])
        finally: temporary.rename(directory)

    def test_rehashed_measurement_forgery_fails_recomputation(self):
        forged=copy.deepcopy(self.report);forged['cells'][0]['observations'][0]['values']['executed_buy_shares']+=1
        identifier='market-report-'+canonical_digest({k:v for k,v in forged.items() if k!='report_id'});forged['report_id']=identifier
        directory=Path(self.root)/'evidence/market-workbench'
        copied=directory/identifier;shutil.copytree(directory/self.report['report_id'],copied)
        try:
            (copied/'report.json').write_bytes(market._bytes(forged))
            with self.assertRaisesRegex(ValueError,'measurements differ'): market.open_market_report(identifier,self.root)
        finally: shutil.rmtree(copied)

    def test_rehashed_unsupported_claim_is_refused(self):
        forged=copy.deepcopy(self.report);forged['release_qualification']=True
        identifier='market-report-'+canonical_digest({k:v for k,v in forged.items() if k!='report_id'});forged['report_id']=identifier
        directory=Path(self.root)/'evidence/market-workbench'/identifier;directory.mkdir()
        try:
            (directory/'report.json').write_bytes(market._bytes(forged))
            with self.assertRaisesRegex(ValueError,'policy differs'): market.open_market_report(identifier,self.root)
        finally: shutil.rmtree(directory)

    def test_corrupt_replay_and_symlinks_refuse(self):
        cell=self.report['cells'][0];directory=Path(self.root)/'evidence/market-workbench'/self.report['report_id']
        path=directory/(cell['artifact_ref']['artifact_sha256']+'.json');raw=path.read_bytes()
        try:
            path.write_bytes(raw+b' ')
            with self.assertRaises(ValueError): market.open_market_report(self.report['report_id'],self.root)
        finally: path.write_bytes(raw)
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp).resolve()/'link';target.symlink_to(directory)
            with self.assertRaises(ValueError): market._read(market._child(target.parent,'link'))

    def test_failed_cells_remain_and_cleanup_failure_stops_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=str(Path(temp).resolve())
            with patch.object(market,'_run',side_effect=ValueError('injected refused cell')):
                report=market.run_market_comparison(root=root)
            self.assertEqual(len(report['cells']),12);self.assertTrue(all(c['status']=='FAILED' for c in report['cells']))
            self.assertEqual(market.open_market_report(report['report_id'],root),report)
            before=market.list_market_reports(root)
            with patch.object(market,'_run',side_effect=market.MarketCleanupError('cleanup lost')):
                with self.assertRaises(market.MarketCleanupError): market.run_market_comparison(root=root)
            self.assertEqual(market.list_market_reports(root),before)

    def test_configuration_write_failure_retry_keeps_identity_and_familiarity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=str(Path(temp).resolve())
            with patch.object(market,'_write',side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError): market.save_market_configuration('market.thin.v1',11,root)
            a=market.save_market_configuration('market.thin.v1',11,root)
            b=market.save_market_configuration('market.thin.v1',11,root)
            c=market.save_market_configuration('market.thin.v1',23,root)
            self.assertEqual(a,b);self.assertNotEqual(a['saved_configuration_id'],c['saved_configuration_id'])
            self.assertIn('not a blind assessment',a['familiarity'])
            self.assertEqual(market.list_market_reports(root)['rejected'],[])
