"""Private health handoff, with a fake GitHub service and no external writes."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'logger'))
try:
    from research_health_publish import GitHubHealthStore, publish_health, receive_health_event
except ModuleNotFoundError as error:
    if error.name != 'research_health_publish':
        raise

    def publish_health(*args, **kwargs):
        raise AssertionError('research_health_publish.publish_health is not implemented')

    def receive_health_event(*args, **kwargs):
        raise AssertionError('research_health_publish.receive_health_event is not implemented')


NOW = datetime.fromisoformat('2026-10-23T12:00:00+00:00')
REPO = '04globe-boop/mykeirin2'
SOURCE = '04globe-boop/mykeirin'
RUN_URL = 'https://github.com/04globe-boop/mykeirin/actions/runs/12345'


def report(status='degraded', observed='2026-10-23T11:00:00+00:00', day='20261023'):
    return {'schema_version': 'operational-health-v1', 'day': day, 'observed_at': observed,
            'status': status, 'schedule': {'known': True, 'scheduled': 10, 'eligible': 8},
            'results': {'prediction_races': 8, 'labelled_races': 0, 'unmatched_races': 8,
                        'source_errors': {'k': 10}, 'source_successes': {'k': 0}},
            'issues': [{'code': 'result_source_all_error', 'component': 'result_source',
                        'severity': 'error', 'source': 'k', 'races': 10}] if status == 'degraded' else [],
            'performance_reported': False, 'evaluation_denominator_changed': False}


class GitHubStore:
    """Observable service state; failures model the external protocol boundary."""
    def __init__(self):
        self.repository = {'private': True, 'full_name': REPO}
        self.assets = {}
        self.asset_releases = {}
        self.release_days = []
        self.last_receipt = None
        self.receipts = {}
        self.dispatches = []
        self.events = []
        self.fail_on = None

    def check(self, event):
        self.events.append(event)
        if self.fail_on == event:
            raise OSError('simulated GitHub failure')

    def get_repository(self):
        self.check('repository')
        return self.repository

    def get_release(self, *, day=None, create=False):
        self.check('release')
        self.release_days.append(day)
        if day is None:
            return {'id': 7, 'tag_name': 'operational-health'}
        return {'id': int(day), 'tag_name': f'operational-health-{day[:4]}-{day[4:6]}-{day[6:]}'}

    def upload_asset(self, release, name, content):
        receipt = name.startswith('notification-receipt-')
        self.check('receipt_upload' if receipt else 'diagnostic_upload')
        if name in self.assets and self.assets[name] != content:
            raise ValueError('immutable asset collision')
        self.assets[name] = bytes(content)
        self.asset_releases[name] = release['id']
        if receipt:
            self.last_receipt = json.loads(content)
            self.receipts[self.last_receipt['day']] = self.last_receipt
        return {'name': name, 'size': len(content), 'id': len(self.assets)}

    def latest_receipt(self, release, *, day):
        self.check('state_read')
        if self.last_receipt and 'day' not in self.last_receipt:
            return self.last_receipt
        return self.receipts.get(day)

    def dispatch(self, payload):
        self.check('dispatch')
        self.dispatches.append(copy.deepcopy(payload))

    def get_asset(self, release, name):
        self.check('asset_read')
        return self.assets.get(name) if self.asset_releases.get(name) == release['id'] else None


class PrivateHealthHandoff(unittest.TestCase):
    def publish(self, client, value=None, run_id='12345', attempt='1', now=NOW):
        return publish_health(value or report(), client, repo=REPO, run_id=run_id, run_attempt=attempt,
                              run_url=f'https://github.com/{SOURCE}/actions/runs/{run_id}', clock=lambda: now)

    def test_diagnostic_precedes_dispatch_and_receipt_after_success(self):
        client = GitHubStore()
        result = self.publish(client)
        self.assertTrue(result['dispatched'])
        self.assertIn(result['asset_name'], client.assets)
        self.assertEqual(client.events, ['repository', 'release', 'diagnostic_upload', 'state_read', 'dispatch', 'receipt_upload'])
        self.assertEqual(client.dispatches[0], {'event_type': 'keirin-health', 'client_payload': {
            'asset_name': result['asset_name'], 'day': '20261023', 'run_id': '12345',
            'attempt': '1', 'source_run_url': RUN_URL}})
        saved = json.loads(client.assets[result['asset_name']])
        self.assertEqual(saved['schema_version'], 'operational-health-v1')
        self.assertEqual(saved['source']['repository'], SOURCE)
        self.assertIsNotNone(client.last_receipt)

    def test_repeated_signature_saves_diagnostic_without_dispatching_again(self):
        client = GitHubStore()
        self.publish(client)
        changed = report()
        changed['issues'][0]['races'] = 99
        changed['results']['labelled_races'] = 3
        result = self.publish(client, changed, run_id='12346')
        self.assertFalse(result['dispatched'])
        self.assertIn(result['asset_name'], client.assets)
        self.assertEqual(len(client.dispatches), 1)
        self.assertEqual(len([name for name in client.assets if name.startswith('health-')]), 2)

    def test_first_healthy_run_saves_diagnostic_but_needs_no_notification_state(self):
        client = GitHubStore()
        result = self.publish(client, report('healthy'))
        self.assertFalse(result['dispatched'])
        self.assertEqual(client.dispatches, [])
        self.assertIsNone(client.last_receipt)
        self.assertIn(result['asset_name'], client.assets)

    def test_recovery_dispatches_once_then_stays_quiet(self):
        client = GitHubStore()
        self.publish(client)
        self.assertTrue(self.publish(client, report('healthy'), run_id='12346')['dispatched'])
        self.assertFalse(self.publish(client, report('healthy'), run_id='12347')['dispatched'])
        self.assertEqual(len(client.dispatches), 2)

    def test_continuation_on_next_jst_day_dispatches_for_the_same_historical_day(self):
        client = GitHubStore()
        self.publish(client)
        tomorrow = datetime.fromisoformat('2026-10-23T15:10:00+00:00')
        continued = report(observed='2026-10-23T15:05:00+00:00')
        result = self.publish(client, continued, run_id='12346', now=tomorrow)
        self.assertTrue(result['dispatched'])
        self.assertEqual(len(client.dispatches), 2)

    def test_changed_incident_codes_dispatch_without_using_count_noise(self):
        client = GitHubStore()
        self.publish(client)
        changed = report()
        changed['issues'].append({'code': 'schedule_unknown', 'component': 'schedule', 'severity': 'error'})
        self.assertTrue(self.publish(client, changed, run_id='12346')['dispatched'])

    def test_alternating_target_days_keep_independent_notification_receipts(self):
        client = GitHubStore()
        self.publish(client)
        self.assertTrue(self.publish(client, report(day='20261022'), run_id='12346')['dispatched'])
        self.assertFalse(self.publish(client, report(day='20261023'), run_id='12347')['dispatched'])
        self.assertFalse(self.publish(client, report(day='20261022'), run_id='12348')['dispatched'])
        self.assertEqual(len(client.dispatches), 2)

    def test_diagnostics_and_receipts_live_in_their_validated_daily_releases(self):
        client = GitHubStore()
        first = self.publish(client)
        second = self.publish(client, report(day='20261022'), run_id='12346')
        self.assertEqual(client.release_days, ['20261023', '20261022'])
        self.assertNotEqual(client.asset_releases[first['asset_name']], client.asset_releases[second['asset_name']])
        for payload in client.dispatches:
            event = {'action': 'keirin-health', 'client_payload': payload['client_payload']}
            received = receive_health_event(event, 'repository_dispatch', client, repo=REPO, clock=lambda: NOW)
            self.assertEqual(received['report']['day'], event['client_payload']['day'])
        self.assertEqual(client.release_days, ['20261023', '20261022', '20261023', '20261022'])
        for name, raw in client.assets.items():
            if name.startswith('notification-receipt-'):
                target = json.loads(raw)['day']
                self.assertEqual(client.asset_releases[name], int(target))

    def test_public_or_wrong_target_repository_rejects_all_writes(self):
        for repository in ({'private': False, 'full_name': REPO}, {'private': True, 'full_name': SOURCE}):
            with self.subTest(repository=repository):
                client = GitHubStore();client.repository = repository
                with self.assertRaises(ValueError):
                    self.publish(client)
                self.assertEqual(client.assets, {})
                self.assertEqual(client.dispatches, [])

    def test_malformed_repository_response_is_a_validation_failure(self):
        client = GitHubStore();client.repository = []
        with self.assertRaises(ValueError):
            self.publish(client)
        self.assertEqual(client.assets, {})

    def test_report_run_metadata_and_future_observations_are_validated(self):
        for field, value in (('schema_version', 'unknown'), ('day', '20260230'), ('status', 'success'),
                             ('observed_at', '2026-10-23T13:00:00'), ('observed_at', '2026-10-24T00:00:00+00:00'),
                             ('performance_reported', True)):
            with self.subTest(field=field, value=value):
                client = GitHubStore();bad = report();bad[field] = value
                with self.assertRaises(ValueError):
                    self.publish(client, bad)
                self.assertEqual(client.assets, {})
        for run_id in ('0', '../secret', '123;echo token'):
            with self.subTest(run_id=run_id):
                with self.assertRaises(ValueError):
                    self.publish(GitHubStore(), run_id=run_id)

    def test_only_whitelisted_counts_and_reason_codes_reach_assets(self):
        value = report()
        value['summaries'] = {'gnn': {'nll': 999.999, 'truth': ['PRIVATE RUNNER']}}
        value['results']['secret'] = 'ghp_sensitive'
        value['results']['unmatched_race_ids'] = ['202610231201']
        value['issues'][0]['reason'] = 'PRIVATE RUNNER ghp_sensitive'
        value['schedule']['excluded_reasons'] = {'202610231201': 'player details'}
        client = GitHubStore();result = self.publish(client, value)
        raw = client.assets[result['asset_name']].decode()
        for disallowed in ('999.999', 'PRIVATE RUNNER', 'ghp_sensitive', '202610231201', 'summaries', 'unmatched_race_ids'):
            self.assertNotIn(disallowed, raw)
        self.assertEqual(json.loads(raw)['issues'][0]['code'], 'result_source_all_error')

    def test_actual_op_racelist_and_unknown_source_time_counts_survive_the_whitelist(self):
        value = report()
        value['results']['source_errors'] = {'k': 10, 'op': 3, 'racelist': 2}
        value['results']['source_successes'] = {'k': 0, 'op': 7, 'racelist': 1}
        value['results']['source_errors_time_unknown'] = {'k': 8, 'op': 1, 'racelist': 1}
        client = GitHubStore();result = self.publish(client, value)
        saved = json.loads(client.assets[result['asset_name']])['results']
        self.assertEqual(saved['source_errors'], {'k': 10, 'op': 3, 'racelist': 2})
        self.assertEqual(saved['source_successes'], {'k': 0, 'op': 7, 'racelist': 1})
        self.assertEqual(saved.get('source_errors_time_unknown'), {'k': 8, 'op': 1, 'racelist': 1})

    def test_actual_op_and_racelist_issue_source_names_are_accepted(self):
        for source in ('op', 'racelist'):
            with self.subTest(source=source):
                value = report();value['issues'][0]['source'] = source
                client = GitHubStore();result = self.publish(client, value)
                saved = json.loads(client.assets[result['asset_name']])
                self.assertEqual(saved['issues'][0]['source'], source)

    def test_full_audit_keeps_cohort_candidate_counts_without_scientific_or_identity_fields(self):
        value = report()
        value['cohorts'] = {'v1': {'expected_races': 8, 'excluded_before_freeze': 2,
                                 'predicted_races': 7, 'labelled_races': 6, 'missing_races': 1,
                                 'partial_races': 1, 'candidate_ids': {'gbdt': 'PRIVATE_HASH'},
                                 'candidates': {'gbdt': {'predicted': 7, 'primary': 6, 'labelled': 5,
                                                         'primary_labelled': 4, 'missing_races': 2,
                                                         'missing_race_ids': ['202610231201'],
                                                         'nll': 123.456, 'p': [0.9], 'truth': ['PRIVATE RUNNER']},
                                                'gbdt+gnn': {'predicted': 6, 'primary': 5, 'labelled': 4,
                                                             'primary_labelled': 3, 'missing_races': 3},
                                                'PRIVATE_NAME': {'predicted': 5}}}}
        client = GitHubStore();result = self.publish(client, value)
        saved = json.loads(client.assets[result['asset_name']])['cohorts']['v1']
        self.assertEqual(saved.get('expected_races'), 8)
        self.assertEqual(saved.get('excluded_before_freeze'), 2)
        self.assertEqual(saved.get('candidates'), {
            'gbdt': {'predicted': 7, 'primary': 6, 'labelled': 5, 'primary_labelled': 4, 'missing_races': 2},
            'gbdt+gnn': {'predicted': 6, 'primary': 5, 'labelled': 4, 'primary_labelled': 3, 'missing_races': 3}})
        raw = client.assets[result['asset_name']].decode()
        for secret in ('PRIVATE_HASH', 'PRIVATE RUNNER', 'PRIVATE_NAME', '123.456', '202610231201', 'truth', 'candidate_ids'):
            self.assertNotIn(secret, raw)

    def test_api_failures_never_advance_notification_receipt_before_dispatch_success(self):
        for failure in ('repository', 'release', 'diagnostic_upload', 'state_read', 'dispatch'):
            with self.subTest(failure=failure):
                client = GitHubStore();client.fail_on = failure
                with self.assertRaises(OSError):
                    self.publish(client)
                self.assertIsNone(client.last_receipt)
                self.assertEqual(client.dispatches, [])

    def test_receipt_upload_failure_is_visible_and_retry_may_redispatch(self):
        client = GitHubStore();client.fail_on = 'receipt_upload'
        with self.assertRaises(OSError):
            self.publish(client)
        self.assertEqual(len(client.dispatches), 1)
        self.assertIsNone(client.last_receipt)
        client.fail_on = None
        self.assertTrue(self.publish(client)['dispatched'])

    def test_malformed_prior_state_fails_closed_after_preserving_diagnostic(self):
        client = GitHubStore();client.last_receipt = {'fingerprint': 'forged', 'status': 'degraded'}
        with self.assertRaises(ValueError):
            self.publish(client)
        self.assertTrue(client.assets)
        self.assertEqual(client.dispatches, [])

    def test_private_receiver_checks_dispatch_asset_hash_and_metadata(self):
        client = GitHubStore();self.publish(client)
        event = {'action': 'keirin-health', 'client_payload': client.dispatches[0]['client_payload']}
        result = receive_health_event(event, 'repository_dispatch', client, repo=REPO, clock=lambda: NOW)
        self.assertEqual(result['report']['day'], '20261023')
        self.assertEqual(result['source_run_url'], RUN_URL)
        self.assertNotIn('ref', result)
        asset = event['client_payload']['asset_name']
        client.assets[asset] += b' '
        with self.assertRaises(ValueError):
            receive_health_event(event, 'repository_dispatch', client, repo=REPO, clock=lambda: NOW)

    def test_receiver_ref_injection_wrong_run_and_extra_payload_are_rejected(self):
        client = GitHubStore();self.publish(client)
        good = {'action': 'keirin-health', 'client_payload': client.dispatches[0]['client_payload']}
        for key, value in (('ref', 'attacker/branch'), ('tag', 'attacker-release'), ('run_id', '54321'), ('day', '20261024'), ('source_run_url', 'https://example.com/')):
            with self.subTest(key=key):
                event = copy.deepcopy(good);event['client_payload'][key] = value
                with self.assertRaises(ValueError):
                    receive_health_event(event, 'repository_dispatch', client, repo=REPO, clock=lambda: NOW)

    def test_manual_receiver_derives_validated_source_run_from_the_asset_name(self):
        client = GitHubStore();result = self.publish(client)
        event = {'inputs': {'day': '20261023', 'asset': result['asset_name']}}
        received = receive_health_event(event, 'workflow_dispatch', client, repo=REPO, clock=lambda: NOW)
        self.assertEqual(received['source_run_url'], RUN_URL)
        self.assertEqual(received['report']['source']['run_id'], '12345')


    def test_k_recovery_dispatches_with_unchanged_degraded_issue_codes(self):
        client = GitHubStore();value = report()
        value['issues'] = [{'code': 'forecast_missing', 'component': 'inference', 'severity': 'error'}]
        value['results'].update(source_errors={}, source_successes={})
        self.publish(client, value)
        value['results']['source_successes'] = {'k': 67}
        value['results']['source_success_latest_at'] = {'k': '2026-10-23T10:59:00+00:00'}
        self.assertTrue(self.publish(client, value, run_id='12346')['dispatched'])
        value['results']['source_successes']['k'] = 68
        self.assertFalse(self.publish(client, value, run_id='12347')['dispatched'])
        self.assertEqual(len(client.dispatches), 2)

    def test_first_healthy_verified_k_recovery_dispatches_once_per_jst_day(self):
        client = GitHubStore();value = report('healthy')
        value['results'].update(source_errors={}, source_successes={'k': 67}, source_success_latest_at={'k': '2026-10-23T10:59:00+00:00'})
        self.assertTrue(self.publish(client, value)['dispatched'])
        value['results']['source_successes']['k'] = 68
        self.assertFalse(self.publish(client, value, run_id='12346')['dispatched'])
        value['observed_at'] = '2026-10-23T15:05:00+00:00'
        tomorrow = datetime.fromisoformat('2026-10-23T15:10:00+00:00')
        self.assertTrue(self.publish(client, value, run_id='12347', now=tomorrow)['dispatched'])
        self.assertEqual(len(client.dispatches), 2)

    def test_verified_netkeirin_proof_dispatches_without_count_noise(self):
        client = GitHubStore();value = report('healthy')
        value['capture'] = {'run_summary': {'odds': 1, 'error': 0, 'end': '2026-10-23T10:59:00+00:00'}}
        self.assertTrue(self.publish(client, value)['dispatched'])
        value['capture']['run_summary']['odds'] = 200
        self.assertFalse(self.publish(client, value, run_id='12346')['dispatched'])

    def test_first_healthy_partial_or_unknown_provider_proof_stays_quiet(self):
        value = report('healthy')
        value['results'].update(source_errors={'k': 1}, source_successes={'k': 67})
        value['capture'] = {'run_summary': {'odds': 1, 'error': 1}}
        self.assertFalse(self.publish(GitHubStore(), value)['dispatched'])
        value['capture']['run_summary']['error'] = 0
        value['schedule']['known'] = False
        self.assertFalse(self.publish(GitHubStore(), value)['dispatched'])


    def test_positive_counts_without_a_real_success_time_do_not_emit_positive_proof(self):
        value=report('healthy');value['results'].update(source_errors={},source_successes={'k':67})
        self.assertFalse(self.publish(GitHubStore(),value)['dispatched'])
        value['capture']={'run_summary':{'odds':1,'error':0}}
        self.assertFalse(self.publish(GitHubStore(),value)['dispatched'])

    def test_old_proof_then_fresh_actual_time_crosses_throttle_with_same_issue_codes(self):
        client=GitHubStore();value=report(day='20261021',observed='2026-10-29T04:00:00+00:00')
        value['issues']=[{'code':'forecast_missing','component':'inference','severity':'error'}]
        value['results'].update(source_errors={},source_successes={'k':67},source_success_latest_at={'k':'2026-10-23T04:00:00+00:00'})
        now=datetime.fromisoformat('2026-10-29T04:00:00+00:00')
        self.assertTrue(self.publish(client,value,now=now)['dispatched'])
        value['results']['source_success_latest_at']['k']='2026-10-29T03:30:00+00:00'
        self.assertTrue(self.publish(client,value,run_id='12346',now=now)['dispatched'])
        value['results']['source_successes']['k']=68
        self.assertFalse(self.publish(client,value,run_id='12347',now=now)['dispatched'])
        self.assertEqual(len(client.dispatches),2)
        saved=json.loads(client.assets[client.dispatches[-1]['client_payload']['asset_name']])
        self.assertEqual(saved['results'].get('source_success_latest_at'),{'k':'2026-10-29T03:30:00+00:00'})

    def test_invalid_and_future_actual_success_times_are_rejected_before_upload(self):
        for captured in (None,'bad-time','2026-10-23T10:59:00','2026-10-23T11:01:00+00:00'):
            with self.subTest(captured=captured):
                value=report('healthy');value['results'].update(source_errors={},source_successes={'k':67},source_success_latest_at={'k':captured})
                client=GitHubStore()
                with self.assertRaises(ValueError):self.publish(client,value)
                self.assertEqual(client.assets,{})
        value=report('healthy');value['capture']={'run_summary':{'odds':1,'error':0,'end':'2026-10-23T11:01:00+00:00'}}
        with self.assertRaises(ValueError):self.publish(GitHubStore(),value)


class Response:
    def __init__(self, content=b'', status=200):
        self.content, self.status = content, status

    def read(self, limit):
        return self.content[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class HTTPQueue:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class GitHubHealthProtocol(unittest.TestCase):
    def test_missing_daily_release_is_created_with_only_the_derived_tag(self):
        opener = HTTPQueue(HTTPError('https://api.github.com/test', 404, 'not found', {}, None),
                           Response(json.dumps({'id': 7, 'tag_name': 'operational-health-2026-10-23'}).encode(), 201))
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        self.assertEqual(client.get_release(day='20261023', create=True)['id'], 7)
        created = json.loads(opener.requests[-1].data)
        self.assertEqual(created['tag_name'], 'operational-health-2026-10-23')
        self.assertEqual(created['name'], 'operational-health-2026-10-23')

    def test_http_release_tags_are_derived_only_from_validated_target_days(self):
        values = [Response(json.dumps({'id': 7, 'tag_name': 'operational-health-2026-10-23'}).encode()),
                  Response(json.dumps({'id': 8, 'tag_name': 'operational-health-2026-10-22'}).encode())]
        opener = HTTPQueue(*values)
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        self.assertEqual(client.get_release(day='20261023')['id'], 7)
        self.assertEqual(client.get_release(day='20261022')['id'], 8)
        self.assertTrue(opener.requests[0].full_url.endswith('/releases/tags/operational-health-2026-10-23'))
        self.assertTrue(opener.requests[1].full_url.endswith('/releases/tags/operational-health-2026-10-22'))
        with self.assertRaises(ValueError):
            client.get_release(day='20260230')
        self.assertEqual(len(opener.requests), 2)

    def test_real_asset_inventory_selects_receipts_for_only_the_target_day(self):
        service = GitHubStore()
        for run, day in (('12345', '20261023'), ('12346', '20261022')):
            publish_health(report(day=day), service, repo=REPO, run_id=run, run_attempt='1',
                           run_url=f'https://github.com/{SOURCE}/actions/runs/{run}', clock=lambda: NOW)
        receipts = [(name, raw) for name, raw in service.assets.items() if name.startswith('notification-receipt-')]
        inventory = [{'name': name, 'id': index + 8, 'created_at': '2026-10-23T12:00:00Z'}
                     for index, (name, _) in enumerate(receipts)]
        by_id = {index + 8: raw for index, (_, raw) in enumerate(receipts)}
        def opener(request, **kwargs):
            if '/assets?' in request.full_url:
                return Response(json.dumps(inventory).encode())
            return Response(by_id[int(request.full_url.rsplit('/', 1)[1])])
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        self.assertEqual(client.latest_receipt({'id': 7, 'tag_name': 'operational-health-2026-10-23'}, day='20261023')['day'], '20261023')

    def test_dispatch_requires_the_documented_no_content_success_status(self):
        opener = HTTPQueue(Response(status=200))
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        with self.assertRaises(ValueError):
            client.dispatch({'event_type': 'keirin-health', 'client_payload': {}})

    def test_permission_failure_does_not_create_an_assumed_missing_release(self):
        opener = HTTPQueue(HTTPError('https://api.github.com/test', 403, 'forbidden', {}, None))
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        with self.assertRaises(HTTPError):
            client.get_release(day='20261023', create=True)
        self.assertEqual(len(opener.requests), 1)

    def test_asset_download_redirect_never_forwards_the_private_token(self):
        signed = 'https://release-assets.githubusercontent.com/asset?id=signed'
        opener = HTTPQueue(HTTPError('https://api.github.com/test', 302, 'redirect', {'Location': signed}, None), Response(b'private report'))
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        self.assertEqual(client.request('GET', '/releases/assets/7', raw=True), b'private report')
        self.assertEqual(opener.requests[0].get_header('Authorization'), 'Bearer private-test-token')
        self.assertIsNone(opener.requests[1].get_header('Authorization'))

    def test_untrusted_asset_redirect_is_rejected_before_an_external_request(self):
        opener = HTTPQueue(HTTPError('https://api.github.com/test', 302, 'redirect', {'Location': 'https://attacker.example/report'}, None))
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        with self.assertRaises(ValueError):
            client.request('GET', '/releases/assets/7', raw=True)
        self.assertEqual(len(opener.requests), 1)

    def test_upload_host_is_constructed_from_the_fixed_repo_not_api_content(self):
        name = 'health-20261023-run-12345-attempt-1-' + 'a' * 64 + '.json'
        opener = HTTPQueue(Response(b'[]'), Response(json.dumps({'id': 8, 'name': name, 'size': 2}).encode(), 201))
        client = GitHubHealthStore(REPO, 'private-test-token', opener=opener)
        client.upload_asset({'id': 7, 'tag_name': 'operational-health-2026-10-23', 'upload_url': 'https://attacker.example/'}, name, b'{}')
        self.assertTrue(opener.requests[-1].full_url.startswith('https://uploads.github.com/repos/' + REPO + '/releases/7/assets?'))
        self.assertEqual(opener.requests[-1].data, b'{}')


if __name__ == '__main__':
    unittest.main()
