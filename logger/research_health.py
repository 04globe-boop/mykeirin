"""Score-free operational coverage; the ledger, not legacy event counts, proves issuance."""
import argparse
from collections import Counter
from datetime import datetime,timedelta,timezone
import gzip,hashlib,json,math,re,sqlite3,unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

JST=ZoneInfo('Asia/Tokyo')
SCHEMA='operational-health-v1'

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def stamp(value):
    result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Timezone required')
    return result

def read_records(path,issues):
    path=Path(path)
    if not path.exists():return []
    values=[]
    try:
        op=gzip.open if path.suffix=='.gz' else open
        with op(path,'rt',encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():continue
                value=json.loads(line)
                if not isinstance(value,dict):raise ValueError('Record must be an object')
                values.append(value)
    except (OSError,EOFError,ValueError) as error:
        issues.append({'code':'evidence_unreadable','component':'evidence','severity':'error','file':path.name,'reason':type(error).__name__})
    return values

def audit_day(data_dir,day,bundles,*,now=None,extra_path=None,failures=()):
    if len(day)!=8 or not day.isdigit():raise ValueError('YYYYMMDD required')
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('Timezone required')
    out=Path(data_dir);issues=[]
    def issue(code,component,**details):
        issues.append(dict(code=code,component=component,severity='error',**details))
    for value in failures:
        if not re.fullmatch(r'[a-z_]+:[a-z_]+',value):raise ValueError('Invalid operational failure')
        component,code=value.split(':');issue(code,component)
    records=read_records(out/f'odds_{day}.jsonl.gz',issues)
    lists=[];schedule={};latest_count=None
    for record in records:
        if record.get('type')!='racelist' or record.get('kaisai_date',day)!=day:continue
        try:
            at=stamp(record['fetched_at'])
            if at>now:continue
            rows=record['data']
            if not isinstance(rows,list):raise ValueError('Invalid race list')
            selected=[v for v in rows if isinstance(v,dict) and str(v.get('race_id','')).startswith(day)]
            lists.append((at,selected))
        except (KeyError,ValueError,TypeError):issue('schedule_record_invalid','schedule')
    for _,rows in sorted(lists,key=lambda v:v[0]):
        latest_count=len(rows)
        for row in rows:schedule[row['race_id']]=row
    if not lists:issue('schedule_unknown','schedule')
    elif schedule and latest_count==0:issue('unexpected_empty_schedule','schedule')
    known=bool(lists);eligible=set();excluded={};unverified=set();completed=set();cutoffs={}
    for rid,meta in schedule.items():
        try:
            close=stamp(f'{day[:4]}-{day[4:6]}-{day[6:]}T{meta["close"]}:00+09:00')
            cutoff=close-timedelta(minutes=5);cutoffs[rid]=cutoff
            if cutoff>now:continue
            completed.add(rid)
            cards=[]
            for record in records:
                if record.get('type')=='entries' and record.get('race_id')==rid:
                    at=stamp(record['fetched_at'])
                    if at<=cutoff:cards.append((at,record.get('data')))
            if not cards:unverified.add(rid);continue
            entries=max(cards,key=lambda v:v[0])[1]
            active=[v for v in entries if str(v.get('sales_sts',''))!='3']
            classes=[unicodedata.normalize('NFKC',str(v.get('kyu',''))+str(v.get('han',''))) for v in active]
            if classes and all(v.startswith('L') for v in classes):excluded[rid]='class_outside_registered_scope'
            elif len(active)<5 or len(active)>9:excluded[rid]='unsupported_active_field_size'
            elif all(v.startswith(('A','S')) for v in classes):eligible.add(rid)
            else:unverified.add(rid)
        except (KeyError,ValueError,TypeError):unverified.add(rid);issue('schedule_or_card_invalid','schedule',race_id=rid)
    if unverified:issue('eligibility_unverified','capture',races=len(unverified))
    inputs=read_records(out/f'forward_inputs_{day}.jsonl.gz',issues)
    input_hashes={digest(v) for v in inputs}
    prediction_rows=[];labels={};path=out/f'forward_{day}.sqlite'
    compressed=path.with_suffix(path.suffix+'.gz')
    if path.exists() or compressed.exists():
        try:
            if path.exists():db=sqlite3.connect('file:'+path.resolve().as_posix()+'?mode=ro',uri=True)
            else:
                with gzip.open(compressed,'rb') as stream:raw=stream.read(512_000_001)
                if len(raw)>512_000_000:raise ValueError('Ledger size limit')
                db=sqlite3.connect(':memory:');db.deserialize(raw)
            try:
                db.execute('PRAGMA trusted_schema=OFF')
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Ledger integrity')
                prediction_rows=db.execute('SELECT race_id,model_id,recorded_at,decision_at,close_at,frozen_at,payload,sha256,synthetic FROM predictions').fetchall()
                labels={rid:(at,truth) for rid,at,truth in db.execute('SELECT race_id,available_at,truth FROM labels')}
            finally:db.close()
        except (sqlite3.Error,ValueError,OSError,EOFError):issue('ledger_unreadable','evidence')
    actual=set();model_rows={};missing_inputs=set();invalid_predictions=set()
    for rid,mid,at,decision,close,frozen,raw,sha,synthetic in prediction_rows:
        try:
            value=json.loads(raw);p=value['p']
            if not rid.startswith(day) or digest(value)!=sha:raise ValueError('Prediction identity/hash')
            if not p or len(p)!=len(value['outcomes']) or not all(math.isfinite(v) and v>=0 for v in p) or abs(sum(p)-1)>1e-6:raise ValueError('Probability distribution')
            issued=stamp(at);target=stamp(decision)
            valid=not synthetic and value.get('primary_horizon_eligible') is True and stamp(frozen)<issued<=target<stamp(close) and 0<=(target-issued).total_seconds()<=30 and (stamp(close)-target).total_seconds()==300
            if issued>now:raise ValueError('Future prediction')
            if value['input_hash'] not in input_hashes:missing_inputs.add(rid)
            labelled=rid in labels
            if labelled:
                label_at,truth=labels[rid]
                if not stamp(close)<stamp(label_at)<=now or json.loads(truth) not in value['outcomes']:
                    issue('label_invalid','results',race_id=rid);labelled=False
            model_rows.setdefault(mid,[]).append((rid,valid,labelled));actual.add(rid)
        except (KeyError,ValueError,TypeError):invalid_predictions.add(rid)
    if invalid_predictions:issue('prediction_invalid','evidence',races=len(invalid_predictions))
    if missing_inputs:issue('prediction_input_missing','evidence',races=len(missing_inputs))
    unknown_actual=actual-set(schedule)
    if unknown_actual:issue('prediction_schedule_missing','evidence',races=len(unknown_actual))
    cohorts={}
    if not bundles:issue('cohorts_unavailable','inference')
    for name,bundle in bundles.items():
        try:manifest=json.loads((Path(bundle)/'bundle.json').read_text(encoding='utf-8'));candidate_ids=manifest['candidate_ids']
        except (OSError,ValueError,KeyError):issue('cohort_manifest_unavailable','inference',cohort=name);continue
        before_freeze=set()
        if manifest.get('frozen_at'):
            try:before_freeze={rid for rid in eligible if cutoffs[rid]<=stamp(manifest['frozen_at'])}
            except (ValueError,TypeError):issue('cohort_freeze_time_invalid','evidence',cohort=name);continue
        expected=eligible-before_freeze
        candidates={};union=set();labelled=set();per_race=Counter()
        for candidate,mid in candidate_ids.items():
            rows=model_rows.get(mid,[]);primary={rid for rid,ok,_ in rows if ok};matched={rid for rid,ok,label in rows if ok and label}
            union.update(primary);labelled.update(matched);per_race.update(primary)
            missing=expected-primary
            candidates[candidate]={'predicted':len(rows),'primary':len(primary),'labelled':sum(v[2] for v in rows),'primary_labelled':len(matched),'missing_races':len(missing),'missing_race_ids':sorted(missing)}
        missing=expected-union
        partial={rid for rid,n in per_race.items() if n<len(candidate_ids) and rid in expected}
        if union&before_freeze:issue('prediction_before_cohort_freeze','evidence',cohort=name,races=len(union&before_freeze))
        cohorts[name]={'candidates':candidates,'expected_races':len(expected),'excluded_before_freeze':len(before_freeze),'predicted_races':len(union),'labelled_races':len(labelled),'missing_races':len(missing),'missing_race_ids':sorted(missing),'partial_races':len(partial),'partial_race_ids':sorted(partial)}
        if missing:issue('forecast_missing','inference',cohort=name,races=len(missing))
        if partial:issue('candidate_partial_issuance','inference',cohort=name,races=len(partial))
    events=read_records(out/f'forward_events_{day}.jsonl.gz',issues)
    misses={v.get('race_id') for v in events if v.get('type')=='miss'}
    predicted_events={v.get('race_id') for v in events if v.get('type')=='predicted'}
    contradicted=misses&actual;event_missing=actual-predicted_events
    if contradicted:issue('legacy_miss_conflicts_with_ledger','events',races=len(contradicted))
    if event_missing:issue('prediction_event_evidence_missing','events',races=len(event_missing))
    observations=read_records(out/f'forward_observations_{day}.jsonl',issues)
    if not observations:observations=read_records(out/f'forward_observations_{day}.jsonl.gz',issues)
    latest_observations={}
    for value in observations:
        try:
            rid=value['race_id'];recorded=stamp(value['recorded_at'])
            missing_source=(value.get('schema_version')=='result-observation-v1'
                            and value.get('status')=='unavailable' and value.get('reason')=='no_result_observation'
                            and all(key in value and value[key] is None for key in
                                    ('available_at','source_id','source_type','source_sha256')))
            if not missing_source and stamp(value['available_at'])>now:continue
            if not rid.startswith(day) or recorded>now:continue
            # A failed lookup does not refute a previously confirmed result.
            if missing_source:continue
            if rid not in latest_observations or recorded>=stamp(latest_observations[rid]['recorded_at']):latest_observations[rid]=value
        except (ValueError,TypeError,KeyError):issue('observation_evidence_invalid','results')
    observed_exclusions={rid:v.get('reason',v.get('status')) for rid,v in latest_observations.items() if v.get('status') in ('excluded_tie','refund')}
    source_records=read_records(extra_path,issues) if extra_path else []
    source_records=[v for v in source_records if str(v.get('race_id','')).startswith(day) or (v.get('what')=='racelist' and v.get('kaisai_date')==day)]
    latest_sources={};untimed_errors=Counter()
    for value in source_records:
        if value.get('type')=='error' and not value.get('fetched_at'):
            untimed_errors[str(value.get('what','unknown'))]+=1
            continue
        try:
            at=stamp(value['fetched_at'])
            if at>now:continue
            key=(value.get('race_id',value.get('kaisai_date')),value.get('what') if value.get('type')=='error' else value.get('type'))
            if key not in latest_sources or at>=stamp(latest_sources[key]['fetched_at']):latest_sources[key]=value
        except (ValueError,KeyError,TypeError):issue('result_source_timestamp_invalid','result_source')
    error_counts=Counter(str(v.get('what','unknown')) for v in latest_sources.values() if v.get('type')=='error')
    error_counts.update(untimed_errors)
    successes=Counter(str(v.get('type')) for v in latest_sources.values() if v.get('type')!='error')
    success_latest_at={}
    for value in latest_sources.values():
        if value.get('type')=='error':continue
        source=str(value.get('type'));captured=value['fetched_at']
        if source not in success_latest_at or stamp(captured)>stamp(success_latest_at[source]):success_latest_at[source]=captured
    if extra_path and not Path(extra_path).exists():issue('result_source_file_missing','result_source')
    if error_counts.get('k') and not successes.get('k'):issue('result_source_all_error','result_source',source='k',races=error_counts['k'])
    if error_counts.get('racelist'):issue('result_source_schedule_failed','result_source',errors=error_counts['racelist'])
    unlabelled=actual-set(labels);unmatched=unlabelled-set(observed_exclusions)
    # K runs at 06:17 and NK at 06:47 JST (60-minute job budget), then a
    # reconciliation job runs. Midnight is still a normal wait for results.
    expected_by=(datetime.strptime(day,'%Y%m%d')+timedelta(days=1,hours=8)).replace(tzinfo=JST)
    label_due=now>=expected_by
    if unmatched and label_due:issue('result_labels_missing','results',races=len(unmatched))
    summaries=sorted(out.glob(f'logger_summary_{day}*.json'))
    run=None
    for summary_path in summaries:
        try:
            raw_summary=json.loads(summary_path.read_text(encoding='utf-8'))
            allowed={'schema_version','run_id','run_attempt','start','end','races_known','odds','lines','entries','miss','error'}
            value={k:v for k,v in raw_summary.items() if k in allowed}
            if run is None or stamp(value['end'])>stamp(run['end']):run=value
        except (OSError,ValueError,KeyError):issue('run_summary_unreadable','capture')
    if run:
        if run.get('error',0)>0 and run.get('odds',0)==0:issue('capture_run_all_error','capture',errors=run['error'])
        if run.get('races_known')==0 and completed:issue('known_schedule_lost_on_restart','capture',races=len(completed))
    for filename,code,component,field in [(f'forward_preparation_{day}.json','cohort_preparation_failed','inference','failures'),('research_upload.json','canonical_publication_failed','transport','errors')]:
        evidence=out/filename
        if evidence.exists():
            try:
                value=json.loads(evidence.read_text(encoding='utf-8'))
                if value.get(field):issue(code,component,count=len(value[field]))
            except (OSError,ValueError,TypeError):issue('operational_receipt_unreadable','evidence')
    runtime=out/f'forward_runtime_{day}.jsonl'
    runtime_errors=read_records(runtime if runtime.exists() else str(runtime)+'.gz',issues)
    if runtime_errors:issue('inference_runtime_failed','inference',errors=len(runtime_errors))
    return {'schema_version':SCHEMA,'day':day,'observed_at':now.isoformat(),'status':'degraded' if issues else 'healthy',
        'schedule':{'known':known,'scheduled':len(schedule) if known else None,'scheduled_latest':latest_count,'completed_cutoffs':len(completed),'eligible':len(eligible),'excluded':len(excluded),'excluded_reasons':excluded,'eligibility_unverified':len(unverified),'pending_cutoffs':len(schedule)-len(completed)},
        'capture':{'record_types':dict(Counter(v.get('type','unknown') for v in records)),'run_summary':run},
        'cohorts':cohorts,
        'results':{'prediction_races':len(actual),'labelled_races':len(actual&set(labels)),'unlabelled_races':len(unlabelled),'excluded_result_races':len(observed_exclusions),'excluded_reasons':observed_exclusions,'unmatched_races':len(unmatched),'unmatched_race_ids':sorted(unmatched),'source_errors':dict(error_counts),'source_successes':dict(successes),'source_success_latest_at':success_latest_at,'source_errors_time_unknown':dict(untimed_errors),'due':label_due,'expected_by':expected_by.isoformat()},
        'events':{'record_types':dict(Counter(v.get('type','unknown') for v in events)),'legacy_miss_races':len(misses),'contradicted_miss_races':len(contradicted),'prediction_event_missing_races':len(event_missing)},
        'issues':issues,'performance_reported':False,'evaluation_denominator_changed':False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',default='data');parser.add_argument('--day',required=True);parser.add_argument('--bundles',default='research-runtime/active_bundles.json');parser.add_argument('--extra');parser.add_argument('--output');parser.add_argument('--gate',action='store_true');parser.add_argument('--failure',action='append',default=[])
    args=parser.parse_args()
    try:bundles=json.loads(Path(args.bundles).read_text(encoding='utf-8'))
    except (OSError,ValueError):bundles={}
    report=audit_day(args.data_dir,args.day,bundles,extra_path=args.extra,failures=args.failure)
    path=Path(args.output or Path(args.data_dir)/f'forward_health_{args.day}.json');path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'day':args.day,'status':report['status'],'issues':[v['code'] for v in report['issues']],'report':str(path)},ensure_ascii=False))
    return int(args.gate and report['status']!='healthy')

if __name__=='__main__':raise SystemExit(main())
