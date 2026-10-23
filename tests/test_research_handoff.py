"""Private-only capture handoff through a fake GitHub subprocess boundary."""
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
import research_assets

try:
    handoff=importlib.import_module('research_handoff')
except ModuleNotFoundError:
    handoff=None


class FakeGitHub:
    def __init__(self):
        self.private=True
        self.releases={}
        self.uploads=[]

    def run(self,args,**kwargs):
        parts=args[1:]
        if parts[0]=='api':
            return SimpleNamespace(returncode=0,stdout=json.dumps({'private':self.private,'full_name':'owner/private-data'}),stderr='')
        action=parts[1];tag=parts[2]
        if action=='create':
            if tag in self.releases:
                return SimpleNamespace(returncode=1,stdout='',stderr='already exists')
            self.releases[tag]={}
        elif action=='upload':
            for path in parts[3:parts.index('--repo')]:
                file=Path(path)
                if file.name in self.releases[tag] and '--clobber' not in parts:
                    return SimpleNamespace(returncode=1,stdout='',stderr='already exists')
                self.releases[tag][file.name]=file.read_bytes()
                self.uploads.append((tag,file.name,'--clobber' in parts))
        elif action=='download':
            name=parts[parts.index('-p')+1]
            directory=Path(parts[parts.index('-D')+1])
            if name not in self.releases.get(tag,{}):
                return SimpleNamespace(returncode=1,stdout='',stderr='missing asset')
            (directory/name).write_bytes(self.releases[tag][name])
        else:
            raise AssertionError('Unexpected GitHub operation: '+action)
        return SimpleNamespace(returncode=0,stdout='',stderr='')


