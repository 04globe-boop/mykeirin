"""Read-only post-deadline audit; only private diagnostic evidence is written."""
import argparse,hashlib,json,os,tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.error import HTTPError
from zoneinfo import ZoneInfo
import research_assets
from research_health import audit_day
from research_health_publish import (ASSET,DATA_REPO,SOURCE_REPO,GitHubHealthStore,
    _canonical,_safe_report,_private_repository,_day,publish_health)
JST=ZoneInfo('Asia/Tokyo')

def choose_day(now,day=None):
    if now.tzinfo is None or now.utcoffset() is None:raise ValueError('Timezone required')
    target=_day(day or (now.astimezone(JST).date()-timedelta(days=1)).strftime('%Y%m%d'))
    due=(datetime.strptime(target,'%Y%m%d')+timedelta(days=1,hours=8)).replace(tzinfo=JST)
    if now<due:raise ValueError('Result audit deadline has not passed')
    return target

def stage_report(report,client,*,run_id,attempt,run_url,now):
    choose_day(now,report['day'])
    safe=_safe_report(report,run_id=run_id,attempt=attempt,run_url=run_url,now=now)
    _private_repository(client,DATA_REPO)
    raw=_canonical(safe);digest=hashlib.sha256(raw).hexdigest()
    name=f"health-{safe['day']}-run-{run_id}-attempt-{attempt}-{digest}.json"
    try:release=client.get_release(day=safe['day'],create=True)
    except HTTPError as error:
        if error.code!=422:raise
        release=client.get_release(day=safe['day'],create=False)
    client.upload_asset(release,name,raw)
    return {'day':safe['day'],'asset_name':name,'status':safe['status']}

def finish_report(client,*,day,asset_name,run_id,attempt,run_url,now):
    day=choose_day(now,day);m=ASSET.fullmatch(asset_name)
    if not m or (m[1],m[2],m[3])!=(day,run_id,attempt):raise ValueError('Staged report identity mismatch')
    _private_repository(client,DATA_REPO)
    release=client.get_release(day=day,create=False);raw=client.get_asset(release,asset_name)
    if not isinstance(raw,bytes) or hashlib.sha256(raw).hexdigest()!=m[4]:raise ValueError('Staged report hash mismatch')
    value=json.loads(raw);safe=_safe_report(value,run_id=run_id,attempt=attempt,run_url=run_url,now=now)
    if value!=safe or safe['day']!=day:raise ValueError('Staged report contents mismatch')
    result=publish_health(safe,client,repo=DATA_REPO,run_id=run_id,run_attempt=attempt,run_url=run_url,clock=lambda:now)
    return dict(result,status=safe['status'])

def stable_snapshot(repo,day,restored):
    tags={'forward-'+day[:4]+'-'+day[4:6], 'odds-'+day[:4]+'-'+day[4:6], 'research-state'}
    inventories={}
    for tag in tags:
        status,value=research_assets._api_json(f'repos/{repo}/releases/tags/{tag}')
        if status==404:inventories[tag]={};continue
        if status!=200 or not isinstance(value.get('assets'),list):return False
        inventories[tag]={v['name']:v for v in value['assets']}
    for name,item in restored['assets'].items():
        tag='research-state' if name=='research_history.jsonl.gz' else ('odds-' if name.startswith('odds_') else 'forward-')+day[:4]+'-'+day[4:6]
        current=inventories[tag].get(name)
        if item['status']=='absent':
            if current is not None:return False
        elif item['status']!='restored' or current is None or (item['asset_id'],item['updated_at'])!=(current['id'],current['updated_at']):return False
    return True

def prepare_report(day,now):
    # Every source is downloaded into an owned temporary copy, never the logger's data.
    with tempfile.TemporaryDirectory(prefix='health-audit-',dir=os.environ.get('KEIRIN_TEST_TMP')) as tmp:
        base=Path(tmp);data=base/'data'
        try:
            restored=research_assets.state_download(DATA_REPO,data,day)
            if not restored['ok']:raise ValueError('Incomplete restoration')
            bundles=research_assets.install_bundles(DATA_REPO,'research/bundle.json',base/'runtime')
            if set(bundles)!={'v1','v2'}:raise ValueError('Incomplete cohort preparation')
            extra=base/'extra';name='extra_'+day[:6]+'.jsonl.gz'
            research_assets.fetch_asset(DATA_REPO,'extra',name,extra)
            report=audit_day(data,day,bundles,now=now,extra_path=extra/name)
            if not stable_snapshot(DATA_REPO,day,restored):raise ValueError('Snapshot changed')
            return report
        except (OSError,ValueError,RuntimeError,KeyError,TypeError):
            return {'schema_version':'operational-health-v1','day':day,'observed_at':now.isoformat(),
                'status':'degraded','schedule':{'known':False},'results':{},'cohorts':{},
                'issues':[{'code':'postdeadline_audit_unavailable','component':'evidence','severity':'error'}],
                'performance_reported':False,'evaluation_denominator_changed':False}

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=('prepare','finish'));p.add_argument('--day');p.add_argument('--asset');a=p.parse_args()
    now=datetime.now(timezone.utc);day=choose_day(now,a.day)
    run=os.environ['GITHUB_RUN_ID'];attempt=os.environ['GITHUB_RUN_ATTEMPT'];url=f'https://github.com/{SOURCE_REPO}/actions/runs/{run}'
    client=GitHubHealthStore(DATA_REPO,os.environ['GH_TOKEN'])
    if a.mode=='prepare':
        report=prepare_report(day,now);result=stage_report(report,client,run_id=run,attempt=attempt,run_url=url,now=datetime.now(timezone.utc))
        with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as stream:
            stream.write('day='+result['day']+chr(10)+'asset='+result['asset_name']+chr(10))
    else:
        result=finish_report(client,day=day,asset_name=a.asset,run_id=run,attempt=attempt,run_url=url,now=now)
    print(json.dumps(result,ensure_ascii=False))
    if a.mode=='finish' and result['status']!='healthy':raise SystemExit(1)
if __name__=='__main__':main()
