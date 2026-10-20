import json,os,sys,tempfile,unittest
from datetime import datetime,timezone
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


if __name__=='__main__':unittest.main()
