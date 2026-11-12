"""Regression of the observed handoff hole; no primary forecast is backfilled."""
import os,sys,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from run_logger import Logger,JST
from test_logger import FakeClock,FakeClient,mk

class WindowForecaster:
    """An external predictor with the frozen trigger/end contract, no model fitting."""
    def __init__(self,clock):
        self.clock=clock;self.issued=[];self.handled=set()
    def tick(self,races,now,end):
        upcoming=[]
        for race in races:
            cutoff=race.close-timedelta(minutes=5);trigger=cutoff-timedelta(seconds=20)
            if race.race_id in self.handled or trigger>end:continue
            if now<trigger:upcoming.append(trigger);continue
            self.handled.add(race.race_id)
            if now>cutoff:continue
            for cohort in ('v1','v2'):self.issued.append((cohort,race.race_id,self.clock.now(),cutoff))
        return upcoming
    def close(self):pass

class HandoffWindow(unittest.TestCase):
    def run_case(self,start,minutes,closes,*,forecast=True,hook=None):
        clock=FakeClock(start);races=[mk(f'{close:%Y%m%d}27{i:02}',close) for i,close in enumerate(closes,1)]
        client=FakeClient(clock,races);client._tick=lambda:None
        predictor=hook(clock) if hook else WindowForecaster(clock) if forecast else None
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as directory:
            logger=Logger(client,directory,minutes,clock=clock.now,sleeper=clock.sleep,forecaster=predictor,log=lambda *_:None)
            summary=logger.run()
            if predictor:
                original=list(predictor.issued)
                Logger(client,directory,.1,clock=clock.now,sleeper=clock.sleep,forecaster=predictor,log=lambda *_:None).run()
                self.assertEqual(predictor.issued[:len(original)],original,'Repeated invocation must preserve existing in-memory receipts')
                keys=[(v[0],v[1]) for v in predictor.issued]
                self.assertEqual(len(keys),len(set(keys)),'Repeated invocation must not issue the same cohort/race twice')
        return summary,predictor

    def test_observed_204931_finish_keeps_204940_trigger_for_both_cohorts(self):
        start=datetime(2026,11,12,20,24,31,tzinfo=JST)
        summary,p=self.run_case(start,25,[datetime(2026,11,12,20,55,tzinfo=JST)])
        self.assertEqual([x[0] for x in p.issued],['v1','v2'],'Both cohorts lost the known near-end forecast')
        self.assertTrue(all(x[2]==datetime(2026,11,12,20,49,40,tzinfo=JST) and x[2]<x[3] for x in p.issued))
        self.assertLessEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=28))

    def test_forecast_at_the_handoff_lookahead_boundary_is_protected(self):
        start=datetime(2026,11,12,20,22,40,tzinfo=JST)
        _,p=self.run_case(start,25,[datetime(2026,11,12,20,55,tzinfo=JST)])
        self.assertEqual(len(p.issued),2,'An exactly 120-second-away trigger must not be lost')

    def test_no_nearby_prediction_does_not_extend_capture(self):
        start=datetime(2026,11,12,20,20,tzinfo=JST)
        summary,p=self.run_case(start,25,[datetime(2026,11,12,20,55,tzinfo=JST)])
        self.assertEqual(p.issued,[])
        self.assertEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=25))

    def test_no_predictor_keeps_original_window(self):
        start=datetime(2026,11,12,20,24,31,tzinfo=JST)
        summary,_=self.run_case(start,25,[datetime(2026,11,12,20,55,tzinfo=JST)],forecast=False)
        self.assertEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=25))

    def test_failed_predictor_does_not_keep_extending(self):
        class Failed(WindowForecaster):
            def tick(self,*args):raise RuntimeError('unavailable predictor')
        start=datetime(2026,11,12,20,24,31,tzinfo=JST)
        summary,p=self.run_case(start,25,[datetime(2026,11,12,20,55,tzinfo=JST)],hook=Failed)
        self.assertEqual(p.issued,[])
        self.assertEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=25))

    def test_dense_schedule_cannot_extend_indefinitely(self):
        start=datetime(2026,11,12,20,24,31,tzinfo=JST)
        closes=[datetime(2026,11,12,20,55,tzinfo=JST)+timedelta(minutes=i) for i in range(8)]
        summary,p=self.run_case(start,25,closes)
        self.assertLessEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=28))
        self.assertTrue(all(x[2]<=x[3] for x in p.issued))
        self.assertGreaterEqual(len(p.issued),2)
        self.assertTrue(summary.get('handoff_guard_capped'))

    def test_midnight_refresh_protects_new_day_prediction(self):
        start=datetime(2026,11,12,23,50,tzinfo=JST)
        closes=[datetime(2026,11,12,23,59,tzinfo=JST),datetime(2026,11,13,0,9,tzinfo=JST)]
        summary,p=self.run_case(start,12,closes)
        self.assertEqual(len(p.issued),4)
        self.assertEqual({(x[1][:8],x[2].strftime('%Y%m%d')) for x in p.issued},{('20261112','20261112'),('20261113','20261113')})
        self.assertTrue(all(x[2]<=x[3] for x in p.issued))
        self.assertLessEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=15))

    def test_prepare_restores_the_day_reached_by_the_guard_extension(self):
        from unittest.mock import patch
        import research_assets
        from contextlib import chdir
        start=datetime(2026,11,12,23,34,31,tzinfo=JST);restored=[]
        def download(repo,directory,day):restored.append(day);return {'ok':True}
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as directory,chdir(directory):
            with patch.object(research_assets,'datetime') as time,patch.object(research_assets,'state_download',side_effect=download),patch.object(research_assets,'install_bundles',return_value={}):
                time.now.return_value=start
                with patch.object(sys,'argv',['research_assets.py','prepare','--repo','owner/private','--window-min','25']):research_assets.main()
        self.assertEqual(restored,['20261112','20261113'])

    def test_empty_runtime_group_keeps_original_window(self):
        class Empty(WindowForecaster):
            workers=[]
            def tick(self,*args):return []
        start=datetime(2026,11,12,20,24,31,tzinfo=JST)
        summary,p=self.run_case(start,25,[datetime(2026,11,12,20,55,tzinfo=JST)],hook=Empty)
        self.assertEqual(datetime.fromisoformat(summary['end']),start+timedelta(minutes=25))

    def test_last_worker_failure_inside_tick_ends_without_an_extra_sleep(self):
        class DropsLastWorker(WindowForecaster):
            def __init__(self,clock):super().__init__(clock);self.workers=[True]
            def tick(self,*args):self.workers.clear();return []
        start=datetime(2026,11,12,20,49,31,tzinfo=JST)
        summary,p=self.run_case(start,0,[datetime(2026,11,12,20,55,tzinfo=JST)],hook=DropsLastWorker)
        self.assertEqual(datetime.fromisoformat(summary['end']),start,'An internally failed last worker must not delay handoff')

if __name__=='__main__':unittest.main()
