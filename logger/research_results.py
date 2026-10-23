"""Append verified post-close observations around independently frozen ledgers.

Source adapters never masquerade as another source. Admission verifies the
cards available at every actual issuance and the complete candidate universes.
All original inputs, predictions, labels and history events remain immutable.
"""
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import gzip
from itertools import permutations
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from result_adapter import adapt_nk_result


class _AdmissionError(ValueError):
    def __init__(self, reason, status='unavailable'):
        super().__init__(reason)
        self.status = status


def _require(condition, reason, status='unavailable'):
    if not condition:
        raise _AdmissionError(reason, status)


def _normalize(value):
    return ''.join(unicodedata.normalize('NFKC', str(value)).split())


def _read_gzip(path):
    if path is None or not Path(path).exists():
        return []
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _outcome(row):
    if row['rank'] is not None:
        return str(row['rank'])
    status = _normalize(row['status'])
    detail = row.get('outcome_detail', '')
    if status in ('落', '落車', '落車棄権') or (status in ('棄', '棄権') and '落車' in detail):
        return '落'
    if status in ('失', '失格'):
        return '失'
    if status in ('欠', '欠場', '欠車'):
        return '欠'
    return status  # Unknown retirement detail remains unknown; never infer a fall.


def _finish(row):
    value = _normalize(row.get('finish', ''))
    return 'マ' if value == 'ク' else value


def _adapt_k(record, modules):
    """Keep K provenance and its frozen parser; inspect ties without ranking them."""
    observation = {
        'schema_version': 'result-observation-v1', 'source_type': 'k',
        'race_id': record.get('race_id'), 'source_id': record.get('url'),
        'source_sha256': None, 'available_at': record.get('fetched_at'),
        'rows': [], 'truth_cars': None, 'payouts': [], 'status': 'invalid', 'reason': None,
        'refund_details_available': False, 'withdrawn_cars': [], 'requires_withdrawal_verification': False,
    }
    try:
        observation['source_sha256'] = modules['data'].digest(record)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        observation['reason'] = 'invalid_k_json_record'
        return observation
    try:
        rid = record.get('race_id')
        _require(isinstance(rid, str) and re.fullmatch(r'[0-9]{12}', rid), 'invalid_k_race_id', 'invalid')
        modules['data'].stamp(record['fetched_at'])
        url = urlsplit(record['url'])
        _require(url.scheme == 'https' and url.netloc == 'keirin.kdreams.jp'
                 and not url.query and not url.fragment, 'invalid_k_source_url', 'invalid')
        match = re.search(r'/racedetail/([0-9]{16})/$', url.path)
        _require(match is not None, 'invalid_k_source_url', 'invalid')
        kid = match[1]
        first = date(int(kid[2:6]), int(kid[6:8]), int(kid[8:10]))
        _require(1 <= int(kid[10:12]) <= 10 and kid[12:14] == '00', 'invalid_k_source_url', 'invalid')
        actual = first + timedelta(days=int(kid[10:12]) - 1)
        _require(actual.strftime('%Y%m%d') + kid[:2] + kid[14:16] == rid,
                 'k_source_url_race_mismatch', 'invalid')
        data = record['data']
        title = unicodedata.normalize('NFKC', data.get('page_title', ''))
        _require(f'{rid[:4]}年{rid[4:6]}月{rid[6:8]}日' in title
                 and re.search(r'(?<![0-9])' + str(int(rid[10:])) + r'R(?![0-9])', title),
                 'k_title_race_mismatch', 'invalid')
        table = data.get('result') or []
        if not table:
            refund = '返還' in json.dumps(data.get('refund', []), ensure_ascii=False)
            observation.update(status='refund' if refund else 'pending',
                               reason='whole_refund' if refund else 'result_pending')
            return observation
        header = [_normalize(value) for value in table[0]]
        _require(len(set(header)) == len(header) and all(k in header for k in ('着順', '車番', '選手名')),
                 'invalid_k_result_header', 'invalid')
        parsed = []
        for cells in table[1:]:
            _require(isinstance(cells, list) and len(cells) == len(header), 'invalid_k_result_width', 'invalid')
            values = dict(zip(header, cells))
            car, status = _normalize(values['車番']), _normalize(values['着順'])
            _require(re.fullmatch(r'[1-9]', car), 'invalid_k_result_car', 'invalid')
            rank = int(status) if re.fullmatch(r'[1-9]', status) else None
            _require(rank is not None or status in ('棄', '落', '失', '欠', '欠場', '棄権', '落車棄権', '失格'),
                     'invalid_k_runner_status', 'invalid')
            name = modules['legacy'].canonical_name(values['選手名'])
            _require(bool(name), 'invalid_k_runner_name', 'invalid')
            parsed.append({'car': car, 'name': name, 'graduate': None, 'rank': rank,
                           'status': status, 'finish': _normalize(values.get('決まり手', '')),
                           'outcome_detail': _normalize(values.get('着差', ''))})
        _require(3 <= len(parsed) <= 9 and len({v['car'] for v in parsed}) == len(parsed),
                 'invalid_k_result_roster', 'invalid')
        counts = Counter(v['rank'] for v in parsed if v['rank'] is not None)
        position = 1
        for rank, count in sorted(counts.items()):
            _require(rank == position, 'invalid_k_rank_sequence', 'invalid')
            position += count
        _require(sum(counts.values()) >= 3, 'incomplete_k_top_three', 'invalid')
        if any(counts[rank] > 1 for rank in (1, 2, 3)):
            observation.update(status='excluded_tie', reason='top_three_tie')
        else:
            # The independently frozen K contract remains authoritative for K.
            verified = modules['settlement'].result_rows(record)
            _require({(v['car'], v['name'], v['rank']) for v in verified}
                     == {(v['car'], v['name'], v['rank']) for v in parsed}, 'k_parser_disagreement', 'invalid')
            observation.update(status='ready', truth_cars=[next(v['car'] for v in parsed if v['rank'] == rank)
                                                          for rank in (1, 2, 3)])
        observation['rows'] = parsed
        observation['withdrawn_cars'] = [v['car'] for v in parsed if _outcome(v) == '欠']
        observation['requires_withdrawal_verification'] = bool(observation['withdrawn_cars'])
        return observation
    except _AdmissionError as error:
        observation.update(status=error.status, reason=str(error))
    except (ValueError, TypeError, KeyError, IndexError):
        observation.update(status='invalid', reason='invalid_k_result_observation')
    return observation


