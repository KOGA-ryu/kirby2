"""Chapter 4 causal acceptance through the public governed route. No Qt/images."""
from __future__ import annotations
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kirby2.ui import execution_practice as practice
from kirby2.ui import execution_library as library
from kirby2.ui.execution_commitments import Commitments, ExecutionRefusal
from kirby2.full_day.models import canonical_sha256


class ExecutionAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = str(Path(self.temp.name).resolve())
        self.handle,self.frame = practice.start_execution_practice()
        self.sequence = 0

    def tearDown(self):
        practice.close_execution_practice(self.handle)
        self.temp.cleanup()

    def act(self,action,**payload):
        self.sequence += 1
        self.frame = practice.act_execution_practice(self.handle,self.frame['frame_id'],str(self.sequence),action,payload)
        return self.frame

    def race(self, cut=1_008_070):
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        self.act('ADVANCE',delta_us=4950)
        self.assertEqual(self.account()['confirmed_position'],300)
        self.act('CANCEL',order_id='EX-PLAYER-1')
        self.act('ADVANCE',delta_us=cut-1_004_950)

    def account(self): return self.frame['knowledge']['account']

    def test_actual_cancel_ack_before_fill_does_not_release_unreported_fill(self):
        self.race()
        self.assertEqual((self.account()['confirmed_position'],self.account()['buy_commitment']),(300,200))
        receipts = self.frame['knowledge']['receipts']
        self.assertEqual(receipts[-1]['event_type'],'ORDER_CANCELLED')
        self.assertEqual(receipts[-1]['data']['cancelled_quantity'],500)
        self.assertEqual(self.act('SUBMIT',side='buy',quantity=700,price_ticks=10000)['refusal']['code'],'EXPOSURE_CAP')
        self.act('ADVANCE',delta_us=40)
        self.assertEqual((self.account()['confirmed_position'],self.account()['buy_commitment']),(500,0))
        self.act('FREEZE',policy='COMPLETE')
        review = practice.frozen_execution_review(self.handle)
        trades = [e for e in review['venue_events'] if e['event_type']=='TRADE']
        self.assertEqual([e['data']['quantity'] for e in trades],[300,200])
        self.assertLess(trades[-1]['simulation_time_us'],receipts[-1]['source_us'])
        refusal_decision=next(o for o in review['operations'] if o['refusal'] is not None)['decision']
        self.assertEqual(refusal_decision['account']['confirmed_position'],300)
        self.assertEqual(refusal_decision['account']['buy_commitment'],200)
        comparison=next(row for row in review['decision_comparisons'] if row['simulation_time_us']==1_008_070)
        self.assertEqual((comparison['client_confirmed_position'],comparison['venue_position']),(300,500))
        self.assertTrue(any(e['event_type']=='ORDER_ACCEPTED' and e['data']['order_id'].startswith('FD-O') for e in review['venue_events']))

    def test_delayed_ack_snapshot_cannot_leak_fill_quantity(self):
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        self.act('ADVANCE',delta_us=3050)
        self.assertEqual(self.account()['confirmed_position'],0)
        self.assertEqual(self.account()['buy_commitment'],1000)
        self.assertNotIn('order_snapshots',json.dumps(self.frame))
        self.assertNotIn('venue_events',self.frame)
        with self.assertRaises(ExecutionRefusal): practice.frozen_execution_review(self.handle)

    def test_pending_venue_and_client_checkpoint_fresh_process_continuation(self):
        self.race(cut=1_007_625)
        self.act('FREEZE',policy='PARTIAL')
        saved = library.save_execution_evidence(self.handle,self.root)
        expected = library.verify_execution_continuation(saved['evidence_id'],3000,self.root)
        script = ('import json,sys; from kirby2.ui.execution_library import verify_execution_continuation; '
                  'print(json.dumps(verify_execution_continuation(sys.argv[1],3000,sys.argv[2]),sort_keys=True))')
        result = json.loads(subprocess.check_output([sys.executable,'-B','-c',script,saved['evidence_id'],self.root],text=True))
        self.assertEqual(result,expected)
        self.assertEqual(result['knowledge']['account']['confirmed_position'],500)
        self.assertEqual(result['knowledge']['account']['buy_commitment'],0)
        self.assertEqual(library.open_execution_evidence(saved['evidence_id'],self.root),saved)

    def test_duplicate_request_and_stale_origin_cannot_duplicate_submission(self):
        origin = self.frame['frame_id']
        frame = self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        retry = practice.act_execution_practice(self.handle,origin,'1','SUBMIT',dict(side='buy',quantity=1000,price_ticks=10000))
        self.assertEqual(frame,retry)
        self.assertEqual(len(retry['knowledge']['account']['orders']),1)
        with self.assertRaises(ExecutionRefusal):
            practice.act_execution_practice(self.handle,origin,'new','SUBMIT',dict(side='buy',quantity=1,price_ticks=10000))
        with self.assertRaises(ExecutionRefusal):
            practice.act_execution_practice(self.handle,origin,'1','SUBMIT',dict(side='buy',quantity=999,price_ticks=10000))

    def test_hold_freezes_all_pending_work_and_equal_time_restore(self):
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        self.act('HOLD')
        before = copy.deepcopy(self.frame['knowledge'])
        self.assertEqual(self.act('ADVANCE',delta_us=5000)['refusal']['code'],'GUIDED_HOLD')
        self.assertEqual(self.frame['simulation_time_us'],1_000_000)
        self.assertEqual(self.frame['knowledge'],before)
        self.act('FREEZE',policy='PARTIAL')
        saved = library.save_execution_evidence(self.handle,self.root)
        result = library.verify_execution_continuation(saved['evidence_id'],10_000,self.root)
        self.assertEqual(result['status'],'PASS')

    def test_disarm_expiry_and_reserved_reduction(self):
        self.act('ARM',target=1000,price_ticks=10000,expires_us=1_001_000)
        generation = self.frame['intent_generation']
        self.act('ADVANCE',delta_us=1001)
        self.assertIsNone(self.frame['intent'])
        self.assertEqual(self.act('FIRE',intent_generation=generation)['refusal']['code'],'DISARMED_INTENT')
        self.act('SUBMIT',side='buy',quantity=300,price_ticks=10000)
        self.act('ADVANCE',delta_us=3500)
        self.assertEqual(self.account()['confirmed_position'],300)
        self.act('REDUCE',quantity=200,price_ticks=9998)
        self.assertEqual(self.account()['possible_position'],[100,300])
        self.assertEqual(self.act('REDUCE',quantity=200,price_ticks=9998)['refusal']['code'],'REDUCTION_RESERVED')

    def test_unknown_routing_preserves_reservation_and_cleanup(self):
        with patch.object(self.handle.runtime,'submit_request',side_effect=RuntimeError('after reservation')):
            with self.assertRaises(RuntimeError): self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        self.assertEqual(self.handle.ledger.view()['buy_commitment'],1000)
        self.assertEqual(self.handle.ledger.unknown,'UNSETTLED_BACKEND_OPERATION')
        self.assertEqual(practice.close_execution_practice(self.handle),dict(status='CLOSED',execution_settlement='NOT_CLAIMED'))

    def test_finalization_partial_is_not_flat_and_save_failure_is_retryable(self):
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        self.assertEqual(self.act('FREEZE',policy='COMPLETE')['refusal']['code'],'UNRESOLVED_EXECUTION')
        self.act('FREEZE',policy='PARTIAL')
        with patch.object(library,'_write',side_effect=OSError('disk failed')):
            with self.assertRaises(OSError): library.save_execution_evidence(self.handle,self.root)
        self.assertEqual(library.list_execution_evidence(self.root)['entries'],[])
        saved = library.save_execution_evidence(self.handle,self.root)
        self.assertEqual(saved['review']['settlement'],'OUTSTANDING')
        self.assertEqual(library.save_execution_evidence(self.handle,self.root),saved)

    def test_rehashed_false_claims_are_rejected(self):
        self.act('FREEZE',policy='PARTIAL')
        saved = library.save_execution_evidence(self.handle,self.root)
        path = Path(self.root)/'evidence/execution-practice-v1'/(saved['evidence_id']+'.json')
        record = json.loads(path.read_text()); record['knowledge']['account']['confirmed_position']=777
        record['evidence_id']='execution-evidence-'+canonical_sha256({k:v for k,v in record.items() if k!='evidence_id'})
        with self.assertRaises(ValueError): library._verify(record)

    def test_equal_time_commands_and_late_market_state_are_monotone(self):
        self.act('SUBMIT',side='buy',quantity=400,price_ticks=10000)
        self.act('SUBMIT',side='buy',quantity=600,price_ticks=10000)
        self.act('ADVANCE',delta_us=3500)
        account=self.account()
        self.assertEqual(account['orders']['EX-PLAYER-1']['filled'],300)
        self.assertEqual(account['orders']['EX-PLAYER-2']['filled'],0)
        self.act('FREEZE',policy='PARTIAL')
        saved=library.save_execution_evidence(self.handle,self.root)
        result=library.verify_execution_continuation(saved['evidence_id'],7000,self.root)
        self.assertEqual(result['knowledge']['account']['orders']['EX-PLAYER-1']['filled'],400)
        self.assertEqual(result['knowledge']['account']['orders']['EX-PLAYER-2']['filled'],100)

    def test_stale_market_refuses_new_risk_and_preserves_cancellation(self):
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=10000)
        self.handle.runtime.delivery.latest_market_state=None
        self.assertEqual(self.act('SUBMIT',side='buy',quantity=1,price_ticks=10000)['refusal']['code'],'STALE_MARKET')
        self.assertIsNone(self.act('CANCEL',order_id='EX-PLAYER-1')['refusal'])
        self.assertTrue(self.account()['orders']['EX-PLAYER-1']['pending_cancel'])

    def test_price_touch_no_fill_and_adverse_queue_fills(self):
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=9998)
        self.act('ADVANCE',delta_us=15000)
        self.assertEqual(self.account()['confirmed_position'],0)
        self.act('FREEZE',policy='PARTIAL')
        touch = practice.frozen_execution_review(self.handle)
        self.assertTrue(any(e['event_type']=='TRADE' and e['data']['price_ticks']==9998 for e in touch['venue_events']))
        practice.close_execution_practice(self.handle)
        self.handle,self.frame=practice.start_execution_practice('execution.queue-pressure.v1',11)
        self.act('SUBMIT',side='buy',quantity=1000,price_ticks=9998)
        self.act('ADVANCE',delta_us=15000)
        self.assertEqual(self.account()['confirmed_position'],1000)
        self.act('FREEZE',policy='COMPLETE')
        review=practice.frozen_execution_review(self.handle)
        trades=[e for e in review['venue_events'] if e['event_type']=='TRADE']
        fills=[e for e in trades if 'EX-PLAYER-1' in e['data'].values()]
        self.assertEqual(sum(e['data']['quantity'] for e in fills),1000)
        self.assertTrue(any(e['simulation_time_us']>=fills[-1]['simulation_time_us'] and e['data']['price_ticks']<9998 for e in trades))

    def test_failed_acquisition_never_publishes_authority(self):
        before=set(practice._ACTIVE)
        with patch.object(practice,'_frame',side_effect=ValueError('projection failed')):
            with self.assertRaises(ValueError): practice.start_execution_practice()
        self.assertEqual(set(practice._ACTIVE),before)

    def test_exact_repeat_has_separate_durable_attempt_identity(self):
        self.act('FREEZE',policy='PARTIAL')
        first=library.save_execution_evidence(self.handle,self.root)
        practice.close_execution_practice(self.handle)
        self.handle,self.frame=practice.start_execution_practice()
        self.act('FREEZE',policy='PARTIAL')
        second=library.save_execution_evidence(self.handle,self.root)
        self.assertNotEqual(first['evidence_id'],second['evidence_id'])
        self.assertEqual(first['checkpoint_sha256'],second['checkpoint_sha256'])
        self.assertEqual(first['review'],second['review'])

    def test_operation_bound_reserves_freeze_and_frozen_evidence_is_immutable(self):
        for _ in range(practice.MAX_OPERATIONS-1):self.act('DISARM')
        with self.assertRaises(ExecutionRefusal):self.act('DISARM')
        self.act('FREEZE',policy='PARTIAL')
        before=practice.frozen_execution_review(self.handle)
        with self.assertRaises(ExecutionRefusal):self.act('FREEZE',policy='PARTIAL')
        self.assertEqual(practice.frozen_execution_review(self.handle),before)
        saved=library.save_execution_evidence(self.handle,self.root)
        self.assertEqual(library.open_execution_evidence(saved['evidence_id'],self.root),saved)


class CommitmentArithmeticAcceptance(unittest.TestCase):
    def test_opposite_sides_duplicate_economics_and_corrections(self):
        ledger=Commitments();ledger.reserve('first','buy',300)
        def message(seq,kind,data):return dict(client_payload=dict(mechanics_sequence=seq,event_type=kind,event_data=data))
        fill=message(1,'TRADE',dict(trade_id='t1',maker_order_id='other',taker_order_id='first',quantity=300))
        ledger.observe(fill);ledger.reserve('buy','buy',500);ledger.reserve('sell','sell',200)
        self.assertEqual(ledger.interval(),(100,800))
        duplicate=copy.deepcopy(fill);duplicate['client_payload']['mechanics_sequence']=99
        ledger.observe(duplicate);self.assertEqual(ledger.interval(),(100,800))
        with self.assertRaises(ExecutionRefusal):ledger.reserve('excess','buy',300)
        ledger.observe(message(2,'TRADE_BUST',{}))
        self.assertEqual(ledger.unknown,'UNSUPPORTED_CORRECTION')
        with self.assertRaises(ExecutionRefusal):ledger.reserve('blocked','buy',1)


if __name__=='__main__': unittest.main()
