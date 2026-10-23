"""Transport frozen research artifacts with an explicitly pinned archive hash.

Only this repository's committed reference chooses executable model code.
Daily forecasts and observations remain private Release assets, outside Git.
"""
import argparse,gzip,hashlib,json,os,re,shutil,subprocess,sys,tempfile,uuid,zipfile
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from research_state import (StateIntegrityError,asset_day,asset_kind,compress,
                            days_for_window,merge_asset,sha256)


def gh(*args,required=True):
    result=subprocess.run(['gh',*args],capture_output=True,text=True)
    if result.returncode and required:raise RuntimeError('GitHub artifact operation failed: '+result.stderr[-300:])
    return result.returncode==0


def download(repo,tag,name,directory):
    Path(directory).mkdir(parents=True,exist_ok=True)
    return gh('release','download',tag,'--repo',repo,'-p',name,'-D',str(directory),'--clobber',required=False)


def install_bundle(repo,reference,directory):
    config=json.loads(Path(reference).read_text(encoding='utf-8'));target=Path(directory)
    target.mkdir(parents=True,exist_ok=True)
    if not download(repo,config['tag'],config['file'],target):raise RuntimeError('Frozen bundle unavailable')
    archive=target/config['file']
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=config['sha256']:raise ValueError('Bundle SHA-256 mismatch')
    root=target/'bundle'
    with zipfile.ZipFile(archive) as z:
        if sum(info.file_size for info in z.infolist())>100_000_000:raise ValueError('Oversized bundle')
        for member in z.namelist():
            if not (root/member).resolve().is_relative_to(root.resolve()):raise ValueError('Unsafe archive path')
        z.extractall(root)
    return root


def install_bundles(repo,reference,directory):
    config=json.loads(Path(reference).read_text(encoding='utf-8'));target=Path(directory)
    entries=config.get('bundles',{'v1':config});installed={};failures={}
    if not entries:raise ValueError('No registered bundles')
    for name,item in entries.items():
        if not name.replace('-','').isalnum():raise ValueError('Invalid cohort name')
        destination=target/name;destination.mkdir(parents=True,exist_ok=True)
        ref=destination/'reference.json';ref.write_text(json.dumps(item),encoding='utf-8')
        try:installed[name]=str(install_bundle(repo,ref,destination))
        except Exception as error:failures[name]=type(error).__name__+': '+str(error)[:200]
    (target/'active_bundles.json').write_text(json.dumps(installed,indent=2),encoding='utf-8')
    (target/'preparation.json').write_text(json.dumps({'installed':installed,'failures':failures},indent=2),encoding='utf-8')
    print(json.dumps({'installed':installed,'failures':failures}))
    if not installed:raise RuntimeError('No research bundle available')
    return installed


def _api_json(endpoint):
    """Read the HTTP status, never infer 'new day' from a failed gh download."""
    result=subprocess.run(['gh','api',endpoint,'--include'],capture_output=True,text=True)
    statuses=re.findall(r'^HTTP/\S+\s+(\d{3})',result.stdout,re.MULTILINE)
    status=int(statuses[-1]) if statuses else (200 if result.returncode==0 else None)
    body=re.split(r'\r?\n\r?\n',result.stdout,maxsplit=1)[-1]
    if status==200 and result.returncode==0:
        try:return status,json.loads(body)
        except ValueError:return None,{}
    return status,{}