def _same_result(first, second):
    if first['status'] != second['status']:
        return False
    a = {row['car']: row for row in first['rows']}
    b = {row['car']: row for row in second['rows']}
    if set(a) != set(b):
        return False
    for car in a:
        x, y = a[car], b[car]
        if (x['name'], x['rank'], _outcome(x), _finish(x)) != (y['name'], y['rank'], _outcome(y), _finish(y)):
            return False
        if x.get('graduate') is not None and y.get('graduate') is not None and x['graduate'] != y['graduate']:
            return False
    return True


def _schedule(schedules, rid, at, modules):
    eligible = [(captured, value) for captured, value in schedules if captured <= at]
    _require(bool(eligible), 'no_captured_preissue_schedule')
    latest = max(captured for captured, _ in eligible)
    rows = [value for captured, value in eligible if captured == latest]
    _require(all(value == rows[0] for value in rows), 'conflicting_preissue_schedule')
    meta = rows[0]
    _require(str(meta.get('kaisai_date')) == rid[:8]
             and str(meta.get('jyo_cd')).zfill(2) == rid[8:10], 'schedule_race_mismatch')
    close = modules['data'].stamp(f'{rid[:4]}-{rid[4:6]}-{rid[6:8]}T{meta["close"]}:00+09:00')
    return meta, close


def _verified_contexts(predictions,schedules,rid,now,modules):
    stamp = modules['data'].stamp
    contexts = []
    if predictions:
        for prediction in predictions:
            issued, close = stamp(prediction['recorded_at']), stamp(prediction['close_at'])
            _require(issued <= stamp(prediction['decision_at']) < close and issued <= now,
                     'invalid_actual_issuance_time')
            meta, schedule_close = _schedule(schedules, rid, issued, modules)
            _require(schedule_close == close, 'prediction_schedule_close_mismatch')
            contexts.append((issued, close, prediction, meta))
    else:
        # Non-forecast history still needs a captured pre-close schedule/card.
        choices = []
        for captured, meta in schedules:
            close = stamp(f'{rid[:4]}-{rid[4:6]}-{rid[6:8]}T{meta["close"]}:00+09:00')
            if captured < close <= now:
                choices.append((captured, close))
        _require(bool(choices), 'no_captured_preclose_schedule')
        _, close = max(choices)
        meta, _ = _schedule(schedules, rid, close, modules)
        contexts.append((close, close, None, meta))
    contexts.sort(key=lambda context: context[0])
    return contexts


