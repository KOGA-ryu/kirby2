"""C6 acceptance probes over the public world boundary; no Qt or images."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.ui import radar_session as r, radar_evidence as e, playbooks, playbook_store
from kirby2.ui.radar_recipes import SYMBOLS, STEP_US


class RadarAcceptance(unittest.TestCase):
    def setUp(self): self.handles=[]
    def tearDown(self):
        for w in self.handles:
            if not w.closed:r.close_world(w)
        self.assertEqual(r.retained_worlds(),0)
    def start(self,mode='CONTINUOUS'):
        w,_=r.start_world(mode=mode);self.handles.append(w);return w
    def act(self,w,action,**payload):
        f=r.refresh_world(w)
        return r.act_world(w,f['frame_id'],'request-'+str(len(w.operations)),action,payload)
    def advance(self,w,cut):
        while w.now<cut:self.act(w,'ADVANCE',steps=min(4,(cut-w.now)//STEP_US))
    def brio(self):
        w=self.start();self.advance(w,3_500_000)
        candidate=w.current['BRIO']['candidate_id']
        self.act(w,'SELECT',candidate_id=candidate);self.act(w,'ARM',candidate_id=candidate)
        return w

    def test_partial_acquisition_failure_releases_composite(self):
        original=r.create_instrument;acquired=[]
        def fail(seed,symbol):
            if symbol=='CINDER':raise RuntimeError('injected third acquisition failure')
            result=original(seed,symbol);acquired.append(symbol);return result
        with patch.object(r,'create_instrument',side_effect=fail),self.assertRaisesRegex(RuntimeError,'third'):
            r.start_world()
        self.assertEqual(acquired,['ASTER','BRIO']);self.assertEqual(r.retained_worlds(),0)

    def test_guided_world_hold_and_independent_books(self):
        w=self.start('GUIDED');initial=r.refresh_world(w)
        f=self.act(w,'ADVANCE',steps=1)
        self.assertEqual(f['refusal']['code'],'WORLD_PAUSED');self.assertEqual(f['world_times'],initial['world_times'])
        self.act(w,'RESUME');f=self.act(w,'ADVANCE',steps=1)
        self.assertEqual(set(f['world_times'].values()),{1_500_000})
        self.assertEqual(len({id(x.engine.book) for x in w.runtimes.values()}),3)
        self.assertEqual(len({x.plan.seed_policy.root_seed for x in w.runtimes.values()}),3)

    def test_eligibility_before_score_cinder_never_qualifies(self):
        w=self.start();row=r._evaluation(w,'CINDER')
        self.assertEqual(row['result']['status'],'NOT_ELIGIBLE');self.assertIsNone(row['score'])
        rows=[dict(symbol='ASTER',score=999999999,result={'status':'UNAVAILABLE'}),
              dict(symbol='BRIO',score=998500,result={'status':'ELIGIBLE'}),
              dict(symbol='CINDER',score=998500,result={'status':'ELIGIBLE'})]
        self.assertEqual(r.rank_candidates(rows),['BRIO','CINDER'])
        self.assertEqual(r.rank_candidates(rows,'CINDER'),['CINDER','BRIO'])
        rows[1]['score']+=100
        self.assertEqual(r.rank_candidates(rows,'CINDER'),['BRIO','CINDER'])
        from kirby2.features.market_observations import observe_delivered_book
        measured=observe_delivered_book(r._observation(w,'ASTER')['market'],observed_at_us=2000000,available_at_us=1000000)
        self.assertEqual(measured['status'],'STALE_BOOK');self.assertIsNone(measured['relative_activity'])
        self.advance(w,9_000_000)
        self.assertTrue(all(next(v for v in scan['rows'] if v['symbol']=='CINDER')['result']['status']!='ELIGIBLE' for scan in w.evaluations))

    def test_expiry_revalidates_old_click_and_visibility_is_not_attention(self):
        w=self.start();candidate=w.current['ASTER']['candidate_id'];old=r.refresh_world(w)
        for state in ('PRESENTED','BACKGROUND','MINIMIZED','SUPPRESSED','UNKNOWN'):
            self.act(w,'PRESENT',candidate_ids=[candidate],visibility=state)
        self.advance(w,2_500_000)
        f=self.act(w,'SELECT',candidate_id=candidate);self.assertEqual(f['refusal']['code'],'EXPIRED_CANDIDATE')
        with self.assertRaisesRegex(ValueError,'older world cut'):
            r.act_world(w,old['frame_id'],'stale','SELECT',{'candidate_id':candidate})
        self.act(w,'FREEZE');review=e.review_world(w)
        row=review['candidates'][0];self.assertEqual(row['review_disposition'],'MISSED_OR_UNNOTICED')
        self.assertTrue(all(v['noticed'] is None and v['response_time_us'] is None for v in row['visibility_receipts']))

    def test_selected_armed_identity_and_pending_cancel_checkpoint_restore(self):
        w=self.brio();self.act(w,'SUBMIT',symbol='BRIO',side='buy',quantity=500,price_ticks=9998)
        self.act(w,'INSPECT',symbol='CINDER');self.advance(w,5_000_000)
        self.assertEqual(w.armed,'BRIO');self.assertEqual(w.ledgers['BRIO'].position,200)
        self.act(w,'CANCEL',symbol='BRIO',order_id='RADAR-PLAYER-1')
        record=e.checkpoint_world(w);restored,_=e.restore_world(record);self.handles.append(restored)
        self.assertNotEqual(restored.world_id,w.world_id)
        self.assertTrue(restored.ledgers['BRIO'].orders['RADAR-PLAYER-1']['pending_cancel'])
        for world in (w,restored):self.act(world,'ADVANCE',steps=1)
        self.assertEqual(r.scientific_frame(r.refresh_world(w)),r.scientific_frame(r.refresh_world(restored)))
        self.assertEqual(w.ledgers['BRIO'].view()['buy_commitment'],0)
        self.assertEqual(w.ledgers['BRIO'].position,200)
        self.advance(w,7_500_000);a=w.current['ASTER']['candidate_id']
        f=self.act(w,'SELECT',candidate_id=a);self.assertEqual(f['refusal']['code'],'OTHER_EXPOSURE')
        self.act(w,'SUBMIT',symbol='BRIO',side='sell',quantity=200,price_ticks=9998)
        self.act(w,'ADVANCE',steps=1);self.assertEqual(w.ledgers['BRIO'].position,0)
        self.assertIsNone(self.act(w,'SELECT',candidate_id=a)['refusal'])
        self.assertIsNone(self.act(w,'ARM',candidate_id=a)['refusal']);self.assertEqual(w.armed,'ASTER')

    def test_duplicates_stale_world_and_cross_symbol_order_are_fenced(self):
        w=self.brio();frame=r.refresh_world(w);p=dict(symbol='BRIO',side='buy',quantity=100,price_ticks=9998)
        r.act_world(w,frame['frame_id'],'same','SUBMIT',p)
        r.act_world(w,frame['frame_id'],'same','SUBMIT',p)
        self.assertEqual(len(w.ledgers['BRIO'].orders),1)
        with self.assertRaisesRegex(ValueError,'Retry changed'):
            r.act_world(w,frame['frame_id'],'same','SUBMIT',dict(p,quantity=200))
        f=self.act(w,'SUBMIT',symbol='CINDER',side='buy',quantity=1,price_ticks=10000)
        self.assertEqual(f['refusal']['code'],'ARMED_SYMBOL_CHANGED')
        r.close_world(w)
        with self.assertRaisesRegex(ValueError,'no longer owns'):r.refresh_world(w)

    def test_pending_cancel_alone_prevents_transfer_until_receipt(self):
        w=self.start();self.advance(w,7500000)
        brio=w.current['BRIO']['candidate_id']
        self.act(w,'SELECT',candidate_id=brio);self.act(w,'ARM',candidate_id=brio)
        self.act(w,'SUBMIT',symbol='BRIO',side='buy',quantity=100,price_ticks=9950)
        self.assertEqual(w.ledgers['BRIO'].position,0)
        self.act(w,'CANCEL',symbol='BRIO',order_id='RADAR-PLAYER-1')
        candidate=w.current['ASTER']['candidate_id']
        self.assertEqual(self.act(w,'SELECT',candidate_id=candidate)['refusal']['code'],'OTHER_EXPOSURE')
        self.act(w,'ADVANCE',steps=1)
        self.assertIsNone(self.act(w,'SELECT',candidate_id=candidate)['refusal'])
        self.assertIsNone(self.act(w,'ARM',candidate_id=candidate)['refusal'])

    def test_capacity_refusal_and_partial_advance_failure_retain_truthful_ownership(self):
        w=self.brio()
        for _ in range(r.MAX_ORDERS):self.assertIsNone(self.act(w,'SUBMIT',symbol='BRIO',side='buy',quantity=1,price_ticks=9950)['refusal'])
        self.assertEqual(self.act(w,'SUBMIT',symbol='BRIO',side='buy',quantity=1,price_ticks=9950)['refusal']['code'],'ORDER_BUDGET')
        self.assertEqual(w.ledgers['BRIO'].view()['buy_commitment'],r.MAX_ORDERS)
        self.act(w,'ADVANCE',steps=1)
        self.assertLess(sum(len(x.events) for x in w.runtimes.values()),r.MAX_EVENTS)
        runtime=w.runtimes['BRIO']
        with patch.object(runtime,'advance_to',side_effect=RuntimeError('injected advance failure')):
            with self.assertRaisesRegex(RuntimeError,'advance failure'):self.act(w,'ADVANCE',steps=1)
        frame=r.refresh_world(w)
        self.assertEqual(frame['phase'],'INTEGRITY_LOCKED');self.assertTrue(w.locked)
        self.assertEqual(r.retained_worlds(),1)
        r.close_world(w)
        v=self.start()
        with patch.object(r,'MAX_EVENTS',1),self.assertRaisesRegex(RuntimeError,'event budget'):
            self.act(v,'ADVANCE',steps=1)
        self.assertEqual(r.refresh_world(v)['phase'],'INTEGRITY_LOCKED')
        with patch.object(r,'MAX_CANDIDATES',0),self.assertRaisesRegex(RuntimeError,'candidate budget'):
            r.start_world()
        self.assertEqual(r.retained_worlds(),1)

    def test_full_frozen_review_persists_unfilled_and_excluded_evidence(self):
        w=self.brio();self.act(w,'SUBMIT',symbol='BRIO',side='buy',quantity=500,price_ticks=10000)
        self.advance(w,4_000_000);self.act(w,'FREEZE')
        with tempfile.TemporaryDirectory() as root:
            root=str(Path(root).resolve())
            saved=e.save_world(w,root);opened=e.open_world(saved['evidence_id'],root)
            self.assertEqual(saved,opened);review=opened['review']
            self.assertEqual(set(review['venue_events']),set(SYMBOLS))
            metric=review['metrics']['orders'][0]
            self.assertEqual(metric['outcome'],'PARTIAL');self.assertEqual(metric['filled'],300)
            self.assertEqual(metric['fills'][0]['horizon_status'],'AVAILABLE')
            self.assertEqual(metric['fills'][0]['measurement']['decision_cost_half_tick_shares'],600)
            record=e._record(w);record['review']['accounts']['BRIO']['confirmed_position']=0
            record=playbook_store.seal({k:v for k,v in record.items() if k!='artifact_id'},e.PREFIX)
            with self.assertRaisesRegex(ValueError,'review differs'):e.verify_record(record)

    def test_overload_and_budget_never_silently_slow_or_release_exposure(self):
        w=self.brio();self.act(w,'SUBMIT',symbol='BRIO',side='buy',quantity=500,price_ticks=9998)
        f=self.act(w,'OVERLOAD');self.assertEqual(f['phase'],'PAUSED')
        self.assertEqual(f['accounts']['BRIO']['buy_commitment'],500)
        with patch.object(r,'MAX_OPERATIONS',len(w.operations)+1):
            with self.assertRaisesRegex(ValueError,'budget'):self.act(w,'RESUME')
            self.assertEqual(self.act(w,'FREEZE')['phase'],'FROZEN')

    def test_frozen_metrics_distinguish_venue_fills_from_unreceived_reports(self):
        w=self.start();candidate=w.current['ASTER']['candidate_id']
        self.act(w,'SELECT',candidate_id=candidate);self.act(w,'ARM',candidate_id=candidate)
        self.act(w,'SUBMIT',symbol='ASTER',side='buy',quantity=500,price_ticks=10000)
        # A lower-level assessment fixture deliberately stops inside the normal
        # latency interval. All three real runtimes still share one cut. This
        # exercises review arithmetic, not a new public world stepping policy.
        for runtime in w.runtimes.values():runtime.advance_to(1002800)
        w.now=1002800;r._reconcile(w);r._scan(w)
        self.assertEqual(w.ledgers['ASTER'].orders['RADAR-PLAYER-1']['filled'],0)
        self.act(w,'FREEZE')
        review=e.review_world(w);metric=review['metrics']['orders'][0]
        self.assertEqual(metric['filled'],300);self.assertEqual(metric['known_filled'],0)
        self.assertEqual(metric['unresolved'],500);self.assertEqual(metric['outcome'],'PARTIAL')
        self.assertEqual(metric['fills'][0]['horizon_status'],'TRUNCATED')
        self.assertIsNone(metric['fills'][0]['measurement']['markout_half_tick_shares'])

    def test_c4_observation_horizon_is_unchanged(self):
        w=self.start();self.act(w,'ADVANCE',steps=1);cut=r._observation(w,'ASTER')
        self.assertEqual(playbooks.evaluate_session_playbook(w.source,cut)['status'],'ELIGIBLE')
        with self.assertRaises(ValueError):playbooks.evaluate_playbook(w.source,cut)
        from kirby2.full_day.models import canonical_sha256
        cut['schema_id']='KIRBY2_RULE_OBSERVATION_V1'
        cut['observation_id']='observation-'+canonical_sha256({k:v for k,v in cut.items() if k!='observation_id'})
        with self.assertRaises(ValueError):playbooks.evaluate_playbook(w.source,cut)


if __name__=='__main__':unittest.main()