def fetch_asset(repo,tag,name,directory):
    status,release=_api_json(f'repos/{repo}/releases/tags/{tag}')
    if status==404:
        # GitHub also hides inaccessible private repositories behind a 404.
        accessible,_=_api_json(f'repos/{repo}')
        return {'status':'absent'} if accessible==200 else {'status':'error','reason':'Repository access unverified'}
    if status!=200:return {'status':'error','reason':f'Release metadata HTTP {status}'}
    if not isinstance(release,dict) or not isinstance(release.get('assets'),list) or any(
            not isinstance(asset,dict) or not isinstance(asset.get('name'),str) for asset in release['assets']):
        return {'status':'error','reason':'Malformed release asset inventory'}
    matches=[asset for asset in release['assets'] if asset.get('name')==name]
    if not matches:return {'status':'absent'}
    if not download(repo,tag,name,directory) or not (Path(directory)/name).is_file():
        return {'status':'error','reason':'Existing asset download failed'}
    asset=matches[0]
    return {'status':'restored','asset_id':asset.get('id'),'updated_at':asset.get('updated_at'),
            'sha256':sha256(Path(directory)/name)}


def _read_json(path,default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def _write_json(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def _day_tag(day,prefix='forward'):
    return prefix+'-'+day[:4]+'-'+day[4:6]


def _local_asset(out,name,staging):
    plain=out/name[:-3] if name.endswith('.gz') else None
    if plain is not None and plain.exists():
        result=staging/name;compress(plain,result);return result
    return out/name if (out/name).exists() else None


def _install_asset(out,source,name):
    shutil.copy2(source,out/name)
    if name.endswith('.sqlite.gz') or re.match(r'forward_(?:events|runtime|observations)_\d{8}\.jsonl\.gz$',name):
        with gzip.open(source,'rb') as src,(out/name[:-3]).open('wb') as dest:
            shutil.copyfileobj(src,dest)


def state_download(repo,directory,day):
    out=Path(directory);out.mkdir(parents=True,exist_ok=True)
    receipt_path=out/'research_transport.json'
    receipt=_read_json(receipt_path,{'schema':'research-transport-v1','repo':repo,'days':{},'assets':{},'baselines':{}})
    if receipt['repo']!=repo:raise StateIntegrityError('Transport repository differs')
    assets={};baseline=out/'research_restored';baseline.mkdir(exist_ok=True)
    names=[f'forward_{day}.sqlite.gz',f'forward_events_{day}.jsonl.gz',f'forward_inputs_{day}.jsonl.gz',
           f'forward_observations_{day}.jsonl.gz',f'forward_runtime_{day}.jsonl.gz',
           f'odds_{day}.jsonl.gz','research_history.jsonl.gz']
    with tempfile.TemporaryDirectory(prefix='restore-',dir=out) as tmp:
        staging=Path(tmp)
        for name in names:
            tag='research-state' if name.startswith('research_history') else _day_tag(day,'odds' if name.startswith('odds_') else 'forward')
            try:
                item=fetch_asset(repo,tag,name,staging)
                if item['status']=='restored':
                    source=staging/name;digest=sha256(source)
                    snapshot=baseline/(digest+'-'+name)
                    if not snapshot.exists():shutil.copy2(source,snapshot)
                    ref={'path':snapshot.relative_to(out).as_posix(),'sha256':digest}
                    if ref not in receipt['baselines'].setdefault(name,[]):receipt['baselines'][name].append(ref)
                    localdir=staging/'local';localdir.mkdir(exist_ok=True)
                    local=_local_asset(out,name,localdir)
                    merged=staging/'merged'/name
                    merge_asset(local,source,merged,name)
                    _install_asset(out,merged,name)
                    item.update(sha256=digest,kind=asset_kind(name))
            except Exception as error:
                item={'status':'error','reason':type(error).__name__+': '+str(error)[:200]}
            assets[name]=item
    receipt['assets'].update(assets)
    receipt['days'][day]={name:item for name,item in assets.items() if asset_day(name)==day}
    receipt['restored_at']=datetime.now(timezone.utc).isoformat()
    _write_json(receipt_path,receipt)
    return {'day':day,'assets':assets,'ok':all(item['status']!='error' for item in assets.values())}


def pack_state(directory,stage=None):
    """Network-free capture handoff. Keep receipts and research_restored with it."""
    out=Path(directory);out.mkdir(parents=True,exist_ok=True)
    for path in sorted(out.glob('forward_*')):
        if path.suffix in ('.sqlite','.jsonl'):compress(path,out/(path.name+'.gz'))
    assets=[]
    for path in sorted([*out.glob('forward_*'),*out.glob('odds_*.jsonl.gz')]):
        if asset_day(path.name) and path.suffix in ('.gz','.json'):
            assets.append({'name':path.name,'sha256':sha256(path),'kind':asset_kind(path.name)})
    history=out/'research_history.jsonl.gz'
    if history.exists():assets.append({'name':history.name,'sha256':sha256(history),'kind':asset_kind(history.name)})
    manifest={'schema':'research-pack-v1','run_id':os.environ.get('GITHUB_RUN_ID'),
              'run_attempt':os.environ.get('GITHUB_RUN_ATTEMPT'),'stage':stage or os.environ.get('GITHUB_JOB','local'),
              'packed_at':datetime.now(timezone.utc).isoformat(),'assets':assets}
    receipt=out/'research_transport.json'
    if receipt.exists():manifest['transport_sha256']=sha256(receipt)
    _write_json(out/'research_pack.json',manifest)
    return manifest


def _ensure_release(repo,tag,title):
    if not gh('release','view',tag,'--repo',repo,required=False):
        gh('release','create',tag,'--repo',repo,'--title',title,'--notes','Private research evidence; no adoption decision.')


def upload_state(repo,directory,include_history=False):
    """Back up first, validate every asset, then publish under the shared writer lock."""
    out=Path(directory);out.mkdir(parents=True,exist_ok=True)
    source_pack=_read_json(out/'research_pack.json',None)
    packed=pack_state(out)
    run_id=re.sub(r'[^A-Za-z0-9-]','-',str(os.environ.get('GITHUB_RUN_ID','local')))+'-'+str(os.environ.get('GITHUB_RUN_ATTEMPT','1'))+'-'+uuid.uuid4().hex[:12]
    run=out/'research_runs'/run_id;run.mkdir(parents=True)
    backup=run/'backup';backup.mkdir()
    tag='research-run-'+run_id
    manifest={'schema':'research-run-backup-v1','run_id':run_id,'source_pack':source_pack or packed,
              'publication_stage':os.environ.get('GITHUB_JOB','local'),'assets':[],'errors':[]}
    _ensure_release(repo,tag,'Immutable research run '+run_id)
    preserved={}

    def preserve(path,phase,kind):
        digest=sha256(path)
        if digest not in preserved:
            dest=backup/('run-'+run_id+'-'+phase+'-'+path.name)
            shutil.copy2(path,dest)
            gh('release','upload',tag,str(dest),'--repo',repo)  # Never clobber run evidence.
            preserved[digest]=dest.name
        # Keep each role and source name even when an earlier immutable upload
        # already contains exactly these bytes (e.g. restored and remote originals).
        manifest['assets'].append({'name':path.name,'backup_name':preserved[digest],'phase':phase,'kind':kind,'sha256':digest})

    paths=[]
    for item in packed['assets']:
        name=item['name'];original=run/'original'/name;original.parent.mkdir(exist_ok=True)
        shutil.copy2(out/name,original)
        preserve(original,'original',item['kind'])
        if name!='research_history.jsonl.gz' or include_history:paths.append((name,original))
    for name in ('research_transport.json','research_pack.json'):
        if (out/name).exists():preserve(out/name,'original','transport_manifest')
    receipt=_read_json(out/'research_transport.json',{})
    if receipt.get('repo')!=repo:manifest['errors'].append('Missing or wrong transport repository receipt')
    for refs in receipt.get('baselines',{}).values():
        for ref in refs:
            path=out/ref['path']
            if not path.resolve().is_relative_to((out/'research_restored').resolve()) or not path.is_file() or sha256(path)!=ref['sha256']:
                manifest['errors'].append('Missing or altered restoration baseline')
            else:preserve(path,'baseline','restoration_baseline')
    remote_dir=run/'remote';remote_dir.mkdir()
    plans=[]
    for name,original in paths:
        day=asset_day(name)
        if day:
            statuses=receipt.get('days',{}).get(day)
            if not statuses or any(item['status']=='error' for item in statuses.values()):
                manifest['errors'].append('Unrestored research day: '+day)
        elif receipt.get('assets',{}).get(name,{}).get('status') not in ('absent','restored'):
            manifest['errors'].append('Unrestored research history')
        remote_tag=_day_tag(day,'odds' if name.startswith('odds_') else 'forward') if day else 'research-state'
        try:
            current=fetch_asset(repo,remote_tag,name,remote_dir)
            if current['status']=='error':raise StateIntegrityError('Current remote unavailable: '+name)
            remote=remote_dir/name if current['status']=='restored' else None
            if remote is not None:preserve(remote,'remote',asset_kind(name))
            merged=original
            for number,ref in enumerate(receipt.get('baselines',{}).get(name,[])):
                base=out/ref['path']
                if not base.resolve().is_relative_to((out/'research_restored').resolve()) or not base.is_file() or sha256(base)!=ref['sha256']:
                    raise StateIntegrityError('Unverified restoration baseline')
                target=run/('baseline-merge-'+str(number))/name
                merged=merge_asset(merged,base,target,name)
            merged=merge_asset(merged,remote,run/'merged'/name,name)
            plans.append((remote_tag,merged))
        except Exception as error:
            manifest['errors'].append(type(error).__name__+': '+str(error)[:300])
    manifest['status']='refused' if manifest['errors'] else 'validated'
    if not manifest['errors']:
        for _,path in plans:preserve(path,'merged',asset_kind(path.name))
    manifest_path=backup/('run-'+run_id+'-manifest.json');_write_json(manifest_path,manifest)
    gh('release','upload',tag,str(manifest_path),'--repo',repo)
    _write_json(out/'research_upload.json',manifest)
    if manifest['errors']:raise StateIntegrityError('Canonical publication refused; originals retained: '+'; '.join(manifest['errors'])[:1000])
    for remote_tag,path in plans:
        _ensure_release(repo,remote_tag,'Prospective research evidence')
        gh('release','upload',remote_tag,str(path),'--repo',repo,'--clobber')
        _install_asset(out,path,path.name)
    return manifest


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','settle','upload','pack'])
    p.add_argument('--repo');p.add_argument('--data-dir',default='data');p.add_argument('--day')
    p.add_argument('--window-min',type=float,default=12);p.add_argument('--stage')
    p.add_argument('--reference',default='research/bundle.json');p.add_argument('--extra');p.add_argument('--nk')
    p.add_argument('--history',action='store_true',help='Only the daily settlement job may publish history updates')
    a=p.parse_args();now=datetime.now(ZoneInfo('Asia/Tokyo'));day=a.day or now.strftime('%Y%m%d')
    if a.command=='pack':pack_state(a.data_dir,a.stage);return
    if not a.repo:p.error('--repo is required for network operations')
    if a.command=='upload':upload_state(a.repo,a.data_dir,a.history);return
    days=[day] if a.command=='settle' or a.day else days_for_window(now,a.window_min)
    restored=[state_download(a.repo,a.data_dir,value) for value in days]
    try:bundles=install_bundles(a.repo,a.reference,'research-runtime')
    finally:
        preparation=Path('research-runtime/preparation.json')
        if preparation.exists():shutil.copy2(preparation,Path(a.data_dir)/f'forward_preparation_{day}.json')
    if not all(item['ok'] for item in restored):raise StateIntegrityError('Research restoration failed; capture may continue without inference')
    if a.command=='settle':
        from research_runtime import settle_bundles
        print(json.dumps(settle_bundles(bundles,a.data_dir,a.extra,day,nk_path=a.nk)))


if __name__=='__main__':main()