def _verify_cards(observation, predictions, cards, schedules, now, modules):
    rid=observation['race_id'];stamp=modules['data'].stamp
    available=stamp(observation['available_at'])
    _require(available<=now,'future_result_observation')
    contexts=_verified_contexts(predictions,schedules,rid,now,modules)
    reference = None
    first_card = None
    source_rows = {row['car']: row for row in observation['rows']}
    _require(len(source_rows) == len(observation['rows']), 'duplicate_result_car')
    for issued, close, prediction, meta in contexts:
        _require(available > close, 'result_not_observed_after_close')
        try:
            card = modules['logger_adapter'].latest_capture(cards, rid, 'entries', issued.isoformat(), 7200)
        except (ValueError, KeyError, TypeError) as error:
            raise _AdmissionError('no_eligible_preissue_card') from error
        entries = card.get('data')
        _require(isinstance(entries, list), 'invalid_captured_card')
        by_car, active, withdrawn, all_ids = {}, {}, set(), set()
        for entry in entries:
            car = _normalize(entry['syaban'])
            _require(re.fullmatch(r'[1-9]', car) and car not in by_car, 'ambiguous_captured_card')
            name = modules['legacy'].canonical_name(entry['name'])
            graduate = _normalize(entry['graduate'])
            _require(bool(name) and graduate.isascii() and graduate.isdigit() and int(graduate) > 0,
                     'ambiguous_captured_identity')
            rider = modules['legacy'].identity(entry['name'], entry['graduate'])
            _require(rider not in all_ids, 'ambiguous_captured_identity')
            all_ids.add(rider)
            status = str(entry.get('sales_sts', ''))
            _require(status in ('', '7', '3'), 'unverified_captured_sale_status')
            by_car[car] = entry
            if status == '3':
                withdrawn.add(car)
            else:
                active[car] = rider
        _require(set(source_rows) <= set(by_car), 'result_card_roster_mismatch')
        result_withdrawn = {car for car, row in source_rows.items() if _outcome(row) == '欠'}
        _require(result_withdrawn <= withdrawn and not (withdrawn & (set(source_rows) - result_withdrawn)),
                 'late_or_unverified_withdrawal')
        if observation['source_type'] == 'nk_result':
            _require(all(any(word in source_rows[car].get('outcome_detail', '') for word in ('欠場', '欠車'))
                         for car in result_withdrawn), 'withdrawal_detail_unverified')
        _require(set(source_rows) - result_withdrawn == set(active), 'result_card_roster_mismatch')
        for car, row in source_rows.items():
            entry = by_car[car]
            _require(modules['legacy'].canonical_name(entry['name']) == row['name']
                     and (row.get('graduate') is None or int(_normalize(entry['graduate'])) == row['graduate']),
                     'result_card_identity_mismatch')
        _require(3 <= len(active) <= 9, 'invalid_captured_active_field')
        if prediction is not None:
            payload = json.loads(prediction['payload'])
            outcomes = payload.get('outcomes')
            _require(isinstance(outcomes, list) and all(isinstance(v, list) and len(v) == 3
                     and all(isinstance(rider, str) for rider in v) for v in outcomes), 'prediction_universe_mismatch')
            actual = {tuple(v) for v in outcomes}
            expected = set(permutations(active.values(), 3))
            _require(len(actual) == len(outcomes) and actual == expected, 'prediction_universe_mismatch')
            _require(modules['data'].digest(payload) == prediction['sha256'], 'prediction_payload_integrity_failed')
        _require(reference is None or active == reference, 'candidate_identity_universes_disagree')
        reference = active
        if first_card is None:
            first_card = by_car, meta
    truth = tuple(reference[car] for car in observation['truth_cars']) if observation['truth_cars'] is not None else None
    return truth, first_card


