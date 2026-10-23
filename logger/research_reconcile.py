"""Reconcile a scheduled NK run after its data producer, without altering it."""
import argparse,json
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPOSITORY='04globe-boop/mykeirin'

def reconcile_day(event,repository):
    run=event.get('workflow_run',{})
    if (repository!=REPOSITORY or run.get('head_repository',{}).get('full_name')!=REPOSITORY
        or run.get('head_branch')!='main' or run.get('name')!='nk-results'
        or run.get('event')!='schedule' or run.get('status')!='completed'
        or run.get('conclusion') not in ('success','failure','timed_out','cancelled')):return None
    started=datetime.fromisoformat(run['run_started_at'].replace('Z','+00:00'))
    if started.tzinfo is None:raise ValueError('Source start time must have timezone')
    return (started.astimezone(ZoneInfo('Asia/Tokyo')).date()-timedelta(days=1)).isoformat()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--event',required=True);parser.add_argument('--repo',required=True)
    args=parser.parse_args();day=reconcile_day(json.loads(Path(args.event).read_text(encoding='utf-8')),args.repo)
    print(day or '')

if __name__=='__main__':main()
