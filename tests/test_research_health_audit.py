import copy,hashlib,json,sys,unittest,os,tempfile
from unittest.mock import patch
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from test_research_health_publish import GitHubStore,report,NOW,REPO,RUN_URL
from research_health_audit import choose_day,stage_report,finish_report,prepare_report,stable_snapshot

class PostDeadlineAudit(unittest.TestCase):
    def stage(self,client,value=None):
        return stage_report(value or report(day='20261022'),client,run_id='12345',attempt='1',run_url=RUN_URL,now=NOW)
    def test_target_is_prior_day_after_the_existing_deadline(self):
        at=datetime.fromisoformat('2026-11-12T08:17:00+09:00')
        self.assertEqual(choose_day(at),'20261111')
        for when,day in [('2026-11-12T07:59:00+09:00','20261111'),('2026-11-12T08:17:00+09:00','20261112')]:
            with self.subTest(when=when),self.assertRaises(ValueError):choose_day(datetime.fromisoformat(when),day)
        with self.assertRaises(ValueError):choose_day(datetime(2026,11,12,8,17))
    def test_stage_preserves_sanitized_private_evidence_without_notification(self):
        client=GitHubStore();value=report(day='20261022');value['secret']='do-not-copy';value['results']['unmatched_race_ids']=['202610222701']
        result=self.stage(client,value)
        self.assertEqual(client.dispatches,[]);self.assertIsNone(client.last_receipt)
        raw=client.assets[result['asset_name']]
        self.assertNotIn(b'do-not-copy',raw);self.assertNotIn(b'202610222701',raw)
        self.assertTrue(all(not n.startswith('notification-receipt-') for n in client.assets))
    def test_finish_delivers_the_same_staged_evidence_and_deduplicates(self):
        client=GitHubStore();staged=self.stage(client)
        result=finish_report(client,day=staged['day'],asset_name=staged['asset_name'],run_id='12345',attempt='1',run_url=RUN_URL,now=NOW)
        self.assertTrue(result['dispatched']);self.assertEqual(len(client.dispatches),1)
        again=finish_report(client,day=staged['day'],asset_name=staged['asset_name'],run_id='12345',attempt='1',run_url=RUN_URL,now=NOW)
        self.assertFalse(again['dispatched']);self.assertEqual(len(client.dispatches),1)
    def test_wrong_source_run_or_day_is_rejected(self):
        for day,run in [('20261021','12345'),('20261022','99999')]:
            client=GitHubStore();s=self.stage(client)
            with self.assertRaises(ValueError):finish_report(client,day=day,asset_name=s['asset_name'],run_id=run,attempt='1',run_url=RUN_URL,now=NOW)
            self.assertEqual(client.dispatches,[])
    def test_tamper_or_public_repository_cannot_publish(self):
        client=GitHubStore();s=self.stage(client);client.assets[s['asset_name']]=b'{}'
        with self.assertRaises(ValueError):finish_report(client,day=s['day'],asset_name=s['asset_name'],run_id='12345',attempt='1',run_url=RUN_URL,now=NOW)
        client=GitHubStore();client.repository['private']=False
        with self.assertRaises(ValueError):self.stage(client)
        self.assertEqual(client.assets,{})
    def test_failed_delivery_never_records_success(self):
        client=GitHubStore();s=self.stage(client);client.fail_on='dispatch'
        with self.assertRaises(OSError):finish_report(client,day=s['day'],asset_name=s['asset_name'],run_id='12345',attempt='1',run_url=RUN_URL,now=NOW)
        self.assertIsNone(client.last_receipt)
    def test_restore_failure_stays_unknown_and_cannot_resolve_an_old_gap(self):
        with patch('research_health_audit.research_assets.state_download',return_value={'ok':False}), patch('research_health_audit.research_assets.install_bundles') as install:
            r=prepare_report('20261022',NOW)
        self.assertIs(r['schedule']['known'],False);self.assertEqual(r['results'],{})
        self.assertEqual(r['issues'][0]['component'],'evidence');install.assert_not_called()
    def test_snapshot_changed_during_read_is_rejected(self):
        name='forward_20261022.sqlite.gz';receipt={'assets':{name:{'status':'restored','asset_id':7,'updated_at':'t1'}}}
        with patch('research_health_audit.research_assets._api_json',return_value=(200,{'assets':[{'name':name,'id':7,'updated_at':'t1'}]})):
            self.assertTrue(stable_snapshot(REPO,'20261022',receipt))
        with patch('research_health_audit.research_assets._api_json',return_value=(200,{'assets':[{'name':name,'id':8,'updated_at':'t2'}]})):
            self.assertFalse(stable_snapshot(REPO,'20261022',receipt))

    def test_prepare_cli_outputs_two_machine_readable_lines(self):
        import research_health_audit as module
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            output=Path(tmp)/'out';client=GitHubStore()
            env={'GITHUB_RUN_ID':'12345','GITHUB_RUN_ATTEMPT':'1','GH_TOKEN':'fake','GITHUB_OUTPUT':str(output)}
            with patch.dict(os.environ,env),patch.object(sys,'argv',['audit','prepare','--day','20261022']),patch.object(module,'GitHubHealthStore',return_value=client),patch.object(module,'prepare_report',return_value=report(day='20261022')):
                module.main()
            lines=output.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines),2);self.assertEqual(lines[0],'day=20261022')
            self.assertTrue(lines[1].startswith('asset=health-20261022-run-12345-'))

if __name__=='__main__':unittest.main()