def _history_event(observation, card_context, history, modules):
    entries, meta = card_context
    rid = observation['race_id']
    when = date(int(rid[:4]), int(rid[4:6]), int(rid[6:8]))
    runners = []
    details = {}
    for value in observation['rows']:
        if _outcome(value) == '欠':
            continue
        entry = entries[value['car']]
        rider = modules['legacy'].identity(entry['name'], entry['graduate'])
        past = [event for event in history.events.get(rider, []) if event[0] < when]
        style = max(past, key=lambda event: event[0])[1].style if past else ''
        cls = unicodedata.normalize('NFKC', str(entry.get('kyu', '')) + str(entry.get('han', '')))
        _require(bool(cls) and bool(entry.get('fuken')), 'incomplete_preissue_history_attributes')
        def number(key):
            try:
                parsed = float(modules['legacy'].number(entry.get(key)))
                return parsed if math.isfinite(parsed) else None
            except (ValueError, TypeError):
                return None
        row = asdict(modules['legacy'].Runner(
            rider, '', str(entry['fuken']), int(_normalize(entry['graduate'])),
            number('age'), number('rating'), None, cls, style,
            value['rank'], _outcome(value), _finish(value), value['car']))
        row.pop('name')
        runners.append(row)
        if value['rank'] is None:
            details[value['car']] = {'status': value['status'], 'outcome_detail': value.get('outcome_detail', '')}
    return {'race_id': rid, 'date': when.isoformat(), 'venue': str(meta['jyo_cd']), 'field_size': len(runners),
            'available_at': observation['available_at'], 'source_sha256': observation['source_sha256'],
            'source_id': observation['source_id'], 'observation_schema_version': 'result-observation-v1',
            'outcome_details': details, 'runners': runners}


