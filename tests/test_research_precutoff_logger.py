import gzip,json,os,sys,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from run_logger import Logger,State,JST,PRE_OFFSETS,CARD_OFFSETS,label_of,to_races
from test_logger import FakeClock,FakeClient,mk


class FakeCapture:
    def __init__(self,clock,seconds=1.,fail=False):self.clock=clock;self.seconds=seconds;self.fail=fail;self.calls=[]
    def request(self,race_id,*,budget_seconds,deadline):
        self.calls.append((race_id,budget_seconds,deadline,self.clock.now()))
        self.clock.sleep(min(self.seconds,budget_seconds))
        if self.fail or self.seconds>budget_seconds:raise TimeoutError('deliberate bounded capture timeout')
        return {'official_dt':'','list_9':[['010203',5.]]},self.clock.now().isoformat()


def rows(directory):
    with gzip.open(Path(directory)/'odds_20261020.jsonl.gz','rt',encoding='utf-8') as f:return [json.loads(v) for v in f]


class PrecutoffLoggerIntegrity(unittest.TestCase):
    def test_late_start_extra_slot_does_not_displace_minus_seven_card_catchup(self):
        close=datetime(2026,10,20,12,tzinfo=JST);ck=FakeClock(close-timedelta(minutes=5,seconds=29))
        capture=FakeCapture(ck);client=FakeClient(ck,[mk('202610202701',close)])
        class Hook:
            def __init__(self):self.calls=[]
            def tick(self,races,now,end):
                target=close-timedelta(minutes=5,seconds=20)
                if now<target:return [target]
                if not self.calls:self.calls.append(now)
                return []
        hook=Hook()
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            Logger(client,tmp,.5,clock=ck.now,sleeper=ck.sleep,pre_capture=capture,forecaster=hook,log=lambda *_:None).run()
            data=rows(tmp)
            cards=[v for v in data if v['type'] in ('entries','lines') and v['label']=='-7']
            self.assertEqual(len(cards),2)
            self.assertTrue(all(datetime.fromisoformat(v['fetched_at'])<hook.calls[0] for v in cards))
            self.assertTrue(any(v['type']=='miss' and v['label']=='-5.5' and v['reason']=='precutoff_yielded_existing' for v in data))
            self.assertEqual(capture.calls,[])

    def test_original_labels_and_card_slots_retained_new_slots_resume_once(self):
        self.assertEqual([label_of(v) for v in (-60,-7,-5,+8,15,30)],['-60','-7','-5','+8','+15','+30'])
        self.assertEqual([label_of(v) for v in (-5.5,-2.5,-1.5)],['-5.5','-2.5','-1.5'])
        self.assertTrue(set((-60,-30,-20,-15,-10,-7,-5,-3,-2,-1)).issubset(PRE_OFFSETS))
        self.assertEqual(CARD_OFFSETS,{-60,-7,-5})
        close=datetime(2026,10,20,12,tzinfo=JST);ck=FakeClock(close-timedelta(minutes=11));capture=FakeCapture(ck)
        client=FakeClient(ck,[mk('202610202701',close)])
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            Logger(client,tmp,10.5,clock=ck.now,sleeper=ck.sleep,pre_capture=capture,log=lambda *_:None).run()
            first=rows(tmp);new=[v for v in first if v.get('capture_class')=='additional_pre_cutoff']
            self.assertEqual({v['label'] for v in new},{'-5.5','-2.5','-1.5'})
            for v in new:
                age=(datetime.fromisoformat(v['nominal_cutoff_at'])-datetime.fromisoformat(v['fetched_at'])).total_seconds()
                self.assertEqual(age,29.)
                self.assertEqual(v['capture_budget_seconds'],5.)
            self.assertEqual({v['label'] for v in first if v['type'] in ('lines','entries')},{'-7','-5'})
            self.assertTrue({'-10','-7','-5','-3','-2','-1'}.issubset({v['label'] for v in first if v['type']=='odds'}))
            Logger(client,tmp,.1,clock=ck.now,sleeper=ck.sleep,pre_capture=capture,log=lambda *_:None).run()
            self.assertEqual(len([v for v in rows(tmp) if v.get('capture_class')=='additional_pre_cutoff']),3)
            self.assertEqual(len(capture.calls),3)

    def test_timeout_keeps_live_wakeup_and_original_slot(self):
        close=datetime(2026,10,20,12,tzinfo=JST);ck=FakeClock(close-timedelta(minutes=6));capture=FakeCapture(ck,seconds=20.)
        class Hook:
            def __init__(self):self.calls=[]
            def tick(self,races,now,end):
                target=close-timedelta(minutes=5,seconds=20)
                if now<target:return [target]
                if not self.calls:self.calls.append(now)
                return []
        hook=Hook();client=FakeClient(ck,[mk('202610202701',close)])
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            Logger(client,tmp,1.2,clock=ck.now,sleeper=ck.sleep,pre_capture=capture,forecaster=hook,log=lambda *_:None).run()
            data=rows(tmp)
            self.assertEqual(hook.calls[0],close-timedelta(minutes=5,seconds=20))
            self.assertTrue(any(v['type']=='miss' and v['label']=='-5.5' and v['reason']=='precutoff_capture_failed' for v in data))
            self.assertTrue(any(v['type']=='odds' and v['label']=='-5' for v in data))
            self.assertFalse(any(v['type']=='odds' and v['label']=='-5.5' for v in data))

    def test_fetch_rechecks_current_clock_after_another_race_used_the_budget(self):
        close=datetime(2026,10,20,12,tzinfo=JST);target=close-timedelta(minutes=5.5);ck=FakeClock(target);capture=FakeCapture(ck,seconds=5.)
        races=to_races([mk(f'2026102027{i:02}',close) for i in range(1,4)])
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            logger=Logger(FakeClient(ck,[]),tmp,1,clock=ck.now,pre_capture=capture,log=lambda *_:None);logger.state=State()
            for race in races:logger.fetch(race,-5.5,target,target)
            self.assertEqual([v[1] for v in capture.calls],[5.,1.])
            data=rows(tmp)
            self.assertTrue(any(v['type']=='miss' and v['race_id']==races[-1].race_id and v['reason']=='precutoff_budget_expired' for v in data))
            self.assertLess(ck.now(),close-timedelta(minutes=5,seconds=20))

    def test_late_response_is_not_saved_and_ordinary_capture_still_works(self):
        close=datetime(2026,10,20,12,tzinfo=JST);target=close-timedelta(minutes=5.5);ck=FakeClock(target)
        class BadCapture:
            def request(self,*args,**kwargs):
                ck.sleep(7)
                return {'official_dt':''},ck.now().isoformat()
        race=to_races([mk('202610202701',close)])[0]
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            logger=Logger(FakeClient(ck,[]),tmp,1,clock=ck.now,pre_capture=BadCapture(),log=lambda *_:None);logger.state=State()
            logger.fetch(race,-5.5,target,target)
            ck.t=close-timedelta(minutes=5);logger.fetch(race,-5,ck.now(),ck.now())
            data=rows(tmp)
            self.assertFalse(any(v['type']=='odds' and v['label']=='-5.5' for v in data))
            self.assertTrue(any(v['type']=='odds' and v['label']=='-5' for v in data))

    def test_missing_bounded_worker_never_uses_unbounded_client_for_new_slot(self):
        close=datetime(2026,10,20,12,tzinfo=JST);target=close-timedelta(minutes=5.5);ck=FakeClock(target);client=FakeClient(ck,[])
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            logger=Logger(client,tmp,1,clock=ck.now,log=lambda *_:None);logger.state=State()
            race=to_races([mk('202610202701',close)])[0];logger.fetch(race,-5.5,target,target)
            self.assertEqual(client.calls,[])
            self.assertEqual(rows(tmp)[0]['reason'],'precutoff_worker_unavailable')


if __name__=='__main__':unittest.main()
