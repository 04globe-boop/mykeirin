"""Transport frozen research artifacts with an explicitly pinned archive hash.

Only this repository's committed reference chooses executable model code.
Daily forecasts and observations remain private Release assets, outside Git.
"""
import argparse,gzip,hashlib,json,os,shutil,subprocess,sys,zipfile
from datetime import datetime,timedelta
from pathlib import Path


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


def state_download(repo,directory,day):
    out=Path(directory);tag='forward-'+day[:4]+'-'+day[4:6]
    for pattern in (f'forward_{day}.sqlite.gz',f'forward_events_{day}.jsonl.gz',f'forward_inputs_{day}.jsonl.gz'):
        if download(repo,tag,pattern,out) and pattern.endswith(('.sqlite.gz','events_'+day+'.jsonl.gz')):
            with gzip.open(out/pattern,'rb') as src,(out/pattern[:-3]).open('wb') as dest:shutil.copyfileobj(src,dest)
    download(repo,'research-state','research_history.jsonl.gz',out)


def upload_state(repo,directory,include_history=False):
    out=Path(directory)
    for path in sorted(out.glob('forward_*')):
        if path.suffix in ('.sqlite','.jsonl'):
            with path.open('rb') as src,(out/(path.name+'.gz')).open('wb') as raw:
                with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as dest:shutil.copyfileobj(src,dest)
    for path in sorted(out.glob('forward_*')):
        if path.suffix not in ('.gz','.json'):continue
        day=path.name.split('_')[-1][:8]
        if len(day)!=8 or not day.isdigit():continue
        tag='forward-'+day[:4]+'-'+day[4:6]
        if not gh('release','view',tag,'--repo',repo,required=False):
            gh('release','create',tag,'--repo',repo,'--title','Prospective research '+day[:6],
               '--notes','Actual pre-cutoff forecasts, observed labels and miss logs. Research only; no adoption decision.')
        gh('release','upload',tag,str(path),'--repo',repo,'--clobber')
    history=out/'research_history.jsonl.gz'
    if include_history and history.exists():
        if not gh('release','view','research-state','--repo',repo,required=False):
            gh('release','create','research-state','--repo',repo,'--title','Observed research history',
               '--notes','Append-only post-race observations for future feature generation.')
        gh('release','upload','research-state',str(history),'--repo',repo,'--clobber')


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['prepare','settle','upload'])
    p.add_argument('--repo',required=True);p.add_argument('--data-dir',default='data');p.add_argument('--day')
    p.add_argument('--reference',default='research/bundle.json');p.add_argument('--extra')
    p.add_argument('--history',action='store_true',help='Only the daily settlement job may publish history updates')
    a=p.parse_args();day=a.day or datetime.now().strftime('%Y%m%d')
    if a.command=='upload':upload_state(a.repo,a.data_dir,a.history);return
    bundle=install_bundle(a.repo,a.reference,'research-runtime');state_download(a.repo,a.data_dir,day)
    if a.command=='settle':
        tag='odds-'+day[:4]+'-'+day[4:6];download(a.repo,tag,f'odds_{day}.jsonl.gz',a.data_dir)
        sys.path.insert(0,str(bundle/'source'))
        from keirin_lab.settlement import settle_day
        print(json.dumps(settle_day(bundle,a.data_dir,a.extra,day)))


if __name__=='__main__':main()