def settle_observed_day(bundle, out_dir, extra_path, nk_path, day, *, module_loader, clock=None):
    """Admit source observations using unchanged frozen ledger/history interfaces.

    The caller applies the holdout report guard before publishing summaries.
    This function returns the compatible summaries but never publishes them.
    """
    root, out = Path(bundle), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    _require(now.tzinfo is not None and now.utcoffset() is not None, 'clock_timezone_required')
    manifest = json.loads((root / 'bundle.json').read_text(encoding='utf-8'))
    modules = {name: module_loader(bundle, name) for name in ('data', 'legacy', 'live_data', 'logger_adapter', 'forward', 'settlement')}
    history = modules['live_data'].HistoryPack(root / manifest['history_file'])
    _require(history.sha256 == manifest['history_sha256'], 'history_integrity_failed')
    overlay = out / 'research_history.jsonl.gz'
    existing_history = {}
    for event in _read_gzip(overlay):
        _require(event['race_id'] not in existing_history, 'duplicate_existing_history_race')
        existing_history[event['race_id']] = event
    history.extend(overlay, now.isoformat())
    cohort = manifest.get('cohort') or (root.parent.name if root.name == 'bundle' else root.name)
    report = {'schema_version': 'observed-settlement-report-v1', 'date': day, 'at': now.isoformat(),
              'cohort': cohort, 'new_labels': 0, 'existing_labels': 0, 'history_added': 0,
              'settled': 0, 'history_races': 0, 'unavailable': [], 'excluded': [], 'summaries': {}}
    ledger = modules['forward'].ForwardLedger(out / f'forward_{day}.sqlite', clock=lambda: now)
    sidecar = out / f'forward_observations_{day}.jsonl'
    def receipt(observations, rid, status, reason=None):
        for original in observations or [{'race_id': rid, 'source_type': None, 'source_id': None,
                                          'source_sha256': None, 'available_at': None}]:
            value = dict(original)
            value.update(schema_version='result-observation-v1', race_id=rid, status=status,
                         reason=reason, recorded_at=now.isoformat(), cohort=cohort,
                         adapter_status=original.get('status'))
            with sidecar.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    try:
        ledger.db.row_factory = __import__('sqlite3').Row
        predictions = defaultdict(list)
        for row in ledger.db.execute('SELECT * FROM predictions WHERE race_id LIKE ? ORDER BY recorded_at,model_id', (day + '%',)):
            predictions[row['race_id']].append(dict(row))
        cards, schedules = defaultdict(list), defaultdict(list)
        for record in _read_gzip(out / f'odds_{day}.jsonl.gz'):
            if record.get('type') == 'entries':
                cards[record.get('race_id')].append(record)
            elif record.get('type') == 'racelist':
                captured = modules['logger_adapter'].capture_available_at(record)
                for meta in record.get('data', []):
                    schedules[meta.get('race_id')].append((captured, meta))
        observations = defaultdict(list)
        for record in _read_gzip(extra_path) + _read_gzip(nk_path):
            rid = record.get('race_id')
            if not isinstance(rid, str) or not rid.startswith(day) or record.get('type') not in ('k', 'nk_result'):
                continue
            observation = adapt_nk_result(record) if record['type'] == 'nk_result' else _adapt_k(record, modules)
            observations[rid].append(observation)
        for rid in sorted(set(predictions) | set(observations)):
            original = observations.get(rid, [])
            try:
                _require(bool(original), 'no_result_observation')
                eligible = []
                for observation in original:
                    if observation['status'] == 'invalid':
                        raise _AdmissionError(observation['reason'], 'invalid')
                    _require(modules['data'].stamp(observation['available_at']) <= now, 'future_result_observation')
                    eligible.append(observation)
                final = [o for o in eligible if o['status'] != 'pending']
                if not final:
                    raise _AdmissionError('result_pending', 'pending')
                for pending in (o for o in eligible if o['status'] == 'pending'):
                    receipt([pending], rid, 'pending', pending['reason'])
                _require(all(_same_result(final[0], other) for other in final[1:]),
                         'result_sources_disagree', 'source_conflict')
                chosen = max(final, key=lambda o: (modules['data'].stamp(o['available_at']), o['source_sha256']))
                if chosen['status']=='refund':
                    for observation in final:
                        for _,close,_,_ in _verified_contexts(predictions[rid],schedules[rid],rid,now,modules):
                            _require(modules['data'].stamp(observation['available_at'])>close,'result_not_observed_after_close')
                    raise _AdmissionError(chosen['reason'],'refund')
                contexts = [_verify_cards(observation, predictions[rid], cards[rid], schedules[rid], now, modules)
                            for observation in final]
                if chosen['status'] != 'ready':
                    raise _AdmissionError(chosen['reason'], chosen['status'])
                truth, card_context = contexts[final.index(chosen)]
                _require(all(context[0] == truth for context in contexts), 'result_sources_disagree', 'source_conflict')
                old = ledger.db.execute('SELECT truth FROM labels WHERE race_id=?', (rid,)).fetchone()
                _require(not old or tuple(json.loads(old[0])) == truth,
                         'existing_label_revision_requires_review', 'source_conflict')
                event = _history_event(chosen, card_context, history, modules)
                if rid in existing_history:
                    old_top = sorted((row['rank'], row['rider_id']) for row in existing_history[rid]['runners'] if row['rank'] in (1, 2, 3))
                    _require(tuple(rider for _, rider in old_top) == truth,
                             'existing_history_revision_requires_review', 'source_conflict')
                    def results(event):
                        return {row['car']: (row['rider_id'], row['rank'], row['status'], row.get('finish', ''))
                                for row in event['runners']}
                    _require(results(existing_history[rid]) == results(event),
                             'existing_history_revision_requires_review', 'source_conflict')
                    event = None
                receipt(final, rid, 'ready')
                if predictions[rid]:
                    if old:
                        report['existing_labels'] += 1
                        status = 'already_labelled'
                    else:
                        ledger.settle(rid, truth, available_at=chosen['available_at'],
                                      source_id=chosen['source_id'] + '#sha256=' + chosen['source_sha256'])
                        report['new_labels'] += 1
                        status = 'admitted'
                else:
                    status = 'admitted'  # Validated observed history, without inventing a forecast.
                if event is not None:
                    with gzip.open(overlay, 'at', encoding='utf-8') as stream:
                        stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
                    existing_history[rid] = event
                    report['history_added'] += 1
                receipt(final, rid, status)
            except _AdmissionError as error:
                entry = {'race_id': rid, 'status': error.status, 'reason': str(error)}
                target = 'excluded' if error.status in ('excluded_tie', 'refund') else 'unavailable'
                report[target].append(entry)
                receipt(original, rid, error.status, str(error))
            except (ValueError, KeyError, TypeError) as error:
                # Unknown data-shape failures are visible and cannot admit a label.
                reason = 'invalid_admission_data_' + type(error).__name__
                report['unavailable'].append({'race_id': rid, 'status': 'invalid', 'reason': reason})
                receipt(original, rid, 'invalid', reason)
        for name, model_id in manifest['candidate_ids'].items():
            report['summaries'][name] = ledger.summary(model_id)
    finally:
        ledger.close()
    report['settled'], report['history_races'] = report['new_labels'], report['history_added']
    return report