class PrivateHandoff(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(handoff,'research_handoff is not implemented')
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('KEIRIN_TEST_TMP'))
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'capture';self.source.mkdir()
        self.target=self.root/'publish';self.target.mkdir()
        self.remote=FakeGitHub()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'GITHUB_RUN_ID':'123456','GITHUB_RUN_ATTEMPT':'2'}).start()
        patch.object(handoff.subprocess,'run',side_effect=self.remote.run).start()
        self.fixture()

    def fixture(self):
        data=self.source/'data';data.mkdir()
        db=sqlite3.connect(data/'forward_20261022.sqlite')
        db.executescript('''CREATE TABLE predictions(race_id TEXT,model_id TEXT,payload TEXT,
            recorded_at TEXT,PRIMARY KEY(race_id,model_id));
            CREATE TABLE labels(race_id TEXT PRIMARY KEY,truth TEXT,recorded_at TEXT);''')
        db.execute('INSERT INTO predictions VALUES(?,?,?,?)',('r1','v1','{"input_hash":"unchanged"}','original'))
        db.commit();db.close()
        (data/'forward_events_20261022.jsonl').write_text('{"type":"predicted","race_id":"r1"}\n')
        for name in ('forward_inputs_20261022.jsonl.gz','odds_20261022.jsonl.gz'):
            with gzip.open(data/name,'wt') as stream:stream.write('{"race_id":"r1"}\n')
        baseline=data/'research_restored';baseline.mkdir()
        original=gzip.compress(b'{"type":"predicted","race_id":"r1"}\n',mtime=0)
        digest=hashlib.sha256(original).hexdigest()
        name='forward_events_20261022.jsonl.gz'
        path=baseline/(digest+'-'+name);path.write_bytes(original)
        receipt={'schema':'research-transport-v1','repo':'owner/private-data',
                 'days':{'20261022':{name:{'status':'restored'}}},'assets':{name:{'status':'restored'}},
                 'baselines':{name:[{'path':path.relative_to(data).as_posix(),'sha256':digest}]}}
        (data/'research_transport.json').write_text(json.dumps(receipt))
        (data/'logger_summary_20261022_123456.json').write_text('{"date":"20261022","errors":0}')
        runtime=self.source/'research-runtime';(runtime/'v1/bundle').mkdir(parents=True)
        (runtime/'active_bundles.json').write_text('{"v1":"research-runtime/v1/bundle"}')
        (runtime/'preparation.json').write_text('{"installed":{"v1":"research-runtime/v1/bundle"}}')
        (runtime/'v1/bundle/bundle.json').write_text('{"candidate_ids":{"a":"v1"}}')
        research_assets.pack_state(data,stage='capture')

    def staged(self):
        result=handoff.stage('owner/private-data',self.source)
        self.tag=result['tag']
        self.archive_name=result['archive']
        self.receipt_name=result['receipt']
        return result

    def rewrite_archive(self,transform,compression=zipfile.ZIP_STORED):
        stored=self.remote.releases[self.tag]
        source=zipfile.ZipFile(io.BytesIO(stored[self.archive_name]))
        files={info.filename:source.read(info) for info in source.infolist()}
        source.close()
        transform(files)
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',compression=compression) as archive:
            for name,body in files.items():
                info=zipfile.ZipInfo(name)
                # Preserve malicious wire names exactly, including Windows
                # backslashes and NUL, rather than letting the test builder clean them.
                info.filename=name;info.orig_filename=name;info.compress_type=compression
                archive.writestr(info,body)
        payload=stream.getvalue();stored[self.archive_name]=payload
        receipt=json.loads(stored[self.receipt_name]);receipt['sha256']=hashlib.sha256(payload).hexdigest();receipt['size']=len(payload)
        stored[self.receipt_name]=json.dumps(receipt).encode()

    def test_real_capture_roundtrip_restores_data_for_safe_canonical_upload(self):
        self.staged()
        result=handoff.receive('owner/private-data',self.target)
        self.assertGreater(result['files'],5)
        for name in ('data/forward_20261022.sqlite.gz','data/odds_20261022.jsonl.gz',
                     'data/logger_summary_20261022_123456.json','research-runtime/v1/bundle/bundle.json'):
            self.assertEqual((self.source/name).read_bytes(),(self.target/name).read_bytes())
        # Exercise the real append-preserving upload after receiving the handoff.
        writes=[]
        def safe_gh(*args,required=True):
            if args[:2]==('release','upload'):writes.append((args[2],Path(args[3]).name,'--clobber' in args))
            return True
        with patch.object(research_assets,'fetch_asset',return_value={'status':'absent'}),patch.object(research_assets,'gh',side_effect=safe_gh):
            research_assets.upload_state('owner/private-data',self.target/'data')
        self.assertTrue(any(name=='forward_20261022.sqlite.gz' and replace for _,name,replace in writes))
        self.assertTrue(writes and not writes[0][2])
        self.assertTrue(all(not clobber for _,_,clobber in self.remote.uploads))

    def test_code_weights_secret_and_raw_ledger_are_never_sent(self):
        (self.source/'secret.env').write_text('DO_NOT_SEND')
        code=self.source/'research-runtime/v1/bundle/source/keirin_lab';code.mkdir(parents=True)
        (code/'live_worker.py').write_text('DO_NOT_SEND')
        (self.source/'research-runtime/v1/bundle/weights.bin').write_bytes(b'DO_NOT_SEND')
        (self.source/'data/forward_bad.py').write_text('DO_NOT_SEND')
        (self.source/'data/research_restored/secret.py').write_text('DO_NOT_SEND')
        self.staged()
        with zipfile.ZipFile(io.BytesIO(self.remote.releases[self.tag][self.archive_name])) as archive:
            names=archive.namelist()
            self.assertFalse(any(name.endswith(('.py','.bin','.env','.sqlite')) for name in names))
            self.assertTrue('research-runtime/v1/bundle/bundle.json' in names)
            self.assertFalse(any(b'DO_NOT_SEND' in archive.read(name) for name in names))

    def test_capture_handoff_without_history_body_can_publish_with_history_disabled(self):
        data=self.source/'data';name='research_history.jsonl.gz'
        body=gzip.compress(b'{"race_id":"previous","available_at":"original"}\n',mtime=0)
        (data/name).write_bytes(body)
        digest=hashlib.sha256(body).hexdigest()
        baseline=data/'research_restored'/(digest+'-'+name);baseline.write_bytes(body)
        receipt=json.loads((data/'research_transport.json').read_text())
        receipt['assets'][name]={'status':'restored'}
        receipt['baselines'][name]=[{'path':baseline.relative_to(data).as_posix(),'sha256':digest}]
        (data/'research_transport.json').write_text(json.dumps(receipt))
        research_assets.pack_state(data,stage='capture')
        self.staged();handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data'/name).exists())
        self.assertTrue((self.target/'data'/baseline.relative_to(data)).exists())
        writes=[]
        def safe_gh(*args,required=True):
            if args[:2]==('release','upload'):writes.append((args[2],Path(args[3]).name,'--clobber' in args))
            return True
        with patch.object(research_assets,'fetch_asset',return_value={'status':'absent'}),patch.object(research_assets,'gh',side_effect=safe_gh):
            research_assets.upload_state('owner/private-data',self.target/'data',include_history=False)
        self.assertTrue(any(name in filename and not replace for _,filename,replace in writes))
        self.assertFalse(any(tag=='research-state' and replace for tag,_,replace in writes))

    def test_strict_restored_odds_baseline_survives_handoff_and_concurrent_publish(self):
        data=self.source/'data';name='odds_20261022.jsonl.gz'
        canonical=self.root/'canonical';canonical.mkdir()
        original=b'{"type":"odds","race_id":"old","label":"-5","fetched_at":"original-time"}\n'
        (canonical/name).write_bytes(gzip.compress(original,mtime=0))
        shutil.copy2(canonical/name,data/name)
        def fetch(repo,tag,asset,directory):
            if asset.startswith('odds_'):
                self.assertEqual(tag,'odds-2026-10')
            source=canonical/asset
            if not source.exists():return {'status':'absent'}
            shutil.copy2(source,Path(directory)/asset)
            return {'status':'restored'}
        with patch.object(research_assets,'fetch_asset',side_effect=fetch):
            restored=research_assets.state_download('owner/private-data',data,'20261022')
        self.assertTrue(restored['ok'])
        with gzip.open(data/name,'at') as stream:
            stream.write('{"type":"odds","race_id":"local-new","label":"-2","fetched_at":"local-time"}\n')
        research_assets.pack_state(data,stage='capture')
        self.staged();handoff.receive('owner/private-data',self.target)
        with gzip.open(canonical/name,'at') as stream:
            stream.write('{"type":"odds","race_id":"remote-new","label":"-1","fetched_at":"remote-time"}\n')
        writes=[]
        def publish(*args,required=True):
            if args[:2]==('release','upload'):
                path=Path(args[3]);replace='--clobber' in args
                writes.append((args[2],path.name,replace))
                if replace:shutil.copy2(path,canonical/path.name)
            return True
        with patch.object(research_assets,'fetch_asset',side_effect=fetch),patch.object(research_assets,'gh',side_effect=publish):
            try:
                research_assets.upload_state('owner/private-data',self.target/'data')
            except RuntimeError as error:
                self.fail('Strict restored odds must retain their baseline through handoff: '+str(error))
        with gzip.open(canonical/name,'rt') as stream:final=[json.loads(line) for line in stream]
        self.assertEqual({row['race_id'] for row in final},{'old','local-new','remote-new'})
        self.assertEqual(len(final),3)
        self.assertEqual(next(row for row in final if row['race_id']=='old')['fetched_at'],'original-time')
        receipt=json.loads((self.target/'data/research_transport.json').read_text())
        ref=receipt['baselines'][name][0]
        self.assertEqual(hashlib.sha256((self.target/'data'/ref['path']).read_bytes()).hexdigest(),ref['sha256'])
        self.assertIn(('odds-2026-10',name,True),writes)
        self.assertFalse(writes[0][2])

    def test_public_repository_is_rejected_before_any_upload(self):
        self.remote.private=False
        with self.assertRaises(RuntimeError):self.staged()
        self.assertFalse(self.remote.releases)
        self.assertFalse(self.remote.uploads)

    def test_second_stage_cannot_replace_the_same_run(self):
        self.staged();before=dict(self.remote.releases[self.tag])
        with self.assertRaises(RuntimeError):self.staged()
        self.assertEqual(self.remote.releases[self.tag],before)

    def test_stage_does_not_rewrite_existing_local_artifacts(self):
        existing=self.source/'data/research_handoff_stage.json'
        existing.write_text('{"keep":"original"}')
        self.staged()
        self.assertEqual(existing.read_text(),'{"keep":"original"}')

    def test_archive_tamper_is_rejected_before_extracting_any_file(self):
        self.staged()
        self.remote.releases[self.tag][self.archive_name]+=b'tampered'
        with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data').exists())

    def test_payload_hash_tamper_with_updated_archive_receipt_is_rejected(self):
        self.staged()
        self.rewrite_archive(lambda files:files.update({'data/forward_inputs_20261022.jsonl.gz':b'changed'}))
        with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data').exists())

    def test_unsafe_and_unexpected_archive_paths_are_rejected_before_code_write(self):
        for attack in ('../outside.py','logger/run_logger.py','data/../logger/run_logger.py',
                       'data\\forward_20261022.sqlite.gz','research-runtime/v1/bundle/source/code.py'):
            with self.subTest(path=attack):
                self.remote.releases.clear();self.staged()
                self.rewrite_archive(lambda files:files.update({attack:b'code'}))
                with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
                self.assertFalse((self.target/'data').exists())
                self.assertFalse((self.target/'logger/run_logger.py').exists())

    def test_wrong_run_receipt_is_rejected(self):
        self.staged()
        stored=self.remote.releases[self.tag]
        receipt=json.loads(stored[self.receipt_name]);receipt['run_id']='different'
        stored[self.receipt_name]=json.dumps(receipt).encode()
        with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data').exists())

    def test_total_unpacked_limit_is_checked_before_extraction(self):
        self.staged()
        with patch.object(handoff,'MAX_TOTAL_BYTES',10):
            with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data').exists())

    def test_compressed_zip_bomb_shape_is_rejected_even_with_valid_archive_hash(self):
        self.staged()
        self.rewrite_archive(lambda files:None,compression=zipfile.ZIP_DEFLATED)
        with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data').exists())

    def test_nul_truncated_archive_path_is_rejected_before_extraction(self):
        self.staged()
        def poison(files):
            files['data/research_pack.json\0.py']=files.pop('data/research_pack.json')
        self.rewrite_archive(poison)
        with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertFalse((self.target/'data').exists())

    def test_staging_capacity_includes_the_manifest_before_uploading(self):
        data_size=sum(path.stat().st_size for _,path in handoff._sources(self.source))
        with patch.object(handoff,'MAX_TOTAL_BYTES',data_size):
            with self.assertRaises(RuntimeError):self.staged()
        self.assertFalse(self.remote.releases)

    def test_existing_code_and_other_content_is_not_overwritten(self):
        self.staged()
        code=self.target/'logger/run_logger.py';code.parent.mkdir();code.write_text('existing code')
        existing=self.target/'data/research_transport.json';existing.parent.mkdir();existing.write_text('{"keep":"original"}')
        with self.assertRaises(RuntimeError):handoff.receive('owner/private-data',self.target)
        self.assertEqual(code.read_text(),'existing code')
        self.assertEqual(existing.read_text(),'{"keep":"original"}')
        self.assertFalse((self.target/'data/forward_20261022.sqlite.gz').exists())


if __name__=='__main__':unittest.main()
