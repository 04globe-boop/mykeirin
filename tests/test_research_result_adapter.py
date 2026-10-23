"""Pure NK result observations: provenance, identity parsing and exclusions."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'logger'))
try:
    from result_adapter import adapt_nk_result
except ModuleNotFoundError as error:
    if error.name != 'result_adapter':
        raise

    def adapt_nk_result(record):
        raise AssertionError('result_adapter.adapt_nk_result is not implemented')


def published_record():
    """A complete literal fixture in the independently collected NK schema."""
    return {
        'type': 'nk_result',
        'race_id': '202610211201',
        'url': 'https://keirin.netkeiba.com/race/result/?race_id=202610211201',
        'fetched_at': '2026-10-23T13:46:19.602114+09:00',
        'data': {
            'title': '青森競輪 テスト FII 2026年10月21日 1R チャレンジ一般 結果・払戻金 | netkeirin',
            'result': [
                ['着', '枠番', '車番', '選手名', '着差 (個人情報)', '上り', '決', 'SB'],
                ['1着', '1', '1', '櫻川雅彦 千葉 42歳 99期 Ａ3', '', '12.8', '差', 'S'],
                ['2着', '5', '5', '奥山雅士 静岡 56歳 65期 Ａ3', '3/4車身', '12.8', 'ク', ''],
                ['3着', '3', '3', '吉原友彦 神奈 48歳 83期 Ａ3', '1/2車身', '13.2', '', 'B'],
                ['4着', '4', '4', '富永昌久 佐賀 46歳 88期 Ａ3', '3/4車輪', '12.7', '', ''],
                ['5着', '6', '7', '中野功史 長崎 48歳 84期 Ａ3', '1/2車身', '12.6', '', ''],
                ['6着', '2', '2', '矢田晋 福岡 48歳 94期 Ａ3', '3車身', '12.6', '', ''],
                ['7着', '6', '6', '落合豊 茨城 55歳 69期 Ａ3', '1車輪', '12.9', '', ''],
            ],
            'payout': {
                '２車複': [['1-5', 330, 2]],
                '２車単': [['1>5', 400, 2]],
                'ワイド': [['1-5', 110, 1], ['1-3', 120, 2], ['3-5', 170, 3]],
                '３連複': [['1-3-5', 180, 1]],
                '３連単': [['1>5>3', 610, 2]],
            },
            'status': '確定',
        },
    }


class NKResultObservation(unittest.TestCase):
    def assert_invalid(self, record, reason=None):
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'invalid')
        self.assertIsNone(observation['truth_cars'])
        self.assertTrue(observation['reason'])
        if reason:
            self.assertEqual(observation['reason'], reason)
        return observation

    def test_published_result_preserves_provenance_and_normalizes_identity(self):
        record = published_record()
        observation = adapt_nk_result(record)
        self.assertEqual(observation['schema_version'], 'result-observation-v1')
        self.assertEqual(observation['source_type'], 'nk_result')
        self.assertEqual(observation['race_id'], '202610211201')
        self.assertEqual(observation['source_id'], record['url'])
        self.assertEqual(observation['available_at'], '2026-10-23T13:46:19.602114+09:00')
        expected_hash = hashlib.sha256(json.dumps(
            record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
        self.assertEqual(observation['source_sha256'], expected_hash)
        self.assertEqual(observation['status'], 'ready')
        self.assertEqual(observation['truth_cars'], ['1', '5', '3'])
        self.assertEqual(observation['payouts'], [{'cars': ['1', '5', '3'], 'yen': 610}])
        self.assertEqual(observation['rows'][0], {
            'car': '1', 'name': '櫻川雅彦', 'graduate': 99,
            'rank': 1, 'status': '1着', 'finish': '差', 'outcome_detail': '',
        })
        self.assertEqual(observation['rows'][2]['name'], '吉原友彦')
        self.assertEqual(observation['withdrawn_cars'], [])
        self.assertFalse(observation['requires_withdrawal_verification'])
        self.assertFalse(observation['refund_details_available'])

    def test_normalization_keeps_name_and_graduate_separate(self):
        record = published_record()
        record['data']['result'][1][3] = '櫻川　雅彦 千葉 ４２歳 ９９期 Ａ３'
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'ready')
        self.assertEqual(observation['rows'][0]['name'], '櫻川雅彦')
        self.assertEqual(observation['rows'][0]['graduate'], 99)

    def test_adaptation_is_pure_and_hash_includes_unconsumed_raw_fields(self):
        record = published_record()
        before = copy.deepcopy(record)
        first = adapt_nk_result(record)
        self.assertEqual(record, before)
        record['collector_note'] = 'raw metadata'
        self.assertNotEqual(first['source_sha256'], adapt_nk_result(record)['source_sha256'])
        first['rows'][0]['name'] = 'changed'
        self.assertEqual(adapt_nk_result(record)['rows'][0]['name'], '櫻川雅彦')

    def test_third_place_tie_remains_excluded_with_both_payouts(self):
        record = published_record()
        record['data']['result'][4][0] = '3着'
        record['data']['payout']['３連単'] = [['1>5>3', 380, 1], ['1>5>4', 410, 2]]
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'excluded_tie')
        self.assertEqual(observation['reason'], 'top_three_tie')
        self.assertIsNone(observation['truth_cars'])
        self.assertEqual(observation['payouts'], [
            {'cars': ['1', '5', '3'], 'yen': 380},
            {'cars': ['1', '5', '4'], 'yen': 410},
        ])
        self.assertEqual([row['rank'] for row in observation['rows'][:4]], [1, 2, 3, 3])

    def test_second_place_tie_is_not_collapsed_into_one_order(self):
        record = published_record()
        record['data']['result'][3][0] = '2着'
        record['data']['payout']['３連単'] = [['1>5>3', 31880, 110], ['1>3>5', 74290, 174]]
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'excluded_tie')
        self.assertIsNone(observation['truth_cars'])
        self.assertEqual(len(observation['payouts']), 2)

    def test_lower_place_tie_does_not_exclude_a_unique_top_three(self):
        record = published_record()
        record['data']['result'][5][0] = '4着'
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'ready')
        self.assertEqual(observation['truth_cars'], ['1', '5', '3'])

    def test_retirement_and_disqualification_are_preserved_below_unique_top_three(self):
        record = published_record()
        record['data']['result'][6][0] = '棄'
        record['data']['result'][6][4] = '－ (落車棄権)'
        record['data']['result'][7][0] = '失'
        record['data']['result'][7][4] = '－ (失格)'
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'ready')
        self.assertEqual(observation['rows'][-2]['status'], '棄')
        self.assertEqual(observation['rows'][-1]['status'], '失')
        self.assertEqual(observation['rows'][-2].get('outcome_detail'), '-(落車棄権)')
        self.assertEqual(observation['rows'][-1].get('outcome_detail'), '-(失格)')
        self.assertIsNone(observation['rows'][-1]['rank'])
        self.assertEqual(len(observation['rows']), 7)

    def test_withdrawal_preserves_label_candidate_and_requires_outer_universe_check(self):
        record = published_record()
        record['data']['result'][7][0] = '欠'
        record['data']['result'][7][4] = '－ (欠場)'
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'ready')
        self.assertEqual(observation['truth_cars'], ['1', '5', '3'])
        self.assertEqual(observation['withdrawn_cars'], ['6'])
        self.assertTrue(observation['requires_withdrawal_verification'])
        self.assertFalse(observation['refund_details_available'])
        self.assertEqual(observation['rows'][-1]['status'], '欠')

    def test_partial_refund_uncertainty_does_not_destroy_a_unique_label(self):
        record = published_record()
        record['data']['payout']['２車複'] = []
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'ready')
        self.assertEqual(observation['truth_cars'], ['1', '5', '3'])
        self.assertFalse(observation['refund_details_available'])
        self.assertEqual(observation['payouts'], [{'cars': ['1', '5', '3'], 'yen': 610}])

    def test_whole_refund_has_no_truth_or_zero_payment_assumption(self):
        record = published_record()
        record['data'].update(status='返還', result=[], payout={'３連単': []})
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'refund')
        self.assertEqual(observation['reason'], 'whole_refund')
        self.assertIsNone(observation['truth_cars'])
        self.assertEqual(observation['payouts'], [])
        self.assertFalse(observation['refund_details_available'])

    def test_pending_retains_actual_fetch_time_without_inventing_a_result(self):
        record = published_record()
        record['data'].update(status='未確定', result=[], payout={})
        observation = adapt_nk_result(record)
        self.assertEqual(observation['status'], 'pending')
        self.assertIsNone(observation['truth_cars'])
        self.assertEqual(observation['available_at'], record['fetched_at'])

    def test_invalid_source_type_race_id_and_observation_time_fail_closed(self):
        for field, value in (
            ('type', 'k'), ('race_id', 202610211201), ('race_id', '202602301201'),
            ('race_id', '202610211200'), ('race_id', '202610211213'),
            ('fetched_at', None), ('fetched_at', '2026-10-23T13:46:19'),
            ('fetched_at', 'not a timestamp'),
        ):
            with self.subTest(field=field, value=value):
                record = published_record()
                record[field] = value
                self.assert_invalid(record)

    def test_url_must_identify_the_same_nk_result(self):
        for url in (
            'https://example.org/race/result/?race_id=202610211201',
            'http://keirin.netkeiba.com/race/result/?race_id=202610211201',
            'https://keirin.netkeiba.com/race/entry/?race_id=202610211201',
            'https://keirin.netkeiba.com/race/result/?race_id=202610211202',
            'https://keirin.netkeiba.com/race/result/?race_id=202610211201&race_id=202610211201',
            'https://keirin.netkeiba.com/race/result/?race_id=202610211201#different',
        ):
            with self.subTest(url=url):
                record = published_record()
                record['url'] = url
                self.assert_invalid(record)

    def test_title_must_match_date_race_number_venue_and_result_page(self):
        original = published_record()['data']['title']
        for title in (
            original.replace('10月21日', '10月22日'),
            original.replace(' 1R ', ' 11R '),
            original.replace('青森競輪', '熊本競輪'),
            original.replace('結果・払戻金', '出走表'),
            '',
        ):
            with self.subTest(title=title):
                record = published_record()
                record['data']['title'] = title
                self.assert_invalid(record)

    def test_headers_widths_and_duplicate_cars_cannot_make_a_label(self):
        for mutation in ('header', 'width', 'duplicate', 'car', 'frame'):
            with self.subTest(mutation=mutation):
                record = published_record()
                table = record['data']['result']
                if mutation == 'header':
                    table[0][0] = '着順'
                elif mutation == 'width':
                    table[1].pop()
                elif mutation == 'duplicate':
                    table[2][2] = '1'
                elif mutation == 'car':
                    table[1][2] = '0'
                else:
                    table[1][1] = '9'
                self.assert_invalid(record)

    def test_malformed_name_or_graduate_never_becomes_a_composite_identity(self):
        for cell in (
            '櫻川雅彦', '千葉 42歳 99期 Ａ3', '櫻川雅彦 千葉 42歳 0期 Ａ3',
            '櫻川雅彦 千葉 42歳 不明期 Ａ3', '櫻川雅彦 千葉 42歳 99期 不明',
            '櫻川123 千葉 42歳 99期 Ａ3', '櫻川雅彦 不明 42歳 99期 Ａ3',
        ):
            with self.subTest(cell=cell):
                record = published_record()
                record['data']['result'][1][3] = cell
                self.assert_invalid(record)

    def test_unknown_outcome_impossible_rank_and_missing_top_three_are_invalid(self):
        for rank in ('0着', '10着', '不明', ''):
            with self.subTest(rank=rank):
                record = published_record()
                record['data']['result'][3][0] = rank
                self.assert_invalid(record)
        record = published_record()
        record['data']['result'][3][0] = '棄'
        self.assert_invalid(record)

    def test_malformed_status_and_status_payout_conflict_are_invalid(self):
        for status in ('成功', '', None, '返還', '未確定'):
            with self.subTest(status=status):
                record = published_record()
                record['data']['status'] = status
                self.assert_invalid(record)

    def test_payout_must_match_top_three_with_real_positive_yen(self):
        for payout in (
            [], [['1>3>5', 610, 2]], [['1>5>9', 610, 2]],
            [['1>1>3', 610, 2]], [['1-5-3', 610, 2]],
            [['1>5>3', 0, 2]], [['1>5>3', True, 2]],
            [['1>5>3', '610', 2]], [['1>5>3', 610]],
            [['1>5>3', 610, 2], ['1>5>3', 610, 2]],
        ):
            with self.subTest(payout=payout):
                record = published_record()
                record['data']['payout']['３連単'] = payout
                self.assert_invalid(record)

    def test_tie_payout_cannot_claim_a_non_tied_runner_or_omit_an_order(self):
        record = published_record()
        record['data']['result'][4][0] = '3着'
        self.assert_invalid(record)
        record['data']['payout']['３連単'] = [['1>5>3', 380, 1], ['1>5>7', 410, 2]]
        self.assert_invalid(record)

    def test_non_json_or_non_table_shapes_are_invalid_without_crashing(self):
        for record in (None, [], {'type': 'nk_result'}, {'raw': float('nan')}):
            with self.subTest(record=record):
                self.assert_invalid(record)
        for field, value in (('data', []), ('result', 'table'), ('payout', []), ('result', [[]])):
            with self.subTest(field=field, value=value):
                record = published_record()
                if field == 'data':
                    record[field] = value
                else:
                    record['data'][field] = value
                self.assert_invalid(record)


if __name__ == '__main__':
    unittest.main()
