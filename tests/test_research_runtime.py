import json,os,sys,tempfile,unittest,sqlite3
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import research_runtime,research_assets


class FrozenCohorts(unittest.TestCase):
    def fixture(self,root,name,value,broken=False):
        bundle=root/name/'bundle';p=bundle/'source/keirin_lab';p.mkdir(parents=True)
        (p/'__init__.py').write_text('')
        (p/'constant.py').write_text('VALUE='+repr(value))
        (p/'live_worker.py').write_text('''from pathlib import Path
from .constant import VALUE
class LiveWorker:
    def __init__(self,bundle,out_dir,log=print):
        self.root=Path(bundle);self.models={VALUE:None};self.frozen_at='fixed';self.closed=False
    def tick(self,races,now,end):
        assert list(races)==['same-race']
        if VALUE=='broken':raise ValueError('failed cohort')
        return [VALUE]
    def close(self):self.closed=True
''')
        (p/'settlement.py').write_text('''from .constant import VALUE
def settle_day(bundle,out,extra,day):return {'cohort':VALUE,'settled':1}
''')
        return bundle

    def test_each_bundle_imports_its_own_relative_modules(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root=Path(tmp);a=self.fixture(root,'v1','old');b=self.fixture(root,'v2','new')
            group=research_runtime.ResearchGroup([a,b],root/'out',log=lambda x:None)
            self.assertEqual(group.tick(iter(['same-race']),None,None),['old','new'])
            self.assertIsNot(type(group.workers[0]),type(group.workers[1]));group.close()
            self.assertTrue(all(w.closed for w in group.workers))

    def test_failed_cohort_does_not_disable_other_worker(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root=Path(tmp);a=self.fixture(root,'v1','broken');b=self.fixture(root,'v2','healthy')
            group=research_runtime.ResearchGroup([a,b],root/'out',log=lambda x:None)
            self.assertEqual(group.tick(['same-race'],None,None),['healthy'])
            self.assertEqual(len(group.workers),1);self.assertTrue(list((root/'out').glob('forward_runtime_*.jsonl')));group.close()

    def test_settlement_preserves_reports_per_cohort(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root=Path(tmp);a=self.fixture(root,'v1','old');b=self.fixture(root,'v2','new');out=root/'out';out.mkdir()
            result=research_runtime.settle_bundles({'v1':a,'v2':b},out,'unused','20261020')
            self.assertEqual(result['cohorts']['v1']['cohort'],'old')
            self.assertEqual(json.loads((out/'forward_report_v2_20261020.json').read_text())['cohort'],'new')

    def test_real_manifest_uses_versioned_observed_adapter_and_holdout_guard(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root=Path(tmp);bundle=self.fixture(root,'v1','legacy');out=root/'out';out.mkdir()
            (bundle/'bundle.json').write_text(json.dumps({'candidate_ids':{}}))
            raw={'summaries':{'gnn':{'settled_races':2,'nll':4.2,'primary_nll':4.1}}}
            with patch('research_results.settle_observed_day',return_value=raw) as adapter, patch.object(research_runtime,'normalize_summary_days'):
                result=research_runtime.settle_bundles({'v1':bundle},out,'extra.gz','20261121',nk_path='nk.gz')
            self.assertEqual(adapter.call_args.args,(bundle,out,'extra.gz','nk.gz','20261121'))
            self.assertIs(adapter.call_args.kwargs['module_loader'],research_runtime.bundle_module)
            self.assertIsNone(result['cohorts']['v1']['summaries']['gnn']['nll'])

    def test_one_unavailable_archive_preserves_available_cohort(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root=Path(tmp);ref=root/'references.json';ref.write_text(json.dumps({'bundles':{'v1':{},'v2':{}}}))
            def install(repo,reference,destination):
                if Path(destination).name=='v2':raise ValueError('hash mismatch')
                return Path(destination)/'bundle'
            with patch.object(research_assets,'install_bundle',side_effect=install):
                installed=research_assets.install_bundles('fixed',ref,root/'runtime')
            self.assertEqual(set(installed),{'v1'})
            self.assertIn('v2',json.loads((root/'runtime/preparation.json').read_text())['failures'])

    def test_reserved_period_reports_coverage_without_interim_performance(self):
        raw={'summaries':{'gnn':{'nll':4.1,'primary_nll':4.0,'brier':0.9,'settled_races':20}}}
        result=research_runtime.protect_test_report(raw,'20261121',now=datetime(2026,12,1,tzinfo=timezone.utc))
        self.assertIsNone(result['summaries']['gnn']['nll'])
        self.assertIsNone(result['summaries']['gnn']['primary_nll'])
        self.assertEqual(result['summaries']['gnn']['settled_races'],20)
        self.assertIn('performance_sealed_until',result)
        self.assertNotIn('brier',result['summaries']['gnn'])

    def test_frozen_legacy_summary_uses_one_japanese_day_across_utc_midnight(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            path=Path(tmp)/'ledger.sqlite';db=sqlite3.connect(path)
            db.execute('CREATE TABLE predictions(race_id TEXT,model_id TEXT,close_at TEXT,payload TEXT,synthetic INTEGER)')
            db.execute('CREATE TABLE labels(race_id TEXT)')
            for i,close in enumerate(('2026-10-19T23:50:00+00:00','2026-10-20T04:25:00+00:00')):
                db.execute('INSERT INTO predictions VALUES(?,?,?,?,0)',(str(i),'fixed',close,json.dumps({'primary_horizon_eligible':True})))
                db.execute('INSERT INTO labels VALUES(?)',(str(i),))
            db.commit();db.close()
            report={'summaries':{'gnn':{'days':2,'primary_days':2,'nll':4.1}}}
            research_runtime.normalize_summary_days(report,path,{'candidate_ids':{'gnn':'fixed'}})
            self.assertEqual(report['summaries']['gnn']['days'],1)
            self.assertEqual(report['summaries']['gnn']['primary_days'],1)
            self.assertEqual(report['summaries']['gnn']['nll'],4.1)


class RestartIssuance(unittest.TestCase):
    def worker(self, root, name, candidate_ids):
        bundle = root / name / 'bundle'
        source = bundle / 'source/keirin_lab'
        source.mkdir(parents=True)
        (source / '__init__.py').write_text('')
        (bundle / 'bundle.json').write_text(json.dumps({'candidate_ids': candidate_ids}))
        # Mirrors the frozen branch that emits an expired-cutoff miss before opening a ledger.
        (source / 'live_worker.py').write_text('''import json
from pathlib import Path
class LiveWorker:
    def __init__(self,bundle,out_dir,log=print):
        self.root=Path(bundle);self.out=Path(out_dir);self.handled=set();self.models={};self.frozen_at='fixed'
        self.manifest=json.loads((self.root/'bundle.json').read_text())
    def tick(self,races,now,end):
        for race in races:
            if race.race_id in self.handled:continue
            if now>race.close:
                self.handled.add(race.race_id)
                with (self.out/('forward_events_'+race.kaisai_date+'.jsonl')).open('a') as stream:
                    stream.write(json.dumps({'type':'miss','race_id':race.race_id})+'\\n')
        return []
    def close(self):pass
''')
        return bundle

    def test_expired_restart_uses_only_its_own_cohort_and_suppresses_partial_false_miss(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root = Path(tmp); out = root/'data'; out.mkdir()
            a = self.worker(root, 'v1', {'a':'v1-a', 'b':'v1-b'})
            b = self.worker(root, 'v2', {'a':'v2-a', 'b':'v2-b'})
            db = sqlite3.connect(out/'forward_20261022.sqlite')
            db.execute('CREATE TABLE predictions(race_id TEXT,model_id TEXT,PRIMARY KEY(race_id,model_id))')
            # r-complete is v1 complete, r-partial is v1 partial, v2 has neither.
            db.executemany('INSERT INTO predictions VALUES(?,?)', [('r-complete','v1-a'),('r-complete','v1-b'),('r-partial','v1-a')])
            db.commit(); db.close()
            before = datetime(2026,10,22,12,tzinfo=timezone.utc)
            races = [SimpleNamespace(race_id=rid,kaisai_date='20261022',close=before) for rid in ('r-complete','r-partial','r-empty')]
            group = research_runtime.ResearchGroup([a,b],out,log=lambda value:None)
            self.addCleanup(group.close)
            group.tick(races,before+timedelta(hours=1),before+timedelta(hours=2))
            events = [json.loads(line) for line in (out/'forward_events_20261022.jsonl').read_text().splitlines()]
            # Complete + partial v1 must never become new total misses; v2 remains independent.
            self.assertEqual([e['race_id'] for e in events if e['type']=='miss'], ['r-empty','r-complete','r-partial','r-empty'])
            self.assertIn('r-partial',group.workers[0].handled)
            self.assertIn('r-partial',group.workers[0]._prior_issuance['20261022'])
            self.assertEqual(group.workers[0]._prior_issuance['20261022']['r-partial'], {'v1-a'})

    def test_before_cutoff_partial_issuance_is_not_marked_globally_handled(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            root = Path(tmp); out=root/'data'; out.mkdir()
            bundle = self.worker(root,'v1',{'a':'v1-a','b':'v1-b'})
            db=sqlite3.connect(out/'forward_20261022.sqlite')
            db.execute('CREATE TABLE predictions(race_id TEXT,model_id TEXT)')
            db.execute('INSERT INTO predictions VALUES(?,?)',('r','v1-a')); db.commit(); db.close()
            now=datetime(2026,10,22,12,tzinfo=timezone.utc)
            race=SimpleNamespace(race_id='r',kaisai_date='20261022',close=now+timedelta(minutes=10))
            group=research_runtime.ResearchGroup([bundle],out,log=lambda value:None)
            self.addCleanup(group.close)
            group.tick([race],now,now+timedelta(minutes=20))
            self.assertNotIn('r',group.workers[0].handled)


if __name__=='__main__':unittest.main()
