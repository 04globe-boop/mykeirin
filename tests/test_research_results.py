"""Observed-result admission with real SQLite and immutable source fixtures."""
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
from itertools import permutations
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'logger'))
from test_research_result_adapter import published_record
try:
    from research_results import settle_observed_day
except ModuleNotFoundError as error:
    if error.name != 'research_results':
        raise

    def settle_observed_day(*args, **kwargs):
        raise AssertionError('research_results.settle_observed_day is not implemented')


NOW = datetime.fromisoformat('2026-10-23T14:00:00+09:00')
RID = '202610211201'
ISSUED = '2026-10-21T08:19:40.000001+09:00'
CLOSE = '2026-10-21T08:25:00+09:00'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def identity(name, graduate):
    name = ''.join(unicodedata.normalize('NFKC', name).split())
    return 'rider:' + str(int(graduate)) + ':' + name


@dataclass
class Runner:
    rider_id: str
    name: str
    pref: str
    term: int
    age: float
    score: float
    gear: float
    cls: str
    style: str
    rank: int | None
    status: str
    finish: str
    car: str


class Ledger:
    """Frozen ForwardLedger interface double; writes use real durable SQLite."""
    def __init__(self, path, *, clock=None):
        self.clock = clock
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS predictions(race_id TEXT,model_id TEXT,protocol_hash TEXT,decision_at TEXT,close_at TEXT,recorded_at TEXT,frozen_at TEXT,meeting_id TEXT,payload TEXT,sha256 TEXT,synthetic INTEGER,PRIMARY KEY(race_id,model_id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS labels(race_id TEXT PRIMARY KEY,available_at TEXT,recorded_at TEXT,truth TEXT,source_id TEXT)')
        for table in ('predictions', 'labels'):
            for action in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS prevent_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append only'); END")

    def settle(self, race_id, truth, *, available_at, source_id):
        predictions = self.db.execute('SELECT close_at,payload FROM predictions WHERE race_id=?', (race_id,)).fetchall()
        if not predictions or datetime.fromisoformat(available_at) > self.clock():
            raise ValueError('Observed released label and existing prediction required')
        for close, payload in predictions:
            if datetime.fromisoformat(available_at) <= datetime.fromisoformat(close) or list(truth) not in json.loads(payload)['outcomes']:
                raise ValueError('Invalid result for prediction universe')
        self.db.execute('INSERT INTO labels VALUES(?,?,?,?,?)', (race_id, available_at, self.clock().isoformat(), json.dumps(truth), source_id))
        self.db.commit()

    def summary(self, model_id):
        count = self.db.execute('SELECT COUNT(*) FROM predictions JOIN labels USING(race_id) WHERE model_id=?', (model_id,)).fetchone()[0]
        return {'settled_races': count}

    def close(self):
        self.db.close()


class History:
    def __init__(self, path):
        self.sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.events = {}

    def extend(self, path, observed_at):
        pass  # fixtures have no prior rider-specific events


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone is required')
    return parsed.astimezone(timezone.utc)


def latest_capture(records, race_id, kind, decision_at, max_age_seconds):
    candidates = [r for r in records if r.get('race_id') == race_id and r.get('type') == kind
                  and 0 <= (stamp(decision_at) - stamp(r['fetched_at'])).total_seconds() <= max_age_seconds]
    if not candidates:
        raise ValueError('No eligible entries capture')
    newest = max(stamp(r['fetched_at']) for r in candidates)
    rows = [r for r in candidates if stamp(r['fetched_at']) == newest]
    if any(row != rows[0] for row in rows[1:]):
        raise ValueError('Conflicting same-time captures')
    return rows[0]


def k_rows(record):
    header = [''.join(v.split()) for v in record['data']['result'][0]]
    result = []
    for cells in record['data']['result'][1:]:
        values = dict(zip(header, cells))
        rank = values['着順']
        result.append({'car': values['車番'], 'name': ''.join(values['選手名'].split()),
                       'rank': int(rank) if rank.isdigit() else None,
                       'status': rank, 'finish': values.get('決まり手', '')})
    if any(sum(r['rank'] == rank for r in result) != 1 for rank in (1, 2, 3)):
        raise ValueError('Tie, cancelled or incomplete top three')
    return result


def loader(bundle, name):
    return {
        'forward': SimpleNamespace(ForwardLedger=Ledger),
        'legacy': SimpleNamespace(Runner=Runner, identity=identity, canonical_name=lambda value: ''.join(unicodedata.normalize('NFKC', str(value)).split()), number=lambda value: float(value)),
        'live_data': SimpleNamespace(HistoryPack=History),
        'logger_adapter': SimpleNamespace(latest_capture=latest_capture, capture_available_at=lambda row: stamp(row['fetched_at'])),
        'data': SimpleNamespace(stamp=stamp, digest=digest),
        'settlement': SimpleNamespace(result_rows=k_rows),
    }[name]


def write_gzip(path, records):
    with gzip.open(path, 'wt', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def read_gzip(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]


class ObservedAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / 'v1'
        self.bundle.mkdir()
        history = self.bundle / 'history.gz'
        with gzip.open(history, 'wt', encoding='utf-8') as stream:
            json.dump({'events': {}, 'captured_at': '2026-10-19T00:00:00+00:00'}, stream)
        self.manifest = {'history_file': 'history.gz', 'history_sha256': hashlib.sha256(history.read_bytes()).hexdigest(), 'candidate_ids': {'a': 'a' * 64, 'b': 'b' * 64}}
        (self.bundle / 'bundle.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        self.out = self.root / 'out'
        self.out.mkdir()
        self.nk = self.root / 'nk.jsonl.gz'
        self.extra = self.root / 'extra.jsonl.gz'
        write_gzip(self.extra, [])
        self.source = published_record()
        write_gzip(self.nk, [self.source])
        runners = [(1, '櫻川雅彦', 99), (2, '矢田晋', 94), (3, '吉原友彦', 83),
                   (4, '富永昌久', 88), (5, '奥山雅士', 65), (6, '落合豊', 69), (7, '中野功史', 84)]
        self.card = {'type': 'entries', 'race_id': RID, 'fetched_at': '2026-10-21T08:18:00.000001+09:00',
                     'data': [{'syaban': str(car), 'name': name, 'graduate': str(term), 'sales_sts': '7',
                               'fuken': '千葉', 'age': '42', 'rating': '70.5', 'kyu': 'Ａ', 'han': '３'} for car, name, term in runners]}
        self.schedule = {'type': 'racelist', 'fetched_at': '2026-10-21T07:00:00.000001+09:00',
                         'data': [{'race_id': RID, 'kaisai_date': '20261021', 'jyo_cd': '12', 'close': '08:25'}]}
        self.records = [self.schedule, self.card]
        self.ids = {str(car): identity(name, term) for car, name, term in runners}
        self.predictions = []
        for model in self.manifest['candidate_ids'].values():
            payload = {'outcomes': list(permutations(self.ids.values(), 3)), 'input_hash': 'immutable-input', 'primary_horizon_eligible': True}
            self.predictions.append((RID, model, 'p' * 64, '2026-10-21T08:20:00+09:00', CLOSE, ISSUED, '2026-10-19T00:00:00+00:00', 'meeting', json.dumps(payload), digest(payload), 0))
        self.original_history = {'race_id': '202610201201', 'date': '2026-10-20', 'venue': '12', 'field_size': 0,
                                 'available_at': '2026-10-20T22:00:00+09:00', 'runners': []}
        write_gzip(self.out / 'research_history.jsonl.gz', [self.original_history])

    def initialize(self):
        write_gzip(self.out / 'odds_20261021.jsonl.gz', self.records)
        ledger = Ledger(self.out / 'forward_20261021.sqlite', clock=lambda: NOW)
        ledger.db.executemany('INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)', self.predictions)
        ledger.db.commit()
        ledger.close()

    def run_settlement(self, now=NOW):
        return settle_observed_day(self.bundle, self.out, self.extra, self.nk, '20261021', module_loader=loader, clock=lambda: now)

    def labels(self):
        db = sqlite3.connect(self.out / 'forward_20261021.sqlite')
        try:
            return db.execute('SELECT * FROM labels').fetchall()
        finally:
            db.close()

    def sidecar(self):
        return [json.loads(line) for line in (self.out / 'forward_observations_20261021.jsonl').read_text(encoding='utf-8').splitlines()]

    def k_source(self):
        return {'type': 'k', 'race_id': RID,
                'url': 'https://keirin.kdreams.jp/aomori/racedetail/1220261019030001/',
                'fetched_at': '2026-10-23T12:00:00.000001+09:00',
                'data': {'page_title': '青森競輪 1R レース詳細 2026年10月21日',
                         'result': [['着順', '車番', '選手名', '決まり手']] +
                         [[str(row[0]).replace('着', ''), row[2], row[3].split()[0], row[6]] for row in self.source['data']['result'][1:]]}}

    def test_verified_label_and_history_are_append_only_with_actual_times(self):
        self.initialize()
        original = (self.out / 'research_history.jsonl.gz').read_bytes()
        report = self.run_settlement()
        self.assertEqual((report['new_labels'], report['existing_labels'], report['history_added']), (1, 0, 1))
        label = self.labels()[0]
        self.assertEqual(json.loads(label[3]), ['rider:99:櫻川雅彦', 'rider:65:奥山雅士', 'rider:83:吉原友彦'])
        self.assertEqual(label[1], self.source['fetched_at'])
        self.assertEqual(stamp(label[2]), stamp(NOW.isoformat()))
        self.assertIn(self.source['url'], label[4])
        self.assertTrue((self.out / 'research_history.jsonl.gz').read_bytes().startswith(original))
        history = read_gzip(self.out / 'research_history.jsonl.gz')
        self.assertEqual(history[0], self.original_history)
        self.assertEqual(history[1]['available_at'], self.source['fetched_at'])
        self.assertEqual(self.sidecar()[-1]['status'], 'admitted')
        db = sqlite3.connect(self.out / 'forward_20261021.sqlite')
        self.assertEqual(db.execute('SELECT * FROM predictions').fetchall(), self.predictions)
        db.close()

    def test_repeat_and_second_cohort_count_existing_shared_labels(self):
        self.initialize()
        self.run_settlement()
        prior = self.labels()
        report = self.run_settlement()
        self.assertEqual((report['new_labels'], report['existing_labels'], report['history_added']), (0, 1, 0))
        self.assertEqual(self.labels(), prior)
        self.assertEqual(self.sidecar()[-1]['status'], 'already_labelled')
        self.assertEqual(report['summaries']['b']['settled_races'], 1)

    def test_installed_bundle_directory_keeps_its_parent_cohort_name(self):
        installed = self.root / 'runtime' / 'v2' / 'bundle'
        shutil.copytree(self.bundle, installed)
        self.bundle = installed
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(report['cohort'], 'v2')
        self.assertEqual(self.sidecar()[-1]['cohort'], 'v2')

    def test_known_withdrawal_without_outcome_detail_remains_unverified(self):
        self.card['data'][5]['sales_sts'] = '3'
        self.source['data']['result'][7][0] = '欠'
        self.source['data']['result'][7][4] = ''
        active = [value for car, value in self.ids.items() if car != '6']
        for index, prediction in enumerate(self.predictions):
            payload = json.loads(prediction[8]);payload['outcomes'] = list(permutations(active, 3))
            changed = list(prediction);changed[8:10] = [json.dumps(payload), digest(payload)]
            self.predictions[index] = tuple(changed)
        write_gzip(self.nk, [self.source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(self.labels(), [])
        self.assertEqual(report['unavailable'][0]['reason'], 'withdrawal_detail_unverified')

    def test_all_candidate_universes_must_match_even_when_truth_is_present(self):
        payload = json.loads(self.predictions[1][8])
        payload['outcomes'].pop()
        changed = list(self.predictions[1])
        changed[8:10] = [json.dumps(payload), digest(payload)]
        self.predictions[1] = tuple(changed)
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(self.labels(), [])
        self.assertEqual(report['unavailable'][0]['reason'], 'prediction_universe_mismatch')
        self.assertEqual(len(read_gzip(self.out / 'research_history.jsonl.gz')), 1)

    def test_identity_and_graduating_term_are_both_checked(self):
        self.source['data']['result'][1][3] = '櫻川雅彦 千葉 42歳 100期 Ａ3'
        write_gzip(self.nk, [self.source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(self.labels(), [])
        self.assertEqual(report['unavailable'][0]['reason'], 'result_card_identity_mismatch')

    def test_future_or_stale_cards_cannot_be_selected_after_issue(self):
        for timestamp in ('2026-10-21T08:19:41.000001+09:00', '2026-10-21T05:00:00.000001+09:00'):
            with self.subTest(timestamp=timestamp):
                self.card['fetched_at'] = timestamp
                if not (self.out / 'forward_20261021.sqlite').exists():
                    self.initialize()
                else:
                    write_gzip(self.out / 'odds_20261021.jsonl.gz', self.records)
                report = self.run_settlement()
                self.assertEqual(self.labels(), [])
                self.assertEqual(report['unavailable'][0]['reason'], 'no_eligible_preissue_card')

    def test_result_cannot_be_available_before_close_or_in_the_future(self):
        for captured in ('2026-10-21T08:24:00+09:00', '2026-10-23T15:00:00+09:00'):
            with self.subTest(captured=captured):
                self.source['fetched_at'] = captured
                write_gzip(self.nk, [self.source])
                if not (self.out / 'forward_20261021.sqlite').exists():
                    self.initialize()
                report = self.run_settlement()
                self.assertEqual(self.labels(), [])
                self.assertTrue(report['unavailable'])

    def test_late_withdrawal_is_held_even_with_a_unique_top_three(self):
        self.source['data']['result'][7][0] = '欠'
        self.source['data']['result'][7][4] = '－ (欠場)'
        write_gzip(self.nk, [self.source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(self.labels(), [])
        self.assertEqual(report['unavailable'][0]['reason'], 'late_or_unverified_withdrawal')

    def test_known_preissue_withdrawal_uses_the_complete_smaller_universe(self):
        self.card['data'][5]['sales_sts'] = '3'
        self.source['data']['result'][7][0] = '欠'
        self.source['data']['result'][7][4] = '－ (欠場)'
        active = [value for car, value in self.ids.items() if car != '6']
        for index, prediction in enumerate(self.predictions):
            payload = json.loads(prediction[8]);payload['outcomes'] = list(permutations(active, 3))
            changed = list(prediction);changed[8:10] = [json.dumps(payload), digest(payload)]
            self.predictions[index] = tuple(changed)
        write_gzip(self.nk, [self.source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(report['new_labels'], 1)
        self.assertEqual(read_gzip(self.out / 'research_history.jsonl.gz')[-1]['field_size'], 6)
        self.assertFalse(self.sidecar()[-1]['refund_details_available'])

    def test_source_agreement_admits_once_and_retains_both_provenances(self):
        write_gzip(self.extra, [self.k_source()])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(report['new_labels'], 1)
        admitted = [row for row in self.sidecar() if row['status'] == 'admitted']
        self.assertEqual({row['source_type'] for row in admitted}, {'k', 'nk_result'})
        self.assertEqual(read_gzip(self.nk)[0]['type'], 'nk_result')

    def test_source_disagreement_is_not_resolved_by_selecting_one_source(self):
        write_gzip(self.extra, [self.k_source()])
        self.source['data']['result'][2][0] = '3着'
        self.source['data']['result'][3][0] = '2着'
        self.source['data']['payout']['３連単'] = [['1>3>5', 610, 2]]
        write_gzip(self.nk, [self.source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(self.labels(), [])
        self.assertEqual(report['unavailable'][0]['reason'], 'result_sources_disagree')
        self.assertEqual(self.sidecar()[-1]['status'], 'source_conflict')

    def test_same_source_revision_is_not_silently_accepted(self):
        revision = copy.deepcopy(self.source)
        revision['fetched_at'] = '2026-10-23T13:50:00+09:00'
        revision['data']['result'][2][0] = '3着'
        revision['data']['result'][3][0] = '2着'
        revision['data']['payout']['３連単'] = [['1>3>5', 610, 2]]
        write_gzip(self.nk, [self.source, revision])
        self.initialize()
        self.assertTrue(self.run_settlement()['unavailable'])
        self.assertEqual(self.labels(), [])

    def test_pending_source_is_not_receipted_as_an_admitted_published_result(self):
        pending = copy.deepcopy(self.source)
        pending['fetched_at'] = '2026-10-21T08:30:00+09:00'
        pending['data'].update(status='未確定', result=[], payout={})
        write_gzip(self.nk, [pending, self.source])
        self.initialize()
        self.assertEqual(self.run_settlement()['new_labels'], 1)
        pending_receipts = [row for row in self.sidecar() if row['source_sha256'] == digest(pending)]
        self.assertTrue(pending_receipts)
        self.assertTrue(all(row['status'] == 'pending' for row in pending_receipts))

    def test_noncanonical_k_original_is_invalid_without_aborting_the_day(self):
        source = self.k_source()
        source['raw_extra'] = float('nan')
        write_gzip(self.extra, [source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(self.labels(), [])
        self.assertEqual(report['unavailable'][0]['reason'], 'invalid_k_json_record')

    def test_lower_place_history_revision_is_flagged_without_overwriting(self):
        self.initialize()
        self.run_settlement()
        old_history = (self.out / 'research_history.jsonl.gz').read_bytes()
        self.source['data']['result'][4][0] = '5着'
        self.source['data']['result'][5][0] = '4着'
        write_gzip(self.nk, [self.source])
        report = self.run_settlement()
        self.assertTrue(report['unavailable'])
        self.assertEqual(report['unavailable'][0]['reason'], 'existing_history_revision_requires_review')
        self.assertEqual((self.out / 'research_history.jsonl.gz').read_bytes(), old_history)

    def test_tie_is_explicitly_excluded_from_labels_and_history(self):
        self.source['data']['result'][4][0] = '3着'
        self.source['data']['payout']['３連単'] = [['1>5>3', 380, 1], ['1>5>4', 410, 2]]
        write_gzip(self.nk, [self.source])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(len(report['excluded']), 1)
        self.assertEqual(self.sidecar()[-1]['status'], 'excluded_tie')
        self.assertEqual(self.labels(), [])
        self.assertEqual(len(read_gzip(self.out / 'research_history.jsonl.gz')), 1)

    def test_tie_exclusion_requires_verified_identity_time_and_full_universe(self):
        for defect in ('identity','time','universe'):
            with self.subTest(defect=defect):
                self.setUp()
                self.source['data']['result'][4][0]='3着'
                self.source['data']['payout']['３連単']=[['1>5>3',380,1],['1>5>4',410,2]]
                if defect=='identity':self.source['data']['result'][1][3]=self.source['data']['result'][1][3].replace('99期','100期')
                if defect=='time':self.source['fetched_at']='2026-10-21T08:24:00+09:00'
                if defect=='universe':
                    payload=json.loads(self.predictions[0][8]);payload['outcomes']=payload['outcomes'][:-1]
                    value=list(self.predictions[0]);value[8]=json.dumps(payload);value[9]=digest(payload);self.predictions[0]=tuple(value)
                self.initialize()
                write_gzip(self.nk,[self.source]);report=self.run_settlement()
                self.assertEqual(report['excluded'],[])
                self.assertTrue(report['unavailable']);self.assertEqual(self.labels(),[])

    def test_refund_exclusion_requires_observation_after_verified_close(self):
        self.source['data'].update(status='返還',result=[],payout={})
        self.source['fetched_at']='2026-10-21T08:24:00+09:00';write_gzip(self.nk,[self.source]);self.initialize()
        report=self.run_settlement();self.assertEqual(report['excluded'],[])
        self.assertEqual(report['unavailable'][0]['reason'],'result_not_observed_after_close')

    def test_refund_and_pending_never_create_truth(self):
        self.initialize()
        for status, final_status in (('返還', 'refund'), ('未確定', 'pending')):
            with self.subTest(status=status):
                self.source['data'].update(status=status, result=[], payout={})
                write_gzip(self.nk, [self.source])
                self.run_settlement()
                self.assertEqual(self.labels(), [])
                self.assertEqual(self.sidecar()[-1]['status'], final_status)

    def test_existing_label_conflict_keeps_original_and_flags_revision(self):
        self.initialize()
        db = sqlite3.connect(self.out / 'forward_20261021.sqlite')
        db.execute('INSERT INTO labels VALUES(?,?,?,?,?)', (RID, '2026-10-22T12:00:00+09:00', '2026-10-22T13:00:00+09:00', json.dumps(['old', 'label', 'retained']), 'original'))
        db.commit();db.close()
        old = self.labels()
        report = self.run_settlement()
        self.assertEqual(self.labels(), old)
        self.assertEqual(report['unavailable'][0]['reason'], 'existing_label_revision_requires_review')
        self.assertEqual(self.sidecar()[-1]['status'], 'source_conflict')

    def test_history_uses_preissue_attributes_and_preserves_fall_information(self):
        self.source['data']['result'][7][0] = '棄'
        self.source['data']['result'][7][4] = '－ (落車棄権)'
        newer = copy.deepcopy(self.card)
        newer['fetched_at'] = '2026-10-21T08:24:00.000001+09:00'
        newer['data'][0]['rating'] = '999'
        self.records.append(newer)
        write_gzip(self.nk, [self.source])
        self.initialize()
        self.run_settlement()
        runners = read_gzip(self.out / 'research_history.jsonl.gz')[-1]['runners']
        self.assertEqual(next(r for r in runners if r['car'] == '1')['score'], 70.5)
        self.assertEqual(next(r for r in runners if r['car'] == '6')['status'], '落')
        self.assertEqual(next(r for r in runners if r['car'] == '5')['finish'], 'マ')

    def test_missing_result_is_a_reasoned_unavailable_observation(self):
        write_gzip(self.nk, [])
        self.initialize()
        report = self.run_settlement()
        self.assertEqual(report['unavailable'][0]['reason'], 'no_result_observation')
        self.assertEqual(self.sidecar()[-1]['status'], 'unavailable')
        self.assertEqual(self.labels(), [])


if __name__ == '__main__':
    unittest.main()
