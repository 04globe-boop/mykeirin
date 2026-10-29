import gzip,hashlib,itertools,json,os,sqlite3,sys,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import research_health

DAY='20261022'
NOW=datetime(2026,10,23,1,tzinfo=timezone.utc)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def gz(path,values):
    with gzip.open(path,'wt',encoding='utf-8') as f:
        for v in values:f.write(json.dumps(v,ensure_ascii=False)+'\n')

class CoverageContract(unittest.TestCase):
    def fixture(self,root,*,schedule=True,female=False):
        data=root/'data';data.mkdir();bundles={}
        for cohort,ids in [('v1',{'gnn':'a'*64,'market':'b'*64}),('v2',{'gnn':'c'*64,'market':'d'*64})]:
            bundle=root/cohort;bundle.mkdir();(bundle/'bundle.json').write_text(json.dumps({'candidate_ids':ids}),encoding='utf-8');bundles[cohort]=str(bundle)
        rid=DAY+'2701';meta={'race_id':rid,'kaisai_date':DAY,'close':'12:00','jyo_cd':'27'}
        entries=[{'syaban':str(i),'name':'選手'+str(i),'graduate':str(100+i),'kyu':'Ｌ' if female else 'Ａ','han':'１','sales_sts':'7'} for i in range(1,6)]
        records=[{'type':'racelist','kaisai_date':DAY,'fetched_at':'2026-10-22T08:00:00+09:00','data':[meta]},
                 {'type':'entries','race_id':rid,'fetched_at':'2026-10-22T11:00:00+09:00','data':entries}]
        gz(data/f'odds_{DAY}.jsonl.gz',records if schedule else [])
        db=sqlite3.connect(data/f'forward_{DAY}.sqlite')
        db.executescript('CREATE TABLE predictions(race_id TEXT,model_id TEXT,protocol_hash TEXT,decision_at TEXT,close_at TEXT,recorded_at TEXT,frozen_at TEXT,meeting_id TEXT,payload TEXT,sha256 TEXT,synthetic INTEGER,PRIMARY KEY(race_id,model_id));CREATE TABLE labels(race_id TEXT PRIMARY KEY,available_at TEXT,recorded_at TEXT,truth TEXT,source_id TEXT);')
        db.commit();db.close()
        return data,bundles,rid,records
    def prediction(self,data,rid,mid,*,label=False,input_present=True):
        inp={'race_id':rid,'decision_at':'2026-10-22T11:55:00+09:00','truth':None,'synthetic':False}
        payload={'outcomes':[['r1','r2','r3']],'p':[1.0],'input_hash':digest(inp),'primary_horizon_eligible':True}
        db=sqlite3.connect(data/f'forward_{DAY}.sqlite')
        db.execute('INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,0)',(rid,mid,'e'*64,inp['decision_at'],'2026-10-22T12:00:00+09:00','2026-10-22T11:54:40+09:00','2026-10-20T01:00:00+09:00','meeting',json.dumps(payload),digest(payload)))
        if label:db.execute('INSERT OR IGNORE INTO labels VALUES(?,?,?,?,?)',(rid,'2026-10-23T06:00:00+09:00','2026-10-23T06:30:00+09:00',json.dumps(['r1','r2','r3']),'https://example.test/result'))
        db.commit();db.close()
        if input_present:gz(data/f'forward_inputs_{DAY}.jsonl.gz',[inp])
    def test_all_source_errors_report_unmatched_races_instead_of_success(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            extra=data/'extra.jsonl.gz';gz(extra,[{'type':'error','what':'k','race_id':rid,'fetched_at':'2026-10-23T06:00:00+09:00','error':'page mismatch: DERUCA'}])
            report=research_health.audit_day(data,DAY,bundles,now=NOW,extra_path=extra)
            self.assertEqual(report['schedule']['scheduled'],1)
            self.assertEqual(report['results']['unmatched_races'],1)
            self.assertEqual(report['results']['source_errors']['k'],1)
            self.assertIn('result_source_all_error',[v['code'] for v in report['issues']])
            self.assertEqual(report['status'],'degraded')
    def test_unknown_schedule_is_not_reported_as_zero_scheduled(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp),schedule=False)
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertFalse(report['schedule']['known']);self.assertIsNone(report['schedule']['scheduled'])
            self.assertIn('schedule_unknown',[v['code'] for v in report['issues']])
    def test_observed_empty_schedule_is_a_normal_zero_day(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp));gz(data/f'odds_{DAY}.jsonl.gz',[{'type':'racelist','kaisai_date':DAY,'fetched_at':'2026-10-22T08:00:00+09:00','data':[]}])
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertTrue(report['schedule']['known']);self.assertEqual(report['schedule']['scheduled'],0);self.assertEqual(report['status'],'healthy')
    def test_latest_empty_list_does_not_erase_previously_scheduled_races(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,records=self.fixture(Path(tmp));records.append({'type':'racelist','kaisai_date':DAY,'fetched_at':'2026-10-22T11:40:00+09:00','data':[]});gz(data/f'odds_{DAY}.jsonl.gz',records)
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['schedule']['scheduled'],1)
            self.assertIn('unexpected_empty_schedule',[v['code'] for v in report['issues']])
    def test_shared_label_counts_for_both_cohorts_and_legacy_miss_is_not_failure(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp))
            for mid in ('a'*64,'b'*64,'c'*64,'d'*64):self.prediction(data,rid,mid,label=True)
            gz(data/f'forward_events_{DAY}.jsonl.gz',[{'type':'miss','race_id':rid,'at':'2026-10-23T01:00:00+09:00','reason':'past_prediction_cutoff'}]*2)
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['cohorts']['v1']['labelled_races'],1);self.assertEqual(report['cohorts']['v2']['labelled_races'],1)
            self.assertEqual(report['cohorts']['v1']['missing_races'],0)
            self.assertEqual(report['events']['contradicted_miss_races'],1)
            self.assertIn('legacy_miss_conflicts_with_ledger',[v['code'] for v in report['issues']])
    def test_market_missing_does_not_erase_core_predictions(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['cohorts']['v1']['candidates']['gnn']['primary'],1)
            self.assertEqual(report['cohorts']['v1']['candidates']['market']['primary'],0)
            self.assertEqual(report['cohorts']['v1']['partial_races'],1)
    def test_confirmed_out_of_scope_is_separate_from_unverified_eligibility(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp),female=True)
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['schedule']['excluded'],1);self.assertEqual(report['schedule']['eligible'],0)
            self.assertEqual(report['cohorts']['v1']['missing_races'],0)
    def test_missing_input_evidence_is_reported_without_modifying_predictions(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64,input_present=False)
            path=data/f'forward_{DAY}.sqlite';before=path.read_bytes()
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertIn('prediction_input_missing',[v['code'] for v in report['issues']])
            self.assertEqual(path.read_bytes(),before)
    def test_sealed_report_never_contains_performance_fields(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp))
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            def keys(value):
                if isinstance(value,dict):return set(value)|set().union(*(keys(v) for v in value.values()))
                if isinstance(value,list):return set().union(*(keys(v) for v in value))
                return set()
            self.assertFalse({'nll','primary_nll','brier','profit','roi','probabilities','truth'}&keys(report))

    def test_external_summary_extra_fields_are_not_copied_into_operational_report(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp))
            (data/f'logger_summary_{DAY}.json').write_text(json.dumps({'end':'2026-10-22T23:00:00+09:00','races_known':1,'odds':0,'error':6,'nll':4.123,'token':'not-an-actual-secret'}),encoding='utf-8')
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertNotIn('nll',report['capture']['run_summary']);self.assertNotIn('token',report['capture']['run_summary'])
            self.assertIn('capture_run_all_error',[v['code'] for v in report['issues']])

    def test_future_tie_observation_cannot_hide_pending_labels(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            (data/f'forward_observations_{DAY}.jsonl').write_text(json.dumps({'race_id':rid,'status':'excluded_tie','available_at':'2026-10-25T00:00:00+09:00','recorded_at':'2026-10-25T00:00:00+09:00'})+'\n',encoding='utf-8')
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['results']['excluded_result_races'],0);self.assertEqual(report['results']['unmatched_races'],1)

    def test_final_same_timestamp_observation_overrides_preliminary_status(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            values=[dict(race_id=rid,status=s,available_at='2026-10-23T06:00:00+09:00',recorded_at='2026-10-23T06:01:00+09:00') for s in ('ready','excluded_tie')]
            (data/f'forward_observations_{DAY}.jsonl').write_text(''.join(json.dumps(v)+'\n' for v in values),encoding='utf-8')
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['results']['excluded_result_races'],1);self.assertEqual(report['results']['unmatched_races'],0)

    def test_runtime_preparation_and_publish_failures_are_visible_without_raw_errors(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp))
            (data/f'forward_preparation_{DAY}.json').write_text(json.dumps({'installed':{},'failures':{'v2':'private-error'}}))
            (data/f'forward_runtime_{DAY}.jsonl').write_text(json.dumps({'at':'2026-10-22T08:00:00+09:00','bundle':'v2','error':'private-error'})+'\n')
            (data/'research_upload.json').write_text(json.dumps({'status':'refused','errors':['private-error']}))
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertTrue({'cohort_preparation_failed','inference_runtime_failed','canonical_publication_failed'} <= {v['code'] for v in report['issues']})
            self.assertNotIn('private-error',json.dumps(report))

    def test_source_recovery_uses_latest_attempt_and_exposes_positive_evidence(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));extra=data/'extra.jsonl.gz'
            gz(extra,[{'type':'error','what':'k','race_id':rid,'fetched_at':'2026-10-23T06:00:00+09:00'},
                      {'type':'k','race_id':rid,'fetched_at':'2026-10-23T06:20:00+09:00','data':{}}])
            report=research_health.audit_day(data,DAY,bundles,now=NOW,extra_path=extra)
            self.assertEqual(report['results']['source_successes']['k'],1)
            self.assertEqual(report['results'].get('source_success_latest_at'),{'k':'2026-10-23T06:20:00+09:00'})
            self.assertNotIn('result_source_all_error',[v['code'] for v in report['issues']])

    def test_legacy_k_page_mismatch_without_timestamp_is_still_a_visible_failure(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));extra=data/'extra.jsonl.gz'
            gz(extra,[{'type':'error','what':'k','race_id':rid,'error':'page mismatch'}])
            report=research_health.audit_day(data,DAY,bundles,now=NOW,extra_path=extra)
            self.assertEqual(report['results']['source_errors']['k'],1)
            self.assertIn('result_source_all_error',[v['code'] for v in report['issues']])
            self.assertEqual(report['results']['source_errors_time_unknown'],{'k':1})

    def test_extra_race_list_failure_without_race_id_is_not_a_healthy_zero_day(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp));extra=data/'extra.jsonl.gz'
            gz(data/f'odds_{DAY}.jsonl.gz',[{'type':'racelist','kaisai_date':DAY,'fetched_at':'2026-10-22T08:00:00+09:00','data':[]}])
            gz(extra,[{'type':'error','what':'racelist','kaisai_date':DAY,'fetched_at':'2026-10-23T06:00:00+09:00'}])
            report=research_health.audit_day(data,DAY,bundles,now=NOW,extra_path=extra)
            self.assertEqual(report['status'],'degraded')
            self.assertIn('result_source_schedule_failed',[v['code'] for v in report['issues']])

    def test_cutoffs_before_or_at_cohort_freeze_are_separate_from_operational_misses(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,_,_=self.fixture(Path(tmp))
            for cohort,at in [('v1','2026-10-22T11:55:00+09:00'),('v2','2026-10-22T11:56:00+09:00')]:
                path=Path(bundles[cohort])/'bundle.json';value=json.loads(path.read_text());value['frozen_at']=at;path.write_text(json.dumps(value))
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertEqual(report['schedule']['eligible'],1)
            for cohort in bundles:
                self.assertEqual(report['cohorts'][cohort]['expected_races'],0)
                self.assertEqual(report['cohorts'][cohort]['excluded_before_freeze'],1)
                self.assertEqual(report['cohorts'][cohort]['missing_races'],0)

    def test_midnight_wait_for_scheduled_results_is_not_a_label_outage(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            early=datetime(2026,10,22,15,10,tzinfo=timezone.utc)
            report=research_health.audit_day(data,DAY,bundles,now=early)
            self.assertEqual(report['results']['unmatched_races'],1)
            self.assertNotIn('result_labels_missing',[v['code'] for v in report['issues']])
            self.assertFalse(report['results']['due'])

    def missing_source_receipt(self, rid):
        return dict(schema_version='result-observation-v1',race_id=rid,status='unavailable',
                    reason='no_result_observation',available_at=None,source_id=None,
                    source_type=None,source_sha256=None,recorded_at='2026-10-23T06:30:00+09:00')

    def test_missing_source_receipt_is_not_corrupt_evidence_after_results_are_due(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            (data/f'forward_observations_{DAY}.jsonl').write_text(json.dumps(self.missing_source_receipt(rid))+'\n',encoding='utf-8')
            report=research_health.audit_day(data,DAY,bundles,now=NOW)
            self.assertNotIn('observation_evidence_invalid',[v['code'] for v in report['issues']])
            self.assertEqual(report['results']['unmatched_races'],1)
            self.assertIn('result_labels_missing',[v['code'] for v in report['issues']])

    def test_missing_source_receipt_preserves_the_normal_predeadline_wait(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            (data/f'forward_observations_{DAY}.jsonl').write_text(json.dumps(self.missing_source_receipt(rid))+'\n',encoding='utf-8')
            before_due=datetime(2026,10,22,22,tzinfo=timezone.utc)
            report=research_health.audit_day(data,DAY,bundles,now=before_due)
            self.assertNotIn('observation_evidence_invalid',[v['code'] for v in report['issues']])
            self.assertEqual(report['results']['unmatched_races'],1)
            self.assertNotIn('result_labels_missing',[v['code'] for v in report['issues']])

    def test_supplied_result_source_without_time_is_still_invalid_evidence(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
            for field,content in [('source_type','nk_result'),('source_id','https://example.test/result'),('source_sha256','a'*64)]:
                with self.subTest(field=field):
                    value=self.missing_source_receipt(rid);value[field]=content
                    (data/f'forward_observations_{DAY}.jsonl').write_text(json.dumps(value)+'\n',encoding='utf-8')
                    report=research_health.audit_day(data,DAY,bundles,now=NOW)
                    self.assertIn('observation_evidence_invalid',[v['code'] for v in report['issues']])
                    self.assertEqual(report['results']['unmatched_races'],1)

    def test_missing_source_attempt_cannot_refute_confirmed_tie_or_refund(self):
        for status in ('excluded_tie','refund'):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
                data,bundles,rid,_=self.fixture(Path(tmp));self.prediction(data,rid,'a'*64)
                confirmed=dict(schema_version='result-observation-v1',race_id=rid,status=status,
                               source_type='nk_result',source_id='https://example.test/result',source_sha256='a'*64,
                               available_at='2026-10-23T06:00:00+09:00',recorded_at='2026-10-23T06:30:00+09:00')
                missing=self.missing_source_receipt(rid);missing['recorded_at']='2026-10-23T07:00:00+09:00'
                path=data/f'forward_observations_{DAY}.jsonl'
                path.write_text(''.join(json.dumps(v)+'\n' for v in (confirmed,missing)),encoding='utf-8')
                report=research_health.audit_day(data,DAY,bundles,now=NOW)
                self.assertEqual(report['results']['excluded_result_races'],1)
                self.assertEqual(report['results']['unmatched_races'],0)
                self.assertNotIn('result_labels_missing',[v['code'] for v in report['issues']])
                self.assertNotIn('observation_evidence_invalid',[v['code'] for v in report['issues']])
                # A later actual source observation is still a real revision.
                revision=dict(confirmed,status='ready',available_at='2026-10-23T07:15:00+09:00',recorded_at='2026-10-23T07:30:00+09:00')
                with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(revision)+'\n')
                report=research_health.audit_day(data,DAY,bundles,now=NOW)
                self.assertEqual(report['results']['excluded_result_races'],0)
                self.assertEqual(report['results']['unmatched_races'],1)

if __name__=='__main__':unittest.main()
