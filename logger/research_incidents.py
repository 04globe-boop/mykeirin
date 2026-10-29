"""Score-free incident lifecycle, run in the private data repository only."""
import argparse
from datetime import datetime,timezone
import json,os,re
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

JST=ZoneInfo('Asia/Tokyo')
MARKER=re.compile(r'<!-- keirin-health-v1 (\{[^\n]+\}) -->')
BOT='github-actions[bot]'

def stamp(value):
    at=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('Timezone required')
    return at

def metadata(item):
    if item.get('user',{}).get('login')!=BOT:return None
    match=MARKER.search(item.get('body') or '')
    if not match:return None
    try:
        data=json.loads(match.group(1));stamp(data['observed_at'])
        if data.get('version')!=1 or not isinstance(data.get('key'),str):return None
        return data
    except (ValueError,KeyError,TypeError):return None

def incident_key(day,issue):
    code=issue.get('code','');component=issue.get('component','')
    if not re.fullmatch(r'[a-z_]{1,80}',str(code)) or not re.fullmatch(r'[a-z_]{1,40}',str(component)):
        raise ValueError('Invalid incident code/component')
    if code=='result_source_all_error':return 'source/k'
    if code in ('capture_run_all_error','known_schedule_lost_on_restart','schedule_unknown','unexpected_empty_schedule'):return 'source/netkeirin'
    return 'coverage/'+day+'/'+component

def healthy_for(key,report,*,since=None):
    def covers_incident(captured):
        if since is None or captured is None:return False
        try:return stamp(since)<=stamp(captured)<=stamp(report['observed_at'])
        except (ValueError,TypeError,KeyError):return False
    results=report.get('results',{})
    if key=='source/k':
        times=results.get('source_success_latest_at') or {}
        return (isinstance(times,dict) and results.get('source_successes',{}).get('k',0)>0
                and not results.get('source_errors',{}).get('k',0) and covers_incident(times.get('k')))
    if key=='source/netkeirin':
        run=report.get('capture',{}).get('run_summary') or {}
        return (report.get('schedule',{}).get('known') is True and run.get('odds',0)>0
                and run.get('error')==0 and covers_incident(run.get('end')))
    # A healthy later day is not evidence that an old gap has been repaired.
    return key.split('/')[1]==report['day'] and report.get('schedule',{}).get('known') is True and not any(v.get('component') in ('evidence','schedule') for v in report.get('issues',[]))

def body_for(state,meta,report,run_url):
    labels={'open':'発生','continuing':'継続','resolved':'復旧'}
    lines=[labels[state]+'：競輪収集・照合の運用監査', '', '対象日: '+report['day'],
           '検知項目: '+(', '.join(meta['codes']) or '該当項目の復旧を確認'),
           '記録時刻: '+meta['observed_at']]
    schedule=report.get('schedule',{});results=report.get('results',{})
    for title,source,names in [('日程',schedule,('scheduled','eligible')),('結果',results,('prediction_races','labelled_races','excluded_result_races','unmatched_races'))]:
        values=[name+'='+str(source[name]) for name in names if type(source.get(name)) is int]
        if values:lines.append(title+': '+', '.join(values))
    if run_url:
        if not re.fullmatch(r'https://github\.com/04globe-boop/mykeirin/actions/runs/[0-9]+',run_url):raise ValueError('Invalid source run URL')
        lines.append('[収集実行]('+run_url+')')
    lines.extend(['','予測値・正解・評価スコアは掲載していません。', '<!-- keirin-health-v1 '+json.dumps(meta,sort_keys=True,separators=(',',':'))+' -->'])
    return '\n'.join(lines)

