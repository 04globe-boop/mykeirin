"""Read-only capture coverage; no forecasts, settlement, publication or scores."""
import gzip,hashlib,json,math,sqlite3,sys,unicodedata
from collections import Counter
from datetime import datetime,timedelta,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from research_assets import install_bundles,state_download,download
from research_health import audit_day,read_records
from research_runtime import bundle_module
REPO='04globe-boop/mykeirin2'
def inventory(p):
    return {str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*') if f.is_file()}
def stats(xs):
    xs=sorted(xs)
    return {'n':len(xs),'mean':sum(xs)/len(xs),'p50':xs[len(xs)//2],'p95':xs[min(len(xs)-1,math.ceil(len(xs)*.95)-1)],'max':xs[-1]} if xs else {'n':0}
def bucket(reason):
    for prefix in ('No eligible entries','No eligible odds','No schedule','History or prediction','Class outside','Unverified entry','Unsupported active','past_prediction','No audited','Ambiguous','Prediction inference'):
        if str(reason).startswith(prefix):return prefix
    return 'other'
def main():
    now=datetime.now(timezone.utc)
    bundles=install_bundles(REPO,'research/bundle.json','audit-runtime')
    if set(bundles)!={'v1','v2'}:raise RuntimeError('Both pinned bundles required')
    adapter=bundle_module(bundles['v1'],'logger_adapter');legacy=bundle_module(bundles['v1'],'legacy');data_module=bundle_module(bundles['v1'],'data')
    frozen=inventory(Path('audit-runtime'))
    extra=Path('audit-extra')
    if not download(REPO,'extra','extra_202610.jsonl.gz',extra):raise RuntimeError('Extra unavailable')
    extra_before=inventory(extra)
    for day in ('20261028','20261029'):
        out=Path('audit-copies')/day
        receipt=state_download(REPO,out,day)
        if not receipt['ok']:raise RuntimeError('Restore failure')
        if receipt['assets'][f'odds_{day}.jsonl.gz']['status']!='restored':raise RuntimeError('Raw missing')
        before=inventory(out);issues=[];records=read_records(out/f'odds_{day}.jsonl.gz',issues)
        if issues:raise RuntimeError('Unreadable source')
        schedule={}
        for r in sorted((v for v in records if v.get('type')=='racelist'),key=adapter.capture_available_at):
            if adapter.capture_available_at(r)>now:continue
            for race in r['data']:
                if str(race.get('race_id','')).startswith(day):schedule[race['race_id']]=race
        closes={}
        for rid,r in schedule.items():
            hh,mm=map(int,r['close'].split(':'))
            closes[rid]=datetime.strptime(day,'%Y%m%d').replace(tzinfo=timezone(timedelta(hours=9)))+timedelta(hours=hh,minutes=mm)
        report=audit_day(out,day,bundles,now=now,extra_path=extra/'extra_202610.jsonl.gz' if day=='20261028' else None)
        by_offset={}
        for label in ('-7','-5.5','-5','-2.5','-2','-1.5','-1'):
            due={rid for rid,close in closes.items() if close+timedelta(minutes=float(label))<=now}
            captured={v.get('race_id') for v in records if v.get('type')=='odds' and str(v.get('label'))==label and adapter.capture_available_at(v)<=now}
            by_offset[label]={'due_scheduled_races':len(due),'captured_races':len(due&captured)}
        frames={}
        for horizon in (5,2,1):
            counts=Counter();ages=[]
            for rid,close in closes.items():
                cutoff=close-timedelta(minutes=horizon)
                if cutoff>now:continue
                counts['due_scheduled_races']+=1
                try:entries=adapter.latest_capture(records,rid,'entries',cutoff.isoformat(),7200)
                except (ValueError,KeyError):counts['card_missing']+=1;continue
                active=[v for v in entries.get('data') or [] if str(v.get('sales_sts',''))!='3']
                if not 5<=len(active)<=9 or any(str(v.get('sales_sts','')) not in ('','7') for v in active):counts['card_unverified']+=1;continue
                if any(not unicodedata.normalize('NFKC',str(v.get('kyu',''))+str(v.get('han',''))).startswith(('A','S')) for v in active):counts['outside_initial_class_scope']+=1;continue
                counts['in_scope']+=1
                mapping={str(int(v['syaban'])):legacy.identity(v['name'],v['graduate']) for v in active}
                try:market=adapter.latest_capture(records,rid,'odds',cutoff.isoformat(),180)
                except ValueError:counts['recent_market_missing']+=1;continue
                try:adapter.trifecta_vector(market,mapping)
                except (ValueError,KeyError,TypeError):counts['market_incomplete_or_invalid']+=1;continue
                counts['complete_recent_market']+=1;ages.append((cutoff-adapter.capture_available_at(market)).total_seconds())
            frames['t_minus_'+str(horizon)]={'counts':dict(counts),'market_age_seconds':stats(ages)}
        events=read_records(out/f'forward_events_{day}.jsonl.gz',[])
        unavailable=Counter(bucket(v.get('reason')) for v in events if v.get('type')=='unavailable')
        error_what=Counter(str(v.get('what','unknown')) for v in records if v.get('type')=='error')
        misses=Counter(str(v.get('reason','unknown')) for v in records if v.get('type')=='miss')
        inputs=read_records(out/f'forward_inputs_{day}.jsonl.gz',[])
        ih={data_module.digest(v):v for v in inputs}
        live={}
        for cohort,bundle in bundles.items():
            manifest=json.loads((Path(bundle)/'bundle.json').read_text());marketid=manifest['candidate_ids']['market']
            path=out/f'forward_{day}.sqlite'
            if not path.exists():continue
            db=sqlite3.connect('file:'+path.resolve().as_posix()+'?mode=ro',uri=True)
            rows=db.execute('SELECT race_id,recorded_at,decision_at,payload FROM predictions WHERE model_id=?',(marketid,)).fetchall();db.close()
            issue_ages=[];cutoff_ages=[];matched=0
            for rid,issued,decision,payload in rows:
                p=json.loads(payload);actual=ih[p['input_hash']]
                try:
                    entries=adapter.latest_capture(records,rid,'entries',issued,7200)
                    active=[v for v in entries['data'] if str(v.get('sales_sts',''))!='3']
                    mapping={str(int(v['syaban'])):legacy.identity(v['name'],v['graduate']) for v in active}
                    market=adapter.latest_capture(records,rid,'odds',issued,180)
                    if adapter.trifecta_vector(market,mapping).tolist()!=actual['odds']:continue
                    at=adapter.capture_available_at(market)
                    issue_ages.append((data_module.stamp(issued)-at).total_seconds());cutoff_ages.append((data_module.stamp(decision)-at).total_seconds());matched+=1
                except (ValueError,KeyError,TypeError):continue
            values=report['cohorts'][cohort]
            live[cohort]={k:values[k] for k in ('expected_races','predicted_races','missing_races','partial_races')}
            live[cohort].update(market_candidate={k:v for k,v in values['candidates'].get('market',{}).items() if k in ('predicted','primary','labelled','primary_labelled','missing_races')},raw_matched_market_rows=matched,market_rows=len(rows),market_age_at_actual_issue=stats(issue_ages),market_age_at_nominal_cutoff=stats(cutoff_ages))
        extra_rows=[v for v in read_records(extra/'extra_202610.jsonl.gz',[]) if str(v.get('race_id','')).startswith(day)]
        source_errors=Counter((str(v.get('what')),str(v.get('status')),bucket(v.get('error'))) for v in extra_rows if v.get('type')=='error')
        history=read_records(out/'research_history.jsonl.gz',[])
        overlay={'races':len(history),'latest_race_date':max((v.get('date','') for v in history),default=None),'latest_available_at':max((v.get('available_at','') for v in history),default=None)}
        if inventory(out)!=before or inventory(Path('audit-runtime'))!=frozen or inventory(extra)!=extra_before:raise RuntimeError('Original copy changed')
        print(json.dumps({'schema':'capture-coverage-inspection-v1','day':day,'observed_at':now.isoformat(),'scheduled':len(schedule),'closed_races':sum(v<=now for v in closes.values()),'raw_record_types':dict(Counter(v.get('type') for v in records)),'by_offset':by_offset,'strict_nominal_frames':frames,'cohorts':live,'unavailable_attempt_reason_buckets':dict(unavailable),'raw_errors_by_what':dict(error_what),'raw_miss_attempts_by_reason':dict(misses),'extra_error_buckets':[{'source':k[0],'status':k[1],'category':k[2],'count':v} for k,v in source_errors.items()],'history_overlay':overlay,'issues':[v['code'] for v in report['issues']],'original_files_unchanged':True,'performance_reported':False},ensure_ascii=False))
if __name__=='__main__':main()
