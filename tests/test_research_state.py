"""Transport regressions use real local ledgers and a fake GitHub boundary."""
import gzip
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'logger'))
import research_assets


def ledger(path, predictions=(), labels=()):
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE predictions(race_id TEXT, model_id TEXT, payload TEXT,
        recorded_at TEXT, PRIMARY KEY(race_id,model_id));
        CREATE TABLE labels(race_id TEXT PRIMARY KEY, truth TEXT, recorded_at TEXT);''')
    db.executemany('INSERT INTO predictions VALUES(?,?,?,?)', predictions)
    db.executemany('INSERT INTO labels VALUES(?,?,?)', labels)
    db.commit()
    db.close()


def zipped(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wb') as stream:
        stream.write(data)


def rows(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


class Remote:
    """Only network calls are replaced; real disk merge/validation is exercised."""
    def __init__(self, root):
        self.root = root / 'remote'
        self.root.mkdir()
        self.errors = set()
        self.uploads = []
        self.fetches = []

    def fetch(self, repo, tag, name, directory):
        self.fetches.append((repo,tag,name))
        if name in self.errors:
            return {'status': 'error', 'reason': 'HTTP 403'}
        source = self.root / name
        if not source.exists():
            return {'status': 'absent'}
        dest = Path(directory) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return {'status': 'restored'}

    def gh(self, *args, required=True):
        if args[:2] == ('release', 'upload'):
            tag, path = args[2], Path(args[3])
            self.uploads.append((tag, path.name, '--clobber' in args, path.read_bytes()))
            if '--clobber' in args:
                shutil.copy2(path, self.root / path.name)
        return True


class StateTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'data'
        self.out.mkdir()
        self.remote = Remote(self.root)
        # fetch_asset is the status-aware external boundary introduced by this change.
        self.addCleanup(patch.stopall)
        patch.object(research_assets, 'fetch_asset', self.remote.fetch, create=True).start()
        patch.object(research_assets, 'download', lambda repo, tag, name, directory:
                     self.remote.fetch(repo, tag, name, directory)['status'] == 'restored').start()
        patch.object(research_assets, 'gh', self.remote.gh).start()

    def restore(self, day='20261022'):
        return research_assets.state_download('fixed/repo', self.out, day)

    def test_restore_receipt_distinguishes_absent_and_permission_failure(self):
        self.remote.errors.add('forward_events_20261022.jsonl.gz')
        receipt = self.restore()
        self.assertIsInstance(receipt, dict)
        statuses = {name: item['status'] for name, item in receipt['assets'].items()}
        self.assertEqual(statuses['forward_20261022.sqlite.gz'], 'absent')
        self.assertEqual(statuses['forward_events_20261022.jsonl.gz'], 'error')
        self.assertTrue((self.out / 'research_transport.json').exists())

    def test_existing_day_with_restore_error_is_backed_up_without_canonical_upload(self):
        self.remote.errors.add('forward_events_20261022.jsonl.gz')
        self.restore()
        (self.out / 'forward_events_20261022.jsonl').write_text('{"type":"miss"}\n')
        with self.assertRaises(RuntimeError):
            research_assets.upload_state('fixed/repo', self.out)
        self.assertTrue(self.remote.uploads)
        self.assertFalse(any(item[2] for item in self.remote.uploads))

    def test_concurrent_remote_label_and_local_prediction_are_both_retained(self):
        path = self.root / 'base.sqlite'
        old = ('r1', 'v1', '{"input_hash":"original"}', 'original-time')
        ledger(path, [old])
        zipped(self.remote.root / 'forward_20261022.sqlite.gz', path.read_bytes())
        self.restore()
        local = self.out / 'forward_20261022.sqlite'
        db = sqlite3.connect(local)
        db.execute('INSERT INTO predictions VALUES(?,?,?,?)', ('r2', 'v1', '{}', 'local-time'))
        db.commit(); db.close()
        path.unlink()
        ledger(path, [old], [('r1', '["a","b","c"]', 'remote-time')])
        zipped(self.remote.root / 'forward_20261022.sqlite.gz', path.read_bytes())
        research_assets.upload_state('fixed/repo', self.out)
        restored = self.root / 'checked.sqlite'
        restored.write_bytes(gzip.decompress((self.remote.root / 'forward_20261022.sqlite.gz').read_bytes()))
        db = sqlite3.connect(restored)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT race_id FROM predictions ORDER BY race_id').fetchall(), [('r1',), ('r2',)])
        self.assertEqual(db.execute('SELECT recorded_at FROM labels').fetchall(), [('remote-time',)])
        self.assertEqual(db.execute('SELECT recorded_at FROM predictions WHERE race_id="r1"').fetchone()[0], 'original-time')
        db.close()
        first_canonical = next(i for i, item in enumerate(self.remote.uploads) if item[2])
        self.assertGreater(first_canonical, 0)
        self.assertTrue(all(not item[2] for item in self.remote.uploads[:first_canonical]))

    def test_conflicting_immutable_key_preserves_both_originals_and_refuses_replacement(self):
        path = self.root / 'base.sqlite'
        ledger(path, [('r1', 'v1', '{"input_hash":"remote"}', 'first')])
        zipped(self.remote.root / 'forward_20261022.sqlite.gz', path.read_bytes())
        self.restore()
        db = sqlite3.connect(self.out / 'forward_20261022.sqlite')
        db.execute('UPDATE predictions SET payload=?', ('{"input_hash":"changed"}',)); db.commit(); db.close()
        before = (self.remote.root / 'forward_20261022.sqlite.gz').read_bytes()
        with self.assertRaises(RuntimeError):
            research_assets.upload_state('fixed/repo', self.out)
        self.assertEqual((self.remote.root / 'forward_20261022.sqlite.gz').read_bytes(), before)
        self.assertTrue(any('original' in item[1] for item in self.remote.uploads))
        manifest=next(json.loads(item[3]) for item in self.remote.uploads if item[1].endswith('manifest.json'))
        remote=next(item for item in manifest['assets'] if item['phase']=='remote')
        self.assertEqual(remote['sha256'],hashlib.sha256(before).hexdigest())
        self.assertTrue(any(item[1]==remote['backup_name'] for item in self.remote.uploads))
        self.assertFalse(any(item[2] for item in self.remote.uploads))

    def test_events_inputs_and_history_keep_append_collections_after_concurrent_write(self):
        for name in ('forward_events_20261022.jsonl.gz', 'forward_inputs_20261022.jsonl.gz', 'research_history.jsonl.gz'):
            zipped(self.remote.root / name, b'{"race_id":"old","a":1,"b":2}\n')
        self.restore()
        with (self.out / 'forward_events_20261022.jsonl').open('a') as stream:
            stream.write('{"race_id":"local","type":"unavailable"}\n')
        for name in ('forward_inputs_20261022.jsonl.gz', 'research_history.jsonl.gz'):
            with gzip.open(self.out / name, 'at') as stream:
                stream.write('{"race_id":"local"}\n')
        for name in ('forward_events_20261022.jsonl.gz', 'forward_inputs_20261022.jsonl.gz', 'research_history.jsonl.gz'):
            zipped(self.remote.root / name, b'{"b":2,"a":1,"race_id":"old"}\n{"race_id":"remote"}\n')
        research_assets.upload_state('fixed/repo', self.out, include_history=True)
        for name in ('forward_events_20261022.jsonl.gz', 'forward_inputs_20261022.jsonl.gz', 'research_history.jsonl.gz'):
            self.assertEqual({r['race_id'] for r in rows(self.remote.root / name)}, {'old', 'local', 'remote'})
            self.assertEqual(len(rows(self.remote.root / name)), 3)

    def test_mutable_reports_are_identified_and_original_remote_is_backed_up(self):
        self.restore()
        name = 'forward_report_20261022.json'
        (self.remote.root / name).write_text('{"settled":1}')
        (self.out / name).write_text('{"settled":2}')
        research_assets.upload_state('fixed/repo', self.out)
        self.assertEqual(json.loads((self.remote.root / name).read_text()), {'settled': 2})
        manifests = [json.loads(item[3]) for item in self.remote.uploads if item[1].endswith('manifest.json')]
        self.assertTrue(any(asset['kind'] == 'mutable_report' for manifest in manifests for asset in manifest['assets']))
        self.assertTrue(any('remote' in item[1] and name in item[1] for item in self.remote.uploads))

    def test_backup_failure_stops_all_canonical_replacement(self):
        self.restore()
        (self.out / 'forward_events_20261022.jsonl').write_text('{"type":"miss"}\n')
        attempted = []
        def fail_upload(*args, required=True):
            if args[:2] == ('release', 'upload'):
                attempted.append('--clobber' in args)
                raise RuntimeError('backup unavailable')
            return True
        with patch.object(research_assets, 'gh', side_effect=fail_upload):
            with self.assertRaises(RuntimeError):
                research_assets.upload_state('fixed/repo', self.out)
        self.assertFalse(self.remote.uploads)
        self.assertEqual(attempted, [False])

    def test_pack_is_network_free_and_preserves_source_stage(self):
        self.restore()
        (self.out / 'forward_events_20261022.jsonl').write_text('{"type":"predicted"}\n')
        with patch.object(research_assets, 'gh', side_effect=AssertionError('network during pack')):
            manifest = research_assets.pack_state(self.out, stage='capture')
        self.assertEqual(manifest['stage'], 'capture')
        self.assertTrue((self.out / 'research_pack.json').exists())
        self.assertEqual(rows(self.out / 'forward_events_20261022.jsonl.gz'), [{'type': 'predicted'}])

    def test_unknown_day_cannot_replace_canonical_even_when_remote_is_readable(self):
        self.restore('20261023')
        name='forward_events_20261022.jsonl.gz'
        zipped(self.remote.root/name,b'{"type":"predicted"}\n')
        (self.out/'forward_events_20261022.jsonl').write_text('{"type":"miss"}\n')
        with self.assertRaises(RuntimeError):
            research_assets.upload_state('fixed/repo',self.out)
        self.assertEqual(rows(self.remote.root/name),[{'type':'predicted'}])
        self.assertFalse(any(item[2] for item in self.remote.uploads))

    def test_missing_restore_baseline_refuses_publication_after_backup(self):
        name='forward_events_20261022.jsonl.gz'
        zipped(self.remote.root/name,b'{"type":"predicted"}\n')
        self.restore()
        for path in (self.out/'research_restored').iterdir():path.unlink()
        with self.assertRaises(RuntimeError):
            research_assets.upload_state('fixed/repo',self.out)
        self.assertTrue(self.remote.uploads)
        self.assertFalse(any(item[2] for item in self.remote.uploads))

    def test_conflicting_history_keeps_both_observations_without_replacing_canonical(self):
        name='research_history.jsonl.gz'
        zipped(self.remote.root/name,b'{"race_id":"r1","available_at":"original"}\n')
        self.restore()
        zipped(self.out/name,b'{"race_id":"r1","available_at":"changed"}\n')
        with self.assertRaises(RuntimeError):
            research_assets.upload_state('fixed/repo',self.out,include_history=True)
        self.assertEqual(rows(self.remote.root/name),[{'race_id':'r1','available_at':'original'}])
        self.assertTrue(any('original' in item[1] and name in item[1] for item in self.remote.uploads))
        self.assertFalse(any(item[2] for item in self.remote.uploads))

    def test_prepare_cli_restores_both_days_when_execution_crosses_midnight(self):
        from zoneinfo import ZoneInfo
        class FixedClock(datetime):
            @classmethod
            def now(cls,tz=None):
                value=datetime(2026,10,22,23,55,tzinfo=ZoneInfo('Asia/Tokyo'))
                return value.astimezone(tz) if tz else value
        for day in ('20261022','20261023'):
            zipped(self.remote.root/f'forward_events_{day}.jsonl.gz',json.dumps({'day':day}).encode()+b'\n')
        def install(*args):
            runtime=Path('research-runtime');runtime.mkdir()
            (runtime/'preparation.json').write_text('{"installed":{"v1":"fixture"}}')
            return {'v1':'fixture'}
        arguments=['research_assets.py','prepare','--repo','fixed/repo','--data-dir',str(self.out),'--window-min','12']
        with contextlib.chdir(self.root),patch.object(research_assets,'datetime',FixedClock),patch.object(research_assets,'install_bundles',side_effect=install),patch.object(sys,'argv',arguments):
            research_assets.main()
        self.assertEqual(json.loads((self.out/'research_transport.json').read_text())['days'].keys(),{'20261022','20261023'})
        for day in ('20261022','20261023'):
            self.assertEqual(json.loads((self.out/f'forward_events_{day}.jsonl').read_text()),{'day':day})

    def test_identical_backup_bytes_are_uploaded_once_with_all_roles_in_manifest(self):
        name='forward_events_20261022.jsonl.gz'
        zipped(self.remote.root/name,b'{"type":"predicted"}\n')
        self.restore()
        manifest=research_assets.upload_state('fixed/repo',self.out)
        immutable=[item for item in self.remote.uploads if not item[2] and not item[1].endswith('manifest.json')]
        digests=[hashlib.sha256(item[3]).hexdigest() for item in immutable]
        self.assertEqual(len(digests),len(set(digests)))
        roles={asset['phase'] for asset in manifest['assets']}
        self.assertTrue({'original','baseline','remote','merged'}.issubset(roles))
        remote_entry=next(asset for asset in manifest['assets'] if asset['name']==name and asset['phase']=='remote')
        baseline_entry=next(asset for asset in manifest['assets'] if asset['phase']=='baseline')
        self.assertEqual(remote_entry['backup_name'],baseline_entry['backup_name'])

    def test_restored_tie_observation_remains_excluded_in_a_fresh_audit(self):
        import research_health
        from test_research_health import CoverageContract, NOW
        fixture=CoverageContract();base=self.root/'audit';base.mkdir()
        data,bundles,rid,_=fixture.fixture(base)
        fixture.prediction(data,rid,'a'*64)
        zipped(self.remote.root/'forward_20261022.sqlite.gz',(data/'forward_20261022.sqlite').read_bytes())
        shutil.copy2(data/'forward_inputs_20261022.jsonl.gz',self.remote.root)
        shutil.copy2(data/'odds_20261022.jsonl.gz',self.out)
        observation={'race_id':rid,'status':'excluded_tie','reason':'top_three_tie',
                     'available_at':'2026-10-22T12:01:00+09:00','recorded_at':'2026-10-23T01:00:00+09:00'}
        zipped(self.remote.root/'forward_observations_20261022.jsonl.gz',json.dumps(observation).encode()+b'\n')
        self.restore()
        report=research_health.audit_day(self.out,'20261022',bundles,now=NOW)
        self.assertEqual(report['results']['excluded_result_races'],1)
        self.assertEqual(report['results']['unmatched_races'],0)

    def test_restored_observation_prefix_survives_real_settlement_rerun_and_publish(self):
        from test_research_results import ObservedAdmission, loader, NOW, write_gzip
        import research_results
        fixture=ObservedAdmission();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.source['data']['result'][4][0]='3着'
        fixture.source['data']['payout']['３連単']=[['1>5>3',380,1],['1>5>4',410,2]]
        write_gzip(fixture.nk,[fixture.source]);fixture.initialize();fixture.run_settlement()
        day='20261021';name=f'forward_observations_{day}.jsonl.gz'
        prefix=(fixture.out/name[:-3]).read_bytes()
        zipped(self.remote.root/name,prefix)
        zipped(self.remote.root/f'forward_{day}.sqlite.gz',(fixture.out/f'forward_{day}.sqlite').read_bytes())
        shutil.copy2(fixture.out/f'odds_{day}.jsonl.gz',self.out)
        shutil.copy2(fixture.out/'research_history.jsonl.gz',self.remote.root)
        self.restore(day)
        research_results.settle_observed_day(fixture.bundle,self.out,fixture.extra,fixture.nk,day,module_loader=loader,clock=lambda:NOW)
        plain=self.out/name[:-3]
        self.assertTrue(plain.read_bytes().startswith(prefix))
        self.assertGreater(len(plain.read_bytes()),len(prefix))
        research_assets.pack_state(self.out,stage='settle')
        self.assertEqual(gzip.decompress((self.out/name).read_bytes()),plain.read_bytes())
        research_assets.upload_state('fixed/repo',self.out)
        self.assertEqual(gzip.decompress((self.remote.root/name).read_bytes()),plain.read_bytes())
        self.assertEqual(len(rows(self.remote.root/name)),2)

    def test_restored_runtime_prefix_is_available_in_plain_and_gzip_forms(self):
        name='forward_runtime_20261022.jsonl.gz'
        first={'at':'2026-10-22T01:00:00+00:00','error':'fixture unavailable','bundle':'v1'}
        zipped(self.remote.root/name,json.dumps(first).encode()+b'\n')
        self.restore()
        plain=self.out/name[:-3]
        self.assertTrue(plain.exists())
        self.assertEqual(json.loads(plain.read_text()),first)
        with plain.open('a') as stream:stream.write('{"bundle":"v2","error":"second"}\n')
        research_assets.pack_state(self.out)
        self.assertEqual(gzip.decompress((self.out/name).read_bytes()),plain.read_bytes())
        self.assertEqual(rows(self.out/name)[0],first)

    def test_odds_restore_failure_keeps_remote_original_and_backs_up_new_fragment(self):
        name='odds_20261022.jsonl.gz'
        original=b'{"type":"odds","race_id":"original","label":"-5"}\n'
        zipped(self.remote.root/name,original)
        self.remote.errors.add(name)
        self.restore()
        zipped(self.out/name,b'{"type":"odds","race_id":"new-fragment","label":"-2"}\n')
        with self.assertRaises(RuntimeError):research_assets.upload_state('fixed/repo',self.out)
        self.assertEqual(gzip.decompress((self.remote.root/name).read_bytes()),original)
        self.assertTrue(any(name in item[1] and 'original' in item[1] for item in self.remote.uploads))
        self.assertFalse(any(item[2] for item in self.remote.uploads))

    def test_concurrent_remote_and_local_odds_append_are_merged_under_the_odds_tag(self):
        name='odds_20261022.jsonl.gz'
        zipped(self.remote.root/name,b'{"type":"racelist","kaisai_date":"20261022","data":[]}\n')
        receipt=self.restore()
        self.assertEqual(receipt['assets'].get(name,{}).get('status'),'restored')
        self.assertIn(('fixed/repo','odds-2026-10',name),self.remote.fetches)
        with gzip.open(self.out/name,'at') as stream:stream.write('{"type":"odds","race_id":"local","label":"-5"}\n')
        with gzip.open(self.remote.root/name,'at') as stream:stream.write('{"type":"odds","race_id":"remote","label":"-2"}\n')
        research_assets.upload_state('fixed/repo',self.out)
        final=rows(self.remote.root/name)
        self.assertEqual(len(final),3)
        self.assertEqual({row.get('race_id') for row in final},{None,'local','remote'})
        canonical=[item for item in self.remote.uploads if item[1]==name and item[2]]
        self.assertEqual(len(canonical),1)
        self.assertEqual(canonical[0][0],'odds-2026-10')

    def _bundle_failure_case(self,command):
        import research_health
        from test_research_health import NOW
        day='20261022';name=f'odds_{day}.jsonl.gz'
        zipped(self.remote.root/name,b'{"type":"racelist","kaisai_date":"20261022","fetched_at":"2026-10-22T08:00:00+09:00","data":[]}\n')
        ref=self.root/'bundles.json';ref.write_text(json.dumps({'bundles':{'v1':{},'v2':{}}}))
        arguments=['research_assets.py',command,'--repo','fixed/repo','--reference',str(ref),'--data-dir',str(self.out),'--day',day]
        with contextlib.chdir(self.root),patch.object(sys,'argv',arguments),patch.object(research_assets,'install_bundle',side_effect=RuntimeError('unavailable fixture')):
            with self.assertRaises(RuntimeError):research_assets.main()
        self.assertTrue((self.out/name).exists(),'Bundle preparation failure must not prevent odds restoration')
        self.assertEqual(rows(self.out/name)[0]['type'],'racelist')
        receipt=json.loads((self.out/'research_transport.json').read_text())
        self.assertEqual(receipt['days'][day][name]['status'],'restored')
        preparation=json.loads((self.out/f'forward_preparation_{day}.json').read_text())
        self.assertEqual(set(preparation['failures']),{'v1','v2'})
        report=research_health.audit_day(self.out,day,{},now=NOW)
        self.assertIn('cohort_preparation_failed',{issue['code'] for issue in report['issues']})
        research_assets.upload_state('fixed/repo',self.out)
        self.assertTrue(any(tag=='odds-2026-10' and file==name and replace for tag,file,replace,_ in self.remote.uploads))

    def test_all_bundle_failures_leave_restored_odds_and_preparation_health_evidence(self):
        self._bundle_failure_case('prepare')

    def test_settle_all_bundle_failure_also_restores_odds_before_preparation(self):
        self._bundle_failure_case('settle')

    def test_prepare_permission_failure_preserves_local_fragment_and_disables_research(self):
        day='20261022';name=f'odds_{day}.jsonl.gz'
        self.remote.errors.add(name)
        local=b'{"type":"odds","race_id":"partial-local","label":"-5"}\n'
        zipped(self.out/name,local)
        def install(*args):
            root=Path('research-runtime');root.mkdir()
            (root/'preparation.json').write_text('{"installed":{"v1":"fixture"},"failures":{}}')
            return {'v1':'fixture'}
        arguments=['research_assets.py','prepare','--repo','fixed/repo','--data-dir',str(self.out),'--day',day]
        with contextlib.chdir(self.root),patch.object(sys,'argv',arguments),patch.object(research_assets,'install_bundles',side_effect=install):
            with self.assertRaises(RuntimeError):research_assets.main()
        self.assertEqual(gzip.decompress((self.out/name).read_bytes()),local)
        receipt=json.loads((self.out/'research_transport.json').read_text())
        self.assertEqual(receipt['days'][day][name]['status'],'error')
        self.assertEqual(receipt['days'][day][f'forward_{day}.sqlite.gz']['status'],'absent')
        with self.assertRaises(RuntimeError):research_assets.upload_state('fixed/repo',self.out)
        self.assertTrue(self.remote.uploads)
        self.assertFalse(any(item[2] for item in self.remote.uploads))


class WindowDays(unittest.TestCase):
    def test_window_restores_previous_day_and_midnight_crossing(self):
        import research_state
        from zoneinfo import ZoneInfo
        jst = ZoneInfo('Asia/Tokyo')
        examples = [
            (datetime(2026, 10, 23, 1, 6, tzinfo=jst), 12, ['20261022', '20261023']),
            (datetime(2026, 10, 23, 23, 55, tzinfo=jst), 12, ['20261023', '20261024']),
            (datetime(2026, 10, 23, 3, 0, tzinfo=jst), 12, ['20261023']),
            (datetime(2026, 10, 23, 14, 55, tzinfo=timezone.utc), 12, ['20261023', '20261024']),
        ]
        for now, minutes, expected in examples:
            with self.subTest(now=now):
                self.assertEqual(research_state.days_for_window(now, minutes), expected)


class GitHubStatuses(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP'))
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def reply(self, status, value):
        return SimpleNamespace(returncode=int(status != 200),
                               stdout=f'HTTP/2.0 {status} Status\nContent-Type: application/json\n\n'+json.dumps(value),
                               stderr='')

    def test_confirmed_absence_is_separate_from_hidden_private_repo_and_forbidden(self):
        examples = [([self.reply(404,{}), self.reply(200,{'private':True})], 'absent'),
                    ([self.reply(404,{}), self.reply(404,{})], 'error'),
                    ([self.reply(403,{})], 'error'),
                    ([self.reply(200,{'assets':[]})], 'absent')]
        for replies, expected in examples:
            with self.subTest(expected=expected,replies=len(replies)):
                with patch.object(research_assets.subprocess,'run',side_effect=replies):
                    result=research_assets.fetch_asset('fixed/repo','forward-2026-10','forward_20261022.sqlite.gz',self.out)
                self.assertEqual(result['status'],expected)

    def test_existing_asset_network_failure_is_never_absent(self):
        response=self.reply(200,{'assets':[{'name':'forward_20261022.sqlite.gz','id':42}]})
        with patch.object(research_assets.subprocess,'run',return_value=response), patch.object(research_assets,'download',return_value=False):
            result=research_assets.fetch_asset('fixed/repo','forward-2026-10','forward_20261022.sqlite.gz',self.out)
        self.assertEqual(result['status'],'error')

    def test_malformed_success_response_is_not_a_new_day(self):
        response=self.reply(200,{'unexpected':'missing assets field'})
        with patch.object(research_assets.subprocess,'run',return_value=response):
            result=research_assets.fetch_asset('fixed/repo','forward-2026-10','forward_20261022.sqlite.gz',self.out)
        self.assertEqual(result['status'],'error')


if __name__ == '__main__':
    unittest.main()
