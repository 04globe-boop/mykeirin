"""Normalize independently collected NK results into post-close observations.

This module does not read a ledger, infer capture times, settle money, or admit a
label. ``ready`` means a candidate with a unique, payout-consistent top three.
The caller must verify the saved card, graduating terms, observation time and
every actual prediction universe. Withdrawals require that additional check.

The current NK collector drops refund details, including some partial refunds.
Consequently refund completeness is always unproven for this source schema;
neither an empty payout nor an absent refund field means a zero-yen settlement.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
from itertools import permutations
import json
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit


_HEADER = ('着', '枠番', '車番', '選手名', '着差(個人情報)', '上り', '決', 'SB')
_BET_TYPES = frozenset(('2車複', '2車単', 'ワイド', '3連複', '3連単', '枠複', '枠単'))
_VENUES = {
    '11': '函館', '12': '青森', '13': 'いわき平', '21': '弥彦',
    '22': '前橋', '23': '取手', '24': '宇都宮', '25': '大宮',
    '26': '西武園', '27': '京王閣', '28': '立川', '31': '松戸',
    '34': '川崎', '35': '平塚', '36': '小田原', '37': '伊東',
    '38': '静岡', '42': '名古屋', '43': '岐阜', '44': '大垣',
    '45': '豊橋', '46': '富山', '47': '松阪', '48': '四日市',
    '51': '福井', '53': '奈良', '54': '向日町', '55': '和歌山',
    '56': '岸和田', '61': '玉野', '62': '広島', '63': '防府',
    '71': '高松', '73': '小松島', '74': '高知', '75': '松山',
    '81': '小倉', '83': '久留米', '84': '武雄', '85': '佐世保',
    '86': '別府', '87': '熊本',
}
_PREFECTURES = frozenset((
    '北海道 北海 青森 岩手 宮城 秋田 山形 福島 茨城 栃木 群馬 埼玉 千葉 東京 '
    '神奈川 神奈 新潟 富山 石川 福井 山梨 長野 岐阜 静岡 愛知 三重 滋賀 京都 '
    '大阪 兵庫 奈良 和歌山 和歌 鳥取 島根 岡山 広島 山口 徳島 香川 愛媛 高知 '
    '福岡 佐賀 長崎 熊本 大分 宮崎 鹿児島 鹿児 沖縄'
).split())
_RIDER = re.compile(
    r'(?P<name>.+?)\s+(?P<pref>\S+)\s+(?P<age>[0-9]{1,3})歳\s+'
    r'(?P<graduate>[0-9]{1,3})期\s+(?:A[123]|S[12S]|L1)'
)


class _InvalidObservation(ValueError):
    """A fixed, non-sensitive reason suitable for an operational report."""


def _require(condition, reason):
    if not condition:
        raise _InvalidObservation(reason)


def _normalized(value):
    _require(isinstance(value, str), 'invalid_text_cell')
    return ''.join(unicodedata.normalize('NFKC', value).split())


def _validate_provenance(record):
    _require(record.get('type') == 'nk_result', 'invalid_source_type')
    race_id = record.get('race_id')
    _require(isinstance(race_id, str) and re.fullmatch(r'[0-9]{12}', race_id), 'invalid_race_id')
    try:
        race_date = date(int(race_id[:4]), int(race_id[4:6]), int(race_id[6:8]))
    except ValueError as error:
        raise _InvalidObservation('invalid_race_id') from error
    _require(race_id[8:10] in _VENUES and 1 <= int(race_id[10:]) <= 12, 'invalid_race_id')
    captured = record.get('fetched_at')
    _require(isinstance(captured, str), 'invalid_fetched_at')
    try:
        available = datetime.fromisoformat(captured.replace('Z', '+00:00'))
        _require(available.tzinfo is not None and available.utcoffset() is not None, 'timezone_required')
    except ValueError as error:
        if isinstance(error, _InvalidObservation):
            raise
        raise _InvalidObservation('invalid_fetched_at') from error
    url = record.get('url')
    _require(isinstance(url, str), 'invalid_source_url')
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise _InvalidObservation('invalid_source_url') from error
    _require(parts.scheme == 'https' and parts.netloc == 'keirin.netkeiba.com'
             and parts.path == '/race/result/' and not parts.fragment
             and query == {'race_id': [race_id]}, 'source_url_race_mismatch')
    return race_id, race_date


def _validate_title(title, race_id, race_date):
    _require(isinstance(title, str), 'invalid_result_title')
    title = ' '.join(unicodedata.normalize('NFKC', title).split())
    dates = re.findall(r'([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日', title)
    expected = (str(race_date.year), str(race_date.month), str(race_date.day))
    _require(len(dates) == 1 and tuple(str(int(v)) for v in dates[0]) == expected,
             'title_date_mismatch')
    numbers = re.findall(r'(?<![0-9A-Za-z])([0-9]{1,2})R(?![0-9A-Za-z])', title)
    _require(len(numbers) == 1 and int(numbers[0]) == int(race_id[10:]), 'title_race_mismatch')
    _require(title.startswith(_VENUES[race_id[8:10]] + '競輪 '), 'title_venue_mismatch')
    _require('結果・払戻金' in title, 'invalid_result_title')


def _parse_rider(cell):
    _require(isinstance(cell, str), 'invalid_rider_cell')
    match = _RIDER.fullmatch(unicodedata.normalize('NFKC', cell).strip())
    _require(match is not None, 'invalid_rider_cell')
    name = _normalized(match['name'])
    _require(bool(name) and all(unicodedata.category(c)[0] in ('L', 'M') or c == '・'
                               for c in name), 'invalid_rider_name')
    _require(match['pref'] in _PREFECTURES and 1 <= int(match['age']) <= 120, 'invalid_rider_cell')
    graduate = int(match['graduate'])
    _require(graduate > 0, 'invalid_graduate')
    return name, graduate


def _parse_rows(table):
    _require(isinstance(table, list), 'invalid_result_table')
    if not table:
        return []
    _require(isinstance(table[0], list) and tuple(_normalized(v) for v in table[0]) == _HEADER,
             'invalid_result_header')
    _require(len(table) <= 10, 'invalid_field_size')
    rows = []
    seen = set()
    for cells in table[1:]:
        _require(isinstance(cells, list) and len(cells) == len(_HEADER), 'invalid_result_row_width')
        _require(all(isinstance(cell, str) for cell in cells), 'invalid_text_cell')
        status, frame, car = (_normalized(v) for v in cells[:3])
        _require(re.fullmatch(r'[1-6]', frame), 'invalid_frame')
        _require(re.fullmatch(r'[1-9]', car), 'invalid_car')
        _require(car not in seen, 'duplicate_car')
        seen.add(car)
        ranked = re.fullmatch(r'([1-9])着', status)
        _require(ranked is not None or status in ('棄', '失', '欠'), 'invalid_runner_status')
        name, graduate = _parse_rider(cells[3])
        rows.append({'car': car, 'name': name, 'graduate': graduate,
                     'rank': int(ranked[1]) if ranked else None, 'status': status,
                     'finish': _normalized(cells[6]), 'outcome_detail': _normalized(cells[4])})
    return rows


def _parse_payouts(payout):
    _require(isinstance(payout, dict), 'invalid_payout_table')
    trifecta = []
    kinds = set()
    for raw_kind, values in payout.items():
        kind = _normalized(raw_kind)
        _require(kind in _BET_TYPES and kind not in kinds, 'invalid_payout_type')
        kinds.add(kind)
        _require(isinstance(values, list), 'invalid_payout_rows')
        for value in values:
            _require(isinstance(value, list) and len(value) == 3, 'invalid_payout_row')
            combination, yen, popularity = value
            _require(isinstance(combination, str) and bool(combination), 'invalid_payout_combination')
            _require(type(yen) is int and yen > 0, 'invalid_payout_amount')
            _require(popularity is None or (type(popularity) is int and popularity > 0),
                     'invalid_payout_popularity')
            if kind == '3連単':
                combination = _normalized(combination)
                _require(re.fullmatch(r'[1-9]>[1-9]>[1-9]', combination), 'invalid_trifecta_combination')
                cars = combination.split('>')
                _require(len(set(cars)) == 3, 'duplicate_trifecta_car')
                trifecta.append({'cars': cars, 'yen': yen})
    _require(len({tuple(p['cars']) for p in trifecta}) == len(trifecta), 'duplicate_trifecta_payout')
    return trifecta


def _winner_orders(rows):
    ranked = {row['car']: row['rank'] for row in rows if row['rank'] is not None}
    counts = Counter(ranked.values())
    position = 1
    for rank, count in sorted(counts.items()):
        _require(rank == position, 'invalid_rank_sequence')
        position += count
    _require(len(ranked) >= 3, 'incomplete_top_three')
    orders = set()
    for order in permutations(ranked, 3):
        if all(ranked[car] == min(rank for other, rank in ranked.items() if other not in order[:i])
               for i, car in enumerate(order)):
            orders.add(order)
    return orders


def adapt_nk_result(record: dict) -> dict:
    """Return a pure, versioned observation; malformed records return ``invalid``.

    ``source_sha256`` matches the frozen research ``data.digest`` convention:
    sorted-key JSON, UTF-8, ensure_ascii=False, allow_nan=False, default JSON
    separators. ``available_at`` is the original zoned fetched_at, never the
    race time or adaptation time. An unserializable original has no digest.
    """
    observation = {
        'schema_version': 'result-observation-v1', 'race_id': None,
        'source_type': 'nk_result', 'source_id': None, 'source_sha256': None,
        'available_at': None, 'rows': [], 'truth_cars': None, 'payouts': [],
        'status': 'invalid', 'reason': None, 'refund_details_available': False,
        'withdrawn_cars': [], 'requires_withdrawal_verification': False,
    }
    if not isinstance(record, dict):
        observation['reason'] = 'invalid_record'
        return observation
    for target, source in (('race_id', 'race_id'), ('source_id', 'url'), ('available_at', 'fetched_at')):
        if isinstance(record.get(source), str):
            observation[target] = record[source]
    try:
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        observation['reason'] = 'invalid_json_record'
        return observation
    observation['source_sha256'] = hashlib.sha256(raw).hexdigest()
    try:
        race_id, race_date = _validate_provenance(record)
        data = record.get('data')
        _require(isinstance(data, dict), 'invalid_result_data')
        _validate_title(data.get('title'), race_id, race_date)
        status = data.get('status')
        _require(isinstance(status, str) and status in ('確定', '返還', '未確定'), 'invalid_source_status')
        rows = _parse_rows(data.get('result'))
        payouts = _parse_payouts(data.get('payout'))
        observation['rows'] = rows
        observation['payouts'] = payouts
        observation['withdrawn_cars'] = [row['car'] for row in rows if row['status'] == '欠']
        observation['requires_withdrawal_verification'] = bool(observation['withdrawn_cars'])
        if status in ('返還', '未確定'):
            _require(not payouts, 'source_status_payout_conflict')
            observation['status'] = 'refund' if status == '返還' else 'pending'
            observation['reason'] = 'whole_refund' if status == '返還' else 'result_pending'
            return observation
        _require(3 <= len(rows) <= 9, 'invalid_field_size')
        orders = _winner_orders(rows)
        _require(bool(payouts), 'missing_trifecta_payout')
        _require({tuple(p['cars']) for p in payouts} == orders, 'payout_top_three_mismatch')
        if len(orders) > 1:
            observation['status'] = 'excluded_tie'
            observation['reason'] = 'top_three_tie'
        else:
            observation['status'] = 'ready'
            observation['truth_cars'] = list(next(iter(orders)))
        return observation
    except _InvalidObservation as error:
        observation['reason'] = str(error)
        return observation
