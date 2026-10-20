import hashlib,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'logger'))
from page_parse import parse_kdreams
from collect_extra import k_page_ok


class SharedResultContract(unittest.TestCase):
    def test_current_producer_matches_versioned_research_contract(self):
        cfg=json.loads((ROOT/'contracts/result-v1.json').read_text(encoding='utf-8'))
        actual=parse_kdreams((ROOT/'contracts/result-v1.html').read_bytes());record=cfg['record']
        self.assertEqual(actual['result'],record['data']['result'])
        self.assertTrue(k_page_ok(record['race_id'],actual))


if __name__=='__main__':unittest.main()
