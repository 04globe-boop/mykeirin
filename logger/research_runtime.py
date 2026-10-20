"""Run independently frozen bundles without sharing their Python module cache."""
import hashlib,importlib,importlib.util,json,sys
from pathlib import Path


def bundle_module(bundle,module):
    root=Path(bundle).resolve();source=root/'source'/'keirin_lab'
    alias='_frozen_keirin_'+hashlib.sha256(str(root).encode()).hexdigest()[:24]
    if alias not in sys.modules:
        spec=importlib.util.spec_from_file_location(alias,source/'__init__.py',submodule_search_locations=[str(source)])
        package=importlib.util.module_from_spec(spec);sys.modules[alias]=package
        try:spec.loader.exec_module(package)
        except BaseException:
            sys.modules.pop(alias,None);raise
    return importlib.import_module(alias+'.'+module)


class ResearchGroup:
    def __init__(self,bundles,out_dir,log=print):
        self.workers=[];self.log=log;self.out=Path(out_dir);self.out.mkdir(parents=True,exist_ok=True)
        for bundle in bundles:
            try:
                worker=bundle_module(bundle,'live_worker').LiveWorker(bundle,out_dir,log=log)
                self.workers.append(worker)
                log(f'research bundle loaded: {Path(bundle).parent.name}; {len(worker.models)} independent models; frozen_at={worker.frozen_at}; research only')
            except Exception as error:
                self.failure(bundle,error)

    def failure(self,bundle,error):
        from datetime import datetime,timezone
        value={'at':datetime.now(timezone.utc).isoformat(),'bundle':str(bundle),'error':type(error).__name__+': '+str(error)[:200]}
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y%m%d')
        with (self.out/f'forward_runtime_{day}.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(value)+'\n')
        self.log('research bundle unavailable; capture continues: '+json.dumps(value))

    def tick(self,races,now,end):
        upcoming=[];races=list(races)
        for worker in list(self.workers):
            try:upcoming.extend(worker.tick(races,now,end))
            except Exception as error:
                self.failure(worker.root,error);worker.close();self.workers.remove(worker)
        return upcoming

    def close(self):
        for worker in self.workers:worker.close()


def settle_bundles(bundles,out_dir,extra,day):
    reports={};out=Path(out_dir)
    for name,bundle in bundles.items():
        try:
            report=bundle_module(bundle,'settlement').settle_day(bundle,out,extra,day)
            reports[name]=report
            (out/f'forward_report_{name}_{day}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as error:
            reports[name]={'error':type(error).__name__+': '+str(error)[:300]}
    result={'date':day,'cohorts':reports,'research_only':True,'adoption':'inconclusive'}
    (out/f'forward_report_{day}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if all('error' in value for value in reports.values()):raise RuntimeError('All cohort settlements failed')
    return result
