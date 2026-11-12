"""The publication job must work without capture-only third-party packages."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
class PublisherStandardLibrary(unittest.TestCase):
    def setUp(self):
        self.env=os.environ.copy();self.env.pop("PYTHONPATH",None)
        if os.name=="nt":
            # Windows lacks the OS timezone files installed on Ubuntu runners.
            # Supply data files only; the isolated child cannot import tzdata.
            import tzdata
            self.env["PYTHONTZPATH"]=str(Path(tzdata.__file__).parent/"zoneinfo")

    def test_actual_publication_entrypoints_import_without_site_packages(self):
        for name in ('research_assets.py','research_handoff.py','research_health.py','research_health_publish.py'):
            with self.subTest(name=name):
                result=subprocess.run([sys.executable,'-S',str(ROOT/'logger'/name),'--help'],capture_output=True,text=True,timeout=15,env=self.env)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_real_pack_and_score_free_audit_without_site_packages(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as directory:
            data=Path(directory)/'data';data.mkdir();report=Path(directory)/'health.json'
            for name,args in [('research_assets.py',['pack','--data-dir',str(data),'--stage','capture']),
                              ('research_health.py',['--data-dir',str(data),'--day','20261112','--output',str(report)])]:
                result=subprocess.run([sys.executable,'-S',str(ROOT/'logger'/name),*args],capture_output=True,text=True,timeout=15,env=self.env)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((data/'research_pack.json').is_file())
            value=json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(value['status'],'degraded')
            self.assertIn('cohorts_unavailable',{v['code'] for v in value['issues']})
            self.assertFalse(value['performance_reported'])

if __name__=='__main__':unittest.main()
