from copy import deepcopy
from datetime import datetime,timezone,timedelta
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import research_incidents

NOW=datetime(2026,10,23,6,tzinfo=timezone.utc)
class FakeIssues:
    def __init__(self):self.issues=[];self.comments={};self.creates=0;self.closes=[]
    def list_issues(self):return deepcopy(self.issues)
    def create_issue(self,title,body):
        self.creates+=1;value={'number':self.creates,'title':title,'body':body,'state':'open','user':{'login':'github-actions[bot]'}}
        self.issues.append(value);return deepcopy(value)
    def list_comments(self,number):return deepcopy(self.comments.get(number,[]))
    def comment(self,number,body):self.comments.setdefault(number,[]).append({'body':body,'user':{'login':'github-actions[bot]'}})
    def close_issue(self,number):
        self.closes.append(number)
        next(v for v in self.issues if v['number']==number)['state']='closed'
    def update_issue(self,number,body):next(v for v in self.issues if v['number']==number)['body']=body

def report(day='20261022',*,bad=True,at=NOW,component='results',code='result_labels_missing'):
    return {'schema_version':'operational-health-v1','day':day,'observed_at':at.isoformat(),'status':'degraded' if bad else 'healthy',
        'schedule':{'known':True,'scheduled':86,'eligible':76},
        'capture':{'run_summary':{'end':at.isoformat(),'odds':0 if bad else 10,'error':6 if bad else 0}},
        'results':{'prediction_races':76,'labelled_races':0 if bad else 74,'excluded_result_races':0 if bad else 2,'unmatched_races':76 if bad else 0,'source_errors':{'k':86} if bad else {},'source_successes':{} if bad else {'k':86},'source_success_latest_at':{} if bad else {'k':at.isoformat()}},
        'issues':[{'component':component,'code':code,'severity':'error','detail':'do not expose private content'}] if bad else []}

