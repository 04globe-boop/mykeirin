import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import run_logger

class CaptureReceipt(unittest.TestCase):
    def test_all_error_run_preserves_score_free_summary_before_normal_return(self):
        summary={'start':'2026-10-21T22:38:00+09:00','end':'2026-10-21T23:03:00+09:00','races_known':0,'odds':0,'lines':0,'entries':0,'miss':0,'error':6}
        with tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
            with patch.object(run_logger.Logger,'run',return_value=summary):
                self.assertEqual(run_logger.main(['--out-dir',tmp,'--window-min','0']),0)
            files=list(Path(tmp).glob('logger_summary_20261021*.json'))
            self.assertEqual(len(files),1)
            value=json.loads(files[0].read_text(encoding='utf-8'))
            self.assertEqual(value['error'],6);self.assertEqual(value['races_known'],0)
            self.assertEqual(value['schema_version'],'capture-summary-v1')

if __name__=='__main__':unittest.main()
