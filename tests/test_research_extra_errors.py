import gzip,json,os,sys,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import collect_extra

class ExtraErrorEvidence(unittest.TestCase):
    def test_page_mismatch_and_missing_url_keep_actual_failure_time(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            before=datetime.now(timezone.utc)
            with patch.object(collect_extra,'NetkeirinClient') as nk, patch.object(collect_extra,'kdreams_url',return_value='https://keirin.kdreams.jp/test/'), patch.object(collect_extra,'oddspark_url',return_value=None), patch.object(collect_extra.Fetcher,'get',return_value=(200,b'maintenance')), patch.object(collect_extra,'parse_kdreams',return_value={'page_title':'DERUCA MAINTENANCE'}):
                nk.return_value.race_list.return_value=[{'race_id':'202610221201'}]
                self.assertEqual(collect_extra.main(['--date','2026-10-22','--out-dir',tmp]),0)
            after=datetime.now(timezone.utc)
            rows=[json.loads(v) for v in gzip.open(Path(tmp)/'extra_202610.jsonl.gz','rt',encoding='utf-8')]
            self.assertEqual(len(rows),2);self.assertTrue(all(v['type']=='error' for v in rows))
            for value in rows:self.assertTrue(before<=datetime.fromisoformat(value['fetched_at'])<=after)

if __name__=='__main__':unittest.main()
