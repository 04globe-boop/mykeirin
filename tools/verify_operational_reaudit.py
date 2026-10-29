"""One-shot operational audit; no canonical state or model writes."""
import hashlib,json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'logger'))
from research_assets import install_bundles,state_download,download
from research_health import audit_day
from research_health_publish import GitHubHealthStore,publish_health

REPO='04globe-boop/mykeirin2'
DAYS=('20261023','20261024','20261025','20261028')

def inventory(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}

def main():
    bundles=install_bundles(REPO,'research/bundle.json','reaudit-runtime')
    if set(bundles)!={'v1','v2'}:raise RuntimeError('Both pinned cohorts required')
    frozen_before=inventory(Path('reaudit-runtime'))
    extra=Path('reaudit-extra')
    if not download(REPO,'extra','extra_202610.jsonl.gz',extra):raise RuntimeError('Existing source evidence unavailable')
    extra_before=inventory(extra)
    outputs=Path('reaudit-reports');outputs.mkdir()
    proofs=[]
    for day in DAYS:
        data=Path('reaudit-copies')/day
        restored=state_download(REPO,data,day)
        if not restored['ok']:raise RuntimeError('Incomplete state restoration')
        for required in (f'forward_{day}.sqlite.gz',f'forward_inputs_{day}.jsonl.gz',f'odds_{day}.jsonl.gz'):
            if restored['assets'].get(required,{}).get('status')!='restored':raise RuntimeError('Required original missing')
        before=inventory(data)
        report=audit_day(data,day,bundles,extra_path=extra/'extra_202610.jsonl.gz' if day=='20261028' else None)
        (outputs/f'health-{day}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        publication=publish_health(report,GitHubHealthStore(REPO,os.environ['GH_TOKEN']),repo=REPO,
            run_id=os.environ['GITHUB_RUN_ID'],run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],
            run_url=f"https://github.com/04globe-boop/mykeirin/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        if inventory(data)!=before:raise RuntimeError('Read-only audit changed restored original')
        proof={'day':day,'status':report['status'],'issues':[v['code'] for v in report['issues']],
            'state_file_count':len(before),'original_files_unchanged':True,
            'cohorts':{name:{k:values[k] for k in ('predicted_races','labelled_races','missing_races','partial_races')} for name,values in report['cohorts'].items()},
            'results':{k:report['results'][k] for k in ('prediction_races','labelled_races','unmatched_races','excluded_result_races','source_errors','source_successes')},
            'publication':publication}
        proofs.append(proof);print(json.dumps(proof,ensure_ascii=False))
    if inventory(extra)!=extra_before:raise RuntimeError('Result source copy changed')
    if inventory(Path('reaudit-runtime'))!=frozen_before:raise RuntimeError('Frozen bundle changed')
    print(json.dumps({'schema':'ops-reaudit-proof-v1','days':len(proofs),'frozen_file_count':len(frozen_before),
                     'frozen_files_unchanged':True,'extra_source_files_unchanged':True,'original_files_unchanged':True,'performance_reported':False,
                     'evaluation_denominator_changed':False}))

if __name__=='__main__':main()