class IncidentLifecycle(unittest.TestCase):
    def test_first_failure_creates_one_issue_and_repeated_counts_do_not_spam(self):
        client=FakeIssues();r=report()
        research_incidents.sync_report(r,client,now=NOW)
        r['results']['unmatched_races']=75
        research_incidents.sync_report(r,client,now=NOW+timedelta(minutes=25))
        self.assertEqual(client.creates,1);self.assertEqual(client.comments,{})
        self.assertIn('発生',client.issues[0]['body'])
    def test_continuation_is_reported_once_per_japanese_day(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW)
        later=NOW+timedelta(days=1);r=report(at=later)
        research_incidents.sync_report(r,client,now=later);research_incidents.sync_report(r,client,now=later+timedelta(minutes=20))
        self.assertEqual(client.creates,1);self.assertEqual(len(client.comments[1]),1);self.assertIn('継続',client.comments[1][0]['body'])
    def test_same_day_verified_recovery_comments_then_closes(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW)
        later=NOW+timedelta(minutes=10);research_incidents.sync_report(report(bad=False,at=later),client,now=later)
        self.assertEqual(client.closes,[1]);self.assertIn('復旧',client.comments[1][0]['body'])
    def test_other_day_healthy_report_cannot_close_historical_coverage_issue(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW)
        research_incidents.sync_report(report(day='20261023',bad=False,at=NOW+timedelta(minutes=10)),client,now=NOW+timedelta(minutes=10))
        self.assertEqual(client.closes,[])
    def test_provider_outage_continues_across_days_without_a_new_issue(self):
        client=FakeIssues();research_incidents.sync_report(report(component='result_source',code='result_source_all_error'),client,now=NOW)
        later=NOW+timedelta(days=1)
        research_incidents.sync_report(report(day='20261023',at=later,component='result_source',code='result_source_all_error'),client,now=later)
        self.assertEqual(client.creates,1);self.assertEqual(len(client.comments[1]),1)
    def test_provider_recovery_requires_positive_source_evidence(self):
        client=FakeIssues();research_incidents.sync_report(report(component='result_source',code='result_source_all_error'),client,now=NOW)
        later=NOW+timedelta(minutes=10);r=report(bad=False,at=later);r['results']['source_successes']={}
        research_incidents.sync_report(r,client,now=later);self.assertEqual(client.closes,[])
        r['results']['source_successes']={'k':1};r['observed_at']=(later+timedelta(seconds=1)).isoformat()
        research_incidents.sync_report(r,client,now=later+timedelta(seconds=1));self.assertEqual(client.closes,[1])
    def test_stale_healthy_snapshot_cannot_close_newer_failure(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW)
        research_incidents.sync_report(report(bad=False,at=NOW-timedelta(minutes=10)),client,now=NOW+timedelta(minutes=1))
        self.assertEqual(client.closes,[])
    def test_reports_and_remote_comments_cannot_inject_scores_or_secrets(self):
        client=FakeIssues();r=report();r['nll']=4.2;r['token']='private-secret';r['issues'][0]['detail']='private-secret'
        research_incidents.sync_report(r,client,now=NOW,run_url='https://github.com/04globe-boop/mykeirin/actions/runs/123')
        body=client.issues[0]['body'];self.assertNotIn('private-secret',body);self.assertNotIn('nll',body)
        self.assertIn('https://github.com/04globe-boop/mykeirin/actions/runs/123',body)
    def test_untrusted_author_marker_does_not_suppress_a_real_incident(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW)
        client.issues[0]['user']={'login':'random-visitor'}
        research_incidents.sync_report(report(at=NOW+timedelta(minutes=1)),client,now=NOW+timedelta(minutes=1))
        self.assertEqual(client.creates,2)
    def test_issue_service_failure_is_visible_to_caller(self):
        class Broken(FakeIssues):
            def create_issue(self,*args):raise RuntimeError('HTTP 403')
        with self.assertRaisesRegex(RuntimeError,'HTTP 403'):research_incidents.sync_report(report(),Broken(),now=NOW)

    def test_repeated_failure_advances_watermark_without_an_extra_notification(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW)
        later=NOW+timedelta(minutes=25);research_incidents.sync_report(report(at=later),client,now=later)
        research_incidents.sync_report(report(bad=False,at=NOW+timedelta(minutes=10)),client,now=later)
        self.assertEqual(client.closes,[]);self.assertEqual(client.comments,{})

    def test_manually_closed_unresolved_incident_is_not_recreated_every_run(self):
        client=FakeIssues();research_incidents.sync_report(report(),client,now=NOW);client.close_issue(1)
        later=NOW+timedelta(minutes=25);research_incidents.sync_report(report(at=later),client,now=later)
        self.assertEqual(client.creates,1)

    def test_reauditing_old_k_success_does_not_close_a_newer_global_outage(self):
        client=FakeIssues();start=datetime.fromisoformat('2026-10-29T03:00:00+00:00')
        research_incidents.sync_report(report(day='20261028',at=start,component='result_source',code='result_source_all_error'),client,now=start)
        later=datetime.fromisoformat('2026-10-29T04:00:00+00:00')
        value=report(day='20261021',bad=False,at=later)
        value['status']='degraded';value['issues']=[{'component':'inference','code':'forecast_missing'}]
        value['results']['source_success_latest_at']={'k':'2026-10-23T04:00:00+00:00'}
        research_incidents.sync_report(value,client,now=later)
        self.assertEqual(client.closes,[])
        self.assertEqual(value['results']['source_successes']['k'],86)

    def test_provider_success_must_cover_the_latest_incident_watermark(self):
        for provider,component,code in [('k','result_source','result_source_all_error'),('netkeirin','capture','capture_run_all_error')]:
            with self.subTest(provider=provider):
                client=FakeIssues();start=datetime.fromisoformat('2026-10-29T02:00:00+00:00')
                current=datetime.fromisoformat('2026-10-29T03:00:00+00:00')
                later=datetime.fromisoformat('2026-10-29T04:00:00+00:00')
                research_incidents.sync_report(report(day='20261028',at=start,component=component,code=code),client,now=start)
                research_incidents.sync_report(report(day='20261028',at=current,component=component,code=code),client,now=current)
                value=report(day='20261021',bad=False,at=later)
                if provider=='k':value['results']['source_success_latest_at']={'k':'2026-10-29T02:30:00+00:00'}
                else:value['capture']['run_summary']['end']='2026-10-29T02:30:00+00:00'
                research_incidents.sync_report(value,client,now=later)
                self.assertEqual(client.closes,[])
                if provider=='k':value['results']['source_success_latest_at']={'k':'2026-10-29T03:30:00+00:00'}
                else:value['capture']['run_summary']['end']='2026-10-29T03:30:00+00:00'
                research_incidents.sync_report(value,client,now=later)
                self.assertEqual(client.closes,[1])

    def test_missing_naive_malformed_and_future_provider_times_cannot_close(self):
        for provider,component,code in [('k','result_source','result_source_all_error'),('netkeirin','capture','capture_run_all_error')]:
            for captured in (None,'bad-time','2026-10-29T03:30:00','2026-10-29T04:01:00+00:00'):
                with self.subTest(provider=provider,captured=captured):
                    client=FakeIssues();start=datetime.fromisoformat('2026-10-29T03:00:00+00:00')
                    later=datetime.fromisoformat('2026-10-29T04:00:00+00:00')
                    research_incidents.sync_report(report(day='20261028',at=start,component=component,code=code),client,now=start)
                    value=report(day='20261021',bad=False,at=later)
                    if provider=='k':value['results']['source_success_latest_at']={} if captured is None else {'k':captured}
                    elif captured is None:value['capture']['run_summary'].pop('end')
                    else:value['capture']['run_summary']['end']=captured
                    research_incidents.sync_report(value,client,now=later)
                    self.assertEqual(client.closes,[])

if __name__=='__main__':unittest.main()
