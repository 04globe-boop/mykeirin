"""Private, score-free health evidence and a throttled Issue-workflow handoff.

Run publication under the collector's short shared writer lock. Diagnostics
and successful-notification receipts are immutable private Release assets.
No notification receipt is written before repository_dispatch succeeds.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


SOURCE_REPO = '04globe-boop/mykeirin'
DATA_REPO = '04globe-boop/mykeirin2'
TAG = 'operational-health'
SOURCE_NAMES = ('k', 'nk', 'nk_result', 'o', 'op', 'oddspark', 'racelist', 'netkeirin', 'unknown')
CANDIDATE_NAMES = ('gbdt', 'gbdt+gnn', 'gbdt+hierarchical', 'gbdt+set_transformer', 'gnn',
                   'hierarchical', 'market', 'orthodox', 'score_pl', 'set_transformer')
MAX_BYTES = 2_000_000
JST = ZoneInfo('Asia/Tokyo')
ASSET = re.compile(r'health-([0-9]{8})-run-([1-9][0-9]{0,19})-attempt-([1-9][0-9]{0,5})-([a-f0-9]{64})\.json')
RECEIPT = re.compile(r'notification-receipt-([0-9]{8})-on-([0-9]{8})-run-([1-9][0-9]{0,19})-attempt-([1-9][0-9]{0,5})-([a-f0-9]{64})\.json')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _stamp(value):
    _require(isinstance(value, str), 'timestamp_required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(result.tzinfo is not None and result.utcoffset() is not None, 'timezone_required')
    return result


def _day(value):
    _require(isinstance(value, str) and re.fullmatch(r'[0-9]{8}', value), 'invalid_day')
    datetime.strptime(value, '%Y%m%d')
    return value


def _release_tag(day):
    day = _day(day)
    return f'{TAG}-{day[:4]}-{day[4:6]}-{day[6:]}'


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _run_metadata(run_id, attempt, run_url):
    _require(isinstance(run_id, str) and re.fullmatch(r'[1-9][0-9]{0,19}', run_id), 'invalid_run_id')
    _require(isinstance(attempt, str) and re.fullmatch(r'[1-9][0-9]{0,5}', attempt), 'invalid_run_attempt')
    _require(run_url == f'https://github.com/{SOURCE_REPO}/actions/runs/{run_id}', 'invalid_source_run_url')
    return {'repository': SOURCE_REPO, 'run_id': run_id, 'run_attempt': attempt, 'run_url': run_url}


def _counts(value, names, *, nullable=()):
    _require(isinstance(value, dict), 'invalid_count_object')
    result = {}
    for name in names:
        if name not in value:
            continue
        count = value[name]
        _require((type(count) is int and 0 <= count <= 1_000_000_000)
                 or (name in nullable and count is None), 'invalid_count')
        result[name] = count
    return result


def _count_map(value, names):
    return _counts(value, names)


def _safe_report(report, *, run_id, attempt, run_url, now):
    _require(isinstance(report, dict) and report.get('schema_version') == 'operational-health-v1', 'invalid_report_schema')
    day = _day(report.get('day'))
    observed = _stamp(report.get('observed_at'))
    _require(observed <= now, 'future_operational_report')
    _require(report.get('performance_reported') is False and report.get('evaluation_denominator_changed') is False,
             'invalid_report_safety_flags')
    status = report.get('status')
    issues = report.get('issues')
    _require(status in ('healthy', 'degraded') and isinstance(issues, list) and len(issues) <= 2000, 'invalid_health_status')
    _require((status == 'healthy') == (not issues), 'health_status_issue_mismatch')
    clean_issues = []
    for issue in issues:
        _require(isinstance(issue, dict), 'invalid_incident')
        code, component = issue.get('code'), issue.get('component')
        _require(isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code), 'invalid_incident_code')
        _require(isinstance(component, str) and re.fullmatch(r'[a-z_]{1,40}', component), 'invalid_incident_component')
        clean = {'code': code, 'component': component}
        severity = issue.get('severity', 'error')
        _require(severity in ('error', 'warning'), 'invalid_incident_severity')
        clean['severity'] = severity
        if 'cohort' in issue:
            _require(issue['cohort'] in ('v1', 'v2'), 'invalid_incident_cohort')
            clean['cohort'] = issue['cohort']
        if 'source' in issue:
            _require(issue['source'] in SOURCE_NAMES, 'invalid_incident_source')
            clean['source'] = issue['source']
        clean.update(_counts(issue, ('races', 'errors', 'count')))
        clean_issues.append(clean)
    schedule = report.get('schedule', {})
    clean_schedule = _counts(schedule, ('scheduled', 'scheduled_latest', 'completed_cutoffs', 'eligible',
                            'excluded', 'eligibility_unverified', 'pending_cutoffs'), nullable=('scheduled', 'scheduled_latest'))
    if 'known' in schedule:
        _require(type(schedule['known']) is bool, 'invalid_schedule_status')
        clean_schedule['known'] = schedule['known']
    results = report.get('results', {})
    clean_results = _counts(results, ('prediction_races', 'labelled_races', 'unlabelled_races',
                           'excluded_result_races', 'unmatched_races'))
    for name in ('source_errors', 'source_successes', 'source_errors_time_unknown'):
        if name in results:
            clean_results[name] = _count_map(results[name], SOURCE_NAMES)
    if 'source_success_latest_at' in results:
        times = results['source_success_latest_at']
        _require(isinstance(times, dict), 'invalid_source_success_times')
        clean_times = {}
        for source in SOURCE_NAMES:
            if source in times:
                _require(_stamp(times[source]) <= observed, 'future_source_success_time')
                clean_times[source] = times[source]
        clean_results['source_success_latest_at'] = clean_times
    clean_cohorts = {}
    cohorts = report.get('cohorts', {})
    _require(isinstance(cohorts, dict), 'invalid_cohorts')
    for cohort in ('v1', 'v2'):
        if cohort in cohorts:
            value = cohorts[cohort]
            clean_cohorts[cohort] = _counts(value, ('expected_races', 'excluded_before_freeze',
                                            'predicted_races', 'labelled_races', 'missing_races', 'partial_races'))
            if 'candidates' in value:
                candidates = value['candidates']
                _require(isinstance(candidates, dict), 'invalid_candidate_counts')
                clean_cohorts[cohort]['candidates'] = {
                    name: _counts(candidates[name], ('predicted', 'primary', 'labelled', 'primary_labelled', 'missing_races'))
                    for name in CANDIDATE_NAMES if name in candidates}
    capture = report.get('capture', {})
    _require(isinstance(capture, dict), 'invalid_capture')
    tags = ('racelist', 'odds', 'lines', 'entries', 'error', 'miss', 'predicted', 'unavailable')
    clean_capture = {}
    if 'record_types' in capture:
        clean_capture['record_types'] = _count_map(capture['record_types'], tags)
    run = capture.get('run_summary')
    if run is not None:
        clean_run = _counts(run, ('races_known', 'odds', 'lines', 'entries', 'miss', 'error'))
        for field in ('start', 'end'):
            if field in run:
                _require(_stamp(run[field]) <= observed, 'future_capture_summary_time')
                clean_run[field] = run[field]
        clean_capture['run_summary'] = clean_run
    events = report.get('events', {})
    clean_events = _counts(events, ('legacy_miss_races', 'contradicted_miss_races', 'prediction_event_missing_races'))
    if 'record_types' in events:
        clean_events['record_types'] = _count_map(events['record_types'], tags)
    result = {'schema_version': 'operational-health-v1', 'day': day, 'observed_at': report['observed_at'],
              'status': status, 'issues': clean_issues, 'schedule': clean_schedule, 'results': clean_results,
              'cohorts': clean_cohorts, 'capture': clean_capture, 'events': clean_events,
              'performance_reported': False, 'evaluation_denominator_changed': False,
              'source': _run_metadata(run_id, attempt, run_url)}
    _require(len(_canonical(result)) <= MAX_BYTES, 'report_too_large')
    return result


def _signature(report):
    values = set()
    for issue in report['issues']:
        code, component = issue['code'], issue['component']
        if code == 'result_source_all_error':
            key = 'source/k'
        elif code in ('capture_run_all_error', 'known_schedule_lost_on_restart', 'schedule_unknown', 'unexpected_empty_schedule'):
            key = 'source/netkeirin'
        else:
            key = 'coverage/' + report['day'] + '/' + component
        values.add((key, code))
    # Provider recovery must reach private incident handling even while an
    # unrelated coverage issue keeps the overall report degraded.
    def add_verified(key, captured):
        if captured is not None:
            # Actual provider time, not the later audit time. A newer proof must
            # reach the receiver even if an older proof could not clear its watermark.
            at = _stamp(captured)
            if at <= _stamp(report['observed_at']):
                values.add((key, 'verified_positive_at_' + at.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S%f')))
    results = report.get('results', {})
    if results.get('source_successes', {}).get('k', 0) > 0 and results.get('source_errors', {}).get('k', 0) == 0:
        add_verified('source/k', results.get('source_success_latest_at', {}).get('k'))
    run = report.get('capture', {}).get('run_summary') or {}
    if report.get('schedule', {}).get('known') is True and run.get('odds', 0) > 0 and run.get('error') == 0:
        add_verified('source/netkeirin', run.get('end'))
    return [list(value) for value in sorted(values)]


def _fingerprint(day, notification_day, status, signature):
    return hashlib.sha256(_canonical({'day': day, 'notification_day': notification_day,
                                     'status': status, 'signature': signature})).hexdigest()


def _validate_receipt(value):
    _require(isinstance(value, dict) and value.get('schema_version') == 'operational-health-notification-v1', 'invalid_notification_receipt')
    _require(value.get('repository') == DATA_REPO and value.get('source_repository') == SOURCE_REPO, 'invalid_receipt_repository')
    day, notification_day = _day(value.get('day')), _day(value.get('notification_day'))
    _require(value.get('status') in ('healthy', 'degraded'), 'invalid_receipt_status')
    signature = value.get('signature')
    _require(isinstance(signature, list) and all(isinstance(v, list) and len(v) == 2
             and all(isinstance(part, str) and re.fullmatch(r'[a-z0-9_/-]{1,150}', part) for part in v)
             for v in signature), 'invalid_receipt_signature')
    _require(value.get('fingerprint') == _fingerprint(day, notification_day, value['status'], signature), 'invalid_receipt_fingerprint')
    _run_metadata(value.get('run_id'), value.get('attempt'), value.get('source_run_url'))
    _stamp(value.get('dispatched_at'))
    match = ASSET.fullmatch(value.get('asset_name', ''))
    _require(match and (match[1], match[2], match[3]) == (day, value['run_id'], value['attempt']), 'invalid_receipt_asset')
    return value


def _private_repository(client, repo):
    _require(repo == DATA_REPO, 'invalid_destination_repository')
    repository = client.get_repository()
    _require(isinstance(repository, dict) and repository.get('private') is True
             and repository.get('full_name') == DATA_REPO, 'private_repository_required')


def publish_health(report, client, *, repo, run_id, run_attempt, run_url, clock=None):
    """Preserve diagnostics every run; notify on a meaningful signature change.

    The caller serializes this entire operation under its short writer lock.
    A POST followed by receipt failure can be retried: the private incident
    lifecycle deduplicates notification retries.
    """
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    _stamp(now.isoformat())
    safe = _safe_report(report, run_id=run_id, attempt=run_attempt, run_url=run_url, now=now)
    _private_repository(client, repo)
    raw = _canonical(safe)
    digest = hashlib.sha256(raw).hexdigest()
    name = f'health-{safe["day"]}-run-{run_id}-attempt-{run_attempt}-{digest}.json'
    release = client.get_release(day=safe['day'], create=True)
    client.upload_asset(release, name, raw)
    prior = client.latest_receipt(release, day=safe['day'])
    if prior is not None:
        _validate_receipt(prior)
        _require(prior['day'] == safe['day'], 'notification_receipt_day_mismatch')
    notification_day = _stamp(safe['observed_at']).astimezone(JST).strftime('%Y%m%d')
    signature = _signature(safe)
    fingerprint = _fingerprint(safe['day'], notification_day, safe['status'], signature)
    def positive_proofs(items):
        return {tuple(value) for value in items if value[0] in ('source/k', 'source/netkeirin')
                and (value[1] == 'verified_positive' or value[1].startswith('verified_positive_at_'))}
    proofs = positive_proofs(signature)
    prior_proofs = positive_proofs(prior['signature']) if prior is not None else set()
    changed = prior is None or prior['fingerprint'] != fingerprint
    should_notify = bool(changed and (safe['status'] == 'degraded' or proofs
                         or (prior is not None and (prior['status'] == 'degraded' or prior_proofs != proofs))))
    result = {'schema_version': 'operational-health-publication-v1', 'asset_name': name,
              'repository': DATA_REPO, 'day': safe['day'], 'release_tag': _release_tag(safe['day']),
              'dispatched': False, 'fingerprint': fingerprint}
    if not should_notify:
        return result
    payload = {'asset_name': name, 'day': safe['day'], 'run_id': run_id,
               'attempt': run_attempt, 'source_run_url': run_url}
    client.dispatch({'event_type': 'keirin-health', 'client_payload': payload})
    state = {'schema_version': 'operational-health-notification-v1', 'repository': DATA_REPO,
             'source_repository': SOURCE_REPO, 'day': safe['day'], 'notification_day': notification_day,
             'status': safe['status'], 'signature': signature, 'fingerprint': fingerprint,
             'run_id': run_id, 'attempt': run_attempt, 'source_run_url': run_url,
             'asset_name': name, 'dispatched_at': now.isoformat()}
    state_name = f'notification-receipt-{safe["day"]}-on-{notification_day}-run-{run_id}-attempt-{run_attempt}-{fingerprint}.json'
    client.upload_asset(release, state_name, _canonical(state))
    result['dispatched'] = True
    return result


def receive_health_event(event, event_name, client, *, repo, clock=None):
    """Download only a validated fixed-repository health asset; never use a ref."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if event_name == 'repository_dispatch':
        _require(event.get('action') == 'keirin-health', 'invalid_dispatch_type')
        payload = event.get('client_payload')
        _require(isinstance(payload, dict) and set(payload) == {'asset_name', 'day', 'run_id', 'attempt', 'source_run_url'}, 'invalid_dispatch_payload')
    elif event_name == 'workflow_dispatch':
        inputs = event.get('inputs')
        _require(isinstance(inputs, dict) and set(inputs) == {'day', 'asset'}, 'invalid_manual_inputs')
        match = ASSET.fullmatch(inputs.get('asset', ''))
        _require(match is not None, 'invalid_health_asset_name')
        payload = {'asset_name': inputs['asset'], 'day': inputs['day'], 'run_id': match[2], 'attempt': match[3],
                   'source_run_url': f'https://github.com/{SOURCE_REPO}/actions/runs/{match[2]}'}
    else:
        raise ValueError('unsupported_health_event')
    day = _day(payload.get('day'))
    _run_metadata(payload.get('run_id'), payload.get('attempt'), payload.get('source_run_url'))
    match = ASSET.fullmatch(payload.get('asset_name', ''))
    _require(match and (match[1], match[2], match[3]) == (day, payload['run_id'], payload['attempt']), 'asset_metadata_mismatch')
    _private_repository(client, repo)
    release = client.get_release(day=day, create=False)
    raw = client.get_asset(release, payload['asset_name'])
    _require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, 'health_asset_missing_or_too_large')
    _require(hashlib.sha256(raw).hexdigest() == match[4], 'health_asset_hash_mismatch')
    value = json.loads(raw)
    clean = _safe_report(value, run_id=payload['run_id'], attempt=payload['attempt'], run_url=payload['source_run_url'], now=now)
    _require(clean == value and clean['day'] == day, 'health_asset_content_mismatch')
    return {'report': clean, 'source_run_url': payload['source_run_url'], 'asset_name': payload['asset_name'],
            'release_tag': _release_tag(day)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class GitHubHealthStore:
    """Contents-only GitHub client; it cannot create or edit Issues."""
    def __init__(self, repo, token, *, opener=None):
        _require(repo == DATA_REPO and isinstance(token, str) and bool(token), 'private_repository_token_required')
        self.repo, self.token = repo, token
        self.open = opener or build_opener(_NoRedirect()).open

    def request(self, method, path, data=None, *, raw=False, upload=False, expected_status=None):
        host = 'uploads.github.com' if upload else 'api.github.com'
        url = f'https://{host}/repos/{self.repo}' + path
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/octet-stream' if raw else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'keirin-private-operational-health'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        body = data if isinstance(data, bytes) else _canonical(data) if data is not None else None
        request = Request(url, data=body, method=method, headers=headers)
        try:
            response = self.open(request, timeout=30)
        except HTTPError as error:
            if not (raw and method == 'GET' and error.code in (301, 302, 303, 307, 308)):
                raise
            location = error.headers.get('Location', '')
            destination = urlsplit(location)
            _require(destination.scheme == 'https' and destination.netloc in
                     ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'), 'unsafe_asset_redirect')
            # Signed download URLs never receive the private repository token.
            response = self.open(Request(location, method='GET', headers={'User-Agent': 'keirin-private-operational-health'}), timeout=30)
        with response:
            _require(200 <= response.status < 300, 'github_http_failure')
            _require(expected_status is None or response.status == expected_status, 'github_http_status_unconfirmed')
            content = response.read(MAX_BYTES + 1)
        _require(len(content) <= MAX_BYTES, 'github_response_too_large')
        return content if raw else json.loads(content) if content else None

    def get_repository(self):
        return self.request('GET', '')

    def get_release(self, *, day, create=False):
        tag = _release_tag(day)
        try:
            release = self.request('GET', '/releases/tags/' + tag)
        except HTTPError as error:
            if not (create and error.code == 404):
                raise
            release = self.request('POST', '/releases', {'tag_name': tag, 'name': tag,
                                   'body': 'Score-free operational health evidence and notification receipts.', 'draft': False, 'prerelease': False})
        _require(isinstance(release, dict) and type(release.get('id')) is int and release['id'] > 0
                 and release.get('tag_name') == tag and release.get('draft', False) is False, 'invalid_health_release')
        return release

    def assets(self, release):
        result = []
        for page in range(1, 101):
            values = self.request('GET', f'/releases/{release["id"]}/assets?' + urlencode({'per_page': 100, 'page': page}))
            _require(isinstance(values, list), 'invalid_asset_inventory')
            for value in values:
                _require(isinstance(value, dict) and type(value.get('id')) is int and value['id'] > 0
                         and isinstance(value.get('name'), str), 'invalid_asset_metadata')
            result.extend(values)
            if len(values) < 100:
                return result
        raise ValueError('asset_inventory_limit')

    def get_asset(self, release, name):
        match = ASSET.fullmatch(name) or RECEIPT.fullmatch(name)
        _require(match is not None and release.get('tag_name') == _release_tag(match[1]), 'asset_release_day_mismatch')
        values = [value for value in self.assets(release) if value['name'] == name]
        _require(len(values) <= 1, 'duplicate_health_asset')
        return self.request('GET', '/releases/assets/' + str(values[0]['id']), raw=True) if values else None

    def upload_asset(self, release, name, content):
        _require((ASSET.fullmatch(name) or RECEIPT.fullmatch(name)) and isinstance(content, bytes) and len(content) <= MAX_BYTES,
                 'invalid_health_upload')
        old = self.get_asset(release, name)
        if old is not None:
            _require(old == content, 'immutable_health_asset_collision')
            return {'name': name, 'size': len(content)}
        result = self.request('POST', f'/releases/{release["id"]}/assets?' + urlencode({'name': name}), content, upload=True)
        _require(isinstance(result, dict) and result.get('name') == name and result.get('size') == len(content)
                 and type(result.get('id')) is int and result['id'] > 0, 'health_asset_upload_unconfirmed')
        return result

    def latest_receipt(self, release, *, day):
        _require(release.get('tag_name') == _release_tag(day), 'receipt_release_day_mismatch')
        values = [value for value in self.assets(release)
                  if (match := RECEIPT.fullmatch(value['name'])) and match[1] == day]
        if not values:
            return None
        chosen = max(values, key=lambda value: (_stamp(value['created_at']), value['id']))
        raw = self.request('GET', '/releases/assets/' + str(chosen['id']), raw=True)
        state = _validate_receipt(json.loads(raw))
        match = RECEIPT.fullmatch(chosen['name'])
        _require((match[1], match[2], match[3], match[4], match[5])
                 == (state['day'], state['notification_day'], state['run_id'], state['attempt'], state['fingerprint']),
                 'receipt_asset_content_mismatch')
        return state

    def dispatch(self, payload):
        _require(payload.get('event_type') == 'keirin-health', 'invalid_health_dispatch')
        result = self.request('POST', '/dispatches', payload, expected_status=204)
        _require(result is None, 'health_dispatch_unconfirmed')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    publish = sub.add_parser('publish')
    publish.add_argument('--report', required=True);publish.add_argument('--repo', required=True)
    publish.add_argument('--run-id', required=True);publish.add_argument('--attempt', required=True);publish.add_argument('--run-url', required=True)
    publish.add_argument('--output')
    receive = sub.add_parser('receive')
    receive.add_argument('--event', required=True);receive.add_argument('--event-name', required=True)
    receive.add_argument('--repo', required=True);receive.add_argument('--output', required=True);receive.add_argument('--metadata-output', required=True)
    args = parser.parse_args()
    try:
        client = GitHubHealthStore(args.repo, os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))
        if args.command == 'publish':
            value = json.loads(Path(args.report).read_text(encoding='utf-8'))
            result = publish_health(value, client, repo=args.repo, run_id=args.run_id, run_attempt=args.attempt, run_url=args.run_url)
            if args.output:
                Path(args.output).write_bytes(_canonical(result))
        else:
            value = json.loads(Path(args.event).read_text(encoding='utf-8'))
            result = receive_health_event(value, args.event_name, client, repo=args.repo)
            Path(args.output).write_bytes(_canonical(result.pop('report')))
            Path(args.metadata_output).write_bytes(_canonical(result))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError, HTTPError, URLError) as error:
        # URLs, token headers and raw API bodies never enter collector logs.
        print('Private operational health handoff failed: ' + type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