def sync_report(report,client,*,now=None,run_url=None):
    if report.get('schema_version')!='operational-health-v1' or not re.fullmatch(r'\d{8}',str(report.get('day',''))):raise ValueError('Invalid operational report')
    now=now or datetime.now(timezone.utc);observed=stamp(report['observed_at'])
    if observed>now:raise ValueError('Future operational report')
    today=now.astimezone(JST).strftime('%Y%m%d');active={}
    for issue in report.get('issues',[]):
        key=incident_key(report['day'],issue);active.setdefault(key,set()).add(issue['code'])
    existing={};silenced=set()
    for issue in client.list_issues():
        if issue.get('pull_request'):continue
        info=metadata(issue)
        if not info:continue
        latest=info
        for comment in client.list_comments(issue['number']):
            value=metadata(comment)
            if value and value['key']==info['key'] and stamp(value['observed_at'])>=stamp(latest['observed_at']):latest=value
        if issue.get('state')=='closed':
            if latest.get('state')!='resolved':silenced.add(info['key'])
            continue
        existing.setdefault(info['key'],(issue,latest))
    actions=[]
    for key,codes in sorted(active.items()):
        if key in silenced and key not in existing:continue
        codes=sorted(codes);previous=existing.get(key)
        meta={'version':1,'key':key,'codes':codes,'state':'open','day':report['day'],'reported_on':today,'observed_at':observed.isoformat()}
        if not previous:
            created=client.create_issue('[運用監査] '+key,body_for('open',meta,report,run_url));actions.append({'action':'created','number':created['number'],'key':key})
        else:
            issue,old=previous
            if observed<stamp(old['observed_at']):continue
            if codes!=old['codes'] or today!=old['reported_on']:
                meta['state']='continuing';client.comment(issue['number'],body_for('continuing',meta,report,run_url));actions.append({'action':'continued','number':issue['number'],'key':key})
            elif observed>stamp(old['observed_at']):
                # Advance the observation watermark quietly, without a comment.
                client.update_issue(issue['number'],body_for('open',meta,report,run_url))
    for key,(issue,old) in existing.items():
        if key in active or observed<stamp(old['observed_at']) or not healthy_for(key,report,since=old['observed_at']):continue
        meta=dict(old,state='resolved',codes=[],reported_on=today,observed_at=observed.isoformat())
        if old.get('state')!='resolved':client.comment(issue['number'],body_for('resolved',meta,report,run_url))
        client.close_issue(issue['number']);actions.append({'action':'resolved','number':issue['number'],'key':key})
    return actions

class GitHubIssues:
    def __init__(self,repo,token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('Invalid repository')
        if not token:raise ValueError('GITHUB_TOKEN required')
        self.repo=repo;self.token=token
        if self.request('GET','').get('private') is not True:raise ValueError('Incidents must be stored in a private repository')
    def request(self,method,path,data=None):
        request=Request('https://api.github.com/repos/'+self.repo+path,data=None if data is None else json.dumps(data).encode(),method=method,
            headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'keirin-operational-health'})
        with urlopen(request,timeout=30) as response:
            raw=response.read(8_000_001)
            if len(raw)>8_000_000:raise ValueError('GitHub response too large')
            return json.loads(raw) if raw else None
    def pages(self,path):
        values=[]
        for page in range(1,101):
            items=self.request('GET',path+('&' if '?' in path else '?')+urlencode({'per_page':100,'page':page}));values.extend(items)
            if len(items)<100:return values
        raise RuntimeError('GitHub pagination limit; refusing incomplete incident inventory')
    def list_issues(self):return self.pages('/issues?state=all')
    def list_comments(self,number):return self.pages('/issues/'+str(int(number))+'/comments')
    def create_issue(self,title,body):return self.request('POST','/issues',{'title':title,'body':body})
    def comment(self,number,body):return self.request('POST','/issues/'+str(int(number))+'/comments',{'body':body})
    def update_issue(self,number,body):return self.request('PATCH','/issues/'+str(int(number)),{'body':body})
    def close_issue(self,number):return self.request('PATCH','/issues/'+str(int(number)),{'state':'closed','state_reason':'completed'})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--report',required=True);parser.add_argument('--repo',required=True);parser.add_argument('--run-url')
    args=parser.parse_args()
    with open(args.report,encoding='utf-8') as stream:report=json.load(stream)
    actions=sync_report(report,GitHubIssues(args.repo,os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')),run_url=args.run_url)
    print(json.dumps({'incident_actions':actions},ensure_ascii=False))

if __name__=='__main__':main()
